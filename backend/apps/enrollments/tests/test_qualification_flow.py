"""E-2 报名名单资格确认专项测试（设计文档 v7，2026-10-28）。

覆盖（用户验收清单）：
- 四操作合法/非法状态转换、原因必填、版本冲突 409；
- CONFIRM 幂等 no-op、日志追加、DISQUALIFIED 重新确认保留历史；
- EXCLUDED 不可逆（四操作均 409）、REVOKE 清凭证不改状态值；
- 终审硬门槛：凭证缺失/单侧无效/REVOKE 后均拒绝；有效凭证放行；
- 跨校访问与无权角色拒绝；日志关联名单删除后 SET_NULL+snapshot 追溯；
- ReviewRecord 隔离。

边界声明：并发行锁验证受 SQLite 测试环境限制（select_for_update 在
SQLite 退化为无操作），真实 MySQL 并发行为待阶段 5 独立验证；
mock_enabled=False 的终审用例通过 patch 客户端实现，不等价于真实
跨域集成验证。
"""

from __future__ import annotations

from django.utils import timezone

from apps.common import enums
from apps.common.crypto import encrypt_text, lookup_hash, mask_id_number
from apps.enrollments.models import (
    QUALIFICATION_DISQUALIFIED,
    QUALIFICATION_EXCLUDED,
    QUALIFICATION_QUALIFIED,
    Enrollment,
    RosterItem,
    RosterQualificationLog,
    ReviewRecord,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
)
from apps.enrollments.tests.factories import (
    BASE,
    complete_draft,
    post_json,
    put_json,
)
from apps.persons.models import Person

_person_seq = 0


def make_person(name: str, audit_status: str, status: str = "ACTIVE") -> Person:
    """真实 persons 域记录（口径同 test_p03_integration）。"""
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
    RosterItem.objects.filter(
        enrollment_id=enrollment_id, athlete_id=mock_athlete_id
    ).update(athlete_id=str(person.pk), athlete_name="")


def _items(enrollment_id: str) -> list[RosterItem]:
    return list(
        RosterItem.objects.filter(enrollment_id=enrollment_id).order_by("created_at", "id")
    )


def _item(enrollment_id: str, index: int = 0) -> RosterItem:
    return _items(enrollment_id)[index]


def _version(roster_id: str) -> int:
    return int(RosterItem.objects.get(pk=roster_id).version)


def _qualify(client, headers, enrollment_id, roster_id, action, version, reason=""):
    return put_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/players/{roster_id}/qualification",
        headers,
        {"action": action, "version": version, "reason": reason},
    )


def _issues(payload) -> list[dict]:
    data = payload.json()["data"]
    return data if isinstance(data, list) else data.get("issues", [])


def _gate_env(monkeypatch):
    """终审门槛用例：mock 关闭（凭证检查生效）+ 客户端替身（避免 503）。

    边界：人员轨用真实 Person 直连查库；run_precheck（窗口/组别规则/
    材料 warning）与 get_group_rules 为替身返回值，仅用于隔离变量，
    不宣称真实赛事配置集成；被测对象是 double_track_issues 的资格
    凭证硬门槛。
    """
    monkeypatch.setattr("apps.enrollments.clients.mock_enabled", lambda: False)
    monkeypatch.setattr(
        "apps.enrollments.clients.EventConfigClient.get_group_rules",
        lambda self, event_id, group_id: {
            "minPlayers": 5,
            "maxPlayers": 25,
            "minGoalkeepers": 1,
            "birthDateFrom": "2008-01-01",
            "birthDateTo": "2012-12-31",
            "requiredMaterialTypes": ["STAMPED_FORM", "STUDENT_STATUS"],
            "confirmed": True,
        },
    )
    # 终审入口先跑 run_precheck（内部依赖 mock 客户端），此处替身放行，
    # 使被测分支收敛到 double_track_issues 的资格凭证硬门槛。
    monkeypatch.setattr(
        "apps.enrollments.precheck.run_precheck",
        lambda enrollment: {"passed": True, "checkedVersion": 0, "issues": []},
    )


