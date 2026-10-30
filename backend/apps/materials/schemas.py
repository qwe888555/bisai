"""材料域请求模型与响应序列化（django-ninja / pydantic v2）。

前端契约（zuqiu 快照 types.ts Material + 5 个消费页面）：
- MaterialCreateBody：ownerType/ownerId/materialType/title/description?/fileName/fileIds；
  冻结 person_material/team_material 无 title 列 → title 持久化到 remark（无损承载
  并回显），description 无承载列 → 声明但不持久化不回显（同 persons education 先例）；
- fileIds 前端是数组但冻结为单 file_id 列（前端实际也只传 1 个）→ 仅接受长度 1；
- reviewStatus 直出冻结 MaterialStatus 真实值（PENDING/APPROVED/REJECTED 本期可达，
  DRAFT/WITHDRAWN 前端类型未定义且无对应调用，不产生）；
- version 按 B4 规则由 updated_at UTC 微秒派生，无新增数据库列。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from apps.common.schemas import StrictSchema


# ---------------------------------------------------------------- 请求

class MaterialCreateBody(StrictSchema):
    ownerType: Literal["PERSON", "TEAM"]
    ownerId: str = Field(min_length=1, max_length=64)
    materialType: str = Field(min_length=1, max_length=50, description="MaterialType.code")
    title: str = Field(min_length=1, max_length=200, description="材料名称（持久化到 remark）")
    description: Optional[str] = Field(
        None, max_length=200,
        description="前端回传契约字段；冻结模型无承载列，接受但不持久化不回显",
    )
    fileName: str = Field(min_length=1, max_length=200)
    fileIds: list[str] = Field(min_length=1, max_length=1, description="冻结单文件列，仅接受 1 个")


class MaterialReviewBody(StrictSchema):
    """审核（POST /materials/{id}/review）：decision APPROVE/REJECT → APPROVED/REJECTED。

    前端 body 仅 {decision, reason?, version}（PeopleManageView/TeamsView）；
    多态归属由服务层双表解析（仅未软删行）：仅一表命中沿用；两表同 id 均为
    有效行时返回 409 STATE_CONFLICT（归属不明确，不静默择一，联调注记：建议
    前端后续补充 ownerType 传参消除歧义）。
    """

    decision: Literal["APPROVE", "REJECT"]
    reason: Optional[str] = Field(None, max_length=200, description="REJECT 时必填（前端强制）")
    version: int = Field(ge=0)


class MaterialUpdateBody(StrictSchema):
    """PUT /materials/{id} 请求体（契约：后端接口文档 §6.6 L1319-1329，批次 D-3）。

    - ownerType/ownerId/materialType 必填但不可变：与当前记录不一致 → 服务层
      400 明确拒绝（不静默忽略、不允许经本接口变更归属或类型）；
    - title（契约 1-150）持久化到 remark（列长 200，无损承载）；
    - description/occurredOn/issuer：契约字段无持久化列——声明以放行，
      服务层不持久化、响应不虚构保存结果；
    - fileIds：契约 1-20，冻结单文件列 → 仅接受 1 个（schema 硬限，与创建同口径）；
    - version 乐观锁（B4 updated_at 微秒派生），不匹配 → 409 VERSION_CONFLICT。
    """

    ownerType: Literal["PERSON", "TEAM"]
    ownerId: str = Field(min_length=1, max_length=64)
    materialType: str = Field(
        min_length=1, max_length=50, description="MaterialType.code；不可变，与现值不一致 → 400"
    )
    title: str = Field(min_length=1, max_length=150, description="持久化到 remark")
    description: Optional[str] = Field(
        None, max_length=2000, description="无持久化列，接受但不持久化不回显"
    )
    occurredOn: Optional[str] = Field(
        None, max_length=10, description="无持久化列，接受但不持久化不回显"
    )
    issuer: Optional[str] = Field(
        None, max_length=150, description="无持久化列，接受但不持久化不回显"
    )
    fileIds: list[str] = Field(min_length=1, max_length=1, description="冻结单文件列，仅接受 1 个")
    version: int = Field(ge=0)


# ---------------------------------------------------------------- 响应

def material_version(material) -> int:
    """B4：updated_at UTC 微秒派生，无新增列。"""
    return int(material.updated_at.timestamp() * 1_000_000)


def material_out(material, kind: str) -> dict:
    """kind: PERSON/TEAM（多态序列化，owner 与 type 由调用方 select_related 预载）。"""
    owner = material.person if kind == "PERSON" else material.team
    return {
        "id": str(material.pk),
        "ownerType": kind,
        "ownerId": str(owner.pk),
        "ownerName": owner.full_name if kind == "PERSON" else owner.name,
        "materialType": material.type.code,
        "materialLabel": material.type.name,
        "title": material.remark or "",
        "fileIds": [material.file_id],
        "fileName": material.file_name,
        "reviewStatus": material.status,
        "reviewReason": material.audit_remark or "",
        "version": material_version(material),
        "createdAt": material.created_at.isoformat() if material.created_at else None,
    }


def audit_log_out(log) -> dict:
    """MaterialAuditLog → 审核历史条目（2026-10-30 问题#9，append-only 只读）。"""
    return {
        "materialKind": log.material_kind,
        "fromStatus": log.from_status,
        "toStatus": log.to_status,
        "auditor": log.auditor,  # 底座账号 ID 映射未定，写入恒空
        "comment": log.comment or "",
        "createdAt": log.created_at.isoformat() if log.created_at else None,
    }
