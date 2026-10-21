"""events 业务逻辑：赛事与赛事组别的查询、创建、更新、软删除。

规则集中在本层，视图只做权限校验与信封包装；写操作带乐观锁 version，
删除为软删除，存在受保护历史关联时返回 409 STATE_CONFLICT，不级联破坏。
"""

from __future__ import annotations

from typing import Optional

from django.utils import timezone

from apps.common.errors import (
    BUSINESS_VALIDATION_FAILED,
    NOT_FOUND,
    STATE_CONFLICT,
    ApiError,
)
from apps.common.ids import new_id
from apps.common.versioning import bump, check_version

from .models import Event, EventGroup


# ---------------------------------------------------------------- 内部校验

def _validate_window(opens, closes) -> None:
    if closes <= opens:
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "报名截止时间必须晚于报名开始时间",
            [{"field": "registrationClosesAt", "reason": "必须晚于 registrationOpensAt"}],
        )


def _validate_age_range(min_age: Optional[int], max_age: Optional[int]) -> None:
    if min_age is not None and max_age is not None and min_age > max_age:
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "最小年龄不能大于最大年龄",
            [{"field": "maxAge", "reason": "maxAge 不能小于 minAge"}],
        )


# ---------------------------------------------------------------- 赛事

def list_events(filters: dict):
    queryset = Event.objects.all()
    if filters.get("name"):
        queryset = queryset.filter(name__icontains=filters["name"])
    if filters.get("season"):
        queryset = queryset.filter(season=filters["season"])
    if filters.get("status"):
        queryset = queryset.filter(status=filters["status"])
    if filters.get("eventCategory"):
        queryset = queryset.filter(event_category=filters["eventCategory"])
    return queryset.order_by("-created_at", "id")


def create_event(data: dict) -> Event:
    _validate_window(data["registrationOpensAt"], data["registrationClosesAt"])
    return Event.objects.create(
        id=new_id("evt"),
        name=data["name"],
        season=data["season"],
        event_category=data.get("eventCategory", ""),
        registration_opens_at=data["registrationOpensAt"],
        registration_closes_at=data["registrationClosesAt"],
        status=data.get("status", "DRAFT"),
        description=data.get("description", ""),
    )


def get_event(event_id: str) -> Event:
    event = Event.objects.filter(pk=event_id).first()
    if event is None:
        raise ApiError(NOT_FOUND)
    return event


def update_event(event_id: str, data: dict) -> Event:
    event = get_event(event_id)
    check_version(event, data.get("version"))
    _validate_window(data["registrationOpensAt"], data["registrationClosesAt"])
    event.name = data["name"]
    event.season = data["season"]
    event.event_category = data.get("eventCategory", "")
    event.registration_opens_at = data["registrationOpensAt"]
    event.registration_closes_at = data["registrationClosesAt"]
    event.status = data.get("status", event.status)
    event.description = data.get("description", "")
    bump(event)
    event.save()
    return event


def delete_event(event_id: str, version: Optional[int]) -> Event:
    event = get_event(event_id)
    check_version(event, version)
    if EventGroup.objects.filter(event=event).exists():
        raise ApiError(STATE_CONFLICT, "该赛事下仍有组别，不能删除")
    event.deleted_at = timezone.now()
    bump(event)
    event.save()
    return event


def available_events(filters: dict):
    """报名可见赛事：默认只看报名进行中；registrationOpen=false 时取其余状态。"""
    queryset = Event.objects.all()
    if filters.get("registrationOpen") is True:
        queryset = queryset.filter(status="REGISTRATION_OPEN")
    elif filters.get("registrationOpen") is False:
        queryset = queryset.exclude(status="REGISTRATION_OPEN")
    if filters.get("season"):
        queryset = queryset.filter(season=filters["season"])
    if filters.get("eventCategory"):
        queryset = queryset.filter(event_category=filters["eventCategory"])
    return queryset.order_by("-created_at", "id")


# ---------------------------------------------------------------- 赛事组别

def list_groups(event_id: str, filters: dict):
    event = get_event(event_id)
    queryset = EventGroup.objects.filter(event=event)
    if filters.get("category"):
        queryset = queryset.filter(category=filters["category"])
    return queryset.order_by("created_at", "id")


def create_group(event_id: str, data: dict) -> EventGroup:
    event = get_event(event_id)
    _validate_age_range(data.get("minAge"), data.get("maxAge"))
    return EventGroup.objects.create(
        id=new_id("evg"),
        event=event,
        name=data["name"],
        category=data["category"],
        gender=data["gender"],
        quota=data.get("quota", 0),
        min_age=data.get("minAge"),
        max_age=data.get("maxAge"),
        max_players=data["maxPlayers"],
        required_material_types=list(data.get("requiredMaterialTypes") or []),
    )


def get_group(event_id: str, group_id: str) -> EventGroup:
    group = EventGroup.objects.filter(event_id=event_id, pk=group_id).first()
    if group is None:
        raise ApiError(NOT_FOUND)
    return group


def update_group(event_id: str, group_id: str, data: dict) -> EventGroup:
    group = get_group(event_id, group_id)
    check_version(group, data.get("version"))
    _validate_age_range(data.get("minAge"), data.get("maxAge"))
    group.name = data["name"]
    group.category = data["category"]
    group.gender = data["gender"]
    group.quota = data.get("quota", 0)
    group.min_age = data.get("minAge")
    group.max_age = data.get("maxAge")
    group.max_players = data["maxPlayers"]
    group.required_material_types = list(data.get("requiredMaterialTypes") or [])
    bump(group)
    group.save()
    return group


def delete_group(event_id: str, group_id: str, version: Optional[int]) -> EventGroup:
    group = get_group(event_id, group_id)
    check_version(group, version)
    # D1 尚无报名快照/比赛事件引用；存在受保护关联时在此追加 409 判断
    group.deleted_at = timezone.now()
    bump(group)
    group.save()
    return group