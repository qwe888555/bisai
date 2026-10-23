"""events 接口：赛事配置（/events）与报名可见（/available-events）。

受保护接口用 register_operation + require 完成角色与权限点校验；
公开报名可见接口对全角色开放；响应统一走信封，分页 data 为 {items,page,pageSize,total}。
"""

from __future__ import annotations

from typing import Optional

from ninja import Router

from apps.common.authentication import JWTAuth
from apps.common.envelope import action_result, ok
from apps.common import idempotency
from apps.common.errors import INVALID_ARGUMENT, ApiError
from apps.common.middleware import audit_state
from apps.common.pagination import paginate
from apps.common.permissions import (
    ADMIN,
    ALL_ROLES,
    MATCH_RECORDER,
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

# ---------------------------------------------------------------- 比赛对阵管理（5.1）

_MATCH_ROLES = (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER)
register_operation("matchList", _MATCH_ROLES)
register_operation("matchCreate", _MATCH_ROLES)
register_operation("matchGet", _MATCH_ROLES)
register_operation("matchUpdate", _MATCH_ROLES)
register_operation("matchDelete", _MATCH_ROLES)
register_operation("matchPublish", _MATCH_ROLES)
register_operation("matchUnpublish", _MATCH_ROLES)


@router.get("/matches", auth=_auth, response=EnvelopeOut, summary="分页查询比赛对阵")
def match_list(
    request,
    eventId: Optional[str] = None,
    groupId: Optional[str] = None,
    status: Optional[str] = None,
    teamId: Optional[str] = None,
    teamName: Optional[str] = None,
):
    require("matchList")
    queryset = services.list_matches(
        {"eventId": eventId, "groupId": groupId, "status": status, "teamId": teamId, "teamName": teamName}
    )
    return paginate(request, queryset, mapper=s.match_out)


@router.post("/matches", auth=_auth, response=EnvelopeOut, summary="创建比赛对阵")
def match_create(request, body: s.MatchCreate):
    require("matchCreate")
    match = services.create_match(body.model_dump())
    audit_state(
        request,
        "match.create",
        object_type="Match",
        object_id=match.pk,
        summary=f"创建对阵 {match.home_team_id} vs {match.away_team_id}",
    )
    return ok(s.match_out(match))


@router.get("/matches/{match_id}", auth=_auth, response=EnvelopeOut, summary="比赛对阵详情")
def match_get(request, match_id: str):
    require("matchGet")
    match = services.get_match(match_id)
    return ok(s.match_out(match))


@router.put("/matches/{match_id}", auth=_auth, response=EnvelopeOut, summary="修改比赛对阵")
def match_update(request, match_id: str, body: s.MatchUpdate):
    require("matchUpdate")
    match = services.update_match(match_id, body.model_dump())
    audit_state(
        request,
        "match.update",
        object_type="Match",
        object_id=match.pk,
        summary="修改比赛对阵",
    )
    return ok(s.match_out(match))


@router.delete("/matches/{match_id}", auth=_auth, response=EnvelopeOut, summary="删除比赛对阵")
def match_delete(request, match_id: str, version: Optional[int] = None):
    require("matchDelete")
    match = services.delete_match(match_id, _require_version(version))
    audit_state(
        request,
        "match.delete",
        object_type="Match",
        object_id=match.pk,
        summary="删除比赛对阵",
    )
    return action_result(True, match.pk, match.version)


@router.post("/matches/{match_id}/publish", auth=_auth, response=EnvelopeOut, summary="发布对阵")
def match_publish(request, match_id: str, body: s.MatchVersionRequest):
    require("matchPublish")
    match = services.publish_match(match_id, body.version)
    audit_state(
        request,
        "match.publish",
        object_type="Match",
        object_id=match.pk,
        summary="发布对阵",
    )
    return ok(s.match_out(match))


@router.post("/matches/{match_id}/unpublish", auth=_auth, response=EnvelopeOut, summary="下线对阵")
def match_unpublish(request, match_id: str, body: s.MatchReasonVersionRequest):
    require("matchUnpublish")
    match = services.unpublish_match(match_id, body.version, body.reason)
    audit_state(
        request,
        "match.unpublish",
        object_type="Match",
        object_id=match.pk,
        summary=f"下线对阵：{body.reason}",
    )
    return ok(s.match_out(match))

# ---------------------------------------------------------------- 比赛事件流（5.2）

register_operation("matchEventList", _MATCH_ROLES)
register_operation("matchEventCreate", _MATCH_ROLES)
register_operation("matchEventGet", _MATCH_ROLES)
register_operation("matchEventUpdate", _MATCH_ROLES)
register_operation("matchEventDelete", _MATCH_ROLES)


@router.get(
    "/matches/{match_id}/events",
    auth=_auth,
    response=EnvelopeOut,
    summary="查看进球/判罚/换人事件",
)
def match_event_list(request, match_id: str, eventType: Optional[str] = None):
    require("matchEventList")
    queryset = services.list_match_events(match_id, eventType)
    return paginate(request, queryset, mapper=s.match_event_out)


@router.post(
    "/matches/{match_id}/events",
    auth=_auth,
    response=EnvelopeOut,
    summary="录入比赛事件",
)
def match_event_create(request, match_id: str, body: s.MatchEventCreate):
    require("matchEventCreate")
    g = idempotency.guard(
        request, "matchEventCreate", object_id=match_id, payload=body.model_dump()
    )
    if g.replay is not None:
        return g.replay
    event = services.create_match_event(match_id, body.model_dump())
    envelope = ok(s.match_event_out(event))
    g.commit(envelope)
    audit_state(
        request,
        "matchEvent.create",
        object_type="MatchEvent",
        object_id=event.pk,
        summary=f"录入{event.event_type}事件",
    )
    return envelope


@router.get(
    "/matches/{match_id}/events/{event_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="比赛事件详情",
)
def match_event_get(request, match_id: str, event_id: str):
    require("matchEventGet")
    event = services.get_match_event(match_id, event_id)
    return ok(s.match_event_out(event))


@router.put(
    "/matches/{match_id}/events/{event_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="修改进球/判罚事件",
)
def match_event_update(request, match_id: str, event_id: str, body: s.MatchEventUpdate):
    require("matchEventUpdate")
    event = services.update_match_event(
        match_id, event_id, body.version, body.event.model_dump()
    )
    audit_state(
        request,
        "matchEvent.update",
        object_type="MatchEvent",
        object_id=event.pk,
        summary="修改比赛事件，比分同步重算",
    )
    return ok(s.match_event_out(event))


@router.delete(
    "/matches/{match_id}/events/{event_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="撤销比赛事件",
)
def match_event_delete(
    request,
    match_id: str,
    event_id: str,
    version: Optional[int] = None,
    matchVersion: Optional[int] = None,
):
    require("matchEventDelete")
    version = _require_version(version)
    if matchVersion is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "缺少 matchVersion",
            [{"field": "matchVersion", "reason": "撤销必须同时提交比赛版本"}],
        )
    event = services.delete_match_event(match_id, event_id, version, matchVersion)
    audit_state(
        request,
        "matchEvent.delete",
        object_type="MatchEvent",
        object_id=event.pk,
        summary="撤销比赛事件，比分同步重算",
    )
    return action_result(True, event.pk, event.version)

