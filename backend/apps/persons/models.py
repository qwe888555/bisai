"""人员域模型：区划/学校/字典/人员主档/扩展档案/变更留痕/合并/敏感访问审计。

依据：docs/design/成员4-DDL草案-V1.4.sql（V1.4-r1，冻结）。
列名、类型、NULL、默认值、UNIQUE、索引名均严格对齐 DDL：
- 全部外键 on_delete=PROTECT（DDL 实现 ON DELETE RESTRICT）；
  扩展档案随主档 CASCADE——Django 迁移 FK 无法带 ON DELETE 子句，
  这两个 FK 用 db_constraint=False，由 0002 迁移按 DDL 显式建
  fk_athlete_person/fk_coach_person（MySQL，ON DELETE CASCADE ON UPDATE RESTRICT）；
- 外键列 db_index=False，索引由 Meta.indexes 按冻结名精确保留（防重复索引）；
- created_at/updated_at 用 auto_now_add/auto_now（总体设计 §4.0）；
- 逻辑引用（底座用户、文件服务）不建外键，存 BigInt/CharField。
"""

from __future__ import annotations

from django.db import models

from apps.common import enums
from apps.common.fields import TextField, TinyIntegerField


class Region(models.Model):
    """行政区划（基础数据，只停用不删）。"""

    name = models.CharField(max_length=50, db_comment="区划名")
    code = models.CharField(max_length=12, db_comment="国标区划码")
    level = models.SmallIntegerField(
        choices=enums.RegionLevel.choices, db_comment="1省/2市州/3区县"
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="上级区划（省级为NULL）",
    )
    sort_order = models.IntegerField(default=0, db_comment="排序")
    is_active = models.BooleanField(default=True, db_comment="是否启用")
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="创建人(底座用户ID,逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "base_region"
        db_table_comment = "行政区划(基础数据,只停用不删)"
        constraints = [
            models.UniqueConstraint(fields=["code"], name="uk_region_code"),
        ]
        indexes = [
            models.Index(fields=["level"], name="idx_region_level"),
            models.Index(fields=["parent", "is_active"], name="idx_region_parent"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.name}({self.code})"


class School(models.Model):
    """学校（基础数据）。"""

    name = models.CharField(max_length=100, db_comment="校名")
    code = models.CharField(
        max_length=20, null=True, blank=True, db_comment="学校标识码(可空唯一,多个NULL合法)"
    )
    school_type = models.CharField(
        max_length=20,
        db_comment="PRIMARY/JUNIOR/SENIOR/NINE_YEAR/COMPLETE/MIDDLE_VOC(字典逻辑引用)",
    )
    region = models.ForeignKey(
        Region,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="所属区县",
    )
    address = models.CharField(max_length=200, null=True, blank=True, db_comment="地址")
    is_active = models.BooleanField(default=True, db_comment="是否启用")
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="创建人(逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "base_school"
        db_table_comment = "学校(基础数据)"
        constraints = [
            models.UniqueConstraint(fields=["code"], name="uk_school_code"),
            models.UniqueConstraint(fields=["name", "region"], name="uk_school_name_region"),
        ]
        indexes = [
            models.Index(fields=["name"], name="idx_school_name"),
            models.Index(fields=["region"], name="idx_school_region"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.name}({self.code or '无标识码'})"


class DictType(models.Model):
    """字典类型（基础数据）。"""

    code = models.CharField(max_length=50, db_comment="字典类型编码 如team_category/position")
    name = models.CharField(max_length=100, db_comment="类型名称")
    remark = models.CharField(max_length=200, null=True, blank=True, default="", db_comment="备注")
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="创建人(逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "base_dict_type"
        db_table_comment = "字典类型(基础数据)"
        constraints = [
            models.UniqueConstraint(fields=["code"], name="uk_dict_type_code"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return self.code


class DictItem(models.Model):
    """字典项（基础数据）。"""

    type = models.ForeignKey(
        DictType,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="所属字典类型",
    )
    value = models.CharField(max_length=50, db_comment="稳定编码,含义禁改")
    label = models.CharField(max_length=100, db_comment="展示名,可改")
    sort_order = models.IntegerField(default=0, db_comment="排序")
    is_active = models.BooleanField(default=True, db_comment="只停用不删")
    ext_json = models.JSONField(null=True, blank=True, db_comment="结构化扩展(组别属性,等成员5清单)")
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="创建人(逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "base_dict_item"
        db_table_comment = "字典项(基础数据)"
        constraints = [
            models.UniqueConstraint(fields=["type", "value"], name="uk_dict_item_value"),
        ]
        indexes = [
            models.Index(fields=["type", "is_active"], name="idx_dict_item_active"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.type_id}:{self.value}"


class Person(models.Model):
    """人员主档（唯一主表，软删，敏感字段加密三件套）。"""

    person_no = models.CharField(max_length=20, db_comment="档案号 P+年份+8位id(应用层回填)")
    full_name = models.CharField(max_length=50, db_comment="姓名")
    gender = TinyIntegerField(
        default=enums.Gender.UNKNOWN,
        choices=enums.Gender.choices,
        db_comment="0未知/1男/2女",
    )
    birth_date = models.DateField(null=True, blank=True, db_comment="出生日期(身份证解析)")
    id_card_cipher = models.CharField(max_length=256, db_comment="身份证号AES密文")
    id_card_hash = models.CharField(
        max_length=64, db_comment="身份证号SHA256+盐,全局唯一(含软删行→复用不重建)"
    )
    id_card_masked = models.CharField(max_length=18, db_comment="身份证掩码")
    phone_cipher = models.CharField(max_length=256, null=True, blank=True, db_comment="手机号AES密文")
    phone_hash = models.CharField(
        max_length=64, null=True, blank=True, db_comment="手机号哈希(精确查重,可空非唯一)"
    )
    phone_masked = models.CharField(max_length=11, null=True, blank=True, db_comment="手机号掩码")
    person_category = models.CharField(
        max_length=20,
        choices=enums.PersonCategory.choices,
        db_comment="ATHLETE/COACH(一人一身份,#3)",
    )
    audit_status = models.CharField(
        max_length=20,
        default=enums.AuditStatus.DRAFT,
        choices=enums.AuditStatus.choices,
        db_comment="DRAFT/PENDING/APPROVED/REJECTED",
    )
    status = models.CharField(
        max_length=20,
        default=enums.EnableStatus.ACTIVE,
        choices=enums.EnableStatus.choices,
        db_comment="ACTIVE/DISABLED",
    )
    region = models.ForeignKey(
        Region,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="所属市州区划(创建时业务必填,服务层)",
    )
    school = models.ForeignKey(
        School,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="所属学校(运动员业务必填,服务层)",
    )
    photo_file_id = models.CharField(max_length=64, null=True, blank=True, db_comment="照片(逻辑引用文件服务)")
    education = models.CharField(
        max_length=100, null=True, blank=True, db_comment="学历(自由文本,可选,E1冻结评审批准加列)"
    )
    import_batch = models.ForeignKey(
        "infra.ImportBatch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="导入批次(追溯)",
    )
    remark = TextField(null=True, blank=True, db_comment="备注")
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="创建人(逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    is_deleted = models.BooleanField(default=False, db_comment="软删标记")
    deleted_at = models.DateTimeField(null=True, blank=True, db_comment="软删时间")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "person"
        db_table_comment = "人员主档(唯一主表,软删,敏感字段加密)"
        constraints = [
            models.UniqueConstraint(fields=["person_no"], name="uk_person_no"),
            models.UniqueConstraint(fields=["id_card_hash"], name="uk_person_id_card_hash"),
        ]
        indexes = [
            models.Index(fields=["full_name"], name="idx_person_name"),
            models.Index(fields=["person_category", "audit_status"], name="idx_person_cat_audit"),
            models.Index(fields=["status", "is_deleted"], name="idx_person_status_del"),
            models.Index(fields=["region"], name="idx_person_region"),
            models.Index(fields=["school"], name="idx_person_school"),
            models.Index(fields=["phone_hash"], name="idx_person_phone_hash"),
            models.Index(fields=["import_batch"], name="idx_person_import"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.person_no}:{self.full_name}"


class AthleteProfile(models.Model):
    """运动员扩展档案（随主档，1:1，person_id 同时为主键）。"""

    person = models.OneToOneField(
        Person,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="athlete_profile",
        # DB 层由 0002 迁移显式建 fk_athlete_person（ON DELETE CASCADE ON UPDATE RESTRICT，MySQL），
        # Django 迁移生成的 FK 无法带 ON DELETE 子句，故不建默认约束（SQLite 开发库同理无 FK）。
        db_constraint=False,
        db_comment="人员ID(OneToOne,同时为主键)",
    )
    grade_level = models.CharField(
        max_length=20,
        db_comment="学段 PRIMARY_LOW/PRIMARY_HIGH/JUNIOR/SENIOR(字典逻辑引用)",
    )
    student_no = models.CharField(max_length=30, null=True, blank=True, db_comment="学籍号")
    position_code = models.CharField(max_length=20, null=True, blank=True, db_comment="场上位置(字典逻辑引用)")
    guardian_name = models.CharField(max_length=50, null=True, blank=True, db_comment="监护人姓名")
    guardian_relation = models.CharField(
        max_length=20, null=True, blank=True, db_comment="与监护人关系(字典逻辑引用)"
    )
    guardian_phone_cipher = models.CharField(
        max_length=256, null=True, blank=True, db_comment="监护人手机号密文"
    )
    guardian_phone_hash = models.CharField(
        max_length=64, null=True, blank=True, db_comment="监护人手机号哈希"
    )
    guardian_phone_masked = models.CharField(
        max_length=11, null=True, blank=True, db_comment="监护人手机号掩码"
    )
    ext_json = models.JSONField(null=True, blank=True, db_comment="预留扩展")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "person_athlete_profile"
        db_table_comment = "运动员扩展档案(随主档,1:1)"
        indexes = [
            models.Index(fields=["student_no"], name="idx_athlete_student"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"athlete:{self.person_id}"


class CoachProfile(models.Model):
    """教练员扩展档案（随主档，1:1，person_id 同时为主键）。"""

    person = models.OneToOneField(
        Person,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="coach_profile",
        # DB 层由 0002 迁移显式建 fk_coach_person（ON DELETE CASCADE ON UPDATE RESTRICT，MySQL）。
        db_constraint=False,
        db_comment="人员ID(OneToOne,同时为主键)",
    )
    coach_level = models.CharField(
        max_length=20, null=True, blank=True, db_comment="教练等级(字典逻辑引用)"
    )
    certificate_no = models.CharField(max_length=50, null=True, blank=True, db_comment="证书编号")
    certificate_expire_date = models.DateField(null=True, blank=True, db_comment="证书有效期至")
    ext_json = models.JSONField(null=True, blank=True, db_comment="预留扩展")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "person_coach_profile"
        db_table_comment = "教练员扩展档案(随主档,1:1)"
        indexes = [
            models.Index(fields=["certificate_no"], name="idx_coach_cert"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"coach:{self.person_id}"


class PersonChangeLog(models.Model):
    """人员字段级变更日志【append-only：禁 update/delete，部署层收回权限】。"""

    person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="人员ID",
    )
    change_type = models.CharField(
        max_length=20,
        choices=enums.ChangeType.choices,
        db_comment="CREATE/UPDATE/AUDIT/MERGE/DISABLE/ENABLE",
    )
    field_changes = models.JSONField(
        null=True, blank=True, db_comment="字段级diff [{field,old,new}],敏感字段只写掩码"
    )
    changed_by = models.BigIntegerField(null=True, blank=True, db_comment="操作人(逻辑引用)")
    reason = models.CharField(max_length=200, null=True, blank=True, default="", db_comment="变更原因")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC,append-only实际不更新)")

    class Meta:
        db_table = "person_change_log"
        db_table_comment = "人员字段级变更日志【append-only:禁update/delete,部署层收回权限】"
        indexes = [
            models.Index(fields=["person", "created_at"], name="idx_pcl_person"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.person_id}:{self.change_type}"


class PersonMergeRecord(models.Model):
    """人员合并记录（保留原ID+映射，生效后不可自动撤销Q7，禁止物理删除）。"""

    primary_person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="merges_kept",
        db_index=False,
        db_comment="保留档人员",
    )
    merged_person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="merges_merged",
        db_index=False,
        db_comment="被合并档人员(生效后软删)",
    )
    merged_by = models.BigIntegerField(null=True, blank=True, db_comment="合并操作人(逻辑引用)")
    reason = models.CharField(max_length=200, db_comment="合并原因")
    confirm_status = models.CharField(
        max_length=20,
        default=enums.ConfirmStatus.PENDING_CONFIRM,
        choices=enums.ConfirmStatus.choices,
        db_comment="PENDING_CONFIRM/CONFIRMED/CANCELLED(映射接口仅CONFIRMED后生效)",
    )
    confirmed_by = models.BigIntegerField(null=True, blank=True, db_comment="签字确认人(队伍/核心成员)")
    confirmed_at = models.DateTimeField(null=True, blank=True, db_comment="确认时间")
    confirm_file_id = models.CharField(
        max_length=64, null=True, blank=True, db_comment="签字凭证(逻辑引用文件服务)"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "person_merge_record"
        db_table_comment = "人员合并记录(保留原ID+映射,生效后不可自动撤销Q7,禁止物理删除)"
        indexes = [
            models.Index(fields=["primary_person"], name="idx_pmr_primary"),
            models.Index(fields=["merged_person"], name="idx_pmr_merged"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.merged_person_id}->{self.primary_person_id}"


class SensitiveAccessLog(models.Model):
    """明文身份证访问审计【append-only，单笔限流，响应不落日志】。"""

    accessed_by = models.BigIntegerField(db_comment="访问人(底座用户ID,逻辑引用)")
    target_person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="被查看人员",
    )
    scene = models.CharField(
        max_length=50,
        choices=enums.AccessScene.choices,
        db_comment="IDCARD_VIEW/PDF_EXPORT/VERIFICATION",
    )
    business_ref = models.CharField(max_length=50, null=True, blank=True, db_comment="业务依据(如报名单号)")
    ip_address = models.CharField(max_length=39, null=True, blank=True, db_comment="来源IP(IPv6兼容)")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC,append-only实际不更新)")

    class Meta:
        db_table = "sensitive_access_log"
        db_table_comment = "明文身份证访问审计【append-only,单笔限流,响应不落日志】"
        indexes = [
            models.Index(fields=["target_person", "created_at"], name="idx_sal_person"),
            models.Index(fields=["accessed_by", "created_at"], name="idx_sal_actor"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.accessed_by}->{self.target_person_id}:{self.scene}"
