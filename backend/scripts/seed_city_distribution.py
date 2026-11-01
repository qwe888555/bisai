#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给「报名统计 → 市州报名热力」补一套按真实市州分布的数据。

背景：演示库里只有“示例市州教育局 / 示例市州二教育局”，热力图上 21 个
市州全是 0，点进去也看不到明细。本脚本：

1. 把两个示例市州改名为成都市、绵阳市，并同步区划、学校、球队名称；
2. 为其余 19 个市州创建区划、学校、球队；
3. 每个市州按 1~4 支球队生成 event_01 的报名单（含球员名册），
   状态覆盖已通过 / 待市审 / 待省审 / 已驳回。

用法（backend/ 目录下）：
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python scripts/seed_city_distribution.py

脚本幂等：已存在的数据会跳过，可重复执行。
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.core.cache import cache  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounts.models import Account, Organization  # noqa: E402
from apps.enrollments.models import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_PENDING_CITY,
    STATUS_PENDING_PROVINCE,
    STATUS_REJECTED,
    Enrollment,
    RosterItem,
)
from apps.persons.models import Region, School  # noqa: E402
from apps.teams.models import Team  # noqa: E402

NOW = timezone.now()
EVENT_ID = "event_01"
SCHOOL_CODE_PREFIX = "SCYX"  # 自建学校标识码前缀，避免与真实国标码冲突

# 市州 → (国标区划码, 学校名, 需新增球队数, 已有球队是否沿用)
CITY_SPECS = [
    ("成都市", "510100", "四川省成都市第七中学", 1, True),
    ("绵阳市", "510700", "四川省绵阳中学", 1, True),
    ("德阳市", "510600", "四川省德阳中学校", 3, False),
    ("南充市", "511300", "四川省南充高级中学", 3, False),
    ("宜宾市", "511500", "四川省宜宾市第一中学校", 3, False),
    ("泸州市", "510500", "四川省泸州高级中学校", 2, False),
    ("达州市", "511700", "四川省达州中学", 2, False),
    ("乐山市", "511100", "四川省乐山第一中学校", 2, False),
    ("凉山州", "513400", "西昌市第一中学", 2, False),
    ("雅安市", "511800", "四川省雅安中学", 2, False),
    ("眉山市", "511400", "四川省眉山中学校", 2, False),
    ("自贡市", "510300", "自贡市蜀光中学", 2, False),
    ("内江市", "511000", "四川省内江市第六中学", 2, False),
    ("遂宁市", "510900", "四川省遂宁中学校", 2, False),
    ("广元市", "510800", "四川省广元中学", 2, False),
    ("巴中市", "511900", "四川省巴中中学", 2, False),
    ("广安市", "511600", "四川省广安友谊中学", 2, False),
    ("资阳市", "512000", "四川省资阳中学", 1, False),
    ("攀枝花市", "510400", "攀枝花市第三高级中学校", 1, False),
    ("阿坝州", "513200", "四川省马尔康中学", 1, False),
    ("甘孜州", "513300", "四川省康定中学", 1, False),
]

# 新增球队的组别轮换：(球队组别, 性别, 报名组别)
TEAM_SLOTS = [
    ("U12", "男子", "group_u12"),
    ("U15", "男子", "group_u15"),
    ("U15", "女子", "group_gu15"),
    ("U18", "男子", "group_u18"),
]

STATUS_CYCLE = [
    STATUS_APPROVED,
    STATUS_APPROVED,
    STATUS_PENDING_CITY,
    STATUS_APPROVED,
    STATUS_PENDING_PROVINCE,
    STATUS_APPROVED,
    STATUS_PENDING_CITY,
    STATUS_REJECTED,
    STATUS_APPROVED,
    STATUS_PENDING_PROVINCE,
]

