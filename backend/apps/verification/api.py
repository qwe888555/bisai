"""verification 接口：现场核验创建（幂等）、列表、详情、人工复核。

路径与响应字段逐字沿用原 urls.py / serializers.py 契约。
"""

from __future__ import annotations

from typing import Literal, Optional

from ninja import Field, Router

from apps.common import idempotency
from apps.common.authentication import JWTAuth
from apps.common.envelope import ok
from apps.common.middleware import audit_state
from apps.common.pagination import paginate
from apps.common.permissions import (
    ADMIN,
    MATCH_RECORDER,
    PROVINCE_REVIEWER,
    register_operation,
    require,
)
from apps.common.schemas import EnvelopeOut, StrictSchema

from . import services
from .models import IdentityCheck

router = Router(tags=["identity"])
_auth = JWTAuth()

_REVIEW_ROLES = (MATCH_RECORDER, ADMIN, PROVINCE_REVIEWER)

register_operation("identityCreate", _REVIEW_ROLES)
register_operation("identityList", _REVIEW_ROLES)
register_operation("identityGet", _REVIEW_ROLES)
register_operation("identityReview", _REVIEW_ROLES)


class IdentityCheckCreate(StrictSchema):
    """与 IdentityCheckCreate 序列化器字段一致。"""

    matchId: str = Field(..., min_length=1, max_length=64)
    enrollmentId: str = Field(..., min_length=1, max_length=64)
    athleteId: str = Field(..., min_length=1, max_length=64)
    capturedPhotoFileId: str = Field(..., min_length=1, max_length=64)
    mode: Literal["FACE_COMPARE", "MANUAL"]


class IdentityManualReview(StrictSchema):
    """与 IdentityManualReview 序列化器字段一致。"""

    version: int = Field(..., ge=0)
    decision: Literal["CONFIRM_IDENTITY", "REJECT_IDENTITY"]
    reason: str = Field(..., min_length=1, max_length=1000)


def _identity_out(check: IdentityCheck) -> dict:
    """等价于 IdentityCheckSerializer.to_representation，字段锁定 camelCase。"""
    data = {
        "id": check.pk,
        "matchId": check.match_id,
        "enrollmentId": check.enrollment_id,
        "athleteId": check.athlete_id,
        "capturedPhotoFileId": check.captured_photo_file_id,
        "mode": check.mode,
        "machineStatus": check.machine_status,
        "machineVerified": bool(check.machine_verified),
        "manualStatus": check.manual_status,
        "version": int(check.version or 0),
        "createdAt": check.created_at,
        "updatedAt": check.updated_at,
    }
    optional = {
        "manualReason": check.manual_reason,
        "reviewerAccountId": check.reviewer_account_id,
        "reviewedAt": check.reviewed_at,
    }
    for key, value in optional.items():
        if value not in (None, ""):
            data[key] = value
    if check.reviewer_account_id:
        from apps.accounts.models import Account

        reviewer = (
            Account.objects.filter(pk=check.reviewer_account_id)
            .values_list("display_name", "username")
            .first()
        )
        if reviewer:
            data["reviewerName"] = reviewer[0] or reviewer[1]
    return data


@router.post(
    "/identity-checks",
    operation_id="identityCreate",
    auth=_auth,
    response=EnvelopeOut,
    summary="球员现场身份核验",
)
def identity_create(request, payload: IdentityCheckCreate):
    actor = require("identityCreate")

    # 幂等：先过权限（上方 require），再读幂等结果（契约 2.2）
    g = idempotency.guard(
        request, "identityCreate", object_id=payload.matchId, payload=payload
    )
    if g.replay is not None:
        return g.replay

    check = services.create_check(actor, payload.model_dump())
    envelope = ok(_identity_out(check))
    g.commit(envelope)
    audit_state(
        request,
        "identity.create",
        object_type="IdentityCheck",
        object_id=check.pk,
        summary=f"现场核验 {check.mode}",
    )
    return envelope


@router.get(
    "/identity-checks/list",
    operation_id="identityList",
    auth=_auth,
    response=EnvelopeOut,
    summary="身份核验记录列表",
)
def identity_list(
    request,
    matchId: Optional[str] = None,
    enrollmentId: Optional[str] = None,
    athleteId: Optional[str] = None,
    manualStatus: Optional[str] = None,
    machineStatus: Optional[str] = None,
):
    actor = require("identityList")
    queryset = services.list_checks(
        {
            "matchId": matchId,
            "enrollmentId": enrollmentId,
            "athleteId": athleteId,
            "manualStatus": manualStatus,
            "machineStatus": machineStatus,
        }
    )
    return paginate(request, queryset, mapper=_identity_out)


@router.get(
    "/identity-checks/{identity_check_id}",
    operation_id="identityGet",
    auth=_auth,
    response=EnvelopeOut,
    summary="身份核验结果",
)
def identity_get(request, identity_check_id: str):
    actor = require("identityGet")
    check = services.get_check(identity_check_id)
    return ok(_identity_out(check))


@router.post(
    "/identity-checks/{identity_check_id}/manual-review",
    operation_id="identityReview",
    auth=_auth,
    response=EnvelopeOut,
    summary="身份核验人工复核",
)
def identity_review(request, identity_check_id: str, payload: IdentityManualReview):
    actor = require("identityReview")
    check = services.manual_review(actor, identity_check_id, payload.model_dump())
    audit_state(
        request,
        "identity.manualReview",
        object_type="IdentityCheck",
        object_id=check.pk,
        summary=f"人工复核 {check.manual_status}",
    )
    return ok(_identity_out(check))
