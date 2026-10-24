"""报名文档生成：报名表 PDF、球员信息表 Word、批量 ZIP、报名汇总 Excel。

默认渲染最新提交快照 payload；草稿按当前工作副本渲染并标注「（草稿）」。
所有输出只含脱敏证件号/手机号/学籍号。
"""

from __future__ import annotations

import io
import zipfile
from typing import Iterable, Optional

from django.utils import timezone

from . import services
from .clients import event_config_client, material_client, person_client, team_client
from .models import Enrollment

SUBJECT_LABELS = {
    "SCHOOL_TEAM": "中小学球队",
    "UNIVERSITY": "高校",
    "CITY": "市州代报",
}

STATUS_LABELS = {
    "DRAFT": "草稿",
    "PENDING_CITY_REVIEW": "待市州审核",
    "PENDING_PROVINCE_REVIEW": "待省级审核",
    "APPROVED": "已通过",
    "REJECTED": "已驳回",
    "WITHDRAWN": "已撤回",
}

MATERIAL_ROWS = (
    ("球队报名表与名单（盖章）", "学校/球队"),
    ("有效身份证明及照片", "学校"),
    ("学籍证明", "学校"),
    ("健康及保险材料", "学校/家长"),
    ("未成年人知情同意材料", "家长"),
    ("转学及特殊资格证明（如涉及）", "学校"),
)


def _org_name(org_id: str) -> str:
    if not org_id:
        return ""
    from apps.accounts.models import Organization

    row = Organization.objects.filter(pk=org_id).values_list("name", flat=True).first()
    return row or org_id


def _draft_context(enrollment: Enrollment) -> dict:
    """草稿：按当前数据渲染。"""
    team = team_client.get_team(enrollment.team_id)
    players = []
    for item in enrollment.players.all():
        person = person_client.get_person(item.athlete_id)
        players.append(
            {
                **services.to_roster(item),
                "gender": person.get("gender"),
                "birthDate": person.get("birthDate"),
                "maskedStudentNumber": person.get("maskedStudentNumber"),
            }
        )
    coaches = [
        team_client.get_coach(coach_id) for coach_id in (team.get("coachIds") or [])
    ]
    return {
        "enrollment": services.to_summary(enrollment),
        "team": team,
        "players": players,
        "coaches": coaches,
        "attachments": [services.to_attachment(a) for a in enrollment.attachments.all()],
        "draft": True,
    }


def build_context(enrollment: Enrollment, snapshot=None) -> dict:
    if snapshot is not None:
        payload = snapshot.payload or {}
        return {
            "enrollment": dict(payload.get("enrollment") or {}),
            "team": payload.get("team") or {},
            "players": payload.get("players") or [],
            "coaches": payload.get("coaches") or [],
            "attachments": payload.get("attachments") or [],
            "draft": False,
            "submittedAt": snapshot.submitted_at,
        }
    return _draft_context(enrollment)


def _material_rows(context: dict) -> list[tuple[str, str, str]]:
    attachments = context.get("attachments") or []
    players = context.get("players") or []
    types = {a.get("materialType") for a in attachments}
    has_student = "STUDENT_STATUS" in types or any(
        player.get("studentStatusFileIds") for player in players
    )
    row_status = {
        "球队报名表与名单（盖章）": "■已上传" if "STAMPED_FORM" in types else "□缺失",
        "学籍证明": "■已上传" if has_student else "□缺失",
    }
    # TODO(确认项#2): 其余 4 类材料契约无枚举，草稿经 material_client 查询，
    # 快照不重新查主档；查不到标"—"并在日志留痕（不谎报缺失）。
    extra: dict[str, str] = {}
    if context.get("draft"):
        try:
            team_materials = material_client.list_materials(
                "TEAM", context["team"].get("id", "")
            )
            person_materials = [
                entry
                for player in players
                for entry in material_client.list_materials(
                    "PERSON", player.get("athleteId", "")
                )
            ]
            found = {
                str(entry.get("type") or "")
                for entry in list(team_materials) + list(person_materials)
            }
            for name, owner_type in (
                ("有效身份证明及照片", "IDENTITY_PHOTO"),
                ("健康及保险材料", "HEALTH_INSURANCE"),
                ("未成年人知情同意材料", "GUARDIAN_CONSENT"),
                ("转学及特殊资格证明（如涉及）", "TRANSFER_CERTIFICATE"),
            ):
                extra[name] = "■已上传" if owner_type in found else "—"
        except Exception:  # noqa: BLE001 - 取不到材料只降级展示，不阻断导出
            extra = {}
    rows: list[tuple[str, str, str]] = []
    for index, (name, owner) in enumerate(MATERIAL_ROWS, start=1):
        status = row_status.get(name) or extra.get(name) or "—"
        rows.append((str(index), name, owner, status))
    return rows


