"""events 域模型：赛事（Event）与赛事组别（EventGroup）。

约定（对齐 accounts 底座，本模块自带软删/版本基类，不跨 app 依赖）：
- 主键一律为不透明字符串 ID（最长 64），不自增整型；
- 可编辑对象带 version 支持乐观锁，删除为软删除（deleted_at），默认 manager 过滤已删；
- 对外响应由 schemas.py 的 mapper 转 camelCase，本模型字段保持 snake_case。
"""

from __future__ import annotations

from django.db import models


class ActiveManager(models.Manager):
    """默认只看未软删对象；需要历史数据时用 all_objects。"""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SoftDeleteVersionedModel(models.Model):
    """版本号 + 时间戳 + 软删除标记的抽象基类。"""

    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True


# ---------------------------------------------------------------- 枚举（与接口文档第 7 节逐字一致）

EVENT_STATUSES = (
    ("DRAFT", "草稿"),
    ("REGISTRATION_OPEN", "报名进行中"),
    ("REGISTRATION_CLOSED", "报名已截止"),
    ("RUNNING", "进行中"),
    ("FINISHED", "已结束"),
)

GROUP_CATEGORIES = (
    ("PRIMARY", "小学组"),
    ("MIDDLE", "初中组"),
    ("UNIVERSITY", "高校组"),
)

GROUP_GENDERS = (
    ("MALE", "男子"),
    ("FEMALE", "女子"),
    ("MIXED", "混合"),
)


class Event(SoftDeleteVersionedModel):
    """赛事配置：赛季、报名时间窗与状态。"""

    id = models.CharField(primary_key=True, max_length=64)
    name = models.CharField(max_length=200)
    season = models.CharField(max_length=30)
    # 赛事类别字典编码，可空
    event_category = models.CharField(max_length=64, blank=True, default="")
    registration_opens_at = models.DateTimeField()
    registration_closes_at = models.DateTimeField()
    status = models.CharField(
        max_length=20, choices=EVENT_STATUSES, default="DRAFT"
    )
    description = models.TextField(max_length=5000, blank=True, default="")

    class Meta:
        db_table = "events_event"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["season"]),
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}({self.season})"


class EventGroup(SoftDeleteVersionedModel):
    """赛事组别：学段 + 性别 + 名额/年龄/上场人数限制。"""

    id = models.CharField(primary_key=True, max_length=64)
    event = models.ForeignKey(
        Event,
        on_delete=models.PROTECT,
        related_name="groups",
        db_column="event_id",
    )
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=12, choices=GROUP_CATEGORIES)
    gender = models.CharField(max_length=10, choices=GROUP_GENDERS)
    # 名额：0 表示不设上限
    quota = models.PositiveIntegerField(default=0)
    min_age = models.PositiveSmallIntegerField(null=True, blank=True)
    max_age = models.PositiveSmallIntegerField(null=True, blank=True)
    max_players = models.PositiveSmallIntegerField()
    required_material_types = models.JSONField(default=list)

    class Meta:
        db_table = "events_event_group"
        indexes = [
            models.Index(fields=["event"]),
            models.Index(fields=["category"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}@{self.event_id}"