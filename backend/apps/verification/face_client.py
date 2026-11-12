"""face 服务客户端：把报名参考照与现场采集照发给独立 face 容器做 1:1 比对。

失败安全：任何读取/网络/服务异常都收敛为 UNAVAILABLE，不向上抛，
上层据此落库；绝不允许把异常伪装成 MATCH。
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from apps.common import conf


def _data_uri(file_record) -> str:
    from apps.attachments.storage import get_storage

    storage = get_storage()
    with storage.open(file_record.storage_key) as stream:
        raw = stream.read()
    mime = file_record.content_type or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "machine_status": "UNAVAILABLE",
        "machine_verified": False,
        "machine_distance": None,
        "machine_reason": reason[:255],
    }


def resolve_reference_photo_file_id(enrollment_id: str, athlete_id: str) -> str:
    """参考照优先级：名单条目 RosterItem.photo_file_id → 人员主档 Person.photo_file_id。"""
    from apps.enrollments.models import RosterItem
    from apps.persons.models import Person

    roster_photo = (
        RosterItem.objects.filter(enrollment_id=enrollment_id, athlete_id=str(athlete_id))
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
    except (ValueError, TypeError):
        # 非数字 id 无法匹配 BigAutoField，按无参考照处理，不抛 500
        person_photo = None
    return person_photo or ""


def _build_client() -> httpx.Client:
    """构造 face 服务 HTTP 客户端；测试可 monkeypatch 本函数注入 MockTransport。"""
    return httpx.Client(timeout=conf.FACE_TIMEOUT_SECONDS)


def compare(capture_file_id: str, reference_file_id: str) -> dict[str, Any]:
    from apps.attachments.models import FileRecord

    capture = FileRecord.objects.filter(pk=capture_file_id).first()
    reference = FileRecord.objects.filter(pk=reference_file_id).first()
    if capture is None:
        return _unavailable("现场照片缺失")
    if not reference_file_id or reference is None:
        return _unavailable("报名参考照片缺失，无法机器比对")

    try:
        payload = {
            "referenceImage": _data_uri(reference),
            "captureImage": _data_uri(capture),
            "threshold": conf.FACE_THRESHOLD,
        }
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"读取照片失败：{type(exc).__name__}")

    try:
        with _build_client() as client:
            resp = client.post(
                conf.FACE_API_BASE.rstrip("/") + "/verify", json=payload
            )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"face 服务不可用：{type(exc).__name__}")

    return {
        "machine_status": data.get("machineStatus", "UNAVAILABLE"),
        "machine_verified": bool(data.get("machineVerified", False)),
        "machine_distance": data.get("distance"),
        "machine_reason": (data.get("reason") or "")[:255],
    }
