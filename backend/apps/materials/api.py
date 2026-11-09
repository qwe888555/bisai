"""材料域接口（django-ninja）：/materials。

前端契约（zuqiu 快照 api/index.ts materialApi L127-133 + 5 个消费页面）：
- materialList  GET  /materials                    （page/pageSize/ownerType?/ownerId?）
- materialCreate POST /materials                  （body 全字段，StrictSchema 多传 400）
- materialReview POST /materials/{id}/review      （body {decision, reason?, version}）
- materialDelete DELETE /materials/{id}?version=  （软删）
- materialGet  GET /materials/{id}                （批次 D-2 补齐：文档契约明确，
  多态双表解析与 review/remove 同口径）
- materialUpdate PUT /materials/{id}               （批次 D-3 补齐：title→remark、
  文件替换留痕、乐观锁；归属/类型不可变 → 400，审计与更新同事务）

审核权限（2026-10-28 统一业务规则：人员资格与履历材料由学校端审核；
市州端/省级端只审核赛事报名，不审核材料）由权限点强制，
数据范围见 services._material_scope
（D1/D5 修订：全局 ADMIN 全库无审核权 / UNIVERSITY 本校 / TEAM 本队，
三态口径同 persons A′）。
写操作统一 audit_state()；version 乐观锁按 B4 updated_at 微秒派生。
"""

from __future__ import annotations

from typing import Optional

from ninja import Query, Router

