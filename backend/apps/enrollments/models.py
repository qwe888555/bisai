"""报名域数据模型（成员3）。

7 张表：enrollment / roster_item / enrollment_attachment / enrollment_snapshot
/ review_record / import_batch / import_error。

约定：
- 主键为不透明字符串 ID（enr_/rst_/att_/snp_/rev_/imb_/ime_），最长 64；
- 继承 accounts.VersionedModel（version + created_at/updated_at/deleted_at +
  objects/all_objects 双 manager）；
- 枚举用 CharField + choices，保证 MySQL / SQLite 通用；
- Snapshot 不可变，只新增不更新。
"""

from __future__ import annotations

from django.db import models

from apps.accounts.models import VersionedModel

# ---------------------------------------------------------------- 枚举

# 2026-10-28 待决 11 方案二 2a：报送主体收敛 SCHOOL_TEAM/CITY（无高校主体）；
# 历史迁移中的 UNIVERSITY choices 不回改，由 state-only 迁移同步模型状态。
# 历史数据按 UNIVERSITY 的读侧筛选兼容见 api._HISTORICAL_SUBJECT_TYPES。
SUBJECT_TYPES = (
    ("SCHOOL_TEAM", "中小学球队"),
    ("CITY", "市州代报"),
)

STATUS_DRAFT = "DRAFT"
STATUS_PENDING_CITY = "PENDING_CITY_REVIEW"
STATUS_PENDING_PROVINCE = "PENDING_PROVINCE_REVIEW"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_WITHDRAWN = "WITHDRAWN"

ENROLLMENT_STATUSES = (
    (STATUS_DRAFT, "草稿"),
    (STATUS_PENDING_CITY, "待市州审核"),
    (STATUS_PENDING_PROVINCE, "待省级审核"),
    (STATUS_APPROVED, "已通过"),
    (STATUS_REJECTED, "已驳回"),
    (STATUS_WITHDRAWN, "已撤回"),
)

EDITABLE_STATUSES = frozenset({STATUS_DRAFT, STATUS_REJECTED, STATUS_WITHDRAWN})
PENDING_STATUSES = frozenset({STATUS_PENDING_CITY, STATUS_PENDING_PROVINCE})
ACTIVE_STATUSES = frozenset(
    {STATUS_DRAFT, STATUS_PENDING_CITY, STATUS_PENDING_PROVINCE, STATUS_APPROVED}
)

MATERIAL_TYPES = (
    ("STAMPED_FORM", "球队报名表与名单（盖章）"),
    ("STUDENT_STATUS", "学籍证明"),
)

QUALIFICATION_QUALIFIED = "QUALIFIED"
QUALIFICATION_PENDING = "PENDING_VERIFICATION"
QUALIFICATION_DISQUALIFIED = "DISQUALIFIED"
QUALIFICATION_EXCLUDED = "EXCLUDED"

QUALIFICATION_STATUSES = (
    (QUALIFICATION_QUALIFIED, "资格有效"),
    (QUALIFICATION_PENDING, "待核实"),
    (QUALIFICATION_DISQUALIFIED, "资格不符"),
    (QUALIFICATION_EXCLUDED, "已剔除"),
)

# E-2 资格操作（v7）：CONFIRM 确认 / REJECT 驳回 / EXCLUDE 剔除（不可逆）
# / REVOKE 撤销凭证（状态不变）。
QUALIFICATION_ACTIONS = (
    ("CONFIRM", "确认资格"),
    ("REJECT", "驳回资格"),
    ("EXCLUDE", "剔除名单"),
    ("REVOKE", "撤销资格凭证"),
)

REVIEW_STAGES = (
    ("CITY", "市州审核"),
    ("PROVINCE", "省级审核"),
    ("SUBMISSION", "提交"),
)

REVIEW_ACTIONS = (
    ("SUBMIT", "提交"),
    ("APPROVE", "通过"),
    ("REJECT", "驳回"),
    ("RETURN_FOR_CORRECTION", "退回补正"),
    ("WITHDRAW", "撤回"),
)

