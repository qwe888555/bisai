"""报名单 CRUD、数据范围与乐观锁。"""

from __future__ import annotations

import json

from apps.enrollments.tests.factories import (
    BASE,
    GROUP_ID,
    create_draft,
    get_detail,
    post_json,
    put_json,
)


def test_create_masks_phone_and_get(client, auth):
    headers = auth("university")
    enrollment = create_draft(client, headers)

    assert enrollment["status"] == "DRAFT"
    assert enrollment["editable"] is True
    assert enrollment["maskedContactPhone"] == "138****0001"
    assert "contactPhone" not in enrollment

    detail = get_detail(client, headers, enrollment["id"])
    assert detail["contactName"] == "张老师"
    assert detail["players"] == []


def test_duplicate_active_enrollment_rejected(client, auth):
    headers = auth("university")
    create_draft(client, headers)

    response = client.post(
        f"{BASE}/enrollments",
        data=json.dumps(
            {
                "eventId": "event_01",
                "groupId": GROUP_ID,
                "teamId": "org_team_01",
                "subjectType": "SCHOOL_TEAM",
                "contactName": "李老师",
                "contactPhone": "13800000002",
            }
        ),
        content_type="application/json",
        **headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DUPLICATE_RESOURCE"


def test_cross_school_access_denied(client, auth):
    school_headers = auth("university")
    other_headers = auth("university2")
    enrollment = create_draft(client, school_headers)

    response = client.get(f"{BASE}/enrollments/{enrollment['id']}", **other_headers)
    assert response.status_code == 403
    assert response.json()["code"] == "DATA_SCOPE_DENIED"

    listing = client.get(f"{BASE}/enrollments", **other_headers)
    assert listing.status_code == 200
    assert listing.json()["data"]["items"] == []


def test_team_cannot_create_enrollment(client, auth):
    """球队不能报送（R1 仅 UNIVERSITY 可建报名单），任何主体类型均 403。"""
    headers = auth("team")
    payload = {
        "eventId": "event_01",
        "groupId": GROUP_ID,
        "teamId": "org_team_01",
        "subjectType": "SCHOOL_TEAM",
        "contactName": "刘教练",
        "contactPhone": "13800000001",
    }
    response = post_json(client, f"{BASE}/enrollments", headers, payload)
    assert response.status_code == 403, response.json()
    assert response.json()["code"] == "FORBIDDEN"

    # 改报主体类型也仍然 403（非学校端一律不可报送）
    wrong_type = post_json(
        client,
        f"{BASE}/enrollments",
        headers,
        {**payload, "subjectType": "UNIVERSITY"},
    )
    assert wrong_type.status_code == 403


def test_non_university_cannot_create_or_operate(client, auth):
    """CITY/PROVINCE_REVIEWER/ADMIN/TEAM 均不能代报或操作他人报名单
    （R1 仅 UNIVERSITY 可建，数据范围层再拦跨校操作）。"""
    school_headers = auth("university")
    enrollment = create_draft(client, school_headers)
    url = f"{BASE}/enrollments"
    payload = {
        "eventId": "event_01",
        "groupId": GROUP_ID,
        "teamId": "org_team_01",
        "subjectType": "SCHOOL_TEAM",
        "contactName": "张老师",
        "contactPhone": "13800000001",
    }
    for username in ("team", "city", "province", "admin"):
        headers = auth(username)
        created = post_json(client, url, headers, payload)
        assert created.status_code == 403, username
        assert created.json()["code"] == "FORBIDDEN"

        updated = put_json(
            client,
            f"{BASE}/enrollments/{enrollment['id']}",
            headers,
            {**payload, "version": enrollment["version"]},
        )
        assert updated.status_code == 403, username

        withdrawn = post_json(
            client,
            f"{BASE}/enrollments/{enrollment['id']}/withdraw",
            headers,
            {"version": enrollment["version"], "reason": "代撤回"},
        )
        assert withdrawn.status_code == 403, username

    # 另一所学校也不能操作非本人创建的报名单（数据范围层先拦）
    other_school = auth("university2")
    operated = put_json(
        client,
        f"{BASE}/enrollments/{enrollment['id']}",
        other_school,
        {**payload, "version": enrollment["version"]},
    )
    assert operated.status_code == 403
    assert operated.json()["code"] == "DATA_SCOPE_DENIED"


def test_update_with_stale_version_conflicts(client, auth):
    headers = auth("university")
    enrollment = create_draft(client, headers)

    response = put_json(
        client,
        f"{BASE}/enrollments/{enrollment['id']}",
        headers,
        {
            "eventId": "event_01",
            "groupId": GROUP_ID,
            "teamId": "org_team_01",
            "subjectType": "SCHOOL_TEAM",
            "contactName": "李老师",
            "contactPhone": "13800000002",
            "version": 99,
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "VERSION_CONFLICT"

    current = get_detail(client, headers, enrollment["id"])
    response = put_json(
        client,
        f"{BASE}/enrollments/{enrollment['id']}",
        headers,
        {
            "eventId": "event_01",
            "groupId": GROUP_ID,
            "teamId": "org_team_01",
            "subjectType": "SCHOOL_TEAM",
            "contactName": "李老师",
            "contactPhone": "13800000002",
            "version": current["version"],
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["contactName"] == "李老师"
    assert response.json()["data"]["version"] == current["version"] + 1


def test_delete_draft_is_soft_and_hidden(client, auth):
    headers = auth("university")
    enrollment = create_draft(client, headers)

    response = client.delete(
        f"{BASE}/enrollments/{enrollment['id']}?version={enrollment['version']}",
        **headers,
    )
    assert response.status_code == 200
    assert response.json()["data"]["success"] is True

    follow_up = client.get(f"{BASE}/enrollments/{enrollment['id']}", **headers)
    assert follow_up.status_code == 404


def test_form_config_rejects_non_university(client, auth):
    """表单配置仅对学校端开放（TEAM/CITY/省级/ADMIN 均 403）。"""
    for username in ("team", "city", "province", "admin"):
        headers = auth(username)
        response = client.get(
            f"{BASE}/enrollment-form-config?eventId=event_01&subjectType=CITY",
            **headers,
        )
        assert response.status_code == 403, username
        assert response.json()["code"] == "FORBIDDEN"