def render_registration_pdf(
    enrollment: Enrollment, snapshot=None
) -> bytes:
    """报名表 PDF（A4 纵向、宋体、默认单页、默认脱敏）。"""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    font = "STSong-Light"
    context = build_context(enrollment, snapshot)
    summary = context["enrollment"]
    team = context["team"]
    players = context["players"]
    coaches = context["coaches"]
    draft = context.get("draft", False)

    event_id = summary.get("eventId") or enrollment.event_id
    event = event_config_client.get_event(event_id)
    group = event_config_client.get_group(event_id, summary.get("groupId") or enrollment.group_id)

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "title", parent=styles["Title"], fontName=font, fontSize=17, leading=21,
        alignment=TA_CENTER, spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "subtitle", parent=styles["Normal"], fontName=font, fontSize=10,
        alignment=TA_CENTER, spaceAfter=6,
    )
    section_style = ParagraphStyle(
        "section", parent=styles["Normal"], fontName=font, fontSize=9.5, leading=13,
    )
    cell_style = ParagraphStyle(
        "cell", parent=styles["Normal"], fontName=font, fontSize=8, leading=10,
    )
    note_style = ParagraphStyle(
        "note", parent=styles["Normal"], fontName=font, fontSize=7, leading=9.5,
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=12 * mm,
        title="报名表",
    )
    story: list = []
    title = "省级中小学足球赛事报名表" + ("（草稿）" if draft else "")
    story.append(Paragraph(title, title_style))
    story.append(
        Paragraph(
            f"{event.get('name', '')} · {group.get('name', '')} · {event.get('year', '')}",
            subtitle_style,
        )
    )

    def section(name: str):
        table = Table([[Paragraph(f"<b>{name}</b>", section_style)]], colWidths=[178 * mm])
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#D9D9D9")),
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.black),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        story.append(table)

    def grid(rows: list[list[str]], col_widths: list[float], header: bool = False):
        table = Table(rows, colWidths=col_widths, repeatRows=1 if header else 0)
        commands = [
            ("FONTNAME", (0, 0), (-1, -1), font),
            ("FONTSIZE", (0, 0), (-1, -1), 7.6),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
        if header:
            commands.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F2F2")))
        labels = [0, 2]
        for row_index in range(len(rows)):
            for column in labels:
                commands.append(
                    ("BACKGROUND", (column, row_index), (column, row_index), colors.HexColor("#F2F2F2"))
                )
        table.setStyle(TableStyle(commands))
        return table

    section("一、参赛单位信息")
    city_name = _org_name(team.get("cityOrgId") or summary.get("cityOrgId") or "")
    info_rows = [
        ["单位名称（学校）", team.get("schoolName") or team.get("name") or "", "所属市州", city_name],
        ["球队名称", team.get("name") or "", "联系人", summary.get("contactName") or ""],
        ["联系电话（脱敏）", summary.get("maskedContactPhone") or "", "主体类型", SUBJECT_LABELS.get(summary.get("subjectType") or "", "")],
        ["报名单号", summary.get("id") or enrollment.pk, "提交批次", str(summary.get("submissionNo") or 0)],
    ]
    story.append(
        grid(info_rows, [30 * mm, 59 * mm, 30 * mm, 59 * mm])
    )

    section("二、教练组成员")
    coach_rows = [["序号", "姓名", "职务", "联系电话（脱敏）"]]
    for index, coach in enumerate(coaches, start=1):
        coach_rows.append(
            [
                str(index),
                coach.get("name") or "",
                coach.get("role") or "",
                coach.get("maskedPhone") or "",
            ]
        )
    story.append(grid(coach_rows, [14 * mm, 55 * mm, 55 * mm, 54 * mm], header=True))

    section("三、运动员名单")
    player_font = 7.4 if len(players) <= 18 else 6.4
    player_rows = [
        ["序号", "姓名", "性别", "出生日期", "身份证号（脱敏）", "学籍号（脱敏）", "球衣号", "位置"]
    ]
    for index, player in enumerate(players, start=1):
        player_rows.append(
            [
                str(index),
                player.get("athleteName") or "",
                player.get("gender") or "",
                str(player.get("birthDate") or ""),
                player.get("maskedIdNumber") or "",
                player.get("maskedStudentNumber") or "",
                "" if player.get("jerseyNumber") is None else str(player.get("jerseyNumber")),
                player.get("position") or "",
            ]
        )
    table = grid(
        player_rows,
        [11 * mm, 24 * mm, 13 * mm, 23 * mm, 40 * mm, 30 * mm, 14 * mm, 23 * mm],
        header=True,
    )
    table.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), player_font)]))
    story.append(table)

    section("四、材料清单")
    material_rows = [["序号", "材料名称", "提交责任方", "状态"]]
    material_rows.extend([list(row) for row in _material_rows(context)])
    story.append(
        grid(material_rows, [14 * mm, 88 * mm, 30 * mm, 46 * mm], header=True)
    )

    signature = Table(
        [[Paragraph("学校（盖章）", cell_style), Paragraph("教练员签字", cell_style), Paragraph("年    月    日", cell_style)]],
        colWidths=[59 * mm, 59 * mm, 60 * mm],
        rowHeights=[16 * mm],
    )
    signature.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(Spacer(1, 2))
    story.append(signature)

    notes = [
        "填表说明：",
        "① 本表由系统按报名提交快照自动生成，内容与提交时一致；历史报名不受后续资料修改影响，名单调整生成新版本。",
        "② 运动员出生日期按所报组别公布的闭区间校验（含起止日），本届原则上只代表一队、参加一组。",
        "③ 身份证号、学籍号、联系电话均为脱敏展示；材料清单由规则预检自动勾选，必需材料缺失可暂存草稿，但不得最终审核通过。",
        "④ 报名截止时间以服务器时间（Asia/Shanghai，精确到秒）为准。",
    ]
    story.append(Spacer(1, 3))
    for line in notes:
        story.append(Paragraph(line, note_style))

    doc.build(story)
    return buffer.getvalue()