# ---------------------------------------------------------------- 赛事文件（5.3）

register_operation("matchDocumentCreate", _MATCH_ROLES)
register_operation("matchDocumentList", _MATCH_ROLES)


@router.post(
    "/matches/{match_id}/documents",
    auth=_auth,
    response=EnvelopeOut,
    summary="关联签到/签字记录/裁判报告",
)
def match_document_create(request, match_id: str, body: s.MatchDocumentCreate):
    require("matchDocumentCreate")
    doc = services.create_match_document(match_id, body.model_dump())
    audit_state(
        request,
        "matchDocument.create",
        object_type="MatchDocument",
        object_id=doc.pk,
        summary=f"挂载{doc.document_type}文件",
    )
    return ok(s.match_document_out(doc))


@router.get(
    "/matches/{match_id}/documents",
    auth=_auth,
    response=EnvelopeOut,
    summary="比赛文件列表",
)
def match_document_list(request, match_id: str, documentType: Optional[str] = None):
    require("matchDocumentList")
    queryset = services.list_match_documents(match_id, documentType)
    return paginate(request, queryset, mapper=s.match_document_out)


# ---------------------------------------------------------------- 图文直播条目（5.4）

register_operation("livePostList", _MATCH_ROLES)
register_operation("livePostCreate", _MATCH_ROLES)
register_operation("livePostGet", _MATCH_ROLES)
register_operation("livePostUpdate", _MATCH_ROLES)
register_operation("livePostDelete", _MATCH_ROLES)


