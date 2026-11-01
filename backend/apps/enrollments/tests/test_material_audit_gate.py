"""材料域审核状态进入报名校验测试（阶段一，2026-10-31 批准口径）。

批准口径（8 条）：
1. 双条件设计：报名附件证据与材料域审核通过证据分别校验、互不替代；
2. 仅对 requiredMaterialTypes ∩ 材料域字典且 need_audit=true 的类型校验
   （当前唯一交集 STUDENT_STATUS）；
3. PENDING/REJECTED/DRAFT/WITHDRAWN 均不算审核通过；
4. 提交预检 WARNING；终审对已查明但未通过审核的材料 ERROR 阻断；
5. 查询不到记录或查询异常 → 降级 WARNING 口径，不臆造缺失，绝不视为
   审核通过（已知边界：从未上传履历材料者终审不因此阻断，预检由 §2
   WARNING 提示——登记为后续产品决策项）；
6. 不消费 is_required、不改模型默认值、不改变 P2 口径；
7/8. 只查 PERSON 侧（TEAM 映射未定不做；TEAM 类型由 applies_to 预检
   自然降级跳过）。

测试基建：报名侧用 mock 替身生态（per_test_*），材料侧用真实
MaterialType/PersonMaterial 行（同库直连，无需 mock 替身）——真实链路
行为由同库数据驱动，与 person_audit_snapshot（P0-3 决策 A）替身边界
一致；不等价于真实跨域部署验证。
"""

from __future__ import annotations

from apps.common import enums
from apps.common.crypto import encrypt_text, lookup_hash, mask_id_number
from apps.enrollments.models import Enrollment, EnrollmentAttachment, RosterItem
from apps.enrollments.tests.factories import (
    BASE,
    add_material,
    add_player,
    complete_draft,
    create_draft,
    get_detail,
    post_json,
)
from apps.materials.models import MaterialType, PersonMaterial
from apps.persons.models import Person

_person_seq = 0


def make_person(name: str) -> Person:
    """真实 persons 域记录（audit APPROVED，排除人员轨噪音）。"""
    global _person_seq
    _person_seq += 1
    id_number = f"11010120100101{_person_seq % 10000:04d}"
    return Person.objects.create(
        person_no=f"P2027{_person_seq:08d}",
        full_name=name,
        id_card_cipher=encrypt_text(id_number),
        id_card_hash=lookup_hash(id_number),
        id_card_masked=mask_id_number(id_number),
        person_category=enums.PersonCategory.ATHLETE,
        audit_status=enums.AuditStatus.APPROVED,
        status="ACTIVE",
    )


def make_material_type(**overrides) -> MaterialType:
    base = dict(
        code="STUDENT_STATUS",
        name="学籍证明",
        applies_to="PERSON",
        need_audit=True,
    )
    base.update(overrides)
    return MaterialType.objects.create(**base)


def make_material(person: Person, status: str, type_code: str = "STUDENT_STATUS") -> PersonMaterial:
    mtype = MaterialType.objects.get(code=type_code)
    return PersonMaterial.objects.create(
        person=person,
        type=mtype,
        file_id=f"file_mat_{person.pk}_{type_code}",
        file_name="学籍证明.pdf",
        status=status,
    )


def bind_first_player(enrollment_id: str, person: Person) -> None:
    RosterItem.objects.filter(
        enrollment_id=enrollment_id, athlete_id="per_test_00"
    ).update(athlete_id=str(person.pk), athlete_name="")


def prepared_enrollment(client, auth) -> tuple:
    """完整草稿（两类附件齐备）+ 首个名单行绑定真实人员，返回 (id, person)。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    person = make_person("材料球员")
    bind_first_player(enrollment_id, person)
    return enrollment_id, person


def _audit_issues(result) -> list[dict]:
    return [i for i in result["issues"] if i.get("field") == "materialAudit"]


def _version(client, school, enrollment_id: str) -> int:
    return get_detail(client, school, enrollment_id)["version"]


# ---------------------------------------------------------------- 提交预检


def test_precheck_pending_material_warning(client, auth):
    """材料域 PENDING：预检 WARNING 待审核文案，不阻断提交。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()
    make_material(person, "PENDING")

    school = auth("university")
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school,
        {"version": _version(client, school, enrollment_id)},
    )
    result = response.json()["data"]
    issues = _audit_issues(result)
    assert len(issues) == 1, result
    assert issues[0]["code"] == "QUALIFICATION_PENDING"
    assert issues[0]["severity"] == "WARNING"
    assert "STUDENT_STATUS" in issues[0]["message"]
    assert "待学校端审核" in issues[0]["message"]
    assert issues[0]["athleteId"] == str(person.pk)
    assert result["passed"] is True


