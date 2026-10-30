#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""修复报名归属链路的历史脏数据（联调报告 2026-10-30）。

三类修复（幂等，可重复执行）：
1. Team.region_id 为空 → 由学校区划沿 parent 上溯到市州（level 2）回填；
2. Enrollment.city_org_id 为空 → 按球队最新归属解析 CITY 组织回填，
   使市州端按 cityOrgId 过滤能查询/审核到该报名；
3. RosterItem.athlete_name 为十六进制样式假名（运动员0e）→
   转成数字假名（运动员0014）。

用法（backend/ 目录下）：
    DATABASE_URL="mysql+pymysql://...python scripts/repair_enrollment_ownership.py
    python scripts/repair_enrollment_ownership.py --dry-run
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from apps.enrollments.clients import _city_org_for  # noqa: E402
from apps.enrollments.models import Enrollment, RosterItem  # noqa: E402
from apps.persons.models import Region  # noqa: E402
from apps.teams.models import Team  # noqa: E402

HEX_NAME = re.compile(r"^运动员([0-9a-fA-F]{2})$")


def city_region(region: Region | None) -> Region | None:
    hops = 0
    while region is not None and region.level != 2 and region.parent_id and hops < 6:
        region = region.parent
        hops += 1
    return region if region is not None and region.level == 2 else None


def repair(dry_run: bool) -> None:
    stats = {"team_region": 0, "enrollment_city": 0, "roster_name": 0}

    teams = Team.objects.filter(is_deleted=False, region__isnull=True).select_related(
        "school", "school__region"
    )
    for team in teams.iterator():
        region = city_region(team.school.region if team.school and team.school.region_id else None)
        if region is None:
            print(f"  跳过 team {team.pk}（学校无区划）")
            continue
        stats["team_region"] += 1
        print(f"  team {team.pk} region <- {region.code} {region.name}")
        if not dry_run:
            Team.objects.filter(pk=team.pk).update(region=region)

    for enrollment in Enrollment.objects.filter(
        city_org_id="", deleted_at__isnull=True
    ).iterator():
        team = Team.objects.filter(pk=enrollment.team_id).first()
        if team is None:
            continue
        city_org_id, city_name = _city_org_for(team)
        if not city_org_id:
            print(f"  跳过 enrollment {enrollment.pk}（球队无市州归属）")
            continue
        stats["enrollment_city"] += 1
        print(
            f"  enrollment {enrollment.pk} city_org_id <- {city_org_id}（{city_name}）"
        )
        if not dry_run:
            Enrollment.objects.filter(pk=enrollment.pk).update(
                city_org_id=city_org_id
            )

    for item in RosterItem.objects.filter(deleted_at__isnull=True).iterator():
        match = HEX_NAME.match(item.athlete_name or "")
        if not match:
            continue
        new_name = f"运动员{int(match.group(1), 16):04d}"
        stats["roster_name"] += 1
        print(f"  roster {item.pk} name {item.athlete_name} -> {new_name}")
        if not dry_run:
            RosterItem.objects.filter(pk=item.pk).update(athlete_name=new_name)

    verb = "待修复" if dry_run else "已修复"
    print(
        f"{verb}：球队 region {stats['team_region']}，"
        f"报名单 city_org_id {stats['enrollment_city']}，"
        f"名单假名 {stats['roster_name']}"
    )


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    with transaction.atomic():
        repair(dry)
        if dry:
            raise SystemExit(0)
