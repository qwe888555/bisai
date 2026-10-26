"""接口冒烟：正常路径 + 400/401/403/404/409 + 幂等 + 文件 + 字典缓存。

运行：cd backend && python -m pytest
"""

from __future__ import annotations

import base64
import json

from django.core.management import call_command
from django.test import TestCase

BASE = "/api/v1"
PASSWORD = "Foot@2026"

# 只校验文件头的最小 PNG，够服务端嗅探使用
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

# 真正的 1x1 PNG，用于验证 Pillow 缩略图
REAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def body(response) -> dict:
    return json.loads(response.content.decode("utf-8"))


class ApiSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)

    # ---------------------------------------------------------------- 工具
    def post(self, path: str, payload: dict, token: str | None = None, **headers):
        auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(
            f"{BASE}{path}",
            data=json.dumps(payload),
            content_type="application/json",
            **auth,
            **headers,
        )

    def get(self, path: str, token: str | None = None):
        auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(f"{BASE}{path}", **auth)

    def login(self, username: str = "admin", password: str = PASSWORD) -> str:
        response = self.post("/auth/login", {"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.content)
        return body(response)["data"]["accessToken"]

    def upload_capture_photo(self, match_id: str, token: str) -> str:
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post(
            f"{BASE}/files",
            data={
                "ownerType": "MATCH",
                "ownerId": match_id,
                "category": "CAPTURE_PHOTO",
                "file": SimpleUploadedFile("capture.png", PNG_BYTES, "image/png"),
            },
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return body(response)["data"]["id"]

    # ---------------------------------------------------------------- 基础
    def test_health_ok(self):
        response = self.client.get(f"{BASE}/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body(response)["code"], "OK")

    def test_login_and_me(self):
        token = self.login()
        response = self.get("/me", token)
        self.assertEqual(response.status_code, 200)
        data = body(response)["data"]
        self.assertEqual(data["username"], "admin")
        self.assertIn("ADMIN", data["roles"])
        self.assertTrue(data["permissions"])

    def test_wrong_password_is_401_envelope(self):
        response = self.post("/auth/login", {"username": "admin", "password": "Wrong@2026"})
        self.assertEqual(response.status_code, 401)
        payload = body(response)
        self.assertEqual(payload["code"], "UNAUTHENTICATED")
        self.assertIn("requestId", payload)

    def test_missing_token_is_401_envelope(self):
        response = self.get("/accounts")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(body(response)["code"], "UNAUTHENTICATED")

    def test_unknown_path_is_404_envelope(self):
        response = self.get("/not-exists")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(body(response)["code"], "NOT_FOUND")

    def test_extra_field_is_400_invalid_argument(self):
        response = self.post(
            "/auth/login",
            {"username": "admin", "password": PASSWORD, "unexpected": 1},
        )
        self.assertEqual(response.status_code, 400)
        payload = body(response)
        self.assertEqual(payload["code"], "INVALID_ARGUMENT")
        fields = [item["field"] for item in payload["data"]]
        self.assertIn("unexpected", fields)

    # ---------------------------------------------------------------- 权限
    def test_coach_cannot_list_accounts(self):
        token = self.login("coach")
        response = self.get("/accounts", token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(body(response)["code"], "FORBIDDEN")

    # ---------------------------------------------------------------- 分页
    def test_accounts_pagination_shape(self):
        token = self.login()
        response = self.get("/accounts?page=1&pageSize=3", token)
        self.assertEqual(response.status_code, 200)
        data = body(response)["data"]
        self.assertEqual(set(data), {"items", "page", "pageSize", "total"})
        self.assertEqual(data["pageSize"], 3)
        self.assertLessEqual(len(data["items"]), 3)
        self.assertGreaterEqual(data["total"], 8)  # seed_demo 的 8 个账号

    # ---------------------------------------------------------------- 乐观锁
    def test_organization_version_conflict_is_409(self):
        token = self.login()
        response = self.client.put(
            f"{BASE}/organizations/org_prov",
            data=json.dumps(
                {
                    "name": "省教育厅",
                    "orgType": "PROVINCE",
                    "code": "PROV",
                    "version": 999,
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(body(response)["code"], "VERSION_CONFLICT")

    # ---------------------------------------------------------------- 幂等
    def test_identity_idempotency_replay_and_conflict(self):
        token = self.login()
        photo_id = self.upload_capture_photo("match_demo_1", token)
        payload = {
            "matchId": "match_demo_1",
            "enrollmentId": "enr_1",
            "athleteId": "ath_1",
            "capturedPhotoFileId": photo_id,
            "mode": "MANUAL",
        }
        key = "idem-key-1234567890"
        first = self.post("/identity-checks", payload, token, HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(first.status_code, 200, first.content)
        first_id = body(first)["data"]["id"]

        replay = self.post("/identity-checks", payload, token, HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(body(replay)["data"]["id"], first_id)

        changed = dict(payload, athleteId="ath_2")
        conflict = self.post(
            "/identity-checks", changed, token, HTTP_IDEMPOTENCY_KEY=key
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(body(conflict)["code"], "IDEMPOTENCY_CONFLICT")

    # ---------------------------------------------------------------- 文件
    def test_file_upload_and_private_download(self):
        token = self.login()
        file_id = self.upload_capture_photo("match_demo_2", token)

        response = self.get(f"/files/{file_id}/content", token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"\x89PNG"))

    def test_image_thumbnail_generated_and_served(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        token = self.login()
        response = self.client.post(
            f"{BASE}/files",
            data={
                "ownerType": "MATCH",
                "ownerId": "match_demo_thumb",
                "category": "CAPTURE_PHOTO",
                "file": SimpleUploadedFile("photo.png", REAL_PNG, "image/png"),
            },
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = body(response)["data"]
        self.assertEqual(data["thumbnailUrl"], f"/files/{data['id']}/thumbnail")

        thumb = self.get(f"/files/{data['id']}/thumbnail", token)
        self.assertEqual(thumb.status_code, 200)
        self.assertEqual(thumb["Content-Type"], "image/jpeg")
        self.assertEqual(thumb["Cache-Control"], "private, no-store")

    def test_bad_extension_is_415(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        token = self.login()
        response = self.client.post(
            f"{BASE}/files",
            data={
                "ownerType": "MATCH",
                "ownerId": "match_demo_3",
                "category": "CAPTURE_PHOTO",
                "file": SimpleUploadedFile("capture.gif", b"GIF89a", "image/gif"),
            },
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(body(response)["code"], "UNSUPPORTED_FILE_TYPE")

    # ---------------------------------------------------------------- 字典缓存
    def test_dictionary_options_cached_and_invalidated(self):
        token = self.login()
        first = self.get("/dictionaries/GENDER/options", token)
        self.assertEqual(first.status_code, 200)
        original_total = body(first)["data"]["total"]

        created = self.post(
            "/dictionary-items",
            {
                "dictType": "GENDER",
                "value": "OTHER",
                "label": "其他",
                "sortOrder": 9,
                "enabled": True,
            },
            token,
        )
        self.assertEqual(created.status_code, 200, created.content)

        second = self.get("/dictionaries/GENDER/options", token)
        self.assertEqual(body(second)["data"]["total"], original_total + 1)

    # ---------------------------------------------------------------- 组织树
    def create_org(self, token: str, payload: dict) -> dict:
        response = self.post("/organizations", payload, token)
        self.assertEqual(response.status_code, 200, response.content)
        return body(response)["data"]

    def org_by_code(self, code: str):
        from apps.accounts.models import Organization

        return Organization.all_objects.get(code=code)

    def test_team_cannot_hang_under_province(self):
        token = self.login()
        response = self.post(
            "/organizations",
            {
                "name": "越级球队",
                "orgType": "TEAM",
                "code": "BIZ_TEAM_BAD_1",
                "parentId": "org_prov",
            },
            token,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body(response)["code"], "INVALID_ARGUMENT")
        self.assertIn("parentId", [item["field"] for item in body(response)["data"]])

    def test_soft_deleted_code_is_revived(self):
        token = self.login()
        created = self.create_org(
            token,
            {"name": "待撤市州", "orgType": "CITY", "code": "BIZ_CITY_REVIVE", "parentId": "org_prov"},
        )
        deleted = self.client.delete(
            f"{BASE}/organizations/{created['id']}?version={created['version']}",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(deleted.status_code, 200, deleted.content)

        revived = self.create_org(
            token,
            {"name": "恢复市州", "orgType": "CITY", "code": "BIZ_CITY_REVIVE", "parentId": "org_prov"},
        )
        self.assertEqual(revived["id"], created["id"])
        self.assertEqual(self.org_by_code("BIZ_CITY_REVIVE").deleted_at, None)

    def test_organization_cycle_and_disable_are_rejected(self):
        token = self.login()
        city = self.create_org(
            token,
            {"name": "某市", "orgType": "CITY", "code": "BIZ_CITY_CYCLE", "parentId": "org_prov"},
        )
        school = self.create_org(
            token,
            {"name": "某校", "orgType": "SCHOOL", "code": "BIZ_SCHOOL_CYCLE", "parentId": city["id"]},
        )

        cycle = self.client.put(
            f"{BASE}/organizations/{city['id']}",
            data=json.dumps(
                {
                    "name": "某市",
                    "orgType": "CITY",
                    "code": "BIZ_CITY_CYCLE",
                    "parentId": school["id"],
                    "version": city["version"],
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(cycle.status_code, 400, cycle.content)
        self.assertEqual(body(cycle)["code"], "INVALID_ARGUMENT")

        disable = self.client.put(
            f"{BASE}/organizations/{city['id']}",
            data=json.dumps(
                {
                    "name": "某市",
                    "orgType": "CITY",
                    "code": "BIZ_CITY_CYCLE",
                    "parentId": "org_prov",
                    "enabled": False,
                    "version": city["version"],
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(disable.status_code, 409, disable.content)
        self.assertEqual(body(disable)["code"], "STATE_CONFLICT")

    def test_code_is_immutable_on_update(self):
        token = self.login()
        city = self.create_org(
            token,
            {"name": "不可改码市", "orgType": "CITY", "code": "BIZ_CITY_IMMUT", "parentId": "org_prov"},
        )
        response = self.client.put(
            f"{BASE}/organizations/{city['id']}",
            data=json.dumps(
                {
                    "name": "不可改码市",
                    "orgType": "CITY",
                    "code": "BIZ_CITY_RENAMED",
                    "parentId": "org_prov",
                    "version": city["version"],
                }
            ),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body(response)["code"], "INVALID_ARGUMENT")

    def test_ensure_business_org_syncs_and_revives(self):
        from django.db import transaction

        from apps.accounts.models import Organization
        from apps.accounts.services import ensure_business_org

        with transaction.atomic():
            org = ensure_business_org("UNIVERSITY", "BIZ_UNI_ENSURE", "同步高校", "org_prov")
        self.assertEqual(org.org_type, "UNIVERSITY")
        self.assertEqual(org.parent_id, "org_prov")

        with transaction.atomic():
            again = ensure_business_org("UNIVERSITY", "BIZ_UNI_ENSURE", "同步高校", "org_prov")
        self.assertEqual(again.pk, org.pk)

        Organization.objects.filter(pk=org.pk).update(deleted_at="2026-01-01T00:00:00Z")
        with transaction.atomic():
            revived = ensure_business_org("UNIVERSITY", "BIZ_UNI_ENSURE", "同步高校", "org_prov")
        self.assertEqual(revived.pk, org.pk)
        self.assertIsNone(revived.deleted_at)

    def test_global_admin_without_org_sees_everything(self):
        from apps.accounts.models import Account
        from apps.accounts.services import scoped_accounts
        from apps.common.ids import new_account_id

        admin = Account.objects.create(
            id=new_account_id(),
            username="global_admin_case",
            display_name="全局管理员",
            roles=["ADMIN"],
            organization=None,
            password="x",
        )
        self.assertEqual(scoped_accounts(admin).count(), Account.objects.count())

    def test_ensure_business_org_type_conflict_and_rollback(self):
        from django.db import transaction

        from apps.accounts.models import Organization
        from apps.accounts.services import ensure_business_org
        from apps.common.errors import ApiError
        from apps.common.ids import new_account_id
        from apps.accounts.models import Account

        token = self.login()
        city = self.create_org(
            token,
            {"name": "挂靠市", "orgType": "CITY", "code": "BIZ_CITY_FOR_ENSURE", "parentId": "org_prov"},
        )

        with transaction.atomic():
            ensure_business_org("UNIVERSITY", "BIZ_CONFLICT_CODE", "冲突高校", "org_prov")

        with self.assertRaises(ApiError) as caught:
            with transaction.atomic():
                ensure_business_org("SCHOOL", "BIZ_CONFLICT_CODE", "冲突学校", city["id"])
        self.assertEqual(caught.exception.code, "STATE_CONFLICT")

        # 组织创建失败时，同事务内的业务记录必须一起回滚
        username = "rollback_case"
        try:
            with transaction.atomic():
                Account.objects.create(
                    id=new_account_id(),
                    username=username,
                    display_name="回滚用例",
                    roles=["COACH"],
                    password="x",
                )
                ensure_business_org("TEAM", "BIZ_ROLLBACK_TEAM", "回滚球队", "org_prov")
        except ApiError:
            pass
        self.assertFalse(Account.all_objects.filter(username=username).exists())
        self.assertFalse(Organization.all_objects.filter(code="BIZ_ROLLBACK_TEAM").exists())

    def test_database_url_decoding_and_mysql_shim(self):
        from config.settings import _database_config

        cfg = _database_config("mysql+pymysql://user:p%40ss@127.0.0.1:3306/football")
        self.assertEqual(cfg["PASSWORD"], "p@ss")
        self.assertEqual(cfg["NAME"], "football")
        self.assertEqual(cfg["PORT"], 3306)

        # pytest 不走 manage.py，PyMySQL 的 shim 必须在 config 包初始化里生效
        import MySQLdb

        self.assertTrue(hasattr(MySQLdb, "connect"))

    def test_policy_document_syncs_on_publish_and_offline(self):
        from apps.events.models import Content
        from apps.events.services import publish_content, unpublish_content
        from apps.policyqa.models import PolicyDocument
        from apps.policyqa.services import answer

        Content.objects.create(
            id="cnt_policy_sync_case",
            content_type="POLICY",
            title="省级足球赛事报名办法",
            summary="报名条件与材料",
            body_html="<p>参赛球员须为本校在籍学生。</p>",
            body_text="参赛球员须为本校在籍学生，报名需提交学籍材料。",
            status="DRAFT",
        )

        publish_content("cnt_policy_sync_case", 0)

        document = PolicyDocument.objects.get(source_content_id="cnt_policy_sync_case")
        self.assertTrue(document.enabled)
        self.assertIsNotNone(document.published_at)

        result = answer("报名需要什么条件")
        self.assertEqual(result["status"], "DISABLED")
        self.assertEqual(len(result["fallbackPolicies"]), 1)
        self.assertEqual(result["fallbackPolicies"][0]["title"], "省级足球赛事报名办法")

        unpublish_content("cnt_policy_sync_case", 1, "测试下线")
        document.refresh_from_db()
        self.assertFalse(document.enabled)
        self.assertEqual(answer("报名需要什么条件")["fallbackPolicies"], [])

    def test_init_admin_command(self):
        from django.core.management import CommandError

        from apps.accounts.models import Account

        call_command("init_admin", username="root_admin", password="Init@123456", verbosity=0)
        account = Account.objects.get(username="root_admin")
        self.assertTrue(account.must_change_password)
        self.assertIsNone(account.organization_id)

        token = self.login("root_admin", "Init@123456")
        me = self.get("/me", token)
        self.assertEqual(me.status_code, 200)

        with self.assertRaises(CommandError):
            call_command("init_admin", username="root_admin", password="Init@123456", verbosity=0)

