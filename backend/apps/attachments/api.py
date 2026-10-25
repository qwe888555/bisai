"""attachments 接口：上传、元数据、受控下载、删除。

私有文件下载返回真实二进制 FileResponse，不走 JSON 信封。
"""

from __future__ import annotations

from urllib.parse import quote

from django.http import FileResponse
from ninja import Field, File, Form, Router
from ninja.files import UploadedFile

from apps.common.authentication import JWTAuth
from apps.common.envelope import action_result, ok
from apps.common.errors import INVALID_ARGUMENT, NOT_FOUND, ApiError
from apps.common.middleware import audit_state
from apps.common.permissions import ALL_ROLES, register_operation, require
from apps.common.schemas import EnvelopeOut, StrictSchema

from . import services
from .models import FileRecord

router = Router(tags=["files"])
_auth = JWTAuth()

register_operation("fileUpload", ALL_ROLES)
register_operation("fileBatchUpload", ALL_ROLES)
register_operation("fileGet", ALL_ROLES)
register_operation("fileDownload", ALL_ROLES)
register_operation("fileDelete", ALL_ROLES)


class FileUploadIn(StrictSchema):
    """单文件上传表单字段，字段名与 FileUploadRequest 一致。"""

    ownerType: str
    ownerId: str = Field(..., min_length=1, max_length=64)
    category: str


class FileBatchUploadIn(StrictSchema):
    """批量上传表单字段，字段名与 FileBatchUploadRequest 一致。"""

    ownerType: str
    ownerId: str = Field(..., min_length=1, max_length=64)
    category: str = Field(..., max_length=50)


def _file_out(record: FileRecord) -> dict:
    """等价于 FileRecordSerializer.to_representation，字段锁定 camelCase。"""
    data = {
        "id": record.pk,
        "ownerType": record.owner_type,
        "ownerId": record.owner_id,
        "originalName": record.original_name,
        "sizeBytes": int(record.size_bytes or 0),
        "private": bool(record.private),
        "version": int(record.version or 0),
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
    }
    if record.category:
        data["category"] = record.category
    if record.content_type:
        data["contentType"] = record.content_type
    if record.thumbnail_key:
        data["thumbnailUrl"] = f"/files/{record.pk}/thumbnail"
    return data


def _require_version(request) -> int:
    raw = request.GET.get("version")
    if raw in (None, ""):
        raise ApiError(
            INVALID_ARGUMENT,
            "缺少 version",
            [{"field": "version", "reason": "该操作必须提交乐观锁版本"}],
        )
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError(INVALID_ARGUMENT, "version 必须是整数") from exc


@router.post("/files", operation_id="fileUpload", auth=_auth, response=EnvelopeOut, summary="上传单个文件")
def file_upload(
    request,
    data: FileUploadIn = Form(...),
    file: UploadedFile = File(...),
):
    actor = require("fileUpload")
    record = services.upload(actor, data.ownerType, data.ownerId, data.category, file)
    audit_state(
        request,
        "file.upload",
        object_type="FileRecord",
        object_id=record.pk,
        summary=f"上传文件 {record.category}",
    )
    return ok(_file_out(record))


@router.post("/files/batch", operation_id="fileBatchUpload", auth=_auth, response=EnvelopeOut, summary="批量上传文件")
def file_batch_upload(
    request,
    data: FileBatchUploadIn = Form(...),
    files: list[UploadedFile] = File(..., min_length=1, max_length=100),
):
    actor = require("fileBatchUpload")
    records, count = services.upload_batch(
        actor, data.ownerType, data.ownerId, data.category, files
    )
    audit_state(
        request,
        "file.batchUpload",
        object_type=data.ownerType,
        object_id=data.ownerId,
        summary=f"批量上传 {count} 个文件",
    )
    return ok({"files": [_file_out(item) for item in records], "count": count})


@router.get("/files/{file_id}", operation_id="fileGet", auth=_auth, response=EnvelopeOut, summary="文件元数据")
def file_get(request, file_id: str):
    actor = require("fileGet")
    record = services.get_record(actor, file_id)
    return ok(_file_out(record))


@router.delete("/files/{file_id}", operation_id="fileDelete", auth=_auth, response=EnvelopeOut, summary="删除文件")
def file_delete(request, file_id: str):
    actor = require("fileDelete")
    record = services.delete_record(actor, file_id, _require_version(request))
    audit_state(
        request,
        "file.delete",
        object_type="FileRecord",
        object_id=record.pk,
        summary="删除文件",
    )
    return action_result(True, record.pk, record.version)


@router.get(
    "/files/{file_id}/content",
    operation_id="fileDownload",
    auth=_auth,
    response=None,
    summary="授权下载私有文件",
)
def file_download(request, file_id: str):
    actor = require("fileDownload")
    from .storage import get_storage

    record = services.get_record(actor, file_id)
    stream = get_storage().open(record.storage_key)
    response = FileResponse(
        stream,
        content_type=record.content_type or "application/octet-stream",
    )
    # 私有文件禁止中间代理与浏览器缓存（契约 3.1）
    response["Cache-Control"] = "private, no-store"
    response["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{quote(record.original_name)}"
    )
    response["Content-Length"] = str(record.size_bytes)
    return response


@router.get(
    "/files/{file_id}/thumbnail",
    operation_id="fileThumbnail",
    auth=_auth,
    response=None,
    summary="图片缩略图（授权读取）",
)
def file_thumbnail(request, file_id: str):
    actor = require("fileDownload")
    from .storage import get_storage

    record = services.get_record(actor, file_id)
    if not record.thumbnail_key:
        raise ApiError(NOT_FOUND, "该文件没有缩略图")
    stream = get_storage().open(record.thumbnail_key)
    response = FileResponse(stream, content_type="image/jpeg")
    response["Cache-Control"] = "private, no-store"
    return response
