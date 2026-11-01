"""事件层级历史值归一化：自由文本 → 标准枚举（EVENT_CATEGORIES）。

只做一次前向清洗：合法枚举值保留；已知别名映射到标准枚举；
其余未知自由文本统一归入 OTHER；空值保留为空（表示未分级）。
归一化不可逆，reverse 为空操作。
"""

from django.db import migrations

CANONICAL = {
    "PROVINCIAL_LEAGUE",
    "CITY_LEAGUE",
    "SCHOOL_LEAGUE",
    "CHAMPIONSHIP",
    "FRIENDLY",
    "OTHER",
}

ALIASES = {
    "PROVINCIAL": "PROVINCIAL_LEAGUE",
    "CITY": "CITY_LEAGUE",
    "CITY_TOURNAMENT": "CITY_LEAGUE",
    "SCHOOL": "SCHOOL_LEAGUE",
    "SCHOOL_GAMES": "SCHOOL_LEAGUE",
    "CHAMPIONSHIPS": "CHAMPIONSHIP",
    "TOURNAMENT": "CHAMPIONSHIP",
}


def _normalize(value):
    if not value:
        return ""
    key = str(value).strip().upper()
    if key in CANONICAL:
        return key
    return ALIASES.get(key, "OTHER")


def normalize_category(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    for event in Event.objects.all():
        normalized = _normalize(event.event_category)
        if normalized != event.event_category:
            event.event_category = normalized
            event.save(update_fields=["event_category", "updated_at"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0007_content_source_match_id"),
    ]

    operations = [
        migrations.RunPython(normalize_category, noop_reverse),
    ]
