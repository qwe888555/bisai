"""Example 应用配置."""

from django.apps import AppConfig


class ExampleConfig(AppConfig):
    """示例业务模块配置."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.example"
    verbose_name = "示例模块"
