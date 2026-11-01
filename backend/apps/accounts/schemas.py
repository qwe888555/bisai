"""accounts 请求模型与响应序列化（django-ninja / pydantic v2）。

请求模型继承 StrictSchema（多传字段直接 400）；响应字段统一由 EnvelopeOut 包裹，
字段名严格沿用原契约 camelCase，禁止改名、禁止加字段。模型对象 → 响应 dict 的
转换集中在本模块，等价原 DRF serializer 的 to_representation。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictSchema

# 与 models.Organization.ORG_TYPES / permissions.ALL_ROLES 保持一致
OrgType = Literal["PROVINCE", "CITY", "SCHOOL", "UNIVERSITY", "TEAM"]
Role = Literal[
    "TEAM",
    "UNIVERSITY",
    "CITY",
    "PROVINCE_REVIEWER",
    "ATHLETE",
    "COACH",
    "ADMIN",
    "MATCH_RECORDER",
]
InviteKind = Literal["ATHLETE", "COACH"]


# ---------------------------------------------------------------- 请求

class LoginRequest(StrictSchema):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class RegisterRequest(StrictSchema):
    registrationToken: str = Field(min_length=16, max_length=512)
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    realName: str = Field(min_length=1, max_length=100)
    idNumber: str = Field(pattern=r"^\d{17}[\dXx]$")
    phone: str = Field(default="", pattern=r"^1\d{10}$")


class ChangePassword(StrictSchema):
    oldPassword: str = Field(min_length=8, max_length=128)
    newPassword: str = Field(min_length=8, max_length=128)


class InviteRequest(StrictSchema):
    kind: InviteKind
    personId: str = Field(default="", min_length=1, max_length=64)
    orgId: str = Field(min_length=1, max_length=64)
    expiresInMinutes: int = Field(default=1440, ge=5, le=10080)


class InviteBatchIn(StrictSchema):
    """批量签发邀请（multipart，配合 Excel 文件）。"""

    orgId: str = Field(default="", max_length=64)
    expiresInMinutes: int = Field(default=1440, ge=5, le=10080)


class AccountCreate(StrictSchema):
    username: str = Field(min_length=3, max_length=64)
    displayName: str = Field(min_length=1, max_length=100)
    roles: list[Role] = Field(min_length=1)
    orgId: str = Field(min_length=1, max_length=64)
    teamIds: list[str] = Field(default_factory=list)
    personId: str = Field(default="", min_length=1, max_length=64)
    enabled: bool = True


class AccountUpdate(AccountCreate):
    version: int = Field(ge=0)


class ResetPassword(StrictSchema):
    version: int = Field(ge=0)
    newPassword: str = Field(min_length=8, max_length=128)
    forceChangeOnNextLogin: bool = False


class OrganizationCreate(StrictSchema):
    parentId: str = Field(default="", min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    orgType: OrgType
    code: str = Field(min_length=1, max_length=64)
    enabled: bool = True


class OrganizationUpdate(OrganizationCreate):
    version: int = Field(ge=0)


class DictionaryItemCreate(StrictSchema):
    dictType: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    sortOrder: int = Field(default=0, ge=0)
    enabled: bool = True


class DictionaryItemUpdate(DictionaryItemCreate):
    version: int = Field(ge=0)


# ---------------------------------------------------------------- 响应

def account_out(account) -> dict:
    """字段顺序对齐原 AccountSerializer。"""
    data = {
        "id": account.pk,
        "username": account.username,
        "displayName": account.display_name,
        "roles": list(account.roles or []),
        "orgId": account.organization_id or "",
        "teamIds": list(account.team_ids or []),
    }
    if account.person_id:
        data["personId"] = account.person_id
    data["enabled"] = bool(account.enabled)
    if account.last_login_at:
        data["lastLoginAt"] = account.last_login_at
    data["version"] = int(account.version or 0)
    data["createdAt"] = account.created_at
    data["updatedAt"] = account.updated_at
    return data


def organization_out(organization) -> dict:
    data = {"id": organization.pk}
    if organization.parent_id:
        data["parentId"] = organization.parent_id
    data["name"] = organization.name
    data["orgType"] = organization.org_type
    data["code"] = organization.code
    data["enabled"] = bool(organization.enabled)
    data["version"] = int(organization.version or 0)
    data["createdAt"] = organization.created_at
    data["updatedAt"] = organization.updated_at
    return data


def dictionary_item_out(item) -> dict:
    return {
        "id": item.pk,
        "dictType": item.dict_type,
        "value": item.value,
        "label": item.label,
        "sortOrder": int(item.sort_order or 0),
        "enabled": bool(item.enabled),
        "version": int(item.version or 0),
        "createdAt": item.created_at,
        "updatedAt": item.updated_at,
    }


def audit_log_out(log) -> dict:
    """空串/None 的可选字段不下发，顺序对齐原 AuditLogSerializer。"""
    data = {"id": log.pk}
    for key, value in (
        ("logType", log.log_type),
        ("operatorAccountId", log.operator_account_id),
        ("action", log.action),
        ("objectType", log.object_type),
        ("objectId", log.object_id),
        ("summary", log.summary),
        ("outcome", log.outcome),
    ):
        if value not in (None, ""):
            data[key] = value
    data["version"] = int(log.version or 0)
    if log.created_at not in (None, ""):
        data["createdAt"] = log.created_at
    if log.updated_at not in (None, ""):
        data["updatedAt"] = log.updated_at
    return data
