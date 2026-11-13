"""报名域数据范围（第三/四层校验）。

- TEAM / UNIVERSITY：只看本队/本校组织子树映射出的球队；
- CITY / PROVINCE_REVIEWER / 挂组织 ADMIN：按组织子树过滤 city_org_id；
- 无组织 ADMIN（全局管理员）：全库读写（与文档“未挂组织 ADMIN 覆盖全库”
  一致）；MATCH_RECORDER 全局只读；
- 筛选条件只能缩小范围，不能扩大权限。

本模块同时向 attachments 注册 ENROLLMENT 归属校验器。
"""

from __future__ import annotations

from typing import Optional

from django.db.models import Q

from apps.common.errors import DATA_SCOPE_DENIED, FORBIDDEN, NOT_FOUND, ApiError
from apps.common.permissions import (
    MATCH_RECORDER,
    ORG_SCOPE_ROLES,
    TEAM,
    UNIVERSITY,
)
from apps.common.scopes import is_global_admin, org_scope_ids

from .clients import team_client
from .models import STATUS_DRAFT, Enrollment

# 审核/管理/记录端：只看已提交的报名，草稿属于报送账号自己的未完成数据。
_DRAFT_HIDDEN_ROLES = ORG_SCOPE_ROLES | {MATCH_RECORDER}
# 报送端：草稿只对创建该草稿的账号可见。
_SUBMITTER_ROLES = {TEAM, UNIVERSITY}


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


def _apply_draft_visibility(account, queryset):
    """草稿可见性：只留在创建者自己的账号里，管理/审核/记录端只看已提交报名。"""
    roles = role_set(account)
    if roles & _DRAFT_HIDDEN_ROLES:
        return queryset.exclude(status=STATUS_DRAFT)
    if roles & _SUBMITTER_ROLES:
        return queryset.exclude(Q(status=STATUS_DRAFT) & ~Q(created_by=account.pk))
    return queryset.exclude(status=STATUS_DRAFT)


def scoped_queryset(account, queryset):
    """按数据范围过滤报名单 QuerySet（含草稿可见性）。"""
    return _apply_draft_visibility(account, _org_scoped_queryset(account, queryset))


def _org_scoped_queryset(account, queryset):
    """按角色/组织树收窄数据范围，不处理草稿。"""
    # 记录员：赛前身份核验需跨队/跨市读取名册，授予全局只读（写权限由 R1/R3 拦截，不在 R5）。
    if MATCH_RECORDER in role_set(account):
        return queryset
    # 全局管理员（未挂组织）：文档口径为全库；挂组织 ADMIN 仍走组织子树。
    if is_global_admin(account):
        return queryset
    if is_org_scoped(account):
        org_ids = org_scope_ids(account)
        if not org_ids:
            return queryset.none()
        return queryset.filter(city_org_id__in=org_ids)
    team_ids = team_scope_ids(account)
    if not team_ids:
        return queryset.none()
    return queryset.filter(Q(team_id__in=team_ids))


def _ensure_draft_visible(account, enrollment: Enrollment) -> None:
    """未提交草稿只允许创建者本人读取，避免管理员/审核端/同组织他人看到半成品。"""
    if enrollment.status != STATUS_DRAFT:
        return
    roles = role_set(account)
    if roles & _SUBMITTER_ROLES and enrollment.created_by == account.pk:
        return
    raise ApiError(
        DATA_SCOPE_DENIED,
        "草稿仅创建者本人可见",
        [{"field": "enrollmentId", "reason": "未提交的草稿不对外可见"}],
    )


def ensure_enrollment_access(account, enrollment: Enrollment) -> None:
    _ensure_draft_visible(account, enrollment)
    if MATCH_RECORDER in role_set(account):
        return
    if is_global_admin(account):
        return
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
    """谁能以什么主体类型报单。

    规则（2026-11-01 用户裁定修订）：
    - UNIVERSITY 角色：本校球队 SCHOOL_TEAM（UNIVERSITY 主体本项目无高校组，已拒）
    - TEAM 角色：仅本队 SCHOOL_TEAM，球队归属由 ensure_team_in_scope 校验
    - CITY/PROVINCE_REVIEWER/ADMIN：不可报送（不代建代提，含市州代报）
    """
    roles = role_set(account)
    allowed: set[str] = set()
    if UNIVERSITY in roles or TEAM in roles:
        allowed.add("SCHOOL_TEAM")
    if not allowed:
        raise ApiError(
            FORBIDDEN,
            "当前角色无报送权限",
            [{"field": "subjectType", "reason": "仅学校端/球队端可报送"}],
        )
    # 2026-10-28 业务规则：本项目无"高校组"，报送主体仅中小学球队
    # （CITY 代报是否放行属 D-1 契约，仍走原 403 分支）；UNIVERSITY
    # 高校主体在服务端拒绝，不静默转换。
    if subject_type != "SCHOOL_TEAM":
        raise ApiError(
            FORBIDDEN,
            "报送主体类型非法",
            [{"field": "subjectType", "reason": "本项目无高校组，仅支持中小学球队报送"}],
        )


def ensure_enrollment_creator(account, enrollment: Enrollment) -> None:
    """报名单的编辑/提交/撤回：学校端（创建者本人）或球队端（本队）可操作。

    - UNIVERSITY：仅创建该报名的账号（本校范围内，不代他人操作）；
    - TEAM（2026-11-01 用户裁定放开）：报名单所属球队在账号数据范围内即可，
      不再要求 created_by 一致——球队账号换人接手时仍能继续维护本队报名。
    """
    roles = role_set(account)
    if UNIVERSITY in roles:
        if account.pk != enrollment.created_by:
            raise ApiError(
                FORBIDDEN,
                "仅创建该报名的账号可操作",
                [{"field": "enrollmentId", "reason": "非报名的创建者"}],
            )
        return
    if TEAM in roles:
        if enrollment.team_id not in set(team_scope_ids(account)):
            raise ApiError(
                DATA_SCOPE_DENIED,
                "该报名单不在当前账号的球队范围内",
                [{"field": "enrollmentId", "reason": "超出球队范围"}],
            )
        return
    raise ApiError(
        FORBIDDEN,
        "当前角色不能操作报名单",
        [{"field": "enrollmentId", "reason": "仅学校端/球队端可操作"}],
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
