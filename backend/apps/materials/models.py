"""材料域模型：类型配置/人员材料/球队材料/审核历史/文件版本。

依据：docs/design/成员4-DDL草案-V1.4.sql（V1.4-r1，冻结）。
- material_audit_log / material_file_version 为多态逻辑引用（material_kind +
  material_id），无外键，引用完整性由应用层 + 定期校验任务保证（A3/F7）；
- R6（allow_multiple=false 时归属+类型 有效材料唯一）由服务层强制，
  idempotency_key 唯一索引兜底重复提交；
- person_material / team_material 软删；material_type 只停用（is_active）。
"""

from __future__ import annotations

from django.db import models

from apps.common import enums


class MaterialType(models.Model):
    """材料类型配置（通用材料引擎，新类型零代码）。"""

    code = models.CharField(max_length=50, db_comment="材料类型编码")
    name = models.CharField(max_length=100, db_comment="类型名称")
    applies_to = models.CharField(
        max_length=20,
        choices=enums.MaterialAppliesTo.choices,
        db_comment="PERSON/TEAM",
    )
    is_required = models.BooleanField(default=False, db_comment="是否必备")
    need_audit = models.BooleanField(default=True, db_comment="是否需审核")
    allow_multiple = models.BooleanField(default=False, db_comment="是否允许多份(R6服务层规则依据)")
    allowed_ext = models.CharField(
        max_length=200, default="jpg,jpeg,png,pdf", db_comment="允许扩展名"
    )
    max_size_mb = models.SmallIntegerField(default=10, db_comment="单文件上限MB")
    sort_order = models.IntegerField(default=0, db_comment="排序")
    is_active = models.BooleanField(default=True, db_comment="只停用不删")
    remark = models.CharField(max_length=200, null=True, blank=True, default="", db_comment="备注")
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="创建人(逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "material_type"
        db_table_comment = "材料类型配置(通用材料引擎,新类型零代码)"
        constraints = [
            models.UniqueConstraint(fields=["code"], name="uk_material_type_code"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.code}({self.applies_to})"


class PersonMaterial(models.Model):
    """人员材料（R6 唯一性服务层强制，替换留版本表，软删）。"""

    person = models.ForeignKey(
        "persons.Person",
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="所属人员(归属禁改)",
    )
    type = models.ForeignKey(
        MaterialType,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="材料类型(归属禁改)",
    )
    file_id = models.CharField(max_length=64, db_comment="文件(逻辑引用文件服务,不存URL)")
    file_name = models.CharField(max_length=200, db_comment="文件名")
    file_size = models.IntegerField(null=True, blank=True, db_comment="文件大小字节")
    file_ext = models.CharField(max_length=10, null=True, blank=True, db_comment="扩展名")
    status = models.CharField(
        max_length=20,
        default=enums.MaterialStatus.DRAFT,
        choices=enums.MaterialStatus.choices,
        db_comment="DRAFT/PENDING/APPROVED/REJECTED/WITHDRAWN",
    )
    audit_remark = models.CharField(
        max_length=200, null=True, blank=True, default="", db_comment="审核意见"
    )
    audited_by = models.BigIntegerField(null=True, blank=True, db_comment="审核人(逻辑引用)")
    audited_at = models.DateTimeField(null=True, blank=True, db_comment="审核时间")
    uploaded_by = models.BigIntegerField(null=True, blank=True, db_comment="上传人(逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    idempotency_key = models.CharField(
        max_length=64, null=True, blank=True, db_comment="上传幂等键(可空唯一,重复提交返回已有记录)"
    )
    remark = models.CharField(max_length=200, null=True, blank=True, default="", db_comment="备注")
    is_deleted = models.BooleanField(default=False, db_comment="软删标记")
    deleted_at = models.DateTimeField(null=True, blank=True, db_comment="软删时间")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "person_material"
        db_table_comment = "人员材料(R6唯一性服务层强制,替换留版本表,软删)"
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key"], name="uk_pm_idempotency"),
        ]
        indexes = [
            models.Index(fields=["person", "type", "status"], name="idx_pm_person_type_status"),
            models.Index(fields=["status", "is_deleted"], name="idx_pm_status_del"),
            models.Index(fields=["type"], name="idx_pm_type"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"pm:{self.id}:{self.file_name}"


class TeamMaterial(models.Model):
    """球队材料（R6 唯一性服务层强制，软删）。"""

    team = models.ForeignKey(
        "teams.Team",
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="所属球队(归属禁改)",
    )
    type = models.ForeignKey(
        MaterialType,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="材料类型(归属禁改)",
    )
    file_id = models.CharField(max_length=64, db_comment="文件(逻辑引用文件服务)")
    file_name = models.CharField(max_length=200, db_comment="文件名")
    file_size = models.IntegerField(null=True, blank=True, db_comment="文件大小字节")
    file_ext = models.CharField(max_length=10, null=True, blank=True, db_comment="扩展名")
    status = models.CharField(
        max_length=20,
        default=enums.MaterialStatus.DRAFT,
        choices=enums.MaterialStatus.choices,
        db_comment="DRAFT/PENDING/APPROVED/REJECTED/WITHDRAWN",
    )
    audit_remark = models.CharField(
        max_length=200, null=True, blank=True, default="", db_comment="审核意见"
    )
    audited_by = models.BigIntegerField(null=True, blank=True, db_comment="审核人(逻辑引用)")
    audited_at = models.DateTimeField(null=True, blank=True, db_comment="审核时间")
    uploaded_by = models.BigIntegerField(null=True, blank=True, db_comment="上传人(逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    idempotency_key = models.CharField(
        max_length=64, null=True, blank=True, db_comment="上传幂等键(可空唯一)"
    )
    remark = models.CharField(max_length=200, null=True, blank=True, default="", db_comment="备注")
    is_deleted = models.BooleanField(default=False, db_comment="软删标记")
    deleted_at = models.DateTimeField(null=True, blank=True, db_comment="软删时间")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "team_material"
        db_table_comment = "球队材料(R6唯一性服务层强制,软删)"
        constraints = [
            models.UniqueConstraint(fields=["idempotency_key"], name="uk_tm_idempotency"),
        ]
        indexes = [
            models.Index(fields=["team", "type", "status"], name="idx_tm2_team_type_status"),
            models.Index(fields=["status", "is_deleted"], name="idx_tm2_status_del"),
            models.Index(fields=["type"], name="idx_tm2_type"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"tm:{self.id}:{self.file_name}"


class MaterialAuditLog(models.Model):
    """材料审核历史【append-only，多态逻辑引用无FK，引用完整性由应用层+定期校验任务保证】。"""

    material_kind = models.CharField(
        max_length=20,
        choices=enums.MaterialAppliesTo.choices,
        db_comment="PERSON/TEAM(多态逻辑引用,无FK)",
    )
    material_id = models.BigIntegerField(db_comment="材料ID(指向person_material或team_material)")
    from_status = models.CharField(max_length=20, db_comment="原状态")
    to_status = models.CharField(max_length=20, db_comment="新状态")
    auditor = models.BigIntegerField(null=True, blank=True, db_comment="审核人(逻辑引用)")
    comment = models.CharField(max_length=200, null=True, blank=True, default="", db_comment="审核意见")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC,append-only实际不更新)")

    class Meta:
        db_table = "material_audit_log"
        db_table_comment = "材料审核历史【append-only,引用完整性由应用层+定期校验任务保证】"
        indexes = [
            models.Index(fields=["material_kind", "material_id"], name="idx_mal_material"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.material_kind}:{self.material_id}:{self.from_status}->{self.to_status}"


class MaterialFileVersion(models.Model):
    """材料文件版本（替换留痕）【append-only；F5已删冗余索引idx_mfv_material，检索走uk_mfv_version最左前缀】。"""

    material_kind = models.CharField(
        max_length=20,
        choices=enums.MaterialAppliesTo.choices,
        db_comment="PERSON/TEAM(多态逻辑引用,无FK)",
    )
    material_id = models.BigIntegerField(db_comment="材料ID")
    version = models.IntegerField(default=1, db_comment="版本号,替换+1")
    file_id = models.CharField(max_length=64, db_comment="该版本文件(旧文件留痕,逻辑引用文件服务)")
    file_name = models.CharField(max_length=200, db_comment="文件名")
    uploaded_by = models.BigIntegerField(null=True, blank=True, db_comment="上传人(逻辑引用)")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC,append-only实际不更新)")

    class Meta:
        db_table = "material_file_version"
        db_table_comment = "材料文件版本(替换留痕)【append-only;F5已删冗余索引idx_mfv_material,检索走uk_mfv_version最左前缀】"
        constraints = [
            models.UniqueConstraint(
                fields=["material_kind", "material_id", "version"],
                name="uk_mfv_version",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.material_kind}:{self.material_id}:v{self.version}"
