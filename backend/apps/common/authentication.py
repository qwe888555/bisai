"""JWT 鉴权与密码哈希（django-ninja 版）。

- 令牌：HS256，payload 含 sub(账号)、sid(令牌ID)、iat、exp；
- 撤销：按令牌 sid 精确撤销（退出）；按账号时间戳整体撤销（改密/重置）；
- 密码：复用 django.contrib.auth.hashers，不引入 passlib；
- 刻意不装 django.contrib.auth：账号模型是自研的 Account，未认证固定为 None。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Optional, Tuple

import jwt
from django.contrib.auth.hashers import check_password as django_check_password
from django.contrib.auth.hashers import make_password as django_make_password
from django.core.cache import cache
from ninja.security import HttpBearer

from . import conf
from .context import set_current_account
from .errors import (
    FORBIDDEN,
    TOKEN_EXPIRED,
    TOKEN_REVOKED,
    UNAUTHENTICATED,
    ApiError,
)

_REVOKED_TOKEN_KEY = "auth:revoked:sid:{sid}"
_ACCOUNT_REVOKED_AT_KEY = "auth:revoked:account:{account_id}"


def make_password(raw: str) -> str:
    return django_make_password(raw)


def check_password(raw: str, encoded: str) -> bool:
    if not encoded:
        return False
    return django_check_password(raw, encoded)


def issue_access_token(account) -> Tuple[str, int]:
    """签发访问令牌，返回 (token, 有效秒数)。"""
    ttl = max(int(conf.ACCESS_TOKEN_TTL_MINUTES), 1)
    now = datetime.now(dt_timezone.utc)
    payload = {
        "sub": account.pk,
        "sid": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()),
    }
    token = jwt.encode(payload, conf.JWT_SECRET, algorithm=conf.JWT_ALGORITHM)
    return token, ttl * 60


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token, conf.JWT_SECRET, algorithms=[conf.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError as exc:
        raise ApiError(TOKEN_EXPIRED) from exc
    except jwt.PyJWTError as exc:
        raise ApiError(UNAUTHENTICATED) from exc
    if not payload.get("sub") or not payload.get("sid"):
        raise ApiError(UNAUTHENTICATED)
    return payload


def revoke_token(payload: dict) -> None:
    """退出登录：只撤销当前令牌。"""
    sid = payload.get("sid")
    if not sid:
        return
    remaining = max(int(payload.get("exp", 0)) - int(datetime.now(dt_timezone.utc).timestamp()), 1)
    cache.set(_REVOKED_TOKEN_KEY.format(sid=sid), 1, timeout=remaining)


def revoke_account_tokens(account_id: str) -> None:
    """改密/重置/停用：撤销该账号此前签发的全部令牌。"""
    now_ts = int(datetime.now(dt_timezone.utc).timestamp())
    ttl = max(int(conf.ACCESS_TOKEN_TTL_MINUTES), 1) * 60
    cache.set(
        _ACCOUNT_REVOKED_AT_KEY.format(account_id=account_id),
        now_ts,
        timeout=ttl + 60,
    )


def _ensure_token_active(payload: dict) -> None:
    if cache.get(_REVOKED_TOKEN_KEY.format(sid=payload["sid"])):
        raise ApiError(TOKEN_REVOKED)
    revoked_at = cache.get(_ACCOUNT_REVOKED_AT_KEY.format(account_id=payload["sub"]))
    if revoked_at and int(payload.get("iat", 0)) < int(revoked_at):
        raise ApiError(TOKEN_REVOKED)


def load_account(token: str):
    """解析令牌并取回已启用账号；失败直接抛契约错误。"""
    payload = decode_token(token)
    _ensure_token_active(payload)

    # 延迟导入，避免 config.settings 加载阶段的循环依赖
    from apps.accounts.models import Account

    account: Optional[object] = Account.objects.filter(pk=payload["sub"]).first()
    if account is None:
        raise ApiError(UNAUTHENTICATED)
    if not account.enabled:
        raise ApiError(FORBIDDEN, "账号已被停用")
    return account, payload


class JWTAuth(HttpBearer):
    """受保护接口的鉴权类：用法 `@router.get(..., auth=JWTAuth())`。

    重写 __call__：ninja 默认在缺头时返回 None，会吐 {"detail":"Unauthorized"}，
    不符合契约 2.1；这里统一改成抛 ApiError，由全局异常处理器输出契约信封。
    """

    def __call__(self, request):
        auth_value = request.headers.get(self.header)
        if not auth_value:
            raise ApiError(UNAUTHENTICATED)
        parts = auth_value.split(" ")
        if parts[0].lower() != self.openapi_scheme:
            raise ApiError(
                UNAUTHENTICATED, "Authorization 头格式应为 Bearer <token>"
            )
        return self.authenticate(request, " ".join(parts[1:]))

    def authenticate(self, request, token: str):
        account, payload = load_account(token)
        set_current_account(account)
        request.user = account  # 兼容审计中间件与调试
        request.auth = account
        request.token_payload = payload  # 退出登录需要 sid
        return account
