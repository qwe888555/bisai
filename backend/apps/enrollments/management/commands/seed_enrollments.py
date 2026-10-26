"""报名域演示数据种子：覆盖全状态流转，供各端联调测试。

用法：
    python manage.py seed_enrollments                # 造数（幂等，已存在则跳过）
    python manage.py seed_enrollments --clean        # 先物理清空报名数据再造
    python manage.py seed_enrollments --password X   # 新建学校端账号的密码

数据矩阵（同队不同组别，避开 ACTIVE 查重；球衣号按组别偏移避开同队查重）：
    org_team_01 × 6 组别：DRAFT / PENDING_CITY / PENDING_PROVINCE / APPROVED / REJECTED / WITHDRAWN
    org_team_02 × 1：PENDING_CITY（属 org_city_02，city 账号按数据范围看不到）
本赛事只办中小学（无高校主体），故不造 UNIVERSITY 报送数据。
"""

from __future__ import annotations

from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import Account
from apps.attachments import services as attachment_services
from apps.enrollments import schemas as s
from apps.enrollments import services

EVENT_ID = "event_01"
TEAM_01 = "org_team_01"  # 示例第一中学男子队（org_city_01）
TEAM_02 = "org_team_02"  # 示例第二中学女子队（org_city_02）

# (team_id, group_id, subject_type, 联系人, 目标状态)
# 目标状态: DRAFT / PENDING_CITY / PENDING_PROVINCE / APPROVED / REJECTED / WITHDRAWN
MATRIX = (
    (TEAM_01, "group_u12", "SCHOOL_TEAM", "张老师", "DRAFT"),
    (TEAM_01, "group_u15", "SCHOOL_TEAM", "张老师", "PENDING_CITY"),
    (TEAM_01, "group_u18", "SCHOOL_TEAM", "张老师", "PENDING_PROVINCE"),
    (TEAM_01, "group_gu12", "SCHOOL_TEAM", "张老师", "APPROVED"),
    (TEAM_01, "group_gu15", "SCHOOL_TEAM", "张老师", "REJECTED"),
    (TEAM_01, "group_u13", "SCHOOL_TEAM", "张老师", "WITHDRAWN"),
    (TEAM_02, "group_u15", "SCHOOL_TEAM", "李老师", "PENDING_CITY"),
)

PLAYERS_PER_TEAM = 8  # 1 门将 + 7 场员，满足 minPlayers=5 / minGoalkeepers=1
ROLES_SCHOOL = ["UNIVERSITY"]


