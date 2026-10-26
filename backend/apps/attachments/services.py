"""文件业务逻辑：类型校验、批量原子性、受控读取。

契约 5：
- 单文件 10 MiB，通用批量最多 100 个、总量 50 MiB，原子完成或整批失败；
- 服务端验证真实内容类型，不信任扩展名；
- 私有文件读取必须按所属业务对象校验权限，不能只凭 ownerId。
"""

from __future__ import annotations

from datetime import timedelta
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

from .models import CATEGORY_EXTENSIONS, IMAGE_CATEGORIES, OWNER_TYPES, FileRecord

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


def _make_thumbnail(data: bytes, max_side: int = 320) -> Optional[bytes]:
    """生成 JPEG 缩略图。非图片或解码失败返回 None，不影响上传主流程。"""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((max_side, max_side))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=82, optimize=True)
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - 缩略图失败不能阻塞上传
        return None


def _store(account, owner_type: str, owner_id: str, category: str, uploaded: UploadedFile) -> FileRecord:
    from .storage import get_storage

    allowed_ext = _validate_category(category)
    ext, data = _prepare(uploaded, allowed_ext)
    file_id = new_file_id()
    storage_key = f"{file_id}.{ext}"
    storage = get_storage()

    import io

    storage.save(storage_key, io.BytesIO(data))

    thumbnail_key = ""
    if ext in {"jpg", "jpeg", "png"} and category in IMAGE_CATEGORIES:
        thumb = _make_thumbnail(data)
        if thumb:
            thumbnail_key = f"{file_id}.thumb.jpg"
            storage.save(thumbnail_key, io.BytesIO(thumb))

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
        thumbnail_key=thumbnail_key,
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


def _signing_key() -> bytes:
    return (conf.CRYPTO_KEY or conf.JWT_SECRET).encode("utf-8")


def _signature(file_id: str, variant: str, expires_at: int) -> str:
    import base64
    import hashlib
    import hmac

    message = f"{file_id}:{variant}:{expires_at}".encode("utf-8")
    digest = hmac.new(_signing_key(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def signed_url(record: FileRecord, variant: str = "content", ttl_seconds: Optional[int] = None) -> dict:
    """生成短期可直连的展示地址，供 <img> 等无法带 Authorization 头的场景使用。

    签名绑定 fileId + variant + 过期时间，泄露的旧地址到期自动失效；
    文件本身仍是私有对象，不属于公开资源。
    """
    import time

    if variant not in {"content", "thumbnail"}:
        raise ApiError(
            INVALID_ARGUMENT,
            "未知的展示类型",
            [{"field": "variant", "reason": "仅支持 content / thumbnail"}],
        )
    if variant == "thumbnail" and not record.thumbnail_key:
        raise ApiError(NOT_FOUND, "该文件没有缩略图")
    default_ttl = int(conf.FILE_TICKET_TTL_SECONDS)
    ttl = max(30, min(int(ttl_seconds or default_ttl), 30 * 24 * 3600))
    expires_at = int(time.time()) + ttl
    signature = _signature(record.pk, variant, expires_at)
    return {
        "url": f"/api/v1/public/files/{record.pk}/{variant}?exp={expires_at}&sign={signature}",
        "expiresAt": timezone.now() + timedelta(seconds=ttl),
        "variant": variant,
    }


def portal_image_url(file_id: str, variant: str = "content") -> str:
    """门户公开投影专用：为已发布内容的图片签发长期地址（默认 7 天）。

    调用方是公开读接口（如 `/public/portal-resources`）在组装 PublicResource.imageUrl
    时使用；业务方必须先确认该资源已发布，不要把草稿内容投影出去。
    """
    record = get_public_record(file_id)
    return signed_url(record, variant, conf.PORTAL_FILE_TICKET_TTL_SECONDS)["url"]



def verify_signature(file_id: str, variant: str, expires_at: int, signature: str) -> None:
    import hmac
    import time

    if variant not in {"content", "thumbnail"}:
        raise ApiError(FORBIDDEN, "展示地址无效")
    if expires_at < int(time.time()):
        raise ApiError(FORBIDDEN, "展示地址已过期，请重新获取")
    expected = _signature(file_id, variant, expires_at)
    if not hmac.compare_digest(expected, signature or ""):
        raise ApiError(FORBIDDEN, "展示地址签名无效")


def get_public_record(file_id: str) -> FileRecord:
    record = FileRecord.objects.filter(pk=file_id).first()
    if record is None:
        raise ApiError(NOT_FOUND)
    return record
