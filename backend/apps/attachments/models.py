"""文件元数据模型。

ownerType/ownerId 只描述挂载关系，**不作为客户端授权依据**（契约 5）：
读取权限由服务端按目标业务对象判断。
"""

from __future__ import annotations

from django.db import models

OWNER_TYPES = (
    "PERSON",
    "TEAM",
    "ENROLLMENT",
    "MATCH",
    "CONTENT",
    "RESOURCE",
    "IDENTITY_CHECK",
)

# 允许的类别 → 允许的扩展名
CATEGORY_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "AVATAR": ("jpg", "jpeg", "png"),
    "ROSTER_PHOTO": ("jpg", "jpeg", "png"),
    "CMS_IMAGE": ("jpg", "jpeg", "png"),
    "CAPTURE_PHOTO": ("jpg", "jpeg", "png"),
    "STAMPED_FORM": ("pdf", "jpg", "jpeg", "png"),
    "STUDENT_STATUS": ("pdf", "jpg", "jpeg", "png"),
    "MATCH_DOCUMENT": ("pdf", "doc", "docx", "xls", "xlsx"),
    "MATERIAL": ("pdf", "doc", "docx", "xls", "xlsx", "jpg", "jpeg", "png"),
}

IMAGE_CATEGORIES = {"AVATAR", "ROSTER_PHOTO", "CMS_IMAGE", "CAPTURE_PHOTO", "MATERIAL"}


class ActiveManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class FileRecord(models.Model):
    id = models.CharField(primary_key=True, max_length=64)
    owner_type = models.CharField(max_length=32)
    owner_id = models.CharField(max_length=64, db_index=True)
    category = models.CharField(max_length=50, blank=True, default="")
    original_name = models.CharField(max_length=255)  # 已脱敏
    content_type = models.CharField(max_length=100, blank=True, default="")
    size_bytes = models.PositiveIntegerField(default=0)
    private = models.BooleanField(default=True)
    storage_key = models.CharField(max_length=255)
    storage_backend = models.CharField(max_length=20, default="local")
    # 图片类文件生成的缩略图存储键（非图片或生成失败时为空）
    thumbnail_key = models.CharField(max_length=255, blank=True, default="")
    uploaded_by = models.CharField(max_length=64, db_index=True)
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "attachments_file_record"
        indexes = [
            models.Index(fields=["owner_type", "owner_id", "category"]),
            models.Index(fields=["uploaded_by"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}:{self.original_name}"
