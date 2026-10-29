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
from django.utils import timezone

from apps.accounts.models import Account, Organization
from apps.common.errors import DATA_SCOPE_DENIED, ApiError
from apps.common.production import (
    DEV_DEFAULT_SECRET_KEY,
    assert_valid_env,
    validate_production_settings,
)
from apps.common.scopes import ensure_team_in_scope, scope_org_codes
from apps.teams.models import Team
from django.test import TestCase as DjangoTestCase  # noqa: E402  需要 setUpTestData 类级夹具


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
        # 2026-10-29 对齐 production.py 第 6 项（P0 加固）：安全基线显式关闭
        # mock 客户端替身；仅显式 False 通过校验。不删除/弱化任何生产校验项。
        ENROLLMENT_MOCK_CLIENTS=False,
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


# ============================================================================
# A′ 组织范围 helper（common/scopes.py）：scope_org_codes / ensure_team_in_scope
#
# 组织树夹具：
#   ORG-P(省) → ORG-C(市) → ORG-SA(学校A,code=SCH-A) → ORG-TA1/TA2(球队,code=team_no)
#                        → ORG-SB(学校B,code=SCH-B) → ORG-TB1(球队)
# A′-min 契约（2026-10-25）：TEAM 组织节点 code == team.team_no。
# ============================================================================

TEAM_A1 = "T202600000001"
TEAM_A2 = "T202600000002"
TEAM_B1 = "T202600000003"
TEAM_NO_MAPPING = "T202699999999"


def _soft_delete(obj) -> None:
    obj.deleted_at = timezone.now()
    obj.save()


class OrgScopeHelperDataTests(DjangoTestCase):
    """scope_org_codes 三态语义与 deny-by-default。"""

    @classmethod
    def setUpTestData(cls):
        cls.prov = Organization.objects.create(
            id="ORG-P", code="P-CODE", name="省", org_type="PROVINCE"
        )
        cls.city = Organization.objects.create(
            id="ORG-C", code="C-CODE", name="市", org_type="CITY", parent=cls.prov
        )
        cls.school_a = Organization.objects.create(
            id="ORG-SA", code="SCH-A", name="学校A", org_type="SCHOOL", parent=cls.city
        )
        cls.team_a1 = Organization.objects.create(
            id="ORG-TA1", code=TEAM_A1, name="A1队", org_type="TEAM", parent=cls.school_a
        )
        cls.team_a2 = Organization.objects.create(
            id="ORG-TA2", code=TEAM_A2, name="A2队", org_type="TEAM", parent=cls.school_a
        )
        cls.school_b = Organization.objects.create(
            id="ORG-SB", code="SCH-B", name="学校B", org_type="SCHOOL", parent=cls.city
        )
        cls.team_b1 = Organization.objects.create(
            id="ORG-TB1", code=TEAM_B1, name="B1队", org_type="TEAM", parent=cls.school_b
        )
        cls.soft_deleted_org = Organization.objects.create(
            id="ORG-SD", code="SD-CODE", name="已软删", org_type="SCHOOL", parent=cls.city
        )
        _soft_delete(cls.soft_deleted_org)

        cls.global_admin = Account.objects.create(
            id="ACC-GA", username="ga", password="x", roles=["ADMIN"]
        )
        cls.school_admin = Account.objects.create(
            id="ACC-SA", username="sa", password="x", roles=["UNIVERSITY"],
            organization=cls.school_a,
        )
        cls.admin_with_org = Account.objects.create(
            id="ACC-AO", username="ao", password="x", roles=["ADMIN"],
            organization=cls.school_a,
        )
        cls.no_org_account = Account.objects.create(
            id="ACC-NO", username="no", password="x", roles=["CITY"]
        )
        cls.soft_deleted_binding = Account.objects.create(
            id="ACC-SD", username="sd", password="x", roles=["CITY"],
            organization=cls.soft_deleted_org,
        )

    def test_global_admin_returns_none(self):
        """无组织的 ADMIN = 全局管理员 → None（全库范围）。"""
        self.assertIsNone(scope_org_codes(self.global_admin))

    def test_admin_with_org_gets_subtree_not_none(self):
        """挂组织的 ADMIN 不是全局管理员（is_global_admin 复用）→ 受限子树编码。"""
        codes = scope_org_codes(self.admin_with_org)
        self.assertIsNotNone(codes)
        self.assertEqual(codes, ["SCH-A", TEAM_A1, TEAM_A2])

    def test_scoped_account_returns_subtree_codes(self):
        """非全局管理员有有效组织范围 → 所属子树全部节点编码。"""
        self.assertEqual(
            scope_org_codes(self.school_admin), ["SCH-A", TEAM_A1, TEAM_A2]
        )

    def test_account_without_org_returns_empty_list(self):
        """无组织绑定的非管理员 → []，必须拒绝访问。"""
        self.assertEqual(scope_org_codes(self.no_org_account), [])

    def test_account_with_nonexistent_org_returns_empty_list(self):
        """绑定组织 ID 在库中不存在（未落库实例模拟）→ []，不放大为全库。"""
        ghost = Account(organization_id="ORG-GHOST", roles=["CITY"])
        self.assertEqual(scope_org_codes(ghost), [])

    def test_account_with_soft_deleted_org_returns_empty_list(self):
        """绑定组织已软删除 → []，deny-by-default。"""
        self.assertEqual(scope_org_codes(self.soft_deleted_binding), [])


