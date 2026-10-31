"""报名规则预检（参数化纯规则校验）。

铁律：只比对已结构化字段；
- 不读取扫描件内容、不识别印章真伪、不验证外部学籍、不调用大模型；
- 所有组别参数从 event_config_client 读取，代码不硬编码组别数值；
- 契约 PrecheckIssue 只有 7 个 code，规则文档衍生校验并入对应 code 并用
  field/message 说明（TODO 确认项#13）。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.common.errors import (
    BUSINESS_VALIDATION_FAILED,
    QUALIFICATION_NOT_CONFIRMED,
    ApiError,
)
from apps.persons.services import person_audit_snapshot

from .clients import event_config_client, material_client, person_client
from .models import (
    ACTIVE_STATUSES,
    QUALIFICATION_DISQUALIFIED,
    QUALIFICATION_EXCLUDED,
    QUALIFICATION_QUALIFIED,
    RosterItem,
)

TZ = ZoneInfo("Asia/Shanghai")
ERROR = "ERROR"
WARNING = "WARNING"

# 契约缺失的 4 类材料（身份证明及照片 / 健康及保险 / 未成年人知情同意 / 转学及特殊资格）
OTHER_MATERIAL_TYPES = (
    "IDENTITY_PHOTO",
    "HEALTH_INSURANCE",
    "GUARDIAN_CONSENT",
    "TRANSFER_CERTIFICATE",
)


def now_shanghai() -> Any:
    """服务器时间（Asia/Shanghai，精确到秒）。"""
    return timezone.localtime(timezone.now(), TZ).replace(microsecond=0)


def window_state(event_id: str) -> tuple[bool, str]:
    """返回 (是否在窗口内, 说明)。"""
    window = event_config_client.get_window(event_id)
    opens_at = _to_shanghai(window.get("opensAt"))
    closes_at = _to_shanghai(window.get("closesAt"))
    now = now_shanghai()
    if opens_at and now < opens_at:
        return False, f"报名尚未开始（{opens_at.isoformat()} 开放）"
    if closes_at and now > closes_at:
        return False, f"报名已截止（{closes_at.isoformat()} 截止）"
    return True, ""


def _to_shanghai(value) -> Optional[Any]:
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return timezone.localtime(value, TZ).replace(microsecond=0)


def ensure_window_open(event_id: str) -> None:
    ok, message = window_state(event_id)
    if not ok:
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            message,
            [{"field": "window", "reason": "OUTSIDE_WINDOW"}],
        )


def outside_window_issue(event_id: str) -> Optional[dict]:
    ok, message = window_state(event_id)
    if ok:
        return None
    return _issue("OUTSIDE_WINDOW", ERROR, message, field="window")


def _issue(code: str, severity: str, message: str, **kwargs) -> dict:
    issue = {"code": code, "message": message[:500], "severity": severity}
    if kwargs.get("field"):
        issue["field"] = str(kwargs["field"])[:200]
    if kwargs.get("athleteId"):
        issue["athleteId"] = kwargs["athleteId"]
    return issue


def _is_goalkeeper(position: str) -> bool:
    text = (position or "").strip().upper()
    return text in {"GK", "GOALKEEPER"} or "守门" in (position or "")


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def run_precheck(enrollment) -> dict[str, Any]:
    """输入报名当前数据 + 赛事配置，输出契约 PrecheckResult。"""
    issues: list[dict] = []

    window_issue = outside_window_issue(enrollment.event_id)
    if window_issue:
        issues.append(window_issue)

    rules = event_config_client.get_group_rules(enrollment.event_id, enrollment.group_id)
    players = list(enrollment.players.all())
    attachments = list(enrollment.attachments.all())
    active_players = [
        item for item in players if item.qualification_status != QUALIFICATION_EXCLUDED
    ]
    persons: dict[str, dict] = {}
    for item in players:
        persons[item.athlete_id] = person_client.get_person(item.athlete_id)

    # 0) 人员档案审核状态（P0-3 决策 A：同进程直连 persons 域；2026-10-28）。
    # 提交阶段按批准规则仅产生 WARNING（不阻断提交）；终审阶段在
    # double_track_issues 中以 ERROR 硬性阻断。查不到档案（含 mock 测试
    # 替身 id、导入外键延迟）在此不加 issue，与"取不到降级"既有口径一致。
    audit_states = person_audit_snapshot(
        {item.athlete_id for item in active_players}
    )
    for item in active_players:
        state = audit_states.get(item.athlete_id)
        if state is not None and state["auditStatus"] != "APPROVED":
            issues.append(
                _issue(
                    "QUALIFICATION_PENDING",
                    WARNING,
                    f"人员档案未通过学校审核（当前状态 {state['auditStatus']}），"
                    "提交后须完成学校端审核方可最终通过",
                    field="auditStatus",
                    athleteId=item.athlete_id,
                )
            )

    # 1) 材料必需性（全量遍历 requiredMaterialTypes，方案 B 2026-10-31）：
    #    两类报名附件承载类型缺失 → MISSING_MATERIAL ERROR 阻断提交（原口径）；
    #    未知类型（无效配置，无报名附件承载无法核验）→ QUALIFICATION_PENDING
    #    WARNING 软提示待人工核对，不臆造缺失、不阻断提交。
    required = list(rules.get("requiredMaterialTypes") or [])
    for material_type in required:
        evidence = _material_evidence(material_type, attachments, players)
        if evidence is None:
            issues.append(
                _issue(
                    "QUALIFICATION_PENDING",
                    WARNING,
                    f"必需材料类型 {material_type} 不在报名附件体系内，"
                    "配置无效，待人工核对",
                    field="materialType",
                )
            )
        elif not evidence:
            if material_type == "STAMPED_FORM":
                message = "缺少球队报名表与名单（盖章）材料"
            else:
                message = "缺少学籍证明材料"
            issues.append(
                _issue(
                    "MISSING_MATERIAL",
                    ERROR,
                    message,
                    field="materialType",
                )
            )

    # 2) 其余 4 类材料：只能经成员4 履历材料查询；取不到降级 WARNING，不臆造缺失
    try:
        team_materials = material_client.list_materials("TEAM", enrollment.team_id)
        person_materials = [
            entry
            for item in players
            for entry in material_client.list_materials("PERSON", item.athlete_id)
        ]
    except ApiError:
        team_materials, person_materials = [], []
    if not team_materials and not person_materials:
        issues.append(
            _issue(
                "QUALIFICATION_PENDING",
                WARNING,
                "身份证明/健康保险/知情同意/转学材料暂无可查询的履历材料，待人工核对",
                field="materials",
            )
        )

    # 2.5) 材料域审核状态（阶段一，2026-10-31 批准口径）：双条件之"审核通过"
    # 证据。报名附件承载与材料域审核通过证据分别校验、互不替代；仅作用于
    # requiredMaterialTypes ∩ 材料域字典且 need_audit=true 的类型（当前唯一
    # 交集 STUDENT_STATUS；STAMPED_FORM 等报名附件体系类型无审核环节）。
    # 提交阶段按批准规则仅产生 WARNING（不阻断提交）；终审在
    # double_track_issues 中对已查明但未通过审核的材料以 ERROR 硬性阻断。
    # 查询不到记录 / 类型不在字典 / 异常 → 降级不臆造（§2 口径），
    # 绝不视为审核通过。只查 active_players（不含教练），不会向教练
    # 要求学籍证明（与 is_required=false 基线初衷一致）。
    audit_candidates = [c for c in required if c != "STAMPED_FORM"]
    audit_person_ids = []
    for item in active_players:
        text = str(item.athlete_id or "")
        if text.isdigit():
            audit_person_ids.append(int(text))
    audit_snapshot = _material_audit_snapshot(audit_person_ids, audit_candidates)
    for code, states in audit_snapshot.items():
        for item in active_players:
            state = states.get(item.athlete_id)
            if state is None or state["approved"]:
                continue
            label = _AUDIT_STATUS_LABELS.get(state["status"], state["status"])
            if state["status"] == "PENDING":
                detail = "待学校端审核"
            elif state["status"] == "REJECTED":
                detail = "已被驳回，需重新提交并通过审核"
            else:
                detail = "未通过学校端审核"
            issues.append(
                _issue(
                    "QUALIFICATION_PENDING",
                    WARNING,
                    f"必需材料 {code} {detail}（当前状态 {label}），"
                    "提交后须完成学校端材料审核方可最终通过",
                    field="materialAudit",
                    athleteId=item.athlete_id,
                )
            )

    # 3) 人数与守门员（参数缺失归 QUALIFICATION_PENDING）
    min_players = rules.get("minPlayers")
    max_players = rules.get("maxPlayers")
    min_goalkeepers = rules.get("minGoalkeepers")
    if min_players is None or max_players is None:
        issues.append(
            _issue(
                "QUALIFICATION_PENDING",
                WARNING,
                "组别名单人数参数未确认，暂无法校验",
                field="playerCount",
            )
        )
    else:
        count = len(active_players)
        if count < int(min_players):
            issues.append(
                _issue(
                    "PLAYER_LIMIT",
                    ERROR,
                    f"大名单人数不足：当前 {count} 人，低于下限 {min_players} 人",
                    field="playerCount",
                )
            )
        if count > int(max_players):
            issues.append(
                _issue(
                    "PLAYER_LIMIT",
                    ERROR,
                    f"大名单人数超限：当前 {count} 人，上限 {max_players} 人",
                    field="playerCount",
                )
            )
    if min_goalkeepers is not None:
        goalkeeper_count = sum(1 for item in active_players if _is_goalkeeper(item.position))
        if goalkeeper_count < int(min_goalkeepers):
            issues.append(
                _issue(
                    "PLAYER_LIMIT",
                    ERROR,
                    f"守门员人数不足：当前 {goalkeeper_count} 人，要求至少 {min_goalkeepers} 人",
                    field="goalkeepers",
                )
            )

    # 4) 出生日期闭区间（含起止日，按日期不比周岁）与姓名/证件一致性
    birth_from = _parse_date(rules.get("birthDateFrom"))
    birth_to = _parse_date(rules.get("birthDateTo"))
    if birth_from is None or birth_to is None:
        issues.append(
            _issue(
                "QUALIFICATION_PENDING",
                WARNING,
                "组别出生日期区间未确认，暂无法校验",
                field="birthDate",
            )
        )
    for item in active_players:
        person = persons.get(item.athlete_id) or {}
        birth_date = _parse_date(person.get("birthDate"))
        if birth_from and birth_to and birth_date:
            if birth_date < birth_from or birth_date > birth_to:
                issues.append(
                    _issue(
                        "PLAYER_LIMIT",
                        ERROR,
                        f"出生日期 {birth_date.isoformat()} 不在组别闭区间"
                        f" {birth_from.isoformat()}—{birth_to.isoformat()} 内",
                        field="birthDate",
                        athleteId=item.athlete_id,
                    )
                )
        if item.athlete_name and person.get("name") and _normalize_name(
            item.athlete_name
        ) != _normalize_name(person.get("name")):
            issues.append(
                _issue(
                    "NAME_MISMATCH",
                    ERROR,
                    "填报姓名与人员主档不一致",
                    field="athleteName",
                    athleteId=item.athlete_id,
                )
            )
        if person.get("hasIdConflict"):
            issues.append(
                _issue(
                    "ID_MISMATCH",
                    ERROR,
                    "证件信息存在冲突，需人工核实",
                    field="idNumber",
                    athleteId=item.athlete_id,
                )
            )
        if person.get("banned"):
            issues.append(
                _issue(
                    "PLAYER_LIMIT",
                    ERROR,
                    "存在适用于本赛事/组别的有效禁赛",
                    field="ban",
                    athleteId=item.athlete_id,
                )
            )

    # 5) 一人一队/一组 + 单内重复
    seen: dict[str, str] = {}
    for item in active_players:
        if item.athlete_id in seen:
            issues.append(
                _issue(
                    "DUPLICATE_PLAYER",
                    ERROR,
                    "同一报名单内重复添加同一运动员",
                    field="athleteId",
                    athleteId=item.athlete_id,
                )
            )
        seen[item.athlete_id] = item.pk

    other_rows = (
        RosterItem.objects.filter(
            enrollment__event_id=enrollment.event_id,
            enrollment__status__in=ACTIVE_STATUSES,
        )
        .exclude(enrollment_id=enrollment.pk)
        .values_list("athlete_id", "enrollment__team_id")
    )
    for athlete_id, team_id in other_rows:
        if athlete_id in seen and team_id != enrollment.team_id:
            issues.append(
                _issue(
                    "DUPLICATE_PLAYER",
                    ERROR,
                    "该运动员本届已代表其他球队报名，同一运动员只能代表一队、参加一组",
                    field="athleteId",
                    athleteId=athlete_id,
                )
            )

    # 6) 资格参数未确认
    if not rules.get("confirmed", False):
        issues.append(
            _issue(
                "QUALIFICATION_PENDING",
                WARNING,
                "组别资格参数尚未确认，按确认政策处理",
                field="rules",
            )
        )

    passed = not any(issue["severity"] == ERROR for issue in issues)
    return {
        "passed": passed,
        "checkedVersion": int(enrollment.version or 0),
        "issues": issues,
    }


def _material_evidence(
    material_type: str, attachments: list, players: list
) -> Optional[bool]:
    """必需材料的报名附件证据判定（提交预检与终审重验共享口径）。

    返回 True/False 表示证据齐备与否；None 表示该类型不在报名附件体系
    内（无效/未知配置，本域数据无法核验）。STUDENT_STATUS 同时认可报名
    附件与名单行 student_status_file_ids 两种承载。
    """
    if material_type == "STAMPED_FORM":
        return any(a.material_type == "STAMPED_FORM" for a in attachments)
    if material_type == "STUDENT_STATUS":
        return any(a.material_type == "STUDENT_STATUS" for a in attachments) or any(
            (item.student_status_file_ids or []) for item in players
        )
    return None


# 材料域审核状态展示文案（阶段一 2026-10-31；其余状态原样输出）
_AUDIT_STATUS_LABELS = {
    "PENDING": "待审核",
    "REJECTED": "已驳回",
}


def _material_audit_snapshot(person_ids: list, type_codes: list) -> dict:
    """材料域审核状态快照（阶段一 2026-10-31，同进程直连 materials 域）。

    对每个类型码独立调用 material_batch_query_internal（系统级只读，
    无账号数据范围；persons 域 person_audit_snapshot 同款先例）：
    - 类型不在材料域字典 / 归属不符 / 入参非法 / 查询异常 → 该码缺席
      （降级口径：不臆造、不阻断，也绝不视为审核通过）；
    - 查到记录 → {code: {person_id(str): {"status": 展示状态,
      "approved": 是否审核通过}}}。approved 按"任一记录 APPROVED"判定
      （allow_multiple 类型多份取并集）；未通过时展示最新一条记录状态
      （排序首位 updated_at DESC）。
    只查 PERSON 侧（阶段一边界：TEAM 归属映射未定，不做；TEAM 类型由
    applies_to 预检自然降级跳过）。
    """
    result: dict[str, dict[str, dict]] = {}
    if not person_ids or not type_codes:
        return result
    from apps.materials import services as material_services

    for code in type_codes:
        try:
            payload = material_services.material_batch_query_internal(
                "PERSON", person_ids, [code]
            )
        except Exception:  # noqa: BLE001 - 材料域异常降级，不阻断报名域
            continue
        items = payload.get("items") or []
        if not items:
            continue
        by_owner: dict[int, list[dict]] = {}
        for entry in items:
            by_owner.setdefault(entry["owner_id"], []).append(entry)
        for oid, rows in by_owner.items():
            approved = any(r["status"] == "APPROVED" for r in rows)
            latest = rows[0]["status"]  # 排序首位即最新
            result.setdefault(code, {})[str(oid)] = {
                "status": "APPROVED" if approved else latest,
                "approved": approved,
            }
    return result


def _normalize_name(value: Optional[str]) -> str:
    return "".join((value or "").split())


def double_track_issues(enrollment) -> list[dict]:
    """球队最终 APPROVED 的双轨合规校验（人员轨 + 球队轨）。

    P0-3 终审硬性重验（2026-10-28）：每次终审实时直连 persons 域取最新
    档案审核状态，不依赖提交阶段或早先预检结果：
    - 档案存在且 audit_status != APPROVED → ERROR（未审/待审/驳回/草稿）；
    - 档案存在但已禁用（DISABLED）→ ERROR；
    - 档案不存在或 id 不可解析 → ERROR；但 ENROLLMENT_MOCK_CLIENTS=true
      的 mock 测试替身生态（athlete_id 为 per_test_* 等假 id）放行——
      该边界仅限测试替身，真实链路必须能核验到档案（不能把 mock 通过
      宣称为真实跨域集成已验证）；
    - requiredMaterialTypes 声明的必需材料在终审时按共享证据口径
      （_material_evidence）重验存在性：STUDENT_STATUS 认可报名附件与
      名单行 student_status_file_ids，与提交预检一致（修复两侧比对差异）；
    - 材料域审核状态硬门槛（阶段一 2026-10-31 批准口径）：字典内
      need_audit=true 的必需类型（当前唯一交集 STUDENT_STATUS）须存在
      APPROVED 记录，已查明未通过 → MISSING_MATERIAL ERROR（报名附件
      承载与审核通过证据互不替代）；查不到记录/异常 → 降级不阻断
      （批准口径#5）；
    - E-2 资格确认硬门槛（v7，2026-10-28）：QUALIFIED 且确认凭证缺失/单侧
      无效 → QUALIFICATION_NOT_CONFIRMED ERROR（双条件
      confirmed_at + confirmed_by；REVOKE 后状态仍 QUALIFIED 但凭证失效，
      终审判凭证不判状态值；无宽限期，无豁免分支）。mock 测试替身生态
      （ENROLLMENT_MOCK_CLIENTS=true，per_test_* 名单行无凭证）放行——
      与人员轨替身边界一致，仅限测试替身，真实链路必须先经 rosterQualify
      人工确认。
    """
    issues: list[dict] = []
    players = list(enrollment.players.all())
    active = [item for item in players if item.qualification_status != QUALIFICATION_EXCLUDED]

    # 人员档案审核状态硬门槛（实时直连，决策 A）
    audit_states = person_audit_snapshot({item.athlete_id for item in active})
    for item in active:
        state = audit_states.get(item.athlete_id)
        if state is None:
            from .clients import mock_enabled

            if mock_enabled():
                continue  # mock 测试替身 id：非真实链路，放行（边界已声明）
            issues.append(
                _issue(
                    "PLAYER_LIMIT",
                    ERROR,
                    "人员档案不存在或无法核验，不能最终通过",
                    field="auditStatus",
                    athleteId=item.athlete_id,
                )
            )
            continue
        if state["auditStatus"] != "APPROVED":
            issues.append(
                _issue(
                    "PLAYER_LIMIT",
                    ERROR,
                    f"人员档案未通过学校审核（当前状态 {state['auditStatus']}），不能最终通过",
                    field="auditStatus",
                    athleteId=item.athlete_id,
                )
            )
        elif state["status"] != "ACTIVE":
            issues.append(
                _issue(
                    "PLAYER_LIMIT",
                    ERROR,
                    "人员档案已禁用，不能最终通过",
                    field="personStatus",
                    athleteId=item.athlete_id,
                )
            )
    for item in active:
        if item.qualification_status != QUALIFICATION_QUALIFIED:
            # 消息按状态区分（2026-10-31）：DISQUALIFIED 为人工驳回结果，
            # 出口是重新确认（CONFIRM）或先 EXCLUDE 剔除名单；其余非
            # QUALIFIED 状态维持原口径。阻断行为不变（均 ERROR）。
            if item.qualification_status == QUALIFICATION_DISQUALIFIED:
                message = "存在资格不符（已被驳回）的人员，不能最终通过"
            else:
                message = "存在未完成资格核实的人员，不能最终通过"
            issues.append(
                _issue(
                    "PLAYER_LIMIT",
                    ERROR,
                    message,
                    field="qualificationStatus",
                    athleteId=item.athlete_id,
                )
            )
            continue
        # E-2 资格确认硬门槛：QUALIFIED ≠ 已确认，凭证缺失/单侧无效即阻断。
        from .clients import mock_enabled

        if not mock_enabled() and (
            item.confirmed_at is None or not item.confirmed_by
        ):
            issues.append(
                _issue(
                    QUALIFICATION_NOT_CONFIRMED,
                    ERROR,
                    "名单资格尚未经人工确认，不能最终通过",
                    field="qualificationConfirm",
                    athleteId=item.athlete_id,
                )
            )
    rules = event_config_client.get_group_rules(enrollment.event_id, enrollment.group_id)

    # 必需材料终审重验（存在性硬校验，不依赖早先预检缓存）。
    # 与提交预检共享 _material_evidence 口径（方案 B 2026-10-31）：
    # STUDENT_STATUS 同时认可报名附件与名单行 student_status_file_ids
    # （修复两侧比对差异）；未知类型无报名附件承载、无法核验，按
    # fail-closed ERROR 处理（终审不得放行无法核验的必需项）。
    attachments = list(enrollment.attachments.all())
    required = list(rules.get("requiredMaterialTypes") or [])
    for material_type in required:
        evidence = _material_evidence(material_type, attachments, players)
        if evidence is None:
            issues.append(
                _issue(
                    "MISSING_MATERIAL",
                    ERROR,
                    f"必需材料 {material_type} 不在报名附件体系内，"
                    "配置无效，不能最终通过",
                    field="materialType",
                )
            )
        elif not evidence:
            issues.append(
                _issue(
                    "MISSING_MATERIAL",
                    ERROR,
                    f"必需材料 {material_type} 在终审时缺失",
                    field="materialType",
                )
            )

    # 材料域审核状态终审硬门槛（阶段一 2026-10-31 批准口径）：已查明但
    # 未通过审核（PENDING/REJECTED/DRAFT/WITHDRAWN 均不算通过）→
    # MISSING_MATERIAL ERROR 阻断；查不到记录 / 类型不在字典 / 异常 →
    # 降级不臆造、不阻断（批准口径#5；已知边界：从未上传履历材料者
    # 不会被此处拦住，提交预检阶段由 §2 WARNING 提示）。与上方存在性
    # 重验互不替代：附件缺失照旧 ERROR，审核未通过同样 ERROR。
    audit_candidates = [c for c in required if c != "STAMPED_FORM"]
    audit_person_ids = []
    for item in active:
        text = str(item.athlete_id or "")
        if text.isdigit():
            audit_person_ids.append(int(text))
    audit_snapshot = _material_audit_snapshot(audit_person_ids, audit_candidates)
    for code, states in audit_snapshot.items():
        for item in active:
            state = states.get(item.athlete_id)
            if state is None or state["approved"]:
                continue
            label = _AUDIT_STATUS_LABELS.get(state["status"], state["status"])
            issues.append(
                _issue(
                    "MISSING_MATERIAL",
                    ERROR,
                    f"必需材料 {code} 未经学校端审核通过（当前状态 {label}），"
                    "不能最终通过",
                    field="materialAudit",
                    athleteId=item.athlete_id,
                )
            )

    min_players = rules.get("minPlayers")
    if min_players is not None and len(active) < int(min_players):
        issues.append(
            _issue(
                "PLAYER_LIMIT",
                ERROR,
                f"剔除后剩余人数 {len(active)} 人，不足组别下限 {min_players} 人",
                field="playerCount",
            )
        )
    return issues
