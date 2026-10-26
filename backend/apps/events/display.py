"""展示名解析：把各域存储的字符串 ID 投影为可读名称。

只服务于响应 mapper；查不到时返回调用方给定的兜底值（默认原 ID），
不猜测、不伪造名称。跨域读取均为只读查询，避免与成员4/成员3 的写链耦合。
"""

from __future__ import annotations

from typing import Optional


def event_name(event_id: str, fallback: Optional[str] = None) -> str:
    if not event_id:
        return fallback or ""
    from .models import Event

    name = Event.objects.filter(pk=event_id).values_list("name", flat=True).first()
    return name or (fallback if fallback is not None else event_id)


def group_name(group_id: str, fallback: Optional[str] = None) -> str:
    if not group_id:
        return fallback or ""
    from .models import EventGroup

    name = EventGroup.objects.filter(pk=group_id).values_list("name", flat=True).first()
    return name or (fallback if fallback is not None else group_id)


def team_name(team_id: str, fallback: Optional[str] = None) -> str:
    """球队名：成员4 的 Team 主键为整型，兼容按 team_no 查询。"""
    if not team_id:
        return fallback or ""
    from apps.teams.models import Team

    raw = str(team_id)
    queryset = Team.objects.filter(is_deleted=False)
    name = None
    if raw.isdigit():
        name = queryset.filter(pk=int(raw)).values_list("name", flat=True).first()
    if name is None:
        name = queryset.filter(team_no=raw).values_list("name", flat=True).first()
    return name or (fallback if fallback is not None else "")


def athlete_name(athlete_id: str, fallback: Optional[str] = None) -> str:
    """运动员姓名：优先取报名名单中的姓名快照，其次人员主档。"""
    if not athlete_id:
        return fallback or ""
    from apps.enrollments.models import RosterItem

    name = (
        RosterItem.objects.filter(athlete_id=athlete_id)
        .order_by("-created_at")
        .values_list("athlete_name", flat=True)
        .first()
    )
    if name:
        return name
    raw = str(athlete_id)
    if raw.isdigit():
        from apps.persons.models import Person

        person = (
            Person.objects.filter(pk=int(raw), is_deleted=False)
            .values_list("full_name", flat=True)
            .first()
        )
        if person:
            return person
    return fallback if fallback is not None else athlete_id