@router.get(
    "/matches/{match_id}/live-posts",
    auth=_auth,
    response=EnvelopeOut,
    summary="分页查询图文直播条目",
)
def live_post_list(request, match_id: str):
    require("livePostList")
    queryset = services.list_live_posts(match_id)
    return paginate(request, queryset, mapper=s.live_post_out)


@router.post(
    "/matches/{match_id}/live-posts",
    auth=_auth,
    response=EnvelopeOut,
    summary="创建图文直播条目",
)
def live_post_create(request, match_id: str, body: s.LivePostCreate):
    require("livePostCreate")
    post = services.create_live_post(match_id, body.model_dump())
    audit_state(
        request,
        "livePost.create",
        object_type="LivePost",
        object_id=post.pk,
        summary="新增图文直播条目",
    )
    return ok(s.live_post_out(post))


@router.get(
    "/matches/{match_id}/live-posts/{post_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="图文直播条目详情",
)
def live_post_get(request, match_id: str, post_id: str):
    require("livePostGet")
    post = services.get_live_post(match_id, post_id)
    return ok(s.live_post_out(post))


@router.put(
    "/matches/{match_id}/live-posts/{post_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="修改图文直播条目",
)
def live_post_update(request, match_id: str, post_id: str, body: s.LivePostUpdate):
    require("livePostUpdate")
    post = services.update_live_post(match_id, post_id, body.model_dump())
    audit_state(
        request,
        "livePost.update",
        object_type="LivePost",
        object_id=post.pk,
        summary="修改图文直播条目",
    )
    return ok(s.live_post_out(post))


@router.delete(
    "/matches/{match_id}/live-posts/{post_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="删除图文直播条目",
)
def live_post_delete(request, match_id: str, post_id: str, version: Optional[int] = None):
    require("livePostDelete")
    version = _require_version(version)
    post = services.delete_live_post(match_id, post_id, version)
    audit_state(
        request,
        "livePost.delete",
        object_type="LivePost",
        object_id=post.pk,
        summary="删除图文直播条目",
    )
    return action_result(True, post.pk, post.version)


# ---------------------------------------------------------------- 直播链接（5.4）

register_operation("liveLinkList", _MATCH_ROLES)
register_operation("liveLinkCreate", _MATCH_ROLES)
register_operation("liveLinkGet", _MATCH_ROLES)
register_operation("liveLinkUpdate", _MATCH_ROLES)
register_operation("liveLinkDelete", _MATCH_ROLES)


@router.get(
    "/matches/{match_id}/live-links",
    auth=_auth,
    response=EnvelopeOut,
    summary="分页查询直播链接",
)
def live_link_list(request, match_id: str):
    require("liveLinkList")
    queryset = services.list_live_links(match_id)
    return paginate(request, queryset, mapper=s.live_link_out)


@router.post(
    "/matches/{match_id}/live-links",
    auth=_auth,
    response=EnvelopeOut,
    summary="创建直播链接",
)
def live_link_create(request, match_id: str, body: s.LiveLinkCreate):
    require("liveLinkCreate")
    link = services.create_live_link(match_id, body.model_dump())
    audit_state(
        request,
        "liveLink.create",
        object_type="LiveLink",
        object_id=link.pk,
        summary="新增直播链接",
    )
    return ok(s.live_link_out(link))


@router.get(
    "/matches/{match_id}/live-links/{link_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="直播链接详情",
)
def live_link_get(request, match_id: str, link_id: str):
    require("liveLinkGet")
    link = services.get_live_link(match_id, link_id)
    return ok(s.live_link_out(link))


