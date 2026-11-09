"""球队档案管理接口（django-ninja）：/teams*（T4-03 批次 A + 批次 B 成员）。

前端契约：zuqiu teamApi（src/api/index.ts L135-157）——list/get/create/update/
remove + members 域 5 接口（批次 B）+ memberGet 详情（批次 D-2，文档契约明确）；
remove（DELETE）于 2026-11-02 补齐交付（软删 + 报名/比赛关联 409 保护）；
coaches 域 3 接口已交付（批次 C，2026-10-29）；teamMatches 聚合入口已交付
（2026-10-29，查询委托 events 读服务）。

- 权限：角色登记按 docs/后端接口文档.md 各接口“角色：”行执行（teamList/
  teamGet/teamCreate/teamUpdate 与成员域 6 接口均为 ADMIN/UNIVERSITY/TEAM/
  CITY/PROVINCE_REVIEWER）；数据范围由 services.visible_teams 判定：全局
  管理员全量、TEAM 本队、UNIVERSITY/CITY/PROVINCE_REVIEWER/挂组织 ADMIN
  按组织子树内的学校范围，越界 403 DATA_SCOPE_DENIED（详情 404）；
  学校解析经 A′-min 组织映射（ensure_business_org，2026-10-25 已移植交付）；
- 写操作统一 audit_state()（team.create/team.update/team.member.*）；
- audit_status 字段保留（Q2 审核流程不启用），创建即默认 APPROVED；
- 成员状态直出冻结枚举 CURRENT/EXITED（不虚构审核状态，用户裁定）。

合并说明（2026-10-26 二轮收编 origin/main）：main 曾先行交付 /teams、
/teams/{team_id} 只读子集（为前端球队下拉、对阵名称与报名学校名称提供
稳定数据源）。本文件的完整实现覆盖该子集——路由与 teamList/teamGet 权限
登记与 main 只读版一致，并保留其检索意图：list keyword 兼容队名/球队编号，
get 路径标识兼容主键 id 与 team_no 回退解析（见 services.py）。
"""

from __future__ import annotations

from typing import Optional

from ninja import Query, Router

from apps.common.authentication import JWTAuth
from apps.common.envelope import action_result, ok
from apps.common.errors import INVALID_ARGUMENT, ApiError, field_errors
from apps.common.middleware import audit_state
from apps.common.pagination import paginate
from apps.common.permissions import (
    ADMIN,
    ATHLETE,
    CITY,
    COACH,
    PROVINCE_REVIEWER,
    TEAM,
    UNIVERSITY,
    register_operation,
    require,
)
from apps.common.schemas import EnvelopeOut

from . import schemas as s
from . import services

router = Router(tags=["teams"])
_auth = JWTAuth()

# 权限点登记：导入期完成，漏登记 require() 会直接报错。
# 角色集以 docs/后端接口文档.md 为基础，写操作另按 2026-11-09 收敛口径
# 收窄为 ADMIN / UNIVERSITY / TEAM（见 _TEAM_WRITE_ROLES）。
_TEAM_ROLES = (ADMIN, UNIVERSITY, TEAM, CITY, PROVINCE_REVIEWER)
# 球队只读（2026-11-01 用户裁定）：运动员/教练个人页可直接查球队档案与赛程。
_TEAM_READ_ROLES = _TEAM_ROLES + (ATHLETE, COACH)
# 写操作收敛（2026-11-09）：前端只有管理员、学校端与球队端提供球队/成员/教练
# 维护入口（TeamsView、TeamProfileView、TeamMembersView、TeamCoachesView）。
# CITY/PROVINCE_REVIEWER 保留读权限用于审核查看，不再授予写权限。
_TEAM_WRITE_ROLES = (ADMIN, UNIVERSITY, TEAM)
register_operation("teamList", _TEAM_READ_ROLES)
register_operation("teamGet", _TEAM_READ_ROLES)
register_operation("teamCreate", _TEAM_WRITE_ROLES)
register_operation("teamUpdate", _TEAM_WRITE_ROLES)
# 2026-11-02 管理端全量联调：前端 TeamsView 删除按钮调用 DELETE /teams/{id}，
# 契约 §teamDelete 已定义（角色同写操作）；此前未交付导致按钮必然 404。
register_operation("teamDelete", _TEAM_WRITE_ROLES)
# 成员域：读=管理端花名册 + 球队端名单页；写=名单页。
# 数据范围统一由 services.visible_teams 判定。
register_operation("memberList", _TEAM_ROLES)
register_operation("memberGet", _TEAM_ROLES)
register_operation("memberSearch", _TEAM_ROLES)
register_operation("memberCreate", _TEAM_WRITE_ROLES)
register_operation("memberUpdate", _TEAM_WRITE_ROLES)
register_operation("memberDelete", _TEAM_WRITE_ROLES)
# 教练域（批次 C，2026-10-29 交付）：写操作随球队写权限收敛。
register_operation("teamCoachList", _TEAM_ROLES)
register_operation("teamCoachCreate", _TEAM_WRITE_ROLES)
# 候选教练搜索（2026-11-04 缺陷修复）：前端添加弹窗原用通用 /people/search，
# TEAM 账号只可见本队现役运动员 → 教练候选恒为空；补球队域候选接口。
register_operation("teamCoachSearch", _TEAM_ROLES)
register_operation("teamCoachDelete", _TEAM_WRITE_ROLES)
# 球队赛程聚合入口（契约 api:teamMatches，2026-10-29 交付）：路由归球队域，
# 查询委托 events 比赛读服务（docs/后端接口文档.md teamMatches 契约）。
register_operation("teamMatches", _TEAM_READ_ROLES)


