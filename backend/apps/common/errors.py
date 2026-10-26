"""统一错误码与异常处理（django-ninja 版）。

契约 2.1：失败必须返回对应 HTTP 状态 + ErrorResponse，禁止所有错误都 200。
错误码与 HTTP 状态的对应关系集中在这里，业务代码只写 ApiError(码, 说明)，
不要自己拼 status。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from ninja.errors import ValidationError as NinjaValidationError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 错误码表

# 成功
OK = "OK"
# 400
INVALID_ARGUMENT = "INVALID_ARGUMENT"
IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
# 401
UNAUTHENTICATED = "UNAUTHENTICATED"
TOKEN_EXPIRED = "TOKEN_EXPIRED"
TOKEN_REVOKED = "TOKEN_REVOKED"
# 403
FORBIDDEN = "FORBIDDEN"
DATA_SCOPE_DENIED = "DATA_SCOPE_DENIED"
PROFILE_BINDING_REQUIRED = "PROFILE_BINDING_REQUIRED"
# 404
NOT_FOUND = "NOT_FOUND"
# 405
METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
# 409
VERSION_CONFLICT = "VERSION_CONFLICT"
STATE_CONFLICT = "STATE_CONFLICT"
DUPLICATE_RESOURCE = "DUPLICATE_RESOURCE"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
RESULT_STALE = "RESULT_STALE"
RESOURCE_REFERENCED = "RESOURCE_REFERENCED"
COACH_REFERENCED = "COACH_REFERENCED"
# 413
BATCH_LIMIT_EXCEEDED = "BATCH_LIMIT_EXCEEDED"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
# 415
UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
# 422
BUSINESS_VALIDATION_FAILED = "BUSINESS_VALIDATION_FAILED"
REQUIRED_MATERIAL_MISSING = "REQUIRED_MATERIAL_MISSING"
RULE_NOT_CONFIRMED = "RULE_NOT_CONFIRMED"
# 429
RATE_LIMITED = "RATE_LIMITED"
# 500 / 503
INTERNAL_ERROR = "INTERNAL_ERROR"
DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"

CODE_HTTP_STATUS: dict[str, int] = {
    OK: 200,
    INVALID_ARGUMENT: 400,
    IDEMPOTENCY_KEY_REQUIRED: 400,
    UNAUTHENTICATED: 401,
    TOKEN_EXPIRED: 401,
    TOKEN_REVOKED: 401,
    FORBIDDEN: 403,
    DATA_SCOPE_DENIED: 403,
    PROFILE_BINDING_REQUIRED: 403,
    NOT_FOUND: 404,
    METHOD_NOT_ALLOWED: 405,
    VERSION_CONFLICT: 409,
    STATE_CONFLICT: 409,
    DUPLICATE_RESOURCE: 409,
    IDEMPOTENCY_CONFLICT: 409,
    RESULT_STALE: 409,
    RESOURCE_REFERENCED: 409,
    COACH_REFERENCED: 409,
    BATCH_LIMIT_EXCEEDED: 413,
    FILE_TOO_LARGE: 413,
    UNSUPPORTED_FILE_TYPE: 415,
    BUSINESS_VALIDATION_FAILED: 422,
    REQUIRED_MATERIAL_MISSING: 422,
    RULE_NOT_CONFIRMED: 422,
    RATE_LIMITED: 429,
    INTERNAL_ERROR: 500,
    DEPENDENCY_UNAVAILABLE: 503,
}

DEFAULT_MESSAGES: dict[str, str] = {
    INVALID_ARGUMENT: "请求参数不合法",
    IDEMPOTENCY_KEY_REQUIRED: "缺少或非法的 Idempotency-Key",
    UNAUTHENTICATED: "未登录或令牌无效",
    TOKEN_EXPIRED: "登录已过期，请重新登录",
    TOKEN_REVOKED: "令牌已失效，请重新登录",
    FORBIDDEN: "当前角色无权执行该操作",
    DATA_SCOPE_DENIED: "超出当前账号的数据范围",
    PROFILE_BINDING_REQUIRED: "需要先完成本人档案绑定",
    NOT_FOUND: "对象不存在或不可见",
    METHOD_NOT_ALLOWED: "请求方法不被支持",
    VERSION_CONFLICT: "数据已被其他操作更新，请刷新后重试",
    STATE_CONFLICT: "当前状态不允许该操作",
    DUPLICATE_RESOURCE: "资源已存在",
    IDEMPOTENCY_CONFLICT: "相同幂等键对应了不同的请求内容",
    RESULT_STALE: "结果已过期，请按最新汇总重新生成",
    RESOURCE_REFERENCED: "资源已被其他业务引用，暂不能删除",
    COACH_REFERENCED: "教练已被其他业务引用，暂不能删除",
    BATCH_LIMIT_EXCEEDED: "批量范围或总量超过上限",
    FILE_TOO_LARGE: "文件超过大小上限",
    UNSUPPORTED_FILE_TYPE: "不支持的文件类型",
    BUSINESS_VALIDATION_FAILED: "业务校验未通过",
    REQUIRED_MATERIAL_MISSING: "缺少必需材料",
    RULE_NOT_CONFIRMED: "规则未确认，无法生成正式结果",
    RATE_LIMITED: "请求过于频繁，请稍后重试",
    INTERNAL_ERROR: "服务内部错误",
    DEPENDENCY_UNAVAILABLE: "依赖服务暂不可用",
}


class ApiError(Exception):
    """业务异常：码 + 说明 + 可选字段级明细。"""

    def __init__(
        self,
        code: str,
        message: Optional[str] = None,
        data: Any = None,
        status: Optional[int] = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, "请求失败")
        self.data = data
        self.status = status or CODE_HTTP_STATUS.get(code, 400)
        super().__init__(f"{self.code}: {self.message}")


def error_envelope(
    code: str, message: Optional[str] = None, data: Any = None
) -> dict:
    from .envelope import Envelope

    return Envelope(code, message or DEFAULT_MESSAGES.get(code, "请求失败"), data).to_dict()


def field_errors(pairs: Iterable[tuple[str, str]]) -> list[dict]:
    return [{"field": field, "reason": reason} for field, reason in pairs]


def _ninja_validation_details(errors: Any) -> list[dict]:
    """把 ninja/pydantic 的校验错误拍平为 [{field, reason}]。"""
    out: list[dict] = []
    for item in errors or []:
        if not isinstance(item, dict):
            out.append({"field": "body", "reason": str(item)})
            continue
        loc = item.get("loc") or []
        field = ".".join(str(part) for part in loc if part not in ("body",)) or "body"
        out.append({"field": field, "reason": item.get("msg") or "参数不合法"})
    return out


def register_exception_handlers(api) -> None:
    """把所有异常统一转成契约信封，挂在 NinjaAPI 上。"""

    @api.exception_handler(ApiError)
    def _api_error(request, exc: ApiError):
        return api.create_response(
            request,
            error_envelope(exc.code, exc.message, exc.data),
            status=exc.status,
        )

    @api.exception_handler(NinjaValidationError)
    def _validation_error(request, exc: NinjaValidationError):
        return api.create_response(
            request,
            error_envelope(INVALID_ARGUMENT, None, _ninja_validation_details(exc.errors)),
            status=400,
        )

    @api.exception_handler(Http404)
    def _http404(request, exc: Http404):
        return api.create_response(request, error_envelope(NOT_FOUND), status=404)

    @api.exception_handler(ObjectDoesNotExist)
    def _object_missing(request, exc: ObjectDoesNotExist):
        return api.create_response(request, error_envelope(NOT_FOUND), status=404)

    @api.exception_handler(Exception)
    def _unhandled(request, exc: Exception):
        logger.exception("未处理异常", exc_info=exc)
        return api.create_response(
            request, error_envelope(INTERNAL_ERROR), status=500
        )
