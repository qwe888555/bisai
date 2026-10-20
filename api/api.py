"""API 路由定义.

展示 django-ninja Router 的 CRUD 端点最佳实践：
- 标准的 HTTP 方法（POST/GET/PATCH/DELETE）
- 明确的状态码（201 Created, 204 No Content）
- Service 层调用（禁止直接操作 ORM）
- 分页查询支持
"""

from typing import Any

from django.http import HttpRequest
from ninja import Router

from api.schemas import (
    ItemCreate,
    ItemResponse,
    ItemUpdate,
    PaginatedResponse,
)
from api.services import create_item, delete_item, get_item, list_items, update_item

router = Router(tags=["Items"])


@router.post("/items", response={201: ItemResponse}, summary="创建商品")
def create_item_endpoint(request: HttpRequest, payload: ItemCreate) -> Any:
    """创建新商品（状态码 201）."""
    item = create_item(payload)
    return 201, item


@router.get("/items", response=PaginatedResponse[ItemResponse], summary="商品列表")
def list_items_endpoint(
    request: HttpRequest,
    status: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> Any:
    """分页查询商品列表，支持按状态筛选."""
    import math

    items, total = list_items(status=status, page=page, per_page=per_page)

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, math.ceil(total / per_page)),
    }


@router.get("/items/{item_id}", response=ItemResponse, summary="获取商品详情")
def get_item_endpoint(request: HttpRequest, item_id: int) -> Any:
    """获取单个商品详情."""
    return get_item(item_id)


@router.patch("/items/{item_id}", response=ItemResponse, summary="更新商品")
def update_item_endpoint(
    request: HttpRequest, item_id: int, payload: ItemUpdate
) -> Any:
    """部分更新商品信息."""
    return update_item(item_id, payload)


@router.delete("/items/{item_id}", response={204: None}, summary="删除商品")
def delete_item_endpoint(request: HttpRequest, item_id: int) -> Any:
    """软删除商品（状态码 204）."""
    delete_item(item_id)
    return 204, None
