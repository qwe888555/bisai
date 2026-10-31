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
from apps.common.conf import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from apps.common.envelope import action_result, ok, page
from apps.common.errors import (
    INVALID_ARGUMENT,
    NOT_FOUND,
    PROFILE_BINDING_REQUIRED,
    ApiError,
    field_errors,
)
from apps.common.middleware import audit_state
from apps.common.pagination import paginate, paginate_list
from apps.common.permissions import (
    ADMIN,
    ALL_ROLES,
    ATHLETE,
    CITY,
    COACH,
    PROVINCE_REVIEWER,
    TEAM,
    UNIVERSITY,
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


# ---------------------------------------------------------------- 人员主档（/people）
# 契约对齐 zuqiu 前端 personApi（src/api/index.ts L118-125）；读链与写链
# 均按契约角色登记，数据范围由服务层统一裁决。

# 前端实际调用方角色：PeopleManageView(ADMIN) / EnrollmentDetailView(UNIVERSITY) /
# TeamCoachesView、TeamMembersView(TEAM)；CITY / PROVINCE_REVIEWER 按文档审核
# 场景预留。ATHLETE/COACH 的自服务走 /me/profile，不登记本组接口。
# 数据范围过滤集中在 services.apply_person_scope（按角色/组织子树收敛）。
_PERSON_QUERY_ROLES = (ADMIN, UNIVERSITY, CITY, PROVINCE_REVIEWER, TEAM)

register_operation("personList", _PERSON_QUERY_ROLES)
register_operation("personSearch", _PERSON_QUERY_ROLES)
register_operation("personGet", _PERSON_QUERY_ROLES)


@router.get("/people", auth=_auth, response=EnvelopeOut, summary="人员档案列表")
def people_list(
    request,
    keyword: Optional[str] = None,
    kind: Optional[str] = None,
    reviewStatus: Optional[str] = None,
):
    account = require("personList")
    queryset = services.list_people(
        {"keyword": keyword, "kind": kind, "reviewStatus": reviewStatus}, account
    )
    return paginate(request, queryset, mapper=s.person_out)


@router.post(
    "/people/search", auth=_auth, response=EnvelopeOut, summary="人员档案检索"
)
def people_search(request, body: s.PersonSearchBody):
    account = require("personSearch")
    items = services.search_people(body.keyword, account)
    # 检索正文不入审计（文档：精确检索服务端不记正文），仅记操作发生
    audit_state(
        request,
        "person.search",
        object_type="Person",
        summary="人员档案检索",
    )
    # 前端 personApi.search 类型为 Person[]（数组非分页），直接返回列表
    return ok([s.person_out(item) for item in items])


@router.get(
    "/people/{person_id}", auth=_auth, response=EnvelopeOut, summary="人员档案详情"
)
def people_get(request, person_id: str):
    """详情在 person_out 基础上追加只读派生字段 coachQualification
    （问题#7，2026-10-31 裁定：仅 GET /people/{id} 输出，列表/搜索契约
    不扩散；四态语义见 services.coach_qualification）。"""
    account = require("personGet")
    person = services.get_person(person_id, account)
    data = s.person_out(person)
    data["coachQualification"] = services.coach_qualification(person)
    return ok(data)


# 写操作权限点按契约文档登记（personCreate/personUpdate/personDelete：
# TEAM/UNIVERSITY/CITY/PROVINCE_REVIEWER/ADMIN）；数据范围在服务层
# （_get_person_for_write / _ensure_school_in_scope）二次校验，接口层放行
# 不等于可写越界对象。
register_operation("personCreate", (TEAM, UNIVERSITY, CITY, PROVINCE_REVIEWER, ADMIN))
register_operation("personUpdate", (TEAM, UNIVERSITY, CITY, PROVINCE_REVIEWER, ADMIN))
register_operation("personDelete", (TEAM, UNIVERSITY, CITY, PROVINCE_REVIEWER, ADMIN))
# 档案审批（问题#2，2026-10-31 裁定 a）：学校端+ADMIN，市州/省不参与
# （与材料审核方案 i 同口径，避免两级重复审）；数据范围服务层二次校验。
register_operation("personReview", (UNIVERSITY, ADMIN))


@router.post("/people", auth=_auth, response=EnvelopeOut, summary="创建人员档案")
def people_create(request, body: s.PersonCreate):
    account = require("personCreate")
    # 风险5 修订（2026-10-31 有条件批准）：审计声明前置（服务层成功返回前
    # 无法预知 object_id）——服务层事务内写 SUCCESS 审计并在此撤销声明防
    # 中间件重复；服务层异常时不撤销，中间件按原行为落 FAILURE（无 object_id）。
    audit_state(request, "person.create", object_type="Person", summary="创建人员档案")
    person = services.create_person(body.model_dump(), account)
    audit_state(request, None)
    return ok(s.person_out(person))


@router.put(
    "/people/{person_id}", auth=_auth, response=EnvelopeOut, summary="更新人员档案"
)
def people_update(request, person_id: str, body: s.PersonUpdate):
    account = require("personUpdate")
    person = services.update_person(person_id, body.model_dump(), account)
    audit_state(
        request,
        "person.update",
        object_type="Person",
        object_id=str(person.pk),
        summary=f"更新人员 {person.full_name}",
    )
    return ok(s.person_out(person))


@router.delete(
    "/people/{person_id}", auth=_auth, response=EnvelopeOut, summary="删除人员档案"
)
def people_delete(request, person_id: str, version: Optional[int] = Query(None, ge=0)):
    account = require("personDelete")
    if version is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "缺少 version",
            field_errors([("version", "该操作必须提交乐观锁版本")]),
        )
    services.delete_person(person_id, version, account)
    audit_state(
        request,
        "person.delete",
        object_type="Person",
        object_id=person_id,
        summary="删除人员档案（软删）",
    )
    return action_result(True, person_id)


