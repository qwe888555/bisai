"""基础设施模型：批量导入批次/导入失败明细/通用操作审计。

依据：docs/design/成员4-DDL草案-V1.4.sql（V1.4-r1，冻结）。
- import_row_error 随批次 CASCADE 删除——Django 迁移 FK 无法带 ON DELETE 子句，
  该 FK 用 db_constraint=False，由 0002 迁移按 DDL 显式建 fk_ire_batch
  （MySQL，ON DELETE CASCADE ON UPDATE RESTRICT）；
- audit_log 无外键为定稿取舍（A5），actor_user_id 等逻辑引用底座用户。
"""

from __future__ import annotations

from django.db import models

from apps.common import enums


class ImportBatch(models.Model):
    """批量导入批次。"""

    batch_no = models.CharField(
        max_length=50, db_comment="批次号(导入幂等键,重复提交返回已有批次)"
    )
    import_type = models.CharField(
        max_length=20,
        choices=enums.ImportType.choices,
        db_comment="REGION/SCHOOL/DICT/MATERIAL_TYPE/PERSON/TEAM",
    )
    status = models.CharField(
        max_length=20,
        default=enums.ImportStatus.PROCESSING,
        choices=enums.ImportStatus.choices,
        db_comment="PROCESSING/COMPLETED/PARTIAL/FAILED",
    )
    total = models.IntegerField(default=0, db_comment="总行数")
    success_count = models.IntegerField(default=0, db_comment="成功行数")
    failed_count = models.IntegerField(default=0, db_comment="失败行数")
    source_file_id = models.CharField(
        max_length=64, null=True, blank=True, db_comment="源文件(逻辑引用文件服务)"
    )
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="操作人(逻辑引用)")
    finished_at = models.DateTimeField(null=True, blank=True, db_comment="完成时间")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "import_batch"
        db_table_comment = "批量导入批次"
        constraints = [
            models.UniqueConstraint(fields=["batch_no"], name="uk_import_batch_no"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.batch_no}:{self.import_type}"


class ImportRowError(models.Model):
    """导入失败行明细【append-only，明细随批次CASCADE】。"""

    batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        # DB 层由 0002 迁移显式建 fk_ire_batch（ON DELETE CASCADE ON UPDATE RESTRICT，MySQL）。
        db_constraint=False,
        db_index=False,
        db_comment="所属批次",
    )
    row_no = models.IntegerField(db_comment="源文件行号")
    raw_data = models.JSONField(null=True, blank=True, db_comment="原始数据(敏感字段只写掩码)")
    error_code = models.CharField(max_length=50, db_comment="错误码")
    error_message = models.CharField(
        max_length=200, null=True, blank=True, default="", db_comment="错误信息"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC,append-only实际不更新)")

    class Meta:
        db_table = "import_row_error"
        db_table_comment = "导入失败行明细【append-only,明细随批次CASCADE】"
        indexes = [
            models.Index(fields=["batch"], name="idx_ire_batch"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.batch_id}:row{self.row_no}:{self.error_code}"


class AuditLog(models.Model):
    """通用操作审计【append-only，无外键为定稿取舍；部署层收回业务账号UPDATE/DELETE权限】。"""

    actor_user_id = models.BigIntegerField(
        null=True, blank=True, db_comment="操作人(底座用户ID,逻辑引用,无FK-设计取舍A5)"
    )
    action = models.CharField(
        max_length=50,
        db_comment="TEAM_CREATE/MEMBER_EXIT/TRANSFER_VERIFY/IMPORT/EXPORT/DISABLE等",
    )
    target_type = models.CharField(
        max_length=50, db_comment="目标对象类型(如team/team_member,逻辑引用)"
    )
    target_id = models.BigIntegerField(null=True, blank=True, db_comment="目标对象ID")
    detail_json = models.JSONField(null=True, blank=True, db_comment="动作摘要(敏感字段只写掩码)")
    ip_address = models.CharField(max_length=39, null=True, blank=True, db_comment="来源IP")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC,append-only实际不更新)")

    class Meta:
        db_table = "audit_log"
        db_table_comment = "通用操作审计【append-only,无外键为定稿取舍;部署层收回业务账号UPDATE/DELETE权限】"
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="idx_audit_target"),
            models.Index(fields=["actor_user_id", "created_at"], name="idx_audit_actor"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.action}:{self.target_type}:{self.target_id}"