SURNAMES = list(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘"
    "于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江"
)
GIVEN_NAMES = [
    "子轩", "浩然", "宇航", "博文", "俊杰", "明轩", "天佑", "嘉豪", "梓涵", "一鸣",
    "思远", "雨泽", "晨阳", "皓宇", "子涵", "欣怡", "诗涵", "若曦", "梦琪", "雨欣",
    "佳怡", "婉清", "语彤", "可欣", "文博", "志强", "宇航", "锦程", "思源", "振宇",
    "泽楷", "俊熙", "铭泽", "家和", "睿哲", "沐阳", "启航", "若彤", "书瑶", "清妍",
    "雨桐", "思琪", "可馨", "梦瑶", "雅涵", "诗琪", "静怡", "语嫣", "紫萱", "心怡",
]
POSITIONS = ["门将", "后卫", "后卫", "中场", "中场", "前锋", "前锋"]


# 示例库里已有两个市州组织，沿用原主键，避免出现两个“成都市”。
CITY_ORG_PKS = {"510100": "org_city_01", "510700": "org_city_02"}


def city_org_pk(region_code: str) -> str:
    return CITY_ORG_PKS.get(region_code, f"org_city_{region_code}")


def _city_org(region_code: str, name: str) -> Organization:
    pk = city_org_pk(region_code)
    org = Organization.objects.filter(pk=pk).first()
    if org is None:
        org = Organization.objects.create(
            id=pk,
            name=name,
            code=region_code,
            org_type="CITY",
            parent_id="org_prov",
            enabled=True,
        )
        return org
    updates = []
    for field, value in (("name", name), ("code", region_code), ("org_type", "CITY"),
                         ("parent_id", "org_prov"), ("enabled", True)):
        if getattr(org, field) != value:
            setattr(org, field, value)
            updates.append(field)
    if updates:
        org.save(update_fields=updates + ["updated_at"])
    return org


def _region(code: str, name: str) -> Region:
    region = Region.objects.filter(code=code).first()
    if region is None:
        region = Region.objects.create(
            name=name, code=code, level=2, parent_id=1, sort_order=int(code[-2:])
        )
    elif region.name != name or region.parent_id != 1:
        region.name = name
        region.parent_id = 1
        region.save(update_fields=["name", "parent", "updated_at"])
    return region


def _school(code: str, name: str, region: Region) -> School:
    school = School.objects.filter(code=code).first()
    if school is None:
        school = School.objects.create(
            name=name, code=code, school_type="COMPLETE", region=region, is_active=True
        )
    else:
        updates = []
        if school.name != name:
            school.name = name
            updates.append("name")
        if school.region_id != region.pk:
            school.region = region
            updates.append("region")
        if updates:
            school.save(update_fields=updates + ["updated_at"])
    return school


def _rename_existing(city_name: str, school_name: str) -> None:
    """把两个示例市州（成都市 / 绵阳市）的区划、学校、球队名称改成真实名称。"""
    if city_name == "成都市":
        org = Organization.objects.filter(pk="org_city_01").first()
        school = School.objects.filter(pk=1).first()
        region = Region.objects.filter(pk=2).first()
        team_names = {
            1: "四川省成都市第七中学男子U15足球队",
            3: "四川省成都市第七中学女子U12足球队",
        }
        team_orgs = {
            "org_team_01": "四川省成都市第七中学男子足球队",
            "org_school_01": "四川省成都市第七中学",
        }
    else:
        org = Organization.objects.filter(pk="org_city_02").first()
        school = School.objects.filter(pk=2).first()
        region = Region.objects.filter(pk=3).first()
        team_names = {
            2: "四川省绵阳中学男子U15足球队",
            4: "四川省绵阳中学女子U12足球队",
        }
        team_orgs = {
            "org_team_02": "四川省绵阳中学女子足球队",
            "org_school_02": "四川省绵阳中学",
        }

    if org is not None and org.name != city_name:
        org.name = city_name
        org.save(update_fields=["name", "updated_at"])
    if region is not None:
        updates = []
        if region.name != city_name:
            region.name = city_name
            updates.append("name")
        expected_code = "510100" if city_name == "成都市" else "510700"
        if region.code != expected_code:
            region.code = expected_code
            updates.append("code")
        if updates:
            region.save(update_fields=updates + ["updated_at"])
    if school is not None and school.name != school_name:
        school.name = school_name
        school.save(update_fields=["name", "updated_at"])
    for team_id, name in team_names.items():
        Team.objects.filter(pk=team_id).update(name=name)
    for org_id, name in team_orgs.items():
        Organization.objects.filter(pk=org_id).update(name=name)


