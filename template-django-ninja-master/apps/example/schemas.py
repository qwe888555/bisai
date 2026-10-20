"""Example Pydantic Schema."""

from datetime import datetime

from ninja import Schema
from pydantic import Field


class TagCreate(Schema):
    """创建标签请求."""

    name: str = Field(..., min_length=1, max_length=100, examples=["紧急"])
    is_active: bool = True


class TagUpdate(Schema):
    """更新标签请求."""

    name: str | None = Field(None, min_length=1, max_length=100)
    is_active: bool | None = None


class TagResponse(Schema):
    """标签响应."""

    id: int
    name: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
