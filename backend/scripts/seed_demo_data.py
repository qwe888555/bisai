#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地演示数据种子脚本（联调用具，不属于任何业务 app）。

对应团队清单「四、建议的下一步顺序」第 3 条：
    Team / Event / EventGroup / Match / MatchEvent / IdentityCheck，
    外加真实 Person（运动员 + 教练，含扩展档案）与 4 条带 Roster/快照/审核记录/
    材料关联的报名单，使报名列表、比赛页、身份核验与遴选公示（sel_publicity）
    均有数据可看。

口径：
- 只建中小学球队（无高校）；
- 新增而非改写：团队既有的 7 条 mock 报名（team_id=org_team_*）原样保留；
- 报名/比赛表对 Team/Person 为 CharField 软引用，这里统一写整型主键字符串
  （str(team.pk) / str(person.pk)），与 _local_team_display/公示适配器一致；
- 演示行统一打 created_by=999000001 哨兵（逻辑引用，不在 UI 展示），--clean 据此清理。

用法（backend/ 目录下）：
    python scripts/seed_demo_data.py                  # 幂等增补，可重复执行
    python scripts/seed_demo_data.py --clean          # 先删本脚本数据再重建
    python scripts/seed_demo_data.py --no-publicity   # 跳过遴选公示发布
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.serializers.json import DjangoJSONEncoder  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounts.services import ensure_business_org  # noqa: E402
from apps.attachments.models import FileRecord  # noqa: E402
from apps.common import crypto, enums  # noqa: E402
from apps.enrollments.models import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_PENDING_CITY,
    STATUS_PENDING_PROVINCE,
    Enrollment,
    EnrollmentAttachment,
    EnrollmentSnapshot,
    RosterItem,
    ReviewRecord,
)
from apps.events.models import Event, EventGroup, Match, MatchEvent  # noqa: E402
from apps.persons.models import (  # noqa: E402
    AthleteProfile,
    CoachProfile,
    Person,
    Region,
    School,
)
from apps.teams.models import Team, TeamCoach, TeamMember  # noqa: E402
from apps.verification.models import IdentityCheck  # noqa: E402

# ---------------------------------------------------------------- 常量

DEMO_CREATOR_ID = 999000001  # 演示数据创建人哨兵（BigInteger 逻辑引用位）
SEED_OPERATOR = "seed_demo"  # 公示发布操作人（便于 --clean 精准清理）
FILE_KEY_PREFIX = "demo/"

EVENT_01 = "event_01"  # 与既有 7 条 mock 报名共用，勿改
EVENT_02 = "event_02"

SQUAD_SIZE = 12  # 每支球队每个年龄段名单人数

# event_01 的组别：id 必须覆盖既有报名引用过的 6 个（group_u12/u13/u15/u18/gu12/gu15）
GROUP_SPECS_EVENT_01 = [
    # (id, 名称, category, gender, quota, min_age, max_age, max_players)
    ("group_u12", "小学男子组", "PRIMARY", "MALE", 16, 9, 12, 18),
    ("group_u13", "初中男子U13组", "MIDDLE", "MALE", 16, 12, 13, 18),
    ("group_u15", "初中男子组", "MIDDLE", "MALE", 16, 13, 15, 22),
    ("group_u18", "高中男子组", "MIDDLE", "MALE", 16, 15, 18, 22),
    ("group_gu12", "小学女子组", "PRIMARY", "FEMALE", 12, 9, 12, 18),
    ("group_gu15", "初中女子组", "MIDDLE", "FEMALE", 12, 13, 15, 22),
]
GROUP_SPECS_EVENT_02 = [
    ("grp_demo_c26_pm", "小学男子组", "PRIMARY", "MALE", 16, 9, 12, 18),
    ("grp_demo_c26_mf", "初中女子组", "MIDDLE", "FEMALE", 12, 13, 15, 22),
]
MATERIAL_TYPES = ["STAMPED_FORM", "STUDENT_STATUS"]

REGION_SPECS = [
    # (code, name, level, parent_code)
    ("510000", "四川省", enums.RegionLevel.PROVINCE, None),
    ("510100", "示例市州", enums.RegionLevel.CITY, "510000"),
    ("510200", "示例市州二", enums.RegionLevel.CITY, "510000"),
]
SCHOOL_SPECS = [
    # (code, name, school_type, region_code, address)
    ("SCH001", "示例第一中学", "SENIOR", "510100", "示例市州学府路1号"),
    ("SCH002", "示例第二中学", "JUNIOR", "510200", "示例市州二文华路2号"),
]

# 球队矩阵：squads 两个年龄段（年轻/年长），出生日期落在对应组别年龄窗内
TEAM_SPECS = [
    {
        "name": "示例第一中学男子足球队",
        "school_code": "SCH001",
        "parent_org": "org_school_01",
        "city_org": "org_city_01",
        "category": "U15",
        "gender": enums.Gender.MALE,
        "account": "acc_school01",
        "squads": [
            {
                "tag": "u15",
                "group": "group_u15",
                "birth": (date(2011, 1, 1), date(2013, 12, 31)),
                "grade": "JUNIOR",
            },
            {
                "tag": "u18",
                "group": "group_u18",
                "birth": (date(2008, 1, 1), date(2010, 12, 31)),
                "grade": "SENIOR",
            },
        ],
    },
    {
        "name": "示例第二中学男子足球队",
        "school_code": "SCH002",
        "parent_org": "org_school_02",
        "city_org": "org_city_02",
        "category": "U15",
        "gender": enums.Gender.MALE,
        "account": "acc_school02",
        "squads": [
            {
                "tag": "u15",
                "group": "group_u15",
                "birth": (date(2011, 1, 1), date(2013, 12, 31)),
                "grade": "JUNIOR",
            },
            {
                "tag": "u18",
                "group": "group_u18",
                "birth": (date(2008, 1, 1), date(2010, 12, 31)),
                "grade": "SENIOR",
            },
        ],
    },
    {
        "name": "示例第一中学女子足球队",
        "school_code": "SCH001",
        "parent_org": "org_school_01",
        "city_org": "org_city_01",
        "category": "U12",
        "gender": enums.Gender.FEMALE,
        "account": "acc_school01",
        "squads": [
            {
                "tag": "gu12",
                "group": "group_gu12",
                "birth": (date(2014, 1, 1), date(2016, 12, 31)),
                "grade": "PRIMARY_HIGH",
            },
            {
                "tag": "gu15",
                "group": "group_gu15",
                "birth": (date(2011, 1, 1), date(2013, 12, 31)),
                "grade": "JUNIOR",
            },
        ],
    },
    {
        "name": "示例第二中学女子足球队",
        "school_code": "SCH002",
        "parent_org": "org_school_02",
        "city_org": "org_city_02",
        "category": "U12",
        "gender": enums.Gender.FEMALE,
        "account": "acc_school02",
        "squads": [
            {
                "tag": "gu12",
                "group": "group_gu12",
                "birth": (date(2014, 1, 1), date(2016, 12, 31)),
                "grade": "PRIMARY_HIGH",
            },
            {
                "tag": "gu15",
                "group": "group_gu15",
                "birth": (date(2011, 1, 1), date(2013, 12, 31)),
                "grade": "JUNIOR",
            },
        ],
    },
]

