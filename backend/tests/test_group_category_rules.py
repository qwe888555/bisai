"""2026-10-28"高校组"清理回归：学段/组别值域服务端收敛。

业务规则（已裁定）：本项目没有"高校组"，学段/组别仅小学/初中/高中
（PRIMARY/MIDDLE，高中口径=MIDDLE 初中段，详见规则文档待决 11）；
服务端在 schema 层明确拒绝 UNIVERSITY，不做静默转换。覆盖：

1. events 组别 GroupCategory：PRIMARY/MIDDLE 合法、UNIVERSITY 拒绝；
2. enrollments 报名 subjectType（创建/更新）：SCHOOL_TEAM/CITY 合法、
   UNIVERSITY 拒绝；读侧过滤保留 UNIVERSITY（历史数据兼容）；
3. teams groupCategory（创建/更新回传）：PRIMARY/MIDDLE 合法、UNIVERSITY 拒绝；
4. 服务层 scope.subject_type_allowed：SCHOOL_TEAM 放行、
   UNIVERSITY 报送被拒、非学校端角色被拒。

运行：$env:DATABASE_URL="sqlite:///:memory:"; python manage.py test tests.test_group_category_rules
"""

from __future__ import annotations

from django.test import TestCase
from pydantic import TypeAdapter, ValidationError

from apps.common.errors import ApiError
from apps.enrollments import schemas as enr_schemas
from apps.enrollments import scope as enr_scope
from apps.events import schemas as evt_schemas
from apps.teams import schemas as team_schemas


class _StubAccount:
    """role_set(account) 仅读 roles 属性，ORM 账号在此场景无必要。"""

    def __init__(self, roles):
        self.roles = roles


class EventGroupCategoryRuleTests(TestCase):
    """events 组别：无高校组。"""

    def setUp(self):
        self.adapter = TypeAdapter(evt_schemas.GroupCategory)

    def test_primary_and_middle_are_valid(self):
        self.assertEqual(self.adapter.validate_python("PRIMARY"), "PRIMARY")
        self.assertEqual(self.adapter.validate_python("MIDDLE"), "MIDDLE")

    def test_university_group_is_rejected(self):
        for bad in ("UNIVERSITY", "HIGHER", "高校组", "university"):
            with self.assertRaises(ValidationError):
                self.adapter.validate_python(bad)


class EnrollmentSubjectTypeRuleTests(TestCase):
    """报名 subjectType：创建/更新拒绝高校主体，读侧过滤保留兼容。"""

    def _create_payload(self, subject_type):
        return {
            "eventId": "evt_1",
            "groupId": "evg_1",
            "teamId": "T00000001",
            "subjectType": subject_type,
            "contactName": "联系人",
            "contactPhone": "13800000000",
        }

    def test_school_team_and_city_are_valid_on_create_and_update(self):
        for st in ("SCHOOL_TEAM", "CITY"):
            body = enr_schemas.EnrollmentCreateIn(**self._create_payload(st))
            self.assertEqual(body.subjectType, st)
        update = self._create_payload("SCHOOL_TEAM") | {"version": 0}
        self.assertEqual(enr_schemas.EnrollmentUpdateIn(**update).subjectType, "SCHOOL_TEAM")

    def test_university_subject_is_rejected_on_create_and_update(self):
        for st in ("UNIVERSITY", "高校", "HIGHER"):
            with self.assertRaises(ValidationError):
                enr_schemas.EnrollmentCreateIn(**self._create_payload(st))
            update = self._create_payload(st) | {"version": 0}
            with self.assertRaises(ValidationError):
                enr_schemas.EnrollmentUpdateIn(**update)

    def test_read_side_filter_keeps_university_for_history(self):
        """读侧过滤显式保留 UNIVERSITY（历史数据兼容，规则文档待决 11）。"""
        self.assertIn(
            "UNIVERSITY",
            enr_schemas.EnrollmentFiltersIn.model_fields["subjectType"].annotation.__args__[0].__args__,
        )