class EnsureTeamInScopeTests(DjangoTestCase):
    """ensure_team_in_scope：A′ 编码映射（TEAM 节点 code == team_no）校验。"""

    @classmethod
    def setUpTestData(cls):
        cls.prov = Organization.objects.create(
            id="ORG-P", code="P-CODE", name="省", org_type="PROVINCE"
        )
        cls.city = Organization.objects.create(
            id="ORG-C", code="C-CODE", name="市", org_type="CITY", parent=cls.prov
        )
        cls.school_a = Organization.objects.create(
            id="ORG-SA", code="SCH-A", name="学校A", org_type="SCHOOL", parent=cls.city
        )
        cls.team_a1_org = Organization.objects.create(
            id="ORG-TA1", code=TEAM_A1, name="A1队", org_type="TEAM", parent=cls.school_a
        )
        cls.team_a2_org = Organization.objects.create(
            id="ORG-TA2", code=TEAM_A2, name="A2队", org_type="TEAM", parent=cls.school_a
        )
        cls.school_b = Organization.objects.create(
            id="ORG-SB", code="SCH-B", name="学校B", org_type="SCHOOL", parent=cls.city
        )
        Organization.objects.create(
            id="ORG-TB1", code=TEAM_B1, name="B1队", org_type="TEAM", parent=cls.school_b
        )

        cls.global_admin = Account.objects.create(
            id="ACC-GA", username="ga", password="x", roles=["ADMIN"]
        )
        cls.school_admin = Account.objects.create(
            id="ACC-SA", username="sa", password="x", roles=["UNIVERSITY"],
            organization=cls.school_a,
        )
        cls.no_org_account = Account.objects.create(
            id="ACC-NO", username="no", password="x", roles=["CITY"]
        )
        cls.team_account = Account.objects.create(
            id="ACC-TA", username="ta", password="x", roles=["TEAM"],
            organization=cls.team_a1_org,
        )

        cls.team_a1 = Team.objects.create(team_no=TEAM_A1, name="A1队", category_code="U12")
        cls.team_a2 = Team.objects.create(team_no=TEAM_A2, name="A2队", category_code="U12")
        cls.team_b1 = Team.objects.create(team_no=TEAM_B1, name="B1队", category_code="U12")
        cls.team_no_mapping = Team.objects.create(
            team_no=TEAM_NO_MAPPING, name="无映射队", category_code="U12"
        )

    def _assert_denied(self, account, team):
        with self.assertRaises(ApiError) as ctx:
            ensure_team_in_scope(account, team)
        self.assertEqual(ctx.exception.code, DATA_SCOPE_DENIED)
        self.assertEqual(ctx.exception.status, 403)

    def test_global_admin_allowed_even_without_team_mapping(self):
        """全局管理员提前放行：即使球队无组织映射也不依赖映射。"""
        ensure_team_in_scope(self.global_admin, self.team_no_mapping)  # 不抛

    def test_team_in_scope_allowed(self):
        """学校A 账号访问本队（映射在本校子树内）→ 允许。"""
        ensure_team_in_scope(self.school_admin, self.team_a1)  # 不抛

    def test_team_out_of_scope_denied(self):
        """学校A 账号访问学校B 的球队 → 403 DATA_SCOPE_DENIED。"""
        self._assert_denied(self.school_admin, self.team_b1)

    def test_team_missing_mapping_denied(self):
        """team_no 解析不出有效 TEAM 节点（映射缺失）→ 拒绝，不得放行。"""
        self._assert_denied(self.school_admin, self.team_no_mapping)

    def test_team_org_soft_deleted_denied(self):
        """TEAM 组织节点已软删除（映射失效）→ 拒绝。"""
        _soft_delete(self.team_a2_org)
        try:
            self._assert_denied(self.school_admin, self.team_a2)
        finally:
            self.team_a2_org.deleted_at = None
            self.team_a2_org.save()

    def test_team_org_disabled_denied(self):
        """TEAM 组织节点已停用（映射失效）→ 拒绝（deny-by-default）。"""
        self.team_a2_org.enabled = False
        self.team_a2_org.save()
        try:
            self._assert_denied(self.school_admin, self.team_a2)
        finally:
            self.team_a2_org.enabled = True
            self.team_a2_org.save()

    def test_account_without_org_denied(self):
        """账号无组织绑定（codes=[]）→ 即使球队映射完好也拒绝。"""
        self._assert_denied(self.no_org_account, self.team_a1)

    def test_team_role_account_own_team_allowed_sibling_denied(self):
        """球队端账号绑定本队 TEAM 节点：本队允许，同校兄弟队拒绝。"""
        ensure_team_in_scope(self.team_account, self.team_a1)  # 不抛
        self._assert_denied(self.team_account, self.team_a2)
