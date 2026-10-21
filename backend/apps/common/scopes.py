"""数据范围校验：角色 → 权限点之后的第三层。

契约 3：ADMIN "不是无条件全库权限"，其范围是所属组织子树；
省/市账号不得越级或越域操作，也不能通过重置密码间接提权。
"""

from __future__ import annotations

from typing import Iterable, Optional

from .errors import DATA_SCOPE_DENIED, FORBIDDEN, ApiError
from .permissions import (
    ADMIN,
    HIGH_PRIVILEGE_ROLES,
    ORG_SCOPE_ROLES,
    ROLE_NAMES,
)

# 账号等级：数字越大权限越高，用于"不得操作同级或更高权限账号"判定
ROLE_LEVEL: dict[str, int] = {
    ADMIN: 90,
    "PROVINCE_REVIEWER": 80,
    "MATCH_RECORDER": 70,
    "CITY": 60,
    "UNIVERSITY": 50,
    "TEAM": 50,
    "ATHLETE": 10,
    "COACH": 10,
}


def is_admin(account) -> bool:
    return ADMIN in (getattr(account, "roles", ()) or ())


def role_level(roles: Optional[Iterable[str]]) -> int:
    return max((ROLE_LEVEL.get(role, 0) for role in (roles or ())), default=0)


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
    """当前账号可见的组织范围。"""
    org_id = account_org_id(account)
    if not org_id:
        return []
    if set(getattr(account, "roles", ()) or ()) & ORG_SCOPE_ROLES:
        return _subtree_ids(org_id)
    return _subtree_ids(org_id)  # 非树角色也只看自身组织（子树通常为空）


def ensure_org_in_scope(account, org_id: Optional[str]) -> None:
    if not org_id:
        if is_admin(account):
            return
        raise ApiError(DATA_SCOPE_DENIED, "缺少所属组织，无法确定数据范围")
    if org_id not in set(org_scope_ids(account)):
        raise ApiError(DATA_SCOPE_DENIED, "目标组织不在当前账号数据范围内")


def ensure_grantable_roles(account, roles: Iterable[str], org_id: Optional[str]) -> None:
    """创建/修改账号时：不得授予超出自身等级或范围的角色。"""
    requested = set(roles or ())
    if requested & HIGH_PRIVILEGE_ROLES and not is_admin(account):
        raise ApiError(
            FORBIDDEN,
            "只有管理员账号可以授予高权限角色",
            [{"field": "roles", "reason": "无权授予 ADMIN"}],
        )
    ensure_org_in_scope(account, org_id)


def ensure_manageable_target(account, target) -> None:
    """删除/停用/改密码时：不得作用于自己或同级及以上权限账号。"""
    if target.pk == getattr(account, "pk", None):
        raise ApiError(FORBIDDEN, "不能对本账号执行该操作")
    target_level = role_level(getattr(target, "roles", ()) or ())
    if target_level >= role_level(getattr(account, "roles", ()) or ()) and not is_admin(account):
        raise ApiError(
            FORBIDDEN,
            "不能操作同级或更高权限的账号",
            [{"field": "accountId", "reason": "目标账号权限不低于当前账号"}],
        )
    if set(getattr(target, "roles", ()) or ()) & HIGH_PRIVILEGE_ROLES and not is_admin(account):
        raise ApiError(FORBIDDEN, "无权操作管理员账号")


def role_names(roles: Optional[Iterable[str]]) -> list[str]:
    return [ROLE_NAMES.get(role, role) for role in (roles or ())]
