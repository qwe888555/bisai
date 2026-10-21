"""消息模型：只属于收件账号，其他人不可见。"""

from __future__ import annotations

from django.db import models


class Message(models.Model):
    id = models.CharField(primary_key=True, max_length=64)
    recipient_account_id = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=150)
    body = models.CharField(max_length=2000, blank=True, default="")
    business_type = models.CharField(max_length=50, blank=True, default="")
    business_id = models.CharField(max_length=64, blank=True, default="")
    read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "notices_message"
        indexes = [models.Index(fields=["recipient_account_id", "-created_at"])]

    def __str__(self) -> str:  # pragma: no cover
        return self.title


def push_message(
    account_id: str, title: str, body: str = "", business_type: str = "", business_id: str = ""
) -> Message:
    """供其他模块调用的发消息入口（报名审核、核验结果等）。"""
    from apps.common.ids import new_message_id

    return Message.objects.create(
        id=new_message_id(),
        recipient_account_id=account_id,
        title=title[:150],
        body=(body or "")[:2000],
        business_type=business_type[:50],
        business_id=business_id[:64],
    )
