"""共享基础模型.

所有 App 的模型可继承此处的 Mixin 获得通用字段和行为。
"""

from django.db import models
from django.utils import timezone


class TimestampMixin(models.Model):
    """时间戳 Mixin: 自动记录创建和更新时间."""

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        abstract = True


class SoftDeleteMixin(models.Model):
    """软删除 Mixin: 标记删除而非物理删除."""

    is_deleted = models.BooleanField(default=False, verbose_name="是否删除")
    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name="删除时间")

    class Meta:
        abstract = True

    def soft_delete(self) -> None:
        """标记为已删除."""
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at", "updated_at"])

    def restore(self) -> None:
        """恢复已删除的记录."""
        self.is_deleted = False
        self.deleted_at = None
        self.save(update_fields=["is_deleted", "deleted_at", "updated_at"])
