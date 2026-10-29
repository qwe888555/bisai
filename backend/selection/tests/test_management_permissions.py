"""竞赛报名管理接口鉴权专项测试（2026-10-29 成员4专项修复第一批）。

覆盖场景：
- 匿名用户访问管理接口 → 401；
- 认证模式被误配为 none 时管理接口仍然 fail-closed → 401；
- 已登录但无管理权限的角色（ATHLETE/TEAM/CITY）→ 403，且数据零写入；
- 有权限用户在其授权范围内操作成功（ADMIN 全量；PROVINCE_REVIEWER 仅公示管理）；
- 通过替换路径 ID 的越权尝试被拒绝（先鉴权后解析对象）；
- 公开门户接口（/publicity/、/publicity/{id}/、/publicity/cities/）匿名仍可访问；
- 停用账号与 main 认证模式下的生产安全约束。

角色映射依据 docs/后端接口文档.md 6.20/6.21：遴选评分与结果 = ADMIN；
公示管理（列表/发布/取消）= ADMIN / PROVINCE_REVIEWER。
"""

import json

from django.test import Client, TestCase, override_settings

from apps.accounts.models import Account
from apps.common.authentication import issue_access_token, make_password
from apps.common.ids import new_account_id
from selection.models import PublicityRecord

from .utils import api_prefix

API_PREFIX = api_prefix()
PUBLICITY_PATH = f"{API_PREFIX}publicity/"

TEMPLATE_BODY = {
    "name": "鉴权测试模板",
    "group_type": "HIGHER",
    "round": "INITIAL",
    "items": [
        {
            "dimension_code": "TECH",
            "dimension_name": "技术",
            "weight": 100,
            "max_score": 100,
        }
    ],
}

PUBLISH_BODY = {
    "event_id": 1,
    "publicity_type": "ATHLETE",
    "source_id": "10001",
}

# 全部管理端点（读）与（写），用于匿名扫描
MANAGEMENT_GET_PATHS = [
    f"{API_PREFIX}templates/",
    f"{API_PREFIX}templates/1/",
    f"{API_PREFIX}scores/",
    f"{API_PREFIX}scores/summary/",
    f"{API_PREFIX}scores/export/",
    f"{API_PREFIX}scores/1/",
    f"{API_PREFIX}results/latest/",
    f"{API_PREFIX}results/batches/",
    f"{API_PREFIX}results/export/",
    f"{PUBLICITY_PATH}manage/",
    f"{PUBLICITY_PATH}manage/1/",
]


def _body(response):
    return json.loads(response.content.decode("utf-8"))


