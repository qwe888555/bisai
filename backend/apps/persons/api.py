"""基础数据管理接口（django-ninja）：/base-data/*。

成员4 域第一阶段：base_region / base_school / base_dict_type / base_dict_item。
- 管理操作（创建/修改/停用/删除）仅 ADMIN；查询类登录即可（ALL_ROLES），
  与 accounts dictionaryOptions 的授权惯例一致；
- 与底座字典接口（accounts /dictionary-items）并存，对象与职责不同互不影响；
- 写操作统一 audit_state()，由 AuditMiddleware 落 accounts_audit_log；
- Region/School/DictItem 只停用不删，DictType 仅在无引用时物理删除。
"""

from __future__ import annotations

from typing import Optional

from ninja import Query, Router

from apps.common.authentication import JWTAuth
from apps.common.envelope import action_result, ok
from apps.common.middleware import audit_state
from apps.common.pagination import paginate, paginate_list
from apps.common.permissions import (
    ADMIN,
    ALL_ROLES,
    register_operation,
    require,
)
from apps.common.schemas import EnvelopeOut

from . import schemas as s
from . import services

router = Router(tags=["base-data"])
_auth = JWTAuth()

# 权限点登记：导入期完成，漏登记 require() 会直接报错
register_operation("regionList", ALL_ROLES)
register_operation("regionCreate", (ADMIN,))
register_operation("regionGet", ALL_ROLES)
register_operation("regionUpdate", (ADMIN,))
register_operation("regionDisable", (ADMIN,))

register_operation("schoolList", ALL_ROLES)
register_operation("schoolCreate", (ADMIN,))
register_operation("schoolGet", ALL_ROLES)
register_operation("schoolUpdate", (ADMIN,))
register_operation("schoolDisable", (ADMIN,))

register_operation("dictTypeList", ALL_ROLES)
register_operation("dictTypeCreate", (ADMIN,))
register_operation("dictTypeGet", ALL_ROLES)
register_operation("dictTypeUpdate", (ADMIN,))
register_operation("dictTypeDelete", (ADMIN,))
register_operation("dictTypeOptions", ALL_ROLES)

register_operation("dictItemList", ALL_ROLES)
register_operation("dictItemCreate", (ADMIN,))
register_operation("dictItemGet", ALL_ROLES)
register_operation("dictItemUpdate", (ADMIN,))
register_operation("dictItemDisable", (ADMIN,))


# ---------------------------------------------------------------- 区划

@router.get("/base-data/regions", auth=_auth, response=EnvelopeOut, summary="区划列表")
def region_list(
    request,
    level: Optional[int] = Query(None, ge=1, le=3),
    parentId: Optional[int] = Query(None, ge=1),
    keyword: Optional[str] = None,
    isActive: Optional[bool] = None,
):
    require("regionList")
    queryset = services.list_regions(
        {"level": level, "parentId": parentId, "keyword": keyword, "isActive": isActive}
    )
    return paginate(request, queryset, mapper=s.region_out)


@router.post("/base-data/regions", auth=_auth, response=EnvelopeOut, summary="创建区划")
def region_create(request, body: s.RegionCreate):
    require("regionCreate")
    region = services.create_region(body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "baseData.region.create",
        object_type="Region",
        object_id=str(region.pk),
        summary=f"创建区划 {region.name}({region.code})",
    )
    return ok(s.region_out(region))


@router.get(
    "/base-data/regions/{region_id}", auth=_auth, response=EnvelopeOut, summary="区划详情"
)
def region_get(request, region_id: str):
    require("regionGet")
    return ok(s.region_out(services.get_region(region_id)))


@router.put(
    "/base-data/regions/{region_id}", auth=_auth, response=EnvelopeOut, summary="修改区划"
)
def region_update(request, region_id: str, body: s.RegionUpdate):
    require("regionUpdate")
    region = services.update_region(region_id, body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "baseData.region.update",
        object_type="Region",
        object_id=str(region.pk),
        summary=f"修改区划 {region.name}({region.code})",
    )
    return ok(s.region_out(region))


@router.delete(
    "/base-data/regions/{region_id}", auth=_auth, response=EnvelopeOut, summary="停用区划"
)
def region_disable(request, region_id: str):
    require("regionDisable")
    region = services.disable_region(region_id)
    audit_state(
        request,
        "baseData.region.disable",
        object_type="Region",
        object_id=str(region.pk),
        summary=f"停用区划 {region.name}({region.code})",
    )
    return action_result(True, region.pk)


# ---------------------------------------------------------------- 学校

@router.get("/base-data/schools", auth=_auth, response=EnvelopeOut, summary="学校列表")
def school_list(
    request,
    regionId: Optional[int] = Query(None, ge=1),
    schoolType: Optional[str] = None,
    keyword: Optional[str] = None,
    isActive: Optional[bool] = None,
):
    require("schoolList")
    queryset = services.list_schools(
        {
            "regionId": regionId,
            "schoolType": schoolType,
            "keyword": keyword,
            "isActive": isActive,
        }
    )
    return paginate(request, queryset, mapper=s.school_out)


@router.post("/base-data/schools", auth=_auth, response=EnvelopeOut, summary="创建学校")
def school_create(request, body: s.SchoolCreate):
    require("schoolCreate")
    school = services.create_school(body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "baseData.school.create",
        object_type="School",
        object_id=str(school.pk),
        summary=f"创建学校 {school.name}",
    )
    return ok(s.school_out(school))


@router.get(
    "/base-data/schools/{school_id}", auth=_auth, response=EnvelopeOut, summary="学校详情"
)
def school_get(request, school_id: str):
    require("schoolGet")
    return ok(s.school_out(services.get_school(school_id)))


