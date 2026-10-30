"""基础数据业务逻辑：区划 / 学校 / 字典类型 / 字典项（persons 域第一阶段）。

依据：docs/design/成员4-DDL草案-V1.4.sql（V1.4-r1，冻结）+ 成员4后端总体设计。
- Region / School / DictItem 只停用不删（is_active），任何接口不物理删除；
- DictType 无停用列：仅当无字典项引用时允许物理删除，否则 409 拒绝；
- 所有外键 PROTECT：父对象必须存在；停用父对象不级联，历史引用不受影响；
- 严格层级校验：省无父、市州父=省、区县父=市，父对象必须处于启用状态
  （因此父对象停用后其子对象不可再编辑，需先恢复父对象）；
- created_by / updated_by 为底座用户ID（BIGINT），当前 Account 主键为非数字
  字符串（acc_xxx），本阶段不写入（保持 NULL），操作人由 AuditLog 留痕；
- 表无 version 乐观锁列（冻结），版本号由 updated_at(UTC微秒) 派生（schemas.person_version），
  PUT/DELETE 统一 409 校验；写链禁止 queryset.update() 修改业务字段（绕过 auto_now）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from apps.common import crypto, enums
from apps.common.enums import RegionLevel
from apps.common.errors import (
    DATA_SCOPE_DENIED,
    DUPLICATE_RESOURCE,
    INVALID_ARGUMENT,
    NOT_FOUND,
    PROFILE_BINDING_REQUIRED,
    STATE_CONFLICT,
    VERSION_CONFLICT,
    ApiError,
    field_errors,
)
from apps.common.permissions import assert_not_self_review
from apps.accounts.models import Organization
from apps.persons.models import (
    AthleteProfile,
    CoachProfile,
    DictItem,
    DictType,
    Person,
    PersonChangeLog,
    Region,
    School,
)
from apps.teams.models import Team, TeamCoach, TeamMember

# 学校类型编码：DDL base_school.school_type 列注释枚举，与 init_base_data 命令
# 保持一致（编码已确认，字典项 label 待成员5确认）。字典数据就绪后可切换为
# 按 base_dict_item 的 school_type 项校验。
SCHOOL_TYPE_CODES: tuple[str, ...] = (
    "PRIMARY", "JUNIOR", "SENIOR", "NINE_YEAR", "COMPLETE", "MIDDLE_VOC",
)


# ---------------------------------------------------------------- 通用

def parse_path_id(raw: str, field: str = "id") -> int:
    """路径参数 → 正整数；不合法直接 400，避免拿脏值查库。"""
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        raise ApiError(
            INVALID_ARGUMENT,
            f"{field} 必须为正整数",
            field_errors([(field, "格式不合法")]),
        ) from None
    if value <= 0:
        raise ApiError(
            INVALID_ARGUMENT,
            f"{field} 必须为正整数",
            field_errors([(field, "必须为正整数")]),
        )
    return value


# ---------------------------------------------------------------- 区划

def list_regions(filters: dict) -> QuerySet:
    queryset = Region.objects.all()
    if filters.get("level") is not None:
        queryset = queryset.filter(level=filters["level"])
    if filters.get("parentId") is not None:
        queryset = queryset.filter(parent_id=filters["parentId"])
    keyword = filters.get("keyword")
    if keyword:
        queryset = queryset.filter(
            Q(name__icontains=keyword) | Q(code__icontains=keyword)
        )
    if filters.get("isActive") is not None:
        queryset = queryset.filter(is_active=filters["isActive"])
    return queryset.order_by("level", "sort_order", "code", "id")


def _resolve_region_parent(parent_id: Optional[int], level: int) -> Optional[Region]:
    """严格层级校验：省无父；市州父=省；区县父=市；父区划必须启用。"""
    if level == RegionLevel.PROVINCE:
        if parent_id:
            raise ApiError(
                INVALID_ARGUMENT,
                "省级区划不能有上级区划",
                field_errors([("parentId", "省级区划 parent 必须为空")]),
            )
        return None
    if not parent_id:
        raise ApiError(
            INVALID_ARGUMENT,
            "市州/区县必须指定上级区划",
            field_errors([("parentId", "不能为空")]),
        )
    parent = Region.objects.filter(pk=parent_id).first()
    if parent is None:
        raise ApiError(
            INVALID_ARGUMENT, "上级区划不存在", field_errors([("parentId", "不存在")])
        )
    expected = level - 1
    if parent.level != expected:
        raise ApiError(
            INVALID_ARGUMENT,
            f"上级区划级别应为 {expected} 级（{RegionLevel(expected).label}）",
            field_errors([("parentId", f"当前为 {parent.level} 级")]),
        )
    if not parent.is_active:
        raise ApiError(
            INVALID_ARGUMENT, "上级区划已停用", field_errors([("parentId", "已停用")])
        )
    return parent


def create_region(data: dict) -> Region:
    if Region.objects.filter(code=data["code"]).exists():
        raise ApiError(
            DUPLICATE_RESOURCE, "区划编码已存在", field_errors([("code", "已被占用")])
        )
    parent = _resolve_region_parent(data.get("parentId"), data["level"])
    return Region.objects.create(
        name=data["name"],
        code=data["code"],
        level=data["level"],
        parent=parent,
        sort_order=int(data.get("sortOrder") or 0),
        is_active=bool(data.get("isActive", True)),
    )


def get_region(region_id: str) -> Region:
    region = Region.objects.filter(pk=parse_path_id(region_id)).first()
    if region is None:
        raise ApiError(NOT_FOUND)
    return region


def update_region(region_id: str, data: dict) -> Region:
    region = get_region(region_id)
    parent_id = data.get("parentId")
    if parent_id == region.pk:
        raise ApiError(
            INVALID_ARGUMENT, "上级区划不能是自身", field_errors([("parentId", "自引用")])
        )
    # code/level 禁改：沿用原值重跑层级校验（父停用则子不可编辑）
    parent = _resolve_region_parent(parent_id, region.level)
    region.name = data["name"]
    region.parent = parent
    region.sort_order = int(data.get("sortOrder") or 0)
    region.is_active = bool(data.get("isActive", True))
    region.save()
    return region


def disable_region(region_id: str) -> Region:
    """停用区划（只停用不删）；存在启用下级时要求先停用下级。"""
    region = get_region(region_id)
    if Region.objects.filter(parent_id=region.pk, is_active=True).exists():
        raise ApiError(STATE_CONFLICT, "存在启用的下级区划，请先停用下级区划")
    region.is_active = False
    region.save()
    return region


# ---------------------------------------------------------------- 学校

def list_schools(filters: dict) -> QuerySet:
    queryset = School.objects.select_related("region")
    if filters.get("regionId") is not None:
        queryset = queryset.filter(region_id=filters["regionId"])
    if filters.get("schoolType"):
        queryset = queryset.filter(school_type=filters["schoolType"])
    keyword = filters.get("keyword")
    if keyword:
        queryset = queryset.filter(
            Q(name__icontains=keyword) | Q(code__icontains=keyword)
        )
    if filters.get("isActive") is not None:
        queryset = queryset.filter(is_active=filters["isActive"])
    return queryset.order_by("name", "id")


def _resolve_school_region(region_id: int) -> Region:
    region = Region.objects.filter(pk=region_id).first()
    if region is None:
        raise ApiError(
            INVALID_ARGUMENT, "所属区划不存在", field_errors([("regionId", "不存在")])
        )
    if not region.is_active:
        raise ApiError(
            INVALID_ARGUMENT, "所属区划已停用", field_errors([("regionId", "已停用")])
        )
    return region


def _check_school_type(value: str) -> None:
    if value not in SCHOOL_TYPE_CODES:
        raise ApiError(
            INVALID_ARGUMENT,
            f"schoolType 非法（允许：{','.join(SCHOOL_TYPE_CODES)}）",
            field_errors([("schoolType", "不在允许值域内")]),
        )


def _check_school_unique(
    name: str, code: Optional[str], region: Region, exclude_id: Optional[int] = None
) -> None:
    if code and (
        School.objects.filter(code=code).exclude(pk=exclude_id).exists()
    ):
        raise ApiError(
            DUPLICATE_RESOURCE, "学校标识码已存在", field_errors([("code", "已被占用")])
        )
    if School.objects.filter(name=name, region=region).exclude(pk=exclude_id).exists():
        raise ApiError(
            DUPLICATE_RESOURCE, "同一区划下校名已存在", field_errors([("name", "已被占用")])
        )


def create_school(data: dict) -> School:
    region = _resolve_school_region(data["regionId"])
    _check_school_type(data["schoolType"])
    _check_school_unique(data["name"], data.get("code") or None, region)
    return School.objects.create(
        name=data["name"],
        code=data.get("code") or None,
        school_type=data["schoolType"],
        region=region,
        address=data.get("address"),
        is_active=bool(data.get("isActive", True)),
    )


def get_school(school_id: str) -> School:
    school = School.objects.select_related("region").filter(
        pk=parse_path_id(school_id)
    ).first()
    if school is None:
        raise ApiError(NOT_FOUND)
    return school


def update_school(school_id: str, data: dict) -> School:
    school = get_school(school_id)
    region = _resolve_school_region(data["regionId"])
    _check_school_type(data["schoolType"])
    _check_school_unique(
        data["name"], school.code, region, exclude_id=school.pk
    )
    school.name = data["name"]
    # code 创建后禁改（Q1#2 一期不可变；绑定组织后改码会导致 org.code 映射失配）
    school.school_type = data["schoolType"]
    school.region = region
    school.address = data.get("address")
    school.is_active = bool(data.get("isActive", True))
    school.save()
    return school


def disable_school(school_id: str) -> School:
    """停用学校（只停用不删；person.school 为 PROTECT 引用，不物理删除）。"""
    school = get_school(school_id)
    school.is_active = False
    school.save()
    return school


# ---------------------------------------------------------------- 字典类型

def list_dict_types(filters: dict) -> QuerySet:
    queryset = DictType.objects.all()
    keyword = filters.get("keyword")
    if keyword:
        queryset = queryset.filter(
            Q(code__icontains=keyword) | Q(name__icontains=keyword)
        )
    return queryset.order_by("code", "id")


def create_dict_type(data: dict) -> DictType:
    if DictType.objects.filter(code=data["code"]).exists():
        raise ApiError(
            DUPLICATE_RESOURCE, "字典类型编码已存在", field_errors([("code", "已被占用")])
        )
    return DictType.objects.create(
        code=data["code"],
        name=data["name"],
        remark=data.get("remark") or "",
    )


def get_dict_type(dict_type_id: str) -> DictType:
    obj = DictType.objects.filter(pk=parse_path_id(dict_type_id)).first()
    if obj is None:
        raise ApiError(NOT_FOUND)
    return obj


def update_dict_type(dict_type_id: str, data: dict) -> DictType:
    obj = get_dict_type(dict_type_id)
    obj.name = data["name"]
    obj.remark = data.get("remark") or ""
    obj.save()
    return obj


def delete_dict_type(dict_type_id: str) -> DictType:
    """表无停用列：无字典项引用时物理删除；有引用时 409 拒绝（PROTECT 兜底）。"""
    obj = get_dict_type(dict_type_id)
    if DictItem.objects.filter(type_id=obj.pk).exists():
        raise ApiError(STATE_CONFLICT, "存在字典项，不能删除，请先处理字典项")
    obj.delete()
    return obj


# ---------------------------------------------------------------- 字典项

def list_dict_items(filters: dict) -> QuerySet:
    queryset = DictItem.objects.select_related("type")
    if filters.get("typeCode"):
        queryset = queryset.filter(type__code=filters["typeCode"])
    keyword = filters.get("keyword")
    if keyword:
        queryset = queryset.filter(
            Q(label__icontains=keyword) | Q(value__icontains=keyword)
        )
    if filters.get("isActive") is not None:
        queryset = queryset.filter(is_active=filters["isActive"])
    return queryset.order_by("sort_order", "value", "id")


def create_dict_item(data: dict) -> DictItem:
    dtype = DictType.objects.filter(code=data["typeCode"]).first()
    if dtype is None:
        raise ApiError(
            INVALID_ARGUMENT, "字典类型不存在", field_errors([("typeCode", "不存在")])
        )
    if DictItem.objects.filter(type=dtype, value=data["value"]).exists():
        raise ApiError(
            DUPLICATE_RESOURCE,
            "同一字典类型下 value 已存在",
            field_errors([("value", "已被占用")]),
        )
    return DictItem.objects.create(
        type=dtype,
        value=data["value"],
        label=data["label"],
        sort_order=int(data.get("sortOrder") or 0),
        is_active=bool(data.get("isActive", True)),
        ext_json=data.get("extJson"),
    )


def get_dict_item(dict_item_id: str) -> DictItem:
    item = DictItem.objects.select_related("type").filter(
        pk=parse_path_id(dict_item_id)
    ).first()
    if item is None:
        raise ApiError(NOT_FOUND)
    return item


def update_dict_item(dict_item_id: str, data: dict) -> DictItem:
    """value/type 禁改（value 稳定编码,含义禁改）；仅更新 label/排序/启停/ext。"""
    item = get_dict_item(dict_item_id)
    item.label = data["label"]
    item.sort_order = int(data.get("sortOrder") or 0)
    item.is_active = bool(data.get("isActive", True))
    item.ext_json = data.get("extJson")
    item.save()
    return item


def disable_dict_item(dict_item_id: str) -> DictItem:
    """停用字典项（只停用不删）。"""
    item = get_dict_item(dict_item_id)
    item.is_active = False
    item.save()
    return item


def dict_type_options(type_code: str) -> QuerySet:
    """可用字典选项（仅启用项）；未知 type_code 返回空集（对齐 accounts 行为）。"""
    return (
        DictItem.objects.filter(type__code=type_code, is_active=True)
        .select_related("type")
        .order_by("sort_order", "value", "id")
    )


# ---------------------------------------------------------------- 人员主档查询
# （/people 读链，契约对齐 zuqiu 前端 personApi；写链依赖待确认项未实施）

def people_queryset() -> QuerySet:
    """人员主档基础查询：排除软删；学校、扩展档案、现役球队名一次取齐。

    team_name 取现役（CURRENT）球队名（team_member 单向流转，历史关系不计）。
    """
    current_team_name = TeamMember.objects.filter(
        person=OuterRef("pk"), status=enums.RelationStatus.CURRENT
    ).values("team__name")[:1]
    return (
        Person.objects.filter(is_deleted=False)
        .select_related("school")
        .annotate(
            student_no_val=Subquery(
                AthleteProfile.objects.filter(person=OuterRef("pk")).values("student_no")[:1]
            ),
            team_name_val=current_team_name,
            # kinds 合成（方案C）与 kind 过滤的存在性事实源（免 N+1）
            athlete_profile_exists=Exists(
                AthleteProfile.objects.filter(person=OuterRef("pk"))
            ),
            coach_profile_exists=Exists(CoachProfile.objects.filter(person=OuterRef("pk"))),
            # P1-7 证书只读派生（免 N+1；仅 COACH 输出，仅供展示、不参与判断）
            coach_certificate_no_val=Subquery(
                CoachProfile.objects.filter(person=OuterRef("pk")).values("certificate_no")[:1]
            ),
            coach_certificate_expire_val=Subquery(
                CoachProfile.objects.filter(person=OuterRef("pk")).values(
                    "certificate_expire_date"
                )[:1]
            ),
            # schoolOrgId 输出（A′-min）：School.code → 组织节点主键（免 N+1；
            # Organization.code 唯一，至多一命中）
            school_org_id_val=Subquery(
                Organization.objects.filter(code=OuterRef("school__code")).values("pk")[:1]
            ),
        )
    )


def apply_person_scope(queryset: QuerySet, account) -> QuerySet:
    """人员数据范围过滤（集中一处；A′ 第三批 2026-10-29 起仅改本函数）。

    口径（成员4 专项① 第三批，业务决定 5+6 项确认）：

    - ADMIN：保持第一批行为不变（不增加学校类型筛选、不重构）——
      scope_org_codes 三态语义（common/scopes.py，不做 `codes or fallback`
      式放大）：
      * None（全局 ADMIN，无组织绑定）→ 全库；
      * []（无组织绑定/绑定不存在/绑定已软删，即幽灵绑定）→ 默认拒绝，
        不得退化为全库；
      * 非空子树编码 → 经 Person.school（School.code）与 Organization.code
        的真实映射过滤，仅可见授权组织子树内学校归属的人员（school 为空
        的人员对挂组织 ADMIN 不可见：deny-by-default）。

    - UNIVERSITY（第三批新增）：绑定节点必须存在且 org_type ∈ {SCHOOL,
      UNIVERSITY}（学校类，同 _resolve_school 口径），否则 403；仅可见
      其组织子树中学校类节点 code 对应的人员。节点类型仅作范围识别，
      授权仍以 Person.school.code 编码映射为准；无 school 归属人员不可见
      （不经任职球队的学校归属反推）。

    - TEAM（第三批新增）：绑定节点必须存在且 org_type == "TEAM"
      （A′-min：TEAM 节点 code == team_no），否则 403；仅可见**绑定球队**
      的正式 CURRENT 球员与教练（TeamMember / TeamCoach 两条关联路径
      各自独立按 CURRENT 过滤），不经学校归属反推，不因组织子树扩展到
      其他球队（多队授权未立项）。

    - CITY / PROVINCE_REVIEWER：403 DATA_SCOPE_DENIED（市级/省级人员
      范围留待独立评审，契约文案不区分角色）。

    角色门禁必须在 scope_org_codes 之前：该 helper 不校验角色，若颠倒
    顺序，UNIVERSITY 等有组织绑定的账号会被其非空子树编码误放行。
    """
    from apps.common.enums import RelationStatus
    from apps.common.permissions import ADMIN
    from apps.common.scopes import org_scope_ids, scope_org_codes

    roles = set(getattr(account, "roles", ()) or ())
    if ADMIN in roles:
        codes = scope_org_codes(account)
        if codes is None:
            return queryset
        if not codes:
            raise ApiError(
                DATA_SCOPE_DENIED,
                "当前账号无有效组织范围，禁止访问人员数据",
            )
        return queryset.filter(school__code__in=codes)

    if "UNIVERSITY" not in roles and "TEAM" not in roles:
        raise ApiError(
            DATA_SCOPE_DENIED,
            "市级/省级人员数据范围尚未开放，当前仅管理员及学校/球队端可查询",
        )

    from apps.accounts.models import Organization
    from apps.teams.models import TeamCoach, TeamMember

    org_id = getattr(account, "organization_id", None)
    org = Organization.objects.filter(pk=org_id).first() if org_id else None
    if org is None:
        raise ApiError(
            DATA_SCOPE_DENIED,
            "当前账号无有效组织范围，禁止访问人员数据",
        )

    if "UNIVERSITY" in roles:
        if org.org_type not in ("SCHOOL", "UNIVERSITY"):
            raise ApiError(
                DATA_SCOPE_DENIED,
                "学校端账号必须绑定学校类组织节点，禁止访问人员数据",
            )
        # 子树内学校类节点 code（剔除 TEAM 等非学校类子节点，防止脏编码）
        school_codes = list(
            Organization.objects.filter(pk__in=org_scope_ids(account))
            .filter(org_type__in=("SCHOOL", "UNIVERSITY"))
            .values_list("code", flat=True)
        )
        if not school_codes:
            raise ApiError(
                DATA_SCOPE_DENIED,
                "当前账号无有效组织范围，禁止访问人员数据",
            )
        return queryset.filter(school__code__in=school_codes)

    # TEAM 分支：仅绑定节点自身（org.code == team_no），不取子树
    if org.org_type != "TEAM":
        raise ApiError(
            DATA_SCOPE_DENIED,
            "球队端账号必须绑定球队组织节点，禁止访问人员数据",
        )
    return queryset.filter(
        Exists(
            TeamMember.objects.filter(
                person=OuterRef("pk"),
                team__team_no=org.code,
                status=RelationStatus.CURRENT,
            )
        )
        | Exists(
            TeamCoach.objects.filter(
                person=OuterRef("pk"),
                team__team_no=org.code,
                status=RelationStatus.CURRENT,
            )
        )
    )


def verify_person_owner(account, owner_id) -> None:
    """PERSON 附件归属校验器（经 PersonsConfig.ready() 注册进 attachments）。

    PERSON 归属文件的上传与读取均由本校验器裁决业务资源所有权
    （attachments._check_owner / can_access 调用），规则复用既有已确认口径：
    - 归属记录不存在（人员不存在或已软删）→ 404，默认拒绝；
    - ADMIN → 放行（A′ 一期数据范围仅 ADMIN 全量，同 apply_person_scope）；
    - 账号绑定本人档案（account.person_id 服务端签发，与 /me/profile 同源）
      → 放行（本人材料上传/查看，对应 materialList/Create 含 ATHLETE/COACH
      的自服务场景）；
    - 其余一律 DATA_SCOPE_DENIED——本市/本校/本队范围依赖 A′ 映射，未交付
      前不得放行，不发明组织层级授权。
    """
    from apps.common.permissions import ADMIN

    raw = str(owner_id or "").strip()
    if not raw:
        raise ApiError(DATA_SCOPE_DENIED, "文件归属对象未确定，暂无法校验归属")
    if not Person.objects.filter(
        pk=parse_path_id(raw, "ownerId"), is_deleted=False
    ).exists():
        raise ApiError(NOT_FOUND, "归属人员不存在或已删除")
    if ADMIN in (getattr(account, "roles", ()) or ()):
        return
    if str(getattr(account, "person_id", "") or "") == raw:
        return
    raise ApiError(
        DATA_SCOPE_DENIED,
        "人员数据范围待组织映射方案确认后开放，当前仅管理员与本人可访问",
    )


def _check_enum(value: Optional[str], allowed: tuple, field: str) -> None:
    if value is not None and value not in allowed:
        raise ApiError(
            INVALID_ARGUMENT,
            f"{field} 非法（允许：{','.join(allowed)}）",
            field_errors([(field, "不在允许值域内")]),
        )


def list_people(filters: dict, account) -> QuerySet:
    """GET /people：keyword 匹配姓名；kind→扩展档案行存在性（方案C 合成口径）；
    reviewStatus→audit_status。

    前端实际传参（admin/PeopleManageView.vue L49-55）：page/pageSize/keyword/
    kind/reviewStatus。kind=ATHLETE/COACH 与 kinds 合成同源（档案行存在性），
    双类别人员在两种过滤下均命中。mock 的 maskedIdNumber 包含匹配与 teamId
    过滤为演示逻辑，不照搬（maskedIdNumber 是敏感掩码，不得作为检索键；
    球队读服务已就绪；人员列表 teamId 筛选确认不实现——2026-10-26 前端确认
    不传该参数，契约清单第 12 项已关闭，按球队筛人走 /selection-dossiers/search）。
    """
    kind = filters.get("kind")
    _check_enum(kind, tuple(enums.PersonCategory.values), "kind")
    review_status = filters.get("reviewStatus")
    _check_enum(review_status, tuple(enums.AuditStatus.values), "reviewStatus")
    coach_duty = filters.get("coachDuty")
    _check_enum(coach_duty, tuple(enums.CoachDuty.values), "coachDuty")
    queryset = apply_person_scope(people_queryset(), account)
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        queryset = queryset.filter(full_name__icontains=keyword)
    if kind == "ATHLETE":
        queryset = queryset.filter(athlete_profile_exists=True)
    elif kind == "COACH":
        queryset = queryset.filter(coach_profile_exists=True)
    if review_status:
        queryset = queryset.filter(audit_status=review_status)
    if coach_duty:
        # D-CAT：仅按现役任职（status=CURRENT）筛选；任一现役任职命中即返回
        # 该人员一次（Exists 子查询去重，EXITED 历史关系不参与）。
        queryset = queryset.filter(
            Exists(
                TeamCoach.objects.filter(
                    person=OuterRef("pk"),
                    status=enums.RelationStatus.CURRENT,
                    duty=coach_duty,
                )
            )
        )
    return queryset.order_by("-id")


def coach_duties_map(person_ids: list) -> dict:
    """D-CAT：当页人员的现役岗位集合（status=CURRENT 去重，枚举序输出）。

    仅供人员管理列表使用（输出 coachDuties）；当页一次查询内存分组，
    不用 ArrayAgg/GROUP_CONCAT（SQLite/MySQL 双兼容，无 N+1）。
    无现役任职的人员不出现在结果中（调用方缺省为空数组）。
    """
    if not person_ids:
        return {}
    rows = TeamCoach.objects.filter(
        person_id__in=person_ids,
        status=enums.RelationStatus.CURRENT,
    ).values_list("person_id", "duty")
    grouped: dict = {}
    for person_id, duty in rows:
        grouped.setdefault(person_id, set()).add(duty)
    order = list(enums.CoachDuty.values)
    return {
        person_id: sorted(duties, key=order.index)
        for person_id, duties in grouped.items()
    }


def search_people(keyword: str, account) -> list:
    """POST /people/search：{keyword} 按姓名包含匹配，返回数组（非分页）。

    mock 的 slice(0, 20) 截断沿用作无分页检索的保护上限（C 类沿用项，
    如需调整待确认）。mock 按 maskedIdNumber 匹配为演示逻辑，不照搬。
    """
    queryset = apply_person_scope(people_queryset(), account)
    term = (keyword or "").strip()
    if term:
        queryset = queryset.filter(full_name__icontains=term)
    return list(queryset.order_by("-id")[:20])


def get_person(person_id: str, account):
    """GET /people/{id}：软删行视为不存在（NOT_FOUND）。"""
    obj = (
        apply_person_scope(people_queryset(), account)
        .filter(pk=parse_path_id(person_id))
        .first()
    )
    if obj is None:
        raise ApiError(NOT_FOUND)
    return obj


def get_own_person(person_id: str):
    """GET /me/profile：按账号绑定 person_id 直取本人档案。

    自读场景对象归属即本人，不经过 apply_person_scope（组织数据范围与
    本人档案无关；person_id 由服务端签发，不接受客户端传入）。
    软删行与 get_person 同口径视为不存在（NOT_FOUND）。
    """
    obj = people_queryset().filter(pk=parse_path_id(person_id)).first()
    if obj is None:
        raise ApiError(NOT_FOUND)
    return obj


def get_profile_match(account) -> dict:
    """GET /me/profile-match：本人主档匹配状态（契约清单 B2）。

    仅基于服务端账号绑定关系判定（接口文档 §6.5 规则：基于已验证注册凭证/
    绑定关系查询，不支持向任意身份证探测——本接口无任何请求参数）。
    三态映射实现口径（文档枚举 NO_MATCH / MATCHED_TO_SELF / REVIEW_REQUIRED）：
    - 未绑定 / 绑定值非法 / 档案不存在或已软删 → NO_MATCH；
    - 绑定档案 audit_status 非 APPROVED（DRAFT/PENDING/REJECTED）→
      REVIEW_REQUIRED，不返回 personId/maskedIdNumber（文档规则：
      "待复核只返回 REVIEW_REQUIRED，不暴露候选人详情"）；
    - APPROVED → MATCHED_TO_SELF + personId + maskedIdNumber
      （maskedIdNumber=冻结列 id_card_masked 直出，与 GET /me/profile 同源）。
    与 GET /me/profile 的差异：本接口职责即探测绑定状态，绑定失效属
    枚举内结果而非请求错误，因此不走 NOT_FOUND/400。
    """
    raw = (getattr(account, "person_id", "") or "").strip()
    try:
        person_pk = int(raw) if raw else 0
    except ValueError:
        return {"result": "NO_MATCH"}
    if person_pk <= 0:
        return {"result": "NO_MATCH"}
    person = people_queryset().filter(pk=person_pk).first()
    if person is None:
        return {"result": "NO_MATCH"}
    if person.audit_status != enums.AuditStatus.APPROVED:
        return {"result": "REVIEW_REQUIRED"}
    return {
        "result": "MATCHED_TO_SELF",
        "personId": str(person.pk),
        "maskedIdNumber": person.id_card_masked,
    }


# ---------------------------------------------------------------- 人员主档写链
# （POST/PUT/DELETE /people；kinds 方案C 多类别 1..2 由扩展档案行承载；
#   乐观锁=B4 派生版本）

# P0-4 关键审核字段（2026-10-28 批准）：任一变化使原审核结论失效（APPROVED → PENDING）。
# coachCategory 当前"接受不持久化"（D2 在案），无法可靠检测变化，暂不入集（已报告）。
KEY_AUDIT_FIELDS = frozenset(
    {"realName", "birthDate", "school", "personCategory", "kinds", "gender"}
)


def _invalidate_audit_on_key_change(person, changes: list) -> bool:
    """关键字段变更且档案已 APPROVED → 原审核结论失效，回 PENDING。

    必须与字段写入同一事务、同一次 person.save() 执行，并向 changes 追加
    audit_status 变更条目（同一份 PersonChangeLog，不新增日志）；
    并发过期写入由 ensure_version 在入口拦截。返回是否触发失效。
    """
    if person.audit_status != enums.AuditStatus.APPROVED:
        return False
    if not any(c["field"] in KEY_AUDIT_FIELDS for c in changes):
        return False
    old = str(person.audit_status)
    person.audit_status = enums.AuditStatus.PENDING
    changes.append({"field": "audit_status", "old": old, "new": str(person.audit_status)})
    return True


def person_audit_snapshot(person_ids) -> dict:
    """报名域直连查询（P0-3 决策 A，2026-10-28）：人员档案审核快照。

    输入 athlete_id 字符串集合；仅返回可解析为 Person 主键、未软删的行，
    值为 {"auditStatus", "status", "personCategory"}。查不到的 id 不出现
    在结果中，由调用方按各自阶段语义处置（提交 WARNING / 终审阻断）。
    只读，不触碰任何 persons 写链。
    """
    parsed = {}
    for raw in person_ids:
        try:
            parsed[int(str(raw))] = str(raw)
        except (TypeError, ValueError):
            continue
    if not parsed:
        return {}
    rows = Person.objects.filter(
        pk__in=parsed.keys(), is_deleted=False
    ).values_list("pk", "audit_status", "status", "person_category")
    return {
        parsed[pk]: {
            "auditStatus": audit_status,
            "status": status,
            "personCategory": person_category,
        }
        for pk, audit_status, status, person_category in rows
    }


def ensure_version(person, request_version) -> None:
    """乐观锁校验：提交版本与当前派生版本不一致 → 409 VERSION_CONFLICT。"""
    from apps.persons.schemas import person_version

    current = person_version(person)
    if int(request_version) != current:
        raise ApiError(
            VERSION_CONFLICT,
            f"数据已被其他操作更新（当前版本 {current}，提交版本 {int(request_version)}）",
        )


def _gender_in(value: Optional[str], field: str) -> int:
    """前端契约性别名（UNSPECIFIED/MALE/FEMALE）→ 冻结 TinyInt 值（0/1/2）。"""
    from apps.persons.schemas import GENDER_IN

    raw = (value or "").strip().upper()
    if not raw:
        return enums.Gender.UNKNOWN
    if raw not in GENDER_IN:
        raise ApiError(
            INVALID_ARGUMENT,
            f"{field} 非法（允许：{','.join(GENDER_IN)}）",
            field_errors([(field, "不在允许值域内")]),
        )
    return GENDER_IN[raw]


def _kind_list(kinds) -> list[str]:
    """一人一身份（问题 11，2026-10-30 批准收紧）：kinds 数组恰好 1 项。

    值域校验+去重+单项校验，不静默丢弃；person_category 存该类别，
    完整类别集合由扩展档案行存在性承载（见 _apply_kinds/_synthesized_kinds）。
    """
    if not isinstance(kinds, list) or not kinds:
        raise ApiError(
            INVALID_ARGUMENT,
            "kinds 必填（1 项）",
            field_errors([("kinds", "至少一项")]),
        )
    if len(kinds) > 1:
        raise ApiError(
            INVALID_ARGUMENT,
            "kinds 仅允许一项（一人一身份）",
            field_errors([("kinds", "只能选择一个身份类别")]),
        )
    unknown = [k for k in kinds if k not in enums.PersonCategory.values]
    if unknown:
        raise ApiError(
            INVALID_ARGUMENT,
            f"kinds 非法（允许：{','.join(enums.PersonCategory.values)}）",
            field_errors([("kinds", "不在允许值域内")]),
        )
    if len(set(kinds)) != len(kinds):
        raise ApiError(
            INVALID_ARGUMENT,
            "kinds 存在重复类别",
            field_errors([("kinds", "类别重复")]),
        )
    return list(kinds)


# 扩展档案行的"业务数据"字段（类别移除保护：有数据即拒绝删行防丢失；
# AthleteProfile.grade_level 为 NOT NULL 占位列，不计入业务数据）
_ATHLETE_DATA_FIELDS = (
    "student_no",
    "position_code",
    "guardian_name",
    "guardian_relation",
    "guardian_phone_cipher",
    "ext_json",
)
_COACH_DATA_FIELDS = ("coach_level", "certificate_no", "certificate_expire_date", "ext_json")


def _profile_has_data(profile, fields) -> bool:
    return any(getattr(profile, f) not in (None, "") for f in fields)


def _apply_kinds(person, new_kinds: list[str], old_kinds: list[str]) -> None:
    """按 kinds 差集维护扩展档案行（方案C 承载）。

    新增类别 → 建 profile 行（AthleteProfile.grade_level 为冻结 NOT NULL 列，
    前端创建表单无学段字段，以空串占位待运动员档案回填）；
    移除类别 → 行内无业务数据才删（有数据 409 拒绝，防档案数据丢失）。
    """
    added = [k for k in new_kinds if k not in old_kinds]
    removed = [k for k in old_kinds if k not in new_kinds]
    # 先校验后变更：任一移除项有数据则整体拒绝（事务回滚，不产生半变更）
    for kind in removed:
        if kind == "ATHLETE":
            profile = AthleteProfile.objects.filter(person=person).first()
            if profile is not None and _profile_has_data(profile, _ATHLETE_DATA_FIELDS):
                raise ApiError(
                    STATE_CONFLICT,
                    "运动员档案已有数据（学籍/位置/监护人等），移除该类别将丢失档案，请先清理档案数据",
                )
        else:
            profile = CoachProfile.objects.filter(person=person).first()
            if profile is not None and _profile_has_data(profile, _COACH_DATA_FIELDS):
                raise ApiError(
                    STATE_CONFLICT,
                    "教练员档案已有数据（等级/证书等），移除该类别将丢失档案，请先清理档案数据",
                )
    for kind in removed:
        (AthleteProfile if kind == "ATHLETE" else CoachProfile).objects.filter(
            person=person
        ).delete()
    for kind in added:
        if kind == "ATHLETE":
            AthleteProfile.objects.get_or_create(person=person, defaults={"grade_level": ""})
        else:
            CoachProfile.objects.get_or_create(person=person)


def _resolve_school(school_org_id):
    """schoolOrgId（Organization 节点 ID）→ School 主档（A′-min 契约）。

    2026-10-25 契约确认：按组织节点主键查询，不把该值当作 School.code 或
    Organization.code；节点必须存在、未删除未停用、类型属于学校类
    （SCHOOL/UNIVERSITY，CITY/PROVINCE/TEAM 等一律拒绝）；随后按
    org.code=School.code 反查学校主档（Person.school 外键固化，必须有业务
    主档承载）。任一不满足 → 400 INVALID_ARGUMENT + 可定位字段明细。
    未传（None/空串）→ None（保留既有"未传=不清空"语义）。
    """
    raw = str(school_org_id or "").strip()
    if not raw:
        return None
    org = Organization.objects.filter(pk=raw).first()
    if org is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "所属学校组织不存在",
            field_errors([("schoolOrgId", "组织节点不存在")]),
        )
    if org.deleted_at is not None or not org.enabled:
        raise ApiError(
            INVALID_ARGUMENT,
            "所属学校组织已停用",
            field_errors([("schoolOrgId", "组织节点已停用或已删除")]),
        )
    if org.org_type not in ("SCHOOL", "UNIVERSITY"):
        raise ApiError(
            INVALID_ARGUMENT,
            "所属组织类型不符",
            field_errors(
                [("schoolOrgId", f"人员归属必须是 SCHOOL/UNIVERSITY 节点，当前为 {org.org_type}")]
            ),
        )
    school = School.objects.filter(code=org.code, is_active=True).first()
    if school is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "组织节点未关联学校主档",
            field_errors([("schoolOrgId", f"组织节点 {org.code} 未关联学校主档")]),
        )
    return school


def _resolve_avatar_file(avatar_file_id):
    """avatarFileId → photo_file_id（校验文件存在且归属 PERSON，不绕过附件归属）。

    FileRecord 主键为 new_file_id() 字符串（前端 avatarFileId 类型即 string）。
    """
    if not avatar_file_id:
        return None
    from apps.attachments.models import FileRecord

    file_id = str(avatar_file_id).strip()
    if not file_id:
        return None
    record = FileRecord.objects.filter(pk=file_id, owner_type="PERSON").first()
    if record is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "avatarFileId 对应文件不存在或归属不符",
            field_errors([("avatarFileId", "文件不存在或归属不符")]),
        )
    return file_id


def _id_card_extras(id_number: str):
    """身份证解析出生日期与性别（GB 11643：第7-14位出生、第17位奇男偶女）。

    依据冻结 DDL birth_date 列注释"身份证解析"；请求显式传入时优先于解析值。
    """
    birth = None
    gender = None
    if len(id_number) == 18 and id_number[:17].isdigit():
        try:
            birth = datetime.strptime(id_number[6:14], "%Y%m%d").date()
        except ValueError:
            birth = None
        gender = enums.Gender.MALE if int(id_number[16]) % 2 else enums.Gender.FEMALE
    return birth, gender


def _phone_fields(phone: Optional[str]) -> dict:
    if not phone:
        return {"phone_cipher": None, "phone_hash": None, "phone_masked": None}
    return {
        "phone_cipher": crypto.encrypt_text(phone),
        "phone_hash": crypto.lookup_hash(phone),
        "phone_masked": crypto.mask_phone(phone),
    }


def _log_change(person, change_type: str, field_changes, reason: str = "") -> None:
    PersonChangeLog.objects.create(
        person=person,
        change_type=change_type,
        field_changes=field_changes or None,
        changed_by=None,  # 底座用户ID映射未定，一期保持 NULL（AuditLog 留痕）
        reason=reason or "",
    )


def _bind_avatar(avatar_file_id, person) -> None:
    """先传后绑（前端新建人员时先传头像，FileRecord.owner_id 为空）：
    人员落库后回填文件归属，保证后续归属校验（can_access 的 verifier 路径）完整。
    """
    from apps.attachments.models import FileRecord

    FileRecord.objects.filter(pk=str(avatar_file_id), owner_type="PERSON").update(
        owner_id=str(person.pk)
    )


def _current_kinds(person) -> list[str]:
    """当前 DB 行存在性 → kinds（与 schemas._synthesized_kinds 同口径，固定顺序）。"""
    kinds: list[str] = []
    if AthleteProfile.objects.filter(person=person).exists():
        kinds.append("ATHLETE")
    if CoachProfile.objects.filter(person=person).exists():
        kinds.append("COACH")
    return kinds


def _ensure_school_in_operator_scope(school_org_id, account, *, required: bool) -> None:
    """schoolOrgId 目标学校的操作者授权范围校验（创建/转校共用）。

    目标学校组织节点必须落在操作者授权组织范围内（复用 ensure_org_in_scope，
    其内部按 is_global_admin 放行全局 ADMIN）：
    - 全局 ADMIN（无组织绑定）：保持原语义，schoolOrgId 可选、范围不限；
    - 挂组织 ADMIN：
      - 创建（required=True）：schoolOrgId 未传 → 目标 school=None 必然在
        子树之外（deny-by-default，无学校归属即无法证明在授权范围内），拒绝；
      - 转校（required=False）：未传保持"不修改学校归属"既有语义，跳过校验；
        已传 → 节点不在授权子树内时拒绝，禁止跨授权边界迁移人员。
    挂组织 ADMIN 之外的角色本就过不了 apply_person_scope 角色门禁，
    此处按"目标写入口"再校验一次，防止单独走写链的路径绕过读范围。
    """
    from apps.common.scopes import ensure_org_in_scope, is_global_admin

    if is_global_admin(account):
        return
    raw = str(school_org_id or "").strip()
    if not raw:
        if required:
            raise ApiError(
                DATA_SCOPE_DENIED,
                "创建人员必须指定数据范围内的学校组织",
                field_errors([("schoolOrgId", "必填")]),
            )
        return
    ensure_org_in_scope(account, raw)


def create_person(payload: dict, account):
    """POST /people：创建人员主档（DRAFT 起步）+ 变更留痕。

    - kinds 方案C：person_category 存主类别（首项），按类别建扩展档案行；
    - idNumber 一期必填（服务层）：冻结 id_card_hash 无 NULL 且唯一含软删行；
      查重覆盖软删行（唯一约束），冲突 409 不复活（恢复语义待确认）；
    - birthDate/gender 未传时按身份证解析（DDL 列注释口径）。
    """
    kind_list = _kind_list(payload.get("kinds"))
    id_number = (payload.get("idNumber") or "").strip().upper() or None
    phone = (payload.get("phone") or "").strip() or None
    _ensure_school_in_operator_scope(payload.get("schoolOrgId"), account, required=True)
    school = _resolve_school(payload.get("schoolOrgId"))
    avatar = _resolve_avatar_file(payload.get("avatarFileId"))
    if not id_number:
        raise ApiError(
            INVALID_ARGUMENT,
            "身份证号必填（无证件档案的承载受冻结唯一约束限制，待确认）",
            field_errors([("idNumber", "必填")]),
        )
    duplicate = Person.objects.filter(id_card_hash=crypto.lookup_hash(id_number)).first()
    if duplicate is not None:
        suffix = "（当前为已删除状态，恢复需另行确认）" if duplicate.is_deleted else ""
        raise ApiError(
            DUPLICATE_RESOURCE,
            f"该证件号已存在档案{suffix}",
            field_errors([("idNumber", "证件号重复")]),
        )
    birth_date = payload.get("birthDate")
    raw_birth, raw_gender = _id_card_extras(id_number)
    if birth_date is None:
        birth_date = raw_birth
    gender_value = _gender_in(payload.get("gender"), "gender")
    if not (payload.get("gender") or "").strip():
        gender_value = raw_gender if raw_gender is not None else enums.Gender.UNKNOWN

    with transaction.atomic():
        person = Person(
            full_name=payload["realName"].strip(),
            gender=gender_value,
            birth_date=birth_date,
            person_category=kind_list[0],
            id_card_cipher=crypto.encrypt_text(id_number),
            id_card_hash=crypto.lookup_hash(id_number),
            id_card_masked=crypto.mask_id_number(id_number),
            **_phone_fields(phone),
            school=school,
            photo_file_id=avatar,
            education=(payload.get("education") or "").strip() or None,
            audit_status=enums.AuditStatus.DRAFT,
            status=enums.EnableStatus.ACTIVE,
        )
        person.save()
        # person_no 按 DDL 列注释回填：P+年份+8位id（应用层回填）
        person.person_no = f"P{person.created_at:%Y}{person.pk:08d}"
        person.save()
        # 方案C：按 kinds 建扩展档案行（空档案，业务字段待对应页面回填）
        if "ATHLETE" in kind_list:
            AthleteProfile.objects.create(person=person, grade_level="")
        if "COACH" in kind_list:
            CoachProfile.objects.create(person=person)
        if avatar:
            _bind_avatar(avatar, person)
        _log_change(person, enums.ChangeType.CREATE, None)
    return person


def update_person(person_id: str, payload: dict, account):
    """PUT /people/{id}：版本校验 → 必填字段全量替换 + 可选字段缺省保留 → diff 留痕。

    idNumber 不接受（身份证变更走更正流程，一期不开放）；
    education 已持久化（E1：未传保留原值、传值含空串=覆盖清空）；
    coachCategory 声明接受但不持久化、不回显（正式字典 E2 待签发，承载待业务决策）。
    """
    person = get_person(person_id, account)
    ensure_version(person, payload["version"])
    # 问题 11 方案 B：kinds 可选。未传=保留现有身份（不调 _apply_kinds、
    # 不改 person_category、不生身份变更日志）；传值=显式变更身份（限 1 项）。
    raw_kinds = payload.get("kinds")
    kind_list = _kind_list(raw_kinds) if raw_kinds is not None else None
    old_kinds = _current_kinds(person)
    gender_value = _gender_in(payload.get("gender"), "gender")
    primary_category = kind_list[0] if kind_list is not None else person.person_category

    changes: list[dict] = []

    def track(field: str, old, new) -> None:
        if old != new:
            # field_changes 落 JSONField：date 等非 JSON 原生类型转 ISO 字符串
            old_out = old.isoformat() if hasattr(old, "isoformat") else old
            new_out = new.isoformat() if hasattr(new, "isoformat") else new
            changes.append({"field": field, "old": old_out, "new": new_out})

    # 类别移除保护（有档案数据 409）必须在落库前校验；_apply_kinds 在事务内执行
    with transaction.atomic():
        new_name = payload["realName"].strip()
        track("realName", person.full_name, new_name)
        person.full_name = new_name

        track("gender", person.gender, gender_value)
        person.gender = gender_value

        track("birthDate", person.birth_date, payload.get("birthDate"))
        person.birth_date = payload.get("birthDate")

        # 问题 11 方案 B：仅显式传 kinds 时才记录身份变更并更新主类别
        if kind_list is not None:
            track("kinds", old_kinds, kind_list)
            track("personCategory", person.person_category, primary_category)
            person.person_category = primary_category

        # 可选字段：请求未传 → 保留原值（编辑表单无对应输入，缺省清空=静默丢数据）
        # 转校（schoolOrgId 已传）必须落在操作者授权范围内；未传则保持
        # "不修改学校归属"既有语义，不做额外校验
        _ensure_school_in_operator_scope(payload.get("schoolOrgId"), account, required=False)
        school = _resolve_school(payload.get("schoolOrgId"))
        if school is not None:
            track("school", person.school_id, school.pk)
            person.school = school
        avatar = _resolve_avatar_file(payload.get("avatarFileId"))
        if avatar is not None:
            track("photoFileId", person.photo_file_id, avatar)
            person.photo_file_id = avatar
        # education：未传（None）→ 保留原值（编辑表单缺省不丢数据）；
        # 传值（含空串）→ 覆盖写入（表单有输入框，可主动清空）
        education_value = payload.get("education")
        if education_value is not None:
            education_value = education_value.strip() or None
            track("education", person.education, education_value)
            person.education = education_value
        phone = (payload.get("phone") or "").strip() or None
        if phone:
            track("phoneMasked", person.phone_masked, crypto.mask_phone(phone))
            person.__dict__.update(_phone_fields(phone))

        # P0-4：关键字段变化使原审核结论失效（同一事务、同一份留痕）
        _invalidate_audit_on_key_change(person, changes)

        person.save()
        # 问题 11 方案 B：仅显式传 kinds 时才执行身份增删
        if kind_list is not None:
            _apply_kinds(person, kind_list, old_kinds)
        if avatar:
            _bind_avatar(avatar, person)
        _log_change(
            person,
            enums.ChangeType.UPDATE,
            changes,
            reason=(payload.get("changeReason") or "").strip(),
        )
    # 重新查询：get_person 的 annotate（exists 快照）与 save 后的 updated_at
    # 都可能过期，返回值须反映建/删行后的真实合成 kinds 与新版本号
    return get_person(person_id, account)


def _change_entry(field: str, old, new) -> Optional[dict]:
    """变更条目（ChangeLog.field_changes 用）：无变化返回 None；
    date 等非 JSON 原生类型转 ISO 字符串。"""
    if old == new:
        return None
    old_out = old.isoformat() if hasattr(old, "isoformat") else old
    new_out = new.isoformat() if hasattr(new, "isoformat") else new
    return {"field": field, "old": old_out, "new": new_out}


def update_own_person(person_id: str, payload: dict):
    """PUT /me/profile：本人档案更新（字段集=MyProfileUpdate，前端实际提交集）。

    - 对象解析：服务端 person_id（不信任客户端任何 id 字段）；软删行 404 同 get_own_person；
    - version 必填乐观锁（与 /people 写链同规则：缺失/非法=400 统一校验错误，不匹配=409）；
    - 身份字段边界：kinds/schoolOrgId/phone/idNumber/avatarFileId 不在本人可改集
      （身份与归属变更走管理员链路；手机号/身份证按页面声明走复核更正）；
    - education：E1 持久化（未传保留原值、传值覆盖，留痕走 _change_entry）；
    - schoolName：不再接受——提交即 400 INVALID_ARGUMENT（批次 A 2026-10-28
      用户批准；响应中的 schoolName 仍按实际学校关联派生回显）；
    - coachCategory：接受不持久化（D2 在案），不回显；
    - 留痕复用 _log_change；审计走 API 层 audit_state。
    """
    person = people_queryset().filter(pk=parse_path_id(person_id)).first()
    if person is None:
        raise ApiError(NOT_FOUND)
    ensure_version(person, payload["version"])
    gender_value = _gender_in(payload.get("gender"), "gender")

    changes: list[dict] = []
    with transaction.atomic():
        new_name = payload["realName"].strip()
        entry = _change_entry("realName", person.full_name, new_name)
        if entry:
            changes.append(entry)
        person.full_name = new_name

        entry = _change_entry("gender", person.gender, gender_value)
        if entry:
            changes.append(entry)
        person.gender = gender_value

        entry = _change_entry("birthDate", person.birth_date, payload.get("birthDate"))
        if entry:
            changes.append(entry)
        person.birth_date = payload.get("birthDate")

        # education：未传 → 保留原值；传值（含空串）→ 覆盖写入（E1 持久化）
        education_value = payload.get("education")
        if education_value is not None:
            education_value = education_value.strip() or None
            entry = _change_entry("education", person.education, education_value)
            if entry:
                changes.append(entry)
            person.education = education_value

        # P0-4：关键字段变化使原审核结论失效（本人与管理员链路同规则，同一事务）
        _invalidate_audit_on_key_change(person, changes)

        person.save()
        _log_change(
            person,
            enums.ChangeType.UPDATE,
            changes,
            reason="本人更新档案",
        )
    # 重查取 save 后的 updated_at（派生 version 必须与持久化结果一致）
    return people_queryset().filter(pk=person.pk).first()


def confirm_own_person(person_id: str, version: int):
    """POST /me/profile-confirmation：本人确认档案，提交进入复核。

    语义依据链：create_person 初值 audit_status=DRAFT（本模块写链）+ 前端
    确认成功文案"档案已确认，等待管理员复核"（MyProfileView.vue:80）+
    AuditStatus.PENDING label"待审核" → 确认=DRAFT→PENDING（提交复核），
    而非直接通过（mock 直接置 APPROVED 与前端文案自相矛盾，不作依据）。
    - 绑定失效（未绑定/软删）→ 403 PROFILE_BINDING_REQUIRED（文档 §6.5 规则
      "只有已验证绑定的本人可确认"；错误码/状态映射/默认消息均已在
      common/errors 预留——与 myProfileGet 的 404 口径不同，按各自文档定稿）；
    - version 必填乐观锁（文档 ProfileConfirmation，同 /people 写链规则）；
    - 状态分支：
      DRAFT → PENDING（提交复核）；
      APPROVED → 409 STATE_CONFLICT（前端按钮 disabled=reviewStatus==='APPROVED'，
      重复确认无业务意义）；
      REJECTED → PENDING（P0-2 统一规则 2026-10-28 批准：本人修改后重新提交，
      reason 记"驳回后重新提交"，旧审核留痕保留不覆盖）；
      PENDING → 幂等成功（前端按钮该状态可点，重复提交无副作用：状态不变、
      不重复写 ChangeLog；实现选择在案）；
    - 留痕：状态变化写 _log_change（audit_status old/new）；审计走 API 层。
    """
    person = people_queryset().filter(pk=parse_path_id(person_id)).first()
    if person is None:
        raise ApiError(PROFILE_BINDING_REQUIRED)
    ensure_version(person, version)
    with transaction.atomic():
        # 行锁内重读：并发 confirm 串行化——后到者在锁内看到 PENDING 走幂等，
        # 不会同读 REJECTED 各自重提（2026-10-28 防重复重提加固）。
        person = people_queryset().select_for_update().get(pk=person.pk)
        current = person.audit_status
        if current == enums.AuditStatus.APPROVED:
            raise ApiError(STATE_CONFLICT, "档案已复核，无需重复确认")
        if current == enums.AuditStatus.PENDING:
            return person  # 幂等：已处于待复核，无状态变化
        # REJECTED → PENDING（P0-2 统一规则 2026-10-28 批准）：本人修改后重新提交；
        # 旧 AUDIT/UPDATE 留痕为 append-only，不覆盖、不删除。
        # 防滥用加固（2026-10-28 批准）：重提必须以"最近一次驳回之后发生了有效
        # 资料修改"为前提。证据只取服务端 PersonChangeLog——update_own_person
        # 仅在值实际变化时生成 field_changes 条目（无变化时日志 field_changes
        # 为 None，自动不构成证据）；confirm 自身的 audit_status-only 重提日志
        # 亦不构成证据。不信任前端标记、提交次数或时间戳。
        last_audit = (
            PersonChangeLog.objects.filter(
                person_id=person.pk, change_type=enums.ChangeType.AUDIT
            )
            .order_by("-created_at", "-id")
            .first()
        )
        if last_audit is not None:
            recent_rows = PersonChangeLog.objects.filter(
                person_id=person.pk,
                change_type=enums.ChangeType.UPDATE,
                created_at__gt=last_audit.created_at,
            ).values_list("field_changes", flat=True)
            has_valid_change = any(
                (entry or {}).get("field") != "audit_status"
                for changes in recent_rows
                for entry in (changes or [])
            )
            if not has_valid_change:
                raise ApiError(
                    STATE_CONFLICT,
                    "驳回后未发生有效资料修改，请先修改档案再重新提交",
                )
        person.audit_status = enums.AuditStatus.PENDING
        person.save()
        _log_change(
            person,
            enums.ChangeType.UPDATE,
            [
                {
                    "field": "audit_status",
                    "old": str(current),
                    "new": enums.AuditStatus.PENDING.value,
                }
            ],
            reason=(
                "驳回后重新提交"
                if current == enums.AuditStatus.REJECTED
                else "本人确认档案"
            ),
        )
    # 重查取 save 后的 updated_at（派生 version 与持久化结果一致）
    return people_queryset().filter(pk=person.pk).first()


def review_person(person_id: str, payload: dict, account):
    """POST /people/{id}/review（P0-2，2026-10-28）：学校端档案审核。

    业务依据（2026-10-28 统一规则 + D1 口径）：
    - 权限点 personReview=UNIVERSITY（学校端是人员资格与履历的最终审核方；
      ADMIN 不因数据范围获得审核权；CITY/PROVINCE_REVIEWER 只审报名；
      TEAM 维护权限≠审核权限）——由 API 层 require() 强制，本函数只裁决
      数据范围；
    - 数据范围：UNIVERSITY 所属组织子树（scope_org_codes ↔ Person.school.code
      编码映射，同材料域 D1/D5 已验证口径）；无有效组织范围（无绑定/绑定
      已软删）默认拒绝不退化全库；范围外档案不可见（404，不泄露存在性，
      同 get_person A′ 口径）。不复用 apply_person_scope（该函数当前仅放行
      ADMIN，属既有 persons 查询接口 A′ 一批口径；对 UNIVERSITY 开放
      personList/personGet/personSearch 影响既有接口面，列为待决事项，
      不随本接口一并放开）；
    - 状态机：仅 PENDING → APPROVED/REJECTED；DRAFT（尚未提交复核，走
      profileConfirm）/APPROVED（重复审核）/REJECTED（重新提交链路 B3
      待业务确认，保守拒绝）→ 409 STATE_CONFLICT；
    - REJECT 必填 reason（同材料域审核口径）；
    - 留痕复用既有 PersonChangeLog（change_type=AUDIT，field_changes 记
      audit_status old/new，reason 记审核意见），不新增模型/迁移；
      changed_by 为 BigInteger 逻辑引用恒空（D4 待批），操作人追溯由通用
      AuditLog（API 层 audit_state）承载；
    - 状态变更与留痕同一事务：留痕失败 → 状态整体回滚，不留半完成审核。
    """
    from apps.common.scopes import scope_org_codes

    codes = scope_org_codes(account)
    if not codes:
        raise ApiError(
            DATA_SCOPE_DENIED,
            "当前账号无有效组织范围，禁止审核人员档案",
        )
    # P1-5 防自审：对象读取、版本校验、状态校验、自审审计校验、状态变更与留痕
    # 全部在同一事务内执行，select_for_update 锁定待审核人员行，防止并发
    # 创建/修改与审核之间的 TOCTOU 竞态。
    with transaction.atomic():
        person = (
            people_queryset()
            .select_for_update()
            .filter(pk=parse_path_id(person_id), school__code__in=codes)
            .first()
        )
        if person is None:
            raise ApiError(NOT_FOUND)
        ensure_version(person, payload["version"])
        current = person.audit_status
        if current != enums.AuditStatus.PENDING:
            raise ApiError(
                STATE_CONFLICT,
                f"档案当前状态为 {current}，仅待审核（PENDING）档案可处理",
            )
        # 防自审：纳入 person.create / person.update / person.self_update；
        # person.self_confirm 仅改 audit_status，不作为创建或修改证据。
        assert_not_self_review(
            account,
            "Person",
            str(person.pk),
            ("person.create", "person.update", "person.self_update"),
        )
        decision = payload["decision"]
        reason = (payload.get("reason") or "").strip()
        if decision == "REJECT" and not reason:
            raise ApiError(
                INVALID_ARGUMENT,
                "驳回档案必须填写审核意见",
                [{"field": "reason", "reason": "REJECT 时必填"}],
            )
        person.audit_status = (
            enums.AuditStatus.APPROVED
            if decision == "APPROVE"
            else enums.AuditStatus.REJECTED
        )
        person.save()
        _log_change(
            person,
            enums.ChangeType.AUDIT,
            [
                {
                    "field": "audit_status",
                    "old": str(current),
                    "new": str(person.audit_status),
                }
            ],
            reason=reason or "审核通过",
        )
    # 重查取 save 后的 updated_at（派生 version 与持久化结果一致）
    return people_queryset().filter(pk=person.pk).first()


def list_own_teams(person_id: str) -> list[dict]:
    """GET /me/teams：本人现役（CURRENT）球员/教练关系的球队，数组直出。

    契约依据（前端实码优先，契约清单 B4）：
    - meApi.teams() 无任何请求参数、响应 Team[]（zuqiu src/api/index.ts:69）；
      权威文档原写分页 TeamPage+active 参数，按前端实现数组直出；
    - 范围=正式关系 TeamMember/TeamCoach 且 status=CURRENT：mock 将"自报归属
      VERIFIED"并入（adapter.ts:340-343），冻结模型无申请表（B5），不采用；
      EXITED 历史关系属履历（/me/team-affiliations 域），不进本接口；
    - UNIVERSITY 全校分支（mock adapter.ts:335-337 按 orgId 过滤全校球队）
      依赖 org→school 映射（D1/方案A′ 未批），暂停实现，权限点仅 ATHLETE/COACH；
    - 字段适配（前端 Team 类型 types.ts:185-206 vs 冻结 Team 模型）：
      无承载且页面未消费的字段不输出（shortName/campusName/gender/orgId/
      cityOrgId）；flagUrl/emblemUrl/groupPhotoUrl 属受控下载契约（无公开
      直链，storage 契约 5），不输出 url；status 直出 audit_status（Q2 审核
      流程不启用默认 APPROVED；DRAFT/REJECTED 越出前端类型域，与人员
      reviewStatus 同款处理）；version 派生 updated_at 微秒（B4 同款）；
      coachName=球队现役（CURRENT）主教练（HEAD_COACH）全名合成。
    """
    pid = int(person_id)
    team_ids = set(
        TeamMember.objects.filter(
            person_id=pid, status=enums.RelationStatus.CURRENT
        ).values_list("team_id", flat=True)
    ) | set(
        TeamCoach.objects.filter(
            person_id=pid, status=enums.RelationStatus.CURRENT
        ).values_list("team_id", flat=True)
    )
    if not team_ids:
        return []
    head_coach_subq = TeamCoach.objects.filter(
        team=OuterRef("pk"),
        status=enums.RelationStatus.CURRENT,
        duty=enums.CoachDuty.HEAD_COACH,
    ).values("person__full_name")[:1]
    rows = (
        Team.objects.filter(pk__in=team_ids, is_deleted=False)
        .select_related("school", "region")
        .annotate(
            member_count=Count(
                "teammember",
                filter=Q(teammember__status=enums.RelationStatus.CURRENT),
            ),
            head_coach_name=Subquery(head_coach_subq),
        )
        .order_by("created_at", "id")
    )
    return [_team_brief_out(row) for row in rows]


def _team_brief_out(team) -> dict:
    """/me/teams 单队输出（dict 直出，EnvelopeOut.data=Any 不进 components）。"""
    return {
        "id": str(team.pk),
        "name": team.name,
        "category": team.category_code,
        "schoolName": team.school.name if team.school_id else None,
        "cityName": team.region.name if team.region_id else None,
        "intro": team.introduction,
        "coachName": team.head_coach_name,
        "memberCount": team.member_count,
        "status": team.audit_status,
        "version": int(team.updated_at.timestamp() * 1_000_000),
        "createdAt": team.created_at.isoformat(),
    }


def delete_person(person_id: str, request_version, account) -> None:
    """DELETE /people/{id}：软删（版本校验 + 现役球队/教练关联保护）。"""
    person = get_person(person_id, account)
    ensure_version(person, request_version)
    if TeamMember.objects.filter(person=person, status=enums.RelationStatus.CURRENT).exists():
        raise ApiError(
            STATE_CONFLICT,
            "人员仍在现役球队名单中，不能删除",
        )
    if TeamCoach.objects.filter(person=person).exists():
        # 保守保护：教练关联行不含可辨状态口径前，存在即挡（待确认后放宽）
        raise ApiError(
            STATE_CONFLICT,
            "人员存在教练关联记录，不能删除",
        )
    with transaction.atomic():
        person.is_deleted = True
        person.deleted_at = timezone.now()
        person.save()
        _log_change(
            person,
            enums.ChangeType.DISABLE,
            [{"field": "isDeleted", "old": False, "new": True}],
            reason="软删除",
        )


# ---------------------------------------------------------------- 遴选档案聚合（§6.8，批次 D-1）

def _reject_undeployed_event_filters(payload: dict) -> None:
    """eventId/groupId 筛选行为待决策（D-A）：契约仅定义参数、未规定过滤规则，
    报名/比赛读模型未落地（冻结 22 表无此域）。非空一律 400 明确拒绝，
    不默认空集、不虚构过滤语义。"""
    for name in ("eventId", "groupId"):
        if (payload.get(name) or "").strip():
            raise ApiError(
                INVALID_ARGUMENT,
                f"{name} 筛选依赖报名/比赛读模型，尚未开放（待决策项 D-A）",
                field_errors([(name, "暂不支持")]),
            )


def dossier_search_queryset(payload: dict, account) -> QuerySet:
    """POST /selection-dossiers/search 查询构建（§6.8 L1736-1756 + §7 DossierSearch）。

    复用：people_queryset（软删排除+档案行注解）、apply_person_scope（一期仅
    ADMIN，D5 口径）、list_people 的 -id 排序先例、create_person 的
    lookup_hash 受控检索链路；teamId=现役 TeamMember 关系（CURRENT 口径同
    /me/teams）。检索资格：文档 L4708"仅运动员遴选档案受控检索，不允许借用
    教练员或其他人员过滤字段扩大数据范围"→ 强制 athlete_profile_exists=True。
    """
    _reject_undeployed_event_filters(payload)
    queryset = apply_person_scope(people_queryset(), account).filter(
        athlete_profile_exists=True
    )
    name = (payload.get("athleteName") or "").strip()
    if name:
        queryset = queryset.filter(full_name__icontains=name)
    id_number = (payload.get("idNumber") or "").strip().upper()
    if id_number:
        queryset = queryset.filter(id_card_hash=crypto.lookup_hash(id_number))
    team_id = payload.get("teamId")
    if team_id:
        queryset = queryset.filter(
            pk__in=TeamMember.objects.filter(
                team_id=parse_path_id(str(team_id), "teamId"),
                status=enums.RelationStatus.CURRENT,
            ).values("person_id")
        )
    return queryset.order_by("-id")


def dossier_out(person, account) -> dict:
    """Person(+注解列) → CandidateDossier（§7 L4692-4704）。

    - athlete：person_out（脱敏同源，idNumber 永不回显）；
    - teams：list_own_teams 现役正式关系（批次 D-1 指令"复用现有正式球队
      关系"，与 /me/teams 同一业务规则；EXITED 历史属履历域不并入，D-D）；
    - materials：materials.services.material_list(PERSON, ownerId)（批次 C
      未软删查询）+ materials.schemas.material_out（零第二套序列化）；逐行
      调用与 /me/teams 同款（pageSize≤100 已限，批量优化待数据量增长）；
    - enrollments/matchRecords：报名/比赛读模型未落地，契约字段可选
      （§7 L4700-4701 必填=否）→ 空数组=无读模型数据，非伪造记录；
    - id：person 主键字符串（无独立档案表，待决策项 D-B）；
    - version：person_version 微秒派生（B4 同款，待决策项 D-C）。
    """
    from .schemas import person_out, person_version
    from apps.materials.schemas import material_out
    from apps.materials.services import material_list as _material_rows

    materials = [
        material_out(row, kind)
        for row, kind in _material_rows(
            {"ownerType": "PERSON", "ownerId": str(person.pk)}, account
        )
    ]
    return {
        "id": str(person.pk),
        "athlete": person_out(person),
        "teams": list_own_teams(person.pk),
        "materials": materials,
        "enrollments": [],
        "matchRecords": [],
        "version": person_version(person),
        "createdAt": person.created_at,
        "updatedAt": person.updated_at,
    }


def dossier_get(athlete_id: str, event_id: Optional[str], account) -> dict:
    """GET /selection-dossiers/{athleteId}（§6.8 L1758-1773）。

    非运动员（无 AthleteProfile 行）/软删/不存在 → 404（"仅运动员遴选档案"
    资格口径同 dossier_search）；eventId 非空 → 400（D-A 待决策）。
    """
    if (event_id or "").strip():
        raise ApiError(
            INVALID_ARGUMENT,
            "eventId 筛选依赖报名读模型，尚未开放（待决策项 D-A）",
            field_errors([("eventId", "暂不支持")]),
        )
    person = (
        apply_person_scope(people_queryset(), account)
        .filter(pk=parse_path_id(athlete_id), athlete_profile_exists=True)
        .first()
    )
    if person is None:
        raise ApiError(NOT_FOUND, "遴选档案不存在")
    return dossier_out(person, account)
