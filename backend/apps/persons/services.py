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

from datetime import date, datetime
from typing import Optional

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Count, Exists, JSONField, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from apps.common import crypto, enums
from apps.common.enums import RegionLevel
from apps.common.errors import (
    BUSINESS_VALIDATION_FAILED,
    DATA_SCOPE_DENIED,
    DUPLICATE_RESOURCE,
    FORBIDDEN,
    INVALID_ARGUMENT,
    NOT_FOUND,
    PROFILE_BINDING_REQUIRED,
    STATE_CONFLICT,
    VERSION_CONFLICT,
    ApiError,
    field_errors,
)
from apps.common.permissions import ATHLETE, COACH, TEAM
from apps.common.ids import new_id
from apps.common.scopes import account_org_code, is_global_admin, visible_school_codes
from apps.accounts.models import Account, AuditLog, Organization
from apps.notices.models import push_message
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
        data["name"], data.get("code") or None, region, exclude_id=school.pk
    )
    school.name = data["name"]
    school.code = data.get("code") or None
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
            # schoolOrgId 输出：School.code → 组织节点主键（免 N+1；
            # Organization.code 唯一，至多一命中）
            school_org_id_val=Subquery(
                Organization.objects.filter(code=OuterRef("school__code")).values("pk")[:1]
            ),
            # education/coachCategory 输出：承载在扩展档案 ext_json（预留扩展列），
            # 免 N+1 一次取回（无档案行 → NULL → 输出 None）
            athlete_ext_val=Subquery(
                AthleteProfile.objects.filter(person=OuterRef("pk")).values("ext_json")[:1],
                output_field=JSONField(),
            ),
            coach_ext_val=Subquery(
                CoachProfile.objects.filter(person=OuterRef("pk")).values("ext_json")[:1],
                output_field=JSONField(),
            ),
        )
    )


def apply_person_scope(queryset: QuerySet, account) -> QuerySet:
    """人员数据范围过滤（集中一处，读接口统一复用）。

    - 无组织的全局管理员：不过滤；
    - roles 含 TEAM：仅本队（Organization.code=team_no 且未软删的 Team）成员
      关系内人员，CURRENT/EXITED 均视为历史可见；队伍或组织 code 缺失 → 403；
    - roles 含 ATHLETE/COACH：仅账号绑定的本人；未绑定 → 空集（个人中心空态，
      不拒绝）；
    - 其余（UNIVERSITY/CITY/PROVINCE_REVIEWER/挂组织的 ADMIN）：按组织子树
      内学校类组织 code 过滤 school（org.code=School.code）；可见学校集合为空
      → 403 DATA_SCOPE_DENIED，不静默放行全量。
    """
    roles = set(getattr(account, "roles", ()) or ())
    if is_global_admin(account):
        return queryset
    if TEAM in roles:
        code = account_org_code(account)
        team = (
            Team.objects.filter(team_no=code, is_deleted=False).first() if code else None
        )
        if team is None:
            raise ApiError(
                DATA_SCOPE_DENIED, "球队账号未绑定有效球队组织，无法确定人员数据范围"
            )
        person_ids = TeamMember.objects.filter(team=team).values("person_id")
        return queryset.filter(pk__in=person_ids)
    if roles & {ATHLETE, COACH}:
        raw = str(getattr(account, "person_id", "") or "").strip()
        try:
            person_pk = int(raw) if raw else 0
        except ValueError:
            person_pk = 0
        if person_pk <= 0:
            return queryset.none()
        return queryset.filter(pk=person_pk)
    codes = visible_school_codes(account)
    if not codes:
        raise ApiError(DATA_SCOPE_DENIED, "当前账号无可访问的学校数据范围")
    return queryset.filter(school__code__in=codes)


def verify_person_owner(account, owner_id) -> None:
    """PERSON 附件归属校验器（经 PersonsConfig.ready() 注册进 attachments）。

    归属裁决与 apply_person_scope 同口径：
    - 归属记录不存在（人员不存在或已软删）→ 404，默认拒绝；
    - 全局管理员 / 组织学校范围内 / TEAM 本队成员 / 账号绑定本人 → 放行；
    - 其余（含无可见学校范围、TEAM 未绑定球队、非本人）→ 403 DATA_SCOPE_DENIED。
    """
    raw = str(owner_id or "").strip()
    if not raw:
        raise ApiError(DATA_SCOPE_DENIED, "文件归属对象未确定，暂无法校验归属")
    person = Person.objects.filter(
        pk=parse_path_id(raw, "ownerId"), is_deleted=False
    ).first()
    if person is None:
        raise ApiError(NOT_FOUND, "归属人员不存在或已删除")
    if not apply_person_scope(Person.objects.filter(pk=person.pk), account).exists():
        raise ApiError(DATA_SCOPE_DENIED, "人员不在当前账号数据范围内")


