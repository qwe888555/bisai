"""报名材料预检与终审重验一致性测试（方案 B，2026-10-31）。

覆盖：
- 提交预检：每种必需材料类型单独缺失、全部缺失 → MISSING_MATERIAL
  ERROR 阻断提交（批准行为不变）；
- 学籍证明可由名单行 student_status_file_ids 承载（预检与终审口径一致，
  修复 double_track_issues 原先只查报名附件的比对差异）；
- 预检：未知/无效 requiredMaterialTypes 配置 → QUALIFICATION_PENDING
  WARNING 软提示，不阻断提交；
- 终审：未知类型 fail-closed ERROR（无报名附件承载无法核验）。

is_required 口径声明（P2）：报名必备项仅由 requiredMaterialTypes 控制，
本域不消费 materials.models 的 is_required 字段，本文件测试不涉及该字段。
"""

from __future__ import annotations

from apps.enrollments.models import (
    Enrollment,
    EnrollmentAttachment,
    RosterItem,
)
from apps.enrollments.tests.factories import (
    BASE,
    add_material,
    add_player,
    complete_draft,
    create_draft,
    get_detail,
    post_json,
    submit,
)


def _rules(**overrides):
    base = {
        "minPlayers": 5,
        "maxPlayers": 25,
        "minGoalkeepers": 1,
        "birthDateFrom": "2008-01-01",
        "birthDateTo": "2012-12-31",
        "requiredMaterialTypes": ["STAMPED_FORM", "STUDENT_STATUS"],
        "confirmed": True,
    }
    base.update(overrides)
    return base


def _patch_rules(monkeypatch, rules):
    monkeypatch.setattr(
        "apps.enrollments.clients.EventConfigClient.get_group_rules",
        lambda self, event_id, group_id: rules,
    )


def _missing_material_issues(result) -> list[dict]:
    return [i for i in result["issues"] if i["code"] == "MISSING_MATERIAL"]


# ---------------------------------------------------------------- 提交预检


def test_precheck_missing_stamped_form_only(client, auth):
    """只缺 STAMPED_FORM：ERROR 阻断提交，消息指向报名表材料。"""
    school = auth("university")
    enrollment = create_draft(client, school)
    for index in range(5):
        add_player(client, school, enrollment["id"], f"per_test_{index:02d}", index + 1)
    add_material(client, school, enrollment["id"], "STUDENT_STATUS")

    version = get_detail(client, school, enrollment["id"])["version"]
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment['id']}/precheck",
        school,
        {"version": version},
    )
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["passed"] is False
    missing = _missing_material_issues(result)
    assert len(missing) == 1
    assert missing[0]["severity"] == "ERROR"
    assert "报名表" in missing[0]["message"]

    blocked = submit(client, school, enrollment["id"], "missing-stamped-key")
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "BUSINESS_VALIDATION_FAILED"


def test_precheck_missing_student_status_only(client, auth):
    """只缺 STUDENT_STATUS：ERROR 阻断提交，消息指向学籍证明材料。"""
    school = auth("university")
    enrollment = create_draft(client, school)
    for index in range(5):
        add_player(client, school, enrollment["id"], f"per_test_{index:02d}", index + 1)
    add_material(client, school, enrollment["id"], "STAMPED_FORM")

    version = get_detail(client, school, enrollment["id"])["version"]
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment['id']}/precheck",
        school,
        {"version": version},
    )
    result = response.json()["data"]
    assert result["passed"] is False
    missing = _missing_material_issues(result)
    assert len(missing) == 1
    assert missing[0]["severity"] == "ERROR"
    assert "学籍" in missing[0]["message"]

    blocked = submit(client, school, enrollment["id"], "missing-student-key")
    assert blocked.status_code == 422


