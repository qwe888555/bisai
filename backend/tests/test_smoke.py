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
