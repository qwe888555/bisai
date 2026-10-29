"""报名域接口（django-ninja）。

视图层零 ORM、零信封拼接、零角色 if：
- 角色/权限点由 require() 完成，数据范围与业务规则在 services/scope；
- 二进制接口（PDF/Word/ZIP/Excel/CSV/模板）直接返回文件流，不走 JSON 信封；
- submit / reviews 先过权限再读幂等结果，成功后 commit。
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from django.http import HttpResponse
from ninja import File, Form, Router, Status
from ninja.files import UploadedFile

from apps.common import idempotency
from apps.common.authentication import JWTAuth
from apps.common.envelope import action_result, ok
from apps.common.errors import INVALID_ARGUMENT, ApiError
from apps.common.middleware import audit_state
from apps.common.pagination import paginate, paginate_list
from apps.common.permissions import (
    ADMIN,
    CITY,
    PROVINCE_REVIEWER,
    TEAM,
    UNIVERSITY,
    register_operation,
    require,
)
from apps.common.schemas import EnvelopeOut

from . import documents, imports, services, statistics
from . import schemas as s
from .models import ENROLLMENT_STATUSES, SUBJECT_TYPES

router = Router(tags=["enrollments"])
_auth = JWTAuth()

# 角色元组：R5 全业务角色（只读/名单/材料维护）；R1 仅学校端（报名单写操作）；
# R3 审核/统计（ADMIN 依口径可代审，但不代建代提）。
R5 = (TEAM, UNIVERSITY, CITY, PROVINCE_REVIEWER, ADMIN)
R1 = (UNIVERSITY,)
R3 = (CITY, PROVINCE_REVIEWER, ADMIN)

# 权限点登记（导入期完成，漏登记 require() 会直接报错）
register_operation("formConfig", R5)
register_operation("enrollmentList", R5)
register_operation("enrollmentCreate", R1)
register_operation("enrollmentGet", R5)
register_operation("enrollmentUpdate", R1)
register_operation("enrollmentDelete", R1)
register_operation("rosterList", R5)
register_operation("rosterCreate", R5)
register_operation("rosterGet", R5)
register_operation("rosterUpdate", R5)
register_operation("rosterDelete", R5)
# E-2（v7 已批准）：资格四操作仅 TEAM+UNIVERSITY 在自身数据范围内执行，
# 不复用 R5；CITY/PROVINCE_REVIEWER 只读、ADMIN 本期不开放（未注册→403）。
register_operation("rosterQualify", (TEAM, UNIVERSITY))
register_operation("precheck", R5)
register_operation("submitEnrollment", R1)
register_operation("withdrawEnrollment", R1)
register_operation("reviewEnrollment", R3)
register_operation("reviewHistory", R5)
register_operation("snapshotGet", R5)
register_operation("attachmentCreate", R5)
register_operation("attachmentList", R5)
register_operation("attachmentDelete", R5)
register_operation("rosterTemplate", R5)
register_operation("rosterImport", R5)
register_operation("photoImport", R5)
register_operation("importErrors", R5)
register_operation("enrollmentPdf", R5)
register_operation("rosterWord", R5)
register_operation("registrationBundle", R5)
register_operation("enrollmentExcel", R3)
register_operation("enrollmentStats", R3)

# 2026-10-28 待决 11 方案二 2a：写侧/表单配置用当前业务值（SUBJECT_TYPES 已收敛，
# 无 UNIVERSITY）；历史列表查询需按 UNIVERSITY 筛历史行，单列历史兼容常量，
# 仅用于 list 过滤，不回灌表单配置或写入校验。
_SUBJECT_TYPES = tuple(item[0] for item in SUBJECT_TYPES)
_HISTORICAL_SUBJECT_TYPES = _SUBJECT_TYPES + ("UNIVERSITY",)
_STATUSES = tuple(item[0] for item in ENROLLMENT_STATUSES)
_DIMENSIONS = ("CITY", "UNIVERSITY", "GROUP")
_IMPORT_MODES = ("VALIDATE_ONLY", "APPLY_VALID_ROWS")


def _enum(value, allowed, field: str):
    """枚举参数校验；空值视为未提供。"""
    if value is None or value == "":
        return None
    if value not in allowed:
        raise ApiError(
            INVALID_ARGUMENT,
            f"{field} 取值非法",
            [{"field": field, "reason": "可选：" + ",".join(allowed)}],
        )
    return value


def _binary(data: bytes, filename: str, content_type: str, inline: bool = False):
    """二进制下载：不包 JSON 信封，文件名按 RFC 5987 编码。"""
    response = HttpResponse(data, content_type=content_type)
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = (
        f"{disposition}; filename*=UTF-8''{quote(filename)}"
    )
    response["Cache-Control"] = "private, no-store"
    return response


# ---------------------------------------------------------------- 表单配置


@router.get(
    "/enrollment-form-config",
    auth=_auth,
    response=EnvelopeOut,
    summary="报名表字段和材料要求",
)
def form_config(
    request,
    eventId: str,
    subjectType: str,
    groupId: Optional[str] = None,
):
    actor = require("formConfig")
    _enum(subjectType, _SUBJECT_TYPES, "subjectType")
    return ok(services.form_config(actor, eventId, groupId, subjectType))


# ---------------------------------------------------------------- 报名单


@router.get("/enrollments", auth=_auth, response=EnvelopeOut, summary="分页查询报名单")
def enrollment_list(
    request,
    eventId: Optional[str] = None,
    groupId: Optional[str] = None,
    teamName: Optional[str] = None,
    cityOrgId: Optional[str] = None,
    subjectType: Optional[str] = None,
    status: Optional[str] = None,
):
    actor = require("enrollmentList")
    filters = {
        "eventId": eventId,
        "groupId": groupId,
        "teamName": teamName,
        "cityOrgId": cityOrgId,
        "subjectType": _enum(subjectType, _HISTORICAL_SUBJECT_TYPES, "subjectType"),
        "status": _enum(status, _STATUSES, "status"),
    }
    queryset = services.list_enrollments(actor, filters)
    return paginate(request, queryset, services.to_summary)


@router.post("/enrollments", auth=_auth, response=EnvelopeOut, summary="创建报名单")
def enrollment_create(request, body: s.EnrollmentCreateIn):
    actor = require("enrollmentCreate")
    data = services.create_enrollment(actor, body)
    audit_state(
        request,
        "enrollment.create",
        object_type="Enrollment",
        object_id=data["id"],
        summary=f"创建报名单 {data['id']}",
    )
    return ok(data)


@router.get(
    "/enrollments/{enrollment_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="报名单详情",
)
def enrollment_get(request, enrollment_id: str):
    actor = require("enrollmentGet")
    return ok(services.get_enrollment(actor, enrollment_id))


@router.put(
    "/enrollments/{enrollment_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="修改报名单",
)
def enrollment_update(request, enrollment_id: str, body: s.EnrollmentUpdateIn):
    actor = require("enrollmentUpdate")
    data = services.update_enrollment(actor, enrollment_id, body)
    audit_state(
        request,
        "enrollment.update",
        object_type="Enrollment",
        object_id=enrollment_id,
        summary="修改报名单",
    )
    return ok(data)


@router.delete(
    "/enrollments/{enrollment_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="删除报名单",
)
def enrollment_delete(request, enrollment_id: str, version: int):
    actor = require("enrollmentDelete")
    object_id, current_version = services.delete_enrollment(
        actor, enrollment_id, version
    )
    audit_state(
        request,
        "enrollment.delete",
        object_type="Enrollment",
        object_id=object_id,
        summary="删除报名单",
    )
    return action_result(True, object_id, current_version)


# ---------------------------------------------------------------- 名单


@router.get(
    "/enrollments/{enrollment_id}/players",
    auth=_auth,
    response=EnvelopeOut,
    summary="分页查询报名名单条目",
)
def roster_list(
    request, enrollment_id: str, athleteName: Optional[str] = None
):
    actor = require("rosterList")
    queryset = services.list_roster(actor, enrollment_id, athleteName)
    return paginate(request, queryset, services.to_roster)


@router.post(
    "/enrollments/{enrollment_id}/players",
    auth=_auth,
    response=EnvelopeOut,
    summary="创建报名名单条目",
)
def roster_create(request, enrollment_id: str, body: s.RosterItemCreateIn):
    actor = require("rosterCreate")
    data = services.create_roster(actor, enrollment_id, body)
    audit_state(
        request,
        "enrollment.roster.create",
        object_type="RosterItem",
        object_id=data["id"],
        summary="新增名单条目",
    )
    return ok(data)


@router.get(
    "/enrollments/{enrollment_id}/players/{roster_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="报名名单条目详情",
)
def roster_get(request, enrollment_id: str, roster_id: str):
    actor = require("rosterGet")
    return ok(services.get_roster(actor, enrollment_id, roster_id))


@router.put(
    "/enrollments/{enrollment_id}/players/{roster_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="修改报名名单条目",
)
def roster_update(
    request, enrollment_id: str, roster_id: str, body: s.RosterItemUpdateIn
):
    actor = require("rosterUpdate")
    # D-1（2026-10-29 批准）：换人审计摘要——旧/新 athleteId 与凭证失效
    # 记入现有 audit_state 摘要；审计落库仍为响应后写入，非事务原子（既有边界）。
    old = services.get_roster(actor, enrollment_id, roster_id)
    data = services.update_roster(actor, enrollment_id, roster_id, body)
    summary = "修改名单条目"
    if old["athleteId"] != data["athleteId"]:
        summary = (
            f"修改名单条目 athleteId={old['athleteId']}→{data['athleteId']}"
            "（资格凭证已失效）"
        )
    audit_state(
        request,
        "enrollment.roster.update",
        object_type="RosterItem",
        object_id=roster_id,
        summary=summary,
    )
    return ok(data)


@router.delete(
    "/enrollments/{enrollment_id}/players/{roster_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="删除报名名单条目",
)
def roster_delete(request, enrollment_id: str, roster_id: str, version: int):
    actor = require("rosterDelete")
    object_id, current_version = services.delete_roster(
        actor, enrollment_id, roster_id, version
    )
    audit_state(
        request,
        "enrollment.roster.delete",
        object_type="RosterItem",
        object_id=object_id,
        summary="删除名单条目",
    )
    return action_result(True, object_id, current_version)


@router.put(
    "/enrollments/{enrollment_id}/players/{roster_id}/qualification",
    auth=_auth,
    response=EnvelopeOut,
    summary="报名名单条目资格确认（E-2：确认/驳回/剔除/撤销凭证）",
)
def roster_qualify(
    request, enrollment_id: str, roster_id: str, body: s.RosterQualificationIn
):
    actor = require("rosterQualify")
    data = services.qualify_roster(actor, enrollment_id, roster_id, body)
    audit_state(
        request,
        "enrollment.roster.qualify",
        object_type="RosterItem",
        object_id=roster_id,
        summary=f"资格操作 {body.action}",
    )
    return ok(data)


# ---------------------------------------------------------------- 预检 / 提交 / 审核


@router.post(
    "/enrollments/{enrollment_id}/precheck",
    auth=_auth,
    response=EnvelopeOut,
    summary="报名规则预检",
)
def precheck(request, enrollment_id: str, body: s.VersionRequestIn):
    actor = require("precheck")
    return ok(services.precheck_enrollment(actor, enrollment_id, body.version))


@router.post(
    "/enrollments/{enrollment_id}/submit",
    auth=_auth,
    response=EnvelopeOut,
    summary="提交或驳回后重新提交",
)
def submit_enrollment(request, enrollment_id: str, body: s.VersionRequestIn):
    actor = require("submitEnrollment")
    g = idempotency.guard(
        request, "submitEnrollment", object_id=enrollment_id, payload=body
    )
    if g.replay is not None:
        # ninja 1.7 起弃用 (status_code, body) 元组响应，统一用 Status 包装
        status_code, replay_body = g.replay
        return Status(status_code, replay_body)
    data = services.submit_enrollment(actor, enrollment_id, body.version)
    envelope = ok(data)
    g.commit(envelope)
    audit_state(
        request,
        "enrollment.submit",
        object_type="Enrollment",
        object_id=enrollment_id,
        summary=f"提交报名（第 {data['submissionNo']} 次）",
    )
    return envelope


@router.post(
    "/enrollments/{enrollment_id}/withdraw",
    auth=_auth,
    response=EnvelopeOut,
    summary="撤回待审报名",
)
def withdraw_enrollment(
    request, enrollment_id: str, body: s.ReasonVersionRequestIn
):
    actor = require("withdrawEnrollment")
    data = services.withdraw_enrollment(
        actor, enrollment_id, body.version, body.reason
    )
    audit_state(
        request,
        "enrollment.withdraw",
        object_type="Enrollment",
        object_id=enrollment_id,
        summary="撤回待审报名",
    )
    return ok(data)


@router.post(
    "/enrollments/{enrollment_id}/reviews",
    auth=_auth,
    response=EnvelopeOut,
    summary="市州/省级通过或驳回",
)
def review_enrollment(
    request, enrollment_id: str, body: s.ReviewDecisionIn
):
    actor = require("reviewEnrollment")
    g = idempotency.guard(
        request, "reviewEnrollment", object_id=enrollment_id, payload=body
    )
    if g.replay is not None:
        # ninja 1.7 起弃用 (status_code, body) 元组响应，统一用 Status 包装
        status_code, replay_body = g.replay
        return Status(status_code, replay_body)
    data = services.review_enrollment(actor, enrollment_id, body)
    envelope = ok(data)
    g.commit(envelope)
    audit_state(
        request,
        "enrollment.review",
        object_type="Enrollment",
        object_id=enrollment_id,
        summary=f"审核报名：{body.decision}",
    )
    return envelope


@router.get(
    "/enrollments/{enrollment_id}/reviews",
    auth=_auth,
    response=EnvelopeOut,
    summary="报名审核及提交历史",
)
def review_history(request, enrollment_id: str):
    actor = require("reviewHistory")
    queryset = services.list_reviews(actor, enrollment_id)
    return paginate(request, queryset, services.to_review)


@router.get(
    "/enrollments/{enrollment_id}/snapshots/{snapshot_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="查看指定提交快照",
)
def snapshot_get(request, enrollment_id: str, snapshot_id: str):
    actor = require("snapshotGet")
    return ok(services.get_snapshot(actor, enrollment_id, snapshot_id))


# ---------------------------------------------------------------- 材料


@router.post(
    "/enrollments/{enrollment_id}/attachments",
    auth=_auth,
    response=EnvelopeOut,
    summary="关联盖章/学籍材料",
)
def attachment_create(
    request, enrollment_id: str, body: s.EnrollmentAttachmentCreateIn
):
    actor = require("attachmentCreate")
    data = services.create_attachment(actor, enrollment_id, body)
    audit_state(
        request,
        "enrollment.attachment.create",
        object_type="EnrollmentAttachment",
        object_id=data["id"],
        summary="关联报名材料",
    )
    return ok(data)


@router.get(
    "/enrollments/{enrollment_id}/attachments",
    auth=_auth,
    response=EnvelopeOut,
    summary="报名材料清单",
)
def attachment_list(request, enrollment_id: str):
    actor = require("attachmentList")
    queryset = services.list_attachments(actor, enrollment_id)
    return paginate(request, queryset, services.to_attachment)


@router.delete(
    "/enrollments/{enrollment_id}/attachments/{attachment_id}",
    auth=_auth,
    response=EnvelopeOut,
    summary="移除当前草稿材料关联",
)
def attachment_delete(
    request, enrollment_id: str, attachment_id: str, version: int
):
    actor = require("attachmentDelete")
    object_id, current_version = services.delete_attachment(
        actor, enrollment_id, attachment_id, version
    )
    audit_state(
        request,
        "enrollment.attachment.delete",
        object_type="EnrollmentAttachment",
        object_id=object_id,
        summary="移除报名材料关联",
    )
    return action_result(True, object_id, current_version)


# ---------------------------------------------------------------- 导入


@router.get(
    "/imports/templates/roster.xlsx",
    auth=_auth,
    response=None,
    summary="下载固定球员导入模板",
)
def roster_template(request):
    require("rosterTemplate")
    return _binary(
        imports.render_roster_template(),
        "球员导入模板.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.post(
    "/enrollments/{enrollment_id}/imports/players",
    auth=_auth,
    response=EnvelopeOut,
    summary="球员Excel校验/导入",
)
def roster_import(
    request,
    enrollment_id: str,
    version: int,
    mode: str = Form(...),
    file: UploadedFile = File(...),
):
    actor = require("rosterImport")
    _enum(mode, _IMPORT_MODES, "mode")
    result = imports.import_players(actor, enrollment_id, version, mode, file)
    audit_state(
        request,
        "enrollment.import.players",
        object_type="ImportBatch",
        object_id=result["batchId"],
        summary=f"球员导入 {mode}",
    )
    return ok(result)


@router.post(
    "/enrollments/{enrollment_id}/imports/photos",
    auth=_auth,
    response=EnvelopeOut,
    summary="照片ZIP匹配上传",
)
def photo_import(
    request,
    enrollment_id: str,
    version: int,
    file: UploadedFile = File(...),
):
    actor = require("photoImport")
    result = imports.import_photos(actor, enrollment_id, version, file)
    audit_state(
        request,
        "enrollment.import.photos",
        object_type="ImportBatch",
        object_id=result["batchId"],
        summary="照片导入",
    )
    return ok(result)


@router.get(
    "/imports/{batch_id}/errors.csv",
    auth=_auth,
    response=None,
    summary="下载导入错误/未匹配清单",
)
def import_errors(request, batch_id: str):
    actor = require("importErrors")
    data, filename = imports.import_errors_csv(actor, batch_id)
    return _binary(data, filename, "text/csv; charset=utf-8")


# ---------------------------------------------------------------- 文档与导出


@router.get(
    "/enrollments/{enrollment_id}/documents/registration.pdf",
    auth=_auth,
    response=None,
    summary="报名表PDF预览/下载",
)
def enrollment_pdf(
    request,
    enrollment_id: str,
    snapshotId: Optional[str] = None,
    inline: bool = False,
):
    actor = require("enrollmentPdf")
    enrollment, snapshot = services.resolve_snapshot(
        actor, enrollment_id, snapshotId
    )
    data = documents.render_registration_pdf(enrollment, snapshot)
    filename = services.safe_document_name(enrollment, "报名表.pdf")
    return _binary(data, filename, "application/pdf", inline=inline)


@router.get(
    "/enrollments/{enrollment_id}/documents/players.docx",
    auth=_auth,
    response=None,
    summary="下载球员报名Word表",
)
def roster_word(
    request, enrollment_id: str, snapshotId: Optional[str] = None
):
    actor = require("rosterWord")
    enrollment, snapshot = services.resolve_snapshot(
        actor, enrollment_id, snapshotId
    )
    data = documents.render_players_docx(enrollment, snapshot)
    filename = services.safe_document_name(enrollment, "球员报名表.docx")
    return _binary(
        data,
        filename,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.post(
    "/enrollment-exports/documents.zip",
    auth=_auth,
    response=None,
    summary="按筛选/选中批量导出报名文档",
)
def registration_bundle(request, body: s.EnrollmentExportIn):
    actor = require("registrationBundle")
    enrollments = services.export_enrollments(actor, body)
    data = documents.build_documents_zip(enrollments)
    audit_state(
        request,
        "enrollment.export.documents",
        object_type="Enrollment",
        summary=f"批量导出报名表 {len(enrollments)} 份",
    )
    return _binary(data, "报名文档.zip", "application/zip")


@router.post(
    "/enrollment-exports/registrations.xlsx",
    auth=_auth,
    response=None,
    summary="报名情况/全市汇总导出Excel",
)
def enrollment_excel(request, body: s.EnrollmentExportIn):
    actor = require("enrollmentExcel")
    enrollments = services.export_enrollments(actor, body)
    data = documents.export_registrations_xlsx(enrollments)
    audit_state(
        request,
        "enrollment.export.excel",
        object_type="Enrollment",
        summary=f"导出报名汇总 {len(enrollments)} 条",
    )
    return _binary(
        data,
        "报名情况汇总.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------- 统计


@router.get(
    "/enrollment-statistics",
    auth=_auth,
    response=EnvelopeOut,
    summary="市州/高校/组别报名统计",
)
def enrollment_stats(
    request,
    eventId: str,
    dimension: str,
    cityOrgId: Optional[str] = None,
    groupId: Optional[str] = None,
):
    actor = require("enrollmentStats")
    _enum(dimension, _DIMENSIONS, "dimension")
    rows = statistics.enrollment_statistics(
        actor, eventId, dimension, cityOrgId, groupId
    )
    return paginate_list(request, rows)
