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
"""events 域冒烟测试：赛事层级归一化、内容分类过滤、赛果快讯、官方积分榜。"""

from django.test import TestCase
from django.utils import timezone

from apps.common.ids import new_id

from .models import (
    Content,
    Event,
    EventGroup,
    Match,
    MatchEvent,
    normalize_event_category,
    event_category_label,
)
from . import services


def _make_event(category: str = "") -> Event:
    return Event.objects.create(
        id=new_id("evt"),
        name="测试赛事",
        season="2026",
        event_category=category,
        registration_opens_at=timezone.now(),
        registration_closes_at=timezone.now() + timezone.timedelta(days=7),
        status="RUNNING",
    )


def _make_match(event: Event, group: EventGroup, home: str, away: str,
                hs: int, as_: int, result_published: bool = True) -> Match:
    return Match.objects.create(
        id=new_id("mch"),
        event_id=event.pk,
        group_id=group.pk,
        home_team_id=home,
        away_team_id=away,
        kickoff_at=timezone.now(),
        home_score=hs,
        away_score=as_,
        status="FINISHED",
        result_type="NORMAL",
        result_published=result_published,
    )


class NormalizeEventCategoryTests(TestCase):
    def test_canonical_and_alias_and_unknown(self):
        self.assertEqual(normalize_event_category("SCHOOL_LEAGUE"), "SCHOOL_LEAGUE")
        self.assertEqual(normalize_event_category("school_league"), "SCHOOL_LEAGUE")
        self.assertEqual(normalize_event_category("TOURNAMENT"), "CHAMPIONSHIP")
        self.assertEqual(normalize_event_category("省联赛"), "OTHER")
        self.assertEqual(normalize_event_category(""), "")

    def test_label_unknown(self):
        self.assertEqual(event_category_label("PROVINCIAL_LEAGUE"), "省级联赛")
        self.assertEqual(event_category_label(""), "未分级")
        self.assertEqual(event_category_label("whatever"), "未分级")


class ContentCategoryFilterTests(TestCase):
    def setUp(self):
        self.event = _make_event(category="SCHOOL_LEAGUE")
        self._make_content("关联赛事", self.event.pk)
        self._make_content("无赛事内容", "")

    def _make_content(self, title: str, event_id: str) -> Content:
        return Content.objects.create(
            id=new_id("cnt"),
            content_type="NEWS",
            title=title,
            summary="",
            body_html="<p>x</p>",
            body_text="x",
            event_id=event_id,
            status="PUBLISHED",
            revision=1,
        )

    def test_category_filter_does_not_leak_unattached(self):
        qs = services._content_queryset({"eventCategory": "SCHOOL_LEAGUE"})
        ids = list(qs.values_list("title", flat=True))
        self.assertEqual(ids, ["关联赛事"])

    def test_category_without_events_returns_empty(self):
        qs = services._content_queryset({"eventCategory": "CITY_LEAGUE"})
        self.assertEqual(list(qs), [])


class ResultNewsTests(TestCase):
    def setUp(self):
        self.event = _make_event()
        group = EventGroup.objects.create(
            id=new_id("grp"),
            event=self.event,
            name="小学男子组",
            category="PRIMARY",
            gender="MALE",
            max_players=12,
            required_material_types=[],
        )
        self.group = group
        self.match = _make_match(self.event, group, "1", "2", 2, 1, result_published=False)
        MatchEvent.objects.create(
            id=new_id("mev"),
            match_id=self.match.pk,
            event_type="GOAL",
            minute=12,
            team_id="1",
            scorer_athlete_id="ath_1",
        )
        MatchEvent.objects.create(
            id=new_id("mev"),
            match_id=self.match.pk,
            event_type="PENALTY",
            minute=40,
            team_id="2",
            athlete_id="ath_2",
            penalty_type="YELLOW_CARD",
            reason="拉人",
        )

    def test_publish_creates_news_and_delete_offlines(self):
        services.publish_match_result(self.match.pk, 0)
        news = Content.objects.filter(source_match_id=self.match.pk).first()
        self.assertIsNotNone(news)
        self.assertEqual(news.status, "PUBLISHED")
        self.assertEqual(news.content_type, "HIGHLIGHT")
        self.assertIn("2:1", news.title)
        # 自动快讯必须包含进球球员名单与红黄牌，而不只是比分。
        self.assertIn("ath_1", news.body_html)
        self.assertIn("黄牌", news.body_html)
        self.assertIn("拉人", news.body_html)
        self.assertIn("ath_1", news.body_text)

        self.match.refresh_from_db()
        services.publish_match_result(self.match.pk, self.match.version)
        self.assertEqual(
            Content.objects.filter(source_match_id=self.match.pk).count(), 1,
            "重复发布不堆积",
        )

        self.match.refresh_from_db()
        services.delete_match_result(self.match.pk, self.match.version)
        news.refresh_from_db()
        self.assertEqual(news.status, "OFFLINE")


class StandingsTests(TestCase):
    def setUp(self):
        self.event = _make_event()
        self.group = EventGroup.objects.create(
            id=new_id("grp"),
            event=self.event,
            name="小学男子组",
            category="PRIMARY",
            gender="MALE",
            max_players=12,
            required_material_types=[],
        )

    def test_points_ranking(self):
        # A：1 胜 1 平 = 4 分；B：1 平 1 负 = 1 分；C：2 平 = 2 分
        _make_match(self.event, self.group, "A", "B", 2, 1)
        _make_match(self.event, self.group, "B", "C", 0, 0)
        _make_match(self.event, self.group, "A", "C", 1, 1)

        result = services.standings(self.event.pk, self.group.pk)
        teams = {row["teamId"]: row for row in result["items"]}
        self.assertEqual(teams["A"]["points"], 4)
        self.assertEqual(teams["B"]["points"], 1)
        self.assertEqual(teams["C"]["points"], 2)
        # 排序：A(4) > C(2) > B(1)；密集排名 [1,2,3]
        self.assertEqual([row["teamId"] for row in result["items"]], ["A", "C", "B"])
        self.assertEqual([row["rank"] for row in result["items"]], [1, 2, 3])

    def test_unpublished_result_excluded(self):
        _make_match(self.event, self.group, "A", "B", 1, 0, result_published=True)
        _make_match(self.event, self.group, "B", "C", 1, 0, result_published=False)
        result = services.standings(self.event.pk)
        # 只统计已发布赛果的球队：A、B 两队在榜，C 队未发布比赛不入榜
        self.assertEqual(result["total"], 2)
        self.assertNotIn("C", {row["teamId"] for row in result["items"]})
