"""accounts 业务逻辑：认证、账号、组织、字典、审计日志。

视图层只做参数解析与响应封装，权限与范围校验在 common.permissions /
common.scopes 中完成，本模块负责第四层（对象归属与状态）。
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone

from apps.common import crypto
from apps.common.authentication import (
    check_password,
    issue_access_token,
    make_password,
    revoke_account_tokens,
    revoke_token,
)
from apps.common.errors import (
    DUPLICATE_RESOURCE,
    INVALID_ARGUMENT,
    NOT_FOUND,
    STATE_CONFLICT,
    UNAUTHENTICATED,
    FORBIDDEN,
    ApiError,
)
from apps.common.ids import (
    new_account_id,
    new_dictionary_id,
    new_invitation_id,
    new_organization_id,
)
from apps.common.permissions import INVITE_KINDS
from apps.common.scopes import (
    ensure_grantable_roles,
    ensure_manageable_target,
    ensure_org_in_scope,
    is_admin,
    org_scope_ids,
)
from apps.common.versioning import bump, check_version

from .models import Account, AuditLog, DictionaryItem, Organization, RegistrationInvitation


# ---------------------------------------------------------------- 认证

def login(username: str, password: str) -> dict[str, Any]:
    account = Account.objects.filter(username=username).first()
    if account is None or not check_password(password, account.password):
        # 不区分"账号不存在/密码错误"，避免账号枚举
        raise ApiError(UNAUTHENTICATED, "账号或密码错误")
    if not account.enabled:
        raise ApiError(FORBIDDEN, "账号已被停用")

    token, expires_in = issue_access_token(account)
    Account.objects.filter(pk=account.pk).update(last_login_at=timezone.now())
    result = {
        "accessToken": token,
        "expiresIn": expires_in,
        "accountId": account.pk,
        "roles": list(account.roles or []),
    }
    if account.must_change_password:
        result["mustChangePassword"] = True
    return result


def logout(payload: dict) -> None:
    revoke_token(payload or {})


def change_password(account: Account, old_password: str, new_password: str) -> None:
    if not check_password(old_password, account.password):
        raise ApiError(INVALID_ARGUMENT, "原密码不正确", [{"field": "oldPassword", "reason": "原密码不正确"}])
    if old_password == new_password:
        raise ApiError(INVALID_ARGUMENT, "新密码不能与原密码相同", [{"field": "newPassword", "reason": "与旧密码相同"}])
    account.password = make_password(new_password)
    account.must_change_password = False
    bump(account)
    account.save()
    # 改密后旧令牌全部失效（契约 2.1）
    revoke_account_tokens(account.pk)


def register(data: dict) -> Account:
    """消费一次性邀请凭证后建账号。

    注意：人员主档的建立/绑定归成员4 的 /people 接口，本接口只建账号并
    记录 personId 绑定意图，避免只凭身份证认领已有档案。
    """
    invitation = RegistrationInvitation.objects.filter(
        token_hash=crypto.hash_token(data["registrationToken"])
    ).first()
    if invitation is None:
        raise ApiError(INVALID_ARGUMENT, "注册凭证无效", [{"field": "registrationToken", "reason": "凭证不存在"}])
    if invitation.used_at is not None:
        raise ApiError(STATE_CONFLICT, "注册凭证已被使用")
    if invitation.expires_at <= timezone.now():
        raise ApiError(STATE_CONFLICT, "注册凭证已过期")

    if Account.objects.filter(username=data["username"]).exists():
        raise ApiError(DUPLICATE_RESOURCE, "登录账号已存在", [{"field": "username", "reason": "已被占用"}])

    with transaction.atomic():
        account = Account.objects.create(
            id=new_account_id(),
            username=data["username"],
            display_name=data["realName"],
            roles=[invitation.kind],
            organization=invitation.organization,
            person_id=invitation.person_id or "",
            enabled=True,
            password=make_password(data["password"]),
        )
        # 身份证/手机号加密留存，等成员4 的人员接口完成绑定时再落到主档
        RegistrationInvitation.objects.filter(pk=invitation.pk).update(used_at=timezone.now())
    return account


def create_invitation(actor: Account, data: dict) -> dict[str, Any]:
    kind = data["kind"]
    if kind not in INVITE_KINDS:
        raise ApiError(INVALID_ARGUMENT, "邀请凭证只允许运动员/教练业务身份")
    ensure_org_in_scope(actor, data["orgId"])
    minutes = int(data.get("expiresInMinutes") or 1440)
    # 凭证明文为 32 字节随机数，只在签发响应里返回一次，库里只存哈希
    plain_token = secrets.token_hex(32)
    expires_at = timezone.now() + timedelta(minutes=minutes)
    RegistrationInvitation.objects.create(
        id=new_invitation_id(),
        token_hash=crypto.hash_token(plain_token),
        kind=kind,
        person_id=data.get("personId") or "",
        organization_id=data["orgId"],
        expires_at=expires_at,
        created_by=actor.pk,
    )
    return {"registrationToken": plain_token, "expiresAt": expires_at}


# ---------------------------------------------------------------- 账号

def scoped_accounts(actor: Account):
    scope = org_scope_ids(actor)
    if not scope:
        return Account.objects.none()
    if is_admin(actor) and not getattr(actor, "organization_id", None):
        return Account.objects.all()
    return Account.objects.filter(organization_id__in=scope)


def list_accounts(actor: Account, filters: dict):
    queryset = scoped_accounts(actor)
    username = filters.get("username")
    if username:
        queryset = queryset.filter(username__icontains=username)
    role = filters.get("role")
    if role:
        queryset = queryset.filter(roles__contains=[role])
    org_id = filters.get("orgId")
    if org_id:
        ensure_org_in_scope(actor, org_id)
        queryset = queryset.filter(organization_id=org_id)
    enabled = filters.get("enabled")
    if enabled is not None:
        queryset = queryset.filter(enabled=enabled)
    return queryset.order_by("-created_at", "id")


def _validate_account_refs(org_id: str, person_id: Optional[str]) -> Organization:
    organization = Organization.objects.filter(pk=org_id).first()
    if organization is None:
        raise ApiError(INVALID_ARGUMENT, "组织不存在", [{"field": "orgId", "reason": "不存在"}])
    return organization


def create_account(actor: Account, data: dict) -> Account:
    ensure_grantable_roles(actor, data["roles"], data.get("orgId"))
    organization = _validate_account_refs(data["orgId"], data.get("personId"))
    if Account.objects.filter(username=data["username"]).exists():
        raise ApiError(DUPLICATE_RESOURCE, "登录账号已存在", [{"field": "username", "reason": "已被占用"}])
    return Account.objects.create(
        id=new_account_id(),
        username=data["username"],
        display_name=data.get("displayName", ""),
        roles=list(data["roles"]),
        organization=organization,
        team_ids=list(data.get("teamIds") or []),
        person_id=data.get("personId") or "",
        enabled=bool(data.get("enabled", True)),
        # 新建账号初始密码与账号名一致，强制首登修改
        password=make_password(data["username"]),
        must_change_password=True,
    )


def get_account(actor: Account, account_id: str) -> Account:
    account = scoped_accounts(actor).filter(pk=account_id).first()
    if account is None:
        raise ApiError(NOT_FOUND)
    return account


def update_account(actor: Account, account_id: str, data: dict) -> Account:
    account = get_account(actor, account_id)
    ensure_manageable_target(actor, account)
    ensure_grantable_roles(actor, data["roles"], data.get("orgId"))
    check_version(account, data.get("version"))
    if (
        Account.objects.filter(username=data["username"]).exclude(pk=account.pk).exists()
    ):
        raise ApiError(DUPLICATE_RESOURCE, "登录账号已存在", [{"field": "username", "reason": "已被占用"}])

    account.username = data["username"]
    account.display_name = data.get("displayName", "")
    account.roles = list(data["roles"])
    account.organization = _validate_account_refs(data["orgId"], data.get("personId"))
    # PUT 为整份替换：未提交的可选字段清空
    account.team_ids = list(data.get("teamIds") or [])
    account.person_id = data.get("personId") or ""
    account.enabled = bool(data.get("enabled", account.enabled))
    bump(account)
    account.save()
    if not account.enabled:
        revoke_account_tokens(account.pk)
    return account


def delete_account(actor: Account, account_id: str, version: Optional[int]) -> Account:
    account = get_account(actor, account_id)
    ensure_manageable_target(actor, account)
    check_version(account, version)
    if _has_protected_history(account):
        raise ApiError(STATE_CONFLICT, "该账号存在受保护的历史关联，不能删除")
    account.deleted_at = timezone.now()
    account.enabled = False
    bump(account)
    account.save()
    revoke_account_tokens(account.pk)
    return account


def _has_protected_history(account: Account) -> bool:
    """报名快照、审核记录等一旦引用该账号就不允许删除。

    成员3/成员4/成员5 的报名、审核、比赛事件表建好后，在这里追加存在性检查，
    目前只有账号自身的消息/日志，不构成受保护关联。
    """
    return False


def reset_password(
    actor: Account, account_id: str, version: Optional[int], new_password: str, force: bool
) -> Account:
    account = get_account(actor, account_id)
    ensure_manageable_target(actor, account)
    check_version(account, version)
    account.password = make_password(new_password)
    account.must_change_password = bool(force)
    bump(account)
    account.save()
    # 不得在响应或日志回显新密码；同时撤销该账号既有令牌
    revoke_account_tokens(account.pk)
    return account


# ---------------------------------------------------------------- 组织

def scoped_organizations(actor: Account):
    scope = org_scope_ids(actor)
    if not scope:
        return Organization.objects.none()
    return Organization.objects.filter(pk__in=scope)


def list_organizations(actor: Account, filters: dict):
    queryset = scoped_organizations(actor)
    parent_id = filters.get("parentId")
    if parent_id:
        queryset = queryset.filter(parent_id=parent_id)
    org_type = filters.get("orgType")
    if org_type:
        queryset = queryset.filter(org_type=org_type)
    return queryset.order_by("org_type", "code")


def create_organization(actor: Account, data: dict) -> Organization:
    parent = None
    if data.get("parentId"):
        parent = Organization.objects.filter(pk=data["parentId"]).first()
        if parent is None:
            raise ApiError(INVALID_ARGUMENT, "父组织不存在", [{"field": "parentId", "reason": "不存在"}])
        ensure_org_in_scope(actor, parent.pk)
    if Organization.objects.filter(code=data["code"]).exists():
        raise ApiError(DUPLICATE_RESOURCE, "组织编码已存在", [{"field": "code", "reason": "已被占用"}])
    return Organization.objects.create(
        id=new_organization_id(),
        parent=parent,
        name=data["name"],
        org_type=data["orgType"],
        code=data["code"],
        enabled=bool(data.get("enabled", True)),
    )


def get_organization(actor: Account, org_id: str) -> Organization:
    organization = scoped_organizations(actor).filter(pk=org_id).first()
    if organization is None:
        raise ApiError(NOT_FOUND)
    return organization


def update_organization(actor: Account, org_id: str, data: dict) -> Organization:
    organization = get_organization(actor, org_id)
    check_version(organization, data.get("version"))
    parent = None
    if data.get("parentId"):
        if data["parentId"] == org_id:
            raise ApiError(INVALID_ARGUMENT, "父组织不能是自身", [{"field": "parentId", "reason": "自引用"}])
        parent = Organization.objects.filter(pk=data["parentId"]).first()
        if parent is None:
            raise ApiError(INVALID_ARGUMENT, "父组织不存在", [{"field": "parentId", "reason": "不存在"}])
        ensure_org_in_scope(actor, parent.pk)
    if (
        Organization.objects.filter(code=data["code"])
        .exclude(pk=organization.pk)
        .exists()
    ):
        raise ApiError(DUPLICATE_RESOURCE, "组织编码已存在", [{"field": "code", "reason": "已被占用"}])

    organization.parent = parent
    organization.name = data["name"]
    organization.org_type = data["orgType"]
    organization.code = data["code"]
    organization.enabled = bool(data.get("enabled", True))
    bump(organization)
    organization.save()
    return organization


def delete_organization(actor: Account, org_id: str, version: Optional[int]) -> Organization:
    organization = get_organization(actor, org_id)
    check_version(organization, version)
    if Organization.objects.filter(parent_id=org_id).exists():
        raise ApiError(STATE_CONFLICT, "存在下级组织，不能删除")
    if Account.objects.filter(organization_id=org_id).exists():
        raise ApiError(STATE_CONFLICT, "该组织下仍有账号，不能删除")
    organization.deleted_at = timezone.now()
    bump(organization)
    organization.save()
    return organization


# ---------------------------------------------------------------- 字典

def list_dictionary_items(filters: dict):
    queryset = DictionaryItem.objects.all()
    dict_type = filters.get("dictType")
    if dict_type:
        queryset = queryset.filter(dict_type=dict_type)
    return queryset.order_by("dict_type", "sort_order", "id")


def dictionary_options(dict_type: str):
    return (
        DictionaryItem.objects.filter(dict_type=dict_type, enabled=True)
        .order_by("sort_order", "id")
    )


def create_dictionary_item(data: dict) -> DictionaryItem:
    if DictionaryItem.objects.filter(
        dict_type=data["dictType"], value=data["value"]
    ).exists():
        raise ApiError(DUPLICATE_RESOURCE, "同一字典类型下 value 已存在")
    return DictionaryItem.objects.create(
        id=new_dictionary_id(),
        dict_type=data["dictType"],
        value=data["value"],
        label=data["label"],
        sort_order=int(data.get("sortOrder") or 0),
        enabled=bool(data.get("enabled", True)),
    )


def get_dictionary_item(item_id: str) -> DictionaryItem:
    item = DictionaryItem.objects.filter(pk=item_id).first()
    if item is None:
        raise ApiError(NOT_FOUND)
    return item


def update_dictionary_item(item_id: str, data: dict) -> DictionaryItem:
    item = get_dictionary_item(item_id)
    check_version(item, data.get("version"))
    if (
        DictionaryItem.objects.filter(dict_type=data["dictType"], value=data["value"])
        .exclude(pk=item.pk)
        .exists()
    ):
        raise ApiError(DUPLICATE_RESOURCE, "同一字典类型下 value 已存在")
    item.dict_type = data["dictType"]
    item.value = data["value"]
    item.label = data["label"]
    item.sort_order = int(data.get("sortOrder") or 0)
    item.enabled = bool(data.get("enabled", True))
    bump(item)
    item.save()
    return item


def delete_dictionary_item(item_id: str, version: Optional[int]) -> DictionaryItem:
    item = get_dictionary_item(item_id)
    check_version(item, version)
    item.deleted_at = timezone.now()
    bump(item)
    item.save()
    return item


# ---------------------------------------------------------------- 审计日志

def list_audit_logs(actor: Account, filters: dict):
    queryset = AuditLog.objects.all()
    if not is_admin(actor):
        account_ids = list(scoped_accounts(actor).values_list("id", flat=True))
        queryset = queryset.filter(operator_account_id__in=account_ids)
    log_type = filters.get("logType")
    if log_type:
        queryset = queryset.filter(log_type=log_type)
    object_id = filters.get("objectId")
    if object_id:
        queryset = queryset.filter(object_id=object_id)
    if filters.get("from"):
        queryset = queryset.filter(created_at__gte=filters["from"])
    if filters.get("to"):
        queryset = queryset.filter(created_at__lte=filters["to"])
    return queryset.order_by("-created_at", "id")