# 报名矩阵：4 条新报名（真实 Person/Roster），状态覆盖 已通过/待省审/待市审
ENROLLMENT_SPECS = [
    {
        "key": "boys_u15",
        "team": 0,
        "squad": "u15",
        "group": "group_u15",
        "status": STATUS_APPROVED,
        "contact": "张伟",
        "remarks": "示例第一中学初中男子组报名（已通过）",
    },
    {
        "key": "boys_u18",
        "team": 0,
        "squad": "u18",
        "group": "group_u18",
        "status": STATUS_PENDING_PROVINCE,
        "contact": "张伟",
        "remarks": "示例第一中学高中男子组报名（待省级审核）",
    },
    {
        "key": "girls_u12",
        "team": 2,
        "squad": "gu12",
        "group": "group_gu12",
        "status": STATUS_APPROVED,
        "contact": "李娜",
        "remarks": "示例第一中学小学女子组报名（已通过）",
    },
    {
        "key": "girls_u15",
        "team": 3,
        "squad": "gu15",
        "group": "group_gu15",
        "status": STATUS_PENDING_CITY,
        "contact": "王秀兰",
        "remarks": "示例第二中学初中女子组报名（待市州审核）",
    },
]

_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK = "10X98765432"

_SURNAMES = (
    "王李张刘陈杨黄赵吴周徐孙马朱胡林郭何高罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘蒋蔡余杜叶程苏魏吕丁沈任姚"
)
_MALE_GIVEN = [
    "伟磊", "子轩", "浩然", "梓涵", "雨泽", "俊杰", "思远", "晨曦", "昊天", "嘉豪",
    "铭轩", "宇轩", "沐阳", "子墨", "一诺", "依诺", "志强", "永军", "建国", "晓东",
]
_FEMALE_GIVEN = [
    "欣怡", "诗涵", "梦琪", "若曦", "欣妍", "梓萱", "若彤", "芷若", "嘉怡", "雨桐",
    "思彤", "语彤", "子萱", "雨欣", "可欣", "静怡", "雅婷", "慧敏", "梦瑶", "一诺",
]
_POSITIONS = ["GK", "DF", "DF", "MF", "MF", "FW", "DF", "MF", "FW", "DF", "MF", "FW"]

_STATS = {}
_birth_seq: dict[tuple[str, date], int] = {}
_person_index = 0


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- 基础数据


def _spread_birth(start: date, end: date, count: int) -> list[date]:
    span = (end - start).days
    return [start + timedelta(days=(i * 29 + 11) % (span + 1)) for i in range(count)]


def _make_id_card(region_code: str, birth: date) -> str:
    key = (region_code, birth)
    _birth_seq[key] = _birth_seq.get(key, 0) + 1
    body = f"{region_code}{birth:%Y%m%d}{_birth_seq[key]:03d}"
    total = sum(int(body[i]) * _ID_WEIGHTS[i] for i in range(17))
    return body + _ID_CHECK[total % 11]


def _make_phone(seq: int) -> str:
    return f"1{3 + seq % 7}{seq:09d}"


def _person_name(index: int, gender: int) -> str:
    surnames = list(_SURNAMES)
    given = _MALE_GIVEN if gender == enums.Gender.MALE else _FEMALE_GIVEN
    return surnames[index % len(surnames)] + given[index % len(given)]


def _create_person(
    *,
    name: str,
    gender: int,
    birth: date,
    id_card: str,
    phone: str,
    category: str,
    region: Region,
    school: School,
    student_no: str = "",
    grade: str = "",
    position: str = "",
    guardian: str = "",
    coach_level: str = "",
    certificate_no: str = "",
    guardian_phone: str = "",
) -> Person:
    global _person_index
    _person_index += 1
    person = Person.objects.filter(id_card_hash=crypto.lookup_hash(id_card)).first()
    values = {
        "full_name": name,
        "gender": gender,
        "birth_date": birth,
        "id_card_cipher": crypto.encrypt_text(id_card),
        "id_card_hash": crypto.lookup_hash(id_card),
        "id_card_masked": crypto.mask_id_number(id_card),
        "phone_cipher": crypto.encrypt_text(phone),
        "phone_hash": crypto.lookup_hash(phone),
        "phone_masked": crypto.mask_phone(phone),
        "person_category": category,
        "audit_status": enums.AuditStatus.APPROVED,
        "status": enums.EnableStatus.ACTIVE,
        "region": region,
        "school": school,
        "created_by": DEMO_CREATOR_ID,
    }
    if person is None:
        person = Person.objects.create(**values)
    else:
        for field, value in values.items():
            setattr(person, field, value)
        person.save()
    if not person.person_no:
        person.person_no = f"P{person.created_at:%Y}{person.pk:08d}"
        person.save(update_fields=["person_no"])

    if category == enums.PersonCategory.ATHLETE:
        AthleteProfile.objects.update_or_create(
            person=person,
            defaults={
                "grade_level": grade,
                "student_no": student_no,
                "position_code": position,
                "guardian_name": guardian,
                "guardian_relation": "父母",
                "guardian_phone_cipher": crypto.encrypt_text(guardian_phone),
                "guardian_phone_hash": crypto.lookup_hash(guardian_phone),
                "guardian_phone_masked": crypto.mask_phone(guardian_phone),
            },
        )
    else:
        CoachProfile.objects.update_or_create(
            person=person,
            defaults={
                "coach_level": coach_level,
                "certificate_no": certificate_no,
                "certificate_expire_date": date(2028, 12, 31),
            },
        )
    return person


