"""Integration tests for the main-project adapters.

These only run when ``selection`` is copied into the main backend (the
``apps.*`` modules are installed).  Standalone deployments skip the module.
"""

from datetime import date

from django.apps import apps
from django.test import TestCase
from django.utils import timezone
from unittest import skipUnless

from selection.enums import GroupType, PublicityType
from selection.services.archive import get_archive_client
from selection.services.publicity_source import get_publicity_source_client

MAIN_DATA_AVAILABLE = apps.is_installed("apps.common") and apps.is_installed(
    "apps.enrollments"
)


@skipUnless(MAIN_DATA_AVAILABLE, "main project models are not installed")
class MainAdapterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.common.enums import CoachDuty, Gender, PersonCategory
        from apps.enrollments.models import Enrollment, RosterItem
        from apps.events.models import Event, EventGroup
        from apps.persons.models import AthleteProfile, Person, Region, School
        from apps.teams.models import Team, TeamCoach

        now = timezone.now()
        region = Region.objects.create(name="江城市", code="420100", level=2)
        school = School.objects.create(
            name="江城一中", school_type="SENIOR", region=region
        )
        team = Team.objects.create(
            team_no="T20260001",
            name="江城一中队",
            category_code="SENIOR",
            region=region,
            school=school,
        )
        athlete = Person.objects.create(
            person_no="P20260001",
            full_name="张三",
            gender=Gender.MALE,
            id_card_cipher="cipher-athlete",
            id_card_hash="hash-athlete",
            id_card_masked="510101********0011",
            person_category=PersonCategory.ATHLETE,
            audit_status="APPROVED",
            region=region,
            school=school,
        )
        AthleteProfile.objects.create(
            person=athlete, grade_level="SENIOR", position_code="FW"
        )
        coach = Person.objects.create(
            person_no="P20260002",
            full_name="李四",
            gender=Gender.MALE,
            id_card_cipher="cipher-coach",
            id_card_hash="hash-coach",
            id_card_masked="510101********0022",
            person_category=PersonCategory.COACH,
            audit_status="APPROVED",
            region=region,
            school=school,
        )
        assistant = Person.objects.create(
            person_no="P20260003",
            full_name="王五",
            gender=Gender.MALE,
            id_card_cipher="cipher-assistant",
            id_card_hash="hash-assistant",
            id_card_masked="510101********0033",
            person_category=PersonCategory.COACH,
            audit_status="APPROVED",
            region=region,
            school=school,
        )
        event = Event.objects.create(
            id="evt_2026",
            name="2026 省级联赛",
            season="2026",
            registration_opens_at=now,
            registration_closes_at=now,
            status="REGISTRATION_CLOSED",
        )
        group = EventGroup.objects.create(
            id="evg_u18",
            event=event,
            name="高校男子组",
            category="UNIVERSITY",
            gender="MALE",
            max_players=11,
        )
        enrollment = Enrollment.objects.create(
            id="enr_approved",
            event_id=event.id,
            group_id=group.id,
            team_id=str(team.id),
            subject_type="SCHOOL_TEAM",
            status="APPROVED",
            contact_name="李四",
        )
        RosterItem.objects.create(
            id="rst_approved",
            enrollment=enrollment,
            athlete_id=str(athlete.id),
            athlete_name="张三",
            masked_id_number="510101********0011",
            jersey_number=9,
            position="FW",
        )
        TeamCoach.objects.create(
            team=team,
            person=coach,
            duty=CoachDuty.HEAD_COACH,
            status="CURRENT",
            joined_at=date(2024, 9, 1),
        )
        TeamCoach.objects.create(
            team=team,
            person=assistant,
            duty=CoachDuty.ASSISTANT_COACH,
            status="CURRENT",
            joined_at=date(2024, 9, 1),
        )

        # A non-approved enrollment must never leak into scoring/publicity.
        draft = Enrollment.objects.create(
            id="enr_draft",
            event_id=event.id,
            group_id=group.id,
            team_id=str(team.id),
            subject_type="SCHOOL_TEAM",
            status="DRAFT",
            contact_name="李四",
        )
        RosterItem.objects.create(
            id="rst_draft",
            enrollment=draft,
            athlete_id="per_draft_only",
            athlete_name="草稿球员",
            masked_id_number="510101********0099",
            jersey_number=10,
        )

        cls.event_id = event.id
        cls.group_type = GroupType.HIGHER
        cls.athlete_id = str(athlete.id)
        cls.team_coach_id = str(
            TeamCoach.objects.get(duty=CoachDuty.HEAD_COACH).id
        )

    def test_archive_uses_approved_enrollment(self):
        client = get_archive_client()

        candidate = client.get_candidate("rst_approved", self.event_id)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.candidate_id, self.athlete_id)
        self.assertEqual(candidate.event_id, self.event_id)
        self.assertEqual(candidate.group_type, GroupType.HIGHER)
        self.assertEqual(candidate.name, "张三")
        self.assertEqual(candidate.team_name, "江城一中队")
        self.assertEqual(candidate.position, "FW")

        self.assertIsNone(client.get_candidate("rst_draft", self.event_id))

        candidates = client.list_candidates("UNIVERSITY")
        self.assertEqual([item.candidate_id for item in candidates], [self.athlete_id])

    def test_publicity_source_maps_athlete_coach_team(self):
        client = get_publicity_source_client()

        athlete = client.get_registration(
            PublicityType.ATHLETE, "rst_approved", event_id=self.event_id
        )
        self.assertEqual(athlete.group_type, GroupType.HIGHER)
        self.assertEqual(athlete.city, "江城市")
        self.assertEqual(athlete.data["name"], "张三")
        self.assertEqual(athlete.data["team_name"], "江城一中队")
        self.assertEqual(athlete.data["school_name"], "江城一中")
        self.assertEqual(athlete.data["id_card"], "510101********0011")

        coach = client.get_registration(
            PublicityType.COACH, self.team_coach_id, event_id=self.event_id
        )
        self.assertEqual(coach.data["name"], "李四")
        self.assertEqual(coach.data["coach_role"], "主教练")
        self.assertEqual(coach.city, "江城市")

        team = client.get_registration(
            PublicityType.TEAM, "enr_approved", event_id=self.event_id
        )
        self.assertEqual(team.data["team_name"], "江城一中队")
        self.assertEqual(team.data["coach_name"], "李四")

    def test_publicity_list_filters_and_excludes_drafts(self):
        records = get_publicity_source_client().list_registrations(
            group_type="UNIVERSITY",
            event_id=self.event_id,
        )
        athlete_records = [
            item
            for item in records
            if item.publicity_type == PublicityType.ATHLETE
        ]
        self.assertEqual(
            [item.source_id for item in athlete_records], ["rst_approved"]
        )
        self.assertNotIn(
            "rst_draft", [item.source_id for item in records]
        )

        middle = get_publicity_source_client().list_registrations(
            group_type=GroupType.MIDDLE,
            event_id=self.event_id,
        )
        self.assertEqual(middle, [])