@router.get("/teams", auth=_auth, response=EnvelopeOut, summary="球队档案列表")
def team_list(
    request,
    keyword: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    cityOrgId: Optional[str] = None,
):
    account = require("teamList")
    queryset = services.list_teams(
        {
            "keyword": keyword,
            "category": category,
            "status": status,
            "cityOrgId": cityOrgId,
        },
        account,
    )
    return paginate(request, queryset, mapper=s.team_out)


@router.get("/teams/{team_id}", auth=_auth, response=EnvelopeOut, summary="球队档案详情")
def team_get(request, team_id: str):
    account = require("teamGet")
    return ok(s.team_out(services.get_team(team_id, account)))


@router.post("/teams", auth=_auth, response=EnvelopeOut, summary="创建球队档案")
def team_create(request, body: s.TeamCreateBody):
    account = require("teamCreate")
    # 学校解析：schoolOrgId（Organization 节点 ID）→ 节点校验 + School 主档反查
    # （A′-min 契约，ensure_business_org 已移植）；任一不满足 400 INVALID_ARGUMENT。
    org, school = services._resolve_school(body.schoolOrgId)
    team = services.create_team(body.model_dump(), org, school, account)
    audit_state(
        request,
        "team.create",
        object_type="Team",
        object_id=str(team.pk),
        summary=f"创建球队 {team.name}({team.team_no})",
    )
    return ok(s.team_out(team))


@router.put("/teams/{team_id}", auth=_auth, response=EnvelopeOut, summary="更新球队档案")
def team_update(request, team_id: str, body: s.TeamUpdateBody):
    account = require("teamUpdate")
    team = services.update_team(team_id, body.model_dump(exclude_unset=True), account)
    audit_state(
        request,
        "team.update",
        object_type="Team",
        object_id=str(team.pk),
        summary=f"更新球队 {team.name}",
    )
    return ok(s.team_out(team))


@router.delete("/teams/{team_id}", auth=_auth, response=EnvelopeOut, summary="删除球队档案")
def team_delete(
    request, team_id: str, version: Optional[int] = Query(None, ge=0)
):
    account = require("teamDelete")
    if version is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "缺少 version",
            field_errors([("version", "该操作必须提交乐观锁版本")]),
        )
    team = services.delete_team(team_id, version, account)
    audit_state(
        request,
        "team.delete",
        object_type="Team",
        object_id=str(team.pk),
        summary=f"删除球队 {team.name}({team.team_no})",
    )
    return action_result(True, str(team.pk))


# ---------------------------------------------------------------- 球队成员（批次 B）
# 契约对齐 zuqiu teamApi.members*（index.ts L141-149）与 TeamMembersView 实码。

@router.get(
    "/teams/{team_id}/members", auth=_auth, response=EnvelopeOut, summary="球队成员名单"
)
def team_member_list(request, team_id: str, keyword: Optional[str] = None):
    account = require("memberList")
    queryset = services.member_list(team_id, {"keyword": keyword}, account)
    return paginate(request, queryset, mapper=s.member_out)


@router.post(
    "/teams/{team_id}/members/search",
    auth=_auth,
    response=EnvelopeOut,
    summary="候选球员搜索",
)
def team_member_search(request, team_id: str, body: s.MemberSearchBody):
    account = require("memberSearch")
    candidates = services.member_search(team_id, body.keyword, account)
    return ok(candidates)


# 注意：GET /members/{member_id} 必须注册在 POST /members/search 之后——
# Django URL 按注册序匹配，字面量 search 需先于 {member_id} 转换器，
# 否则 search 的 POST 会命中详情路由被 405 拒绝（D-2 自测发现并修正）。
@router.get(
    "/teams/{team_id}/members/{member_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="球队成员关系详情",
)
def team_member_get(request, team_id: str, member_id: str):
    account = require("memberGet")
    member = services.member_get(team_id, member_id, account)
    return ok(s.member_out(member))


@router.post(
    "/teams/{team_id}/members", auth=_auth, response=EnvelopeOut, summary="添加球队成员"
)
def team_member_create(request, team_id: str, body: s.MemberCreateBody):
    account = require("memberCreate")
    member = services.member_add(team_id, body.model_dump(), account)
    audit_state(
        request,
        "team.member.create",
        object_type="TeamMember",
        object_id=str(member.pk),
        summary=f"添加成员 {member.person.full_name} @ {team_id}",
    )
    return ok(s.member_out(member))


