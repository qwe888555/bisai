"""events 域模型：赛事（Event）与赛事组别（EventGroup）。

约定（对齐 accounts 底座，本模块自带软删/版本基类，不跨 app 依赖）：
- 主键一律为不透明字符串 ID（最长 64），不自增整型；
- 可编辑对象带 version 支持乐观锁，删除为软删除（deleted_at），默认 manager 过滤已删；
- 对外响应由 schemas.py 的 mapper 转 camelCase，本模型字段保持 snake_case。
"""

from __future__ import annotations

from django.db import models


class ActiveManager(models.Manager):
    """默认只看未软删对象；需要历史数据时用 all_objects。"""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SoftDeleteVersionedModel(models.Model):
    """版本号 + 时间戳 + 软删除标记的抽象基类。"""

    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True


# ---------------------------------------------------------------- 枚举（与接口文档第 7 节逐字一致）

EVENT_STATUSES = (
    ("DRAFT", "草稿"),
    ("REGISTRATION_OPEN", "报名进行中"),
    ("REGISTRATION_CLOSED", "报名已截止"),
    ("RUNNING", "进行中"),
    ("FINISHED", "已结束"),
)

# 2026-10-28 待决 11 方案二 2a：本项目无"高校组"，组别收敛 PRIMARY/MIDDLE；
# 历史迁移中的 UNIVERSITY choices 不回改，由 state-only 迁移同步模型状态。
GROUP_CATEGORIES = (
    ("PRIMARY", "小学组"),
    ("MIDDLE", "初中组"),
)

GROUP_GENDERS = (
    ("MALE", "男子"),
    ("FEMALE", "女子"),
    ("MIXED", "混合"),
)

# 赛事层级标准枚举：取代自由文本 event_category，稳定区分省/市/校/杯赛。
# 历史自由文本经 0008 数据迁移归一化；未知值一律归入 OTHER。
EVENT_CATEGORIES = (
    ("PROVINCIAL_LEAGUE", "省级联赛"),
    ("CITY_LEAGUE", "市级联赛"),
    ("SCHOOL_LEAGUE", "校级联赛"),
    ("CHAMPIONSHIP", "锦标赛"),
    ("FRIENDLY", "友谊赛"),
    ("OTHER", "其他"),
)

_EVENT_CATEGORY_KEYS = {code for code, _ in EVENT_CATEGORIES}

# 历史自由文本 → 标准枚举映射（0008 迁移与写入归一化共用，保持单一口径）
EVENT_CATEGORY_ALIASES = {
    "PROVINCIAL": "PROVINCIAL_LEAGUE",
    "PROVINCIAL_LEAGUE": "PROVINCIAL_LEAGUE",
    "CITY": "CITY_LEAGUE",
    "CITY_LEAGUE": "CITY_LEAGUE",
    "CITY_TOURNAMENT": "CITY_LEAGUE",
    "SCHOOL": "SCHOOL_LEAGUE",
    "SCHOOL_LEAGUE": "SCHOOL_LEAGUE",
    "SCHOOL_GAMES": "SCHOOL_LEAGUE",
    "CHAMPIONSHIP": "CHAMPIONSHIP",
    "CHAMPIONSHIPS": "CHAMPIONSHIP",
    "TOURNAMENT": "CHAMPIONSHIP",
    "FRIENDLY": "FRIENDLY",
}


def normalize_event_category(value: str) -> str:
    """把任意赛事分类输入归一化为标准枚举编码；空值/未知值返回 OTHER。"""
    if not value:
        return ""
    key = str(value).strip().upper()
    if key in _EVENT_CATEGORY_KEYS:
        return key
    return EVENT_CATEGORY_ALIASES.get(key, "OTHER")


def event_category_label(value: str) -> str:
    """标准枚举编码 → 中文标签；未知/空值显示「未分级」，前端无需自行兜底。"""
    if not value:
        return "未分级"
    for code, label in EVENT_CATEGORIES:
        if code == value:
            return label
    return "未分级"


