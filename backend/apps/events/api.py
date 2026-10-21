"""events 接口：赛事配置（/events）与报名可见（/available-events）。

受保护接口用 register_operation + require 完成角色与权限点校验；
公开报名可见接口对全角色开放；响应统一走信封，分页 data 为 {items,page,pageSize,total}。
"""

from __future__ import annotations

from typing import Optional

from ninja import Router

from apps.common.authentication import JWTAuth
from apps.common.envelope import action_result, ok
from apps.common.errors import INVALID_ARGUMENT, ApiError
from apps.common.middleware import audit_state
from apps.common.pagination import paginate
from apps.common.permissions import (
    ADMIN,
    ALL_ROLES,
    PROVINCE_REVIEWER,
    register_operation,
    require,
)
from apps.common.schemas import EnvelopeOut

from . import schemas as s
from . import services

router = Router(tags=["events"])
_auth = JWTAuth()

# 管理端：ADMIN / PROVINCE_REVIEWER
register_operation("eventList", (ADMIN, PROVINCE_REVIEWER))
register_operation("eventCreate", (ADMIN, PROVINCE_REVIEWER))
register_operation("eventGet", (ADMIN, PROVINCE_REVIEWER))
register_operation("eventUpdate", (ADMIN, PROVINCE_REVIEWER))
register_operation("eventDelete", (ADMIN, PROVINCE_REVIEWER))
# 报名可见：全角色
register_operation("availableEvents", ALL_ROLES)
# 组别管理端
register_operation("eventGroupList", (ADMIN, PROVINCE_REVIEWER))
register_operation("eventGroupCreate", (ADMIN, PROVINCE_REVIEWER))
register_operation("eventGroupGet", (ADMIN, PROVINCE_REVIEWER))
register_operation("eventGroupUpdate", (ADMIN, PROVINCE_REVIEWER))
register_operation("eventGroupDelete", (ADMIN, PROVINCE_REVIEWER))
# 报名可选组别：全角色
register_operation("availableGroups", ALL_ROLES)


def _require_version(version: Optional[int]) -> int:
    if version is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "缺少 version",
            [{"field": "version", "reason": "该操作必须提交乐观锁版本"}],
        )
    return version


# ---------------------------------------------------------------- 赛事管理

@router.get("/events", auth=_auth, response=EnvelopeOut, summary="分页查询赛事")
def event_list(
    request,
    name: Optional[str] = None,
    season: Optional[str] = None,
    status: Optional[str] = None,
    eventCategory: Optional[str] = None,
):
    require("eventList")
    queryset = services.list_events(
        {"name": name, "season": season, "status": status, "eventCategory": eventCategory}
    )
    return paginate(request, queryset, mapper=s.event_out)


@router.post("/events", auth=_auth, response=EnvelopeOut, summary="创建赛事")
def event_create(request, body: s.EventCreate):
    require("eventCreate")
    event = services.create_event(body.model_dump())
    audit_state(
        request,
        "event.create",
        object_type="Event",
        object_id=event.pk,
        summary=f"创建赛事 {event.name}",
    )
    return ok(s.event_out(event))


@router.get("/events/{event_id}", auth=_auth, response=EnvelopeOut, summary="赛事详情")
def event_get(request, event_id: str):
    require("eventGet")
    event = services.get_event(event_id)
    return ok(s.event_out(event))


@router.put("/events/{event_id}", auth=_auth, response=EnvelopeOut, summary="修改赛事")
def event_update(request, event_id: str, body: s.EventUpdate):
    require("eventUpdate")
    event = services.update_event(event_id, body.model_dump())
    audit_state(
        request,
        "event.update",
        object_type="Event",
        object_id=event.pk,
        summary="修改赛事",
    )
    return ok(s.event_out(event))


@router.delete("/events/{event_id}", auth=_auth, response=EnvelopeOut, summary="删除赛事")
def event_delete(request, event_id: str, version: Optional[int] = None):
    require("eventDelete")
    event = services.delete_event(event_id, _require_version(version))
    audit_state(
        request,
        "event.delete",
        object_type="Event",
        object_id=event.pk,
        summary="删除赛事",
    )
    return action_result(True, event.pk, event.version)


# ---------------------------------------------------------------- 报名可见

@router.get("/available-events", auth=_auth, response=EnvelopeOut, summary="报名可见赛事列表")
def available_events(
    request,
    season: Optional[str] = None,
    registrationOpen: Optional[bool] = None,
    eventCategory: Optional[str] = None,
):
    require("availableEvents")
    queryset = services.available_events(
        {"season": season, "registrationOpen": registrationOpen, "eventCategory": eventCategory}
    )
    return paginate(request, queryset, mapper=s.event_out)


# ---------------------------------------------------------------- 赛事组别管理

@router.get(
    "/events/{event_id}/groups",
    auth=_auth,
    response=EnvelopeOut,
    summary="分页查询赛事组别",
)
def group_list(request, event_id: str, category: Optional[str] = None):
    require("eventGroupList")
    queryset = services.list_groups(event_id, {"category": category})
    return paginate(request, queryset, mapper=s.group_out)


@router.post(
    "/events/{event_id}/groups",
    auth=_auth,
    response=EnvelopeOut,
    summary="创建赛事组别",
)
def group_create(request, event_id: str, body: s.EventGroupCreate):
    require("eventGroupCreate")
    group = services.create_group(event_id, body.model_dump())
    audit_state(
        request,
        "eventGroup.create",
        object_type="EventGroup",
        object_id=group.pk,
        summary=f"创建组别 {group.name}",
    )
    return ok(s.group_out(group))


@router.get(
    "/events/{event_id}/groups/{group_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="赛事组别详情",
)
def group_get(request, event_id: str, group_id: str):
    require("eventGroupGet")
    group = services.get_group(event_id, group_id)
    return ok(s.group_out(group))


@router.put(
    "/events/{event_id}/groups/{group_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="修改赛事组别",
)
def group_update(request, event_id: str, group_id: str, body: s.EventGroupUpdate):
    require("eventGroupUpdate")
    group = services.update_group(event_id, group_id, body.model_dump())
    audit_state(
        request,
        "eventGroup.update",
        object_type="EventGroup",
        object_id=group.pk,
        summary="修改组别",
    )
    return ok(s.group_out(group))


@router.delete(
    "/events/{event_id}/groups/{group_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="删除赛事组别",
)
def group_delete(request, event_id: str, group_id: str, version: Optional[int] = None):
    require("eventGroupDelete")
    group = services.delete_group(event_id, group_id, _require_version(version))
    audit_state(
        request,
        "eventGroup.delete",
        object_type="EventGroup",
        object_id=group.pk,
        summary="删除组别",
    )
    return action_result(True, group.pk, group.version)


@router.get(
    "/available-events/{event_id}/groups",
    auth=_auth,
    response=EnvelopeOut,
    summary="报名可选组别",
)
def available_groups(request, event_id: str):
    require("availableGroups")
    queryset = services.list_groups(event_id, {})
    return paginate(request, queryset, mapper=s.group_out)