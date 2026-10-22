"""MySQL 精确类型字段（严格对齐冻结 DDL V1.4-r1）。

Django 内置字段与目标 MySQL 8.0 类型的两处差异，用自定义字段补齐：
1. gender 声明为 TINYINT，而 SmallIntegerField 生成 smallint → TinyIntegerField；
2. remark/introduction 声明为 TEXT，而 TextField 生成 longtext → TextField。
"""

from __future__ import annotations

from django.db import models


class TinyIntegerField(models.SmallIntegerField):
    """MySQL TINYINT（有符号），对应 DDL 中声明为 TINYINT 的列。"""

    def db_type(self, connection):
        return "tinyint"


class TextField(models.TextField):
    """MySQL TEXT，对应 DDL 中声明为 TEXT 的列（Django 默认生成 longtext）。"""

    def db_type(self, connection):
        return "text"
