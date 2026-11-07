"""P0-3 D 批：组别资格规则结构化字段与状态派生测试。

覆盖：
- derive_rules_status 两态派生（UNCONFIGURED / CONFIGURED）；
- group_out 输出 rulesConfirmed / rulesStatus 与实体一致；
- EventConfigClient.get_group_rules 非 mock 分支直查 EventGroup 实体。
"""

from __future__ import annotations

from datetime import date

from django.test import TestCase
from django.utils import timezone

from apps.common.errors import BUSINESS_VALIDATION_FAILED, STATE_CONFLICT, ApiError
from apps.common.ids import new_id
from apps.events.models import (
    RULES_STATUS_CONFIGURED,
    RULES_STATUS_UNCONFIGURED,
    Content,
    Event,
    EventGroup,
    Match,
    MatchEvent,
    derive_rules_status,
    event_category_label,
    normalize_event_category,
)
from apps.events.schemas import group_out
from . import services


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


class PublishedResultProtectionTests(TestCase):
    """赛果发布后冻结事件/赛果写入；弃权人工比分不被事件重算覆盖。"""

    def setUp(self):
        self.match = Match.objects.create(
            id="mtc_published",
            event_id="evt_published",
            group_id="grp_published",
            home_team_id="team_home",
            away_team_id="team_away",
            kickoff_at=timezone.now(),
            status="FINISHED",
            result_published=True,
        )
        self.event = MatchEvent.objects.create(
            id="mev_published",
            match_id=self.match.pk,
            event_type="GOAL",
            minute=10,
            team_id="team_home",
            scorer_athlete_id="athlete_1",
        )

    def _event_payload(self):
        return {
            "matchVersion": self.match.version,
            "eventType": "GOAL",
            "minute": 20,
            "teamId": "team_home",
            "scorerAthleteId": "athlete_2",
        }

    def test_create_event_rejected_after_result_published(self):
        from apps.events.services import create_match_event

        with self.assertRaises(ApiError) as cm:
            create_match_event(self.match.pk, self._event_payload())
        self.assertEqual(cm.exception.code, STATE_CONFLICT)

    def test_update_event_rejected_after_result_published(self):
        from apps.events.services import update_match_event

        with self.assertRaises(ApiError) as cm:
            update_match_event(
                self.match.pk,
                self.event.pk,
                self.event.version,
                {"matchVersion": self.match.version, "eventType": "GOAL",
                 "minute": 30, "teamId": "team_home", "scorerAthleteId": "athlete_1"},
            )
        self.assertEqual(cm.exception.code, STATE_CONFLICT)

    def test_delete_event_rejected_after_result_published(self):
        from apps.events.services import delete_match_event

        with self.assertRaises(ApiError) as cm:
            delete_match_event(
                self.match.pk, self.event.pk, self.event.version, self.match.version
            )
        self.assertEqual(cm.exception.code, STATE_CONFLICT)

    def test_write_result_rejected_after_result_published(self):
        from apps.events.services import write_match_result

        with self.assertRaises(ApiError) as cm:
            write_match_result(
                self.match.pk,
                {"version": self.match.version, "resultType": "NORMAL"},
                {"MATCH_RECORDER"},
            )
        self.assertEqual(cm.exception.code, STATE_CONFLICT)

    def test_forfeit_score_not_overwritten_by_rescore(self):
        from apps.events.services import _rescore

        self.match.result_type = "FORFEIT"
        self.match.manual_home_score = 3
        self.match.manual_away_score = 0
        self.match.home_score = 3
        self.match.away_score = 0
        self.match.save()

        _rescore(self.match)

        self.assertEqual(self.match.home_score, 3)
        self.assertEqual(self.match.away_score, 0)
        self.assertEqual(self.match.result_type, "FORFEIT")

    def test_switch_forfeit_to_normal_recomputes_from_events(self):
        from apps.events.services import write_match_result

        self.match.result_published = False
        self.match.result_type = "FORFEIT"
        self.match.manual_home_score = 3
        self.match.manual_away_score = 0
        self.match.home_score = 3
        self.match.away_score = 0
        self.match.save()

        match = write_match_result(
            self.match.pk,
            {"version": self.match.version, "resultType": "NORMAL"},
            {"MATCH_RECORDER"},
        )

        self.assertEqual(match.result_type, "NORMAL")
        self.assertEqual(match.home_score, 1)
        self.assertEqual(match.away_score, 0)
        self.assertIsNone(match.manual_home_score)


