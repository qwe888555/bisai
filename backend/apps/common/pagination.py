"""分页：page 从 1 开始、pageSize 默认 20 上限 100，data 固定四字段。

用法（视图里）：
    return paginate(request, queryset)        # 或
    return paginate_list(request, [dict, ...])
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from .conf import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from .envelope import page


def read_page_params(request) -> tuple[int, int]:
    """读取并纠正 page / pageSize；非法值退回默认，不报错。"""
    page_number = _to_int(request.GET.get("page"), 1)
    page_size = _to_int(request.GET.get("pageSize"), DEFAULT_PAGE_SIZE)
    return max(page_number, 1), min(max(page_size, 1), MAX_PAGE_SIZE)


def _to_int(raw: Optional[str], default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def paginate(request, queryset: Any, mapper=None) -> dict:
    """对 QuerySet / 列表分页并返回契约信封。

    mapper：把每条记录转成可序列化的 dict（如 Serializer 风格的 to_out），
    不传则要求元素本身已可序列化。
    """
    page_number, page_size = read_page_params(request)
    total = queryset.count() if hasattr(queryset, "count") else len(list(queryset))
    start = (page_number - 1) * page_size
    items = list(queryset[start : start + page_size])
    if mapper is not None:
        items = [mapper(item) for item in items]
    return page(items, page_number, page_size, total)


def paginate_list(request, items: Sequence[Any]) -> dict:
    page_number, page_size = read_page_params(request)
    start = (page_number - 1) * page_size
    return page(
        list(items[start : start + page_size]),
        page_number,
        page_size,
        len(items),
    )
