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
from apps.events.models import (
    RULES_STATUS_CONFIGURED,
    RULES_STATUS_UNCONFIGURED,
    Event,
    EventGroup,
    Match,
    MatchEvent,
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
