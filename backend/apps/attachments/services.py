"""文件业务逻辑：类型校验、批量原子性、受控读取。

契约 5：
- 单文件 10 MiB，通用批量最多 100 个、总量 50 MiB，原子完成或整批失败；
- 服务端验证真实内容类型，不信任扩展名；
- 私有文件读取必须按所属业务对象校验权限，不能只凭 ownerId。
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from apps.common import conf
from apps.common.crypto import mask_filename
from apps.common.errors import (
    BATCH_LIMIT_EXCEEDED,
    FILE_TOO_LARGE,
    FORBIDDEN,
    INVALID_ARGUMENT,
    NOT_FOUND,
    UNSUPPORTED_FILE_TYPE,
    ApiError,
)
from apps.common.ids import new_file_id
from apps.common.permissions import ADMIN, MATCH_RECORDER, PROVINCE_REVIEWER
from apps.common.versioning import bump, check_version

from .models import CATEGORY_EXTENSIONS, OWNER_TYPES, FileRecord

# 各业务模块注册"该 ownerId 是否归当前账号"的校验器：
# OWNER_VERIFIERS["ENROLLMENT"] = lambda account, owner_id: ...
# 成员3/4/5 建好报名、人员、比赛表后必须注册，否则这里只能放行（见 _check_owner）。
OWNER_VERIFIERS: dict[str, Callable[[object, str], None]] = {}

_EXT_CONTENT_TYPE = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"


def register_owner_verifier(owner_type: str, verifier: Callable[[object, str], None]) -> None:
    OWNER_VERIFIERS[owner_type] = verifier


def _check_owner(account, owner_type: str, owner_id: str) -> None:
    if owner_type not in OWNER_TYPES:
        raise ApiError(
            INVALID_ARGUMENT,
            "未知的文件归属类型",
            [{"field": "ownerType", "reason": "不在枚举范围内"}],
        )
    if not (owner_id or "").strip():
        # 先传后绑（前端新建人员先传头像）：文件暂无归属对象，仍按角色兜底校验
        # ——只有管理/录入类角色可上传无主文件，普通账号不得借空归属绕过校验
        roles = set(getattr(account, "roles", ()) or ())
        if not roles & {ADMIN, PROVINCE_REVIEWER, MATCH_RECORDER}:
            raise ApiError(
                FORBIDDEN,
                "文件归属对象尚未创建，暂无法受理该上传",
            )
        return
    verifier = OWNER_VERIFIERS.get(owner_type)
    if verifier is not None:
        verifier(account, owner_id)
        return
    # 迁移期兜底：业务表尚未建好时，只允许"管理/录入"类角色上传，
    # 避免普通账号把文件挂到别人的对象上。
    roles = set(getattr(account, "roles", ()) or ())
    if not roles & {ADMIN, PROVINCE_REVIEWER, MATCH_RECORDER}:
        raise ApiError(
            FORBIDDEN,
            f"暂无法校验 {owner_type} 归属，请等待对应模块注册校验器",
        )


def _validate_category(category: str) -> tuple[str, ...]:
    allowed = CATEGORY_EXTENSIONS.get(category)
    if not allowed:
        raise ApiError(
            INVALID_ARGUMENT,
            "未知的文件类别",
            [{"field": "category", "reason": "不在枚举范围内"}],
        )
    return allowed


def _extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _sniff(head: bytes, ext: str) -> None:
    """校验真实内容与扩展名一致，防止改名绕过。"""
    if ext in {"jpg", "jpeg"}:
        ok = head.startswith(b"\xff\xd8\xff")
    elif ext == "png":
        ok = head.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext == "pdf":
        ok = head.startswith(b"%PDF")
    elif ext in {"docx", "xlsx"}:
        ok = head.startswith(_ZIP_MAGIC)
    elif ext in {"doc", "xls"}:
        ok = head.startswith(_OLE_MAGIC)
    else:
        ok = True
    if not ok:
        raise ApiError(
            UNSUPPORTED_FILE_TYPE,
            "文件真实内容与扩展名不符",
            [{"field": "file", "reason": "内容类型校验失败"}],
        )


def _prepare(uploaded: UploadedFile, allowed_ext: tuple[str, ...]) -> tuple[str, bytes]:
    ext = _extension(uploaded.name or "")
    if ext not in allowed_ext:
        raise ApiError(
            UNSUPPORTED_FILE_TYPE,
            "该类别不允许此文件类型",
            [{"field": "file", "reason": f"仅允许 {', '.join(allowed_ext)}"}],
        )
    limit = conf.MAX_FILE_SIZE_MB * 1024 * 1024
    if uploaded.size and uploaded.size > limit:
        raise ApiError(
            FILE_TOO_LARGE,
            f"单文件不得超过 {conf.MAX_FILE_SIZE_MB} MiB",
            [{"field": "file", "reason": "超过大小上限"}],
        )
    data = uploaded.read()
    if len(data) > limit:
        raise ApiError(
            FILE_TOO_LARGE,
            f"单文件不得超过 {conf.MAX_FILE_SIZE_MB} MiB",
            [{"field": "file", "reason": "超过大小上限"}],
        )
    _sniff(data[:16], ext)
    return ext, data


def _store(account, owner_type: str, owner_id: str, category: str, uploaded: UploadedFile) -> FileRecord:
    from .storage import get_storage

    allowed_ext = _validate_category(category)
    ext, data = _prepare(uploaded, allowed_ext)
    file_id = new_file_id()
    storage_key = f"{file_id}.{ext}"
    storage = get_storage()

    import io

    storage.save(storage_key, io.BytesIO(data))
    return FileRecord.objects.create(
        id=file_id,
        owner_type=owner_type,
        owner_id=owner_id,
        category=category,
        original_name=mask_filename(uploaded.name)[:255],
        content_type=_EXT_CONTENT_TYPE.get(ext, uploaded.content_type or ""),
        size_bytes=len(data),
        private=True,
        storage_key=storage_key,
        storage_backend=getattr(storage, "backend", "local"),
        uploaded_by=account.pk,
    )


def upload(account, owner_type: str, owner_id: str, category: str, uploaded: UploadedFile) -> FileRecord:
    _check_owner(account, owner_type, owner_id)
    return _store(account, owner_type, owner_id, category, uploaded)


def upload_batch(
    account, owner_type: str, owner_id: str, category: str, files: Iterable[UploadedFile]
) -> tuple[list[FileRecord], int]:
    files = list(files)
    if not files:
        raise ApiError(
            BATCH_LIMIT_EXCEEDED,
            "批量上传至少 1 个文件",
            [{"field": "files", "reason": "为空"}],
        )
    if len(files) > conf.MAX_BATCH_FILES:
        raise ApiError(
            BATCH_LIMIT_EXCEEDED,
            f"单批次最多 {conf.MAX_BATCH_FILES} 个文件",
            [{"field": "files", "reason": "数量超限"}],
        )
    total = sum(f.size or 0 for f in files)
    if total > conf.MAX_BATCH_TOTAL_MB * 1024 * 1024:
        raise ApiError(
            BATCH_LIMIT_EXCEEDED,
            f"单批次总大小不得超过 {conf.MAX_BATCH_TOTAL_MB} MiB",
            [{"field": "files", "reason": "总量超限"}],
        )

    _check_owner(account, owner_type, owner_id)
    from .storage import get_storage

    storage = get_storage()
    created: list[FileRecord] = []
    try:
        for uploaded in files:
            created.append(_store(account, owner_type, owner_id, category, uploaded))
    except Exception:
        # 整批失败：回滚元数据并清理已落地对象，避免残留
        for record in created:
            storage.delete(record.storage_key)
            FileRecord.all_objects.filter(pk=record.pk).delete()
        raise
    return created, len(created)


def can_access(record: FileRecord, account) -> bool:
    """第四层校验：对象归属。业务模块注册校验器后由校验器裁决。"""
    if record.uploaded_by == account.pk:
        return True
    roles = set(getattr(account, "roles", ()) or ())
    if roles & {ADMIN, PROVINCE_REVIEWER, MATCH_RECORDER}:
        return True
    verifier = OWNER_VERIFIERS.get(record.owner_type)
    if verifier is not None:
        try:
            verifier(account, record.owner_id)
            return True
        except ApiError:
            return False
    return False


def get_record(account, file_id: str, *, require_access: bool = True) -> FileRecord:
    record = FileRecord.objects.filter(pk=file_id).first()
    if record is None:
        raise ApiError(NOT_FOUND)
    if require_access and not can_access(record, account):
        raise ApiError(FORBIDDEN, "无权访问该文件", [{"field": "fileId", "reason": "超出数据范围"}])
    return record


def delete_record(account, file_id: str, version: Optional[int]) -> FileRecord:
    record = get_record(account, file_id)
    check_version(record, version)
    # 与审批快照/比赛记录绑定的文件不允许删除：业务模块注册校验器后在这里拦截
    verifier = OWNER_VERIFIERS.get(record.owner_type)
    if verifier is not None and record.uploaded_by != account.pk:
        raise ApiError(FORBIDDEN, "不能删除他人上传的文件")
    record.deleted_at = timezone.now()
    bump(record)
    record.save()
    return record