class Event(SoftDeleteVersionedModel):
    """赛事配置：赛季、报名时间窗与状态。"""

    id = models.CharField(primary_key=True, max_length=64)
    name = models.CharField(max_length=200)
    season = models.CharField(max_length=30)
    # 赛事层级标准枚举（见 EVENT_CATEGORIES），写入前经 normalize_event_category 归一化
    event_category = models.CharField(
        max_length=32, choices=EVENT_CATEGORIES, blank=True, default=""
    )
    registration_opens_at = models.DateTimeField()
    registration_closes_at = models.DateTimeField()
    status = models.CharField(
        max_length=20, choices=EVENT_STATUSES, default="DRAFT"
    )
    description = models.TextField(max_length=5000, blank=True, default="")

    class Meta:
        db_table = "events_event"
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["season"]),
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}({self.season})"


class EventGroup(SoftDeleteVersionedModel):
    """赛事组别：学段 + 性别 + 名额/年龄/上场人数限制。

    P0-3 D 批：资格规则结构化字段（min_players / min_goalkeepers /
    birth_date_from / birth_date_to）与独立的 rules_confirmed。
    rules_status 不持久化，由 derive_rules_status 按两态派生
    （UNCONFIGURED / CONFIGURED）；NOT_APPLICABLE 本期不实现。
    min_age / max_age 保留，不与出生日期边界自动换算。
    """

    id = models.CharField(primary_key=True, max_length=64)
    event = models.ForeignKey(
        Event,
        on_delete=models.PROTECT,
        related_name="groups",
        db_column="event_id",
    )
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=12, choices=GROUP_CATEGORIES)
    gender = models.CharField(max_length=10, choices=GROUP_GENDERS)
    # 名额：0 表示不设上限
    quota = models.PositiveIntegerField(default=0)
    min_age = models.PositiveSmallIntegerField(null=True, blank=True)
    max_age = models.PositiveSmallIntegerField(null=True, blank=True)
    max_players = models.PositiveSmallIntegerField()
    required_material_types = models.JSONField(default=list)
    # P0-3 资格规则结构化值列（可空；为空表示该项未配置）
    min_players = models.PositiveSmallIntegerField(
        null=True, blank=True, db_comment="名单人数下限"
    )
    min_goalkeepers = models.PositiveSmallIntegerField(
        null=True, blank=True, db_comment="守门员人数下限"
    )
    birth_date_from = models.DateField(
        null=True, blank=True, db_comment="出生日期下限(含)"
    )
    birth_date_to = models.DateField(
        null=True, blank=True, db_comment="出生日期上限(含)"
    )
    # 独立的规则确认标志，不与人员/报名/赛事审核状态混用
    rules_confirmed = models.BooleanField(
        default=False, db_comment="组别资格规则是否经业务确认"
    )

    class Meta:
        db_table = "events_event_group"
        indexes = [
            models.Index(fields=["event"]),
            models.Index(fields=["category"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}@{self.event_id}"


# ---------------------------------------------------------------- 资格规则状态派生

RULES_STATUS_UNCONFIGURED = "UNCONFIGURED"
RULES_STATUS_CONFIGURED = "CONFIGURED"


def derive_rules_status(
    *,
    min_players,
    max_players,
    rules_confirmed: bool,
) -> str:
    """按两态派生规则状态：UNCONFIGURED / CONFIGURED。

    - UNCONFIGURED：rules_confirmed=False，或 min_players / max_players 任一为空；
    - CONFIGURED：rules_confirmed=True 且 min_players、max_players 均非空。
    min_goalkeepers / birth_date_from / birth_date_to 为可选增强，不影响核心状态。
    NOT_APPLICABLE 本期不实现，不通过 NULL 推断。
    """
    if not rules_confirmed:
        return RULES_STATUS_UNCONFIGURED
    if min_players is None or max_players is None:
        return RULES_STATUS_UNCONFIGURED
    return RULES_STATUS_CONFIGURED

# ---------------------------------------------------------------- 比赛对阵与事件

MATCH_STATUSES = (
    ("SCHEDULED", "已排期"),
    ("LIVE", "进行中"),
    ("FINISHED", "已结束"),
    ("CANCELLED", "已取消"),
)

MATCH_EVENT_TYPES = (
    ("GOAL", "进球"),
    ("PENALTY", "判罚"),
    ("SUBSTITUTION", "换人"),
)

PENALTY_TYPES = (
    ("YELLOW_CARD", "黄牌"),
    ("RED_CARD", "红牌"),
    ("PENALTY_KICK", "点球"),
    ("OTHER", "其他"),
)


class Match(SoftDeleteVersionedModel):
    """比赛对阵与裁判分配；赛果直接写在 Match.score，不另建 Result 版本。"""

    id = models.CharField(primary_key=True, max_length=64)
    event_id = models.CharField(max_length=64, db_index=True)
    group_id = models.CharField(max_length=64, db_index=True)
    home_team_id = models.CharField(max_length=64)
    away_team_id = models.CharField(max_length=64)
    kickoff_at = models.DateTimeField()
    venue = models.CharField(max_length=200, blank=True, default="")
    stage = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(
        max_length=12, choices=MATCH_STATUSES, default="SCHEDULED"
    )
    referees = models.JSONField(default=list)
    home_score = models.PositiveIntegerField(null=True, blank=True)
    away_score = models.PositiveIntegerField(null=True, blank=True)
    published = models.BooleanField(default=False)
    result_published = models.BooleanField(default=False)
    score_revision = models.PositiveIntegerField(default=0)
    # 图文直播短轮询父资源版本：每次新增/修改/删除直播帖都 +1
    feed_revision = models.PositiveIntegerField(default=0)
    # 5.5 赛果：NORMAL 比分由有效进球事件派生；FORFEIT/ADMIN_CORRECTION 用人工分与原因
    result_type = models.CharField(max_length=18, default="NORMAL")
    manual_home_score = models.PositiveIntegerField(null=True, blank=True)
    manual_away_score = models.PositiveIntegerField(null=True, blank=True)
    result_reason = models.CharField(max_length=1000, blank=True, default="")

    class Meta:
        db_table = "events_match"
        indexes = [
            models.Index(fields=["event_id"]),
            models.Index(fields=["group_id"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"Match@{self.event_id}"


class MatchEvent(SoftDeleteVersionedModel):
    """比赛事件流：进球 / 判罚 / 换人；match_version 记录录入时的比赛版本快照。"""

    id = models.CharField(primary_key=True, max_length=64)
    match_id = models.CharField(max_length=64, db_index=True)
    event_type = models.CharField(max_length=12, choices=MATCH_EVENT_TYPES)
    minute = models.PositiveSmallIntegerField()
    stoppage_minute = models.PositiveSmallIntegerField(null=True, blank=True)
    team_id = models.CharField(max_length=64)
    # GOAL 专用
    scorer_athlete_id = models.CharField(max_length=64, blank=True, default="")
    own_goal = models.BooleanField(default=False)
    # PENALTY 专用
    athlete_id = models.CharField(max_length=64, blank=True, default="")
    penalty_type = models.CharField(
        max_length=14, choices=PENALTY_TYPES, null=True, blank=True
    )
    reason = models.CharField(max_length=500, blank=True, default="")
    # SUBSTITUTION 专用
    out_athlete_id = models.CharField(max_length=64, blank=True, default="")
    in_athlete_id = models.CharField(max_length=64, blank=True, default="")
    note = models.CharField(max_length=1000, blank=True, default="")
    match_version = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "events_match_event"
        indexes = [models.Index(fields=["match_id"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.event_type}@{self.match_id}"


# ---------------------------------------------------------------- 门户内容与资源

CONTENT_TYPES = (
    ("NEWS", "新闻"),
    ("POLICY", "政策"),
    ("HIGHLIGHT", "精彩瞬间"),
    ("BEST_LINEUP", "最佳阵容"),
)

CONTENT_STATUSES = (
    ("DRAFT", "草稿"),
    ("PUBLISHED", "已发布"),
    ("OFFLINE", "已下线"),
)


class Content(SoftDeleteVersionedModel):
    """新闻/政策/精彩瞬间/最佳阵容；revision 为正文修订版本，问答出处需携带。"""

    id = models.CharField(primary_key=True, max_length=64)
    content_type = models.CharField(max_length=14, choices=CONTENT_TYPES)
    title = models.CharField(max_length=200)
    summary = models.CharField(max_length=1000, blank=True, default="")
    body_html = models.TextField(max_length=50000)
    cover_file_id = models.CharField(max_length=64, blank=True, default="")
    event_id = models.CharField(max_length=64, blank=True, default="")
    group_id = models.CharField(max_length=64, blank=True, default="")
    policy_document_no = models.CharField(max_length=100, blank=True, default="")
    policy_effective_from = models.DateField(null=True, blank=True)
    policy_effective_to = models.DateField(null=True, blank=True)
    pinned = models.BooleanField(default=False)
    status = models.CharField(
        max_length=12, choices=CONTENT_STATUSES, default="DRAFT"
    )
    published_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    body_text = models.TextField(max_length=50000, blank=True, default="")
    # 由「门户资源·新闻配图」自动创建：标题/摘要/正文跟随配图同步，删除配图时一并下线。
    auto_created = models.BooleanField(default=False)
    # 自动赛果快讯溯源：publish_match_result 生成的快讯记录来源比赛，幂等 upsert/下线。
    source_match_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "events_content"
        indexes = [
            models.Index(fields=["content_type"]),
            models.Index(fields=["status"]),
            models.Index(fields=["-published_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.title


PORTAL_RESOURCE_TYPES = (
    ("CAROUSEL", "轮播"),
    ("NEWS_LINK_IMAGE", "新闻链接图"),
    ("PARTNER", "合作方"),
)


class PortalResource(SoftDeleteVersionedModel):
    """门户轮播 / 新闻链接图 / 合作方；targetContentId 与 targetUrl 至多一个。"""

    id = models.CharField(primary_key=True, max_length=64)
    resource_type = models.CharField(max_length=18, choices=PORTAL_RESOURCE_TYPES)
    title = models.CharField(max_length=150)
    # 文字介绍：合作关系、配图说明等，门户按需展示。
    description = models.CharField(max_length=300, blank=True, default="")
    image_file_id = models.CharField(max_length=64, blank=True, default="")
    # 轮播视频：仅 CAROUSEL 使用，直链 mp4/webm，首屏无直播时代替图片播放。
    video_url = models.CharField(max_length=2000, blank=True, default="")
    target_content_id = models.CharField(max_length=64, blank=True, default="")
    target_url = models.CharField(max_length=2000, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "events_portal_resource"
        indexes = [models.Index(fields=["resource_type"]), models.Index(fields=["sort_order"])]

    def __str__(self) -> str:  # pragma: no cover
        return self.title

# ---------------------------------------------------------------- 赛事文件与图文直播（5.3 / 5.4）

DOCUMENT_TYPES = (
    ("SIGN_IN", "签到表"),
    ("SIGNED_RECORD", "记录集签字确认表"),
    ("REFEREE_REPORT", "裁判员报告书"),
)


class MatchDocument(SoftDeleteVersionedModel):
    """比赛归档文件：签到 / 签字记录 / 裁判报告；fileId 指向成员2文件服务。"""

    id = models.CharField(primary_key=True, max_length=64)
    match_id = models.CharField(max_length=64, db_index=True)
    document_type = models.CharField(max_length=16, choices=DOCUMENT_TYPES)
    file_id = models.CharField(max_length=64)

    class Meta:
        db_table = "events_match_document"
        indexes = [
            models.Index(fields=["match_id"]),
            models.Index(fields=["document_type"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.document_type}@{self.match_id}"


class LivePost(SoftDeleteVersionedModel):
    """图文直播条目；cursor 为写入时父资源 feedRevision 快照，供短轮询增量。"""

    id = models.CharField(primary_key=True, max_length=64)
    match_id = models.CharField(max_length=64, db_index=True)
    text = models.CharField(max_length=3000)
    image_file_ids = models.JSONField(default=list)
    minute = models.PositiveSmallIntegerField(null=True, blank=True)
    cursor = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "events_live_post"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["match_id"]), models.Index(fields=["cursor"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"LivePost@{self.match_id}"


# ---------------------------------------------------------------- 官方名次与奖项（P1-3，2026-11-02）

# 与小组赛积分榜（standings，按已发布赛果实时推算）分工：本表承载赛事
# 结束后由组委会录定的官方最终名次与奖项，人工确认，不从比赛自动推导。
RANK_MEDALS = (
    ("GOLD", "金牌"),
    ("SILVER", "银牌"),
    ("BRONZE", "铜牌"),
)

RANK_SOURCES = (
    ("AUTO", "按积分榜推导"),
    ("MANUAL", "人工录定"),
)

AWARD_TYPES = (
    ("TOP_SCORER", "最佳射手"),
    ("MVP", "最有价值球员"),
    ("BEST_GOALKEEPER", "最佳门将"),
    ("BEST_DEFENDER", "最佳后卫"),
    ("FAIR_PLAY", "公平竞赛奖"),
    ("OTHER", "其他奖项"),
)


class EventRanking(SoftDeleteVersionedModel):
    """赛事官方最终名次：一支球队在赛事（可细分组别）中的官方名次。

    唯一性（同赛事同组别同球队仅一行）由服务层校验，不用条件唯一索引
    —— MySQL 不支持部分索引，软删行也不能占用唯一键。
    """

    id = models.CharField(primary_key=True, max_length=64)
    event_id = models.CharField(max_length=64, db_index=True)
    # 空串表示赛事整体名次（不按组别拆分）
    group_id = models.CharField(max_length=64, blank=True, default="")
    team_id = models.CharField(max_length=64, db_index=True)
    # 官方名次从 1 开始；允许并列（同名次多行），故不做唯一约束
    final_rank = models.PositiveIntegerField()
    medal = models.CharField(max_length=10, choices=RANK_MEDALS, blank=True, default="")
    source = models.CharField(
        max_length=10, choices=RANK_SOURCES, default="MANUAL"
    )
    published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    remark = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        db_table = "events_event_ranking"
        indexes = [
            models.Index(fields=["event_id", "group_id"]),
            models.Index(fields=["published"]),
        ]
        ordering = ["final_rank", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return f"#{self.final_rank}@{self.event_id}"


class EventAward(SoftDeleteVersionedModel):
    """赛事官方奖项：个人或球队获奖记录，published 控制门户可见。"""

    id = models.CharField(primary_key=True, max_length=64)
    event_id = models.CharField(max_length=64, db_index=True)
    group_id = models.CharField(max_length=64, blank=True, default="")
    award_type = models.CharField(max_length=24, choices=AWARD_TYPES)
    # 个人奖填运动员、球队奖（如公平竞赛）填球队；OTHER 奖项两者可同时为空
    athlete_id = models.CharField(max_length=64, blank=True, default="")
    team_id = models.CharField(max_length=64, blank=True, default="")
    # 量化值展示：如最佳射手进球数「12球」，纯文本不参与计算
    value = models.CharField(max_length=100, blank=True, default="")
    published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    remark = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        db_table = "events_event_award"
        indexes = [
            models.Index(fields=["event_id", "group_id"]),
            models.Index(fields=["award_type"]),
            models.Index(fields=["published"]),
        ]
        ordering = ["award_type", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.award_type}@{self.event_id}"


class LiveLink(SoftDeleteVersionedModel):
    """外部直播链接（视频流/直播平台），enabled 控制是否展示。"""

    id = models.CharField(primary_key=True, max_length=64)
    match_id = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=150)
    url = models.CharField(max_length=2000)
    enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "events_live_link"
        indexes = [models.Index(fields=["match_id"])]

    def __str__(self) -> str:  # pragma: no cover
        return self.title