# ---------------------------------------------------------------- 基础状态机


def test_confirm_sets_credential_and_appends_log(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    response = _qualify(client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk))
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["qualificationStatus"] == QUALIFICATION_QUALIFIED
    assert data["confirmedAt"] is not None
    assert data["confirmedBy"] == "acc_university"

    refreshed = RosterItem.objects.get(pk=item.pk)
    assert refreshed.confirmed_at is not None
    assert refreshed.confirmed_by == "acc_university"
    log = RosterQualificationLog.objects.get(roster_item_id_snapshot=item.pk)
    assert log.action == "CONFIRM"
    assert log.operator_account_id == "acc_university"
    assert log.athlete_id == item.athlete_id


def test_confirm_idempotent_no_extra_log(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    first = _qualify(client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk))
    assert first.status_code == 200
    version_after = _version(item.pk)

    second = _qualify(client, school, enrollment_id, item.pk, "CONFIRM", version_after)
    assert second.status_code == 200
    assert second.json()["data"]["confirmedAt"] == first.json()["data"]["confirmedAt"]
    assert _version(item.pk) == version_after  # 幂等 no-op：版本不再递增
    assert RosterQualificationLog.objects.filter(
        roster_item_id_snapshot=item.pk
    ).count() == 1


def test_reject_requires_reason(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    response = _qualify(client, school, enrollment_id, item.pk, "REJECT", _version(item.pk))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"
    # 空白 reason 同样拒绝
    blank = _qualify(
        client, school, enrollment_id, item.pk, "REJECT", _version(item.pk), reason="   "
    )
    assert blank.status_code == 400


def test_reject_sets_disqualified_clears_credential_and_logs(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    response = _qualify(
        client, school, enrollment_id, item.pk, "REJECT", _version(item.pk), reason="资格材料不符"
    )
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["qualificationStatus"] == QUALIFICATION_DISQUALIFIED
    assert data["confirmedAt"] is None
    assert data["confirmedBy"] == ""
    refreshed = RosterItem.objects.get(pk=item.pk)
    assert refreshed.confirmed_at is None
    assert refreshed.confirmed_by == ""
    log = RosterQualificationLog.objects.get(
        roster_item_id_snapshot=item.pk, action="REJECT"
    )
    assert log.reason == "资格材料不符"


def test_reconfirm_after_reject_keeps_history(client, auth):
    """DISQUALIFIED → CONFIRM 允许（v7 决策 6）：新凭证 + 新日志，驳回记录保留。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    assert _qualify(
        client, school, enrollment_id, item.pk, "REJECT", _version(item.pk), reason="首次驳回"
    ).status_code == 200
    confirm = _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    )
    assert confirm.status_code == 200, confirm.json()
    assert confirm.json()["data"]["qualificationStatus"] == QUALIFICATION_QUALIFIED
    assert confirm.json()["data"]["confirmedAt"] is not None

    actions = list(
        RosterQualificationLog.objects.filter(roster_item_id_snapshot=item.pk)
        .order_by("created_at", "id")
        .values_list("action", flat=True)
    )
    assert actions == ["REJECT", "CONFIRM"]  # 历史驳回记录保留不覆盖


def test_exclude_requires_reason_and_sets_excluded(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school, players=6)
    item = _item(enrollment_id)

    response = _qualify(
        client, school, enrollment_id, item.pk, "EXCLUDE", _version(item.pk), reason="超出年龄"
    )
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["qualificationStatus"] == QUALIFICATION_EXCLUDED
    refreshed = RosterItem.objects.get(pk=item.pk)
    assert refreshed.confirmed_at is None
    assert refreshed.confirmed_by == ""


def test_exclude_irreversible_all_ops_409(client, auth):
    """EXCLUDED 在资格操作接口中不可逆：四操作全部 409。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school, players=6)
    item = _item(enrollment_id)
    assert _qualify(
        client, school, enrollment_id, item.pk, "EXCLUDE", _version(item.pk), reason="资格不符"
    ).status_code == 200

    for action in ("CONFIRM", "REJECT", "EXCLUDE", "REVOKE"):
        response = _qualify(
            client, school, enrollment_id, item.pk, action, _version(item.pk), reason="尝试恢复"
        )
        assert response.status_code == 409, (action, response.json())
        assert response.json()["code"] == "STATE_CONFLICT"
    assert RosterItem.objects.get(pk=item.pk).qualification_status == QUALIFICATION_EXCLUDED


def test_revoke_without_credential_409(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    response = _qualify(
        client, school, enrollment_id, item.pk, "REVOKE", _version(item.pk), reason="误报名"
    )
    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"


def test_revoke_clears_credential_keeps_status_and_allows_reconfirm(client, auth):
    """REVOKE 双清凭证、qualification_status 保持 QUALIFIED（终审判凭证非状态值）。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200
    response = _qualify(
        client, school, enrollment_id, item.pk, "REVOKE", _version(item.pk), reason="确认有误"
    )
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["qualificationStatus"] == QUALIFICATION_QUALIFIED  # 状态不变
    assert data["confirmedAt"] is None
    assert data["confirmedBy"] == ""

    actions = list(
        RosterQualificationLog.objects.filter(roster_item_id_snapshot=item.pk)
        .order_by("created_at", "id")
        .values_list("action", flat=True)
    )
    assert actions == ["CONFIRM", "REVOKE"]

    # 撤销后可重新确认（新凭证 + 新日志）
    reconfirm = _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    )
    assert reconfirm.status_code == 200
    assert reconfirm.json()["data"]["confirmedAt"] is not None
    actions = list(
        RosterQualificationLog.objects.filter(roster_item_id_snapshot=item.pk)
        .order_by("created_at", "id")
        .values_list("action", flat=True)
    )
    assert actions == ["CONFIRM", "REVOKE", "CONFIRM"]


def test_reason_required_for_revoke_and_exclude(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    assert _qualify(
        client, school, enrollment_id, item.pk, "EXCLUDE", _version(item.pk)
    ).status_code == 400


def test_version_conflict_409(client, auth):
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    response = _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk) + 100
    )
    assert response.status_code == 409
    assert response.json()["code"] == "VERSION_CONFLICT"


# ---------------------------------------------------------------- 权限与数据范围


def test_cross_school_denied(client, auth):
    school = auth("university")
    other = auth("university2")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    response = _qualify(
        client, other, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "DATA_SCOPE_DENIED"


def test_city_province_admin_forbidden(client, auth):
    """CITY/PROVINCE_REVIEWER 只读、ADMIN 不开放：未注册 rosterQualify → 403。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    for username in ("city", "province", "admin"):
        response = _qualify(
            client, auth(username), enrollment_id, item.pk, "CONFIRM", _version(item.pk)
        )
        assert response.status_code == 403, (username, response.json())
        assert response.json()["code"] == "FORBIDDEN"


def test_team_role_within_scope_allowed(client, auth):
    """TEAM 在自身球队数据范围内可执行资格操作。"""
    school = auth("university")
    team = auth("team")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    response = _qualify(
        client, team, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    )
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["confirmedBy"] == "acc_team"


# ---------------------------------------------------------------- 终审硬门槛


def test_final_review_blocks_without_credential(client, auth, monkeypatch):
    """mock 替身放行关闭后：QUALIFIED 但无凭证 → QUALIFICATION_NOT_CONFIRMED。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    for index in range(5):
        approved = make_person(f"无凭证球员{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)
    _gate_env(monkeypatch)

    from apps.enrollments import precheck

    enrollment = Enrollment.objects.get(pk=enrollment_id)
    issues = precheck.double_track_issues(enrollment)
    hit = [
        i
        for i in issues
        if i["code"] == "QUALIFICATION_NOT_CONFIRMED"
        and i["severity"] == "ERROR"
    ]
    assert hit, issues
    assert len(hit) == 5  # 全部活跃名单行均被阻断


def test_final_review_blocks_single_sided_credential(client, auth, monkeypatch):
    """单侧脏数据（只有 confirmed_at 或只有 confirmed_by）均不得通过终审。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    for index in range(5):
        approved = make_person(f"单侧凭证{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)
    _gate_env(monkeypatch)

    from apps.enrollments import precheck

    items = _items(enrollment_id)
    # 只写 confirmed_at
    RosterItem.objects.filter(pk=items[0].pk).update(confirmed_at=timezone.now())
    # 只写 confirmed_by
    RosterItem.objects.filter(pk=items[1].pk).update(confirmed_by="acc_university")

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    hit = [i for i in issues if i["code"] == "QUALIFICATION_NOT_CONFIRMED"]
    athletes = {i.get("athleteId") for i in hit}
    assert str(items[0].pk) in athletes or items[0].athlete_id in athletes
    assert items[1].athlete_id in athletes or str(items[1].pk) in athletes
    assert len(hit) == 5


def test_final_review_blocks_after_revoke(client, auth, monkeypatch):
    """REVOKE 后状态仍 QUALIFIED 但凭证失效 → 终审拒绝（判凭证非状态值）。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    for index in range(5):
        approved = make_person(f"撤销球员{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)
    items = _items(enrollment_id)
    for item in items:
        assert _qualify(
            client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
        ).status_code == 200
    assert _qualify(
        client, school, enrollment_id, items[0].pk, "REVOKE", _version(items[0].pk), reason="确认有误"
    ).status_code == 200

    _gate_env(monkeypatch)
    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    hit = [
        i
        for i in issues
        if i["code"] == "QUALIFICATION_NOT_CONFIRMED"
        and i.get("athleteId") == items[0].athlete_id
    ]
    assert hit, issues


def test_final_review_passes_with_valid_credential(client, auth, monkeypatch):
    """全部活跃名单行均有有效凭证（双条件）→ 终审校验无 ERROR。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    for index in range(5):
        approved = make_person(f"有效凭证{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)
    for item in _items(enrollment_id):
        assert _qualify(
            client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
        ).status_code == 200

    _gate_env(monkeypatch)
    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    assert not [i for i in issues if i["severity"] == "ERROR"], issues
    assert not [i for i in issues if i["code"] == "QUALIFICATION_NOT_CONFIRMED"], issues


def test_final_review_http_422_without_credential(client, auth, monkeypatch):
    """完整终审链路（HTTP）：无凭证 → 422 BUSINESS_VALIDATION_FAILED。"""
    school = auth("university")
    city = auth("city")
    province = auth("province")
    enrollment_id = complete_draft(client, school)
    for index in range(5):
        approved = make_person(f"链路无凭证{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)

    assert post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/submit",
        school,
        {"version": Enrollment.objects.get(pk=enrollment_id).version},
        HTTP_IDEMPOTENCY_KEY="e2-gate-submit",
    ).status_code == 200
    assert post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/reviews",
        city,
        {"version": Enrollment.objects.get(pk=enrollment_id).version,
         "decision": "APPROVE", "reason": ""},
        HTTP_IDEMPOTENCY_KEY="e2-gate-city",
    ).status_code == 200

    _gate_env(monkeypatch)
    blocked = post_json(
        client,
        f"{BASE}/enrollments/{enrollment_id}/reviews",
        province,
        {"version": Enrollment.objects.get(pk=enrollment_id).version,
         "decision": "APPROVE", "reason": ""},
        HTTP_IDEMPOTENCY_KEY="e2-gate-prov",
    )
    assert blocked.status_code == 422, blocked.json()
    assert blocked.json()["code"] == "BUSINESS_VALIDATION_FAILED"
    assert any(
        i["code"] == "QUALIFICATION_NOT_CONFIRMED" for i in _issues(blocked)
    ), blocked.json()
    assert Enrollment.objects.get(pk=enrollment_id).status != "APPROVED"


# ---------------------------------------------------------------- 审计与追溯


def test_log_survives_roster_hard_delete(client, auth):
    """SET_NULL + roster_item_id_snapshot：名单行硬删后日志保留可追溯。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school, players=6)
    item = _item(enrollment_id)
    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200

    # 硬删（测试准备，模拟级联删除场景）：FK 置空，snapshot + athlete_id 保留
    RosterItem.objects.filter(pk=item.pk).delete()

    log = RosterQualificationLog.objects.get(roster_item_id_snapshot=item.pk)
    assert log.roster_item_id is None  # SET_NULL
    assert log.roster_item_id_snapshot == item.pk  # 冗余名单 ID 可追溯
    assert log.athlete_id == item.athlete_id  # 人员维度可读


def test_review_record_isolation(client, auth):
    """资格操作不写 ReviewRecord（独立日志表）。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    before = ReviewRecord.objects.filter(enrollment_id=enrollment_id).count()

    for item in _items(enrollment_id):
        assert _qualify(
            client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
        ).status_code == 200

    assert (
        ReviewRecord.objects.filter(enrollment_id=enrollment_id).count() == before
    )
    assert RosterQualificationLog.objects.filter(
        roster_item_id_snapshot__in=[i.pk for i in _items(enrollment_id)]
    ).count() == 5


# ---------------------------------------------------------------- 换人与凭证绑定（2026-10-29 批准）


def _update_player(client, headers, enrollment_id, roster_id, **overrides):
    body = {
        "athleteId": "per_test_00",
        "jerseyNumber": None,
        "position": "",
        "photoFileId": "",
        "studentStatusFileIds": [],
        "version": _version(roster_id),
    }
    body.update(overrides)
    return put_json(
        client, f"{BASE}/enrollments/{enrollment_id}/players/{roster_id}", headers, body
    )


def test_athlete_change_invalidates_credential(client, auth):
    """换人即失效：A 已确认 → 换成 B 后两凭证字段同时清空（不改状态/日志）。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)
    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200

    response = _update_player(
        client, school, enrollment_id, item.pk, athleteId="per_test_99"
    )
    assert response.status_code == 200, response.json()

    refreshed = RosterItem.objects.get(pk=item.pk)
    assert refreshed.athlete_id == "per_test_99"  # 球员已换
    assert refreshed.confirmed_at is None  # 凭证双清
    assert refreshed.confirmed_by == ""
    assert refreshed.qualification_status == QUALIFICATION_QUALIFIED  # 状态不变
    assert _version(item.pk) > 0  # bump 正常递增


def test_athlete_change_preserves_old_log(client, auth):
    """换人不改写历史日志：旧 CONFIRM 日志仍指向原球员，且不新增日志。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)
    old_athlete = item.athlete_id
    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200

    assert _update_player(
        client, school, enrollment_id, item.pk, athleteId="per_test_99"
    ).status_code == 200

    logs = list(
        RosterQualificationLog.objects.filter(roster_item_id_snapshot=item.pk)
    )
    assert len(logs) == 1  # 换人不追加、不改写资格日志
    assert logs[0].action == "CONFIRM"
    assert logs[0].athlete_id == old_athlete  # 原确认对应球员可追溯


def test_roster_update_without_athlete_change_keeps_credential(client, auth):
    """同一球员的无变化更新（改位置）不清除凭证。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)
    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200
    confirmed_at = RosterItem.objects.get(pk=item.pk).confirmed_at

    response = _update_player(
        client, school, enrollment_id, item.pk, position="GK"
    )
    assert response.status_code == 200, response.json()

    refreshed = RosterItem.objects.get(pk=item.pk)
    assert refreshed.athlete_id == item.athlete_id
    assert refreshed.confirmed_at == confirmed_at  # 凭证保留
    assert refreshed.confirmed_by == "acc_university"


def test_roster_update_failure_no_partial_commit(client, auth):
    """更新失败（重复球员 409）时球员与凭证均不发生部分提交。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)
    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200
    before = RosterItem.objects.get(pk=item.pk)

    response = _update_player(
        client, school, enrollment_id, item.pk, athleteId="per_test_01"
    )
    assert response.status_code == 409
    assert response.json()["code"] == "DUPLICATE_RESOURCE"

    after = RosterItem.objects.get(pk=item.pk)
    assert after.athlete_id == before.athlete_id
    assert after.confirmed_at == before.confirmed_at
    assert after.confirmed_by == before.confirmed_by
    assert after.version == before.version


def test_final_review_rejects_after_athlete_change(client, auth, monkeypatch):
    """换人后凭旧确认不能通过终审（对 B 无凭证 → QUALIFICATION_NOT_CONFIRMED）。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    for index in range(5):
        approved = make_person(f"换人前{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)
    incoming = make_person("换入球员B", enums.AuditStatus.APPROVED)
    item = _item(enrollment_id)
    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200

    assert _update_player(
        client, school, enrollment_id, item.pk, athleteId=str(incoming.pk)
    ).status_code == 200

    _gate_env(monkeypatch)
    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    # 该行阻断原因必须是凭证缺失，而非人员轨（B 档案已 APPROVED）
    for issue in issues:
        if issue.get("athleteId") == str(incoming.pk):
            assert issue["code"] == "QUALIFICATION_NOT_CONFIRMED", issues
    assert any(
        i["code"] == "QUALIFICATION_NOT_CONFIRMED"
        and i.get("athleteId") == str(incoming.pk)
        for i in issues
    ), issues


def test_reconfirm_after_athlete_change_passes_final_review(client, auth, monkeypatch):
    """换人后重新确认（对 B）即可通过终审校验。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    for index in range(5):
        approved = make_person(f"换人重确认{index}", enums.AuditStatus.APPROVED)
        bind_player(enrollment_id, f"per_test_{index:02d}", approved)
    incoming = make_person("换入球员C", enums.AuditStatus.APPROVED)
    items = _items(enrollment_id)
    for row in items:  # 全部行先确认，隔离变量到"换人→重确认"
        assert _qualify(
            client, school, enrollment_id, row.pk, "CONFIRM", _version(row.pk)
        ).status_code == 200
    item = items[0]
    assert _update_player(
        client, school, enrollment_id, item.pk, athleteId=str(incoming.pk)
    ).status_code == 200

    reconfirm = _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    )
    assert reconfirm.status_code == 200, reconfirm.json()
    assert reconfirm.json()["data"]["confirmedBy"] == "acc_university"

    _gate_env(monkeypatch)
    from apps.enrollments import precheck

    issues = precheck.double_track_issues(Enrollment.objects.get(pk=enrollment_id))
    assert not [i for i in issues if i["severity"] == "ERROR"], issues