class ProtectedDeleteTests(TestCase):
    """赛事/组别/对阵存在逻辑引用时统一 409。"""

    def setUp(self):
        self.event = Event.objects.create(
            id="evt_ref",
            name="引用赛事",
            season="2026",
            registration_opens_at=timezone.now(),
            registration_closes_at=timezone.now(),
            status="DRAFT",
        )
        self.group = EventGroup.objects.create(
            id="grp_ref",
            event=self.event,
            name="引用组别",
            category="PRIMARY",
            gender="MALE",
            max_players=11,
        )
        self.match = Match.objects.create(
            id="mtc_ref",
            event_id=self.event.pk,
            group_id=self.group.pk,
            home_team_id="team_a",
            away_team_id="team_b",
            kickoff_at=timezone.now(),
            status="SCHEDULED",
        )

    def _clean_event(self):
        return Event.objects.create(
            id="evt_clean",
            name="待删赛事",
            season="2026",
            registration_opens_at=timezone.now(),
            registration_closes_at=timezone.now(),
            status="DRAFT",
        )

    def _enrollment(self, event_id, group_id, enrollment_id):
        from apps.enrollments.models import Enrollment

        return Enrollment.objects.create(
            id=enrollment_id,
            event_id=event_id,
            group_id=group_id,
            team_id="team_a",
            subject_type="SCHOOL_TEAM",
            contact_name="联系人",
        )

    def _assert_conflict(self, func, *args):
        with self.assertRaises(ApiError) as cm:
            func(*args)
        self.assertEqual(cm.exception.code, STATE_CONFLICT)

    def test_delete_event_blocked_by_match(self):
        from apps.events.services import delete_event

        event = self._clean_event()
        Match.objects.create(
            id="mtc_event",
            event_id=event.pk,
            group_id="grp_any",
            home_team_id="team_a",
            away_team_id="team_b",
            kickoff_at=timezone.now(),
            status="SCHEDULED",
        )
        self._assert_conflict(delete_event, event.pk, event.version)

    def test_delete_event_blocked_by_enrollment(self):
        from apps.events.services import delete_event

        event = self._clean_event()
        self._enrollment(event.pk, "grp_any", "enr_event")
        self._assert_conflict(delete_event, event.pk, event.version)

    def test_delete_group_blocked_by_match(self):
        from apps.events.services import delete_group

        self._assert_conflict(
            delete_group, self.event.pk, self.group.pk, self.group.version
        )

    def test_delete_group_blocked_by_enrollment(self):
        from apps.events.services import delete_group

        group = EventGroup.objects.create(
            id="grp_enr",
            event=self.event,
            name="报名组别",
            category="PRIMARY",
            gender="MALE",
            max_players=11,
        )
        self._enrollment(self.event.pk, group.pk, "enr_group")
        self._assert_conflict(delete_group, self.event.pk, group.pk, group.version)

    def test_delete_match_blocked_by_document(self):
        from apps.events.models import MatchDocument
        from apps.events.services import delete_match

        MatchDocument.objects.create(
            id="doc_ref",
            match_id=self.match.pk,
            document_type="SIGN_IN",
            file_id="file_ref",
        )
        self._assert_conflict(delete_match, self.match.pk, self.match.version)

    def test_delete_match_blocked_by_live_post(self):
        from apps.events.models import LivePost
        from apps.events.services import delete_match

        LivePost.objects.create(id="post_ref", match_id=self.match.pk, text="直播")
        self._assert_conflict(delete_match, self.match.pk, self.match.version)

    def test_delete_match_blocked_by_live_link(self):
        from apps.events.models import LiveLink
        from apps.events.services import delete_match

        LiveLink.objects.create(
            id="link_ref", match_id=self.match.pk, title="直播链接", url="https://x"
        )
        self._assert_conflict(delete_match, self.match.pk, self.match.version)

    def test_delete_match_blocked_by_published_result(self):
        from apps.events.services import delete_match

        self.match.result_published = True
        self.match.save()
        self._assert_conflict(delete_match, self.match.pk, self.match.version)


