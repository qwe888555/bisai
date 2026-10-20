"""API 业务逻辑层.

展示 Service 层模式：
- 业务逻辑与 HTTP 层解耦
- 事务管理（@transaction.atomic）
- 自定义异常处理
- 分页查询封装
"""

import math

from django.db import transaction
from django.db.models import QuerySet

from api.exceptions import ItemNotFoundError
from api.models import Item
from api.schemas import ItemCreate, ItemUpdate


def _base_queryset() -> QuerySet[Item]:
    """获取基础 QuerySet（排除软删除记录）."""
    return Item.objects.filter(is_deleted=False)


@transaction.atomic
def create_item(payload: ItemCreate) -> Item:
    """创建商品（事务保护）."""
    return Item.objects.create(**payload.model_dump())


def get_item(item_id: int) -> Item:
    """获取单个商品，不存在则抛出 ItemNotFoundError."""
    try:
        return _base_queryset().get(id=item_id)
    except Item.DoesNotExist as err:
        raise ItemNotFoundError(item_id) from err


def list_items(
    *,
    status: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[Item], int]:
    """获取商品列表（分页 + 状态筛选）.

    Args:
        status: 按状态筛选（None 表示全部）
        page: 页码，从 1 开始
        per_page: 每页数量

    Returns:
        (当前页数据, 总记录数)
    """
    qs = _base_queryset()
    if status:
        qs = qs.filter(status=status)
    qs = qs.order_by("-created_at")

    total = qs.count()
    total_pages = max(1, math.ceil(total / per_page))
    # 确保 page 在有效范围内
    safe_page = max(1, min(page, total_pages)) if total > 0 else 1
    offset = (safe_page - 1) * per_page
    items = list(qs[offset : offset + per_page])
    return items, total


@transaction.atomic
def update_item(item_id: int, payload: ItemUpdate) -> Item:
    """更新商品（部分更新，仅更新提供字段）."""
    item = get_item(item_id)
    update_data = payload.model_dump(exclude_unset=True)
    if update_data:
        for field, value in update_data.items():
            setattr(item, field, value)
        item.save()
    return item


@transaction.atomic
def delete_item(item_id: int) -> None:
    """软删除商品."""
    item = get_item(item_id)
    item.soft_delete()
