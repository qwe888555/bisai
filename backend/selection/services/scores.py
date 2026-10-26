"""Score entry, editing, listing and round summaries."""

from types import SimpleNamespace

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from ..common import (
    BusinessError,
    get_field,
    paginated,
    parse_candidate_id,
    parse_decimal,
    parse_event_id,
    parse_int,
)
from ..enums import GroupType, RoundType
from ..models import ResultBatch, Score, ScoreItem, ScoreTemplate
from .archive import get_archive_client
from .scoring import calculate_total, dimension_score, score_detail
from .templates import normalize_dimension_code


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def _requested_scores(raw_scores):
    if not isinstance(raw_scores, list) or not raw_scores:
        raise BusinessError(400, "评分明细不能为空")

    requested = {}
    for index, raw in enumerate(raw_scores):
        if not isinstance(raw, dict):
            raise BusinessError(400, f"第{index + 1}条评分明细格式错误")
        code = normalize_dimension_code(
            get_field(raw, "dimension_code", "dimensionCode")
        )
        if code in requested:
            raise BusinessError(400, f"评分维度重复: {code}")
        requested[code] = parse_decimal(
            get_field(raw, "score"),
            f"维度 {code} 的得分",
        )
    return requested


def _build_snapshot(template_items, requested):
    """Match request dimensions to a template and compute the total."""
    template_codes = {item.dimension_code for item in template_items}
    unknown = set(requested) - template_codes
    if unknown:
        raise BusinessError(
            400, f"未知评分维度: {', '.join(sorted(unknown))}"
        )

    values = []
    for item in template_items:
        if item.dimension_code not in requested:
            raise BusinessError(
                400,
                f"缺少评分维度: {item.dimension_name}({item.dimension_code})",
            )
        value = requested[item.dimension_code]
        if value < 0 or value > item.max_score:
            raise BusinessError(
                400,
                f"维度 {item.dimension_code} 的得分必须在0到"
                f"{item.max_score}之间，当前为{value}",
            )
        values.append({"template_item": item, "score": value})

    total = calculate_total(
        [
            SimpleNamespace(
                score=entry["score"],
                max_score=entry["template_item"].max_score,
                weight=entry["template_item"].weight,
            )
            for entry in values
        ]
    )
    return values, total


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def get_score_detail(score_id):
    score = (
        Score.objects.prefetch_related("items")
        .filter(pk=score_id)
        .first()
    )
    if not score:
        raise BusinessError(404, "评分记录不存在")
    return score_detail(score)


def list_scores(event_id, group_type, score_round, keyword, page, size):
    queryset = Score.objects.prefetch_related("items").filter(
        event_id=event_id,
        group_type=group_type,
        score_round=score_round,
    )

    keyword = (keyword or "").strip()
    if keyword:
        if len(keyword) > 50:
            raise BusinessError(400, "keyword长度不能超过50个字符")
        queryset = queryset.filter(
            Q(candidate_name__icontains=keyword)
            | Q(team_name__icontains=keyword)
        )

    total = queryset.count()
    offset = (page - 1) * size
    records = list(
        queryset.order_by("-total_score", "candidate_id")[
            offset : offset + size
        ]
    )
    return paginated(
        [score_detail(score) for score in records],
        total,
        page,
        size,
    )