def render_players_docx(enrollment: Enrollment, snapshot=None) -> bytes:
    """球员报名信息表 Word（宋体三号标题 + 表格）。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt

    context = build_context(enrollment, snapshot)
    summary = context["enrollment"]
    event_id = summary.get("eventId") or enrollment.event_id
    event = event_config_client.get_event(event_id)
    group = event_config_client.get_group(
        event_id, summary.get("groupId") or enrollment.group_id
    )

    document = Document()
    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(
        f"{event.get('name', '')}{group.get('name', '')}球员报名信息表"
        + ("（草稿）" if context.get("draft") else "")
    )
    run.font.name = "宋体"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    run.font.size = Pt(16)

    headers = [
        "序号", "姓名", "性别", "出生日期", "身份证号（脱敏）",
        "学籍号（脱敏）", "学校编码", "球衣号码", "位置", "资格状态",
    ]
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    header_cells = table.rows[0].cells
    for index, name in enumerate(headers):
        header_cells[index].text = name
        for paragraph in header_cells[index].paragraphs:
            for header_run in paragraph.runs:
                header_run.font.bold = True
                header_run.font.name = "宋体"
                header_run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
                header_run.font.size = Pt(9)

    team = context["team"]
    for index, player in enumerate(context["players"], start=1):
        cells = table.add_row().cells
        values = [
            str(index),
            player.get("athleteName") or "",
            player.get("gender") or "",
            str(player.get("birthDate") or ""),
            player.get("maskedIdNumber") or "",
            player.get("maskedStudentNumber") or "",
            team.get("schoolCode") or "",
            "" if player.get("jerseyNumber") is None else str(player.get("jerseyNumber")),
            player.get("position") or "",
            player.get("qualificationStatus") or "QUALIFIED",
        ]
        for cell, value in zip(cells, values):
            cell.text = value
            for paragraph in cell.paragraphs:
                for cell_run in paragraph.runs:
                    cell_run.font.name = "宋体"
                    cell_run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
                    cell_run.font.size = Pt(9)

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def build_documents_zip(enrollments: Iterable[Enrollment]) -> bytes:
    """批量报名表 ZIP；文件名 {学校编码}_{球队名称}_报名表.pdf。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for enrollment in enrollments:
            snapshot = services.latest_snapshot(enrollment)
            pdf = render_registration_pdf(enrollment, snapshot)
            team = team_client.get_team(enrollment.team_id)
            school_code = str(team.get("schoolCode") or "school")
            team_name = str(team.get("name") or enrollment.team_id)
            safe_name = "".join(
                char for char in f"{school_code}_{team_name}_报名表.pdf"
                if char not in '\\/:*?"<>|'
            )
            archive.writestr(safe_name, pdf)
    return buffer.getvalue()


