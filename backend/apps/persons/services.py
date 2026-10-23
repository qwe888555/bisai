"""基础数据业务逻辑：区划 / 学校 / 字典类型 / 字典项（persons 域第一阶段）。

依据：docs/design/成员4-DDL草案-V1.4.sql（V1.4-r1，冻结）+ 成员4后端总体设计。
- Region / School / DictItem 只停用不删（is_active），任何接口不物理删除；
- DictType 无停用列：仅当无字典项引用时允许物理删除，否则 409 拒绝；
- 所有外键 PROTECT：父对象必须存在；停用父对象不级联，历史引用不受影响；
- 严格层级校验：省无父、市州父=省、区县父=市，父对象必须处于启用状态
  （因此父对象停用后其子对象不可再编辑，需先恢复父对象）；
- created_by / updated_by 为底座用户ID（BIGINT），当前 Account 主键为非数字
  字符串（acc_xxx），本阶段不写入（保持 NULL），操作人由 AuditLog 留痕；
- 表无 version 乐观锁列，更新为最后写入胜出，审计留痕兜底。
"""

from __future__ import annotations

from typing import Optional

from django.db.models import Q, QuerySet

from apps.common.enums import RegionLevel
from apps.common.errors import (
    DUPLICATE_RESOURCE,
    INVALID_ARGUMENT,
    NOT_FOUND,
    STATE_CONFLICT,
    ApiError,
    field_errors,
)
from apps.persons.models import DictItem, DictType, Region, School

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
