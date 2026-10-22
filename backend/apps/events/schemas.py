"""events 请求模型与响应 mapper（django-ninja / pydantic v2）。

请求模型继承 StrictSchema（多传未知字段直接 400 INVALID_ARGUMENT）；
响应由 mapper 转契约 camelCase，禁止改名、禁止加字段。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import Field

from apps.common.schemas import StrictSchema

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
    data = {
        "id": event.pk,
        "name": event.name,
        "season": event.season,
        "eventCategory": event.event_category,
        "registrationOpensAt": event.registration_opens_at,
        "registrationClosesAt": event.registration_closes_at,
        "status": event.status,
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
    # 球队名称由成员4球队服务维护，本库不冗余；未取到时不输出该可选字段
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
    if event.scorer_athlete_id:
        data["scorerAthleteId"] = event.scorer_athlete_id
    if event.athlete_id:
        data["athleteId"] = event.athlete_id
    if event.penalty_type:
        data["penaltyType"] = event.penalty_type
    if event.reason:
        data["reason"] = event.reason
    if event.out_athlete_id:
        data["outAthleteId"] = event.out_athlete_id
    if event.in_athlete_id:
        data["inAthleteId"] = event.in_athlete_id
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