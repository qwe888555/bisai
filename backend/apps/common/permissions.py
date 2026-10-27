"""角色、权限点注册表与校验入口。

契约 3：每个非公开操作同时校验 角色 → 权限点 api:{operationId} → 数据范围
→ 对象归属/状态。本模块负责前两层，数据范围见 scopes.py。

约定：新增接口必须在自己的 api.py 导入期调用 register_operation("xxx", Roles...)；
视图首行调用 require("xxx") 完成校验并取回当前账号。漏登记会直接报错，
避免新接口绕过权限。
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from django.core.exceptions import ImproperlyConfigured

from .context import get_current_account
from .errors import FORBIDDEN, UNAUTHENTICATED, ApiError

# ---------------------------------------------------------------- 角色

TEAM = "TEAM"
UNIVERSITY = "UNIVERSITY"
CITY = "CITY"
PROVINCE_REVIEWER = "PROVINCE_REVIEWER"
ATHLETE = "ATHLETE"
COACH = "COACH"
ADMIN = "ADMIN"
MATCH_RECORDER = "MATCH_RECORDER"

ALL_ROLES: tuple[str, ...] = (
    TEAM,
    UNIVERSITY,
    CITY,
    PROVINCE_REVIEWER,
    ATHLETE,
    COACH,
    ADMIN,
    MATCH_RECORDER,
)

ROLE_NAMES: dict[str, str] = {
    TEAM: "中小学球队端",
    UNIVERSITY: "学校端",
    CITY: "市州端",
    PROVINCE_REVIEWER: "省级审核",
    ATHLETE: "运动员",
    COACH: "教练员",
    ADMIN: "管理员",
    MATCH_RECORDER: "赛事录入员",
}

# 高权限角色：只有本身具备 ADMIN 的账号才能授予或改动同等级账号
HIGH_PRIVILEGE_ROLES: frozenset[str] = frozenset({ADMIN})

# 具备组织树数据范围的角色（其余角色只限本人/本队）
ORG_SCOPE_ROLES: frozenset[str] = frozenset({ADMIN, PROVINCE_REVIEWER, CITY})

# 邀请凭证只允许 ATHLETE / COACH 业务身份
INVITE_KINDS: tuple[str, ...] = (ATHLETE, COACH)

# 需要 Idempotency-Key 的操作（契约 2.2）
IDEMPOTENT_OPERATIONS: frozenset[str] = frozenset(
    {"identityCreate", "submitEnrollment", "matchEventCreate", "selectionGenerate"}
)

# ---------------------------------------------------------------- 菜单

MENUS: list[dict] = [
    {
        "key": "account",
        "title": "账号管理",
        "path": "/admin/accounts",
        "permission": "api:accountList",
        "roles": [ADMIN, PROVINCE_REVIEWER],
    },
    {
        "key": "organization",
        "title": "组织管理",
        "path": "/admin/organizations",
        "permission": "api:organizationList",
        "roles": [ADMIN],
    },
    {
        "key": "dictionary",
        "title": "字典管理",
        "path": "/admin/dictionary-items",
        "permission": "api:dictionaryList",
        "roles": [ADMIN],
    },
    {
        "key": "audit",
        "title": "审计日志",
        "path": "/admin/audit-logs",
        "permission": "api:auditList",
        "roles": [ADMIN, PROVINCE_REVIEWER],
    },
    {
        "key": "identity",
        "title": "身份核验",
        "path": "/identity-checks",
        "permission": "api:identityList",
        "roles": [MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER],
    },
    {
        "key": "message",
        "title": "消息中心",
        "path": "/me/messages",
        "permission": "api:messageList",
        "roles": list(ALL_ROLES),
    },
]

# ---------------------------------------------------------------- 权限点

OPERATION_ROLES: dict[str, tuple[str, ...]] = {}


def register_operation(operation_id: str, roles: Sequence[str]) -> tuple[str, ...]:
    """登记权限点；重复登记以最新为准，角色必须合法。"""
    invalid = [role for role in roles if role not in ALL_ROLES]
    if invalid:
        raise ImproperlyConfigured(
            f"权限点 {operation_id} 含未知角色：{invalid}"
        )
    resolved = tuple(dict.fromkeys(roles))
    OPERATION_ROLES[operation_id] = resolved
    return resolved


def operation_roles(operation_id: str) -> tuple[str, ...]:
    return OPERATION_ROLES.get(operation_id, ())


# 成员2 负责的接口权限点（其余成员在自己模块里用 register_operation 追加）
register_operation("createInvite", (ADMIN,))
register_operation("logout", ALL_ROLES)
register_operation("currentUser", ALL_ROLES)
register_operation("changePassword", ALL_ROLES)

register_operation("accountList", (ADMIN, PROVINCE_REVIEWER))
register_operation("accountCreate", (ADMIN, PROVINCE_REVIEWER))
register_operation("accountGet", (ADMIN, PROVINCE_REVIEWER))
register_operation("accountUpdate", (ADMIN, PROVINCE_REVIEWER))
register_operation("accountDelete", (ADMIN, PROVINCE_REVIEWER))
register_operation("resetPassword", (ADMIN, PROVINCE_REVIEWER))

register_operation("organizationList", (ADMIN,))
register_operation("organizationCreate", (ADMIN,))
register_operation("organizationGet", (ADMIN,))
register_operation("organizationUpdate", (ADMIN,))
register_operation("organizationDelete", (ADMIN,))

register_operation("dictionaryList", (ADMIN,))
register_operation("dictionaryCreate", (ADMIN,))
register_operation("dictionaryGet", (ADMIN,))
register_operation("dictionaryUpdate", (ADMIN,))
register_operation("dictionaryDelete", (ADMIN,))
register_operation("dictionaryOptions", ALL_ROLES)

register_operation("auditList", (ADMIN, PROVINCE_REVIEWER))

register_operation("messageList", ALL_ROLES)
register_operation("messageGet", ALL_ROLES)
register_operation("messageRead", ALL_ROLES)

register_operation("fileUpload", ALL_ROLES)
register_operation("fileBatchUpload", ALL_ROLES)
register_operation("fileGet", ALL_ROLES)
register_operation("fileDownload", ALL_ROLES)
register_operation("fileDelete", ALL_ROLES)

register_operation("identityCreate", (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER))
register_operation("identityList", (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER))
register_operation("identityGet", (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER))
register_operation("identityReview", (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER))


def permissions_for(roles: Optional[Iterable[str]]) -> list[str]:
    """当前角色可用的权限点集合，供 /me 返回。"""
    role_set = set(roles or ())
    return sorted(
        f"api:{operation}"
        for operation, allowed in OPERATION_ROLES.items()
        if role_set & set(allowed)
    )


def menus_for(roles: Optional[Iterable[str]]) -> list[dict]:
    """按角色过滤菜单；对外不暴露 roles 字段。"""
    role_set = set(roles or ())

    def build(items: list[dict]) -> list[dict]:
        result: list[dict] = []
        for item in items:
            if not role_set & set(item.get("roles", ())):
                continue
            node = {key: value for key, value in item.items() if key != "roles"}
            children = build(item.get("children") or [])
            if children:
                node["children"] = children
            result.append(node)
        return result

    return build(MENUS)


# ---------------------------------------------------------------- 校验入口


def require(operation_id: str) -> Optional[object]:
    """视图首行调用：校验登录态 + 角色 + 权限点，返回当前账号。

    未登记的 operationId 视为开发错误，直接抛配置异常（不是 403），
    以免新接口悄悄绕过权限。
    """
    allowed = OPERATION_ROLES.get(operation_id)
    if allowed is None:
        raise ImproperlyConfigured(
            f"接口 {operation_id} 未登记权限点，请在 api.py 导入期 register_operation()"
        )

    account = get_current_account()
    if account is None:
        raise ApiError(UNAUTHENTICATED)
    if not getattr(account, "enabled", False):
        raise ApiError(FORBIDDEN, "账号已被停用")

    if not set(getattr(account, "roles", ()) or ()) & set(allowed):
        raise ApiError(
            FORBIDDEN,
            f"当前角色无权执行 {operation_id}",
            [
                {
                    "field": "role",
                    "reason": "需要以下角色之一：" + ",".join(allowed),
                }
            ],
        )
    return account


def current_account() -> Optional[object]:
    return get_current_account()