def _ensure_school_in_scope(account, school) -> None:
    """写链：提交的所属学校必须在账号人员数据范围内（与 apply_person_scope 同口径）。

    - 全局管理员放行；
    - TEAM 账号仅允许本队所属学校；
    - 其余按组织子树内学校 code 判定；无可见学校范围 → 403。
    """
    if is_global_admin(account):
        return
    roles = set(getattr(account, "roles", ()) or ())
    if TEAM in roles:
        code = account_org_code(account)
        team = (
            Team.objects.filter(team_no=code, is_deleted=False).first() if code else None
        )
        if team is None:
            raise ApiError(
                DATA_SCOPE_DENIED, "球队账号未绑定有效球队组织，无法确定人员数据范围"
            )
        if school is None or team.school_id != school.pk:
            raise ApiError(DATA_SCOPE_DENIED, "提交的所属学校不在本队范围内")
        return
    codes = visible_school_codes(account)
    if not codes:
        raise ApiError(DATA_SCOPE_DENIED, "当前账号无可访问的学校数据范围")
    if school is None or school.code not in codes:
        raise ApiError(DATA_SCOPE_DENIED, "提交的所属学校不在当前账号数据范围内")


def _get_person_for_write(person_id: str, account) -> Person:
    """写链对象解析：不存在/软删 → 404；存在但不在范围内 → 403。

    与读详情（get_person）的 404 口径不同：写操作显式区分"不可见"与"不存在"，
    拒绝越界写入且不通过接口层放行绕过服务层校验。
    """
    person = people_queryset().filter(pk=parse_path_id(person_id)).first()
    if person is None:
        raise ApiError(NOT_FOUND)
    if not apply_person_scope(Person.objects.filter(pk=person.pk), account).exists():
        raise ApiError(DATA_SCOPE_DENIED, "人员不在当前账号数据范围内")
    return person


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
    teamId 依赖成员关系查询，待球队读服务就绪后一并补充）。
    """
    kind = filters.get("kind")
    _check_enum(kind, tuple(enums.PersonCategory.values), "kind")
    review_status = filters.get("reviewStatus")
    _check_enum(review_status, tuple(enums.AuditStatus.values), "reviewStatus")
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
    return queryset.order_by("-id")


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


def coach_qualification(person) -> str:
    """教练资格派生（问题#7，2026-10-31 裁定 + 2026-10-31 二轮修正）：四态只读派生。

    零落库零迁移，不新增第五种状态：
    - MISSING：无教练扩展档案行（非教练/未建教练档案），或证书编号与
      有效期均未登记（certificate_no 与 certificate_expire_date 皆空）；
    - UNKNOWN：证书信息不完整——有证书编号但未登记有效期，或仅有有效期
      而未登记证书编号（二轮修正：原先"有日期无证号"归 MISSING，
      按裁定改为 UNKNOWN，视为证书信息不完整而非无证书）；
    - VALID / EXPIRED：证书编号已登记，以当前业务日期为基准
      （settings.TIME_ZONE=Asia/Shanghai，timezone.localdate() 随配置生效）：
      certificate_expire_date >= 今日 → VALID（有效期至当日含当日有效，
      次日零点起过期），< 今日 → EXPIRED；
    - 派生字段只读：不新增数据库字段、不写 CoachProfile、不参与
      audit_status 与报名资格（enrollments.qualification_status）逻辑。
    """
    try:
        profile = person.coach_profile
    except ObjectDoesNotExist:
        return "MISSING"
    cert_no = (profile.certificate_no or "").strip()
    expire = profile.certificate_expire_date
    if not cert_no:
        # 证号+有效期皆空=无证书 MISSING；仅有有效期=信息不完整 UNKNOWN
        return "UNKNOWN" if expire is not None else "MISSING"
    if expire is None:
        return "UNKNOWN"
    return "VALID" if expire >= timezone.localdate() else "EXPIRED"


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
    """方案C（2026-10-24 用户拍板：前端多选为准）：kinds 数组 1..2 项。

    值域校验+去重，不静默丢弃；person_category 存主类别（首项），
    完整类别集合由扩展档案行存在性承载（见 _apply_kinds/_synthesized_kinds）。
    """
    if not isinstance(kinds, list) or not kinds:
        raise ApiError(
            INVALID_ARGUMENT,
            "kinds 必填（1-2 项）",
            field_errors([("kinds", "至少一项")]),
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
    """schoolOrgId（Organization 节点 ID）→ School 主档（2026-10-25 契约确认）。

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
    school = _resolve_school(payload.get("schoolOrgId"))
    _ensure_school_in_scope(account, school)
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
        # education/coachCategory 落扩展档案 ext_json（须在档案行建好之后）
        save_profile_ext_values(
            person,
            {
                EDUCATION_EXT_KEY: _ext_text(payload.get("education")),
                COACH_CATEGORY_EXT_KEY: _ext_text(payload.get("coachCategory")),
            },
        )
        if avatar:
            _bind_avatar(avatar, person)
        _log_change(person, enums.ChangeType.CREATE, None)
        # 风险5：person.create 审计与建档同事务（字段对齐中间件 _write 实码）
        _audit_in_transaction(
            "person.create",
            person.pk,
            getattr(account, "pk", "") or "",
            f"创建人员 {person.full_name}({person.person_no})",
        )
    return person