# ---------------------------------------------------------------- D-1 换人审计摘要（2026-10-29 批准）


def _capture_audit(monkeypatch):
    captured = {}

    def fake_audit(request, action, **kwargs):
        captured["action"] = action
        captured.update(kwargs)

    monkeypatch.setattr("apps.enrollments.api.audit_state", fake_audit)
    return captured


def test_update_audit_summary_on_athlete_change(client, auth, monkeypatch):
    """换人成功：摘要含旧/新 athleteId 与凭证失效标记，不超 500 字符。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)
    captured = _capture_audit(monkeypatch)

    response = _update_player(
        client, school, enrollment_id, item.pk, athleteId="per_test_99"
    )
    assert response.status_code == 200, response.json()

    assert captured["action"] == "enrollment.roster.update"
    assert captured["object_id"] == item.pk
    expected = (
        f"修改名单条目 athleteId={item.athlete_id}→per_test_99（资格凭证已失效）"
    )
    assert captured["summary"] == expected
    assert len(captured["summary"]) <= 500


def test_update_audit_summary_without_athlete_change(client, auth, monkeypatch):
    """同一球员更新：摘要保持原样，不含失效标记。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)
    captured = _capture_audit(monkeypatch)

    response = _update_player(
        client, school, enrollment_id, item.pk, position="GK"
    )
    assert response.status_code == 200, response.json()

    assert captured["summary"] == "修改名单条目"


