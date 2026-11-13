"""FACE_COMPARE 机器比对链路测试（不依赖真实 face 服务与模型权重）。

覆盖：
- services.create_check 的 FACE_COMPARE 分流（MATCH / MISMATCH / UNAVAILABLE）
- 平台 FACE_MODE=MANUAL 时拒绝 FACE_COMPARE
- 参考照解析优先级（RosterItem → Person → 空）
- face_client.compare 的 HTTP 成功/5xx/连不上/参考照缺失四条失败安全路径
- 接口端到端一条（真实 JWT + 幂等头）
"""

from __future__ import annotations

import io
from unittest import mock

import httpx
from django.test import TestCase

from apps.attachments.models import FileRecord
from apps.attachments.storage import get_storage
from apps.common.errors import ApiError
from apps.enrollments.models import Enrollment, RosterItem
from apps.events.models import Match
from apps.persons.models import Person
from apps.verification import face_client, services
from apps.verification.models import IdentityCheck

BASE = "/api/v1/identity-checks"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 40


class FaceCompareTestCase(TestCase):
    def setUp(self):
        self.storage = get_storage()
        self.match = Match.objects.create(
            id="mat_face_t",
            event_id="evt_face",
            group_id="grp_face",
            home_team_id="team_h",
            away_team_id="team_a",
            kickoff_at="2026-06-01T10:00:00+08:00",
        )
        self.enrollment = Enrollment.objects.create(
            id="enr_face_t",
            event_id="evt_face",
            group_id="grp_face",
            team_id="team_h",
            subject_type="SCHOOL_TEAM",
            status="APPROVED",
            contact_name="联系人",
        )
        self.roster = RosterItem.objects.create(
            id="rst_face_t",
            enrollment=self.enrollment,
            athlete_id="ath_face_1",
            athlete_name="测试球员",
        )
        self.capture = self._make_file(
            "file_cap_t", "MATCH", self.match.id, "CAPTURE_PHOTO"
        )
        self.reference = self._make_file(
            "file_ref_t", "PERSON", "ath_face_1", "ROSTER_PHOTO"
        )
        self.roster.photo_file_id = self.reference.id
        self.roster.save()

    def _make_file(self, fid: str, owner_type: str, owner_id: str, category: str):
        key = "key_" + fid
        self.storage.save(key, io.BytesIO(JPEG))
        return FileRecord.objects.create(
            id=fid,
            owner_type=owner_type,
            owner_id=owner_id,
            category=category,
            original_name=fid + ".jpg",
            content_type="image/jpeg",
            size_bytes=len(JPEG),
            storage_key=key,
            uploaded_by="tester",
        )

    def _payload(self, **overrides) -> dict:
        data = {
            "matchId": self.match.id,
            "enrollmentId": self.enrollment.id,
            "athleteId": self.roster.athlete_id,
            "capturedPhotoFileId": self.capture.id,
            "mode": "FACE_COMPARE",
        }
        data.update(overrides)
        return data

    # -------------------------------------------------- services 分流
    def test_create_check_match(self):
        outcome = {
            "machine_status": "MATCH",
            "machine_verified": True,
            "machine_distance": 0.12,
            "machine_reason": "同一人（距离不超过阈值）",
        }
        with (
            mock.patch("apps.common.conf.FACE_MODE", "FACE_COMPARE"),
            mock.patch(
                "apps.verification.face_client.resolve_reference_photo_file_id",
                return_value=self.reference.id,
            ),
            mock.patch("apps.verification.face_client.compare", return_value=outcome),
        ):
            check = services.create_check(mock.Mock(), self._payload())

        self.assertEqual(check.mode, "FACE_COMPARE")
        self.assertEqual(check.machine_status, "MATCH")
        self.assertIs(check.machine_verified, True)
        self.assertEqual(check.machine_distance, 0.12)
        self.assertEqual(check.machine_reason, outcome["machine_reason"])
        self.assertEqual(check.manual_status, "NOT_REVIEWED")

    def test_create_check_mismatch(self):
        outcome = {
            "machine_status": "MISMATCH",
            "machine_verified": False,
            "machine_distance": 0.91,
            "machine_reason": "不是同一人（距离超过阈值）",
        }
        with (
            mock.patch("apps.common.conf.FACE_MODE", "FACE_COMPARE"),
            mock.patch(
                "apps.verification.face_client.resolve_reference_photo_file_id",
                return_value=self.reference.id,
            ),
            mock.patch("apps.verification.face_client.compare", return_value=outcome),
        ):
            check = services.create_check(mock.Mock(), self._payload())

        self.assertEqual(check.machine_status, "MISMATCH")
        self.assertIs(check.machine_verified, False)
        self.assertEqual(check.machine_distance, 0.91)

    def test_create_check_unavailable_still_persists(self):
        outcome = {
            "machine_status": "UNAVAILABLE",
            "machine_verified": False,
            "machine_distance": None,
            "machine_reason": "face 服务不可用：ConnectError",
        }
        with (
            mock.patch("apps.common.conf.FACE_MODE", "FACE_COMPARE"),
            mock.patch(
                "apps.verification.face_client.resolve_reference_photo_file_id",
                return_value=self.reference.id,
            ),
            mock.patch("apps.verification.face_client.compare", return_value=outcome),
        ):
            check = services.create_check(mock.Mock(), self._payload())

        self.assertEqual(check.machine_status, "UNAVAILABLE")
        self.assertIs(check.machine_verified, False)
        self.assertIsNone(check.machine_distance)
        self.assertEqual(IdentityCheck.objects.count(), 1)

    def test_face_compare_rejected_when_platform_manual(self):
        with mock.patch("apps.common.conf.FACE_MODE", "MANUAL"):
            with self.assertRaises(ApiError) as ctx:
                services.create_check(mock.Mock(), self._payload())
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(IdentityCheck.objects.count(), 0)

    # -------------------------------------------------- 参考照解析
    def test_resolve_reference_roster_first(self):
        resolved = face_client.resolve_reference_photo_file_id(
            self.enrollment.id, self.roster.athlete_id
        )
        self.assertEqual(resolved, self.reference.id)

    def test_resolve_reference_person_fallback(self):
        self.roster.photo_file_id = ""
        self.roster.save()
        person = Person.objects.create(
            person_no="P-FACE-1",
            full_name="测试球员",
            person_category="ATHLETE",
            photo_file_id="file_person_photo",
        )
        resolved = face_client.resolve_reference_photo_file_id(
            self.enrollment.id, person.pk
        )
        self.assertEqual(resolved, "file_person_photo")

    def test_resolve_reference_empty(self):
        self.roster.photo_file_id = ""
        self.roster.save()
        # Person 主键是 BigAutoField，athlete_id 实际存数字字符串；用不存在的数字 id
        resolved = face_client.resolve_reference_photo_file_id(
            self.enrollment.id, "999999"
        )
        self.assertEqual(resolved, "")

    # -------------------------------------------------- face_client HTTP
    def _patch_client(self, handler):
        return mock.patch(
            "apps.verification.face_client._build_client",
            lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_face_client_success(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "machineStatus": "MATCH",
                    "machineVerified": True,
                    "distance": 0.1,
                    "threshold": 0.3,
                    "reason": "同一人",
                },
            )

        with self._patch_client(handler):
            out = face_client.compare(self.capture.id, self.reference.id)
        self.assertEqual(out["machine_status"], "MATCH")
        self.assertIs(out["machine_verified"], True)
        self.assertEqual(out["machine_distance"], 0.1)

    def test_face_client_http_500_unavailable(self):
        def handler(request):
            return httpx.Response(500, text="internal error")

        with self._patch_client(handler):
            out = face_client.compare(self.capture.id, self.reference.id)
        self.assertEqual(out["machine_status"], "UNAVAILABLE")
        self.assertIs(out["machine_verified"], False)

    def test_face_client_connect_error_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        with self._patch_client(handler):
            out = face_client.compare(self.capture.id, self.reference.id)
        self.assertEqual(out["machine_status"], "UNAVAILABLE")
        self.assertIn("face 服务不可用", out["machine_reason"])

    def test_face_client_missing_reference_unavailable(self):
        out = face_client.compare(self.capture.id, "")
        self.assertEqual(out["machine_status"], "UNAVAILABLE")
        self.assertIn("报名参考照片缺失", out["machine_reason"])

    # -------------------------------------------------- 接口端到端
    def test_api_face_compare_endpoint(self):
        from apps.accounts.models import Account
        from apps.common.authentication import issue_access_token, make_password
        from apps.common.ids import new_account_id

        recorder = Account.objects.create(
            id=new_account_id(),
            username="face-recorder",
            roles=["MATCH_RECORDER"],
            password=make_password("recorder-pass-123"),
            enabled=True,
        )
        token = issue_access_token(recorder)[0]
        outcome = {
            "machine_status": "MATCH",
            "machine_verified": True,
            "machine_distance": 0.12,
            "machine_reason": "同一人",
        }
        with (
            mock.patch("apps.common.conf.FACE_MODE", "FACE_COMPARE"),
            mock.patch(
                "apps.verification.face_client.resolve_reference_photo_file_id",
                return_value=self.reference.id,
            ),
            mock.patch("apps.verification.face_client.compare", return_value=outcome),
        ):
            response = self.client.post(
                BASE,
                data=__import__("json").dumps(self._payload()),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {token}",
                HTTP_IDEMPOTENCY_KEY="face-api-key-1",
            )

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["machineStatus"], "MATCH")
        self.assertIs(data["machineVerified"], True)
        self.assertEqual(data["machineDistance"], 0.12)

    # -------------------------------------------------- 参考照探测接口
    def test_api_reference_photo_roster(self):
        from apps.accounts.models import Account
        from apps.common.authentication import issue_access_token, make_password
        from apps.common.ids import new_account_id

        recorder = Account.objects.create(
            id=new_account_id(),
            username="ref-recorder",
            roles=["MATCH_RECORDER"],
            password=make_password("recorder-pass-123"),
            enabled=True,
        )
        token = issue_access_token(recorder)[0]
        response = self.client.get(
            f"{BASE}/reference-photo",
            {"enrollmentId": self.enrollment.id, "athleteId": self.roster.athlete_id},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIs(data["available"], True)
        self.assertEqual(data["source"], "ROSTER")
        self.assertEqual(data["fileId"], self.reference.id)

    def test_api_reference_photo_person_fallback(self):
        self.roster.photo_file_id = ""
        self.roster.save()
        person = Person.objects.create(
            person_no="P-FACE-2",
            full_name="测试球员二",
            person_category="ATHLETE",
            photo_file_id=self.reference.id,
        )
        out = services.reference_photo(self.enrollment.id, person.pk)
        self.assertIs(out["available"], True)
        self.assertEqual(out["source"], "PERSON")

    def test_reference_photo_none(self):
        self.roster.photo_file_id = ""
        self.roster.save()
        out = services.reference_photo(self.enrollment.id, "999999")
        self.assertIs(out["available"], False)
        self.assertEqual(out["source"], "NONE")
        self.assertEqual(out["fileId"], "")

    def test_reference_photo_dangling_file(self):
        """名单里写了 fileId 但文件记录已删：探测必须判不可用，不能骗前端说可用。"""
        self.roster.photo_file_id = ""
        self.roster.save()
        person = Person.objects.create(
            person_no="P-FACE-3",
            full_name="测试球员三",
            person_category="ATHLETE",
            photo_file_id="file_gone",
        )
        out = services.reference_photo(self.enrollment.id, person.pk)
        self.assertIs(out["available"], False)
        self.assertEqual(out["source"], "PERSON")
