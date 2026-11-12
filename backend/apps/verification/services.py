"""身份核验业务逻辑。

支持两种核验模式，结论并存、可追溯：

- ``FACE_COMPARE``：现场照片与运动员报名照片做 1:1 比对，结果写入
  ``machine_status``（MATCH / MISMATCH / UNAVAILABLE）。比对服务未配置、
  图片缺失、调用失败时一律降级为 UNAVAILABLE，**绝不虚构识别结果**。
- ``MANUAL``：只做现场人工核验，``machine_status`` 固定 NOT_RUN。

人工复核只改变 ``manual_status``，不修改机器结果。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from django.utils import timezone

from apps.common import conf
from apps.common.errors import (
    INVALID_ARGUMENT,
    NOT_FOUND,
    ApiError,
)
from apps.common.ids import new_identity_id
from apps.common.versioning import bump, check_version

from .models import IdentityCheck

logger = logging.getLogger(__name__)

MODES = ("FACE_COMPARE", "MANUAL")
MACHINE_STATUSES = ("NOT_RUN", "MATCH", "MISMATCH", "UNAVAILABLE")


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


def _load_image_b64(file_id: str) -> str | None:
    """读取文件字节并转 base64；记录或物理文件缺失时返回 None。"""
    from apps.attachments.models import FileRecord
    from apps.attachments.storage import StorageNotFoundError, get_storage

    record = FileRecord.objects.filter(pk=file_id).first()
    if record is None or not record.storage_key:
        return None

    stream = None
    try:
        stream = get_storage().open(record.storage_key)
        data = stream.read()
    except StorageNotFoundError:
        logger.warning(
            "人脸比对取图失败：文件 %s 在存储中缺失（key=%s）", file_id, record.storage_key
        )
        return None
    finally:
        if stream is not None and hasattr(stream, "close"):
            stream.close()

    if not data:
        return None
    # DeepFace 只把带 data:image/ 前缀的字符串当 base64 解析
    mime = record.content_type or "image/jpeg"
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def _resolve_reference_photo(enrollment_id: str, athlete_id: str) -> str | None:
    """报名名单照片优先，其次人员主档照片；都为空返回 None。"""
    from apps.enrollments.models import RosterItem
    from apps.persons.models import Person

    roster_photo = (
        RosterItem.objects.filter(enrollment_id=enrollment_id, athlete_id=athlete_id)
        .values_list("photo_file_id", flat=True)
        .first()
    )
    if roster_photo:
        return roster_photo

    try:
        person_photo = (
            Person.objects.filter(pk=athlete_id)
            .values_list("photo_file_id", flat=True)
            .first()
        )
    except (TypeError, ValueError):
        # athlete_id 不是 Person 的合法主键（历史脏数据）时视为没有参考照片
        person_photo = None
    return person_photo or None


def _run_face_compare(
    captured_photo_file_id: str, enrollment_id: str, athlete_id: str
) -> str:
    """调用比对服务，返回 MATCH / MISMATCH / UNAVAILABLE。

    任何前置条件不满足都返回 UNAVAILABLE：不阻断现场录入，也不猜结果。
    """
    base = (conf.FACE_API_BASE or "").strip()
    if not base:
        logger.info("未配置 FACE_API_BASE，机器比对结果记为 UNAVAILABLE")
        return "UNAVAILABLE"

    reference_id = _resolve_reference_photo(enrollment_id, athlete_id)
    if not reference_id:
        logger.info("运动员 %s 没有报名照片，机器比对结果记为 UNAVAILABLE", athlete_id)
        return "UNAVAILABLE"

    img1 = _load_image_b64(reference_id)
    img2 = _load_image_b64(captured_photo_file_id)
    if not img1 or not img2:
        return "UNAVAILABLE"

    try:
        import httpx
    except ImportError:  # pragma: no cover - 依赖缺失时降级
        logger.warning("未安装 httpx，机器比对降级为 UNAVAILABLE")
        return "UNAVAILABLE"

    headers = {"Content-Type": "application/json"}
    if conf.FACE_API_KEY:
        headers["Authorization"] = f"Bearer {conf.FACE_API_KEY}"
    payload = {
        "img1": img1,
        "img2": img2,
        "threshold": conf.FACE_THRESHOLD,
        "model": conf.FACE_MODEL,
        "detector": conf.FACE_DETECTOR,
        "antiSpoofing": conf.FACE_ANTI_SPOOFING,
    }

    try:
        with httpx.Client(timeout=conf.FACE_TIMEOUT_SECONDS) as client:
            response = client.post(f"{base.rstrip('/')}/compare", json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001 - 网络/超时统一降级
        logger.warning("人脸比对服务调用失败：%s", exc)
        return "UNAVAILABLE"

    if response.status_code >= 400:
        logger.warning(
            "人脸比对服务返回 %s：%s", response.status_code, response.text[:200]
        )
        return "UNAVAILABLE"

    try:
        result = response.json()
    except ValueError:
        logger.warning("人脸比对服务返回非 JSON 内容")
        return "UNAVAILABLE"

    verified = result.get("verified")
    if not isinstance(verified, bool):
        logger.warning("人脸比对服务缺少 verified 字段")
        return "UNAVAILABLE"
    # 开启防伪时，翻拍/屏幕照直接判不一致
    if result.get("isReal") is False:
        return "MISMATCH"
    return "MATCH" if verified else "MISMATCH"


def create_check(account, data: dict[str, Any]) -> IdentityCheck:
    mode = data.get("mode") or "MANUAL"
    if mode not in MODES:
        raise ApiError(INVALID_ARGUMENT, "mode 只能为 FACE_COMPARE 或 MANUAL")

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
