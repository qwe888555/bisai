"""Schema 基类与信封响应模型（django-ninja / pydantic v2）。

- StrictSchema：拒绝未声明字段（契约禁止借未知字段改 owner/权限/总分）；
- EnvelopeOut：契约统一响应体，所有接口都用它作为 response 声明，
  写错结构会直接被 ninja 校验拦下，避免漏包信封。
"""

from __future__ import annotations

from typing import Any

from ninja import Schema
from pydantic import ConfigDict


class StrictSchema(Schema):
    """请求模型基类：多传字段直接 400 INVALID_ARGUMENT。"""

    model_config = ConfigDict(extra="forbid")


class EnvelopeOut(Schema):
    """契约 2.1 统一响应体：{code, message, data, requestId}。"""

    code: str
    message: str
    data: Any = None
    requestId: str = ""
