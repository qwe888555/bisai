#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给「遴选档案」补真实可检索的运动员主档。

背景：市州分布脚本只建了报名名册（RosterItem），没有对应的 Person /
AthleteProfile / TeamMember，因此遴选档案按球队检索时 43 支球队里只有
4 支能查到人，其余市州的球队都是空结果。

本脚本遍历 event_01 报名名册中 athlete_id 形如 ``ath_city_*`` 的条目，
为每人补齐：
- Person（运动员主档，含加密身份证/手机号、学校、市州）；
- AthleteProfile（1:1 扩展档案：学段、学籍号、位置、监护人）；
- TeamMember（CURRENT 现役关系，球衣号与报名名册一致）；
- PersonMaterial（身份证 / 学籍卡 / 体检报告，覆盖已通过/待审核状态）。

用法（backend/ 目录下）：
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python scripts/seed_dossier_athletes.py

脚本幂等：按身份证哈希匹配，已存在的人员只补缺失的关系/材料。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.cache import cache  # noqa: E402
from django.db import transaction  # noqa: E402

from apps.common import crypto, enums  # noqa: E402
from apps.enrollments.models import RosterItem  # noqa: E402
from apps.materials.models import MaterialType, PersonMaterial  # noqa: E402
from apps.persons.models import AthleteProfile, Person  # noqa: E402
from apps.teams.models import Team, TeamMember  # noqa: E402

EVENT_ID = "event_01"
ROSTER_PREFIX = "ath_city_"

ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
ID_CHECK = "10X98765432"

# 材料类型 → (显示文件名, 审核状态)
MATERIAL_SPECS = [
    ("ID_CARD", "身份证正面.jpg", enums.MaterialStatus.APPROVED),
    ("STUDENT_CARD", "学籍证明.pdf", enums.MaterialStatus.APPROVED),
    ("MEDICAL", "体检报告.pdf", enums.MaterialStatus.PENDING),
]

_birth_seq: dict[tuple[str, date], int] = {}


def _make_id_card(region_code: str, birth: date) -> str:
    key = (region_code, birth)
    # 从 500 起排，避开演示库里 1~25 的既有身份证序号，
    # 否则同市州同生日的运动员会被误判为同一人。
    _birth_seq[key] = _birth_seq.get(key, 0) + 1
    body = f"{region_code}{birth:%Y%m%d}{500 + _birth_seq[key]:03d}"
    total = sum(int(body[i]) * ID_WEIGHTS[i] for i in range(17))
    return body + ID_CHECK[total % 11]


def _make_phone(seq: int) -> str:
    return f"1{3 + seq % 7}{seq:09d}"


def _grade_for(category: str) -> str:
    return {"U12": "PRIMARY_HIGH", "U15": "JUNIOR", "U18": "SENIOR"}.get(
        category, "JUNIOR"
    )


def _birth_for(category: str, index: int) -> date:
    year = {"U12": 2014, "U15": 2011, "U18": 2008}.get(category, 2011)
    # 同队同岁，按名册序号错开月日，避免身份证号重复。
    return date(year, 1 + index % 12, 1 + index % 27)


def _team_region_code(team: Team) -> str:
    if team.region is not None and team.region.code:
        return team.region.code
    if team.school is not None and team.school.region_id:
        return team.school.region.code
    return "510100"


def _team_gender(team: Team) -> int:
    return enums.Gender.FEMALE if "女子" in team.name else enums.Gender.MALE