@router.put(
    "/teams/{team_id}/members/{member_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="更新球队成员",
)
def team_member_update(request, team_id: str, member_id: str, body: s.MemberUpdateBody):
    account = require("memberUpdate")
    member = services.member_update(
        team_id, member_id, body.model_dump(exclude_unset=True), account
    )
    audit_state(
        request,
        "team.member.update",
        object_type="TeamMember",
        object_id=str(member.pk),
        summary=f"更新成员 {member.person.full_name}",
    )
    return ok(s.member_out(member))


@router.delete(
    "/teams/{team_id}/members/{member_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="移出球队成员（软退出）",
)
def team_member_delete(
    request, team_id: str, member_id: str, version: Optional[int] = Query(None, ge=0)
):
    account = require("memberDelete")
    if version is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "缺少 version",
            field_errors([("version", "该操作必须提交乐观锁版本")]),
        )
    member = services.member_remove(team_id, member_id, version, account)
    audit_state(
        request,
        "team.member.remove",
        object_type="TeamMember",
        object_id=str(member.pk),
        summary=f"移出成员 {member.person.full_name}（CURRENT→EXITED）",
    )
    return action_result(True, str(member.pk))


# ---------------------------------------------------------------- 球队教练（批次 C）
# 契约对齐 zuqiu teamApi.coaches/addCoach/removeCoach（index.ts L136-139）与
# TeamCoachesView 实码；DELETE 路径参数为教练关系行 id + version 查询参数。

@router.get(
    "/teams/{team_id}/coaches", auth=_auth, response=EnvelopeOut, summary="球队教练名单"
)
def team_coach_list(
    request,
    team_id: str,
    category: Optional[str] = None,
    reviewStatus: Optional[str] = None,
):
    account = require("teamCoachList")
    queryset = services.coach_list(
        team_id, {"category": category, "reviewStatus": reviewStatus}, account
    )
    return paginate(request, queryset, mapper=s.coach_out)


@router.post(
    "/teams/{team_id}/coaches", auth=_auth, response=EnvelopeOut, summary="添加球队教练"
)
def team_coach_create(request, team_id: str, body: s.CoachAddBody):
    account = require("teamCoachCreate")
    coach = services.coach_add(team_id, body.model_dump(), account)
    audit_state(
        request,
        "team.coach.create",
        object_type="TeamCoach",
        object_id=str(coach.pk),
        summary=f"添加教练 {coach.person.full_name} @ {team_id}",
    )
    return ok(s.coach_out(coach))


@router.post(
    "/teams/{team_id}/coaches/search",
    auth=_auth,
    response=EnvelopeOut,
    summary="候选教练搜索",
)
def team_coach_search(request, team_id: str, body: s.CoachSearchBody):
    account = require("teamCoachSearch")
    candidates = services.coach_search(team_id, body.keyword, account)
    return ok(candidates)


# 注意：POST /coaches/search 必须注册在 DELETE /coaches/{coach_id} 之前——
# Django URL 按注册序匹配，字面量 search 若排在转换器之后，该 POST 会先命中
# 详情路由并按方法不匹配返回 405（与 members/search 同一坑，2026-11-04 复现）。
@router.delete(
    "/teams/{team_id}/coaches/{coach_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="移出球队教练（软退出）",
)
def team_coach_delete(
    request, team_id: str, coach_id: str, version: Optional[int] = Query(None, ge=0)
):
    account = require("teamCoachDelete")
    if version is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "缺少 version",
            field_errors([("version", "该操作必须提交乐观锁版本")]),
        )
    coach = services.coach_remove(team_id, coach_id, version, account)
    audit_state(
        request,
        "team.coach.remove",
        object_type="TeamCoach",
        object_id=str(coach.pk),
        summary=f"移出教练 {coach.person.full_name}（CURRENT→EXITED）",
    )
    return action_result(True, str(coach.pk))


# ---------------------------------------------------------------- 球队赛程（teamMatches 契约）
# GET /teams/{teamId}/matches：成员4 聚合入口，查询委托 events 比赛读服务
# （docs/后端接口文档.md L1889-1906），输出复用 events match_out；
# 消费方 zuqiu teamApi.matches（admin/TeamsView 参赛队伍卡片赛程）。

@router.get(
    "/teams/{team_id}/matches", auth=_auth, response=EnvelopeOut, summary="球队赛程列表"
)
def team_match_list(request, team_id: str, eventId: Optional[str] = None):
    account = require("teamMatches")
    from apps.events.schemas import match_out
    from apps.events.services import list_matches

    # 球队可见性与 404 语义复用 get_team（主键 id / team_no 回退解析）；
    # 过滤按球队参与（home/away），输出契约与 /matches 完全一致。
    team = services.get_team(team_id, account)
    queryset = list_matches({"teamId": str(team.pk), "eventId": eventId})
    return paginate(request, queryset, mapper=match_out)
