"""B3 生产环境安全启动校验测试。

运行方式（与其他测试一致，内存 SQLite、不触碰 MySQL）：
    .\\.venv\\Scripts\\python.exe manage.py test apps.common

策略：validate_production_settings 为纯函数，直接以 SimpleNamespace 注入配置对象，
不重载全局 settings、不改真实环境变量、不连接任何数据库。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from django.apps import apps as django_apps
from django.core.exceptions import ImproperlyConfigured

from apps.common.production import (
    DEV_DEFAULT_SECRET_KEY,
    assert_valid_env,
    validate_production_settings,
)


def make_settings(**overrides):
    """安全基线配置；各测试按需覆盖单个字段制造不安全场景。"""
    base = dict(
        DJANGO_ENV="prod",
        DEBUG=False,
        SECRET_KEY="S" * 40,
        JWT_SECRET="J" * 40,
        ALLOWED_HOSTS=["api.example.edu.cn"],
        DATABASES={"default": {"ENGINE": "django.db.backends.mysql", "NAME": "football"}},
        CRYPTO_KEY="C" * 44,
        CORS_ORIGINS=["https://front.example.edu.cn"],
        REDIS_URL="redis://cache:6379/0",
        TIME_ZONE="Asia/Shanghai",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def violations_text(s) -> str:
    v, w = validate_production_settings(s)
    return "；".join(v + w)


class ProductionValidationTests(TestCase):
    # ---------------------------------------------------- 拒绝项

    def test_prod_unsafe_defaults_all_rejected(self):
        """全默认开发配置（DEBUG=True/默认密钥/回退JWT/通配符/SQLite）→ 全部拒绝。"""
        s = make_settings(
            DEBUG=True,
            SECRET_KEY=DEV_DEFAULT_SECRET_KEY,
            JWT_SECRET=DEV_DEFAULT_SECRET_KEY,
            ALLOWED_HOSTS=["*"],
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "x.db"}},
            CRYPTO_KEY="",
            CORS_ORIGINS=["http://localhost:5173", "http://127.0.0.1:5173"],
            REDIS_URL="",
        )
        violations, warnings = validate_production_settings(s)
        self.assertEqual(len(violations), 5, violations)
        for keyword in ("DEBUG", "DJANGO_SECRET_KEY", "JWT_SECRET", "ALLOWED_HOSTS", "SQLite"):
            self.assertTrue(any(keyword in v for v in violations), (keyword, violations))
        self.assertEqual(len(warnings), 3)  # 仅时区安全基线不触发告警（CRYPTO/CORS/Redis）

    def test_safe_production_config_passes(self):
        violations, warnings = validate_production_settings(make_settings())
        self.assertEqual(violations, [])
        self.assertEqual(warnings, [])

    def test_missing_secret_key_rejected(self):
        s = make_settings(SECRET_KEY="")
        self.assertTrue(any("DJANGO_SECRET_KEY" in v for v in validate_production_settings(s)[0]))

    def test_blank_secret_key_rejected(self):
        s = make_settings(SECRET_KEY="   ")
        self.assertTrue(any("DJANGO_SECRET_KEY" in v for v in validate_production_settings(s)[0]))

    def test_dev_default_secret_key_rejected(self):
        s = make_settings(SECRET_KEY=DEV_DEFAULT_SECRET_KEY)
        self.assertTrue(
            any("开发默认值" in v for v in validate_production_settings(s)[0])
        )

    def test_blank_jwt_secret_rejected(self):
        s = make_settings(JWT_SECRET="  \t ")
        self.assertTrue(any("JWT_SECRET" in v for v in validate_production_settings(s)[0]))

    def test_jwt_secret_fallback_to_secret_key_rejected(self):
        """JWT_SECRET 与 SECRET_KEY 相同 = 回退生效，视为未独立配置。"""
        s = make_settings(JWT_SECRET=make_settings().SECRET_KEY)
        self.assertTrue(any("回退" in v for v in validate_production_settings(s)[0]))

    def test_allowed_hosts_wildcard_rejected(self):
        s = make_settings(ALLOWED_HOSTS=["*"])
        self.assertTrue(any("通配符" in v for v in validate_production_settings(s)[0]))

    def test_allowed_hosts_empty_rejected(self):
        s = make_settings(ALLOWED_HOSTS=[])
        self.assertTrue(any("为空" in v for v in validate_production_settings(s)[0]))

    def test_sqlite_fallback_rejected(self):
        s = make_settings(
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "x.db"}}
        )
        self.assertTrue(any("SQLite" in v for v in validate_production_settings(s)[0]))

    # ---------------------------------------------------- 告警项

    def test_crypto_key_empty_warns_but_not_rejected(self):
        """临时策略：CRYPTO_KEY 缺失仅告警；人员敏感字段加密上线前必须升级为拒绝项。"""
        violations, warnings = validate_production_settings(make_settings(CRYPTO_KEY=""))
        self.assertFalse(any("CRYPTO_KEY" in v for v in violations))
        self.assertTrue(any("CRYPTO_KEY" in w and "强制拒绝" in w for w in warnings))

    def test_crypto_key_configured_no_warning(self):
        _, warnings = validate_production_settings(make_settings())
        self.assertFalse(any("CRYPTO_KEY" in w for w in warnings))

    def test_cors_default_origins_warns(self):
        _, warnings = validate_production_settings(
            make_settings(CORS_ORIGINS=["http://localhost:5173", "http://127.0.0.1:5173"])
        )
        self.assertTrue(any("CORS_ORIGINS" in w for w in warnings))

    def test_redis_empty_warns(self):
        _, warnings = validate_production_settings(make_settings(REDIS_URL=""))
        self.assertTrue(any("REDIS_URL" in w for w in warnings))

    def test_non_shanghai_timezone_warns(self):
        _, warnings = validate_production_settings(make_settings(TIME_ZONE="UTC"))
        self.assertTrue(any("TIME_ZONE" in w for w in warnings))

    # ---------------------------------------------------- 泄密防护

    def test_messages_never_leak_secret_values(self):
        """错误/告警文本只含配置项与修复方向，绝不含密钥或凭据值。"""
        s = make_settings(
            SECRET_KEY="LEAK-SECRET-MARKER",
            JWT_SECRET="LEAK-JWT-MARKER",
            CRYPTO_KEY="",
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": "x.db",
                    "PASSWORD": "LEAK-PASSWORD-MARKER",
                }
            },
            ALLOWED_HOSTS=["*"],
        )
        text = violations_text(s)
        for marker in ("LEAK-SECRET-MARKER", "LEAK-JWT-MARKER", "LEAK-PASSWORD-MARKER"):
            self.assertNotIn(marker, text)

    # ---------------------------------------------------- 环境档位取值

    def test_unknown_env_value_rejected_without_echo(self):
        with self.assertRaises(ValueError) as ctx:
            assert_valid_env("staging")
        self.assertNotIn("staging", str(ctx.exception))  # 不回显输入内容

    def test_blank_env_value_rejected(self):
        with self.assertRaises(ValueError):
            assert_valid_env("")

    def test_valid_env_values_accepted(self):
        for value in ("dev", "test", "prod"):
            assert_valid_env(value)  # 不抛即通过


class ReadyEnforcementTests(TestCase):
    """AppConfig.ready 触发链路：dev 不校验、prod 不安全配置拒绝启动。

    直接取 app 注册表中已注册的 CommonConfig 实例（与生产 ready() 同一对象），
    避免 AppConfig 手工构造对模块参数的强约束；settings 经参数注入，不改全局。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = django_apps.get_app_config("common")

    def test_dev_mode_skips_validation(self):
        """dev 模式下即使全不安全配置也不校验不报错——现有开发/测试方式零影响。"""
        unsafe = make_settings(DJANGO_ENV="dev", DEBUG=True, SECRET_KEY="")
        self.config._enforce_production(unsafe)  # 不抛

    def test_test_mode_skips_validation(self):
        """test 模式与 dev 同级跳过校验。"""
        unsafe = make_settings(DJANGO_ENV="test", DEBUG=True, SECRET_KEY="")
        self.config._enforce_production(unsafe)  # 不抛

    def test_prod_mode_rejects_unsafe_config(self):
        unsafe = make_settings(DEBUG=True)
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self.config._enforce_production(unsafe)
        # 确认异常来自校验器而非其他环节，且指明拒绝启动
        self.assertIn("生产环境安全校验未通过", str(ctx.exception))
        self.assertIn("DEBUG", str(ctx.exception))

    def test_prod_mode_safe_config_passes(self):
        self.config._enforce_production(make_settings())  # 不抛