def update_person(person_id: str, payload: dict, account):
    """PUT /people/{id}：版本校验 → 必填字段全量替换 + 可选字段缺省保留 → diff 留痕。

    idNumber 不接受（身份证变更走更正流程，一期不开放）；
    education/coachCategory 落扩展档案 ext_json（预留扩展列，未传=保留原值）；
    问题#4：复审字段（姓名/出生日期/类别/学校/教练类别）实际变更时，
    APPROVED 档案自动退回 PENDING（_apply_re_review_on_change）。
    """
    person = _get_person_for_write(person_id, account)
    ensure_version(person, payload["version"])
    kind_list = _kind_list(payload.get("kinds"))
    old_kinds = _current_kinds(person)
    old_ext = read_profile_ext_values(person)
    gender_value = _gender_in(payload.get("gender"), "gender")
    primary_category = kind_list[0]

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

        birth_value = _coerce_date(payload.get("birthDate"))
        track("birthDate", person.birth_date, birth_value)
        person.birth_date = birth_value

        track("kinds", old_kinds, kind_list)
        track("personCategory", person.person_category, primary_category)
        person.person_category = primary_category

        # 可选字段：请求未传 → 保留原值（编辑表单无对应输入，缺省清空=静默丢数据）
        school = _resolve_school(payload.get("schoolOrgId"))
        if school is not None:
            _ensure_school_in_scope(account, school)
            track("school", person.school_id, school.pk)
            person.school = school
        avatar = _resolve_avatar_file(payload.get("avatarFileId"))
        if avatar is not None:
            track("photoFileId", person.photo_file_id, avatar)
            person.photo_file_id = avatar
        phone = (payload.get("phone") or "").strip() or None
        if phone:
            track("phoneMasked", person.phone_masked, crypto.mask_phone(phone))
            person.__dict__.update(_phone_fields(phone))

        person.save()
        _apply_kinds(person, kind_list, old_kinds)
        ext_updates = {
            EDUCATION_EXT_KEY: _ext_text(payload.get("education")),
            COACH_CATEGORY_EXT_KEY: _ext_text(payload.get("coachCategory")),
        }
        for key, value in ext_updates.items():
            if value is not None:
                track(key, old_ext.get(key), value)
        save_profile_ext_values(person, ext_updates)
        if avatar:
            _bind_avatar(avatar, person)
        # 问题#4：复审字段实际变更 → APPROVED 退回 PENDING（与变更同事务）；
        # 退回时 reason 追加明确后缀 + 站内信通知本人/学校端（遗留风险1 落地）
        demoted = _apply_re_review_on_change(person, changes)
        reason = (payload.get("changeReason") or "").strip()
        if demoted:
            reason = f"{reason}（复审字段变更，退回待审）".strip()
        _log_change(
            person,
            enums.ChangeType.UPDATE,
            changes,
            reason=reason,
        )
        if demoted:
            _notify_re_review_return(person)
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


