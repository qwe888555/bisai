"""Example API 路由."""

import math
from typing import Any

from django.http import HttpRequest
from ninja import Router

from apps.example.schemas import TagCreate, TagResponse, TagUpdate
from apps.example.services import create_tag, delete_tag, get_tag, list_tags, update_tag
from common.base.schemas import PaginatedResponse

router = Router(tags=["Example"])


@router.post("/tags", response={201: TagResponse}, summary="创建标签")
def create_tag_endpoint(request: HttpRequest, payload: TagCreate) -> Any:
    """创建新标签（状态码 201）."""
    tag = create_tag(payload)
    return 201, tag


@router.get("/tags", response=PaginatedResponse[TagResponse], summary="标签列表")
def list_tags_endpoint(
    request: HttpRequest,
    page: int = 1,
    per_page: int = 20,
) -> Any:
    """分页查询标签列表."""
    items, total = list_tags(page=page, per_page=per_page)
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": max(1, math.ceil(total / per_page)),
    }


@router.get("/tags/{tag_id}", response=TagResponse, summary="获取标签")
def get_tag_endpoint(request: HttpRequest, tag_id: int) -> Any:
    """获取单个标签."""
    return get_tag(tag_id)


@router.patch("/tags/{tag_id}", response=TagResponse, summary="更新标签")
def update_tag_endpoint(request: HttpRequest, tag_id: int, payload: TagUpdate) -> Any:
    """更新标签信息."""
    return update_tag(tag_id, payload)


@router.delete("/tags/{tag_id}", response={204: None}, summary="删除标签")
def delete_tag_endpoint(request: HttpRequest, tag_id: int) -> Any:
    """删除标签（状态码 204）."""
    delete_tag(tag_id)
    return 204, None
