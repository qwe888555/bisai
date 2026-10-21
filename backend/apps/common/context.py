"""请求级上下文。

requestId 与当前账号不能挂在 DRF 的 request.state 上（Django 没有这个属性，
且有并发混用风险），统一用 contextvars 传递，中间件写入、业务代码读取。
"""

from __future__ import annotations

import contextvars
import uuid
from typing import Any, Optional

request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "request_id", default=None
)
current_account_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "current_account", default=None
)

REQUEST_ID_HEADER = "X-Request-Id"
ID_PREFIX_REQUEST = "req"


def new_request_id() -> str:
    return f"{ID_PREFIX_REQUEST}_{uuid.uuid4().hex[:20]}"


def set_request_id(value: Optional[str]) -> str:
    value = (value or "").strip() or new_request_id()
    request_id_var.set(value)
    return value


def get_request_id() -> str:
    return request_id_var.get() or new_request_id()


def set_current_account(account: Any) -> None:
    current_account_var.set(account)


def get_current_account() -> Any:
    return current_account_var.get()