def _apply_re_review_on_change(person, changes) -> bool:
    """问题#4（2026-10-31 裁定）：复审字段实际变更 → APPROVED 自动退回 PENDING。

    审核结论不因静默修改过期：与材料域"仅 PENDING 可改"哲学对齐，
    档案侧以"改后重审"替代"禁止修改"（本人纠错不受阻，学校端重新把关）。

    - 字段集：姓名/出生日期/身份类别(kinds)/主类别/学校/教练类别；
      education 不触发（与问题清单"至少应算"口径一致，资格核心是教练类别）；
    - 幂等：changes 只含 old!=new 的实际变更，值未变（表单全量回传）不触发；
    - kinds 仅顺序变化（集合相同）不触发（遗留风险2 补充裁定：类别增删才是
      资格实质变更，此时 personCategory=kinds 首项的漂移同步豁免）；
    - 仅 APPROVED 退回：DRAFT/PENDING 本在待审轨道，REJECTED 待本人重提；
      档案状态机无 CLOSED 态（AuditStatus 仅 DRAFT/PENDING/APPROVED/REJECTED，
      2026-10-31 已核实枚举、DDL 注释与接口文档）——原报告"遗留风险3：CLOSED
      档案冻结"前提不成立，REJECTED 改字段维持原状态即现行为（纠错走本人
      重提链路，问题#2 已实现）；
    - 退回条目（auditStatus old/new）并入同一条 UPDATE ChangeLog，调用点在
      reason 追加"复审字段变更，退回待审"后缀，并经 _notify_re_review_return
      发站内信通知本人与学校端（遗留风险1 落地，复用 notices.push_message）。
    调用点：update_person / update_own_person 的 save_profile_ext_values 之后、
    _log_change 之前（须在事务内，退回与变更同事务原子落库）。
    """
    re_review_fields = {
        "realName",
        "birthDate",
        "kinds",
        "personCategory",
        "school",
        "coachCategory",
    }
    triggered = {c["field"] for c in changes if c["field"] in re_review_fields}
    kinds_entry = next((c for c in changes if c["field"] == "kinds"), None)
    if kinds_entry and set(kinds_entry["old"] or []) == set(kinds_entry["new"] or []):
        triggered.discard("kinds")
        triggered.discard("personCategory")
    if person.audit_status != enums.AuditStatus.APPROVED:
        return False
    if not triggered:
        return False
    changes.append(
        {
            "field": "auditStatus",
            "old": enums.AuditStatus.APPROVED.value,
            "new": enums.AuditStatus.PENDING.value,
        }
    )
    person.audit_status = enums.AuditStatus.PENDING
    person.save()
    return True


def _notify_re_review_return(person) -> None:
    """复审退回站内信（遗留风险1 落地）：通知本人与学校端，复用 notices.push_message。

    - 接收人：绑定该档案的启用账号（Account.person_id）+ 档案归属学校的组织
      启用账号（Organization.code=school.code，code 全局唯一）——与 personReview
      "学校端把关"口径一致；无绑定账号/无学校归属/学校无组织时静默跳过；
    - 双重身份去重：既绑定档案又属学校组织的账号只收一条（本人口径优先）；
    - 文案不含敏感字段明细（身份证/手机号不进消息体），字段明细走变更留痕；
    - 调用点在事务内：消息与退回同事务原子落库（退回回滚则消息不残留）；
    - 消息经现成 /me/messages 列表/详情/已读接口触达（notices 契约不变）。
    """
    own_ids = set(
        Account.objects.filter(person_id=str(person.pk), enabled=True).values_list(
            "pk", flat=True
        )
    )
    school_ids: set[str] = set()
    if person.school_id:
        org = Organization.objects.filter(code=person.school.code).first()
        if org is not None:
            school_ids = set(
                Account.objects.filter(
                    organization_id=org.pk, enabled=True
                ).values_list("pk", flat=True)
            ) - own_ids
    if not own_ids and not school_ids:
        return
    label = (
        "运动员"
        if person.person_category == enums.PersonCategory.ATHLETE.value
        else "教练员"
    )
    for account_id in own_ids:
        push_message(
            account_id,
            "人员档案已退回待审核",
            "您的人员档案因信息变更已自动退回待审核，请等待重新审核。",
            "PERSON",
            str(person.pk),
        )
    for account_id in school_ids:
        push_message(
            account_id,
            "本校人员档案退回待审核",
            f"{label} {person.full_name} 的档案因信息变更已退回待审核，请及时复核。",
            "PERSON",
            str(person.pk),
        )


def _coerce_date(value):
    """ISO 字符串 → date（写链同值回传的幂等比较用：date 与 str 直接比较恒
    不等，会把未变化的出生日期误判为变更并在问题#4 下误触发复审）；
    非字符串/非法格式原样返回，走原有赋值/报错路径。"""
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return value
    return value


# ---- education / coachCategory 承载（扩展档案 ext_json 预留扩展列）----------
# 冻结 DDL 无独立列；person_athlete_profile / person_coach_profile 的 ext_json
# 列注释即"预留扩展"，按 person 已有档案行读写，不新增列、不动 DDL
# （5.txt 问题一：前端提交后刷新不回显）。
EDUCATION_EXT_KEY = "education"
COACH_CATEGORY_EXT_KEY = "coachCategory"
EXT_TEXT_KEYS: tuple[str, ...] = (EDUCATION_EXT_KEY, COACH_CATEGORY_EXT_KEY)


def _ext_text(value) -> Optional[str]:
    """扩展文本归一：None/空白 → None（未传=保留原值，与 /people 可选字段同口径）。"""
    if value is None:
        return None
    return str(value).strip() or None


def _profile_rows(person) -> list:
    """person 已存在的扩展档案行（方案C：行存在性=类别事实源，各 0..1 行）。"""
    return [
        row
        for row in (
            AthleteProfile.objects.filter(person=person).first(),
            CoachProfile.objects.filter(person=person).first(),
        )
        if row is not None
    ]