@override_settings(
    SELECTION_AUTH_MODE="auto",
    SELECTION_ENVELOPE_MODE="native",
    SELECTION_ARCHIVE_MODE="demo",
    SELECTION_PUBLICITY_SOURCE_MODE="demo",
)
class ManagementPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        def _account(username, roles, enabled=True):
            return Account.objects.create(
                id=new_account_id(),
                username=username,
                display_name=username,
                roles=roles,
                password=make_password("pass-123456"),
                enabled=enabled,
            )

        cls.admin = _account("perm-admin", ["ADMIN"])
        cls.reviewer = _account("perm-reviewer", ["PROVINCE_REVIEWER"])
        cls.athlete = _account("perm-athlete", ["ATHLETE"])
        cls.team = _account("perm-team", ["TEAM"])
        cls.city = _account("perm-city", ["CITY"])
        cls.disabled_admin = _account(
            "perm-disabled", ["ADMIN"], enabled=False
        )

        cls.admin_token = issue_access_token(cls.admin)[0]
        cls.reviewer_token = issue_access_token(cls.reviewer)[0]
        cls.athlete_token = issue_access_token(cls.athlete)[0]
        cls.team_token = issue_access_token(cls.team)[0]
        cls.city_token = issue_access_token(cls.city)[0]
        cls.disabled_token = issue_access_token(cls.disabled_admin)[0]

    def setUp(self):
        self.client = Client()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _get(self, path, token=None, params=None):
        return self.client.get(path, data=params or {}, **self._auth(token) if token else {})

    def _post(self, path, payload=None, token=None):
        return self.client.post(
            path,
            data=json.dumps(payload if payload is not None else {}),
            content_type="application/json",
            **self._auth(token) if token else {},
        )

    def _publish_as_admin(self, source_id="10001"):
        response = self._post(
            PUBLICITY_PATH, dict(PUBLISH_BODY, source_id=source_id), self.admin_token
        )
        self.assertEqual(response.status_code, 200, response.content)
        return _body(response)["data"]

    # ------------------------------------------------------------------
    # 1) 匿名用户：所有管理接口 401，且不产生任何数据写入
    # ------------------------------------------------------------------
    def test_anonymous_denied_on_all_management_endpoints(self):
        for path in MANAGEMENT_GET_PATHS:
            response = self.client.get(path)
            self.assertEqual(
                response.status_code, 401, f"{path} 匿名访问应 401: {response.content}"
            )

        write_cases = [
            (f"{API_PREFIX}templates/", TEMPLATE_BODY),
            (f"{API_PREFIX}scores/", {}),
            (f"{API_PREFIX}results/generate/", {}),
            (PUBLICITY_PATH, dict(PUBLISH_BODY)),
            (f"{PUBLICITY_PATH}batch/", {"event_id": 1}),
            (f"{PUBLICITY_PATH}1/cancel/", {"reason": "x"}),
        ]
        for path, payload in write_cases:
            response = self._post(path, payload)
            self.assertEqual(
                response.status_code, 401, f"{path} 匿名写入应 401: {response.content}"
            )

        # 未授权请求不得产生任何公示记录（防数据面副作用）
        self.assertEqual(PublicityRecord.objects.count(), 0)

    # ------------------------------------------------------------------
    # 2) 认证模式被误配为 none：管理接口仍 fail-closed
    # ------------------------------------------------------------------
    @override_settings(SELECTION_AUTH_MODE="none")
    def test_auth_mode_none_cannot_reach_management(self):
        response = self._get(f"{API_PREFIX}templates/")
        self.assertEqual(response.status_code, 401)

        response = self._post(PUBLICITY_PATH, dict(PUBLISH_BODY))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(PublicityRecord.objects.count(), 0)

    # ------------------------------------------------------------------
    # 3) 已登录但无管理权限：403，且数据零写入
    # ------------------------------------------------------------------
    def test_authenticated_without_role_is_denied(self):
        for token in (self.athlete_token, self.team_token, self.city_token):
            response = self._get(f"{API_PREFIX}templates/", token)
            self.assertEqual(response.status_code, 403, response.content)

            response = self._post(PUBLICITY_PATH, dict(PUBLISH_BODY), token)
            self.assertEqual(response.status_code, 403, response.content)

            response = self._post(f"{PUBLICITY_PATH}1/cancel/", {"reason": "x"}, token)
            self.assertEqual(response.status_code, 403, response.content)

            # 返回主项目统一错误信封：code 为错误码字符串而非业务 0
            payload = _body(response)
            self.assertNotEqual(payload.get("code"), 0)

        self.assertEqual(PublicityRecord.objects.count(), 0)

    # ------------------------------------------------------------------
    # 4) ADMIN：权限范围内全链路可用
    # ------------------------------------------------------------------
    def test_admin_can_operate_within_permission(self):
        response = self._post(f"{API_PREFIX}templates/", TEMPLATE_BODY, self.admin_token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(_body(response)["code"], 0)

        published = self._publish_as_admin()

        response = self._get(f"{PUBLICITY_PATH}manage/", self.admin_token, {"event_id": 1})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(_body(response)["data"]["total"], 1)

        record_id = published["id"]
        response = self._get(
            f"{PUBLICITY_PATH}manage/{record_id}/", self.admin_token
        )
        self.assertEqual(response.status_code, 200, response.content)

        response = self._post(
            f"{PUBLICITY_PATH}{record_id}/cancel/", {"reason": "复核有误"}, self.admin_token
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(_body(response)["data"]["status"], "CANCELLED")

    # ------------------------------------------------------------------
    # 5) PROVINCE_REVIEWER：仅公示管理，评分/结果/规则全部 403
    # ------------------------------------------------------------------
    def test_reviewer_can_manage_publicity_but_not_scoring(self):
        response = self._get(f"{PUBLICITY_PATH}manage/", self.reviewer_token, {"event_id": 1})
        self.assertEqual(response.status_code, 200, response.content)

        response = self._post(PUBLICITY_PATH, dict(PUBLISH_BODY), self.reviewer_token)
        self.assertEqual(response.status_code, 200, response.content)

        denied_paths = [
            f"{API_PREFIX}templates/",
            f"{API_PREFIX}scores/",
            f"{API_PREFIX}scores/summary/",
            f"{API_PREFIX}results/latest/",
        ]
        for path in denied_paths:
            response = self._get(path, self.reviewer_token)
            self.assertEqual(response.status_code, 403, f"{path}: {response.content}")

        response = self._post(f"{API_PREFIX}scores/", {}, self.reviewer_token)
        self.assertEqual(response.status_code, 403, response.content)

        response = self._post(f"{API_PREFIX}results/generate/", {}, self.reviewer_token)
        self.assertEqual(response.status_code, 403, response.content)

    # ------------------------------------------------------------------
    # 6) 公开门户接口：匿名仍可访问；管理面匿名仍 401
    # ------------------------------------------------------------------
    def test_public_portal_stays_open_without_login(self):
        published = self._publish_as_admin()
        record_id = published["id"]

        # 匿名列表
        response = self.client.get(PUBLICITY_PATH, {"event_id": 1})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(_body(response)["data"]["total"], 1)

        # 匿名详情（已发布记录）
        response = self.client.get(f"{PUBLICITY_PATH}{record_id}/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(_body(response)["data"]["id"], record_id)

        # 匿名城市列表
        response = self.client.get(f"{PUBLICITY_PATH}cities/", {"event_id": 1})
        self.assertEqual(response.status_code, 200, response.content)

        # 管理面匿名依旧 401
        response = self.client.get(f"{PUBLICITY_PATH}manage/")
        self.assertEqual(response.status_code, 401)

    # ------------------------------------------------------------------
    # 7) 停用账号：令牌签发后账号被停用 → 403
    # ------------------------------------------------------------------
    def test_disabled_account_is_denied(self):
        response = self._get(f"{API_PREFIX}templates/", self.disabled_token)
        self.assertEqual(response.status_code, 403, response.content)

    # ------------------------------------------------------------------
    # 8) 路径 ID 替换越权：先鉴权后解析对象
    # ------------------------------------------------------------------
    def test_id_substitution_attempts_are_denied(self):
        published = self._publish_as_admin()
        record_id = published["id"]

        # 无权限角色替换为真实存在的记录 ID 仍然 403
        response = self._post(
            f"{PUBLICITY_PATH}{record_id}/cancel/", {"reason": "x"}, self.athlete_token
        )
        self.assertEqual(response.status_code, 403, response.content)
        record = PublicityRecord.objects.get(pk=record_id)
        self.assertEqual(record.status, "PUBLISHED")

        # 匿名持有真实 ID 也无法取消：401 先于对象解析
        response = self._post(f"{PUBLICITY_PATH}{record_id}/cancel/", {"reason": "x"})
        self.assertEqual(response.status_code, 401)

        # PROVINCE_REVIEWER 无评分权限：真实 score id 替换无效
        response = self._get(f"{API_PREFIX}scores/1/", self.reviewer_token)
        self.assertEqual(response.status_code, 403, response.content)

        # 有权限但对不存在的对象：通过鉴权后返回 404（对象不存在）
        response = self._get(f"{PUBLICITY_PATH}manage/999999/", self.admin_token)
        self.assertEqual(response.status_code, 404, response.content)

    # ------------------------------------------------------------------
    # 9) main 认证模式：无 JWT 仍 401（生产安全约束）
    # ------------------------------------------------------------------
    @override_settings(SELECTION_AUTH_MODE="main")
    def test_auth_mode_main_requires_jwt(self):
        response = self._get(f"{API_PREFIX}templates/")
        self.assertEqual(response.status_code, 401)

        response = self._get(f"{API_PREFIX}templates/", self.admin_token)
        self.assertEqual(response.status_code, 200, response.content)
