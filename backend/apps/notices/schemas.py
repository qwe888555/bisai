"""notices 请求模型。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from apps.common.schemas import StrictSchema

# 与前端消息分类一一对应：审核结果/材料提醒/赛程变更/公示通知/系统。
MessageBusinessType = Literal["REVIEW", "MATERIAL", "MATCH", "PUBLICATION", "SYSTEM"]


class MessagePublishIn(StrictSchema):
    """管理员发布系统消息：roles 为空表示发送给全部启用账号。"""

    title: str = Field(min_length=1, max_length=150)
    body: str = Field(default="", max_length=2000)
    roles: Optional[list[str]] = Field(None, max_length=8)
    businessType: Optional[MessageBusinessType] = Field(default="SYSTEM")
