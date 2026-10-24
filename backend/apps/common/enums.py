"""全系统统一状态枚举（唯一出处，总体设计 §4.0/§十一）。

仅收录 DDL 注释中明确枚举的状态类字段；字典驱动的可扩展枚举
（school_type/category_code/grade_level/position_code/coach_level/
guardian_relation 等）走 base_dict_item，不在此硬编码，
code 合法性由服务层校验（DDL 审核报告 F7）。
"""

from __future__ import annotations

from django.db import models


class AuditStatus(models.TextChoices):
    """审核状态（person / team / person_material / team_material 共用）。"""

    DRAFT = "DRAFT", "草稿"
    PENDING = "PENDING", "待审核"
    APPROVED = "APPROVED", "已通过"
    REJECTED = "REJECTED", "已驳回"


class EnableStatus(models.TextChoices):
    """启用/停用状态（person / team 共用）。"""

    ACTIVE = "ACTIVE", "启用"
    DISABLED = "DISABLED", "停用"


class Gender(models.IntegerChoices):
    UNKNOWN = 0, "未知"
    MALE = 1, "男"
    FEMALE = 2, "女"


class PersonCategory(models.TextChoices):
    ATHLETE = "ATHLETE", "运动员"
    COACH = "COACH", "教练员"


class RegionLevel(models.IntegerChoices):
    PROVINCE = 1, "省"
    CITY = 2, "市州"
    COUNTY = 3, "区县"


class TransferStatus(models.TextChoices):
    """转队单据状态（R5：条件更新流转，服务层强制）。"""

    PENDING = "PENDING", "待核验"
    COMPLETED = "COMPLETED", "已完成"
    CANCELLED = "CANCELLED", "已取消"


class RelationStatus(models.TextChoices):
    """关系状态（team_member / team_coach 共用，单向流转，历史禁改）。"""

    CURRENT = "CURRENT", "现役"
    EXITED = "EXITED", "已离队"


class RelationSource(models.TextChoices):
    """关系来源（team_member：DIRECT/TRANSFER；team_coach 仅 DIRECT，F1 方案B）。"""

    DIRECT = "DIRECT", "直接添加"
    TRANSFER = "TRANSFER", "转队产生"


class CoachDuty(models.TextChoices):
    HEAD_COACH = "HEAD_COACH", "主教练"
    ASSISTANT_COACH = "ASSISTANT_COACH", "助理教练"
    GOALKEEPER_COACH = "GOALKEEPER_COACH", "守门员教练"


class ConfirmStatus(models.TextChoices):
    """合并签字确认状态（映射接口仅 CONFIRMED 后生效）。"""

    PENDING_CONFIRM = "PENDING_CONFIRM", "待签字确认"
    CONFIRMED = "CONFIRMED", "已确认"
    CANCELLED = "CANCELLED", "已取消"


class MaterialStatus(models.TextChoices):
    """材料状态（person_material / team_material 共用）。"""

    DRAFT = "DRAFT", "草稿"
    PENDING = "PENDING", "待审核"
    APPROVED = "APPROVED", "已通过"
    REJECTED = "REJECTED", "已驳回"
    WITHDRAWN = "WITHDRAWN", "已撤回"


class MaterialAppliesTo(models.TextChoices):
    """材料归属（material_type.applies_to 与多态 material_kind 共用值域）。"""

    PERSON = "PERSON", "人员材料"
    TEAM = "TEAM", "球队材料"


class ChangeType(models.TextChoices):
    """人员变更类型（person_change_log，append-only）。"""

    CREATE = "CREATE", "创建"
    UPDATE = "UPDATE", "更新"
    AUDIT = "AUDIT", "审核"
    MERGE = "MERGE", "合并"
    DISABLE = "DISABLE", "停用"
    ENABLE = "ENABLE", "启用"


class ImportType(models.TextChoices):
    REGION = "REGION", "区划导入"
    SCHOOL = "SCHOOL", "学校导入"
    DICT = "DICT", "字典导入"
    MATERIAL_TYPE = "MATERIAL_TYPE", "材料类型导入"
    PERSON = "PERSON", "人员导入"
    TEAM = "TEAM", "球队导入"


class ImportStatus(models.TextChoices):
    PROCESSING = "PROCESSING", "处理中"
    COMPLETED = "COMPLETED", "全部成功"
    PARTIAL = "PARTIAL", "部分成功"
    FAILED = "FAILED", "全部失败"


class AccessScene(models.TextChoices):
    """敏感访问场景（sensitive_access_log）。"""

    IDCARD_VIEW = "IDCARD_VIEW", "明文身份证查看"
    PDF_EXPORT = "PDF_EXPORT", "PDF导出"
    VERIFICATION = "VERIFICATION", "核验场景"
