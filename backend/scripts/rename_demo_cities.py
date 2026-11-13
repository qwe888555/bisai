#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把演示库里的「示例市州」改成真实市州，让报名统计热力表正确归位。

背景：演示库的市州组织名是「示例市州教育局 / 示例市州二教育局」，热力表
无法把它们对应到 21 个内置市州，只能落在「未匹配内置市州」区域。

本脚本只改名称（市州组织、区划、学校、球队），不新增/删除任何报名数据：
- org_city_01 → 成都市教育局（区划码 510100）
- org_city_02 → 德阳市教育局（区划码 510600，原 510200 不是有效市州码）

用法（backend/ 目录下）：
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python scripts/rename_demo_cities.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from apps.accounts.models import Organization  # noqa: E402
from apps.persons.models import Region, School  # noqa: E402
from apps.teams.models import Team  # noqa: E402

# 每个市州要同步的名称；主键缺失时跳过对应项，脚本可重复执行。
CITY_RENAMES = [
    {
        "city": "成都市",
        "region_code": "510100",
        "city_org": "org_city_01",
        "school_org": "org_school_01",
        "school_code": "SCH001",
        "school_name": "四川省成都市第七中学",
        "teams": {
            "T202600000017": "四川省成都市第七中学男子足球队",
            "T202600000019": "四川省成都市第七中学女子足球队",
            "T202600000022": "四川省成都市第七中学U18足球队",
        },
        "team_orgs": {
            "org_team_01": "四川省成都市第七中学男子队",
            "org_bdb482acb0ae45d19a4c": "四川省成都市第七中学男子足球队",
            "org_dd6798a7a7cb48b696ec": "四川省成都市第七中学女子足球队",
            "org_b75cebb7511b4331a63a": "四川省成都市第七中学U18足球队",
        },
    },
    {
        "city": "德阳市",
        "region_code": "510600",
        "city_org": "org_city_02",
        "school_org": "org_school_02",
        "school_code": "SCH002",
        "school_name": "四川省德阳中学校",
        "teams": {
            "T202600000018": "四川省德阳中学校男子足球队",
            "T202600000020": "四川省德阳中学校女子足球队",
        },
        "team_orgs": {
            "org_team_02": "四川省德阳中学校女子队",
            "org_a7623b3048f842c4a1af": "四川省德阳中学校男子足球队",
            "org_bce47073458c49fd8c22": "四川省德阳中学校女子足球队",
        },
    },
]


def _set_names(model, pk: str, name: str, extra: dict | None = None) -> int:
    obj = model.objects.filter(pk=pk).first()
    if obj is None:
        return 0
    changed = []
    if obj.name != name:
        obj.name = name
        changed.append("name")
    for field, value in (extra or {}).items():
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            changed.append(field)
    if changed:
        obj.save(update_fields=changed + ["updated_at"])
        return 1
    return 0


def rename_city(spec: dict) -> int:
    """同步一个市州的城市组织、区划、学校与球队名称；返回改动条数。"""
    changed = 0
    if not Organization.objects.filter(pk=spec["city_org"]).exists():
        return changed
    changed += _set_names(
        Organization, spec["city_org"], f"{spec['city']}教育局"
    )

    school = School.objects.filter(code=spec["school_code"]).first()
    if school is not None:
        # 区划跟着学校走：改名并修正无效的市州码。
        region = Region.objects.filter(pk=school.region_id).first()
        if region is not None:
            updates = {}
            if region.name != spec["city"]:
                updates["name"] = spec["city"]
            if region.code != spec["region_code"]:
                updates["code"] = spec["region_code"]
            if updates:
                for field, value in updates.items():
                    setattr(region, field, value)
                region.save(update_fields=list(updates) + ["updated_at"])
                changed += 1
        changed += _set_names(School, school.pk, spec["school_name"])
        changed += _set_names(Organization, spec["school_org"], spec["school_name"])

    for team_no, name in spec["teams"].items():
        team = Team.objects.filter(team_no=team_no).first()
        if team is not None:
            changed += _set_names(Team, team.pk, name)
    for org_id, name in spec["team_orgs"].items():
        changed += _set_names(Organization, org_id, name)
    return changed


def main() -> None:
    with transaction.atomic():
        total = sum(rename_city(spec) for spec in CITY_RENAMES)
    cities = list(
        Organization.objects.filter(org_type="CITY").values_list("name", flat=True)
    )
    print(f"  更新 {total} 项；当前市州组织：{cities}")
    print("完成。刷新「报名统计」页面即可看到按真实市州归位。")


if __name__ == "__main__":
    main()