def _player_name(city_index: int, team_index: int, player_index: int) -> str:
    seed = city_index * 97 + team_index * 31 + player_index * 7
    surname = SURNAMES[seed % len(SURNAMES)]
    given = GIVEN_NAMES[(seed * 13 + player_index) % len(GIVEN_NAMES)]
    return f"{surname}{given}"


def _status_for(index: int) -> str:
    return STATUS_CYCLE[index % len(STATUS_CYCLE)]


SCHOOL_ORG_PKS = {1: "org_school_01", 2: "org_school_02"}


def _school_org(city_org: Organization, school: School) -> Organization:
    pk = SCHOOL_ORG_PKS.get(school.pk, f"org_school_{school.pk}")
    org = Organization.objects.filter(pk=pk).first()
    if org is None:
        org = Organization.objects.create(
            id=pk,
            name=school.name,
            code=school.code or f"{SCHOOL_CODE_PREFIX}{school.pk}",
            org_type="SCHOOL",
            parent=city_org,
            enabled=True,
        )
        return org
    updates = []
    for field, value in (
        ("name", school.name),
        ("code", school.code or org.code),
        ("org_type", "SCHOOL"),
        ("parent_id", city_org.pk),
        ("enabled", True),
    ):
        if getattr(org, field) != value:
            setattr(org, field, value)
            updates.append(field)
    if updates:
        org.save(update_fields=updates + ["updated_at"])
    return org


def _team_org(school_org: Organization, team: Team) -> Organization:
    pk = f"org_team_{team.team_no}"
    org = Organization.objects.filter(pk=pk).first()
    if org is None:
        return Organization.objects.create(
            id=pk,
            name=team.name,
            code=team.team_no,
            org_type="TEAM",
            parent=school_org,
            enabled=True,
        )
    updates = []
    for field, value in (
        ("name", team.name),
        ("code", team.team_no),
        ("org_type", "TEAM"),
        ("parent_id", school_org.pk),
        ("enabled", True),
    ):
        if getattr(org, field) != value:
            setattr(org, field, value)
            updates.append(field)
    if updates:
        org.save(update_fields=updates + ["updated_at"])
    return org


def seed_city_teams() -> tuple[int, int, int]:
    """创建/补齐 19 个市州的区划、学校、球队；返回 (区划, 学校, 球队) 新增数。"""
    new_regions = new_schools = new_teams = 0
    for city_index, (city_name, region_code, school_name, add_count, reuse) in enumerate(
        CITY_SPECS
    ):
        if reuse:
            _rename_existing(city_name, school_name)
            region = _region(region_code, city_name)
            # 沿用示例学校主键（1=成都、2=绵阳），只改名，不再新建。
            school = School.objects.get(pk=1 if city_name == "成都市" else 2)
        else:
            before = Region.objects.filter(code=region_code).exists()
            region = _region(region_code, city_name)
            new_regions += 0 if before else 1
            school_code = f"{SCHOOL_CODE_PREFIX}{region_code}"
            before = School.objects.filter(code=school_code).exists()
            school = _school(school_code, school_name, region)
            new_schools += 0 if before else 1
        city_org = _city_org(region_code, city_name)
        school_org = _school_org(city_org, school)

        for slot in range(add_count):
            category, gender, group_id = TEAM_SLOTS[slot % len(TEAM_SLOTS)]
            team_no = f"T2026{region_code}{slot + 1:02d}"
            team = Team.objects.filter(team_no=team_no).first()
            if team is None:
                team = Team.objects.create(
                    team_no=team_no,
                    name=f"{school_name}{gender}{category}足球队",
                    category_code=category,
                    region=region,
                    school=school,
                    status="ACTIVE",
                )
                new_teams += 1
            elif team.school_id != school.pk or team.region_id != region.pk:
                team.school = school
                team.region = region
                team.name = f"{school_name}{gender}{category}足球队"
                team.save(update_fields=["school", "region", "name", "updated_at"])
            _team_org(school_org, team)
    return new_regions, new_schools, new_teams


