"""accounts 业务逻辑：认证、账号、组织、字典、审计日志。

视图层只做参数解析与响应封装，权限与范围校验在 common.permissions /
common.scopes 中完成，本模块负责第四层（对象归属与状态）。
"""

from __future__ import annotations

import csv
import io
import secrets
from datetime import timedelta
from typing import Any, Optional

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
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
    BATCH_LIMIT_EXCEEDED,
    BUSINESS_VALIDATION_FAILED,
    DATA_SCOPE_DENIED,
    DUPLICATE_RESOURCE,
    FILE_TOO_LARGE,
    INVALID_ARGUMENT,
    NOT_FOUND,
    STATE_CONFLICT,
    UNAUTHENTICATED,
    FORBIDDEN,
    UNSUPPORTED_FILE_TYPE,
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
    is_global_admin,
    org_scope_ids,
    visible_school_codes,
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
    if invitation.person_id and Account.objects.filter(person_id=invitation.person_id).exists():
        raise ApiError(
            DUPLICATE_RESOURCE,
            "该人员已绑定其他账号",
            [{"field": "personId", "reason": "一个人员只能绑定一个账号"}],
        )

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


_INVITATION_ORG_TYPES = ("SCHOOL", "UNIVERSITY", "TEAM")


def _ensure_invitation_org(
    org: Organization, person=None, field: str = "orgId"
) -> None:
    """邀请只能挂学校/球队；带人员时校验人员学校与组织一致。"""
    if org.org_type not in _INVITATION_ORG_TYPES:
        raise ApiError(
            INVALID_ARGUMENT,
            "邀请只能挂学校或球队组织",
            [{"field": field, "reason": "不可挂省/市州节点"}],
        )
    if org.org_type in ("SCHOOL", "UNIVERSITY"):
        if person is None:
            return
        person_school = person.school.code if person.school_id else ""
        if person_school != org.code:
            raise ApiError(
                BUSINESS_VALIDATION_FAILED,
                "人员所属学校与邀请组织不一致",
                [{"field": field, "reason": "请选择人员所属学校"}],
            )
        return
    from apps.teams.models import Team

    team = Team.objects.filter(team_no=org.code, is_deleted=False).first()
    if team is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "球队组织不存在",
            [{"field": field, "reason": f"未找到球队 {org.code}"}],
        )
    if person is None:
        return
    if not person.school_id or team.school_id != person.school_id:
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "人员所属学校与球队不一致",
            [{"field": field, "reason": "人员不属于该球队所属学校"}],
        )


def create_invitation(actor: Account, data: dict) -> dict[str, Any]:
    kind = data["kind"]
    if kind not in INVITE_KINDS:
        raise ApiError(INVALID_ARGUMENT, "邀请凭证只允许运动员/教练业务身份")
    org = Organization.objects.filter(pk=data["orgId"]).first()
    if org is None:
        raise ApiError(
            NOT_FOUND,
            "组织不存在",
            [{"field": "orgId", "reason": "不存在或已删除"}],
        )
    ensure_org_in_scope(actor, org.pk)
    person = None
    person_id = data.get("personId") or ""
    if person_id:
        from apps.persons.models import Person

        try:
            person = Person.objects.filter(pk=person_id, is_deleted=False).first()
        except (TypeError, ValueError):
            # personId 允许是尚未落库的占位值；此时只校验组织类型
            person = None
    _ensure_invitation_org(org, person)
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


# ---------------------------------------------------------------- 批量邀请
# 5.txt 账号批量发放：按学校/队伍 Excel 导入名单，一次签发邀请并导出 CSV。
# 方案要点：
# - 只给"已有人员主档"发邀请（身份证号匹配 id_card_hash），不凭姓名/证件认领；
# - 组织编码逐行解析并做数据范围校验（ADMIN 全局、CITY/UNIVERSITY 子树）；
# - 明文凭证只在本次响应/CSV 中出现一次（库中仅存哈希）。

BATCH_MAX_ROWS = 500
BATCH_MAX_BYTES = 5 * 1024 * 1024
BATCH_EXPIRES_DEFAULT = 1440

