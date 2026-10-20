"""Example 业务逻辑层."""

import math

from django.db import transaction

from apps.example.models import ExampleTag
from apps.example.schemas import TagCreate, TagUpdate
from common.base.exceptions import BusinessError


class TagNotFoundError(BusinessError):
    """标签不存在异常."""

    def __init__(self, tag_id: int) -> None:
        super().__init__(message=f"标签不存在: {tag_id}", code="tag_not_found")


@transaction.atomic
def create_tag(payload: TagCreate) -> ExampleTag:
    """创建标签."""
    return ExampleTag.objects.create(**payload.model_dump())


def get_tag(tag_id: int) -> ExampleTag:
    """获取标签."""
    try:
        return ExampleTag.objects.get(id=tag_id)
    except ExampleTag.DoesNotExist as err:
        raise TagNotFoundError(tag_id) from err


def list_tags(page: int = 1, per_page: int = 20) -> tuple[list[ExampleTag], int]:
    """标签列表（分页）."""
    qs = ExampleTag.objects.filter(is_active=True).order_by("name")
    total = qs.count()
    total_pages = max(1, math.ceil(total / per_page))
    safe_page = max(1, min(page, total_pages)) if total > 0 else 1
    offset = (safe_page - 1) * per_page
    return list(qs[offset : offset + per_page]), total


@transaction.atomic
def update_tag(tag_id: int, payload: TagUpdate) -> ExampleTag:
    """更新标签."""
    tag = get_tag(tag_id)
    update_data = payload.model_dump(exclude_unset=True)
    if update_data:
        for field, value in update_data.items():
            setattr(tag, field, value)
        tag.save()
    return tag


@transaction.atomic
def delete_tag(tag_id: int) -> None:
    """删除标签."""
    tag = get_tag(tag_id)
    tag.delete()
