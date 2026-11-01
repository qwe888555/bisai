"""P0-3 D 批：qualify_roster(CONFIRM) 规则未配置前置校验测试。

直接调用 apps.enrollments.services.qualify_roster，patch scope.get_enrollment
与 event_config_client.get_group_rules 以隔离数据范围与赛事配置变量。

验收点：
- UNCONFIGURED 时 CONFIRM 抛出 RULES_UNCONFIGURED（422），不写资格凭证；
- CONFIGURED 时 CONFIRM 正常执行原有确认逻辑；
- REJECT / EXCLUDE / REVOKE 不受规则状态前置校验影响。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from apps.common.errors import ApiError, RULES_UNCONFIGURED
from apps.enrollments.models import (
    QUALIFICATION_DISQUALIFIED,
    QUALIFICATION_EXCLUDED,
    QUALIFICATION_QUALIFIED,
    Enrollment,
    RosterItem,
    RosterQualificationLog,
    STATUS_DRAFT,
)
from apps.enrollments.services import qualify_roster

UNCONFIGURED_RULES = {
    "minPlayers": None,
    "maxPlayers": 25,
    "minGoalkeepers": 1,
    "birthDateFrom": "2008-01-01",
    "birthDateTo": "2012-12-31",
    "requiredMaterialTypes": ["STAMPED_FORM", "STUDENT_STATUS"],
    "confirmed": False,
}

CONFIGURED_RULES = {
    "minPlayers": 5,
    "maxPlayers": 25,
    "minGoalkeepers": 1,
    "birthDateFrom": "2008-01-01",
    "birthDateTo": "2012-12-31",
    "requiredMaterialTypes": ["STAMPED_FORM", "STUDENT_STATUS"],
    "confirmed": True,
}


def _payload(action: str, version: int = 0, reason: str = "") -> SimpleNamespace:
    return SimpleNamespace(action=action, version=version, reason=reason)


class QualifyRosterRulesConfigTests(TestCase):
    def setUp(self):
        self.actor = SimpleNamespace(pk="acc_test")
        self.enrollment = Enrollment.objects.create(
            id="enr_test",
            event_id="evt_test",
            group_id="grp_test",
            team_id="team_test",
            subject_type="SCHOOL_TEAM",
            status=STATUS_DRAFT,
            contact_name="张老师",
            contact_phone_cipher="x",
            contact_phone_masked="138****0001",
            city_org_id="",
            created_by="acc_test",
        )
        self.item = RosterItem.objects.create(
            id="ri_test",
            enrollment_id=self.enrollment.pk,
            athlete_id="ath_01",
            athlete_name="测试运动员",
            jersey_number=10,
            position="DF",
        )

    def _scope_patch(self):
        return patch(
            "apps.enrollments.services.scope.get_enrollment",
            return_value=self.enrollment,
        )

    def test_confirm_unconfigured_raises_rules_unconfigured_no_credential(self):
        with (
            self._scope_patch(),
            patch(
                "apps.enrollments.clients.EventConfigClient.get_group_rules",
                return_value=UNCONFIGURED_RULES,
            ),
        ):
            with self.assertRaises(ApiError) as cm:
                qualify_roster(
                    self.actor,
                    self.enrollment.pk,
                    self.item.pk,
                    _payload("CONFIRM"),
                )
        self.assertEqual(cm.exception.code, RULES_UNCONFIGURED)

        refreshed = RosterItem.objects.get(pk=self.item.pk)
        # 未写入资格确认凭证（confirmed_at / confirmed_by 为空）
        self.assertIsNone(refreshed.confirmed_at)
        self.assertEqual(refreshed.confirmed_by, "")
        self.assertEqual(
            RosterQualificationLog.objects.filter(
                roster_item_id_snapshot=self.item.pk, action="CONFIRM"
            ).count(),
            0,
        )

    def test_confirm_configured_normal(self):
        with (
            self._scope_patch(),
            patch(
                "apps.enrollments.clients.EventConfigClient.get_group_rules",
                return_value=CONFIGURED_RULES,
            ),
        ):
            result = qualify_roster(
                self.actor,
                self.enrollment.pk,
                self.item.pk,
                _payload("CONFIRM"),
            )
        self.assertEqual(result["qualificationStatus"], QUALIFICATION_QUALIFIED)
        self.assertIsNotNone(result["confirmedAt"])

        refreshed = RosterItem.objects.get(pk=self.item.pk)
        self.assertEqual(refreshed.qualification_status, QUALIFICATION_QUALIFIED)
        self.assertIsNotNone(refreshed.confirmed_at)
        self.assertEqual(refreshed.confirmed_by, "acc_test")

    def test_confirm_missing_group_propagates_not_found(self):
        """B3 补充：真实模式下组别不存在（含归属不符）时 NOT_FOUND 原样传播。"""
        from apps.common.errors import NOT_FOUND

        with (
            self._scope_patch(),
            patch("apps.enrollments.clients.mock_enabled", lambda: False),
        ):
            with self.assertRaises(ApiError) as cm:
                qualify_roster(
                    self.actor,
                    self.enrollment.pk,
                    self.item.pk,
                    _payload("CONFIRM"),
                )
        self.assertEqual(cm.exception.code, NOT_FOUND)
        # 未写入任何资格状态
        refreshed = RosterItem.objects.get(pk=self.item.pk)
        self.assertIsNone(refreshed.confirmed_at)
        self.assertEqual(refreshed.confirmed_by, "")

    def test_repeat_confirm_after_confirmed_is_idempotent_when_unconfigured(self):
        """B1：已有有效凭证时重复 CONFIRM 为幂等 no-op，不再因规则未配置而 422。"""
        with (
            self._scope_patch(),
            patch(
                "apps.enrollments.clients.EventConfigClient.get_group_rules",
                return_value=CONFIGURED_RULES,
            ),
        ):
            qualify_roster(
                self.actor,
                self.enrollment.pk,
                self.item.pk,
                _payload("CONFIRM"),
            )
        version_after_confirm = RosterItem.objects.get(pk=self.item.pk).version
        with (
            self._scope_patch(),
            patch(
                "apps.enrollments.clients.EventConfigClient.get_group_rules",
                return_value=UNCONFIGURED_RULES,
            ),
        ):
            result = qualify_roster(
                self.actor,
                self.enrollment.pk,
                self.item.pk,
                _payload("CONFIRM", version=version_after_confirm),
            )
        self.assertEqual(result["qualificationStatus"], QUALIFICATION_QUALIFIED)
        self.assertIsNotNone(result["confirmedAt"])

    def test_reject_unaffected_by_unconfigured_rules(self):
        with (
            self._scope_patch(),
            patch(
                "apps.enrollments.clients.EventConfigClient.get_group_rules",
                return_value=UNCONFIGURED_RULES,
            ),
        ):
            result = qualify_roster(
                self.actor,
                self.enrollment.pk,
                self.item.pk,
                _payload("REJECT", reason="资格材料不符"),
            )
        self.assertEqual(result["qualificationStatus"], QUALIFICATION_DISQUALIFIED)

    def test_exclude_unaffected_by_unconfigured_rules(self):
        with (
            self._scope_patch(),
            patch(
                "apps.enrollments.clients.EventConfigClient.get_group_rules",
                return_value=UNCONFIGURED_RULES,
            ),
        ):
            result = qualify_roster(
                self.actor,
                self.enrollment.pk,
                self.item.pk,
                _payload("EXCLUDE", reason="超出名额"),
            )
        self.assertEqual(result["qualificationStatus"], QUALIFICATION_EXCLUDED)

    def test_revoke_unaffected_by_unconfigured_rules(self):
        # 先在 CONFIGURED 下 CONFIRM 写入凭证
        with (
            self._scope_patch(),
            patch(
                "apps.enrollments.clients.EventConfigClient.get_group_rules",
                return_value=CONFIGURED_RULES,
            ),
        ):
            qualify_roster(
                self.actor,
                self.enrollment.pk,
                self.item.pk,
                _payload("CONFIRM"),
            )
        version_after_confirm = RosterItem.objects.get(pk=self.item.pk).version
        # 再切到 UNCONFIGURED，REVOKE 不应被规则校验拦截
        with (
            self._scope_patch(),
            patch(
                "apps.enrollments.clients.EventConfigClient.get_group_rules",
                return_value=UNCONFIGURED_RULES,
            ),
        ):
            result = qualify_roster(
                self.actor,
                self.enrollment.pk,
                self.item.pk,
                _payload("REVOKE", version=version_after_confirm, reason="复核撤销"),
            )
        self.assertIsNone(result["confirmedAt"])
        refreshed = RosterItem.objects.get(pk=self.item.pk)
        self.assertIsNone(refreshed.confirmed_at)
        self.assertEqual(refreshed.confirmed_by, "")
