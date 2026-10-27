"""球队档案管理接口（django-ninja）：/teams*（T4-03 批次 A + 批次 B 成员）。

前端契约：zuqiu teamApi（src/api/index.ts L135-157）——list/get/create/update
+ members 域 5 接口（批次 B）+ memberGet 详情（批次 D-2，文档契约明确）；
remove（DELETE）暂不开放（关联检查依赖报名/比赛域，契约清单在案）；
coaches 属批次 C，matches 属比赛域，不在本轮。

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
    CITY,
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
# 角色集逐条对齐契约文档（docs/后端接口文档.md 各接口“角色：”行）：
# 10 个接口均为 TEAM / UNIVERSITY / CITY / PROVINCE_REVIEWER / ADMIN。
_TEAM_ROLES = (ADMIN, UNIVERSITY, TEAM, CITY, PROVINCE_REVIEWER)
register_operation("teamList", _TEAM_ROLES)
register_operation("teamGet", _TEAM_ROLES)
register_operation("teamCreate", _TEAM_ROLES)
register_operation("teamUpdate", _TEAM_ROLES)
# 成员域：读=管理端花名册 + 球队端名单页；写=名单页 + 省市管理端，
# 数据范围统一由 services.visible_teams 判定。
register_operation("memberList", _TEAM_ROLES)
register_operation("memberGet", _TEAM_ROLES)
register_operation("memberSearch", _TEAM_ROLES)
register_operation("memberCreate", _TEAM_ROLES)
register_operation("memberUpdate", _TEAM_ROLES)
register_operation("memberDelete", _TEAM_ROLES)


@router.get("/teams", auth=_auth, response=EnvelopeOut, summary="球队档案列表")
def team_list(
    request,
    keyword: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
):
    account = require("teamList")
    queryset = services.list_teams(
        {"keyword": keyword, "category": category, "status": status}, account
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
