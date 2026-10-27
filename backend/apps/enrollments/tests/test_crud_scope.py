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


def test_team_can_create_only_own_team(client, auth):
    """球队端现在可以建自己球队的报名单，但不能报主体类型错的。"""
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
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["teamId"] == "org_team_01"

    # subjectType 不匹配也会被拦
    wrong_type = post_json(
        client,
        f"{BASE}/enrollments",
        headers,
        {**payload, "subjectType": "UNIVERSITY"},
    )
    assert wrong_type.status_code == 403


def test_non_team_or_university_cannot_create_or_operate(client, auth):
    """CITY/PROVINCE_REVIEWER/ADMIN 代报未放开（TODO 确认项#6），
    仍不能新建、也不能操作他人报名单的新建/编辑/提交/撤回路径。"""
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
    for username in ("city", "province", "admin"):
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
