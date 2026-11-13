"""身份核验业务逻辑。

两种模式：
- MANUAL（默认）：现场拍照 + 人工确认，machine_status=NOT_RUN，不调任何识别服务。
- FACE_COMPARE（需配置 FACE_MODE=FACE_COMPARE 且 face 容器可用）：调独立
  face 服务做 1:1 机器比对，落 machine_status / machine_verified / machine_distance。

人工确认只改变 manualStatus，**不虚构机器识别结果**。
"""

from __future__ import annotations

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


def _verify_roster(enrollment_id: str, athlete_id: str, match_id: str) -> None:
    """球员必须在该报名单内，且报名单与比赛属于同一赛事（契约 4.5）。"""
    from apps.enrollments.models import Enrollment, RosterItem

    enrollment = Enrollment.objects.filter(pk=enrollment_id).first()
    if enrollment is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "报名单不存在",
            [{"field": "enrollmentId", "reason": "不存在"}],
        )
    item = RosterItem.objects.filter(
        enrollment_id=enrollment_id, athlete_id=str(athlete_id)
    ).first()
    if item is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "该球员不在此报名单内",
            [{"field": "athleteId", "reason": "名单不匹配"}],
        )

    from apps.events.models import Match

    match = Match.objects.filter(pk=match_id).first()
    if match is not None and str(match.event_id) != str(enrollment.event_id):
        raise ApiError(
            INVALID_ARGUMENT,
            "报名单与比赛不属于同一赛事",
            [{"field": "enrollmentId", "reason": "赛事不匹配"}],
        )


def create_check(account, data: dict[str, Any]) -> IdentityCheck:
    mode = data.get("mode", "MANUAL")
    if mode not in ("MANUAL", "FACE_COMPARE"):
        raise ApiError(
            INVALID_ARGUMENT, "mode 取值不合法，仅支持 MANUAL / FACE_COMPARE"
        )

    _verify_capture_photo(data["capturedPhotoFileId"], data["matchId"])
    _verify_roster(data["enrollmentId"], data["athleteId"], data["matchId"])

    if mode == "FACE_COMPARE":
        if conf.FACE_MODE != "FACE_COMPARE":
            raise ApiError(
                INVALID_ARGUMENT,
                "平台仅支持现场人工核验，mode 必须为 MANUAL",
            )
        from . import face_client

        reference_photo_file_id = face_client.resolve_reference_photo_file_id(
            data["enrollmentId"], data["athleteId"]
        )
        outcome = face_client.compare(
            data["capturedPhotoFileId"], reference_photo_file_id
        )
        return IdentityCheck.objects.create(
            id=new_identity_id(),
            match_id=data["matchId"],
            enrollment_id=data["enrollmentId"],
            athlete_id=data["athleteId"],
            captured_photo_file_id=data["capturedPhotoFileId"],
            mode="FACE_COMPARE",
            machine_status=outcome["machine_status"],
            machine_verified=outcome["machine_verified"],
            machine_distance=outcome["machine_distance"],
            machine_reason=outcome["machine_reason"],
            manual_status="NOT_REVIEWED",
        )

    return IdentityCheck.objects.create(
        id=new_identity_id(),
        match_id=data["matchId"],
        enrollment_id=data["enrollmentId"],
        athlete_id=data["athleteId"],
        captured_photo_file_id=data["capturedPhotoFileId"],
        mode="MANUAL",
        machine_status="NOT_RUN",
        machine_verified=False,
        machine_distance=None,
        manual_status="NOT_REVIEWED",
    )


def reference_photo(enrollment_id: str, athlete_id: str) -> dict[str, Any]:
    """核验前探测参考照：返回 fileId / source / available。

    记录员角色无人员档案读取权限，前端无法自行判断「名单照片 → 档案头像」两级回落，
    因此由后端按与比对完全一致的优先级给出结论；文件记录已删则按不可用处理。
    """
    from apps.attachments.models import FileRecord

    from . import face_client

    resolved = face_client.resolve_reference_photo(enrollment_id, athlete_id)
    file_id = resolved["file_id"]
    if file_id and not FileRecord.objects.filter(pk=file_id).exists():
        return {"fileId": file_id, "source": resolved["source"], "available": False}
    return {
        "fileId": file_id,
        "source": resolved["source"],
        "available": bool(file_id),
    }


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
    # 注意：绝不修改 machine_status / machine_verified / machine_distance
    bump(check)
    check.save()
    return check
