"""policyqa 接口：唯一 AI 入口，公众免登录。"""

from __future__ import annotations

from typing import Optional

from ninja import Field, Router

from apps.common.envelope import ok
from apps.common.schemas import EnvelopeOut, StrictSchema

from . import services

router = Router(tags=["policy"])


class PolicyQuestion(StrictSchema):
    """与 PolicyQuestion 序列化器字段一致。"""

    question: str = Field(..., min_length=1, max_length=1000)
    eventId: Optional[str] = Field(None, min_length=1, max_length=64)
    season: Optional[str] = Field(None, max_length=30)


@router.post(
    "/public/policy-questions",
    auth=None,
    response=EnvelopeOut,
    summary="政策与报名问答",
)
def policy_question(request, payload: PolicyQuestion):
    result = services.answer(payload.question, payload.eventId, payload.season)
    return ok(result)
