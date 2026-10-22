"""报名单 CRUD、数据范围与乐观锁。"""

from __future__ import annotations

import json

from apps.enrollments.tests.factories import (
    BASE,
    GROUP_ID,
    create_draft,
    get_detail,
    put_json,
)


def test_create_masks_phone_and_get(client, auth):
    headers = auth("team")
    enrollment = create_draft(client, headers)

    assert enrollment["status"] == "DRAFT"
    assert enrollment["editable"] is True
    assert enrollment["maskedContactPhone"] == "138****0001"
    assert "contactPhone" not in enrollment

    detail = get_detail(client, headers, enrollment["id"])
    assert detail["contactName"] == "张老师"
    assert detail["players"] == []


def test_duplicate_active_enrollment_rejected(client, auth):
    headers = auth("team")
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


def test_cross_team_access_denied(client, auth):
    team_headers = auth("team")
    other_headers = auth("team2")
    enrollment = create_draft(client, team_headers)

    response = client.get(f"{BASE}/enrollments/{enrollment['id']}", **other_headers)
    assert response.status_code == 403
    assert response.json()["code"] == "DATA_SCOPE_DENIED"

    listing = client.get(f"{BASE}/enrollments", **other_headers)
    assert listing.status_code == 200
    assert listing.json()["data"]["items"] == []


def test_update_with_stale_version_conflicts(client, auth):
    headers = auth("team")
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
    headers = auth("team")
    enrollment = create_draft(client, headers)

    response = client.delete(
        f"{BASE}/enrollments/{enrollment['id']}?version={enrollment['version']}",
        **headers,
    )
    assert response.status_code == 200
    assert response.json()["data"]["success"] is True

    follow_up = client.get(f"{BASE}/enrollments/{enrollment['id']}", **headers)
    assert follow_up.status_code == 404


def test_form_config_rejects_escalated_subject_type(client, auth):
    headers = auth("team")
    response = client.get(
        f"{BASE}/enrollment-form-config?eventId=event_01&subjectType=CITY", **headers
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
