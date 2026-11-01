"""种子数据：一条命令拉起可登录的演示环境。

用法：
    python manage.py seed_demo                # 默认密码 Foot@2026
    python manage.py seed_demo --password 自定义密码

生成：1 个省级组织树 + 8 类角色账号 + 常用字典项。
其他成员拉取代码后先跑这条命令，就能直接用各角色账号联调。
"""

from __future__ import annotations

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand

from apps.accounts.models import Account, DictionaryItem, Organization
from apps.common.ids import new_dictionary_id
from apps.persons.models import Region, School
from apps.teams.models import Team

REGIONS: tuple[tuple[str, str, int, str], ...] = (
    ("510000", "示例省", 1, ""),
    ("510100", "示例市州", 2, "510000"),
    ("510101", "示例市州县", 3, "510100"),
    ("510200", "示例市州二", 2, "510000"),
    ("510201", "示例市州二县", 3, "510200"),
)

SCHOOLS: tuple[tuple[str, str, str, str], ...] = (
    ("SCH001", "示例第一中学", "SENIOR", "510101"),
    ("SCH002", "示例第二中学", "SENIOR", "510201"),
)

TEAMS: tuple[tuple[str, str, str, str, str], ...] = (
    ("TEAM001", "示例第一中学男子队", "U15_MALE", "SCH001", "510101"),
    ("TEAM002", "示例第二中学女子队", "U15_FEMALE", "SCH002", "510201"),
)

ORGANIZATIONS: tuple[tuple[str, str, str, str, str], ...] = (
    ("org_prov", "", "省教育厅", "PROVINCE", "PROV"),
    ("org_city_01", "org_prov", "示例市州教育局", "CITY", "CITY01"),
    ("org_city_02", "org_prov", "示例市州二教育局", "CITY", "CITY02"),
    ("org_school_01", "org_city_01", "示例第一中学", "SCHOOL", "SCH001"),
    ("org_school_02", "org_city_02", "示例第二中学", "SCHOOL", "SCH002"),
    ("org_univ_01", "org_prov", "示例大学", "UNIVERSITY", "UNI001"),
    ("org_team_01", "org_school_01", "示例第一中学男子队", "TEAM", "TEAM001"),
    ("org_team_02", "org_school_02", "示例第二中学女子队", "TEAM", "TEAM002"),
)

ACCOUNTS: tuple[tuple[str, str, str, list[str], str], ...] = (
    ("acc_admin", "admin", "系统管理员", ["ADMIN"], "org_prov"),
    ("acc_province", "province", "省级审核员", ["PROVINCE_REVIEWER"], "org_prov"),
    ("acc_city", "city", "市州操作员", ["CITY"], "org_city_01"),
    ("acc_team", "team", "球队端账号", ["TEAM"], "org_team_01"),
    ("acc_university", "university", "高校端账号", ["UNIVERSITY"], "org_univ_01"),
    ("acc_recorder", "recorder", "赛事录入员", ["MATCH_RECORDER"], "org_city_01"),
    ("acc_athlete", "athlete", "运动员演示账号", ["ATHLETE"], "org_school_01"),
    ("acc_coach", "coach", "教练员演示账号", ["COACH"], "org_school_01"),
)

DICTIONARY_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("GENDER", "MALE", "男"),
    ("GENDER", "FEMALE", "女"),
    ("GENDER", "UNSPECIFIED", "未指定"),
    ("EDUCATION", "PRIMARY", "小学"),
    ("EDUCATION", "JUNIOR", "初中"),
    ("EDUCATION", "SENIOR", "高中"),
    ("EDUCATION", "UNIVERSITY", "大学"),
    ("COACH_CATEGORY", "A", "A 级教练"),
    ("COACH_CATEGORY", "B", "B 级教练"),
    ("COACH_CATEGORY", "C", "C 级教练"),
    ("POSITION", "GK", "守门员"),
    ("POSITION", "DF", "后卫"),
    ("POSITION", "MF", "中场"),
    ("POSITION", "FW", "前锋"),
)


class Command(BaseCommand):
    help = "初始化省/市/学校组织树、各角色演示账号与常用字典项"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--password", default="Foot@2026", help="演示账号统一初始密码"
        )
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="已存在账号也重设密码（默认保留原密码）",
        )

    def handle(self, *args, **options) -> None:
        password = options["password"]
        reset = options["reset_password"]

        for org_id, parent_id, name, org_type, code in ORGANIZATIONS:
            Organization.objects.update_or_create(
                id=org_id,
                defaults={
                    "parent_id": parent_id or None,
                    "name": name,
                    "org_type": org_type,
                    "code": code,
                    "enabled": True,
                },
            )
        self.stdout.write(f"组织：{len(ORGANIZATIONS)} 个")

        region_by_code: dict[str, Region] = {}
        for code, name, level, _parent_code in REGIONS:
            region_by_code[code], _ = Region.objects.update_or_create(
                code=code, defaults={"name": name, "level": level}
            )
        for code, _name, _level, parent_code in REGIONS:
            if parent_code:
                Region.objects.filter(code=code).update(
                    parent=region_by_code[parent_code]
                )
        self.stdout.write(f"行政区划：{len(REGIONS)} 个")

        school_by_code: dict[str, School] = {}
        for code, name, school_type, region_code in SCHOOLS:
            school_by_code[code], _ = School.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "school_type": school_type,
                    "region": region_by_code[region_code],
                },
            )
        self.stdout.write(f"学校：{len(SCHOOLS)} 所")

        for team_no, name, category_code, school_code, region_code in TEAMS:
            Team.objects.update_or_create(
                team_no=team_no,
                defaults={
                    "name": name,
                    "category_code": category_code,
                    "school": school_by_code[school_code],
                    "region": region_by_code[region_code],
                },
            )
        self.stdout.write(f"球队：{len(TEAMS)} 支（team_no 与 TEAM 组织 code 对齐）")

        for account_id, username, display_name, roles, org_id in ACCOUNTS:
            defaults = {
                "username": username,
                "display_name": display_name,
                "roles": roles,
                "organization_id": org_id,
                "enabled": True,
            }
            account, created = Account.objects.get_or_create(
                id=account_id, defaults=defaults
            )
            if created or reset:
                account.password = make_password(password)
                account.must_change_password = False
                account.save(update_fields=["password", "must_change_password"])
            if not created:
                account.username = username
                account.display_name = display_name
                account.roles = roles
                account.organization_id = org_id
                account.enabled = True
                account.save()
        self.stdout.write(f"账号：{len(ACCOUNTS)} 个，初始密码 {password}")

        created_items = 0
        for dict_type, value, label in DICTIONARY_ITEMS:
            _, created = DictionaryItem.objects.get_or_create(
                dict_type=dict_type,
                value=value,
                defaults={
                    "id": new_dictionary_id(),
                    "label": label,
                    "sort_order": 0,
                    "enabled": True,
                },
            )
            created_items += int(created)
        self.stdout.write(f"字典项：新增 {created_items} / 共 {len(DICTIONARY_ITEMS)}")

        self.stdout.write(
            self.style.SUCCESS("种子数据完成，可直接用 admin/省级/市州 等账号登录联调")
        )