def test_precheck_rejected_material_warning(client, auth):
    """材料域 REJECTED：预检 WARNING 已驳回文案。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()
    make_material(person, "REJECTED")

    school = auth("university")
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school,
        {"version": _version(client, school, enrollment_id)},
    )
    result = response.json()["data"]
    issues = _audit_issues(result)
    assert len(issues) == 1, result
    assert issues[0]["severity"] == "WARNING"
    assert "已被驳回" in issues[0]["message"]
    assert result["passed"] is True


def test_precheck_withdrawn_material_warning(client, auth):
    """材料域 WITHDRAWN（口径#3 第四种未通过态）：WARNING 不视为通过。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()
    make_material(person, "WITHDRAWN")

    school = auth("university")
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school,
        {"version": _version(client, school, enrollment_id)},
    )
    result = response.json()["data"]
    issues = _audit_issues(result)
    assert len(issues) == 1, result
    assert issues[0]["severity"] == "WARNING"
    assert "未通过学校端审核" in issues[0]["message"]


def test_precheck_approved_material_no_issue(client, auth):
    """材料域 APPROVED：无审核状态 issue，预检通过。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()
    make_material(person, "APPROVED")

    school = auth("university")
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school,
        {"version": _version(client, school, enrollment_id)},
    )
    result = response.json()["data"]
    assert not _audit_issues(result), result
    assert result["passed"] is True


def test_precheck_no_records_degrades_silently(client, auth):
    """口径#5：查得到人但查不到材料记录 → 无审核状态 issue（不臆造）。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()  # 类型存在，但该人无任何 PersonMaterial

    school = auth("university")
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school,
        {"version": _version(client, school, enrollment_id)},
    )
    result = response.json()["data"]
    assert not _audit_issues(result), result


def test_precheck_type_not_in_dict_degrades(client, auth):
    """口径#5：类型不在材料域字典 → 整体降级，无审核状态 issue。"""
    enrollment_id, person = prepared_enrollment(client, auth)  # 不创建 MaterialType
    school = auth("university")
    response = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school,
        {"version": _version(client, school, enrollment_id)},
    )
    result = response.json()["data"]
    assert not _audit_issues(result), result


# ---------------------------------------------------------------- 终审重验


def test_final_review_pending_material_error(client, auth):
    """口径#4：终审发现 PENDING 材料 → MISSING_MATERIAL ERROR 阻断。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()
    make_material(person, "PENDING")

    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    audit = [i for i in issues if i.get("field") == "materialAudit"]
    assert len(audit) == 1, issues
    assert audit[0]["code"] == "MISSING_MATERIAL"
    assert audit[0]["severity"] == "ERROR"
    assert "STUDENT_STATUS" in audit[0]["message"]
    assert "未经学校端审核通过" in audit[0]["message"]
    assert audit[0]["athleteId"] == str(person.pk)


def test_final_review_rejected_material_error(client, auth):
    """口径#4：终审发现 REJECTED 材料 → ERROR 阻断。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()
    make_material(person, "REJECTED")

    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    audit = [i for i in issues if i.get("field") == "materialAudit"]
    assert len(audit) == 1, issues
    assert audit[0]["code"] == "MISSING_MATERIAL"
    assert "已驳回" in audit[0]["message"]


def test_final_review_approved_material_passes(client, auth):
    """终审发现 APPROVED 材料 → 无审核状态 issue。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()
    make_material(person, "APPROVED")

    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    assert not [i for i in issues if i.get("field") == "materialAudit"], issues


def test_final_review_no_records_not_blocked(client, auth):
    """口径#5 已知边界：从未上传履历材料者终审不因此阻断（登记项）。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()  # 类型存在，但无任何材料记录

    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    assert not [i for i in issues if i.get("field") == "materialAudit"], issues


# ---------------------------------------------------------------- 双条件互不替代


def test_attachment_does_not_substitute_audit(client, auth):
    """双条件：报名附件在 + 材料域 REJECTED → 终审仍 ERROR（附件不替代审核）。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()
    make_material(person, "REJECTED")
    # STUDENT_STATUS 报名附件仍在（complete_draft 已加）——存在性重验通过，
    # 但审核状态硬门槛独立触发。

    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    audit = [i for i in issues if i.get("field") == "materialAudit"]
    existence = [
        i
        for i in issues
        if i["code"] == "MISSING_MATERIAL" and i.get("field") == "materialType"
    ]
    assert len(audit) == 1, issues  # 审核未通过 → ERROR
    assert not existence, issues  # 附件存在 → 存在性重验不报


def test_audit_does_not_substitute_attachment(client, auth):
    """双条件：材料域 APPROVED + 报名附件缺失 → 终审仍 MISSING_MATERIAL。"""
    enrollment_id, person = prepared_enrollment(client, auth)
    make_material_type()
    make_material(person, "APPROVED")
    EnrollmentAttachment.objects.filter(
        enrollment_id=enrollment_id, material_type="STUDENT_STATUS"
    ).delete()
    RosterItem.objects.filter(enrollment_id=enrollment_id).update(
        student_status_file_ids=[]
    )

    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    audit = [i for i in issues if i.get("field") == "materialAudit"]
    missing = [
        i
        for i in issues
        if i["code"] == "MISSING_MATERIAL" and i.get("field") == "materialType"
    ]
    assert not audit, issues  # 审核通过 → 无审核状态 issue
    assert len(missing) == 1, issues  # 附件缺失 → 独立 ERROR（互不替代）
    assert "STUDENT_STATUS" in missing[0]["message"]
