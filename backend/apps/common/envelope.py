"""统一响应信封：{code, message, data, requestId}。

契约 2.1：成功一律 HTTP 200 + code=OK；分页 data 固定
{items, page, pageSize, total}；动作类接口返回 ActionResult。
业务代码一律用 ok()/page()/action_result() 构造返回值，不要自己拼 dict。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .context import get_request_id

CODE_OK = "OK"
MESSAGE_OK = "成功"


class Envelope:
    """统一信封对象；requestId 延迟到渲染时从上下文取。"""

    __slots__ = ("code", "message", "data", "request_id")

    def __init__(
        self,
        code: str = CODE_OK,
        message: str = MESSAGE_OK,
        data: Any = None,
        request_id: Optional[str] = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "data": self.data,
            "requestId": self.request_id or get_request_id(),
        }


def ok(data: Any = None, message: str = MESSAGE_OK) -> dict:
    return Envelope(CODE_OK, message, data).to_dict()


def page(
    items: Iterable[Any], current_page: int, page_size: int, total: int
) -> dict:
    return ok(
        {
            "items": list(items),
            "page": current_page,
            "pageSize": page_size,
            "total": total,
        }
    )


def action_result(
    success: bool = True,
    object_id: Optional[str] = None,
    version: Optional[int] = None,
) -> dict:
    data: dict = {"success": success}
    if object_id is not None:
        data["id"] = object_id
    if version is not None:
        data["version"] = version
    return ok(data)
