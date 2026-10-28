import json

from django.test import Client, TestCase, override_settings

from selection.enums import GroupType, PublicityType, RoundType
from selection.models import PublicityRecord, ResultBatch, ResultItem
from selection.services.publicity import mask_id_card
from selection.services.publicity_source import get_publicity_source_client

from .utils import api_prefix

API_PREFIX = api_prefix()
PUBLICITY_PATH = f"{API_PREFIX}publicity/"


@override_settings(
    SELECTION_AUTH_MODE="none",
    SELECTION_ENVELOPE_MODE="native",
    SELECTION_ARCHIVE_MODE="demo",
    SELECTION_PUBLICITY_SOURCE_MODE="demo",
)
class PublicityApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.headers = {"HTTP_X_USER_ID": "99"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def post_json(self, path, payload):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **self.headers,
        )

    def get_json(self, path, params=None):
        return self.client.get(path, data=params or {}, **self.headers)

    @staticmethod
    def body(response):
        return json.loads(response.content.decode("utf-8"))

    def publish(self, publicity_type, source_id, event_id=1):
        response = self.post_json(
            PUBLICITY_PATH,
            {
                "event_id": event_id,
                "publicity_type": publicity_type,
                "source_id": source_id,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        result = self.body(response)
        self.assertEqual(result["code"], 0)
        return result["data"]

    def cancel(self, record_id, reason="报名信息有误"):
        response = self.post_json(
            f"{PUBLICITY_PATH}{record_id}/cancel/",
            {"reason": reason},
        )
        self.assertEqual(response.status_code, 200, response.content)
        return self.body(response)["data"]

    def public_list(self, **params):
        response = self.get_json(PUBLICITY_PATH, params)
        self.assertEqual(response.status_code, 200, response.content)
        return self.body(response)["data"]

    # ------------------------------------------------------------------
    # Publish / masking / whitelist
    # ------------------------------------------------------------------
    def test_publish_athlete_masks_id_card_and_applies_whitelist(self):
        source = get_publicity_source_client().get_registration(
            PublicityType.ATHLETE, "10001"
        )
        raw_id_card = source.data["id_card"]

        data = self.publish("ATHLETE", "10001")

        self.assertEqual(data["action"], "published")
        self.assertEqual(data["name"], "张伟")
        self.assertEqual(data["group_type"], "MIDDLE")
        self.assertEqual(data["group_type_label"], "中学组")
        self.assertEqual(data["publicity_type_label"], "运动员")
        self.assertEqual(data["city"], "江城市")
        self.assertEqual(
            data["id_card_masked"],
            f"{raw_id_card[:6]}********{raw_id_card[-4:]}",
        )
        self.assertNotEqual(data["id_card_masked"], raw_id_card)
        self.assertNotIn("phone", data["fields"])
        self.assertNotIn("birth_date", data["fields"])
        self.assertEqual(data["fields"]["jersey_number"], 1)
        self.assertEqual(
            set(data["fields"]),
            {
                "name",
                "gender",
                "team_name",
                "school_name",
                "position",
                "jersey_number",
            },
        )

        # Raw ID card must not appear in the response or the saved snapshot.
        self.assertNotIn(
            raw_id_card, json.dumps(data, ensure_ascii=False)
        )
        record = PublicityRecord.objects.get(
            publicity_type="ATHLETE", source_id="10001"
        )
        self.assertNotIn(
            raw_id_card,
            json.dumps(record.profile_json, ensure_ascii=False),
        )
        self.assertNotIn(
            raw_id_card,
            json.dumps(
                [
                    record.subject_name,
                    record.team_name,
                    record.school_name,
                    record.id_card_masked,
                ],
                ensure_ascii=False,
            ),
        )

    def test_publish_coach_and_team(self):
        coach = self.publish("COACH", "C1001")
        self.assertEqual(coach["publicity_type_label"], "教练员")
        self.assertEqual(coach["name"], "陈志强")
        self.assertTrue(coach["id_card_masked"])
        self.assertEqual(coach["fields"]["coach_role"], "主教练")
        self.assertNotIn("phone", coach["fields"])

        team = self.publish("TEAM", "T1001")
        self.assertEqual(team["publicity_type_label"], "球队")
        self.assertEqual(team["name"], "江城一中")
        self.assertEqual(team["team_name"], "江城一中")
        self.assertEqual(team["id_card_masked"], "")
        self.assertEqual(team["fields"]["coach_name"], "陈志强")
        self.assertNotIn("contact_phone", team["fields"])
        self.assertNotIn("team_type", team["fields"])

    @override_settings(
        SELECTION_PUBLICITY_FIELD_WHITELIST={
            "ATHLETE": ("name", "id_card", "team_name"),
        }
    )
    def test_field_whitelist_is_configurable(self):
        data = self.publish("ATHLETE", "10001")
        self.assertEqual(set(data["fields"]), {"name", "team_name"})
        self.assertNotIn("position", data["fields"])
        self.assertTrue(data["id_card_masked"])

    def test_mask_id_card_for_18_and_15_digits(self):
        self.assertEqual(
            mask_id_card("510101200001010011"),
            "510101********0011",
        )
        self.assertEqual(
            mask_id_card("510101200001011"),
            "510101******011",
        )
        self.assertEqual(mask_id_card(""), "")

    # ------------------------------------------------------------------
    # Cancel / visibility
    # ------------------------------------------------------------------
    def test_duplicate_cancel_and_republish(self):
        first = self.publish("ATHLETE", "10002")

        response = self.post_json(
            PUBLICITY_PATH,
            {
                "event_id": 1,
                "publicity_type": "ATHLETE",
                "source_id": "10002",
            },
        )
        self.assertEqual(response.status_code, 409)

        listing = self.public_list(event_id=1)
        self.assertEqual(listing["total"], 1)

        cancelled = self.cancel(first["id"])
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(cancelled["status_label"], "已取消")
        self.assertEqual(cancelled["cancel_reason"], "报名信息有误")
        self.assertFalse(cancelled["already_cancelled"])

        # Portal cannot see it immediately after cancellation.
        listing = self.public_list(event_id=1)
        self.assertEqual(listing["total"], 0)
        self.assertEqual(listing["records"], [])

        response = self.get_json(f"{PUBLICITY_PATH}{first['id']}/")
        self.assertEqual(response.status_code, 404)

        # CMS can still see and audit the cancelled record.
        response = self.get_json(
            f"{PUBLICITY_PATH}manage/",
            {"event_id": 1, "status": "CANCELLED"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        managed = self.body(response)["data"]
        self.assertEqual(managed["total"], 1)
        self.assertEqual(managed["records"][0]["cancel_reason"], "报名信息有误")

        response = self.get_json(
            f"{PUBLICITY_PATH}manage/{first['id']}/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.body(response)["data"]["status"], "CANCELLED"
        )

        # Repeated cancel is idempotent.
        again = self.cancel(first["id"], reason="重复点击")
        self.assertEqual(again["status"], "CANCELLED")
        self.assertTrue(again["already_cancelled"])

        # Re-publishing the same registration is allowed and clears cancel data.
        republished = self.publish("ATHLETE", "10002")
        self.assertEqual(republished["action"], "republished")
        self.assertEqual(republished["status"], "PUBLISHED")
        self.assertEqual(republished["cancel_reason"], "")
        self.assertIsNone(republished["cancelled_at"])
        self.assertEqual(self.public_list(event_id=1)["total"], 1)

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    def test_filters_by_group_city_and_type(self):
        self.publish("ATHLETE", "10001")  # MIDDLE, 江城市
        self.publish("ATHLETE", "10002")  # MIDDLE, 南湖市
        self.publish("ATHLETE", "20001")  # PRIMARY, 江城市

        middle = self.public_list(event_id=1, group_type="MIDDLE")
        self.assertEqual(middle["total"], 2)

        primary = self.public_list(event_id=1, group_type="小学组")
        self.assertEqual(primary["total"], 1)
        self.assertEqual(primary["records"][0]["name"], "郭鹏")

        south_lake = self.public_list(
            event_id=1, group_type="MIDDLE", city="南湖市"
        )
        self.assertEqual(south_lake["total"], 1)
        self.assertEqual(south_lake["records"][0]["name"], "王芳")

        only_athletes = self.public_list(event_id=1, type="运动员")
        self.assertEqual(only_athletes["total"], 3)

        response = self.get_json(
            f"{PUBLICITY_PATH}cities/",
            {"event_id": 1, "group_type": "MIDDLE"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            set(self.body(response)["data"]), {"江城市", "南湖市"}
        )

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------
    def test_batch_publish_is_idempotent(self):
        payload = {
            "event_id": 1,
            "publicity_type": "ATHLETE",
            "group_type": "MIDDLE",
        }
        response = self.post_json(f"{PUBLICITY_PATH}batch/", payload)
        self.assertEqual(response.status_code, 200, response.content)
        first = self.body(response)["data"]
        self.assertEqual(first["published"], 15)
        self.assertEqual(first["skipped"], 0)
        self.assertEqual(first["failed"], 0)
        self.assertEqual(len(first["records"]), 15)

        response = self.post_json(f"{PUBLICITY_PATH}batch/", payload)
        self.assertEqual(response.status_code, 200, response.content)
        second = self.body(response)["data"]
        self.assertEqual(second["published"], 0)
        self.assertEqual(second["skipped"], 15)
        self.assertEqual(second["failed"], 0)

        self.assertEqual(
            PublicityRecord.objects.filter(
                event_id=1,
                group_type="MIDDLE",
                publicity_type="ATHLETE",
            ).count(),
            15,
        )
        self.assertEqual(
            self.public_list(
                event_id=1, group_type="MIDDLE", type="ATHLETE"
            )["total"],
            15,
        )

    # ------------------------------------------------------------------
    # Shortlist -> publicity bridge
    # ------------------------------------------------------------------
    def _result_batch(self, candidate_ids, active=True, stale=False):
        batch = ResultBatch.objects.create(
            event_id="1",
            group_type=GroupType.MIDDLE,
            result_round=RoundType.INITIAL,
            quota=len(candidate_ids),
            initial_weight=1,
            final_weight=0,
            formula_snapshot="测试",
            version_no=99,
            active=active,
            stale=stale,
            generated_by="99",
        )
        for rank, candidate_id in enumerate(candidate_ids, start=1):
            ResultItem.objects.create(
                batch=batch,
                rank_no=rank,
                candidate_id=candidate_id,
                candidate_name=f"候选{candidate_id}",
                composite_score=90,
            )
        return batch

    def test_publish_result_batch_only_publishes_shortlisted_athletes(self):
        batch = self._result_batch(["10001", "10002", "10003"])

        response = self.post_json(
            f"{PUBLICITY_PATH}result-batch/",
            {"result_batch_id": batch.id},
        )
        self.assertEqual(response.status_code, 200, response.content)
        data = self.body(response)["data"]
        self.assertEqual(data["candidate_count"], 3)
        self.assertEqual(data["published"], 3)
        self.assertEqual(data["skipped"], 0)
        self.assertEqual(data["failed"], 0)
        self.assertEqual(
            set(
                PublicityRecord.objects.values_list(
                    "source_id", flat=True
                )
            ),
            {"10001", "10002", "10003"},
        )

        # Repeated calls must skip, not duplicate.
        response = self.post_json(
            f"{PUBLICITY_PATH}result-batch/",
            {"result_batch_id": batch.id},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.body(response)["data"]["skipped"], 3)
        self.assertEqual(PublicityRecord.objects.count(), 3)

    def test_publish_result_batch_rejects_stale_batch(self):
        batch = self._result_batch(["10001"], stale=True)

        response = self.post_json(
            f"{PUBLICITY_PATH}result-batch/",
            {"result_batch_id": batch.id},
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(PublicityRecord.objects.count(), 0)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def test_validation_errors(self):
        response = self.post_json(
            PUBLICITY_PATH,
            {
                "event_id": 1,
                "publicity_type": "UNKNOWN",
                "source_id": "10001",
            },
        )
        self.assertEqual(response.status_code, 400)

        response = self.post_json(
            PUBLICITY_PATH,
            {
                "event_id": 1,
                "publicity_type": "ATHLETE",
                "source_id": "99999",
            },
        )
        self.assertEqual(response.status_code, 404)

        response = self.post_json(
            PUBLICITY_PATH,
            {
                "event_id": 2,
                "publicity_type": "ATHLETE",
                "source_id": "10001",
            },
        )
        self.assertEqual(response.status_code, 400)

        response = self.post_json(
            PUBLICITY_PATH,
            {"publicity_type": "ATHLETE", "source_id": "10001"},
        )
        self.assertEqual(response.status_code, 400)

        response = self.post_json(
            f"{PUBLICITY_PATH}batch/", {"event_id": 1}
        )
        self.assertEqual(response.status_code, 400)
