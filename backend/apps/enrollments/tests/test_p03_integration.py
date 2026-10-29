"""P0-3 跨域集成测试（2026-10-28 决策 A：报名域同进程直连 persons 域）。

与既有 mock 测试（test_review_flow.py 等，athlete_id=per_test_* 测试替身）
的边界：本文件为真实 Person 记录的真实跨模块集成验证——RosterItem.athlete_id
绑定真实 Person 主键，person_audit_snapshot / double_track_issues 实时查库。

覆盖（用户验收清单）：
- 提交阶段：档案未通过审核 → QUALIFICATION_PENDING WARNING，不阻断提交；
- 省级终审：档案 PENDING/DISABLED → 422 ERROR 阻断；全 APPROVED → 通过；
- 提交后材料被删 → 终审 422 MISSING_MATERIAL（实时重验，不依赖预检缓存）；
- EXCLUDED 名单条目不参与人员轨校验；
- 跨校隔离（越权访问）。

材料边界声明：requiredMaterialTypes 两类（STAMPED_FORM/STUDENT_STATUS）均为
报名附件体系（EnrollmentAttachment），非材料域审核对象。
"""

from __future__ import annotations

from apps.common import enums
from apps.common.crypto import encrypt_text, lookup_hash, mask_id_number
from apps.enrollments.models import (
    QUALIFICATION_EXCLUDED,
    EnrollmentAttachment,
    RosterItem,
)
from apps.enrollments.tests.factories import (
    BASE,
    complete_draft,
    get_detail,
    post_json,
    review,
    submit,
)
from apps.persons.models import Person

_person_seq = 0


def make_person(name: str, audit_status: str, status: str = "ACTIVE") -> Person:
    """真实 persons 域记录（crypto 三件套口径与 persons 域测试一致）。"""
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
        audit_status=audit_status,
        status=status,
    )


def bind_player(enrollment_id: str, mock_athlete_id: str, person: Person) -> None:
    """把 mock 测试替身 id 换绑为真实 Person 主键（测试数据准备，非业务写链）。

    athlete_name 一并清空：add_player 时的姓名来自 person_client mock（按 id
    派生），换绑后 mock 名与真实档案名无对应关系；姓名一致性校验在 mock 替身
    下不具验证意义（athlete_name 为空时 precheck 跳过该项），真实姓名核对
    属真实 PersonClient 接通后的集成范围。
    """
    RosterItem.objects.filter(
        enrollment_id=enrollment_id, athlete_id=mock_athlete_id
    ).update(athlete_id=str(person.pk), athlete_name="")


def _issues(payload) -> list[dict]:
    data = payload.json()["data"]
    return data if isinstance(data, list) else data.get("issues", [])


def test_submit_warns_but_not_blocked_for_pending_person(client, auth):
    school_headers = auth("university")
    enrollment_id = complete_draft(client, school_headers)
    pending = make_person("待审球员", enums.AuditStatus.PENDING)
    bind_player(enrollment_id, "per_test_00", pending)

    version = get_detail(client, school_headers, enrollment_id)["version"]
    precheck = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        school_headers,
        {"version": version},
    )
    assert precheck.status_code == 200
    result = precheck.json()["data"]
    # 提交阶段：WARNING 不阻断（passed=True，无 ERROR）
    assert result["passed"] is True
    warnings = [
        i
        for i in result["issues"]
        if i["severity"] == "WARNING" and i["code"] == "QUALIFICATION_PENDING"
    ]
    matched = [w for w in warnings if w.get("athleteId") == str(pending.pk)]
    assert matched, result["issues"]
    assert matched[0]["field"] == "auditStatus"
    assert "PENDING" in matched[0]["message"]

    # 提交成功（WARNING 不产生 422）
    response = submit(client, school_headers, enrollment_id, "p03-warn-submit")
    assert response.status_code == 200, response.json()


def test_province_review_blocks_pending_person(client, auth):
    school_headers = auth("university")
    city_headers = auth("city")
    province_headers = auth("province")
    enrollment_id = complete_draft(client, school_headers)
    pending = make_person("终审待审", enums.AuditStatus.PENDING)
    bind_player(enrollment_id, "per_test_02", pending)

    assert submit(client, school_headers, enrollment_id, "p03-block-submit").status_code == 200
    assert review(client, city_headers, enrollment_id, "APPROVE", "p03-block-city").status_code == 200

    # 省级终审：实时直连 persons 域 → ERROR 阻断（不依赖提交时预检结果）
    blocked = review(client, province_headers, enrollment_id, "APPROVE", "p03-block-prov")
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "BUSINESS_VALIDATION_FAILED"
    hit = [
        i
        for i in _issues(blocked)
        if i["code"] == "PLAYER_LIMIT"
        and i["field"] == "auditStatus"
        and i.get("athleteId") == str(pending.pk)
    ]
    assert hit, blocked.json()
    assert "PENDING" in hit[0]["message"]


