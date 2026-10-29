"""accounts 域现状锁定测试（SQLite 内存库，不触碰 MySQL）。

目的：在组织域/权限域修复（决策包 B1/B2）之前，为鉴权链、权限点、
组织数据范围的**当前实际行为**建立回归基线。

运行方式：
    $env:DATABASE_URL="sqlite:///:memory:"; python manage.py test apps.accounts

原则：
- 每条断言均先核对真实代码（api.py / services.py / scopes.py /
  permissions.py / authentication.py / errors.py）后编写，只锁定现状；
- 标注「已知缺陷基线」的用例记录的是决策清单已登记的缺陷行为，
  后续修复时**必须反转断言**，不得把缺陷当作正确安全行为保留；
- 不修改任何生产代码；不写入真实数据库。
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from apps.accounts.models import Account, AuditLog, Organization, RegistrationInvitation
from apps.common import enums
from apps.common.authentication import issue_access_token, make_password
from apps.common.crypto import encrypt_text, lookup_hash, mask_id_number
from apps.common.ids import new_account_id, new_organization_id
from apps.persons.models import Person

BASE = "/api/v1"
PASSWORD = "Pass-12345678"

# 全套运行时同一 IP 的登录次数会超过 30/min（限流键存于进程级 LocMemCache，
# 跨测试用例累积），这里仅在测试内抬高阈值以隔离该干扰；限流行为本身由
# middleware 覆盖，不属于本文件的锁定目标。
@override_settings(
    THROTTLE_RATES={
        "login": "1000000/min",
        "registration": "1000000/min",
        "policy_question": "1000000/min",
    }
)
class AccountScopeTestBase(TestCase):
    """两棵组织树 + 多角色账号的基础数据。

    树 1：P(省) → C(市) → S(学校) → T(球队)；C2(市, 挂 P)
    树 2：Q(省) → Q1(市)   —— 用于范围外对照
    """

    @classmethod
    def setUpTestData(cls):
        cls.P = cls._org("P000", "测试省", "PROVINCE", None)
        cls.C = cls._org("C000", "测试市", "CITY", cls.P)
        cls.S = cls._org("S000", "测试学校", "UNIVERSITY", cls.C)
        cls.T = cls._org("T000", "测试球队", "TEAM", cls.S)
        cls.C2 = cls._org("C200", "邻市", "CITY", cls.P)
        cls.Q = cls._org("Q000", "外省", "PROVINCE", None)
        cls.Q1 = cls._org("Q100", "外省市", "CITY", cls.Q)

        cls.admin = cls._account("scope-admin", ["ADMIN"], cls.P)
        cls.prov_rev = cls._account("scope-prov", ["PROVINCE_REVIEWER"], cls.P)
        cls.city_u = cls._account("scope-city", ["CITY"], cls.C)
        cls.peer_city = cls._account("scope-city-peer", ["CITY"], cls.C)
        cls.city_far = cls._account("scope-city-far", ["CITY"], cls.C2)
        cls.school_u = cls._account("scope-school", ["UNIVERSITY"], cls.S)
        cls.rootless_admin = cls._account("scope-rootless-admin", ["ADMIN"], None)
        cls.outsider = cls._account("scope-outsider", ["UNIVERSITY"], cls.Q1)

    # ------------------------------------------------------------ 数据工具

    @staticmethod
    def _org(code: str, name: str, org_type: str, parent) -> Organization:
        return Organization.objects.create(
            id=new_organization_id(),
            code=code,
            name=name,
            org_type=org_type,
            parent=parent,
        )

    @staticmethod
    def _account(username: str, roles: list, org) -> Account:
        return Account.objects.create(
            id=new_account_id(),
            username=username,
            display_name=username,
            roles=roles,
            organization=org,
            password=make_password(PASSWORD),
            enabled=True,
        )

    # ------------------------------------------------------------ 请求工具

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _post(self, path, payload, token=None):
        return self.client.post(
            path,
            data=self._json(payload),
            content_type="application/json",
            **self._auth(token),
        )

    def _get(self, path, token=None, query=None):
        return self.client.get(path, data=query or {}, **self._auth(token))

    def _put(self, path, payload, token=None):
        return self.client.put(
            path,
            data=self._json(payload),
            content_type="application/json",
            **self._auth(token),
        )

    def _delete(self, path, token=None, query=None):
        # 注意：Django test client 的 delete(data=...) 是请求体而非查询串，
        # version 等查询参数必须拼进 URL。
        if query:
            from urllib.parse import urlencode

            path = f"{path}?{urlencode(query)}"
        return self.client.delete(path, **self._auth(token))

    @staticmethod
    def _json(payload) -> str:
        import json

        return json.dumps(payload)

    def _login(self, username: str, password: str = PASSWORD) -> str:
        resp = self._post(f"{BASE}/auth/login", {"username": username, "password": password})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]["accessToken"]

    def _org_body(self, code: str, name: str, org_type: str, parent_id: str = "", **extra) -> dict:
        body = {"name": name, "orgType": org_type, "code": code, "enabled": True}
        if parent_id:
            body["parentId"] = parent_id
        body.update(extra)
        return body


# ================================================================ 认证与令牌


class AuthFlowTests(AccountScopeTestBase):
    """登录、JWT 头校验、令牌撤销的现状。"""

    def test_login_success_payload(self):
        resp = self._post(
            f"{BASE}/auth/login", {"username": "scope-admin", "password": PASSWORD}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertTrue(data["accessToken"])
        self.assertGreater(data["expiresIn"], 0)
        self.assertEqual(data["accountId"], self.admin.pk)
        self.assertEqual(data["roles"], ["ADMIN"])
        self.assertNotIn("mustChangePassword", data)

    def test_login_unified_error_no_enumeration(self):
        """账号不存在与密码错误返回完全一致的 401，防账号枚举。"""
        msgs = []
        for username, password in (("no-such-user", PASSWORD), ("scope-admin", "Wrong-Pass-999")):
            resp = self._post(f"{BASE}/auth/login", {"username": username, "password": password})
            self.assertEqual(resp.status_code, 401)
            body = resp.json()
            self.assertEqual(body["code"], "UNAUTHENTICATED")
            self.assertEqual(body["message"], "账号或密码错误")
            msgs.append(body["message"])
        self.assertEqual(len(set(msgs)), 1)

    def test_login_disabled_account_403(self):
        self.peer_city.enabled = False
        self.peer_city.save(update_fields=["enabled"])
        resp = self._post(
            f"{BASE}/auth/login", {"username": "scope-city-peer", "password": PASSWORD}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_me_without_header_401(self):
        resp = self._get(f"{BASE}/me")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "UNAUTHENTICATED")

    def test_me_non_bearer_header_401(self):
        resp = self.client.get(f"{BASE}/me", HTTP_AUTHORIZATION="Token abc")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Bearer", resp.json()["message"])

    def test_me_with_garbage_token_401(self):
        resp = self._get(f"{BASE}/me", token="not-a-jwt")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "UNAUTHENTICATED")

    def test_me_success_payload(self):
        resp = self._get(f"{BASE}/me", token=self._login("scope-admin"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["username"], "scope-admin")
        self.assertEqual(data["orgId"], self.P.pk)
        self.assertIn("api:accountList", data["permissions"])
        self.assertIn("api:organizationList", data["permissions"])
        menu_keys = {m["key"] for m in data["menus"]}
        self.assertIn("account", menu_keys)
        self.assertIn("organization", menu_keys)

    def test_me_athlete_has_no_org(self):
        resp = self._get(f"{BASE}/me", token=self._login("scope-outsider"))
        self.assertEqual(resp.status_code, 200)

    def test_me_disabled_account_with_valid_token_403(self):
        """有效令牌 + 停用账号：load_account 返回 403 而非 401。"""
        token, _ = issue_access_token(self.peer_city)
        self.peer_city.enabled = False
        self.peer_city.save(update_fields=["enabled"])
        resp = self._get(f"{BASE}/me", token=token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_logout_revokes_only_current_token(self):
        token = self._login("scope-city")
        resp = self._post(f"{BASE}/auth/logout", {}, token=token)
        self.assertEqual(resp.status_code, 200)
        resp = self._get(f"{BASE}/me", token=token)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "TOKEN_REVOKED")

    def test_change_password_rejects_bad_old_and_same(self):
        token = self._login("scope-school")
        resp = self._post(
            f"{BASE}/me/password",
            {"oldPassword": "Wrong-Pass-999", "newPassword": "NewPass-123456"},
            token=token,
        )
        self.assertEqual(resp.status_code, 400)
        resp = self._post(
            f"{BASE}/me/password",
            {"oldPassword": PASSWORD, "newPassword": PASSWORD},
            token=token,
        )
        self.assertEqual(resp.status_code, 400)

    def test_change_password_success_revokes_old_tokens(self):
        old_token = self._login("scope-school")
        resp = self._post(
            f"{BASE}/me/password",
            {"oldPassword": PASSWORD, "newPassword": "NewPass-123456"},
            token=old_token,
        )
        self.assertEqual(resp.status_code, 200)

        resp = self._get(f"{BASE}/me", token=old_token)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "TOKEN_REVOKED")

        resp = self._post(
            f"{BASE}/auth/login",
            {"username": "scope-school", "password": "NewPass-123456"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_created_account_must_change_password_on_first_login(self):
        """管理员建号初始密码=用户名，首登返回 mustChangePassword 标记。"""
        token = self._login("scope-admin")
        resp = self._post(
            f"{BASE}/accounts",
            {
                "username": "fresh-001",
                "displayName": "新账号",
                "roles": ["UNIVERSITY"],
                "orgId": self.S.pk,
            },
            token=token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        resp = self._post(
            f"{BASE}/auth/login", {"username": "fresh-001", "password": "fresh-001"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["data"].get("mustChangePassword"))


# ================================================================ 权限点与角色


class PermissionPointTests(AccountScopeTestBase):
    """角色 → 权限点第二层的现状（require）。"""

    def test_athlete_forbidden_on_account_list(self):
        resp = self._get(f"{BASE}/accounts", token=self._login("scope-outsider"))
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body["code"], "FORBIDDEN")
        self.assertIn("当前角色无权执行", body["message"])
        self.assertEqual(body["data"][0]["field"], "role")

    def test_province_reviewer_has_account_list_permission(self):
        resp = self._get(f"{BASE}/accounts", token=self._login("scope-prov"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], "OK")

    def test_organization_list_admin_only(self):
        resp = self._get(f"{BASE}/organizations", token=self._login("scope-prov"))
        self.assertEqual(resp.status_code, 403)
        resp = self._get(f"{BASE}/organizations", token=self._login("scope-admin"))
        self.assertEqual(resp.status_code, 200)


# ================================================================ 组织数据范围


class DataScopeTests(AccountScopeTestBase):
    """org_scope_ids / scoped_accounts / ensure_org_in_scope 现状。"""

    def test_account_list_scoped_to_own_subtree(self):
        """省级账号可见本省整棵子树（含两市一校），看不见外省账号。"""
        resp = self._get(f"{BASE}/accounts", token=self._login("scope-prov"))
        self.assertEqual(resp.status_code, 200)
        usernames = {a["username"] for a in resp.json()["data"]["items"]}
        self.assertIn("scope-admin", usernames)
        self.assertIn("scope-city", usernames)
        self.assertIn("scope-school", usernames)
        self.assertIn("scope-city-far", usernames)
        self.assertNotIn("scope-outsider", usernames)

    def test_city_role_has_no_account_list_permission(self):
        """现状锁定：accountList 权限点仅 ADMIN/PROVINCE_REVIEWER，市级 403。"""
        resp = self._get(f"{BASE}/accounts", token=self._login("scope-city"))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_city_scope_filters_accounts_at_service_layer(self):
        """服务层锁定：CITY 账号的可见账号集合=本组织子树（含本校，不含省/邻市/外省）。"""
        from apps.accounts.services import scoped_accounts

        usernames = {a.username for a in scoped_accounts(self.city_u)}
        self.assertIn("scope-city-peer", usernames)
        self.assertIn("scope-school", usernames)
        self.assertNotIn("scope-admin", usernames)  # 上级省账号不可见
        self.assertNotIn("scope-city-far", usernames)  # 邻市不可见
        self.assertNotIn("scope-outsider", usernames)

    def test_get_account_out_of_scope_returns_404(self):
        """现状：范围外详情表现为 NOT_FOUND（先 scope 过滤再查询），非 403。"""
        resp = self._get(f"{BASE}/accounts/{self.outsider.pk}", token=self._login("scope-prov"))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_create_account_out_of_scope_org_403_data_scope_denied(self):
        """持 accountCreate 点的省级账号越界建号 → DATA_SCOPE_DENIED。"""
        token = self._login("scope-prov")
        resp = self._post(
            f"{BASE}/accounts",
            {
                "username": "invader-001",
                "displayName": "越界账号",
                "roles": ["UNIVERSITY"],
                "orgId": self.Q1.pk,  # 外省组织
            },
            token=token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        self.assertFalse(Account.objects.filter(username="invader-001").exists())

    def test_city_role_cannot_create_account(self):
        """现状锁定：CITY 无 accountCreate 权限点，角色层 403。"""
        resp = self._post(
            f"{BASE}/accounts",
            {
                "username": "city-created",
                "displayName": "市级建号",
                "roles": ["UNIVERSITY"],
                "orgId": self.S.pk,
            },
            token=self._login("scope-city"),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_non_admin_cannot_grant_admin_role(self):
        """PROVINCE_REVIEWER 持有 accountCreate 点但不得授予 ADMIN 角色。"""
        token = self._login("scope-prov")
        resp = self._post(
            f"{BASE}/accounts",
            {
                "username": "elevate-01",
                "displayName": "提权尝试",
                "roles": ["ADMIN"],
                "orgId": self.C.pk,
            },
            token=token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")
        self.assertIn("管理员", resp.json()["message"])

    def test_cannot_operate_self(self):
        token = self._login("scope-admin")
        resp = self._delete(
            f"{BASE}/accounts/{self.admin.pk}", token=token, query={"version": 0}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("不能对本账号", resp.json()["message"])

    def test_cannot_manage_peer_level_account(self):
        """同市同角色账号互不管辖（level 相等且非 admin）。"""
        token = self._login("scope-city")
        resp = self._delete(
            f"{BASE}/accounts/{self.peer_city.pk}", token=token, query={"version": 0}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_province_can_reset_city_account_password(self):
        """正向层级：省级(80) 可重置市级(60) 密码。"""
        token = self._login("scope-prov")
        resp = self._post(
            f"{BASE}/accounts/{self.city_u.pk}/password-reset",
            {"version": 0, "newPassword": "Reset-Pass-123", "forceChangeOnNextLogin": True},
            token=token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        resp = self._post(
            f"{BASE}/auth/login", {"username": "scope-city", "password": "Reset-Pass-123"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["data"].get("mustChangePassword"))

    def test_organization_list_scoped_to_subtree(self):
        """organizationList 仅 ADMIN：返回其所属组织整棵子树，不含外省。"""
        resp = self._get(f"{BASE}/organizations", token=self._login("scope-admin"))
        self.assertEqual(resp.status_code, 200)
        codes = {o["code"] for o in resp.json()["data"]["items"]}
        self.assertEqual(codes, {"P000", "C000", "S000", "T000", "C200"})
        self.assertNotIn("Q000", codes)

    def test_get_organization_out_of_scope_returns_404(self):
        resp = self._get(f"{BASE}/organizations/{self.Q1.pk}", token=self._login("scope-admin"))
        self.assertEqual(resp.status_code, 404)

    def test_audit_logs_non_admin_role_has_no_permission(self):
        """现状锁定：auditList 权限点仅 ADMIN/PROVINCE_REVIEWER，其他角色 403。"""
        resp = self._get(f"{BASE}/audit-logs", token=self._login("scope-city"))
        self.assertEqual(resp.status_code, 403)

    def test_province_audit_log_scope_at_service_layer(self):
        """服务层锁定：非 admin 查审计日志仅见范围内账号的操作日志。"""
        from apps.accounts.services import list_audit_logs

        AuditLog.objects.create(
            id="log_in_scope", operator_account_id=self.city_u.pk, action="op.in"
        )
        AuditLog.objects.create(
            id="log_out_scope", operator_account_id=self.outsider.pk, action="op.out"
        )
        rows = list(list_audit_logs(self.prov_rev, {}))
        ids = {r.pk for r in rows}
        self.assertIn("log_in_scope", ids)
        self.assertNotIn("log_out_scope", ids)


# ================================================================ 组织管理现状（含已知缺陷基线）


class OrganizationBehaviorTests(AccountScopeTestBase):
    """组织 CRUD 的当前正式行为基线（origin/main 68e0d4d 收口语义）。"""

    def _admin_token(self):
        return self._login("scope-admin")

    def test_create_and_soft_delete_organization(self):
        token = self._admin_token()
        resp = self._post(
            f"{BASE}/organizations",
            self._org_body("Z900", "临时组织", "UNIVERSITY", parent_id=self.C.pk),
            token=token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        org_id = resp.json()["data"]["id"]

        resp = self._delete(f"{BASE}/organizations/{org_id}", token=token, query={"version": 0})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Organization.objects.filter(pk=org_id).exists())  # 软删后默认管理器不可见
        self.assertTrue(Organization.all_objects.filter(pk=org_id).exists())

    def test_delete_blocked_by_children_or_accounts(self):
        token = self._admin_token()
        resp = self._delete(f"{BASE}/organizations/{self.C.pk}", token=token, query={"version": 0})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")

    def test_duplicate_code_rejected(self):
        token = self._admin_token()
        resp = self._post(
            f"{BASE}/organizations",
            self._org_body("C000", "重复编码", "CITY", parent_id=self.P.pk),
            token=token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "DUPLICATE_RESOURCE")

    def test_self_parent_rejected_on_update(self):
        token = self._admin_token()
        resp = self._put(
            f"{BASE}/organizations/{self.C.pk}",
            self._org_body("C000", "测试市", "CITY", parent_id=self.C.pk, version=0),
            token=token,
        )
        self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------ 当前正式实现语义基线
    # 以下用例锁定收编后的正式行为（origin/main 提交 68e0d4d「组织与管理员收口」：
    # 层级矩阵、停用父级、成环检测、软删复活，见 apps/accounts/services.py 的
    # ORG_PARENT_TYPES/_validate_org_parent/create_organization，以及
    # apps/common/scopes.py 的 is_global_admin），替换收编前钉住旧行为的缺陷基线断言。

    def test_soft_deleted_org_code_reuse_revives_same_node(self):
        """软删编码复用：复活原节点，而非新建或报错。

        create_organization 以 all_objects 全量查重（services.py:400-412）：
        命中软删行时经 _apply_org_fields 复活原记录——ID 不变、deleted_at
        清空、版本递增、parent 按本次请求落库；历史子节点（含软删子）仍指向
        同一节点 ID；不产生第二条同编码记录。
        """
        token = self._admin_token()
        resp = self._post(
            f"{BASE}/organizations",
            self._org_body("D900", "待删组织", "CITY", parent_id=self.P.pk),
            token=token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        org_id = resp.json()["data"]["id"]
        v0 = Organization.all_objects.get(pk=org_id).version

        # 先造一个子节点再软删，使 D900 可删；软删子仍保留对 D900 的指向
        resp = self._post(
            f"{BASE}/organizations",
            self._org_body("D901", "临时子校", "UNIVERSITY", parent_id=org_id),
            token=token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        child_id = resp.json()["data"]["id"]
        resp = self._delete(f"{BASE}/organizations/{child_id}", token=token, query={"version": 0})
        self.assertEqual(resp.status_code, 200)
        resp = self._delete(f"{BASE}/organizations/{org_id}", token=token, query={"version": 0})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(Organization.all_objects.get(pk=org_id).deleted_at)

        resp = self._post(
            f"{BASE}/organizations",
            self._org_body("D900", "复用编码", "CITY", parent_id=self.P.pk),
            token=token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["id"], org_id)  # 复活原节点：ID 保持
        revived = Organization.all_objects.get(pk=org_id)
        self.assertIsNone(revived.deleted_at)  # deleted_at 清空
        self.assertGreater(revived.version, v0)  # 版本递增
        self.assertEqual(revived.parent_id, self.P.pk)  # parent 按本次请求落库
        self.assertEqual(
            Organization.all_objects.filter(code="D900").count(), 1
        )  # 不产生第二条同编码记录
        child = Organization.all_objects.get(pk=child_id)
        self.assertIsNotNone(child.deleted_at)  # 软删子不复活
        self.assertEqual(child.parent_id, org_id)  # 子节点关系保留（指向原 ID）

    def test_update_rejects_type_mismatched_parent(self):
        """非法层级组合在更新时被拒（层级矩阵），且无部分写入。

        ORG_PARENT_TYPES 规定 CITY 只能挂 PROVINCE（services.py:299-305）；
        把 CITY 组织的父改为其 UNIVERSITY 子节点属非法组合，返回 400，
        请求在 _apply_org_fields 之前被拦，原父子关系与类型保持不变。
        """
        token = self._admin_token()
        org_a = self._org("EA00", "环甲", "CITY", self.P)
        org_b = self._org("EB00", "环乙", "UNIVERSITY", org_a)
        resp = self._put(
            f"{BASE}/organizations/{org_a.pk}",
            self._org_body("EA00", "环甲", "CITY", parent_id=org_b.pk, version=0),
            token=token,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        org_a.refresh_from_db()
        self.assertEqual(org_a.parent_id, self.P.pk)  # 无部分写入
        self.assertEqual(org_a.org_type, "CITY")

    def test_update_cycle_detected_with_legal_parent_type(self):
        """合法父子类型组合下经类型变更更新形成环：环检测拦截。

        链路 P→A(CITY)→B(UNIVERSITY) 合法；更新 A 为 TEAM 并挂到子孙 B
        ——TEAM 挂 UNIVERSITY 属合法组合，层级矩阵放行后由
        _validate_org_parent 的祖先游走命中成环检测（services.py:356-366），
        返回 400 且 A 不发生任何写入。
        """
        token = self._admin_token()
        org_a = self._org("EC10", "环甲", "CITY", self.P)
        org_b = self._org("EC20", "环乙", "UNIVERSITY", org_a)
        resp = self._put(
            f"{BASE}/organizations/{org_a.pk}",
            self._org_body("EC10", "环甲", "TEAM", parent_id=org_b.pk, version=0),
            token=token,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        org_a.refresh_from_db()
        self.assertEqual(org_a.parent_id, self.P.pk)  # 无部分写入
        self.assertEqual(org_a.org_type, "CITY")  # 类型未变更
        self.assertEqual(org_b.parent_id, org_a.pk)  # 子节点关系未变

    def test_disabled_parent_rejects_attach_and_create(self):
        """停用父级：挂子与建子均被拒（409），不产生任何子节点数据。

        _validate_org_parent 校验 parent.enabled（services.py:350-355），
        创建与更新共用同一校验。
        """
        token = self._admin_token()
        disabled = self._org("ED00", "停用市", "CITY", self.P)
        resp = self._put(
            f"{BASE}/organizations/{disabled.pk}",
            self._org_body("ED00", "停用市", "CITY", parent_id=self.P.pk, enabled=False, version=0),
            token=token,
        )
        self.assertEqual(resp.status_code, 200)

        other = self._org("EO00", "其他校", "UNIVERSITY", self.C)
        resp = self._put(
            f"{BASE}/organizations/{other.pk}",
            self._org_body("EO00", "其他校", "UNIVERSITY", parent_id=disabled.pk, version=0),
            token=token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        other.refresh_from_db()
        self.assertEqual(other.parent_id, self.C.pk)  # 无部分写入

        resp = self._post(
            f"{BASE}/organizations",
            self._org_body("EN00", "新建子校", "UNIVERSITY", parent_id=disabled.pk),
            token=token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertEqual(
            Organization.all_objects.filter(code="EN00").count(), 0
        )  # 数据库未产生子节点
        self.assertEqual(
            Organization.all_objects.filter(parent_id=disabled.pk).count(), 0
        )

    def test_illegal_parent_type_combo_rejected(self):
        """越级组合（TEAM 挂 PROVINCE）创建被拒（400），库中无残留。

        ORG_PARENT_TYPES["TEAM"] = {"SCHOOL", "UNIVERSITY"}，省级不在允许集
        （services.py:299-305、344-349）；校验先于落库执行。
        """
        token = self._admin_token()
        before = Organization.all_objects.count()
        resp = self._post(
            f"{BASE}/organizations",
            self._org_body("EC00", "越级球队", "TEAM", parent_id=self.P.pk),
            token=token,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertEqual(Organization.all_objects.count(), before)  # 库状态不变
        self.assertEqual(Organization.all_objects.filter(code="EC00").count(), 0)

    def test_rootless_admin_reads_globally_and_scoped_roles_stay_in_subtree(self):
        """无组织 ADMIN（is_global_admin）全库可见；挂组织 ADMIN 与普通角色按子树。

        common/scopes.py:35-40：ADMIN 且无所属组织 → 全局读写；挂组织的
        ADMIN 仍按组织子树限定（避免“读空写通”）。有 accountList 权限的
        普通角色（PROVINCE_REVIEWER）同样按组织子树过滤，不获得全库访问。
        """
        resp = self._get(f"{BASE}/accounts", token=self._login("scope-rootless-admin"))
        self.assertEqual(resp.status_code, 200)
        global_usernames = {it["username"] for it in resp.json()["data"]["items"]}
        self.assertEqual(
            global_usernames,
            {
                "scope-admin",
                "scope-prov",
                "scope-city",
                "scope-city-peer",
                "scope-city-far",
                "scope-school",
                "scope-rootless-admin",
                "scope-outsider",
            },
        )  # 全库可见（含范围外账号）

        resp = self._get(f"{BASE}/accounts", token=self._login("scope-admin"))
        self.assertEqual(resp.status_code, 200)
        subtree_usernames = {it["username"] for it in resp.json()["data"]["items"]}
        self.assertEqual(
            subtree_usernames,
            {
                "scope-admin",
                "scope-prov",
                "scope-city",
                "scope-city-peer",
                "scope-city-far",
                "scope-school",
            },
        )  # 挂组织 ADMIN 限定在 P 子树
        self.assertNotIn("scope-outsider", subtree_usernames)
        self.assertNotIn("scope-rootless-admin", subtree_usernames)

        resp = self._get(f"{BASE}/accounts", token=self._login("scope-prov"))
        self.assertEqual(resp.status_code, 200)
        reviewer_usernames = {it["username"] for it in resp.json()["data"]["items"]}
        self.assertEqual(reviewer_usernames, subtree_usernames)  # 普通角色同样按子树，非全库

    def test_org_scope_roles_and_plain_roles_share_behavior(self):
        """现状锁定：ORG_SCOPE_ROLES 与非树角色的 scope 行为完全相同。

        scopes.org_scope_ids 两个分支返回同样的子树集合（代码异味），
        学校账号（UNIVERSITY）同样按组织子树过滤——修复前不可依赖该
        常量做行为区分。
        """
        from apps.accounts.services import scoped_accounts

        school_visible = {a.username for a in scoped_accounts(self.school_u)}
        self.assertIn("scope-school", school_visible)  # 本校自己
        self.assertNotIn("scope-admin", school_visible)  # 省级账号不可见
        self.assertNotIn("scope-city", school_visible)  # 上级市账号不可见


# ================================================================ 幽灵 personId 绑定防线


@override_settings(
    THROTTLE_RATES={
        "login": "1000000/min",
        "registration": "1000000/min",
        "policy_question": "1000000/min",
    }
)
class PersonBindingTests(AccountScopeTestBase):
    """非空 personId 绑定目标必须存在且未软删（services._ensure_person_bindable）。

    覆盖账号创建 / 账号更新（解绑、重绑）/ 邀请签发 / 邀请激活四条链路；
    每条用例均验证数据库最终状态（无部分写入），而非仅断言 HTTP 状态码。
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.person_a = cls._person("110101200001010011", "绑定甲")
        cls.person_b = cls._person("110101200001010022", "绑定乙")
        cls.person_dead = cls._person("110101200001010033", "已删人", deleted=True)

    # ------------------------------------------------------------ 数据工具

    @staticmethod
    def _person(id_number: str, name: str, deleted: bool = False) -> Person:
        return Person.objects.create(
            person_no=f"P2026{id_number[-6:]}",
            full_name=name,
            id_card_cipher=encrypt_text(id_number),
            id_card_hash=lookup_hash(id_number),
            id_card_masked=mask_id_number(id_number),
            person_category=enums.PersonCategory.ATHLETE,
            is_deleted=deleted,
        )

    def _account_body(self, username: str, person_id: str = "", **extra) -> dict:
        # personId 为空时必须省略键：schema 对显式空串按 min_length=1 拒绝
        body = {
            "username": username,
            "displayName": username,
            "roles": ["UNIVERSITY"],
            "orgId": self.S.pk,
        }
        if person_id:
            body["personId"] = person_id
        body.update(extra)
        return body

    def _create_account(self, username: str, person_id: str = ""):
        token = self._login("scope-admin")
        resp = self._post(
            f"{BASE}/accounts", self._account_body(username, person_id), token=token
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return Account.objects.get(username=username)

    def _invite(self, person_id: str = "") -> dict:
        """以 scope-admin 签发邀请，返回响应 json。"""
        token = self._login("scope-admin")
        payload = {"kind": "ATHLETE", "orgId": self.S.pk}
        if person_id:
            payload["personId"] = person_id
        return self._post(f"{BASE}/accounts/registration-invitations", payload, token=token)

    def _register(self, token: str, username: str):
        return self._post(
            f"{BASE}/auth/registrations",
            {
                "registrationToken": token,
                "username": username,
                "password": PASSWORD,
                "realName": "激活人",
                "idNumber": "110101201305011234",
            },
        )

    # ------------------------------------------------------------ 账号创建

    def test_create_account_with_unknown_person_rejected(self):
        """绑定不存在的 Person → 400，且不产生部分写入。"""
        token = self._login("scope-admin")
        resp = self._post(
            f"{BASE}/accounts", self._account_body("ghost-001", "99999999"), token=token
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertEqual(resp.json()["data"][0]["field"], "personId")
        self.assertFalse(Account.objects.filter(username="ghost-001").exists())

    def test_create_account_with_non_numeric_person_rejected(self):
        """Person 主键为数值型：非数值 personId 不可能命中，同样 400。"""
        token = self._login("scope-admin")
        resp = self._post(
            f"{BASE}/accounts", self._account_body("ghost-002", "not-a-person"), token=token
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertFalse(Account.objects.filter(username="ghost-002").exists())

    def test_create_account_with_soft_deleted_person_rejected(self):
        """绑定已软删 Person → 400，且不产生部分写入。"""
        token = self._login("scope-admin")
        resp = self._post(
            f"{BASE}/accounts",
            self._account_body("ghost-003", str(self.person_dead.pk)),
            token=token,
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertEqual(resp.json()["data"][0]["field"], "personId")
        self.assertFalse(Account.objects.filter(username="ghost-003").exists())

    def test_create_account_with_valid_person_binds(self):
        """绑定有效 Person → 成功落库 person_id。"""
        token = self._login("scope-admin")
        resp = self._post(
            f"{BASE}/accounts",
            self._account_body("bound-001", str(self.person_a.pk)),
            token=token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["personId"], str(self.person_a.pk))
        account = Account.objects.get(username="bound-001")
        self.assertEqual(account.person_id, str(self.person_a.pk))

    # ------------------------------------------------------------ 账号更新

    def test_update_account_unbind_then_rebind_other_person(self):
        """PUT 解绑（缺省 personId）→ 清空；再绑其他有效 Person → 落库。"""
        account = self._create_account("rebind-001", str(self.person_a.pk))

        # 解绑：PUT 整份替换且未提交 personId → person_id 清空
        resp = self._put(
            f"{BASE}/accounts/{account.pk}",
            self._account_body("rebind-001", version=account.version),
            token=self._login("scope-admin"),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        account.refresh_from_db()
        self.assertEqual(account.person_id, "")

        # 重绑其他有效 Person
        resp = self._put(
            f"{BASE}/accounts/{account.pk}",
            self._account_body(
                "rebind-001", str(self.person_b.pk), version=account.version
            ),
            token=self._login("scope-admin"),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        account.refresh_from_db()
        self.assertEqual(account.person_id, str(self.person_b.pk))

    def test_update_account_rebind_person_bound_elsewhere_rejected(self):
        """重绑已被其他账号占用的人员 → 409（既有唯一绑定规则），原绑定不变。"""
        self._create_account("dup-001", str(self.person_a.pk))
        other = self._create_account("dup-002")

        resp = self._put(
            f"{BASE}/accounts/{other.pk}",
            self._account_body(
                "dup-002", str(self.person_a.pk), version=other.version
            ),
            token=self._login("scope-admin"),
        )
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["code"], "DUPLICATE_RESOURCE")
        other.refresh_from_db()
        self.assertEqual(other.person_id, "")  # 无部分写入

    def test_update_account_with_unknown_person_rejected(self):
        """更新时绑定幽灵 personId → 400，原绑定不被破坏。"""
        account = self._create_account("upd-ghost", str(self.person_a.pk))

        resp = self._put(
            f"{BASE}/accounts/{account.pk}",
            self._account_body("upd-ghost", "99999999", version=account.version),
            token=self._login("scope-admin"),
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        account.refresh_from_db()
        self.assertEqual(account.person_id, str(self.person_a.pk))  # 无部分写入

    # ------------------------------------------------------------ 邀请签发

    def test_invitation_with_unknown_person_rejected(self):
        """签发绑定不存在人员 → 400，不落邀请凭证。"""
        resp = self._invite("99999999")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertEqual(resp.json()["data"][0]["field"], "personId")
        self.assertFalse(RegistrationInvitation.objects.exists())

    def test_invitation_with_soft_deleted_person_rejected(self):
        """签发绑定已软删人员 → 400，不落邀请凭证。"""
        resp = self._invite(str(self.person_dead.pk))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertFalse(RegistrationInvitation.objects.exists())

    def test_invitation_with_valid_person_created(self):
        """签发绑定有效人员 → 成功落库 person_id。"""
        resp = self._invite(str(self.person_a.pk))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("registrationToken", resp.json()["data"])
        invitation = RegistrationInvitation.objects.get()
        self.assertEqual(invitation.person_id, str(self.person_a.pk))

    # ------------------------------------------------------------ 邀请激活

    def test_register_with_valid_person_binds_account(self):
        """邀请激活：有效 personId 正常建号并绑定。"""
        resp = self._invite(str(self.person_a.pk))
        plain_token = resp.json()["data"]["registrationToken"]

        resp = self._register(plain_token, "reg-001")
        self.assertEqual(resp.status_code, 200, resp.content)
        account = Account.objects.get(username="reg-001")
        self.assertEqual(account.person_id, str(self.person_a.pk))
        invitation = RegistrationInvitation.objects.get()
        self.assertIsNotNone(invitation.used_at)

    def test_register_revalidates_person_deleted_after_invitation(self):
        """签发后人员被软删 → 激活时 400 拒绝建号（服务端重验），无部分写入。"""
        resp = self._invite(str(self.person_a.pk))
        plain_token = resp.json()["data"]["registrationToken"]

        self.person_a.is_deleted = True
        self.person_a.save(update_fields=["is_deleted", "updated_at"])

        resp = self._register(plain_token, "reg-002")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertEqual(resp.json()["data"][0]["field"], "personId")
        self.assertFalse(Account.objects.filter(username="reg-002").exists())
        # 凭证未被消费
        invitation = RegistrationInvitation.objects.get()
        self.assertIsNone(invitation.used_at)

    def test_register_with_ghost_person_on_invitation_rejected(self):
        """凭证上本就挂着幽灵 personId（绕过签发校验直插库）→ 激活仍被拒。"""
        from datetime import timedelta

        from apps.common import crypto
        from django.utils import timezone as tz

        RegistrationInvitation.objects.create(
            id="inv-ghost-001",
            token_hash=crypto.hash_token("a" * 64),
            kind="ATHLETE",
            person_id="99999999",
            organization_id=self.S.pk,
            expires_at=tz.now() + timedelta(minutes=30),
            created_by="scope-admin",
        )
        resp = self._register("a" * 64, "reg-003")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertFalse(Account.objects.filter(username="reg-003").exists())
