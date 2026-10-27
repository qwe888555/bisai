"""Registration publicity service (module 7.3).

Rules implemented here:

* Public snapshots are built from member 4's registration archive, never from
  arbitrary browser input.
* Only the configured field whitelist is copied into ``profile_json``.
* ID-card numbers are masked before the snapshot is written; the raw number is
  never persisted or returned.
* Every public query filters ``status=PUBLISHED``.  Cancelling a record makes
  it disappear from lists and details immediately, while the cancelled row
  remains in the table for audit purposes.
"""

import re
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..common import (
    BusinessError,
    get_field,
    paginated,
    parse_event_id,
    parse_int,
    to_datetime_str,
)
from ..enums import GroupType, PublicityStatus, PublicityType
from ..models import PublicityRecord, ResultBatch
from .publicity_source import get_publicity_source_client

# Defaults are also written to config/settings.py.  Member 4 can override the
# whole dict there without changing any service code.
DEFAULT_FIELD_WHITELIST = {
    PublicityType.ATHLETE: (
        "name",
        "gender",
        "id_card",
        "team_name",
        "school_name",
        "position",
        "jersey_number",
    ),
    PublicityType.COACH: (
        "name",
        "gender",
        "id_card",
        "team_name",
        "school_name",
        "coach_role",
    ),
    PublicityType.TEAM: (
        "team_name",
        "school_name",
        "coach_name",
    ),
}


def mask_id_card(value):
    """Return a display-safe ID-card number.

    18-digit: keep the first 6 and last 4 characters.
    15-digit: keep the first 6 and last 3 characters.
    Other values are masked conservatively.
    """
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return ""
    if len(text) >= 18:
        return f"{text[:6]}{'*' * (len(text) - 10)}{text[-4:]}"
    if len(text) >= 15:
        return f"{text[:6]}{'*' * (len(text) - 9)}{text[-3:]}"
    if len(text) >= 7:
        return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"
    return "*" * len(text)


_ID_CARD_FIELD_ALIASES = {
    "idcard",
    "idnumber",
    "identitycard",
    "identitynumber",
    "sfzh",
    "身份证号",
    "身份证号码",
    "证件号码",
}


def _is_id_card_field(field):
    normalised = re.sub(r"[^a-z0-9]", "", str(field).lower())
    return (
        normalised in _ID_CARD_FIELD_ALIASES
        or str(field).strip() in _ID_CARD_FIELD_ALIASES
    )


def _field_whitelist(publicity_type):
    configured = getattr(
        settings, "SELECTION_PUBLICITY_FIELD_WHITELIST", None
    )
    if not isinstance(configured, dict):
        configured = DEFAULT_FIELD_WHITELIST

    fields = configured.get(publicity_type)
    if fields is None:
        fields = DEFAULT_FIELD_WHITELIST.get(publicity_type, ())
    if not fields:
        raise BusinessError(
            500, f"未配置{PublicityType.label(publicity_type)}公示字段白名单"
        )
    return tuple(str(field) for field in fields)


