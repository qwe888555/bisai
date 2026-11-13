"""报名单 CRUD、数据范围与乐观锁。"""

from __future__ import annotations

import json

from apps.enrollments.tests.factories import (
    BASE,
    GROUP_ID,
    complete_draft,
    create_draft,
    get_detail,
    post_json,
    put_json,
    submit,
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


def test_city_province_admin_cannot_create_or_operate(client, auth):
    """2026-11-01 口径：报送主体=学校端 + 球队端（仅本队）；
    CITY/省级/ADMIN 仍不能新建，也不能操作他人报名单的编辑/撤回。"""
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


def test_team_can_submit_own_team_only(client, auth):
    """球队端可报送本队报名（2026-11-01 用户裁定），跨队一律 DATA_SCOPE_DENIED。"""
    payload = {
        "eventId": "event_01",
        "groupId": GROUP_ID,
        "teamId": "org_team_01",
        "subjectType": "SCHOOL_TEAM",
        "contactName": "王教练",
        "contactPhone": "13800000009",
    }
    team_headers = auth("team")
    created = post_json(client, f"{BASE}/enrollments", team_headers, payload)
    assert created.status_code == 200, created.text
    enrollment = created.json()["data"]
    assert enrollment["teamId"] == "org_team_01"

    # 同队账号可继续维护该报名单（不要求 created_by 一致）
    updated = put_json(
        client,
        f"{BASE}/enrollments/{enrollment['id']}",
        team_headers,
        {**payload, "contactName": "王教练2", "version": enrollment["version"]},
    )
    assert updated.status_code == 200, updated.text

    # 另一支球队既不能为本队建单（teamId 越界），也不能操作他人报名单
    other_team = auth("team2")
    denied = post_json(client, f"{BASE}/enrollments", other_team, payload)
    assert denied.status_code == 403
    assert denied.json()["code"] == "DATA_SCOPE_DENIED"

    crossed = put_json(
        client,
        f"{BASE}/enrollments/{enrollment['id']}",
        other_team,
        {**payload, "version": updated.json()["data"]["version"]},
    )
    assert crossed.status_code == 403

    # 球队端不能以非 SCHOOL_TEAM 主体报送
    bad_subject = post_json(
        client,
        f"{BASE}/enrollments",
        team_headers,
        {**payload, "subjectType": "CITY"},
    )
    assert bad_subject.status_code == 403


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


def test_admin_sees_submitted_but_not_drafts(client, auth):
    """管理员覆盖全库的已提交报名，但草稿只留在报送账号自己那里。"""
    school_a = auth("university")
    school_b = auth("university2")
    submitted_id = complete_draft(client, school_a, team_id="org_team_01")
    submitted = submit(client, school_a, submitted_id, "admin-list-submit")
    assert submitted.status_code == 200, submitted.json()
    draft = create_draft(client, school_b, team_id="org_team_02")

    admin = auth("globaladmin")
    listing = client.get(f"{BASE}/enrollments", **admin)
    assert listing.status_code == 200
    ids = {item["id"] for item in listing.json()["data"]["items"]}
    assert submitted_id in ids
    assert draft["id"] not in ids

    assert client.get(f"{BASE}/enrollments/{submitted_id}", **admin).status_code == 200
    hidden = client.get(f"{BASE}/enrollments/{draft['id']}", **admin)
    assert hidden.status_code == 403
    assert hidden.json()["code"] == "DATA_SCOPE_DENIED"

    # 学校端本人仍能看到并继续维护自己的草稿。
    own = client.get(f"{BASE}/enrollments/{draft['id']}", **school_b)
    assert own.status_code == 200

    stats = client.get(
        f"{BASE}/enrollment-statistics?eventId=event_01&dimension=CITY", **admin
    )
    assert stats.status_code == 200


def test_recorder_reads_but_cannot_write_roster(client, auth):
    """记录台跨队只读已提交报名；名单/附件写操作需创建者或本队（2026-11-04 修复）。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    submitted = submit(client, school, enrollment_id, "recorder-roster-submit")
    assert submitted.status_code == 200, submitted.json()
    recorder = auth("recorder")

    listing = client.get(f"{BASE}/enrollments/{enrollment_id}/players", **recorder)
    assert listing.status_code == 200

    created = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/players",
        recorder,
        {"athleteId": "per_test_00", "jerseyNumber": 7, "position": "DF"},
    )
    assert created.status_code == 403
    assert created.json()["code"] == "FORBIDDEN"

    attachment = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/attachments",
        recorder,
        {"materialType": "STAMPED_FORM", "fileId": "file_not_owned"},
    )
    assert attachment.status_code == 403
    assert attachment.json()["code"] == "FORBIDDEN"
