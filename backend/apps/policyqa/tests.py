"""政策问答 AI_ENABLED=false 显式单测（2026-10-29 成员4专项修复⑧）。

只覆盖 DISABLED 降级路径，不改配置默认值、不动业务代码：
- 默认关闭时 answer() 返回 DISABLED + 政策原文兜底（不调模型、无网络依赖）；
- 无已发布政策时兜底列表为空；
- 个人信息拦截先于 DISABLED 生效（身份证/手机号拒绝外发）。

2026-10-30 集成修正：conf.AI_ENABLED 是 apps/common/conf.py 导入时的快照
常量，override_settings 不生效；改用 mock.patch.object(conf, "AI_ENABLED", ...)
主动控制开关（与 backend/tests/test_smoke.py 惯例一致），并新增开启态对照
用例证明是否走 DISABLED 确由该开关决定。
"""

from unittest import mock

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
    def setUp(self):
        # patch services.py 实际引用的 apps.common.conf 模块对象属性（非 settings）
        patcher = mock.patch.object(conf, "AI_ENABLED", False)
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


class EnabledSwitchControlTests(TestCase):
    """开关有效性对照（无网络）：AI_ENABLED patch 为 True 时，同一问题
    不得再走 DISABLED——证明 DISABLED 与否确由被测代码读取的
    conf.AI_ENABLED 决定，而非依赖进程默认值。此时无政策命中且模型
    配置不全，answer() 走 NO_EVIDENCE 或 UNAVAILABLE，均不发起模型调用。"""

    def test_enabled_switch_leaves_disabled_path(self):
        with mock.patch.object(conf, "AI_ENABLED", True):
            result = answer("报名需要什么条件")
        self.assertNotEqual(result["status"], "DISABLED")