# Excel 表头 → 内部字段
_BATCH_HEADERS: dict[str, str] = {
    "姓名": "realName",
    "身份证号": "idNumber",
    "手机号": "phone",
    "角色": "kind",
    "组织编码": "orgCode",
    "人员ID": "personId",
}
_BATCH_REQUIRED: tuple[str, ...] = ("realName", "idNumber", "kind", "orgCode")


def _row_text(value) -> str:
    return "" if value is None else str(value).strip()


def parse_invitation_workbook(upload) -> list[dict]:
    """解析批量邀请 xlsx（第一行为表头），返回按行的原始字段。"""
    filename = (getattr(upload, "name", "") or "").lower()
    if not filename.endswith(".xlsx"):
        raise ApiError(
            UNSUPPORTED_FILE_TYPE,
            "批量发放仅支持 .xlsx",
            [{"field": "file", "reason": "需要 .xlsx"}],
        )
    raw = upload.read()
    if len(raw) > BATCH_MAX_BYTES:
        raise ApiError(
            FILE_TOO_LARGE,
            "文件超过 5MB 上限",
            [{"field": "file", "reason": "请拆分后分批导入"}],
        )
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheet = workbook.active
    except Exception as exc:  # noqa: BLE001 - 交由统一错误响应
        raise ApiError(
            INVALID_ARGUMENT,
            "无法解析该 xlsx 文件",
            [{"field": "file", "reason": "请使用模板（.xlsx）"}],
        ) from exc

    rows_iter = sheet.iter_rows(values_only=True)
    header_row = next(rows_iter, None)
    if not header_row:
        raise ApiError(INVALID_ARGUMENT, "名单为空", [{"field": "file", "reason": "缺少表头"}])
    positions: dict[str, int] = {}
    for index, header in enumerate(header_row):
        key = _BATCH_HEADERS.get(_row_text(header))
        if key and key not in positions:
            positions[key] = index
    missing = [name for name in _BATCH_REQUIRED if name not in positions]
    if missing:
        labels = {value: key for key, value in _BATCH_HEADERS.items()}
        raise ApiError(
            INVALID_ARGUMENT,
            "名单缺少必需列",
            [
                {"field": "file", "reason": f"缺少表头：{labels[name]}"}
                for name in missing
            ],
        )

    rows: list[dict] = []
    for offset, values in enumerate(rows_iter, start=2):
        if values is None or all(_row_text(v) == "" for v in values):
            continue
        row = {"rowNo": offset}
        for key, index in positions.items():
            row[key] = _row_text(values[index] if index < len(values) else "")
        rows.append(row)
    if not rows:
        raise ApiError(INVALID_ARGUMENT, "名单没有数据行", [{"field": "file", "reason": "仅表头"}])
    if len(rows) > BATCH_MAX_ROWS:
        raise ApiError(
            BATCH_LIMIT_EXCEEDED,
            f"单批最多 {BATCH_MAX_ROWS} 行",
            [{"field": "file", "reason": f"本次 {len(rows)} 行，请分批"}],
        )
    return rows


def _resolve_batch_org(org_code: str, fallback_org_id: Optional[str]) -> Organization:
    if org_code:
        org = Organization.objects.filter(code=org_code).first()
    elif fallback_org_id:
        org = Organization.objects.filter(pk=fallback_org_id).first()
    else:
        org = None
    if org is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "组织不存在",
            [{"field": "orgCode", "reason": f"未找到组织编码 {org_code or '-'}"}],
        )
    return org


def _match_person_by_id_number(id_number: str, real_name: str):
    from apps.persons.models import Person

    normalized = crypto.normalize_id_number(id_number)
    if len(normalized) != 18 or not normalized[:17].isdigit() or normalized[17] not in "0123456789X":
        raise ApiError(
            INVALID_ARGUMENT,
            "身份证号格式不正确",
            [{"field": "idNumber", "reason": "需 18 位"}],
        )
    person = Person.objects.filter(id_card_hash=crypto.lookup_hash(normalized)).first()
    if person is None or person.is_deleted:
        raise ApiError(
            INVALID_ARGUMENT,
            "未找到人员档案",
            [{"field": "idNumber", "reason": "请先在人员管理中建档后再发账号"}],
        )
    if person.full_name != real_name:
        raise ApiError(
            INVALID_ARGUMENT,
            "姓名与人员档案不一致",
            [{"field": "realName", "reason": "与身份证匹配到的档案姓名不符"}],
        )
    return person


