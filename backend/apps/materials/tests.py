"""材料域 HTTP 层测试（T4-03 批次 C：GET/POST /materials、
POST /materials/{id}/review、DELETE /materials/{id}?version=；
批次 D-2 补齐 materialGet、批次 D-3 补齐 materialUpdate）。

一律 SQLite 内存库；覆盖：认证/角色/数据范围、Person/Team 归属与跨归属越权、
类型表空缺/无效/合法、文件不存在/归属不匹配/重复绑定/先传后绑回填、
R6 唯一性、审核状态转换与审核历史、乐观锁、软删与重复操作、
事务回滚、审计留痕、前端真实请求字段与响应结构、
多态双命中 409（复验修复①）、文件归属条件更新防并发双绑（复验修复②）、
审核状态与审计日志同事务原子性（复验修复③）、
文件版本唯一约束冲突转译（D-3 局部修复）。
"""

from __future__ import annotations

import json
from unittest import mock

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account, AuditLog
from apps.attachments.models import FileRecord
from apps.common import enums
from apps.common.authentication import issue_access_token, make_password
from apps.common.ids import new_account_id, new_file_id
from apps.materials.models import (
    MaterialAuditLog,
    MaterialFileVersion,
    MaterialType,
    PersonMaterial,
    TeamMaterial,
)
from apps.persons.models import Person, Region
from apps.teams.models import Team

BASE = "/api/v1/materials"


def _token(account):
    return issue_access_token(account)[0]


def services_material_create(payload, account):
    from apps.materials import services

    return services.material_create(payload, account)


class MaterialApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # ---- 账号 ----
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="materials-admin",
            display_name="材料管理员",
            roles=["ADMIN"],
            password=make_password("admin-pass-123"),
            enabled=True,
        )
        cls.uni = Account.objects.create(
            id=new_account_id(),
            username="materials-uni",
            display_name="学校端",
            roles=["UNIVERSITY"],
            password=make_password("uni-pass-123"),
            enabled=True,
        )
        cls.athlete = Account.objects.create(
            id=new_account_id(),
            username="materials-athlete",
            display_name="运动员",
            roles=["ATHLETE"],
            password=make_password("athlete-pass-123"),
            enabled=True,
        )
        cls.admin_token = _token(cls.admin)
        cls.uni_token = _token(cls.uni)
        cls.athlete_token = _token(cls.athlete)

        # ---- 归属对象 ----
        province = Region.objects.create(name="丙省", code="900000", level=1)
        cls.city = Region.objects.create(name="丙州市", code="900100", level=2, parent=province)

        def _person(seq, name, deleted=False):
            return Person.objects.create(
                person_no=f"P2026{seq:08d}",
                full_name=name,
                id_card_cipher=f"cipher-{seq}",
                id_card_hash=f"hash-{seq}",
                id_card_masked=f"1101****{seq:04d}X",
                person_category=enums.PersonCategory.ATHLETE,
                audit_status=enums.AuditStatus.APPROVED,
                is_deleted=deleted,
                deleted_at=timezone.now() if deleted else None,
            )

        cls.person_a = _person(1, "材料甲")
        cls.person_b = _person(2, "材料乙")
        cls.person_c = _person(3, "材料丙")
        cls.person_del = _person(4, "材料删", deleted=True)
        cls.team_a = Team.objects.create(
            team_no="T202600000101", name="丙一小男足队", category_code="PRIMARY", region=cls.city
        )
        cls.team_soft = Team.objects.create(
            team_no="T202600000102",
            name="已删球队",
            category_code="PRIMARY",
            is_deleted=True,
        )

        # ---- 材料类型 ----
        cls.type_person = MaterialType.objects.create(
            code="ATHLETE_CERTIFICATE", name="运动员等级证书", applies_to="PERSON"
        )
        cls.type_team = MaterialType.objects.create(
            code="TEAM_HONOR", name="球队荣誉证明", applies_to="TEAM"
        )
        cls.type_team_multi = MaterialType.objects.create(
            code="TEAM_REPORT", name="球队报告", applies_to="TEAM", allow_multiple=True
        )
        cls.type_free = MaterialType.objects.create(
            code="FREE_PASS", name="免审材料", applies_to="PERSON", need_audit=False
        )
        cls.type_multi = MaterialType.objects.create(
            code="AWARD", name="获奖证书", applies_to="PERSON", allow_multiple=True
        )
        cls.type_off = MaterialType.objects.create(
            code="OFF_TYPE", name="停用类型", applies_to="PERSON", is_active=False
        )

        # ---- 文件 ----
        def _file(owner_type, owner_id="", name="证明.pdf"):
            return FileRecord.objects.create(
                id=new_file_id(),
                owner_type=owner_type,
                owner_id=owner_id,
                original_name=name,
                storage_key=f"k/{name}",
                uploaded_by="tester",
            )

        cls.file_free_p = _file("PERSON", name="无主人员文件.pdf")  # 先传后绑
        cls.file_bound_p = _file("PERSON", owner_id=str(cls.person_a.pk), name="已绑甲.pdf")
        cls.file_bound_c = _file("PERSON", owner_id=str(cls.person_c.pk), name="已绑丙.pdf")
        cls.file_free_t = _file("TEAM", name="无主球队文件.pdf")
        cls.file_wrong = _file("TEAM", name="球队文件误用.pdf")

        # ---- 预置材料 ----
        cls.pm_pending = PersonMaterial.objects.create(
            person=cls.person_a,
            type=cls.type_person,
            file_id=cls.file_bound_p.pk,
            file_name="已绑甲.pdf",
            file_ext="pdf",
            status=enums.MaterialStatus.PENDING,
            remark="甲的证书",
        )
        cls.tm_pending = TeamMaterial.objects.create(
            pk=201,  # 显式错开两表主键，避免 _get_material_any 双表解析撞 id
            team=cls.team_a,
            type=cls.type_team,
            file_id=cls.file_free_t.pk,
            file_name="无主球队文件.pdf",
            status=enums.MaterialStatus.PENDING,
            remark="球队荣誉",
        )
        cls.pm_approved = PersonMaterial.objects.create(
            person=cls.person_b,
            type=cls.type_person,
            file_id=cls.file_bound_c.pk,
            file_name="已绑丙.pdf",
            status=enums.MaterialStatus.APPROVED,
            remark="乙的证书",
        )
        cls.pm_deleted = PersonMaterial.objects.create(
            person=cls.person_a,
            type=cls.type_multi,
            file_id=cls.file_free_p.pk,
            file_name="无主人员文件.pdf",
            status=enums.MaterialStatus.PENDING,
            remark="已删材料",
            is_deleted=True,
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

    def _delete(self, path, token=None, version=None):
        # Django test client 的 delete(data=...) 是请求体而非 query string，
        # DELETE 查询参数需手工拼 URL（teams 测试同款约定）
        if version is not None:
            path = f"{path}?version={version}"
        return self.client.delete(path, **self._auth(token))

    @staticmethod
    def _mver(material) -> int:
        return int(material.updated_at.timestamp() * 1_000_000)

    def _create_payload(self, **overrides):
        payload = {
            "ownerType": "PERSON",
            "ownerId": str(self.person_a.pk),
            "materialType": "AWARD",
            "title": "市联赛获奖证书",
            "description": "2026 市联赛一等奖",
            "fileName": "获奖证书.pdf",
            "fileIds": [self.file_free_p.pk],
        }
        payload.update(overrides)
        return payload

    # ------------------------------------------------------------ 认证与权限

    def test_list_requires_auth(self):
        self.assertEqual(self._get(BASE).status_code, 401)

    def test_create_requires_auth(self):
        self.assertEqual(self._post(BASE, self._create_payload()).status_code, 401)

    def test_review_requires_auth(self):
        resp = self._post(
            f"{BASE}/{self.pm_pending.pk}/review",
            {"decision": "APPROVE", "version": self._mver(self.pm_pending)},
        )
        self.assertEqual(resp.status_code, 401)

    def test_remove_requires_auth(self):
        self.assertEqual(
            self._delete(f"{BASE}/{self.pm_pending.pk}", version=self._mver(self.pm_pending)).status_code,
            401,
        )

    def test_list_athlete_scope_denied(self):
        """ATHLETE 在文档角色集内但数据范围一期仅 ADMIN（A′ 待决阻塞口径）。"""
        resp = self._get(BASE, token=self.athlete_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    def test_list_university_scope_denied(self):
        """UNIVERSITY 依赖 A′ 映射：范围拒绝（待决阻塞，非最终规则）。"""
        resp = self._get(BASE, token=self.uni_token, query={"ownerType": "PERSON", "ownerId": self.person_a.pk})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    def test_create_university_scope_denied(self):
        resp = self._post(BASE, self._create_payload(), token=self.uni_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    def test_review_university_scope_denied(self):
        resp = self._post(
            f"{BASE}/{self.pm_pending.pk}/review",
            {"decision": "APPROVE", "version": self._mver(self.pm_pending)},
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 403)

    def test_remove_university_scope_denied(self):
        resp = self._delete(
            f"{BASE}/{self.pm_pending.pk}", token=self.uni_token, version=self._mver(self.pm_pending)
        )
        self.assertEqual(resp.status_code, 403)

    # ------------------------------------------------------------ 列表

    def test_list_person_scoped_excludes_deleted(self):
        resp = self._get(
            BASE,
            token=self.admin_token,
            query={"ownerType": "PERSON", "ownerId": self.person_a.pk, "page": 1, "pageSize": 10},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual(body["total"], 1)  # pm_deleted 软删排除
        item = body["items"][0]
        self.assertEqual(item["ownerType"], "PERSON")
        self.assertEqual(item["ownerName"], "材料甲")
        self.assertEqual(item["materialType"], "ATHLETE_CERTIFICATE")
        self.assertEqual(item["materialLabel"], "运动员等级证书")
        self.assertEqual(item["title"], "甲的证书")  # remark → title
        self.assertEqual(item["reviewStatus"], "PENDING")
        self.assertEqual(item["fileIds"], [self.file_bound_p.pk])
        self.assertIsInstance(item["version"], int)
        for absent in ("description", "reviewedAt", "photoUrl"):
            self.assertNotIn(absent, item)

    def test_list_team_scoped_owner_name(self):
        resp = self._get(
            BASE,
            token=self.admin_token,
            query={"ownerType": "TEAM", "ownerId": self.team_a.pk},
        )
        items = resp.json()["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ownerName"], "丙一小男足队")

    def test_list_global_merges_both_kinds(self):
        """Dashboard 调用：无 ownerType → 跨两表合并（夹具 3 条有效材料）。"""
        resp = self._get(BASE, token=self.admin_token, query={"page": 1, "pageSize": 6})
        self.assertEqual(resp.json()["data"]["total"], 3)

    def test_list_owner_type_invalid(self):
        resp = self._get(BASE, token=self.admin_token, query={"ownerType": "CITY"})
        self.assertEqual(resp.status_code, 400)

    def test_list_owner_type_without_owner(self):
        resp = self._get(BASE, token=self.admin_token, query={"ownerType": "PERSON"})
        self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------------ 创建：类型

    def test_create_type_missing_when_table_empty(self):
        """类型表无该 code → 409 明确报错（不伪造成功、不硬编码 13 种类型）。"""
        resp = self._post(BASE, self._create_payload(materialType="NO_SUCH_TYPE"), token=self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("尚未初始化", resp.json()["message"])

    def test_create_type_inactive(self):
        resp = self._post(BASE, self._create_payload(materialType="OFF_TYPE"), token=self.admin_token)
        self.assertEqual(resp.status_code, 409)

    def test_create_type_owner_mismatch(self):
        """TEAM 类型用于 PERSON 归属 → 409（跨归属类型越权）。"""
        resp = self._post(BASE, self._create_payload(materialType="TEAM_HONOR"), token=self.admin_token)
        self.assertEqual(resp.status_code, 409)

    def test_create_all_types_empty_returns_409(self):
        """清空类型表模拟种子未导入：创建 409、列表正常空结果。"""
        PersonMaterial.objects.all().delete()  # PROTECT：先删引用行再清类型表
        TeamMaterial.objects.all().delete()
        MaterialType.objects.all().delete()
        resp = self._post(BASE, self._create_payload(), token=self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("尚未初始化", resp.json()["message"])
        resp2 = self._get(
            BASE, token=self.admin_token, query={"ownerType": "PERSON", "ownerId": self.person_a.pk}
        )
        self.assertEqual(resp2.status_code, 200)

    # ------------------------------------------------------------ 创建：归属与文件

    def test_create_person_success_and_response_shape(self):
        resp = self._post(BASE, self._create_payload(), token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["ownerType"], "PERSON")
        self.assertEqual(data["ownerId"], str(self.person_a.pk))
        self.assertEqual(data["ownerName"], "材料甲")
        self.assertEqual(data["title"], "市联赛获奖证书")
        self.assertEqual(data["reviewStatus"], "PENDING")  # need_audit=True
        self.assertEqual(data["fileIds"], [self.file_free_p.pk])
        self.assertEqual(data["fileName"], "获奖证书.pdf")
        self.assertEqual(data["reviewReason"], "")
        self.assertIsInstance(data["version"], int)
        # 先传后绑：文件归属回填
        self.file_free_p.refresh_from_db()
        self.assertEqual(self.file_free_p.owner_id, str(self.person_a.pk))
        # 版本表 v1 留痕
        self.assertTrue(
            MaterialFileVersion.objects.filter(
                material_kind="PERSON", material_id=int(data["id"]), version=1
            ).exists()
        )

    def test_create_team_success(self):
        resp = self._post(
            BASE,
            self._create_payload(
                ownerType="TEAM",
                ownerId=str(self.team_a.pk),
                materialType="TEAM_REPORT",  # allow_multiple，不与预置 TM 撞 R6
                fileIds=[self.file_wrong.pk],  # TEAM 文件
            ),
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["ownerName"], "丙一小男足队")

    def test_create_person_missing_or_deleted(self):
        resp = self._post(BASE, self._create_payload(ownerId="999999"), token=self.admin_token)
        self.assertEqual(resp.status_code, 404)
        resp2 = self._post(
            BASE, self._create_payload(ownerId=str(self.person_del.pk)), token=self.admin_token
        )
        self.assertEqual(resp2.status_code, 404)

    def test_create_team_missing(self):
        resp = self._post(
            BASE,
            self._create_payload(ownerType="TEAM", ownerId=str(self.team_soft.pk), materialType="TEAM_HONOR"),
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 404)

    def test_create_file_missing(self):
        resp = self._post(BASE, self._create_payload(fileIds=["no-such-file"]), token=self.admin_token)
        self.assertEqual(resp.status_code, 404)

    def test_create_file_owner_type_mismatch(self):
        """PERSON 归属材料误用 TEAM 文件 → 409。"""
        resp = self._post(
            BASE, self._create_payload(fileIds=[self.file_wrong.pk]), token=self.admin_token
        )
        self.assertEqual(resp.status_code, 409)

    def test_create_file_bound_to_other_person(self):
        """文件已绑定 person_c，用于 person_a 材料 → 409（不得接受任意文件 ID）。"""
        resp = self._post(
            BASE, self._create_payload(fileIds=[self.file_bound_c.pk]), token=self.admin_token
        )
        self.assertEqual(resp.status_code, 409)

    def test_create_file_ids_must_be_single(self):
        """fileIds 2 个 → 400（冻结单文件承载）。"""
        resp = self._post(
            BASE,
            self._create_payload(fileIds=[self.file_free_p.pk, self.file_bound_p.pk]),
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_r6_duplicate_blocked(self):
        """person_a 已有 type_person 有效材料 → 409。"""
        resp = self._post(
            BASE,
            self._create_payload(materialType="ATHLETE_CERTIFICATE"),
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("不允许重复", resp.json()["message"])

    def test_create_r6_allow_multiple_ok(self):
        """allow_multiple=True 类型可重复提交。"""
        resp = self._post(BASE, self._create_payload(), token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        resp2 = self._post(BASE, self._create_payload(title="第二份"), token=self.admin_token)
        self.assertEqual(resp2.status_code, 200)

    def test_create_need_audit_false_directly_approved(self):
        resp = self._post(
            BASE, self._create_payload(materialType="FREE_PASS"), token=self.admin_token
        )
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")

    def test_create_extra_field_rejected(self):
        """前端未定义的多余字段 → StrictSchema 400（契约防漂移）。"""
        resp = self._post(BASE, self._create_payload(hackField="x"), token=self.admin_token)
        self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------------ 审核

    def test_review_approve_success_and_audit_log(self):
        version = self._mver(self.pm_pending)
        resp = self._post(
            f"{BASE}/{self.pm_pending.pk}/review",
            {"decision": "APPROVE", "version": version},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")
        self.pm_pending.refresh_from_db()
        self.assertEqual(self.pm_pending.status, enums.MaterialStatus.APPROVED)
        self.assertIsNone(self.pm_pending.audited_by)  # 底座账号 ID 映射未定，恒空
        self.assertIsNotNone(self.pm_pending.audited_at)
        log = MaterialAuditLog.objects.get(material_id=self.pm_pending.pk, material_kind="PERSON")
        self.assertEqual(log.from_status, "PENDING")
        self.assertEqual(log.to_status, "APPROVED")

    def test_review_reject_requires_reason(self):
        resp = self._post(
            f"{BASE}/{self.pm_pending.pk}/review",
            {"decision": "REJECT", "version": self._mver(self.pm_pending)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)

    def test_review_reject_success(self):
        version = self._mver(self.tm_pending)
        resp = self._post(
            f"{BASE}/{self.tm_pending.pk}/review",
            {"decision": "REJECT", "reason": "材料模糊不清", "version": version},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["reviewStatus"], "REJECTED")
        self.assertEqual(data["reviewReason"], "材料模糊不清")
        log = MaterialAuditLog.objects.get(material_id=self.tm_pending.pk, material_kind="TEAM")
        self.assertEqual(log.to_status, "REJECTED")
        self.assertEqual(log.comment, "材料模糊不清")

    def test_review_non_pending_rejected(self):
        """非法状态转换：APPROVED 材料再审核 → 409。"""
        resp = self._post(
            f"{BASE}/{self.pm_approved.pk}/review",
            {"decision": "REJECT", "reason": "重复审核", "version": self._mver(self.pm_approved)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)

    def test_review_version_conflict(self):
        resp = self._post(
            f"{BASE}/{self.pm_pending.pk}/review",
            {"decision": "APPROVE", "version": self._mver(self.pm_pending) - 1},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.pm_pending.refresh_from_db()
        self.assertEqual(self.pm_pending.status, enums.MaterialStatus.PENDING)

    def test_review_missing_version(self):
        resp = self._post(
            f"{BASE}/{self.pm_pending.pk}/review",
            {"decision": "APPROVE"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)

    def test_review_material_missing(self):
        resp = self._post(
            f"{BASE}/999999/review",
            {"decision": "APPROVE", "version": 1},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------ 软删

    def test_remove_soft_deletes(self):
        version = self._mver(self.pm_pending)
        resp = self._delete(f"{BASE}/{self.pm_pending.pk}", token=self.admin_token, version=version)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["data"]["success"])
        self.pm_pending.refresh_from_db()
        self.assertTrue(self.pm_pending.is_deleted)
        self.assertIsNotNone(self.pm_pending.deleted_at)
        # 列表不再出现
        resp2 = self._get(
            BASE, token=self.admin_token, query={"ownerType": "PERSON", "ownerId": self.person_a.pk}
        )
        self.assertEqual(resp2.json()["data"]["total"], 0)

    def test_remove_missing_version(self):
        resp = self._delete(f"{BASE}/{self.pm_pending.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 400)

    def test_remove_version_conflict(self):
        resp = self._delete(
            f"{BASE}/{self.pm_pending.pk}", token=self.admin_token, version=self._mver(self.pm_pending) - 1
        )
        self.assertEqual(resp.status_code, 409)
        self.pm_pending.refresh_from_db()
        self.assertFalse(self.pm_pending.is_deleted)

    def test_remove_repeat_returns_404(self):
        version = self._mver(self.pm_pending)
        self._delete(f"{BASE}/{self.pm_pending.pk}", token=self.admin_token, version=version)
        resp = self._delete(f"{BASE}/{self.pm_pending.pk}", token=self.admin_token, version=version)
        self.assertEqual(resp.status_code, 404)

    def test_remove_material_missing(self):
        resp = self._delete(f"{BASE}/999999", token=self.admin_token, version=1)
        self.assertEqual(resp.status_code, 404)

    # ------------------------------------------------------------ 事务回滚与审计

    def test_create_rollback_on_version_log_failure(self):
        """MaterialFileVersion 落库失败 → 材料行与文件归属回填一并回滚。"""
        payload = self._create_payload(materialType="FREE_PASS")
        with mock.patch.object(
            MaterialFileVersion.objects, "create", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                services_material_create(payload, self.admin)
        self.assertFalse(
            PersonMaterial.objects.filter(remark="市联赛获奖证书").exists()
        )
        self.file_free_p.refresh_from_db()
        self.assertEqual(self.file_free_p.owner_id, "")  # 归属回填已回滚

    def test_audit_logs_written(self):
        self._post(BASE, self._create_payload(), token=self.admin_token)
        self._post(
            f"{BASE}/{self.pm_pending.pk}/review",
            {"decision": "APPROVE", "version": self._mver(self.pm_pending)},
            token=self.admin_token,
        )
        self.pm_pending.refresh_from_db()
        self._delete(
            f"{BASE}/{self.pm_pending.pk}", token=self.admin_token, version=self._mver(self.pm_pending)
        )
        actions = set(
            AuditLog.objects.filter(operator_account_id=self.admin.pk).values_list("action", flat=True)
        )
        self.assertTrue({"material.create", "material.review", "material.remove"} <= actions)

    # ------------------------------------ 多态歧义与写链原子性（批次 C 复验修复）

    def _make_dual_id_materials(self, person_deleted=False):
        """构造两表同主键（pk=300）材料对：人员材料+球队材料。

        类型取 allow_multiple（AWARD/TEAM_REPORT），文件沿用无主/TEAM 文件，
        均为直接落库夹具，不触发创建链路校验。
        """
        pm = PersonMaterial.objects.create(
            pk=300,
            person=self.person_c,
            type=self.type_multi,
            file_id=self.file_free_p.pk,
            file_name="双命中人员材料.pdf",
            status=enums.MaterialStatus.PENDING,
            remark="双命中-人员",
            is_deleted=person_deleted,
            deleted_at=timezone.now() if person_deleted else None,
        )
        tm = TeamMaterial.objects.create(
            pk=300,
            team=self.team_a,
            type=self.type_team_multi,
            file_id=self.file_wrong.pk,
            file_name="双命中球队材料.pdf",
            status=enums.MaterialStatus.PENDING,
            remark="双命中-球队",
        )
        return pm, tm

    def test_review_same_id_both_tables_returns_409_no_mutation(self):
        """修复①：两表同 id 有效行 → 审核 409，两行状态均不变、无新增审核日志。"""
        pm, tm = self._make_dual_id_materials()
        resp = self._post(
            f"{BASE}/300/review",
            {"decision": "APPROVE", "version": 1},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertIn("同时存在于人员材料与球队材料", resp.json()["message"])
        pm.refresh_from_db()
        tm.refresh_from_db()
        self.assertEqual(pm.status, enums.MaterialStatus.PENDING)
        self.assertEqual(tm.status, enums.MaterialStatus.PENDING)
        self.assertIsNone(pm.audited_at)
        self.assertIsNone(tm.audited_at)
        # 双命中禁止更新：两表（两种 material_kind）均无新增审核日志
        self.assertFalse(MaterialAuditLog.objects.filter(material_id=300).exists())

    def test_remove_same_id_both_tables_returns_409_no_mutation(self):
        """修复①：两表同 id 有效行 → 删除 409，两行均未被软删。"""
        pm, tm = self._make_dual_id_materials()
        resp = self._delete(f"{BASE}/300", token=self.admin_token, version=1)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        pm.refresh_from_db()
        tm.refresh_from_db()
        self.assertFalse(pm.is_deleted)
        self.assertIsNone(pm.deleted_at)
        self.assertFalse(tm.is_deleted)
        self.assertIsNone(tm.deleted_at)

    def test_same_id_person_soft_deleted_resolves_valid_team(self):
        """同 id 人员材料已软删、球队材料有效 → 解析为球队材料并审核成功。

        现有软删过滤规则：两表解析均只取 is_deleted=False 行（_active_queryset，
        与列表同规则），软删行不参与双命中判定——不改规则，仅固化行为。
        """
        pm, tm = self._make_dual_id_materials(person_deleted=True)
        version = self._mver(tm)
        resp = self._post(
            f"{BASE}/300/review",
            {"decision": "APPROVE", "version": version},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["ownerName"], "丙一小男足队")  # 解析到 TEAM
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")
        pm.refresh_from_db()
        self.assertTrue(pm.is_deleted)  # 人员材料原样保留软删态
        self.assertEqual(pm.status, enums.MaterialStatus.PENDING)
        log = MaterialAuditLog.objects.get(material_id=300, material_kind="TEAM")
        self.assertEqual(log.to_status, "APPROVED")
        self.assertFalse(
            MaterialAuditLog.objects.filter(material_id=300, material_kind="PERSON").exists()
        )

    def test_material_id_missing_both_tables_returns_404(self):
        """修复①：两表均无此 id → review/delete 均 404（不误报歧义）。"""
        resp = self._post(
            f"{BASE}/999888/review",
            {"decision": "APPROVE", "version": 1},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 404)
        resp2 = self._delete(f"{BASE}/999888", token=self.admin_token, version=1)
        self.assertEqual(resp2.status_code, 404)

    def test_create_file_bind_race_lost_returns_409_no_residue(self):
        """修复②：回填条件更新失败（影响行数=0）→ 409 且创建数据零残留。

        说明：SQLite 内存库单连接无法可靠复现真实并发写，本用例通过注入
        "UPDATE 影响行数=0" 验证条件更新失败路径与事务回滚——即前置校验与
        回填之间文件已被并发请求抢绑的竞态结局，非真实并发压力验证。
        """
        real_filter = FileRecord.objects.filter

        def racing_filter(*args, **kwargs):
            qs = real_filter(*args, **kwargs)
            qs.update = lambda *a, **kw: 0  # 竞态注入：绑定权已被并发请求取得
            return qs

        with mock.patch.object(FileRecord.objects, "filter", side_effect=racing_filter):
            resp = self._post(BASE, self._create_payload(), token=self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertIn("抢先绑定", resp.json()["message"])
        # 材料行未残留（事务整体回滚）
        self.assertFalse(PersonMaterial.objects.filter(remark="市联赛获奖证书").exists())
        # 版本留痕未残留
        self.assertFalse(MaterialFileVersion.objects.filter(file_id=self.file_free_p.pk).exists())
        # 文件归属未被占用（仍为无主，可重试）
        self.file_free_p.refresh_from_db()
        self.assertEqual(self.file_free_p.owner_id, "")

    def test_review_rollback_on_audit_log_failure(self):
        """修复③：MaterialAuditLog 落库失败 → 状态/版本整体回滚到审核前。"""
        version = self._mver(self.tm_pending)
        with mock.patch.object(
            MaterialAuditLog.objects, "create", side_effect=RuntimeError("boom")
        ):
            resp = self._post(
                f"{BASE}/{self.tm_pending.pk}/review",
                {"decision": "APPROVE", "version": version},
                token=self.admin_token,
            )
        self.assertEqual(resp.status_code, 500)  # 未处理异常 → INTERNAL_ERROR 信封
        self.assertEqual(resp.json()["code"], "INTERNAL_ERROR")
        self.tm_pending.refresh_from_db()
        self.assertEqual(self.tm_pending.status, enums.MaterialStatus.PENDING)
        self.assertIsNone(self.tm_pending.audited_at)  # 无半完成审核结果
        self.assertEqual(self.tm_pending.audit_remark, "")
        self.assertEqual(self._mver(self.tm_pending), version)  # 版本未漂移
        self.assertFalse(
            MaterialAuditLog.objects.filter(
                material_id=self.tm_pending.pk, material_kind="TEAM"
            ).exists()
        )

    # ------------------------------------------------------------ 材料详情（批次 D-2）

    def test_get_requires_auth(self):
        self.assertEqual(self._get(f"{BASE}/{self.pm_pending.pk}").status_code, 401)

    def test_get_permission_registered(self):
        from apps.common.permissions import operation_roles

        self.assertEqual(
            operation_roles("materialGet"),
            (
                "ADMIN",
                "UNIVERSITY",
                "TEAM",
                "PROVINCE_REVIEWER",
                "ATHLETE",
                "COACH",
                "CITY",
            ),
        )

    def test_get_university_scope_denied(self):
        resp = self._get(f"{BASE}/{self.pm_pending.pk}", token=self.uni_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    def test_get_detail_person_shape(self):
        resp = self._get(f"{BASE}/{self.pm_pending.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["id"], str(self.pm_pending.pk))
        self.assertEqual(data["ownerType"], "PERSON")
        self.assertEqual(data["ownerId"], str(self.person_a.pk))
        self.assertEqual(data["ownerName"], "材料甲")
        self.assertEqual(data["materialType"], "ATHLETE_CERTIFICATE")
        self.assertEqual(data["materialLabel"], "运动员等级证书")
        self.assertEqual(data["title"], "甲的证书")
        self.assertEqual(data["fileIds"], [self.file_bound_p.pk])
        self.assertEqual(data["fileName"], "已绑甲.pdf")
        self.assertEqual(data["reviewStatus"], "PENDING")
        self.assertEqual(data["version"], self._mver(self.pm_pending))
        self.assertIsNotNone(data["createdAt"])

    def test_get_detail_team_shape(self):
        resp = self._get(f"{BASE}/{self.tm_pending.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["id"], str(self.tm_pending.pk))
        self.assertEqual(data["ownerType"], "TEAM")
        self.assertEqual(data["ownerId"], str(self.team_a.pk))
        self.assertEqual(data["ownerName"], "丙一小男足队")
        self.assertEqual(data["materialType"], "TEAM_HONOR")
        self.assertEqual(data["title"], "球队荣誉")

    def test_get_missing_returns_404(self):
        resp = self._get(f"{BASE}/999888", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_get_soft_deleted_returns_404(self):
        resp = self._get(f"{BASE}/{self.pm_deleted.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_get_same_id_both_tables_returns_409(self):
        """详情与 review/remove 同口径：双表同 id 有效行 → 409 不静默择一。"""
        self._make_dual_id_materials()
        resp = self._get(f"{BASE}/300", token=self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertIn("同时存在于人员材料与球队材料", resp.json()["message"])

    # ------------------------------------------------------------ 更新（批次 D-3）

    def _update_payload(self, material, **overrides) -> dict:
        """按材料现状构造合法更新体（fileIds 默认维持原文件）。"""
        is_person = isinstance(material, PersonMaterial)
        payload = {
            "ownerType": "PERSON" if is_person else "TEAM",
            "ownerId": str(material.person_id if is_person else material.team_id),
            "materialType": material.type.code,
            "title": "更新后的材料标题",
            "fileIds": [material.file_id],
            "version": self._mver(material),
        }
        payload.update(overrides)
        return payload

    def _update(self, material_id, payload, token=None):
        return self.client.put(
            f"{BASE}/{material_id}",
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token),
        )

    def test_update_requires_auth(self):
        payload = self._update_payload(self.pm_pending)
        self.assertEqual(self._update(self.pm_pending.pk, payload).status_code, 401)

    def test_update_permission_registered(self):
        from apps.common.permissions import operation_roles

        self.assertEqual(
            operation_roles("materialUpdate"),
            (
                "ADMIN",
                "UNIVERSITY",
                "TEAM",
                "PROVINCE_REVIEWER",
                "ATHLETE",
                "COACH",
                "CITY",
            ),
        )

    def test_update_university_scope_denied(self):
        resp = self._update(
            self.pm_pending.pk, self._update_payload(self.pm_pending), self.uni_token
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    def test_update_approved_material_409(self):
        """T4-04-L13：APPROVED 材料拒绝更新（409 STATE_CONFLICT），零写入。"""
        old_remark = self.pm_approved.remark
        old_file = self.pm_approved.file_id
        old_version = self._mver(self.pm_approved)
        resp = self._update(
            self.pm_approved.pk,
            self._update_payload(self.pm_approved, title="试图改已审材料"),
            self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertIn("仅待审核", resp.json()["message"])
        # 拒绝发生在任何业务写入之前：材料字段/文件绑定/版本/审计/版本留痕零变化
        self.pm_approved.refresh_from_db()
        self.assertEqual(self.pm_approved.remark, old_remark)
        self.assertEqual(self.pm_approved.file_id, old_file)
        self.assertEqual(self._mver(self.pm_approved), old_version)
        self.assertFalse(
            MaterialAuditLog.objects.filter(
                material_kind="PERSON", material_id=self.pm_approved.pk
            ).exists()
        )
        self.assertFalse(
            MaterialFileVersion.objects.filter(
                material_kind="PERSON", material_id=self.pm_approved.pk
            ).exists()
        )

    def test_update_rejected_material_409(self):
        """T4-04-L13：REJECTED 材料拒绝更新（409 STATE_CONFLICT），零写入。"""
        pm_rejected = PersonMaterial.objects.create(
            person=self.person_b,
            type=self.type_person,
            file_id=self.file_bound_c.pk,
            file_name="丙被驳回.pdf",
            status=enums.MaterialStatus.REJECTED,
            remark="丙的驳回件",
        )
        resp = self._update(
            pm_rejected.pk,
            self._update_payload(pm_rejected, title="试图改驳回材料"),
            self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        pm_rejected.refresh_from_db()
        self.assertEqual(pm_rejected.remark, "丙的驳回件")  # 字段未变
        self.assertEqual(pm_rejected.status, enums.MaterialStatus.REJECTED)  # 状态未转换
        self.assertFalse(
            MaterialAuditLog.objects.filter(
                material_kind="PERSON", material_id=pm_rejected.pk
            ).exists()
        )

    def test_update_title_success_db_state(self):
        """正常更新标题：响应与 DB 实态一致；version 刷新；文件/状态不动。"""
        old_version = self._mver(self.pm_pending)
        resp = self._update(self.pm_pending.pk, self._update_payload(self.pm_pending), self.admin_token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["title"], "更新后的材料标题")
        self.pm_pending.refresh_from_db()
        self.assertEqual(self.pm_pending.remark, "更新后的材料标题")
        self.assertEqual(self.pm_pending.file_id, self.file_bound_p.pk)  # 文件未动
        self.assertEqual(self.pm_pending.status, enums.MaterialStatus.PENDING)  # 状态不动
        self.assertNotEqual(self._mver(self.pm_pending), old_version)  # 版本已刷新
        self.assertEqual(data["version"], self._mver(self.pm_pending))
        # 同文件更新不追加文件版本留痕
        self.assertFalse(
            MaterialFileVersion.objects.filter(
                material_kind="PERSON", material_id=self.pm_pending.pk
            ).exists()
        )
        # 操作审计已写入（from=to=PENDING，非状态变更历史）
        log = MaterialAuditLog.objects.get(material_kind="PERSON", material_id=self.pm_pending.pk)
        self.assertEqual(log.from_status, "PENDING")
        self.assertEqual(log.to_status, "PENDING")
        self.assertIn("更新材料", log.comment)

    def test_update_team_material_success(self):
        """TEAM 材料同口径：owner_pk 取 team_id，标题落 tm.remark。"""
        resp = self._update(self.tm_pending.pk, self._update_payload(self.tm_pending), self.admin_token)
        self.assertEqual(resp.status_code, 200)
        self.tm_pending.refresh_from_db()
        self.assertEqual(self.tm_pending.remark, "更新后的材料标题")
        self.assertEqual(
            MaterialAuditLog.objects.get(material_kind="TEAM", material_id=self.tm_pending.pk).to_status,
            "PENDING",
        )

    def test_update_missing_version_400(self):
        payload = self._update_payload(self.pm_pending)
        del payload["version"]
        resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 400)
        self.pm_pending.refresh_from_db()
        self.assertNotEqual(self.pm_pending.remark, "更新后的材料标题")

    def test_update_stale_version_409(self):
        payload = self._update_payload(self.pm_pending, version=self._mver(self.pm_pending) + 1)
        resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        self.pm_pending.refresh_from_db()
        self.assertEqual(self.pm_pending.remark, "甲的证书")  # 未被部分更新

    def test_update_soft_deleted_404(self):
        resp = self._update(self.pm_deleted.pk, self._update_payload(self.pm_deleted), self.admin_token)
        self.assertEqual(resp.status_code, 404)

    def test_update_missing_404(self):
        payload = self._update_payload(self.pm_pending)
        resp = self._update(999888, payload, self.admin_token)
        self.assertEqual(resp.status_code, 404)

    def test_update_same_id_both_tables_409(self):
        """双表同 id：归属不明确，拒绝前不发生任何变更/审计。"""
        self._make_dual_id_materials()
        person_material = PersonMaterial.objects.get(pk=300)
        payload = self._update_payload(person_material)
        payload["version"] = self._mver(person_material)
        resp = self._update(300, payload, self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        person_material.refresh_from_db()
        self.assertEqual(person_material.remark, "双命中-人员")  # 原值未动
        self.assertFalse(
            MaterialAuditLog.objects.filter(material_kind="PERSON", material_id=300).exists()
        )

    def test_update_owner_type_change_rejected(self):
        payload = self._update_payload(self.pm_pending, ownerType="TEAM")
        resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 400)
        self.pm_pending.refresh_from_db()
        self.assertEqual(self.pm_pending.remark, "甲的证书")

    def test_update_owner_id_change_rejected(self):
        payload = self._update_payload(self.pm_pending, ownerId=str(self.person_b.pk))
        resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 400)
        self.file_bound_p.refresh_from_db()
        self.assertEqual(self.file_bound_p.owner_id, str(self.person_a.pk))  # 文件绑定未动

    def test_update_material_type_change_rejected(self):
        payload = self._update_payload(self.pm_pending, materialType="AWARD")
        resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 400)

    def test_update_multiple_file_ids_400(self):
        payload = self._update_payload(
            self.pm_pending, fileIds=[self.file_bound_p.pk, self.file_free_p.pk]
        )
        resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 400)

    def test_update_file_missing_404(self):
        payload = self._update_payload(self.pm_pending, fileIds=["MISSING_FILE_ID"])
        resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 404)
        self.assertIn("不存在", resp.json()["message"])

    def test_update_file_owner_type_mismatch_409(self):
        """文件归属类型与材料不符（TEAM 文件绑 PERSON 材料）→ 409。"""
        payload = self._update_payload(self.pm_pending, fileIds=[self.file_wrong.pk])
        resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("归属类型", resp.json()["message"])

    def test_update_file_bound_to_other_owner_409(self):
        """文件已绑其他归属（person_c）→ 409，不抢占他人文件绑定。"""
        payload = self._update_payload(self.pm_pending, fileIds=[self.file_bound_c.pk])
        resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.file_bound_c.refresh_from_db()
        self.assertEqual(self.file_bound_c.owner_id, str(self.person_c.pk))

    def test_update_swap_file_success(self):
        """换文件成功：文件字段实态更新、无主文件原子认领、版本留痕 v1、旧绑定不动。"""
        resp = self._update(
            self.pm_pending.pk,
            self._update_payload(self.pm_pending, fileIds=[self.file_free_p.pk]),
            self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["fileIds"], [self.file_free_p.pk])
        self.pm_pending.refresh_from_db()
        self.assertEqual(self.pm_pending.file_id, self.file_free_p.pk)
        self.assertEqual(self.pm_pending.file_name, "无主人员文件.pdf")  # 真实来源 original_name
        self.assertEqual(self.pm_pending.file_ext, "pdf")
        self.file_free_p.refresh_from_db()
        self.assertEqual(self.file_free_p.owner_id, str(self.person_a.pk))  # 认领回填
        # 旧文件绑定保持（不解绑、不破坏）
        self.file_bound_p.refresh_from_db()
        self.assertEqual(self.file_bound_p.owner_id, str(self.person_a.pk))
        # 版本留痕（夹具无历史版本 → v1）
        fv = MaterialFileVersion.objects.get(
            material_kind="PERSON", material_id=self.pm_pending.pk
        )
        self.assertEqual(fv.version, 1)
        self.assertEqual(fv.file_id, self.file_free_p.pk)
        # 审计仍写入
        self.assertTrue(
            MaterialAuditLog.objects.filter(material_kind="PERSON", material_id=self.pm_pending.pk).exists()
        )

    def test_update_swap_file_race_lost_409(self):
        """认领条件更新影响行数=0 → 409 且更新零残留（同创建修复②口径）。"""
        real_filter = FileRecord.objects.filter

        def racing_filter(*args, **kwargs):
            qs = real_filter(*args, **kwargs)
            qs.update = lambda *a, **kw: 0  # 竞态注入：绑定权已被并发请求取得
            return qs

        payload = self._update_payload(self.pm_pending, fileIds=[self.file_free_p.pk])
        with mock.patch.object(FileRecord.objects, "filter", side_effect=racing_filter):
            resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        # 材料行/文件/版本/审计零残留
        self.pm_pending.refresh_from_db()
        self.assertEqual(self.pm_pending.remark, "甲的证书")
        self.assertEqual(self.pm_pending.file_id, self.file_bound_p.pk)
        self.file_free_p.refresh_from_db()
        self.assertEqual(self.file_free_p.owner_id, "")
        self.assertFalse(
            MaterialFileVersion.objects.filter(
                material_kind="PERSON", material_id=self.pm_pending.pk
            ).exists()
        )
        self.assertFalse(
            MaterialAuditLog.objects.filter(material_kind="PERSON", material_id=self.pm_pending.pk).exists()
        )

    def test_update_file_version_conflict_409(self):
        """D-3 局部修复：uk_mfv_version 唯一约束冲突 → 409 STATE_CONFLICT 且零残留。

        说明：SQLite 内存库单连接无法复现真实并发提交，本用例注入
        IntegrityError 验证转译路径与事务回滚完整性（材料行/文件认领/
        版本留痕/审计零部分写入），非真实 MySQL 并发验证。
        """

        def conflicting_create(*args, **kwargs):
            raise IntegrityError(
                "UNIQUE constraint failed: material_file_version.material_kind,"
                " material_file_version.material_id, material_file_version.version"
            )

        payload = self._update_payload(self.pm_pending, fileIds=[self.file_free_p.pk])
        with mock.patch.object(
            MaterialFileVersion.objects, "create", side_effect=conflicting_create
        ):
            resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertIn("抢先更新", resp.json()["message"])
        # 材料行回滚：标题/文件均保持原值
        self.pm_pending.refresh_from_db()
        self.assertEqual(self.pm_pending.remark, "甲的证书")
        self.assertEqual(self.pm_pending.file_id, self.file_bound_p.pk)
        # 文件认领回滚：无主文件仍无主，可重试
        self.file_free_p.refresh_from_db()
        self.assertEqual(self.file_free_p.owner_id, "")
        # 审计记录零残留（无孤立审计）
        self.assertFalse(
            MaterialAuditLog.objects.filter(
                material_kind="PERSON", material_id=self.pm_pending.pk
            ).exists()
        )

    def test_update_rollback_on_audit_log_failure(self):
        """审计落库失败 → 材料/文件认领/版本留痕整体回滚（不留孤立审计）。"""

        def failing_create(*args, **kwargs):
            raise RuntimeError("audit log down")

        payload = self._update_payload(self.pm_pending, fileIds=[self.file_free_p.pk])
        with mock.patch.object(MaterialAuditLog.objects, "create", side_effect=failing_create):
            resp = self._update(self.pm_pending.pk, payload, self.admin_token)
        self.assertEqual(resp.status_code, 500)
        self.pm_pending.refresh_from_db()
        self.assertEqual(self.pm_pending.remark, "甲的证书")  # 标题回滚
        self.assertEqual(self.pm_pending.file_id, self.file_bound_p.pk)  # 文件回滚
        self.file_free_p.refresh_from_db()
        self.assertEqual(self.file_free_p.owner_id, "")  # 认领回滚
        self.assertFalse(
            MaterialFileVersion.objects.filter(
                material_kind="PERSON", material_id=self.pm_pending.pk
            ).exists()
        )
        self.assertFalse(
            MaterialAuditLog.objects.filter(material_kind="PERSON", material_id=self.pm_pending.pk).exists()
        )
