"""accounts 接口（django-ninja）。

路由与原 urls.py 逐字一致，仅把 <str:xx> 换成 {xx}；accounts/registration-invitations
必须声明在 accounts/{account_id} 之前，保持同一 router 内的匹配顺序。
"""

from __future__ import annotations

from typing import Optional

from django.utils.dateparse import parse_datetime
from ninja import Query, Router

from apps.common.authentication import JWTAuth
from apps.common.envelope import action_result, ok
from apps.common.errors import INVALID_ARGUMENT, ApiError
from apps.common.middleware import audit_state
from apps.common.pagination import paginate
from apps.common.permissions import (
    ADMIN,
    ALL_ROLES,
    PROVINCE_REVIEWER,
    menus_for,
    permissions_for,
    register_operation,
    require,
)
from apps.common.schemas import EnvelopeOut

from . import schemas as s
from . import services

router = Router(tags=["accounts"])
_auth = JWTAuth()

# 权限点登记：与本应用路由一一对应，导入期完成，漏登记 require() 会直接报错
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


def _bool_param(raw):
    """沿用原语意：1/true/yes/on 为真，其余非空为假，空为 None。"""
    if raw is None or raw == "":
        return None
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _require_version(version: Optional[int]) -> int:
    if version is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "缺少 version",
            [{"field": "version", "reason": "该操作必须提交乐观锁版本"}],
        )
    return version


# ---------------------------------------------------------------- 认证

@router.post("/auth/login", operation_id="login", auth=None, response=EnvelopeOut, summary="登录")
def login(request, body: s.LoginRequest):
    result = services.login(body.username, body.password)
    audit_state(
        request,
        "auth.login",
        log_type="LOGIN",
        object_type="Account",
        object_id=result["accountId"],
        summary=f"账号 {result['accountId']} 登录",
    )
    return ok(result)


@router.post("/auth/registrations", operation_id="register", auth=None, response=EnvelopeOut, summary="邀请注册建号")
def register(request, body: s.RegisterRequest):
    account = services.register(body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "auth.register",
        object_type="Account",
        object_id=account.pk,
        summary="邀请注册建号",
    )
    return action_result(True, account.pk, account.version)


@router.post("/auth/logout", operation_id="logout", auth=_auth, response=EnvelopeOut, summary="退出登录")
def logout(request):
    require("logout")
    services.logout({"sid": (getattr(request, "token_payload", {}) or {}).get("sid")})
    audit_state(request, "auth.logout", log_type="LOGIN", summary="退出登录")
    return action_result(True)


@router.get("/me", operation_id="currentUser", auth=_auth, response=EnvelopeOut, summary="当前账号信息")
def current_user(request):
    actor = require("currentUser")
    data = {
        "accountId": actor.pk,
        "username": actor.username,
        "displayName": actor.display_name,
        "roles": list(actor.roles or []),
        "orgId": actor.organization_id or "",
        "teamIds": list(actor.team_ids or []),
        "permissions": permissions_for(actor.roles),
        "menus": menus_for(actor.roles),
    }
    if actor.person_id:
        data["personId"] = actor.person_id
    return ok(data)


@router.post("/me/password", operation_id="changePassword", auth=_auth, response=EnvelopeOut, summary="修改本人密码")
def change_password(request, body: s.ChangePassword):
    actor = require("changePassword")
    services.change_password(actor, body.oldPassword, body.newPassword)
    audit_state(
        request,
        "account.changePassword",
        object_type="Account",
        object_id=actor.pk,
        summary="修改本人密码",
    )
    return action_result(True, actor.pk, actor.version)


# ---------------------------------------------------------------- 邀请

@router.post(
    "/accounts/registration-invitations",
    operation_id="createInvite",
    auth=_auth,
    response=EnvelopeOut,
    summary="签发注册邀请凭证",
)
def create_invite(request, body: s.InviteRequest):
    actor = require("createInvite")
    result = services.create_invitation(actor, body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "account.createInvite",
        object_type="RegistrationInvitation",
        summary="签发注册邀请凭证",
    )
    return ok(result)


# ---------------------------------------------------------------- 账号

@router.get("/accounts", operation_id="accountList", auth=_auth, response=EnvelopeOut, summary="账号列表")
def account_list(
    request,
    username: Optional[str] = None,
    role: Optional[str] = None,
    orgId: Optional[str] = None,
    enabled: Optional[str] = None,
):
    actor = require("accountList")
    queryset = services.list_accounts(
        actor,
        {
            "username": username,
            "role": role,
            "orgId": orgId,
            "enabled": _bool_param(enabled),
        },
    )
    return paginate(request, queryset, mapper=s.account_out)


