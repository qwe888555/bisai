"""报名域测试数据构造：走真实接口，不绕过视图/权限/幂等。"""

from __future__ import annotations

import json

from django.core.files.uploadedfile import SimpleUploadedFile

BASE = "/api/v1"
EVENT_ID = "event_01"
GROUP_ID = "group_u15"


def post_json(client, url: str, headers: dict, payload: dict, **extra):
    return client.post(
        url, data=json.dumps(payload), content_type="application/json", **headers, **extra
    )


def put_json(client, url: str, headers: dict, payload: dict):
    return client.put(
        url, data=json.dumps(payload), content_type="application/json", **headers
    )


def create_draft(
    client,
    headers: dict,
    team_id: str = "org_team_01",
    group_id: str = GROUP_ID,
    event_id: str = EVENT_ID,
) -> dict:
    response = post_json(
        client,
        f"{BASE}/enrollments",
        headers,
        {
            "eventId": event_id,
            "groupId": group_id,
            "teamId": team_id,
            "subjectType": "SCHOOL_TEAM",
            "contactName": "张老师",
            "contactPhone": "13800000001",
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def add_player(
    client,
    headers: dict,
    enrollment_id: str,
    athlete_id: str,
    jersey: int,
    position: str = "DF",
) -> dict:
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/players",
        headers,
        {
            "athleteId": athlete_id,
            "jerseyNumber": jersey,
            "position": position,
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def add_material(client, headers: dict, enrollment_id: str, category: str) -> str:
    upload = client.post(
        f"{BASE}/files",
        data={
            "ownerType": "ENROLLMENT",
            "ownerId": enrollment_id,
            "category": category,
            "file": SimpleUploadedFile(
                f"{category}.pdf",
                b"%PDF-1.4\n% test material\n",
                content_type="application/pdf",
            ),
        },
        **headers,
    )
    assert upload.status_code == 200, upload.json()
    file_id = upload.json()["data"]["id"]
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/attachments",
        headers,
        {"materialType": category, "fileId": file_id},
    )
    assert response.status_code == 200, response.json()
    return response.json()["data"]["id"]


def complete_draft(
    client,
    headers: dict,
    players: int = 5,
    materials: bool = True,
    team_id: str = "org_team_01",
    group_id: str = GROUP_ID,
    event_id: str = EVENT_ID,
) -> str:
    enrollment = create_draft(client, headers, team_id, group_id, event_id)
    for index in range(players):
        add_player(
            client,
            headers,
            enrollment["id"],
            f"per_test_{index:02d}",
            jersey=index + 1,
            position="GK" if index == 0 else "DF",
        )
    if materials:
        add_material(client, headers, enrollment["id"], "STAMPED_FORM")
        add_material(client, headers, enrollment["id"], "STUDENT_STATUS")
    return enrollment["id"]


def get_detail(client, headers: dict, enrollment_id: str) -> dict:
    response = client.get(f"{BASE}/enrollments/{enrollment_id}", **headers)
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def submit(client, headers: dict, enrollment_id: str, key: str):
    version = get_detail(client, headers, enrollment_id)["version"]
    return post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/submit",
        headers,
        {"version": version},
        HTTP_IDEMPOTENCY_KEY=key,
    )


def review(client, headers: dict, enrollment_id: str, decision: str, key: str, reason: str = ""):
    version = get_detail(client, headers, enrollment_id)["version"]
    return post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/reviews",
        headers,
        {"version": version, "decision": decision, "reason": reason},
        HTTP_IDEMPOTENCY_KEY=key,
    )


def approve_flow(
    client,
    team_headers: dict,
    city_headers: dict,
    province_headers: dict,
    enrollment_id: str,
    key_prefix: str,
) -> dict:
    response = submit(client, team_headers, enrollment_id, f"{key_prefix}-submit")
    assert response.status_code == 200, response.json()
    response = review(client, city_headers, enrollment_id, "APPROVE", f"{key_prefix}-city")
    assert response.status_code == 200, response.json()
    response = review(
        client, province_headers, enrollment_id, "APPROVE", f"{key_prefix}-prov"
    )
    assert response.status_code == 200, response.json()
    return response.json()["data"]
