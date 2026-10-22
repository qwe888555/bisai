"""报名域数据范围（第三/四层校验）。

- TEAM / UNIVERSITY：只看本队/本校组织子树映射出的球队；
- CITY / PROVINCE_REVIEWER / ADMIN：按组织子树过滤 city_org_id；
- 筛选条件只能缩小范围，不能扩大权限。

本模块同时向 attachments 注册 ENROLLMENT 归属校验器。
"""

from __future__ import annotations

from typing import Optional

from django.db.models import Q

from apps.common.errors import DATA_SCOPE_DENIED, FORBIDDEN, NOT_FOUND, ApiError
from apps.common.permissions import (
    ADMIN,
    CITY,
    ORG_SCOPE_ROLES,
    PROVINCE_REVIEWER,
    TEAM,
    UNIVERSITY,
)
from apps.common.scopes import org_scope_ids

from .clients import team_client
from .models import Enrollment


def role_set(account) -> set[str]:
    return set(getattr(account, "roles", ()) or ())


def is_org_scoped(account) -> bool:
    return bool(role_set(account) & ORG_SCOPE_ROLES)


def team_scope_ids(account) -> list[str]:
    """本账号可报送/可见的球队 ID（按组织子树映射）。"""
    ids: list[str] = []
    seen: set[str] = set()
    for org_id in org_scope_ids(account):
        for team_id in team_client.team_ids_for_org(org_id):
            if team_id and team_id not in seen:
                seen.add(team_id)
                ids.append(team_id)
    return ids


def scoped_queryset(account, queryset):
    """按数据范围过滤报名单 QuerySet。"""
    if is_org_scoped(account):
        org_ids = org_scope_ids(account)
        if not org_ids:
            return queryset.none()
        return queryset.filter(city_org_id__in=org_ids)
    team_ids = team_scope_ids(account)
    if not team_ids:
        return queryset.none()
    return queryset.filter(Q(team_id__in=team_ids))


def ensure_enrollment_access(account, enrollment: Enrollment) -> None:
    if is_org_scoped(account):
        if enrollment.city_org_id not in set(org_scope_ids(account)):
            raise ApiError(
                DATA_SCOPE_DENIED,
                "该报名单不在当前账号的数据范围内",
                [{"field": "enrollmentId", "reason": "超出组织范围"}],
            )
        return
    if enrollment.team_id not in set(team_scope_ids(account)):
        raise ApiError(
            DATA_SCOPE_DENIED,
            "该报名单不在当前账号的数据范围内",
            [{"field": "enrollmentId", "reason": "超出球队范围"}],
        )


def get_enrollment(account, enrollment_id: str) -> Enrollment:
    enrollment = Enrollment.objects.filter(pk=enrollment_id).first()
    if enrollment is None:
        raise ApiError(NOT_FOUND)
    ensure_enrollment_access(account, enrollment)
    return enrollment


def subject_type_allowed(account, subject_type: str) -> None:
    """subjectType 必须匹配账号实际报送权限（不能自报 CITY 越级）。"""
    roles = role_set(account)
    if subject_type == "SCHOOL_TEAM" and TEAM in roles:
        return
    if subject_type == "UNIVERSITY" and UNIVERSITY in roles:
        return
    if subject_type == "CITY" and CITY in roles:
        return
    if roles & {ADMIN, PROVINCE_REVIEWER}:
        return
    raise ApiError(
        FORBIDDEN,
        "当前账号无权以该主体类型报送",
        [{"field": "subjectType", "reason": "与账号报送权限不匹配"}],
    )


def ensure_team_in_scope(account, team: dict) -> None:
    """校验球队归当前账号可报送（TEAM/UNIVERSITY 仅本队；市州/省按组织子树）。"""
    team_id = team.get("id") or ""
    if is_org_scoped(account):
        city_org_id = team.get("cityOrgId") or ""
        if not city_org_id:
            raise ApiError(FORBIDDEN, "球队缺少所属市州，无法确定数据范围")
        if city_org_id not in set(org_scope_ids(account)):
            raise ApiError(
                DATA_SCOPE_DENIED,
                "球队不在当前账号的组织范围内",
                [{"field": "teamId", "reason": "超出组织范围"}],
            )
        return
    if team_id not in set(team_scope_ids(account)):
        raise ApiError(
            DATA_SCOPE_DENIED,
            "球队不在当前账号的数据范围内",
            [{"field": "teamId", "reason": "超出球队范围"}],
        )


# ---------------------------------------------------------------- 文件归属校验器


def verify_enrollment_owner(account, enrollment_id: str) -> None:
    """attachments 回调：当前账号能否把文件挂到该报名对象。"""
    enrollment: Optional[Enrollment] = Enrollment.objects.filter(pk=enrollment_id).first()
    if enrollment is None:
        raise ApiError(NOT_FOUND)
    ensure_enrollment_access(account, enrollment)


def allowed_export_queryset(account, filters: dict):
    """批量导出：filters 只在账号数据范围内生效。"""
    queryset = scoped_queryset(account, Enrollment.objects.all())
    return apply_filters(queryset, filters)


def apply_filters(queryset, filters: Optional[dict]):
    filters = filters or {}
    event_id = filters.get("eventId")
    if event_id:
        queryset = queryset.filter(event_id=event_id)
    group_id = filters.get("groupId")
    if group_id:
        queryset = queryset.filter(group_id=group_id)
    city_org_id = filters.get("cityOrgId")
    if city_org_id:
        queryset = queryset.filter(city_org_id=city_org_id)
    subject_type = filters.get("subjectType")
    if subject_type:
        queryset = queryset.filter(subject_type=subject_type)
    status = filters.get("status")
    if status:
        queryset = queryset.filter(status=status)
    team_name = (filters.get("teamName") or "").strip()
    if team_name:
        from .clients import team_client as teams

        matched = [
            team_id
            for team_id in queryset.values_list("team_id", flat=True).distinct()
            if team_name in (teams.get_team(team_id).get("name") or "")
        ]
        queryset = queryset.filter(team_id__in=matched)
    return queryset
