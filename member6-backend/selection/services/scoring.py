"""Fixed weighted scoring formula and score serialization."""

from decimal import Decimal, ROUND_HALF_UP

from ..common import BusinessError, to_datetime_str, to_float
from ..enums import GroupType, RoundType

HUNDRED = Decimal("100")
TWO_PLACES = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")


def _get(obj, name):
    if isinstance(obj, dict):
        return obj[name]
    return getattr(obj, name)


def calculate_total(items):
    """Calculate a 0-100 weighted total.

    Each dimension is first normalized to a percentage of its own max score,
    then weighted.  Normalizing by the sum of weights means a template whose
    weights do not total exactly 100 still produces a comparable result; the
    template validator normally enforces a 100 total anyway.
    """
    weighted_sum = Decimal("0")
    weight_sum = Decimal("0")

    for item in items:
        score = Decimal(str(_get(item, "score")))
        max_score = Decimal(str(_get(item, "max_score")))
        weight = Decimal(str(_get(item, "weight")))

        if max_score <= 0:
            raise BusinessError(400, "维度评分上限必须大于0")
        if score < 0 or score > max_score:
            raise BusinessError(
                400, f"维度得分必须在0到{max_score}之间，当前为{score}"
            )

        ratio = (score / max_score).quantize(
            SIX_PLACES, rounding=ROUND_HALF_UP
        )
        weighted_sum += ratio * HUNDRED * weight
        weight_sum += weight

    if weight_sum <= 0:
        raise BusinessError(400, "评分权重之和必须大于0")

    return (weighted_sum / weight_sum).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )


def calculate_composite(initial, final, initial_weight, final_weight):
    """Combine initial and final totals.

    If only one round exists, that round's total is used directly.  Otherwise
    the result is normalized by the sum of weights, so 0.4/0.6 and 40/60 are
    equivalent.
    """
    initial = Decimal(str(initial)) if initial is not None else None
    final = Decimal(str(final)) if final is not None else None
    initial_weight = Decimal(str(initial_weight))
    final_weight = Decimal(str(final_weight))

    if initial is None and final is None:
        return None

    if initial is not None and final is not None:
        weight_sum = initial_weight + final_weight
        if weight_sum <= 0:
            raise BusinessError(400, "初选权重与复选权重之和必须大于0")
        value = (initial * initial_weight + final * final_weight) / weight_sum
    elif initial is not None:
        value = initial
    else:
        value = final

    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def dimension_score(score, dimension_code):
    """Read one dimension score from a prefetched Score record."""
    if not dimension_code:
        return Decimal("-1")
    for item in score.items.all():
        if item.dimension_code == dimension_code:
            return item.score
    return Decimal("-1")


def score_item_detail(item):
    return {
        "id": item.id,
        "dimension_code": item.dimension_code,
        "dimension_name": item.dimension_name,
        "weight": to_float(item.weight),
        "max_score": to_float(item.max_score),
        "score": to_float(item.score),
        "sort_no": item.sort_no,
    }


def score_detail(score, include_items=True):
    data = {
        "id": score.id,
        "event_id": score.event_id,
        "group_type": score.group_type,
        "group_type_label": GroupType.label(score.group_type),
        "round": score.score_round,
        "round_label": RoundType.label(score.score_round),
        "template_id": score.template_id,
        "candidate_id": score.candidate_id,
        "candidate_name": score.candidate_name,
        "team_name": score.team_name,
        "position": score.player_position,
        "total_score": to_float(score.total_score),
        "version": score.version,
        "created_by": score.created_by,
        "created_at": to_datetime_str(score.created_at),
        "updated_by": score.updated_by,
        "updated_at": to_datetime_str(score.updated_at),
    }
    if include_items:
        data["items"] = [score_item_detail(item) for item in score.items.all()]
    else:
        data["items"] = []
    return data
