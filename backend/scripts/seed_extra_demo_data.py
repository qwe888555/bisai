#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补充本地演示数据：把前端所有“空态”页面填满，便于联调验收。

覆盖范围（全部为本地测试数据，不属于任何业务 app）：
- 把 acc_athlete / acc_coach 绑定到真实人员档案；
- 站内消息（报名、审核、材料、赛程、公示、系统）；
- 数据字典类型 + 字典项（组别、位置、教练等级、材料类型等）；
- 材料类型 + 人员材料 / 球队材料 + 审核历史 + 文件版本；
- 门户新闻 / 政策 / 精彩瞬间 / 最佳阵容 + 轮播图；
- 图文直播帖子、直播外链、比赛归档文件；
- 把正在直播的比赛发布到门户。

用法（backend/ 目录下）：
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python scripts/seed_extra_demo_data.py

脚本幂等：已存在的数据会跳过，可重复执行。
"""

from __future__ import annotations

import io
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.cache import cache  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounts.models import Account, Organization  # noqa: E402
from apps.attachments.models import FileRecord  # noqa: E402
from apps.attachments.services import upload  # noqa: E402
from apps.common import enums  # noqa: E402
from apps.common.ids import new_id  # noqa: E402
from apps.events import services as event_services  # noqa: E402
from apps.events.models import Content, Match, PortalResource  # noqa: E402
from apps.materials.models import (  # noqa: E402
    MaterialAuditLog,
    MaterialFileVersion,
    MaterialType,
    PersonMaterial,
    TeamMaterial,
)
from apps.notices.models import Message, push_message  # noqa: E402
from apps.persons.models import DictItem, DictType, Person  # noqa: E402
from apps.teams.models import Team  # noqa: E402

NOW = timezone.now()
ADMIN = Account.objects.get(pk="acc_admin")


# ---------------------------------------------------------------- 文件素材

def _pdf_bytes(title: str) -> bytes:
    """生成一个可被浏览器识别的最小 PDF。"""
    text = re.sub(r"[^\x20-\x7e]", "", title) or "Demo material"
    stream = f"BT /F1 20 Tf 72 760 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(data)
    data += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    data += b"0000000000 65535 f \n"
    for offset in offsets:
        data += f"{offset:010d} 00000 n \n".encode("ascii")
    data += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(data)


def _gradient_png(width: int, height: int, title: str, subtitle: str, colors) -> bytes:
    """生成一张带文字的渐变占位图（用于轮播/封面）。"""
    from PIL import Image, ImageDraw, ImageFont

    start, end = colors
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(int(start[i] + (end[i] - start[i]) * ratio) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)

    # 简单的球场线条装饰
    line = (255, 255, 255, 38)
    for x in range(-height, width + height, 110):
        draw.line([(x, 0), (x + height, height)], fill=(255, 255, 255), width=2)
    draw.ellipse(
        [width * 0.62, -height * 0.35, width * 1.05, height * 0.72],
        outline=(255, 255, 255),
        width=4,
    )

    font_path = "C:/Windows/Fonts/msyh.ttc"
    try:
        title_font = ImageFont.truetype(font_path, int(height * 0.13))
        sub_font = ImageFont.truetype(font_path, int(height * 0.055))
    except Exception:  # noqa: BLE001 - 无中文字体时退回内置字体
        title_font = ImageFont.load_default()
        sub_font = ImageFont.load_default()

    draw.text((width * 0.07, height * 0.38), title, font=title_font, fill=(255, 255, 255))
    draw.text((width * 0.07, height * 0.62), subtitle, font=sub_font, fill=(210, 255, 120))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _upload_pdf(owner_type: str, owner_id: str, name: str, title: str) -> FileRecord:
    uploaded = SimpleUploadedFile(name, _pdf_bytes(title), content_type="application/pdf")
    return upload(ADMIN, owner_type, owner_id, "MATERIAL" if owner_type in {"PERSON", "TEAM"} else "MATCH_DOCUMENT", uploaded)


def _upload_image(owner_type: str, owner_id: str, name: str, data: bytes) -> FileRecord:
    uploaded = SimpleUploadedFile(name, data, content_type="image/png")
    return upload(ADMIN, owner_type, owner_id, "CMS_IMAGE", uploaded)


# ---------------------------------------------------------------- 账号绑定

def seed_account_bindings() -> None:
    person_updates = {"acc_athlete": "1", "acc_coach": "25"}
    for account_id, person_id in person_updates.items():
        account = Account.objects.filter(pk=account_id).first()
        if account is None:
            print(f"  ! 账号不存在，跳过：{account_id}")
            continue
        if account.person_id != person_id:
            account.person_id = person_id
            account.save(update_fields=["person_id", "updated_at"])
            print(f"  + 绑定账号 {account.username} → 人员 {person_id}")
        else:
            print(f"  = 账号 {account.username} 已绑定人员 {person_id}")

    # 球队端数据范围按“账号组织节点 code == Team.team_no”解析；
    # 原有 acc_team 指向 legacy 组织 TEAM001，与任何 Team 行都不匹配，
    # 导致球队端页面全空。这里改指向示例第一中学男子足球队的组织节点。
    team_org = (
        Organization.objects.filter(org_type="TEAM", code="T202600000001").first()
    )
    team_account = Account.objects.filter(pk="acc_team").first()
    if team_org and team_account and team_account.organization_id != team_org.pk:
        team_account.organization_id = team_org.pk
        team_account.save(update_fields=["organization", "updated_at"])
        print(f"  + 球队账号 team → 组织 {team_org.pk}（{team_org.name}）")


# ---------------------------------------------------------------- 消息

MESSAGE_SPECS = [
    ("acc_admin", "新的报名单待审核", "示例第一中学男子足球队提交了 U15 组别报名单，请及时审核。", "ENROLLMENT", "enr_demo_boys_u15", False),
    ("acc_admin", "城市审核已通过", "示例第一中学女子足球队 U12 组别报名已通过城市审核，等待省级复核。", "REVIEW", "enr_demo_girls_u12", False),
    ("acc_admin", "材料待审核提醒", "运动员王伟磊提交了体检证明，请在人员管理中审核。", "MATERIAL", "1", False),
    ("acc_admin", "今日比赛直播中", "示例第二中学女子足球队 vs 示例第一中学女子足球队 正在直播。", "MATCH", "mat_demo_gu12_r2", True),
    ("acc_admin", "遴选公示已发布", "U15 组别入围名单已发布公示，请关注反馈。", "PUBLICATION", "1", True),
    ("acc_province", "省级复核待处理", "示例第一中学男子足球队 U18 组别报名单已提交省级复核。", "REVIEW", "enr_demo_boys_u18", False),
    ("acc_province", "公示发布成功", "教练员唐雨泽的公示信息已发布。", "PUBLICATION", "26", True),
    ("acc_city", "城市审核待处理", "示例第一中学男子足球队 U15 组别报名单等待城市审核。", "ENROLLMENT", "enr_31e34bc529384014a036", False),
    ("acc_city", "赛程变更提醒", "本周 U15 组别比赛场地调整为示例市体育中心 2 号场。", "MATCH", "mat_demo_u15_r3", False),
    ("acc_team", "报名材料补充通知", "请补充球队保险材料后重新提交报名。", "MATERIAL", "1", False),
    ("acc_team", "报名审核结果", "示例第一中学男子足球队 U15 组别报名已通过审核。", "ENROLLMENT", "enr_demo_boys_u15", True),
    ("acc_university", "本校球队赛程提醒", "示例第一中学男子足球队下一场比赛将于本周六开赛。", "MATCH", "mat_demo_u15_r3", False),
    ("acc_athlete", "你的公示信息已发布", "王伟磊的运动员公示信息已发布，可在“我的公示”查看。", "PUBLICATION", "1", False),
    ("acc_athlete", "材料审核通过", "身份证复印件材料已通过审核。", "MATERIAL", "1", True),
    ("acc_athlete", "比赛直播提醒", "你关注的 U12 组别比赛正在直播。", "MATCH", "mat_demo_gu12_r2", False),
    ("acc_coach", "教练公示已发布", "唐雨泽的教练员公示信息已发布。", "PUBLICATION", "26", False),
    ("acc_coach", "球队材料待补充", "请上传教练员资格证书扫描件。", "MATERIAL", "1", False),
    ("acc_school01", "报名统计已更新", "本校 U15 组别报名人数更新为 12 人。", "ENROLLMENT", "enr_demo_boys_u15", True),
    ("acc_recorder", "比赛记录台待确认", "U12 组别直播比赛还有 1 条事件待确认。", "MATCH", "mat_demo_gu12_r2", False),
]


def seed_messages() -> None:
    for index, (account_id, title, body, business_type, business_id, read) in enumerate(
        MESSAGE_SPECS
    ):
        if not Account.objects.filter(pk=account_id).exists():
            continue
        if Message.objects.filter(recipient_account_id=account_id, title=title).exists():
            continue
        message = push_message(account_id, title, body, business_type, business_id)
        created_at = NOW - timedelta(hours=index * 6 + 2)
        updates = {"created_at": created_at}
        if read:
            updates["read"] = True
            updates["read_at"] = created_at + timedelta(hours=1)
        Message.objects.filter(pk=message.pk).update(**updates)
    print(f"  = 消息总数：{Message.objects.count()}")


# ---------------------------------------------------------------- 数据字典

DICT_SPECS = {
    "team_category": (
        "球队组别",
        [
            ("PRIMARY", "小学组"),
            ("U12", "U12 组"),
            ("U13", "U13 组"),
            ("U15", "U15 组"),
            ("U18", "U18 组"),
        ],
    ),
    "position": (
        "场上位置",
        [
            ("GK", "门将"),
            ("DF", "后卫"),
            ("MF", "中场"),
            ("FW", "前锋"),
        ],
    ),
    "coach_category": (
        "教练类别",
        [
            ("HEAD_COACH", "主教练"),
            ("ASSISTANT_COACH", "助理教练"),
            ("GOALKEEPER_COACH", "守门员教练"),
        ],
    ),
    "coach_level": (
        "教练等级",
        [
            ("A", "A 级"),
            ("B", "B 级"),
            ("C", "C 级"),
        ],
    ),
    "guardian_relation": (
        "监护人关系",
        [
            ("FATHER", "父亲"),
            ("MOTHER", "母亲"),
            ("OTHER", "其他"),
        ],
    ),
    "school_type": (
        "学校类别",
        [
            ("PRIMARY", "小学"),
            ("MIDDLE", "初中"),
            ("HIGH", "高中"),
            ("VOCATIONAL", "中职"),
        ],
    ),
    "material_type": (
        "材料类型",
        [
            ("ID_CARD", "身份证复印件"),
            ("STUDENT_CARD", "学籍证明"),
            ("MEDICAL", "体检证明"),
            ("COACH_CERT", "教练员证书"),
            ("TEAM_LICENSE", "球队注册证明"),
            ("INSURANCE", "意外伤害保险"),
        ],
    ),
    "event_category": (
        "赛事类别",
        [
            ("LEAGUE", "联赛"),
            ("CHAMPIONSHIP", "锦标赛"),
            ("INVITATIONAL", "邀请赛"),
        ],
    ),
    "education": (
        "学历",
        [
            ("PRIMARY", "小学"),
            ("JUNIOR", "初中"),
            ("SENIOR", "高中"),
            ("BACHELOR", "本科"),
        ],
    ),
    "gender": (
        "性别",
        [
            ("MALE", "男"),
            ("FEMALE", "女"),
        ],
    ),
}


def seed_dictionaries() -> None:
    type_count = 0
    item_count = 0
    for code, (name, items) in DICT_SPECS.items():
        dtype, created = DictType.objects.get_or_create(
            code=code, defaults={"name": name, "remark": "演示字典"}
        )
        if created:
            type_count += 1
        for sort_order, (value, label) in enumerate(items, start=1):
            _, item_created = DictItem.objects.get_or_create(
                type=dtype,
                value=value,
                defaults={"label": label, "sort_order": sort_order, "is_active": True},
            )
            if item_created:
                item_count += 1
    print(f"  + 新增字典类型 {type_count}，字典项 {item_count}（合计 {DictItem.objects.count()} 项）")


# ---------------------------------------------------------------- 材料

MATERIAL_TYPE_SPECS = [
    ("ID_CARD", "身份证复印件", "PERSON", True, False, False, 1),
    ("STUDENT_CARD", "学籍证明", "PERSON", True, True, False, 2),
    ("MEDICAL", "体检证明", "PERSON", True, True, False, 3),
    ("COACH_CERT", "教练员证书", "PERSON", True, True, False, 4),
    ("TEAM_LICENSE", "球队注册证明", "TEAM", True, True, False, 1),
    ("INSURANCE", "意外伤害保险", "TEAM", True, True, False, 2),
]


def seed_material_types() -> None:
    for code, name, applies_to, required, need_audit, allow_multiple, sort_order in MATERIAL_TYPE_SPECS:
        MaterialType.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "applies_to": applies_to,
                "is_required": required,
                "need_audit": need_audit,
                "allow_multiple": allow_multiple,
                "allowed_ext": "pdf,jpg,jpeg,png",
                "max_size_mb": 10,
                "sort_order": sort_order,
                "is_active": True,
                "remark": "演示材料类型",
            },
        )
    print(f"  = 材料类型：{MaterialType.objects.count()} 个")


PERSON_MATERIALS = [
    # (person_id, type_code, title, status, audit_remark)
    (1, "ID_CARD", "王伟磊身份证复印件", "APPROVED", "信息清晰，审核通过"),
    (1, "MEDICAL", "王伟磊体检证明", "PENDING", ""),
    (1, "STUDENT_CARD", "王伟磊学籍证明", "REJECTED", "证明已过期，请重新上传"),
    (2, "ID_CARD", "李子轩身份证复印件", "PENDING", ""),
    (25, "COACH_CERT", "唐雨泽教练员证书", "APPROVED", "证书在有效期内"),
    (26, "COACH_CERT", "许俊杰教练员证书", "PENDING", ""),
]

TEAM_MATERIALS = [
    (1, "TEAM_LICENSE", "示例第一中学男子足球队注册证明", "APPROVED", "材料完整"),
    (1, "INSURANCE", "示例第一中学男子足球队意外伤害保险", "PENDING", ""),
    (3, "TEAM_LICENSE", "示例第一中学女子足球队注册证明", "PENDING", ""),
]


def _status_enum(value: str):
    return {
        "APPROVED": enums.MaterialStatus.APPROVED,
        "PENDING": enums.MaterialStatus.PENDING,
        "REJECTED": enums.MaterialStatus.REJECTED,
        "DRAFT": enums.MaterialStatus.DRAFT,
    }[value]


def seed_person_materials() -> None:
    created_count = 0
    for person_id, type_code, title, status, remark in PERSON_MATERIALS:
        person = Person.objects.filter(pk=person_id).first()
        mtype = MaterialType.objects.filter(code=type_code).first()
        if person is None or mtype is None:
            continue
        if PersonMaterial.objects.filter(person=person, type=mtype, is_deleted=False).exists():
            continue
        record = _upload_pdf("PERSON", str(person.pk), f"{title}.pdf", title)
        age_days = len(PERSON_MATERIALS) - created_count
        material = PersonMaterial.objects.create(
            person=person,
            type=mtype,
            file_id=record.pk,
            file_name=record.original_name,
            file_size=record.size_bytes,
            file_ext="pdf",
            status=_status_enum(status),
            audit_remark=remark,
            audited_at=NOW - timedelta(days=age_days) if status != "PENDING" else None,
            idempotency_key=f"seed-pm-{person.pk}-{type_code}"[:64],
            remark=title,
        )
        if status != "PENDING":
            MaterialAuditLog.objects.create(
                material_kind="PERSON",
                material_id=material.pk,
                from_status=enums.MaterialStatus.PENDING,
                to_status=material.status,
                comment=remark,
                created_at=NOW - timedelta(days=age_days),
            )
        MaterialFileVersion.objects.get_or_create(
            material_kind="PERSON",
            material_id=material.pk,
            version=1,
            defaults={"file_id": record.pk, "file_name": record.original_name},
        )
        created_count += 1
    print(f"  + 新增人员材料 {created_count}（合计 {PersonMaterial.objects.count()} 条）")


def seed_team_materials() -> None:
    created_count = 0
    for team_id, type_code, title, status, remark in TEAM_MATERIALS:
        team = Team.objects.filter(pk=team_id).first()
        mtype = MaterialType.objects.filter(code=type_code).first()
        if team is None or mtype is None:
            continue
        if TeamMaterial.objects.filter(team=team, type=mtype, is_deleted=False).exists():
            continue
        record = _upload_pdf("TEAM", str(team.pk), f"{title}.pdf", title)
        age_days = len(TEAM_MATERIALS) - created_count
        # 人员材料与球队材料是两张独立自增表，默认都会从 1 开始；
        # review/remove 仅凭 id 双表解析时同 id 会 409。演示数据把球队材料
        # 主键放到高位，避免与人员材料碰撞（仅测试数据口径）。
        material = TeamMaterial.objects.create(
            id=10000 + created_count,
            team=team,
            type=mtype,
            file_id=record.pk,
            file_name=record.original_name,
            file_size=record.size_bytes,
            file_ext="pdf",
            status=_status_enum(status),
            audit_remark=remark,
            audited_at=NOW - timedelta(days=age_days) if status != "PENDING" else None,
            idempotency_key=f"seed-tm-{team.pk}-{type_code}"[:64],
            remark=title,
        )
        if status != "PENDING":
            MaterialAuditLog.objects.create(
                material_kind="TEAM",
                material_id=material.pk,
                from_status=enums.MaterialStatus.PENDING,
                to_status=material.status,
                comment=remark,
                created_at=NOW - timedelta(days=age_days),
            )
        MaterialFileVersion.objects.get_or_create(
            material_kind="TEAM",
            material_id=material.pk,
            version=1,
            defaults={"file_id": record.pk, "file_name": record.original_name},
        )
        created_count += 1
    print(f"  + 新增球队材料 {created_count}（合计 {TeamMaterial.objects.count()} 条）")


# ---------------------------------------------------------------- 门户内容

CONTENT_SPECS = [
    # contentType, title, summary, body, days_ago, pinned, policy_no, from, to
    (
        "NEWS",
        "四川省青少年校园足球联赛小组赛精彩收官",
        "全省 16 个市州的球队在小组赛中展开激烈角逐，多场比赛在最后时刻分出胜负。",
        "10 月 26 日，2026 年四川省青少年校园足球联赛小组赛阶段全部结束。"
        "本轮比赛共产生 27 粒进球，其中 U15 组别的一场焦点战以 3:2 逆转收场。"
        "赛事组委会表示，淘汰赛将于 11 月 2 日开打。",
        1,
        False,
        "",
        None,
        None,
    ),
    (
        "NEWS",
        "教练员专项培训在成都举行",
        "来自全省的 80 余名基层足球教练参加为期三天的专项培训。",
        "本次培训围绕青少年体能训练、伤病预防与比赛数据分析展开，"
        "邀请省内高校足球专家进行现场示范。参训教练完成全部课程后可获得继续教育学分。",
        3,
        False,
        "",
        None,
        None,
    ),
    (
        "NEWS",
        "校园足球特色学校名单公布",
        "省教育厅公布新一批校园足球特色学校名单，共有 42 所学校入选。",
        "入选学校将在师资培训、场地建设和竞赛组织等方面获得专项支持。"
        "名单已在省教育厅门户网站同步公开，公示期为五个工作日。",
        5,
        False,
        "",
        None,
        None,
    ),
    (
        "NEWS",
        "冬日训练指南：科学热身与防护",
        "气温下降，青少年足球训练需要注意热身、补水和装备选择。",
        "冬季训练前应进行至少 15 分钟的动态热身，训练中注意分层增减衣物。"
        "如遇低温雨雪天气，应适当调整训练强度并做好场地安全检查。",
        7,
        False,
        "",
        None,
        None,
    ),
    (
        "POLICY",
        "四川省青少年校园足球赛事安全管理办法",
        "规范赛事组织、场地安全、医疗救护和应急预案等环节。",
        "为保障青少年校园足球赛事安全有序开展，特制定本办法。"
        "各承办单位应落实场地检查、医疗点设置和突发事件报告制度。",
        2,
        False,
        "川教体〔2026〕12号",
        date(2026, 9, 1),
        date(2027, 12, 31),
    ),
    (
        "POLICY",
        "关于做好2026年校园足球报名工作的通知",
        "明确报名时间、材料要求和审核流程。",
        "各市州教育行政部门应于 10 月 31 日前完成报名材料初审，"
        "省级复核工作在 11 月 5 日前完成。报名材料包括报名表、学籍证明和体检证明。",
        4,
        True,
        "川教体〔2026〕15号",
        date(2026, 10, 1),
        date(2026, 12, 31),
    ),
    (
        "POLICY",
        "校园足球经费使用与管理细则",
        "规范专项经费的使用范围、审批流程和绩效评价。",
        "专项经费主要用于赛事组织、教练培训、场地维护和保险购买。"
        "各用款单位应建立台账，接受教育、财政和审计部门的监督检查。",
        8,
        False,
        "川教体〔2026〕18号",
        date(2026, 8, 1),
        date(2027, 8, 1),
    ),
    (
        "HIGHLIGHT",
        "精彩瞬间：倒钩破门点燃全场",
        "U15 组别焦点战诞生本轮最佳进球。",
        "比赛第 78 分钟，示例第一中学男子足球队 9 号球员在禁区内倒钩破门，"
        "帮助球队完成逆转。现场观众报以热烈掌声。",
        1,
        False,
        "",
        None,
        None,
    ),
    (
        "HIGHLIGHT",
        "精彩瞬间：门将神扑集锦",
        "本轮比赛门将高接低挡，多次化解必进球。",
        "U12 组别比赛中，两位门将贡献了多次精彩扑救，"
        "其中一次近距离封堵被评为本轮最佳防守瞬间。",
        3,
        False,
        "",
        None,
        None,
    ),
    (
        "BEST_LINEUP",
        "U15组别第三轮最佳阵容",
        "综合进球、助攻和防守数据评选产生。",
        "门将：李明；后卫：张浩然、刘梓涵、陈雨泽；"
        "中场：杨俊杰、黄思远；前锋：王伟磊、赵晨曦。",
        2,
        False,
        "",
        None,
        None,
    ),
    (
        "BEST_LINEUP",
        "U12组别第三轮最佳阵容",
        "小球员们在第三轮比赛中展现了出色的技术与团队配合。",
        "门将：周子轩；后卫：吴宇航、郑博文；"
        "中场：孙诗涵、徐欣怡；前锋：朱若曦。",
        2,
        False,
        "",
        None,
        None,
    ),
]


def _plain_text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def seed_contents() -> None:
    created_count = 0
    for index, (
        content_type,
        title,
        summary,
        body,
        days_ago,
        pinned,
        policy_no,
        effective_from,
        effective_to,
    ) in enumerate(CONTENT_SPECS):
        if Content.objects.filter(content_type=content_type, title=title).exists():
            continue
        content = Content.objects.create(
            id=new_id("cnt"),
            content_type=content_type,
            title=title,
            summary=summary,
            body_html=f"<p>{body}</p>",
            body_text=body,
            event_id="event_01",
            policy_document_no=policy_no,
            policy_effective_from=effective_from,
            policy_effective_to=effective_to,
            pinned=pinned,
            status="PUBLISHED",
            published_at=NOW - timedelta(days=days_ago),
            revision=1,
        )
        if content_type in {"NEWS", "HIGHLIGHT", "BEST_LINEUP"}:
            palette = [
                ((12, 74, 56), (32, 132, 96)),
                ((15, 52, 96), (36, 108, 168)),
                ((92, 48, 18), (196, 108, 42)),
                ((44, 30, 88), (108, 74, 168)),
            ][index % 4]
            image = _gradient_png(1280, 720, title[:18], "四川省青少年校园足球联赛", palette)
            record = _upload_image("CONTENT", content.pk, "content-cover.png", image)
            content.cover_file_id = record.pk
            content.save(update_fields=["cover_file_id", "updated_at"])
        created_count += 1
    print(f"  + 新增门户内容 {created_count}（合计 {Content.objects.count()} 条）")


CAROUSEL_SPECS = [
    ("四川省青少年校园足球联赛", "2026 赛季 · 全省校园足球在这里开球", 1),
    ("小组赛精彩收官", "淘汰赛 11 月 2 日开打", 2),
    ("校园足球特色学校", "42 所学校入选新一批名单", 3),
]


def seed_resources() -> None:
    created_count = 0
    news_ids = list(
        Content.objects.filter(content_type="NEWS", status="PUBLISHED")
        .order_by("-published_at")
        .values_list("pk", flat=True)
    )
    for sort_order, (title, subtitle, target_index) in enumerate(CAROUSEL_SPECS, start=1):
        if PortalResource.objects.filter(resource_type="CAROUSEL", title=title).exists():
            continue
        image = _gradient_png(
            1600,
            600,
            title,
            subtitle,
            [((8, 58, 44), (28, 122, 84)), ((10, 42, 88), (42, 116, 176)), ((76, 40, 14), (184, 104, 36))][
                (sort_order - 1) % 3
            ],
        )
        record = _upload_image("RESOURCE", "", "carousel-banner.png", image)
        resource = PortalResource.objects.create(
            id=new_id("pres"),
            resource_type="CAROUSEL",
            title=title,
            image_file_id=record.pk,
            target_content_id=news_ids[target_index - 1] if len(news_ids) >= target_index else "",
            sort_order=sort_order,
            enabled=True,
        )
        FileRecord.objects.filter(pk=record.pk).update(owner_id=resource.pk)
        created_count += 1

    # 新闻链接图（首页/资讯列表可复用）
    if not PortalResource.objects.filter(resource_type="NEWS_LINK_IMAGE", title="赛事资讯").exists():
        image = _gradient_png(
            800, 450, "赛事资讯", "关注赛场内外", ((18, 64, 52), (44, 140, 100))
        )
        record = _upload_image("RESOURCE", "", "news-link.png", image)
        resource = PortalResource.objects.create(
            id=new_id("pres"),
            resource_type="NEWS_LINK_IMAGE",
            title="赛事资讯",
            image_file_id=record.pk,
            target_content_id=news_ids[0] if news_ids else "",
            sort_order=10,
            enabled=True,
        )
        FileRecord.objects.filter(pk=record.pk).update(owner_id=resource.pk)
        created_count += 1

    print(f"  + 新增门户资源 {created_count}（合计 {PortalResource.objects.count()} 条）")


# ---------------------------------------------------------------- 比赛直播/归档

LIVE_POSTS = [
    (0, "比赛即将开始，双方球员正在热身。"),
    (12, "主队率先制造威胁，7 号球员禁区外远射稍稍偏出。"),
    (23, "客队门将连续两次精彩扑救，力保球门不失。"),
    (37, "主队 9 号球员头球破门，场上比分 1:0！"),
    (58, "客队加强进攻，连续获得角球机会。"),
    (76, "主队反击再下一城，比分扩大为 2:0。"),
    (90, "全场比赛结束，主队 2:0 获胜。"),
]

LIVE_LINKS = [
    ("官方视频直播", "https://live.example.com/sc-youth-u12"),
    ("赛事图文直播", "https://live.example.com/sc-youth-u12-text"),
]

MATCH_DOCUMENTS = [
    ("mat_demo_gu12_r2", "SIGN_IN", "U12女子组签到表.pdf"),
    ("mat_demo_u15_r1", "REFEREE_REPORT", "U15男子组裁判员报告书.pdf"),
    ("mat_demo_u15_r1", "SIGNED_RECORD", "U15男子组签字确认表.pdf"),
    ("mat_demo_gu12_r1", "SIGN_IN", "U12女子组首轮签到表.pdf"),
]


def seed_match_live() -> None:
    live_match = Match.objects.filter(pk="mat_demo_gu12_r2").first()
    if live_match is not None and not live_match.published:
        live_match.published = True
        live_match.save(update_fields=["published", "updated_at"])
        print("  + 已把直播比赛发布到门户：mat_demo_gu12_r2")

    created_posts = 0
    for index, (minute, text) in enumerate(LIVE_POSTS):
        if event_services.list_live_posts("mat_demo_gu12_r2").filter(text=text).exists():
            continue
        image_ids = []
        if index == 3:
            image = _gradient_png(
                960, 540, "进球瞬间", "U12 女子组 · 第 37 分钟", ((52, 18, 72), (156, 72, 168))
            )
            record = _upload_image("MATCH", "mat_demo_gu12_r2", "live-goal.png", image)
            image_ids = [record.pk]
        event_services.create_live_post(
            "mat_demo_gu12_r2",
            {"text": text, "minute": minute, "imageFileIds": image_ids},
        )
        created_posts += 1
    print(f"  + 新增直播帖子 {created_posts}（合计 {event_services.list_live_posts('mat_demo_gu12_r2').count()} 条）")

    created_links = 0
    for title, url in LIVE_LINKS:
        if event_services.list_live_links("mat_demo_gu12_r2").filter(title=title).exists():
            continue
        event_services.create_live_link(
            "mat_demo_gu12_r2", {"title": title, "url": url, "enabled": True}
        )
        created_links += 1
    print(f"  + 新增直播外链 {created_links}（合计 {event_services.list_live_links('mat_demo_gu12_r2').count()} 条）")

    created_docs = 0
    for match_id, document_type, file_name in MATCH_DOCUMENTS:
        if event_services.list_match_documents(match_id, document_type).exists():
            continue
        record = _upload_pdf("MATCH", match_id, file_name, file_name)
        event_services.create_match_document(
            match_id, {"documentType": document_type, "fileId": record.pk}
        )
        created_docs += 1
    print(f"  + 新增比赛归档文件 {created_docs}")


# ---------------------------------------------------------------- main

def main() -> None:
    print("== 补充演示数据 ==")
    with transaction.atomic():
        print("[1/7] 账号绑定人员档案")
        seed_account_bindings()
        print("[2/7] 站内消息")
        seed_messages()
        print("[3/7] 数据字典")
        seed_dictionaries()
        print("[4/7] 材料类型与材料")
        seed_material_types()
        seed_person_materials()
        seed_team_materials()
        print("[5/7] 门户内容与轮播")
        seed_contents()
        seed_resources()
        print("[6/7] 图文直播与比赛归档")
        seed_match_live()
        print("[7/7] 清理公开接口缓存")
        cache.clear()
    print("完成。前端可直接刷新查看。")
    print(f"  内容 {Content.objects.count()} 条 / 资源 {PortalResource.objects.count()} 条 / "
          f"消息 {Message.objects.count()} 条 / 字典项 {DictItem.objects.count()} 项")
    print(f"  人员材料 {PersonMaterial.objects.count()} 条 / 球队材料 {TeamMaterial.objects.count()} 条 / "
          f"直播帖子 {event_services.list_live_posts('mat_demo_gu12_r2').count()} 条")


if __name__ == "__main__":
    main()
