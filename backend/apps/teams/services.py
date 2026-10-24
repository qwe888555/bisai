"""球队域业务逻辑：球队档案 CRUD（批次 A）+ 球队成员管理（批次 B）。

依据：docs/design/成员4-DDL草案-V1.4.sql（V1.4-r1，冻结）+ zuqiu 前端实码契约。
- Team 软删（is_deleted）；关系表与转队单禁止物理删除（模型 docstring）；
- team_no 按 DDL 列注释回填：T+年份+8位主键id（与 person_no 同款，pk 派生
  全局唯一 + uk_team_no 兜底，无计数器竞态）；
- audit_status 字段保留（Q2 审核流程不启用），创建即默认 APPROVED；
- 成员状态严格 CURRENT/EXITED（不虚构审核状态）；R2 现役唯一/R3 队内
  现役球衣号唯一为服务层规则；PENDING 转出单阻断直接移出；
- 写链禁 queryset.update()（绕过 auto_now），统一 instance.save()；
- 学校组织映射（A′-min：ensure_business_org）尚未交付（2026-10-24 复核）：
  前端创建 payload 的 schoolOrgId（Organization id）→ persons.School 的解析
  依赖该接口；未交付前 API 层返回明确冲突错误，不做任何绕过，不写 Organization；
- 无冻结承载的前端字段（shortName/gender/campusName/coachName 输入值）：
  声明但不持久化不回显（同 persons education/coachCategory 先例）；
  coachName 输出仅由现役（CURRENT）主教练关系合成（list_own_teams 先例）。
"""

from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.db.models import Count, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from apps.common import enums
from apps.common.errors import DATA_SCOPE_DENIED, NOT_FOUND, STATE_CONFLICT, ApiError
from apps.common.permissions import ADMIN
from apps.teams.models import Team, TeamCoach, TeamMember, TeamTransfer

# ensure_business_org 由成员2在 accounts 域交付（A′-min 已确认方案）；
# 本轮只读核实其尚未存在，映射交付后替换 _resolve_school 的占位实现。
_ORG_SERVICE_NOT_DELIVERED = "学校组织映射（accounts.ensure_business_org）尚未交付，暂无法定位所属学校"


def _team_queryset() -> QuerySet[Team]:
    """软删过滤 + 合成字段来源（school/region）。"""
    return Team.objects.filter(is_deleted=False).select_related("school", "region")


def _annotate_team(queryset: QuerySet[Team]) -> QuerySet[Team]:
    """memberCount（现役成员数）与现役主教练姓名合成（/me/teams 同款口径）。"""
    head_coach_subq = TeamCoach.objects.filter(
        team=OuterRef("pk"),
        status=enums.RelationStatus.CURRENT,
        duty=enums.CoachDuty.HEAD_COACH,
    ).values("person__full_name")[:1]
    return queryset.annotate(
        member_count=Count(
            "teammember",
            filter=Q(teammember__status=enums.RelationStatus.CURRENT),
        ),
        head_coach_name=Subquery(head_coach_subq),
    )


def _require_admin_scope(account) -> None:
    """球队数据范围门卫：一期仅 ADMIN 全量。

    UNIVERSITY（本校）/TEAM（本队）依赖 org→school 映射（A′-min 未交付），
    按安全约束不放行（同 persons.apply_person_scope 先例）；映射交付后
    仅需扩展本函数，接口层不变。
    """
    if ADMIN in (getattr(account, "roles", ()) or ()):
        return
    raise ApiError(DATA_SCOPE_DENIED)


def list_teams(filters: dict, account) -> QuerySet[Team]:
    """GET /teams：keyword/category/status 筛选 + 数据范围 + 分页。"""
    _require_admin_scope(account)
    queryset = _team_queryset()
    keyword = filters.get("keyword")
    if keyword:
        queryset = queryset.filter(name__icontains=keyword)
    if filters.get("category"):
        queryset = queryset.filter(category_code=filters["category"])
    if filters.get("status"):
        queryset = queryset.filter(audit_status=filters["status"])
    return _annotate_team(queryset).order_by("-created_at", "-id")


def get_team(team_id: str, account) -> Team:
    """GET /teams/{id}：详情（不存在/软删 → 404；非 ADMIN → 403 范围拒绝）。"""
    from apps.persons.services import parse_path_id

    _require_admin_scope(account)
    team = _annotate_team(_team_queryset()).filter(pk=parse_path_id(team_id)).first()
    if team is None:
        raise ApiError(NOT_FOUND)
    return team