def read_profile_ext_values(person) -> dict:
    """读 education/coachCategory（先运动员档案后教练档案，取首个非空）。"""
    values: dict = {}
    for row in _profile_rows(person):
        data = row.ext_json if isinstance(row.ext_json, dict) else {}
        for key in EXT_TEXT_KEYS:
            if key not in values and data.get(key) not in (None, ""):
                values[key] = data[key]
    return values


def save_profile_ext_values(person, updates: dict) -> None:
    """把 education/coachCategory 落到扩展档案 ext_json。

    - 只写请求明确给出的键（None=未传→保留原值，不清空）；
    - 无对应档案行的键无处承载则跳过（不新建行：grade_level 为冻结 NOT NULL 占位列）；
    - 一人两类别时两个档案行都写，保证任一档案链读回一致。
    """
    pending = {k: v for k, v in updates.items() if v is not None}
    if not pending:
        return
    for row in _profile_rows(person):
        data = dict(row.ext_json) if isinstance(row.ext_json, dict) else {}
        if any(data.get(k) != v for k, v in pending.items()):
            data.update(pending)
            row.ext_json = data
            row.save(update_fields=["ext_json", "updated_at"])


def _ensure_own_school_name(person, submitted) -> None:
    """本人档案 schoolName 为派生只读字段（=person.school.name）。

    前端表单会原样回传 → 放行；试图改成别的值 → 422 明确业务错误。
    修复"200 OK 但字段未保存"（5.txt 问题一）。
    """
    if submitted is None:
        return
    current = person.school.name if person.school_id else ""
    if str(submitted).strip() == current:
        return
    raise ApiError(
        BUSINESS_VALIDATION_FAILED,
        "学校归属由档案所属学校派生，本人不可直接修改",
        field_errors([("schoolName", "不可编辑：需变更学校归属请联系管理员调整档案所属学校")]),
    )


def update_own_person(person_id: str, payload: dict):
    """PUT /me/profile：本人档案更新（字段集=MyProfileUpdate，前端实际提交集）。

    - 对象解析：服务端 person_id（不信任客户端任何 id 字段）；软删行 404 同 get_own_person；
    - version 必填乐观锁（与 /people 写链同规则：缺失/非法=400 统一校验错误，不匹配=409）；
    - 身份字段边界：kinds/schoolOrgId/phone/idNumber/avatarFileId 不在本人可改集
      （身份与归属变更走管理员链路；手机号/身份证按页面声明走复核更正）；
    - schoolName：派生只读（=person.school.name）。原值回传放行；改成别值 → 422
      业务错误（不再"200 OK 但字段未保存"，5.txt 问题一）；
    - education/coachCategory：落扩展档案 ext_json（预留扩展列，不动 DDL），
      未传=保留原值；读回见 person_out；
    - 问题#4：复审字段（姓名/出生日期/教练类别）实际变更时，APPROVED 档案
      自动退回 PENDING（_apply_re_review_on_change；本人入口触发面为字段集子集）；
    - 留痕复用 _log_change；审计走 API 层 audit_state。
    """
    person = people_queryset().filter(pk=parse_path_id(person_id)).first()
    if person is None:
        raise ApiError(NOT_FOUND)
    ensure_version(person, payload["version"])
    gender_value = _gender_in(payload.get("gender"), "gender")
    _ensure_own_school_name(person, payload.get("schoolName"))
    ext_updates = {
        EDUCATION_EXT_KEY: _ext_text(payload.get("education")),
        COACH_CATEGORY_EXT_KEY: _ext_text(payload.get("coachCategory")),
    }
    old_ext = read_profile_ext_values(person)

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

        birth_value = _coerce_date(payload.get("birthDate"))
        entry = _change_entry("birthDate", person.birth_date, birth_value)
        if entry:
            changes.append(entry)
        person.birth_date = birth_value

        person.save()
        for key, value in ext_updates.items():
            entry = _change_entry(key, old_ext.get(key), value if value is not None else old_ext.get(key))
            if entry:
                changes.append(entry)
        save_profile_ext_values(person, ext_updates)
        # 问题#4：复审字段实际变更 → APPROVED 退回 PENDING（与变更同事务；
        # 退回 reason 追加后缀 + 站内信通知，遗留风险1 同 update_person）
        demoted = _apply_re_review_on_change(person, changes)
        _log_change(
            person,
            enums.ChangeType.UPDATE,
            changes,
            reason="本人更新档案（复审字段变更，退回待审）" if demoted else "本人更新档案",
        )
        if demoted:
            _notify_re_review_return(person)
    # 重查取 save 后的 updated_at（派生 version 必须与持久化结果一致）
    return people_queryset().filter(pk=person.pk).first()