@router.post("/accounts", operation_id="accountCreate", auth=_auth, response=EnvelopeOut, summary="创建账号")
def account_create(request, body: s.AccountCreate):
    actor = require("accountCreate")
    account = services.create_account(actor, body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "account.create",
        object_type="Account",
        object_id=account.pk,
        summary=f"创建账号 {account.username}",
    )
    return ok(s.account_out(account))


@router.get("/accounts/{account_id}", operation_id="accountGet", auth=_auth, response=EnvelopeOut, summary="账号详情")
def account_get(request, account_id: str):
    actor = require("accountGet")
    account = services.get_account(actor, account_id)
    return ok(s.account_out(account))


@router.put("/accounts/{account_id}", operation_id="accountUpdate", auth=_auth, response=EnvelopeOut, summary="修改账号")
def account_update(request, account_id: str, body: s.AccountUpdate):
    actor = require("accountUpdate")
    account = services.update_account(
        actor, account_id, body.model_dump(exclude_unset=True)
    )
    audit_state(
        request,
        "account.update",
        object_type="Account",
        object_id=account.pk,
        summary="修改账号",
    )
    return ok(s.account_out(account))


@router.delete("/accounts/{account_id}", operation_id="accountDelete", auth=_auth, response=EnvelopeOut, summary="删除账号")
def account_delete(request, account_id: str, version: Optional[int] = None):
    actor = require("accountDelete")
    account = services.delete_account(actor, account_id, _require_version(version))
    audit_state(
        request,
        "account.delete",
        object_type="Account",
        object_id=account.pk,
        summary="删除账号",
    )
    return action_result(True, account.pk, account.version)


@router.post(
    "/accounts/{account_id}/password-reset",
    operation_id="resetPassword",
    auth=_auth,
    response=EnvelopeOut,
    summary="重置账号密码",
)
def reset_password(request, account_id: str, body: s.ResetPassword):
    actor = require("resetPassword")
    account = services.reset_password(
        actor,
        account_id,
        body.version,
        body.newPassword,
        body.forceChangeOnNextLogin,
    )
    audit_state(
        request,
        "account.resetPassword",
        object_type="Account",
        object_id=account.pk,
        summary="重置账号密码",
    )
    return action_result(True, account.pk, account.version)


# ---------------------------------------------------------------- 组织

@router.get("/organizations", operation_id="organizationList", auth=_auth, response=EnvelopeOut, summary="组织列表")
def organization_list(
    request,
    parentId: Optional[str] = None,
    orgType: Optional[str] = None,
):
    actor = require("organizationList")
    queryset = services.list_organizations(
        actor, {"parentId": parentId, "orgType": orgType}
    )
    return paginate(request, queryset, mapper=s.organization_out)


@router.post("/organizations", operation_id="organizationCreate", auth=_auth, response=EnvelopeOut, summary="创建组织")
def organization_create(request, body: s.OrganizationCreate):
    actor = require("organizationCreate")
    organization = services.create_organization(
        actor, body.model_dump(exclude_unset=True)
    )
    audit_state(
        request,
        "organization.create",
        object_type="Organization",
        object_id=organization.pk,
        summary="创建组织",
    )
    return ok(s.organization_out(organization))


@router.get(
    "/organizations/{organization_id}",
    operation_id="organizationGet",
    auth=_auth,
    response=EnvelopeOut,
    summary="组织详情",
)
def organization_get(request, organization_id: str):
    actor = require("organizationGet")
    organization = services.get_organization(actor, organization_id)
    return ok(s.organization_out(organization))


@router.put(
    "/organizations/{organization_id}",
    operation_id="organizationUpdate",
    auth=_auth,
    response=EnvelopeOut,
    summary="修改组织",
)
def organization_update(request, organization_id: str, body: s.OrganizationUpdate):
    actor = require("organizationUpdate")
    organization = services.update_organization(
        actor, organization_id, body.model_dump(exclude_unset=True)
    )
    audit_state(
        request,
        "organization.update",
        object_type="Organization",
        object_id=organization.pk,
        summary="修改组织",
    )
    return ok(s.organization_out(organization))


