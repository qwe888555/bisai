"""生产环境安全启动校验测试（B3 + 2026-10-28 ENROLLMENT_MOCK_CLIENTS 加固）。

validate_production_settings 为纯函数，用 SimpleNamespace 注入 settings 字段
逐项验证 violations/warnings；_enforce_production 验证 ImproperlyConfigured
拒绝启动路径与 dev/test 零影响。mock_enabled 的 prod 运行时双保险矩阵见
apps/enrollments/tests/test_p03_integration.py（依赖 os.environ，与 pytest
monkeypatch 配合）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.common.production import validate_production_settings
from apps.enrollments.clients import mock_enabled


def _base_settings(**overrides) -> SimpleNamespace:
    """满足现有全部校验的最小合法 prod settings 骨架（便于聚焦单项目标）。"""
    fields = {
        "DJANGO_ENV": "prod",
        "DEBUG": False,
        "SECRET_KEY": "x" * 40,
        "JWT_SECRET": "y" * 40,
        "ALLOWED_HOSTS": ["example.com"],
        "DATABASES": {"default": {"ENGINE": "django.db.backends.mysql"}},
        "ENROLLMENT_MOCK_CLIENTS": False,
        "CRYPTO_KEY": "z" * 32,
        "CORS_ORIGINS": ["https://example.com"],
        "REDIS_URL": "redis://localhost:6379/0",
        "TIME_ZONE": "Asia/Shanghai",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class EnrollmentMockClientsProductionTests(SimpleTestCase):
    """风险二：生产环境 mock 配置不得绕过报名资格校验。"""

    def test_mock_enabled_true_is_violation(self):
        violations, _ = validate_production_settings(
            _base_settings(ENROLLMENT_MOCK_CLIENTS=True)
        )
        hit = [v for v in violations if "ENROLLMENT_MOCK_CLIENTS" in v]
        self.assertEqual(len(hit), 1)
        self.assertIn("开启状态", hit[0])
        self.assertIn("false", hit[0])

    def test_mock_config_missing_is_violation(self):
        violations, _ = validate_production_settings(
            _base_settings(ENROLLMENT_MOCK_CLIENTS=None)
        )
        hit = [v for v in violations if "ENROLLMENT_MOCK_CLIENTS" in v]
        self.assertEqual(len(hit), 1)
        self.assertIn("未配置", hit[0])

    def test_mock_config_illegal_value_is_violation(self):
        violations, _ = validate_production_settings(
            _base_settings(ENROLLMENT_MOCK_CLIENTS="abc")
        )
        hit = [v for v in violations if "ENROLLMENT_MOCK_CLIENTS" in v]
        self.assertEqual(len(hit), 1)
        self.assertIn("非法", hit[0])

    def test_mock_explicit_false_passes_this_check(self):
        violations, _ = validate_production_settings(_base_settings())
        self.assertFalse(
            [v for v in violations if "ENROLLMENT_MOCK_CLIENTS" in v],
            violations,
        )

    def test_violation_message_does_not_leak_secrets(self):
        """错误消息只含配置项与修复方向，不输出密钥值。"""
        violations, _ = validate_production_settings(
            _base_settings(
                ENROLLMENT_MOCK_CLIENTS=True,
                SECRET_KEY="super-secret-value-123",
                JWT_SECRET="super-secret-value-123",
            )
        )
        joined = "；".join(violations)
        self.assertNotIn("super-secret-value-123", joined)


class EnforceProductionStartupTests(SimpleTestCase):
    """CommonConfig._enforce_production：violations → ImproperlyConfigured 拒绝启动。"""

    def _enforce(self, settings_obj):
        from types import SimpleNamespace

        from apps.common.apps import CommonConfig

        # _enforce_production 不依赖 self（仅读 settings 参数），类调用注入占位
        return CommonConfig._enforce_production(SimpleNamespace(), settings_obj)

    def test_prod_with_mock_on_rejects_startup(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._enforce(_base_settings(ENROLLMENT_MOCK_CLIENTS=True))
        self.assertIn("ENROLLMENT_MOCK_CLIENTS", str(ctx.exception))

    def test_prod_with_mock_off_not_blocked_by_this_check(self):
        """mock=false 时不得因本项拒绝启动（其余配置均合法）。"""
        try:
            self._enforce(_base_settings())
        except Exception as exc:  # noqa: BLE001 - 任何异常都说明本项误伤
            self.fail(f"mock=false 不应触发启动拒绝：{exc}")

    def test_dev_env_skips_validation_entirely(self):
        """dev 档零校验：mock 开启不报错（现有开发流程零影响）。"""
        s = _base_settings(DJANGO_ENV="dev", DEBUG=True, ENROLLMENT_MOCK_CLIENTS=True)
        try:
            self._enforce(s)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"dev 档不应执行生产校验：{exc}")


class MockEnabledRuntimeTests(SimpleTestCase):
    """mock_enabled() 运行期双保险（os.environ 直读口径）。"""

    def _with_env(self, django_env, mock_clients):
        import os
        from unittest import mock as ut_mock

        env = {"DJANGO_ENV": django_env}
        if mock_clients is not None:
            env["ENROLLMENT_MOCK_CLIENTS"] = mock_clients
        with ut_mock.patch.dict(os.environ, env):
            if mock_clients is None:
                os.environ.pop("ENROLLMENT_MOCK_CLIENTS", None)  # 模拟"未配置"
            return mock_enabled()

    def test_prod_forces_false_even_when_switch_on(self):
        self.assertFalse(self._with_env("prod", "true"))

    def test_dev_default_is_true(self):
        self.assertTrue(self._with_env("dev", None))
        self.assertTrue(self._with_env("dev", "true"))

    def test_dev_explicit_false(self):
        self.assertFalse(self._with_env("dev", "false"))


class SubprocessStartupTests(SimpleTestCase):
    """生产配置启动路径回归（隔离子进程跑 `manage.py check`）。

    覆盖配置函数单测无法触及的真实加载链：settings._load_dotenv →
    assert_valid_env → CommonConfig.ready → _enforce_production。断言退出码
    与 traceback 中的异常类型（而非仅字符串出现）。
    隔离前提（2026-10-28 核实）：
    - settings 的 load_dotenv(override=False) 不覆盖显式 os.environ，
      且 backend/.env 不含 DJANGO_ENV / ENROLLMENT_MOCK_CLIENTS 键，
      "mock 缺失"场景真实成立；
    - 显式注入 DATABASE_URL=sqlite:///:memory: 隔离真实 MySQL；
    - check 命令本身不执行数据库查询，无任何数据库写入。
    """

    @classmethod
    def _run_check(cls, *, django_env, mock_clients="__unset__"):
        import os
        import subprocess
        import sys

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("DJANGO_ENV", "ENROLLMENT_MOCK_CLIENTS", "DATABASE_URL")
        }
        env["DATABASE_URL"] = "sqlite:///:memory:"
        env["PYTHONIOENCODING"] = "utf-8"
        if django_env is not None:
            env["DJANGO_ENV"] = django_env
        if mock_clients != "__unset__":
            env["ENROLLMENT_MOCK_CLIENTS"] = mock_clients
        backend_dir = str(cls._backend_dir())
        return subprocess.run(
            [sys.executable, "manage.py", "check"],
            env=env,
            cwd=backend_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    @staticmethod
    def _backend_dir():
        return Path(__file__).resolve().parent.parent

    def test_prod_with_mock_on_rejects_startup(self):
        """prod + mock=true → ImproperlyConfigured 拒绝启动，违规含目标项。"""
        proc = self._run_check(django_env="prod", mock_clients="true")
        self.assertNotEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("django.core.exceptions.ImproperlyConfigured", proc.stderr)
        self.assertIn("ENROLLMENT_MOCK_CLIENTS", proc.stderr)
        self.assertIn("开启状态", proc.stderr)

    def test_prod_with_mock_missing_rejects_startup(self):
        """prod + mock 未设置 → 同样拒绝启动，违规指明"未配置"。"""
        proc = self._run_check(django_env="prod", mock_clients="__unset__")
        self.assertNotEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("django.core.exceptions.ImproperlyConfigured", proc.stderr)
        self.assertIn("ENROLLMENT_MOCK_CLIENTS", proc.stderr)
        self.assertIn("未配置", proc.stderr)

    def test_illegal_mock_value_rejected_with_clear_error(self):
        """mock 取值非法（如 abc）→ settings 加载期 ValueError，任何环境拒绝，
        错误信息指明合法取值集合（不回显原值）。"""
        proc = self._run_check(django_env="dev", mock_clients="abc")
        self.assertNotEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("ValueError", proc.stderr)
        self.assertIn("ENROLLMENT_MOCK_CLIENTS 取值非法", proc.stderr)

    def test_prod_with_mock_false_no_mock_violation_but_other_checks_active(self):
        """prod + mock=false → 本项不违规；其余既有生产校验（DEBUG 等）仍生效。"""
        proc = self._run_check(django_env="prod", mock_clients="false")
        self.assertNotEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("django.core.exceptions.ImproperlyConfigured", proc.stderr)
        self.assertNotIn("ENROLLMENT_MOCK_CLIENTS", proc.stderr)  # 本项零提及
        self.assertIn("DEBUG 处于开启状态", proc.stderr)  # 既有校验正常生效

    def test_dev_env_mock_on_passes_check(self):
        """dev + mock=true → check 通过：既有开发/测试流程零影响。"""
        proc = self._run_check(django_env="dev", mock_clients="true")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
