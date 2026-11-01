"""Scoring candidate listing.

The scoring module stores scores against the archive's canonical candidate
id (``RosterItem.athlete_id``).  The frontend cannot use the person archive
search for this because it returns Person ids, which do not match roster ids.
This service exposes the archive client's candidate list so the scoring dialog
can show the athletes that are actually eligible for a group.
"""

from ..common import BusinessError
from ..enums import GroupType
from .archive import get_archive_client

MAX_CANDIDATES = 500


def list_candidates(event_id=None, group_type=None, keyword=""):
    if not group_type:
        raise BusinessError(400, "组别不能为空")

    group_type = GroupType.parse(group_type)
    candidates = get_archive_client().list_candidates(group_type)

    event_text = str(event_id or "").strip()
    keyword = str(keyword or "").strip().lower()

    result = []
    for candidate in candidates:
        if event_text and str(candidate.event_id) != event_text:
            continue
        if keyword and keyword not in candidate.name.lower() and keyword not in (
            candidate.team_name or ""
        ).lower():
            continue
        result.append(
            {
                "candidate_id": str(candidate.candidate_id),
                "event_id": str(candidate.event_id),
                "name": candidate.name,
                "team_name": candidate.team_name,
                "group_type": candidate.group_type,
                "position": candidate.position,
                "id_card_masked": candidate.id_card_masked,
            }
        )
        if len(result) >= MAX_CANDIDATES:
            break

    return result