class TeamGroupCategoryRuleTests(TestCase):
    """teams groupCategory：创建/更新回传均无高校组。"""

    def _create_payload(self, category):
        return {"name": "测试队", "groupCategory": category}

    def test_primary_and_middle_are_valid(self):
        for cat in ("PRIMARY", "MIDDLE"):
            body = team_schemas.TeamCreateBody(**self._create_payload(cat))
            self.assertEqual(body.groupCategory, cat)
        update = {"groupCategory": "MIDDLE", "version": 0}
        self.assertEqual(team_schemas.TeamUpdateBody(**update).groupCategory, "MIDDLE")

    def test_university_group_is_rejected(self):
        for bad in ("UNIVERSITY", "高校组", "HIGHER"):
            with self.assertRaises(ValidationError):
                team_schemas.TeamCreateBody(**self._create_payload(bad))
            with self.assertRaises(ValidationError):
                team_schemas.TeamUpdateBody(**{"groupCategory": bad, "version": 0})


class EnrollmentSubjectScopeTests(TestCase):
    """服务层 scope.subject_type_allowed：与 schema 口径一致。"""

    def test_school_team_subject_allowed_for_university_role(self):
        acct = _StubAccount(["UNIVERSITY"])
        enr_scope.subject_type_allowed(acct, "SCHOOL_TEAM")  # 不抛即通过

    def test_university_subject_rejected_no_silent_conversion(self):
        from apps.common.errors import FORBIDDEN

        acct = _StubAccount(["UNIVERSITY"])
        with self.assertRaises(ApiError) as ctx:
            enr_scope.subject_type_allowed(acct, "UNIVERSITY")
        self.assertEqual(ctx.exception.code, FORBIDDEN)

    def test_non_university_role_rejected(self):
        from apps.common.errors import FORBIDDEN

        acct = _StubAccount(["CITY"])
        with self.assertRaises(ApiError) as ctx:
            enr_scope.subject_type_allowed(acct, "SCHOOL_TEAM")
        self.assertEqual(ctx.exception.code, FORBIDDEN)


class ModelChoicesConvergenceTests(TestCase):
    """待决 11 方案二 2a（2026-10-28）：模型 choices 收敛 + 历史读侧兼容分离。"""

    def test_event_group_categories_excludes_university(self):
        from apps.events import models as evt_models

        self.assertEqual(
            dict(evt_models.GROUP_CATEGORIES), {"PRIMARY": "小学组", "MIDDLE": "初中组"}
        )
        self.assertNotIn("UNIVERSITY", dict(evt_models.GROUP_CATEGORIES))

    def test_subject_types_excludes_university(self):
        from apps.enrollments import models as enr_models

        self.assertEqual(
            dict(enr_models.SUBJECT_TYPES),
            {"SCHOOL_TEAM": "中小学球队", "CITY": "市州代报"},
        )

    def test_form_config_and_write_side_use_converged_values(self):
        """form-config 与写入校验共用收敛后的 _SUBJECT_TYPES，不再声明高校选项。"""
        from apps.enrollments import api as enr_api

        self.assertEqual(enr_api._SUBJECT_TYPES, ("SCHOOL_TEAM", "CITY"))

    def test_history_list_filter_keeps_university(self):
        """历史列表过滤独立常量保留 UNIVERSITY，读侧兼容不回灌写侧。"""
        from apps.enrollments import api as enr_api

        self.assertIn("UNIVERSITY", enr_api._HISTORICAL_SUBJECT_TYPES)
        self.assertNotIn("UNIVERSITY", enr_api._SUBJECT_TYPES)

    def test_university_role_and_org_type_unchanged(self):
        """学校端角色与组织节点类型（"高校"组织）合法保留，与学段无混淆。"""
        from apps.accounts.models import Organization
        from apps.common import permissions as acc_perms

        self.assertIn("UNIVERSITY", acc_perms.ALL_ROLES)
        self.assertIn("UNIVERSITY", [o[0] for o in Organization.ORG_TYPES])
