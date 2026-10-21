"""政策文本库。

契约 4.6：只从**已发布且适用赛事/年度/有效期**的 POLICY 内容取少量文本，
不建向量库、不读私有材料、不连业务库生成 SQL。这里保存政策正文快照，
正式发布内容由成员5 的 CMS 接口写 source_content_id 关联过来。
"""

from __future__ import annotations

from django.db import models


class PolicyDocument(models.Model):
    id = models.CharField(primary_key=True, max_length=64)
    source_content_id = models.CharField(max_length=64, blank=True, default="")
    title = models.CharField(max_length=200)
    summary = models.CharField(max_length=1000, blank=True, default="")
    body = models.TextField()
    event_id = models.CharField(max_length=64, blank=True, default="")
    season = models.CharField(max_length=30, blank=True, default="")
    revision = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_to = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "policyqa_policy_document"
        indexes = [
            models.Index(fields=["enabled", "event_id", "season"]),
            models.Index(fields=["source_content_id"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.title
