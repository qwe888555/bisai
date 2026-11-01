"""P0-3 D 批：组别资格规则结构化字段与状态派生测试。

覆盖：
- derive_rules_status 两态派生（UNCONFIGURED / CONFIGURED）；
- group_out 输出 rulesConfirmed / rulesStatus 与实体一致；
- EventConfigClient.get_group_rules 非 mock 分支直查 EventGroup 实体。
"""

from __future__ import annotations

from datetime import date

from django.test import TestCase

from apps.events.models import (
    RULES_STATUS_CONFIGURED,
    RULES_STATUS_UNCONFIGURED,
    Event,
    EventGroup,
    derive_rules_status,
)
from apps.events.schemas import group_out


class DeriveRulesStatusTests(TestCase):
    def test_rules_confirmed_false_is_unconfigured(self):
        self.assertEqual(
            derive_rules_status(
                min_players=5, max_players=25, rules_confirmed=False
            ),
            RULES_STATUS_UNCONFIGURED,
        )

    def test_min_players_none_is_unconfigured(self):
        self.assertEqual(
            derive_rules_status(
                min_players=None, max_players=25, rules_confirmed=True
            ),
            RULES_STATUS_UNCONFIGURED,
        )

    def test_max_players_none_is_unconfigured(self):
        self.assertEqual(
            derive_rules_status(
                min_players=5, max_players=None, rules_confirmed=True
            ),
            RULES_STATUS_UNCONFIGURED,
        )

    def test_all_present_is_configured(self):
        self.assertEqual(
            derive_rules_status(
                min_players=5, max_players=25, rules_confirmed=True
            ),
            RULES_STATUS_CONFIGURED,
        )

    def test_optional_fields_blank_still_configured(self):
        """min_goalkeepers / birth_date_from / birth_date_to 为空不影响核心状态。"""
        self.assertEqual(
            derive_rules_status(
                min_players=5,
                max_players=25,
                rules_confirmed=True,
            ),
            RULES_STATUS_CONFIGURED,
        )


class GroupOutRulesStatusTests(TestCase):
    def setUp(self):
        self.event = Event.objects.create(
            id="evt_rules",
            name="规则测试赛事",
            season="2027",
            registration_opens_at="2027-01-01T00:00:00Z",
            registration_closes_at="2027-12-31T23:59:59Z",
            status="DRAFT",
        )

    def _group(self, **overrides):
        defaults = dict(
            id="grp_rules",
            event=self.event,
            name="U15 测试组",
            category="MIDDLE",
            gender="MALE",
            max_players=25,
            required_material_types=["STAMPED_FORM"],
        )
        defaults.update(overrides)
        return EventGroup.objects.create(**defaults)

    def test_unconfigured_when_not_confirmed(self):
        group = self._group(rules_confirmed=False, min_players=5)
        data = group_out(group)
        self.assertFalse(data["rulesConfirmed"])
        self.assertEqual(data["rulesStatus"], RULES_STATUS_UNCONFIGURED)

    def test_unconfigured_when_min_players_missing(self):
        group = self._group(rules_confirmed=True, min_players=None)
        data = group_out(group)
        self.assertTrue(data["rulesConfirmed"])
        self.assertEqual(data["rulesStatus"], RULES_STATUS_UNCONFIGURED)

    def test_configured_when_confirmed_and_bounds_present(self):
        group = self._group(
            rules_confirmed=True,
            min_players=5,
            min_goalkeepers=1,
            birth_date_from=date(2008, 1, 1),
            birth_date_to=date(2012, 12, 31),
        )
        data = group_out(group)
        self.assertTrue(data["rulesConfirmed"])
        self.assertEqual(data["rulesStatus"], RULES_STATUS_CONFIGURED)
        self.assertEqual(data["minPlayers"], 5)
        self.assertEqual(data["minGoalkeepers"], 1)
        self.assertEqual(data["birthDateFrom"], "2008-01-01")
        self.assertEqual(data["birthDateTo"], "2012-12-31")


