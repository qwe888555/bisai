"""Generate and query best-player selection results."""

from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Max

from ..common import (
    BusinessError,
    get_field,
    parse_decimal,
    parse_event_id,
    parse_int,
    to_datetime_str,
    to_float,
)
from ..enums import GroupType, RoundType
from ..models import ResultBatch, ResultItem, Score
from .scoring import calculate_composite, dimension_score


def _detail_json(score):
    if score is None:
        return None
    return {
        "total": to_float(score.total_score),
        "items": [
            {
                "dimension_code": item.dimension_code,
                "dimension_name": item.dimension_name,
                "weight": to_float(item.weight),
                "max_score": to_float(item.max_score),
                "score": to_float(item.score),
                "sort_no": item.sort_no,
            }
            for item in score.items.all()
        ],
    }


def _formula_snapshot(
    result_round, initial_weight, final_weight, tie_code
):
    tie = f"{tie_code}维度" if tie_code else ""
    if result_round == RoundType.FINAL:
        parts = ([tie] if tie else []) + [
            "复选总分",
            "初选总分",
            "档案ID",
        ]
        return (
            f"初选总分×{initial_weight:f} + "
            f"复选总分×{final_weight:f}"
            f"（同分依次比较：{'、'.join(parts)}）"
        )

    parts = ([tie] if tie else []) + ["档案ID"]
    return f"初选总分（同分依次比较：{'、'.join(parts)}）"


def result_item_detail(item):
    return {
        "rank_no": item.rank_no,
        "candidate_id": item.candidate_id,
        "candidate_name": item.candidate_name,
        "team_name": item.team_name,
        "position": item.player_position,
        "initial_score": to_float(item.initial_score),
        "final_score": to_float(item.final_score),
        "composite_score": to_float(item.composite_score),
        "detail": item.detail_json,
    }


def result_batch_detail(batch, include_items=True):
    data = {
        "id": batch.id,
        "event_id": batch.event_id,
        "group_type": batch.group_type,
        "group_type_label": GroupType.label(batch.group_type),
        "result_round": batch.result_round,
        "result_round_label": RoundType.label(batch.result_round),
        "quota": batch.quota,
        "initial_weight": to_float(batch.initial_weight),
        "final_weight": to_float(batch.final_weight),
        "formula_snapshot": batch.formula_snapshot,
        "version_no": batch.version_no,
        "active": batch.active,
        "stale": batch.stale,
        "generated_by": batch.generated_by,
        "generated_at": to_datetime_str(batch.generated_at),
        "items": [],
    }
    if include_items:
        data["items"] = [
            result_item_detail(item) for item in batch.items.all()
        ]
    return data


