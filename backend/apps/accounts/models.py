"""accounts 数据模型。

约定：
- 主键一律为不透明字符串 ID（契约 2：最长 64，不拿身份证当主键）；
- 所有可编辑对象带 version 字段支持乐观锁；
- 删除为软删除（deleted_at），默认 manager 过滤掉已删对象。
"""

from __future__ import annotations

from django.db import models


class ActiveManager(models.Manager):
    """默认只看未软删对象；需要历史数据时用 all_objects。"""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class VersionedModel(models.Model):
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True


class Organization(VersionedModel):
    """组织树：省 → 市州 → 学校/高校 → 球队。"""

    ORG_TYPES = (
        ("PROVINCE", "省"),
        ("CITY", "市州"),
        ("SCHOOL", "学校"),
        ("UNIVERSITY", "高校"),
        ("TEAM", "球队"),
    )

    id = models.CharField(primary_key=True, max_length=64)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        db_column="parent_id",
    )
    name = models.CharField(max_length=100)
    org_type = models.CharField(max_length=20, choices=ORG_TYPES)
    code = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "accounts_organization"
        indexes = [models.Index(fields=["org_type"]), models.Index(fields=["parent"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name}({self.code})"


class Account(VersionedModel):
    """登录账号；与人员主档(person_id)解耦，不装 django.contrib.auth。"""

    id = models.CharField(primary_key=True, max_length=64)
    username = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=100, blank=True, default="")
    roles = models.JSONField(default=list)
    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="accounts",
        db_column="org_id",
    )
    team_ids = models.JSONField(default=list, blank=True)
    person_id = models.CharField(max_length=64, blank=True, default="")
    enabled = models.BooleanField(default=True)
    password = models.CharField(max_length=256)
    must_change_password = models.BooleanField(default=False)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_account"
        indexes = [models.Index(fields=["organization"]), models.Index(fields=["enabled"])]

    def __str__(self) -> str:  # pragma: no cover
        return self.username


class DictionaryItem(VersionedModel):
    """普通业务字典；内置角色/状态枚举编码不允许通过字典接口改名。"""

    id = models.CharField(primary_key=True, max_length=64)
    dict_type = models.CharField(max_length=64)
    value = models.CharField(max_length=64)
    label = models.CharField(max_length=100)
    sort_order = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "accounts_dictionary_item"
        indexes = [models.Index(fields=["dict_type", "sort_order"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.dict_type}:{self.label}"


class AuditLog(models.Model):
    """审计日志：只写不改，由 AuditMiddleware 落库，客户端不能创建或删除。"""

    LOG_TYPES = (("OPERATION", "操作"), ("LOGIN", "登录"), ("ENROLLMENT", "报名"))
    OUTCOMES = (("SUCCESS", "成功"), ("FAILURE", "失败"))

    id = models.CharField(primary_key=True, max_length=64)
    log_type = models.CharField(max_length=20, choices=LOG_TYPES, default="OPERATION")
    operator_account_id = models.CharField(max_length=64, blank=True, default="")
    action = models.CharField(max_length=100, blank=True, default="")
    object_type = models.CharField(max_length=50, blank=True, default="")
    object_id = models.CharField(max_length=64, blank=True, default="")
    summary = models.CharField(max_length=500, blank=True, default="")
    outcome = models.CharField(max_length=20, choices=OUTCOMES, default="SUCCESS")
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_audit_log"
        indexes = [
            models.Index(fields=["log_type", "-created_at"]),
            models.Index(fields=["object_id"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.action}/{self.object_id}"


class RegistrationInvitation(models.Model):
    """一次性注册邀请凭证（契约 3：不凭证件知识认领已有档案）。

    只保存凭证哈希；明文 token 仅在签发响应里返回一次。
    """

    id = models.CharField(primary_key=True, max_length=64)
    token_hash = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=20)
    person_id = models.CharField(max_length=64, blank=True, default="")
    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invitations",
        db_column="org_id",
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_registration_invitation"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.kind}@{self.organization_id or '-'}"
