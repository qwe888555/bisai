"""共享 Pydantic Schema.

通用响应格式和分页包装器，各 App 可复用。
"""

from ninja import Schema


class MessageResponse(Schema):
    """通用消息响应."""

    message: str


class ErrorResponse(Schema):
    """统一错误响应格式."""

    detail: str
    code: str = "error"


class PaginatedResponse[T](Schema):
    """泛型分页响应.

    用法: PaginatedResponse[ItemResponse]
    """

    items: list[T]
    total: int
    page: int
    per_page: int
    total_pages: int
