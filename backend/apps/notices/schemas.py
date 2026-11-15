"""notices 请求模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from apps.common.schemas import StrictSchema


class MessagePublishIn(StrictSchema):
    """管理员发布系统消息：roles 为空表示发送给全部启用账号。"""

    title: str = Field(min_length=1, max_length=150)
    body: str = Field(default="", max_length=2000)
    roles: Optional[list[str]] = Field(None, max_length=8)