@router.put(
    "/matches/{match_id}/live-links/{link_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="修改直播链接",
)
def live_link_update(request, match_id: str, link_id: str, body: s.LiveLinkUpdate):
    require("liveLinkUpdate")
    link = services.update_live_link(match_id, link_id, body.model_dump())
    audit_state(
        request,
        "liveLink.update",
        object_type="LiveLink",
        object_id=link.pk,
        summary="修改直播链接",
    )
    return ok(s.live_link_out(link))


@router.delete(
    "/matches/{match_id}/live-links/{link_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="删除直播链接",
)
def live_link_delete(request, match_id: str, link_id: str, version: Optional[int] = None):
    require("liveLinkDelete")
    version = _require_version(version)
    link = services.delete_live_link(match_id, link_id, version)
    audit_state(
        request,
        "liveLink.delete",
        object_type="LiveLink",
        object_id=link.pk,
        summary="删除直播链接",
    )
    return action_result(True, link.pk, link.version)

# ---------------------------------------------------------------- 内容引擎（7.1，ADMIN / PROVINCE_REVIEWER）

_CMS_ROLES = (ADMIN, PROVINCE_REVIEWER)
register_operation("contentList", _CMS_ROLES)
register_operation("contentCreate", _CMS_ROLES)
register_operation("contentGet", _CMS_ROLES)
register_operation("contentUpdate", _CMS_ROLES)
register_operation("contentDelete", _CMS_ROLES)
register_operation("contentPublish", _CMS_ROLES)
register_operation("contentUnpublish", _CMS_ROLES)


@router.get("/contents", auth=_auth, response=EnvelopeOut, summary="分页查询内容")
def content_list(
    request,
    contentType: Optional[str] = None,
    eventId: Optional[str] = None,
    groupId: Optional[str] = None,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    eventCategory: Optional[str] = None,
):
    require("contentList")
    qs = services.list_contents({
        "contentType": contentType, "eventId": eventId, "groupId": groupId,
        "status": status, "keyword": keyword, "eventCategory": eventCategory,
    })
    return paginate(request, qs, mapper=s.content_out)


@router.post("/contents", auth=_auth, response=EnvelopeOut, summary="创建内容")
def content_create(request, body: s.ContentCreate):
    require("contentCreate")
    content = services.create_content(body.model_dump())
    audit_state(request, "content.create", object_type="Content",
                object_id=content.pk, summary="创建内容")
    return ok(s.content_out(content))


@router.get("/contents/{content_id}", auth=_auth, response=EnvelopeOut, summary="内容详情")
def content_get(request, content_id: str):
    require("contentGet")
    return ok(s.content_out(services.get_content(content_id)))


@router.put("/contents/{content_id}", auth=_auth, response=EnvelopeOut, summary="修改内容")
def content_update(request, content_id: str, body: s.ContentUpdate):
    require("contentUpdate")
    content = services.update_content(content_id, body.model_dump())
    audit_state(request, "content.update", object_type="Content",
                object_id=content.pk, summary="修改内容，revision 递增")
    return ok(s.content_out(content))


@router.delete("/contents/{content_id}", auth=_auth, response=EnvelopeOut, summary="删除内容")
def content_delete(request, content_id: str, version: Optional[int] = None):
    require("contentDelete")
    version = _require_version(version)
    content = services.delete_content(content_id, version)
    audit_state(request, "content.delete", object_type="Content",
                object_id=content.pk, summary="删除内容")
    return action_result(True, content.pk, content.version)


@router.post("/contents/{content_id}/publish", auth=_auth, response=EnvelopeOut, summary="发布内容")
def content_publish(request, content_id: str, body: s.VersionRequest):
    require("contentPublish")
    content = services.publish_content(content_id, body.version)
    audit_state(request, "content.publish", object_type="Content",
                object_id=content.pk, summary="发布内容")
    return ok(s.content_out(content))


@router.post("/contents/{content_id}/unpublish", auth=_auth, response=EnvelopeOut, summary="内容下线")
def content_unpublish(request, content_id: str, body: s.ReasonVersionRequest):
    require("contentUnpublish")
    content = services.unpublish_content(content_id, body.version, body.reason)
    audit_state(request, "content.unpublish", object_type="Content",
                object_id=content.pk, summary=f"内容下线：{body.reason}")
    return ok(s.content_out(content))


