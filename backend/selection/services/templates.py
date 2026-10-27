"""Scoring-template validation and CRUD."""

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..common import (
    BusinessError,
    get_field,
    parse_decimal,
    parse_int,
    to_datetime_str,
    to_float,
)
from ..enums import GroupType, RoundType
from ..models import ScoreTemplate, TemplateItem

MAX_TEMPLATE_ITEMS = 20
WEIGHT_TOTAL = Decimal("100")


def normalize_dimension_code(value):
    if value is None:
        raise BusinessError(400, "维度编码不能为空")
    code = str(value).strip().upper()
    if not code:
        raise BusinessError(400, "维度编码不能为空")
    if len(code) > 32:
        raise BusinessError(400, "维度编码长度不能超过32个字符")
    return code


def validate_items(raw_items):
    """Validate and normalize a template's dimension list."""
    if not isinstance(raw_items, list) or not raw_items:
        raise BusinessError(400, "评分维度不能为空")
    if len(raw_items) > MAX_TEMPLATE_ITEMS:
        raise BusinessError(
            400, f"评分维度不能超过{MAX_TEMPLATE_ITEMS}个"
        )

    result = []
    seen_codes = set()
    weight_sum = Decimal("0")

    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise BusinessError(400, f"第{index + 1}个评分维度格式错误")

        code = normalize_dimension_code(
            get_field(raw, "dimension_code", "dimensionCode")
        )
        if code in seen_codes:
            raise BusinessError(400, f"维度编码重复: {code}")
        seen_codes.add(code)

        name = str(
            get_field(
                raw,
                "dimension_name",
                "dimensionName",
                required=True,
                label="维度名称",
            )
        ).strip()
        if len(name) > 64:
            raise BusinessError(400, "维度名称长度不能超过64个字符")

        weight = parse_decimal(
            get_field(raw, "weight"),
            f"维度 {code} 的权重",
            min_value=Decimal("0.01"),
            max_value=Decimal("100"),
        )
        max_score = parse_decimal(
            get_field(raw, "max_score", "maxScore", default=100),
            f"维度 {code} 的评分上限",
            default=Decimal("100"),
            min_value=Decimal("0.01"),
        )
        sort_no = parse_int(
            get_field(raw, "sort_no", "sortNo", default=index),
            f"维度 {code} 的排序号",
            required=False,
            default=index,
            min_value=0,
        )

        weight_sum += weight
        result.append(
            {
                "dimension_code": code,
                "dimension_name": name,
                "weight": weight,
                "max_score": max_score,
                "sort_no": sort_no,
            }
        )

    if weight_sum != WEIGHT_TOTAL:
        raise BusinessError(
            400,
            f"各维度权重之和必须为100，当前为{weight_sum:f}",
        )

    return result


def template_detail(template):
    return {
        "id": template.id,
        "name": template.name,
        "group_type": template.group_type,
        "group_type_label": GroupType.label(template.group_type),
        "round": template.score_round,
        "round_label": RoundType.label(template.score_round),
        "status": template.status,
        "created_by": template.created_by,
        "created_at": to_datetime_str(template.created_at),
        "updated_at": to_datetime_str(template.updated_at),
        "items": [
            {
                "id": item.id,
                "dimension_code": item.dimension_code,
                "dimension_name": item.dimension_name,
                "weight": to_float(item.weight),
                "max_score": to_float(item.max_score),
                "sort_no": item.sort_no,
            }
            for item in template.items.all()
        ],
    }


def get_template_or_404(template_id):
    template = (
        ScoreTemplate.objects.prefetch_related("items")
        .filter(pk=template_id)
        .first()
    )
    if not template:
        raise BusinessError(404, "评分模板不存在")
    return template


def get_template_detail(template_id):
    return template_detail(get_template_or_404(template_id))


def list_templates(group_type=None, score_round=None, status=None):
    queryset = ScoreTemplate.objects.prefetch_related("items").all()

    if group_type:
        queryset = queryset.filter(group_type=GroupType.parse(group_type))
    if score_round:
        queryset = queryset.filter(score_round=RoundType.parse(score_round))
    if status is not None:
        queryset = queryset.filter(status=status)

    return [
        template_detail(template)
        for template in queryset.order_by("-created_at", "-id")
    ]


@transaction.atomic
def create_template(data, user_id):
    name = str(
        get_field(data, "name", required=True, label="模板名称")
    ).strip()
    if not name:
        raise BusinessError(400, "模板名称不能为空")
    if len(name) > 100:
        raise BusinessError(400, "模板名称长度不能超过100个字符")

    group_type = GroupType.parse(get_field(data, "group_type", "groupType"))
    score_round = RoundType.parse(
        get_field(data, "round", "score_round", "scoreRound")
    )
    items = validate_items(
        get_field(data, "items", required=True, label="评分维度")
    )

    # Only one active template per group + round.  Historical scores keep
    # their own item snapshots, so disabling an old template is safe.
    ScoreTemplate.objects.filter(
        group_type=group_type,
        score_round=score_round,
        status=1,
    ).update(status=0, updated_at=timezone.now())

    template = ScoreTemplate.objects.create(
        name=name,
        group_type=group_type,
        score_round=score_round,
        status=1,
        created_by=user_id,
    )
    TemplateItem.objects.bulk_create(
        [TemplateItem(template=template, **item) for item in items]
    )
    return template_detail(template)


@transaction.atomic
def update_template(template_id, data, user_id):
    template = get_template_or_404(template_id)

    if any(key in data for key in ("group_type", "groupType")):
        group_type = GroupType.parse(
            get_field(data, "group_type", "groupType")
        )
        if group_type != template.group_type:
            raise BusinessError(400, "模板创建后不能修改组别")

    if any(key in data for key in ("round", "score_round", "scoreRound")):
        score_round = RoundType.parse(
            get_field(data, "round", "score_round", "scoreRound")
        )
        if score_round != template.score_round:
            raise BusinessError(400, "模板创建后不能修改轮次")

    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise BusinessError(400, "模板名称不能为空")
        if len(name) > 100:
            raise BusinessError(400, "模板名称长度不能超过100个字符")
        template.name = name

    if "items" in data:
        items = validate_items(data.get("items"))
        TemplateItem.objects.filter(template=template).delete()
        TemplateItem.objects.bulk_create(
            [TemplateItem(template=template, **item) for item in items]
        )

    template.save()
    # Re-query so the prefetched item cache does not show deleted dimensions.
    return template_detail(get_template_or_404(template.id))