def seed_regions_and_schools() -> tuple[dict[str, Region], dict[str, School]]:
    regions: dict[str, Region] = {}
    for code, name, level, parent_code in REGION_SPECS:
        region, _ = Region.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "level": level,
                "parent": regions.get(parent_code) if parent_code else None,
                "sort_order": 10 if level == enums.RegionLevel.PROVINCE else 20,
                "is_active": True,
                "created_by": DEMO_CREATOR_ID,
            },
        )
        regions[code] = region
    schools: dict[str, School] = {}
    for code, name, school_type, region_code, address in SCHOOL_SPECS:
        school, _ = School.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "school_type": school_type,
                "region": regions[region_code],
                "address": address,
                "is_active": True,
                "created_by": DEMO_CREATOR_ID,
            },
        )
        schools[code] = school
    _STATS["regions"] = len(regions)
    _STATS["schools"] = len(schools)
    log(f"✅ 区划/学校：{len(regions)} 个区划、{len(schools)} 所学校")


def seed_persons_and_teams() -> list[dict]:
    """建 4 支球队及其运动员/教练（返回带 person/team 引用的上下文）。"""
    schools = {s.code: s for s in School.objects.all()}
    contexts: list[dict] = []

    for team_idx, spec in enumerate(TEAM_SPECS):
        school = schools[spec["school_code"]]
        city_region = school.region  # 学校所属市州区划（公示 city 口径）
        context: dict = {"spec": spec, "squads": {}, "coaches": []}

        # ---- 运动员（两个年龄段；球衣号按队内唯一编排：年长组整体后移）
        for squad_index, squad in enumerate(spec["squads"]):
            players: list[dict] = []
            births = _spread_birth(squad["birth"][0], squad["birth"][1], SQUAD_SIZE)
            for i in range(SQUAD_SIZE):
                birth = births[i]
                id_card = _make_id_card(city_region.code, birth)
                phone = _make_phone(_person_index + 1)
                guardian_phone = _make_phone(_person_index + 5000)
                person = _create_person(
                    name=_person_name(_person_index, spec["gender"]),
                    gender=spec["gender"],
                    birth=birth,
                    id_card=id_card,
                    phone=phone,
                    category=enums.PersonCategory.ATHLETE,
                    region=city_region,
                    school=school,
                    student_no=f"S2026{_person_index:06d}",
                    grade=squad["grade"],
                    position=_POSITIONS[i % len(_POSITIONS)],
                    guardian="家长" + str(i + 1),
                    guardian_phone=guardian_phone,
                )
                players.append(
                    {
                        "person": person,
                        "jersey": squad_index * SQUAD_SIZE + i + 1,
                        "position": _POSITIONS[i % len(_POSITIONS)],
                    }
                )
            context["squads"][squad["tag"]] = {"spec": squad, "players": players}

        # ---- 教练（主教练 + 助理教练）
        for duty, level in (
            (enums.CoachDuty.HEAD_COACH, "A"),
            (enums.CoachDuty.ASSISTANT_COACH, "B"),
        ):
            id_card = _make_id_card(city_region.code, date(1985, 6, 15))
            phone = _make_phone(_person_index + 1)
            person = _create_person(
                name=_person_name(_person_index, spec["gender"]),
                gender=spec["gender"],
                birth=date(1985, 6, 15),
                id_card=id_card,
                phone=phone,
                category=enums.PersonCategory.COACH,
                region=city_region,
                school=school,
                coach_level=level,
                certificate_no=f"CFA-2026-{team_idx:02d}{len(context['coaches']):02d}",
            )
            context["coaches"].append({"person": person, "duty": duty})

        # ---- 球队主档 + 组织节点（与 teams.services.create_team 同事务语义）
        with transaction.atomic():
            team = Team.objects.filter(
                name=spec["name"], school=school, is_deleted=False
            ).first()
            values = {
                "category_code": spec["category"],
                "region": city_region,
                "school": school,
                "introduction": f"{spec['name']}（演示数据）",
                "status": enums.EnableStatus.ACTIVE,
                "created_by": DEMO_CREATOR_ID,
            }
            if team is None:
                team = Team.objects.create(name=spec["name"], **values)
                team.team_no = f"T{team.created_at:%Y}{team.pk:08d}"
                team.save(update_fields=["team_no"])
            else:
                for field, value in values.items():
                    setattr(team, field, value)
                team.save()
            ensure_business_org("TEAM", team.team_no, team.name, parent_org_id=spec["parent_org"])
        context["team"] = team

        # ---- 成员/教练关系（幂等；球衣号即报名名单同款，队内唯一）
        for squad_ctx in context["squads"].values():
            for player in squad_ctx["players"]:
                TeamMember.objects.update_or_create(
                    team=team,
                    person=player["person"],
                    defaults={
                        "jersey_number": player["jersey"],
                        "position_code": player["position"],
                        "status": enums.RelationStatus.CURRENT,
                        "joined_at": date(2026, 9, 5),
                        "source": enums.RelationSource.DIRECT,
                    },
                )
        for coach in context["coaches"]:
            TeamCoach.objects.update_or_create(
                team=team,
                person=coach["person"],
                duty=coach["duty"],
                defaults={
                    "status": enums.RelationStatus.CURRENT,
                    "joined_at": date(2026, 9, 1),
                    "source": enums.RelationSource.DIRECT,
                },
            )
        contexts.append(context)

    _STATS["persons"] = Person.objects.filter(created_by=DEMO_CREATOR_ID).count()
    _STATS["teams"] = Team.objects.filter(created_by=DEMO_CREATOR_ID).count()
    log(
        f"✅ 人员/球队：{_STATS['persons']} 人（运动员 "
        f"{Person.objects.filter(created_by=DEMO_CREATOR_ID, person_category='ATHLETE').count()}"
        f" + 教练 {Person.objects.filter(created_by=DEMO_CREATOR_ID, person_category='COACH').count()}）、"
        f"{_STATS['teams']} 支球队、"
        f"{TeamMember.objects.filter(team__created_by=DEMO_CREATOR_ID).count()} 条球员关系、"
        f"{TeamCoach.objects.filter(team__created_by=DEMO_CREATOR_ID).count()} 条教练关系"
    )
    return contexts