# ---------------------------------------------------------------- 门户资源（7.2，ADMIN / PROVINCE_REVIEWER）

register_operation("portalResourceList", _CMS_ROLES)
register_operation("portalResourceCreate", _CMS_ROLES)
register_operation("portalResourceGet", _CMS_ROLES)
register_operation("portalResourceUpdate", _CMS_ROLES)
register_operation("portalResourceDelete", _CMS_ROLES)


@router.get("/portal-resources", auth=_auth, response=EnvelopeOut, summary="分页查询门户资源")
def portal_resource_list(request, resourceType: Optional[str] = None, enabled: Optional[bool] = None):
    require("portalResourceList")
    qs = services.list_portal_resources(resourceType, enabled)
    return paginate(request, qs, mapper=s.portal_resource_out)


@router.post("/portal-resources", auth=_auth, response=EnvelopeOut, summary="创建门户资源")
def portal_resource_create(request, body: s.PortalResourceCreate):
    require("portalResourceCreate")
    resource = services.create_portal_resource(body.model_dump())
    audit_state(request, "portalResource.create", object_type="PortalResource",
                object_id=resource.pk, summary="创建门户资源")
    return ok(s.portal_resource_out(resource))


@router.get("/portal-resources/{resource_id}", auth=_auth, response=EnvelopeOut, summary="门户资源详情")
def portal_resource_get(request, resource_id: str):
    require("portalResourceGet")
    return ok(s.portal_resource_out(services.get_portal_resource(resource_id)))


@router.put("/portal-resources/{resource_id}", auth=_auth, response=EnvelopeOut, summary="修改门户资源")
def portal_resource_update(request, resource_id: str, body: s.PortalResourceUpdate):
    require("portalResourceUpdate")
    resource = services.update_portal_resource(resource_id, body.model_dump())
    audit_state(request, "portalResource.update", object_type="PortalResource",
                object_id=resource.pk, summary="修改门户资源")
    return ok(s.portal_resource_out(resource))


@router.delete("/portal-resources/{resource_id}", auth=_auth, response=EnvelopeOut, summary="删除门户资源")
def portal_resource_delete(request, resource_id: str, version: Optional[int] = None):
    require("portalResourceDelete")
    version = _require_version(version)
    resource = services.delete_portal_resource(resource_id, version)
    audit_state(request, "portalResource.delete", object_type="PortalResource",
                object_id=resource.pk, summary="删除门户资源")
    return action_result(True, resource.pk, resource.version)


# ---------------------------------------------------------------- 公开只读（7.1 / 7.2，免登录）

@router.get("/public/contents", auth=None, response=EnvelopeOut, summary="门户已发布内容列表")
def public_content_list(
    request,
    contentType: Optional[str] = None,
    eventId: Optional[str] = None,
    groupId: Optional[str] = None,
    keyword: Optional[str] = None,
    eventCategory: Optional[str] = None,
):
    qs = services.public_list_contents({
        "contentType": contentType, "eventId": eventId, "groupId": groupId,
        "keyword": keyword, "eventCategory": eventCategory,
    })
    return paginate(request, qs, mapper=s.public_content_out)


@router.get("/public/contents/{content_id}", auth=None, response=EnvelopeOut, summary="门户内容详情")
def public_content_get(request, content_id: str):
    content = services.public_get_content(content_id)
    return ok(s.public_content_out(content))


@router.get("/public/resources", auth=None, response=EnvelopeOut, summary="门户轮播/链接图/合作方展示")
def public_resource_list(request, resourceType: Optional[str] = None):
    qs = services.public_list_resources(resourceType)
    return paginate(request, qs, mapper=s.public_resource_out)

# ---------------------------------------------------------------- 赛果(5.5)：MATCH_RECORDER / ADMIN / PROVINCE_REVIEWER

_RESULT_ROLES = (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER)
register_operation("matchResultGet", _RESULT_ROLES)
register_operation("matchResultWrite", _RESULT_ROLES)
register_operation("matchResultPublish", _RESULT_ROLES)
register_operation("matchResultDelete", _RESULT_ROLES)
register_operation("goalRanking", _RESULT_ROLES)