def export_registrations_xlsx(enrollments: Iterable[Enrollment]) -> bytes:
    """报名汇总 Excel：汇总 sheet + 明细 sheet（全部脱敏）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    rows = list(enrollments)
    workbook = Workbook()
    summary_sheet = workbook.active
    summary_sheet.title = "汇总"
    detail_sheet = workbook.create_sheet("明细")

    header_fill = PatternFill("solid", fgColor="D9D9D9")
    bold = Font(bold=True)

    summary_headers = ["赛事", "组别", "市州/高校", "球队数", "运动员数", "待审", "已通过", "已驳回"]
    summary_sheet.append(summary_headers)
    for cell in summary_sheet[1]:
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    buckets: dict[tuple[str, str, str], dict] = {}
    for enrollment in rows:
        group = event_config_client.get_group(enrollment.event_id, enrollment.group_id)
        key = (
            enrollment.event_id,
            group.get("name") or enrollment.group_id,
            _org_name(enrollment.city_org_id),
        )
        bucket = buckets.setdefault(
            key,
            {"teams": set(), "athletes": 0, "pending": 0, "approved": 0, "rejected": 0},
        )
        bucket["teams"].add(enrollment.team_id)
        bucket["athletes"] += enrollment.players.exclude(
            qualification_status="EXCLUDED"
        ).count()
        if enrollment.status.startswith("PENDING"):
            bucket["pending"] += 1
        elif enrollment.status == "APPROVED":
            bucket["approved"] += 1
        elif enrollment.status == "REJECTED":
            bucket["rejected"] += 1
    for key, bucket in sorted(buckets.items()):
        summary_sheet.append(
            [
                key[0],
                key[1],
                key[2],
                len(bucket["teams"]),
                bucket["athletes"],
                bucket["pending"],
                bucket["approved"],
                bucket["rejected"],
            ]
        )

    detail_headers = [
        "序号", "报名单号", "赛事", "组别", "球队名称", "主体类型", "市州",
        "状态", "提交批次", "联系人", "联系电话（脱敏）", "运动员数", "创建时间",
    ]
    detail_sheet.append(detail_headers)
    for cell in detail_sheet[1]:
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    for index, enrollment in enumerate(rows, start=1):
        team = team_client.get_team(enrollment.team_id)
        group = event_config_client.get_group(enrollment.event_id, enrollment.group_id)
        detail_sheet.append(
            [
                index,
                enrollment.pk,
                enrollment.event_id,
                group.get("name") or enrollment.group_id,
                team.get("name") or enrollment.team_id,
                SUBJECT_LABELS.get(enrollment.subject_type, enrollment.subject_type),
                _org_name(enrollment.city_org_id),
                STATUS_LABELS.get(enrollment.status, enrollment.status),
                int(enrollment.submission_no or 0),
                enrollment.contact_name,
                enrollment.contact_phone_masked,
                enrollment.players.exclude(qualification_status="EXCLUDED").count()
                if hasattr(enrollment, "players")
                else 0,
                timezone.localtime(enrollment.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            ]
        )

    for sheet in (summary_sheet, detail_sheet):
        for column in sheet.columns:
            width = max(len(str(cell.value or "")) for cell in column) + 4
            sheet.column_dimensions[column[0].column_letter].width = min(max(width, 10), 40)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
