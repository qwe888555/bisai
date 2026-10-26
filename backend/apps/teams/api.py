"""球队只读接口：/teams、/teams/{team_id}。

成员4 的完整球队档案写链仍在其分支维护；main 先补只读查询，
让前端球队下拉、对阵名称与报名学校名称有稳定数据源。
"""

from __future__ import annotations

from typing import Optional

from ninja import Router

from apps.common.authentication import JWTAuth
from apps.common.envelope import ok
from apps.common.pagination import paginate
from apps.common.permissions import (
    ADMIN,
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

register_operation("teamList", (ADMIN, UNIVERSITY, TEAM, PROVINCE_REVIEWER))
register_operation("teamGet", (ADMIN, UNIVERSITY, TEAM, PROVINCE_REVIEWER))


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
