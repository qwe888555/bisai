"""乐观锁：契约 2.2 —— PUT/DELETE 带 version，不匹配即 409 不静默覆盖。"""

from __future__ import annotations

from typing import Optional

from .errors import INVALID_ARGUMENT, VERSION_CONFLICT, ApiError


def check_version(instance, given: Optional[int]) -> None:
    if given is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "缺少 version",
            [{"field": "version", "reason": "该操作必须提交乐观锁版本"}],
        )
    try:
        current = int(instance.version)
    except (TypeError, ValueError):
        current = 0
    if int(given) != current:
        raise ApiError(
            VERSION_CONFLICT,
            data=[{"field": "version", "reason": "版本不匹配"}],
        )


def bump(instance) -> None:
    instance.version = int(instance.version or 0) + 1