def test_update_audit_not_declared_on_failure(client, auth, monkeypatch):
    """更新失败（重复球员 409）：不产生成功审计声明。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)
    captured = _capture_audit(monkeypatch)

    response = _update_player(
        client, school, enrollment_id, item.pk, athleteId="per_test_01"
    )
    assert response.status_code == 409

    assert captured == {}  # 视图未走到 audit_state，无虚假成功声明


# ---------------------------------------------------------------- D-2 终审后资格禁令（2026-10-29 批准）


def _set_status(enrollment_id, status):
    Enrollment.objects.filter(pk=enrollment_id).update(status=status)


def test_approved_blocks_all_qualification_ops(client, auth):
    """APPROVED 下四操作均 409 STATE_CONFLICT + ENROLLMENT_APPROVED。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)
    _set_status(enrollment_id, STATUS_APPROVED)

    for action in ("CONFIRM", "REJECT", "EXCLUDE", "REVOKE"):
        response = _qualify(
            client, school, enrollment_id, item.pk, action, _version(item.pk), "测试原因"
        )
        assert response.status_code == 409, (action, response.json())
        assert response.json()["code"] == "STATE_CONFLICT"
        assert response.json()["data"][0]["reason"] == "ENROLLMENT_APPROVED"


def test_approved_repeated_confirm_blocked(client, auth):
    """APPROVED 下重复 CONFIRM 也 409（禁令优先于幂等 no-op）。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)
    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200
    _set_status(enrollment_id, STATUS_APPROVED)

    response = _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    )
    assert response.status_code == 409
    assert response.json()["data"][0]["reason"] == "ENROLLMENT_APPROVED"


def test_approved_rejection_leaves_no_trace(client, auth):
    """被拒操作不产生新日志、不改凭证/资格状态/名单记录。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)
    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200
    _set_status(enrollment_id, STATUS_APPROVED)

    before_item = RosterItem.objects.get(pk=item.pk)
    before_logs = list(
        RosterQualificationLog.objects.filter(roster_item_id_snapshot=item.pk)
        .order_by("id")
        .values_list("id", flat=True)
    )
    for action in ("CONFIRM", "REJECT", "EXCLUDE", "REVOKE"):
        assert _qualify(
            client, school, enrollment_id, item.pk, action, _version(item.pk), "测试原因"
        ).status_code == 409

    after_item = RosterItem.objects.get(pk=item.pk)
    for field in ("athlete_id", "qualification_status", "confirmed_at", "confirmed_by", "version"):
        assert getattr(after_item, field) == getattr(before_item, field), field
    after_logs = list(
        RosterQualificationLog.objects.filter(roster_item_id_snapshot=item.pk)
        .order_by("id")
        .values_list("id", flat=True)
    )
    assert after_logs == before_logs


def test_rejected_and_withdrawn_still_operable(client, auth):
    """REJECTED/WITHDRAWN 不在禁令内，原有可操作行为保留。"""
    school = auth("university")
    enrollment_id = complete_draft(client, school)
    item = _item(enrollment_id)

    _set_status(enrollment_id, STATUS_REJECTED)
    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200
    _set_status(enrollment_id, STATUS_WITHDRAWN)
    assert _qualify(
        client, school, enrollment_id, item.pk, "CONFIRM", _version(item.pk)
    ).status_code == 200  # 幂等 no-op 200（已有有效凭证）