def test_province_review_passes_with_all_approved_persons(client, auth):
    school_headers = auth("university")
    city_headers = auth("city")
    province_headers = auth("province")
    enrollment_id = complete_draft(client, school_headers)
    for index in range(5):
        approved = make_person(f"已审球员{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)

    assert submit(client, school_headers, enrollment_id, "p03-pass-submit").status_code == 200
    assert review(client, city_headers, enrollment_id, "APPROVE", "p03-pass-city").status_code == 200
    approved_response = review(
        client, province_headers, enrollment_id, "APPROVE", "p03-pass-prov"
    )
    assert approved_response.status_code == 200, approved_response.json()
    assert approved_response.json()["data"]["status"] == "APPROVED"


def test_province_review_rechecks_materials_after_deletion(client, auth):
    """提交时材料齐全，提交后删除 → 终审实时重验 MISSING_MATERIAL 阻断。"""
    school_headers = auth("university")
    city_headers = auth("city")
    province_headers = auth("province")
    enrollment_id = complete_draft(client, school_headers)
    for index in range(5):
        approved = make_person(f"材料球员{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)

    assert submit(client, school_headers, enrollment_id, "p03-del-submit").status_code == 200

    EnrollmentAttachment.objects.filter(
        enrollment_id=enrollment_id, material_type="STUDENT_STATUS"
    ).delete()

    assert review(client, city_headers, enrollment_id, "APPROVE", "p03-del-city").status_code == 200
    blocked = review(client, province_headers, enrollment_id, "APPROVE", "p03-del-prov")
    assert blocked.status_code == 422
    assert blocked.json()["code"] == "BUSINESS_VALIDATION_FAILED"
    assert any(i["code"] == "MISSING_MATERIAL" for i in _issues(blocked)), blocked.json()


def test_province_review_blocks_disabled_person(client, auth):
    school_headers = auth("university")
    city_headers = auth("city")
    province_headers = auth("province")
    enrollment_id = complete_draft(client, school_headers)
    disabled = make_person(
        "禁用球员", enums.AuditStatus.APPROVED, status=enums.EnableStatus.DISABLED
    )
    bind_player(enrollment_id, "per_test_01", disabled)

    assert submit(client, school_headers, enrollment_id, "p03-dis-submit").status_code == 200
    assert review(client, city_headers, enrollment_id, "APPROVE", "p03-dis-city").status_code == 200
    blocked = review(client, province_headers, enrollment_id, "APPROVE", "p03-dis-prov")
    assert blocked.status_code == 422
    hit = [
        i
        for i in _issues(blocked)
        if i["field"] == "personStatus" and i.get("athleteId") == str(disabled.pk)
    ]
    assert hit, blocked.json()


def test_excluded_roster_item_skips_person_audit(client, auth):
    """EXCLUDED 条目不参与人员轨校验：PENDING 档案被剔除后终审可通过。"""
    school_headers = auth("university")
    city_headers = auth("city")
    province_headers = auth("province")
    enrollment_id = complete_draft(client, school_headers, players=6)
    # 6 人中 1 人 PENDING 且被剔除，其余 5 人 APPROVED（满足 min_players=5）；
    # 剔除对象原为唯一守门员（index 0），守门员职责转移给活跃球员以隔离变量
    pending = make_person("被剔除球员", enums.AuditStatus.PENDING)
    bind_player(enrollment_id, "per_test_00", pending)
    RosterItem.objects.filter(
        enrollment_id=enrollment_id, athlete_id=str(pending.pk)
    ).update(
        qualification_status=QUALIFICATION_EXCLUDED,
        qualification_reason="资格不符剔除",
        position="DF",
    )
    RosterItem.objects.filter(
        enrollment_id=enrollment_id, athlete_id="per_test_01"
    ).update(position="GK")
    for index in range(1, 6):
        approved = make_person(f"剔除场景已审{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)

    assert submit(client, school_headers, enrollment_id, "p03-exc-submit").status_code == 200
    assert review(client, city_headers, enrollment_id, "APPROVE", "p03-exc-city").status_code == 200
    approved_response = review(
        client, province_headers, enrollment_id, "APPROVE", "p03-exc-prov"
    )
    assert approved_response.status_code == 200, approved_response.json()


def test_mock_enabled_prod_matrix(monkeypatch):
    """生产双保险矩阵（2026-10-28 加固）：DJANGO_ENV=prod 时 mock_enabled()
    恒为 False——即使 ENROLLMENT_MOCK_CLIENTS 被误开；dev/test 行为不变。"""
    from apps.enrollments.clients import mock_enabled

    monkeypatch.setenv("DJANGO_ENV", "prod")
    monkeypatch.setenv("ENROLLMENT_MOCK_CLIENTS", "true")
    assert mock_enabled() is False  # 开关误开也被强制关闭
    monkeypatch.setenv("DJANGO_ENV", "dev")
    assert mock_enabled() is True
    monkeypatch.setenv("ENROLLMENT_MOCK_CLIENTS", "false")
    assert mock_enabled() is False
    monkeypatch.setenv("DJANGO_ENV", "test")
    monkeypatch.delenv("ENROLLMENT_MOCK_CLIENTS")
    assert mock_enabled() is True  # test 档默认 true（兼容现有测试运行方式）


def test_prod_env_fail_closed_on_final_review(client, auth, monkeypatch):
    """生产双保险（2026-10-28）：DJANGO_ENV=prod 强制走真实客户端路径。
    真实 PersonClient/EventConfigClient 尚未接通时，终审校验链 fail-closed
    ——503 DEPENDENCY_UNAVAILABLE 拒绝进入业务流程，mock 派生数据（per_test_*
    替身档案、mock 组别规则）无法让报名通过终审。

    边界声明：本用例验证的是"mock 被禁止 + 不可用依赖被拒绝"，不等价于
    "真实客户端调用链已实现"，更不等价于"真实跨域集成已验证"。
    """
    school_headers = auth("university")
    city_headers = auth("city")
    province_headers = auth("province")
    enrollment_id = complete_draft(client, school_headers)
    for index in range(5):
        approved = make_person(f"生产档位已审{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)

    # 数据准备仍在 mock 档完成（dev 默认）：提交 + 市州通过，抵达省级终审门前；
    # 切档前缓存最新 version——prod 下连详情读链也 fail-closed（详情聚合依赖
    # team_client），review 工厂内部的 get_detail 会先行 503，故直接发终审请求。
    assert submit(client, school_headers, enrollment_id, "p03-prod-submit").status_code == 200
    assert review(client, city_headers, enrollment_id, "APPROVE", "p03-prod-city").status_code == 200
    version = get_detail(client, school_headers, enrollment_id)["version"]

    monkeypatch.setenv("DJANGO_ENV", "prod")
    from apps.enrollments.clients import mock_enabled

    assert mock_enabled() is False

    # 省级终审：真实分支未接通的客户端（球队/人员/赛事配置）→ 503
    # DEPENDENCY_UNAVAILABLE 拒绝——终审绝不以 mock 派生数据放行（fail-closed）
    blocked = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/reviews",
        province_headers,
        {"version": version, "decision": "APPROVE", "reason": ""},
        HTTP_IDEMPOTENCY_KEY="p03-prod-prov",
    )
    assert blocked.status_code == 503, blocked.json()
    assert blocked.json()["code"] == "DEPENDENCY_UNAVAILABLE", blocked.json()

    # 报名单未被终审通过（fail-closed，状态不变）
    from apps.enrollments.models import Enrollment

    assert Enrollment.objects.get(pk=enrollment_id).status != "APPROVED"


def test_cross_school_isolation_on_p0_flow(client, auth):
    """跨校隔离：他校账号无法预检/提交他人报名单（沿用 deny-by-default 口径）。"""
    school_headers = auth("university")
    other_headers = auth("university2")
    enrollment_id = complete_draft(client, school_headers)
    version = get_detail(client, school_headers, enrollment_id)["version"]

    precheck = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/precheck",
        other_headers,
        {"version": version},
    )
    assert precheck.status_code == 403
    assert precheck.json()["code"] == "DATA_SCOPE_DENIED"

    # 提交同样被数据范围拦截（get_detail 已在工厂内 403，这里直发请求体验证）
    direct = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/submit",
        other_headers,
        {"version": version},
        HTTP_IDEMPOTENCY_KEY="p03-cross-submit",
    )
    assert direct.status_code == 403
    assert direct.json()["code"] == "DATA_SCOPE_DENIED"
