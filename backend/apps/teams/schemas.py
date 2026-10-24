"""球队域请求/输出 Schema（T4-03 批次 A）。

前端契约对齐（zuqiu 实码）：
- 创建 payload = TeamProfileView.createTeam 实际提交（name/schoolOrgId/
  groupCategory/gender/campusName/intro，L240-247；学校组织未绑定时前端
  已阻止提交 L237）；
- 更新 payload = save() 原样提交表单全部字段 + 三个媒体 fileId + version
  （L205-213）：shortName/schoolName/cityName/campusName/gender/coachName/
  schoolOrgId/groupCategory 属"回传原值或无冻结承载"字段——声明以放行
  正常编辑（extra=forbid 下不声明会 400），服务层不持久化不回显
  （同 persons PersonCreate.education/coachCategory 先例）；
- 媒体映射（冻结列）：flagFileId→flag_file_id、emblemFileId→logo_file_id
  （列注释"队徽"）、groupPhotoFileId→group_photo_file_id；
- version 必填乐观锁（前端 Team.version: number 必填且 save() 已携带；
  派生自 updated_at UTC 微秒，B4 同款）；
- team_out 输出键=前端 Team 类型实际消费集；flagUrl/emblemUrl/groupPhotoUrl
  属受控下载契约（无公开直链），不输出 url（list_own_teams 同款，联调待办）。
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from apps.common import enums
from apps.common.schemas import StrictSchema


class TeamCreateBody(StrictSchema):
    """POST /teams 请求体（键名与前端 createTeam payload 一致）。"""

    name: str = Field(min_length=1, max_length=100)
    groupCategory: str = Field(min_length=1, max_length=20)
    schoolOrgId: Optional[str] = Field(None, min_length=1, max_length=64)
    intro: Optional[str] = None
    # ---- 声明但不持久化（冻结 Team 无对应列）----
    gender: Optional[str] = Field(None, max_length=20)
    campusName: Optional[str] = Field(None, max_length=100)


class TeamUpdateBody(StrictSchema):
    """PUT /teams/{id} 请求体（键名与前端 save() payload 一致；version 必填）。"""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    intro: Optional[str] = None
    flagFileId: Optional[str] = Field(None, max_length=64)
    emblemFileId: Optional[str] = Field(None, max_length=64)
    groupPhotoFileId: Optional[str] = Field(None, max_length=64)
    # ---- 回传原值/无承载：声明但不持久化不回显 ----
    shortName: Optional[str] = Field(None, max_length=100)
    schoolName: Optional[str] = Field(None, max_length=100)
    cityName: Optional[str] = Field(None, max_length=100)
    campusName: Optional[str] = Field(None, max_length=100)
    gender: Optional[str] = Field(None, max_length=20)
    coachName: Optional[str] = Field(None, max_length=100)
    schoolOrgId: Optional[str] = Field(None, min_length=1, max_length=64)
    groupCategory: Optional[str] = Field(None, max_length=20)
    version: int = Field(ge=0)


class MemberSearchBody(StrictSchema):
    """POST /teams/{id}/members/search 正文（前端 TeamMembersView L93 只传 keyword，
    与 persons PersonSearchBody 同款值域约束）。"""

    keyword: str = Field(min_length=1, max_length=50)


class MemberCreateBody(StrictSchema):
    """POST /teams/{id}/members 请求体（键名与前端 addMember payload 一致，
    TeamMembersView L110-114：athleteId/jerseyNumber/position，无 version/日期）。"""

    athleteId: str = Field(min_length=1, max_length=64)
    jerseyNumber: Optional[int] = Field(None, ge=0, le=9999)
    # position_code（冻结 CharField(20)）为字典逻辑引用；正式值域（base_dict_item
    # 'position'）尚未导入且设计文档无正式代码定义（2026-10-24 只读核查）——
    # 按不透明字符串透传存取，不做值域映射/校验，不自行创造字典规则（联调待办）。
    position: Optional[str] = Field(None, max_length=20)


class MemberUpdateBody(StrictSchema):
    """PUT /teams/{id}/members/{member_id} 请求体（version 必填）。

    athleteId/effectiveFrom/effectiveTo：前端 saveMemberEdit 原样回传字段
    （L138-145；effectiveFrom 每次打开编辑被前端置为当天 L129）——声明以放行
    （extra=forbid），服务层一律不应用：人员重绑=移出+重加或转队流程；
    joined_at 映射会给每次编辑重写加入日期，effectiveTo 无冻结承载列。
    """

    athleteId: Optional[str] = Field(None, min_length=1, max_length=64)
    jerseyNumber: Optional[int] = Field(None, ge=0, le=9999)
    position: Optional[str] = Field(None, max_length=20)
    effectiveFrom: Optional[str] = Field(None, max_length=10)
    effectiveTo: Optional[str] = Field(None, max_length=10)
    version: int = Field(ge=0)


def member_out(member) -> dict:
    """成员关系输出（dict 直出）。

    - status 直出冻结枚举 CURRENT/EXITED（用户裁定：不虚构审核状态，
      前端类型域差异登记联调待办）；active=是否现役（派生值，mock 同名键）；
    - effectiveFrom ← joined_at（加入日期即生效起期的真实承载；
      effectiveTo 无冻结列不输出）；position ← position_code 原值透传；
    - maskedIdNumber/avatarFileId ← person 关系列（明文/直链不输出，
      受控下载契约同 L5）。
    """
    person = member.person
    return {
        "id": str(member.pk),
        "teamId": str(member.team_id),
        "athleteId": str(member.person_id),
        "athleteName": person.full_name,
        "maskedIdNumber": person.id_card_masked,
        "avatarFileId": person.photo_file_id,
        "jerseyNumber": member.jersey_number,
        "position": member.position_code,
        "status": member.status,
        "active": member.status == enums.RelationStatus.CURRENT,
        "effectiveFrom": member.joined_at.isoformat() if member.joined_at else None,
        "version": int(member.updated_at.timestamp() * 1_000_000),
    }


def team_out(team) -> dict:
    """球队输出（dict 直出，EnvelopeOut.data=Any 不进 components）。

    member_count/head_coach_name 由 services._annotate_team 注解提供；
    输出不含 orgId/cityOrgId/gender/campusName/shortName/媒体 url（联调待办）。
    """
    return {
        "id": str(team.pk),
        "teamNo": team.team_no,
        "name": team.name,
        "category": team.category_code,
        "schoolName": team.school.name if team.school_id else None,
        "cityName": team.region.name if team.region_id else None,
        "intro": team.introduction,
        "coachName": getattr(team, "head_coach_name", None),
        "memberCount": getattr(team, "member_count", 0),
        "status": team.audit_status,
        "version": int(team.updated_at.timestamp() * 1_000_000),
        "createdAt": team.created_at.isoformat(),
    }
