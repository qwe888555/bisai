# 2026-11-15 组别口径统一：小学组/初中组/高中组（PRIMARY/MIDDLE/HIGH）。

import re

from django.db import migrations


def normalize_team_category(apps, schema_editor):
    """把球队的历史组别编码收敛到 PRIMARY/MIDDLE/HIGH。

    历史数据里同时存在 U12/U15/U18 和 MIDDLE 等写法；名称能明确判断学段
    时以名称为准，否则按编码映射。无法判断的保持原值，避免误改。
    """
    Team = apps.get_model("teams", "Team")
    for team in Team.objects.all().only("pk", "name", "category_code"):
        name = (team.name or "").upper()
        code = (team.category_code or "").upper()
        target = None
        if "高中" in name or re.search(r"U(18|19|20)", name):
            target = "HIGH"
        elif "小学" in name or "U12" in name:
            target = "PRIMARY"
        elif "初中" in name or re.search(r"U(13|14|15)", name):
            target = "MIDDLE"
        elif code in ("PRIMARY", "MIDDLE", "HIGH"):
            target = code
        elif "U12" in code:
            target = "PRIMARY"
        elif re.search(r"U(13|14|15)", code):
            target = "MIDDLE"
        elif re.search(r"U(18|19|20)", code):
            target = "HIGH"
        if target and team.category_code != target:
            team.category_code = target
            team.save(update_fields=["category_code"])


class Migration(migrations.Migration):

    dependencies = [
        ("teams", "0003_merge_20261101_0132"),
    ]

    operations = [
        migrations.RunPython(
            normalize_team_category, migrations.RunPython.noop
        ),
    ]