@router.get("/matches/{match_id}/result", auth=_auth, response=EnvelopeOut, summary="赛果信息")
def match_result_get(request, match_id: str):
    require("matchResultGet")
    return ok(s.match_result_out(services.get_match_result(match_id)))


@router.put("/matches/{match_id}/result", auth=_auth, response=EnvelopeOut, summary="保存或修改赛果")
def match_result_write(request, match_id: str, body: s.MatchResultWrite):
    account = require("matchResultWrite")
    match = services.write_match_result(match_id, body.model_dump(), getattr(account, "roles", ()))
    audit_state(request, "matchResult.write", object_type="Match",
                object_id=match.pk, summary=f"保存赛果 {match.result_type}")
    return ok(s.match_result_out(match))


@router.post("/matches/{match_id}/result/publish", auth=_auth, response=EnvelopeOut, summary="发布已确认赛果")
def match_result_publish(request, match_id: str, body: s.VersionRequest):
    require("matchResultPublish")
    match = services.publish_match_result(match_id, body.version)
    audit_state(request, "matchResult.publish", object_type="Match",
                object_id=match.pk, summary="发布赛果")
    return ok(s.match_result_out(match))


@router.delete("/matches/{match_id}/result", auth=_auth, response=EnvelopeOut, summary="撤下赛果")
def match_result_delete(request, match_id: str, version: Optional[int] = None):
    require("matchResultDelete")
    version = _require_version(version)
    match = services.delete_match_result(match_id, version)
    audit_state(request, "matchResult.delete", object_type="Match",
                object_id=match.pk, summary="撤下赛果，回到事件派生")
    return action_result(True, match.pk, match.version)


@router.get("/statistics/goals", auth=_auth, response=EnvelopeOut, summary="进球榜及分组统计")
def goal_ranking(
    request,
    eventId: str,
    groupId: Optional[str] = None,
    category: Optional[str] = None,
    page: int = 1,
    pageSize: int = 20,
):
    require("goalRanking")
    page = max(1, page)
    pageSize = min(100, max(1, pageSize))
    return ok(services.goal_ranking(eventId, groupId, category, page, pageSize))

# ---------------------------------------------------------------- 门户公开读(T5-07，免登录)

@router.get("/public/matches", auth=None, response=EnvelopeOut, summary="门户对阵及赛果列表")
def public_match_list(
    request,
    eventId: str,
    groupId: Optional[str] = None,
    status: Optional[str] = None,
    resultPublished: Optional[bool] = None,
):
    qs = services.public_list_matches({
        "eventId": eventId, "groupId": groupId, "status": status,
        "resultPublished": resultPublished,
    })
    return paginate(request, qs, mapper=s.public_match_out)


@router.get("/public/matches/{match_id}", auth=None, response=EnvelopeOut, summary="已发布比赛对阵详情")
def public_match_get(request, match_id: str):
    return ok(s.public_match_out(services.public_get_match(match_id)))


@router.get("/public/matches/{match_id}/live-feed", auth=None, response=EnvelopeOut, summary="短轮询图文直播")
def public_live_feed(request, match_id: str, afterRevision: int = 0):
    feed = services.public_live_feed(match_id, afterRevision)
    return ok({
        "feedRevision": feed["feedRevision"],
        "items": [s.public_live_post_out(p) for p in feed["items"]],
        "deletedIds": feed["deletedIds"],
        "resetRequired": feed["resetRequired"],
    })


@router.get("/public/matches/{match_id}/live-links", auth=None, response=EnvelopeOut, summary="门户直播外链")
def public_live_links(request, match_id: str):
    qs = services.public_list_live_links(match_id)
    return paginate(request, qs, mapper=s.public_live_link_out)


@router.get("/public/statistics/goals", auth=None, response=EnvelopeOut, summary="门户进球榜/三组统计")
def public_goal_ranking(
    request,
    eventId: str,
    groupId: Optional[str] = None,
    category: Optional[str] = None,
    page: int = 1,
    pageSize: int = 20,
):
    page = max(1, page)
    pageSize = min(100, max(1, pageSize))
    return ok(services.goal_ranking(eventId, groupId, category, page, pageSize, published_only=True))