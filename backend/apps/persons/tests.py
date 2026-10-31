"""基础数据管理接口自动化测试（SQLite 内存库，不触碰 MySQL）。

运行方式：
    $env:DATABASE_URL="sqlite:///:memory:"; python manage.py test apps.persons

覆盖：JWT 鉴权 401 / 角色 403 / 区划严格层级校验与唯一约束 / 学校双唯一约束
与 school_type 值域 / 字典类型引用删除保护 / 字典项 (type,value) 唯一与 value
禁改 / 停用规则 / 列表过滤与分页 / 统一信封 / 审计留痕。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account, AuditLog, Organization
from apps.attachments.models import FileRecord
from apps.common import enums
from apps.common.authentication import issue_access_token, make_password
from apps.common.crypto import encrypt_text, lookup_hash, mask_id_number
from apps.common.ids import new_account_id, new_file_id, new_organization_id
from apps.materials.models import MaterialType, PersonMaterial
from apps.notices.models import Message
from apps.persons.models import (
    AthleteProfile,
    CoachProfile,
    DictItem,
    DictType,
    Person,
    PersonChangeLog,
    Region,
    School,
)
from apps.teams.models import Team, TeamCoach, TeamMember

BASE = "/api/v1/base-data"
PEOPLE = "/api/v1/people"
ME_PROFILE = "/api/v1/me/profile"
ME_PROFILE_MATCH = "/api/v1/me/profile-match"
ME_PROFILE_CONFIRM = "/api/v1/me/profile-confirmation"


class BaseDataApiTests(TestCase):
    """区划 / 学校 / 字典类型 / 字典项 管理接口。"""

    @classmethod
    def setUpTestData(cls):
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="base-admin",
            display_name="基础数据管理员",
            roles=["ADMIN"],
            password=make_password("admin-pass-123"),
            enabled=True,
        )
        cls.viewer = Account.objects.create(
            id=new_account_id(),
            username="base-viewer",
            roles=["ATHLETE"],
            password=make_password("viewer-pass-123"),
            enabled=True,
        )
        cls.admin_token = issue_access_token(cls.admin)[0]
        cls.viewer_token = issue_access_token(cls.viewer)[0]

    # ------------------------------------------------------------ 请求工具

    @staticmethod
    def _auth(token):
        if not token:
            return {}
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _get(self, path, token=None, query=None):
        return self.client.get(path, data=query or {}, **self._auth(token))

    def _post(self, path, payload, token=None):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token),
        )

    def _put(self, path, payload, token=None):
        return self.client.put(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token),
        )

    def _delete(self, path, token=None):
        return self.client.delete(path, **self._auth(token))

    def _create_region(self, payload, token=None) -> dict:
        resp = self._post(f"{BASE}/regions", payload, token or self.admin_token)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]

    def _create_school(self, payload, token=None) -> dict:
        resp = self._post(f"{BASE}/schools", payload, token or self.admin_token)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]

    def _create_dict_type(self, payload, token=None) -> dict:
        resp = self._post(f"{BASE}/dict-types", payload, token or self.admin_token)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]

    def _create_dict_item(self, payload, token=None) -> dict:
        resp = self._post(f"{BASE}/dict-items", payload, token or self.admin_token)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]

    def _region_tree(self):
        """省 → 市 → 县 三级区划，返回 (province, city, county)。"""
        province = self._create_region({"name": "庚省", "code": "700000", "level": 1})
        city = self._create_region(
            {"name": "庚市", "code": "700100", "level": 2, "parentId": province["id"]}
        )
        county = self._create_region(
            {"name": "庚县", "code": "700101", "level": 3, "parentId": city["id"]}
        )
        return province, city, county

    # ------------------------------------------------------------ 鉴权与权限

    def test_missing_token_returns_401_envelope(self):
        resp = self._get(f"{BASE}/regions")
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertEqual(body["code"], "UNAUTHENTICATED")
        self.assertTrue(body["requestId"])

    def test_query_open_to_all_roles_write_admin_only(self):
        resp = self._get(f"{BASE}/regions", token=self.viewer_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], "OK")

        resp = self._post(
            f"{BASE}/regions",
            {"name": "越权省", "code": "990000", "level": 1},
            token=self.viewer_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")
        self.assertEqual(Region.objects.count(), 0)

    def test_bad_path_id_returns_400(self):
        resp = self._get(f"{BASE}/regions/abc", token=self.admin_token)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")

    def test_unknown_id_returns_404(self):
        resp = self._get(f"{BASE}/regions/99999", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    # ------------------------------------------------------------ 区划

    def test_region_create_and_detail(self):
        data = self._create_region({"name": "测试省", "code": "100000", "level": 1})
        self.assertEqual(data["level"], 1)
        self.assertIsNone(data["parentId"])
        self.assertTrue(data["isActive"])

        resp = self._get(f"{BASE}/regions/{data['id']}", token=self.viewer_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["code"], "100000")

    def test_region_hierarchy_strict_validation(self):
        province = self._create_region({"name": "甲省", "code": "110000", "level": 1})
        city = self._create_region(
            {"name": "甲市", "code": "110100", "level": 2, "parentId": province["id"]}
        )

        # 省级不允许有上级
        resp = self._post(
            f"{BASE}/regions",
            {"name": "错省", "code": "120000", "level": 1, "parentId": province["id"]},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        # 市州必须指定上级
        resp = self._post(
            f"{BASE}/regions", {"name": "孤儿市", "code": "130100", "level": 2},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        # 县的上级必须是市州，不能是省
        resp = self._post(
            f"{BASE}/regions",
            {"name": "错县", "code": "110101", "level": 3, "parentId": province["id"]},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        # 上级不存在
        resp = self._post(
            f"{BASE}/regions",
            {"name": "悬空县", "code": "110102", "level": 3, "parentId": 99999},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        # 正常：县的上级是市州
        county = self._create_region(
            {"name": "甲县", "code": "110101", "level": 3, "parentId": city["id"]}
        )
        self.assertEqual(county["parentId"], city["id"])

    def test_region_duplicate_code_conflict(self):
        self._create_region({"name": "乙省", "code": "200000", "level": 1})
        resp = self._post(
            f"{BASE}/regions", {"name": "乙省二", "code": "200000", "level": 1},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "DUPLICATE_RESOURCE")

    def test_region_update_code_immutable(self):
        province = self._create_region({"name": "丙省", "code": "300000", "level": 1})
        # code 是稳定编码：Update 模型不接受，多传即 400
        resp = self._put(
            f"{BASE}/regions/{province['id']}",
            {"name": "丙省改", "code": "399999", "sortOrder": 0, "isActive": True},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)

        resp = self._put(
            f"{BASE}/regions/{province['id']}",
            {"name": "丙省改", "sortOrder": 3, "isActive": True},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["name"], "丙省改")
        self.assertEqual(data["code"], "300000")
        self.assertEqual(data["sortOrder"], 3)

    def test_region_disable_blocked_by_active_child(self):
        province = self._create_region({"name": "丁省", "code": "400000", "level": 1})
        city = self._create_region(
            {"name": "丁市", "code": "400100", "level": 2, "parentId": province["id"]}
        )
        resp = self._delete(f"{BASE}/regions/{province['id']}", token=self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")

        # 先停用子级，再停用父级
        resp = self._delete(f"{BASE}/regions/{city['id']}", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        resp = self._delete(f"{BASE}/regions/{province['id']}", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        detail = self._get(
            f"{BASE}/regions/{province['id']}", token=self.admin_token
        ).json()["data"]
        self.assertFalse(detail["isActive"])

    def test_region_list_filters_and_pagination(self):
        for idx in range(3):
            self._create_region(
                {"name": "戊省", "code": f"500{idx}0", "level": 1, "sortOrder": idx}
            )
        province = self._create_region({"name": "己省", "code": "600000", "level": 1})
        city = self._create_region(
            {"name": "己市", "code": "600100", "level": 2, "parentId": province["id"]}
        )

        resp = self._get(
            f"{BASE}/regions", token=self.admin_token,
            query={"level": 1, "pageSize": 2, "page": 2},
        )
        body = resp.json()["data"]
        self.assertEqual(body["total"], 4)
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["pageSize"], 2)
        self.assertEqual(len(body["items"]), 2)

        resp = self._get(
            f"{BASE}/regions", token=self.admin_token,
            query={"parentId": province["id"]},
        )
        data = resp.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["code"], "600100")

        resp = self._get(
            f"{BASE}/regions", token=self.admin_token, query={"keyword": "己"}
        )
        self.assertEqual(resp.json()["data"]["total"], 2)

        self._delete(f"{BASE}/regions/{city['id']}", token=self.admin_token)
        resp = self._get(
            f"{BASE}/regions", token=self.admin_token, query={"isActive": "false"}
        )
        self.assertEqual(resp.json()["data"]["total"], 1)

    # ------------------------------------------------------------ 学校

    def test_school_create_update_and_output(self):
        _, _, county = self._region_tree()
        data = self._create_school(
            {"name": "庚一小", "schoolType": "PRIMARY", "regionId": county["id"],
             "address": "xx路1号"}
        )
        self.assertEqual(data["regionId"], county["id"])
        self.assertEqual(data["regionName"], "庚县")
        self.assertIsNone(data["code"])

        resp = self._put(
            f"{BASE}/schools/{data['id']}",
            {"name": "庚一小改", "schoolType": "JUNIOR", "regionId": county["id"],
             "address": "yy路2号", "code": "SCH001", "isActive": True},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["schoolType"], "JUNIOR")
        self.assertEqual(data["code"], "SCH001")

    def test_school_validation_errors(self):
        _, _, county = self._region_tree()
        # schoolType 值域
        resp = self._post(
            f"{BASE}/schools",
            {"name": "错类型校", "schoolType": "BAD", "regionId": county["id"]},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        # 区划不存在
        resp = self._post(
            f"{BASE}/schools",
            {"name": "悬空校", "schoolType": "PRIMARY", "regionId": 99999},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        # 区划已停用
        self._delete(f"{BASE}/regions/{county['id']}", token=self.admin_token)
        resp = self._post(
            f"{BASE}/schools",
            {"name": "停用区划校", "schoolType": "PRIMARY", "regionId": county["id"]},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)

    def test_school_unique_constraints(self):
        _, city, county = self._region_tree()
        other_county = self._create_region(
            {"name": "庚二县", "code": "700102", "level": 3, "parentId": city["id"]}
        )
        self._create_school(
            {"name": "庚一中", "schoolType": "JUNIOR", "regionId": county["id"],
             "code": "SCH100"}
        )
        # code 唯一
        resp = self._post(
            f"{BASE}/schools",
            {"name": "另一校", "schoolType": "PRIMARY", "regionId": other_county["id"],
             "code": "SCH100"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        # (name, region) 唯一
        resp = self._post(
            f"{BASE}/schools",
            {"name": "庚一中", "schoolType": "PRIMARY", "regionId": county["id"]},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        # 同名不同区划合法；多个 NULL code 合法
        self._create_school(
            {"name": "庚一中", "schoolType": "PRIMARY", "regionId": other_county["id"]}
        )
        self._create_school(
            {"name": "庚二中", "schoolType": "PRIMARY", "regionId": other_county["id"]}
        )
        self.assertEqual(School.objects.filter(code__isnull=True).count(), 2)

    def test_school_update_exclude_self_unique(self):
        _, _, county = self._region_tree()
        self._create_school(
            {"name": "甲校", "schoolType": "PRIMARY", "regionId": county["id"],
             "code": "SCH200"}
        )
        second = self._create_school(
            {"name": "乙校", "schoolType": "PRIMARY", "regionId": county["id"],
             "code": "SCH201"}
        )
        resp = self._put(
            f"{BASE}/schools/{second['id']}",
            {"name": "乙校", "schoolType": "PRIMARY", "regionId": county["id"],
             "code": "SCH200"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)

        resp = self._put(
            f"{BASE}/schools/{second['id']}",
            {"name": "乙校改", "schoolType": "PRIMARY", "regionId": county["id"],
             "code": "SCH201"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)

    def test_school_disable_and_list_filters(self):
        _, _, county = self._region_tree()
        self._create_school(
            {"name": "启用校", "schoolType": "PRIMARY", "regionId": county["id"]}
        )
        disabled = self._create_school(
            {"name": "停用校", "schoolType": "JUNIOR", "regionId": county["id"]}
        )
        resp = self._delete(f"{BASE}/schools/{disabled['id']}", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)

        resp = self._get(
            f"{BASE}/schools", token=self.admin_token, query={"isActive": "false"}
        )
        items = resp.json()["data"]["items"]
        self.assertEqual([i["name"] for i in items], ["停用校"])

        resp = self._get(
            f"{BASE}/schools", token=self.admin_token, query={"schoolType": "PRIMARY"}
        )
        self.assertEqual(resp.json()["data"]["total"], 1)

        resp = self._get(
            f"{BASE}/schools", token=self.admin_token,
            query={"regionId": county["id"], "keyword": "启用"},
        )
        self.assertEqual(resp.json()["data"]["total"], 1)

    # ------------------------------------------------------------ 字典类型

    def test_dict_type_crud_and_duplicate(self):
        data = self._create_dict_type(
            {"code": "position", "name": "场上位置", "remark": "足球位置"}
        )
        self.assertEqual(data["code"], "position")

        resp = self._post(
            f"{BASE}/dict-types", {"code": "position", "name": "重复"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "DUPLICATE_RESOURCE")

        # code 禁改：多传即 400
        resp = self._put(
            f"{BASE}/dict-types/{data['id']}",
            {"name": "场上位置改", "remark": "", "code": "position2"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)

        resp = self._put(
            f"{BASE}/dict-types/{data['id']}",
            {"name": "场上位置改", "remark": "备注改"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["name"], "场上位置改")

    def test_dict_type_delete_protection(self):
        dtype = self._create_dict_type({"code": "grade", "name": "学段"})
        # 无引用：允许物理删除
        resp = self._delete(f"{BASE}/dict-types/{dtype['id']}", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(DictType.objects.count(), 0)

        # 有引用：拒绝并保留
        dtype2 = self._create_dict_type({"code": "position", "name": "场上位置"})
        self._create_dict_item({"typeCode": "position", "value": "GK", "label": "守门员"})
        resp = self._delete(f"{BASE}/dict-types/{dtype2['id']}", token=self.admin_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertEqual(DictType.objects.count(), 1)

    def test_dict_type_list_keyword(self):
        self._create_dict_type({"code": "position", "name": "场上位置"})
        self._create_dict_type({"code": "team_category", "name": "球队组别"})
        resp = self._get(
            f"{BASE}/dict-types", token=self.admin_token, query={"keyword": "team"}
        )
        self.assertEqual(resp.json()["data"]["total"], 1)

    # ------------------------------------------------------------ 字典项

    def test_dict_item_create_and_duplicate(self):
        self._create_dict_type({"code": "position", "name": "场上位置"})
        data = self._create_dict_item(
            {"typeCode": "position", "value": "GK", "label": "守门员",
             "sortOrder": 1, "extJson": {"group": "U12"}}
        )
        self.assertEqual(data["typeCode"], "position")
        self.assertEqual(data["extJson"], {"group": "U12"})

        # 未知 typeCode
        resp = self._post(
            f"{BASE}/dict-items", {"typeCode": "nope", "value": "X", "label": "未知"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        # (type, value) 唯一
        resp = self._post(
            f"{BASE}/dict-items", {"typeCode": "position", "value": "GK",
                                   "label": "重复"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)

    def test_dict_item_update_value_immutable(self):
        self._create_dict_type({"code": "position", "name": "场上位置"})
        item = self._create_dict_item(
            {"typeCode": "position", "value": "GK", "label": "守门员"}
        )
        # value 禁改：多传即 400
        resp = self._put(
            f"{BASE}/dict-items/{item['id']}",
            {"label": "守门员改", "sortOrder": 0, "isActive": True, "value": "NEW"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)

        resp = self._put(
            f"{BASE}/dict-items/{item['id']}",
            {"label": "守门员改", "sortOrder": 2, "isActive": True,
             "extJson": {"hot": True}},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["label"], "守门员改")
        self.assertEqual(data["sortOrder"], 2)
        self.assertEqual(data["extJson"], {"hot": True})

    def test_dict_item_disable_and_options(self):
        self._create_dict_type({"code": "position", "name": "场上位置"})
        self._create_dict_item(
            {"typeCode": "position", "value": "GK", "label": "守门员", "sortOrder": 2}
        )
        self._create_dict_item(
            {"typeCode": "position", "value": "DF", "label": "后卫", "sortOrder": 1}
        )
        off = self._create_dict_item(
            {"typeCode": "position", "value": "OFF", "label": "已停用", "sortOrder": 0}
        )
        resp = self._delete(f"{BASE}/dict-items/{off['id']}", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)

        # options：仅启用项，按 sort_order 排序
        resp = self._get(
            f"{BASE}/dict-types/position/options", token=self.viewer_token
        )
        items = resp.json()["data"]["items"]
        self.assertEqual([i["value"] for i in items], ["DF", "GK"])

        # 列表过滤 typeCode + isActive
        resp = self._get(
            f"{BASE}/dict-items", token=self.admin_token,
            query={"typeCode": "position", "isActive": "false"},
        )
        self.assertEqual(resp.json()["data"]["total"], 1)

        # 未知 typeCode 的 options 返回空集（对齐 accounts dictionaryOptions 行为）
        resp = self._get(f"{BASE}/dict-types/nope/options", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 0)

    # ------------------------------------------------------------ 分页与边界

    def test_pagination_invalid_params_clamped(self):
        # 契约：分页非法值"纠正不拒绝"（common/pagination.py 非法值退回默认并 clamp 到 1..100）
        resp = self._get(f"{BASE}/regions", token=self.admin_token, query={"page": "0"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["page"], 1)
        resp = self._get(f"{BASE}/regions", token=self.admin_token, query={"page": "-1"})
        self.assertEqual(resp.json()["data"]["page"], 1)
        resp = self._get(f"{BASE}/regions", token=self.admin_token, query={"pageSize": "101"})
        self.assertEqual(resp.json()["data"]["pageSize"], 100)
        resp = self._get(f"{BASE}/regions", token=self.admin_token, query={"pageSize": "0"})
        self.assertEqual(resp.json()["data"]["pageSize"], 1)
        resp = self._get(f"{BASE}/regions", token=self.admin_token, query={"pageSize": "abc"})
        self.assertEqual(resp.json()["data"]["pageSize"], 20)  # 非数字才退默认

    def test_region_keyword_no_match_returns_empty_page(self):
        self._create_region({"name": "独省", "code": "910000", "level": 1})
        resp = self._get(f"{BASE}/regions", token=self.admin_token, query={"keyword": "不存在的名称"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual(body["items"], [])
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["page"], 1)

    def test_school_put_unknown_id_returns_404(self):
        _, _, county = self._region_tree()
        payload = {"name": "边界中学", "schoolType": "PRIMARY", "regionId": county["id"]}
        self._create_school(payload)  # 同 payload 可成功创建，证明 404 来自 id 而非 schema
        resp = self._put(f"{BASE}/schools/99999", payload, token=self.admin_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    # ------------------------------------------------------------ 审计

    def test_audit_log_written_for_write_operations(self):
        data = self._create_region({"name": "辛省", "code": "800000", "level": 1})
        self.assertTrue(
            AuditLog.objects.filter(
                action="baseData.region.create",
                object_type="Region",
                object_id=str(data["id"]),
                operator_account_id=self.admin.pk,
            ).exists()
        )
        self._delete(f"{BASE}/regions/{data['id']}", token=self.admin_token)
        self.assertTrue(
            AuditLog.objects.filter(
                action="baseData.region.disable", object_id=str(data["id"])
            ).exists()
        )


class PeopleApiTests(TestCase):
    """人员主档查询链（GET /people、POST /people/search、GET /people/{id}）。

    契约对齐 zuqiu 前端 personApi：列表传 page/pageSize/keyword/kind/reviewStatus
    （admin/PeopleManageView.vue），search 正文仅 {keyword} 且返回数组
    （PublicationsManageView/TeamCoachesView/EnrollmentDetailView）。
    """

    ID_CARD = "110101201305011234"

    @classmethod
    def setUpTestData(cls):
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="people-admin",
            display_name="人员管理员",
            roles=["ADMIN"],
            password=make_password("admin-pass-123"),
            enabled=True,
        )
        cls.uni = Account.objects.create(
            id=new_account_id(),
            username="people-uni",
            roles=["UNIVERSITY"],
            password=make_password("uni-pass-123"),
            enabled=True,
        )
        cls.admin_token = issue_access_token(cls.admin)[0]
        cls.uni_token = issue_access_token(cls.uni)[0]

        region = Region.objects.create(name="甲省", code="800000", level=1)
        cls.school = School.objects.create(
            name="甲一小", code="SCH-800100-101", school_type="PRIMARY", region=region
        )
        # schoolOrgId 映射夹具：org.code = School.code（schoolOrgId 按组织节点 ID）
        cls.org_school = Organization.objects.create(
            id=new_organization_id(),
            name="甲一小组织",
            org_type="SCHOOL",
            code="SCH-800100-101",
        )
        cls.uni.organization = cls.org_school
        cls.uni.save(update_fields=["organization"])
        team = Team.objects.create(
            team_no="T202600000001",
            name="甲一队",
            category_code="U12",
            school=cls.school,
        )
        cls.athlete = Person.objects.create(
            person_no="P202600000001",
            full_name="张小明",
            gender=enums.Gender.MALE,
            birth_date=date(2013, 5, 1),
            id_card_cipher=encrypt_text(cls.ID_CARD),
            id_card_hash=lookup_hash(cls.ID_CARD),
            id_card_masked=mask_id_number(cls.ID_CARD),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.APPROVED,
            school=cls.school,
        )
        cls.coach = Person.objects.create(
            person_no="P202600000002",
            full_name="李大 Coach",
            id_card_cipher=encrypt_text("110101198001015678"),
            id_card_hash=lookup_hash("110101198001015678"),
            id_card_masked=mask_id_number("110101198001015678"),
            person_category=enums.PersonCategory.COACH,
            audit_status=enums.AuditStatus.PENDING,
        )
        # 方案C：kinds 由扩展档案行存在性合成，fixture 同步建行
        CoachProfile.objects.create(person=cls.coach)
        AthleteProfile.objects.create(
            person=cls.athlete, grade_level="JUNIOR", student_no="G123456789"
        )
        TeamMember.objects.create(
            team=team, person=cls.athlete, joined_at=date(2026, 3, 1)
        )

    # ------------------------------------------------------------ 请求工具

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _get(self, path, token=None, query=None):
        return self.client.get(path, data=query or {}, **self._auth(token))

    def _post(self, path, payload, token=None):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token),
        )

    # ------------------------------------------------------------ 鉴权与数据范围

    def test_missing_token_returns_401(self):
        resp = self._get(PEOPLE)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "UNAUTHENTICATED")

    def test_university_sees_only_own_school_people(self):
        # UNIVERSITY 已按契约登记：范围=本校组织子树内的学校（org.code=School.code）
        resp = self._get(PEOPLE, token=self.uni_token)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["realName"], "张小明")

        # 检索同样按范围过滤：他校/无学校归属人员不命中
        resp = self._post(PEOPLE + "/search", {"keyword": "李"}, token=self.uni_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"], [])

        # 范围外详情 → 404（不泄露存在性）
        resp = self._get(f"{PEOPLE}/{self.coach.pk}", token=self.uni_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    # ------------------------------------------------------------ 列表

    def test_admin_list_maps_frontend_person_fields(self):
        resp = self._get(PEOPLE, token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual(body["total"], 2)

        # -id 排序：coach（后建）在前
        coach_row, athlete_row = body["items"]
        self.assertEqual(coach_row["realName"], "李大 Coach")
        self.assertEqual(coach_row["kinds"], ["COACH"])
        self.assertEqual(coach_row["reviewStatus"], "PENDING")
        self.assertEqual(coach_row["gender"], "UNSPECIFIED")
        # B4 派生版本：updated_at(UTC) 微秒 epoch，非占位 0
        from apps.persons.schemas import person_version

        self.assertEqual(coach_row["version"], person_version(self.coach))
        # education/coachCategory 由扩展档案 ext_json 承载（2026-10-27 修订）：
        # 无值 → None；不映射冻结列 coach_level
        self.assertIsNone(coach_row["coachCategory"])
        self.assertIsNone(coach_row["education"])

        self.assertEqual(athlete_row["realName"], "张小明")
        self.assertEqual(athlete_row["kinds"], ["ATHLETE"])
        self.assertEqual(athlete_row["maskedIdNumber"], "110101********1234")
        self.assertEqual(athlete_row["maskedStudentNumber"], "G1******89")
        self.assertEqual(athlete_row["gender"], "MALE")
        self.assertEqual(athlete_row["birthDate"], "2013-05-01")
        self.assertEqual(athlete_row["schoolOrgId"], str(self.org_school.pk))
        self.assertEqual(athlete_row["schoolName"], "甲一小")
        self.assertEqual(athlete_row["teamName"], "甲一队")
        self.assertIsNone(athlete_row["maskedPhone"])
        self.assertIsNone(athlete_row["education"])
        self.assertNotIn("avatarUrl", athlete_row)

    def test_list_filters_kind_keyword_review_status(self):
        resp = self._get(PEOPLE, token=self.admin_token, query={"kind": "ATHLETE"})
        self.assertEqual(resp.json()["data"]["total"], 1)
        self.assertEqual(resp.json()["data"]["items"][0]["realName"], "张小明")

        resp = self._get(PEOPLE, token=self.admin_token, query={"keyword": "张"})
        self.assertEqual(resp.json()["data"]["total"], 1)

        resp = self._get(
            PEOPLE, token=self.admin_token, query={"reviewStatus": "APPROVED"}
        )
        self.assertEqual(resp.json()["data"]["total"], 1)

        resp = self._get(PEOPLE, token=self.admin_token, query={"kind": "REFREE"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")

        resp = self._get(
            PEOPLE, token=self.admin_token, query={"reviewStatus": "CLOSED"}
        )
        self.assertEqual(resp.status_code, 400)

    # ------------------------------------------------------------ 检索

    def test_search_returns_plain_array(self):
        resp = self._post(PEOPLE + "/search", {"keyword": "张"}, token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], str(self.athlete.pk))

        resp = self._post(PEOPLE + "/search", {"keyword": ""}, token=self.admin_token)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")

    # ------------------------------------------------------------ 详情

    def test_get_person_detail_and_soft_deleted_404(self):
        resp = self._get(f"{PEOPLE}/{self.athlete.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["realName"], "张小明")

        Person.objects.filter(pk=self.athlete.pk).update(is_deleted=True)
        resp = self._get(f"{PEOPLE}/{self.athlete.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_get_person_bad_path_id_returns_400(self):
        resp = self._get(PEOPLE + "/abc", token=self.admin_token)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")


class PersonVersionMechanismTests(TestCase):
    """B4 乐观锁机制验证：updated_at(UTC微秒) 派生版本的可靠性证据。

    验证点：微秒分辨率可区分连续写入 / 未写时派生值稳定 / UTC 派生正确 /
    queryset.update() 是 auto_now 盲区（写链禁用约定的依据）。
    """

    @classmethod
    def setUpTestData(cls):
        cls.person = Person.objects.create(
            person_no="P202600000009",
            full_name="版本员",
            id_card_cipher=encrypt_text("110101200001010011"),
            id_card_hash=lookup_hash("110101200001010011"),
            id_card_masked=mask_id_number("110101200001010011"),
            person_category=enums.PersonCategory.ATHLETE,
        )

    def _derived(self, person=None) -> int:
        from apps.persons.schemas import person_version

        return person_version(person or Person.objects.get(pk=self.person.pk))

    def test_consecutive_saves_produce_distinct_versions(self):
        import time

        v0 = self._derived()
        self.person.save()
        v1 = self._derived()
        self.person.full_name = "版本员二"
        # Windows 系统时钟粒度约 15.6ms，两次 save 落在同一 tick 会派生同版本号；
        # 睡眠一 tick 让"连续保存版本递增"的断言可稳定验证（Linux 上无副作用）
        time.sleep(0.03)
        self.person.save()
        v2 = self._derived()
        self.assertGreater(v1, v0)
        self.assertGreater(v2, v1)
        self.assertEqual(v2 - v1 < 1_000_000, True)  # 两次写间隔 <1s，仍可区分

    def test_version_stable_across_reads(self):
        v1 = self._derived()
        v2 = self._derived()
        v3 = self._derived(Person.objects.get(pk=self.person.pk))
        self.assertEqual(v1, v2)
        self.assertEqual(v1, v3)

    def test_derivation_is_utc_epoch_microseconds(self):
        from apps.persons.schemas import person_version

        before = int(timezone.now().timestamp() * 1_000_000)
        self.person.save()
        after = int(timezone.now().timestamp() * 1_000_000)
        derived = person_version(self.person)
        self.assertGreaterEqual(derived, before - 1_000_000)
        self.assertLessEqual(derived, after + 1_000_000)

    def test_queryset_update_bypasses_auto_now(self):
        """盲区证据：queryset.update() 不刷新 updated_at → 写链必须用 instance.save()。"""
        v0 = self._derived()
        Person.objects.filter(pk=self.person.pk).update(full_name="绕过改名")
        self.person.refresh_from_db()
        self.assertEqual(self.person.full_name, "绕过改名")
        self.assertEqual(self._derived(), v0)  # 版本未变 → 证明该路径不可用于写链


class PeopleWriteApiTests(TestCase):
    """人员写链（POST/PUT/DELETE /people）：方案A 单类别 + B4 派生版本乐观锁。"""

    ID_CARD = "110101201305011234"

    @classmethod
    def setUpTestData(cls):
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="write-admin",
            roles=["ADMIN"],
            password=make_password("admin-pass-123"),
            enabled=True,
        )
        cls.uni = Account.objects.create(
            id=new_account_id(),
            username="write-uni",
            roles=["UNIVERSITY"],
            password=make_password("uni-pass-123"),
            enabled=True,
        )
        cls.admin_token = issue_access_token(cls.admin)[0]
        cls.uni_token = issue_access_token(cls.uni)[0]
        region = Region.objects.create(name="乙省", code="810000", level=1)
        cls.school = School.objects.create(
            name="乙一小", code="SCH-810100-201", school_type="PRIMARY", region=region
        )
        # schoolOrgId 映射夹具：org.code = School.code（schoolOrgId 按组织节点 ID）
        cls.org_school = Organization.objects.create(
            id=new_organization_id(),
            name="乙一小组织",
            org_type="SCHOOL",
            code="SCH-810100-201",
        )
        # 已停用学校组织节点（写入 400 用例）
        cls.org_school_disabled = Organization.objects.create(
            id=new_organization_id(),
            name="停用学校组织",
            org_type="SCHOOL",
            code="SCH-DISABLED-201",
            enabled=False,
        )
        # 类型不符节点（TEAM 不能作为 schoolOrgId）
        cls.org_team = Organization.objects.create(
            id=new_organization_id(),
            name="别队组织",
            org_type="TEAM",
            code="T209901010002",
        )

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _post(self, payload, token=None):
        return self.client.post(
            PEOPLE,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token or self.admin_token),
        )

    def _put(self, person_id, payload, token=None):
        return self.client.put(
            f"{PEOPLE}/{person_id}",
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token or self.admin_token),
        )

    def _delete(self, person_id, version=None, token=None):
        # Django test client 的 delete(data=...) 走请求体而非 query string，
        # 而 DELETE 的 version 是 Query 参数 → 手工拼 URL
        url = f"{PEOPLE}/{person_id}"
        if version is not None:
            url = f"{url}?version={version}"
        return self.client.delete(url, **self._auth(token or self.admin_token))

    def _create(self, **overrides):
        payload = {
            "kinds": ["ATHLETE"],
            "realName": "王测试",
            "idNumber": self.ID_CARD,
            "phone": "13800001111",
            "schoolOrgId": str(self.org_school.pk),
        }
        payload.update(overrides)
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"], Person.objects.get(pk=int(resp.json()["data"]["id"]))

    # ------------------------------------------------------------ 创建

    def test_create_maps_id_card_and_fills_person_no(self):
        data, person = self._create()
        self.assertEqual(data["realName"], "王测试")
        self.assertEqual(data["kinds"], ["ATHLETE"])
        self.assertEqual(data["maskedIdNumber"], "110101********1234")
        self.assertEqual(data["maskedPhone"], "138****1111")
        self.assertEqual(data["reviewStatus"], "DRAFT")
        self.assertEqual(data["schoolOrgId"], str(self.org_school.pk))
        # 身份证解析（DDL birth_date 注释口径）：第7-14位出生、第17位奇→男
        self.assertEqual(data["birthDate"], "2013-05-01")
        self.assertEqual(data["gender"], "MALE")
        self.assertEqual(person.person_category, "ATHLETE")
        self.assertNotEqual(person.id_card_cipher, self.ID_CARD)
        self.assertEqual(person.id_card_hash, lookup_hash(self.ID_CARD))
        self.assertEqual(person.person_no.startswith("P"), True)
        self.assertEqual(len(person.person_no), 13)  # P+年份4+id 8位 = 13
        self.assertTrue(
            PersonChangeLog.objects.filter(
                person=person, change_type=enums.ChangeType.CREATE
            ).exists()
        )

    def test_create_dual_kind_creates_both_profiles(self):
        """方案C（前端多选为准）：双类别经扩展档案行承载，不静默丢弃。"""
        resp = self._post(
            {"kinds": ["ATHLETE", "COACH"], "realName": "双类别", "idNumber": "110101201305015678"}
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        # 合成固定 ATHLETE→COACH 顺序，与提交顺序无关
        self.assertEqual(data["kinds"], ["ATHLETE", "COACH"])
        person = Person.objects.get(full_name="双类别")
        self.assertEqual(person.person_category, "ATHLETE")  # 主类别=提交首项
        self.assertTrue(AthleteProfile.objects.filter(person=person).exists())
        self.assertTrue(CoachProfile.objects.filter(person=person).exists())

    def test_create_duplicate_kinds_rejected(self):
        resp = self._post(
            {"kinds": ["ATHLETE", "ATHLETE"], "realName": "重复类别", "idNumber": "110101201305015678"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")

    def test_create_invalid_kind_value_rejected(self):
        resp = self._post(
            {"kinds": ["REFREE"], "realName": "非法类别", "idNumber": "110101201305015678"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_duplicate_id_card_409(self):
        self._create()
        resp = self._post(
            {"kinds": ["COACH"], "realName": "同证人", "idNumber": self.ID_CARD}
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "DUPLICATE_RESOURCE")

    def test_create_soft_deleted_duplicate_409(self):
        _, person = self._create()
        Person.objects.filter(pk=person.pk).update(is_deleted=True)
        person.refresh_from_db()
        person.is_deleted = True
        person.save()
        resp = self._post(
            {"kinds": ["COACH"], "realName": "复用尝试", "idNumber": self.ID_CARD}
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("已删除", resp.json()["message"])

    def test_create_school_invalid_400(self):
        resp = self._post(
            {
                "kinds": ["ATHLETE"],
                "realName": "错学校",
                "idNumber": "110101201305015678",
                "schoolOrgId": "999999",
            }
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")

    def test_create_school_org_disabled_returns_400(self):
        resp = self._post(
            {
                "kinds": ["ATHLETE"],
                "realName": "停用学校",
                "idNumber": "110101201305015678",
                "schoolOrgId": str(self.org_school_disabled.pk),
            }
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")

    def test_create_school_org_wrong_type_returns_400(self):
        resp = self._post(
            {
                "kinds": ["ATHLETE"],
                "realName": "类型不符",
                "idNumber": "110101201305015678",
                "schoolOrgId": str(self.org_team.pk),
            }
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")

    def test_create_requires_id_number(self):
        resp = self._post({"kinds": ["ATHLETE"], "realName": "无证人"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")

    def test_create_denied_for_university_without_school_scope(self):
        """UNIVERSITY 已按契约登记写权限点；账号未绑定学校组织 → 无可见范围 403。"""
        resp = self._post(
            {"kinds": ["ATHLETE"], "realName": "越权", "idNumber": self.ID_CARD},
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    # ------------------------------------------------------------ 更新（乐观锁）

    def test_update_success_and_change_log(self):
        _, person = self._create()
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE"],
                "realName": "王改名",
                "gender": "MALE",
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["realName"], "王改名")
        person.refresh_from_db()
        self.assertEqual(data["version"], person_version(person))
        log = PersonChangeLog.objects.filter(
            person=person, change_type=enums.ChangeType.UPDATE
        ).first()
        self.assertIsNotNone(log)
        self.assertIn(
            {"field": "realName", "old": "王测试", "new": "王改名"},
            log.field_changes,
        )

    def test_update_optional_fields_kept_when_absent(self):
        _, person = self._create()  # phone=13800001111、school 已建
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE"],
                "realName": "王保号",
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 200)
        person.refresh_from_db()
        self.assertEqual(person.phone_masked, "138****1111")  # 未传 phone → 保留
        self.assertEqual(person.school_id, self.school.pk)  # 未传 school → 保留

    def test_update_stale_version_rejected_and_not_overwritten(self):
        _, person = self._create()
        from apps.persons.schemas import person_version

        stale_version = person_version(person)
        # 另一次写入先落地（模拟并发）
        person.full_name = "并发先写"
        person.save()
        current_version = person_version(person)
        self.assertNotEqual(stale_version, current_version)

        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE"],
                "realName": "迟到的覆盖",
                "version": stale_version,
            },
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        person.refresh_from_db()
        self.assertEqual(person.full_name, "并发先写")  # 未被覆盖

    def test_update_education_coach_category_persisted_and_readable(self):
        """education/coachCategory 落扩展档案 ext_json，写后可读回（5.txt 问题一）。"""
        _, person = self._create()
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE"],
                "realName": "王学历",
                "education": "本科",
                "coachCategory": "A",
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["education"], "本科")
        self.assertEqual(data["coachCategory"], "A")
        self.assertEqual(
            AthleteProfile.objects.get(person=person).ext_json,
            {"education": "本科", "coachCategory": "A"},
        )
        # 详情链同样读得回（未持久化缺陷已闭环）
        detail = self._get_detail(person.pk).json()["data"]
        self.assertEqual(detail["education"], "本科")
        self.assertEqual(detail["coachCategory"], "A")

    # ------------------------------------------------------------ 删除（软删+乐观锁）

    def test_delete_stale_version_rejected_and_not_deleted(self):
        _, person = self._create()
        from apps.persons.schemas import person_version

        stale_version = person_version(person)
        person.full_name = "删除前改名"
        person.save()
        resp = self._delete(person.pk, version=stale_version)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        person.refresh_from_db()
        self.assertFalse(person.is_deleted)  # 未被删除

    def test_delete_missing_version_400(self):
        _, person = self._create()
        resp = self._delete(person.pk)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")

    def test_delete_protected_by_active_team_member(self):
        _, person = self._create()
        team = Team.objects.create(
            team_no="T202600000009",
            name="乙一队",
            category_code="U12",
            school=self.school,
        )
        TeamMember.objects.create(
            team=team, person=person, joined_at=date(2026, 3, 1)
        )
        from apps.persons.schemas import person_version

        resp = self._delete(person.pk, version=person_version(person))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        person.refresh_from_db()
        self.assertFalse(person.is_deleted)

    def test_delete_success_then_detail_404(self):
        _, person = self._create()
        from apps.persons.schemas import person_version

        resp = self._delete(person.pk, version=person_version(person))
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        self.assertTrue(person.is_deleted)
        self.assertIsNotNone(person.deleted_at)
        self.assertTrue(
            PersonChangeLog.objects.filter(
                person=person, change_type=enums.ChangeType.DISABLE
            ).exists()
        )
        resp = self._get_detail(person.pk)
        self.assertEqual(resp.status_code, 404)

    def _get_detail(self, person_id):
        return self.client.get(f"{PEOPLE}/{person_id}", **self._auth(self.admin_token))

    def _get_list(self, query=None):
        return self.client.get(PEOPLE, data=query or {}, **self._auth(self.admin_token))

    # ------------------------------------------------------ 方案C：类别增减

    def test_update_add_kind_creates_profile(self):
        _, person = self._create()  # ATHLETE
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE", "COACH"],
                "realName": "王测试",
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["kinds"], ["ATHLETE", "COACH"])
        self.assertTrue(CoachProfile.objects.filter(person=person).exists())

    def test_update_remove_empty_kind_deletes_profile(self):
        resp = self._post(
            {"kinds": ["ATHLETE", "COACH"], "realName": "双师", "idNumber": "110101201305017890"}
        )
        person = Person.objects.get(pk=int(resp.json()["data"]["id"]))
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {"kinds": ["ATHLETE"], "realName": "双师", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["kinds"], ["ATHLETE"])
        self.assertFalse(CoachProfile.objects.filter(person=person).exists())
        log = PersonChangeLog.objects.filter(
            person=person, change_type=enums.ChangeType.UPDATE
        ).first()
        kinds_change = [c for c in log.field_changes if c["field"] == "kinds"]
        self.assertEqual(kinds_change[0]["old"], ["ATHLETE", "COACH"])
        self.assertEqual(kinds_change[0]["new"], ["ATHLETE"])

    def test_update_remove_coach_kind_with_data_rejected_409(self):
        resp = self._post(
            {"kinds": ["ATHLETE", "COACH"], "realName": "证教练", "idNumber": "110101201305019012"}
        )
        person = Person.objects.get(full_name="证教练")
        CoachProfile.objects.filter(person=person).update(certificate_no="C2026001")
        from apps.persons.schemas import person_version

        person.refresh_from_db()  # queryset.update 不刷 updated_at，版本不受影响
        resp = self._put(
            person.pk,
            {"kinds": ["ATHLETE"], "realName": "证教练", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertTrue(CoachProfile.objects.filter(person=person).exists())

    def test_update_remove_athlete_kind_with_data_rejected_409(self):
        resp = self._post(
            {"kinds": ["ATHLETE", "COACH"], "realName": "学籍员", "idNumber": "110101201305023456"}
        )
        person = Person.objects.get(full_name="学籍员")
        AthleteProfile.objects.filter(person=person).update(student_no="G2026001")
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {"kinds": ["COACH"], "realName": "学籍员", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertTrue(AthleteProfile.objects.filter(person=person).exists())

    def test_kind_filter_matches_profile_existence(self):
        """kind 过滤与 kinds 合成同源：双类别人员在两种过滤下均命中。"""
        self._create()
        resp = self._post(
            {"kinds": ["ATHLETE", "COACH"], "realName": "兼项员", "idNumber": "110101201305034567"}
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self._get_list({"kind": "ATHLETE"})
        names = [item["realName"] for item in resp.json()["data"]["items"]]
        self.assertIn("兼项员", names)
        resp = self._get_list({"kind": "COACH"})
        names = [item["realName"] for item in resp.json()["data"]["items"]]
        self.assertIn("兼项员", names)

    # ------------------------------------------------------ 头像：先传后绑

    def test_avatar_upload_before_person_then_bind(self):
        """前端新建流程：ownerId 空上传 → 200；建人带 avatarFileId → 归属回填。"""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.attachments.services import upload as file_upload

        record = file_upload(
            self.admin,
            "PERSON",
            "",
            "AVATAR",
            SimpleUploadedFile("a.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
        )
        resp = self._post(
            {
                "kinds": ["ATHLETE"],
                "realName": "头像员",
                "idNumber": "110101201305045678",
                "avatarFileId": str(record.pk),
            }
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["avatarFileId"], str(record.pk))
        person = Person.objects.get(full_name="头像员")
        record.refresh_from_db()
        self.assertEqual(person.photo_file_id, str(record.pk))
        self.assertEqual(record.owner_id, str(person.pk))  # 先传后绑：归属回填

    def test_avatar_upload_empty_owner_denied_for_university(self):
        """空归属仍走角色兜底校验，普通角色不得借空 ownerId 绕过权限。"""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.attachments.services import upload as file_upload
        from apps.common.errors import ApiError

        with self.assertRaises(ApiError) as ctx:
            file_upload(
                self.uni,
                "PERSON",
                "",
                "AVATAR",
                SimpleUploadedFile("a.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF"),
            )
        self.assertEqual(ctx.exception.code, "FORBIDDEN")


class MyProfileApiTests(TestCase):
    """GET/PUT /me/profile 与 GET /me/profile-match（前端 MyProfileView）。

    自读对象=服务端账号 person_id 绑定档案，无客户端传入 id，无数据范围
    歧义；PUT version 必填乐观锁（2026-10-24 用户裁定，已交付）；
    profile-match 三态映射口径见 services.get_profile_match。
    """

    @classmethod
    def setUpTestData(cls):
        region = Region.objects.create(name="甲省", code="800000", level=1)
        cls.school = School.objects.create(
            name="甲一小", code="SCH-800100-301", school_type="PRIMARY", region=region
        )
        # 映射夹具：org.code = School.code（schoolOrgId 输出按组织节点 ID）
        cls.org_school = Organization.objects.create(
            id=new_organization_id(),
            name="甲一小组织",
            org_type="SCHOOL",
            code="SCH-800100-301",
        )
        cls.athlete = Person.objects.create(
            person_no="P202600000001",
            full_name="张小明",
            gender=enums.Gender.MALE,
            birth_date=date(2013, 5, 1),
            id_card_cipher=encrypt_text("110101201305011234"),
            id_card_hash=lookup_hash("110101201305011234"),
            id_card_masked=mask_id_number("110101201305011234"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.APPROVED,
            school=cls.school,
        )
        cls.coach = Person.objects.create(
            person_no="P202600000002",
            full_name="李大 Coach",
            id_card_cipher=encrypt_text("110101198001015678"),
            id_card_hash=lookup_hash("110101198001015678"),
            id_card_masked=mask_id_number("110101198001015678"),
            person_category=enums.PersonCategory.COACH,
            audit_status=enums.AuditStatus.PENDING,
        )
        # 方案C：kinds 由扩展档案行存在性合成
        AthleteProfile.objects.create(person=cls.athlete, student_no="G123456789")
        CoachProfile.objects.create(person=cls.coach)
        # profile-confirmation 主链用 DRAFT 初态档案（create_person 同初值）
        cls.draft = Person.objects.create(
            person_no="P202600000004",
            full_name="王草稿",
            id_card_cipher=encrypt_text("110101199601014444"),
            id_card_hash=lookup_hash("110101199601014444"),
            id_card_masked=mask_id_number("110101199601014444"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.DRAFT,
        )
        # 方案C：draft 为运动员类别，补建扩展档案行（kinds 合成事实源）
        AthleteProfile.objects.create(person=cls.draft)
        cls.draft_user = Account.objects.create(
            id=new_account_id(),
            username="me-draft",
            display_name="王草稿",
            roles=["ATHLETE"],
            password=make_password("me-pass-123"),
            enabled=True,
            person_id=str(cls.draft.pk),
        )
        cls.athlete_user = Account.objects.create(
            id=new_account_id(),
            username="me-athlete",
            display_name="张小明",
            roles=["ATHLETE"],
            password=make_password("me-pass-123"),
            enabled=True,
            person_id=str(cls.athlete.pk),
        )
        cls.coach_user = Account.objects.create(
            id=new_account_id(),
            username="me-coach",
            display_name="李大 Coach",
            roles=["COACH"],
            password=make_password("me-pass-123"),
            enabled=True,
            person_id=str(cls.coach.pk),
        )
        cls.unbound_user = Account.objects.create(
            id=new_account_id(),
            username="me-unbound",
            display_name="未绑定档案",
            roles=["ATHLETE"],
            password=make_password("me-pass-123"),
            enabled=True,
        )
        cls.uni_user = Account.objects.create(
            id=new_account_id(),
            username="me-uni",
            display_name="学校端",
            roles=["UNIVERSITY"],
            organization=cls.org_school,
            password=make_password("me-pass-123"),
            enabled=True,
        )
        cls.athlete_token = issue_access_token(cls.athlete_user)[0]
        cls.coach_token = issue_access_token(cls.coach_user)[0]
        cls.unbound_token = issue_access_token(cls.unbound_user)[0]
        cls.uni_token = issue_access_token(cls.uni_user)[0]
        cls.draft_token = issue_access_token(cls.draft_user)[0]

        # ---- GET /me/teams：本人现役（CURRENT）球员/教练关系球队 ----
        # city=市州（Team.region 语义为所属市州），schoolName/cityName 合成来源
        cls.city = Region.objects.create(
            name="甲州市", code="800100", level=2, parent=region
        )
        cls.team_main = Team.objects.create(
            team_no="T202600000001",
            name="甲一小男足一队",
            category_code="PRIMARY",
            school=cls.school,
            region=cls.city,
            introduction="校主力队",
        )
        TeamMember.objects.create(
            team=cls.team_main, person=cls.athlete, joined_at=date(2026, 3, 1),
            jersey_number=9,
        )
        TeamCoach.objects.create(
            team=cls.team_main, person=cls.coach,
            duty=enums.CoachDuty.HEAD_COACH, joined_at=date(2026, 3, 1),
        )
        # 历史（EXITED）关系 + 本人双关系同队（队员+教练）→ 去重且 EXITED 不计
        cls.team_exited = Team.objects.create(
            team_no="T202600000003",
            name="甲一小男足二队",
            category_code="PRIMARY",
            school=cls.school,
        )
        TeamMember.objects.create(
            team=cls.team_exited, person=cls.athlete, joined_at=date(2025, 3, 1),
            status=enums.RelationStatus.EXITED, left_at=timezone.now(),
            left_reason="毕业离队",
        )
        TeamMember.objects.create(
            team=cls.team_exited, person=cls.draft, joined_at=date(2026, 3, 3)
        )
        TeamCoach.objects.create(
            team=cls.team_exited, person=cls.draft,
            duty=enums.CoachDuty.ASSISTANT_COACH, joined_at=date(2026, 3, 3),
        )
        # 软删球队：有关系行也不应出现
        cls.team_deleted = Team.objects.create(
            team_no="T202600000004",
            name="已解散队",
            category_code="PRIMARY",
            is_deleted=True,
            deleted_at=timezone.now(),
        )
        TeamMember.objects.create(
            team=cls.team_deleted, person=cls.athlete, joined_at=date(2026, 1, 1)
        )
        # 绑定档案但无任何球队关系 → 空数组（前端 el-empty 分支）
        cls.norel = Person.objects.create(
            person_no="P202600000005",
            full_name="无关系",
            id_card_cipher=encrypt_text("110101199701015555"),
            id_card_hash=lookup_hash("110101199701015555"),
            id_card_masked=mask_id_number("110101199701015555"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.DRAFT,
        )
        cls.norel_user = Account.objects.create(
            id=new_account_id(),
            username="me-norel",
            display_name="无关系",
            roles=["ATHLETE"],
            password=make_password("me-pass-123"),
            enabled=True,
            person_id=str(cls.norel.pk),
        )
        cls.norel_token = issue_access_token(cls.norel_user)[0]

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def test_missing_token_returns_401(self):
        resp = self.client.get(ME_PROFILE)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "UNAUTHENTICATED")

    def test_athlete_reads_own_profile(self):
        resp = self.client.get(ME_PROFILE, **self._auth(self.athlete_token))
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["realName"], "张小明")
        self.assertEqual(data["kinds"], ["ATHLETE"])
        self.assertEqual(data["reviewStatus"], "APPROVED")
        self.assertEqual(data["maskedIdNumber"], "110101********1234")
        self.assertEqual(data["maskedStudentNumber"], "G1******89")
        self.assertEqual(data["schoolOrgId"], str(self.org_school.pk))
        from apps.persons.schemas import person_version

        self.assertEqual(data["version"], person_version(self.athlete))

    def test_coach_reads_own_profile(self):
        resp = self.client.get(ME_PROFILE, **self._auth(self.coach_token))
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["realName"], "李大 Coach")
        self.assertEqual(data["kinds"], ["COACH"])
        self.assertEqual(data["reviewStatus"], "PENDING")

    def test_account_without_person_returns_404(self):
        resp = self.client.get(ME_PROFILE, **self._auth(self.unbound_token))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_soft_deleted_own_person_returns_404(self):
        self.athlete.is_deleted = True
        self.athlete.save(update_fields=["is_deleted"])
        resp = self.client.get(ME_PROFILE, **self._auth(self.athlete_token))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_non_self_service_role_denied(self):
        # myProfile 仅登记 ATHLETE/COACH：管理角色走 /people 查询链，不走自读
        resp = self.client.get(ME_PROFILE, **self._auth(self.uni_token))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    # ---------------------------------------------------------- PUT /me/profile

    def _put(self, payload, token=None):
        return self.client.put(
            ME_PROFILE,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token or self.athlete_token),
        )

    def test_put_missing_token_returns_401(self):
        resp = self.client.put(
            ME_PROFILE, data="{}", content_type="application/json"
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "UNAUTHENTICATED")

    def test_put_account_without_person_returns_404(self):
        from apps.persons.schemas import person_version

        resp = self._put(
            {"realName": "无人", "version": person_version(self.athlete)},
            token=self.unbound_token,
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_put_non_self_service_role_denied(self):
        from apps.persons.schemas import person_version

        resp = self._put(
            {"realName": "越权", "version": person_version(self.athlete)},
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_put_success_updates_own_profile(self):
        """合法更新：落库+审计+ChangeLog+派生 version 与持久化一致；响应与 GET 同构。"""
        from apps.persons.schemas import person_version

        current = person_version(self.athlete)
        resp = self._put(
            {
                "realName": "张小改",
                "gender": "MALE",
                "birthDate": "2013-05-01",
                "schoolName": "甲一小",
                "education": "本科",
                "coachCategory": "主教练",
                "version": current,
            }
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.full_name, "张小改")

        # version 与持久化结果一致（updated_at 前进 → 派生 version 单调）
        new_version = person_version(self.athlete)
        self.assertEqual(data["version"], new_version)
        self.assertGreater(new_version, current)

        # 响应与 GET /me/profile 同构：kinds 合成/脱敏/reviewStatus 一致
        self.assertEqual(data["kinds"], ["ATHLETE"])
        get_data = self.client.get(
            ME_PROFILE, **self._auth(self.athlete_token)
        ).json()["data"]
        self.assertEqual(get_data["kinds"], data["kinds"])
        self.assertEqual(get_data["version"], data["version"])
        self.assertEqual(get_data["realName"], "张小改")

        # 审计与变更留痕
        self.assertTrue(
            AuditLog.objects.filter(
                action="person.self_update", object_id=str(self.athlete.pk)
            ).exists()
        )
        log = PersonChangeLog.objects.filter(
            person=self.athlete, change_type=enums.ChangeType.UPDATE
        ).latest("id")
        fields = {c["field"]: c for c in log.field_changes}
        self.assertEqual(fields["realName"]["old"], "张小明")
        self.assertEqual(fields["realName"]["new"], "张小改")
        # 问题#4：姓名属复审字段，APPROVED 档案改后自动退回 PENDING，
        # reason 追加后缀（遗留风险1：留痕兜底感知）
        self.assertEqual(log.reason, "本人更新档案（复审字段变更，退回待审）")

        # education/coachCategory 落扩展档案 ext_json（预留扩展列）→ 提交后读得回；
        # schoolName 为派生只读字段，回传原值放行
        self.assertEqual(data["education"], "本科")
        self.assertEqual(data["coachCategory"], "主教练")
        self.assertEqual(data["schoolName"], "甲一小")
        self.assertEqual(get_data["education"], "本科")
        self.assertEqual(get_data["coachCategory"], "主教练")
        self.assertEqual(
            AthleteProfile.objects.get(person=self.athlete).ext_json,
            {"education": "本科", "coachCategory": "主教练"},
        )
        # 落库字段进入变更留痕
        self.assertEqual(fields["education"]["new"], "本科")
        self.assertEqual(fields["coachCategory"]["new"], "主教练")

    def test_put_changed_school_name_returns_422(self):
        """schoolName 派生只读：改成别值 → 422 明确业务错误（不再 200 OK 但未保存）。"""
        from apps.persons.schemas import person_version

        version_before = person_version(self.athlete)
        resp = self._put(
            {
                "realName": "张小改",
                "schoolName": "乙一小",
                "version": version_before,
            }
        )
        self.assertEqual(resp.status_code, 422, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], "BUSINESS_VALIDATION_FAILED")
        self.assertEqual([item["field"] for item in body["data"]], ["schoolName"])
        self.assertTrue(body["data"][0]["reason"])
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.full_name, "张小明")
        self.assertEqual(person_version(self.athlete), version_before)

    def test_put_education_omitted_keeps_previous_value(self):
        """education/coachCategory 未传=保留原值（不清空，与 /people 可选字段同口径）。"""
        from apps.persons.schemas import person_version

        coach_ext = CoachProfile.objects.get(person=self.coach)
        coach_ext.ext_json = {"education": "硕士"}
        coach_ext.save(update_fields=["ext_json"])

        resp = self._put(
            {"realName": "李大改", "version": person_version(self.coach)},
            token=self.coach_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["education"], "硕士")
        self.assertEqual(
            CoachProfile.objects.get(person=self.coach).ext_json, {"education": "硕士"}
        )

    def test_put_missing_version_rejected_no_update(self):
        from apps.persons.schemas import person_version

        stale_free_version = person_version(self.athlete)
        resp = self._put({"realName": "无版本", "gender": "MALE"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.full_name, "张小明")
        self.assertEqual(person_version(self.athlete), stale_free_version)
        self.assertFalse(
            PersonChangeLog.objects.filter(
                person=self.athlete, change_type=enums.ChangeType.UPDATE
            ).exists()
        )

    def test_put_extra_field_rejected_cannot_target_other_person(self):
        """StrictSchema extra=forbid：提交他人 id/personId 均被拒，更新目标只由服务端解析。"""
        from apps.persons.schemas import person_version

        for key in ("id", "personId"):
            resp = self._put(
                {
                    "realName": "越权改名",
                    key: str(self.coach.pk),
                    "version": person_version(self.athlete),
                }
            )
            self.assertEqual(resp.status_code, 400, key)
            self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.coach.refresh_from_db()
        self.assertEqual(self.coach.full_name, "李大 Coach")  # 他人档案未被指定
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.full_name, "张小明")  # 本人档案未被改名

    def test_put_invalid_gender_rejected_no_update(self):
        from apps.persons.schemas import person_version

        before = person_version(self.athlete)
        resp = self._put({"realName": "张小明", "gender": "X", "version": before})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.athlete.refresh_from_db()
        self.assertEqual(person_version(self.athlete), before)
        self.assertFalse(
            PersonChangeLog.objects.filter(
                person=self.athlete, change_type=enums.ChangeType.UPDATE
            ).exists()
        )

    def test_put_stale_version_409_no_overwrite(self):
        from apps.persons.schemas import person_version

        stale = person_version(self.athlete)
        self.athlete.full_name = "新近改名"
        self.athlete.save()
        logs_before = PersonChangeLog.objects.filter(
            person=self.athlete, change_type=enums.ChangeType.UPDATE
        ).count()
        resp = self._put(
            {"realName": "旧请求覆盖", "gender": "MALE", "version": stale}
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.full_name, "新近改名")  # 原数据未被旧请求覆盖
        self.assertEqual(
            PersonChangeLog.objects.filter(
                person=self.athlete, change_type=enums.ChangeType.UPDATE
            ).count(),
            logs_before,
        )

    def test_put_concurrent_old_version_cannot_overwrite_new(self):
        """并发语义：新版本提交后，持旧版本的请求不得覆盖已提交数据。"""
        from apps.persons.schemas import person_version

        v1 = person_version(self.athlete)
        first = self._put({"realName": "并发一", "gender": "MALE", "version": v1})
        self.assertEqual(first.status_code, 200, first.content)
        second = self._put({"realName": "并发二", "gender": "MALE", "version": v1})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["code"], "VERSION_CONFLICT")
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.full_name, "并发一")

    def test_put_kinds_not_changeable_via_self(self):
        """kinds 多选语义保持，但身份变更走管理员链路：本人提交 kinds=额外字段拒绝。"""
        from apps.persons.schemas import person_version

        resp = self._put(
            {
                "realName": "张小明",
                "kinds": ["ATHLETE", "COACH"],
                "version": person_version(self.athlete),
            }
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.athlete.refresh_from_db()
        self.assertFalse(CoachProfile.objects.filter(person=self.athlete).exists())

    def test_put_coach_profile_kinds_synthesis_consistent(self):
        """教练账号更新后 kinds 合成与 GET 一致（方案C 行存在性事实源不变）。"""
        from apps.persons.schemas import person_version

        resp = self._put(
            {"realName": "李教练改", "gender": "MALE", "version": person_version(self.coach)},
            token=self.coach_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["kinds"], ["COACH"])
        get_data = self.client.get(ME_PROFILE, **self._auth(self.coach_token)).json()["data"]
        self.assertEqual(get_data["kinds"], data["kinds"])
        self.assertEqual(get_data["version"], data["version"])

    def test_put_soft_deleted_own_person_returns_404(self):
        from apps.persons.schemas import person_version

        self.coach.is_deleted = True
        self.coach.save(update_fields=["is_deleted"])
        resp = self._put(
            {"realName": "改删档", "version": person_version(self.coach)},
            token=self.coach_token,
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    # ------------------------------------------------- GET /me/profile-match

    def _get_match(self, token=None):
        return self.client.get(ME_PROFILE_MATCH, **self._auth(token))

    def test_match_missing_token_returns_401(self):
        resp = self._get_match()
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "UNAUTHENTICATED")

    def test_match_unbound_account_returns_no_match(self):
        resp = self._get_match(self.unbound_token)
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["result"], "NO_MATCH")
        self.assertNotIn("personId", data)
        self.assertNotIn("maskedIdNumber", data)

    def test_match_approved_returns_matched_to_self(self):
        resp = self._get_match(self.athlete_token)
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["result"], "MATCHED_TO_SELF")
        self.assertEqual(data["personId"], str(self.athlete.pk))
        # maskedIdNumber 与 GET /me/profile 同源（冻结列 id_card_masked）
        self.assertEqual(data["maskedIdNumber"], "110101********1234")

    def test_match_pending_returns_review_required_without_details(self):
        """绑定档案未通过复核：只返回 REVIEW_REQUIRED，不暴露候选人详情（文档规则）。"""
        resp = self._get_match(self.coach_token)
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["result"], "REVIEW_REQUIRED")
        self.assertNotIn("personId", data)
        self.assertNotIn("maskedIdNumber", data)

    def test_match_rejected_returns_review_required(self):
        rejected = Person.objects.create(
            person_no="P202600000003",
            full_name="王驳回",
            id_card_cipher=encrypt_text("110101199001019090"),
            id_card_hash=lookup_hash("110101199001019090"),
            id_card_masked=mask_id_number("110101199001019090"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.REJECTED,
        )
        rejected_user = Account.objects.create(
            id=new_account_id(),
            username="me-rejected",
            display_name="王驳回",
            roles=["ATHLETE"],
            password=make_password("me-pass-123"),
            enabled=True,
            person_id=str(rejected.pk),
        )
        resp = self._get_match(issue_access_token(rejected_user)[0])
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["result"], "REVIEW_REQUIRED")
        self.assertNotIn("personId", data)
        self.assertNotIn("maskedIdNumber", data)

    def test_match_soft_deleted_person_returns_no_match(self):
        self.athlete.is_deleted = True
        self.athlete.save(update_fields=["is_deleted"])
        resp = self._get_match(self.athlete_token)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["result"], "NO_MATCH")

    def test_match_illegal_binding_value_returns_no_match(self):
        """绑定值非法属绑定失效=NO_MATCH（接口无参数，非请求错误）。"""
        Account.objects.filter(pk=self.athlete_user.pk).update(person_id="not-a-number")
        resp = self._get_match(self.athlete_token)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["result"], "NO_MATCH")

    def test_match_non_self_service_role_denied(self):
        # profileMatch 仅登记 ATHLETE/COACH，与 myProfileGet 同口径
        resp = self._get_match(self.uni_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    # ------------------------------------------ POST /me/profile-confirmation

    def _post_confirm(self, payload, token=None):
        return self.client.post(
            ME_PROFILE_CONFIRM,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token or self.draft_token),
        )

    def test_confirm_missing_token_returns_401(self):
        resp = self.client.post(ME_PROFILE_CONFIRM, data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "UNAUTHENTICATED")

    def test_confirm_unbound_account_returns_403_binding_required(self):
        """未绑定 → 403 PROFILE_BINDING_REQUIRED（文档 profileConfirm 定稿口径，非 404）。"""
        resp = self._post_confirm({"version": 1}, token=self.unbound_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "PROFILE_BINDING_REQUIRED")

    def test_confirm_soft_deleted_person_returns_403_binding_required(self):
        """软删档案=绑定失效 → 403 PROFILE_BINDING_REQUIRED。"""
        self.draft.is_deleted = True
        self.draft.save(update_fields=["is_deleted"])
        from apps.persons.schemas import person_version

        resp = self._post_confirm({"version": person_version(self.draft)})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "PROFILE_BINDING_REQUIRED")

    def test_confirm_draft_success_moves_to_pending(self):
        """主链：DRAFT→PENDING 提交复核 + ChangeLog + 审计 + 响应与 GET 同构 + version 前进。"""
        from apps.persons.schemas import person_version

        before = person_version(self.draft)
        resp = self._post_confirm({"version": before})
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.audit_status, enums.AuditStatus.PENDING)
        self.assertEqual(data["reviewStatus"], "PENDING")
        self.assertEqual(data["realName"], "王草稿")
        self.assertEqual(data["kinds"], ["ATHLETE"])
        # version 与持久化结果一致且前进
        self.assertEqual(data["version"], person_version(self.draft))
        self.assertGreater(person_version(self.draft), before)
        # 状态流转留痕
        log = PersonChangeLog.objects.filter(
            person=self.draft, change_type=enums.ChangeType.UPDATE
        ).latest("id")
        self.assertEqual(log.reason, "本人确认档案")
        entry = next(c for c in log.field_changes if c["field"] == "audit_status")
        self.assertEqual(entry["old"], "DRAFT")
        self.assertEqual(entry["new"], "PENDING")
        self.assertTrue(
            AuditLog.objects.filter(
                action="person.self_confirm", object_id=str(self.draft.pk)
            ).exists()
        )

    def test_confirm_missing_version_rejected_no_change(self):
        resp = self._post_confirm({})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.audit_status, enums.AuditStatus.DRAFT)
        self.assertFalse(
            PersonChangeLog.objects.filter(
                person=self.draft, change_type=enums.ChangeType.UPDATE
            ).exists()
        )

    def test_confirm_stale_version_409_no_state_change(self):
        """过期 version → 409，档案保持 DRAFT 不被旧请求推进。"""
        from apps.persons.schemas import person_version

        stale = person_version(self.draft)
        self.draft.full_name = "王草稿改名"
        self.draft.save()
        resp = self._post_confirm({"version": stale})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.audit_status, enums.AuditStatus.DRAFT)
        self.assertEqual(self.draft.full_name, "王草稿改名")

    def test_confirm_approved_returns_state_conflict(self):
        """APPROVED 重复确认 → 409（前端按钮已禁用该状态，后端同样防护）。"""
        from apps.persons.schemas import person_version

        resp = self._post_confirm(
            {"version": person_version(self.athlete)}, token=self.athlete_token
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.audit_status, enums.AuditStatus.APPROVED)

    def test_confirm_rejected_resubmit_moves_to_pending(self):
        """REJECTED → 本人再次确认 → PENDING（问题#2 2026-10-31 裁定 c：驳回后重提）。

        闭环"驳回→修改→重提→再审"；不强制先修改档案（系统无法可靠判定
        实质修改）；ChangeLog reason 区分重提场景。
        """
        from apps.persons.schemas import person_version

        rejected = Person.objects.create(
            person_no="P202600000003",
            full_name="王驳回",
            id_card_cipher=encrypt_text("110101199001019090"),
            id_card_hash=lookup_hash("110101199001019090"),
            id_card_masked=mask_id_number("110101199001019090"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.REJECTED,
        )
        rejected_user = Account.objects.create(
            id=new_account_id(),
            username="me-rejected-confirm",
            display_name="王驳回",
            roles=["ATHLETE"],
            password=make_password("me-pass-123"),
            enabled=True,
            person_id=str(rejected.pk),
        )
        resp = self._post_confirm(
            {"version": person_version(rejected)},
            token=issue_access_token(rejected_user)[0],
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["reviewStatus"], "PENDING")
        rejected.refresh_from_db()
        self.assertEqual(rejected.audit_status, enums.AuditStatus.PENDING)
        log = PersonChangeLog.objects.filter(
            person=rejected, change_type=enums.ChangeType.UPDATE
        ).latest("id")
        self.assertEqual(log.reason, "本人确认档案（驳回后重提）")
        entry = next(c for c in log.field_changes if c["field"] == "audit_status")
        self.assertEqual(entry["old"], "REJECTED")
        self.assertEqual(entry["new"], "PENDING")

    def test_confirm_pending_is_idempotent_no_new_changelog(self):
        """PENDING 重复确认 → 幂等成功：状态不变、无新 ChangeLog（实现选择在案）。"""
        from apps.persons.schemas import person_version

        logs_before = PersonChangeLog.objects.filter(
            person=self.coach, change_type=enums.ChangeType.UPDATE
        ).count()
        resp = self._post_confirm(
            {"version": person_version(self.coach)}, token=self.coach_token
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["reviewStatus"], "PENDING")
        self.coach.refresh_from_db()
        self.assertEqual(self.coach.audit_status, enums.AuditStatus.PENDING)
        self.assertEqual(
            PersonChangeLog.objects.filter(
                person=self.coach, change_type=enums.ChangeType.UPDATE
            ).count(),
            logs_before,
        )

    def test_confirm_extra_field_rejected_cannot_target_other(self):
        """StrictSchema extra=forbid：提交 id 等额外字段被拒，确认对象只由服务端解析。"""
        from apps.persons.schemas import person_version

        resp = self._post_confirm(
            {"id": str(self.coach.pk), "version": person_version(self.draft)}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.coach.refresh_from_db()
        self.assertEqual(self.coach.audit_status, enums.AuditStatus.PENDING)  # 他人未受影响

    def test_confirm_non_self_service_role_denied(self):
        from apps.persons.schemas import person_version

        resp = self._post_confirm(
            {"version": person_version(self.draft)}, token=self.uni_token
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    # ---- GET /me/teams（契约清单 B4）----

    ME_TEAMS = "/api/v1/me/teams"

    def test_my_teams_missing_token_returns_401(self):
        resp = self.client.get(self.ME_TEAMS)
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "UNAUTHENTICATED")

    def test_my_teams_unbound_account_returns_empty_list(self):
        """未绑定 personId 的 ATHLETE/COACH → 空列表（个人中心空态，不拒绝）。"""
        resp = self.client.get(self.ME_TEAMS, **self._auth(self.unbound_token))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"], [])

    def test_my_teams_university_returns_own_school_teams(self):
        """UNIVERSITY 返回本校组织子树内球队，响应字段与 ATHLETE 路径一致。"""
        resp = self.client.get(self.ME_TEAMS, **self._auth(self.uni_token))
        self.assertEqual(resp.status_code, 200, resp.content)
        teams = resp.json()["data"]
        # 本校两支队（软删队不在范围）；按 created_at,id 排序
        self.assertEqual(
            [t["id"] for t in teams],
            [str(self.team_main.pk), str(self.team_exited.pk)],
        )
        self.assertEqual(teams[0]["schoolName"], "甲一小")
        self.assertEqual(teams[0]["status"], "APPROVED")
        # 字段集合必须与本人视图完全一致
        athlete_teams = self.client.get(
            self.ME_TEAMS, **self._auth(self.athlete_token)
        ).json()["data"]
        self.assertEqual(set(teams[0].keys()), set(athlete_teams[0].keys()))

    def test_my_teams_athlete_current_team_brief(self):
        from apps.persons.schemas import person_version

        resp = self.client.get(self.ME_TEAMS, **self._auth(self.athlete_token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], "OK")
        teams = resp.json()["data"]
        # 仅现役关系：EXITED 历史/软删队均不返回
        self.assertEqual([t["id"] for t in teams], [str(self.team_main.pk)])
        team = teams[0]
        self.assertEqual(team["name"], "甲一小男足一队")
        self.assertEqual(team["category"], "PRIMARY")
        self.assertEqual(team["schoolName"], "甲一小")
        self.assertEqual(team["cityName"], "甲州市")
        self.assertEqual(team["intro"], "校主力队")
        self.assertEqual(team["coachName"], "李大 Coach")  # 现役主教练合成
        self.assertEqual(team["memberCount"], 1)
        self.assertEqual(team["status"], "APPROVED")  # audit_status 直出（Q2 默认）
        self.team_main.refresh_from_db()
        self.assertEqual(team["version"], person_version(self.team_main))
        self.assertTrue(team["createdAt"].startswith("20"))
        # 无承载/无直链字段不输出（前端 optional 且页面未消费）
        for absent in (
            "orgId",
            "cityOrgId",
            "shortName",
            "campusName",
            "gender",
            "flagUrl",
            "emblemUrl",
            "groupPhotoUrl",
        ):
            self.assertNotIn(absent, team)

    def test_my_teams_coach_relation(self):
        resp = self.client.get(self.ME_TEAMS, **self._auth(self.coach_token))
        self.assertEqual(resp.status_code, 200)
        teams = resp.json()["data"]
        self.assertEqual([t["id"] for t in teams], [str(self.team_main.pk)])

    def test_my_teams_dual_relation_deduplicated(self):
        """draft 对 team_exited 同时有队员+教练关系 → 去重 1 条。"""
        resp = self.client.get(self.ME_TEAMS, **self._auth(self.draft_token))
        self.assertEqual(resp.status_code, 200)
        teams = resp.json()["data"]
        self.assertEqual([t["id"] for t in teams], [str(self.team_exited.pk)])
        # memberCount 仅计 CURRENT：draft 现役 1 人（athlete EXITED 不计）
        self.assertEqual(teams[0]["memberCount"], 1)
        self.assertIsNone(teams[0]["coachName"])  # 无现役主教练
        self.assertIsNone(teams[0]["cityName"])  # 未挂区划

    def test_my_teams_empty_when_no_relations(self):
        resp = self.client.get(self.ME_TEAMS, **self._auth(self.norel_token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"], [])


class PersonReviewApiTests(TestCase):
    """POST /people/{id}/review 学校端档案审批（问题#2，2026-10-31 裁定 a/b/c）。

    权限 personReview=(UNIVERSITY, ADMIN)、市州/省不参与（裁定 a，同材料
    方案 i）；仅 PENDING 可审、DRAFT 须本人先确认（裁定 b）；REJECT 必填
    原因；REJECTED 重提=本人再次确认回 PENDING（裁定 c）。
    """

    @classmethod
    def setUpTestData(cls):
        region = Region.objects.create(name="丙省", code="820000", level=1)
        cls.school = School.objects.create(
            name="丙一小", code="SCH-820100-401", school_type="PRIMARY", region=region
        )
        # 映射夹具：org.code = School.code（schoolOrgId 按组织节点 ID）
        cls.org_school = Organization.objects.create(
            id=new_organization_id(),
            name="丙一小组织",
            org_type="SCHOOL",
            code="SCH-820100-401",
        )
        other_school = School.objects.create(
            name="丙二小", code="SCH-820100-402", school_type="PRIMARY", region=region
        )
        cls.org_school_other = Organization.objects.create(
            id=new_organization_id(),
            name="丙二小组织",
            org_type="SCHOOL",
            code="SCH-820100-402",
        )
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="review-admin",
            roles=["ADMIN"],
            password=make_password("review-pass-123"),
            enabled=True,
        )
        cls.uni = Account.objects.create(
            id=new_account_id(),
            username="review-uni",
            roles=["UNIVERSITY"],
            organization=cls.org_school,
            password=make_password("review-pass-123"),
            enabled=True,
        )
        # 同校第二个学校端账号：防自审用例的"他人审核"正例
        cls.uni_second = Account.objects.create(
            id=new_account_id(),
            username="review-uni-second",
            roles=["UNIVERSITY"],
            organization=cls.org_school,
            password=make_password("review-pass-123"),
            enabled=True,
        )
        cls.uni_other = Account.objects.create(
            id=new_account_id(),
            username="review-uni-other",
            roles=["UNIVERSITY"],
            organization=cls.org_school_other,
            password=make_password("review-pass-123"),
            enabled=True,
        )
        cls.city = Account.objects.create(
            id=new_account_id(),
            username="review-city",
            roles=["CITY"],
            password=make_password("review-pass-123"),
            enabled=True,
        )
        cls.admin_token = issue_access_token(cls.admin)[0]
        cls.uni_token = issue_access_token(cls.uni)[0]
        cls.uni_second_token = issue_access_token(cls.uni_second)[0]
        cls.uni_other_token = issue_access_token(cls.uni_other)[0]
        cls.city_token = issue_access_token(cls.city)[0]

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _create_person(self, **overrides):
        """走 POST /people 建档（DRAFT 起步），返回 (响应data, Person行)。

        token 参数指定建档账号（问题#5 防自审用例需区分创建者与审核者，
        默认 admin；问题#5 裁定②后"admin 建档 + admin 审核"即自审 403）。
        """
        token = overrides.pop("_token", self.admin_token)
        payload = {
            "kinds": ["ATHLETE"],
            "realName": "赵审批",
            "idNumber": "110101201305017777",
            "schoolOrgId": str(self.org_school.pk),
        }
        payload.update(overrides)
        resp = self.client.post(
            PEOPLE,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        return data, Person.objects.get(pk=int(data["id"]))

    def _confirm_as_self(self, person):
        """本人确认（DRAFT→PENDING 提交复核；REJECTED→PENDING 重提）。

        username 随机后缀：同一 person 在重提闭环等场景会二次确认，
        避免账号名撞 UNIQUE 约束。
        """
        from apps.persons.schemas import person_version

        user = Account.objects.create(
            id=new_account_id(),
            username=f"review-self-{person.pk}-{new_account_id()[:8]}",
            display_name=person.full_name,
            roles=["ATHLETE"],
            password=make_password("review-pass-123"),
            enabled=True,
            person_id=str(person.pk),
        )
        return self.client.post(
            ME_PROFILE_CONFIRM,
            data=json.dumps({"version": person_version(person)}),
            content_type="application/json",
            **self._auth(issue_access_token(user)[0]),
        )

    def _pending_person(self, **create_overrides):
        """前置：建档 → 本人确认 → PENDING（create_overrides 透传建档账号）。"""
        _, person = self._create_person(**create_overrides)
        resp = self._confirm_as_self(person)
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        return person

    def _review(self, person_id, payload, token=None):
        return self.client.post(
            f"{PEOPLE}/{person_id}/review",
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token or self.uni_token),
        )

    # ------------------------------------------------------------ 主链

    def test_review_approve_success_full_chain(self):
        """主链：建档 DRAFT → 本人确认 PENDING → 学校端 APPROVE → APPROVED。"""
        from apps.persons.schemas import person_version

        person = self._pending_person()
        before = person_version(person)
        resp = self._review(str(person.pk), {"decision": "APPROVE", "version": before})
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["reviewStatus"], "APPROVED")
        person.refresh_from_db()
        self.assertEqual(data["version"], person_version(person))
        self.assertGreater(person_version(person), before)
        # AUDIT 留痕（old=PENDING/new=APPROVED）+ API 层审计
        log = PersonChangeLog.objects.filter(
            person=person, change_type=enums.ChangeType.AUDIT
        ).latest("id")
        self.assertEqual(log.reason, "审批通过")
        entry = next(c for c in log.field_changes if c["field"] == "audit_status")
        self.assertEqual(entry["old"], "PENDING")
        self.assertEqual(entry["new"], "APPROVED")
        self.assertTrue(
            AuditLog.objects.filter(
                action="person.review", object_id=str(person.pk)
            ).exists()
        )

    def test_review_reject_with_reason_success(self):
        """REJECT 带原因 → REJECTED；ChangeLog 留存审核意见。"""
        from apps.persons.schemas import person_version

        person = self._pending_person()
        resp = self._review(
            str(person.pk),
            {
                "decision": "REJECT",
                "reason": "姓名与学籍不一致",
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "REJECTED")
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.REJECTED)
        log = PersonChangeLog.objects.filter(
            person=person, change_type=enums.ChangeType.AUDIT
        ).latest("id")
        self.assertEqual(log.reason, "姓名与学籍不一致")

    def test_review_reject_without_reason_400(self):
        """REJECT 必填原因（同 material_review 口径）；version 用真实值，
        确保 400 来自 reason 校验而非乐观锁（服务层先 ensure_version）。"""
        from apps.persons.schemas import person_version

        person = self._pending_person()
        resp = self._review(
            str(person.pk),
            {"decision": "REJECT", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.PENDING)

    # ------------------------------------------------------------ 权限与范围

    def test_review_out_of_scope_school_403(self):
        """他校 UNIVERSITY 账号 → 403 DATA_SCOPE_DENIED（写链范围校验）。"""
        person = self._pending_person()
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": 0},
            token=self.uni_other_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.PENDING)

    def test_review_city_forbidden_by_role(self):
        """CITY 不在 personReview 角色集（裁定 a）→ 403 FORBIDDEN。"""
        person = self._pending_person()
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": 0},
            token=self.city_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_review_admin_global_can_review(self):
        """全局 ADMIN 在角色集内且数据范围不受限 → 可审本校档案。

        问题#5 后建档账号改为 uni（原 admin 建档 + admin 审核即自审 403，
        由 test_review_admin_creator_forbidden 单独覆盖）。
        """
        from apps.persons.schemas import person_version

        person = self._pending_person(_token=self.uni_token)
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": person_version(person)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")

    # -------------------------------------------------------- 防自审自批

    def test_review_creator_forbidden(self):
        """问题#5：审核自己创建的档案 → 403 FORBIDDEN，状态不变。

        追溯来源=AuditLog person.create（POST /people 唯一建档入口）。
        """
        from apps.persons.schemas import person_version

        person = self._pending_person(_token=self.uni_token)
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": person_version(person)},
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body["code"], "FORBIDDEN")
        self.assertEqual(body["message"], "不得审核自己创建或提交的人员档案")
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.PENDING)

    def test_review_admin_creator_forbidden(self):
        """问题#5 裁定②：ADMIN 走同一审核入口同受自审限制。"""
        from apps.persons.schemas import person_version

        person = self._pending_person()  # admin 建档
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": person_version(person)},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.PENDING)

    def test_review_other_same_school_account_allowed(self):
        """同校其他学校端账号可审（限制不禁止同校互审，非一刀切禁审）。"""
        from apps.persons.schemas import person_version

        person = self._pending_person(_token=self.uni_token)
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": person_version(person)},
            token=self.uni_second_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")

    def test_review_latest_submitter_forbidden(self):
        """问题#5：审核"最近一次提交"者本人档案 → 403（提交者=多角色账号）。

        账号同时持 UNIVERSITY（本校）与 ATHLETE（绑定档案）角色，本人确认
        提交后审核 → 命中 person.self_confirm 最近提交记录 → 拒绝。
        """
        from apps.persons.schemas import person_version

        _, person = self._create_person(_token=self.uni_second_token)
        hybrid = Account.objects.create(
            id=new_account_id(),
            username=f"review-hybrid-{person.pk}",
            display_name=person.full_name,
            roles=["UNIVERSITY", "ATHLETE"],
            organization=self.org_school,
            person_id=str(person.pk),
            password=make_password("review-pass-123"),
            enabled=True,
        )
        hybrid_token = issue_access_token(hybrid)[0]
        resp = self.client.post(
            ME_PROFILE_CONFIRM,
            data=json.dumps({"version": person_version(person)}),
            content_type="application/json",
            **self._auth(hybrid_token),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": person_version(person)},
            token=hybrid_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.PENDING)

    def test_review_earlier_submitter_allowed_after_resubmit(self):
        """多次提交取最近一次（裁定核实项④）：早期提交者（现为审核员）
        在他人重提后不再命中最近提交记录 → 允许审核。"""
        from apps.persons.schemas import person_version

        _, person = self._create_person(_token=self.uni_second_token)
        # 第一次提交：多角色账号（UNIVERSITY+ATHLETE，本校）= 早期提交者
        hybrid = Account.objects.create(
            id=new_account_id(),
            username=f"review-hybrid-early-{person.pk}",
            display_name=person.full_name,
            roles=["UNIVERSITY", "ATHLETE"],
            organization=self.org_school,
            person_id=str(person.pk),
            password=make_password("review-pass-123"),
            enabled=True,
        )
        hybrid_token = issue_access_token(hybrid)[0]
        resp = self.client.post(
            ME_PROFILE_CONFIRM,
            data=json.dumps({"version": person_version(person)}),
            content_type="application/json",
            **self._auth(hybrid_token),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        # 驳回 → 另一绑定账号（纯 ATHLETE）重提 = 最近提交者
        person.refresh_from_db()
        resp = self._review(
            str(person.pk),
            {
                "decision": "REJECT",
                "reason": "信息有误",
                "version": person_version(person),
            },
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        resp = self.client.post(
            ME_PROFILE_CONFIRM,
            data=json.dumps({"version": person_version(person)}),
            content_type="application/json",
            **self._auth(self._bind_new_account(person)),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        # 早期提交者（hybrid）此时非创建者、非最近提交者 → 允许
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": person_version(person)},
            token=hybrid_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")

    def _bind_new_account(self, person):
        """为档案新建绑定账号（重提场景避免账号名撞 UNIQUE），返回 token。"""
        user = Account.objects.create(
            id=new_account_id(),
            username=f"review-resub-{person.pk}-{new_account_id()[:8]}",
            display_name=person.full_name,
            roles=["ATHLETE"],
            password=make_password("review-pass-123"),
            enabled=True,
            person_id=str(person.pk),
        )
        return issue_access_token(user)[0]

    def test_review_without_audit_log_allowed(self):
        """缺日志（seed/历史存量）→ 无可比较操作者身份，不拦截（裁定核实项⑤：
        不得因缺日志绕过已有保护，但也不虚构身份拒绝）；数据范围/状态机/乐观锁
        照常生效。"""
        from apps.persons.schemas import person_version

        person = Person.objects.create(
            person_no="P202600000901",
            full_name="钱无日志",
            school=self.school,
            id_card_cipher=encrypt_text("110101201305019001"),
            id_card_hash=lookup_hash("110101201305019001"),
            id_card_masked=mask_id_number("110101201305019001"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.PENDING,
        )
        self.assertFalse(
            AuditLog.objects.filter(object_type="Person", object_id=str(person.pk)).exists()
        )
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")

    # ------------------------------------------------------------ 状态机

    def test_review_draft_state_conflict(self):
        """裁定 b：DRAFT 不可直接审，须本人先提交复核。"""
        from apps.persons.schemas import person_version

        _, person = self._create_person()
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.DRAFT)

    def test_review_approved_terminal_409(self):
        """APPROVED 终态不可再审。"""
        from apps.persons.schemas import person_version

        person = self._pending_person()
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.APPROVED)

        resp = self._review(
            str(person.pk), {"decision": "APPROVE", "version": person_version(person)}
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertIn("仅待复核", resp.json()["message"])

    def test_review_rejected_terminal_409(self):
        """REJECTED 终态不可再审；重提路径=本人再次确认回 PENDING。"""
        from apps.persons.schemas import person_version

        person = self._pending_person()
        resp = self._review(
            str(person.pk),
            {
                "decision": "REJECT",
                "reason": "材料不齐",
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.REJECTED)

        resp = self._review(
            str(person.pk), {"decision": "APPROVE", "version": person_version(person)}
        )
        self.assertEqual(resp.status_code, 409)
        # 重提闭环：本人再次确认 → PENDING → 可再审
        resp = self._confirm_as_self(person)
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.PENDING)
        resp = self._review(
            str(person.pk),
            {"decision": "APPROVE", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")

    def test_review_stale_version_409(self):
        """过期 version → 409 VERSION_CONFLICT，状态不变。"""
        from apps.persons.schemas import person_version

        person = self._pending_person()
        stale = person_version(person)
        person.full_name = "赵审批改名"
        person.save()
        resp = self._review(str(person.pk), {"decision": "APPROVE", "version": stale})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.PENDING)
        self.assertEqual(person.full_name, "赵审批改名")

    def test_review_missing_version_400(self):
        """version 缺失 → 400 统一校验错误（StrictSchema 必填）。"""
        person = self._pending_person()
        resp = self._review(str(person.pk), {"decision": "APPROVE"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")


class PersonReReviewApiTests(TestCase):
    """问题#4：APPROVED 档案复审字段变更自动退回 PENDING（2026-10-31 裁定）。

    字段集 realName/birthDate/kinds/personCategory/school/coachCategory 实际
    变更（old!=new）→ APPROVED 退回 PENDING；education 不触发；DRAFT/PENDING/
    REJECTED 不动；同值回传（表单全量回传）幂等不触发。
    附带修复回归：birthDate ISO 字符串与 date 同值比较（_coerce_date）。
    """

    @classmethod
    def setUpTestData(cls):
        region = Region.objects.create(name="丁省", code="830000", level=1)
        cls.school = School.objects.create(
            name="丁一小", code="SCH-830100-501", school_type="PRIMARY", region=region
        )
        cls.org_school = Organization.objects.create(
            id=new_organization_id(),
            name="丁一小组织",
            org_type="SCHOOL",
            code="SCH-830100-501",
        )
        other_school = School.objects.create(
            name="丁二小", code="SCH-830100-502", school_type="PRIMARY", region=region
        )
        cls.org_school_other = Organization.objects.create(
            id=new_organization_id(),
            name="丁二小组织",
            org_type="SCHOOL",
            code="SCH-830100-502",
        )
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="rereview-admin",
            roles=["ADMIN"],
            password=make_password("rereview-pass-123"),
            enabled=True,
        )
        cls.admin_token = issue_access_token(cls.admin)[0]

        def _person(no, name, id_suffix, status, with_school=True):
            row = Person.objects.create(
                person_no=f"P2026000000{no}",
                full_name=name,
                gender=enums.Gender.MALE,
                birth_date=date(2013, 5, 1),
                id_card_cipher=encrypt_text(f"11010120130501{id_suffix}"),
                id_card_hash=lookup_hash(f"11010120130501{id_suffix}"),
                id_card_masked=mask_id_number(f"11010120130501{id_suffix}"),
                person_category=enums.PersonCategory.ATHLETE,
                audit_status=status,
                school=cls.school if with_school else None,
            )
            AthleteProfile.objects.create(person=row)
            return row

        cls.approved = _person("101", "钱复审", "8888", enums.AuditStatus.APPROVED)
        cls.pending = _person("102", "孙待审", "8899", enums.AuditStatus.PENDING)
        cls.draft = _person("103", "周草稿", "8800", enums.AuditStatus.DRAFT)
        # 双类别档案（kinds 排序豁免用例）：类别集合相同仅顺序变化不触发复审
        cls.dual = _person("104", "吴双类别", "8877", enums.AuditStatus.APPROVED)
        CoachProfile.objects.create(person=cls.dual)
        cls.own = Account.objects.create(
            id=new_account_id(),
            username="rereview-own",
            display_name="钱复审",
            roles=["ATHLETE"],
            password=make_password("rereview-pass-123"),
            enabled=True,
            person_id=str(cls.approved.pk),
        )
        cls.own_token = issue_access_token(cls.own)[0]
        # 学校端账号（站内信接收方验证用）：属档案所属学校组织
        cls.school_user = Account.objects.create(
            id=new_account_id(),
            username="rereview-school",
            display_name="丁一审校员",
            roles=["UNIVERSITY"],
            organization=cls.org_school,
            password=make_password("rereview-pass-123"),
            enabled=True,
        )
        # 停用的学校端账号：不应收到站内信（错误接收人排除用）
        cls.school_user_disabled = Account.objects.create(
            id=new_account_id(),
            username="rereview-school-disabled",
            display_name="丁一停用账号",
            roles=["UNIVERSITY"],
            organization=cls.org_school,
            password=make_password("rereview-pass-123"),
            enabled=False,
        )

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _put_person(self, person, **overrides):
        """PUT /people/{id}（admin）：默认全量原值回传（幂等基线）。"""
        from apps.persons.schemas import person_version

        payload = {
            "kinds": ["ATHLETE"],
            "realName": person.full_name,
            "birthDate": person.birth_date.isoformat(),
            "version": person_version(person),
        }
        payload.update(overrides)
        return self.client.put(
            f"{PEOPLE}/{person.pk}",
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(self.admin_token),
        )

    def _put_own(self, person, **overrides):
        """PUT /me/profile（本人）：默认全量原值回传（幂等基线）。"""
        from apps.persons.schemas import person_version

        payload = {
            "realName": person.full_name,
            "gender": "MALE",
            "birthDate": person.birth_date.isoformat(),
            "version": person_version(person),
        }
        payload.update(overrides)
        return self.client.put(
            ME_PROFILE,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(self.own_token),
        )

    def _audit_status_entries(self, person):
        """最新一条 UPDATE ChangeLog 中的 auditStatus 条目（无则空列表）。"""
        log = PersonChangeLog.objects.filter(
            person=person, change_type=enums.ChangeType.UPDATE
        ).latest("id")
        return [c for c in (log.field_changes or []) if c["field"] == "auditStatus"]

    # ------------------------------------------------------------ 管理员入口

    def test_admin_update_realname_change_demotes_approved(self):
        """APPROVED + 实际改姓名 → 退回 PENDING；ChangeLog 含退回条目。"""
        resp = self._put_person(self.approved, realName="钱复审改")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "PENDING")
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.audit_status, enums.AuditStatus.PENDING)
        entries = self._audit_status_entries(self.approved)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["old"], "APPROVED")
        self.assertEqual(entries[0]["new"], "PENDING")
        # 遗留风险1：退回留痕 reason 明确后缀（changeReason 未传 → 纯后缀）
        log = PersonChangeLog.objects.filter(
            person=self.approved, change_type=enums.ChangeType.UPDATE
        ).latest("id")
        self.assertEqual(log.reason, "（复审字段变更，退回待审）")

    def test_admin_update_school_change_demotes_approved(self):
        """APPROVED + 实际改学校 → 退回 PENDING（归属变更须重审）。"""
        resp = self._put_person(
            self.approved, schoolOrgId=str(self.org_school_other.pk)
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "PENDING")

    def test_admin_update_unchanged_values_no_demote(self):
        """全量原值回传 → 无任何实际变更 → 保持 APPROVED（幂等）。"""
        resp = self._put_person(self.approved)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.audit_status, enums.AuditStatus.APPROVED)

    def test_admin_update_phone_only_no_demote(self):
        """phone 不在复审字段集 → 保持 APPROVED。"""
        resp = self._put_person(self.approved, phone="13900001111")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")

    def test_admin_update_pending_realname_stays_pending(self):
        """PENDING 改姓名 → 保持 PENDING，无 auditStatus 退回条目。"""
        resp = self._put_person(self.pending, realName="孙待审改")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "PENDING")
        self.assertEqual(self._audit_status_entries(self.pending), [])

    def test_admin_update_kinds_reorder_only_no_demote(self):
        """遗留风险2：kinds 集合相同仅顺序变化 → 不触发复审（保持 APPROVED）。"""
        resp = self._put_person(self.dual, kinds=["COACH", "ATHLETE"])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")
        self.dual.refresh_from_db()
        self.assertEqual(self.dual.audit_status, enums.AuditStatus.APPROVED)
        self.assertEqual(self._audit_status_entries(self.dual), [])

    def test_admin_update_kinds_remove_demotes(self):
        """kinds 集合实质变化（移除 COACH，空档案行可移除）→ 退回 PENDING。"""
        resp = self._put_person(self.dual, kinds=["ATHLETE"])
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "PENDING")

    # ------------------------------------------------------------ 站内信通知（遗留风险1）

    def test_admin_update_demote_notifies_self_and_school(self):
        """退回时站内信通知本人（绑定账号）与学校端（学校组织账号）。"""
        resp = self._put_person(self.approved, realName="钱复审改")
        self.assertEqual(resp.status_code, 200, resp.content)
        own_msgs = Message.objects.filter(recipient_account_id=self.own.pk)
        # 精确一条：同请求不重复发送
        self.assertEqual(own_msgs.count(), 1)
        self.assertEqual(own_msgs.first().title, "人员档案已退回待审核")
        self.assertEqual(own_msgs.first().business_type, "PERSON")
        self.assertEqual(own_msgs.first().business_id, str(self.approved.pk))
        school_msgs = Message.objects.filter(recipient_account_id=self.school_user.pk)
        self.assertEqual(school_msgs.count(), 1)
        self.assertEqual(school_msgs.first().title, "本校人员档案退回待审核")
        self.assertIn("钱复审改", school_msgs.first().body)
        # 错误接收人排除：管理员（未绑档案/无组织）与停用的学校端账号均不收
        self.assertFalse(
            Message.objects.filter(recipient_account_id=self.admin.pk).exists()
        )
        self.assertFalse(
            Message.objects.filter(
                recipient_account_id=self.school_user_disabled.pk
            ).exists()
        )

    def test_own_update_demote_notifies_self_and_school(self):
        """本人入口退回同样通知：本人一条 + 学校端一条（同事务原子落库）。"""
        resp = self._put_own(self.approved, realName="钱复审自改")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            Message.objects.filter(
                recipient_account_id=self.own.pk, title="人员档案已退回待审核"
            ).count(),
            1,
        )
        self.assertEqual(
            Message.objects.filter(recipient_account_id=self.school_user.pk).count(), 1
        )

    def test_no_notification_when_not_demoted(self):
        """未退回不发消息：全原值幂等（不触发）与 PENDING 变更（非 APPROVED）均不发。"""
        self._put_person(self.approved)
        self._put_person(self.pending, realName="孙待审改")
        self.assertEqual(Message.objects.count(), 0)

    # ------------------------------------------------------------ 本人入口

    def test_own_update_realname_change_demotes_approved(self):
        """本人改姓名 → APPROVED 退回 PENDING（本人纠错后须重审）。"""
        resp = self._put_own(self.approved, realName="钱复审改")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "PENDING")
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.audit_status, enums.AuditStatus.PENDING)

    def test_own_update_education_only_no_demote(self):
        """education 不在复审字段集 → 保持 APPROVED。"""
        resp = self._put_own(self.approved, education="本科")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["reviewStatus"], "APPROVED")

    def test_admin_update_draft_realname_stays_draft(self):
        """DRAFT 改姓名 → 保持 DRAFT（本在待审轨道前段，不误动）。"""
        resp = self._put_person(self.draft, realName="周草稿改")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.audit_status, enums.AuditStatus.DRAFT)
        self.assertEqual(self._audit_status_entries(self.draft), [])


