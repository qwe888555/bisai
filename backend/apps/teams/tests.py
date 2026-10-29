"""球队档案 HTTP 层测试（T4-03 批次 A：GET /teams、GET /teams/{id}、
POST /teams、PUT /teams/{id}；DELETE 暂不开放——关联检查依赖报名/比赛域）。

一律 SQLite 内存库；覆盖：认证/权限/数据范围隔离、筛选分页软删排除、
404、创建必填与映射阻塞、team_no 编号规则与唯一性、媒体字段映射、
主教练合成、无承载字段不持久化不回显、PUT 乐观链（缺失/过期/越权）、
审计留痕与事务回滚。
"""

from __future__ import annotations

import json
from datetime import date
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account, AuditLog, Organization
from apps.common import crypto, enums
from apps.common.authentication import issue_access_token, make_password
from apps.common.ids import new_account_id, new_organization_id
from apps.persons.models import AthleteProfile, Person, Region, School
from apps.teams.models import Team, TeamCoach, TeamMember, TeamTransfer

BASE = "/api/v1/teams"


def _token(account):
    return issue_access_token(account)[0]


class TeamApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # ---- 账号 ----
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="teams-admin",
            display_name="球队管理员",
            roles=["ADMIN"],
            password=make_password("admin-pass-123"),
            enabled=True,
        )
        cls.uni = Account.objects.create(
            id=new_account_id(),
            username="teams-uni",
            display_name="学校端",
            roles=["UNIVERSITY"],
            password=make_password("uni-pass-123"),
            enabled=True,
        )
        cls.athlete = Account.objects.create(
            id=new_account_id(),
            username="teams-athlete",
            display_name="运动员",
            roles=["ATHLETE"],
            password=make_password("athlete-pass-123"),
            enabled=True,
        )
        cls.admin_token = _token(cls.admin)
        cls.uni_token = _token(cls.uni)
        cls.athlete_token = _token(cls.athlete)

        # ---- 基础数据 ----
        province = Region.objects.create(name="甲省", code="800000", level=1)
        cls.city = Region.objects.create(
            name="甲州市", code="800100", level=2, parent=province
        )
        cls.school = School.objects.create(
            name="甲一小", code="SCH-800100-001", school_type="PRIMARY", region=cls.city
        )
        # ---- 组织树（A′-min 映射夹具：org.code = School.code）----
        cls.org_prov = Organization.objects.create(
            id=new_organization_id(), name="甲省组织", org_type="PROVINCE", code="ORG-PROV-TF"
        )
        cls.org_city = Organization.objects.create(
            id=new_organization_id(),
            name="甲市组织",
            org_type="CITY",
            code="ORG-CITY-TF",
            parent=cls.org_prov,
        )
        cls.org_school = Organization.objects.create(
            id=new_organization_id(),
            name="甲一小组织",
            org_type="SCHOOL",
            code="SCH-800100-001",
            parent=cls.org_city,
        )
        # 学校类型但无 School 主档承接的孤立节点（反查主档失败用例）
        cls.org_school_lone = Organization.objects.create(
            id=new_organization_id(),
            name="无主档学校组织",
            org_type="SCHOOL",
            code="SCH-NO-MASTER",
            parent=cls.org_city,
        )
        # 已停用学校组织节点
        cls.org_school_disabled = Organization.objects.create(
            id=new_organization_id(),
            name="停用学校组织",
            org_type="SCHOOL",
            code="SCH-DISABLED-01",
            parent=cls.org_city,
            enabled=False,
        )
        # 类型不符节点（TEAM 不能作为 schoolOrgId）
        cls.org_team = Organization.objects.create(
            id=new_organization_id(),
            name="别队组织",
            org_type="TEAM",
            code="T209901010001",
            parent=cls.org_school,
        )

        # ---- 人员（主教练合成来源）----
        cls.coach_person = Person.objects.create(
            person_no="P202600000011",
            full_name="李教练",
            id_card_cipher=crypto.encrypt_text("110101198001015678"),
            id_card_hash=crypto.lookup_hash("110101198001015678"),
            id_card_masked=crypto.mask_id_number("110101198001015678"),
            person_category=enums.PersonCategory.COACH,
            audit_status=enums.AuditStatus.APPROVED,
        )
        cls.athlete_person = Person.objects.create(
            person_no="P202600000012",
            full_name="张小明",
            id_card_cipher=crypto.encrypt_text("110101201305011234"),
            id_card_hash=crypto.lookup_hash("110101201305011234"),
            id_card_masked=crypto.mask_id_number("110101201305011234"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.APPROVED,
            school=cls.school,
        )

        # ---- 球队 ----
        cls.team_main = Team.objects.create(
            team_no="T202600000001",
            name="甲一小男足一队",
            category_code="PRIMARY",
            school=cls.school,
            region=cls.city,
            introduction="校主力队",
            flag_file_id="file-flag-1",
            logo_file_id="file-logo-1",
            group_photo_file_id="file-photo-1",
        )
        TeamMember.objects.create(
            team=cls.team_main, person=cls.athlete_person, joined_at=date(2026, 3, 1)
        )
        TeamCoach.objects.create(
            team=cls.team_main,
            person=cls.coach_person,
            duty=enums.CoachDuty.HEAD_COACH,
            joined_at=date(2026, 3, 1),
        )
        cls.team_plain = Team.objects.create(
            team_no="T202600000002",
            name="乙校女足队",
            category_code="MIDDLE",
        )
        cls.team_soft = Team.objects.create(
            team_no="T202600000009",
            name="已删队",
            category_code="PRIMARY",
            school=cls.school,
            is_deleted=True,
            deleted_at=timezone.now(),
        )

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _get(self, path, token=None, query=None):
        return self.client.get(path, data=query or {}, **self._auth(token))

    def _post(self, path, payload, token=None):
        return self.client.post(
            path, data=json.dumps(payload), content_type="application/json", **self._auth(token)
        )

    def _put(self, path, payload, token=None):
        return self.client.put(
            path, data=json.dumps(payload), content_type="application/json", **self._auth(token)
        )

    @staticmethod
    def _full_update_payload(team, version):
        """模拟前端 save() 真实提交：表单全字段原样回传 + version（L205-213）。"""
        return {
            "name": team.name,
            "shortName": "甲一队",
            "schoolName": "甲一小",
            "cityName": "甲州市",
            "campusName": "本部",
            "gender": "MALE",
            "coachName": "被忽略的名字",
            "intro": "更新后的简介",
            "schoolOrgId": "42",
            "groupCategory": "PRIMARY",
            "version": version,
        }

    # ------------------------------------------------------------ 认证与权限

    def test_list_requires_auth(self):
        resp = self._get(BASE)
        self.assertEqual(resp.status_code, 401)

    def test_get_requires_auth(self):
        resp = self._get(f"{BASE}/{self.team_main.pk}")
        self.assertEqual(resp.status_code, 401)

    def test_create_requires_auth(self):
        resp = self._post(BASE, {"name": "x", "groupCategory": "PRIMARY"})
        self.assertEqual(resp.status_code, 401)

    def test_update_requires_auth(self):
        resp = self._put(f"{BASE}/{self.team_main.pk}", {"version": 1})
        self.assertEqual(resp.status_code, 401)

    def test_list_forbidden_for_athlete(self):
        """ATHLETE 未登记 teamList 权限点 → 403 FORBIDDEN（角色拒绝先于范围）。"""
        resp = self._get(BASE, token=self.athlete_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_get_forbidden_for_university_without_org(self):
        """UNIVERSITY 无组织绑定（空范围）→ DATA_SCOPE_DENIED（A′ 第 2 批：
        有效范围账号见 TeamDataScopeTests 的放行/越界用例）。"""
        resp = self._get(f"{BASE}/{self.team_main.pk}", token=self.uni_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    def test_create_forbidden_for_athlete(self):
        resp = self._post(
            BASE, {"name": "x", "groupCategory": "PRIMARY"}, token=self.athlete_token
        )
        self.assertEqual(resp.status_code, 403)

    # ------------------------------------------------------------ 列表

    def test_list_admin_paginated_filtered_and_soft_deleted_excluded(self):
        resp = self._get(
            BASE,
            token=self.admin_token,
            query={"page": 1, "pageSize": 10, "keyword": "甲一小"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        names = [item["name"] for item in body["items"]]
        self.assertIn("甲一小男足一队", names)
        self.assertNotIn("乙校女足队", names)  # keyword 不命中
        self.assertNotIn("已删队", names)  # 软删排除
        self.assertEqual(body["total"], 1)

    def test_list_filter_by_category(self):
        resp = self._get(
            BASE, token=self.admin_token, query={"category": "MIDDLE"}
        )
        names = [item["name"] for item in resp.json()["data"]["items"]]
        self.assertEqual(names, ["乙校女足队"])

    def test_list_university_scope_denied(self):
        resp = self._get(BASE, token=self.uni_token)
        self.assertEqual(resp.status_code, 403)

    # ------------------------------------------------------------ 详情

    def test_get_detail_with_synthesized_fields(self):
        resp = self._get(f"{BASE}/{self.team_main.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["teamNo"], "T202600000001")
        self.assertEqual(data["coachName"], "李教练")  # 现役主教练合成
        self.assertEqual(data["memberCount"], 1)
        self.assertEqual(data["schoolName"], "甲一小")
        self.assertEqual(data["cityName"], "甲州市")
        self.assertEqual(data["category"], "PRIMARY")
        self.assertEqual(data["intro"], "校主力队")
        self.assertEqual(data["status"], "APPROVED")
        self.assertIsInstance(data["version"], int)
        # 无承载/无契约键不输出（联调待办：媒体受控 url/orgId）
        for absent in ("orgId", "cityOrgId", "gender", "campusName", "shortName", "flagUrl"):
            self.assertNotIn(absent, data)

    def test_get_plain_team_null_coach_and_zero_members(self):
        resp = self._get(f"{BASE}/{self.team_plain.pk}", token=self.admin_token)
        data = resp.json()["data"]
        self.assertIsNone(data["coachName"])
        self.assertEqual(data["memberCount"], 0)
        self.assertIsNone(data["schoolName"])

    def test_get_not_found(self):
        resp = self._get(f"{BASE}/99999", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_get_soft_deleted_returns_404(self):
        resp = self._get(f"{BASE}/{self.team_soft.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------ 创建

    def test_create_missing_required_fields_400(self):
        resp = self._post(BASE, {"name": "无组别队"}, token=self.admin_token)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Team.objects.count(), 3)

    def test_create_rejects_unknown_fields(self):
        resp = self._post(
            BASE,
            {"name": "x", "groupCategory": "PRIMARY", "teamNo": "T999"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_invalid_school_org_returns_400(self):
        """A′-min（2026-10-25 交付）：schoolOrgId 须为真实组织节点 ID；
        不存在的节点 → 400 INVALID_ARGUMENT + 字段明细，不落库。"""
        resp = self._post(
            BASE,
            {"name": "新队", "groupCategory": "PRIMARY", "schoolOrgId": "42"},
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        # field_errors 为列表信封：data = [{"field", "reason"}]
        self.assertEqual(resp.json()["data"][0]["field"], "schoolOrgId")
        self.assertEqual(Team.objects.count(), 3)

    def test_create_admin_success_creates_team_org_node(self):
        """创建成功 → 同事务建立 TEAM 组织节点：code=team_no、name=队名、
        parent=所属学校组织节点（A′-min 契约）。"""
        resp = self._post(
            BASE,
            {
                "name": "映射新队",
                "groupCategory": "PRIMARY",
                "schoolOrgId": str(self.org_school.pk),
                "intro": "映射链路用",
            },
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        team = Team.objects.filter(team_no=data["teamNo"]).first()
        self.assertIsNotNone(team)
        self.assertEqual(team.school_id, self.school.pk)
        org = Organization.objects.filter(code=data["teamNo"]).first()
        self.assertIsNotNone(org)
        self.assertEqual(org.org_type, "TEAM")
        self.assertEqual(org.name, "映射新队")
        self.assertEqual(org.parent_id, self.org_school.pk)
        self.assertTrue(org.enabled)
        self.assertEqual(Organization.objects.filter(org_type="TEAM").count(), 2)  # 夹具1+新建1

    def test_create_school_org_disabled_returns_400(self):
        """schoolOrgId 节点已停用 → 400 INVALID_ARGUMENT，不落库。"""
        resp = self._post(
            BASE,
            {"name": "新队", "groupCategory": "PRIMARY", "schoolOrgId": str(self.org_school_disabled.pk)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertEqual(Team.objects.count(), 3)

    def test_create_school_org_wrong_type_returns_400(self):
        """schoolOrgId 为 TEAM 类型节点 → 400 类型不符，不落库。"""
        resp = self._post(
            BASE,
            {"name": "新队", "groupCategory": "PRIMARY", "schoolOrgId": str(self.org_team.pk)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertEqual(Team.objects.count(), 3)

    def test_create_school_org_without_school_master_returns_400(self):
        """组织节点类型合法但 org.code 无 School 主档承接 → 400，不落库。"""
        resp = self._post(
            BASE,
            {"name": "新队", "groupCategory": "PRIMARY", "schoolOrgId": str(self.org_school_lone.pk)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertEqual(Team.objects.count(), 3)

    def test_create_rollback_leaves_no_orphan_team_or_org(self):
        """TEAM 组织节点建立失败（父层级校验等）→ 同事务球队行一并回滚，
        无孤立球队/孤立组织节点（A′-min 原子性契约）。"""
        from apps.common.errors import STATE_CONFLICT, ApiError

        with mock.patch(
            "apps.accounts.services.ensure_business_org",
            side_effect=ApiError(STATE_CONFLICT, "模拟组织同步失败"),
        ):
            resp = self._post(
                BASE,
                {
                    "name": "回滚新队",
                    "groupCategory": "PRIMARY",
                    "schoolOrgId": str(self.org_school.pk),
                },
                token=self.admin_token,
            )
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(Team.objects.filter(name="回滚新队").exists())
        self.assertEqual(
            Organization.objects.filter(org_type="TEAM").count(), 1
        )  # 仅夹具节点，无新节点

    def test_create_service_level_full_rules(self):
        """服务层直测（A′-min 映射交付后的规则锚定）：编号回填/媒体映射/默认状态/无承载字段。"""
        team = None
        from apps.teams import services

        team = services.create_team(
            {
                "name": "丙校混合队",
                "groupCategory": "PRIMARY",
                "gender": "MIXED",  # 无承载：不落库
                "campusName": "东校区",  # 无承载：不落库
                "intro": "服务层直建",
                "flagFileId": "f-flag",
                "emblemFileId": "f-logo",
                "groupPhotoFileId": "f-photo",
            },
            org=self.org_school,
            school=self.school,
            account=self.admin,
        )
        year = team.created_at.year
        self.assertEqual(team.team_no, f"T{year}{team.pk:08d}")  # T+年份+8位id
        self.assertEqual(len(team.team_no), 13)
        self.assertEqual(team.category_code, "PRIMARY")
        self.assertEqual(team.flag_file_id, "f-flag")
        self.assertEqual(team.logo_file_id, "f-logo")  # emblemFileId→logo_file_id
        self.assertEqual(team.group_photo_file_id, "f-photo")
        self.assertEqual(team.audit_status, enums.AuditStatus.APPROVED)  # Q2 默认
        self.assertEqual(team.school_id, self.school.pk)

    def test_create_team_no_unique_across_rows(self):
        from apps.teams import services

        first = services.create_team(
            {"name": "队A", "groupCategory": "PRIMARY"},
            org=self.org_school,
            school=self.school,
            account=self.admin,
        )
        second = services.create_team(
            {"name": "队B", "groupCategory": "PRIMARY"},
            org=self.org_school,
            school=self.school,
            account=self.admin,
        )
        self.assertNotEqual(first.team_no, second.team_no)
        self.assertEqual(Team.objects.filter(team_no=first.team_no).count(), 1)

    def test_create_rolls_back_when_team_no_fill_fails(self):
        """事务边界：创建成功但 team_no 回填失败 → 整体回滚，无残留行。

        注：audit_state 在服务事务之外调用（persons 同款先例），审计失败
        不构成回滚场景；真实回滚窗口=create_team 的 atomic 内两段 save。
        """
        from apps.teams import services

        real_save = Team.save
        calls = {"n": 0}

        def flaky_save(instance, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:  # 第2次=team_no 回填 save
                raise RuntimeError("db down during team_no fill")
            return real_save(instance, *args, **kwargs)

        with mock.patch.object(Team, "save", flaky_save):
            with self.assertRaises(RuntimeError):
                services.create_team(
                    {"name": "回滚队", "groupCategory": "PRIMARY"},
                    org=self.org_school,
                    school=self.school,
                    account=self.admin,
                )
        self.assertFalse(Team.objects.filter(name="回滚队").exists())

    # ------------------------------------------------------------ 更新

    def _current_version(self, team):
        return int(team.updated_at.timestamp() * 1_000_000)

    def test_update_success_with_real_frontend_payload(self):
        """前端真实全字段 payload（含未变化回传字段）→ 正常编辑不失败；
        可变字段生效，groupCategory/coachName 等被忽略。"""
        version = self._current_version(self.team_main)
        payload = self._full_update_payload(self.team_main, version)
        payload["flagFileId"] = "file-flag-new"
        resp = self._put(
            f"{BASE}/{self.team_main.pk}", payload, token=self.admin_token
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["name"], self.team_main.name)  # 未改 name 保留
        self.assertEqual(data["intro"], "更新后的简介")
        self.assertEqual(data["coachName"], "李教练")  # 忽略传入值，仍为合成
        self.assertEqual(data["category"], "PRIMARY")  # groupCategory 忽略
        self.assertNotIn("campusName", data)  # 无承载不回显
        self.assertGreater(data["version"], version)  # version 前进
        self.team_main.refresh_from_db()
        self.assertEqual(self.team_main.flag_file_id, "file-flag-new")

    def test_update_missing_version_400(self):
        resp = self._put(
            f"{BASE}/{self.team_main.pk}",
            {"name": "新名"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)

    def test_update_stale_version_409(self):
        stale = self._current_version(self.team_main) - 1
        resp = self._put(
            f"{BASE}/{self.team_main.pk}",
            {"name": "并发改名", "version": stale},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        self.team_main.refresh_from_db()
        self.assertEqual(self.team_main.name, "甲一小男足一队")  # 状态不变

    def test_update_forbidden_for_university(self):
        resp = self._put(
            f"{BASE}/{self.team_main.pk}",
            {"name": "越权改名", "version": self._current_version(self.team_main)},
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.team_main.refresh_from_db()
        self.assertEqual(self.team_main.name, "甲一小男足一队")

    def test_update_soft_deleted_returns_404(self):
        resp = self._put(
            f"{BASE}/{self.team_soft.pk}",
            {"name": "x", "version": self._current_version(self.team_soft)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------ 审计

    def test_audit_log_written_for_create_and_update(self):
        from apps.teams import services

        team = services.create_team(
            {"name": "审计队", "groupCategory": "PRIMARY"},
            org=self.org_school,
            school=self.school,
            account=self.admin,
        )
        self._post(
            BASE, {"name": "审计队2", "groupCategory": "PRIMARY"}, token=self.admin_token
        )  # 缺 schoolOrgId → 400，不建队；审计断言针对服务层 create+API update 组合
        resp = self._put(
            f"{BASE}/{team.pk}",
            {"intro": "改简介", "version": self._current_version(team)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            AuditLog.objects.filter(
                action="team.update",
                object_type="Team",
                object_id=str(team.pk),
                operator_account_id=self.admin.pk,
            ).exists()
        )


def _athlete(seq: int, name: str, **kw) -> Person:
    """测试运动员（含 AthleteProfile 行——方案C 的 ATHLETE 事实源）。"""
    idnum = f"110101201301{seq:04d}X" if seq % 2 else f"110101201302{seq:04d}X"
    person = Person.objects.create(
        person_no=f"P2026{seq:08d}",
        full_name=name,
        id_card_cipher=crypto.encrypt_text(idnum),
        id_card_hash=crypto.lookup_hash(idnum),
        id_card_masked=crypto.mask_id_number(idnum),
        person_category=enums.PersonCategory.ATHLETE,
        audit_status=enums.AuditStatus.APPROVED,
        **kw,
    )
    AthleteProfile.objects.create(person=person, grade_level="")
    return person


class TeamMemberApiTests(TestCase):
    """批次 B：GET/POST /teams/{id}/members(+ /search)、PUT/DELETE members/{mid}。

    状态严格 CURRENT/EXITED（不虚构审核状态）；R2 现役唯一/R3 队内现役球衣号
    唯一（服务层）；PENDING 转出单阻断移出；position 透传（字典值域缺口在案）。
    """

    @classmethod
    def setUpTestData(cls):
        # ---- 账号 ----
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="members-admin",
            display_name="成员管理员",
            roles=["ADMIN"],
            password=make_password("admin-pass-123"),
            enabled=True,
        )
        cls.uni = Account.objects.create(
            id=new_account_id(),
            username="members-uni",
            display_name="学校端",
            roles=["UNIVERSITY"],
            password=make_password("uni-pass-123"),
            enabled=True,
        )
        cls.athlete = Account.objects.create(
            id=new_account_id(),
            username="members-athlete",
            display_name="运动员",
            roles=["ATHLETE"],
            password=make_password("athlete-pass-123"),
            enabled=True,
        )
        cls.admin_token = _token(cls.admin)
        cls.uni_token = _token(cls.uni)
        cls.athlete_token = _token(cls.athlete)

        # ---- 基础数据 ----
        province = Region.objects.create(name="乙省", code="900000", level=1)
        cls.city = Region.objects.create(
            name="乙州市", code="900100", level=2, parent=province
        )
        cls.school = School.objects.create(
            name="乙一小", school_type="PRIMARY", region=cls.city
        )

        # ---- 人员 ----
        cls.ath_a = _athlete(101, "张小明")  # 现役成员
        cls.ath_b = _athlete(102, "李小虎")  # 自由球员
        cls.ath_c = _athlete(103, "王小磊")  # 已离队（历史）
        cls.ath_d = _athlete(104, "赵小鹏")  # 现役 + PENDING 转出单
        cls.ath_e = _athlete(105, "孙小冬")  # 中文 position 透传用
        cls.ath_del = _athlete(106, "钱小失", is_deleted=True)  # 软删人员
        cls.coach_only = Person.objects.create(
            person_no="P202600000107",
            full_name="周教练",
            id_card_cipher=crypto.encrypt_text("110101198501015678"),
            id_card_hash=crypto.lookup_hash("110101198501015678"),
            id_card_masked=crypto.mask_id_number("110101198501015678"),
            person_category=enums.PersonCategory.COACH,
            audit_status=enums.AuditStatus.APPROVED,
        )  # 无 AthleteProfile 行 → 非 ATHLETE
        cls.ath_plain = Person.objects.create(
            person_no="P202600000108",
            full_name="陈小无",
            id_card_cipher=crypto.encrypt_text("110101201303011234"),
            id_card_hash=crypto.lookup_hash("110101201303011234"),
            id_card_masked=crypto.mask_id_number("110101201303011234"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.APPROVED,
        )  # person_category=ATHLETE 但无档案行 → 按方案C 不算 ATHLETE

        # ---- 球队与成员关系 ----
        cls.team_main = Team.objects.create(
            team_no="T202600000011", name="乙一小男足一队", category_code="PRIMARY",
            school=cls.school, region=cls.city,
        )
        cls.team_plain = Team.objects.create(
            team_no="T202600000012", name="乙校女足队", category_code="MIDDLE"
        )
        cls.team_soft = Team.objects.create(
            team_no="T202600000019", name="已删乙队", category_code="PRIMARY",
            is_deleted=True, deleted_at=timezone.now(),
        )
        cls.member_a = TeamMember.objects.create(
            team=cls.team_main, person=cls.ath_a,
            jersey_number=7, position_code="GK", joined_at=date(2026, 3, 1),
        )
        cls.member_c = TeamMember.objects.create(
            team=cls.team_main, person=cls.ath_c, joined_at=date(2026, 1, 10),
            status=enums.RelationStatus.EXITED,
            left_at=timezone.now(), left_reason="试训结束",
        )
        cls.member_d = TeamMember.objects.create(
            team=cls.team_main, person=cls.ath_d, jersey_number=9,
            joined_at=date(2026, 3, 2),
        )
        cls.member_del = TeamMember.objects.create(
            team=cls.team_main, person=cls.ath_del, joined_at=date(2026, 3, 3),
        )

        # ---- 转队单 ----
        cls.transfer_pending = TeamTransfer.objects.create(
            person=cls.ath_d, from_team=cls.team_main, to_team=cls.team_plain,
            transfer_date=date(2026, 10, 30), apply_reason="转队申请待核验",
        )  # 默认 PENDING
        transfer_done = TeamTransfer.objects.create(
            person=cls.ath_e, from_team=cls.team_plain, to_team=cls.team_main,
            transfer_date=date(2026, 2, 1), apply_reason="转入",
            status=enums.TransferStatus.COMPLETED,
        )
        cls.member_trans = TeamMember.objects.create(
            team=cls.team_main, person=cls.ath_e, joined_at=date(2026, 2, 1),
            source=enums.RelationSource.TRANSFER, transfer=transfer_done,
        )

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _get(self, path, token=None, query=None):
        return self.client.get(path, data=query or {}, **self._auth(token))

    def _post(self, path, payload, token=None):
        return self.client.post(
            path, data=json.dumps(payload), content_type="application/json", **self._auth(token)
        )

    def _put(self, path, payload, token=None):
        return self.client.put(
            path, data=json.dumps(payload), content_type="application/json", **self._auth(token)
        )

    @staticmethod
    def _mver(member) -> int:
        return int(member.updated_at.timestamp() * 1_000_000)

    def _mpath(self, team_id) -> str:
        return f"{BASE}/{team_id}/members"

    def _delete(self, path, token=None, version=None):
        url = f"{path}?version={version}" if version is not None else path
        return self.client.delete(url, **self._auth(token))

    # ------------------------------------------------------------ 认证与权限

    def test_member_list_requires_auth(self):
        resp = self._get(self._mpath(self.team_main.pk))
        self.assertEqual(resp.status_code, 401)

    def test_member_add_requires_auth(self):
        resp = self._post(
            self._mpath(self.team_main.pk),
            {"athleteId": str(self.ath_b.pk), "jerseyNumber": 10},
        )
        self.assertEqual(resp.status_code, 401)

    def test_member_delete_requires_auth(self):
        resp = self._delete(f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}")
        self.assertEqual(resp.status_code, 401)

    def test_member_list_forbidden_for_athlete_role(self):
        """ATHLETE 未登记 memberList → 403 FORBIDDEN（角色拒绝先于范围）。"""
        resp = self._get(self._mpath(self.team_main.pk), token=self.athlete_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_member_scope_denied_for_university(self):
        """UNIVERSITY 无组织绑定（空范围）→ DATA_SCOPE_DENIED（A′ 第 2 批；
        有范围账号行为见 TeamDataScopeTests）。"""
        for resp in (
            self._get(self._mpath(self.team_main.pk), token=self.uni_token),
            self._post(
                self._mpath(self.team_main.pk),
                {"athleteId": str(self.ath_b.pk)},
                token=self.uni_token,
            ),
            self.client.post(
                f"{self._mpath(self.team_main.pk)}/search",
                data=json.dumps({"keyword": "小"}),
                content_type="application/json",
                **self._auth(self.uni_token),
            ),
        ):
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    def test_member_update_forbidden_for_university(self):
        """批次 B 验收保留限制③：UNIVERSITY 独立调用 PUT members → 403。

        本夹具 uni 无组织绑定（空范围）→ services.member_update 入口
        _require_admin_scope 拒绝（A′ 第 2 批，先于版本校验与写操作）；
        合法 version 正常提交以排除"缺/错版本"干扰，断言关系行零变更。
        """
        version = self._mver(self.member_a)
        resp = self._put(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}",
            {"jerseyNumber": 8, "version": version},
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.jersey_number, 7)  # 未被拒绝请求改动
        self.assertEqual(self.member_a.status, enums.RelationStatus.CURRENT)
        self.assertEqual(self._mver(self.member_a), version)  # 版本未漂移

    def test_member_delete_forbidden_for_university(self):
        """批次 B 验收保留限制③：UNIVERSITY 独立调用 DELETE members → 403。

        version 正常提交（排除"缺 version 400"路径）；范围拒绝先于软退出，
        不执行 CURRENT→EXITED（status/left_at/version 均不变）。
        """
        version = self._mver(self.member_a)
        resp = self._delete(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}",
            token=self.uni_token,
            version=version,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.status, enums.RelationStatus.CURRENT)
        self.assertIsNone(self.member_a.left_at)
        self.assertEqual(self._mver(self.member_a), version)  # 版本未漂移

    # ------------------------------------------------------------ 成员列表

    def test_member_list_fields_and_status(self):
        resp = self._get(
            self._mpath(self.team_main.pk), token=self.admin_token,
            query={"page": 1, "pageSize": 10},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        # 软删人员的关系行不出现在列表（member_del 排除）
        self.assertEqual(body["total"], 4)  # a/d/e 现役 + c 离队
        by_name = {item["athleteName"]: item for item in body["items"]}
        row = by_name["张小明"]
        self.assertEqual(row["athleteId"], str(self.ath_a.pk))
        self.assertEqual(row["teamId"], str(self.team_main.pk))
        self.assertEqual(row["maskedIdNumber"], self.ath_a.id_card_masked)
        self.assertEqual(row["jerseyNumber"], 7)
        self.assertEqual(row["position"], "GK")
        self.assertEqual(row["status"], "CURRENT")  # 真实冻结枚举，不虚构审核态
        self.assertTrue(row["active"])
        self.assertEqual(row["effectiveFrom"], "2026-03-01")
        self.assertIsInstance(row["version"], int)
        exited = by_name["王小磊"]
        self.assertEqual(exited["status"], "EXITED")
        self.assertFalse(exited["active"])

    def test_member_list_keyword_filter(self):
        resp = self._get(
            self._mpath(self.team_main.pk), token=self.admin_token, query={"keyword": "小鹏"}
        )
        names = [item["athleteName"] for item in resp.json()["data"]["items"]]
        self.assertEqual(names, ["赵小鹏"])  # 队内成员命中；自由球员/软删人员不出现

    def test_member_list_missing_team_404(self):
        resp = self._get(f"{BASE}/99999/members", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)

    def test_member_list_soft_deleted_team_404(self):
        resp = self._get(self._mpath(self.team_soft.pk), token=self.admin_token)
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------ 成员详情（批次 D-2）

    def test_member_get_requires_auth(self):
        resp = self._get(f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}")
        self.assertEqual(resp.status_code, 401)

    def test_member_get_permission_registered(self):
        from apps.common.permissions import operation_roles

        # 文档角色表含 CITY，与 memberList 登记口径对齐（不含 CITY）；
        # 数据范围经 A′ 第 2 批逐对象校验，本夹具无 CITY 账号（批次 D-2 验收记录）
        self.assertEqual(
            operation_roles("memberGet"),
            ("ADMIN", "UNIVERSITY", "TEAM", "PROVINCE_REVIEWER"),
        )

    def test_member_get_forbidden_for_athlete_role(self):
        """ATHLETE 未登记 memberGet → 403 FORBIDDEN（角色拒绝先于范围）。"""
        resp = self._get(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}",
            token=self.athlete_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_member_get_scope_denied_for_university(self):
        resp = self._get(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}",
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    def test_member_get_success_fields(self):
        resp = self._get(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}",
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["id"], str(self.member_a.pk))
        self.assertEqual(data["teamId"], str(self.team_main.pk))
        self.assertEqual(data["athleteId"], str(self.ath_a.pk))
        self.assertEqual(data["athleteName"], "张小明")
        self.assertEqual(data["maskedIdNumber"], self.ath_a.id_card_masked)
        self.assertEqual(data["jerseyNumber"], 7)
        self.assertEqual(data["position"], "GK")
        self.assertEqual(data["status"], "CURRENT")  # 冻结枚举真实状态
        self.assertTrue(data["active"])
        self.assertEqual(data["effectiveFrom"], "2026-03-01")
        self.assertEqual(data["version"], self._mver(self.member_a))

    def test_member_get_exited_history_readable(self):
        resp = self._get(
            f"{self._mpath(self.team_main.pk)}/{self.member_c.pk}",
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "EXITED")
        self.assertFalse(data["active"])

    def test_member_get_missing_team_404(self):
        resp = self._get(f"{BASE}/99999/members/{self.member_a.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)

    def test_member_get_soft_deleted_team_404(self):
        resp = self._get(
            f"{self._mpath(self.team_soft.pk)}/{self.member_a.pk}",
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 404)

    def test_member_get_cross_team_404(self):
        """成员属他队 → 404（_MEMBER_NOT_TEAM 语义，不泄露他队关系存在性）。"""
        other = TeamMember.objects.create(
            team=self.team_plain, person=self.ath_b, joined_at=date(2026, 4, 1)
        )
        resp = self._get(
            f"{self._mpath(self.team_main.pk)}/{other.pk}", token=self.admin_token
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")
        self.assertIn("不属于该球队", resp.json()["message"])

    # ------------------------------------------------------------ 候选搜索

    def test_member_search_athletes_only_excludes_current(self):
        resp = self.client.post(
            f"{self._mpath(self.team_main.pk)}/search",
            data=json.dumps({"keyword": "小"}),
            content_type="application/json",
            **self._auth(self.admin_token),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        ids = [p["id"] for p in data]
        self.assertIn(str(self.ath_b.pk), ids)  # 自由球员
        self.assertIn(str(self.ath_c.pk), ids)  # EXITED 历史可重新加入
        self.assertNotIn(str(self.ath_a.pk), ids)  # 本队现役排除
        self.assertNotIn(str(self.ath_d.pk), ids)  # 本队现役排除
        self.assertNotIn(str(self.coach_only.pk), ids)  # 非 ATHLETE
        self.assertNotIn(str(self.ath_plain.pk), ids)  # 无档案行非 ATHLETE（方案C）
        self.assertNotIn(str(self.ath_del.pk), ids)  # 软删排除
        first = data[0]
        self.assertEqual(first["realName"], Person.objects.get(pk=first["id"]).full_name)
        self.assertIn("schoolName", first)  # Person 形状（前端候选卡消费）

    def test_member_search_limit_20(self):
        for i in range(25):
            _athlete(300 + i, f"候选球员{i:02d}")
        resp = self.client.post(
            f"{self._mpath(self.team_main.pk)}/search",
            data=json.dumps({"keyword": "候选球员"}),
            content_type="application/json",
            **self._auth(self.admin_token),
        )
        self.assertEqual(len(resp.json()["data"]), 20)

    def test_member_search_missing_team_404(self):
        resp = self.client.post(
            f"{BASE}/99999/members/search",
            data=json.dumps({"keyword": "小"}),
            content_type="application/json",
            **self._auth(self.admin_token),
        )
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------ 添加成员

    def test_member_add_success(self):
        resp = self._post(
            self._mpath(self.team_main.pk),
            {"athleteId": str(self.ath_b.pk), "jerseyNumber": 10, "position": "FW"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["athleteName"], "李小虎")
        self.assertEqual(data["status"], "CURRENT")
        self.assertTrue(data["active"])
        self.assertEqual(data["position"], "FW")
        self.assertEqual(data["jerseyNumber"], 10)
        self.assertEqual(data["effectiveFrom"], timezone.localdate().isoformat())
        # 夹具预置 5 行（a/c/d/del/transfer 来源 e）+ 新增 1 行
        self.assertEqual(TeamMember.objects.filter(team=self.team_main).count(), 6)

    def test_member_add_position_stored_as_is(self):
        """position 透传存取（无正式字典值域，不映射不校验——缺口在案）。"""
        resp = self._post(
            self._mpath(self.team_main.pk),
            {"athleteId": str(self.ath_b.pk), "position": "中场"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["position"], "中场")
        self.assertIsNone(resp.json()["data"]["jerseyNumber"])

    def test_member_add_non_athlete_conflict(self):
        """无 AthleteProfile 行（教练/纯主类别）→ 409（方案C 事实源）。"""
        for pid in (self.coach_only.pk, self.ath_plain.pk):
            resp = self._post(
                self._mpath(self.team_main.pk),
                {"athleteId": str(pid)},
                token=self.admin_token,
            )
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(resp.json()["code"], "STATE_CONFLICT")

    def test_member_add_unknown_or_deleted_athlete_404(self):
        for pid in ("99999", self.ath_del.pk):
            resp = self._post(
                self._mpath(self.team_main.pk),
                {"athleteId": str(pid)},
                token=self.admin_token,
            )
            self.assertEqual(resp.status_code, 404)

    def test_member_add_duplicate_current_409(self):
        resp = self._post(
            self._mpath(self.team_main.pk),
            {"athleteId": str(self.ath_a.pk), "jerseyNumber": 11},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")

    def test_member_add_rejoin_after_exit_allowed(self):
        resp = self._post(
            self._mpath(self.team_main.pk),
            {"athleteId": str(self.ath_c.pk), "jerseyNumber": 12},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        rows = TeamMember.objects.filter(team=self.team_main, person=self.ath_c)
        self.assertEqual(rows.count(), 2)  # 历史 EXITED 行保留 + 新 CURRENT 行
        self.assertTrue(
            rows.filter(status=enums.RelationStatus.CURRENT, jersey_number=12).exists()
        )

    def test_member_add_duplicate_jersey_409(self):
        resp = self._post(
            self._mpath(self.team_main.pk),
            {"athleteId": str(self.ath_b.pk), "jerseyNumber": 7},  # member_a 已用
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("R3", resp.json()["message"])

    # ------------------------------------------------------------ 更新成员

    def test_member_update_success(self):
        resp = self._put(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}",
            {
                "athleteId": str(self.ath_a.pk),  # 回传字段
                "jerseyNumber": 18,
                "position": "DF",
                "effectiveFrom": "2020-01-01",  # 前端默认回传，不得应用
                "version": self._mver(self.member_a),
            },
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["jerseyNumber"], 18)
        self.assertEqual(data["position"], "DF")
        self.assertGreater(data["version"], self._mver(self.member_a))
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.joined_at, date(2026, 3, 1))  # 未被重写

    def test_member_update_athlete_id_tamper_ignored(self):
        resp = self._put(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}",
            {
                "athleteId": str(self.ath_b.pk),  # 篡改目标
                "version": self._mver(self.member_a),
            },
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.person_id, self.ath_a.pk)  # 未重绑

    def test_member_update_missing_version_400(self):
        resp = self._put(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}", {"jerseyNumber": 3},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)

    def test_member_update_stale_version_409(self):
        stale = self._mver(self.member_a)
        self.member_a.position_code = "MF"
        self.member_a.save()  # 版本前进
        resp = self._put(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}",
            {"jerseyNumber": 3, "version": stale},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.position_code, "MF")  # 未被覆盖

    def test_member_update_cross_team_404(self):
        stranger = TeamMember.objects.create(
            team=self.team_plain, person=self.ath_b, joined_at=date(2026, 4, 1)
        )
        resp = self._put(
            f"{self._mpath(self.team_main.pk)}/{stranger.pk}",
            {"jerseyNumber": 3, "version": self._mver(stranger)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 404)

    def test_member_update_exited_forbidden(self):
        resp = self._put(
            f"{self._mpath(self.team_main.pk)}/{self.member_c.pk}",
            {"jerseyNumber": 3, "version": self._mver(self.member_c)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)

    def test_member_update_duplicate_jersey_409_and_self_keep_ok(self):
        url = f"{self._mpath(self.team_main.pk)}/{self.member_d.pk}"
        resp = self._put(
            url, {"jerseyNumber": 7, "version": self._mver(self.member_d)},  # member_a 占用
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        resp = self._put(
            url, {"jerseyNumber": 9, "version": self._mver(self.member_d)},  # 自身现值
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------ 移出成员

    def test_member_delete_soft_exit(self):
        resp = self._delete(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}",
            token=self.admin_token, version=self._mver(self.member_a),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["data"]["success"])
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.status, enums.RelationStatus.EXITED)
        self.assertIsNotNone(self.member_a.left_at)  # 无物理删除，行仍在
        # 离队后可重新加入（R2 只限现役）
        resp = self._post(
            self._mpath(self.team_main.pk),
            {"athleteId": str(self.ath_a.pk), "jerseyNumber": 7},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)

    def test_member_delete_missing_version_400(self):
        resp = self._delete(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}", token=self.admin_token
        )
        self.assertEqual(resp.status_code, 400)

    def test_member_delete_stale_version_409(self):
        stale = self._mver(self.member_a)
        self.member_a.jersey_number = 77
        self.member_a.save()
        resp = self._delete(
            f"{self._mpath(self.team_main.pk)}/{self.member_a.pk}",
            token=self.admin_token, version=stale,
        )
        self.assertEqual(resp.status_code, 409)
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.status, enums.RelationStatus.CURRENT)

    def test_member_delete_repeat_exit_409(self):
        resp = self._delete(
            f"{self._mpath(self.team_main.pk)}/{self.member_c.pk}",
            token=self.admin_token, version=self._mver(self.member_c),
        )
        self.assertEqual(resp.status_code, 409)

    def test_member_delete_cross_team_404(self):
        stranger = TeamMember.objects.create(
            team=self.team_plain, person=self.ath_b, joined_at=date(2026, 4, 1)
        )
        resp = self._delete(
            f"{self._mpath(self.team_main.pk)}/{stranger.pk}",
            token=self.admin_token, version=self._mver(stranger),
        )
        self.assertEqual(resp.status_code, 404)

    def test_member_delete_blocked_by_pending_transfer(self):
        """PENDING 转出单阻断：成员与转队单均不变。"""
        resp = self._delete(
            f"{self._mpath(self.team_main.pk)}/{self.member_d.pk}",
            token=self.admin_token, version=self._mver(self.member_d),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.member_d.refresh_from_db()
        self.assertEqual(self.member_d.status, enums.RelationStatus.CURRENT)
        self.assertIsNone(self.member_d.left_at)
        self.transfer_pending.refresh_from_db()
        self.assertEqual(self.transfer_pending.status, enums.TransferStatus.PENDING)

    def test_member_delete_transfer_source_safety_fail(self):
        """source=TRANSFER 关系行安全失败（规则留待转队域，不自行定口径）。"""
        resp = self._delete(
            f"{self._mpath(self.team_main.pk)}/{self.member_trans.pk}",
            token=self.admin_token, version=self._mver(self.member_trans),
        )
        self.assertEqual(resp.status_code, 409)
        self.member_trans.refresh_from_db()
        self.assertEqual(self.member_trans.status, enums.RelationStatus.CURRENT)

    def test_member_delete_rolls_back_when_save_fails(self):
        from apps.teams import services

        with mock.patch.object(TeamMember, "save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                services.member_remove(
                    str(self.team_main.pk), str(self.member_a.pk),
                    self._mver(self.member_a), self.admin,
                )
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.status, enums.RelationStatus.CURRENT)
        self.assertIsNone(self.member_a.left_at)  # 无部分写入残留

    # ------------------------------------------------------------ 审计

    def test_member_audit_logs_written(self):
        base = self._mpath(self.team_main.pk)
        self._post(base, {"athleteId": str(self.ath_b.pk)}, token=self.admin_token)
        self._put(
            f"{base}/{self.member_a.pk}",
            {"jerseyNumber": 18, "version": self._mver(self.member_a)},
            token=self.admin_token,
        )
        self.member_a.refresh_from_db()  # PUT 已推进 updated_at，取最新版本
        self._delete(
            f"{base}/{self.member_a.pk}", token=self.admin_token,
            version=self._mver(self.member_a),
        )
        for action in ("team.member.create", "team.member.update", "team.member.remove"):
            self.assertTrue(
                AuditLog.objects.filter(
                    action=action,
                    object_type="TeamMember",
                    operator_account_id=self.admin.pk,
                ).exists(),
                action,
            )


class TeamDataScopeTests(TestCase):
    """A′ 第 2 批：teams 域数据范围授权（HTTP 层 + verify_team_owner 服务层）。

    组织树：ORG-P → ORG-C → ORG-SCH-A(code=SCH-A) → TEAM 节点 a1(启用) /
            a2(停用)；ORG-SCH-B(code=SCH-B) → TEAM 节点 b1(启用)。
    球队：team_a1(映射正常) / team_a2(节点停用→拒绝) /
          team_nomap(无 TEAM 节点→拒绝) / team_b1(乙校)。
    账号：全局 ADMIN / 挂市组织 ADMIN / 甲校 UNIVERSITY / 乙校 UNIVERSITY /
          甲一队 TEAM / 无绑定 / 幽灵绑定 / 软删组织绑定（后三者空范围）。
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin_global = Account.objects.create(
            id=new_account_id(), username="scope-admin-global",
            display_name="全局管理员", roles=["ADMIN"],
            password=make_password("admin-pass-123"), enabled=True,
        )
        cls.uni_a = Account.objects.create(
            id=new_account_id(), username="scope-uni-a",
            display_name="甲校端", roles=["UNIVERSITY"],
            password=make_password("uni-pass-123"), enabled=True,
        )
        cls.uni_b = Account.objects.create(
            id=new_account_id(), username="scope-uni-b",
            display_name="乙校端", roles=["UNIVERSITY"],
            password=make_password("uni-pass-123"), enabled=True,
        )
        cls.team_acc = Account.objects.create(
            id=new_account_id(), username="scope-team-a1",
            display_name="甲一队端", roles=["TEAM"],
            password=make_password("team-pass-123"), enabled=True,
        )
        cls.no_org = Account.objects.create(
            id=new_account_id(), username="scope-no-org",
            display_name="无绑定校端", roles=["UNIVERSITY"],
            password=make_password("noorg-pass-123"), enabled=True,
        )
        # 幽灵绑定（organization_id 指向不存在节点）无法经 FK 落库（DB 层
        # 参照完整性），改为服务级用例：test_ghost_binding_denied_service_level。

        # ---- 组织树（两校并列，TEAM 节点 code=team_no，A′ 映射）----
        org_prov = Organization.objects.create(
            id=new_organization_id(), name="范围省", org_type="PROVINCE", code="ORG-P-SCOPE"
        )
        org_city = Organization.objects.create(
            id=new_organization_id(), name="范围市", org_type="CITY",
            code="ORG-C-SCOPE", parent=org_prov,
        )
        cls.org_city = org_city
        cls.org_school_a = Organization.objects.create(
            id=new_organization_id(), name="甲校组织", org_type="SCHOOL",
            code="SCH-SCOPE-A", parent=org_city,
        )
        cls.org_school_b = Organization.objects.create(
            id=new_organization_id(), name="乙校组织", org_type="SCHOOL",
            code="SCH-SCOPE-B", parent=org_city,
        )
        cls.org_team_a1 = Organization.objects.create(
            id=new_organization_id(), name="甲一队组织", org_type="TEAM",
            code="T202688880001", parent=cls.org_school_a,
        )
        cls.org_team_a2 = Organization.objects.create(
            id=new_organization_id(), name="甲二队组织", org_type="TEAM",
            code="T202688880002", parent=cls.org_school_a, enabled=False,
        )  # 停用 TEAM 节点 → 对应球队范围拒绝
        Organization.objects.create(
            id=new_organization_id(), name="乙一队组织", org_type="TEAM",
            code="T202688880003", parent=cls.org_school_b,
        )
        org_softed = Organization.objects.create(
            id=new_organization_id(), name="软删校组织", org_type="SCHOOL",
            code="SCH-SCOPE-DEAD", parent=org_city,
            deleted_at=timezone.now(),
        )
        cls.soft_acc = Account.objects.create(
            id=new_account_id(), username="scope-soft-org",
            display_name="软删绑定", roles=["UNIVERSITY"],
            password=make_password("soft-pass-123"), enabled=True,
            organization_id=org_softed.pk,
        )
        cls.admin_city = Account.objects.create(
            id=new_account_id(), username="scope-admin-org",
            display_name="甲校管理员", roles=["ADMIN"],
            password=make_password("admin-pass-123"), enabled=True,
            organization_id=cls.org_school_a.pk,
        )  # 挂组织 ADMIN：仅甲校子树（乙校=子树外对照）

        for acc, org in (
            (cls.uni_a, cls.org_school_a), (cls.uni_b, cls.org_school_b),
            (cls.team_acc, cls.org_team_a1),
        ):
            acc.organization_id = org.pk
            acc.save()

        # ---- 学校主档（create_team 链 _resolve_school 反查 School.code=org.code）----
        region_scope = Region.objects.create(name="范围省Region", code="980000", level=1)
        School.objects.create(
            name="甲校主档", code="SCH-SCOPE-A", school_type="PRIMARY", region=region_scope
        )
        School.objects.create(
            name="乙校主档", code="SCH-SCOPE-B", school_type="PRIMARY", region=region_scope
        )

        # ---- 球队（team_no 与 TEAM 节点 code 一一对应；nomap 无节点）----
        cls.team_a1 = Team.objects.create(
            team_no="T202688880001", name="甲一队", category_code="PRIMARY"
        )
        cls.team_a2 = Team.objects.create(
            team_no="T202688880002", name="甲二队", category_code="PRIMARY"
        )
        cls.team_nomap = Team.objects.create(
            team_no="T202688880009", name="无映射队", category_code="PRIMARY"
        )
        cls.team_b1 = Team.objects.create(
            team_no="T202688880003", name="乙一队", category_code="PRIMARY"
        )

        # ---- 甲一队现役成员（member 目标校验用例）----
        cls.ath_x = _athlete(201, "范围小明")
        cls.member_x = TeamMember.objects.create(
            team=cls.team_a1, person=cls.ath_x, jersey_number=10,
            joined_at=date(2026, 3, 1),
        )

        cls.admin_global_token = _token(cls.admin_global)
        cls.admin_city_token = _token(cls.admin_city)
        cls.uni_a_token = _token(cls.uni_a)
        cls.uni_b_token = _token(cls.uni_b)
        cls.team_token = _token(cls.team_acc)
        cls.no_org_token = _token(cls.no_org)
        cls.soft_token = _token(cls.soft_acc)

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _get(self, path, token=None, query=None):
        return self.client.get(path, data=query or {}, **self._auth(token))

    def _post(self, path, payload, token=None):
        return self.client.post(
            path, data=json.dumps(payload), content_type="application/json",
            **self._auth(token),
        )

    def _put(self, path, payload, token=None):
        return self.client.put(
            path, data=json.dumps(payload), content_type="application/json",
            **self._auth(token),
        )

    def _delete(self, path, token=None, version=None):
        url = f"{path}?version={version}" if version is not None else path
        return self.client.delete(url, **self._auth(token))

    @staticmethod
    def _list_team_nos(resp):
        body = resp.json()["data"]
        return sorted(item["teamNo"] for item in body["items"])

    def _assert_scope_denied(self, resp):
        """拒绝用例统一断言：403 + DATA_SCOPE_DENIED（具体错误码与状态码）。"""
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    # ------------------------------------------------------------ 全局 ADMIN

    def test_global_admin_sees_and_reads_all_teams(self):
        """验收1：全局 ADMIN 全库——列表含两校全部球队，可读乙校球队详情。"""
        resp = self._get(BASE, token=self.admin_global_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self._list_team_nos(resp),
            ["T202688880001", "T202688880002", "T202688880003", "T202688880009"],
        )
        resp = self._get(f"{BASE}/{self.team_b1.pk}", token=self.admin_global_token)
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------ 挂组织 ADMIN

    def test_org_admin_list_filtered_to_subtree(self):
        """验收2+5：挂组织 ADMIN 列表仅含子树内有效 TEAM 节点球队——
        服务端谓词过滤（停用节点/无映射/越界球队均不在查询集中）。"""
        resp = self._get(BASE, token=self.admin_city_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._list_team_nos(resp), ["T202688880001"])

    def test_org_admin_get_out_of_subtree_denied(self):
        """验收2：挂组织 ADMIN 读子树外球队 → 403 DATA_SCOPE_DENIED。"""
        self._assert_scope_denied(
            self._get(f"{BASE}/{self.team_b1.pk}", token=self.admin_city_token)
        )

    def test_org_admin_get_disabled_and_missing_mapping_denied(self):
        """验收9：TEAM 节点停用 / 映射缺失的球队一律拒绝，不意外放行。"""
        self._assert_scope_denied(
            self._get(f"{BASE}/{self.team_a2.pk}", token=self.admin_city_token)
        )
        self._assert_scope_denied(
            self._get(f"{BASE}/{self.team_nomap.pk}", token=self.admin_city_token)
        )

    # ------------------------------------------------------------ TEAM 角色

    def test_team_role_own_team_ok_cross_team_denied(self):
        """验收3：TEAM 本队访问成功（含成员列表），跨队访问拒绝。"""
        resp = self._get(f"{BASE}/{self.team_a1.pk}", token=self.team_token)
        self.assertEqual(resp.status_code, 200)
        resp = self._get(
            f"{BASE}/{self.team_a1.pk}/members", token=self.team_token
        )
        self.assertEqual(resp.status_code, 200)
        self._assert_scope_denied(
            self._get(f"{BASE}/{self.team_b1.pk}", token=self.team_token)
        )
        self._assert_scope_denied(
            self._get(f"{BASE}/{self.team_b1.pk}/members", token=self.team_token)
        )

    # ------------------------------------------------------------ UNIVERSITY

    def test_university_school_scope_list_and_get(self):
        """验收4：学校端列表仅见本校球队；跨校详情/成员拒绝。"""
        resp = self._get(BASE, token=self.uni_a_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._list_team_nos(resp), ["T202688880001"])
        resp = self._get(f"{BASE}/{self.team_a1.pk}", token=self.uni_a_token)
        self.assertEqual(resp.status_code, 200)
        self._assert_scope_denied(
            self._get(f"{BASE}/{self.team_b1.pk}", token=self.uni_a_token)
        )
        self._assert_scope_denied(
            self._get(f"{BASE}/{self.team_a1.pk}", token=self.uni_b_token)
        )

    def test_university_create_team_in_scope_ok(self):
        """验收7正向：学校端在本校组织下建队成功（含 TEAM 节点同事务建立）。"""
        resp = self._post(
            BASE,
            {"name": "甲校新队", "groupCategory": "PRIMARY",
             "schoolOrgId": str(self.org_school_a.pk)},
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 200)
        team = Team.objects.filter(name="甲校新队").first()
        self.assertIsNotNone(team)
        self.assertTrue(
            Organization.objects.filter(code=team.team_no, org_type="TEAM").exists()
        )

    def test_university_create_team_out_of_scope_denied(self):
        """验收7：学校端不能在范围外学校建队（403 且不落库）。"""
        before = Team.objects.count()
        self._assert_scope_denied(
            self._post(
                BASE,
                {"name": "越权建队", "groupCategory": "PRIMARY",
                 "schoolOrgId": str(self.org_school_b.pk)},
                token=self.uni_a_token,
            )
        )
        self.assertEqual(Team.objects.count(), before)

    # ------------------------------------------------------------ 空范围 / 失效范围

    def test_empty_scope_accounts_denied(self):
        """验收9：无绑定 / 软删组织绑定（空范围）一律拒绝（HTTP 层）。"""
        for token in (self.no_org_token, self.soft_token):
            self._assert_scope_denied(self._get(BASE, token=token))
            self._assert_scope_denied(
                self._get(f"{BASE}/{self.team_a1.pk}", token=token)
            )

    def test_ghost_binding_denied_service_level(self):
        """验收9：绑定组织不存在（幽灵绑定，DB 外键层无法落库）→ 服务层
        空范围拒绝，不得放大为全库（不落库的 Account 实例模拟）。"""
        from apps.common.errors import ApiError
        from apps.common import scopes
        from apps.teams.services import _require_admin_scope

        ghost = Account(organization_id="ORG-GHOST-T4-SCOPE", roles=["UNIVERSITY"])
        with self.assertRaises(ApiError) as ctx:
            scopes.ensure_team_in_scope(ghost, self.team_a1)
        self.assertEqual(ctx.exception.code, "DATA_SCOPE_DENIED")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            _require_admin_scope(ghost)
        self.assertEqual(ctx.exception.code, "DATA_SCOPE_DENIED")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            scopes.ensure_org_in_scope(ghost, self.org_school_a.pk)
        self.assertEqual(ctx.exception.code, "DATA_SCOPE_DENIED")
        self.assertEqual(ctx.exception.status, 403)

    # ------------------------------------------------------------ 成员目标校验

    def test_member_ops_target_team_checked(self):
        """验收6：member list/get/search/add/update/remove 均校验目标球队——
        甲一队端访问乙一队（目标球队在范围外）→ 403，先于成员归属 404。"""
        base_b = f"{BASE}/{self.team_b1.pk}/members"
        self._assert_scope_denied(self._get(base_b, token=self.team_token))
        self._assert_scope_denied(
            self._get(f"{base_b}/{self.member_x.pk}", token=self.team_token)
        )
        self._assert_scope_denied(
            self._post(f"{base_b}/search", {"keyword": "小"}, token=self.team_token)
        )
        self._assert_scope_denied(
            self._post(base_b, {"athleteId": str(self.ath_x.pk)}, token=self.team_token)
        )
        self._assert_scope_denied(
            self._put(
                f"{base_b}/{self.member_x.pk}",
                {"jerseyNumber": 8, "version": int(self.member_x.updated_at.timestamp() * 1_000_000)},
                token=self.team_token,
            )
        )
        self._assert_scope_denied(
            self._delete(
                f"{base_b}/{self.member_x.pk}",
                token=self.team_token,
                version=int(self.member_x.updated_at.timestamp() * 1_000_000),
            )
        )
        # 状态零变更（拒绝先于写操作）
        self.member_x.refresh_from_db()
        self.assertEqual(self.member_x.jersey_number, 10)
        self.assertEqual(self.member_x.status, enums.RelationStatus.CURRENT)

    # ------------------------------------------------------------ 附件归属校验器

    def test_verify_team_owner_scoping(self):
        """验收8：verify_team_owner 不因本轮改动放宽跨范围访问——
        全局 ADMIN 全库放行；挂组织/学校/球队端按 A′ 映射逐对象裁决。"""
        from apps.common.errors import ApiError
        from apps.teams.services import verify_team_owner

        # 放行路径
        verify_team_owner(self.admin_global, str(self.team_b1.pk))
        verify_team_owner(self.admin_city, str(self.team_a1.pk))
        verify_team_owner(self.uni_a, str(self.team_a1.pk))
        # 拒绝路径：越界 / 空范围
        for account, owner in (
            (self.admin_city, self.team_b1),
            (self.uni_a, self.team_b1),
            (self.uni_b, self.team_a1),
            (self.team_acc, self.team_b1),
            (self.no_org, self.team_a1),
        ):
            with self.assertRaises(ApiError) as ctx:
                verify_team_owner(account, str(owner.pk))
            self.assertEqual(ctx.exception.code, "DATA_SCOPE_DENIED")
            self.assertEqual(ctx.exception.status, 403)