IMPORT_MODES = (
    ("VALIDATE_ONLY", "仅校验"),
    ("APPLY_VALID_ROWS", "写入合法行"),
    ("PHOTO_IMPORT", "照片导入"),
)


class Enrollment(VersionedModel):
    """一支真实球队在一个赛事组别的一次报名业务记录。"""

    id = models.CharField(primary_key=True, max_length=64)
    event_id = models.CharField(max_length=64, db_index=True)
    group_id = models.CharField(max_length=64, db_index=True)
    team_id = models.CharField(max_length=64, db_index=True)
    subject_type = models.CharField(max_length=20, choices=SUBJECT_TYPES)
    status = models.CharField(
        max_length=30, choices=ENROLLMENT_STATUSES, default=STATUS_DRAFT
    )
    submission_no = models.PositiveIntegerField(default=0)
    contact_name = models.CharField(max_length=100)
    contact_phone_cipher = models.TextField(blank=True, default="")
    contact_phone_masked = models.CharField(max_length=30, blank=True, default="")
    city_org_id = models.CharField(max_length=64, blank=True, default="")
    remarks = models.TextField(max_length=2000, blank=True, default="")
    created_by = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "enrollments_enrollment"
        verbose_name = "报名单"
        verbose_name_plural = "报名单"
        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(fields=["event_id", "status"]),
            models.Index(fields=["team_id", "group_id"]),
            models.Index(fields=["city_org_id"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}({self.status})"

    @property
    def editable(self) -> bool:
        return self.status in EDITABLE_STATUSES


class RosterItem(VersionedModel):
    """报名名单条目（运动员）。"""

    id = models.CharField(primary_key=True, max_length=64)
    enrollment = models.ForeignKey(
        Enrollment,
        on_delete=models.CASCADE,
        related_name="players",
        db_column="enrollment_id",
    )
    athlete_id = models.CharField(max_length=64, db_index=True)
    athlete_name = models.CharField(max_length=100, blank=True, default="")
    masked_id_number = models.CharField(max_length=30, blank=True, default="")
    jersey_number = models.PositiveIntegerField(null=True, blank=True)
    position = models.CharField(max_length=50, blank=True, default="")
    photo_file_id = models.CharField(max_length=64, blank=True, default="")
    student_status_file_ids = models.JSONField(default=list, blank=True)
    # TODO(确认项#3): 契约 RosterItem 无 qualification_status，此处按双轨规则扩展；
    # 对外 API 暂不输出该字段，待成员1扩展契约。
    qualification_status = models.CharField(
        max_length=30,
        choices=QUALIFICATION_STATUSES,
        default=QUALIFICATION_QUALIFIED,
    )
    qualification_reason = models.CharField(
        max_length=500, null=True, blank=True, default=None
    )
    # E-2 资格确认凭证（v7）：QUALIFIED 状态本身不代表已确认，终审须
    # confirmed_at 与 confirmed_by 同时有效；REVOKE 清凭证不改状态值。
    confirmed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmed_by = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "enrollments_roster_item"
        verbose_name = "报名名单条目"
        verbose_name_plural = "报名名单条目"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["enrollment", "athlete_id"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}({self.athlete_id})"


class RosterQualificationLog(VersionedModel):
    """E-2 资格操作追加式审计日志（独立表，不混入 ReviewRecord）。

    v7 设计：roster_item 删除（软删/级联）置 NULL 后，凭
    roster_item_id_snapshot + athlete_id 保留人员维度历史追溯；
    append-only：业务代码只 create，不 update/delete。
    """

    id = models.CharField(primary_key=True, max_length=64)
    roster_item = models.ForeignKey(
        RosterItem,
        on_delete=models.SET_NULL,
        related_name="qualification_logs",
        db_column="roster_item_id",
        null=True,
        blank=True,
    )
    roster_item_id_snapshot = models.CharField(max_length=64, db_index=True)
    athlete_id = models.CharField(max_length=64, blank=True, default="")
    action = models.CharField(max_length=20, choices=QUALIFICATION_ACTIONS)
    reason = models.TextField(blank=True, default="")
    operator_account_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "enrollments_roster_qualification_log"
        verbose_name = "名单资格操作日志"
        verbose_name_plural = "名单资格操作日志"
        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(fields=["roster_item", "-created_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}({self.action}/{self.roster_item_id_snapshot})"


class EnrollmentAttachment(VersionedModel):
    """报名材料与文件的关联（只存关联，不存文件本体）。"""

    id = models.CharField(primary_key=True, max_length=64)
    enrollment = models.ForeignKey(
        Enrollment,
        on_delete=models.CASCADE,
        related_name="attachments",
        db_column="enrollment_id",
    )
    material_type = models.CharField(max_length=30, choices=MATERIAL_TYPES)
    file_id = models.CharField(max_length=64)
    athlete_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "enrollments_enrollment_attachment"
        verbose_name = "报名材料关联"
        verbose_name_plural = "报名材料关联"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["enrollment", "material_type"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}({self.material_type})"


class EnrollmentSnapshot(VersionedModel):
    """提交快照：不可变，payload 冻结提交时的报名全量内容。"""

    id = models.CharField(primary_key=True, max_length=64)
    enrollment = models.ForeignKey(
        Enrollment,
        on_delete=models.CASCADE,
        related_name="snapshots",
        db_column="enrollment_id",
    )
    submission_no = models.PositiveIntegerField()
    submitted_at = models.DateTimeField()
    payload = models.JSONField(default=dict)

    class Meta:
        db_table = "enrollments_enrollment_snapshot"
        verbose_name = "报名提交快照"
        verbose_name_plural = "报名提交快照"
        ordering = ["-submission_no", "-created_at"]
        unique_together = ("enrollment", "submission_no")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}(#{self.submission_no})"


class ReviewRecord(VersionedModel):
    """报名审核/提交/撤回全历史；绑定 submissionNo + snapshotId。"""

    id = models.CharField(primary_key=True, max_length=64)
    enrollment = models.ForeignKey(
        Enrollment,
        on_delete=models.CASCADE,
        related_name="reviews",
        db_column="enrollment_id",
    )
    submission_no = models.PositiveIntegerField(default=0)
    stage = models.CharField(max_length=20, choices=REVIEW_STAGES)
    action = models.CharField(max_length=30, choices=REVIEW_ACTIONS)
    from_status = models.CharField(max_length=30, blank=True, default="")
    to_status = models.CharField(max_length=30, blank=True, default="")
    reason = models.CharField(max_length=500, blank=True, default="")
    operator_account_id = models.CharField(max_length=64, blank=True, default="")
    snapshot_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "enrollments_review_record"
        verbose_name = "报名审核记录"
        verbose_name_plural = "报名审核记录"
        ordering = ["-created_at", "id"]
        indexes = [models.Index(fields=["enrollment", "-created_at"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}({self.stage}/{self.action})"


class ImportBatch(VersionedModel):
    """一次同步导入的批次统计。"""

    id = models.CharField(primary_key=True, max_length=64)
    enrollment = models.ForeignKey(
        Enrollment,
        on_delete=models.CASCADE,
        related_name="import_batches",
        db_column="enrollment_id",
    )
    mode = models.CharField(max_length=30, choices=IMPORT_MODES)
    total = models.PositiveIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    failure_count = models.PositiveIntegerField(default=0)
    created_by = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "enrollments_import_batch"
        verbose_name = "导入批次"
        verbose_name_plural = "导入批次"
        ordering = ["-created_at", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}({self.mode})"


class ImportError(VersionedModel):
    """导入逐行错误（文件名/信息已脱敏）。"""

    id = models.CharField(primary_key=True, max_length=64)
    batch = models.ForeignKey(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="errors",
        db_column="batch_id",
    )
    row_number = models.PositiveIntegerField(null=True, blank=True)
    field = models.CharField(max_length=100, blank=True, default="")
    code = models.CharField(max_length=80)
    message = models.CharField(max_length=500)
    file_name = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "enrollments_import_error"
        verbose_name = "导入错误"
        verbose_name_plural = "导入错误"
        ordering = ["row_number", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}({self.code})"
