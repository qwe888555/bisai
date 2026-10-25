"""基础数据请求模型与响应序列化（django-ninja / pydantic v2）。

请求模型继承 StrictSchema（多传字段直接 400）：code / value 等"稳定编码"字段
不进 Update 模型，传了即 400，从契约层实现"创建后禁改"；
响应统一由 EnvelopeOut 包裹，字段名 camelCase，与 accounts 模块约定一致。
created_by/updated_by 本阶段恒空（底座用户ID映射未定），不在响应中输出。
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from django.core.exceptions import ObjectDoesNotExist
from pydantic import Field

from apps.common import crypto
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


# ---------------------------------------------------------------- 人员主档

class PersonSearchBody(StrictSchema):
    """POST /people/search 正文：前端三处调用（PublicationsManageView L160、
    TeamCoachesView L77、EnrollmentDetailView L253）均只传 keyword。"""

    keyword: str = Field(min_length=1, max_length=50)


class DossierSearchBody(StrictSchema):
    """POST /selection-dossiers/search 请求体（接口文档 §6.8 dossierSearch + §7 DossierSearch）。

    - idNumber：受控检索（文档 L4713"服务端加密并生成查重索引；不回显到响应
      与日志"）→ 服务层 lookup_hash 精确匹配（与 create_person 写链同款），
      响应仅 maskedIdNumber；
    - eventId/groupId：契约仅定义"内部ID"参数、未规定筛选行为，报名/比赛读
      模型未落地（冻结 22 表无此域）→ 服务层遇非空值返回 400 明确未开放
      （待决策项 D-A：不默认空集、不虚构过滤语义）；
    - 检索资格：文档 L4708"仅运动员遴选档案受控检索，不允许借用教练员或
      其他人员过滤字段扩大数据范围"→ 服务层强制 AthleteProfile 行存在。
    """

    athleteName: Optional[str] = Field(None, max_length=100)
    idNumber: Optional[str] = Field(
        None, min_length=18, max_length=18, pattern=r"^\d{17}[\dXx]$"
    )
    teamId: Optional[str] = Field(None, min_length=1, max_length=64)
    eventId: Optional[str] = Field(None, min_length=1, max_length=64)
    groupId: Optional[str] = Field(None, min_length=1, max_length=64)
    page: Optional[int] = Field(None, ge=1)
    pageSize: Optional[int] = Field(None, ge=1, le=100)


GENDER_OUT: dict[int, str] = {0: "UNSPECIFIED", 1: "MALE", 2: "FEMALE"}
GENDER_IN: dict[str, int] = {"UNSPECIFIED": 0, "MALE": 1, "FEMALE": 2}


def person_version(person) -> int:
    """乐观锁版本号 = updated_at(UTC) 的微秒 epoch（B4 验证方案）。

    冻结模型无 version 列；updated_at 为 auto_now（UTC 存储，USE_TZ=True，
    MySQL DATETIME(6)/SQLite 微秒字符串），所有写路径经 instance.save() 刷新。
    epoch 微秒 ≈1.77e15 < float64 尾数精度 2^53≈4.5e15，整数化无损。
    注意：写链禁止 queryset.update() 修改业务字段（绕过 auto_now，见测试
    test_queryset_update_bypasses_auto_now 作为盲区证据）。
    """
    return int(person.updated_at.timestamp() * 1_000_000)


class PersonCreate(StrictSchema):
    """POST /people 创建（契约对齐 PeopleManageView 新建表单 + 文档 §6.5）。

    - kinds：方案A 一期单类别（2026-10-24 用户决策）——schema 限项数=1，
      多类别/空值直接 400，不静默取首值或丢弃；
    - education / coachCategory：前端表单会提交（PeopleManageView L130-131），
      为避免 extra=forbid 拦截 400 阻断保存而**声明但不持久化**（承载方案待定）；
    - idNumber/phone 只写不回显，服务端加密+查重索引；一期创建服务层强制
      要求 idNumber（冻结 id_card_hash 无 NULL 且全局唯一含软删行，无证件
      档案最多只能存在一条空串行，与文档"可选"冲突，已列待确认）。
    """

    kinds: list[str] = Field(min_length=1, max_length=2)
    realName: str = Field(min_length=1, max_length=100)
    idNumber: Optional[str] = Field(
        None, min_length=18, max_length=18, pattern=r"^\d{17}[\dXx]$"
    )
    phone: Optional[str] = Field(None, min_length=11, max_length=11, pattern=r"^1\d{10}$")
    gender: Optional[str] = Field(None, max_length=20)
    birthDate: Optional[date] = None
    schoolOrgId: Optional[str] = Field(None, min_length=1, max_length=64)
    avatarFileId: Optional[str] = Field(None, min_length=1, max_length=64)
    education: Optional[str] = Field(None, max_length=100)
    coachCategory: Optional[str] = Field(None, max_length=64)


class PersonUpdate(StrictSchema):
    """PUT /people/{id} 更新（前端编辑表单不含 idNumber/phone 输入，L370 仅新建显示）。

    - version 必填：乐观锁（updated_at UTC 微秒派生，B4 已验证方案）；
    - 必填字段（kinds/realName/gender/birthDate）全量替换；可选字段
      （phone/schoolOrgId/avatarFileId）未传=保留原值——编辑表单无手机号输入，
      缺省清空会造成静默数据丢失（实现口径在案，非全量清空）；
    - idNumber 不接受：身份证变更走更正流程（文档 §9 待确认），一期不开放。
    """

    kinds: list[str] = Field(min_length=1, max_length=2)
    realName: str = Field(min_length=1, max_length=100)
    gender: Optional[str] = Field(None, max_length=20)
    birthDate: Optional[date] = None
    schoolOrgId: Optional[str] = Field(None, min_length=1, max_length=64)
    avatarFileId: Optional[str] = Field(None, min_length=1, max_length=64)
    phone: Optional[str] = Field(None, min_length=11, max_length=11, pattern=r"^1\d{10}$")
    education: Optional[str] = Field(None, max_length=100)
    coachCategory: Optional[str] = Field(None, max_length=64)
    changeReason: Optional[str] = Field(None, max_length=200)
    version: int = Field(ge=0)


class MyProfileUpdate(StrictSchema):
    """PUT /me/profile 本人档案更新（字段集=前端 MyProfileView.save 实际提交，L59-66）。

    - version 必填乐观锁（2026-10-24 用户裁定：与 /people 写链同规则；
      缺失/非法 → 400 INVALID_ARGUMENT 统一校验错误；不匹配 → 409 VERSION_CONFLICT）；
    - kinds/schoolOrgId/phone/idNumber/avatarFileId 不接受：身份、归属、联系方式与
      证件属管理员/更正链路（页面声明"身份证号与手机号需通过管理员复核后变更"）；
    - schoolName/education/coachCategory：前端提交但冻结模型无对应持久列
      （D2 在案）——接受不持久化不回显，不虚构成功（回显为空=预期非缺陷）；
    - birthDate 与 /people 写链同为全量替换语义（缺省=清空）。
    """

    realName: str = Field(min_length=1, max_length=100)
    gender: Optional[str] = Field(None, max_length=20)
    birthDate: Optional[date] = None
    schoolName: Optional[str] = Field(None, max_length=100)
    education: Optional[str] = Field(None, max_length=100)
    coachCategory: Optional[str] = Field(None, max_length=64)
    version: int = Field(ge=0)


class ProfileConfirmation(StrictSchema):
    """POST /me/profile-confirmation 请求（接口文档 §6.5 ProfileConfirmation）。

    version 必填乐观锁（文档定稿；与 PUT /me/profile 同规则：缺失/非法=400
    统一校验错误，不匹配=409）。前端 confirmProfile 现不传 body（index.ts:67），
    与文档契约不符——按文档实现，前端待办与 updateProfile 同款：读取详情后
    将 version 原样回传。
    """

    version: int = Field(ge=0)


def _synthesized_kinds(person) -> list[str]:
    """kinds 合成（方案C）：扩展档案行存在性为事实源，固定 ATHLETE→COACH 顺序。

    列表查询用 annotate 的 athlete_profile_exists/coach_profile_exists（免 N+1）；
    单对象（创建/更新返回值）回退访问 1:1 反向关系（不存在抛 ObjectDoesNotExist）。
    """
    kinds: list[str] = []
    athlete_flag = getattr(person, "athlete_profile_exists", None)
    if athlete_flag is not None:
        has_athlete = bool(athlete_flag)
    else:
        try:
            has_athlete = person.athlete_profile is not None
        except ObjectDoesNotExist:
            has_athlete = False
    if has_athlete:
        kinds.append("ATHLETE")
    coach_flag = getattr(person, "coach_profile_exists", None)
    if coach_flag is not None:
        has_coach = bool(coach_flag)
    else:
        try:
            has_coach = person.coach_profile is not None
        except ObjectDoesNotExist:
            has_coach = False
    if has_coach:
        kinds.append("COACH")
    return kinds


def _school_org_id_out(person):
    """schoolOrgId 输出：组织节点 ID（A′-min 契约，2026-10-25 确认）。

    列表/详情经 people_queryset 的 school_org_id_val 注解免 N+1；注解缺失
    （孤立调用）时按 School.code 反查兜底（school 已 select_related，至多
    一次额外查询）。学校无 code 或无对应组织节点 → None（不虚构）。
    """
    annotated = getattr(person, "school_org_id_val", None)
    if annotated:
        return annotated
    if not person.school_id:
        return None
    from apps.accounts.models import Organization

    code = getattr(person.school, "code", None)
    if not code:
        return None
    return Organization.objects.filter(code=code).values_list("pk", flat=True).first()


def person_out(person) -> dict:
    """冻结 Person(+扩展档案注解列) → zuqiu Person 字段映射。

    已知映射差异（A/B/C/D 报告在案，查询链不擅自定案）：
    - kinds：**方案C 承载（2026-10-24 用户拍板：前端多选为准）**——冻结
      person_category 单值列存主类别（提交首项），AthleteProfile/CoachProfile
      扩展档案行存在性承载完整类别集合（冻结模型天然支持各 0..1 行），
      输出按行存在性合成、固定 ATHLETE→COACH 顺序；
    - schoolOrgId：**A′-min 契约（2026-10-25 确认）**——输入输出均为组织节点
      ID；输出经 people_queryset 的 school_org_id_val 注解（School.code →
      Organization 主键，免 N+1），注解缺失时按 school.code 反查兜底；
      学校无 code 或无对应组织节点 → None（不虚构）；
    - coachCategory：**不输出**（用户 2026-10-24 决策）——冻结列 coach_level 语义为
      "教练等级"，与前端 coachCategory"教练类别"无法确认同一，字典清单（成员5）
      未交付前停止输出该映射，避免错误值进入前端表单；前端字段为可选，读取侧
      取不到即 undefined（编辑表单该项显示为空），无解析错误；
    - education：冻结无承载列，不输出（承载方案待决策，见核查报告）；
    - avatarUrl：mock 发明的公开直链，违反"附件禁止公开直链"基线，不输出；
      头像正式字段=avatarFileId ← photo_file_id，展示走受控下载 /files/{id}/content；
    - reviewStatus 直出 audit_status（冻结含 DRAFT，前端类型未定义该值）。
    """
    student_no = getattr(person, "student_no_val", None)
    kinds = _synthesized_kinds(person)
    return {
        "id": str(person.pk),
        "kinds": kinds,
        "realName": person.full_name,
        "maskedIdNumber": person.id_card_masked,
        "maskedPhone": person.phone_masked,
        "maskedStudentNumber": (
            crypto.mask_student_number(student_no) if student_no else None
        ),
        "gender": GENDER_OUT.get(person.gender, "UNSPECIFIED"),
        "birthDate": person.birth_date.isoformat() if person.birth_date else None,
        "schoolOrgId": _school_org_id_out(person),
        "schoolName": person.school.name if person.school_id else None,
        "teamName": getattr(person, "team_name_val", None),
        "avatarFileId": person.photo_file_id,
        "reviewStatus": person.audit_status,
        "version": person_version(person),
        "createdAt": person.created_at,
    }