def _issue_one_invitation(
    actor: Account, minutes: int, row: dict, fallback_org_id: Optional[str]
) -> dict:
    kind = _row_text(row.get("kind")).upper()
    if kind not in INVITE_KINDS:
        raise ApiError(
            INVALID_ARGUMENT,
            "角色只允许运动员/教练",
            [{"field": "kind", "reason": "仅 ATHLETE / COACH"}],
        )
    org = _resolve_batch_org(_row_text(row.get("orgCode")), fallback_org_id)
    ensure_org_in_scope(actor, org.pk)

    person_id = _row_text(row.get("personId"))
    if person_id:
        from apps.persons.models import Person

        person = Person.objects.filter(pk=person_id, is_deleted=False).first()
        if person is None:
            raise ApiError(
                INVALID_ARGUMENT,
                "人员档案不存在",
                [{"field": "personId", "reason": "不存在或已删除"}],
            )
        if person.full_name != _row_text(row.get("realName")):
            raise ApiError(
                INVALID_ARGUMENT,
                "姓名与人员档案不一致",
                [{"field": "realName", "reason": "与 personId 对应档案不符"}],
            )
    else:
        person = _match_person_by_id_number(
            _row_text(row.get("idNumber")), _row_text(row.get("realName"))
        )

    # 不得跨数据范围发放：非全局管理员只能给子树内学校的人员发账号
    school_codes = visible_school_codes(actor)
    if school_codes is not None:
        person_school = person.school.code if person.school_id else ""
        if person_school not in school_codes:
            raise ApiError(
                DATA_SCOPE_DENIED,
                "人员不在当前账号数据范围内",
                [{"field": "idNumber", "reason": "所属学校超出当前账号范围"}],
            )

    # 邀请只能挂学校/球队，且必须与人员所属学校一致
    _ensure_invitation_org(org, person, field="orgCode")

    if Account.objects.filter(person_id=person.pk).exists():
        raise ApiError(
            DUPLICATE_RESOURCE,
            "该人员已绑定账号",
            [{"field": "idNumber", "reason": "一个人员只能绑定一个账号"}],
        )
    if RegistrationInvitation.objects.filter(
        person_id=person.pk, used_at__isnull=True, expires_at__gt=timezone.now()
    ).exists():
        raise ApiError(
            DUPLICATE_RESOURCE,
            "该人员已有未使用的邀请",
            [{"field": "idNumber", "reason": "请使用已签发凭证或等待过期"}],
        )

    plain_token = secrets.token_hex(32)
    expires_at = timezone.now() + timedelta(minutes=minutes)
    RegistrationInvitation.objects.create(
        id=new_invitation_id(),
        token_hash=crypto.hash_token(plain_token),
        kind=kind,
        person_id=person.pk,
        organization_id=org.pk,
        expires_at=expires_at,
        created_by=actor.pk,
    )
    return {
        "rowNo": row["rowNo"],
        "realName": person.full_name,
        "maskedIdNumber": person.id_card_masked,
        "kind": kind,
        "orgCode": org.code,
        "registrationToken": plain_token,
        "expiresAt": expires_at.isoformat(),
    }


