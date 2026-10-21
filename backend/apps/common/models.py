"""common 自有模型：幂等记录。

放在 common 而不是某个业务应用，是因为报名提交、比赛事件、遴选生成等
多个模块都要用（契约 2.2），且这些记录与业务表无外键关系。
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class IdempotencyRecord(models.Model):
    """账号＋操作＋对象隔离的幂等结果缓存，默认保留 24 小时。"""

    id = models.CharField(primary_key=True, max_length=64)
    account_id = models.CharField(max_length=64, db_index=True)
    operation = models.CharField(max_length=64, db_index=True)
    object_id = models.CharField(max_length=64, blank=True, default="")
    key_hash = models.CharField(max_length=64)
    request_hash = models.CharField(max_length=64)
    response_body = models.JSONField(null=True, blank=True)
    status_code = models.PositiveSmallIntegerField(default=200)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "common_idempotency_record"
        unique_together = ("account_id", "operation", "object_id", "key_hash")
        indexes = [
            models.Index(fields=["account_id", "operation", "object_id"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.account_id}:{self.operation}:{self.object_id}"

    @property
    def expired(self) -> bool:
        return self.expires_at <= timezone.now()
