"""API 自定义异常.

共享业务异常基类见 common.base.exceptions。
"""

from common.base.exceptions import BusinessError  # noqa: F401


class ItemNotFoundError(BusinessError):
    """商品不存在异常."""

    def __init__(self, item_id: int) -> None:
        super().__init__(
            message=f"商品不存在: {item_id}",
            code="item_not_found",
        )


class ItemStatusTransitionError(BusinessError):
    """商品状态转换非法异常."""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(
            message=f"不允许从 '{current}' 转换到 '{target}'",
            code="invalid_status_transition",
        )
