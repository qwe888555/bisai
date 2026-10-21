"""幂等：Idempotency-Key 的处理。

契约 2.2：报名提交/审核、比赛事件新增、遴选结果生成、核验创建必须带
Idempotency-Key（8—128 字符），按 账号＋操作＋对象 隔离，保留 24 小时。
相同键相同正文返回第一次结果；同键不同正文 409 IDEMPOTENCY_CONFLICT。
**必须先验证当前访问权限再读取幂等结果，不能借缓存越权** —— 因此本模块
只在视图体内调用，且调用位置必须在 require(...) 之后。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Optional

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from .context import get_current_account, get_request_id
from .crypto import sha256_text
from .errors import (
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_KEY_REQUIRED,
    UNAUTHENTICATED,
    ApiError,
)
from .ids import new_idempotency_id
from .models import IdempotencyRecord

logger = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "Idempotency-Key"
KEY_MIN_LENGTH = 8
KEY_MAX_LENGTH = 128
TTL_HOURS = 24


def _json_safe(value: Any) -> Any:
    """把 datetime / Decimal / UUID 等转成 JSONField 能存的纯结构。"""
    try:
        return json.loads(json.dumps(value, cls=DjangoJSONEncoder))
    except (TypeError, ValueError):
        return str(value)


def read_key(request) -> str:
    raw = (request.headers.get(IDEMPOTENCY_HEADER) or "").strip()
    if not raw:
        raise ApiError(
            IDEMPOTENCY_KEY_REQUIRED, "该操作必须携带 Idempotency-Key 请求头"
        )
    if not (KEY_MIN_LENGTH <= len(raw) <= KEY_MAX_LENGTH):
        raise ApiError(
            IDEMPOTENCY_KEY_REQUIRED,
            f"Idempotency-Key 长度必须在 {KEY_MIN_LENGTH}—{KEY_MAX_LENGTH} 之间",
        )
    return raw


def _payload_fingerprint(request, payload: Any = None) -> str:
    if payload is not None:
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump()
        elif hasattr(payload, "dict") and not isinstance(payload, dict):
            payload = payload.dict()
        try:
            canonical = json.dumps(
                payload, sort_keys=True, ensure_ascii=False, default=str
            )
        except (TypeError, ValueError):
            canonical = str(payload)
        return sha256_text(canonical)

    raw = getattr(request, "body", b"")
    if isinstance(raw, (bytes, bytearray)) and raw:
        return sha256_text(bytes(raw).decode("utf-8", "ignore"))
    return sha256_text("")


class IdempotencyGuard:
    """按操作取一次幂等保护；replay 非空时视图直接返回它。"""

    def __init__(
        self,
        request,
        operation: str,
        object_id: str = "",
        payload: Any = None,
    ) -> None:
        account = get_current_account()
        if account is None:
            raise ApiError(UNAUTHENTICATED, "幂等保护需要在已认证请求内使用")

        self.request = request
        self.operation = operation
        self.object_id = object_id or ""
        self.account_id = account.pk
        self.key_hash = sha256_text(read_key(request))
        self.request_hash = _payload_fingerprint(request, payload)
        self.replay: Optional[tuple[int, dict]] = None

        record = IdempotencyRecord.objects.filter(
            account_id=self.account_id,
            operation=self.operation,
            object_id=self.object_id,
            key_hash=self.key_hash,
        ).first()
        if record is None or record.expired:
            return
        if record.request_hash != self.request_hash:
            raise ApiError(IDEMPOTENCY_CONFLICT)
        body = dict(record.response_body or {})
        body["requestId"] = get_request_id()
        self.replay = (record.status_code, body)

    def commit(self, envelope: Any, status: int = 200) -> None:
        """执行成功后落库；重复提交时由唯一约束兜底。"""
        payload = envelope.to_dict() if hasattr(envelope, "to_dict") else envelope
        try:
            IdempotencyRecord.objects.create(
                id=new_idempotency_id(),
                account_id=self.account_id,
                operation=self.operation,
                object_id=self.object_id,
                key_hash=self.key_hash,
                request_hash=self.request_hash,
                response_body=_json_safe(payload),
                status_code=status,
                expires_at=timezone.now() + timedelta(hours=TTL_HOURS),
            )
        except Exception:  # noqa: BLE001 - 业务已成功，幂等落库失败不能影响返回
            logger.warning("幂等记录写入失败，后续重复提交将无法回放", exc_info=True)


def guard(
    request, operation: str, object_id: str = "", payload: Any = None
) -> IdempotencyGuard:
    return IdempotencyGuard(request, operation, object_id, payload)