class Command(BaseCommand):
    help = "创建报名域演示数据（全状态流转）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--clean", action="store_true", help="先物理清空现有报名数据")
        parser.add_argument("--password", default="Foot@2026", help="新建学校端账号密码")

    # ------------------------------------------------------------ 账号

    def _ensure_account(self, account_id: str, username: str, roles: list[str], org_id: str, password: str) -> Account:
        account, created = Account.objects.get_or_create(
            id=account_id,
            defaults={
                "username": username,
                "display_name": username,
                "roles": roles,
                "organization_id": org_id,
                "enabled": True,
            },
        )
        if created:
            account.password = make_password(password)
            account.must_change_password = False
            account.save(update_fields=["password", "must_change_password"])
        return account

    # ------------------------------------------------------------ 主流程

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        if options["clean"]:
            from apps.enrollments.models import (
                Enrollment,
                EnrollmentAttachment,
                EnrollmentSnapshot,
                ImportBatch,
                ReviewRecord,
                RosterItem,
            )

            for model in (ReviewRecord, EnrollmentAttachment, EnrollmentSnapshot, ImportBatch, RosterItem, Enrollment):
                deleted, _ = model.all_objects.all().delete()
                self.stdout.write(f"已清空 {model.__name__}: {deleted} 行")
            self.stdout.write(self.style.WARNING("历史报名数据已物理清空（含软删）"))

        password = options["password"]
        school01 = self._ensure_account("acc_school01", "school01", ROLES_SCHOOL, "org_school_01", password)
        school02 = self._ensure_account("acc_school02", "school02", ROLES_SCHOOL, "org_school_02", password)
        city = Account.objects.filter(username="city").first()
        province = Account.objects.filter(username="province").first()
        if not all((city, province)):
            self.stderr.write("缺少 seed_demo 基础账号，请先执行：python manage.py seed_demo")
            return

        rows = []
        for index, (team_id, group_id, subject_type, contact, target) in enumerate(MATRIX):
            creator = {TEAM_01: school01, TEAM_02: school02}[team_id]
            detail = self._build_one(
                creator, city, province, team_id, group_id, subject_type, contact, target,
                jersey_base=index * 20,
            )
            rows.append((team_id, group_id, subject_type, target, detail["id"], detail["version"]))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("报名演示数据创建完成："))
        self.stdout.write(f"{'球队':<14}{'组别':<14}{'主体':<14}{'状态':<20}{'ID'}")
        for team_id, group_id, subject_type, target, pk, version in rows:
            self.stdout.write(f"{team_id:<14}{group_id:<14}{subject_type:<14}{target:<20}{pk} (v{version})")
        self.stdout.write("")
        self.stdout.write("登录账号：school01/school02（学校端，密码所设值）、city（市州）、province（省级）、admin（管理）")

    def _build_one(self, creator: Account, city: Account, province: Account, team_id: str, group_id: str, subject_type: str, contact: str, target: str, jersey_base: int) -> dict:
        # 1) 创建草稿
        detail = services.create_enrollment(
            creator,
            s.EnrollmentCreateIn(
                eventId=EVENT_ID,
                groupId=group_id,
                teamId=team_id,
                subjectType=subject_type,
                contactName=contact,
                contactPhone="1380000000" + str(abs(hash(team_id + group_id)) % 10),
                remarks="演示数据",
            ),
        )
        enrollment_id = detail["id"]

        # 2) 名单（1 门将 + 场员）；球衣号按组别偏移避开同队查重
        for index in range(PLAYERS_PER_TEAM):
            services.create_roster(
                creator,
                enrollment_id,
                s.RosterItemCreateIn(
                    athleteId=f"per_seed_{team_id}_{index:02d}",
                    jerseyNumber=jersey_base + index + 1,
                    position="GK" if index == 0 else "DF",
                ),
            )

        if target == "DRAFT":
            return services.get_enrollment(creator, enrollment_id)

        # 3) 两类材料（STAMPED_FORM 缺失会 ERROR 阻断提交，必须挂）
        for category in ("STAMPED_FORM", "STUDENT_STATUS"):
            record = attachment_services.upload(
                creator,
                "ENROLLMENT",
                enrollment_id,
                category,
                SimpleUploadedFile(
                    f"{category}_{enrollment_id}.pdf",
                    b"%PDF-1.4\n% seed material\n",
                    content_type="application/pdf",
                ),
            )
            services.create_attachment(
                creator,
                enrollment_id,
                s.EnrollmentAttachmentCreateIn(materialType=category, fileId=record.pk),
            )

        # 4) 提交（冻结快照 + 状态迁移）
        detail = services.submit_enrollment(creator, enrollment_id, detail["version"])
        if target == "PENDING_CITY":
            return detail

        # 5) 市州初审
        reviewed = services.review_enrollment(
            city,
            enrollment_id,
            s.ReviewDecisionIn(
                version=detail["version"],
                decision="REJECT" if target == "REJECTED" else "APPROVE",
                reason="演示数据：市州初审驳回" if target == "REJECTED" else "",
            ),
        )
        if target == "REJECTED":
            return reviewed
        if target == "WITHDRAWN":
            return services.withdraw_enrollment(
                creator, enrollment_id, reviewed["version"], reason="演示数据：省级审核中撤回"
            )
        if target == "PENDING_PROVINCE":
            return reviewed

        # 6) 省级终审
        return services.review_enrollment(
            province,
            enrollment_id,
            s.ReviewDecisionIn(version=reviewed["version"], decision="APPROVE", reason=""),
        )