class EventConfigClientGroupRulesTests(TestCase):
    """非 mock 分支直查 EventGroup 实体。"""

    def setUp(self):
        self.event = Event.objects.create(
            id="evt_client",
            name="Client 测试赛事",
            season="2027",
            registration_opens_at="2027-01-01T00:00:00Z",
            registration_closes_at="2027-12-31T23:59:59Z",
            status="DRAFT",
        )
        self.group = EventGroup.objects.create(
            id="grp_client",
            event=self.event,
            name="Client 测试组",
            category="MIDDLE",
            gender="MALE",
            max_players=22,
            required_material_types=["STAMPED_FORM"],
            min_players=8,
            min_goalkeepers=2,
            birth_date_from=date(2009, 1, 1),
            birth_date_to=date(2011, 12, 31),
            rules_confirmed=True,
        )

    def test_get_group_rules_matches_entity(self):
        from apps.enrollments.clients import event_config_client
        from unittest.mock import patch

        with patch("apps.enrollments.clients.mock_enabled", lambda: False):
            rules = event_config_client.get_group_rules(self.event.pk, self.group.pk)

        self.assertEqual(rules["minPlayers"], 8)
        self.assertEqual(rules["maxPlayers"], 22)
        self.assertEqual(rules["minGoalkeepers"], 2)
        self.assertEqual(rules["birthDateFrom"], "2009-01-01")
        self.assertEqual(rules["birthDateTo"], "2011-12-31")
        self.assertEqual(rules["requiredMaterialTypes"], ["STAMPED_FORM"])
        self.assertTrue(rules["confirmed"])

    def test_get_group_rules_missing_group_raises_not_found(self):
        from apps.common.errors import ApiError, NOT_FOUND
        from apps.enrollments.clients import event_config_client
        from unittest.mock import patch

        with patch("apps.enrollments.clients.mock_enabled", lambda: False):
            with self.assertRaises(ApiError) as cm:
                event_config_client.get_group_rules(self.event.pk, "nonexistent")
        self.assertEqual(cm.exception.code, NOT_FOUND)

    def test_get_group_rules_rejects_group_of_other_event(self):
        """B3：group_id 属于其他赛事时按归属校验拒绝，避免跨赛事读取规则。"""
        from apps.common.errors import ApiError, NOT_FOUND
        from apps.enrollments.clients import event_config_client
        from unittest.mock import patch

        other_event = Event.objects.create(
            id="evt_other",
            name="其他赛事",
            season="2027",
            registration_opens_at="2027-01-01T00:00:00Z",
            registration_closes_at="2027-12-31T23:59:59Z",
            status="DRAFT",
        )
        with patch("apps.enrollments.clients.mock_enabled", lambda: False):
            with self.assertRaises(ApiError) as cm:
                event_config_client.get_group_rules(other_event.pk, self.group.pk)
        self.assertEqual(cm.exception.code, NOT_FOUND)


class UpdateGroupRulesConfirmedTests(TestCase):
    """B2：更新组别时未显式提供 rulesConfirmed 不得静默清除确认状态。"""

    def setUp(self):
        from apps.events.services import create_group

        self.event = Event.objects.create(
            id="evt_update",
            name="更新测试赛事",
            season="2027",
            registration_opens_at="2027-01-01T00:00:00Z",
            registration_closes_at="2027-12-31T23:59:59Z",
            status="DRAFT",
        )
        self.group = create_group(
            self.event.pk,
            {
                "name": "更新测试组",
                "category": "MIDDLE",
                "gender": "MALE",
                "maxPlayers": 25,
                "requiredMaterialTypes": ["STAMPED_FORM"],
                "minPlayers": 5,
                "rulesConfirmed": True,
            },
        )

    def _payload(self, **overrides):
        base = {
            "name": "更新测试组",
            "category": "MIDDLE",
            "gender": "MALE",
            "maxPlayers": 25,
            "requiredMaterialTypes": ["STAMPED_FORM"],
            "minPlayers": 5,
            "version": self.group.version,
        }
        base.update(overrides)
        return base

    def test_update_without_rules_confirmed_keeps_confirmed(self):
        from apps.events.services import update_group

        updated = update_group(
            self.event.pk, self.group.pk, self._payload(name="改名后的组")
        )
        self.assertTrue(updated.rules_confirmed)
        self.assertEqual(
            group_out(updated)["rulesStatus"], RULES_STATUS_CONFIGURED
        )

    def test_update_with_explicit_false_clears_confirmed(self):
        from apps.events.services import update_group

        updated = update_group(
            self.event.pk,
            self.group.pk,
            self._payload(rulesConfirmed=False),
        )
        self.assertFalse(updated.rules_confirmed)
        self.assertEqual(
            group_out(updated)["rulesStatus"], RULES_STATUS_UNCONFIGURED
        )

    def test_update_via_schema_dump_keeps_confirmed(self):
        """B2 schema 层路径：API 实际调用 body.model_dump()，未提供
        rulesConfirmed 时 dump 出 None，update_group 不得清除确认状态。"""
        from apps.events.schemas import EventGroupUpdate
        from apps.events.services import update_group

        body = EventGroupUpdate(
            name="改名后的组",
            category="MIDDLE",
            gender="MALE",
            maxPlayers=25,
            requiredMaterialTypes=["STAMPED_FORM"],
            minPlayers=5,
            version=self.group.version,
        )
        self.assertIsNone(body.model_dump()["rulesConfirmed"])
        updated = update_group(self.event.pk, self.group.pk, body.model_dump())
        self.assertTrue(updated.rules_confirmed)
