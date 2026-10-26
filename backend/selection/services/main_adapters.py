"""Real-data adapters for the main project's member4 tables.

Member6 was originally developed against deterministic demo data.  When the
``selection`` app is copied into the main ``backend/`` project, the models
below are available and ``SELECTION_ARCHIVE_MODE=auto`` /
``SELECTION_PUBLICITY_SOURCE_MODE=auto`` switch to them automatically.

Mapping (main -> member6):

======================  ==========================================
main model/field        member6 concept
======================  ==========================================
Enrollment.event_id     event_id (string ``evt_...``)
EventGroup.category     group_type (``UNIVERSITY`` -> ``HIGHER``)
RosterItem.athlete_id   scoring candidate_id (person id)
RosterItem.id           publicity source_id for ATHLETE
TeamCoach.id            publicity source_id for COACH
Enrollment.id           publicity source_id for TEAM
Team.name               team_name
Person.id_card_masked   id_card (already masked; masking is idempotent)
Person.region.name      city
Team.school / Person.school -> school_name
======================  ==========================================

Only APPROVED enrollments are exposed.  Draft/rejected/withdrawn rows must
never appear on the public publicity page or in scoring.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from django.apps import apps as django_apps

from ..enums import GroupType, PublicityType
from .archive import SelectionArchiveClient, SelectionCandidate
from .publicity_source import PublicitySourceClient, PublicitySourceRecord

_CATEGORY_TO_GROUP = {
    "PRIMARY": GroupType.PRIMARY,
    "MIDDLE": GroupType.MIDDLE,
    "UNIVERSITY": GroupType.HIGHER,
}
_GROUP_TO_CATEGORIES = {
    GroupType.PRIMARY: ("PRIMARY",),
    GroupType.MIDDLE: ("MIDDLE",),
    GroupType.HIGHER: ("UNIVERSITY",),
}

_REQUIRED_APPS = (
    "apps.enrollments",
    "apps.persons",
    "apps.teams",
    "apps.events",
)


def main_data_available() -> bool:
    """True when the member4 models are installed in this Django project."""
    return all(django_apps.is_installed(name) for name in _REQUIRED_APPS)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _as_int(value):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _get_person(person_id):
    from apps.persons.models import Person

    pk = _as_int(person_id)
    if pk is None:
        return None
    return (
        Person.objects.filter(pk=pk, is_deleted=False)
        .select_related("region", "school")
        .first()
    )


def _get_team(team_id):
    from apps.teams.models import Team

    pk = _as_int(team_id)
    if pk is None:
        return None
    return (
        Team.objects.filter(pk=pk, is_deleted=False)
        .select_related("region", "school")
        .first()
    )


def _get_group(group_id):
    from apps.events.models import EventGroup

    return EventGroup.objects.filter(pk=group_id).first()


def _group_map(group_ids: Iterable[str]) -> Dict[str, object]:
    from apps.events.models import EventGroup

    ids = list({str(group_id) for group_id in group_ids if group_id})
    if not ids:
        return {}
    return {
        str(group.id): group
        for group in EventGroup.objects.filter(id__in=ids)
    }


def _group_type(group):
    if group is None:
        return None
    return _CATEGORY_TO_GROUP.get(group.category)


def _status_approved():
    from apps.enrollments.models import STATUS_APPROVED

    return STATUS_APPROVED


def _approved_enrollments(event_id=None):
    from apps.enrollments.models import Enrollment

    queryset = Enrollment.objects.filter(status=_status_approved())
    if event_id is not None:
        queryset = queryset.filter(event_id=str(event_id))
    return queryset.order_by("-created_at", "-id")


def _region_name(*objects):
    for obj in objects:
        region = getattr(obj, "region", None) if obj is not None else None
        if region is not None and region.name:
            return region.name
    return ""


def _school_name(*objects):
    for obj in objects:
        school = getattr(obj, "school", None) if obj is not None else None
        if school is not None and school.name:
            return school.name
    return ""


def _person_name(person):
    return getattr(person, "full_name", "") if person is not None else ""


def _person_gender(person):
    if person is None:
        return ""
    try:
        return person.get_gender_display()
    except Exception:
        return ""


def _person_position(person):
    if person is None:
        return ""
    try:
        profile = person.athlete_profile
    except Exception:
        return ""
    return getattr(profile, "position_code", "") or ""


def _team_name(team, fallback=""):
    return (getattr(team, "name", "") if team is not None else "") or fallback


def _athlete_record(item, enrollment, group, person, team):
    group_type = _group_type(group)
    if group_type is None:
        return None
    name = item.athlete_name or _person_name(person)
    data = {
        "name": name,
        "gender": _person_gender(person),
        "id_card": (
            (getattr(person, "id_card_masked", "") if person else "")
            or item.masked_id_number
        ),
        "team_name": _team_name(team, str(enrollment.team_id)),
        "school_name": _school_name(person, team),
        "position": item.position or _person_position(person),
        "jersey_number": item.jersey_number,
    }
    return PublicitySourceRecord(
        source_id=str(item.id),
        event_id=str(enrollment.event_id),
        group_type=group_type,
        city=_region_name(person, team),
        publicity_type=PublicityType.ATHLETE,
        data=data,
    )


def _coach_record(relation, enrollment, group, person, team):
    group_type = _group_type(group)
    if group_type is None:
        return None
    data = {
        "name": _person_name(person),
        "gender": _person_gender(person),
        "id_card": getattr(person, "id_card_masked", "") if person else "",
        "team_name": _team_name(team, str(enrollment.team_id)),
        "school_name": _school_name(person, team),
        "coach_role": relation.get_duty_display(),
    }
    return PublicitySourceRecord(
        source_id=str(relation.id),
        event_id=str(enrollment.event_id),
        group_type=group_type,
        city=_region_name(person, team),
        publicity_type=PublicityType.COACH,
        data=data,
    )


def _head_coach_name(team_id):
    from apps.teams.models import TeamCoach

    numeric_team_id = _as_int(team_id)
    if numeric_team_id is None:
        return ""
    relations = TeamCoach.objects.filter(
        team_id=numeric_team_id, status="CURRENT"
    ).select_related("person")
    # Lexicographic duty ordering would put ASSISTANT_COACH before HEAD_COACH.
    relation = relations.filter(duty="HEAD_COACH").order_by("id").first()
    if relation is None:
        relation = relations.order_by("id").first()
    return _person_name(getattr(relation, "person", None)) if relation else ""


def _team_record(enrollment, group, team):
    group_type = _group_type(group)
    if group_type is None:
        return None
    team_name = _team_name(team, str(enrollment.team_id))
    return PublicitySourceRecord(
        source_id=str(enrollment.id),
        event_id=str(enrollment.event_id),
        group_type=group_type,
        city=_region_name(team),
        publicity_type=PublicityType.TEAM,
        data={
            "team_name": team_name,
            "school_name": _school_name(team),
            "coach_name": _head_coach_name(enrollment.team_id),
        },
    )


def _team_enrollment_map(enrollments):
    """One representative enrollment per team, preserving the newest row."""
    mapping = {}
    for enrollment in enrollments:
        mapping.setdefault(str(enrollment.team_id), enrollment)
    return mapping


# ---------------------------------------------------------------------------
# Archive (scoring)
# ---------------------------------------------------------------------------
class MainProjectArchiveClient(SelectionArchiveClient):
    def get_candidate(
        self, candidate_id: str, event_id: Optional[str] = None
    ) -> Optional[SelectionCandidate]:
        from apps.enrollments.models import RosterItem

        queryset = RosterItem.objects.filter(
            enrollment__status=_status_approved()
        ).select_related("enrollment")
        if event_id is not None:
            queryset = queryset.filter(enrollment__event_id=str(event_id))

        text = str(candidate_id)
        item = queryset.filter(id=text).first()
        if item is None:
            item = queryset.filter(athlete_id=text).order_by(
                "-enrollment__created_at", "-id"
            ).first()
        if item is None:
            return None

        enrollment = item.enrollment
        group = _get_group(enrollment.group_id)
        group_type = _group_type(group)
        if group_type is None:
            return None
        person = _get_person(item.athlete_id)
        team = _get_team(enrollment.team_id)

        # Normalize to the person/athlete id so scoring by roster id and by
        # athlete id produce the same unique score row.
        normalized_id = str(item.athlete_id or item.id)
        return SelectionCandidate(
            candidate_id=normalized_id,
            event_id=str(enrollment.event_id),
            name=item.athlete_name or _person_name(person),
            team_name=_team_name(team, str(enrollment.team_id)),
            group_type=group_type,
            position=item.position or _person_position(person),
            id_card_masked=(
                (getattr(person, "id_card_masked", "") if person else "")
                or item.masked_id_number
            ),
        )

    def list_candidates(self, group_type: str) -> List[SelectionCandidate]:
        from apps.enrollments.models import Enrollment, RosterItem

        group_type = GroupType.parse(group_type)
        group_ids = list(
            Enrollment.objects.filter(status=_status_approved())
            .values_list("group_id", flat=True)
            .distinct()
        )
        groups = _group_map(group_ids)
        wanted = _GROUP_TO_CATEGORIES.get(group_type)
        if not wanted:
            return []
        matching_group_ids = [
            group_id
            for group_id, group in groups.items()
            if group.category in wanted
        ]
        if not matching_group_ids:
            return []

        items = (
            RosterItem.objects.filter(
                enrollment__status=_status_approved(),
                enrollment__group_id__in=matching_group_ids,
            )
            .select_related("enrollment")
            .order_by("athlete_id", "-enrollment__created_at", "-id")
        )
        result = []
        seen = set()
        for item in items:
            if item.athlete_id in seen:
                continue
            seen.add(item.athlete_id)
            group = groups.get(str(item.enrollment.group_id))
            person = _get_person(item.athlete_id)
            team = _get_team(item.enrollment.team_id)
            group_type_code = _group_type(group)
            if group_type_code is None:
                continue
            result.append(
                SelectionCandidate(
                    candidate_id=str(item.athlete_id or item.id),
                    event_id=str(item.enrollment.event_id),
                    name=item.athlete_name or _person_name(person),
                    team_name=_team_name(
                        team, str(item.enrollment.team_id)
                    ),
                    group_type=group_type_code,
                    position=item.position or _person_position(person),
                    id_card_masked=(
                        (
                            getattr(person, "id_card_masked", "")
                            if person
                            else ""
                        )
                        or item.masked_id_number
                    ),
                )
            )
        return result


# ---------------------------------------------------------------------------
# Registration publicity
# ---------------------------------------------------------------------------
class MainProjectPublicitySourceClient(PublicitySourceClient):
    def get_registration(
        self,
        publicity_type: str,
        source_id: str,
        event_id: Optional[str] = None,
    ) -> Optional[PublicitySourceRecord]:
        try:
            type_code = PublicityType.parse(publicity_type)
        except Exception:
            return None
        source_id = str(source_id)

        if type_code == PublicityType.ATHLETE:
            return self._athlete_registration(source_id, event_id)
        if type_code == PublicityType.COACH:
            return self._coach_registration(source_id, event_id)
        if type_code == PublicityType.TEAM:
            return self._team_registration(source_id, event_id)
        return None

    def list_registrations(
        self,
        group_type: Optional[str] = None,
        city: Optional[str] = None,
        publicity_type: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> List[PublicitySourceRecord]:
        enrollments = list(
            self._enrollments(event_id=event_id, group_type=group_type)
        )
        if not enrollments:
            return []

        try:
            type_code = (
                PublicityType.parse(publicity_type)
                if publicity_type
                else None
            )
        except Exception:
            return []

        records: List[PublicitySourceRecord] = []
        if type_code in (None, PublicityType.ATHLETE):
            records.extend(self._athlete_records(enrollments))
        if type_code in (None, PublicityType.COACH):
            records.extend(self._coach_records(enrollments))
        if type_code in (None, PublicityType.TEAM):
            records.extend(self._team_records(enrollments))

        city_text = str(city or "").strip()
        if city_text:
            records = [
                record for record in records if record.city == city_text
            ]
        return records

    # -- internal queries -------------------------------------------------
    def _enrollments(self, event_id=None, group_type=None):
        queryset = _approved_enrollments(event_id)
        if group_type:
            group_type = GroupType.parse(group_type)
            categories = _GROUP_TO_CATEGORIES.get(group_type)
            if not categories:
                return queryset.none()
            from apps.events.models import EventGroup

            group_ids = EventGroup.objects.filter(
                category__in=categories
            ).values_list("id", flat=True)
            queryset = queryset.filter(group_id__in=list(group_ids))
        return queryset

    def _athlete_records(self, enrollments):
        from apps.enrollments.models import RosterItem

        enrollment_by_id = {
            str(enrollment.id): enrollment for enrollment in enrollments
        }
        groups = _group_map(
            enrollment.group_id for enrollment in enrollments
        )
        items = RosterItem.objects.filter(
            enrollment_id__in=list(enrollment_by_id)
        ).select_related("enrollment")
        records = []
        for item in items:
            enrollment = enrollment_by_id.get(str(item.enrollment_id))
            if enrollment is None:
                continue
            group = groups.get(str(enrollment.group_id))
            person = _get_person(item.athlete_id)
            team = _get_team(enrollment.team_id)
            record = _athlete_record(
                item, enrollment, group, person, team
            )
            if record is not None:
                records.append(record)
        return records

    def _coach_records(self, enrollments):
        from apps.teams.models import TeamCoach

        teams = _team_enrollment_map(enrollments)
        teams = {team_id: enrollment for team_id, enrollment in teams.items()}
        groups = _group_map(
            enrollment.group_id for enrollment in teams.values()
        )
        numeric_team_ids = [
            pk for pk in (_as_int(team_id) for team_id in teams) if pk
        ]
        if not numeric_team_ids:
            return []
        relations = (
            TeamCoach.objects.filter(
                team_id__in=numeric_team_ids, status="CURRENT"
            )
            .select_related("person", "team", "team__region", "team__school")
            .order_by("team_id", "duty", "id")
        )
        records = []
        for relation in relations:
            enrollment = teams.get(str(relation.team_id))
            if enrollment is None:
                continue
            group = groups.get(str(enrollment.group_id))
            record = _coach_record(
                relation,
                enrollment,
                group,
                relation.person,
                relation.team,
            )
            if record is not None:
                records.append(record)
        return records

    def _team_records(self, enrollments):
        groups = _group_map(
            enrollment.group_id for enrollment in enrollments
        )
        records = []
        for enrollment in enrollments:
            group = groups.get(str(enrollment.group_id))
            team = _get_team(enrollment.team_id)
            record = _team_record(enrollment, group, team)
            if record is not None:
                records.append(record)
        return records

    def _athlete_registration(self, source_id, event_id):
        from apps.enrollments.models import RosterItem

        queryset = RosterItem.objects.filter(
            enrollment__status=_status_approved()
        ).select_related("enrollment")
        if event_id is not None:
            queryset = queryset.filter(enrollment__event_id=str(event_id))
        item = queryset.filter(id=source_id).first()
        if item is None:
            item = queryset.filter(athlete_id=source_id).order_by(
                "-enrollment__created_at", "-id"
            ).first()
        if item is None:
            return None
        enrollment = item.enrollment
        group = _get_group(enrollment.group_id)
        return _athlete_record(
            item,
            enrollment,
            group,
            _get_person(item.athlete_id),
            _get_team(enrollment.team_id),
        )

    def _coach_registration(self, source_id, event_id):
        from apps.teams.models import TeamCoach

        queryset = TeamCoach.objects.filter(status="CURRENT").select_related(
            "person", "team", "team__region", "team__school"
        )
        if event_id is not None:
            teams = _team_enrollment_map(
                _approved_enrollments(event_id)
            )
            numeric_team_ids = [
                pk
                for pk in (_as_int(team_id) for team_id in teams)
                if pk
            ]
            if not numeric_team_ids:
                return None
            queryset = queryset.filter(team_id__in=numeric_team_ids)
        else:
            teams = _team_enrollment_map(
                _approved_enrollments()
            )

        relation = queryset.filter(id=_as_int(source_id)).first()
        if relation is None:
            relation = queryset.filter(person_id=_as_int(source_id)).order_by(
                "-joined_at", "-id"
            ).first()
        if relation is None:
            return None
        enrollment = teams.get(str(relation.team_id))
        if enrollment is None:
            return None
        group = _get_group(enrollment.group_id)
        return _coach_record(
            relation,
            enrollment,
            group,
            relation.person,
            relation.team,
        )

    def _team_registration(self, source_id, event_id):
        from apps.enrollments.models import Enrollment

        queryset = _approved_enrollments(event_id)
        enrollment = queryset.filter(id=source_id).first()
        if enrollment is None:
            enrollment = queryset.filter(team_id=source_id).order_by(
                "-created_at", "-id"
            ).first()
        if enrollment is None:
            return None
        group = _get_group(enrollment.group_id)
        return _team_record(
            enrollment, group, _get_team(enrollment.team_id)
        )
