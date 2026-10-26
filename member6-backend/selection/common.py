"""Small helpers shared by all selection-scoring API views and services."""

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger("selection")


class BusinessError(Exception):
    """An expected business error, converted to a JSON response by api_view.

    Signature is ``BusinessError(code, message)`` so call sites read like an
    HTTP error: ``raise BusinessError(409, "该球员本轮评分已存在")``.
    """

    def __init__(self, code=400, message="业务处理失败"):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Response envelope
# ---------------------------------------------------------------------------
def ok(data=None, message="ok"):
    return {"code": 0, "message": message, "data": data}


def fail(code, message):
    return {"code": code, "message": message, "data": None}


def json_response(payload, status=200):
    return JsonResponse(
        payload,
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


def api_view(methods):
    """Decorator for plain-Django JSON views.

    It checks the HTTP method, parses nothing itself, converts BusinessError
    into the standard envelope, and turns unexpected exceptions into a 500
    response instead of leaking a stack trace to the browser.
    """

    def decorator(func):
        @csrf_exempt
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return json_response(
                    fail(405, f"不支持的请求方法: {request.method}"), 405
                )
            try:
                return func(request, *args, **kwargs)
            except BusinessError as exc:
                return json_response(fail(exc.code, exc.message), exc.code)
            except Exception:  # pragma: no cover - defensive branch
                logger.exception("遴选评分接口发生未处理异常")
                return json_response(fail(500, "服务器内部错误"), 500)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------
def parse_json(request):
    """Parse the request body as a JSON object."""
    if not request.body:
        raise BusinessError(400, "请求体不能为空")
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BusinessError(400, "请求体不是合法的JSON")
    if not isinstance(data, dict):
        raise BusinessError(400, "请求体必须是JSON对象")
    return data


def _is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def get_field(data, *names, default=None, required=False, label=None):
    """Read a field that may be sent as camelCase or snake_case."""
    if not isinstance(data, dict):
        raise BusinessError(400, "请求体必须是JSON对象")
    for name in names:
        if name in data:
            value = data[name]
            if required and _is_blank(value):
                raise BusinessError(400, f"{label or names[0]}不能为空")
            return value
    if required:
        raise BusinessError(400, f"{label or names[0]}不能为空")
    return default


def parse_int(
    value,
    field_name,
    required=True,
    default=None,
    min_value=None,
    max_value=None,
):
    if _is_blank(value):
        if default is not None:
            return default
        if required:
            raise BusinessError(400, f"{field_name}不能为空")
        return None
    if isinstance(value, bool):
        raise BusinessError(400, f"{field_name}必须是整数")
    if isinstance(value, float) and not value.is_integer():
        raise BusinessError(400, f"{field_name}必须是整数")
    if isinstance(value, str) and not re.fullmatch(r"[+-]?\d+", value.strip()):
        raise BusinessError(400, f"{field_name}必须是整数")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise BusinessError(400, f"{field_name}必须是整数")
    if min_value is not None and result < min_value:
        raise BusinessError(400, f"{field_name}不能小于{min_value}")
    if max_value is not None and result > max_value:
        raise BusinessError(400, f"{field_name}不能大于{max_value}")
    return result


def parse_decimal(
    value,
    field_name,
    required=True,
    default=None,
    min_value=None,
    max_value=None,
):
    if _is_blank(value):
        if default is not None:
            value = default
        elif required:
            raise BusinessError(400, f"{field_name}不能为空")
        else:
            return None
    if isinstance(value, bool):
        raise BusinessError(400, f"{field_name}必须是数字")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise BusinessError(400, f"{field_name}必须是数字")
    if not result.is_finite():
        raise BusinessError(400, f"{field_name}必须是有限数字")
    if min_value is not None and result < Decimal(str(min_value)):
        raise BusinessError(400, f"{field_name}不能小于{min_value}")
    if max_value is not None and result > Decimal(str(max_value)):
        raise BusinessError(400, f"{field_name}不能大于{max_value}")
    return result


def parse_page(request):
    page = parse_int(
        request.GET.get("page"), "page", required=False, default=1, min_value=1
    )
    size = parse_int(
        request.GET.get("size"), "size", required=False, default=10, min_value=1
    )
    if size > 100:
        size = 100
    return page, size


def paginated(records, total, page, size):
    return {
        "records": records,
        "total": total,
        "page": page,
        "size": size,
    }


# ---------------------------------------------------------------------------
# Response value formatting
# ---------------------------------------------------------------------------
def to_float(value):
    if value is None:
        return None
    return float(value)


def to_datetime_str(value):
    if not value:
        return None
    try:
        value = timezone.localtime(value)
    except (ValueError, AttributeError):
        pass
    return value.strftime("%Y-%m-%d %H:%M:%S")