def seed_city_enrollments() -> tuple[int, int]:
    """按市州生成 event_01 报名单与球员名册；返回 (报名单, 球员) 新增数。"""
    new_enrollments = new_players = 0
    cache_year = "2026"
    for city_index, (city_name, region_code, school_name, add_count, reuse) in enumerate(
        CITY_SPECS
    ):
        city_org = Organization.objects.get(pk=city_org_pk(region_code))
        for slot in range(add_count):
            category, gender, group_id = TEAM_SLOTS[slot % len(TEAM_SLOTS)]
            team_no = f"T2026{region_code}{slot + 1:02d}"
            team = Team.objects.filter(team_no=team_no).first()
            if team is None:
                continue
            enrollment_id = f"enr_city_{region_code}_{slot + 1}"
            if Enrollment.objects.filter(pk=enrollment_id).exists():
                continue
            order = city_index * 4 + slot
            status = _status_for(order)
            enrollment = Enrollment.objects.create(
                id=enrollment_id,
                event_id=EVENT_ID,
                group_id=group_id,
                team_id=str(team.pk),
                subject_type="SCHOOL_TEAM",
                status=status,
                submission_no=1,
                contact_name=_player_name(city_index, slot, 0),
                contact_phone_masked=f"138****{order:04d}",
                city_org_id=city_org.pk,
                created_by="acc_school01",
            )
            new_enrollments += 1
            player_count = 16 + (order % 9)
            rows = []
            for player_index in range(player_count):
                rows.append(
                    RosterItem(
                        id=f"ros_city_{region_code}_{slot + 1}_{player_index + 1:02d}",
                        enrollment=enrollment,
                        athlete_id=(
                            f"ath_city_{cache_year}_{region_code}_"
                            f"{slot + 1}_{player_index + 1:02d}"
                        ),
                        athlete_name=_player_name(city_index, slot, player_index + 1),
                        masked_id_number=f"{region_code[:4]}**********{player_index + 1:04d}",
                        jersey_number=player_index + 1,
                        position=POSITIONS[player_index % len(POSITIONS)],
                        qualification_status="QUALIFIED",
                    )
                )
            RosterItem.objects.bulk_create(rows)
            new_players += len(rows)
    return new_enrollments, new_players


def main() -> None:
    print("== 市州报名分布演示数据 ==")
    with transaction.atomic():
        regions, schools, teams = seed_city_teams()
        enrollments, players = seed_city_enrollments()
        cache.clear()
    print(f"  + 新增区划 {regions} / 学校 {schools} / 球队 {teams}")
    print(f"  + 新增报名单 {enrollments} / 球员 {players}")
    print(f"  当前 event_01 报名单 {Enrollment.objects.filter(event_id=EVENT_ID).count()} 条，"
          f"球员 {RosterItem.objects.filter(enrollment__event_id=EVENT_ID).count()} 人")
    print(f"  市州组织 {Organization.objects.filter(org_type='CITY').count()} 个")
    print("完成。刷新「报名统计」页面即可看到各市州热力与下钻。")


if __name__ == "__main__":
    main()