# ---------------------------------------------------------------- 赛事 / 比赛


def seed_events_and_groups() -> dict[str, EventGroup]:
    event_01, _ = Event.objects.update_or_create(
        id=EVENT_01,
        defaults={
            "name": "2026年四川省中小学足球联赛",
            "season": "2026",
            "event_category": "SCHOOL_LEAGUE",
            "registration_opens_at": _aware(2026, 9, 1),
            "registration_closes_at": _aware(2026, 12, 31, 23, 59),
            "status": "RUNNING",
            "description": "省级中小学足球联赛（演示数据）：报名通道开放至 2026-12-31，比赛进行中。",
        },
    )
    Event.objects.update_or_create(
        id=EVENT_02,
        defaults={
            "name": "2026年四川省青少年足球锦标赛",
            "season": "2026",
            "event_category": "CHAMPIONSHIP",
            "registration_opens_at": _aware(2026, 11, 1),
            "registration_closes_at": _aware(2027, 1, 15, 23, 59),
            "status": "DRAFT",
            "description": "青少年足球锦标赛（演示数据）：筹备中，报名尚未开始。",
        },
    )
    groups: dict[str, EventGroup] = {}
    for specs, event in (
        (GROUP_SPECS_EVENT_01, event_01),
        (GROUP_SPECS_EVENT_02, Event.objects.get(pk=EVENT_02)),
    ):
        for gid, name, category, gender, quota, min_age, max_age, max_players in specs:
            group, _ = EventGroup.objects.update_or_create(
                id=gid,
                defaults={
                    "event": event,
                    "name": name,
                    "category": category,
                    "gender": gender,
                    "quota": quota,
                    "min_age": min_age,
                    "max_age": max_age,
                    "max_players": max_players,
                    "required_material_types": list(MATERIAL_TYPES),
                },
            )
            groups[gid] = group
    _STATS["events"] = Event.objects.count()
    _STATS["groups"] = EventGroup.objects.count()
    log(f"✅ 赛事/组别：{_STATS['events']} 个赛事、{_STATS['groups']} 个组别")
    return groups


