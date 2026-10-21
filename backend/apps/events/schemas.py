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