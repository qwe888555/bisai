"""共享日志工具.

包含请求追踪 ID 中间件和日志过滤器，用于将 request_id 注入日志记录。
"""

import logging
import uuid
from collections.abc import Callable
from threading import local

from django.http import HttpRequest, HttpResponse

_request_local = local()


def get_request_id() -> str:
    """获取当前线程的请求 ID（用于日志中关联同一请求）."""
    return getattr(_request_local, "request_id", "-")


class RequestIdFilter(logging.Filter):
    """日志过滤器: 自动注入 request_id 到每条日志记录.

    使用方式（在 LOGGING 配置的 filters 中引用）:
        "filters": ["request_id"],
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class SimpleJsonFormatter(logging.Formatter):
    """简易 JSON 格式化器（无需外部依赖）.

    生产环境推荐使用 python-json-logger，功能更完善：
    pip install python-json-logger
    然后替换为 pythonjsonlogger.jsonlogger.JsonFormatter
    """

    def format(self, record: logging.LogRecord) -> str:
        import datetime as dt
        import json

        log_data: dict[str, str | int | float] = {
            "level": record.levelname,
            "time": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "logger": record.name,
            "module": record.module,
            "line": record.lineno,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data, ensure_ascii=False)


class RequestIdMiddleware:
    """为每个请求注入唯一 request_id，并在响应头返回.

    自动添加到 X-Request-ID 响应头，方便客户端关联请求与日志。
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = str(uuid.uuid4())[:8]
        request.request_id = request_id  # type: ignore[attr-defined]
        _request_local.request_id = request_id
        response: HttpResponse = self.get_response(request)
        response["X-Request-ID"] = request_id
        return response
