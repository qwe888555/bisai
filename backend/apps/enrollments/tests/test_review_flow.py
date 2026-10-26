"""提交、审核状态机、幂等与预检阻断。"""

from __future__ import annotations

import json

from apps.enrollments.tests.factories import (
    BASE,
    approve_flow,
    complete_draft,
    get_detail,
    post_json,
    review,
    submit,
)


def test_submit_blocked_by_precheck_missing_material(client, auth):
    school_headers = auth("university")
    enrollment_id = complete_draft(client, school_headers, materials=False)
    version = get_detail(client, school_headers, enrollment_id)["version"]

    precheck = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school_headers,
        {"version": version},
    )
    assert precheck.status_code == 200
    result = precheck.json()["data"]
    assert result["passed"] is False
    codes = {issue["code"] for issue in result["issues"]}
    assert "MISSING_MATERIAL" in codes

    response = submit(client, school_headers, enrollment_id, "missing-material-key")
    assert response.status_code == 422
    assert response.json()["code"] == "BUSINESS_VALIDATION_FAILED"


def test_city_then_province_approve_and_snapshot_frozen(client, auth):
    school_headers = auth("university")
    city_headers = auth("city")
    province_headers = auth("province")
    enrollment_id = complete_draft(client, school_headers)

    approved = approve_flow(
        client, school_headers, city_headers, province_headers, enrollment_id, "flow"
    )
    assert approved["status"] == "APPROVED"
    assert approved["editable"] is False
    assert approved["submissionNo"] == 1
    snapshot_id = approved["snapshotId"]
    assert snapshot_id

    history = client.get(f"{BASE}/enrollments/{enrollment_id}/reviews", **school_headers)
    assert history.status_code == 200
    actions = [item["action"] for item in history.json()["data"]["items"]]
    assert actions == ["APPROVE", "APPROVE", "SUBMIT"]

    snapshot = client.get(
        f"{BASE}/enrollments/{enrollment_id}/snapshots/{snapshot_id}", **school_headers
    )
    assert snapshot.status_code == 200
    payload = snapshot.json()["data"]
    assert payload["submissionNo"] == 1
    assert len(payload["players"]) == 5
    assert payload["enrollment"]["status"] == "PENDING_CITY_REVIEW"

    # 已通过后不可编辑、不可重复提交
    version = get_detail(client, school_headers, enrollment_id)["version"]
    update = client.put(
        f"{BASE}/enrollments/{enrollment_id}",
        data=json.dumps(
            {
                "eventId": "event_01",
                "groupId": "group_u15",
                "teamId": "org_team_01",
                "subjectType": "SCHOOL_TEAM",
                "contactName": "新联系人",
                "contactPhone": "13800000003",
                "version": version,
            }
        ),
        content_type="application/json",
        **school_headers,
    )
    assert update.status_code == 409
    resubmit = submit(client, school_headers, enrollment_id, "resubmit-after-approve")
    assert resubmit.status_code == 409
    assert resubmit.json()["code"] == "STATE_CONFLICT"


def test_city_cannot_review_province_stage(client, auth):
    school_headers = auth("university")
    city_headers = auth("city")
    enrollment_id = complete_draft(client, school_headers)

    assert submit(client, school_headers, enrollment_id, "stage-submit").status_code == 200
    first = review(client, city_headers, enrollment_id, "APPROVE", "stage-city")
    assert first.status_code == 200
    assert first.json()["data"]["status"] == "PENDING_PROVINCE_REVIEW"

    again = review(client, city_headers, enrollment_id, "APPROVE", "stage-city-2")
    assert again.status_code == 403
    assert again.json()["code"] == "FORBIDDEN"

    province_headers = auth("province")
    province = review(
        client, province_headers, enrollment_id, "APPROVE", "stage-prov"
    )
    assert province.status_code == 200
    assert province.json()["data"]["status"] == "APPROVED"


def test_withdraw_pending_and_cannot_withdraw_approved(client, auth):
    school_headers = auth("university")
    city_headers = auth("city")
    province_headers = auth("province")
    enrollment_id = complete_draft(client, school_headers)
    assert submit(client, school_headers, enrollment_id, "withdraw-submit").status_code == 200

    version = get_detail(client, school_headers, enrollment_id)["version"]
    withdrawn = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/withdraw",
        school_headers,
        {"version": version, "reason": "材料需要补充"},
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["data"]["status"] == "WITHDRAWN"
    assert withdrawn.json()["data"]["editable"] is True

    again = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/withdraw",
        school_headers,
        {
            "version": withdrawn.json()["data"]["version"],
            "reason": "再次撤回",
        },
    )
    assert again.status_code == 409

    # 重新提交后走完整审批，已通过不可撤回
    second = complete_draft(
        client, school_headers, team_id="org_team_01", group_id="group_u12", players=5
    )
    approve_flow(
        client, school_headers, city_headers, province_headers, second, "approved"
    )
    approved_version = get_detail(client, school_headers, second)["version"]
    response = post_json(
        client,
        f"{BASE}/enrollments/{second}/withdraw",
        school_headers,
        {"version": approved_version, "reason": "已通过后撤回"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"


def test_submit_idempotency_replay_and_conflict(client, auth):
    school_headers = auth("university")
    enrollment_id = complete_draft(client, school_headers)
    version = get_detail(client, school_headers, enrollment_id)["version"]
    payload = {"version": version}

    first = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/submit",
        school_headers,
        payload,
        HTTP_IDEMPOTENCY_KEY="same-key-0001",
    )
    assert first.status_code == 200
    replay = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/submit",
        school_headers,
        payload,
        HTTP_IDEMPOTENCY_KEY="same-key-0001",
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["submissionNo"] == first.json()["data"]["submissionNo"]

    conflict = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/submit",
        school_headers,
        {"version": version + 1},
        HTTP_IDEMPOTENCY_KEY="same-key-0001",
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_precheck_reports_outside_window(client, auth, monkeypatch):
    school_headers = auth("university")
    enrollment_id = complete_draft(client, school_headers)
    version = get_detail(client, school_headers, enrollment_id)["version"]

    monkeypatch.setenv("ENROLLMENT_MOCK_WINDOW_OPEN", "2099-01-01T00:00:00+08:00")
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school_headers,
        {"version": version},
    )
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["passed"] is False
    assert "OUTSIDE_WINDOW" in {issue["code"] for issue in result["issues"]}