@router.put(
    "/base-data/schools/{school_id}", auth=_auth, response=EnvelopeOut, summary="修改学校"
)
def school_update(request, school_id: str, body: s.SchoolUpdate):
    require("schoolUpdate")
    school = services.update_school(school_id, body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "baseData.school.update",
        object_type="School",
        object_id=str(school.pk),
        summary=f"修改学校 {school.name}",
    )
    return ok(s.school_out(school))


@router.delete(
    "/base-data/schools/{school_id}", auth=_auth, response=EnvelopeOut, summary="停用学校"
)
def school_disable(request, school_id: str):
    require("schoolDisable")
    school = services.disable_school(school_id)
    audit_state(
        request,
        "baseData.school.disable",
        object_type="School",
        object_id=str(school.pk),
        summary=f"停用学校 {school.name}",
    )
    return action_result(True, school.pk)


# ---------------------------------------------------------------- 字典类型

@router.get(
    "/base-data/dict-types", auth=_auth, response=EnvelopeOut, summary="字典类型列表"
)
def dict_type_list(
    request,
    keyword: Optional[str] = None,
):
    require("dictTypeList")
    queryset = services.list_dict_types({"keyword": keyword})
    return paginate(request, queryset, mapper=s.dict_type_out)


@router.post(
    "/base-data/dict-types", auth=_auth, response=EnvelopeOut, summary="创建字典类型"
)
def dict_type_create(request, body: s.DictTypeCreate):
    require("dictTypeCreate")
    obj = services.create_dict_type(body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "baseData.dictType.create",
        object_type="DictType",
        object_id=str(obj.pk),
        summary=f"创建字典类型 {obj.code}",
    )
    return ok(s.dict_type_out(obj))


@router.get(
    "/base-data/dict-types/{dict_type_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="字典类型详情",
)
def dict_type_get(request, dict_type_id: str):
    require("dictTypeGet")
    return ok(s.dict_type_out(services.get_dict_type(dict_type_id)))


@router.put(
    "/base-data/dict-types/{dict_type_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="修改字典类型",
)
def dict_type_update(request, dict_type_id: str, body: s.DictTypeUpdate):
    require("dictTypeUpdate")
    obj = services.update_dict_type(dict_type_id, body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "baseData.dictType.update",
        object_type="DictType",
        object_id=str(obj.pk),
        summary=f"修改字典类型 {obj.code}",
    )
    return ok(s.dict_type_out(obj))


@router.delete(
    "/base-data/dict-types/{dict_type_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="删除字典类型",
)
def dict_type_delete(request, dict_type_id: str):
    require("dictTypeDelete")
    obj = services.delete_dict_type(dict_type_id)
    audit_state(
        request,
        "baseData.dictType.delete",
        object_type="DictType",
        object_id=str(obj.pk),
        summary=f"删除字典类型 {obj.code}",
    )
    return action_result(True, obj.pk)


@router.get(
    "/base-data/dict-types/{type_code}/options",
    auth=_auth,
    response=EnvelopeOut,
    summary="字典可用选项",
)
def dict_type_options(request, type_code: str):
    require("dictTypeOptions")
    queryset = services.dict_type_options(type_code)
    return paginate_list(request, [s.dict_item_out(item) for item in queryset])


# ---------------------------------------------------------------- 字典项

@router.get(
    "/base-data/dict-items", auth=_auth, response=EnvelopeOut, summary="字典项列表"
)
def dict_item_list(
    request,
    typeCode: Optional[str] = None,
    keyword: Optional[str] = None,
    isActive: Optional[bool] = None,
):
    require("dictItemList")
    queryset = services.list_dict_items(
        {"typeCode": typeCode, "keyword": keyword, "isActive": isActive}
    )
    return paginate(request, queryset, mapper=s.dict_item_out)


@router.post(
    "/base-data/dict-items", auth=_auth, response=EnvelopeOut, summary="创建字典项"
)
def dict_item_create(request, body: s.DictItemCreate):
    require("dictItemCreate")
    item = services.create_dict_item(body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "baseData.dictItem.create",
        object_type="DictItem",
        object_id=str(item.pk),
        summary=f"创建字典项 {item.type.code}:{item.value}",
    )
    return ok(s.dict_item_out(item))


@router.get(
    "/base-data/dict-items/{dict_item_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="字典项详情",
)
def dict_item_get(request, dict_item_id: str):
    require("dictItemGet")
    return ok(s.dict_item_out(services.get_dict_item(dict_item_id)))


@router.put(
    "/base-data/dict-items/{dict_item_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="修改字典项",
)
def dict_item_update(request, dict_item_id: str, body: s.DictItemUpdate):
    require("dictItemUpdate")
    item = services.update_dict_item(dict_item_id, body.model_dump(exclude_unset=True))
    audit_state(
        request,
        "baseData.dictItem.update",
        object_type="DictItem",
        object_id=str(item.pk),
        summary=f"修改字典项 {item.type.code}:{item.value}",
    )
    return ok(s.dict_item_out(item))


@router.delete(
    "/base-data/dict-items/{dict_item_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="停用字典项",
)
def dict_item_disable(request, dict_item_id: str):
    require("dictItemDisable")
    item = services.disable_dict_item(dict_item_id)
    audit_state(
        request,
        "baseData.dictItem.disable",
        object_type="DictItem",
        object_id=str(item.pk),
        summary=f"停用字典项 {item.type.code}:{item.value}",
    )
    return action_result(True, item.pk)
