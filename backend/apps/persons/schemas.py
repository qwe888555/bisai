"""基础数据请求模型与响应序列化（django-ninja / pydantic v2）。

请求模型继承 StrictSchema（多传字段直接 400）：code / value 等"稳定编码"字段
不进 Update 模型，传了即 400，从契约层实现"创建后禁改"；
响应统一由 EnvelopeOut 包裹，字段名 camelCase，与 accounts 模块约定一致。
created_by/updated_by 本阶段恒空（底座用户ID映射未定），不在响应中输出。
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from apps.common.schemas import StrictSchema


# ---------------------------------------------------------------- 请求

class RegionCreate(StrictSchema):
    name: str = Field(min_length=1, max_length=50)
    code: str = Field(min_length=1, max_length=12)
    level: int = Field(ge=1, le=3, description="1省/2市州/3区县")
    parentId: Optional[int] = Field(default=None, ge=1)
    sortOrder: int = Field(default=0)
    isActive: bool = True


class RegionUpdate(StrictSchema):
    """区划修改（PUT 全量替换可变字段）；code/level 创建后禁改。"""

    name: str = Field(min_length=1, max_length=50)
    parentId: Optional[int] = Field(default=None, ge=1)
    sortOrder: int = Field(default=0)
    isActive: bool = True


class SchoolCreate(StrictSchema):
    name: str = Field(min_length=1, max_length=100)
    code: Optional[str] = Field(default=None, max_length=20, description="学校标识码,可空唯一")
    schoolType: str = Field(min_length=1, max_length=20)
    regionId: int = Field(ge=1, description="所属区县")
    address: Optional[str] = Field(default=None, max_length=200)
    isActive: bool = True


class SchoolUpdate(SchoolCreate):
    """学校修改（PUT 全量替换）；school 的 code 允许修改（可空唯一）。"""


class DictTypeCreate(StrictSchema):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    remark: Optional[str] = Field(default=None, max_length=200)


class DictTypeUpdate(StrictSchema):
    """字典类型修改；code 创建后禁改（稳定编码）。"""

    name: str = Field(min_length=1, max_length=100)
    remark: Optional[str] = Field(default=None, max_length=200)


class DictItemCreate(StrictSchema):
    typeCode: str = Field(min_length=1, max_length=50, description="所属字典类型编码")
    value: str = Field(min_length=1, max_length=50)
    label: str = Field(min_length=1, max_length=100)
    sortOrder: int = Field(default=0)
    isActive: bool = True
    extJson: Optional[dict] = Field(default=None, description="结构化扩展,仅接受对象")


class DictItemUpdate(StrictSchema):
    """字典项修改；value/type 创建后禁改（value 稳定编码,含义禁改）。

    PUT 全量替换：extJson 未传视为清空。
    """

    label: str = Field(min_length=1, max_length=100)
    sortOrder: int = Field(default=0)
    isActive: bool = True
    extJson: Optional[dict] = None


# ---------------------------------------------------------------- 响应

def region_out(region) -> dict:
    return {
        "id": region.pk,
        "name": region.name,
        "code": region.code,
        "level": region.level,
        "parentId": region.parent_id,
        "sortOrder": region.sort_order,
        "isActive": bool(region.is_active),
        "createdAt": region.created_at,
        "updatedAt": region.updated_at,
    }


def school_out(school) -> dict:
    return {
        "id": school.pk,
        "name": school.name,
        "code": school.code,
        "schoolType": school.school_type,
        "regionId": school.region_id,
        "regionName": school.region.name if school.region_id else None,
        "address": school.address,
        "isActive": bool(school.is_active),
        "createdAt": school.created_at,
        "updatedAt": school.updated_at,
    }


def dict_type_out(obj) -> dict:
    return {
        "id": obj.pk,
        "code": obj.code,
        "name": obj.name,
        "remark": obj.remark,
        "createdAt": obj.created_at,
        "updatedAt": obj.updated_at,
    }


def dict_item_out(item) -> dict:
    return {
        "id": item.pk,
        "typeId": item.type_id,
        "typeCode": item.type.code if item.type_id else None,
        "value": item.value,
        "label": item.label,
        "sortOrder": item.sort_order,
        "isActive": bool(item.is_active),
        "extJson": item.ext_json,
        "createdAt": item.created_at,
        "updatedAt": item.updated_at,
    }