def _resolve_school(school_org_id: Optional[str], account) -> Optional[int]:
    """创建用：前端 schoolOrgId（Organization id）→ persons.School 主键解析。

    A′-min 方案（成员2已确认）：业务侧经 accounts.ensure_business_org 获取
    组织后保存解析后的业务 ID，过滤按 ID 执行；School 无 code 视为未注册。
    该接口尚未交付（2026-10-24 全仓复核），映射交付前一律明确报错，
    不把 Organization 主键当作 School 主键，不绕过、不直写 Organization。
    """
    from apps.accounts import services as accounts_services

    resolver = getattr(accounts_services, "ensure_business_org", None)
    if resolver is None:
        raise ApiError(STATE_CONFLICT, _ORG_SERVICE_NOT_DELIVERED)
    # 映射交付后在此实现：ensure_business_org(...) → Organization(SCHOOL)
    # → School.objects.filter(code=org.code) → school.pk；School 无 code=未注册
    raise ApiError(STATE_CONFLICT, _ORG_SERVICE_NOT_DELIVERED)  # pragma: no cover


def create_team(data: dict, school_id: Optional[int], account) -> Team:
    """POST /teams：创建球队 + team_no 回填（事务内；API 层负责审计留痕）。

    - school_id 由 API 层经 _resolve_school 解析后传入（服务层保留参数以便
      独立测试编号回填/媒体映射等规则）；
    - 忽略无承载字段：shortName/gender/campusName（schema 声明以放行前端
      真实 payload，服务层不写入，不虚构保存结果）；
    - audit_status 用模型默认 APPROVED（Q2 审核流程不启用）。
    """
    with transaction.atomic():
        team = Team.objects.create(
            name=data["name"],
            category_code=data["groupCategory"],
            school_id=school_id,
            introduction=data.get("intro"),
            flag_file_id=data.get("flagFileId"),
            logo_file_id=data.get("emblemFileId"),
            group_photo_file_id=data.get("groupPhotoFileId"),
            status=enums.EnableStatus.ACTIVE,
        )
        # team_no 按 DDL 列注释回填：T+年份+8位id（person_no 同款；pk 全局唯一
        # →编号天然唯一，uk_team_no 兜底；整段在事务内，失败即整体回滚）
        team.team_no = f"T{team.created_at:%Y}{team.pk:08d}"
        team.save()
    return team


def update_team(team_id: str, payload: dict, account) -> Team:
    """PUT /teams/{id}：version 乐观锁 + 可变字段替换（exclude_unset 语义）。

    可变字段：name/intro/flagFileId/emblemFileId/groupPhotoFileId（均有冻结承载）；
    接受但忽略：schoolOrgId/groupCategory/gender/campusName/shortName/coachName
    （前端原样回传的不可变/无承载字段，不因未变化字段导致编辑失败）。
    """
    from apps.persons.services import ensure_version, parse_path_id

    _require_admin_scope(account)
    team = _annotate_team(_team_queryset()).filter(pk=parse_path_id(team_id)).first()
    if team is None:
        raise ApiError(NOT_FOUND)
    ensure_version(team, payload["version"])
    mapping = {
        "name": "name",
        "intro": "introduction",
        "flagFileId": "flag_file_id",
        "emblemFileId": "logo_file_id",
        "groupPhotoFileId": "group_photo_file_id",
    }
    changed = False
    for src, dst in mapping.items():
        if src in payload:
            setattr(team, dst, payload[src])
            changed = True
    if changed:
        team.save()
    return _annotate_team(_team_queryset()).filter(pk=team.pk).first()


# ---------------------------------------------------------------- 球队成员（批次 B）
# 契约对齐 zuqiu TeamMembersView（teamApi.members/addMember/searchMembers/
# updateMember/removeMember，index.ts L141-149）。状态严格用冻结枚举
# CURRENT/EXITED（用户裁定：不虚构 PENDING/APPROVED/REJECTED 审核状态）；
# position_code 正式值域（base_dict_item 'position'）尚未导入——只读核查
# 2026-10-24：dict_type 'position' 在库、dict_item 全表 0 行、设计文档仅
# 写"字典 position"无正式代码 → 透传存取，不创造字典规则。

_MEMBER_NOT_TEAM = "成员关系不存在或不属于该球队"


