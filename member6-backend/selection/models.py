"""Data model for the selection-scoring module (module 6.2).

Table names use the ``sel_`` prefix so the app can be merged into the main
project without clashing with existing football tables.
"""

from django.db import models
from django.utils import timezone

from .enums import GroupType, PublicityStatus, PublicityType, RoundType


class ScoreTemplate(models.Model):
    """A scoring template for one group and one round."""

    name = models.CharField("模板名称", max_length=100)
    group_type = models.CharField(
        "组别", max_length=16, choices=GroupType.CHOICES
    )
    score_round = models.CharField(
        "轮次", max_length=16, choices=RoundType.CHOICES
    )
    status = models.SmallIntegerField(
        "状态", default=1, help_text="1=启用，0=停用"
    )
    created_by = models.CharField(
        "创建人ID", max_length=64, blank=True, default=""
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "sel_score_template"
        verbose_name = "评分模板"
        verbose_name_plural = verbose_name
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["group_type", "score_round", "status"],
                name="idx_sel_tpl_group_round",
            )
        ]

    def __str__(self):
        return f"{self.name}({self.group_type}/{self.score_round})"


class TemplateItem(models.Model):
    """One weighted dimension inside a scoring template."""

    template = models.ForeignKey(
        ScoreTemplate,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="模板",
    )
    dimension_code = models.CharField("维度编码", max_length=32)
    dimension_name = models.CharField("维度名称", max_length=64)
    weight = models.DecimalField(
        "权重", max_digits=6, decimal_places=2, help_text="所有维度权重之和为100"
    )
    max_score = models.DecimalField(
        "评分上限", max_digits=6, decimal_places=2, default=100
    )
    sort_no = models.IntegerField("排序号", default=0)

    class Meta:
        db_table = "sel_template_item"
        verbose_name = "模板维度"
        verbose_name_plural = verbose_name
        ordering = ["sort_no", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "dimension_code"],
                name="uq_sel_template_dimension",
            )
        ]

    def __str__(self):
        return f"{self.template_id}:{self.dimension_code}"


class Score(models.Model):
    """A candidate's submitted score for one event + round.

    ``version`` implements optimistic locking for score edits.  The unique
    key (event_id, score_round, candidate_id) makes the initial and final
    rounds independent while preventing duplicate submissions in one round.
    """

    event_id = models.CharField("赛事ID", max_length=64)
    group_type = models.CharField(
        "组别", max_length=16, choices=GroupType.CHOICES
    )
    score_round = models.CharField(
        "轮次", max_length=16, choices=RoundType.CHOICES
    )
    template = models.ForeignKey(
        ScoreTemplate,
        on_delete=models.PROTECT,
        related_name="scores",
        verbose_name="评分模板",
    )
    candidate_id = models.CharField("球员档案ID", max_length=64)
    candidate_name = models.CharField("球员姓名", max_length=64)
    team_name = models.CharField("所属球队", max_length=128, blank=True, default="")
    player_position = models.CharField(
        "场上位置", max_length=32, blank=True, default=""
    )
    total_score = models.DecimalField("总分", max_digits=6, decimal_places=2)
    version = models.IntegerField("版本号", default=0)
    created_by = models.CharField(
        "录入人ID", max_length=64, blank=True, default=""
    )
    created_at = models.DateTimeField("录入时间", auto_now_add=True)
    updated_by = models.CharField(
        "修改人ID", max_length=64, blank=True, default=""
    )
    updated_at = models.DateTimeField("修改时间", auto_now=True)

    class Meta:
        db_table = "sel_score"
        verbose_name = "遴选评分"
        verbose_name_plural = verbose_name
        ordering = ["-total_score", "candidate_id"]
        indexes = [
            models.Index(
                fields=["event_id", "group_type", "score_round"],
                name="idx_sel_score_event_round",
            ),
            models.Index(
                fields=["event_id", "candidate_id"],
                name="idx_sel_score_event_candidate",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["event_id", "score_round", "candidate_id"],
                name="uq_sel_score_event_round_candidate",
            )
        ]

    def __str__(self):
        return f"{self.candidate_name}({self.score_round})"


class ScoreItem(models.Model):
    """Snapshot of one dimension score.

    Dimension name/weight/max_score are copied from the template at entry
    time, so later template changes cannot alter historical scores.
    """

    score_record = models.ForeignKey(
        Score,
        on_delete=models.CASCADE,
        related_name="items",
        db_column="score_id",
        verbose_name="评分记录",
    )
    dimension_code = models.CharField("维度编码", max_length=32)
    dimension_name = models.CharField("维度名称", max_length=64)
    weight = models.DecimalField("权重", max_digits=6, decimal_places=2)
    max_score = models.DecimalField("评分上限", max_digits=6, decimal_places=2)
    score = models.DecimalField("维度得分", max_digits=6, decimal_places=2)
    sort_no = models.IntegerField("排序号", default=0)

    class Meta:
        db_table = "sel_score_item"
        verbose_name = "评分明细"
        verbose_name_plural = verbose_name
        ordering = ["sort_no", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["score_record", "dimension_code"],
                name="uq_sel_score_dimension",
            )
        ]

    def __str__(self):
        return f"{self.score_record_id}:{self.dimension_code}={self.score}"


