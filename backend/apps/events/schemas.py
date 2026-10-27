"""events 请求模型与响应 mapper（django-ninja / pydantic v2）。

请求模型继承 StrictSchema（多传未知字段直接 400 INVALID_ARGUMENT）；
响应由 mapper 转契约 camelCase，禁止改名、禁止加字段。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import Field

from apps.common.schemas import StrictSchema
from .services import event_phase

from . import display

EventStatus = Literal["DRAFT", "REGISTRATION_OPEN", "REGISTRATION_CLOSED", "RUNNING", "FINISHED"]
GroupCategory = Literal["PRIMARY", "MIDDLE", "UNIVERSITY"]
GroupGender = Literal["MALE", "FEMALE", "MIXED"]


# ---------------------------------------------------------------- 赛事

class EventCreate(StrictSchema):
    name: str = Field(min_length=1, max_length=200)
    season: str = Field(min_length=1, max_length=30)
    eventCategory: str = Field(default="", max_length=64)
    registrationOpensAt: datetime
    registrationClosesAt: datetime
    status: EventStatus = "DRAFT"
    description: str = Field(default="", max_length=5000)


class EventUpdate(EventCreate):
    version: int = Field(ge=0)


# ---------------------------------------------------------------- 赛事组别

class EventGroupCreate(StrictSchema):
    name: str = Field(min_length=1, max_length=100)
    category: GroupCategory
    gender: GroupGender
    quota: int = Field(default=0, ge=0)
    minAge: Optional[int] = Field(default=None, ge=0, le=60)
    maxAge: Optional[int] = Field(default=None, ge=0, le=60)
    maxPlayers: int = Field(ge=1, le=100)
    requiredMaterialTypes: list[str]


class EventGroupUpdate(EventGroupCreate):
    version: int = Field(ge=0)


# ---------------------------------------------------------------- 响应 mapper

def event_out(event) -> dict:
    phase = event_phase(event)
    data = {
        "id": event.pk,
        "name": event.name,
        "season": event.season,
        "eventCategory": event.event_category,
        "registrationOpensAt": event.registration_opens_at,
        "registrationClosesAt": event.registration_closes_at,
        "status": event.status,
        "currentPhase": phase["currentPhase"],
        "phaseStartedAt": phase["phaseStartedAt"],
        "phaseEndsAt": phase["phaseEndsAt"],
        "progressPercent": phase["progressPercent"],
        "description": event.description,
        "version": int(event.version or 0),
        "createdAt": event.created_at,
        "updatedAt": event.updated_at,
    }
    return data


def group_out(group) -> dict:
    data = {
        "id": group.pk,
        "eventId": group.event_id,
        "name": group.name,
        "category": group.category,
        "gender": group.gender,
        "quota": int(group.quota or 0),
        "maxPlayers": int(group.max_players or 0),
        "requiredMaterialTypes": list(group.required_material_types or []),
        "version": int(group.version or 0),
        "createdAt": group.created_at,
        "updatedAt": group.updated_at,
    }
    if group.min_age is not None:
        data["minAge"] = int(group.min_age)
    if group.max_age is not None:
        data["maxAge"] = int(group.max_age)
    return data

# ---------------------------------------------------------------- 比赛对阵（5.1）

MatchStatus = Literal["SCHEDULED", "LIVE", "FINISHED", "CANCELLED"]
RefereeRole = Literal["REFEREE", "ASSISTANT", "FOURTH_OFFICIAL", "OTHER"]


class RefereeAssignment(StrictSchema):
    name: str = Field(min_length=1, max_length=100)
    role: RefereeRole
    level: str = Field(default="", max_length=100)


class MatchCreate(StrictSchema):
    eventId: str = Field(min_length=1, max_length=64)
    groupId: str = Field(min_length=1, max_length=64)
    homeTeamId: str = Field(min_length=1, max_length=64)
    awayTeamId: str = Field(min_length=1, max_length=64)
    kickoffAt: datetime
    venue: str = Field(default="", max_length=200)
    stage: str = Field(default="", max_length=100)
    status: MatchStatus
    referees: list[RefereeAssignment] = []


class MatchUpdate(MatchCreate):
    version: int = Field(ge=0)


class MatchVersionRequest(StrictSchema):
    version: int = Field(ge=0)


class MatchReasonVersionRequest(StrictSchema):
    version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


def match_out(match) -> dict:
    data = {
        "id": match.pk,
        "eventId": match.event_id,
        "groupId": match.group_id,
        "homeTeamId": match.home_team_id,
        "awayTeamId": match.away_team_id,
        "kickoffAt": match.kickoff_at,
        "venue": match.venue or "",
        "stage": match.stage or "",
        "status": match.status,
        "referees": list(match.referees or []),
        "published": bool(match.published),
        "resultPublished": bool(match.result_published),
        "scoreRevision": int(match.score_revision or 0),
        "version": int(match.version or 0),
        "createdAt": match.created_at,
        "updatedAt": match.updated_at,
    }
    # 赛事/组别名称取成员5 本域；球队名称取成员4 Team 主档（只读解析）。
    data["eventName"] = display.event_name(match.event_id, match.event_id)
    data["groupName"] = display.group_name(match.group_id, match.group_id)
    home_name = display.team_name(match.home_team_id)
    away_name = display.team_name(match.away_team_id)
    if home_name:
        data["homeTeamName"] = home_name
    if away_name:
        data["awayTeamName"] = away_name
    if match.home_score is not None:
        data["homeScore"] = int(match.home_score)
    if match.away_score is not None:
        data["awayScore"] = int(match.away_score)
    return data

# ---------------------------------------------------------------- 比赛事件（5.2）

MatchEventType = Literal["GOAL", "PENALTY", "SUBSTITUTION"]
PenaltyType = Literal["YELLOW_CARD", "RED_CARD", "PENALTY_KICK", "OTHER"]


class MatchEventCreate(StrictSchema):
    """按 eventType 分支：GOAL 需 scorerAthleteId；PENALTY 需 penaltyType+reason；
    SUBSTITUTION 需 outAthleteId+inAthleteId。分支必填由服务层校验，三分支不混合。"""

    eventType: MatchEventType
    minute: int = Field(ge=0, le=150)
    stoppageMinute: Optional[int] = Field(default=None, ge=0, le=30)
    teamId: str = Field(min_length=1, max_length=64)
    matchVersion: int = Field(ge=0)
    note: str = Field(default="", max_length=1000)
    # GOAL 专用
    scorerAthleteId: Optional[str] = Field(default=None, min_length=1, max_length=64)
    ownGoal: bool = False
    # PENALTY 专用
    athleteId: Optional[str] = Field(default=None, min_length=1, max_length=64)
    penaltyType: Optional[PenaltyType] = None
    reason: Optional[str] = Field(default=None, min_length=1, max_length=500)
    # SUBSTITUTION 专用
    outAthleteId: Optional[str] = Field(default=None, min_length=1, max_length=64)
    inAthleteId: Optional[str] = Field(default=None, min_length=1, max_length=64)


class MatchEventUpdate(StrictSchema):
    version: int = Field(ge=0)
    event: MatchEventCreate


def match_event_out(event) -> dict:
    data = {
        "id": event.pk,
        "matchId": event.match_id,
        "eventType": event.event_type,
        "minute": int(event.minute),
        "teamId": event.team_id,
        "ownGoal": bool(event.own_goal),
        "note": event.note or "",
        "matchVersion": int(event.match_version or 0),
        "version": int(event.version or 0),
        "createdAt": event.created_at,
        "updatedAt": event.updated_at,
    }
    if event.stoppage_minute is not None:
        data["stoppageMinute"] = int(event.stoppage_minute)
    team_name = display.team_name(event.team_id)
    if team_name:
        data["teamName"] = team_name
    if event.scorer_athlete_id:
        data["scorerAthleteId"] = event.scorer_athlete_id
        data["scorerName"] = display.athlete_name(
            event.scorer_athlete_id, event.scorer_athlete_id
        )
    if event.athlete_id:
        data["athleteId"] = event.athlete_id
        data["athleteName"] = display.athlete_name(event.athlete_id, event.athlete_id)
    if event.penalty_type:
        data["penaltyType"] = event.penalty_type
    if event.reason:
        data["reason"] = event.reason
    if event.out_athlete_id:
        data["outAthleteId"] = event.out_athlete_id
        data["outAthleteName"] = display.athlete_name(
            event.out_athlete_id, event.out_athlete_id
        )
    if event.in_athlete_id:
        data["inAthleteId"] = event.in_athlete_id
        data["inAthleteName"] = display.athlete_name(
            event.in_athlete_id, event.in_athlete_id
        )
    return data

# ---------------------------------------------------------------- 赛事文件 / 图文直播 / 直播链接（5.3 / 5.4）

DocumentType = Literal["SIGN_IN", "SIGNED_RECORD", "REFEREE_REPORT"]


class MatchDocumentCreate(StrictSchema):
    documentType: DocumentType
    fileId: str = Field(min_length=1, max_length=64)


class LivePostCreate(StrictSchema):
    text: str = Field(min_length=1, max_length=3000)
    imageFileIds: list[str] = Field(default_factory=list, max_length=9)
    minute: Optional[int] = Field(default=None, ge=0, le=150)


class LivePostUpdate(StrictSchema):
    text: str = Field(min_length=1, max_length=3000)
    imageFileIds: list[str] = Field(default_factory=list, max_length=9)
    minute: Optional[int] = Field(default=None, ge=0, le=150)
    version: int = Field(ge=0)


class LiveLinkCreate(StrictSchema):
    title: str = Field(min_length=1, max_length=150)
    url: str = Field(min_length=1, max_length=2000)
    enabled: bool = True


class LiveLinkUpdate(StrictSchema):
    title: str = Field(min_length=1, max_length=150)
    url: str = Field(min_length=1, max_length=2000)
    enabled: bool = True
    version: int = Field(ge=0)


def match_document_out(doc) -> dict:
    return {
        "id": doc.pk,
        "matchId": doc.match_id,
        "documentType": doc.document_type,
        "fileId": doc.file_id,
        "version": int(doc.version or 0),
        "createdAt": doc.created_at,
        "updatedAt": doc.updated_at,
    }


def live_post_out(post) -> dict:
    data = {
        "id": post.pk,
        "matchId": post.match_id,
        "text": post.text,
        "imageFileIds": list(post.image_file_ids or []),
        "cursor": int(post.cursor or 0),
        "version": int(post.version or 0),
        "createdAt": post.created_at,
        "updatedAt": post.updated_at,
    }
    if post.minute is not None:
        data["minute"] = int(post.minute)
    return data


def live_link_out(link) -> dict:
    return {
        "id": link.pk,
        "matchId": link.match_id,
        "title": link.title,
        "url": link.url,
        "enabled": bool(link.enabled),
        "version": int(link.version or 0),
        "createdAt": link.created_at,
        "updatedAt": link.updated_at,
    }

# ---------------------------------------------------------------- 内容引擎（7.1）与门户资源（7.2）

ContentContentType = Literal["NEWS", "POLICY", "HIGHLIGHT", "BEST_LINEUP"]
ContentStatus = Literal["DRAFT", "PUBLISHED", "OFFLINE"]
ResourceType = Literal["CAROUSEL", "NEWS_LINK_IMAGE", "PARTNER"]


class VersionRequest(StrictSchema):
    version: int = Field(ge=0)


class ReasonVersionRequest(StrictSchema):
    version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


class ContentCreate(StrictSchema):
    contentType: ContentContentType
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=1000)
    bodyHtml: str = Field(min_length=1, max_length=50000)
    coverFileId: str = Field(default="", max_length=64)
    eventId: str = Field(default="", max_length=64)
    groupId: str = Field(default="", max_length=64)
    policyDocumentNo: str = Field(default="", max_length=100)
    policyEffectiveFrom: Optional[date] = None
    policyEffectiveTo: Optional[date] = None
    pinned: bool = False


class ContentUpdate(ContentCreate):
    version: int = Field(ge=0)


class PortalResourceCreate(StrictSchema):
    resourceType: ResourceType
    title: str = Field(min_length=1, max_length=150)
    imageFileId: str = Field(default="", max_length=64)
    targetContentId: str = Field(default="", max_length=64)
    targetUrl: str = Field(default="", max_length=2000)
    sortOrder: int = Field(default=0, ge=0)
    enabled: bool = True


class PortalResourceUpdate(PortalResourceCreate):
    version: int = Field(ge=0)


def _date_str(value) -> Optional[str]:
    return value.isoformat() if value else None


def content_out(content) -> dict:
    data = {
        "id": content.pk,
        "contentType": content.content_type,
        "title": content.title,
        "summary": content.summary or "",
        "bodyHtml": content.body_html,
        "coverFileId": content.cover_file_id or "",
        "eventId": content.event_id or "",
        "groupId": content.group_id or "",
        "policyDocumentNo": content.policy_document_no or "",
        "pinned": bool(content.pinned),
        "status": content.status,
        "revision": int(content.revision or 1),
        "bodyText": content.body_text or "",
        "version": int(content.version or 0),
        "createdAt": content.created_at,
        "updatedAt": content.updated_at,
    }
    data["policyEffectiveFrom"] = _date_str(content.policy_effective_from)
    data["policyEffectiveTo"] = _date_str(content.policy_effective_to)
    data["publishedAt"] = content.published_at
    return data


def _portal_image_url(file_id: str, base_url: str = "", variant: str = "content") -> str:
    """为已发布门户内容签发图片地址；文件缺失时返回空串，不阻断列表。"""
    if not file_id:
        return ""
    from apps.attachments import services as attachments

    try:
        url = attachments.portal_image_url(str(file_id), variant)
    except Exception:  # noqa: BLE001 - 图片缺失不应阻断公开列表
        return ""
    if base_url:
        from urllib.parse import urljoin

        return urljoin(base_url, url.lstrip("/"))
    return url


def public_content_out(content, base_url: str = "") -> dict:
    """PublicContent 安全投影：不含 version/status/bodyText/pinned 等内部字段。"""
    return {
        "id": content.pk,
        "contentType": content.content_type,
        "title": content.title,
        "summary": content.summary or "",
        "bodyHtml": content.body_html,
        "publicCoverUrl": _portal_image_url(content.cover_file_id, base_url),
        "eventId": content.event_id or "",
        "groupId": content.group_id or "",
        "publishedAt": content.published_at,
        "revision": int(content.revision or 1),
    }


def portal_resource_out(resource) -> dict:
    return {
        "id": resource.pk,
        "resourceType": resource.resource_type,
        "title": resource.title,
        "imageFileId": resource.image_file_id or "",
        "targetContentId": resource.target_content_id or "",
        "targetUrl": resource.target_url or "",
        "sortOrder": int(resource.sort_order or 0),
        "enabled": bool(resource.enabled),
        "version": int(resource.version or 0),
        "createdAt": resource.created_at,
        "updatedAt": resource.updated_at,
    }


def public_resource_out(resource, base_url: str = "") -> dict:
    """PublicResource 安全投影：不暴露 imageFileId 内部引用与 enabled。"""
    return {
        "id": resource.pk,
        "resourceType": resource.resource_type,
        "title": resource.title,
        "imageUrl": _portal_image_url(resource.image_file_id, base_url),
        "targetContentId": resource.target_content_id or "",
        "targetUrl": resource.target_url or "",
        "sortOrder": int(resource.sort_order or 0),
    }

# ---------------------------------------------------------------- 赛果(5.5)与进球榜

class MatchResultWrite(StrictSchema):
    version: int = Field(ge=0)
    resultType: Literal["NORMAL", "FORFEIT", "ADMIN_CORRECTION"]
    manualHomeScore: Optional[int] = Field(None, ge=0)
    manualAwayScore: Optional[int] = Field(None, ge=0)
    reason: Optional[str] = Field(None, max_length=1000)


def match_result_out(match) -> dict:
    """MatchResult 投影；NORMAL 比分来自事件派生，人工比分落在 home/awayScore。"""
    return {
        "matchId": match.pk,
        "resultType": match.result_type,
        "homeScore": int(match.home_score or 0),
        "awayScore": int(match.away_score or 0),
        "reason": match.result_reason or "",
        "published": bool(match.result_published),
        "sourceScoreRevision": int(match.score_revision or 0),
        "version": int(match.version or 0),
    }


def goal_ranking_row_out(rank: int, athlete_id: str, athlete_name: str,
                         team_id: str, team_name: str, goals: int) -> dict:
    return {
        "rank": rank,
        "athleteId": athlete_id,
        "athleteName": athlete_name,
        "teamId": team_id,
        "teamName": team_name,
        "goals": int(goals),
    }

# ---------------------------------------------------------------- 门户公开只读(T5-07)

def public_event_out(event) -> dict:
    """PublicEvent 安全投影：不含 version/createdAt/updatedAt 内部字段；
    status 保留（访客需据此判断报名/进行状态）。"""
    phase = event_phase(event)
    return {
        "id": event.pk,
        "name": event.name,
        "season": event.season,
        "eventCategory": event.event_category,
        "status": event.status,
        "currentPhase": phase["currentPhase"],
        "phaseStartedAt": phase["phaseStartedAt"],
        "phaseEndsAt": phase["phaseEndsAt"],
        "progressPercent": phase["progressPercent"],
        "registrationOpensAt": event.registration_opens_at,
        "registrationClosesAt": event.registration_closes_at,
        "description": event.description or "",
    }

def public_match_out(match) -> dict:
    """PublicMatch 安全投影：仅已发布对阵；名称取赛事/球队主档，查不到回退 ID。"""
    return {
        "id": match.pk,
        "eventId": match.event_id,
        "eventName": display.event_name(match.event_id, match.event_id),
        "groupId": match.group_id,
        "groupName": display.group_name(match.group_id, match.group_id),
        "homeTeamName": display.team_name(match.home_team_id, match.home_team_id),
        "awayTeamName": display.team_name(match.away_team_id, match.away_team_id),
        "kickoffAt": match.kickoff_at,
        "venue": match.venue or "",
        "stage": match.stage or "",
        "status": match.status,
        "referees": list(match.referees or []),
        "homeScore": int(match.home_score or 0),
        "awayScore": int(match.away_score or 0),
        "scoreRevision": int(match.score_revision or 0),
    }


def public_match_detail_out(match, events) -> dict:
    """门户比赛详情：在对阵投影上补裁判组与已记录事件（含姓名投影）。"""
    data = public_match_out(match)
    data["events"] = [match_event_out(event) for event in events]
    return data


def public_live_post_out(post, base_url: str = "") -> dict:
    """PublicLivePost 投影；图片按已发布内容签发短期地址。"""
    image_urls = []
    for file_id in post.image_file_ids or []:
        url = _portal_image_url(str(file_id), base_url)
        if url:
            image_urls.append(url)
    return {
        "id": post.pk,
        "text": post.text,
        "imageUrls": image_urls,
        "minute": post.minute,
        "updatedAt": post.updated_at,
    }


def public_live_link_out(link) -> dict:
    """公开直播外链：只含标题与URL，不暴露内部ID/version。"""
    return {"title": link.title, "url": link.url}