def summary(event_id, group_type, score_round):
    """Return ranked scores, using the same tie-break as result generation."""
    scores = list(
        Score.objects.prefetch_related("items").filter(
            event_id=event_id,
            group_type=group_type,
            score_round=score_round,
        )
    )
    tie_code = getattr(settings, "SELECTION_TIE_BREAK_DIMENSION_CODE", "TECH")

    scores.sort(
        key=lambda score: (
            -score.total_score,
            -dimension_score(score, tie_code),
            score.candidate_id,
        )
    )

    result = []
    for rank, score in enumerate(scores, start=1):
        detail = score_detail(score)
        detail["rank_no"] = rank
        result.append(detail)
    return result


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
@transaction.atomic
def create_score(data, user_id):
    event_id = parse_event_id(
        get_field(data, "event_id", "eventId", required=True, label="赛事ID"),
        "赛事ID",
    )
    group_type = GroupType.parse(get_field(data, "group_type", "groupType"))
    score_round = RoundType.parse(
        get_field(data, "round", "score_round", "scoreRound")
    )
    template_id = parse_int(
        get_field(
            data, "template_id", "templateId", required=True, label="模板ID"
        ),
        "模板ID",
        min_value=1,
    )
    candidate_id = parse_candidate_id(
        get_field(
            data,
            "candidate_id",
            "candidateId",
            required=True,
            label="球员ID",
        ),
        "球员ID",
    )
    raw_scores = get_field(
        data, "scores", required=True, label="评分明细"
    )

    template = (
        ScoreTemplate.objects.prefetch_related("items")
        .filter(pk=template_id)
        .first()
    )
    if not template:
        raise BusinessError(404, "评分模板不存在")
    if template.status != 1:
        raise BusinessError(400, "评分模板已停用")
    if template.group_type != group_type:
        raise BusinessError(400, "评分模板组别与请求不一致")
    if template.score_round != score_round:
        raise BusinessError(400, "评分模板轮次与请求不一致")

    candidate = get_archive_client().get_candidate(
        candidate_id, event_id=event_id
    )
    if not candidate:
        raise BusinessError(400, f"未找到球员档案: {candidate_id}")
    if candidate.group_type != group_type:
        raise BusinessError(400, "球员档案组别与请求组别不一致")
    if str(candidate.event_id) != str(event_id):
        raise BusinessError(400, "球员档案赛事与请求赛事不一致")

    # The archive may accept a roster-item id or a person id; always store the
    # archive's canonical candidate id so duplicates cannot slip through.
    candidate_id = str(candidate.candidate_id)

    if Score.objects.filter(
        event_id=event_id,
        score_round=score_round,
        candidate_id=candidate_id,
    ).exists():
        raise BusinessError(
            409, "该球员本轮评分已存在，请使用修改接口"
        )

    template_items = list(template.items.all())
    values, total = _build_snapshot(
        template_items, _requested_scores(raw_scores)
    )

    try:
        with transaction.atomic():
            score = Score.objects.create(
                event_id=event_id,
                group_type=group_type,
                score_round=score_round,
                template=template,
                candidate_id=candidate_id,
                candidate_name=candidate.name,
                team_name=candidate.team_name or "",
                player_position=candidate.position or "",
                total_score=total,
                version=0,
                created_by=user_id,
                updated_by=user_id,
            )
            ScoreItem.objects.bulk_create(
                [
                    ScoreItem(
                        score_record=score,
                        dimension_code=entry["template_item"].dimension_code,
                        dimension_name=entry["template_item"].dimension_name,
                        weight=entry["template_item"].weight,
                        max_score=entry["template_item"].max_score,
                        score=entry["score"],
                        sort_no=entry["template_item"].sort_no,
                    )
                    for entry in values
                ]
            )
            mark_result_stale(event_id, group_type)
    except IntegrityError:
        # Two concurrent requests passed the pre-check.
        raise BusinessError(
            409, "该球员本轮评分已存在，请使用修改接口"
        )

    return get_score_detail(score.id)


@transaction.atomic
def update_score(score_id, data, user_id):
    score = (
        Score.objects.prefetch_related("items")
        .filter(pk=score_id)
        .first()
    )
    if not score:
        raise BusinessError(404, "评分记录不存在")

    raw_scores = get_field(
        data, "scores", required=True, label="评分明细"
    )
    requested = _requested_scores(raw_scores)
    items = list(score.items.all())
    by_code = {item.dimension_code: item for item in items}

    unknown = set(requested) - set(by_code)
    if unknown:
        raise BusinessError(
            400, f"未知评分维度: {', '.join(sorted(unknown))}"
        )

    for item in items:
        if item.dimension_code not in requested:
            raise BusinessError(
                400,
                f"缺少评分维度: {item.dimension_name}({item.dimension_code})",
            )
        value = requested[item.dimension_code]
        if value < 0 or value > item.max_score:
            raise BusinessError(
                400,
                f"维度 {item.dimension_code} 的得分必须在0到"
                f"{item.max_score}之间，当前为{value}",
            )
        item.score = value

    total = calculate_total(items)
    new_version = score.version + 1

    # Optimistic lock: only update if nobody changed the row after we read it.
    updated = Score.objects.filter(
        pk=score.id, version=score.version
    ).update(
        total_score=total,
        updated_by=user_id,
        updated_at=timezone.now(),
        version=new_version,
    )
    if updated == 0:
        raise BusinessError(409, "评分已被其他人修改，请刷新后重试")

    ScoreItem.objects.bulk_update(items, ["score"])
    mark_result_stale(score.event_id, score.group_type)
    return get_score_detail(score.id)


def mark_result_stale(event_id, group_type):
    """Any score change invalidates the current active result batch."""
    ResultBatch.objects.filter(
        event_id=event_id,
        group_type=group_type,
        active=True,
    ).update(stale=True)