def _aware(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return timezone.make_aware(datetime(year, month, day, hour, minute))


def _referee(name: str, role: str, level: str = "一级") -> dict:
    return {"name": name, "role": role, "level": level}


def seed_matches(contexts: list[dict]) -> dict[str, Match]:
    """按组别排比赛；球队 ID 用整型主键字符串（软引用）。"""
    t = [c["team"] for c in contexts]
    home, away = str(t[0].pk), str(t[1].pk)  # 男队：第一中学 vs 第二中学
    girls_home, girls_away = str(t[2].pk), str(t[3].pk)  # 女队
    venue = "市体育中心足球场"

    match_specs = [
        # (id, group, home, away, kickoff, stage, status, home_score, away_score, published, result_published, reason)
        ("mat_demo_u15_r1", "group_u15", home, away, _aware(2026, 10, 10, 15, 0), "小组赛", "FINISHED", 3, 1, True, True, ""),
        ("mat_demo_u15_r2", "group_u15", away, home, _aware(2026, 10, 24, 15, 0), "半决赛", "FINISHED", 1, 2, True, True, ""),
        ("mat_demo_u15_r3", "group_u15", home, away, _aware(2026, 11, 7, 15, 0), "决赛", "SCHEDULED", None, None, False, False, ""),
        ("mat_demo_u18_r1", "group_u18", home, away, _aware(2026, 10, 11, 9, 0), "小组赛", "FINISHED", 2, 2, True, True, ""),
        ("mat_demo_u18_r2", "group_u18", home, away, _aware(2026, 11, 8, 9, 0), "小组赛", "SCHEDULED", None, None, False, False, ""),
        ("mat_demo_u18_r3", "group_u18", away, home, _aware(2026, 10, 18, 9, 0), "小组赛", "CANCELLED", None, None, False, False, "因暴雨改期，另行通知"),
        ("mat_demo_gu12_r1", "group_gu12", girls_home, girls_away, _aware(2026, 10, 12, 10, 0), "小组赛", "FINISHED", 4, 0, True, True, ""),
        ("mat_demo_gu12_r2", "group_gu12", girls_away, girls_home, _aware(2026, 10, 27, 19, 0), "半决赛", "LIVE", 1, 0, False, False, ""),
        ("mat_demo_gu15_r1", "group_gu15", girls_home, girls_away, _aware(2026, 10, 13, 14, 0), "小组赛", "FINISHED", 1, 1, True, True, ""),
        ("mat_demo_gu15_r2", "group_gu15", girls_away, girls_home, _aware(2026, 11, 10, 14, 0), "半决赛", "SCHEDULED", None, None, False, False, ""),
    ]
    matches: dict[str, Match] = {}
    for (
        mid,
        group_id,
        home_team,
        away_team,
        kickoff,
        stage,
        status,
        home_score,
        away_score,
        published,
        result_published,
        reason,
    ) in match_specs:
        match, _ = Match.objects.update_or_create(
            id=mid,
            defaults={
                "event_id": EVENT_01,
                "group_id": group_id,
                "home_team_id": home_team,
                "away_team_id": away_team,
                "kickoff_at": kickoff,
                "venue": venue,
                "stage": stage,
                "status": status,
                "referees": [
                    _referee("王建国", "REFEREE"),
                    _referee("李晓梅", "ASSISTANT"),
                    _referee("赵卫东", "FOURTH_OFFICIAL"),
                ],
                "home_score": home_score,
                "away_score": away_score,
                "published": published,
                "result_published": result_published,
                "result_type": "NORMAL",
                "result_reason": reason,
            },
        )
        matches[mid] = match
    _STATS["matches"] = len(matches)
    log(f"✅ 比赛：{len(matches)} 场（含 Finished/Live/Scheduled/Cancelled 各状态）")
    return matches


def _athlete(contexts: list[dict], team_idx: int, squad_tag: str, index: int) -> dict:
    return contexts[team_idx]["squads"][squad_tag]["players"][index]


def seed_match_events(contexts: list[dict], matches: dict[str, Match]) -> int:
    """为已结束/进行中的比赛补进球、判罚、换人事件。"""
    created = 0

    def add(match: Match, **kwargs) -> None:
        nonlocal created
        mev_id = kwargs.pop("id")
        defaults = {
            "match_id": match.pk,
            "match_version": match.version,
            "stoppage_minute": None,
            "own_goal": False,
            "note": "",
        }
        defaults.update(kwargs)
        MatchEvent.objects.update_or_create(id=mev_id, defaults=defaults)
        created += 1

    def goal(match, idx, minute, team_idx, squad, player_idx, note=""):
        player = _athlete(contexts, team_idx, squad, player_idx)
        add(
            match,
            id=f"mev_demo_{idx}",
            event_type="GOAL",
            minute=minute,
            team_id=str(contexts[team_idx]["team"].pk),
            scorer_athlete_id=str(player["person"].pk),
            note=note,
        )

    def card(match, idx, minute, team_idx, squad, player_idx, penalty_type, reason):
        player = _athlete(contexts, team_idx, squad, player_idx)
        add(
            match,
            id=f"mev_demo_{idx}",
            event_type="PENALTY",
            minute=minute,
            team_id=str(contexts[team_idx]["team"].pk),
            athlete_id=str(player["person"].pk),
            penalty_type=penalty_type,
            reason=reason,
        )

    def sub(match, idx, minute, team_idx, squad, out_idx, in_idx):
        out_player = _athlete(contexts, team_idx, squad, out_idx)
        in_player = _athlete(contexts, team_idx, squad, in_idx)
        add(
            match,
            id=f"mev_demo_{idx}",
            event_type="SUBSTITUTION",
            minute=minute,
            team_id=str(contexts[team_idx]["team"].pk),
            out_athlete_id=str(out_player["person"].pk),
            in_athlete_id=str(in_player["person"].pk),
            note="战术调整",
        )

    # 男队 U15 第一轮 3:1
    m = matches["mat_demo_u15_r1"]
    goal(m, "u15r1_g1", 12, 0, "u15", 0)
    goal(m, "u15r1_g2", 34, 0, "u15", 4)
    goal(m, "u15r1_g3", 58, 0, "u15", 7)
    goal(m, "u15r1_g4", 71, 1, "u15", 5)
    card(m, "u15r1_y1", 45, 1, "u15", 3, "YELLOW_CARD", "战术犯规")
    card(m, "u15r1_y2", 80, 0, "u15", 2, "YELLOW_CARD", "拖延时间")
    sub(m, "u15r1_s1", 60, 0, "u15", 10, 11)

    # 男队 U15 半决赛 1:2
    m = matches["mat_demo_u15_r2"]
    goal(m, "u15r2_g1", 22, 1, "u15", 8)
    goal(m, "u15r2_g2", 55, 0, "u15", 1)
    goal(m, "u15r2_g3", 77, 0, "u15", 9, note="制胜球")
    card(m, "u15r2_r1", 63, 1, "u15", 6, "RED_CARD", "严重犯规")

    # 男队 U18 小组赛 2:2
    m = matches["mat_demo_u18_r1"]
    goal(m, "u18r1_g1", 10, 0, "u18", 2)
    goal(m, "u18r1_g2", 30, 0, "u18", 6)
    goal(m, "u18r1_g3", 44, 1, "u18", 1)
    goal(m, "u18r1_g4", 66, 1, "u18", 9)

    # 女队 U12 小组赛 4:0
    m = matches["mat_demo_gu12_r1"]
    for i, minute in enumerate((5, 19, 38, 52)):
        goal(m, f"gu12r1_g{i + 1}", minute, 2, "gu12", i)

    # 女队 U12 半决赛（进行中）1:0
    m = matches["mat_demo_gu12_r2"]
    goal(m, "gu12r2_g1", 27, 2, "gu12", 3)

    # 女队 U15 小组赛 1:1
    m = matches["mat_demo_gu15_r1"]
    goal(m, "gu15r1_g1", 25, 2, "gu15", 5)
    goal(m, "gu15r1_g2", 70, 3, "gu15", 4)

    _STATS["match_events"] = MatchEvent.objects.filter(id__startswith="mev_demo_").count()
    log(f"✅ 比赛事件：{_STATS['match_events']} 条（进球/黄红牌/换人）")
    return created


# ---------------------------------------------------------------- 文件 / 核验


def _demo_pdf(title: str) -> bytes:
    """最小可识别 PDF 占位内容（配合 seed_files 真实写字节）。"""
    import re as _re

    text = _re.sub(r"[^\x20-\x7e]", "", title) or "Demo material"
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


def _demo_jpeg(seed: int) -> bytes:
    """占位抓拍照 JPEG。"""
    import io as _io

    from PIL import Image, ImageDraw

    width, height = 640, 480
    image = Image.new("RGB", (width, height), (36 + seed % 40, 60, 104))
    draw = ImageDraw.Draw(image)
    for x in range(-height, width + height, 70):
        draw.line([(x, 0), (x + height, height)], fill=(60, 96, 148), width=6)
    draw.ellipse(
        [width * 0.3, height * 0.2, width * 0.7, height * 0.8],
        outline=(235, 240, 250), width=4,
    )
    buffer = _io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _write_storage(storage_key: str, data: bytes) -> None:
    """把字节写入配置的存储后端（Local 散列路径 / MinIO）。"""
    import io as _io

    from apps.attachments.storage import get_storage

    get_storage().save(storage_key, _io.BytesIO(data))


def seed_files(contexts: list[dict], enrollments: dict[str, Enrollment]) -> dict[str, str]:
    """演示文件记录：核验抓拍照 + 报名材料（同步写真实占位字节）。"""
    photo_ids: dict[str, str] = {}
    seq = 0
    for match_id, match in _iter_identity_matches(contexts):
        for team_idx, squad_tag, players in match:
            for player in players[:4]:
                seq += 1
                file_id = f"fil_demo_face_{seq:03d}"
                data = _demo_jpeg(seq)
                FileRecord.objects.update_or_create(
                    id=file_id,
                    defaults={
                        "owner_type": "IDENTITY_CHECK",
                        "owner_id": match_id,
                        "category": "FACE_CAPTURE",
                        "original_name": f"capture_{seq:03d}.jpg",
                        "content_type": "image/jpeg",
                        "size_bytes": len(data),
                        "private": True,
                        "storage_key": f"{FILE_KEY_PREFIX}identity/{file_id}.jpg",
                        "storage_backend": "local",
                        "uploaded_by": "acc_recorder",
                    },
                )
                _write_storage(f"{FILE_KEY_PREFIX}identity/{file_id}.jpg", data)
                photo_ids[(match_id, str(player["person"].pk))] = file_id

    material_ids: dict[str, dict[str, str]] = {}
    for key, enrollment in enrollments.items():
        form_id = f"fil_demo_form_{key}"
        form_data = _demo_pdf(f"Enrollment form {key}")
        FileRecord.objects.update_or_create(
            id=form_id,
            defaults={
                "owner_type": "ENROLLMENT",
                "owner_id": enrollment.pk,
                "category": "STAMPED_FORM",
                "original_name": f"报名表与名单_{key}.pdf",
                "content_type": "application/pdf",
                "size_bytes": len(form_data),
                "private": True,
                "storage_key": f"{FILE_KEY_PREFIX}enrollments/{key}/form.pdf",
                "storage_backend": "local",
                "uploaded_by": enrollment.created_by or "acc_school01",
            },
        )
        _write_storage(f"{FILE_KEY_PREFIX}enrollments/{key}/form.pdf", form_data)
        status_id = f"fil_demo_status_{key}"
        status_data = _demo_pdf(f"Student status {key}")
        FileRecord.objects.update_or_create(
            id=status_id,
            defaults={
                "owner_type": "ENROLLMENT",
                "owner_id": enrollment.pk,
                "category": "STUDENT_STATUS",
                "original_name": f"学籍证明_{key}.pdf",
                "content_type": "application/pdf",
                "size_bytes": len(status_data),
                "private": True,
                "storage_key": f"{FILE_KEY_PREFIX}enrollments/{key}/status.pdf",
                "storage_backend": "local",
                "uploaded_by": enrollment.created_by or "acc_school01",
            },
        )
        _write_storage(f"{FILE_KEY_PREFIX}enrollments/{key}/status.pdf", status_data)
        material_ids[key] = {"form": form_id, "status": status_id}
    _STATS["files"] = FileRecord.objects.filter(storage_key__startswith=FILE_KEY_PREFIX).count()
    log(f"✅ 演示文件：{_STATS['files']} 条文件记录")
    return {"photos": photo_ids, "materials": material_ids}


def _iter_identity_matches(contexts: list[dict]):
    """产出 (match_id, [(team_idx, squad_tag, players), ...])，用于核验数据。"""
    return [
        ("mat_demo_gu12_r2", [(2, "gu12", contexts[2]["squads"]["gu12"]["players"])]),
        ("mat_demo_u15_r1", [
            (0, "u15", contexts[0]["squads"]["u15"]["players"]),
            (1, "u15", contexts[1]["squads"]["u15"]["players"]),
        ]),
        ("mat_demo_gu12_r1", [(2, "gu12", contexts[2]["squads"]["gu12"]["players"])]),
    ]


def seed_identity_checks(
    contexts: list[dict],
    enrollments: dict[str, Enrollment],
    photo_ids: dict[tuple[str, str], str],
) -> None:
    """身份核验：机器结果与人工结果分开（契约 4.5）。"""
    created = 0

    def add(match_id: str, enrollment: Enrollment, player: dict, *, mode, machine_status,
            machine_verified, manual_status, reviewer="", reviewed_at=None, reason=""):
        nonlocal created
        IdentityCheck.objects.update_or_create(
            id=f"idc_demo_{match_id}_{player['person'].pk}",
            defaults={
                "match_id": match_id,
                "enrollment_id": enrollment.pk,
                "athlete_id": str(player["person"].pk),
                "captured_photo_file_id": photo_ids.get((match_id, str(player["person"].pk)), ""),
                "mode": mode,
                "machine_status": machine_status,
                "machine_verified": machine_verified,
                "manual_status": manual_status,
                "manual_reason": reason,
                "reviewer_account_id": reviewer,
                "reviewed_at": reviewed_at,
            },
        )
        created += 1

    reviewed = _aware(2026, 10, 27, 20, 30)
    # 已结束比赛：机器比对 + 人工确认
    m = "mat_demo_gu12_r1"
    for i, player in enumerate(contexts[2]["squads"]["gu12"]["players"][:4]):
        add(
            m,
            enrollments["girls_u12"],
            player,
            mode="FACE_COMPARE",
            machine_status="MATCH",
            machine_verified=True,
            manual_status="CONFIRMED",
            reviewer="acc_recorder",
            reviewed_at=reviewed,
        )
    m = "mat_demo_u15_r1"
    for i, player in enumerate(contexts[0]["squads"]["u15"]["players"][:4]):
        mismatch = i == 3
        add(
            m,
            enrollments["boys_u15"],
            player,
            mode="FACE_COMPARE",
            machine_status="MISMATCH" if mismatch else "MATCH",
            machine_verified=not mismatch,
            manual_status="PENDING" if mismatch else "CONFIRMED",
            reviewer="acc_recorder",
            reviewed_at=None if mismatch else reviewed,
            reason="机器比对不一致，待人工复核" if mismatch else "",
        )
    # 进行中比赛：一条人工驳回
    m = "mat_demo_gu12_r2"
    for i, player in enumerate(contexts[2]["squads"]["gu12"]["players"][:3]):
        rejected = i == 2
        add(
            m,
            enrollments["girls_u12"],
            player,
            mode="FACE_COMPARE",
            machine_status="MATCH",
            machine_verified=True,
            manual_status="REJECTED" if rejected else "CONFIRMED",
            reviewer="acc_recorder",
            reviewed_at=reviewed,
            reason="非本人出场，取消本场成绩" if rejected else "",
        )
    # 未开始比赛：未跑核验
    m = "mat_demo_u15_r3"
    for player in contexts[0]["squads"]["u15"]["players"][:2]:
        add(
            m,
            enrollments["boys_u15"],
            player,
            mode="FACE_COMPARE",
            machine_status="NOT_RUN",
            machine_verified=False,
            manual_status="NOT_REVIEWED",
        )
    _STATS["identity_checks"] = IdentityCheck.objects.filter(id__startswith="idc_demo_").count()
    log(f"✅ 身份核验：{_STATS['identity_checks']} 条")


# ---------------------------------------------------------------- 报名单


def _snapshot_payload(enrollment: Enrollment, team: Team, players: list[dict],
                      coaches: list[dict], attachments: list[EnrollmentAttachment]) -> dict:
    from apps.enrollments.services import to_attachment, to_roster

    player_payloads = []
    for item in enrollment.players.all().select_related():
        person = Person.objects.filter(pk=item.athlete_id).first()
        player_payloads.append(
            {
                **to_roster(item),
                "gender": person.get_gender_display() if person else None,
                "birthDate": person.birth_date.isoformat() if person and person.birth_date else None,
                "maskedStudentNumber": (
                    crypto.mask_student_number(person.athlete_profile.student_no)
                    if person and hasattr(person, "athlete_profile") and person.athlete_profile.student_no
                    else None
                ),
            }
        )
    payload = {
        "enrollment": {"id": enrollment.pk, "status": enrollment.status},
        "team": {
            "id": str(team.pk),
            "name": team.name,
            "schoolName": team.school.name if team.school_id else "",
            "cityName": team.region.name if team.region_id else "",
            "coachIds": [str(c["person"].pk) for c in coaches],
        },
        "players": player_payloads,
        "coachIds": [str(c["person"].pk) for c in coaches],
        "coaches": [
            {"id": str(c["person"].pk), "name": c["person"].full_name, "role": c["duty"]}
            for c in coaches
        ],
        "attachments": [to_attachment(item) for item in attachments],
    }
    # 快照落 JSONField：datetime 等先转 ISO 字符串，冻结后不再变化（与 build_snapshot_payload 一致）
    return json.loads(json.dumps(payload, cls=DjangoJSONEncoder))

def seed_enrollments(contexts: list[dict]) -> dict[str, Enrollment]:
    enrollments: dict[str, Enrollment] = {}
    review_total = 0
    roster_total = 0
    for spec in ENROLLMENT_SPECS:
        context = contexts[spec["team"]]
        team = context["team"]
        squad_ctx = context["squads"][spec["squad"]]
        players = squad_ctx["players"]
        coaches = context["coaches"]
        city_org = context["spec"]["city_org"]
        account = context["spec"]["account"]
        phone = _make_phone(90000 + len(enrollments))

        enrollment, _ = Enrollment.objects.update_or_create(
            id=f"enr_demo_{spec['key']}",
            defaults={
                "event_id": EVENT_01,
                "group_id": spec["group"],
                "team_id": str(team.pk),
                "subject_type": "SCHOOL_TEAM",
                "status": spec["status"],
                "submission_no": 1,
                "contact_name": spec["contact"],
                "contact_phone_cipher": crypto.encrypt_text(phone),
                "contact_phone_masked": crypto.mask_phone(phone),
                "city_org_id": city_org,
                "remarks": spec["remarks"],
                "created_by": account,
            },
        )
        enrollments[spec["key"]] = enrollment

        # 名单：球衣号与球队成员关系一致（同队同赛事跨报名唯一，年长组整体后移）
        for i, player in enumerate(players):
            pending = spec["key"] == "boys_u15" and i == 5
            RosterItem.objects.update_or_create(
                id=f"rst_demo_{spec['key']}_{i:02d}",
                defaults={
                    "enrollment": enrollment,
                    "athlete_id": str(player["person"].pk),
                    "athlete_name": player["person"].full_name,
                    "masked_id_number": player["person"].id_card_masked,
                    "jersey_number": player["jersey"],
                    "position": player["position"],
                    "qualification_status": "PENDING_VERIFICATION" if pending else "QUALIFIED",
                    "qualification_reason": "学籍证明待核实" if pending else None,
                },
            )
            roster_total += 1

        # 材料关联 + 快照（提交即冻结）
        attachments = [
            EnrollmentAttachment.objects.update_or_create(
                id=f"att_demo_{spec['key']}_form",
                defaults={
                    "enrollment": enrollment,
                    "material_type": "STAMPED_FORM",
                    "file_id": f"fil_demo_form_{spec['key']}",
                },
            )[0],
            EnrollmentAttachment.objects.update_or_create(
                id=f"att_demo_{spec['key']}_status",
                defaults={
                    "enrollment": enrollment,
                    "material_type": "STUDENT_STATUS",
                    "file_id": f"fil_demo_status_{spec['key']}",
                },
            )[0],
        ]
        payload = _snapshot_payload(enrollment, team, players, coaches, attachments)
        EnrollmentSnapshot.objects.update_or_create(
            id=f"snp_demo_{spec['key']}",
            defaults={
                "enrollment": enrollment,
                "submission_no": 1,
                "submitted_at": _aware(2026, 10, 8, 10, 0),
                "payload": payload,
            },
        )

        # 审核链：状态推导 version 与既有数据一致（提交+1，逐级审核+1）
        version = {STATUS_DRAFT: 0, STATUS_PENDING_CITY: 1, STATUS_PENDING_PROVINCE: 2,
                   STATUS_APPROVED: 3, "REJECTED": 2, "WITHDRAWN": 3}[spec["status"]]
        enrollment.version = version
        enrollment.save(update_fields=["version"])

        chain = [
            ("SUBMISSION", "SUBMIT", STATUS_DRAFT, STATUS_PENDING_CITY, account),
        ]
        if spec["status"] in (STATUS_PENDING_PROVINCE, STATUS_APPROVED):
            chain.append(("CITY", "APPROVE", STATUS_PENDING_CITY, STATUS_PENDING_PROVINCE, "acc_admin"))
        if spec["status"] == STATUS_APPROVED:
            chain.append(("PROVINCE", "APPROVE", STATUS_PENDING_PROVINCE, STATUS_APPROVED, "acc_province"))
        for i, (stage, action, from_status, to_status, operator) in enumerate(chain):
            ReviewRecord.objects.update_or_create(
                id=f"rev_demo_{spec['key']}_{i}",
                defaults={
                    "enrollment": enrollment,
                    "submission_no": 1,
                    "stage": stage,
                    "action": action,
                    "from_status": from_status,
                    "to_status": to_status,
                    "reason": "材料齐全，同意申报" if action == "APPROVE" else "",
                    "operator_account_id": operator,
                    "snapshot_id": f"snp_demo_{spec['key']}",
                },
            )
            review_total += 1

    _STATS["enrollments"] = len(enrollments)
    _STATS["roster_items"] = roster_total
    _STATS["reviews"] = review_total
    log(
        f"✅ 报名单：{len(enrollments)} 条（已通过 2 / 待省审 1 / 待市审 1），"
        f"名单 {roster_total} 条、快照 {len(enrollments)} 份、审核记录 {review_total} 条、"
        f"材料关联 {2 * len(enrollments)} 条"
    )
    return enrollments


# ---------------------------------------------------------------- 遴选公示


def publish_publicity(enrollments: dict[str, Enrollment]) -> None:
    """按成员6 公示源发布演示报名（ATHLETE/COACH/TEAM），使遴选公示不再为 0。"""
    from selection.common import BusinessError
    from selection.services.publicity import publish_registration
    from selection.services.publicity_source import get_publicity_source_client

    client = get_publicity_source_client()
    records = client.list_registrations(event_id=EVENT_01)

    coach_pks = {
        str(relation.pk)
        for relation in TeamCoach.objects.filter(team__created_by=DEMO_CREATOR_ID)
    }
    demo_ids = (
        {item.pk for enrollment in enrollments.values() for item in enrollment.players.all()}
        | {enrollment.pk for enrollment in enrollments.values()}
        | coach_pks
    )
    targets = [record for record in records if record.source_id in demo_ids]

    published = skipped = 0
    for record in targets:
        try:
            publish_registration(
                {
                    "event_id": EVENT_01,
                    "publicity_type": record.publicity_type,
                    "source_id": record.source_id,
                },
                user_id=SEED_OPERATOR,
            )
            published += 1
        except BusinessError as exc:
            if exc.code == 409:
                skipped += 1
            else:
                raise
    _STATS["publicity"] = published + skipped
    log(f"✅ 遴选公示：发布 {published} 条（已存在跳过 {skipped} 条）")


# ---------------------------------------------------------------- 清理


def clean_demo_data() -> None:
    """删除本脚本产生的演示数据（保留 event_01 及其组别：既有 mock 报名仍在引用）。"""
    from selection.models import PublicityRecord

    PublicityRecord.objects.filter(published_by=SEED_OPERATOR).delete()
    IdentityCheck.objects.filter(id__startswith="idc_demo_").delete()
    MatchEvent.objects.filter(id__startswith="mev_demo_").delete()
    Match.objects.filter(id__startswith="mat_demo_").delete()
    Enrollment.objects.filter(id__startswith="enr_demo_").delete()  # 级联名单/快照/审核/材料
    team_nos = list(
        Team.objects.filter(created_by=DEMO_CREATOR_ID).values_list("team_no", flat=True)
    )
    TeamCoach.objects.filter(team__created_by=DEMO_CREATOR_ID).delete()
    TeamMember.objects.filter(team__created_by=DEMO_CREATOR_ID).delete()
    Team.objects.filter(created_by=DEMO_CREATOR_ID).delete()
    from apps.accounts.models import Organization

    Organization.all_objects.filter(code__in=team_nos).delete()
    Person.objects.filter(created_by=DEMO_CREATOR_ID).delete()  # 级联扩展档案
    FileRecord.objects.filter(storage_key__startswith=FILE_KEY_PREFIX).delete()
    EventGroup.objects.filter(id__startswith="grp_demo_").delete()
    Event.objects.filter(pk=EVENT_02).delete()
    School.objects.filter(created_by=DEMO_CREATOR_ID).delete()
    # Region.parent 为 PROTECT 自引用：按层级自深到浅逐条删
    for region in Region.objects.filter(created_by=DEMO_CREATOR_ID).order_by("-level"):
        region.delete()
    log("🧹 已清理本脚本产生的演示数据")


# ---------------------------------------------------------------- 汇总


def print_summary() -> None:
    from selection.models import PublicityRecord

    log("")
    log("📊 演示数据汇总：")
    log(f"  人员 person            : {Person.objects.count()} 行")
    log(f"  球队 team              : {Team.objects.count()} 行")
    log(f"  赛事 events_event      : {Event.objects.count()} 行")
    log(f"  组别 events_event_group: {EventGroup.objects.count()} 行")
    log(f"  比赛 events_match      : {Match.objects.count()} 行")
    log(f"  比赛事件 match_event   : {MatchEvent.objects.count()} 行")
    log(f"  身份核验 identity_check: {IdentityCheck.objects.count()} 行")
    log(f"  报名 enrollments       : {Enrollment.objects.count()} 行")
    log(f"  遴选公示 sel_publicity : {PublicityRecord.objects.count()} 行")


def main() -> None:
    args = set(sys.argv[1:])
    if args - {"--clean", "--no-publicity"}:
        log("用法：python scripts/seed_demo_data.py [--clean] [--no-publicity]")
        sys.exit(2)

    if "--clean" in args:
        with transaction.atomic():
            clean_demo_data()

    with transaction.atomic():
        seed_regions_and_schools()
        contexts = seed_persons_and_teams()
        seed_events_and_groups()
        matches = seed_matches(contexts)
        seed_match_events(contexts, matches)
        # 先占位报名（拿 ID 供文件/核验引用），再补快照等细节
        placeholder = {
            spec["key"]: Enrollment(id=f"enr_demo_{spec['key']}", event_id=EVENT_01,
                                    group_id=spec["group"], team_id="", subject_type="SCHOOL_TEAM",
                                    status=spec["status"], submission_no=1,
                                    contact_name=spec["contact"], city_org_id=contexts[spec["team"]]["spec"]["city_org"],
                                    created_by=contexts[spec["team"]]["spec"]["account"])
            for spec in ENROLLMENT_SPECS
        }
        files = seed_files(contexts, placeholder)
        seed_identity_checks(contexts, placeholder, files["photos"])
        enrollments = seed_enrollments(contexts)
        # 文件/核验里的 owner_id 已用占位 ID（与正式 ID 一致），无需回填

    if "--no-publicity" not in args:
        publish_publicity(enrollments)
    print_summary()


if __name__ == "__main__":
    main()