@router.post(
    "/people/{person_id}/review", auth=_auth, response=EnvelopeOut, summary="审批人员档案"
)
def people_review(request, person_id: str, body: s.PersonReviewBody):
    account = require("personReview")
    person = services.review_person(person_id, body.model_dump(), account)
    audit_state(
        request,
        "person.review",
        object_type="Person",
        object_id=str(person.pk),
        summary=f"档案审批 {person.full_name} → {person.audit_status}",
    )
    return ok(s.person_out(person))


# ---------------------------------------------------------------- 本人档案（/me/profile）
# 前端 MyProfileView 经 profileApi 调用（zuqiu src/api/index.ts L65-66）；
# 权限点命名与权威接口文档 §6.5 对齐（myProfileGet/myProfileUpdate）。
# GET：自读对象=服务端账号 person_id 绑定档案，无客户端传入 id，无数据范围歧义。
# PUT（2026-10-24 用户裁定）：version 必填乐观锁（缺失/非法=400 统一校验错误，
# 不匹配=409 VERSION_CONFLICT）；字段集=MyProfileUpdate（前端实际提交集），
# 身份/归属/联系方式/证件不在本人可改集；education/coachCategory 落扩展档案
# ext_json 回显，schoolName 派生只读（改成别值→422）——5.txt 问题一已闭环。
register_operation("myProfileGet", (ATHLETE, COACH))
register_operation("myProfileUpdate", (ATHLETE, COACH))


def _own_person_id(account) -> str:
    """当前账号绑定档案 ID（服务端签发，空=未绑定）。"""
    return (getattr(account, "person_id", "") or "").strip()


@router.get("/me/profile", auth=_auth, response=EnvelopeOut, summary="查看本人档案")
def my_profile(request):
    account = require("myProfileGet")
    person_id = _own_person_id(account)
    if not person_id:
        raise ApiError(NOT_FOUND, "当前账号未绑定人员档案")
    return ok(s.person_out(services.get_own_person(person_id)))


@router.put("/me/profile", auth=_auth, response=EnvelopeOut, summary="更新本人档案")
def my_profile_update(request, body: s.MyProfileUpdate):
    account = require("myProfileUpdate")
    person_id = _own_person_id(account)
    if not person_id:
        raise ApiError(NOT_FOUND, "当前账号未绑定人员档案")
    person = services.update_own_person(person_id, body.model_dump())
    audit_state(
        request,
        "person.self_update",
        object_type="Person",
        object_id=str(person.pk),
        summary=f"本人更新档案 {person.full_name}",
    )
    return ok(s.person_out(person))


# GET /me/profile-match（契约清单 B2）：前端实际为 GET（权威文档原写 POST，
# 按"前端实码优先"原则实现 GET 并同步文档）；权限点沿用文档命名 profileMatch。
# 仅服务端绑定关系判定，无请求参数；响应三态见 services.get_profile_match。
register_operation("profileMatch", (ATHLETE, COACH))


@router.get(
    "/me/profile-match", auth=_auth, response=EnvelopeOut, summary="本人主档匹配状态"
)
def my_profile_match(request):
    account = require("profileMatch")
    # dict 直出（EnvelopeOut.data=Any）：NO_MATCH/REVIEW_REQUIRED 时无
    # personId/maskedIdNumber 键，与前端 mock 契约形状一致（非 null 占位）
    return ok(services.get_profile_match(account))


