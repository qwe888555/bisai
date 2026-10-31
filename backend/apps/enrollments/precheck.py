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

from apps.common.errors import BUSINESS_VALIDATION_FAILED, ApiError

from .clients import event_config_client, material_client, person_client
from .models import (
    ACTIVE_STATUSES,
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

    # 1) 材料必需性（契约内两类）
    required = list(rules.get("requiredMaterialTypes") or [])
    has_stamped = any(a.material_type == "STAMPED_FORM" for a in attachments)
    has_student = any(a.material_type == "STUDENT_STATUS" for a in attachments) or any(
        (item.student_status_file_ids or []) for item in players
    )
    for material_type in required:
        if material_type == "STAMPED_FORM" and not has_stamped:
            issues.append(
                _issue(
                    "MISSING_MATERIAL",
                    ERROR,
                    "缺少球队报名表与名单（盖章）材料",
                    field="materialType",
                )
            )
        elif material_type == "STUDENT_STATUS" and not has_student:
            issues.append(
                _issue(
                    "MISSING_MATERIAL",
                    ERROR,
                    "缺少学籍证明材料",
                    field="materialType",
                )
            )

    # 2) 其余 4 类材料：只能经成员4 履历材料查询；取不到降级 WARNING，不臆造缺失。
    #    问题#3（2026-10-30 裁定 1-b/2-a/3-a）：已提交材料按学校端审核状态分级——
    #    提交阶段一律 WARNING 放行（软提交），省级终审在 double_track_issues 升级 ERROR。
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
    else:
        for entry in team_materials + person_materials:
            status = entry.get("status")
            if status == "REJECTED":
                issues.append(
                    _issue(
                        "QUALIFICATION_PENDING",
                        WARNING,
                        f"履历材料「{entry.get('fileName')}」学校端审核未通过，需重新提交并审核",
                        field="materials",
                    )
                )
            elif status == "PENDING":
                issues.append(
                    _issue(
                        "QUALIFICATION_PENDING",
                        WARNING,
                        f"履历材料「{entry.get('fileName')}」待学校端审核",
                        field="materials",
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


def _normalize_name(value: Optional[str]) -> str:
    return "".join((value or "").split())


def double_track_issues(enrollment) -> list[dict]:
    """球队最终 APPROVED 的双轨合规校验（人员轨 + 球队轨）。

    问题#3（2026-10-30 裁定 1-b/2-a/3-a）：省级终审硬门槛——学校端审核结论。
    档案：每名运动员主档须学校端 APPROVED；材料：已提交履历材料不得存在
    待审/驳回。材料清单取不到时降级跳过（不臆造缺失）。
    """
    issues: list[dict] = []
    players = list(enrollment.players.all())
    active = [item for item in players if item.qualification_status != QUALIFICATION_EXCLUDED]
    for item in active:
        person = person_client.get_person(item.athlete_id)
        review_status = person.get("reviewStatus") or ""
        if review_status != "APPROVED":
            issues.append(
                _issue(
                    "QUALIFICATION_PENDING",
                    ERROR,
                    f"运动员学校端档案审核状态为 {review_status or '未送审'}，不能最终通过",
                    field="reviewStatus",
                    athleteId=item.athlete_id,
                )
            )
    try:
        team_materials = material_client.list_materials("TEAM", enrollment.team_id)
        person_materials = [
            entry
            for item in active
            for entry in material_client.list_materials("PERSON", item.athlete_id)
        ]
    except ApiError:
        team_materials, person_materials = [], []
    for entry in team_materials + person_materials:
        status = entry.get("status")
        if status == "REJECTED":
            issues.append(
                _issue(
                    "QUALIFICATION_PENDING",
                    ERROR,
                    f"履历材料「{entry.get('fileName')}」学校端审核未通过，不能最终通过",
                    field="materials",
                )
            )
        elif status == "PENDING":
            issues.append(
                _issue(
                    "QUALIFICATION_PENDING",
                    ERROR,
                    f"履历材料「{entry.get('fileName')}」待学校端审核，不能最终通过",
                    field="materials",
                )
            )
    for item in active:
        if item.qualification_status != QUALIFICATION_QUALIFIED:
            issues.append(
                _issue(
                    "PLAYER_LIMIT",
                    ERROR,
                    "存在未完成资格核实的人员，不能最终通过",
                    field="qualificationStatus",
                    athleteId=item.athlete_id,
                )
            )
    rules = event_config_client.get_group_rules(enrollment.event_id, enrollment.group_id)
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
