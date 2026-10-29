"""球队域业务逻辑：球队档案 CRUD（批次 A）+ 球队成员管理（批次 B）。

依据：docs/design/成员4-DDL草案-V1.4.sql（V1.4-r1，冻结）+ zuqiu 前端实码契约。
- Team 软删（is_deleted）；关系表与转队单禁止物理删除（模型 docstring）；
- team_no 按 DDL 列注释回填：T+年份+8位主键id（与 person_no 同款，pk 派生
  全局唯一 + uk_team_no 兜底，无计数器竞态）；
- audit_status 字段保留（Q2 审核流程不启用），创建即默认 APPROVED；
- 成员状态严格 CURRENT/EXITED（不虚构审核状态）；R2 现役唯一/R3 队内
  现役球衣号唯一为服务层规则；PENDING 转出单阻断直接移出；
- 写链禁 queryset.update()（绕过 auto_now），统一 instance.save()；
- 学校组织映射（A′-min，2026-10-25 契约确认并交付）：schoolOrgId（Organization
  节点 ID）→ 节点校验（存在/未停用/学校类类型）→ org.code 反查 School 主档；
  create_team 同事务经 accounts.ensure_business_org 建立 TEAM 组织节点
  （code=team_no，parent=所属学校/高校组织节点）；
- 无冻结承载的前端字段（shortName/gender/campusName/coachName 输入值）：
  声明但不持久化不回显（同 persons education/coachCategory 先例）；
  coachName 输出仅由现役（CURRENT）主教练关系合成（list_own_teams 先例）。

合并说明（2026-10-26 二轮收编 origin/main）：main 曾先行交付只读查询版
（为对战、报名与门户接口提供球队名称解析，数据范围沿用一期仅 ADMIN 的
冻结约定）。其独有检索行为已并入本文件：list_teams 的 keyword 同时命中
队名与球队编号；get_team 的路径标识兼容主键 id 与 team_no 回退解析
（team_no 形如 T+年份+8位id，与纯数字主键天然不混淆）。注解/范围门卫
两版实现同构，以本文件完整版为准。
"""

from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.db.models import Count, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from apps.common import enums, scopes
from apps.common.errors import (
    DATA_SCOPE_DENIED,
    INVALID_ARGUMENT,
    NOT_FOUND,
    STATE_CONFLICT,
    ApiError,
    field_errors,
)
from apps.common.permissions import ADMIN
from apps.teams.models import Team, TeamCoach, TeamMember, TeamTransfer

# ensure_business_org 已自 origin/main 最小移植进 accounts 域（2026-10-25，
# A′-min 契约经用户批准）；球队与组织节点的映射统一经该入口完成。


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
    """球队数据范围门卫（A′ 第 2 批扩展，预留扩展点收口在本函数）。

    - 全局 ADMIN（scopes.scope_org_codes 返回 None，即无组织绑定的 ADMIN）
      全库放行；挂组织 ADMIN 只能访问绑定组织及其子树；
    - UNIVERSITY / TEAM 等其余账号必须具有有效组织范围（非空编码列表）；
      空范围（无绑定/绑定不存在/绑定已软删）一律拒绝；
    - 本函数只做"角色→范围"门卫，具体目标球队是否在范围内由各服务继续
      执行逐对象校验：scopes.ensure_team_in_scope（模块限定调用，避免与
      enrollments 域同名函数混淆）；list_teams 用 _team_scope_filter 谓词
      在查询集层过滤，不做"返回全部后由前端隐藏"。
    """
    codes = scopes.scope_org_codes(account)
    if codes is None or codes:
        return
    raise ApiError(DATA_SCOPE_DENIED)


def _team_scope_filter(account) -> Optional[Q]:
    """list_teams 范围谓词（与 ensure_team_in_scope 同口径）。

    全局管理员 → None（不过滤）；其余账号 → 限定为账号组织子树内未停用的
    TEAM 节点（A′ 映射：Organization.code=team_no）对应的球队；节点软删经
    默认 manager 自然排除。空范围已在门卫处拒绝，不会走到这里；子树内无
    有效 TEAM 节点时谓词为空集（列表为空而非报错）。
    """
    from apps.accounts.models import Organization

    if scopes.scope_org_codes(account) is None:
        return None
    team_codes = Organization.objects.filter(
        pk__in=scopes.org_scope_ids(account), org_type="TEAM", enabled=True
    ).values_list("code", flat=True)
    return Q(team_no__in=team_codes)