def _ensure_person(roster: RosterItem, team: Team, seq: int) -> tuple[Person, bool]:
    category = team.category_code or "U15"
    birth = _birth_for(category, seq)
    id_card = _make_id_card(_team_region_code(team), birth)
    id_hash = crypto.lookup_hash(id_card)
    person = Person.objects.filter(id_card_hash=id_hash).first()
    created = False
    if person is None:
        phone = _make_phone(seq)
        guardian_phone = _make_phone(seq + 7000)
        person = Person.objects.create(
            person_no=f"TMP{id_hash[:16]}",
            full_name=roster.athlete_name or f"运动员{seq}",
            gender=_team_gender(team),
            birth_date=birth,
            id_card_cipher=crypto.encrypt_text(id_card),
            id_card_hash=id_hash,
            id_card_masked=crypto.mask_id_number(id_card),
            phone_cipher=crypto.encrypt_text(phone),
            phone_hash=crypto.lookup_hash(phone),
            phone_masked=crypto.mask_phone(phone),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.APPROVED,
            status=enums.EnableStatus.ACTIVE,
            region=team.region,
            school=team.school,
        )
        person.person_no = f"P{person.created_at:%Y}{person.pk:08d}"
        person.save(update_fields=["person_no"])
        created = True

    AthleteProfile.objects.update_or_create(
        person=person,
        defaults={
            "grade_level": _grade_for(category),
            "student_no": f"S2026{person.pk:06d}",
            "position_code": roster.position or "MF",
            "guardian_name": f"{person.full_name[:1]}家长",
            "guardian_relation": "父母",
        },
    )
    return person, created


def _ensure_membership(roster: RosterItem, team: Team, person: Person) -> None:
    TeamMember.objects.update_or_create(
        team=team,
        person=person,
        defaults={
            "jersey_number": roster.jersey_number,
            "position_code": roster.position or "MF",
            "status": enums.RelationStatus.CURRENT,
            "joined_at": date(2026, 9, 5),
            "source": enums.RelationSource.DIRECT,
        },
    )


def _ensure_materials(person: Person) -> None:
    types = {row.code: row for row in MaterialType.objects.all()}
    for code, file_name, status in MATERIAL_SPECS:
        material_type = types.get(code)
        if material_type is None:
            continue
        PersonMaterial.objects.update_or_create(
            idempotency_key=f"dossier_{person.pk}_{code}",
            defaults={
                "person": person,
                "type": material_type,
                "file_id": f"file_dossier_{person.pk}_{code.lower()}",
                "file_name": file_name,
                "file_size": 240_000 + person.pk * 137,
                "file_ext": file_name.rsplit(".", 1)[-1],
                "status": status,
                "remark": "2026 年度报名资格审查",
                "is_deleted": False,
            },
        )


def main() -> None:
    print("== 遴选档案运动员数据 ==")
    rosters = (
        RosterItem.objects.filter(
            enrollment__event_id=EVENT_ID,
            athlete_id__startswith=ROSTER_PREFIX,
        )
        .select_related("enrollment")
        .order_by("enrollment_id", "jersey_number", "id")
    )
    total = rosters.count()
    print(f"  待处理名册条目 {total} 条")

    created_persons = created_members = created_materials = 0
    skipped = 0
    with transaction.atomic():
        for seq, roster in enumerate(rosters, start=1):
            team = Team.objects.filter(pk=roster.enrollment.team_id, is_deleted=False).first()
            if team is None:
                skipped += 1
                continue
            person, created = _ensure_person(roster, team, seq)
            created_persons += 1 if created else 0
            before = TeamMember.objects.filter(team=team, person=person).exists()
            _ensure_membership(roster, team, person)
            created_members += 0 if before else 1
            before = PersonMaterial.objects.filter(
                person=person, is_deleted=False
            ).count()
            _ensure_materials(person)
            created_materials += max(
                0,
                PersonMaterial.objects.filter(person=person, is_deleted=False).count()
                - before,
            )
        cache.clear()

    print(f"  + 新建人员 {created_persons} / 球队关系 {created_members} / 材料 {created_materials}")
    if skipped:
        print(f"  ! 跳过 {skipped} 条（球队不存在）")
    print(
        f"  当前运动员主档 {Person.objects.filter(person_category='ATHLETE', is_deleted=False).count()} 人，"
        f"现役球队关系 {TeamMember.objects.filter(status='CURRENT').count()} 条"
    )
    print("完成。可在「遴选档案」按姓名或球队检索。")


if __name__ == "__main__":
    main()
