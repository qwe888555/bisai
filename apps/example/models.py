"""Example 数据模型.

展示多 App 架构下的模型定义模式：
- 从 common.base.models 继承 TimestampMixin
- 显式 db_table 命名（apps_<模块>_<表>）
"""

from django.db import models

from common.base.models import TimestampMixin


class ExampleTag(TimestampMixin):
    """示例标签模型."""

    name = models.CharField(max_length=100, unique=True, verbose_name="标签名称")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    class Meta:
        db_table = "apps_example_tag"
        verbose_name = "示例标签"
        verbose_name_plural = "示例标签"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name
