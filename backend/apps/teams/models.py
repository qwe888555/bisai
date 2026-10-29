"""球队域模型：球队主表/转队单据/球员关系/教练关系/人员合并记录归属 persons。

依据：docs/design/成员4-DDL草案-V1.4.sql（V1.4-r1，冻结）。
- 关系表（team_member/team_coach）与转队单据禁止物理删除，
  无软删字段，历史关系单向流转（CURRENT→EXITED）；
- 6 条服务层唯一规则（R1-R5 涉及本 app）不建数据库唯一索引；
- 外键全部 PROTECT，列上 db_index=False，索引按冻结名在 Meta.indexes 保留。
"""

from __future__ import annotations

from django.db import models

from apps.common import enums
from apps.common.fields import TextField


class Team(models.Model):
    """球队主表（唯一主表，软删）。"""

    team_no = models.CharField(max_length=20, db_comment="球队编号 T+年份+8位id(应用层回填)")
    name = models.CharField(max_length=100, db_comment="球队名")
    category_code = models.CharField(max_length=20, db_comment="组别(字典team_category逻辑引用)")
    region = models.ForeignKey(
        "persons.Region",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="所属市州(逻辑隔离维度)",
    )
    school = models.ForeignKey(
        "persons.School",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="所属学校(创建时业务必填,服务层)",
    )
    logo_file_id = models.CharField(max_length=64, null=True, blank=True, db_comment="队徽(逻辑引用文件服务)")
    flag_file_id = models.CharField(max_length=64, null=True, blank=True, db_comment="队旗(逻辑引用)")
    group_photo_file_id = models.CharField(max_length=64, null=True, blank=True, db_comment="合照(逻辑引用)")
    introduction = TextField(null=True, blank=True, db_comment="球队简介")
    audit_status = models.CharField(
        max_length=20,
        default=enums.AuditStatus.APPROVED,
        choices=enums.AuditStatus.choices,
        db_comment="字段保留,审核流程不启用(Q2)",
    )
    status = models.CharField(
        max_length=20,
        default=enums.EnableStatus.ACTIVE,
        choices=enums.EnableStatus.choices,
        db_comment="ACTIVE/DISABLED",
    )
    import_batch = models.ForeignKey(
        "infra.ImportBatch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="导入批次(追溯)",
    )
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="创建人(逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    is_deleted = models.BooleanField(default=False, db_comment="软删标记")
    deleted_at = models.DateTimeField(null=True, blank=True, db_comment="软删时间")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "team"
        db_table_comment = "球队主表(唯一主表,软删)"
        constraints = [
            models.UniqueConstraint(fields=["team_no"], name="uk_team_no"),
        ]
        indexes = [
            models.Index(fields=["name"], name="idx_team_name"),
            models.Index(fields=["region", "category_code"], name="idx_team_region_cat"),
            models.Index(fields=["school"], name="idx_team_school"),
            models.Index(fields=["status", "is_deleted"], name="idx_team_status_del"),
            models.Index(fields=["import_batch"], name="idx_team_import"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.team_no}:{self.name}"


class TeamTransfer(models.Model):
    """转队单据（仅运动员，F1 方案B；登记制，核验后直办，禁止物理删除，纠错=反向单Q6）。"""

    person = models.ForeignKey(
        "persons.Person",
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="转队人员",
    )
    from_team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name="transfers_out",
        db_index=False,
        db_comment="原球队",
    )
    to_team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        related_name="transfers_in",
        db_index=False,
        db_comment="目标球队",
    )
    transfer_date = models.DateField(db_comment="转队日期")
    apply_reason = models.CharField(max_length=200, db_comment="转队原因")
    status = models.CharField(
        max_length=20,
        default=enums.TransferStatus.PENDING,
        choices=enums.TransferStatus.choices,
        db_comment="PENDING/COMPLETED/CANCELLED(条件更新流转,R5服务层规则)",
    )
    verified_by = models.BigIntegerField(null=True, blank=True, db_comment="核验人(赛事管理员,逻辑引用)")
    verified_at = models.DateTimeField(null=True, blank=True, db_comment="核验时间")
    completed_at = models.DateTimeField(null=True, blank=True, db_comment="完成时间")
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="创建人(逻辑引用)")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "team_transfer"
        db_table_comment = "转队单据(仅运动员,F1方案B;登记制,核验后直办,禁止物理删除,纠错=反向单Q6)"
        indexes = [
            models.Index(fields=["person", "status"], name="idx_tt_person_status"),
            models.Index(fields=["from_team"], name="idx_tt_from"),
            models.Index(fields=["to_team"], name="idx_tt_to"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.person_id}:{self.from_team_id}->{self.to_team_id}"


class TeamMember(models.Model):
    """球员-球队关系（含历史，N:M 中间表，禁止物理删除）。"""

    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="球队",
    )
    person = models.ForeignKey(
        "persons.Person",
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="球员(服务层校验为ATHLETE,R2现役唯一服务层强制)",
    )
    jersey_number = models.SmallIntegerField(
        null=True, blank=True, db_comment="球衣号(R3队内现役唯一,服务层强制)"
    )
    position_code = models.CharField(max_length=20, null=True, blank=True, db_comment="场上位置(字典逻辑引用)")
    status = models.CharField(
        max_length=20,
        default=enums.RelationStatus.CURRENT,
        choices=enums.RelationStatus.choices,
        db_comment="CURRENT/EXITED(单向流转,历史关系禁改)",
    )
    joined_at = models.DateField(db_comment="加入日期")
    left_at = models.DateTimeField(null=True, blank=True, db_comment="离队时间")
    left_reason = models.CharField(
        max_length=200, null=True, blank=True, default="", db_comment="离队原因"
    )
    source = models.CharField(
        max_length=20,
        default=enums.RelationSource.DIRECT,
        choices=enums.RelationSource.choices,
        db_comment="DIRECT/TRANSFER",
    )
    transfer = models.ForeignKey(
        TeamTransfer,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="转队单(转队产生时关联)",
    )
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="创建人(逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "team_member"
        db_table_comment = "球员-球队关系(含历史,N:M中间表,禁止物理删除)"
        indexes = [
            models.Index(fields=["person", "status"], name="idx_tm_person_status"),
            models.Index(fields=["team", "status"], name="idx_tm_team_status"),
            models.Index(fields=["team", "jersey_number"], name="idx_tm_team_jersey"),
            models.Index(fields=["transfer"], name="idx_tm_transfer"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.person_id}@{self.team_id}:{self.status}"


class TeamCoach(models.Model):
    """教练-球队关系（含历史，N:M 中间表，禁止物理删除）。

    F1 已决议（方案B）：不含 transfer_id，team_transfer 仅用于运动员转队；
    教练调岗历史由本表关系行 + audit_log（操作人/时间/行为）追溯。
    """

    team = models.ForeignKey(
        Team,
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="球队",
    )
    person = models.ForeignKey(
        "persons.Person",
        on_delete=models.PROTECT,
        db_index=False,
        db_comment="教练(服务层校验为COACH)",
    )
    duty = models.CharField(
        max_length=20,
        choices=enums.CoachDuty.choices,
        db_comment="D-CAT六值:HEAD_COACH/ASSISTANT_COACH/GOALKEEPER_COACH/FITNESS_COACH/MANAGER/OTHER(R4现役主教练唯一,服务层强制)",
    )
    duty_other = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        db_comment="OTHER补充说明(≤50字,仅duty=OTHER时填写且非OTHER必须为空,写入校验随教练任职服务落地)",
    )
    status = models.CharField(
        max_length=20,
        default=enums.RelationStatus.CURRENT,
        choices=enums.RelationStatus.choices,
        db_comment="CURRENT/EXITED(单向流转,历史禁改)",
    )
    joined_at = models.DateField(db_comment="加入日期")
    left_at = models.DateTimeField(null=True, blank=True, db_comment="离队时间")
    left_reason = models.CharField(
        max_length=200, null=True, blank=True, default="", db_comment="离队原因"
    )
    source = models.CharField(
        max_length=20,
        default=enums.RelationSource.DIRECT,
        choices=enums.RelationSource.choices,
        db_comment="来源(仅DIRECT;教练调岗经audit_log留痕,F1方案B)",
    )
    created_by = models.BigIntegerField(null=True, blank=True, db_comment="创建人(逻辑引用)")
    updated_by = models.BigIntegerField(null=True, blank=True, db_comment="更新人(逻辑引用)")
    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间(UTC)")
    updated_at = models.DateTimeField(auto_now=True, db_comment="更新时间(UTC)")

    class Meta:
        db_table = "team_coach"
        db_table_comment = "教练-球队关系(含历史,N:M中间表,禁止物理删除)"
        indexes = [
            models.Index(fields=["person", "status"], name="idx_tc_person_status"),
            models.Index(fields=["team", "status"], name="idx_tc_team_status"),
        ]

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.person_id}@{self.team_id}:{self.duty}"
