"""真实适配器契约测试：仅在 ENROLLMENT_MOCK_CLIENTS=false 时运行。

关掉 mock 后，clients 必须直接读成员4 主档（Team/Person/Material/Event），
产出与 mock 同 shape 的 dict，且不得改写主档。默认（mock）模式下整体跳过。
运行：ENROLLMENT_MOCK_CLIENTS=false python -m pytest apps/enrollments/tests/test_clients_real.py
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.enrollments import clients

pytestmark = pytest.mark.skipif(
    clients.mock_enabled(),
    reason="仅 ENROLLMENT_MOCK_CLIENTS=false 时运行真实适配器契约测试",
)


@pytest.fixture
def real_fixtures(db):
    from apps.accounts.models import Organization
    from apps.events.models import Event, EventGroup
    from apps.materials.models import MaterialType, TeamMaterial
    from apps.persons.models import AthleteProfile, Person
    from apps.teams.models import Team, TeamCoach

    now = timezone.now()
    prov = Organization.objects.create(
        id="r_prov", name="省", org_type="PROVINCE", code="RPROV"
    )
    school = Organization.objects.create(
        id="r_school", name="校", org_type="SCHOOL", code="RSCH", parent=prov
    )
    team_org = Organization.objects.create(
        id="r_team", name="队", org_type="TEAM", code="T_REAL", parent=school
    )

    team = Team.objects.create(team_no="T_REAL", name="真实男子队")

    athlete = Person.objects.create(
        person_no="P_REAL_1",
        full_name="真实运动员",
        gender=1,
        birth_date=date(2012, 5, 1),
        id_card_hash="h_athlete",
        id_card_masked="110***********0001",
        person_category="ATHLETE",
    )
    AthleteProfile.objects.create(
        person=athlete, grade_level="MIDDLE", student_no="G20260001"
    )

    coach = Person.objects.create(
        person_no="P_REAL_C",
        full_name="真实教练",
        gender=1,
        id_card_hash="h_coach",
        id_card_masked="110***********0002",
        person_category="COACH",
    )
    TeamCoach.objects.create(
        team=team,
        person=coach,
        duty="HEAD_COACH",
        status="CURRENT",
        joined_at=date(2026, 1, 1),
    )

    event = Event.objects.create(
        id="r_event",
        name="真实赛事",
        season="2026",
        registration_opens_at=now - timedelta(days=10),
        registration_closes_at=now + timedelta(days=10),
    )
    group = EventGroup.objects.create(
        id="r_group",
        event=event,
        name="初中男子组",
        category="MIDDLE",
        gender="MALE",
        max_players=25,
        required_material_types=["STAMPED_FORM"],
    )
    mtype = MaterialType.objects.create(
        code="STAMPED_FORM", name="盖章报名表", applies_to="TEAM"
    )
    TeamMaterial.objects.create(
        team=team, type=mtype, file_id="f_real", file_name="表.pdf", status="APPROVED"
    )

    return {
        "prov": prov,
        "school": school,
        "team_org": team_org,
        "team": team,
        "athlete": athlete,
        "coach": coach,
        "event": event,
        "group": group,
        "mtype": mtype,
    }


def test_team_ids_for_org_real(real_fixtures):
    c = clients.team_client
    school_ids = c.team_ids_for_org(real_fixtures["school"].pk)
    assert str(real_fixtures["team"].pk) in school_ids
    team_ids = c.team_ids_for_org(real_fixtures["team_org"].pk)
    assert team_ids == [str(real_fixtures["team"].pk)]


def test_get_team_real(real_fixtures):
    c = clients.team_client
    team = real_fixtures["team"]
    data = c.get_team(str(team.pk))
    assert data["id"] == str(team.pk)
    assert data["name"] == "真实男子队"
    assert data["coachIds"] == [str(real_fixtures["coach"].pk)]
    assert data["schoolCode"] == ""
    assert data["schoolName"] == ""


def test_get_coach_real(real_fixtures):
    c = clients.team_client
    coach = real_fixtures["coach"]
    data = c.get_coach(str(coach.pk))
    assert data["id"] == str(coach.pk)
    assert data["name"] == "真实教练"
    assert data["role"] == "主教练"
    assert "maskedPhone" in data


def test_get_person_real(real_fixtures):
    c = clients.person_client
    athlete = real_fixtures["athlete"]
    data = c.get_person(str(athlete.pk))
    assert data["id"] == str(athlete.pk)
    assert data["name"] == "真实运动员"
    assert data["gender"] == "MALE"
    assert data["birthDate"] == "2012-05-01"
    assert data["maskedIdNumber"] == "110***********0001"
    assert data["maskedStudentNumber"]
    assert data["hasIdConflict"] is False
    assert data["banned"] is False


def test_event_config_real(real_fixtures):
    c = clients.event_config_client
    event = real_fixtures["event"]
    group = real_fixtures["group"]

    ev = c.get_event(event.pk)
    assert ev["year"] == 2026
    assert ev["timezone"] == "Asia/Shanghai"

    grp = c.get_group(event.pk, group.pk)
    assert grp["name"] == "初中男子组"
    assert grp["eventId"] == event.pk

    groups = c.get_groups(event.pk)
    assert any(g["id"] == group.pk for g in groups)

    win = c.get_window(event.pk)
    assert win["opensAt"] == event.registration_opens_at
    assert win["closesAt"] == event.registration_closes_at

    rules = c.get_group_rules(event.pk, group.pk)
    assert rules["maxPlayers"] == 25
    assert rules["minPlayers"] is None  # 模型无此字段，归 QUALIFICATION_PENDING
    assert "STAMPED_FORM" in rules["requiredMaterialTypes"]


def test_list_materials_real(real_fixtures):
    c = clients.material_client
    team = real_fixtures["team"]
    athlete = real_fixtures["athlete"]

    team_mats = c.list_materials("TEAM", str(team.pk))
    assert len(team_mats) == 1
    assert team_mats[0]["type"] == "STAMPED_FORM"

    person_mats = c.list_materials("PERSON", str(athlete.pk))
    assert person_mats == []
