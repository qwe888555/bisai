"""政策问答 AI_ENABLED=false 显式单测（2026-10-29 成员4专项修复⑧）。

只覆盖 DISABLED 降级路径，不改配置默认值、不动业务代码：
- 关闭时 answer() 返回 DISABLED + 政策原文兜底（不调模型、无网络依赖）；
- 无已发布政策时兜底列表为空；
- 个人信息拦截先于 DISABLED 生效（身份证/手机号拒绝外发）。

配置开关（2026-11-02）：AI_* 由 conf 在模块导入时读定为常量，
override_settings 改不到它——部署机 .env 一旦开 AI_ENABLED=true，
旧写法会让本类测试打到真实模型接口（实测 74s 且断言失效）。
故直接 patch conf.AI_ENABLED，测试与本机 .env 解耦。
"""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.common import conf
from apps.common.errors import ApiError
from apps.policyqa.models import PolicyDocument
from apps.policyqa.services import answer


def _enabled_document(title="省级足球赛事报名办法"):
    # _candidate_documents 只取 enabled 且 published_at 非空的快照
    return PolicyDocument.objects.create(
        id=f"pol_{title}",
        title=title,
        summary="报名条件与材料",
        body="参赛球员须为本校在籍学生，报名需提交学籍材料。",
        revision=1,
        enabled=True,
        published_at=timezone.now(),
    )


class DisabledAnswerTests(TestCase):
    # 覆盖 conf 常量而非 settings：见模块 docstring
    def setUp(self):
        patcher = patch.object(conf, "AI_ENABLED", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disabled_returns_status_and_fallback(self):
        document = _enabled_document()
        result = answer("报名需要什么条件")
        self.assertEqual(result["status"], "DISABLED")
        self.assertEqual(result["answer"], "政策问答当前已关闭，请查阅下列政策原文。")
        self.assertEqual(result["citations"], [])
        self.assertFalse(result["aiGenerated"])
        self.assertEqual(len(result["fallbackPolicies"]), 1)
        self.assertEqual(result["fallbackPolicies"][0]["title"], document.title)
        self.assertIn("requestId", result)

    def test_disabled_without_documents_fallback_empty(self):
        result = answer("报名需要什么条件")
        self.assertEqual(result["status"], "DISABLED")
        self.assertEqual(result["fallbackPolicies"], [])

    def test_disabled_still_rejects_personal_info(self):
        _enabled_document()
        with self.assertRaises(ApiError) as caught:
            answer("我的身份证号是110101199001011234，能报名吗")
        self.assertEqual(caught.exception.code, "BUSINESS_VALIDATION_FAILED")
