"""人员档案批量导入（人员档案与账号流程后端改造说明 §3）。

- 模板固定 8 列，表头必须完全一致（与报名名单导入同一策略）；
- 单文件最多 500 行，超过直接拒绝；
- VALIDATE_ONLY 只校验不落库，APPLY_VALID_ROWS 写入有效行、逐行隔离错误；
- 学校账号只能导入本校人员；管理员可指定 schoolOrgId 或由行内学校解析；
- 错误清单以 JSON 存在批次表，GET /people/imports/{batchId}/errors.csv 下载。
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from typing import Optional

from django.db import transaction

from apps.common import crypto
from apps.common.errors import (
    BATCH_LIMIT_EXCEEDED,
    DUPLICATE_RESOURCE,
    FORBIDDEN,
    INVALID_ARGUMENT,
    NOT_FOUND,
    UNSUPPORTED_FILE_TYPE,
    ApiError,
)
from apps.common.ids import new_id

from . import services
from .models import Person, PersonImportBatch, School

TEMPLATE_HEADERS = (
    "姓名",
    "身份证号",
    "手机号",
    "人员类型",
    "学校",
    "学历",
    "性别",
    "出生日期",
)
MAX_ROWS = 500
IMPORT_MODES = ("VALIDATE_ONLY", "APPLY_VALID_ROWS")

_KIND_MAP = {
    "运动员": "ATHLETE",
    "ATHLETE": "ATHLETE",
    "教练员": "COACH",
    "COACH": "COACH",
}
_GENDER_MAP = {
    "男": "MALE",
    "MALE": "MALE",
    "女": "FEMALE",
    "FEMALE": "FEMALE",
    "未说明": "UNSPECIFIED",
    "未指定": "UNSPECIFIED",
    "UNSPECIFIED": "UNSPECIFIED",
}
_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CHECK_CODES = "10X98765432"


def valid_id_number(value: str) -> bool:
    """18 位身份证 + ISO 7064 MOD 11-2 校验位。"""
    text = (value or "").strip().upper()
    if not re.fullmatch(r"\d{17}[\dX]", text):
        return False
    total = sum(int(text[index]) * _WEIGHTS[index] for index in range(17))
    return _CHECK_CODES[total % 11] == text[17]


def render_template() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "人员档案"
    sheet.append(list(TEMPLATE_HEADERS))
    fill = PatternFill("solid", fgColor="D9D9D9")
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")
    # 身份证号/手机号按文本列写入，避免 Excel 转科学计数法
    for row in range(2, MAX_ROWS + 2):
        sheet.cell(row=row, column=2).number_format = "@"
        sheet.cell(row=row, column=3).number_format = "@"
    for index, width in enumerate((12, 22, 14, 12, 24, 14, 10, 14), start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    # Excel 数值单元格会把身份证/手机号读成 110101...1234.0
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def _read_rows(uploaded) -> list[tuple[int, list[str]]]:
    name = (getattr(uploaded, "name", "") or "").lower()
    if not name.endswith(".xlsx"):
        raise ApiError(
            UNSUPPORTED_FILE_TYPE,
            "人员档案导入仅支持 xlsx",
            [{"field": "file", "reason": "需要 .xlsx"}],
        )
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(uploaded.read()), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            UNSUPPORTED_FILE_TYPE,
            "无法解析该 xlsx 文件",
            [{"field": "file", "reason": "文件损坏或格式不符"}],
        ) from exc
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise ApiError(INVALID_ARGUMENT, "模板为空")
    header = tuple(_text(cell) for cell in rows[0][: len(TEMPLATE_HEADERS)])
    if header != TEMPLATE_HEADERS:
        raise ApiError(
            INVALID_ARGUMENT,
            "表头与固定模板不一致",
            [{"field": "file", "reason": "表头必须为：" + " | ".join(TEMPLATE_HEADERS)}],
        )
    data_rows: list[tuple[int, list[str]]] = []
    for index, raw in enumerate(rows[1:], start=2):
        values = [_text(cell) for cell in raw[: len(TEMPLATE_HEADERS)]]
        if not any(values):
            continue
        data_rows.append((index, values))
    if len(data_rows) > MAX_ROWS:
        raise ApiError(
            BATCH_LIMIT_EXCEEDED,
            f"单次最多导入 {MAX_ROWS} 行，请拆分文件",
            [{"field": "file", "reason": "行数超限"}],
        )
    return data_rows


def _row_error(row_number: int, field: str, code: str, message: str) -> dict:
    return {
        "rowNumber": row_number,
        "field": field,
        "code": code,
        "message": message,
    }


def _resolve_school_org(actor, raw: str, fallback_org_id: str):
    """行内「学校」→ 组织节点；支持组织编码或学校全称。"""
    from apps.accounts.models import Organization

    text = (raw or "").strip()
    if text:
        orgs = list(
            Organization.objects.filter(
                code=text, org_type__in=("SCHOOL", "UNIVERSITY"), enabled=True
            )[:2]
        )
        if len(orgs) == 1:
            return orgs[0]
        if len(orgs) > 1:
            raise ApiError(INVALID_ARGUMENT, "学校编码不唯一")
        schools = list(School.objects.filter(name=text, is_active=True)[:2])
        if len(schools) > 1:
            raise ApiError(INVALID_ARGUMENT, "学校名称重复，请填写学校编码")
        if schools:
            org = Organization.objects.filter(
                code=schools[0].code,
                org_type__in=("SCHOOL", "UNIVERSITY"),
                enabled=True,
            ).first()
            if org is not None:
                return org
        raise ApiError(INVALID_ARGUMENT, "学校不存在或未启用")

    org_id = (fallback_org_id or "").strip()
    if not org_id and "UNIVERSITY" in (getattr(actor, "roles", ()) or ()):
        org_id = getattr(actor, "organization_id", "") or ""
    org = (
        Organization.objects.filter(
            pk=org_id, org_type__in=("SCHOOL", "UNIVERSITY"), enabled=True
        ).first()
        if org_id
        else None
    )
    if org is None:
        raise ApiError(INVALID_ARGUMENT, "学校必填")
    return org


def _build_payload(actor, values: list[str], fallback_org_id: str):
    name, id_number, phone, kind_text, school_text, education, gender_text, birth_text = values
    if not name:
        raise ApiError(INVALID_ARGUMENT, "姓名必填")
    if not id_number:
        raise ApiError(INVALID_ARGUMENT, "身份证号必填")
    if not valid_id_number(id_number):
        raise ApiError(INVALID_ARGUMENT, "身份证号格式或校验位不正确")
    if not phone:
        raise ApiError(INVALID_ARGUMENT, "手机号必填")
    if not re.fullmatch(r"1\d{10}", phone):
        raise ApiError(INVALID_ARGUMENT, "手机号格式不正确")
    kind = _KIND_MAP.get(kind_text.strip().upper())
    if kind is None:
        raise ApiError(INVALID_ARGUMENT, "人员类型仅支持运动员/教练员")
    org = _resolve_school_org(actor, school_text, fallback_org_id)
    services._ensure_school_in_operator_scope(org.pk, actor, required=True)
    services._resolve_school(org.pk)

    gender = ""
    if gender_text:
        gender = _GENDER_MAP.get(gender_text.strip().upper())
        if gender is None:
            raise ApiError(INVALID_ARGUMENT, "性别仅支持 男/女/未说明")
    birth = None
    if birth_text:
        try:
            birth = date.fromisoformat(birth_text)
        except ValueError:
            raise ApiError(INVALID_ARGUMENT, "出生日期格式应为 YYYY-MM-DD") from None

    return {
        "realName": name,
        "kinds": [kind],
        "idNumber": id_number.upper(),
        "phone": phone,
        "schoolOrgId": str(org.pk),
        "education": education or None,
        "gender": gender,
        "birthDate": birth,
    }


def _error_parts(exc: ApiError) -> tuple[str, str]:
    data = exc.data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return (
            str(data[0].get("field") or ""),
            str(data[0].get("reason") or exc.message),
        )
    return "", exc.message


def import_people(actor, mode: str, fallback_org_id: Optional[str], uploaded) -> dict:
    if mode not in IMPORT_MODES:
        raise ApiError(
            INVALID_ARGUMENT,
            "mode 只支持 VALIDATE_ONLY / APPLY_VALID_ROWS",
            [{"field": "mode", "reason": "VALIDATE_ONLY 或 APPLY_VALID_ROWS"}],
        )
    rows = _read_rows(uploaded)
    batch_id = new_id("pib")
    errors: list[dict] = []
    success_count = 0
    seen_ids: set[str] = set()

    with transaction.atomic():
        batch = PersonImportBatch.objects.create(
            id=batch_id,
            mode=mode,
            total=len(rows),
            created_by=actor.pk,
        )
        for row_number, values in rows:
            try:
                payload = _build_payload(actor, values, fallback_org_id or "")
                id_hash = crypto.lookup_hash(payload["idNumber"])
                if id_hash in seen_ids:
                    raise ApiError(
                        DUPLICATE_RESOURCE,
                        "文件内身份证号重复",
                    )
                seen_ids.add(id_hash)
                duplicate = Person.objects.filter(id_card_hash=id_hash).first()
                if duplicate is not None:
                    suffix = "（档案已删除）" if duplicate.is_deleted else ""
                    raise ApiError(
                        DUPLICATE_RESOURCE,
                        f"该证件号已存在档案{suffix}",
                    )
                if mode == "APPLY_VALID_ROWS":
                    services.create_person(payload, actor)
                success_count += 1
            except ApiError as exc:
                field, reason = _error_parts(exc)
                errors.append(_row_error(row_number, field, exc.code, reason))
        batch.success_count = success_count
        batch.failure_count = len(rows) - success_count
        batch.errors = errors
        batch.save(
            update_fields=[
                "success_count",
                "failure_count",
                "errors",
                "updated_at",
            ]
        )

    return {
        "batchId": batch_id,
        "mode": mode,
        "total": len(rows),
        "successCount": success_count,
        "failureCount": len(errors),
        "errors": errors,
    }


def import_errors_csv(actor, batch_id: str) -> tuple[bytes, str]:
    batch = PersonImportBatch.objects.filter(pk=batch_id).first()
    if batch is None:
        raise ApiError(NOT_FOUND)
    if batch.created_by != str(actor.pk) and "ADMIN" not in (
        getattr(actor, "roles", ()) or ()
    ):
        raise ApiError(FORBIDDEN)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["行号", "字段", "错误码", "说明"])
    for error in batch.errors or []:
        writer.writerow(
            [
                error.get("rowNumber", ""),
                error.get("field", ""),
                error.get("code", ""),
                error.get("message", ""),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig"), f"{batch_id}-errors.csv"
