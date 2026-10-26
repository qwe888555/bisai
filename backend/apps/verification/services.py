"""身份核验业务逻辑。

机器比对：配置了 FACE_API_BASE 且模式为 FACE_COMPARE 时调用第三方接口；
未配置或调用失败一律 machineStatus=UNAVAILABLE，**不虚构识别结果**。
"""

from __future__ import annotations

import base64
from typing import Any

from django.utils import timezone

from apps.common import conf
from apps.common.errors import (
    INVALID_ARGUMENT,
    NOT_FOUND,
    STATE_CONFLICT,
    ApiError,
)
from apps.common.ids import new_identity_id
from apps.common.versioning import bump, check_version

from .models import IdentityCheck


def _run_face_compare(captured_photo_file_id: str, enrollment_id: str, athlete_id: str) -> str:
    """返回 machineStatus：MATCH / MISMATCH / UNAVAILABLE。"""
    if not conf.FACE_API_BASE or not conf.FACE_API_KEY:
        return "UNAVAILABLE"
    try:
        import httpx
    except ImportError:  # pragma: no cover
        return "UNAVAILABLE"
    try:
        # 参考照片由成员3/4 的报名名单快照提供，这里只传 ID 由对方服务解析
        payload = {
            "capturedPhotoFileId": captured_photo_file_id,
            "enrollmentId": enrollment_id,
            "athleteId": athlete_id,
            "threshold": conf.FACE_THRESHOLD,
        }
        with httpx.Client(timeout=conf.AI_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{conf.FACE_API_BASE.rstrip('/')}/compare",
                json=payload,
                headers={"Authorization": f"Bearer {conf.FACE_API_KEY}"},
            )
        if response.status_code >= 400:
            return "UNAVAILABLE"
        body = response.json()
        score = float(body.get("score", 0))
        return "MATCH" if score >= conf.FACE_THRESHOLD else "MISMATCH"
    except Exception:  # noqa: BLE001 - 依赖不可用时降级，不抛厂商原始错误
        return "UNAVAILABLE"


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
    mode = data["mode"]
    if mode not in {"FACE_COMPARE", "MANUAL"}:
        raise ApiError(INVALID_ARGUMENT, "mode 取值不合法")
    if mode == "MANUAL" and conf.FACE_MODE != "MANUAL":
        # 业务方未确认人工降级模式时，不允许走 MANUAL
        raise ApiError(STATE_CONFLICT, "当前配置不允许使用人工降级模式")

    _verify_capture_photo(data["capturedPhotoFileId"], data["matchId"])

    machine_status = "NOT_RUN"
    if mode == "FACE_COMPARE":
        machine_status = _run_face_compare(
            data["capturedPhotoFileId"], data["enrollmentId"], data["athleteId"]
        )

    return IdentityCheck.objects.create(
        id=new_identity_id(),
        match_id=data["matchId"],
        enrollment_id=data["enrollmentId"],
        athlete_id=data["athleteId"],
        captured_photo_file_id=data["capturedPhotoFileId"],
        mode=mode,
        machine_status=machine_status,
        # 只有真实机器比对且命中时才为 true
        machine_verified=(machine_status == "MATCH"),
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
    if filters.get("machineStatus"):
        queryset = queryset.filter(machine_status=filters["machineStatus"])
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
