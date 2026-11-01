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

    def test_publish_creates_news_and_delete_offlines(self):
        services.publish_match_result(self.match.pk, 0)
        news = Content.objects.filter(source_match_id=self.match.pk).first()
        self.assertIsNotNone(news)
        self.assertEqual(news.status, "PUBLISHED")
        self.assertEqual(news.content_type, "HIGHLIGHT")
        self.assertIn("2:1", news.title)

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