class DossierApiTests(TestCase):
    """§6.8 档案聚合（批次 D-1）：POST /selection-dossiers/search、
    GET /selection-dossiers/{athleteId}。

    契约依据：接口文档 L1736-1773 + §7 CandidateDossier/DossierSearch（L4692-4718）。
    - 角色 ADMIN/PROVINCE_REVIEWER（文档 L1740/L1762）；数据范围由
      apply_person_scope 按角色/组织范围裁决（未绑定组织 → 无可见学校范围）；
    - 检索资格：文档 L4708 仅运动员档案（AthleteProfile 行存在）；
    - athlete 复用 person_out（脱敏同源）、teams 复用 list_own_teams 现役
      正式关系（EXITED 不并入）、materials 复用 material_list/material_out
      （未软删）；enrollments/matchRecords 契约可选 + 读模型未落地 → 空数组；
    - eventId/groupId 行为契约未规定（D-A 待决策）→ 非空 400 明确未开放。
    """

    SEARCH = "/api/v1/selection-dossiers/search"
    GET_BASE = "/api/v1/selection-dossiers"
    ID_CARD = "110101201305011234"

    @classmethod
    def setUpTestData(cls):
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="dossier-admin",
            display_name="档案管理员",
            roles=["ADMIN"],
            password=make_password("admin-pass-123"),
            enabled=True,
        )
        cls.prov = Account.objects.create(
            id=new_account_id(),
            username="dossier-prov",
            roles=["PROVINCE_REVIEWER"],
            password=make_password("prov-pass-123"),
            enabled=True,
        )
        cls.team_acct = Account.objects.create(
            id=new_account_id(),
            username="dossier-team",
            roles=["TEAM"],
            password=make_password("team-pass-123"),
            enabled=True,
        )
        cls.admin_token = issue_access_token(cls.admin)[0]
        cls.prov_token = issue_access_token(cls.prov)[0]
        cls.team_token = issue_access_token(cls.team_acct)[0]

        region = Region.objects.create(name="乙省", code="810000", level=1)
        cls.school = School.objects.create(
            name="乙二中", school_type="JUNIOR", region=region
        )
        cls.team_a = Team.objects.create(
            team_no="T202600000201",
            name="乙二A队",
            category_code="U13",
            school=cls.school,
        )
        cls.team_b = Team.objects.create(
            team_no="T202600000202", name="乙二B队", category_code="U15"
        )

        # 运动员 A：现役 A 队 + 历史 B 队（EXITED）+ 1 有效材料 + 1 软删材料
        cls.athlete = Person.objects.create(
            person_no="P202600000201",
            full_name="王候选",
            gender=enums.Gender.MALE,
            birth_date=date(2012, 8, 8),
            id_card_cipher=encrypt_text(cls.ID_CARD),
            id_card_hash=lookup_hash(cls.ID_CARD),
            id_card_masked=mask_id_number(cls.ID_CARD),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.APPROVED,
            school=cls.school,
        )
        AthleteProfile.objects.create(
            person=cls.athlete, grade_level="JUNIOR", student_no="G201200001"
        )
        TeamMember.objects.create(
            team=cls.team_a, person=cls.athlete, joined_at=date(2026, 3, 1)
        )
        TeamMember.objects.create(
            team=cls.team_b,
            person=cls.athlete,
            joined_at=date(2025, 3, 1),
            status=enums.RelationStatus.EXITED,
            left_at=date(2026, 1, 15),
        )
        # 运动员 B：无球队、无材料
        cls.athlete_b = Person.objects.create(
            person_no="P202600000202",
            full_name="李备选",
            gender=enums.Gender.FEMALE,
            birth_date=date(2013, 1, 2),
            id_card_cipher=encrypt_text("110101201301025678"),
            id_card_hash=lookup_hash("110101201301025678"),
            id_card_masked=mask_id_number("110101201301025678"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.APPROVED,
        )
        AthleteProfile.objects.create(person=cls.athlete_b, grade_level="JUNIOR")
        # 软删运动员（创建即软删，与 materials 夹具同款；people_queryset 排除）
        cls.athlete_del = Person.objects.create(
            person_no="P202600000204",
            full_name="钱已删",
            id_card_cipher=encrypt_text("110101201206013333"),
            id_card_hash=lookup_hash("110101201206013333"),
            id_card_masked=mask_id_number("110101201206013333"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.APPROVED,
            is_deleted=True,
            deleted_at=timezone.now(),
        )
        AthleteProfile.objects.create(person=cls.athlete_del, grade_level="JUNIOR")
        # 仅教练（无 AthleteProfile 行）→ 不构成遴选档案（文档 L4708）
        cls.coach = Person.objects.create(
            person_no="P202600000203",
            full_name="赵教练",
            id_card_cipher=encrypt_text("110101198501017890"),
            id_card_hash=lookup_hash("110101198501017890"),
            id_card_masked=mask_id_number("110101198501017890"),
            person_category=enums.PersonCategory.COACH,
            audit_status=enums.AuditStatus.APPROVED,
        )
        CoachProfile.objects.create(person=cls.coach)

        mtype = MaterialType.objects.create(
            code="ATHLETE_CERT", name="运动员证书", applies_to="PERSON"
        )
        file_rec = FileRecord.objects.create(
            id=new_file_id(),
            owner_type="PERSON",
            owner_id=str(cls.athlete.pk),
            original_name="证书.pdf",
            storage_key="k/cert.pdf",
            uploaded_by="tester",
        )
        cls.material = PersonMaterial.objects.create(
            person=cls.athlete,
            type=mtype,
            file_id=file_rec.pk,
            file_name="证书.pdf",
            file_ext="pdf",
            status=enums.MaterialStatus.PENDING,
            remark="王的证书",
        )
        deleted_file = FileRecord.objects.create(
            id=new_file_id(),
            owner_type="PERSON",
            owner_id=str(cls.athlete.pk),
            original_name="旧证.pdf",
            storage_key="k/old.pdf",
            uploaded_by="tester",
        )
        PersonMaterial.objects.create(
            person=cls.athlete,
            type=mtype,
            file_id=deleted_file.pk,
            file_name="旧证.pdf",
            file_ext="pdf",
            status=enums.MaterialStatus.PENDING,
            is_deleted=True,
            deleted_at=timezone.now(),
            remark="已删材料",
        )

    # ------------------------------------------------------------ 请求工具

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _get(self, path, token=None, query=None):
        return self.client.get(path, data=query or {}, **self._auth(token))

    def _post(self, path, payload, token=None):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token),
        )

    # ------------------------------------------------------------ 鉴权与权限点

    def test_missing_token_returns_401(self):
        resp = self._post(self.SEARCH, {})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], "UNAUTHENTICATED")

        resp = self._get(f"{self.GET_BASE}/{self.athlete.pk}")
        self.assertEqual(resp.status_code, 401)

    def test_search_forbidden_for_role_without_permission(self):
        """TEAM 不在文档角色域（L1740）→ 权限点拒绝 403 FORBIDDEN。"""
        resp = self._post(self.SEARCH, {}, token=self.team_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    def test_search_data_scope_denied_without_org_scope(self):
        """PROVINCE_REVIEWER 未绑定组织节点 → 无可见学校范围 → 403
        DATA_SCOPE_DENIED（复用 apply_person_scope 同口径）。"""
        resp = self._post(self.SEARCH, {}, token=self.prov_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

        resp = self._get(
            f"{self.GET_BASE}/{self.athlete.pk}", token=self.prov_token
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    def test_permission_points_registered_with_contract_roles(self):
        from apps.common.permissions import operation_roles

        self.assertEqual(
            operation_roles("dossierSearch"), ("ADMIN", "PROVINCE_REVIEWER")
        )
        self.assertEqual(
            operation_roles("dossierGet"), ("ADMIN", "PROVINCE_REVIEWER")
        )

    # ------------------------------------------------------------ 检索行为

    def test_search_returns_athletes_only_with_contract_shape(self):
        resp = self._post(self.SEARCH, {}, token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual(body["total"], 2)  # 仅两名运动员；教练不构成遴选档案
        for item in body["items"]:
            for key in (
                "id",
                "athlete",
                "teams",
                "materials",
                "enrollments",
                "matchRecords",
                "version",
                "createdAt",
                "updatedAt",
            ):
                self.assertIn(key, item)
            self.assertEqual(item["enrollments"], [])  # 报名读模型未落地
            self.assertEqual(item["matchRecords"], [])  # 比赛读模型未落地
            self.assertNotIn("idNumber", item["athlete"])  # 证件号永不回显

    def test_search_filter_by_name(self):
        resp = self._post(self.SEARCH, {"athleteName": "王候"}, token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 1)
        self.assertEqual(
            resp.json()["data"]["items"][0]["athlete"]["realName"], "王候选"
        )

        resp = self._post(self.SEARCH, {"athleteName": "赵教练"}, token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 0)  # 教练不在档案域

    def test_search_filter_by_id_number_controlled_lookup(self):
        """idNumber 受控检索（文档 L4713）：hash 精确匹配、响应仅掩码。"""
        resp = self._post(self.SEARCH, {"idNumber": self.ID_CARD}, token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual(body["total"], 1)
        masked = body["items"][0]["athlete"]["maskedIdNumber"]
        self.assertEqual(masked, mask_id_number(self.ID_CARD))
        self.assertNotIn(self.ID_CARD, json.dumps(resp.json()))  # 全号不回显

        resp = self._post(
            self.SEARCH, {"idNumber": "110101201305019999"}, token=self.admin_token
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 0)

    def test_search_filter_by_team_current_relation_only(self):
        """teamId=现役正式关系（CURRENT 口径同 /me/teams，EXITED 不计）。"""
        resp = self._post(
            self.SEARCH, {"teamId": str(self.team_a.pk)}, token=self.admin_token
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 1)
        self.assertEqual(resp.json()["data"]["items"][0]["id"], str(self.athlete.pk))

        resp = self._post(
            self.SEARCH, {"teamId": str(self.team_b.pk)}, token=self.admin_token
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 0)  # 仅 EXITED 历史 → 不命中

    def test_search_rejects_event_and_group_filters(self):
        """eventId/groupId 契约未规定过滤行为（D-A 待决策）→ 非空 400 明确
        未开放，不默认空集、不虚构过滤语义。"""
        for payload in ({"eventId": "EV1"}, {"groupId": "G1"}):
            resp = self._post(self.SEARCH, payload, token=self.admin_token)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")

    def test_search_pagination_and_order(self):
        resp = self._post(
            self.SEARCH, {"page": 1, "pageSize": 1}, token=self.admin_token
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["items"]), 1)
        # -id 排序（复用 list_people 先例）：后建的 athlete_b 在前
        self.assertEqual(body["items"][0]["athlete"]["realName"], "李备选")

        resp = self._post(
            self.SEARCH, {"page": 2, "pageSize": 1}, token=self.admin_token
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json()["data"]["items"][0]["athlete"]["realName"], "王候选"
        )

    # ------------------------------------------------------------ 详情聚合

    def test_get_aggregates_from_existing_relations(self):
        """聚合与库内实际关系一致：athlete 脱敏、teams 仅现役、materials
        未软删、enrollments/matchRecords 空数组（读模型未落地）。"""
        resp = self._get(
            f"{self.GET_BASE}/{self.athlete.pk}", token=self.admin_token
        )
        self.assertEqual(resp.status_code, 200)
        dossier = resp.json()["data"]

        self.assertEqual(dossier["id"], str(self.athlete.pk))
        self.assertEqual(dossier["athlete"]["realName"], "王候选")
        self.assertEqual(dossier["athlete"]["kinds"], ["ATHLETE"])
        self.assertEqual(
            dossier["athlete"]["maskedIdNumber"], mask_id_number(self.ID_CARD)
        )
        self.assertNotIn(self.ID_CARD, json.dumps(resp.json()))

        # teams：仅现役 A 队，EXITED 历史 B 队不并入（同 /me/teams 口径）
        self.assertEqual([t["id"] for t in dossier["teams"]], [str(self.team_a.pk)])

        # materials：未软删 1 条，形状复用批次 C material_out
        self.assertEqual(len(dossier["materials"]), 1)
        material = dossier["materials"][0]
        self.assertEqual(material["materialType"], "ATHLETE_CERT")
        self.assertEqual(material["materialLabel"], "运动员证书")
        self.assertEqual(material["reviewStatus"], "PENDING")
        self.assertEqual(material["fileName"], "证书.pdf")
        self.assertEqual(material["ownerType"], "PERSON")

        # 报名/比赛读模型未落地：契约可选字段空数组（非伪造记录）
        self.assertEqual(dossier["enrollments"], [])
        self.assertEqual(dossier["matchRecords"], [])

        # version：B4 微秒派生（person_version 同源）
        expected_version = int(
            Person.objects.get(pk=self.athlete.pk).updated_at.timestamp() * 1_000_000
        )
        self.assertEqual(dossier["version"], expected_version)

    def test_get_404_for_missing_or_non_athlete(self):
        resp = self._get(f"{self.GET_BASE}/999999", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

        resp = self._get(f"{self.GET_BASE}/{self.coach.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)  # 仅教练 → 不构成遴选档案

    def test_get_and_search_exclude_soft_deleted_person(self):
        """软删人员不构成有效档案（复用 people_queryset 软删过滤；
        get_person 先例"软删行视为不存在"）。"""
        resp = self._get(f"{self.GET_BASE}/{self.athlete_del.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

        resp = self._post(
            self.SEARCH, {"athleteName": "钱已删"}, token=self.admin_token
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 0)

    def test_get_rejects_event_id_filter(self):
        resp = self._get(
            f"{self.GET_BASE}/{self.athlete.pk}",
            token=self.admin_token,
            query={"eventId": "EV1"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")


class PersonDataScopeTests(TestCase):
    """人员数据范围关键用例（2026-10-27）。

    CITY 本市 / UNIVERSITY 本校 / TEAM 本队（含历史关系）/ ATHLETE 本人 /
    PROVINCE_REVIEWER 省域；越界详情 404，列表按范围过滤，写链越界 403。
    """

    @classmethod
    def setUpTestData(cls):
        province = Region.objects.create(name="丁省", code="720000", level=1)
        cls.city_a = Region.objects.create(
            name="丁市甲", code="720100", level=2, parent=province
        )
        cls.city_b = Region.objects.create(
            name="丁市乙", code="720200", level=2, parent=province
        )
        cls.school_a = School.objects.create(
            name="丁甲一小", code="SCH-720100-001", school_type="PRIMARY", region=cls.city_a
        )
        cls.school_b = School.objects.create(
            name="丁甲二小", code="SCH-720100-002", school_type="PRIMARY", region=cls.city_a
        )
        cls.school_c = School.objects.create(
            name="丁乙一小", code="SCH-720200-001", school_type="PRIMARY", region=cls.city_b
        )

        cls.org_prov = Organization.objects.create(
            id=new_organization_id(), name="丁省组织", org_type="PROVINCE", code="ORG-PROV-DS"
        )
        cls.org_city_a = Organization.objects.create(
            id=new_organization_id(),
            name="丁甲市组织",
            org_type="CITY",
            code="ORG-CITY-DS-A",
            parent=cls.org_prov,
        )
        cls.org_city_b = Organization.objects.create(
            id=new_organization_id(),
            name="丁乙市组织",
            org_type="CITY",
            code="ORG-CITY-DS-B",
            parent=cls.org_prov,
        )
        # org.code = School.code（人员数据范围按组织子树内学校 code 过滤）
        cls.org_school_a = Organization.objects.create(
            id=new_organization_id(),
            name="丁甲一小组织",
            org_type="SCHOOL",
            code=cls.school_a.code,
            parent=cls.org_city_a,
        )
        cls.org_school_b = Organization.objects.create(
            id=new_organization_id(),
            name="丁甲二小组织",
            org_type="SCHOOL",
            code=cls.school_b.code,
            parent=cls.org_city_a,
        )
        cls.org_school_c = Organization.objects.create(
            id=new_organization_id(),
            name="丁乙一小组织",
            org_type="SCHOOL",
            code=cls.school_c.code,
            parent=cls.org_city_b,
        )

        cls.team_a = Team.objects.create(
            team_no="T202600000301",
            name="丁甲一队",
            category_code="PRIMARY",
            school=cls.school_a,
            region=cls.city_a,
        )
        cls.org_team_a = Organization.objects.create(
            id=new_organization_id(),
            name="丁甲一队组织",
            org_type="TEAM",
            code=cls.team_a.team_no,
            parent=cls.org_school_a,
        )

        def _person(no, name, school, id_no, category=enums.PersonCategory.ATHLETE):
            return Person.objects.create(
                person_no=no,
                full_name=name,
                school=school,
                id_card_cipher=encrypt_text(id_no),
                id_card_hash=lookup_hash(id_no),
                id_card_masked=mask_id_number(id_no),
                person_category=category,
                audit_status=enums.AuditStatus.APPROVED,
            )

        cls.p_a = _person("P202600000301", "丁甲一", cls.school_a, "110101201001010011")
        cls.p_b = _person("P202600000302", "丁甲二", cls.school_b, "110101201002020022")
        cls.p_c = _person("P202600000303", "丁乙一", cls.school_c, "110101201003030033")
        cls.p_extra = _person(
            "P202600000304",
            "丁甲教",
            cls.school_a,
            "110101198001010044",
            enums.PersonCategory.COACH,
        )
        # 本队关系：现役 + 历史（EXITED 同属本队可见范围）
        TeamMember.objects.create(
            team=cls.team_a, person=cls.p_a, joined_at=date(2026, 3, 1)
        )
        TeamMember.objects.create(
            team=cls.team_a,
            person=cls.p_b,
            joined_at=date(2025, 3, 1),
            status=enums.RelationStatus.EXITED,
            left_at=timezone.now(),
        )

        def _account(username, roles, org=None, person_id=""):
            return Account.objects.create(
                id=new_account_id(),
                username=username,
                display_name=username,
                roles=roles,
                organization=org,
                person_id=person_id,
                password=make_password("scope-pass-123"),
                enabled=True,
            )

        cls.city_user = _account("scope-ds-city-a", ["CITY"], cls.org_city_a)
        cls.uni_user = _account("scope-ds-uni-a", ["UNIVERSITY"], cls.org_school_a)
        cls.team_user = _account("scope-ds-team-a", ["TEAM"], cls.org_team_a)
        cls.prov_user = _account("scope-ds-prov", ["PROVINCE_REVIEWER"], cls.org_prov)
        cls.athlete_user = _account(
            "scope-ds-athlete", ["ATHLETE"], person_id=str(cls.p_c.pk)
        )
        cls.athlete_unbound = _account("scope-ds-athlete-unbound", ["ATHLETE"])

        cls.city_token = issue_access_token(cls.city_user)[0]
        cls.uni_token = issue_access_token(cls.uni_user)[0]
        cls.team_token = issue_access_token(cls.team_user)[0]
        cls.prov_token = issue_access_token(cls.prov_user)[0]
        cls.athlete_token = issue_access_token(cls.athlete_user)[0]

    # ------------------------------------------------------------ 请求工具

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _get(self, path, token=None, query=None):
        return self.client.get(path, data=query or {}, **self._auth(token))

    def _post(self, path, payload, token=None):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token),
        )

    def _put(self, path, payload, token=None):
        return self.client.put(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(token),
        )

    def _names(self, token) -> set:
        resp = self._get(PEOPLE, token=token)
        self.assertEqual(resp.status_code, 200, resp.content)
        return {item["realName"] for item in resp.json()["data"]["items"]}

    # ------------------------------------------------------------ 各角色范围

    def test_city_sees_only_schools_in_its_city(self):
        """CITY：本市两所学校下人员可见，外市人员过滤且详情 404。"""
        self.assertEqual(self._names(self.city_token), {"丁甲一", "丁甲二", "丁甲教"})
        out_of_scope = self._get(f"{PEOPLE}/{self.p_c.pk}", token=self.city_token)
        self.assertEqual(out_of_scope.status_code, 404)
        self.assertEqual(out_of_scope.json()["code"], "NOT_FOUND")
        # 检索同样收敛到本市范围
        resp = self._post(PEOPLE + "/search", {"keyword": "丁"}, token=self.city_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            {item["realName"] for item in resp.json()["data"]},
            {"丁甲一", "丁甲二", "丁甲教"},
        )

    def test_university_sees_only_own_school(self):
        """UNIVERSITY：仅本校人员；同市他校人员过滤且详情 404。"""
        self.assertEqual(self._names(self.uni_token), {"丁甲一", "丁甲教"})
        out_of_scope = self._get(f"{PEOPLE}/{self.p_b.pk}", token=self.uni_token)
        self.assertEqual(out_of_scope.status_code, 404)
        self.assertEqual(out_of_scope.json()["code"], "NOT_FOUND")

    def test_team_sees_only_own_team_members_including_history(self):
        """TEAM：仅本队成员关系（CURRENT 与 EXITED 均为历史可见）。"""
        self.assertEqual(self._names(self.team_token), {"丁甲一", "丁甲二"})
        out_of_scope = self._get(f"{PEOPLE}/{self.p_extra.pk}", token=self.team_token)
        self.assertEqual(out_of_scope.status_code, 404)
        self.assertEqual(out_of_scope.json()["code"], "NOT_FOUND")

    def test_my_teams_team_role_returns_bound_team(self):
        """TEAM：/me/teams 只返回组织 code 与 team_no 对齐的本队（不再 403）。"""
        resp = self._get("/api/v1/me/teams", token=self.team_token)
        self.assertEqual(resp.status_code, 200, resp.content)
        teams = resp.json()["data"]
        self.assertEqual([t["id"] for t in teams], [str(self.team_a.pk)])
        self.assertEqual(teams[0]["name"], "丁甲一队")

    def test_my_teams_team_role_unaligned_org_returns_empty(self):
        """TEAM：组织 code 与任何 team_no 不匹配 → 空数组 200（工作台可进入）。"""
        org = Organization.objects.create(
            id=new_organization_id(),
            name="未对齐球队组织",
            org_type="TEAM",
            code="TEAM-NOT-ALIGNED",
            parent=self.org_school_a,
        )
        account = Account.objects.create(
            id=new_account_id(),
            username="scope-ds-team-unaligned",
            display_name="scope-ds-team-unaligned",
            roles=["TEAM"],
            organization=org,
            password=make_password("scope-pass-123"),
            enabled=True,
        )
        token = issue_access_token(account)[0]
        resp = self._get("/api/v1/me/teams", token=token)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"], [])

    def test_athlete_scope_returns_only_self(self):
        """ATHLETE：只返回账号绑定本人；未绑定 → 空集（不 403）。"""
        from apps.persons.services import apply_person_scope, people_queryset

        scoped = apply_person_scope(people_queryset(), self.athlete_user)
        self.assertEqual(list(scoped.values_list("pk", flat=True)), [self.p_c.pk])

        unbound = apply_person_scope(people_queryset(), self.athlete_unbound)
        self.assertEqual(unbound.count(), 0)

    def test_province_reviewer_sees_whole_province(self):
        """PROVINCE_REVIEWER：省域组织子树内全部学校人员可见。"""
        self.assertEqual(
            self._names(self.prov_token),
            {"丁甲一", "丁甲二", "丁乙一", "丁甲教"},
        )
        resp = self._get(f"{PEOPLE}/{self.p_c.pk}", token=self.prov_token)
        self.assertEqual(resp.status_code, 200, resp.content)

    # ------------------------------------------------------------ 写链范围

    def test_university_create_person_school_scope(self):
        """UNIVERSITY 创建人员：提交学校须在本校范围内，越界 403。"""
        in_scope = self._post(
            PEOPLE,
            {
                "kinds": ["ATHLETE"],
                "realName": "本校新生",
                "idNumber": "110101201501010055",
                "schoolOrgId": str(self.org_school_a.pk),
            },
            token=self.uni_token,
        )
        self.assertEqual(in_scope.status_code, 200, in_scope.content)

        out_of_scope = self._post(
            PEOPLE,
            {
                "kinds": ["ATHLETE"],
                "realName": "他校新生",
                "idNumber": "110101201502020066",
                "schoolOrgId": str(self.org_school_b.pk),
            },
            token=self.uni_token,
        )
        self.assertEqual(out_of_scope.status_code, 403)
        self.assertEqual(out_of_scope.json()["code"], "DATA_SCOPE_DENIED")

    def test_update_and_delete_denied_out_of_scope(self):
        """写链目标人员越界 → 403（服务层校验，非接口层放行）。"""
        from apps.persons.schemas import person_version

        resp = self._put(
            f"{PEOPLE}/{self.p_c.pk}",
            {
                "kinds": ["ATHLETE"],
                "realName": "越权改名",
                "version": person_version(self.p_c),
            },
            token=self.city_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        self.p_c.refresh_from_db()
        self.assertEqual(self.p_c.full_name, "丁乙一")

        resp = self.client.delete(
            f"{PEOPLE}/{self.p_extra.pk}?version={person_version(self.p_extra)}",
            **self._auth(self.team_token),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        self.p_extra.refresh_from_db()
        self.assertFalse(self.p_extra.is_deleted)


class CoachQualificationTests(TestCase):
    """GET /people/{id} 只读派生字段 coachQualification（问题#7，2026-10-31 裁定）。

    四态：MISSING 无证书（无 CoachProfile 行，或证书编号与有效期均未登记）/
    UNKNOWN 证书信息不完整（有证号无有效期，或有有效期无证号——二轮修正）/
    VALID 有效期 >= 当前业务日期 / EXPIRED 有效期 < 当前业务日期
    （有效期至当日含当日有效）。
    业务日期基准=settings.TIME_ZONE（Asia/Shanghai，timezone.localdate()）。
    零落库：仅派生输出，不写 CoachProfile、不改 audit_status。
    """

    @classmethod
    def setUpTestData(cls):
        region = Region.objects.create(name="庚省", code="990000", level=1)
        cls.school = School.objects.create(
            name="庚一小", code="SCH-990100-701", school_type="PRIMARY", region=region
        )
        cls.org_school = Organization.objects.create(
            id=new_organization_id(),
            name="庚一小组织",
            org_type="SCHOOL",
            code="SCH-990100-701",
        )
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="qual-admin",
            roles=["ADMIN"],
            password=make_password("qual-pass-123"),
            enabled=True,
        )
        cls.admin_token = issue_access_token(cls.admin)[0]

    @classmethod
    def _coach(cls, no, name, id_no, cert_no=None, expire=None):
        person = Person.objects.create(
            person_no=no,
            full_name=name,
            school=cls.school,
            id_card_cipher=encrypt_text(id_no),
            id_card_hash=lookup_hash(id_no),
            id_card_masked=mask_id_number(id_no),
            person_category=enums.PersonCategory.COACH,
            audit_status=enums.AuditStatus.APPROVED,
        )
        CoachProfile.objects.create(
            person=person, certificate_no=cert_no, certificate_expire_date=expire
        )
        return person

    def _detail(self, person):
        resp = self.client.get(
            f"{PEOPLE}/{person.pk}",
            **{"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]

    # ------------------------------------------------------------ 四态

    def test_detail_without_coach_profile_missing(self):
        """非教练（无 CoachProfile 行）→ MISSING（无教练证书）。"""
        person = Person.objects.create(
            person_no="P202600000951",
            full_name="庚运动员",
            school=self.school,
            id_card_cipher=encrypt_text("110101201001019801"),
            id_card_hash=lookup_hash("110101201001019801"),
            id_card_masked=mask_id_number("110101201001019801"),
            person_category=enums.PersonCategory.ATHLETE,
            audit_status=enums.AuditStatus.APPROVED,
        )
        AthleteProfile.objects.create(person=person)
        self.assertEqual(self._detail(person)["coachQualification"], "MISSING")

    def test_detail_blank_certificate_no_missing(self):
        """证书编号与有效期均未登记 → MISSING（无证书）。"""
        person = self._coach(
            "P202600000952", "庚无证", "110101198501019802"
        )
        self.assertEqual(self._detail(person)["coachQualification"], "MISSING")

    def test_detail_expire_without_certificate_no_unknown(self):
        """二轮修正：仅有有效期、未登记证书编号 → UNKNOWN（证书信息不完整，
        不再归 MISSING；不新增第五种状态）。"""
        person = self._coach(
            "P202600000959",
            "庚孤证",
            "110101198501019809",
            expire=timezone.localdate() + timedelta(days=30),
        )
        self.assertEqual(self._detail(person)["coachQualification"], "UNKNOWN")

    def test_detail_certificate_without_expire_unknown(self):
        """有证书编号未登记有效期 → UNKNOWN。"""
        person = self._coach(
            "P202600000953", "庚未知", "110101198501019803", cert_no="CERT-2026-001"
        )
        self.assertEqual(self._detail(person)["coachQualification"], "UNKNOWN")

    def test_detail_expire_today_valid(self):
        """有效期边界（裁定：expire_date == 今日 → VALID，含当日有效）。"""
        person = self._coach(
            "P202600000954",
            "庚今日",
            "110101198501019804",
            cert_no="CERT-2026-002",
            expire=timezone.localdate(),
        )
        self.assertEqual(self._detail(person)["coachQualification"], "VALID")

    def test_detail_expire_yesterday_expired(self):
        """有效期昨日（< 今日）→ EXPIRED。"""
        person = self._coach(
            "P202600000955",
            "庚过期",
            "110101198501019805",
            cert_no="CERT-2026-003",
            expire=timezone.localdate() - timedelta(days=1),
        )
        self.assertEqual(self._detail(person)["coachQualification"], "EXPIRED")

    def test_detail_expire_future_valid(self):
        person = self._coach(
            "P202600000956",
            "庚有效",
            "110101198501019806",
            cert_no="CERT-2026-004",
            expire=timezone.localdate() + timedelta(days=30),
        )
        self.assertEqual(self._detail(person)["coachQualification"], "VALID")

    def test_derivation_is_read_only(self):
        """派生只读：detail 前后 CoachProfile 行与主档均未变化（零落库）。"""
        from apps.persons.schemas import person_version

        person = self._coach(
            "P202600000957",
            "庚只读",
            "110101198501019807",
            cert_no="CERT-2026-005",
            expire=timezone.localdate() - timedelta(days=1),
        )
        before_version = person_version(person)
        before_expire = (
            CoachProfile.objects.get(person=person).certificate_expire_date
        )
        self.assertEqual(self._detail(person)["coachQualification"], "EXPIRED")
        person.refresh_from_db()
        profile = CoachProfile.objects.get(person=person)
        self.assertEqual(person_version(person), before_version)
        self.assertEqual(profile.certificate_expire_date, before_expire)

    # ------------------------------------------------------------ 契约边界

    def test_list_does_not_expose_coach_qualification(self):
        """裁定范围：仅 GET /people/{id} 输出，列表契约不扩散。"""
        self._coach(
            "P202600000958",
            "庚列表",
            "110101198501019808",
            cert_no="CERT-2026-006",
        )
        resp = self.client.get(
            PEOPLE, **{"HTTP_AUTHORIZATION": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        for item in resp.json()["data"]["items"]:
            self.assertNotIn("coachQualification", item)


class AuditTransactionConsistencyTests(TestCase):
    """审计事务一致性（风险5 修订方案，2026-10-31 有条件批准）T1-T3。

    覆盖：T1 服务层审计失败 → 业务整体回滚（Person/PersonChangeLog/
    SUCCESS 审计零残留）；T2 成功审计精确一条（API 层 audit_state(request,
    None) 撤销中间件声明，防重复）+ 幂等重复确认保留 SUCCESS 审计（裁定：
    语义=请求成功处理而非状态变化）；T3 服务层失败形成 500 时中间件按原
    行为落 FAILURE——用"仅首次抛错"的 mock 精确区分服务层写与中间件写，
    不以恒抛错 mock 误判中间件行为。

    验证边界（T5 声明）：本类在 SQLite 运行，可验证事务原子性与审计唯一
    性；select_for_update 在 SQLite 被 ORM 静默忽略
    （features.has_select_for_update=False），Person 行锁互斥与 MySQL
    REPEATABLE READ 快照时序未在本环境验证，SQLite 通过不代表 MySQL
    并发安全已验证（须 MySQL 可控环境专项执行，本批不连接真实 MySQL）。
    """

    @classmethod
    def setUpTestData(cls):
        region = Region.objects.create(name="壬省", code="970000", level=1)
        cls.school = School.objects.create(
            name="壬一小", code="SCH-970100-901", school_type="PRIMARY", region=region
        )
        cls.org_school = Organization.objects.create(
            id=new_organization_id(),
            name="壬一小组织",
            org_type="SCHOOL",
            code="SCH-970100-901",
        )
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="audit-tx-admin",
            roles=["ADMIN"],
            password=make_password("audit-tx-pass-123"),
            enabled=True,
        )
        cls.admin_token = issue_access_token(cls.admin)[0]

    def _post_create(self, real_name="钱事务", id_no="110101201001019821"):
        payload = {
            "kinds": ["ATHLETE"],
            "realName": real_name,
            "idNumber": id_no,
            "schoolOrgId": str(self.org_school.pk),
        }
        return self.client.post(
            PEOPLE,
            data=json.dumps(payload),
            content_type="application/json",
            **self._auth(self.admin_token),
        )

    def _auth(self, token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _bind_and_confirm(self, person, seq=1):
        """新建绑定账号并提交复核，返回 (响应, 确认账号 token)。

        seq 用于同档案多次绑定账号的场景防 username 撞 UNIQUE。"""
        from apps.persons.schemas import person_version

        user = Account.objects.create(
            id=new_account_id(),
            username=f"audit-tx-self-{person.pk}-{seq}",
            display_name=person.full_name,
            roles=["ATHLETE"],
            password=make_password("audit-tx-pass-123"),
            enabled=True,
            person_id=str(person.pk),
        )
        token = issue_access_token(user)[0]
        resp = self.client.post(
            ME_PROFILE_CONFIRM,
            data=json.dumps({"version": person_version(person)}),
            content_type="application/json",
            **self._auth(token),
        )
        return resp, token

    def test_t1_audit_write_failure_rolls_back_business(self):
        """T1：服务层审计写入失败 → 业务事务整体回滚，三者零残留。"""
        from unittest import mock

        with mock.patch.object(
            AuditLog.objects, "create", side_effect=Exception("audit db down")
        ):
            resp = self.client.post(
                PEOPLE,
                data=json.dumps(
                    {
                        "kinds": ["ATHLETE"],
                        "realName": "钱事务",
                        "idNumber": "110101201001019821",
                        "schoolOrgId": str(self.org_school.pk),
                    }
                ),
                content_type="application/json",
                raise_request_exception=False,
                **self._auth(self.admin_token),
            )
        self.assertEqual(resp.status_code, 500)
        # Person、PersonChangeLog、SUCCESS 审计同生共死，零残留
        self.assertEqual(Person.objects.filter(full_name="钱事务").count(), 0)
        self.assertEqual(
            PersonChangeLog.objects.filter(
                change_type=enums.ChangeType.CREATE
            ).count(),
            0,
        )
        self.assertEqual(
            AuditLog.objects.filter(action="person.create").count(), 0
        )

    def test_t2_success_audit_exactly_once(self):
        """T2：创建/提交成功后各自 SUCCESS 审计精确一条（中间件被
        audit_state(request, None) 撤销不重复）；幂等重复确认每次请求
        仍各记一条 SUCCESS（裁定保留），不写 ChangeLog。"""
        from apps.persons.schemas import person_version

        resp = self._post_create()
        self.assertEqual(resp.status_code, 200, resp.content)
        person = Person.objects.get(full_name="钱事务")
        self.assertEqual(
            AuditLog.objects.filter(
                action="person.create",
                object_id=str(person.pk),
                outcome="SUCCESS",
            ).count(),
            1,
        )
        resp, _ = self._bind_and_confirm(person)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(
            AuditLog.objects.filter(
                action="person.self_confirm",
                object_id=str(person.pk),
                outcome="SUCCESS",
            ).count(),
            1,
        )
        # 幂等重复确认：审计按请求记 SUCCESS（请求成功处理语义），状态不变、
        # ChangeLog 数量不变（幂等不写留痕）
        person.refresh_from_db()
        before_version = person_version(person)
        cl_count = PersonChangeLog.objects.filter(person=person).count()
        resp, _ = self._bind_and_confirm(person, seq=2)
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        self.assertEqual(person.audit_status, enums.AuditStatus.PENDING)
        self.assertEqual(person_version(person), before_version)
        self.assertEqual(
            PersonChangeLog.objects.filter(person=person).count(), cl_count
        )
        logs = AuditLog.objects.filter(
            action="person.self_confirm",
            object_id=str(person.pk),
            outcome="SUCCESS",
        ).order_by("created_at")
        self.assertEqual(logs.count(), 2)
        self.assertEqual(
            len({log.operator_account_id for log in logs}), 2
        )  # 两次请求两个绑定账号，各自如实记录

    def test_t3_service_failure_middleware_writes_failure_audit(self):
        """T3：服务层审计失败 → 500 且业务回滚；中间件（声明未撤销）按原
        行为记录 FAILURE。mock 仅首次调用（服务层）抛错、后续（中间件）
        放行，与 T1 的恒抛错 mock 严格区分，不误判中间件写入。"""
        from unittest import mock

        real_create = AuditLog.objects.create
        calls = {"n": 0}

        def flaky_create(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:  # 仅服务层事务内首次写入失败
                raise Exception("audit db down")
            return real_create(*args, **kwargs)

        with mock.patch.object(AuditLog.objects, "create", side_effect=flaky_create):
            resp = self.client.post(
                PEOPLE,
                data=json.dumps(
                    {
                        "kinds": ["ATHLETE"],
                        "realName": "钱事务",
                        "idNumber": "110101201001019821",
                        "schoolOrgId": str(self.org_school.pk),
                    }
                ),
                content_type="application/json",
                raise_request_exception=False,
                **self._auth(self.admin_token),
            )
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(Person.objects.filter(full_name="钱事务").count(), 0)
        # 中间件 FAILURE 恰一条（声明未撤销→原行为写入），无 SUCCESS 残留
        failures = AuditLog.objects.filter(
            action="person.create", outcome="FAILURE"
        )
        self.assertEqual(failures.count(), 1)
        self.assertEqual(
            failures.first().operator_account_id, self.admin.pk
        )
        self.assertEqual(
            AuditLog.objects.filter(
                action="person.create", outcome="SUCCESS"
            ).count(),
            0,
        )