def _team_or_404(team_id: str) -> Team:
    """成员操作用：球队必须存在且未软删（数据范围门卫由各服务先调）。"""
    from apps.persons.services import parse_path_id

    team = Team.objects.filter(pk=parse_path_id(team_id), is_deleted=False).first()
    if team is None:
        raise ApiError(NOT_FOUND)
    return team


def _member_queryset() -> QuerySet[TeamMember]:
    """成员关系查询：排除软删球队/软删人员 + person 一次取齐。"""
    return TeamMember.objects.filter(
        team__is_deleted=False, person__is_deleted=False
    ).select_related("person", "team")


def member_list(team_id: str, filters: dict, account) -> QuerySet[TeamMember]:
    """GET /teams/{id}/members：keyword 匹配球员姓名 + 分页。

    返回 CURRENT 与 EXITED 全量关系（状态真实输出，历史可见）；
    mock 的 maskedIdNumber 检索与 status 筛选参数为演示逻辑，前端实际
    只传 page/pageSize/keyword（TeamMembersView L55-59），不照搬。
    """
    _require_admin_scope(account)
    team = _team_or_404(team_id)
    queryset = _member_queryset().filter(team=team)
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        queryset = queryset.filter(person__full_name__icontains=keyword)
    return queryset.order_by("-created_at", "-id")


def member_get(team_id: str, member_id: str, account) -> TeamMember:
    """GET /teams/{id}/members/{member_id}（批次 D-2）：成员关系详情。

    与 member_list 同一查询事实源（_member_queryset）；成员不属该队/不存在
    → 404（_MEMBER_NOT_TEAM 语义，不泄露他队关系）；EXITED 历史关系可读
    （状态直出冻结枚举）；数据范围同列表（一期仅 ADMIN）。
    """
    from apps.persons.services import parse_path_id

    _require_admin_scope(account)
    team = _team_or_404(team_id)
    member = _member_queryset().filter(team=team, pk=parse_path_id(member_id)).first()
    if member is None:
        raise ApiError(NOT_FOUND, _MEMBER_NOT_TEAM)
    return member


def member_search(team_id: str, keyword: str, account) -> list:
    """POST /teams/{id}/members/search：候选球员（复用 persons 读链事实源）。

    - ATHLETE 身份=AthleteProfile 行存在性（方案C 完整类别事实源，与
      persons kind 过滤同源），非 person_category 单值列；
    - 排除本队现役成员（R2 前置；EXITED 历史行可重新加入）；
    - 上限 20 沿用 persons search_people 保护上限；maskedIdNumber 检索
      为 mock 演示逻辑，persons 域已裁定不照搬，此处同样不实现。
    """
    from apps.persons.schemas import person_out
    from apps.persons.services import people_queryset

    _require_admin_scope(account)
    team = _team_or_404(team_id)
    queryset = people_queryset().filter(athlete_profile_exists=True)
    term = (keyword or "").strip()
    if term:
        queryset = queryset.filter(full_name__icontains=term)
    current_ids = TeamMember.objects.filter(
        team=team, status=enums.RelationStatus.CURRENT
    ).values_list("person_id", flat=True)
    queryset = queryset.exclude(pk__in=current_ids)
    return [person_out(p) for p in queryset.order_by("-id")[:20]]


def member_add(team_id: str, payload: dict, account) -> TeamMember:
    """POST /teams/{id}/members：校验球队/人员 + R2 现役唯一 + R3 球衣号唯一。

    - 人员须存在、未软删且具备 ATHLETE 身份（档案行存在性）；非运动员、
      重复现役、球衣号冲突均 409 STATE_CONFLICT（服务层规则 R2/R3，无 DB 约束）；
    - joined_at=服务器当日（前端 payload 无日期字段；实现选择，联调确认）；
    - status 用模型默认 CURRENT（冻结枚举真实状态，不制造审核流程）；
      position 透传存取（字典值域缺口，见模块注释）。
    """
    from apps.persons.models import AthleteProfile, Person
    from apps.persons.services import parse_path_id

    _require_admin_scope(account)
    team = _team_or_404(team_id)
    person = Person.objects.filter(
        pk=parse_path_id(payload["athleteId"]), is_deleted=False
    ).first()
    if person is None:
        raise ApiError(NOT_FOUND, "运动员不存在或已删除")
    if not AthleteProfile.objects.filter(person=person).exists():
        raise ApiError(STATE_CONFLICT, "该人员不具备运动员身份，不能加入球队名单")
    with transaction.atomic():
        if TeamMember.objects.filter(
            team=team,
            person=person,
            status=enums.RelationStatus.CURRENT,
        ).exists():
            raise ApiError(STATE_CONFLICT, "该球员已是本队现役成员（R2 现役唯一）")
        jersey = payload.get("jerseyNumber")
        if (
            jersey is not None
            and TeamMember.objects.filter(
                team=team,
                status=enums.RelationStatus.CURRENT,
                jersey_number=jersey,
            ).exists()
        ):
            raise ApiError(STATE_CONFLICT, f"球衣号 {jersey} 已被本队现役成员使用（R3）")
        member = TeamMember.objects.create(
            team=team,
            person=person,
            jersey_number=jersey,
            position_code=payload.get("position"),
            joined_at=timezone.localdate(),
        )
    return member