def confirm_own_person(person_id: str, version: int, account):
    """POST /me/profile-confirmation：本人确认档案，提交进入复核。

    语义依据链：create_person 初值 audit_status=DRAFT（本模块写链）+ 前端
    确认成功文案"档案已确认，等待管理员复核"（MyProfileView.vue:80）+
    AuditStatus.PENDING label"待审核" → 确认=DRAFT→PENDING（提交复核），
    而非直接通过（mock 直接置 APPROVED 与前端文案自相矛盾，不作依据）。
    - 绑定失效（未绑定/软删）→ 403 PROFILE_BINDING_REQUIRED（文档 §6.5 规则
      "只有已验证绑定的本人可确认"；错误码/状态映射/默认消息均已在
      common/errors 预留——与 myProfileGet 的 404 口径不同，按各自文档定稿）；
    - version 必填乐观锁（文档 ProfileConfirmation，同 /people 写链规则）；
    - 状态分支（REJECTED 重提为问题#2 2026-10-31 裁定 c）：
      DRAFT → PENDING（提交复核）；
      APPROVED → 409 STATE_CONFLICT（前端按钮 disabled=reviewStatus==='APPROVED'，
      重复确认无业务意义）；
      REJECTED → PENDING（驳回后本人再次确认=重提，闭环"驳回→修改→重提→再审"；
      不强制"先修改过档案"——系统无法可靠判定实质修改，原 B3 未决项就此定稿）；
      PENDING → 幂等成功（前端按钮该状态可点，重复提交无副作用：状态不变、
      不重复写 ChangeLog；实现选择在案）；
    - 风险5 修订（2026-10-31 有条件批准）：绑定解析与全部校验移入
      transaction.atomic 并对 Person 行 select_for_update 锁行重查——
      获锁后重检绑定（未绑定/软删→403）、版本号、状态机；与 review_person
      锁同一行，构成"提交先提交、审核后获锁"的串行顺序（MySQL REPEATABLE
      READ 下审核事务对 AuditLog 的首次读发生在获锁之后，快照必含最新已
      提交审计；SQLite 无行锁语义，验证边界见测试与报告）；
    - 审计事务化（风险5）：atomic 内经 _audit_in_transaction 写
      person.self_confirm SUCCESS 审计，与 Person/PersonChangeLog 同连接
      同事务；幂等重复确认同样写 SUCCESS 审计，语义=请求成功处理而非状态
      变化（2026-10-31 裁定保留）；API 层成功返回后
      audit_state(request, None) 撤销中间件声明防重复，服务层异常不撤销。
    - 留痕：状态变化写 _log_change（audit_status old/new）。
    """
    with transaction.atomic():
        person = (
            people_queryset()
            .select_for_update()
            .filter(pk=parse_path_id(person_id))
            .first()
        )
        if person is None:
            raise ApiError(PROFILE_BINDING_REQUIRED)
        ensure_version(person, version)
        current = person.audit_status
        if current == enums.AuditStatus.APPROVED:
            raise ApiError(STATE_CONFLICT, "档案已复核，无需重复确认")
        if current == enums.AuditStatus.PENDING:
            # 幂等：已处于待复核，无状态变化（审计仍记请求成功，不写 ChangeLog）
            _audit_in_transaction(
                "person.self_confirm",
                person.pk,
                getattr(account, "pk", "") or "",
                f"本人确认档案 {person.full_name}",
            )
            return person
        # REJECTED 落到此处=驳回后重提（问题#2 裁定 c）：再次确认即回 PENDING
        resubmit = current == enums.AuditStatus.REJECTED
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
            reason="本人确认档案（驳回后重提）" if resubmit else "本人确认档案",
        )
        _audit_in_transaction(
            "person.self_confirm",
            person.pk,
            getattr(account, "pk", "") or "",
            f"本人确认档案 {person.full_name}",
        )
    # 重查取 save 后的 updated_at（派生 version 与持久化结果一致）
    return people_queryset().filter(pk=person.pk).first()


def _audit_in_transaction(action: str, person_pk, operator_account_id: str, summary: str) -> None:
    """服务层事务内审计（风险5 修订方案，2026-10-31 有条件批准）。

    与业务写链同一数据库连接、同一 atomic 块：AuditLog 与 Person/
    PersonChangeLog 同生共死——审计写失败向外抛异常、业务整体回滚，
    消除"业务成功、审计丢失"的静默窗口（原 AuditMiddleware._write 在
    get_response 返回后另起写入且整体 try/except 吞错）。
    字段赋值对齐 AuditMiddleware._write 实码（common/middleware.py
    L163-172，非推测）：id=new_id("log")、log_type="OPERATION"（中间件
    默认值）、action/object_type/object_id 截断口径一致、summary 直出
    （中间件同样不截断）、outcome 由响应码映射——事务内业务成立恒 SUCCESS。
    中间件防重复：API 层在服务层成功返回后 audit_state(request, None)
    撤销本请求的审计声明，_write 首行守卫（action 为空即跳过）自然生效
    ——零通用模块修改、无 ContextVar；服务层异常时不撤销，中间件按原
    行为落 FAILURE。action/object_type/object_id 与 _assert_not_self_review
    查询口径逐字一致（"person.create"/"person.self_confirm" + "Person"）。
    """
    AuditLog.objects.create(
        id=new_id("log"),
        log_type="OPERATION",
        operator_account_id=operator_account_id,
        action=action[:100],
        object_type="Person",
        object_id=str(person_pk)[:64],
        summary=summary or "",
        outcome="SUCCESS",
    )


