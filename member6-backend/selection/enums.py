"""Group and round constants, plus tolerant parsing of Chinese/English input."""

from .common import BusinessError


class GroupType:
    HIGHER = "HIGHER"
    MIDDLE = "MIDDLE"
    PRIMARY = "PRIMARY"

    CHOICES = (
        (HIGHER, "高校组"),
        (MIDDLE, "中学组"),
        (PRIMARY, "小学组"),
    )
    LABELS = dict(CHOICES)

    _ALIASES = {}

    @classmethod
    def parse(cls, value, field_name="组别"):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise BusinessError(400, f"{field_name}不能为空")
        text = str(value).strip()
        code = cls._ALIASES.get(text) or cls._ALIASES.get(text.upper())
        if not code:
            raise BusinessError(
                400,
                f"不支持的{field_name}: {value}（可选 HIGHER/MIDDLE/PRIMARY）",
            )
        return code

    @classmethod
    def label(cls, code):
        return cls.LABELS.get(code, code)


class RoundType:
    INITIAL = "INITIAL"
    FINAL = "FINAL"

    CHOICES = (
        (INITIAL, "初选"),
        (FINAL, "复选"),
    )
    LABELS = dict(CHOICES)

    _ALIASES = {}

    @classmethod
    def parse(cls, value, field_name="轮次"):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise BusinessError(400, f"{field_name}不能为空")
        text = str(value).strip()
        code = cls._ALIASES.get(text) or cls._ALIASES.get(text.upper())
        if not code:
            raise BusinessError(
                400, f"不支持的{field_name}: {value}（可选 INITIAL/FINAL）"
            )
        return code

    @classmethod
    def label(cls, code):
        return cls.LABELS.get(code, code)


# Register Chinese labels and common aliases.  Both snake_case and uppercase
# English are accepted because the API may be called by different clients.
for _code, _label in GroupType.CHOICES:
    GroupType._ALIASES[_code] = _code
    GroupType._ALIASES[_code.lower()] = _code
    GroupType._ALIASES[_label] = _code

GroupType._ALIASES.update(
    {
        "高校": GroupType.HIGHER,
        "大学组": GroupType.HIGHER,
        "大学": GroupType.HIGHER,
        "中学": GroupType.MIDDLE,
        "初中组": GroupType.MIDDLE,
        "初中": GroupType.MIDDLE,
        "高中组": GroupType.MIDDLE,
        "高中": GroupType.MIDDLE,
        "小学": GroupType.PRIMARY,
        "小学组": GroupType.PRIMARY,
    }
)

for _code, _label in RoundType.CHOICES:
    RoundType._ALIASES[_code] = _code
    RoundType._ALIASES[_code.lower()] = _code
    RoundType._ALIASES[_label] = _code

RoundType._ALIASES.update(
    {
        "初赛": RoundType.INITIAL,
        "初轮": RoundType.INITIAL,
        "初选轮": RoundType.INITIAL,
        "复赛": RoundType.FINAL,
        "决赛": RoundType.FINAL,
        "终选": RoundType.FINAL,
        "复选轮": RoundType.FINAL,
    }
)


class PublicityType:
    """The three kinds of registration information shown on the portal."""

    ATHLETE = "ATHLETE"
    COACH = "COACH"
    TEAM = "TEAM"

    CHOICES = (
        (ATHLETE, "运动员"),
        (COACH, "教练员"),
        (TEAM, "球队"),
    )
    LABELS = dict(CHOICES)

    _ALIASES = {}

    @classmethod
    def parse(cls, value, field_name="公示类型"):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise BusinessError(400, f"{field_name}不能为空")
        text = str(value).strip()
        code = cls._ALIASES.get(text) or cls._ALIASES.get(text.upper())
        if not code:
            raise BusinessError(
                400,
                f"不支持的{field_name}: {value}"
                "（可选 ATHLETE/COACH/TEAM）",
            )
        return code

    @classmethod
    def label(cls, code):
        return cls.LABELS.get(code, code)


class PublicityStatus:
    """Publicity visibility state.

    Cancelled records are kept for audit, but every public query filters on
    PUBLISHED so the portal cannot see them immediately after cancellation.
    """

    PUBLISHED = "PUBLISHED"
    CANCELLED = "CANCELLED"

    CHOICES = (
        (PUBLISHED, "已公示"),
        (CANCELLED, "已取消"),
    )
    LABELS = dict(CHOICES)

    _ALIASES = {}

    @classmethod
    def parse(cls, value, field_name="公示状态"):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise BusinessError(400, f"{field_name}不能为空")
        text = str(value).strip()
        code = cls._ALIASES.get(text) or cls._ALIASES.get(text.upper())
        if not code:
            raise BusinessError(
                400,
                f"不支持的{field_name}: {value}"
                "（可选 PUBLISHED/CANCELLED）",
            )
        return code

    @classmethod
    def label(cls, code):
        return cls.LABELS.get(code, code)


for _code, _label in PublicityType.CHOICES:
    PublicityType._ALIASES[_code] = _code
    PublicityType._ALIASES[_code.lower()] = _code
    PublicityType._ALIASES[_label] = _code

PublicityType._ALIASES.update(
    {
        "球员": PublicityType.ATHLETE,
        "运动员信息": PublicityType.ATHLETE,
        "教练": PublicityType.COACH,
        "领队": PublicityType.COACH,
        "运动队": PublicityType.TEAM,
        "队伍": PublicityType.TEAM,
        "球队信息": PublicityType.TEAM,
    }
)

for _code, _label in PublicityStatus.CHOICES:
    PublicityStatus._ALIASES[_code] = _code
    PublicityStatus._ALIASES[_code.lower()] = _code
    PublicityStatus._ALIASES[_label] = _code

PublicityStatus._ALIASES.update(
    {
        "PUBLISH": PublicityStatus.PUBLISHED,
        "公示中": PublicityStatus.PUBLISHED,
        "CANCELED": PublicityStatus.CANCELLED,
        "CANCEL": PublicityStatus.CANCELLED,
        "取消": PublicityStatus.CANCELLED,
    }
)
