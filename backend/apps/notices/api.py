"""notices 接口：本人系统消息，路径固定 /me/messages。

路径与响应字段逐字沿用原 urls.py / serializers.py 契约，响应统一走信封。
"""

from __future__ import annotations

from django.utils import timezone
from ninja import Router

from apps.common.authentication import JWTAuth
from apps.common.envelope import action_result, ok
from apps.common.errors import NOT_FOUND, ApiError
from apps.common.pagination import paginate_list
from apps.common.permissions import ALL_ROLES, register_operation, require
from apps.common.schemas import EnvelopeOut
from apps.common.versioning import bump

from .models import Message

router = Router(tags=["notices"])
_auth = JWTAuth()

register_operation("messageList", ALL_ROLES)
register_operation("messageGet", ALL_ROLES)
register_operation("messageRead", ALL_ROLES)


def _message_out(message: Message) -> dict:
    """等价于 MessageSerializer.to_representation，字段锁定 camelCase。"""
    data = {
        "id": message.pk,
        "title": message.title,
        "read": bool(message.read),
        "version": int(message.version or 0),
        "createdAt": message.created_at,
        "updatedAt": message.updated_at,
    }
    optional = {
        "body": message.body,
        "businessType": message.business_type,
        "businessId": message.business_id,
        "readAt": message.read_at,
    }
    for key, value in optional.items():
        if value not in (None, ""):
            data[key] = value
    return data


def _own_messages(account):
    return Message.objects.filter(recipient_account_id=account.pk)


def _get_own_message(account, message_id: str) -> Message:
    message = _own_messages(account).filter(pk=message_id).first()
    if message is None:
        raise ApiError(NOT_FOUND)
    return message


@router.get("/me/messages", auth=_auth, response=EnvelopeOut, summary="本人系统消息列表")
def message_list(request):
    actor = require("messageList")
    queryset = _own_messages(actor)
    read = request.GET.get("read")
    if read not in (None, ""):
        queryset = queryset.filter(read=str(read).lower() in {"1", "true", "yes"})
    queryset = queryset.order_by("-created_at", "id")
    return paginate_list(request, [_message_out(item) for item in queryset])


@router.get(
    "/me/messages/{message_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="本人消息详情",
)
def message_get(request, message_id: str):
    actor = require("messageGet")
    message = _get_own_message(actor, message_id)
    return ok(_message_out(message))


@router.post(
    "/me/messages/{message_id}/read",
    auth=_auth,
    response=EnvelopeOut,
    summary="标记本人消息已读",
)
def message_read(request, message_id: str):
    actor = require("messageRead")
    message = _get_own_message(actor, message_id)
    if not message.read:
        message.read = True
        message.read_at = timezone.now()
        bump(message)
        message.save()
    return action_result(True, message.pk, message.version)