def _normalise_scalar(value, field_name):
    """Accept only scalar values from the source record."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        return text or None
    raise BusinessError(400, f"白名单字段{field_name}的数据格式不支持")


def _check_length(value, max_length, label):
    if value is not None and len(str(value)) > max_length:
        raise BusinessError(
            400, f"{label}长度不能超过{max_length}个字符"
        )


def _build_snapshot(source):
    """Convert a raw archive record into a whitelisted, masked snapshot."""
    if not isinstance(source.data, dict):
        raise BusinessError(500, "报名档案数据格式错误，data 必须是字典")

    profile = {}
    id_card_masked = ""

    for field in _field_whitelist(source.publicity_type):
        if _is_id_card_field(field):
            if not id_card_masked:
                id_card_masked = mask_id_card(source.data.get(field))
            continue
        value = _normalise_scalar(source.data.get(field), field)
        if value is None:
            continue
        if isinstance(value, str):
            _check_length(value, 255, f"公示字段{field}")
        profile[field] = value

    subject_name = str(profile.get("name") or "").strip()
    team_name = str(profile.get("team_name") or "").strip()
    school_name = str(profile.get("school_name") or "").strip()

    if source.publicity_type == PublicityType.TEAM:
        if not subject_name:
            subject_name = team_name
        if not team_name:
            team_name = subject_name

    if not subject_name:
        raise BusinessError(
            400, "报名档案缺少姓名或球队名称，无法生成公示内容"
        )

    if source.publicity_type in (
        PublicityType.ATHLETE,
        PublicityType.COACH,
    ) and not id_card_masked:
        raise BusinessError(
            400, "运动员/教练员报名档案缺少身份证号，无法生成脱敏内容"
        )

    _check_length(subject_name, 64, "公示对象名称")
    _check_length(team_name, 128, "所属球队")
    _check_length(school_name, 128, "学校")

    return {
        "subject_name": subject_name,
        "team_name": team_name,
        "school_name": school_name,
        "id_card_masked": id_card_masked,
        "profile_json": profile,
    }


def _prepare_source(source, event_id, expected_type=None):
    """Validate one source record and build its public snapshot."""
    source_event_id = parse_event_id(
        source.event_id, "报名档案赛事ID"
    )
    if source_event_id != str(event_id):
        raise BusinessError(
            400, "报名档案不属于当前赛事，不能在本赛事公示"
        )

    publicity_type = PublicityType.parse(
        source.publicity_type, "报名档案公示类型"
    )
    if expected_type and publicity_type != expected_type:
        raise BusinessError(400, "报名档案类型与公示类型不一致")

    group_type = GroupType.parse(
        source.group_type, "报名档案组别"
    )
    city = str(source.city or "").strip()
    if not city:
        raise BusinessError(400, "报名档案缺少市州，无法按市州公示")
    _check_length(city, 64, "市州")

    source_id = str(source.source_id or "").strip()
    if not source_id:
        raise BusinessError(400, "报名档案ID不能为空")
    _check_length(source_id, 64, "报名档案ID")

    return {
        "event_id": event_id,
        "publicity_type": publicity_type,
        "group_type": group_type,
        "city": city,
        "source_id": source_id,
        "snapshot": _build_snapshot(source),
    }


@transaction.atomic
def _publish_source(
    source,
    event_id,
    user_id,
    expected_type=None,
    on_published="error",
):
    prepared = _prepare_source(source, event_id, expected_type)
    snapshot = prepared["snapshot"]

    existing = (
        PublicityRecord.objects.select_for_update()
        .filter(
            event_id=event_id,
            publicity_type=prepared["publicity_type"],
            source_id=prepared["source_id"],
        )
        .first()
    )

    if existing and existing.status == PublicityStatus.PUBLISHED:
        if on_published == "skip":
            return existing, "skipped"
        raise BusinessError(409, "该报名信息已在公示中")

    now = timezone.now()
    if existing:
        record = existing
        action = "republished"
    else:
        record = PublicityRecord(
            event_id=event_id,
            publicity_type=prepared["publicity_type"],
            source_id=prepared["source_id"],
        )
        action = "published"

    record.group_type = prepared["group_type"]
    record.city = prepared["city"]
    record.subject_name = snapshot["subject_name"]
    record.team_name = snapshot["team_name"]
    record.school_name = snapshot["school_name"]
    record.id_card_masked = snapshot["id_card_masked"]
    record.profile_json = snapshot["profile_json"]
    record.status = PublicityStatus.PUBLISHED
    record.published_by = user_id
    record.published_at = now
    record.cancelled_by = ""
    record.cancelled_at = None
    record.cancel_reason = ""
    record.save()

    return record, action


def publish_registration(data, user_id=""):
    """Publish one athlete/coach/team registration."""
    event_id = parse_event_id(
        get_field(data, "event_id", "eventId", label="赛事ID"),
        "event_id",
    )
    publicity_type = PublicityType.parse(
        get_field(
            data,
            "publicity_type",
            "publicityType",
            "type",
            label="公示类型",
        )
    )
    source_id = str(
        get_field(
            data,
            "source_id",
            "sourceId",
            "registration_id",
            "registrationId",
            required=True,
            label="报名档案ID",
        )
    ).strip()

    # Look the record up without an event filter so a record from another event
    # produces the clearer "不属于本赛事" 400 from _prepare_source rather than a
    # misleading 404.  Source IDs are globally unique in both demo and main data.
    source = get_publicity_source_client().get_registration(
        publicity_type, source_id
    )
    if source is None:
        raise BusinessError(404, "未找到对应的报名档案记录")

    record, action = _publish_source(
        source,
        event_id,
        user_id,
        expected_type=publicity_type,
    )
    payload = _serialize(record, management=True)
    payload["action"] = action
    return payload


def _publish_selected_sources(
    sources, event_id, user_id, expected_type=None
):
    """Publish a pre-filtered source list, skipping already-published rows."""
    published = 0
    skipped = 0
    failed = 0
    errors = []
    records = []

    for source in sources:
        try:
            record, action = _publish_source(
                source,
                event_id,
                user_id,
                expected_type=expected_type,
                on_published="skip",
            )
        except BusinessError as exc:
            failed += 1
            errors.append(
                {
                    "source_id": str(source.source_id),
                    "candidate_id": str(
                        getattr(source, "candidate_id", "") or ""
                    ),
                    "message": exc.message,
                }
            )
            continue

        if action == "skipped":
            skipped += 1
            continue

        published += 1
        records.append(_serialize(record, management=True))

    return {
        "published": published,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "records": records,
    }


def batch_publish(data, user_id=""):
    """Publish all source records matching a filter.

    Already-published records are skipped rather than treated as failures, so
    the endpoint can be called repeatedly by a CMS "publish all" button.
    """
    event_id = parse_event_id(
        get_field(data, "event_id", "eventId", label="赛事ID"),
        "event_id",
    )
    group_raw = get_field(
        data, "group_type", "groupType", required=False
    )
    type_raw = get_field(
        data,
        "publicity_type",
        "publicityType",
        "type",
        required=False,
    )
    city = get_field(data, "city", "city_name", "cityName", default="")
    city = str(city or "").strip()

    group_type = GroupType.parse(group_raw) if group_raw else None
    publicity_type = PublicityType.parse(type_raw) if type_raw else None
    if not any((group_type, publicity_type, city)):
        raise BusinessError(
            400, "请至少指定公示类型、组别或市州，避免误公示全部报名信息"
        )

    sources = get_publicity_source_client().list_registrations(
        group_type=group_type,
        city=city,
        publicity_type=publicity_type,
        event_id=event_id,
    )
    return _publish_selected_sources(
        sources, event_id, user_id, expected_type=publicity_type
    )


def publish_result_batch(data, user_id=""):
    """Publish only the athletes in one active result batch.

    This is the shortlist → publicity bridge: ``ResultItem.candidate_id`` is
    the person archive ID used by scoring, while ``PublicityRecord.source_id``
    is the roster row ID.  Mapping through the roster prevents the old batch
    endpoint from publishing every approved registration in the group.
    """
    batch_id = parse_int(
        get_field(
            data,
            "result_batch_id",
            "resultBatchId",
            "batch_id",
            "batchId",
            label="结果批次ID",
        ),
        "结果批次ID",
        min_value=1,
    )
    batch = (
        ResultBatch.objects.prefetch_related("items")
        .filter(pk=batch_id)
        .first()
    )
    if batch is None:
        raise BusinessError(404, "遴选结果批次不存在")
    if not batch.active or batch.stale:
        raise BusinessError(
            409, "仅现行且未过期的结果批次可以发布公示"
        )

    candidate_ids = {
        str(item.candidate_id)
        for item in batch.items.all()
        if str(item.candidate_id or "").strip()
    }
    if not candidate_ids:
        raise BusinessError(400, "结果批次没有入围名单")

    sources = get_publicity_source_client().list_registrations(
        group_type=batch.group_type,
        publicity_type=PublicityType.ATHLETE,
        event_id=batch.event_id,
    )
    selected = {}
    for source in sources:
        candidate_id = str(
            getattr(source, "candidate_id", "") or ""
        ).strip()
        if candidate_id in candidate_ids and candidate_id not in selected:
            selected[candidate_id] = source

    result = _publish_selected_sources(
        selected.values(),
        batch.event_id,
        user_id,
        expected_type=PublicityType.ATHLETE,
    )
    missing = sorted(candidate_ids - set(selected))
    for candidate_id in missing:
        result["failed"] += 1
        result["errors"].append(
            {
                "candidate_id": candidate_id,
                "message": "未找到该运动员的有效报名档案",
            }
        )
    result.update(
        {
            "result_batch_id": batch.id,
            "candidate_count": len(candidate_ids),
        }
    )
    return result


def list_my_publications(account, page=1, size=10):
    """Return all publicity rows belonging to the current person.

    The UI used to poll ``/publicity/manage/`` once per event and match masked
    ID cards by string.  This resolves the account's person once and matches
    roster/coach relation IDs, so cancelled rows are included without N+1
    requests and without name-based guessing.
    """
    person_id = str(getattr(account, "person_id", "") or "").strip()
    if not person_id:
        return paginated([], 0, page, size)

    try:
        from .main_adapters import (
            main_data_available,
            person_publicity_source_ids,
        )

        if not main_data_available():
            return paginated([], 0, page, size)
        source_ids = person_publicity_source_ids(person_id)
    except Exception:  # pragma: no cover - depends on member4 data
        return paginated([], 0, page, size)

    athlete_ids = source_ids.get(PublicityType.ATHLETE) or set()
    coach_ids = source_ids.get(PublicityType.COACH) or set()
    if not athlete_ids and not coach_ids:
        return paginated([], 0, page, size)

    queryset = PublicityRecord.objects.filter(
        Q(
            publicity_type=PublicityType.ATHLETE,
            source_id__in=athlete_ids,
        )
        | Q(
            publicity_type=PublicityType.COACH,
            source_id__in=coach_ids,
        )
    )
    total = queryset.count()
    records = [
        _serialize(record, management=True)
        for record in queryset[(page - 1) * size : page * size]
    ]
    return paginated(records, total, page, size)


@transaction.atomic
def cancel_publicity(record_id, data=None, user_id=""):
    """Cancel one publicity record.  Idempotent for repeated clicks."""
    reason = get_field(
        data or {},
        "reason",
        "cancel_reason",
        "cancelReason",
        default="",
    )
    reason = str(reason or "").strip()
    _check_length(reason, 200, "取消原因")

    record = (
        PublicityRecord.objects.select_for_update()
        .filter(pk=record_id)
        .first()
    )
    if record is None:
        raise BusinessError(404, "公示记录不存在")

    already_cancelled = record.status == PublicityStatus.CANCELLED
    if not already_cancelled:
        record.status = PublicityStatus.CANCELLED
        record.cancelled_by = user_id
        record.cancelled_at = timezone.now()
        record.cancel_reason = reason
        record.save()

    payload = _serialize(record, management=True)
    payload["already_cancelled"] = already_cancelled
    return payload


def _optional_group(value):
    return GroupType.parse(value) if value else None


def _optional_type(value):
    return PublicityType.parse(value) if value else None


def _optional_status(value):
    return PublicityStatus.parse(value) if value else None


def _apply_filters(
    queryset,
    group_type=None,
    city=None,
    publicity_type=None,
    keyword="",
):
    if group_type:
        queryset = queryset.filter(group_type=group_type)
    city = str(city or "").strip()
    if city:
        queryset = queryset.filter(city=city)
    if publicity_type:
        queryset = queryset.filter(publicity_type=publicity_type)
    keyword = str(keyword or "").strip()
    if keyword:
        queryset = queryset.filter(
            Q(subject_name__icontains=keyword)
            | Q(team_name__icontains=keyword)
            | Q(school_name__icontains=keyword)
            | Q(source_id__icontains=keyword)
        )
    return queryset


def list_public(
    event_id,
    group_type=None,
    city=None,
    publicity_type=None,
    keyword="",
    page=1,
    size=10,
):
    """Portal list: published records only."""
    queryset = PublicityRecord.objects.filter(
        event_id=event_id,
        status=PublicityStatus.PUBLISHED,
    )
    queryset = _apply_filters(
        queryset,
        _optional_group(group_type),
        city,
        _optional_type(publicity_type),
        keyword,
    )

    total = queryset.count()
    records = [
        _serialize(record)
        for record in queryset[(page - 1) * size : page * size]
    ]
    return paginated(records, total, page, size)


def list_management(
    event_id,
    group_type=None,
    city=None,
    publicity_type=None,
    status=None,
    keyword="",
    page=1,
    size=10,
):
    """CMS list: includes cancelled records for audit/management."""
    queryset = PublicityRecord.objects.filter(event_id=event_id)
    status_code = _optional_status(status)
    if status_code:
        queryset = queryset.filter(status=status_code)
    queryset = _apply_filters(
        queryset,
        _optional_group(group_type),
        city,
        _optional_type(publicity_type),
        keyword,
    )

    total = queryset.count()
    records = [
        _serialize(record, management=True)
        for record in queryset[(page - 1) * size : page * size]
    ]
    return paginated(records, total, page, size)


def list_cities(event_id, group_type=None, publicity_type=None):
    """Return the city filter options for published records."""
    queryset = PublicityRecord.objects.filter(
        event_id=event_id,
        status=PublicityStatus.PUBLISHED,
    )
    group_code = _optional_group(group_type)
    type_code = _optional_type(publicity_type)
    if group_code:
        queryset = queryset.filter(group_type=group_code)
    if type_code:
        queryset = queryset.filter(publicity_type=type_code)
    return list(
        queryset.values_list("city", flat=True)
        .distinct()
        .order_by("city")
    )


def get_public_detail(record_id):
    """Public detail endpoint; cancelled records look non-existent."""
    record = PublicityRecord.objects.filter(
        pk=record_id,
        status=PublicityStatus.PUBLISHED,
    ).first()
    if record is None:
        raise BusinessError(404, "公示信息不存在或已取消")
    return _serialize(record)


def get_management_detail(record_id):
    record = PublicityRecord.objects.filter(pk=record_id).first()
    if record is None:
        raise BusinessError(404, "公示记录不存在")
    return _serialize(record, management=True)


def _serialize(record, management=False):
    payload = {
        "id": record.id,
        "event_id": record.event_id,
        "group_type": record.group_type,
        "group_type_label": GroupType.label(record.group_type),
        "city": record.city,
        "publicity_type": record.publicity_type,
        "publicity_type_label": PublicityType.label(
            record.publicity_type
        ),
        "name": record.subject_name,
        "team_name": record.team_name,
        "school_name": record.school_name,
        "id_card_masked": record.id_card_masked,
        "fields": record.profile_json or {},
        "status": record.status,
        "status_label": PublicityStatus.label(record.status),
        "published_at": to_datetime_str(record.published_at),
    }

    if management:
        payload.update(
            {
                "source_id": record.source_id,
                "published_by": record.published_by,
                "cancelled_by": record.cancelled_by,
                "cancelled_at": to_datetime_str(record.cancelled_at),
                "cancel_reason": record.cancel_reason,
                "created_at": to_datetime_str(record.created_at),
                "updated_at": to_datetime_str(record.updated_at),
            }
        )

    return payload
