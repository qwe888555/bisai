"""中间件与日志脱敏。

顺序固定为 requestId → CORS → 审计 → 限流（见 settings.MIDDLEWARE 注释）：
requestId 必须最外层，保证任何响应（含异常）都带上它。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from django.core.cache import cache
from django.http import HttpResponse

from . import conf
from .context import (
    REQUEST_ID_HEADER,
    get_current_account,
    get_request_id,
    set_current_account,
    set_request_id,
)
from .crypto import mask_id_number, mask_phone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 日志脱敏


class SensitiveLogFilter(logging.Filter):
    """日志里不得出现完整身份证、手机号、密码、令牌（契约 2 审计要求）。"""

    _id = re.compile(r"\d{17}[\dXx]")
    _phone = re.compile(r"\b1\d{10}\b")
    _secret = re.compile(
        r"(?i)\b(password|newPassword|oldPassword|accessToken|registrationToken|"
        r"token|secret|apiKey|authorization)\b\s*[=:]\s*(\"[^\"]*\"|'[^']*'|\S+)"
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            redacted = self._redact(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
        except Exception:  # noqa: BLE001 - 日志脱敏失败不能影响业务
            pass
        return True

    def _redact(self, text: str) -> str:
        text = self._id.sub(lambda m: mask_id_number(m.group()), text)
        text = self._phone.sub(lambda m: mask_phone(m.group()), text)
        text = self._secret.sub(lambda m: f"{m.group(1)}=***", text)
        return text


# ---------------------------------------------------------------- requestId


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = set_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.request_id = request_id
        set_current_account(None)
        response = self.get_response(request)
        response[REQUEST_ID_HEADER] = request_id
        return response


# ---------------------------------------------------------------- CORS


class CorsMiddleware:
    """最小 CORS 实现：不引入 django-cors-headers，只需覆盖白名单与预检。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get("Origin", "")
        if request.method == "OPTIONS" and request.headers.get(
            "Access-Control-Request-Method"
        ):
            response = HttpResponse(status=200)
            self._apply_headers(response, origin, preflight=True)
            return response

        response = self.get_response(request)
        self._apply_headers(response, origin, preflight=False)
        return response

    def _allowed(self, origin: str) -> bool:
        if not origin:
            return False
        return "*" in conf.CORS_ORIGINS or origin in conf.CORS_ORIGINS

    def _apply_headers(self, response, origin: str, preflight: bool) -> None:
        if not self._allowed(origin):
            return
        response["Access-Control-Allow-Origin"] = origin
        response["Vary"] = "Origin"
        response["Access-Control-Allow-Credentials"] = "true"
        response["Access-Control-Expose-Headers"] = f"{REQUEST_ID_HEADER}, Retry-After"
        if preflight:
            response["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Idempotency-Key, X-Request-Id"
            )
            response["Access-Control-Max-Age"] = "600"


# ---------------------------------------------------------------- 审计

AUDIT_LOG_TYPE = "audit_log_type"
AUDIT_ACTION = "audit_action"
AUDIT_OBJECT_TYPE = "audit_object_type"
AUDIT_OBJECT_ID = "audit_object_id"
AUDIT_SUMMARY = "audit_summary"


def audit_state(
    request,
    action: str,
    *,
    log_type: str = "OPERATION",
    object_type: Optional[str] = None,
    object_id: Optional[str] = None,
    summary: Optional[str] = None,
) -> None:
    """视图内声明要写的审计内容；中间件在响应后统一落库。"""
    setattr(request, AUDIT_ACTION, action)
    setattr(request, AUDIT_LOG_TYPE, log_type)
    setattr(request, AUDIT_OBJECT_TYPE, object_type)
    setattr(request, AUDIT_OBJECT_ID, object_id)
    setattr(request, AUDIT_SUMMARY, (summary or "")[:500])


class AuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        self._write(request, response)
        return response

    def _write(self, request, response) -> None:
        action = getattr(request, AUDIT_ACTION, None)
        if not action:
            return
        try:
            # 延迟导入：审计模型属于 accounts 应用
            from apps.accounts.models import AuditLog
            from .ids import new_id

            account = get_current_account() or getattr(request, "user", None)
            AuditLog.objects.create(
                id=new_id("log"),
                log_type=getattr(request, AUDIT_LOG_TYPE, "OPERATION"),
                operator_account_id=getattr(account, "pk", "") or "",
                action=action[:100],
                object_type=(getattr(request, AUDIT_OBJECT_TYPE, None) or "")[:50],
                object_id=(getattr(request, AUDIT_OBJECT_ID, None) or "")[:64],
                summary=getattr(request, AUDIT_SUMMARY, "") or "",
                outcome="SUCCESS" if response.status_code < 400 else "FAILURE",
            )
        except Exception:  # noqa: BLE001 - 审计失败只记日志，不破坏业务响应
            logger.warning("写入审计日志失败", exc_info=True)


# ---------------------------------------------------------------- 限流

_THROTTLE_PATHS: tuple[tuple[str, str], ...] = (
    ("/api/v1/auth/login", "login"),
    ("/api/v1/auth/registrations", "registration"),
    ("/api/v1/public/policy-questions", "policy_question"),
)


class RateLimitMiddleware:
    """按 IP 的粗粒度限流，与 DRF throttle 双保险。

    覆盖登录、注册、政策问答三类高风险入口；窗口内超限直接 429。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        scope = self._scope(request)
        if scope:
            limit, window = scope
            key = f"throttle:{request.path}:{self._client_ip(request)}"
            retry_after = self._exceeded(key, limit, window)
            if retry_after is not None:
                response = HttpResponse(
                    self._body(retry_after),
                    status=429,
                    content_type="application/json; charset=utf-8",
                )
                response["Retry-After"] = str(retry_after)
                return response
        return self.get_response(request)

    def _scope(self, request) -> Optional[tuple[int, int]]:
        if request.method not in {"POST", "PUT", "DELETE"}:
            return None
        for path, name in _THROTTLE_PATHS:
            if request.path.startswith(path):
                return self._parse(name)
        return None

    def _parse(self, name: str) -> Optional[tuple[int, int]]:
        from django.conf import settings as django_settings

        rates = getattr(django_settings, "THROTTLE_RATES", {})
        raw = rates.get(name, "")
        match = re.match(r"^(\d+)\s*/\s*(sec|min|hour|day)$", str(raw).strip())
        if not match:
            return None
        limit = int(match.group(1))
        window = {"sec": 1, "min": 60, "hour": 3600, "day": 86400}[match.group(2)]
        return limit, window

    def _client_ip(self, request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "unknown")

    def _exceeded(self, key: str, limit: int, window: int) -> Optional[int]:
        try:
            count = cache.get_or_set(key, 0, timeout=window)
            count = cache.incr(key)
        except Exception:  # noqa: BLE001 - 缓存不可用时不阻断业务
            return None
        if count > limit:
            return window
        return None

    def _body(self, retry_after: int) -> str:
        import json

        return json.dumps(
            {
                "code": "RATE_LIMITED",
                "message": "请求过于频繁，请稍后重试",
                "data": [{"field": "retryAfter", "reason": f"{retry_after} 秒后可重试"}],
                "requestId": get_request_id(),
            },
            ensure_ascii=False,
        )