def _assert_not_self_review(person, account) -> None:
    """防自审自批（问题#5，2026-10-31 裁定）：禁止审核自己创建或最近一次提交的档案。

    追溯口径（AuditLog 只读查询，不新增表/字段/迁移）：
    - 入口覆盖（裁定核实项①②）：person.create（POST /people，API 层
      audit_state）为唯一建档入口；person.self_confirm（POST
      /me/profile-confirmation）为唯一 DRAFT/REJECTED→PENDING 提交入口，
      驳回后重提走同一入口（services.confirm_own_person）；seed 脚本直造
      数据不经 API、无审计日志（属缺日志场景，见下）；
    - 查询限定（裁定核实项③）：object_type="Person" + object_id=人员ID +
      outcome="SUCCESS"（失败请求审计落 FAILURE，不构成创建/提交事实），
      三条件同时命中方计入；
    - 最近一次提交（裁定核实项④）：created_at 倒序取首条（AuditLog.id 为
      随机 uuid 非时间序，不可作排序键）；PENDING 幂等重复确认同样落
      SUCCESS 日志，但操作者恒为档案绑定本人，与最近一次提交者同向，不漏判；
    - 缺日志处理（裁定核实项⑤）：查无创建/提交记录时无法确立可比较的
      操作者身份，不做拦截——这是"无证据可判"，不是绕过：数据范围
      （_get_person_for_write）、状态机、乐观锁与本函数之后的事务
      PersonChangeLog(AUDIT) 行保护全部保留；
    - 仅作用于人员档案审批（review_person），不扩展到材料审核。
    """
    entries = AuditLog.objects.filter(
        object_type="Person",
        object_id=str(person.pk),
        outcome="SUCCESS",
    ).filter(Q(action="person.create") | Q(action="person.self_confirm"))
    creator_id = (
        entries.filter(action="person.create")
        .order_by("-created_at")
        .values_list("operator_account_id", flat=True)
        .first()
    )
    last_submitter_id = (
        entries.filter(action="person.self_confirm")
        .order_by("-created_at")
        .values_list("operator_account_id", flat=True)
        .first()
    )
    account_id = getattr(account, "pk", "") or ""
    # account_id 非空才比较：日志 operator 缺失（空串）不得与空值账号互相命中
    if account_id and account_id in (creator_id, last_submitter_id):
        raise ApiError(FORBIDDEN, "不得审核自己创建或提交的人员档案")