def create_invitation_batch(
    actor: Account,
    org_id: Optional[str],
    expires_in_minutes: Optional[int],
    upload,
) -> dict:
    """批量签发邀请：逐行独立事务，部分成功，返回成功行（含明文凭证一次）。"""
    rows = parse_invitation_workbook(upload)
    minutes = int(expires_in_minutes or BATCH_EXPIRES_DEFAULT)
    if not 5 <= minutes <= 10080:
        raise ApiError(
            INVALID_ARGUMENT,
            "有效期必须在 5—10080 分钟之间",
            [{"field": "expiresInMinutes", "reason": "5—10080"}],
        )

    # 同一份名单内按身份证号去重：一个人只能绑定一个账号
    seen_ids: dict[str, int] = {}
    duplicate_of: dict[int, int] = {}
    for row in rows:
        key = crypto.normalize_id_number(_row_text(row.get("idNumber"))) or _row_text(
            row.get("idNumber")
        )
        if key in seen_ids:
            duplicate_of[row["rowNo"]] = seen_ids[key]
        else:
            seen_ids[key] = row["rowNo"]

    results: list[dict] = []
    success_count = 0
    for row in rows:
        if row["rowNo"] in duplicate_of:
            first_row = duplicate_of[row["rowNo"]]
            results.append(
                {
                    "rowNo": row["rowNo"],
                    "realName": _row_text(row.get("realName")),
                    "kind": _row_text(row.get("kind")).upper(),
                    "orgCode": _row_text(row.get("orgCode")),
                    "status": "FAILED",
                    "error": f"名单内身份证号重复（与第 {first_row} 行相同）",
                    "code": DUPLICATE_RESOURCE,
                    "fieldErrors": [
                        {"field": "idNumber", "reason": "名单内重复"}
                    ],
                }
            )
            continue
        try:
            with transaction.atomic():
                item = _issue_one_invitation(actor, minutes, row, org_id)
            item["status"] = "OK"
            success_count += 1
        except ApiError as exc:
            item = {
                "rowNo": row["rowNo"],
                "realName": _row_text(row.get("realName")),
                "kind": _row_text(row.get("kind")).upper(),
                "orgCode": _row_text(row.get("orgCode")),
                "status": "FAILED",
                "error": exc.message,
                "code": exc.code,
                "fieldErrors": exc.data or [],
            }
        results.append(item)
    return {
        "total": len(results),
        "successCount": success_count,
        "failureCount": len(results) - success_count,
        "rows": results,
    }