from apps.common.authentication import JWTAuth
from apps.common.envelope import action_result, ok
from apps.common.errors import INVALID_ARGUMENT, ApiError, field_errors
from apps.common.middleware import audit_state
from apps.common.pagination import paginate, paginate_list
from apps.common.permissions import (
    ADMIN,
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

router = Router(tags=["materials"])
_auth = JWTAuth()

# 权限点登记：导入期完成，漏登记 require() 会直接报错。
# 角色按 2026-10-28 统一业务规则 + D1/D5 决策登记：
# - materialReview = UNIVERSITY（学校端审核）+ ADMIN（2026-11-02 管理端全量
#   联调回归：接口契约 docs §materialReview 与实现注记均列 ADMIN，且人员管理/
#   参赛队伍两个管理端页面的「通过/驳回」按钮实际调用该接口；此前仅登记
#   UNIVERSITY 导致管理员点按钮必然 403。数据范围仍由 _material_scope 收口，
#   全局 ADMIN 全库、挂组织 ADMIN 同现有口径）；
# - CITY/PROVINCE_REVIEWER 只审报名，不进 materialReview；保留列表/详情/历史
#   读取用于审核查看，但不再授予 create/update/delete（前端没有对应维护入口，
#   2026-11-09 收敛写权限）；
# - TEAM 有材料维护范围（本队 TEAM 材料，见 services._material_scope），
#   但不进 materialReview（维护权限≠审核权限，一·补四）；
# - ATHLETE/COACH 为本人材料预留；数据范围见 services._material_scope。
_MATERIAL_READ_ROLES = (
    ADMIN,
    UNIVERSITY,
    TEAM,
    PROVINCE_REVIEWER,
    ATHLETE,
    COACH,
    CITY,
)
# 有材料维护入口的角色：人员管理/球队资料/球队材料面板/个人履历材料。
_MATERIAL_WRITE_ROLES = (ADMIN, UNIVERSITY, TEAM, ATHLETE, COACH)
register_operation("materialList", _MATERIAL_READ_ROLES)
register_operation("materialGet", _MATERIAL_READ_ROLES)
register_operation("materialUpdate", _MATERIAL_WRITE_ROLES)
register_operation("materialCreate", _MATERIAL_WRITE_ROLES)
register_operation("materialReview", (ADMIN, UNIVERSITY))
register_operation("materialDelete", _MATERIAL_WRITE_ROLES)
# 审核历史（P1-9 原定任务）：读取类操作，角色集同 materialGet；数据范围同单对象读。
register_operation("materialHistory", _MATERIAL_READ_ROLES)


@router.get("/materials", auth=_auth, response=EnvelopeOut, summary="材料列表")
def material_list(
    request,
    page: Optional[int] = Query(None, ge=1),
    pageSize: Optional[int] = Query(None, ge=1, le=100),
    ownerType: Optional[str] = None,
    ownerId: Optional[str] = None,
    status: Optional[str] = None,
    needAudit: Optional[str] = None,
    keyword: Optional[str] = None,
):
    account = require("materialList")
    rows = services.material_list(
        {
            "ownerType": ownerType,
            "ownerId": ownerId,
            "status": status,
            "needAudit": needAudit,
            "keyword": keyword,
        },
        account,
    )
    # 行为 (material, kind) 元组（多态），先物化再走 paginate_list
    # （paginate 的 count 探测对 list 误判 list.count()，故不直接用 paginate）
    items = [s.material_out(material, kind) for material, kind in rows]
    return paginate_list(request, items)


@router.get(
    "/materials/{material_id}", auth=_auth, response=EnvelopeOut, summary="材料详情"
)
def material_get(request, material_id: str):
    account = require("materialGet")
    material, kind = services.material_get(material_id, account)
    return ok(s.material_out(material, kind))


@router.get(
    "/materials/{material_id}/audit-logs",
    auth=_auth,
    response=EnvelopeOut,
    summary="材料审核历史",
)
def material_audit_logs(request, material_id: str):
    """审核历史（P1-9 原定任务 + D3 分页）：append-only 记录按创建时间正序，
    统一分页规范 page/pageSize（默认 20 上限 100），data 四字段契约。"""
    account = require("materialHistory")
    logs, kind = services.material_audit_logs(material_id, account)
    # 排序稳定性由服务层 order_by("created_at", "id") 保证；分页切片在
    # 数据库完成（QuerySet 走 paginate 数据库分支），不整表物化
    return paginate(request, logs, mapper=lambda log: s.audit_log_out(log, kind))


@router.put(
    "/materials/{material_id}", auth=_auth, response=EnvelopeOut, summary="修改材料"
)
def material_update(request, material_id: str, body: s.MaterialUpdateBody):
    account = require("materialUpdate")
    material, kind = services.material_update(
        material_id, body.model_dump(exclude_unset=True), account
    )
    audit_state(
        request,
        "material.update",
        object_type="Material",
        object_id=f"{kind}:{material.pk}",
        summary=f"更新材料 {material.remark}（{kind}:{material.pk}）",
    )
    return ok(s.material_out(material, kind))


@router.post("/materials", auth=_auth, response=EnvelopeOut, summary="创建材料")
def material_create(request, body: s.MaterialCreateBody):
    account = require("materialCreate")
    material, kind = services.material_create(body.model_dump(exclude_unset=True), account)
    audit_state(
        request,
        "material.create",
        object_type="Material",
        object_id=f"{kind}:{material.pk}",
        summary=f"提交材料 {material.remark}（{kind}:{material.pk}）",
    )
    return ok(s.material_out(material, kind))


@router.post(
    "/materials/{material_id}/review", auth=_auth, response=EnvelopeOut, summary="材料审核"
)
def material_review(request, material_id: str, body: s.MaterialReviewBody):
    account = require("materialReview")
    material, kind = services.material_review(material_id, body.model_dump(exclude_unset=True), account)
    audit_state(
        request,
        "material.review",
        object_type="Material",
        object_id=f"{kind}:{material.pk}",
        summary=f"材料审核 {body.decision}（{kind}:{material.pk}）",
    )
    return ok(s.material_out(material, kind))


@router.delete("/materials/{material_id}", auth=_auth, response=EnvelopeOut, summary="删除材料")
def material_remove(
    request,
    material_id: str,
    version: Optional[int] = Query(None, ge=0),
    ownerType: Optional[str] = Query(None),
):
    account = require("materialDelete")
    if version is None:
        raise ApiError(
            INVALID_ARGUMENT,
            "删除材料必须提交乐观锁版本",
            field_errors([("version", "该操作必须提交乐观锁版本")]),
        )
    if ownerType not in (None, "", "PERSON", "TEAM"):
        raise ApiError(
            INVALID_ARGUMENT,
            "ownerType 仅支持 PERSON/TEAM",
            field_errors([("ownerType", "不在枚举范围内")]),
        )
    services.material_remove(material_id, version, account, ownerType or None)
    audit_state(
        request,
        "material.remove",
        object_type="Material",
        object_id=material_id,
        summary=f"删除材料（id={material_id}）",
    )
    return action_result(True, material_id)