def review_person(person_id: str, payload: dict, account):
    """POST /people/{id}/review：学校端档案审批（问题#2，2026-10-31 裁定 a/b/c）。

    - 权限：personReview=(UNIVERSITY, ADMIN)，与材料审核方案 i 同口径，
      市州/省不参与档案审批（避免两级重复审）；
    - 数据范围：_get_person_for_write（404/403 口径，学校端仅本校组织子树）；
    - version 必填乐观锁（缺失/非法=400，不匹配=409）；
    - 状态机（裁定 b）：仅 PENDING 可审——DRAFT 须本人先确认（409），
      APPROVED/REJECTED 终态（409）；REJECTED 重提=本人再次确认回 PENDING；
    - REJECT 必填原因（400，同 material_review）；
    - 防自审自批（问题#5，2026-10-31 裁定）：仅 PENDING 进入实质审批时校验
      "审核者 ≠ 创建者 且 ≠ 最近一次提交者"（_assert_not_self_review，
      403 FORBIDDEN），ADMIN 走同一入口同受限制；状态机之后校验，
      DRAFT/终态仍按 409 口径返回，已有行为不变；
    - 状态变更与 PersonChangeLog(AUDIT) 同事务：日志落库失败整体回滚，
      不留"已改状态、无留痕"的半完成审批（同 material_review）；审批自身
      审计（person.review）仍走 API 层，与自审判定无依赖；
    - 风险5 修订（2026-10-31 有条件批准）：对象解析与全部校验移入
      transaction.atomic 并对 Person 行 select_for_update 锁行——获锁后
      重检软删/存在（404）、数据范围（403，内联 _get_person_for_write
      原口径）、版本号（409）、状态机（409），错误码优先级与原实现一致；
      _assert_not_self_review 在获锁之后首次读取 AuditLog（裁定第 5 条：
      锁前不得读自审依据）——MySQL REPEATABLE READ 下快照此时才建立，
      必含获锁前已提交的最新提交审计，与 confirm_own_person 的行锁构成
      "提交先提交、审核后获锁"的串行顺序；SQLite 无行锁语义，验证边界
      见测试类说明与报告。
    """
    with transaction.atomic():
        person = (
            people_queryset()
            .select_for_update()
            .filter(pk=parse_path_id(person_id))
            .first()
        )
        if person is None:
            raise ApiError(NOT_FOUND)
        if not apply_person_scope(Person.objects.filter(pk=person.pk), account).exists():
            raise ApiError(DATA_SCOPE_DENIED, "人员不在当前账号数据范围内")
        ensure_version(person, payload["version"])
        current = person.audit_status
        if current == enums.AuditStatus.DRAFT:
            raise ApiError(
                STATE_CONFLICT, "档案未经本人确认，须先由本人提交复核后方可审批"
            )
        if current != enums.AuditStatus.PENDING:
            raise ApiError(
                STATE_CONFLICT,
                f"档案当前状态为 {current}，仅待复核（PENDING）档案可审批",
            )
        _assert_not_self_review(person, account)
        decision = payload["decision"]
        reason = (payload.get("reason") or "").strip()
        if decision == "REJECT" and not reason:
            raise ApiError(
                INVALID_ARGUMENT,
                "驳回档案必须填写审核意见",
                field_errors([("reason", "REJECT 时必填")]),
            )
        target = (
            enums.AuditStatus.APPROVED
            if decision == "APPROVE"
            else enums.AuditStatus.REJECTED
        )
        person.audit_status = target
        person.save()
        _log_change(
            person,
            enums.ChangeType.AUDIT,
            [{"field": "audit_status", "old": str(current), "new": target.value}],
            reason=reason or "审批通过",
        )
    # 重查取 save 后的 updated_at（派生 version 与持久化结果一致）
    return people_queryset().filter(pk=person.pk).first()


def list_own_teams(person_id: str) -> list[dict]:
    """GET /me/teams（ATHLETE/COACH）：本人现役（CURRENT）球员/教练关系的球队。

    契约依据（前端实码优先，契约清单 B4）：
    - meApi.teams() 无任何请求参数、响应 Team[]（zuqiu src/api/index.ts:69）；
      权威文档原写分页 TeamPage+active 参数，按前端实现数组直出；
    - 范围=正式关系 TeamMember/TeamCoach 且 status=CURRENT：mock 将"自报归属
      VERIFIED"并入（adapter.ts:340-343），冻结模型无申请表（B5），不采用；
      EXITED 历史关系属履历（/me/team-affiliations 域），不进本接口；
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
    return _team_brief_rows(Team.objects.filter(pk__in=team_ids, is_deleted=False))


def list_university_teams(account) -> list[dict]:
    """GET /me/teams（UNIVERSITY）：本校组织子树内球队，输出与本人视图同构。

    复用 teams.services.visible_teams（公开入口）做范围过滤：全局管理员全量、
    学校类组织 code 子树、无可见范围 → 403 DATA_SCOPE_DENIED。
    """
    from apps.teams.services import visible_teams

    return _team_brief_rows(visible_teams(account))


def list_team_account_teams(account) -> list[dict]:
    """GET /me/teams（TEAM）：球队端只看自己绑定的那支队。

    按 A′-min 约定，TEAM 账号组织节点 code == Team.team_no；不做组织子树，
    避免球队端看到同校其他队伍。绑定缺失或数据未对齐时返回空列表，
    让球队工作台能正常进入而不是 403。
    """
    from apps.common.scopes import account_org_code
    from apps.teams.models import Team

    team_no = account_org_code(account)
    if not team_no:
        return []
    return _team_brief_rows(Team.objects.filter(team_no=team_no, is_deleted=False))


def _team_brief_rows(queryset: QuerySet) -> list[dict]:
    """球队行 → /me/teams 输出（memberCount 与现役主教练姓名一次取齐）。"""
    head_coach_subq = TeamCoach.objects.filter(
        team=OuterRef("pk"),
        status=enums.RelationStatus.CURRENT,
        duty=enums.CoachDuty.HEAD_COACH,
    ).values("person__full_name")[:1]
    rows = (
        queryset.select_related("school", "region")
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
    person = _get_person_for_write(person_id, account)
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

    复用：people_queryset（软删排除+档案行注解）、apply_person_scope（角色/
    组织范围口径）、list_people 的 -id 排序先例、create_person 的
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