def invitation_batch_csv(result: dict) -> tuple[bytes, str]:
    """批量邀请结果导出 CSV（含明文凭证；失败行含原因）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["序号", "姓名", "证件号", "角色", "组织编码", "状态", "邀请凭证", "过期时间", "失败原因"]
    )
    for row in result.get("rows", []):
        writer.writerow(
            [
                row.get("rowNo"),
                row.get("realName"),
                row.get("maskedIdNumber", ""),
                row.get("kind"),
                row.get("orgCode", ""),
                row.get("status"),
                row.get("registrationToken", ""),
                row.get("expiresAt", ""),
                row.get("error", ""),
            ]
        )
    filename = f"invitations-{timezone.now():%Y%m%d%H%M%S}.csv"
    # UTF-8 BOM：Excel 双击打开不乱码
    return ("\ufeff" + buffer.getvalue()).encode("utf-8"), filename


# ---------------------------------------------------------------- 账号

def scoped_accounts(actor: Account):
    if is_global_admin(actor):
        return Account.objects.all()
    scope = org_scope_ids(actor)
    if not scope:
        return Account.objects.none()
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


ROLE_ORG_TYPES: dict[str, set[str]] = {
    "TEAM": {"TEAM"},
    # 学校节点在组织树里可能是 SCHOOL（种子数据）或 UNIVERSITY（测试/部分导入）
    "UNIVERSITY": {"UNIVERSITY", "SCHOOL"},
    "CITY": {"CITY"},
    "PROVINCE_REVIEWER": {"PROVINCE"},
}


def _validate_account_refs(
    org_id: str,
    person_id: Optional[str],
    roles: list[str],
    exclude_account_id: Optional[str] = None,
) -> Organization:
    organization = Organization.objects.filter(pk=org_id).first()
    if organization is None:
        raise ApiError(INVALID_ARGUMENT, "组织不存在", [{"field": "orgId", "reason": "不存在"}])
    allowed: Optional[set[str]] = None
    for role in roles or []:
        role_types = ROLE_ORG_TYPES.get(role)
        if role_types is None:
            continue
        if allowed is None:
            allowed = set(role_types)
        else:
            allowed &= role_types
            if not allowed:
                raise ApiError(
                    INVALID_ARGUMENT,
                    "一个账号只能绑定一个组织节点，多角色请分设账号",
                    [{"field": "roles", "reason": "角色要求的组织类型互相冲突"}],
                )
    if allowed and organization.org_type not in allowed:
        raise ApiError(
            INVALID_ARGUMENT,
            f"该角色必须绑定 {'/'.join(sorted(allowed))} 类型的组织节点",
            [{"field": "orgId", "reason": f"当前为 {organization.org_type}"}],
        )
    if person_id:
        duplicated = Account.objects.filter(person_id=person_id)
        if exclude_account_id:
            duplicated = duplicated.exclude(pk=exclude_account_id)
        if duplicated.exists():
            raise ApiError(
                DUPLICATE_RESOURCE,
                "该人员已绑定其他账号",
                [{"field": "personId", "reason": "一个人员只能绑定一个账号"}],
            )
    return organization


def create_account(actor: Account, data: dict) -> Account:
    ensure_grantable_roles(actor, data["roles"], data.get("orgId"))
    organization = _validate_account_refs(
        data["orgId"], data.get("personId"), list(data["roles"])
    )
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
    account.organization = _validate_account_refs(
        data["orgId"], data.get("personId"), list(data["roles"]), exclude_account_id=account.pk
    )
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
    if is_global_admin(actor):
        return Organization.objects.all()
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


# 上级组织类型矩阵：省级为根，高校可直挂省（省属高校），其余严格逐级
ORG_PARENT_TYPES: dict[str, set[str]] = {
    "PROVINCE": set(),
    "CITY": {"PROVINCE"},
    "UNIVERSITY": {"PROVINCE", "CITY"},
    "SCHOOL": {"CITY"},
    "TEAM": {"SCHOOL", "UNIVERSITY"},
}

# 业务主档类型 → 组织节点类型（成员4 的 School/Team 通过 code 映射到组织树）
BIZ_TO_ORG_TYPE: dict[str, str] = {
    "SCHOOL": "SCHOOL",
    "UNIVERSITY": "UNIVERSITY",
    "TEAM": "TEAM",
}


def _org_type_error(org_type: str) -> ApiError:
    return ApiError(
        INVALID_ARGUMENT,
        "组织类型不合法",
        [{"field": "orgType", "reason": f"不支持的类型：{org_type}"}],
    )


def _validate_org_parent(
    org_type: str, parent: Optional[Organization], current_id: str = ""
) -> None:
    """校验层级矩阵、父节点启停与祖先成环（创建与修改共用）。"""
    allowed = ORG_PARENT_TYPES.get(org_type)
    if allowed is None:
        raise _org_type_error(org_type)
    if org_type == "PROVINCE":
        if parent is not None:
            raise ApiError(
                INVALID_ARGUMENT,
                "省级组织不能挂上级",
                [{"field": "parentId", "reason": "省级为根节点"}],
            )
        return
    if parent is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "缺少上级组织",
            [{"field": "parentId", "reason": f"{org_type} 必须挂在 {'/'.join(sorted(allowed))} 下"}],
        )
    if parent.org_type not in allowed:
        raise ApiError(
            INVALID_ARGUMENT,
            "上级组织类型不匹配",
            [{"field": "parentId", "reason": f"{org_type} 只能挂在 {'/'.join(sorted(allowed))} 下"}],
        )
    if not parent.enabled:
        raise ApiError(
            STATE_CONFLICT,
            "上级组织已停用",
            [{"field": "parentId", "reason": "不能挂在停用组织下"}],
        )
    if current_id:
        cursor, seen = parent, set()
        while cursor is not None and cursor.pk not in seen:
            if cursor.pk == current_id:
                raise ApiError(
                    INVALID_ARGUMENT,
                    "上级组织不能是自己的下级",
                    [{"field": "parentId", "reason": "会形成循环引用"}],
                )
            seen.add(cursor.pk)
            cursor = cursor.parent


def _apply_org_fields(
    organization: Organization,
    *,
    parent: Optional[Organization],
    name: str,
    org_type: str,
    code: str,
    enabled: bool,
) -> Organization:
    organization.parent = parent
    organization.name = name
    organization.org_type = org_type
    organization.code = code
    organization.enabled = enabled
    organization.deleted_at = None
    bump(organization)
    organization.save()
    return organization


def create_organization(actor: Account, data: dict) -> Organization:
    parent = None
    if data.get("parentId"):
        parent = Organization.objects.filter(pk=data["parentId"]).first()
        if parent is None:
            raise ApiError(INVALID_ARGUMENT, "父组织不存在", [{"field": "parentId", "reason": "不存在"}])
        ensure_org_in_scope(actor, parent.pk)
    else:
        ensure_org_in_scope(actor, None)
    _validate_org_parent(data["orgType"], parent)

    existing = Organization.all_objects.filter(code=data["code"]).first()
    if existing is not None:
        if existing.deleted_at is None:
            raise ApiError(DUPLICATE_RESOURCE, "组织编码已存在", [{"field": "code", "reason": "已被占用"}])
        # 软删后同 code 重建：复活原节点，保留历史 ID 与子节点关系
        return _apply_org_fields(
            existing,
            parent=parent,
            name=data["name"],
            org_type=data["orgType"],
            code=data["code"],
            enabled=bool(data.get("enabled", True)),
        )
    try:
        return Organization.objects.create(
            id=new_organization_id(),
            parent=parent,
            name=data["name"],
            org_type=data["orgType"],
            code=data["code"],
            enabled=bool(data.get("enabled", True)),
        )
    except IntegrityError:
        # 并发下唯一约束兜底，避免返回未格式化 500
        raise ApiError(DUPLICATE_RESOURCE, "组织编码已存在", [{"field": "code", "reason": "已被占用"}])


def get_organization(actor: Account, org_id: str) -> Organization:
    organization = scoped_organizations(actor).filter(pk=org_id).first()
    if organization is None:
        raise ApiError(NOT_FOUND)
    return organization


def update_organization(actor: Account, org_id: str, data: dict) -> Organization:
    organization = get_organization(actor, org_id)
    check_version(organization, data.get("version"))
    if data["code"] != organization.code:
        raise ApiError(
            INVALID_ARGUMENT,
            "组织编码不可修改",
            [{"field": "code", "reason": "业务主档按 code 映射组织，编码一期不可变"}],
        )
    parent = None
    if data.get("parentId"):
        if data["parentId"] == org_id:
            raise ApiError(INVALID_ARGUMENT, "父组织不能是自身", [{"field": "parentId", "reason": "自引用"}])
        parent = Organization.objects.filter(pk=data["parentId"]).first()
        if parent is None:
            raise ApiError(INVALID_ARGUMENT, "父组织不存在", [{"field": "parentId", "reason": "不存在"}])
        ensure_org_in_scope(actor, parent.pk)
    else:
        ensure_org_in_scope(actor, None)
    _validate_org_parent(data["orgType"], parent, current_id=org_id)

    enabled = bool(data.get("enabled", True))
    if organization.enabled and not enabled:
        if Organization.objects.filter(parent_id=org_id).exists():
            raise ApiError(
                STATE_CONFLICT,
                "存在启用中的下级组织，不能停用",
                [{"field": "enabled", "reason": "请先迁移或停用下级组织"}],
            )

    return _apply_org_fields(
        organization,
        parent=parent,
        name=data["name"],
        org_type=data["orgType"],
        code=organization.code,
        enabled=enabled,
    )


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


def ensure_business_org(
    biz_type: str,
    code: str,
    name: str,
    parent_org_id: Optional[str] = None,
) -> Organization:
    """业务主档（School/University/Team）与组织节点的唯一同步入口。

    调用方必须处于自己的 transaction.atomic() 中：建业务主档与建组织同事务，
    任一步失败整体回滚。业务模块禁止直接创建/修改 Organization。
    """
    if not code:
        raise ApiError(INVALID_ARGUMENT, "业务编码为空，无法挂接组织", [{"field": "code", "reason": "必填"}])
    org_type = BIZ_TO_ORG_TYPE.get(biz_type)
    if org_type is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "不支持的业务类型",
            [{"field": "bizType", "reason": f"仅支持 {sorted(BIZ_TO_ORG_TYPE)}"}],
        )
    parent = None
    if parent_org_id:
        parent = Organization.objects.filter(pk=parent_org_id).first()
        if parent is None:
            raise ApiError(INVALID_ARGUMENT, "父组织不存在", [{"field": "parentOrgId", "reason": "不存在"}])
    _validate_org_parent(org_type, parent)

    existing = Organization.all_objects.filter(code=code).first()
    if existing is not None:
        if existing.deleted_at is None and existing.org_type != org_type:
            raise ApiError(
                STATE_CONFLICT,
                "组织编码已被其他类型占用",
                [{"field": "code", "reason": f"已存在 {existing.org_type} 节点"}],
            )
        same = (
            existing.deleted_at is None
            and existing.org_type == org_type
            and existing.parent_id == parent_org_id
            and existing.enabled
            and (not name or existing.name == name)
        )
        if same:
            return existing
        return _apply_org_fields(
            existing,
            parent=parent,
            name=name or existing.name,
            org_type=org_type,
            code=code,
            enabled=True,
        )
    try:
        return Organization.objects.create(
            id=new_organization_id(),
            parent=parent,
            name=name,
            org_type=org_type,
            code=code,
            enabled=True,
        )
    except IntegrityError:
        raise ApiError(DUPLICATE_RESOURCE, "组织编码已存在", [{"field": "code", "reason": "已被占用"}])


# ---------------------------------------------------------------- 字典

def list_dictionary_items(filters: dict):
    queryset = DictionaryItem.objects.all()
    dict_type = filters.get("dictType")
    if dict_type:
        queryset = queryset.filter(dict_type=dict_type)
    return queryset.order_by("dict_type", "sort_order", "id")


DICT_OPTIONS_TTL = 300  # 字典选项缓存 5 分钟；写操作立即失效


def _dict_options_key(dict_type: str) -> str:
    return f"dict:options:{dict_type}"


def dictionary_options(dict_type: str):
    """下拉选项：带缓存。写字典时由 _invalidate_dict_options 立即失效。"""
    key = _dict_options_key(dict_type)
    cached = cache.get(key)
    if cached is not None:
        return cached
    items = list(
        DictionaryItem.objects.filter(dict_type=dict_type, enabled=True).order_by(
            "sort_order", "id"
        )
    )
    cache.set(key, items, DICT_OPTIONS_TTL)
    return items


def _invalidate_dict_options(*dict_types: str) -> None:
    for dict_type in dict_types:
        if dict_type:
            cache.delete(_dict_options_key(dict_type))


def create_dictionary_item(data: dict) -> DictionaryItem:
    if DictionaryItem.objects.filter(
        dict_type=data["dictType"], value=data["value"]
    ).exists():
        raise ApiError(DUPLICATE_RESOURCE, "同一字典类型下 value 已存在")
    item = DictionaryItem.objects.create(
        id=new_dictionary_id(),
        dict_type=data["dictType"],
        value=data["value"],
        label=data["label"],
        sort_order=int(data.get("sortOrder") or 0),
        enabled=bool(data.get("enabled", True)),
    )
    _invalidate_dict_options(item.dict_type)
    return item


def get_dictionary_item(item_id: str) -> DictionaryItem:
    item = DictionaryItem.objects.filter(pk=item_id).first()
    if item is None:
        raise ApiError(NOT_FOUND)
    return item


def update_dictionary_item(item_id: str, data: dict) -> DictionaryItem:
    item = get_dictionary_item(item_id)
    check_version(item, data.get("version"))
    old_dict_type = item.dict_type
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
    _invalidate_dict_options(old_dict_type, item.dict_type)
    return item


def delete_dictionary_item(item_id: str, version: Optional[int]) -> DictionaryItem:
    item = get_dictionary_item(item_id)
    check_version(item, version)
    item.deleted_at = timezone.now()
    bump(item)
    item.save()
    _invalidate_dict_options(item.dict_type)
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
    if filters.get("outcome"):
        queryset = queryset.filter(outcome=filters["outcome"])
    object_type = filters.get("objectType")
    if object_type:
        queryset = queryset.filter(object_type=object_type)
    object_id = filters.get("objectId")
    if object_id:
        queryset = queryset.filter(object_id=object_id)
    operator_account_id = filters.get("operatorAccountId")
    if operator_account_id:
        queryset = queryset.filter(operator_account_id=operator_account_id)
    keyword = filters.get("keyword")
    if keyword:
        # 关键词覆盖摘要/动作/对象与操作人账号名，避免只能按 ID 精确检索。
        matched_accounts = Account.objects.filter(
            Q(username__icontains=keyword) | Q(display_name__icontains=keyword)
        ).values_list("id", flat=True)
        queryset = queryset.filter(
            Q(summary__icontains=keyword)
            | Q(action__icontains=keyword)
            | Q(object_type__icontains=keyword)
            | Q(object_id__icontains=keyword)
            | Q(operator_account_id__in=matched_accounts)
        )
    if filters.get("from"):
        queryset = queryset.filter(created_at__gte=filters["from"])
    if filters.get("to"):
        queryset = queryset.filter(created_at__lte=filters["to"])
    return queryset.order_by("-created_at", "id")
