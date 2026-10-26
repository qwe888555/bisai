import json
from io import BytesIO

from django.test import Client, TestCase, override_settings
from openpyxl import load_workbook

from selection.models import ResultBatch

from .utils import api_prefix

# Standalone: /api/selection/...  Merged: /api/v1/selection/...
API_PREFIX = api_prefix()

TEMPLATE_PATH = f"{API_PREFIX}templates/"
SCORE_PATH = f"{API_PREFIX}scores/"
RESULTS_PATH = f"{API_PREFIX}results/"


@override_settings(
    SELECTION_AUTH_MODE="none",
    SELECTION_ENVELOPE_MODE="native",
    SELECTION_ARCHIVE_MODE="demo",
    SELECTION_PUBLICITY_SOURCE_MODE="demo",
)
class SelectionApiTests(TestCase):
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

    def put_json(self, path, payload):
        return self.client.put(
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

    def create_template(
        self,
        group_type="HIGHER",
        round_code="INITIAL",
        name="测试模板",
        items=None,
    ):
        if items is None:
            items = [
                {
                    "dimension_code": "TECH",
                    "dimension_name": "技术",
                    "weight": 60,
                    "max_score": 100,
                },
                {
                    "dimension_code": "MATCH",
                    "dimension_name": "比赛",
                    "weight": 40,
                    "max_score": 100,
                },
            ]
        response = self.post_json(
            TEMPLATE_PATH,
            {
                "name": name,
                "group_type": group_type,
                "round": round_code,
                "items": items,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        result = self.body(response)
        self.assertEqual(result["code"], 0)
        return result["data"]

    def create_score(
        self,
        template_id,
        candidate_id,
        round_code="INITIAL",
        group_type="HIGHER",
        scores=None,
        event_id=1,
    ):
        if scores is None:
            scores = [
                {"dimension_code": "TECH", "score": 90},
                {"dimension_code": "MATCH", "score": 80},
            ]
        return self.post_json(
            SCORE_PATH,
            {
                "event_id": event_id,
                "group_type": group_type,
                "round": round_code,
                "template_id": template_id,
                "candidate_id": candidate_id,
                "scores": scores,
            },
        )

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------
    def test_create_list_update_template(self):
        initial = self.create_template()
        self.assertEqual(initial["status"], 1)
        self.assertEqual(len(initial["items"]), 2)
        self.assertEqual(initial["group_type"], "HIGHER")

        final = self.create_template(round_code="FINAL", name="复选模板")
        self.assertEqual(final["round"], "FINAL")

        response = self.get_json(
            TEMPLATE_PATH,
            {"group_type": "HIGHER", "round": "INITIAL"},
        )
        listing = self.body(response)["data"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["id"], initial["id"])

        response = self.get_json(f"{TEMPLATE_PATH}{initial['id']}/")
        detail = self.body(response)["data"]
        self.assertEqual(len(detail["items"]), 2)

        new_items = [
            {
                "dimension_code": "TECH",
                "dimension_name": "技术",
                "weight": 50,
                "max_score": 100,
            },
            {
                "dimension_code": "TACTIC",
                "dimension_name": "战术",
                "weight": 50,
                "max_score": 100,
            },
        ]
        response = self.put_json(
            f"{TEMPLATE_PATH}{initial['id']}/",
            {"name": "新名称", "items": new_items},
        )
        self.assertEqual(response.status_code, 200, response.content)
        updated = self.body(response)["data"]
        self.assertEqual(updated["name"], "新名称")
        self.assertEqual(
            [item["dimension_code"] for item in updated["items"]],
            ["TECH", "TACTIC"],
        )

        # Group and round are immutable after creation.
        response = self.put_json(
            f"{TEMPLATE_PATH}{initial['id']}/",
            {"group_type": "MIDDLE"},
        )
        self.assertEqual(response.status_code, 400)

    def test_template_weight_validation(self):
        response = self.post_json(
            TEMPLATE_PATH,
            {
                "name": "错误权重",
                "group_type": "HIGHER",
                "round": "INITIAL",
                "items": [
                    {
                        "dimension_code": "TECH",
                        "dimension_name": "技术",
                        "weight": 60,
                        "max_score": 100,
                    },
                    {
                        "dimension_code": "MATCH",
                        "dimension_name": "比赛",
                        "weight": 30,
                        "max_score": 100,
                    },
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("100", self.body(response)["message"])

        response = self.post_json(
            TEMPLATE_PATH,
            {
                "name": "重复维度",
                "group_type": "HIGHER",
                "round": "INITIAL",
                "items": [
                    {
                        "dimension_code": "TECH",
                        "dimension_name": "技术1",
                        "weight": 50,
                        "max_score": 100,
                    },
                    {
                        "dimension_code": "tech",
                        "dimension_name": "技术2",
                        "weight": 50,
                        "max_score": 100,
                    },
                ],
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_camel_case_input_is_accepted(self):
        response = self.post_json(
            TEMPLATE_PATH,
            {
                "name": "驼峰字段模板",
                "groupType": "HIGHER",
                "round": "INITIAL",
                "items": [
                    {
                        "dimensionCode": "TECH",
                        "dimensionName": "技术",
                        "weight": 100,
                        "maxScore": 100,
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.body(response)["data"]["items"][0]["weight"], 100.0)

    # ------------------------------------------------------------------
    # Scores
    # ------------------------------------------------------------------
    def test_score_entry_and_duplicate(self):
        template = self.create_template()

        response = self.create_score(template["id"], 10001)
        self.assertEqual(response.status_code, 200, response.content)
        score = self.body(response)["data"]
        self.assertAlmostEqual(score["total_score"], 86.0)
        self.assertEqual(score["version"], 0)
        self.assertEqual(score["candidate_name"], "张伟")

        response = self.create_score(template["id"], 10001)
        self.assertEqual(response.status_code, 409)

        response = self.create_score(template["id"], 99999)
        self.assertEqual(response.status_code, 400)

        response = self.create_score(
            template["id"],
            10002,
            scores=[
                {"dimension_code": "TECH", "score": 110},
                {"dimension_code": "MATCH", "score": 80},
            ],
        )
        self.assertEqual(response.status_code, 400)

        response = self.create_score(
            template["id"],
            10002,
            scores=[{"dimension_code": "TECH", "score": 90}],
        )
        self.assertEqual(response.status_code, 400)

    def test_template_round_must_match(self):
        template = self.create_template(round_code="INITIAL")
        response = self.create_score(
            template["id"], 10001, round_code="FINAL"
        )
        self.assertEqual(response.status_code, 400)

    def test_score_update_and_summary(self):
        template = self.create_template()
        first = self.body(self.create_score(template["id"], 10001))["data"]
        second = self.body(
            self.create_score(
                template["id"],
                10002,
                scores=[
                    {"dimension_code": "TECH", "score": 80},
                    {"dimension_code": "MATCH", "score": 70},
                ],
            )
        )["data"]
        self.body(
            self.create_score(
                template["id"],
                10003,
                scores=[
                    {"dimension_code": "TECH", "score": 70},
                    {"dimension_code": "MATCH", "score": 60},
                ],
            )
        )

        response = self.get_json(
            f"{SCORE_PATH}summary/",
            {
                "event_id": 1,
                "group_type": "HIGHER",
                "round": "INITIAL",
            },
        )
        summary = self.body(response)["data"]
        self.assertEqual(
            [item["candidate_id"] for item in summary],
            ["10001", "10002", "10003"],
        )
        self.assertEqual([item["rank_no"] for item in summary], [1, 2, 3])

        response = self.put_json(
            f"{SCORE_PATH}{second['id']}/",
            {
                "scores": [
                    {"dimension_code": "TECH", "score": 100},
                    {"dimension_code": "MATCH", "score": 100},
                ]
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        updated = self.body(response)["data"]
        self.assertEqual(updated["total_score"], 100.0)
        self.assertEqual(updated["version"], 1)

        response = self.get_json(
            f"{SCORE_PATH}summary/",
            {
                "event_id": 1,
                "group_type": "HIGHER",
                "round": "INITIAL",
            },
        )
        summary = self.body(response)["data"]
        self.assertEqual(summary[0]["candidate_id"], "10002")
        self.assertEqual(summary[0]["total_score"], 100.0)

        response = self.get_json(
            SCORE_PATH,
            {
                "event_id": 1,
                "group_type": "HIGHER",
                "round": "INITIAL",
                "page": 1,
                "size": 2,
            },
        )
        page = self.body(response)["data"]
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["records"]), 2)

        # Make sure the first score is still readable and its version is 0.
        self.assertEqual(first["version"], 0)

    def test_summary_tie_break_by_tech(self):
        template = self.create_template(
            items=[
                {
                    "dimension_code": "TECH",
                    "dimension_name": "技术",
                    "weight": 50,
                    "max_score": 100,
                },
                {
                    "dimension_code": "MATCH",
                    "dimension_name": "比赛",
                    "weight": 50,
                    "max_score": 100,
                },
            ]
        )
        self.create_score(
            template["id"],
            10001,
            scores=[
                {"dimension_code": "TECH", "score": 90},
                {"dimension_code": "MATCH", "score": 70},
            ],
        )
        self.create_score(
            template["id"],
            10002,
            scores=[
                {"dimension_code": "TECH", "score": 80},
                {"dimension_code": "MATCH", "score": 80},
            ],
        )

        response = self.get_json(
            f"{SCORE_PATH}summary/",
            {
                "event_id": 1,
                "group_type": "HIGHER",
                "round": "INITIAL",
            },
        )
        summary = self.body(response)["data"]
        self.assertEqual(summary[0]["total_score"], 80.0)
        self.assertEqual(summary[1]["total_score"], 80.0)
        self.assertEqual(summary[0]["candidate_id"], "10001")

    def test_group_alias_in_query(self):
        template = self.create_template()
        self.create_score(template["id"], 10001)
        response = self.client.get(
            f"{SCORE_PATH}summary/",
            data={
                "event_id": 1,
                "group_type": "高校组",
                "round": "初选",
            },
            **self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.body(response)["data"]), 1)

        # The main backend calls this category "UNIVERSITY"; it maps to HIGHER.
        response = self.client.get(
            f"{SCORE_PATH}summary/",
            data={
                "event_id": 1,
                "group_type": "UNIVERSITY",
                "round": "INITIAL",
            },
            **self.headers,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.body(response)["data"]), 1)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    def _seed_two_rounds(self):
        initial_template = self.create_template(round_code="INITIAL")
        final_template = self.create_template(round_code="FINAL")

        initial_values = [
            (10001, 90, 80),
            (10002, 80, 70),
            (10003, 70, 60),
        ]
        final_values = [
            (10001, 95, 85),
            (10002, 85, 75),
            (10003, 75, 65),
        ]

        initial_scores = {}
        for candidate_id, tech, match in initial_values:
            response = self.create_score(
                initial_template["id"],
                candidate_id,
                round_code="INITIAL",
                scores=[
                    {"dimension_code": "TECH", "score": tech},
                    {"dimension_code": "MATCH", "score": match},
                ],
            )
            initial_scores[candidate_id] = self.body(response)["data"]

        for candidate_id, tech, match in final_values:
            self.create_score(
                final_template["id"],
                candidate_id,
                round_code="FINAL",
                scores=[
                    {"dimension_code": "TECH", "score": tech},
                    {"dimension_code": "MATCH", "score": match},
                ],
            )

        return initial_scores

    def test_result_generation_export_stale_and_history(self):
        initial_scores = self._seed_two_rounds()

        response = self.post_json(
            f"{RESULTS_PATH}generate/",
            {
                "event_id": 1,
                "group_type": "HIGHER",
                "quota": 2,
                "result_round": "FINAL",
                "initial_weight": 0.4,
                "final_weight": 0.6,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        batch = self.body(response)["data"]
        self.assertEqual(batch["version_no"], 1)
        self.assertFalse(batch["stale"])
        self.assertEqual(len(batch["items"]), 2)
        self.assertEqual(
            [item["candidate_id"] for item in batch["items"]],
            ["10001", "10002"],
        )
        self.assertAlmostEqual(
            batch["items"][0]["composite_score"], 89.0
        )
        self.assertEqual(
            batch["items"][0]["detail"]["initial"]["total"], 86.0
        )
        self.assertEqual(
            batch["items"][0]["detail"]["final"]["total"], 91.0
        )

        response = self.get_json(
            f"{RESULTS_PATH}latest/",
            {"event_id": 1, "group_type": "HIGHER"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.body(response)["data"]["version_no"], 1)

        response = self.get_json(
            f"{RESULTS_PATH}export/",
            {"event_id": 1, "group_type": "HIGHER"},
        )
        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content))
        sheet = workbook.active
        self.assertEqual(sheet.title, "遴选结果")
        self.assertEqual(sheet.cell(row=1, column=1).value, "排名")
        self.assertEqual(sheet.cell(row=2, column=1).value, 1)

        # Editing any score invalidates the active result batch.
        score = initial_scores[10001]
        response = self.put_json(
            f"{SCORE_PATH}{score['id']}/",
            {
                "scores": [
                    {"dimension_code": "TECH", "score": 100},
                    {"dimension_code": "MATCH", "score": 100},
                ]
            },
        )
        self.assertEqual(response.status_code, 200, response.content)

        response = self.get_json(
            f"{RESULTS_PATH}latest/",
            {"event_id": 1, "group_type": "HIGHER"},
        )
        self.assertTrue(self.body(response)["data"]["stale"])

        response = self.get_json(
            f"{RESULTS_PATH}export/",
            {"event_id": 1, "group_type": "HIGHER"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.body(response)["code"], 409)

        response = self.post_json(
            f"{RESULTS_PATH}generate/",
            {
                "event_id": 1,
                "group_type": "HIGHER",
                "quota": 2,
                "result_round": "FINAL",
                "initial_weight": 0.4,
                "final_weight": 0.6,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        regenerated = self.body(response)["data"]
        self.assertEqual(regenerated["version_no"], 2)
        self.assertFalse(regenerated["stale"])

        old_batch = ResultBatch.objects.get(pk=batch["id"])
        self.assertFalse(old_batch.active)

        response = self.get_json(
            f"{RESULTS_PATH}batches/",
            {"event_id": 1, "group_type": "HIGHER"},
        )
        history = self.body(response)["data"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["version_no"], 2)
        self.assertEqual(history[0]["item_count"], 2)

    def test_initial_result_uses_initial_score_only(self):
        template = self.create_template(round_code="INITIAL")
        self.create_score(
            template["id"],
            10001,
            scores=[
                {"dimension_code": "TECH", "score": 90},
                {"dimension_code": "MATCH", "score": 80},
            ],
        )
        self.create_score(
            template["id"],
            10002,
            scores=[
                {"dimension_code": "TECH", "score": 80},
                {"dimension_code": "MATCH", "score": 70},
            ],
        )

        response = self.post_json(
            f"{RESULTS_PATH}generate/",
            {
                "event_id": 1,
                "group_type": "HIGHER",
                "quota": 10,
                "result_round": "INITIAL",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        batch = self.body(response)["data"]
        self.assertEqual(batch["result_round"], "INITIAL")
        self.assertEqual(batch["initial_weight"], 1.0)
        self.assertEqual(batch["final_weight"], 0.0)
        self.assertEqual(len(batch["items"]), 2)
        self.assertIsNone(batch["items"][0]["final_score"])
        self.assertEqual(batch["items"][0]["initial_score"], 86.0)
        self.assertIn("初选总分", batch["formula_snapshot"])

    def test_final_result_requires_final_scores(self):
        template = self.create_template(round_code="INITIAL")
        self.create_score(template["id"], 10001)
        response = self.post_json(
            f"{RESULTS_PATH}generate/",
            {
                "event_id": 1,
                "group_type": "HIGHER",
                "quota": 10,
                "result_round": "FINAL",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("没有可用", self.body(response)["message"])

    # ------------------------------------------------------------------
    # Excel
    # ------------------------------------------------------------------
    def test_score_export(self):
        template = self.create_template()
        self.create_score(template["id"], 10001)
        self.create_score(
            template["id"],
            10002,
            scores=[
                {"dimension_code": "TECH", "score": 80},
                {"dimension_code": "MATCH", "score": 70},
            ],
        )

        response = self.get_json(
            f"{SCORE_PATH}export/",
            {
                "event_id": 1,
                "group_type": "HIGHER",
                "round": "INITIAL",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertIn("filename*=UTF-8''", response["Content-Disposition"])

        workbook = load_workbook(BytesIO(response.content))
        sheet = workbook.active
        self.assertEqual(sheet.title, "评分汇总")
        headers = [cell.value for cell in sheet[1]]
        self.assertIn("姓名", headers)
        self.assertIn("总分", headers)
        self.assertEqual(sheet.max_row, 3)

        response = self.get_json(
            f"{SCORE_PATH}export/",
            {
                "event_id": 1,
                "group_type": "MIDDLE",
                "round": "INITIAL",
            },
        )
        self.assertEqual(response.status_code, 404)