def verify_team_owner(account, owner_id: str) -> None:
    """TEAM 附件归属校验器（经 TeamsConfig.ready() 注册进 attachments）。

    TEAM 归属文件的上传与读取均由本校验器裁决业务资源所有权
    （attachments._check_owner / can_access 调用）：
    - 归属记录不存在（球队不存在或已软删）→ 404，默认拒绝；
    - 数据范围：门卫 _require_admin_scope（角色→范围）+ A′ 编码映射
      逐对象校验 scopes.ensure_team_in_scope（A′ 第 2 批）——全局 ADMIN
      全库、挂组织 ADMIN 本子树、UNIVERSITY 本校球队、TEAM 仅本队；
      映射缺失/节点停用/空范围一律拒绝，不因本轮改动放宽跨范围访问。
    """
    from apps.persons.services import parse_path_id

    raw = str(owner_id or "").strip()
    if not raw:
        raise ApiError(DATA_SCOPE_DENIED, "文件归属对象未确定，暂无法校验归属")
    team = Team.objects.filter(
        pk=parse_path_id(raw, "ownerId"), is_deleted=False
    ).first()
    if team is None:
        raise ApiError(NOT_FOUND, "归属球队不存在或已删除")
    _require_admin_scope(account)
    scopes.ensure_team_in_scope(account, team)


def list_teams(filters: dict, account) -> QuerySet[Team]:
    """GET /teams：keyword/category/status 筛选 + 数据范围 + 分页。

    数据范围（A′ 第 2 批）：服务端谓词过滤（_team_scope_filter），越界球队
    不出现在查询集中，而非返回全部后由前端隐藏。
    keyword 并集（二轮收编，main 只读版行为）：同时命中队名与球队编号
    （team_no icontains），供前端球队下拉/对阵名称按编号检索。
    """
    _require_admin_scope(account)
    queryset = _team_queryset()
    scope_q = _team_scope_filter(account)
    if scope_q is not None:
        queryset = queryset.filter(scope_q)
    keyword = filters.get("keyword")
    if keyword:
        queryset = queryset.filter(
            Q(name__icontains=keyword) | Q(team_no__icontains=keyword)
        )
    if filters.get("category"):
        queryset = queryset.filter(category_code=filters["category"])
    if filters.get("status"):
        queryset = queryset.filter(audit_status=filters["status"])
    return _annotate_team(queryset).order_by("-created_at", "-id")


def get_team(team_id: str, account) -> Team:
    """GET /teams/{id}：详情（不存在/软删 → 404；范围外目标球队 → 403
    DATA_SCOPE_DENIED）。

    路径标识解析并集（二轮收编，main 只读版行为）：纯数字优先按主键 id，
    未命中或非数字按 team_no 回退——供对战/报名/门户按编号解析球队名称；
    两者均未命中 → 404（软删行被 queryset 排除，同样 404）。
    解析成功后按 A′ 编码映射执行逐对象范围校验（第 2 批）。
    """
    _require_admin_scope(account)
    raw = str(team_id or "").strip()
    queryset = _annotate_team(_team_queryset())
    team = None
    if raw.isdigit():
        team = queryset.filter(pk=int(raw)).first()
    if team is None:
        team = queryset.filter(team_no=raw).first()
    if team is None:
        raise ApiError(NOT_FOUND)
    scopes.ensure_team_in_scope(account, team)
    return team


_SCHOOL_ORG_TYPES = ("SCHOOL", "UNIVERSITY")


