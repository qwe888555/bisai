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
  （code=team_no，parent=所属学校/院校组织节点）；
- 无冻结承载的前端字段（shortName/gender/campusName/coachName 输入值）：
  声明但不持久化不回显（同 persons education/coachCategory 先例）；
  coachName 输出仅由现役（CURRENT）主教练关系合成（list_own_teams 先例）。

合并说明（2026-10-26 二轮收编 origin/main）：main 曾先行交付只读查询版
（为对战、报名与门户接口提供球队名称解析，当时数据范围仅 ADMIN）。其独有
检索行为已并入本文件：list_teams 的 keyword 同时命中队名与球队编号；
get_team 的路径标识兼容主键 id 与 team_no 回退解析（team_no 形如
T+年份+8位id，与纯数字主键天然不混淆）。注解两版实现同构，以本文件完整版为准。

数据范围（2026-10-27 交付）：无组织的全局管理员全量；TEAM 账号仅本队
（team_no=账号组织节点 code）；其余角色（UNIVERSITY/CITY/PROVINCE_REVIEWER/
挂组织的 ADMIN）按组织子树内的学校类组织 code 过滤 School（A′-min 约定
org.code=School.code）。账号无可见学校范围时抛 403 DATA_SCOPE_DENIED，
不静默返回空列表；详情接口对范围内的不存在/不可见球队统一 404。
"""

from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.db.models import Count, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from apps.common import enums
from apps.common.errors import (
    DATA_SCOPE_DENIED,
    INVALID_ARGUMENT,
    NOT_FOUND,
    STATE_CONFLICT,
    ApiError,
    field_errors,
)
from apps.common.permissions import TEAM
from apps.common.scopes import (
    account_org_code,
    ensure_org_in_scope,
    is_global_admin,
    visible_school_codes,
)
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


def visible_teams(
    account, queryset: Optional[QuerySet[Team]] = None
) -> QuerySet[Team]:
    """当前账号可见的球队（未软删）；无任何范围 → 403 DATA_SCOPE_DENIED。

    - 无组织的全局管理员：全量；
    - roles 含 TEAM：仅 team_no=账号组织节点 code 的本队，org code 为空拒绝；
    - 其余：组织子树内学校类组织的 code 过滤 school（org.code=School.code），
      可见学校集合为空 → 403（保持原范围拒绝语义，不静默返回空列表）。
    """
    qs = queryset if queryset is not None else _team_queryset()
    if is_global_admin(account):
        return qs
    if TEAM in (getattr(account, "roles", ()) or ()):
        code = account_org_code(account)
        if not code:
            raise ApiError(DATA_SCOPE_DENIED, "球队账号未绑定球队组织，无法确定数据范围")
        return qs.filter(team_no=code)
    codes = visible_school_codes(account)
    if not codes:
        raise ApiError(DATA_SCOPE_DENIED, "当前账号无可访问的学校数据范围")
    return qs.filter(school__code__in=codes)


def ensure_team_visible(account, team) -> None:
    """球队必须在账号可见范围内，否则 403 DATA_SCOPE_DENIED。

    供本模块写操作/成员操作与 attachments、persons、materials 复用；
    与 get_team 的 404 语义不同：已取到球队后按范围拒绝写读，保留原
    “范围拒绝先于业务校验”的 403 语义。
    """
    if not visible_teams(account).filter(pk=team.pk).exists():
        raise ApiError(DATA_SCOPE_DENIED, "球队不在当前账号数据范围内")


def verify_team_owner(account, owner_id: str) -> None:
    """TEAM 附件归属校验器（经 TeamsConfig.ready() 注册进 attachments）。

    TEAM 归属文件的上传与读取均由本校验器裁决业务资源所有权
    （attachments._check_owner / can_access 调用）：
    - 归属记录不存在（球队不存在或已软删）→ 404，默认拒绝；
    - 数据范围复用 ensure_team_visible：全局管理员全量、TEAM 账号本队、
      学校/市州/省级按组织子树内的学校范围；不可见 → 403 DATA_SCOPE_DENIED。
    """
    from apps.persons.services import parse_path_id

    raw = str(owner_id or "").strip()
    if not raw:
        raise ApiError(DATA_SCOPE_DENIED, "文件归属对象未确定，暂无法校验归属")
    team = Team.objects.filter(pk=parse_path_id(raw, "ownerId"), is_deleted=False).first()
    if team is None:
        raise ApiError(NOT_FOUND, "归属球队不存在或已删除")
    ensure_team_visible(account, team)


def list_teams(filters: dict, account) -> QuerySet[Team]:
    """GET /teams：keyword/category/status 筛选 + 数据范围 + 分页。

    keyword 并集（二轮收编，main 只读版行为）：同时命中队名与球队编号
    （team_no icontains），供前端球队下拉/对阵名称按编号检索。
    筛选先构造 queryset，再由 visible_teams 套范围，筛选条件不扩大权限。
    """
    queryset = _team_queryset()
    keyword = filters.get("keyword")
    if keyword:
        queryset = queryset.filter(
            Q(name__icontains=keyword) | Q(team_no__icontains=keyword)
        )
    if filters.get("category"):
        queryset = queryset.filter(category_code=filters["category"])
    if filters.get("status"):
        queryset = queryset.filter(audit_status=filters["status"])
    return _annotate_team(visible_teams(account, queryset)).order_by("-created_at", "-id")


def get_team(team_id: str, account) -> Team:
    """GET /teams/{id}：详情（不可见/不存在/软删 → 404，不泄露存在性）。

    路径标识解析并集（二轮收编，main 只读版行为）：纯数字优先按主键 id，
    未命中或非数字按 team_no 回退——供对战/报名/门户按编号解析球队名称；
    两者均未命中 → 404（软删行被 queryset 排除，同样 404）。
    """
    raw = str(team_id or "").strip()
    queryset = _annotate_team(visible_teams(account))
    team = None
    if raw.isdigit():
        team = queryset.filter(pk=int(raw)).first()
    if team is None:
        team = queryset.filter(team_no=raw).first()
    if team is None:
        raise ApiError(NOT_FOUND)
    return team


_SCHOOL_ORG_TYPES = ("SCHOOL", "UNIVERSITY")


def _ensure_create_org_scope(account, org) -> None:
    """创建球队的写入范围：schoolOrgId 解析出的学校组织必须在范围内。

    - 全局管理员直接放行；
    - TEAM 账号允许其绑定 TEAM 节点的父节点（本校学校节点），否则拒绝；
    - 其余角色（UNIVERSITY/CITY/PROVINCE_REVIEWER/挂组织 ADMIN）用
      ensure_org_in_scope 按组织子树判定。
    """
    if is_global_admin(account):
        return
    if TEAM in (getattr(account, "roles", ()) or ()):
        from apps.accounts.models import Organization

        raw_org_id = getattr(account, "organization_id", None)
        if not raw_org_id:
            raise ApiError(DATA_SCOPE_DENIED, "球队账号未绑定球队组织，无法创建球队")
        parent_id = (
            Organization.objects.filter(pk=raw_org_id)
            .values_list("parent_id", flat=True)
            .first()
        )
        if not parent_id or str(parent_id) != str(org.pk):
            raise ApiError(DATA_SCOPE_DENIED, "球队账号只能在本校组织下创建球队")
        return
    ensure_org_in_scope(account, org.pk)


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
      学校/院校组织节点；幂等/复活/类型冲突 409/并发唯一约束兜底/层级校验
      复用 accounts 实现，异常上抛 → 本事务整体回滚，不产生孤立球队或孤立
      组织节点（accounts 侧不吞异常）；
    - 忽略无承载字段：shortName（TeamCreateBody 不含；资料列落库见 update_team）；
      gender/campusName 自 2026-10-29 起落库（补列，见 models.Team）；
    - audit_status 用模型默认 APPROVED（Q2 审核流程不启用）；
    - 写入范围：schoolOrgId 解析出的学校组织须在账号范围内（全局管理员
      全量、TEAM 限本校父节点、其余按组织子树），否则 403 DATA_SCOPE_DENIED。
    """
    from apps.accounts.services import ensure_business_org

    _ensure_create_org_scope(account, org)
    with transaction.atomic():
        team = Team.objects.create(
            name=data["name"],
            category_code=data["groupCategory"],
            school=school,
            introduction=data.get("intro"),
            flag_file_id=data.get("flagFileId"),
            logo_file_id=data.get("emblemFileId"),
            group_photo_file_id=data.get("groupPhotoFileId"),
            # 2026-10-29 补列落库：gender/campusName（TeamCreateBody 声明字段）
            gender=data.get("gender"),
            campus_name=data.get("campusName"),
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

    可变字段：name/intro/flagFileId/emblemFileId/groupPhotoFileId（均有冻结承载）
    + shortName/campusName/gender/coachName（2026-10-29 补列落库，保存即回显，
    避免“保存成功但值消失”）；
    接受但忽略：schoolOrgId/groupCategory（前端原样回传的不可变/无承载字段，
    不因未变化字段导致编辑失败）。
    """
    from apps.persons.services import ensure_version, parse_path_id

    team = _annotate_team(_team_queryset()).filter(pk=parse_path_id(team_id)).first()
    if team is None:
        raise ApiError(NOT_FOUND)
    ensure_team_visible(account, team)
    ensure_version(team, payload["version"])
    mapping = {
        "name": "name",
        "intro": "introduction",
        "flagFileId": "flag_file_id",
        "emblemFileId": "logo_file_id",
        "groupPhotoFileId": "group_photo_file_id",
        # 2026-10-29 补列落库：资料字段（保存即回显）
        "shortName": "short_name",
        "campusName": "campus_name",
        "gender": "gender",
        "coachName": "coach_name",
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
    """成员操作用：球队必须存在且未软删（范围判定由调用方随后 ensure_team_visible）。"""
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
    team = _team_or_404(team_id)
    ensure_team_visible(account, team)
    queryset = _member_queryset().filter(team=team)
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        queryset = queryset.filter(person__full_name__icontains=keyword)
    return queryset.order_by("-created_at", "-id")


def member_get(team_id: str, member_id: str, account) -> TeamMember:
    """GET /teams/{id}/members/{member_id}（批次 D-2）：成员关系详情。

    与 member_list 同一查询事实源（_member_queryset）；成员不属该队/不存在
    → 404（_MEMBER_NOT_TEAM 语义，不泄露他队关系）；EXITED 历史关系可读
    （状态直出冻结枚举）；球队不可见 → 403 DATA_SCOPE_DENIED，成员不属该队
    → 404（_MEMBER_NOT_TEAM 语义）。
    """
    from apps.persons.services import parse_path_id

    team = _team_or_404(team_id)
    ensure_team_visible(account, team)
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

    team = _team_or_404(team_id)
    ensure_team_visible(account, team)
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

    team = _team_or_404(team_id)
    ensure_team_visible(account, team)
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

    team = _team_or_404(team_id)
    ensure_team_visible(account, team)
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

    team = _team_or_404(team_id)
    ensure_team_visible(account, team)
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


# ---------------------------------------------------------------- 球队教练（批次 C）
# 契约对齐 zuqiu teamApi.coaches/addCoach/removeCoach（index.ts L136-139）与
# TeamCoachesView 实码。category 值域以契约为准（HEAD_COACH/ASSISTANT_COACH/
# MANAGER/OTHER），duty 冻结列透传存取（position_code 先例：冻结值域与契约
# 不一致时不做映射，不虚构字典规则）；R4 现役主教练唯一按字面 HEAD_COACH 判定。

_COACH_CATEGORIES = ("HEAD_COACH", "ASSISTANT_COACH", "MANAGER", "OTHER")
_COACH_NOT_TEAM = "教练关系不存在或不属于该球队"


def _coach_queryset() -> QuerySet[TeamCoach]:
    """教练关系查询：排除软删球队/软删人员 + person 一次取齐。"""
    return TeamCoach.objects.filter(
        team__is_deleted=False, person__is_deleted=False
    ).select_related("person", "team")


def coach_list(team_id: str, filters: dict, account) -> QuerySet[TeamCoach]:
    """GET /teams/{id}/coaches：category/reviewStatus 筛选 + 分页。

    - 只返回现役（CURRENT）关系：前端无状态列、mock 移除即从列表消失，
      历史行留库可审计（软退出语义与列表口径一致）；
    - category 透传过滤 duty 列；reviewStatus 按人员审核状态过滤
      （person_out 先例：直出 audit_status，关联级审核列不存在，
      审核对象留待 D1 确认，不虚构审核流程）。
    """
    team = _team_or_404(team_id)
    ensure_team_visible(account, team)
    queryset = _coach_queryset().filter(team=team, status=enums.RelationStatus.CURRENT)
    category = (filters.get("category") or "").strip()
    if category:
        queryset = queryset.filter(duty=category)
    review = (filters.get("reviewStatus") or "").strip()
    if review:
        queryset = queryset.filter(person__audit_status=review)
    return queryset.order_by("-created_at", "-id")


def coach_add(team_id: str, payload: dict, account) -> TeamCoach:
    """POST /teams/{id}/coaches：校验球队/教练 + 现役唯一 + R4 现役主教练唯一。

    - 人员须存在、未软删且具备 COACH 身份（CoachProfile 行存在性，方案C
      完整类别事实源，与 member_add 的 AthleteProfile 校验同源）；
    - category 校验契约 4 值后透传落 duty 列；effectiveFrom（契约必填日期）
      落 joined_at（生效起期的真实承载）；effectiveTo/reviewStatus 声明但
      不应用（effectiveTo 无冻结承载列，reviewStatus 关联级审核列不存在）；
    - 同队现役重复添加 409（R2 同款）；已有现役主教练再加 HEAD_COACH 409（R4）。
    """
    from datetime import date as _date

    from apps.persons.models import CoachProfile, Person
    from apps.persons.services import parse_path_id

    team = _team_or_404(team_id)
    ensure_team_visible(account, team)
    category = (payload.get("category") or "").strip()
    if category not in _COACH_CATEGORIES:
        raise ApiError(
            INVALID_ARGUMENT,
            "教练岗位类别不合法",
            field_errors([("category", f"取值须为 {'/'.join(_COACH_CATEGORIES)}")]),
        )
    raw_from = (payload.get("effectiveFrom") or "").strip()
    try:
        joined_at = _date.fromisoformat(raw_from)
    except ValueError:
        raise ApiError(
            INVALID_ARGUMENT,
            "生效起期格式不合法",
            field_errors([("effectiveFrom", "须为 YYYY-MM-DD 日期")]),
        ) from None
    person = Person.objects.filter(
        pk=parse_path_id(payload["coachId"], "coachId"), is_deleted=False
    ).first()
    if person is None:
        raise ApiError(NOT_FOUND, "教练不存在或已删除")
    if not CoachProfile.objects.filter(person=person).exists():
        raise ApiError(STATE_CONFLICT, "该人员不具备教练身份，不能加入球队教练组")
    with transaction.atomic():
        if _coach_queryset().filter(
            team=team, person=person, status=enums.RelationStatus.CURRENT
        ).exists():
            raise ApiError(STATE_CONFLICT, "该教练已是本队现役教练")
        if category == enums.CoachDuty.HEAD_COACH and _coach_queryset().filter(
            team=team,
            status=enums.RelationStatus.CURRENT,
            duty=enums.CoachDuty.HEAD_COACH,
        ).exists():
            raise ApiError(STATE_CONFLICT, "本队已有现役主教练（R4 现役主教练唯一）")
        coach = TeamCoach.objects.create(
            team=team,
            person=person,
            duty=category,
            joined_at=joined_at,
        )
    return coach


def coach_remove(team_id: str, coach_id: str, version: int, account) -> TeamCoach:
    """DELETE /teams/{id}/coaches/{coach_id}?version=：软退出（CURRENT→EXITED）。

    禁止物理删除（冻结模型，历史关系可审计）；version 乐观锁 + 跨球队归属
    校验（404）；重复移出（已 EXITED）→ 409（member_remove 同款）。
    教练关系无转队单关联（F1 方案B，team_transfer 仅运动员），不设转队阻断。
    """
    from apps.persons.services import ensure_version, parse_path_id

    team = _team_or_404(team_id)
    ensure_team_visible(account, team)
    coach = _coach_queryset().filter(
        team=team, pk=parse_path_id(coach_id, "coachId")
    ).first()
    if coach is None:
        raise ApiError(NOT_FOUND, _COACH_NOT_TEAM)
    ensure_version(coach, version)
    if coach.status != enums.RelationStatus.CURRENT:
        raise ApiError(STATE_CONFLICT, "该教练已离队，不能重复移出")
    with transaction.atomic():
        coach.status = enums.RelationStatus.EXITED
        coach.left_at = timezone.now()
        coach.save()
    return coach
