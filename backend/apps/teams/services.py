"""球队域只读查询：为对战、报名与门户接口提供名称解析。

成员4 的球队写链尚未合入 main；这里仅暴露 list/get 两个只读入口，
数据范围沿用成员4 的冻结约定（一期仅 ADMIN 全量），避免越权读取。
"""

from __future__ import annotations

from django.db.models import Count, OuterRef, Q, QuerySet, Subquery

from apps.common import enums
from apps.common.errors import DATA_SCOPE_DENIED, NOT_FOUND, ApiError
from apps.common.permissions import ADMIN
from apps.teams.models import Team, TeamCoach


def _team_queryset() -> QuerySet[Team]:
    return Team.objects.filter(is_deleted=False).select_related("school", "region")


def _annotate_team(queryset: QuerySet[Team]) -> QuerySet[Team]:
    head_coach = TeamCoach.objects.filter(
        team=OuterRef("pk"),
        status=enums.RelationStatus.CURRENT,
        duty=enums.CoachDuty.HEAD_COACH,
    ).values("person__full_name")[:1]
    return queryset.annotate(
        member_count=Count(
            "teammember",
            filter=Q(teammember__status=enums.RelationStatus.CURRENT),
        ),
        head_coach_name=Subquery(head_coach),
    )


def _require_admin_scope(account) -> None:
    if ADMIN in (getattr(account, "roles", ()) or ()):
        return
    raise ApiError(DATA_SCOPE_DENIED)


def list_teams(filters: dict, account) -> QuerySet[Team]:
    _require_admin_scope(account)
    queryset = _team_queryset()
    keyword = filters.get("keyword")
    if keyword:
        queryset = queryset.filter(
            Q(name__icontains=keyword) | Q(team_no__icontains=keyword)
        )
    if filters.get("category"):
        queryset = queryset.filter(category_code=filters["category"])
    if filters.get("status"):
        queryset = queryset.filter(audit_status=filters["status"])
    return _annotate_team(queryset).order_by("-created_at", "-id")


def get_team(team_id: str, account) -> Team:
    _require_admin_scope(account)
    raw = str(team_id or "").strip()
    queryset = _annotate_team(_team_queryset())
    team = None
    if raw.isdigit():
        team = queryset.filter(pk=int(raw)).first()
    if team is None:
        team = queryset.filter(team_no=raw).first()
    if team is None:
        raise ApiError(NOT_FOUND)
    return team