# POST /me/profile-confirmation（契约清单 B3）：确认=DRAFT→PENDING 提交复核
# （create_person 初值 DRAFT + 前端成功文案"等待管理员复核"；mock 置 APPROVED
# 与文案矛盾不作依据）。version 必填乐观锁（文档 ProfileConfirmation）；
# 绑定失效（未绑定/软删）→ 403 PROFILE_BINDING_REQUIRED（文档定稿口径，
# 错误码已在 common/errors 预留）——与 myProfileGet 的 404 口径不同。
register_operation("profileConfirm", (ATHLETE, COACH))


@router.post(
    "/me/profile-confirmation",
    auth=_auth,
    response=EnvelopeOut,
    summary="确认本人档案信息",
)
def my_profile_confirm(request, body: s.ProfileConfirmation):
    account = require("profileConfirm")
    person_id = _own_person_id(account)
    if not person_id:
        raise ApiError(PROFILE_BINDING_REQUIRED)
    # 风险5 修订：审计声明前置（object_id 服务层成功前不可预知）——服务层
    # 事务内写 SUCCESS 审计，成功返回后撤销声明防中间件重复；异常不撤销，
    # 中间件按原行为落 FAILURE。account 入参供 operator_account_id 取值。
    audit_state(
        request, "person.self_confirm", object_type="Person", summary="本人确认档案"
    )
    person = services.confirm_own_person(person_id, body.version, account)
    audit_state(request, None)
    return ok(s.person_out(person))


# GET /me/teams（契约清单 B4）：前端 meApi.teams() 无参数、Team[] 直出
# （zuqiu src/api/index.ts:69；权威文档原写分页 TeamPage+active 参数，按前端
# 实码实现数组）。
# - ATHLETE/COACH：本人 CURRENT 球员/教练正式关系（EXITED 历史与 mock
#   "自报归属 VERIFIED 合并"均不采用，见 services.list_own_teams）；未绑定
#   personId → 空数组（个人中心空态，不拒绝）。
# - UNIVERSITY：本校组织子树内球队（services.list_university_teams，复用
#   teams.services.visible_teams），响应字段与本人视图完全一致。
# - TEAM：只返回组织节点 code 匹配 team_no 的本队（services.list_team_account_teams）；
#   绑定缺失/数据未对齐返回空数组，保证球队工作台可正常进入。
# 读接口不留审计（同前两个 GET）。
register_operation("myTeams", (ATHLETE, COACH, UNIVERSITY, TEAM))


@router.get("/me/teams", auth=_auth, response=EnvelopeOut, summary="本人所属球队")
def my_teams(request):
    account = require("myTeams")
    if set(account.roles or ()) & {ATHLETE, COACH}:
        person_id = _own_person_id(account)
        if not person_id:
            return ok([])
        # dict 直出（EnvelopeOut.data=Any），字段口径见 services._team_brief_out
        return ok(services.list_own_teams(person_id))
    if TEAM in set(account.roles or ()):
        return ok(services.list_team_account_teams(account))
    return ok(services.list_university_teams(account))


# §6.8 档案聚合（批次 D-1）：文档角色 ADMIN/PROVINCE_REVIEWER（接口文档
# L1740/L1762）；数据范围复用 apply_person_scope（按角色/组织子树收敛，
# 无可见学校范围的账号 403 DATA_SCOPE_DENIED）。
# 读接口不留审计（同 GET /people）；检索正文不入审计（同 POST /people/search）。
register_operation("dossierSearch", (ADMIN, PROVINCE_REVIEWER))
register_operation("dossierGet", (ADMIN, PROVINCE_REVIEWER))


@router.post(
    "/selection-dossiers/search",
    auth=_auth,
    response=EnvelopeOut,
    summary="遴选档案受控检索",
)
def dossier_search(request, body: s.DossierSearchBody):
    account = require("dossierSearch")
    payload = body.model_dump()
    queryset = services.dossier_search_queryset(payload, account)
    # page/pageSize 在请求体（§7 DossierSearch 模型），与 paginate 的 query
    # string 参数源不同：切片/上限/信封语义沿用 pagination 同款（默认 20、
    # 上限 100、page 从 1 起）。
    total = queryset.count()
    page_number = payload.get("page") or 1
    page_size = min(max(payload.get("pageSize") or DEFAULT_PAGE_SIZE, 1), MAX_PAGE_SIZE)
    start = (page_number - 1) * page_size
    items = [
        services.dossier_out(row, account)
        for row in queryset[start : start + page_size]
    ]
    # page() 已含完整信封（ok 包装），不再二次包装
    return page(items, page_number, page_size, total)


@router.get(
    "/selection-dossiers/{athlete_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="球员遴选档案聚合详情",
)
def dossier_get(request, athlete_id: str, eventId: Optional[str] = None):
    account = require("dossierGet")
    return ok(services.dossier_get(athlete_id, eventId, account))