class RosterValidationTests(TestCase):
    """事件球员必须属于本场有效报名名单；乌龙球必须属于对方队。"""

    def setUp(self):
        from apps.enrollments.models import (
            RosterItem,
            STATUS_APPROVED,
            Enrollment,
        )

        self.event = Event.objects.create(
            id="evt_roster",
            name="名单赛事",
            season="2026",
            registration_opens_at=timezone.now(),
            registration_closes_at=timezone.now(),
            status="DRAFT",
        )
        self.group = EventGroup.objects.create(
            id="grp_roster",
            event=self.event,
            name="名单组别",
            category="PRIMARY",
            gender="MALE",
            max_players=11,
        )
        self.match = Match.objects.create(
            id="mtc_roster",
            event_id=self.event.pk,
            group_id=self.group.pk,
            home_team_id="team_a",
            away_team_id="team_b",
            kickoff_at=timezone.now(),
            status="LIVE",
        )
        self.enr_a = Enrollment.objects.create(
            id="enr_roster_a",
            event_id=self.event.pk,
            group_id=self.group.pk,
            team_id="team_a",
            subject_type="SCHOOL_TEAM",
            contact_name="甲队联系人",
            status=STATUS_APPROVED,
        )
        self.enr_b = Enrollment.objects.create(
            id="enr_roster_b",
            event_id=self.event.pk,
            group_id=self.group.pk,
            team_id="team_b",
            subject_type="SCHOOL_TEAM",
            contact_name="乙队联系人",
            status=STATUS_APPROVED,
        )
        RosterItem.objects.create(
            id="rst_a1", enrollment=self.enr_a, athlete_id="ath_a1", athlete_name="甲一"
        )
        RosterItem.objects.create(
            id="rst_a2", enrollment=self.enr_a, athlete_id="ath_a2", athlete_name="甲二"
        )
        RosterItem.objects.create(
            id="rst_b1", enrollment=self.enr_b, athlete_id="ath_b1", athlete_name="乙一"
        )

    def _goal(self, team_id="team_a", scorer="ath_a1", own_goal=False):
        return {
            "matchVersion": self.match.version,
            "eventType": "GOAL",
            "minute": 10,
            "teamId": team_id,
            "scorerAthleteId": scorer,
            "ownGoal": own_goal,
        }

    def _assert_validation_error(self, payload):
        from apps.events.services import create_match_event

        with self.assertRaises(ApiError) as cm:
            create_match_event(self.match.pk, payload)
        self.assertEqual(cm.exception.code, BUSINESS_VALIDATION_FAILED)

    def test_goal_by_rostered_player_succeeds(self):
        from apps.events.services import create_match_event

        event = create_match_event(self.match.pk, self._goal())
        self.assertEqual(event.scorer_athlete_id, "ath_a1")

    def test_goal_by_unknown_player_rejected(self):
        from apps.events.services import create_match_event

        self._assert_validation_error(self._goal(scorer="ath_unknown"))

    def test_goal_by_opponent_rostered_player_rejected(self):
        from apps.events.services import create_match_event

        self._assert_validation_error(self._goal(scorer="ath_b1"))

    def test_own_goal_by_opponent_player_succeeds(self):
        from apps.events.services import create_match_event

        event = create_match_event(
            self.match.pk, self._goal(scorer="ath_b1", own_goal=True)
        )
        self.assertTrue(event.own_goal)

    def test_own_goal_by_same_team_player_rejected(self):
        from apps.events.services import create_match_event

        self._assert_validation_error(
            self._goal(scorer="ath_a1", own_goal=True)
        )

    def test_penalty_by_unknown_player_rejected(self):
        from apps.events.services import create_match_event

        payload = {
            "matchVersion": self.match.version,
            "eventType": "PENALTY",
            "minute": 20,
            "teamId": "team_a",
            "athleteId": "ath_unknown",
            "penaltyType": "YELLOW_CARD",
            "reason": "犯规",
        }
        self._assert_validation_error(payload)

    def test_substitution_with_unknown_player_rejected(self):
        from apps.events.services import create_match_event

        payload = {
            "matchVersion": self.match.version,
            "eventType": "SUBSTITUTION",
            "minute": 60,
            "teamId": "team_a",
            "outAthleteId": "ath_a1",
            "inAthleteId": "ath_unknown",
        }
        self._assert_validation_error(payload)

    def test_substitution_between_two_rostered_players_succeeds(self):
        from apps.events.services import create_match_event

        payload = {
            "matchVersion": self.match.version,
            "eventType": "SUBSTITUTION",
            "minute": 60,
            "teamId": "team_a",
            "outAthleteId": "ath_a1",
            "inAthleteId": "ath_a2",
        }
        event = create_match_event(self.match.pk, payload)
        self.assertEqual(event.in_athlete_id, "ath_a2")

    def test_player_from_non_approved_enrollment_rejected(self):
        from apps.enrollments.models import Enrollment, RosterItem
        from apps.events.services import create_match_event

        draft = Enrollment.objects.create(
            id="enr_roster_draft",
            event_id=self.event.pk,
            group_id=self.group.pk,
            team_id="team_a",
            subject_type="SCHOOL_TEAM",
            contact_name="草稿联系人",
            status="DRAFT",
        )
        RosterItem.objects.create(
            id="rst_draft", enrollment=draft, athlete_id="ath_draft", athlete_name="草稿"
        )
        self._assert_validation_error(self._goal(scorer="ath_draft"))