@router.delete(
    "/organizations/{organization_id}",
    operation_id="organizationDelete",
    auth=_auth,
    response=EnvelopeOut,
    summary="删除组织",
)
def organization_delete(request, organization_id: str, version: Optional[int] = None):
    actor = require("organizationDelete")
    organization = services.delete_organization(
        actor, organization_id, _require_version(version)
    )
    audit_state(
        request,
        "organization.delete",
        object_type="Organization",
        object_id=organization.pk,
        summary="删除组织",
    )
    return action_result(True, organization.pk, organization.version)


# ---------------------------------------------------------------- 字典

@router.get("/dictionary-items", operation_id="dictionaryList", auth=_auth, response=EnvelopeOut, summary="字典项列表")
def dictionary_list(request, dictType: Optional[str] = None):
    actor = require("dictionaryList")
    queryset = services.list_dictionary_items({"dictType": dictType})
    return paginate(request, queryset, mapper=s.dictionary_item_out)


@router.post("/dictionary-items", operation_id="dictionaryCreate", auth=_auth, response=EnvelopeOut, summary="创建字典项")
def dictionary_create(request, body: s.DictionaryItemCreate):
    actor = require("dictionaryCreate")
    item = services.create_dictionary_item(body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "dictionary.create",
        object_type="DictionaryItem",
        object_id=item.pk,
        summary="创建字典项",
    )
    return ok(s.dictionary_item_out(item))


@router.get(
    "/dictionary-items/{dictionary_id}",
    operation_id="dictionaryGet",
    auth=_auth,
    response=EnvelopeOut,
    summary="字典项详情",
)
def dictionary_get(request, dictionary_id: str):
    actor = require("dictionaryGet")
    item = services.get_dictionary_item(dictionary_id)
    return ok(s.dictionary_item_out(item))


@router.put(
    "/dictionary-items/{dictionary_id}",
    operation_id="dictionaryUpdate",
    auth=_auth,
    response=EnvelopeOut,
    summary="修改字典项",
)
def dictionary_update(request, dictionary_id: str, body: s.DictionaryItemUpdate):
    actor = require("dictionaryUpdate")
    item = services.update_dictionary_item(
        dictionary_id, body.model_dump(exclude_unset=True)
    )
    audit_state(
        request,
        "dictionary.update",
        object_type="DictionaryItem",
        object_id=item.pk,
        summary="修改字典项",
    )
    return ok(s.dictionary_item_out(item))


@router.delete(
    "/dictionary-items/{dictionary_id}",
    operation_id="dictionaryDelete",
    auth=_auth,
    response=EnvelopeOut,
    summary="删除字典项",
)
def dictionary_delete(request, dictionary_id: str, version: Optional[int] = None):
    actor = require("dictionaryDelete")
    item = services.delete_dictionary_item(dictionary_id, _require_version(version))
    audit_state(
        request,
        "dictionary.delete",
        object_type="DictionaryItem",
        object_id=item.pk,
        summary="删除字典项",
    )
    return action_result(True, item.pk, item.version)


@router.get(
    "/dictionaries/{dict_type}/options",
    operation_id="dictionaryOptions",
    auth=_auth,
    response=EnvelopeOut,
    summary="字典选项",
)
def dictionary_options(request, dict_type: str):
    actor = require("dictionaryOptions")
    items = services.dictionary_options(dict_type)
    return paginate(request, items, mapper=s.dictionary_item_out)


# ---------------------------------------------------------------- 审计日志

@router.get("/audit-logs", operation_id="auditList", auth=_auth, response=EnvelopeOut, summary="审计日志列表")
def audit_list(
    request,
    logType: Optional[str] = None,
    keyword: Optional[str] = None,
    outcome: Optional[str] = None,
    objectType: Optional[str] = None,
    objectId: Optional[str] = None,
    operatorAccountId: Optional[str] = None,
    time_from: Optional[str] = Query(None, alias="from"),
    time_to: Optional[str] = Query(None, alias="to"),
):
    actor = require("auditList")
    parsed = {}
    for key, raw in (("from", time_from), ("to", time_to)):
        if raw:
            value = parse_datetime(raw)
            if value is None:
                raise ApiError(
                    INVALID_ARGUMENT,
                    f"{key} 必须是带时区的 ISO 8601 时间",
                    [{"field": key, "reason": "格式不合法"}],
                )
            parsed[key] = value
    queryset = services.list_audit_logs(
        actor,
        {
            "logType": logType,
            "keyword": keyword,
            "outcome": outcome,
            "objectType": objectType,
            "objectId": objectId,
            "operatorAccountId": operatorAccountId,
            **parsed,
        },
    )
    return paginate(request, queryset, mapper=s.audit_log_out)
