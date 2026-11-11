"""身份核验业务逻辑。

平台只提供现场人工核验：创建记录时 mode 固定为 MANUAL、
machineStatus 固定为 NOT_RUN，不再调用任何第三方人脸比对服务。
人工确认只改变 manualStatus，**不虚构机器识别结果**。
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from apps.common.errors import (
    INVALID_ARGUMENT,
    NOT_FOUND,
    ApiError,
)
from apps.common.ids import new_identity_id
from apps.common.versioning import bump, check_version

from .models import IdentityCheck


def _verify_capture_photo(photo_file_id: str, match_id: str) -> None:
    from apps.attachments.models import FileRecord

    record = FileRecord.objects.filter(pk=photo_file_id).first()
    if record is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "现场照片不存在",
            [{"field": "capturedPhotoFileId", "reason": "不存在"}],
        )
    if record.owner_type != "MATCH" or record.owner_id != match_id:
        raise ApiError(
            INVALID_ARGUMENT,
            "现场照片必须先上传到本场比赛的 MATCH 归属下",
            [{"field": "capturedPhotoFileId", "reason": "归属不匹配"}],
        )
    if record.category != "CAPTURE_PHOTO":
        raise ApiError(
            INVALID_ARGUMENT,
            "现场照片类别必须是 CAPTURE_PHOTO",
            [{"field": "capturedPhotoFileId", "reason": "类别不匹配"}],
        )


def create_check(account, data: dict[str, Any]) -> IdentityCheck:
    # 自动人脸比对已下线，只接受现场人工核验。
    if data.get("mode", "MANUAL") != "MANUAL":
        raise ApiError(INVALID_ARGUMENT, "平台仅支持现场人工核验，mode 必须为 MANUAL")

    _verify_capture_photo(data["capturedPhotoFileId"], data["matchId"])

    return IdentityCheck.objects.create(
        id=new_identity_id(),
        match_id=data["matchId"],
        enrollment_id=data["enrollmentId"],
        athlete_id=data["athleteId"],
        captured_photo_file_id=data["capturedPhotoFileId"],
        mode="MANUAL",
        machine_status="NOT_RUN",
        machine_verified=False,
        manual_status="NOT_REVIEWED",
    )


def list_checks(filters: dict):
    queryset = IdentityCheck.objects.all()
    if filters.get("matchId"):
        queryset = queryset.filter(match_id=filters["matchId"])
    if filters.get("enrollmentId"):
        queryset = queryset.filter(enrollment_id=filters["enrollmentId"])
    if filters.get("athleteId"):
        queryset = queryset.filter(athlete_id=filters["athleteId"])
    if filters.get("manualStatus"):
        queryset = queryset.filter(manual_status=filters["manualStatus"])
    return queryset.order_by("-created_at", "id")


def get_check(check_id: str) -> IdentityCheck:
    check = IdentityCheck.objects.filter(pk=check_id).first()
    if check is None:
        raise ApiError(NOT_FOUND)
    return check


def manual_review(account, check_id: str, data: dict[str, Any]) -> IdentityCheck:
    check = get_check(check_id)
    check_version(check, data.get("version"))
    decision = data["decision"]
    if decision == "CONFIRM_IDENTITY":
        check.manual_status = "CONFIRMED"
    elif decision == "REJECT_IDENTITY":
        check.manual_status = "REJECTED"
    else:
        raise ApiError(INVALID_ARGUMENT, "decision 取值不合法")

    check.manual_reason = data["reason"][:1000]
    check.reviewer_account_id = account.pk
    check.reviewed_at = timezone.now()
    # 注意：绝不修改 machine_status / machine_verified
    bump(check)
    check.save()
    return check
