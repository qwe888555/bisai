import logging

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


class CommonConfig(AppConfig):
    name = "apps.common"
    label = "common"
    verbose_name = "公共协议与基础设施"

    def ready(self) -> None:
        """prod 模式下执行生产安全启动校验（B3）。

        dev/test 直接跳过：现有开发流程、测试运行（含内存 SQLite）与
        migrate/check 等管理命令行为完全不变。prod 下校验不通过即拒绝启动，
        无绕过开关（安全校验一旦可绕过即形同虚设）。
        """
        self._enforce_production()

    def _enforce_production(self, settings_override=None) -> None:
        """读取 DJANGO_ENV 并在 prod 模式执行校验；拆出以便单元测试注入。"""
        from django.conf import settings as dj_settings

        from .production import validate_production_settings

        s = settings_override or dj_settings
        if getattr(s, "DJANGO_ENV", "dev") != "prod":
            return
        violations, warnings = validate_production_settings(s)
        for item in warnings:
            logger.warning("生产配置告警: %s", item)
        if violations:
            detail = "；".join(violations)
            raise ImproperlyConfigured(
                f"生产环境安全校验未通过（{len(violations)} 项），拒绝启动：{detail}"
                "——修复 backend/.env 后重启"
            )