class ResultBatch(models.Model):
    """One generated best-player selection result (versioned batch)."""

    event_id = models.CharField("赛事ID", max_length=64)
    group_type = models.CharField(
        "组别", max_length=16, choices=GroupType.CHOICES
    )
    result_round = models.CharField(
        "结果依据轮次", max_length=16, choices=RoundType.CHOICES
    )
    quota = models.IntegerField("遴选名额")
    initial_weight = models.DecimalField(
        "初选权重", max_digits=6, decimal_places=2
    )
    final_weight = models.DecimalField(
        "复选权重", max_digits=6, decimal_places=2
    )
    formula_snapshot = models.CharField("公式快照", max_length=255)
    version_no = models.IntegerField("结果版本号")
    active = models.BooleanField("是否当前结果", default=True)
    stale = models.BooleanField(
        "是否已过期", default=False, help_text="评分修改后自动置为已过期"
    )
    generated_by = models.CharField(
        "生成人ID", max_length=64, blank=True, default=""
    )
    generated_at = models.DateTimeField("生成时间", auto_now_add=True)

    class Meta:
        db_table = "sel_result_batch"
        verbose_name = "遴选结果批次"
        verbose_name_plural = verbose_name
        ordering = ["-generated_at", "-id"]
        indexes = [
            models.Index(
                fields=["event_id", "group_type", "active"],
                name="idx_sel_result_active",
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["event_id", "group_type", "version_no"],
                name="uq_sel_result_version",
            )
        ]

    def __str__(self):
        return f"{self.group_type}-v{self.version_no}"


class ResultItem(models.Model):
    """One ranked player inside a result batch, with traceable details."""

    batch = models.ForeignKey(
        ResultBatch,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="结果批次",
    )
    rank_no = models.IntegerField("排名")
    candidate_id = models.CharField("球员档案ID", max_length=64)
    candidate_name = models.CharField("球员姓名", max_length=64)
    team_name = models.CharField("所属球队", max_length=128, blank=True, default="")
    player_position = models.CharField(
        "场上位置", max_length=32, blank=True, default=""
    )
    initial_score = models.DecimalField(
        "初选总分", max_digits=6, decimal_places=2, null=True, blank=True
    )
    final_score = models.DecimalField(
        "复选总分", max_digits=6, decimal_places=2, null=True, blank=True
    )
    composite_score = models.DecimalField(
        "综合分", max_digits=6, decimal_places=2
    )
    detail_json = models.JSONField(
        "评分明细快照", default=dict, help_text="初选/复选各维度得分，支持追溯"
    )

    class Meta:
        db_table = "sel_result_item"
        verbose_name = "遴选结果明细"
        verbose_name_plural = verbose_name
        ordering = ["rank_no", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "candidate_id"],
                name="uq_sel_result_candidate",
            )
        ]

    def __str__(self):
        return f"#{self.rank_no} {self.candidate_name}"


class PublicityRecord(models.Model):
    """A public snapshot of one athlete, coach or team registration.

    Only fields listed in ``SELECTION_PUBLICITY_FIELD_WHITELIST`` are copied
    into ``profile_json``.  The raw ID-card number is never stored: it is
    transformed into ``id_card_masked`` while the snapshot is built.
    """

    event_id = models.CharField("赛事ID", max_length=64)
    group_type = models.CharField(
        "组别", max_length=16, choices=GroupType.CHOICES
    )
    city = models.CharField("市州", max_length=64)
    publicity_type = models.CharField(
        "公示类型", max_length=16, choices=PublicityType.CHOICES
    )
    source_id = models.CharField(
        "报名档案ID", max_length=64, help_text="成员4报名/档案记录ID"
    )
    subject_name = models.CharField("公示对象名称", max_length=64)
    team_name = models.CharField("所属球队", max_length=128, blank=True, default="")
    school_name = models.CharField("学校", max_length=128, blank=True, default="")
    id_card_masked = models.CharField(
        "脱敏身份证号", max_length=32, blank=True, default=""
    )
    profile_json = models.JSONField(
        "白名单字段快照",
        default=dict,
        help_text="只保存白名单内的字段，不保存身份证原文",
    )
    status = models.CharField(
        "公示状态",
        max_length=16,
        choices=PublicityStatus.CHOICES,
        default=PublicityStatus.PUBLISHED,
    )
    published_by = models.CharField(
        "公示操作人ID", max_length=64, blank=True, default=""
    )
    published_at = models.DateTimeField("公示时间", default=timezone.now)
    cancelled_by = models.CharField(
        "取消操作人ID", max_length=64, blank=True, default=""
    )
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
    cancel_reason = models.CharField(
        "取消原因", max_length=200, blank=True, default=""
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "sel_publicity"
        verbose_name = "报名信息公示"
        verbose_name_plural = verbose_name
        ordering = ["-published_at", "-id"]
        indexes = [
            models.Index(
                fields=[
                    "event_id",
                    "status",
                    "group_type",
                    "city",
                    "publicity_type",
                ],
                name="idx_sel_publicity_query",
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["event_id", "publicity_type", "source_id"],
                name="uq_sel_publicity_source",
            )
        ]

    def __str__(self):
        return f"{PublicityType.label(self.publicity_type)}:{self.subject_name}"