def _resolve_school(school_org_id) -> tuple:
    """创建用：前端 schoolOrgId（Organization 节点 ID）→ (组织节点, School 主档)。

    A′-min 契约（2026-10-25 用户确认）：schoolOrgId 一律按组织节点主键查询，
    不当作 School.code 或 Organization.code；节点必须存在、未删除未停用、
    类型属于学校类（SCHOOL/UNIVERSITY，CITY/PROVINCE/TEAM 等一律拒绝）；
    随后按 org.code=School.code 反查学校主档（Team.school 外键固化，必须有
    业务主档承载）。任一不满足 → 400 INVALID_ARGUMENT + 可定位字段明细。
    返回的 org 作为 TEAM 组织节点的父节点（见 create_team）。
    """
    from apps.accounts.models import Organization
    from apps.persons.models import School

    raw = str(school_org_id or "").strip()
    if not raw:
        raise ApiError(
            INVALID_ARGUMENT, "所属学校组织不能为空", field_errors([("schoolOrgId", "必填")])
        )
    org = Organization.objects.filter(pk=raw).first()
    if org is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "所属学校组织不存在",
            field_errors([("schoolOrgId", "组织节点不存在")]),
        )
    if org.deleted_at is not None or not org.enabled:
        raise ApiError(
            INVALID_ARGUMENT,
            "所属学校组织已停用",
            field_errors([("schoolOrgId", "组织节点已停用或已删除")]),
        )
    if org.org_type not in _SCHOOL_ORG_TYPES:
        raise ApiError(
            INVALID_ARGUMENT,
            "所属组织类型不符",
            field_errors(
                [("schoolOrgId", f"球队必须挂在 SCHOOL/UNIVERSITY 节点下，当前为 {org.org_type}")]
            ),
        )
    school = School.objects.filter(code=org.code, is_active=True).first()
    if school is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "组织节点未关联学校主档",
            field_errors([("schoolOrgId", f"组织节点 {org.code} 未关联学校主档")]),
        )
    return org, school


def create_team(data: dict, org, school, account) -> Team:
    """POST /teams：创建球队 + team_no 回填 + TEAM 组织节点（同一事务）。

    - org/school 由 API 层经 _resolve_school 解析后传入（服务层保留参数以便
      独立测试编号回填/媒体映射等规则）；
    - TEAM 组织节点经 accounts.ensure_business_org 在同一事务建立（A′-min
      契约 2026-10-25）：Organization.code=team_no、name=队名、parent=所属
      学校/高校组织节点；幂等/复活/类型冲突 409/并发唯一约束兜底/层级校验
      复用 accounts 实现，异常上抛 → 本事务整体回滚，不产生孤立球队或孤立
      组织节点（accounts 侧不吞异常）；
    - 忽略无承载字段：shortName/gender/campusName（schema 声明以放行前端
      真实 payload，服务层不写入，不虚构保存结果）；
    - audit_status 用模型默认 APPROVED（Q2 审核流程不启用）。
    """
    from apps.accounts.services import ensure_business_org

    # A′ 第 2 批：创建球队必须校验目标学校组织节点在当前账号范围内
    # （全局 ADMIN 全库；其余账号仅限本组织子树；空范围一律拒绝）。
    # schoolOrgId 的存在性/类型/主档校验在 API 层 _resolve_school（400），
    # 范围校验在此层（403），职责分层与既有错误码约定不变。
    scopes.ensure_org_in_scope(account, org.pk)

    with transaction.atomic():
        team = Team.objects.create(
            name=data["name"],
            category_code=data["groupCategory"],
            school=school,
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
        # TEAM 组织节点与球队同事务建立（契约：球队创建失败不得留下孤立组织
        # 节点，反之亦然；业务模块禁止直写 Organization，统一走 accounts 唯一
        # 同步入口，父层级校验由 ensure_business_org 内部完成）
        ensure_business_org("TEAM", team.team_no, team.name, parent_org_id=org.pk)
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
    scopes.ensure_team_in_scope(account, team)
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
    scopes.ensure_team_in_scope(account, team)
    queryset = _member_queryset().filter(team=team)
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        queryset = queryset.filter(person__full_name__icontains=keyword)
    return queryset.order_by("-created_at", "-id")


def member_get(team_id: str, member_id: str, account) -> TeamMember:
    """GET /teams/{id}/members/{member_id}（批次 D-2）：成员关系详情。

    与 member_list 同一查询事实源（_member_queryset）；成员不属该队/不存在
    → 404（_MEMBER_NOT_TEAM 语义，不泄露他队关系）；EXITED 历史关系可读
    （状态直出冻结枚举）；数据范围同列表（A′ 第 2 批逐对象校验）。
    """
    from apps.persons.services import parse_path_id

    _require_admin_scope(account)
    team = _team_or_404(team_id)
    scopes.ensure_team_in_scope(account, team)
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
    scopes.ensure_team_in_scope(account, team)
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
    scopes.ensure_team_in_scope(account, team)
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
    scopes.ensure_team_in_scope(account, team)
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
    scopes.ensure_team_in_scope(account, team)
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
