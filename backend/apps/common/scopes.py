"""数据范围校验：角色 → 权限点之后的第三层。

契约 3：ADMIN "不是无条件全库权限"，其范围是所属组织子树；
省/市账号不得越级或越域操作，也不能通过重置密码间接提权。
"""

from __future__ import annotations

from typing import Iterable, Optional

from .errors import DATA_SCOPE_DENIED, FORBIDDEN, ApiError
from .permissions import (
    ADMIN,
    ALL_ROLES,
    ATHLETE,
    CITY,
    COACH,
    MATCH_RECORDER,
    PROVINCE_REVIEWER,
    ROLE_NAMES,
    TEAM,
    UNIVERSITY,
)

# 账号角色策略：建号（授予）与改号（可管理目标）共用同一张表。
# 原则：能建的角色必须能管，避免"学校建了球队账号却改不了"。
# 删号/重置密码仍由权限点收口（仅 ADMIN/省级），不在本表放行。
# 未列出的角色（ATHLETE/COACH/MATCH_RECORDER）不具备账号管理权限。
ROLE_POLICY: dict[str, frozenset[str]] = {
    ADMIN: frozenset(ALL_ROLES),
    PROVINCE_REVIEWER: frozenset(
        {CITY, UNIVERSITY, TEAM, MATCH_RECORDER, ATHLETE, COACH}
    ),
    CITY: frozenset({UNIVERSITY, TEAM, MATCH_RECORDER, ATHLETE, COACH}),
    UNIVERSITY: frozenset({TEAM, ATHLETE, COACH}),
    TEAM: frozenset(),
}


def is_admin(account) -> bool:
    return ADMIN in (getattr(account, "roles", ()) or ())


def is_global_admin(account) -> bool:
    """无所属组织的 ADMIN 视为全局管理员：读写都覆盖全库。

    挂组织的 ADMIN 仍按组织子树限定，避免"读空写通"的半坏状态。
    """
    return is_admin(account) and not account_org_id(account)


def policy_roles(account) -> frozenset[str]:
    """当前账号可授予、可管理的角色集合（多角色取并集）。"""
    allowed: set[str] = set()
    for role in getattr(account, "roles", ()) or ():
        allowed.update(ROLE_POLICY.get(role, ()))
    return frozenset(allowed)


def account_org_id(account) -> str:
    org = getattr(account, "organization_id", None)
    return org or ""


def _subtree_ids(org_id: str) -> list[str]:
    """自顶向下收集 org_id 及其所有后代（含软删过滤）。"""
    from apps.accounts.models import Organization

    ids: list[str] = []
    seen: set[str] = set()
    queue = [org_id]
    while queue:
        current = queue.pop()
        if not current or current in seen:
            continue
        if not Organization.objects.filter(pk=current).exists():
            continue
        seen.add(current)
        ids.append(current)
        queue.extend(
            Organization.objects.filter(parent_id=current).values_list("id", flat=True)
        )
    return ids


def org_scope_ids(account) -> list[str]:
    """当前账号可见的组织范围；全局管理员由调用方单独放行（返回空表示无范围）。"""
    org_id = account_org_id(account)
    if not org_id:
        return []
    return _subtree_ids(org_id)


def visible_school_codes(account) -> Optional[set[str]]:
    """账号可见的学校类组织 code（SCHOOL/UNIVERSITY）。

    返回 None 表示全量（无组织的全局管理员）；返回空集表示无学校范围。
    按 A′-min 约定组织节点 code 与 School.code 一一对应，调用方用
    `school__code__in` 过滤即可，不需要逻辑外键。
    """
    if is_global_admin(account):
        return None
    ids = set(org_scope_ids(account))
    if not ids:
        return set()
    from apps.accounts.models import Organization

    return set(
        Organization.objects.filter(
            pk__in=ids, org_type__in=("SCHOOL", "UNIVERSITY")
        ).values_list("code", flat=True)
    )


def account_org_code(account) -> str:
    """账号绑定组织节点的 code（TEAM 账号即 team_no）；无组织返回空串。"""
    org_id = account_org_id(account)
    if not org_id:
        return ""
    from apps.accounts.models import Organization

    return (
        Organization.objects.filter(pk=org_id)
        .values_list("code", flat=True)
        .first()
        or ""
    )


def ensure_org_in_scope(account, org_id: Optional[str]) -> None:
    if is_global_admin(account):
        return
    if not org_id:
        raise ApiError(DATA_SCOPE_DENIED, "缺少所属组织，无法确定数据范围")
    if org_id not in set(org_scope_ids(account)):
        raise ApiError(DATA_SCOPE_DENIED, "目标组织不在当前账号数据范围内")


def ensure_grantable_roles(account, roles: Iterable[str], org_id: Optional[str]) -> None:
    """创建/修改账号时：只能授予 ROLE_POLICY 允许的角色，且组织在范围内。"""
    requested = set(roles or ())
    denied = requested - policy_roles(account)
    if denied:
        if ADMIN in denied:
            raise ApiError(
                FORBIDDEN,
                "只有管理员账号可以授予管理员角色",
                [{"field": "roles", "reason": "无权授予 ADMIN"}],
            )
        raise ApiError(
            FORBIDDEN,
            "无权授予所选角色",
            [{"field": "roles", "reason": f"无权授予：{', '.join(sorted(denied))}"}],
        )
    ensure_org_in_scope(account, org_id)


def ensure_manageable_target(account, target) -> None:
    """改/删/停用/重置时：不得作用于自己或超出可管理角色范围的账号。"""
    if target.pk == getattr(account, "pk", None):
        raise ApiError(FORBIDDEN, "不能对本账号执行该操作")
    target_roles = set(getattr(target, "roles", ()) or ())
    denied = target_roles - policy_roles(account)
    if denied:
        raise ApiError(
            FORBIDDEN,
            "不能操作同级或更高权限的账号",
            [
                {
                    "field": "accountId",
                    "reason": f"目标角色超出可管理范围：{', '.join(sorted(denied))}",
                }
            ],
        )


def role_names(roles: Optional[Iterable[str]]) -> list[str]:
    return [ROLE_NAMES.get(role, role) for role in (roles or ())]
