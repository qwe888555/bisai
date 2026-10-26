"""verification 接口测试（SQLite 内存库）。

当前覆盖：GET /identity-checks 别名路由（zuqiu 前端 identityApi.list 实际
调用路径，src/api/index.ts L325）与原 /identity-checks/list 等价。
"""

from __future__ import annotations

from django.test import TestCase

from apps.accounts.models import Account
from apps.common.authentication import issue_access_token, make_password
from apps.common.ids import new_account_id

BASE = "/api/v1/identity-checks"


class IdentityCheckListAliasTests(TestCase):
    """GET /identity-checks 与 GET /identity-checks/list 双路由等价。"""

    @classmethod
    def setUpTestData(cls):
        cls.recorder = Account.objects.create(
            id=new_account_id(),
            username="alias-recorder",
            roles=["MATCH_RECORDER"],
            password=make_password("recorder-pass-123"),
            enabled=True,
        )
        cls.token = issue_access_token(cls.recorder)[0]

    def test_alias_route_returns_same_envelope(self):
        root = self.client.get(
            BASE, HTTP_AUTHORIZATION=f"Bearer {self.token}"
        )
        legacy = self.client.get(
            BASE + "/list", HTTP_AUTHORIZATION=f"Bearer {self.token}"
        )
        self.assertEqual(root.status_code, 200, root.content)
        self.assertEqual(legacy.status_code, 200, legacy.content)
        self.assertEqual(root.json()["code"], "OK")
        self.assertEqual(root.json()["data"], legacy.json()["data"])
        self.assertEqual(root.json()["data"]["total"], 0)

    def test_missing_token_returns_401(self):
        resp = self.client.get(BASE)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "UNAUTHENTICATED")