def test_precheck_missing_all_materials(client, auth):
    """两类必需材料全部缺失：两条 MISSING_MATERIAL ERROR，阻断提交。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school, materials=False)
    version = get_detail(client, school, enrollment_id)["version"]

    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school,
        {"version": version},
    )
    result = response.json()["data"]
    assert result["passed"] is False
    missing = _missing_material_issues(result)
    assert len(missing) == 2
    assert all(i["severity"] == "ERROR" for i in missing)
    messages = " ".join(i["message"] for i in missing)
    assert "报名表" in messages and "学籍" in messages

    blocked = submit(client, school, enrollment_id, "missing-both-key")
    assert blocked.status_code == 422


def test_precheck_student_status_via_roster_file_ids(client, auth):
    """学籍证明由名单行 student_status_file_ids 承载时预检不报缺失。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    # 撤掉学籍证明附件，改为名单行文件 ID 承载
    EnrollmentAttachment.objects.filter(
        enrollment_id=enrollment_id, material_type="STUDENT_STATUS"
    ).delete()
    RosterItem.objects.filter(enrollment_id=enrollment_id).update(
        student_status_file_ids=["file_std_01"]
    )

    version = get_detail(client, school, enrollment_id)["version"]
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school,
        {"version": version},
    )
    result = response.json()["data"]
    assert not _missing_material_issues(result), result
    assert result["passed"] is True


def test_precheck_unknown_material_type_is_warning(client, auth, monkeypatch):
    """无效配置类型：QUALIFICATION_PENDING WARNING 软提示，不阻断提交。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    _patch_rules(
        monkeypatch,
        _rules(requiredMaterialTypes=["STAMPED_FORM", "STUDENT_STATUS", "IDENTITY_PHOTO"]),
    )

    version = get_detail(client, school, enrollment_id)["version"]
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school,
        {"version": version},
    )
    result = response.json()["data"]
    # 已知两类材料齐备 → 无缺失；未知类型仅 WARNING
    assert not _missing_material_issues(result), result
    warnings = [
        i
        for i in result["issues"]
        if i["code"] == "QUALIFICATION_PENDING" and i["severity"] == "WARNING"
        and "IDENTITY_PHOTO" in i["message"]
    ]
    assert warnings, result
    assert result["passed"] is True


# ---------------------------------------------------------------- 终审重验


def test_final_review_student_status_via_roster_file_ids(client, auth):
    """比对差异修复：学籍证明仅由名单行文件 ID 承载时终审不误报缺失。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    EnrollmentAttachment.objects.filter(
        enrollment_id=enrollment_id, material_type="STUDENT_STATUS"
    ).delete()
    RosterItem.objects.filter(enrollment_id=enrollment_id).update(
        student_status_file_ids=["file_std_01"]
    )

    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    assert not [i for i in issues if i["code"] == "MISSING_MATERIAL"], issues


def test_final_review_missing_material_still_error(client, auth):
    """两类材料确缺失时终审仍 ERROR（批准行为不变）。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    EnrollmentAttachment.objects.filter(
        enrollment_id=enrollment_id, material_type="STUDENT_STATUS"
    ).delete()
    RosterItem.objects.filter(enrollment_id=enrollment_id).update(
        student_status_file_ids=[]
    )

    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    missing = [i for i in issues if i["code"] == "MISSING_MATERIAL"]
    assert len(missing) == 1
    assert missing[0]["severity"] == "ERROR"
    assert "STUDENT_STATUS" in missing[0]["message"]


def test_final_review_unknown_type_fails_closed(client, auth, monkeypatch):
    """无效配置类型在终审 fail-closed：MISSING_MATERIAL ERROR 不放行。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    _patch_rules(
        monkeypatch,
        _rules(requiredMaterialTypes=["STAMPED_FORM", "STUDENT_STATUS", "BOGUS_TYPE"]),
    )

    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    missing = [i for i in issues if i["code"] == "MISSING_MATERIAL"]
    assert len(missing) == 1
    assert missing[0]["severity"] == "ERROR"
    assert "BOGUS_TYPE" in missing[0]["message"]