class MatchGetScopeTests(TestCase):
    """TEAM 账号的 matchGet 必须复用列表的 TEAM 数据范围。"""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from apps.teams.models import Team

        self.own_team = Team.objects.get(team_no="TEAM001")
        self.other_team = Team.objects.get(team_no="TEAM002")
        self.own_match = Match.objects.create(
            id="mtc_scope_own",
            event_id="evt_scope",
            group_id="grp_scope",
            home_team_id=str(self.own_team.pk),
            away_team_id=str(self.other_team.pk),
            kickoff_at=timezone.now(),
            status="SCHEDULED",
        )
        self.other_match = Match.objects.create(
            id="mtc_scope_other",
            event_id="evt_scope",
            group_id="grp_scope",
            home_team_id="team_x",
            away_team_id="team_y",
            kickoff_at=timezone.now(),
            status="SCHEDULED",
        )

    def _token(self):
        import json

        response = self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "team", "password": "Foot@2026"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content.decode("utf-8"))["data"]["accessToken"]

    def test_team_can_get_own_match(self):
        response = self.client.get(
            f"/api/v1/matches/{self.own_match.pk}",
            HTTP_AUTHORIZATION=f"Bearer {self._token()}",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_team_cannot_get_other_team_match(self):
        import json

        from apps.common.errors import DATA_SCOPE_DENIED

        response = self.client.get(
            f"/api/v1/matches/{self.other_match.pk}",
            HTTP_AUTHORIZATION=f"Bearer {self._token()}",
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(
            json.loads(response.content.decode("utf-8"))["code"], DATA_SCOPE_DENIED
        )


class MatchRefValidationTests(TestCase):
    """对阵的 groupId 必须属于 eventId，禁止跨赛事拼装。"""

    def setUp(self):
        self.event_a = Event.objects.create(
            id="evt_ref_a", name="赛事A", season="2026",
            registration_opens_at=timezone.now(),
            registration_closes_at=timezone.now(), status="DRAFT",
        )
        self.event_b = Event.objects.create(
            id="evt_ref_b", name="赛事B", season="2026",
            registration_opens_at=timezone.now(),
            registration_closes_at=timezone.now(), status="DRAFT",
        )
        self.group_b = EventGroup.objects.create(
            id="grp_ref_b", event=self.event_b, name="B组别",
            category="PRIMARY", gender="MALE", max_players=11,
        )

    def test_create_match_with_group_from_other_event_rejected(self):
        from apps.events.services import create_match

        with self.assertRaises(ApiError) as cm:
            create_match({
                "eventId": self.event_a.pk,
                "groupId": self.group_b.pk,
                "homeTeamId": "team_a",
                "awayTeamId": "team_b",
                "kickoffAt": timezone.now(),
            })
        self.assertEqual(cm.exception.code, BUSINESS_VALIDATION_FAILED)

    def test_create_match_with_own_group_succeeds(self):
        from apps.events.services import create_match

        group_a = EventGroup.objects.create(
            id="grp_ref_a", event=self.event_a, name="A组别",
            category="PRIMARY", gender="MALE", max_players=11,
        )
        match = create_match({
            "eventId": self.event_a.pk,
            "groupId": group_a.pk,
            "homeTeamId": "team_a",
            "awayTeamId": "team_b",
            "kickoffAt": timezone.now(),
        })
        self.assertEqual(match.group_id, group_a.pk)


class EventFieldExclusivityTests(TestCase):
    """三类事件分支字段互斥，混合字段直接 422。"""

    def setUp(self):
        self.match = Match.objects.create(
            id="mtc_excl", event_id="evt_excl", group_id="grp_excl",
            home_team_id="team_a", away_team_id="team_b",
            kickoff_at=timezone.now(), status="LIVE",
        )

    def _assert_validation_error(self, payload):
        from apps.events.services import create_match_event

        with self.assertRaises(ApiError) as cm:
            create_match_event(self.match.pk, payload)
        self.assertEqual(cm.exception.code, BUSINESS_VALIDATION_FAILED)

    def test_goal_with_penalty_fields_rejected(self):
        from apps.events.services import create_match_event

        self._assert_validation_error({
            "matchVersion": self.match.version, "eventType": "GOAL",
            "minute": 10, "teamId": "team_a", "scorerAthleteId": "ath_1",
            "penaltyType": "YELLOW_CARD",
        })

    def test_goal_without_extra_fields_still_passes_team_check_first(self):
        # 混合字段校验发生在 teamId 校验之后：合法分支字段+错误球队仍报 teamId
        from apps.events.services import create_match_event

        with self.assertRaises(ApiError) as cm:
            create_match_event(self.match.pk, {
                "matchVersion": self.match.version, "eventType": "GOAL",
                "minute": 10, "teamId": "team_c", "scorerAthleteId": "ath_1",
            })
        self.assertIn("teamId", str(cm.exception.data))

    def test_penalty_with_goal_fields_rejected(self):
        from apps.events.services import create_match_event

        self._assert_validation_error({
            "matchVersion": self.match.version, "eventType": "PENALTY",
            "minute": 20, "teamId": "team_a", "athleteId": "ath_1",
            "penaltyType": "YELLOW_CARD", "reason": "犯规",
            "scorerAthleteId": "ath_2",
        })

    def test_substitution_with_goal_fields_rejected(self):
        from apps.events.services import create_match_event

        self._assert_validation_error({
            "matchVersion": self.match.version, "eventType": "SUBSTITUTION",
            "minute": 60, "teamId": "team_a",
            "outAthleteId": "ath_1", "inAthleteId": "ath_2",
            "ownGoal": True,
        })


class EventMatchVersionTests(TestCase):
    """事件响应的 matchVersion 必须是父比赛当前版本，避免客户端下一写 409。"""

    def setUp(self):
        from apps.enrollments.models import (
            RosterItem,
            STATUS_APPROVED,
            Enrollment,
        )

        self.match = Match.objects.create(
            id="mtc_mv", event_id="evt_mv", group_id="grp_mv",
            home_team_id="team_a", away_team_id="team_b",
            kickoff_at=timezone.now(), status="LIVE",
        )
        enrollment = Enrollment.objects.create(
            id="enr_mv", event_id="evt_mv", group_id="grp_mv", team_id="team_a",
            subject_type="SCHOOL_TEAM", contact_name="联系人", status=STATUS_APPROVED,
        )
        RosterItem.objects.create(
            id="rst_mv", enrollment=enrollment, athlete_id="ath_1", athlete_name="球员一"
        )

    def test_create_event_returns_current_match_version(self):
        from apps.events.models import Match as MatchModel
        from apps.events.services import create_match_event

        submitted = self.match.version
        event = create_match_event(self.match.pk, {
            "matchVersion": submitted, "eventType": "GOAL",
            "minute": 10, "teamId": "team_a", "scorerAthleteId": "ath_1",
        })
        current = MatchModel.objects.get(pk=self.match.pk).version
        self.assertEqual(event.match_version, current)
        self.assertGreater(event.match_version, submitted)

    def test_delete_event_returns_current_match_version(self):
        from apps.events.models import Match as MatchModel, MatchEvent
        from apps.events.services import create_match_event, delete_match_event

        event = create_match_event(self.match.pk, {
            "matchVersion": self.match.version, "eventType": "GOAL",
            "minute": 10, "teamId": "team_a", "scorerAthleteId": "ath_1",
        })
        fresh_match = MatchModel.objects.get(pk=self.match.pk)
        fresh_event = MatchEvent.objects.get(pk=event.pk)
        deleted = delete_match_event(
            self.match.pk, event.pk, fresh_event.version, fresh_match.version
        )
        current = MatchModel.objects.get(pk=self.match.pk).version
        self.assertEqual(deleted.match_version, current)


class PublicLiveFeedDeletionTests(TestCase):
    """公共直播短轮询必须返回删除增量 deletedIds。"""

    def setUp(self):
        from apps.events.models import LivePost

        self.match = Match.objects.create(
            id="mtc_feed", event_id="evt_feed", group_id="grp_feed",
            home_team_id="team_a", away_team_id="team_b",
            kickoff_at=timezone.now(), status="LIVE", published=True,
        )
        self.kept = LivePost.objects.create(
            id="post_keep", match_id=self.match.pk, text="保留帖", cursor=1,
        )
        self.removed = LivePost.objects.create(
            id="post_gone", match_id=self.match.pk, text="删除帖", cursor=2,
        )

    def test_feed_returns_deleted_ids(self):
        from apps.events.services import public_live_feed

        self.removed.deleted_at = timezone.now()
        self.removed.save()

        feed = public_live_feed(self.match.pk, 0)
        self.assertEqual(feed["deletedIds"], ["post_gone"])
        self.assertEqual([p.pk for p in feed["items"]], ["post_keep"])

    def test_feed_without_deletions_has_empty_deleted_ids(self):
        from apps.events.services import public_live_feed

        feed = public_live_feed(self.match.pk, 0)
        self.assertEqual(feed["deletedIds"], [])


class DeleteMatchResultTests(TestCase):
    """撤下赛果必须递增 score_revision，与其它比分变更路径一致。"""

    def setUp(self):
        self.match = Match.objects.create(
            id="mtc_unpub", event_id="evt_unpub", group_id="grp_unpub",
            home_team_id="team_a", away_team_id="team_b",
            kickoff_at=timezone.now(), status="FINISHED",
            result_published=True, result_type="FORFEIT",
            manual_home_score=3, manual_away_score=0,
            home_score=3, away_score=0, score_revision=5,
        )

    def test_delete_match_result_bumps_score_revision(self):
        from apps.events.services import delete_match_result

        match = delete_match_result(self.match.pk, self.match.version)
        self.assertFalse(match.result_published)
        self.assertEqual(match.result_type, "NORMAL")
        self.assertEqual(int(match.score_revision), 6)


class DisplayAndSchemaTests(TestCase):
    """批次5：查询参数枚举、kickoffAt 时区校验、姓名按本场名单快照。"""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command

        call_command("seed_demo", verbosity=0)

    def setUp(self):
        from apps.enrollments.models import (
            RosterItem,
            STATUS_APPROVED,
            Enrollment,
        )

        self.event = Event.objects.create(
            id="evt_disp", name="展示赛事", season="2026",
            registration_opens_at=timezone.now(),
            registration_closes_at=timezone.now(), status="DRAFT",
        )
        self.group = EventGroup.objects.create(
            id="grp_disp", event=self.event, name="展示组别",
            category="PRIMARY", gender="MALE", max_players=11,
        )
        self.match = Match.objects.create(
            id="mtc_disp", event_id="evt_disp", group_id="grp_disp",
            home_team_id="team_a", away_team_id="team_b",
            kickoff_at=timezone.now(), status="LIVE",
        )
        enrollment = Enrollment.objects.create(
            id="enr_disp", event_id="evt_disp", group_id="grp_disp", team_id="team_a",
            subject_type="SCHOOL_TEAM", contact_name="联系人", status=STATUS_APPROVED,
        )
        RosterItem.objects.create(
            id="rst_disp", enrollment=enrollment,
            athlete_id="ath_disp", athlete_name="本场快照名",
        )

    def test_athlete_name_prefers_match_roster_snapshot(self):
        from apps.enrollments.models import Enrollment, RosterItem
        from apps.events.display import athlete_name_in_match

        # 全局最新一条是其它赛事的名字，本场应取本场快照
        other = Event.objects.create(
            id="evt_other", name="其它赛事", season="2026",
            registration_opens_at=timezone.now(),
            registration_closes_at=timezone.now(), status="DRAFT",
        )
        other_enrollment = Enrollment.objects.create(
            id="enr_other", event_id=other.pk, group_id="grp_other", team_id="team_z",
            subject_type="SCHOOL_TEAM", contact_name="联系人",
        )
        RosterItem.objects.create(
            id="rst_other", enrollment=other_enrollment,
            athlete_id="ath_disp", athlete_name="全局最新名",
        )

        self.assertEqual(athlete_name_in_match(self.match, "ath_disp"), "本场快照名")

    def test_athlete_name_falls_back_to_global(self):
        from apps.events.display import athlete_name_in_match

        self.assertEqual(
            athlete_name_in_match(self.match, "ath_missing"), "ath_missing"
        )

    def test_match_event_out_uses_match_scoped_name(self):
        from apps.events.models import MatchEvent
        from apps.events.schemas import match_event_out

        event = MatchEvent.objects.create(
            id="mev_disp", match_id=self.match.pk, event_type="GOAL",
            minute=5, team_id="team_a", scorer_athlete_id="ath_disp",
        )
        data = match_event_out(event, match=self.match)
        self.assertEqual(data["scorerName"], "本场快照名")

    def test_match_list_rejects_invalid_status_query(self):
        import json

        response = self.client.get(
            "/api/v1/matches?status=NOT_A_STATUS",
            HTTP_AUTHORIZATION=f"Bearer {self._admin_token()}",
        )
        # 仓库约定：ninja 参数校验失败统一映射 400 INVALID_ARGUMENT
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            json.loads(response.content.decode("utf-8"))["code"], "INVALID_ARGUMENT"
        )

    def _admin_token(self):
        import json

        response = self.client.post(
            "/api/v1/auth/login",
            data=json.dumps({"username": "admin", "password": "Foot@2026"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content.decode("utf-8"))["data"]["accessToken"]

    def test_match_create_rejects_naive_kickoff(self):
        import json

        token = self._admin_token()
        payload = {
            "eventId": "evt_disp", "groupId": "grp_disp",
            "homeTeamId": "team_a", "awayTeamId": "team_b",
            "kickoffAt": "2026-06-01T15:00:00", "status": "SCHEDULED",
        }
        response = self.client.post(
            "/api/v1/matches",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            json.loads(response.content.decode("utf-8"))["code"], "INVALID_ARGUMENT"
        )

    def test_match_create_accepts_aware_kickoff(self):
        import json

        token = self._admin_token()
        payload = {
            "eventId": "evt_disp", "groupId": "grp_disp",
            "homeTeamId": "team_a", "awayTeamId": "team_b",
            "kickoffAt": "2026-06-01T15:00:00+08:00", "status": "SCHEDULED",
        }
        response = self.client.post(
            "/api/v1/matches",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            HTTP_IDEMPOTENCY_KEY=f"aware-kickoff-{payload['kickoffAt']}",
        )
        self.assertEqual(response.status_code, 200, response.content)


class EventVersionConflictTests(TestCase):
    """并发安全批次：事务内版本校验——过期 matchVersion 必须拿 409 而不是双写。"""

    def setUp(self):
        from apps.enrollments.models import (
            RosterItem,
            STATUS_APPROVED,
            Enrollment,
        )

        self.match = Match.objects.create(
            id="mtc_vc", event_id="evt_vc", group_id="grp_vc",
            home_team_id="team_a", away_team_id="team_b",
            kickoff_at=timezone.now(), status="LIVE",
        )
        enrollment = Enrollment.objects.create(
            id="enr_vc", event_id="evt_vc", group_id="grp_vc", team_id="team_a",
            subject_type="SCHOOL_TEAM", contact_name="联系人", status=STATUS_APPROVED,
        )
        RosterItem.objects.create(
            id="rst_vc", enrollment=enrollment, athlete_id="ath_vc", athlete_name="球员"
        )

    def _goal(self, match_version):
        return {
            "matchVersion": match_version, "eventType": "GOAL",
            "minute": 10, "teamId": "team_a", "scorerAthleteId": "ath_vc",
        }

    def test_stale_match_version_conflicts(self):
        from apps.common.errors import VERSION_CONFLICT
        from apps.events.models import Match as MatchModel, MatchEvent
        from apps.events.services import create_match_event

        create_match_event(self.match.pk, self._goal(self.match.version))
        self.assertEqual(MatchEvent.objects.filter(match_id=self.match.pk).count(), 1)

        stale = self.match.version  # 第一笔写入后 version 已 bump，此值已过期
        with self.assertRaises(ApiError) as cm:
            create_match_event(self.match.pk, self._goal(stale))
        self.assertEqual(cm.exception.code, VERSION_CONFLICT)
        # 拒绝后不产生第二笔事件，比分不双算
        self.assertEqual(MatchEvent.objects.filter(match_id=self.match.pk).count(), 1)
        match = MatchModel.objects.get(pk=self.match.pk)
        self.assertEqual(match.home_score, 1)

    def test_current_match_version_passes_after_previous_write(self):
        from apps.events.models import Match as MatchModel
        from apps.events.services import create_match_event

        create_match_event(self.match.pk, self._goal(self.match.version))
        current = MatchModel.objects.get(pk=self.match.pk).version
        event = create_match_event(self.match.pk, self._goal(current))
        self.assertEqual(event.match_version, current + 1)


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


# ---------------------------------------------------------------- P1-3 官方名次与奖项（2026-11-02）

import json

from apps.accounts.models import Account
from apps.common.authentication import issue_access_token, make_password
from apps.common.errors import ApiError
from apps.common.ids import new_account_id

from .models import EventAward, EventRanking


class OfficialResultServiceTests(TestCase):
    """服务层：录定唯一性、发布可见性、软删与乐观锁。"""

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

    @staticmethod
    def _rank_payload(team_id, rank, group_id="", medal=""):
        return {
            "teamId": team_id,
            "finalRank": rank,
            "groupId": group_id,
            "medal": medal,
            "remark": "",
        }

    def test_create_ranking_rejects_duplicate_team(self):
        services.create_ranking(self.event.pk, self._rank_payload("T1", 1, medal="GOLD"))
        with self.assertRaises(ApiError) as caught:
            services.create_ranking(self.event.pk, self._rank_payload("T1", 2))
        self.assertEqual(caught.exception.code, "STATE_CONFLICT")
        # 同球队不同组别是另一行，不冲突
        services.create_ranking(
            self.event.pk, self._rank_payload("T1", 1, group_id=self.group.pk)
        )
        self.assertEqual(EventRanking.objects.filter(event_id=self.event.pk).count(), 2)

    def test_ranking_ordered_by_final_rank(self):
        for rank, team in ((3, "C"), (1, "A"), (2, "B")):
            services.create_ranking(self.event.pk, self._rank_payload(team, rank))
        rows = list(services.list_rankings(self.event.pk))
        self.assertEqual([row.final_rank for row in rows], [1, 2, 3])

    def test_public_results_only_published(self):
        published = services.create_ranking(
            self.event.pk, self._rank_payload("A", 1, medal="GOLD")
        )
        services.set_ranking_published(published.pk, published.version, True)
        draft = services.create_ranking(self.event.pk, self._rank_payload("B", 2))
        award = services.create_award(
            self.event.pk,
            {"awardType": "TOP_SCORER", "athleteId": "p1", "teamId": "",
             "groupId": "", "value": "12球", "remark": ""},
        )
        services.set_award_published(award.pk, award.version, True)

        result = services.public_official_results(self.event.pk)
        self.assertEqual([row.team_id for row in result["rankings"]], ["A"])
        self.assertEqual(len(result["awards"]), 1)
        # 管理端列表含未发布行
        self.assertEqual(len(services.list_rankings(self.event.pk)), 2)

    def test_award_requires_subject_and_unique(self):
        payload = {
            "awardType": "MVP", "athleteId": "", "teamId": "",
            "groupId": "", "value": "", "remark": "",
        }
        with self.assertRaises(ApiError) as caught:
            services.create_award(self.event.pk, payload)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

        payload["athleteId"] = "p1"
        services.create_award(self.event.pk, payload)
        with self.assertRaises(ApiError) as caught:
            services.create_award(self.event.pk, payload)
        self.assertEqual(caught.exception.code, "STATE_CONFLICT")
        # OTHER 奖项不强制获奖对象
        services.create_award(
            self.event.pk,
            {"awardType": "OTHER", "athleteId": "", "teamId": "",
             "groupId": "", "value": "最佳组织奖", "remark": ""},
        )

    def test_delete_ranking_is_soft_and_stale_version_conflict(self):
        ranking = services.create_ranking(self.event.pk, self._rank_payload("T1", 1))
        with self.assertRaises(ApiError) as caught:
            services.update_ranking(
                ranking.pk, {**self._rank_payload("T1", 2), "version": 99}
            )
        self.assertEqual(caught.exception.code, "VERSION_CONFLICT")

        services.delete_ranking(ranking.pk, ranking.version)
        self.assertEqual(EventRanking.objects.filter(pk=ranking.pk).count(), 0)
        self.assertEqual(EventRanking.all_objects.filter(pk=ranking.pk).count(), 1)


class OfficialResultApiTests(TestCase):
    """HTTP 层：角色权限、发布流转、门户只读聚合。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="rank-admin",
            display_name="管理员",
            roles=["ADMIN"],
            password=make_password("admin-pass-123"),
            enabled=True,
        )
        cls.athlete = Account.objects.create(
            id=new_account_id(),
            username="rank-athlete",
            display_name="运动员",
            roles=["ATHLETE"],
            password=make_password("athlete-pass-123"),
            enabled=True,
        )
        cls.admin_token = issue_access_token(cls.admin)[0]
        cls.athlete_token = issue_access_token(cls.athlete)[0]

    def setUp(self):
        self.event = _make_event()

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _post(self, path, payload, token):
        return self.client.post(
            path, data=json.dumps(payload), content_type="application/json",
            **self._auth(token),
        )

    def test_write_forbidden_for_athlete(self):
        resp = self._post(
            f"/api/v1/events/{self.event.pk}/rankings",
            {"teamId": "T1", "finalRank": 1},
            self.athlete_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_create_publish_and_public_visibility(self):
        base = f"/api/v1/events/{self.event.pk}/rankings"
        resp = self._post(
            base, {"teamId": "T1", "finalRank": 1, "medal": "GOLD"}, self.admin_token
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertFalse(body["published"])
        self.assertEqual(body["source"], "MANUAL")

        # 重复录入 → 409
        dup = self._post(base, {"teamId": "T1", "finalRank": 2}, self.admin_token)
        self.assertEqual(dup.status_code, 409)
        self.assertEqual(dup.json()["code"], "STATE_CONFLICT")

        # 发布后门户可见
        pub = self._post(
            f"/api/v1/rankings/{body['id']}/publish", {"version": body["version"]},
            self.admin_token,
        )
        self.assertEqual(pub.status_code, 200)
        self.assertTrue(pub.json()["data"]["published"])

        public = self.client.get(f"/api/v1/public/events/{self.event.pk}/rankings")
        self.assertEqual(public.status_code, 200)
        data = public.json()["data"]
        self.assertEqual(len(data["rankings"]), 1)
        self.assertEqual(data["rankings"][0]["finalRank"], 1)
        self.assertEqual(data["rankings"][0]["medal"], "GOLD")
        # 公开投影不含内部字段
        self.assertNotIn("version", data["rankings"][0])

        # 下线后门户不再可见
        down = self._post(
            f"/api/v1/rankings/{body['id']}/unpublish",
            {"version": pub.json()["data"]["version"]},
            self.admin_token,
        )
        self.assertEqual(down.status_code, 200)
        public = self.client.get(f"/api/v1/public/events/{self.event.pk}/rankings")
        self.assertEqual(public.json()["data"]["rankings"], [])

    def test_award_crud_over_http(self):
        base = f"/api/v1/events/{self.event.pk}/awards"
        resp = self._post(
            base,
            {"awardType": "TOP_SCORER", "athleteId": "p1", "value": "12球"},
            self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual(body["awardType"], "TOP_SCORER")

        # 个人奖缺获奖对象 → 400（schema 层拒绝，awardType 合法但主体为空由服务层判）
        empty = self._post(base, {"awardType": "MVP"}, self.admin_token)
        self.assertEqual(empty.status_code, 400)

        listed = self.client.get(base, **self._auth(self.admin_token))
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["data"]["total"], 1)

        removed = self.client.delete(
            f"/api/v1/awards/{body['id']}?version={body['version']}",
            **self._auth(self.admin_token),
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(EventAward.objects.filter(pk=body["id"]).count(), 0)