def generate_result(data, user_id):
    event_id = parse_event_id(
        get_field(data, "event_id", "eventId", required=True, label="赛事ID"),
        "赛事ID",
    )
    group_type = GroupType.parse(get_field(data, "group_type", "groupType"))
    result_round = RoundType.parse(
        get_field(
            data,
            "result_round",
            "resultRound",
            default=RoundType.FINAL,
        )
        or RoundType.FINAL
    )

    default_quota = int(
        getattr(settings, "SELECTION_DEFAULT_QUOTA", 11)
    )
    quota = parse_int(
        get_field(data, "quota", default=default_quota),
        "遴选名额",
        default=default_quota,
        min_value=1,
    )

    default_initial = getattr(
        settings, "SELECTION_DEFAULT_INITIAL_WEIGHT", "0.4"
    )
    default_final = getattr(
        settings, "SELECTION_DEFAULT_FINAL_WEIGHT", "0.6"
    )
    initial_weight = parse_decimal(
        get_field(
            data,
            "initial_weight",
            "initialWeight",
            default=default_initial,
        ),
        "初选权重",
        default=default_initial,
        min_value=Decimal("0"),
    )
    final_weight = parse_decimal(
        get_field(
            data,
            "final_weight",
            "finalWeight",
            default=default_final,
        ),
        "复选权重",
        default=default_final,
        min_value=Decimal("0"),
    )

    if result_round == RoundType.FINAL:
        if initial_weight + final_weight <= 0:
            raise BusinessError(400, "初选权重与复选权重之和必须大于0")
        effective_initial = initial_weight
        effective_final = final_weight
    else:
        # Initial-round results are based on the initial score only.
        effective_initial = Decimal("1")
        effective_final = Decimal("0")

    base_queryset = Score.objects.prefetch_related("items").filter(
        event_id=event_id,
        group_type=group_type,
    )
    initial_scores = {
        score.candidate_id: score
        for score in base_queryset.filter(score_round=RoundType.INITIAL)
    }
    final_scores = {
        score.candidate_id: score
        for score in base_queryset.filter(score_round=RoundType.FINAL)
    }

    rows = []
    if result_round == RoundType.FINAL:
        # A final result requires a final-round score.  The initial score is
        # optional; if missing, the final score is used directly.
        for candidate_id, final_score in final_scores.items():
            initial_score = initial_scores.get(candidate_id)
            composite = calculate_composite(
                initial_score.total_score if initial_score else None,
                final_score.total_score,
                effective_initial,
                effective_final,
            )
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "initial": initial_score,
                    "final": final_score,
                    "composite": composite,
                }
            )
    else:
        # An initial result requires an initial-round score.
        for candidate_id, initial_score in initial_scores.items():
            composite = calculate_composite(
                initial_score.total_score,
                None,
                effective_initial,
                effective_final,
            )
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "initial": initial_score,
                    "final": None,
                    "composite": composite,
                }
            )

    if not rows:
        raise BusinessError(400, "没有可用于生成结果的评分数据")

    tie_code = str(
        getattr(settings, "SELECTION_TIE_BREAK_DIMENSION_CODE", "TECH") or ""
    ).strip().upper()

    def sort_key(row):
        initial = row["initial"]
        final = row["final"]
        tech_source = final if result_round == RoundType.FINAL else initial
        tech = (
            dimension_score(tech_source, tie_code)
            if tech_source is not None
            else Decimal("-1")
        )
        if result_round == RoundType.FINAL:
            return (
                -row["composite"],
                -tech,
                -(final.total_score if final else Decimal("0")),
                -(initial.total_score if initial else Decimal("0")),
                row["candidate_id"],
            )
        return (
            -row["composite"],
            -tech,
            row["candidate_id"],
        )

    rows.sort(key=sort_key)
    selected = rows[:quota]
    formula = _formula_snapshot(
        result_round, effective_initial, effective_final, tie_code
    )

    try:
        with transaction.atomic():
            old_batch = (
                ResultBatch.objects.select_for_update()
                .filter(
                    event_id=event_id,
                    group_type=group_type,
                    active=True,
                )
                .first()
            )
            if old_batch:
                old_batch.active = False
                old_batch.save(update_fields=["active"])

            max_version = (
                ResultBatch.objects.filter(
                    event_id=event_id,
                    group_type=group_type,
                ).aggregate(Max("version_no"))["version_no__max"]
                or 0
            )

            batch = ResultBatch.objects.create(
                event_id=event_id,
                group_type=group_type,
                result_round=result_round,
                quota=quota,
                initial_weight=effective_initial,
                final_weight=effective_final,
                formula_snapshot=formula,
                version_no=max_version + 1,
                active=True,
                stale=False,
                generated_by=user_id,
            )

            result_items = []
            for rank, row in enumerate(selected, start=1):
                initial = row["initial"]
                final = row["final"]
                source = final if final is not None else initial
                result_items.append(
                    ResultItem(
                        batch=batch,
                        rank_no=rank,
                        candidate_id=row["candidate_id"],
                        candidate_name=source.candidate_name,
                        team_name=source.team_name or "",
                        player_position=source.player_position or "",
                        initial_score=(
                            initial.total_score if initial else None
                        ),
                        final_score=final.total_score if final else None,
                        composite_score=row["composite"],
                        detail_json={
                            "initial": _detail_json(initial),
                            "final": _detail_json(final),
                        },
                    )
                )
            ResultItem.objects.bulk_create(result_items)
    except IntegrityError:
        raise BusinessError(409, "遴选结果正在生成，请稍后重试")

    batch = (
        ResultBatch.objects.prefetch_related("items")
        .get(pk=batch.id)
    )
    return result_batch_detail(batch)


def latest_result(event_id, group_type):
    batch = (
        ResultBatch.objects.prefetch_related("items")
        .filter(
            event_id=event_id,
            group_type=group_type,
            active=True,
        )
        .order_by("-version_no")
        .first()
    )
    if not batch:
        raise BusinessError(404, "尚未生成该组别的遴选结果")
    return result_batch_detail(batch)


def result_history(event_id, group_type, limit=20):
    batches = (
        ResultBatch.objects.filter(
            event_id=event_id,
            group_type=group_type,
        )
        .annotate(item_count=Count("items"))
        .order_by("-generated_at", "-id")[:limit]
    )
    result = []
    for batch in batches:
        detail = result_batch_detail(batch, include_items=False)
        detail["item_count"] = batch.item_count
        result.append(detail)
    return result