def member_update(team_id: str, member_id: str, payload: dict, account) -> TeamMember:
    """PUT /teams/{id}/members/{member_id}：version 乐观锁 + 白名单字段。

    - 可变：jerseyNumber/position（exclude_unset 语义，仅前端提交键应用）；
    - 不应用：athleteId（回传字段，重绑人员=移出+重加或转队）、
      effectiveFrom/effectiveTo（前端回传/无冻结承载，防重写加入日期）；
    - EXITED 历史行禁改（模型注释"单向流转，历史禁改"，安全失败 409）；
    - 球衣号 R3 校验仅对现役成员排除自身。
    """
    from apps.persons.services import ensure_version, parse_path_id

    _require_admin_scope(account)
    team = _team_or_404(team_id)
    member = _member_queryset().filter(team=team, pk=parse_path_id(member_id)).first()
    if member is None:
        raise ApiError(NOT_FOUND, _MEMBER_NOT_TEAM)
    ensure_version(member, payload["version"])
    if member.status != enums.RelationStatus.CURRENT:
        raise ApiError(STATE_CONFLICT, "已离队成员属历史关系，禁止修改")
    changed = False
    if "jerseyNumber" in payload:
        jersey = payload["jerseyNumber"]
        if (
            jersey is not None
            and TeamMember.objects.filter(
                team=team,
                status=enums.RelationStatus.CURRENT,
                jersey_number=jersey,
            )
            .exclude(pk=member.pk)
            .exists()
        ):
            raise ApiError(STATE_CONFLICT, f"球衣号 {jersey} 已被本队现役成员使用（R3）")
        member.jersey_number = jersey
        changed = True
    if "position" in payload:
        member.position_code = payload["position"]
        changed = True
    if changed:
        member.save()
    return _member_queryset().filter(pk=member.pk).first()


def member_remove(team_id: str, member_id: str, version: int, account) -> TeamMember:
    """DELETE /teams/{id}/members/{member_id}?version=：软退出（CURRENT→EXITED）。

    - 禁止物理删除（冻结模型）；version 乐观锁 + 跨球队归属校验（404）；
    - 重复移出（已 EXITED）→ 409；source=TRANSFER 的关系行 → 409 安全失败
      （转队产生的关系退出规则留待转队域，不自行决定口径）；
    - PENDING 转出单阻断：存在 person=成员且 from_team=本队、状态 PENDING 的
      TeamTransfer 时拒绝移出，成员与转队单均不变（转队流程优先）。
    """
    from apps.persons.services import ensure_version, parse_path_id

    _require_admin_scope(account)
    team = _team_or_404(team_id)
    member = _member_queryset().filter(team=team, pk=parse_path_id(member_id)).first()
    if member is None:
        raise ApiError(NOT_FOUND, _MEMBER_NOT_TEAM)
    ensure_version(member, version)
    if member.status != enums.RelationStatus.CURRENT:
        raise ApiError(STATE_CONFLICT, "该成员已离队，不能重复移出")
    if member.source == enums.RelationSource.TRANSFER:
        raise ApiError(
            STATE_CONFLICT, "转队产生的成员关系不能直接移出（安全失败，规则待转队域确认）"
        )
    if TeamTransfer.objects.filter(
        person=member.person,
        from_team=team,
        status=enums.TransferStatus.PENDING,
    ).exists():
        raise ApiError(STATE_CONFLICT, "该球员存在待核验的转出单，暂不能直接移出")
    with transaction.atomic():
        member.status = enums.RelationStatus.EXITED
        member.left_at = timezone.now()
        member.save()
    return member
