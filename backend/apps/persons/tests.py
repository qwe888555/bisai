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
             "address": "yy路2号", "isActive": True},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["schoolType"], "JUNIOR")
        self.assertIsNone(data["code"])

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
        # 更新为他人校名：(name, region) 唯一冲突
        resp = self._put(
            f"{BASE}/schools/{second['id']}",
            {"name": "甲校", "schoolType": "PRIMARY", "regionId": county["id"]},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 409)

        # 保持自身校名（排除自身）；请求不含 code（编码创建后禁改），正常更新
        resp = self._put(
            f"{BASE}/schools/{second['id']}",
            {"name": "乙校", "schoolType": "PRIMARY", "regionId": county["id"]},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["code"], "SCH201")

    def test_school_update_code_immutable(self):
        _, _, county = self._region_tree()
        data = self._create_school(
            {"name": "稳码校", "schoolType": "PRIMARY", "regionId": county["id"],
             "code": "SCH300"}
        )
        # 试图通过更新修改编码：StrictSchema extra=forbid → 400 INVALID_ARGUMENT
        resp = self._put(
            f"{BASE}/schools/{data['id']}",
            {"name": "稳码校", "schoolType": "PRIMARY", "regionId": county["id"],
             "code": "SCH301"},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(School.objects.get(pk=data["id"]).code, "SCH300")

        # 编码未变化的正常更新不受影响
        resp = self._put(
            f"{BASE}/schools/{data['id']}",
            {"name": "稳码校改", "schoolType": "JUNIOR", "regionId": county["id"]},
            token=self.admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["code"], "SCH300")

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


class PersonCoachDutiesTests(TestCase):
    """D-CAT：人员列表 coachDuties 聚合与 coachDuty 现役任职筛选。

    规则（2026-10-28 负责人确认）：岗位类别=TeamCoach 任职关系（六值 duty）；
    列表汇总 status=CURRENT 的 duty 去重并列展示；筛选任一现役任职命中即
    返回该人员一次；EXITED 不参与展示与筛选；coachDuties 仅注入列表响应。
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin = Account.objects.create(
            id=new_account_id(),
            username="duties-admin",
            display_name="岗位列表管理员",
            roles=["ADMIN"],
            password=make_password("admin-pass-123"),
            enabled=True,
        )
        cls.admin_token = issue_access_token(cls.admin)[0]

        # 多岗教练：队1 HEAD_COACH、队2 GOALKEEPER_COACH、队3 HEAD_COACH（同人
        # 两处现役 HEAD，验证筛选去重）、队4 FITNESS_COACH 已退出（EXITED 不参与）
        cls.multi_coach = Person.objects.create(
            person_no="P202600000011",
            full_name="王多岗",
            id_card_cipher=encrypt_text("110101198201023456"),
            id_card_hash=lookup_hash("110101198201023456"),
            id_card_masked=mask_id_number("110101198201023456"),
            person_category=enums.PersonCategory.COACH,
            audit_status=enums.AuditStatus.APPROVED,
        )
        CoachProfile.objects.create(person=cls.multi_coach)
        # 无现役任职教练（刚建档未入队）
        cls.free_coach = Person.objects.create(
            person_no="P202600000012",
            full_name="赵未任职",
            id_card_cipher=encrypt_text("110101198303034567"),
            id_card_hash=lookup_hash("110101198303034567"),
            id_card_masked=mask_id_number("110101198303034567"),
            person_category=enums.PersonCategory.COACH,
            audit_status=enums.AuditStatus.APPROVED,
        )
        CoachProfile.objects.create(person=cls.free_coach)

        teams = [
            Team.objects.create(
                team_no=f"T20260000001{i}",
                name=f"岗位测试队{i}",
                category_code="U12",
            )
            for i in range(1, 5)
        ]
        TeamCoach.objects.create(
            team=teams[0],
            person=cls.multi_coach,
            duty=enums.CoachDuty.HEAD_COACH,
            joined_at=date(2026, 1, 1),
        )
        TeamCoach.objects.create(
            team=teams[1],
            person=cls.multi_coach,
            duty=enums.CoachDuty.GOALKEEPER_COACH,
            joined_at=date(2026, 1, 2),
        )
        TeamCoach.objects.create(
            team=teams[2],
            person=cls.multi_coach,
            duty=enums.CoachDuty.HEAD_COACH,
            joined_at=date(2026, 1, 3),
        )
        TeamCoach.objects.create(
            team=teams[3],
            person=cls.multi_coach,
            duty=enums.CoachDuty.FITNESS_COACH,
            status=enums.RelationStatus.EXITED,
            joined_at=date(2025, 1, 1),
        )

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _get(self, path, token=None, query=None):
        return self.client.get(path, data=query or {}, **self._auth(token))

    def _list_items(self, query=None):
        resp = self._get(PEOPLE, token=self.admin_token, query=query)
        self.assertEqual(resp.status_code, 200)
        return resp.json()["data"]

    def test_list_coach_duties_aggregated_deduped_in_enum_order(self):
        data = self._list_items()
        rows = {row["realName"]: row for row in data["items"]}
        # 两处现役 HEAD_COACH 去重为一项；枚举序输出（HEAD 在 GOALKEEPER 前）
        self.assertEqual(
            rows["王多岗"]["coachDuties"],
            ["HEAD_COACH", "GOALKEEPER_COACH"],
        )
        # 无现役任职教练 → 空数组（非缺键）
        self.assertEqual(rows["赵未任职"]["coachDuties"], [])

    def test_list_coach_duty_filter_hits_any_current_duty_once(self):
        # 任一现役任职命中即返回一次：王多岗在两队任 HEAD_COACH，total 仍为 1
        data = self._list_items({"coachDuty": "HEAD_COACH"})
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["realName"], "王多岗")

        data = self._list_items({"coachDuty": "GOALKEEPER_COACH"})
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["realName"], "王多岗")

        # 无现役任职的教练不被任何类别命中
        data = self._list_items({"coachDuty": "ASSISTANT_COACH"})
        self.assertEqual(data["total"], 0)

    def test_list_coach_duty_filter_excludes_exited_history(self):
        # EXITED 的 FITNESS_COACH 不参与筛选与展示
        data = self._list_items({"coachDuty": "FITNESS_COACH"})
        self.assertEqual(data["total"], 0)
        data = self._list_items()
        rows = {row["realName"]: row for row in data["items"]}
        self.assertNotIn("FITNESS_COACH", rows["王多岗"]["coachDuties"])

    def test_list_coach_duty_invalid_value_rejected_400(self):
        resp = self._get(PEOPLE, token=self.admin_token, query={"coachDuty": "BOGUS"})
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["code"], "INVALID_ARGUMENT")

    def test_list_coach_duties_not_leaked_into_detail_response(self):
        # coachDuties 仅注入列表响应：详情/本人档案共用 person_out 不含该键
        resp = self._get(f"{PEOPLE}/{self.multi_coach.pk}", token=self.admin_token)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("coachDuties", resp.json()["data"])

    def test_teamcoach_duty_six_values_and_duty_other_column(self):
        # D-CAT 六值枚举可落库；duty_other 列可空、可存 OTHER 补充说明
        # （强制校验"OTHER 必填≤50字、非 OTHER 必须为空"随教练任职服务落地）
        team = Team.objects.create(
            team_no="T202600000015", name="六值验证队", category_code="U12"
        )
        row = TeamCoach.objects.create(
            team=team,
            person=self.free_coach,
            duty=enums.CoachDuty.OTHER,
            duty_other="外聘顾问兼任",
            joined_at=date(2026, 2, 1),
        )
        row.refresh_from_db()
        self.assertEqual(row.duty, "OTHER")
        self.assertEqual(row.duty_other, "外聘顾问兼任")
        self.assertIsNone(
            TeamCoach.objects.create(
                team=team,
                person=self.multi_coach,
                duty=enums.CoachDuty.FITNESS_COACH,
                joined_at=date(2026, 2, 2),
            ).duty_other
        )


class CoachQualificationTests(TestCase):
    """GET /people/{id} 只读派生字段 coachQualification（问题#7，2026-10-31 裁定 B 口径移植）。

    四态：MISSING 无证书（无 CoachProfile 行，或证书编号与有效期均未登记）/
    UNKNOWN 证书信息不完整（有证号无有效期，或有有效期无证号——二轮修正）/
    VALID 有效期 >= 当前业务日期 / EXPIRED 有效期 < 当前业务日期
    （有效期至当日含当日有效）。
    业务日期基准=settings.TIME_ZONE（Asia/Shanghai，timezone.localdate()）。
    零落库：仅派生输出，不写 CoachProfile、不改 audit_status；
    取代 A 版 person_out 三态 certificateValidity（证书字段不再经全部人员
    端点输出，本类与列表契约测试共同守护）。
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
        # A′-min 映射夹具：org.code = School.code（schoolOrgId 按组织节点 ID）
        cls.org_school = Organization.objects.create(
            id=new_organization_id(),
            name="甲一小组织",
            org_type="SCHOOL",
            code="SCH-800100-101",
        )
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

    def test_non_admin_denied_by_data_scope(self):
        # 学校端角色已登记权限点，但数据范围过滤（方案A′）未落地 → 明确拒绝不放行
        resp = self._get(PEOPLE, token=self.uni_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

        resp = self._post(PEOPLE + "/search", {"keyword": "张"}, token=self.uni_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

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
        # coachCategory 语义映射未确认（coach_level≠教练类别，字典清单未交付），不输出
        self.assertNotIn("coachCategory", coach_row)

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
        self.assertIsNone(athlete_row["education"])  # E1：education 输出真实列值（创建未传 → None）
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
        v0 = self._derived()
        self.person.save()
        v1 = self._derived()
        self.person.full_name = "版本员二"
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
        # A′-min 映射夹具：org.code = School.code（schoolOrgId 按组织节点 ID）
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

    # ------------------------------------- 契约测试：coachCategory（2026-10-28 口径）

    def test_create_coach_category_accepted_not_echoed_no_level_write(self):
        """契约：创建链接受 coachCategory（静默不持久化），响应不输出，
        CoachProfile 正常创建且 coach_level 保持默认 NULL（无任何映射写入）。"""
        data, person = self._create(
            kinds=["COACH"], realName="类别教练", coachCategory="主教练"
        )
        self.assertNotIn("coachCategory", data)  # 响应不输出（2026-10-24 决策）
        profile = CoachProfile.objects.get(person=person)  # COACH 档案行正常创建
        self.assertIsNone(profile.coach_level)  # 请求字段未被映射写入

    def test_update_coach_category_accepted_no_coach_level_write(self):
        """契约：更新链提交 coachCategory → 合法字段正常更新、响应不输出、
        coach_level 未被本次请求写入。"""
        from apps.persons.schemas import person_version

        _, person = self._create(kinds=["COACH"], realName="教练更新")
        resp = self._put(
            person.pk,
            {
                "kinds": ["COACH"],
                "realName": "教练改名单",
                "coachCategory": "主教练",
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertNotIn("coachCategory", data)
        person.refresh_from_db()
        self.assertEqual(person.full_name, "教练改名单")  # 合法字段正常生效
        profile = CoachProfile.objects.get(person=person)
        self.assertIsNone(profile.coach_level)  # 数据库未被写入

    def test_create_dual_kind_rejected(self):
        """问题 11：一人一身份，新建传入两个身份 → 400 INVALID_ARGUMENT。"""
        resp = self._post(
            {"kinds": ["ATHLETE", "COACH"], "realName": "双类别", "idNumber": "110101201305015678"}
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        # 确认未创建任何人员
        self.assertFalse(Person.objects.filter(full_name="双类别").exists())

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

    def test_create_forbidden_for_university(self):
        resp = self._post(
            {"kinds": ["ATHLETE"], "realName": "越权", "idNumber": self.ID_CARD},
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

    # -------------------------------------------- 批次 A：schoolName 明确拒绝
    #（coachCategory 已恢复原口径：声明不持久化，提交静默忽略，见上方 E1 用例）

    def test_create_school_name_rejected_400(self):
        """批次 A：管理端契约本就不接受 schoolName 直写（归属走 schoolOrgId），
        extra=forbid 统一 400，字段可定位，不建人。"""
        before = Person.objects.count()
        resp = self._post(
            {
                "kinds": ["ATHLETE"],
                "realName": "归属拒绝",
                "idNumber": self.ID_CARD,
                "schoolName": "乙一小",
            }
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], "INVALID_ARGUMENT")
        self.assertIn("schoolName", json.dumps(body.get("data") or {}))
        self.assertEqual(Person.objects.count(), before)  # 无部分写入

    def test_update_school_name_rejected_400(self):
        """契约：管理端更新链同样不接受 schoolName 直写（归属只走 schoolOrgId）。"""
        _, person = self._create()
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE"],
                "realName": "王测试",
                "schoolName": "乙一小",
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], "INVALID_ARGUMENT")
        self.assertIn("schoolName", json.dumps(body.get("data") or {}))
        person.refresh_from_db()
        self.assertEqual(person.full_name, "王测试")  # 姓名未变
        self.assertEqual(person.school.code, "SCH-810100-201")  # 归属未变

    def test_create_mixed_payload_rejected_no_person_created(self):
        """批次 A：合法字段 + schoolName 混合 → 整单 400，education 也不得写入。"""
        before = Person.objects.count()
        resp = self._post(
            {
                "kinds": ["ATHLETE"],
                "realName": "混合创建",
                "idNumber": self.ID_CARD,
                "schoolOrgId": str(self.org_school.pk),
                "education": "本科",
                "schoolName": "乙一小",
            }
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertIn("schoolName", json.dumps(resp.json().get("data") or {}))
        self.assertEqual(Person.objects.count(), before)

    def test_update_mixed_payload_rejected_without_partial_write(self):
        """批次 A：合法变更(realName/education) + schoolName → 400 且零落库零留痕。"""
        _, person = self._create()
        from apps.persons.schemas import person_version

        before_version = person_version(person)
        logs_before = PersonChangeLog.objects.filter(person=person).count()
        audit_before = AuditLog.objects.count()
        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE"],
                "realName": "混合更新",
                "education": "本科",
                "schoolName": "乙一小",
                "version": before_version,
            },
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertIn("schoolName", json.dumps(resp.json().get("data") or {}))
        person.refresh_from_db()
        self.assertEqual(person.full_name, "王测试")  # 合法字段未写入
        self.assertIsNone(person.education)
        self.assertEqual(person_version(person), before_version)  # 版本未前进
        self.assertEqual(
            PersonChangeLog.objects.filter(person=person).count(), logs_before
        )
        self.assertEqual(AuditLog.objects.count(), audit_before)

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

    def test_update_education_persisted_and_read_back(self):
        """E1：education 真实持久化——响应回显 + 数据库列读回；coachCategory 仍不输出。"""
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
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["education"], "本科")
        self.assertNotIn("coachCategory", resp.json()["data"])  # 字典未签发，仍不持久化不回显
        person.refresh_from_db()
        self.assertEqual(person.education, "本科")  # 数据库读回，非仅响应字段

    def test_update_education_absent_keeps_value(self):
        """E1：更新其他字段未传 education → 原值不丢失（缺省不清空）。"""
        _, person = self._create(education="硕士")
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE"],
                "realName": "王改名",
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        self.assertEqual(person.education, "硕士")
        self.assertEqual(resp.json()["data"]["education"], "硕士")

    def test_update_education_blank_clears_value(self):
        """E1：传空串 → 主动清空（表单有输入框，覆盖写入语义）。"""
        _, person = self._create(education="硕士")
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE"],
                "realName": "王清空",
                "education": "  ",
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        self.assertIsNone(person.education)
        self.assertIsNone(resp.json()["data"]["education"])

    def test_create_education_persisted_and_read_back(self):
        """E1：创建链持久化 + GET 回显。"""
        data, person = self._create(education="博士")
        self.assertEqual(data["education"], "博士")
        person.refresh_from_db()
        self.assertEqual(person.education, "博士")
        resp = self.client.get(
            f"{PEOPLE}/{person.pk}", **self._auth(self.admin_token)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["education"], "博士")

    def test_update_education_too_long_rejected_no_write(self):
        """E1：超过 100 字符 → 400 校验错误，数据库原值不变。"""
        _, person = self._create(education="原值")
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE"],
                "realName": "王超长",
                "education": "长" * 101,
                "version": person_version(person),
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        person.refresh_from_db()
        self.assertEqual(person.education, "原值")  # 未被写入

    def test_update_person_non_admin_scope_denied(self):
        """写链现状钉住：学校端账号（无组织绑定）更新人员 → 403 FORBIDDEN
        （persons 写链权限点未授予学校端角色，A′ persons 域范围校验未落地；
        权限点层即拒，不进入数据范围判断——不在本轮扩大范围）。"""
        _, person = self._create()
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {
                "kinds": ["ATHLETE"],
                "realName": "越范围",
                "version": person_version(person),
            },
            token=self.uni_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")
        person.refresh_from_db()
        self.assertEqual(person.full_name, "王测试")  # 未被写入

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

    # ------------------------------------------------------ 问题 11：类别变更（显式移除历史双身份）

    def test_update_dual_kind_rejected(self):
        """问题 11：更新传入两个身份 → 400 INVALID_ARGUMENT。"""
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
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        # 确认未创建 CoachProfile
        self.assertFalse(CoachProfile.objects.filter(person=person).exists())

    def test_update_remove_empty_kind_deletes_profile(self):
        """问题 11：历史双身份（ORM 构造）显式移除空 Profile → 删除成功。"""
        # 先用 API 创建 ATHLETE 人员
        resp = self._post(
            {"kinds": ["ATHLETE"], "realName": "双师", "idNumber": "110101201305017890"}
        )
        person = Person.objects.get(pk=int(resp.json()["data"]["id"]))
        # ORM 直接构造历史双身份（模拟迁移前数据，不依赖 API 新建双身份）
        CoachProfile.objects.create(person=person)
        from apps.persons.services import _current_kinds
        self.assertEqual(_current_kinds(person), ["ATHLETE", "COACH"])
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
        """问题 11：历史双身份（ORM 构造）显式移除有数据 Profile → 409。"""
        resp = self._post(
            {"kinds": ["ATHLETE"], "realName": "证教练", "idNumber": "110101201305019012"}
        )
        person = Person.objects.get(full_name="证教练")
        # ORM 构造历史双身份 + COACH 有业务数据
        CoachProfile.objects.create(person=person, certificate_no="C2026001")
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
        """问题 11：历史双身份（ORM 构造）显式移除有数据 Profile → 409。"""
        resp = self._post(
            {"kinds": ["COACH"], "realName": "学籍员", "idNumber": "110101201305023456"}
        )
        person = Person.objects.get(full_name="学籍员")
        # ORM 构造历史双身份 + ATHLETE 有业务数据
        AthleteProfile.objects.create(person=person, grade_level="", student_no="G2026001")
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {"kinds": ["COACH"], "realName": "学籍员", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertTrue(AthleteProfile.objects.filter(person=person).exists())

    def test_kind_filter_matches_profile_existence(self):
        """kind 过滤与 kinds 合成同源：ORM 构造双类别人员在两种过滤下均命中。"""
        self._create()
        resp = self._post(
            {"kinds": ["ATHLETE"], "realName": "兼项员", "idNumber": "110101201305034567"}
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person = Person.objects.get(full_name="兼项员")
        # ORM 构造历史双身份
        CoachProfile.objects.create(person=person)
        resp = self._get_list({"kind": "ATHLETE"})
        names = [item["realName"] for item in resp.json()["data"]["items"]]
        self.assertIn("兼项员", names)
        resp = self._get_list({"kind": "COACH"})
        names = [item["realName"] for item in resp.json()["data"]["items"]]
        self.assertIn("兼项员", names)

    # ------------------------------------------------------ 问题 11 方案 B：kinds 可选

    def test_update_omit_kinds_keeps_dual_profiles(self):
        """问题 11 方案 B：历史双身份人员不传 kinds 更新普通字段 → 两 Profile 均保留。"""
        resp = self._post(
            {"kinds": ["ATHLETE"], "realName": "双身份员", "idNumber": "110101201305044444"}
        )
        person = Person.objects.get(full_name="双身份员")
        # ORM 构造历史双身份
        CoachProfile.objects.create(person=person)
        old_category = person.person_category
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {"realName": "双身份员改名", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person.refresh_from_db()
        # 两个 Profile 均保留
        self.assertTrue(AthleteProfile.objects.filter(person=person).exists())
        self.assertTrue(CoachProfile.objects.filter(person=person).exists())
        # person_category 不变
        self.assertEqual(person.person_category, old_category)
        # 不产生 kinds 变更日志
        log = PersonChangeLog.objects.filter(
            person=person, change_type=enums.ChangeType.UPDATE
        ).order_by("-id").first()
        kinds_changes = [c for c in (log.field_changes or []) if c["field"] == "kinds"]
        self.assertEqual(kinds_changes, [])

    def test_update_omit_kinds_keeps_single_identity(self):
        """问题 11 方案 B：单身份人员不传 kinds → 身份保持不变。"""
        _, person = self._create()  # ATHLETE
        old_category = person.person_category
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {"realName": "单身份改名", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["kinds"], ["ATHLETE"])
        person.refresh_from_db()
        self.assertEqual(person.person_category, old_category)
        self.assertTrue(AthleteProfile.objects.filter(person=person).exists())
        self.assertFalse(CoachProfile.objects.filter(person=person).exists())

    def test_update_omit_kinds_no_spurious_identity_log(self):
        """问题 11 方案 B：未传 kinds 时不产生虚假的身份变更日志条目。"""
        _, person = self._create()  # ATHLETE
        from apps.persons.schemas import person_version

        resp = self._put(
            person.pk,
            {"realName": "测日志", "version": person_version(person)},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        log = PersonChangeLog.objects.filter(
            person=person, change_type=enums.ChangeType.UPDATE
        ).order_by("-id").first()
        fields = [c["field"] for c in (log.field_changes or [])]
        self.assertNotIn("kinds", fields)
        self.assertNotIn("personCategory", fields)

    def test_service_layer_rejects_dual_kinds_directly(self):
        """问题 11：服务层直接调用 _kind_list 也拒绝两个身份，不依赖 Schema。"""
        from apps.persons.services import _kind_list
        from apps.common.errors import ApiError, INVALID_ARGUMENT

        with self.assertRaises(ApiError) as cm:
            _kind_list(["ATHLETE", "COACH"])
        self.assertEqual(cm.exception.code, INVALID_ARGUMENT)

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


class PersonAdminScopeTests(TestCase):
    """A′ 第一批：ADMIN 数据范围对齐（列表/搜索/详情/创建/转校/删除）。

    口径：全局 ADMIN（无组织绑定）全库；挂组织 ADMIN 仅授权组织子树内
    人员（Person.school ↔ Organization.code 真实映射）；幽灵/软删组织
    绑定默认拒绝不退化全库；UNIVERSITY/TEAM 读开放移至
    PersonThirdBatchScopeTests（A′ 第三批），CITY 保持拒绝。
    写测试断言数据库最终状态，不只看 HTTP 状态码。

    组织树夹具：
        ORG-PS(省,code=P-900) → ORG-CT(市,code=C-900100)
            → ORG-SA(学校A,code=SCH-900100-301) / ORG-SA2(学校A2,code=SCH-900100-302)
        ORG-PS → ORG-SB(学校B,code=SCH-900200-901，在市子树之外)
        ORG-DEAD（软删组织，挂 ADMIN 用）
    """

    @classmethod
    def setUpTestData(cls):
        cls._account_seq = 0

        def make_account(username, roles, org_id=None):
            cls._account_seq += 1
            return Account.objects.create(
                id=new_account_id(),
                username=username,
                display_name=username,
                roles=roles,
                password=make_password("pass-123456"),
                enabled=True,
                organization_id=org_id,
            )

        cls.global_admin = make_account("scope-global-admin", ["ADMIN"])
        cls.city_admin = make_account("scope-city-admin", ["ADMIN"], org_id="ORG-CT")
        # 注："绑定不存在的组织"被 Account.organization 外键约束排除（DB 不可能状态），
        # 不落库夹具；该边界以未保存实例的单元测试覆盖（见 test_nonexistent_org_binding_unit_denied）。
        cls.dead_admin = make_account("scope-dead-admin", ["ADMIN"], org_id="ORG-DEAD")
        cls.uni = make_account("scope-uni", ["UNIVERSITY"], org_id="ORG-SA")
        cls.team_acct = make_account("scope-team", ["TEAM"], org_id="ORG-SA")
        cls.city_acct = make_account("scope-city", ["CITY"], org_id="ORG-CT")
        cls.global_token = issue_access_token(cls.global_admin)[0]
        cls.scoped_admin_token = issue_access_token(cls.city_admin)[0]
        cls.dead_token = issue_access_token(cls.dead_admin)[0]
        cls.uni_token = issue_access_token(cls.uni)[0]
        cls.team_token = issue_access_token(cls.team_acct)[0]
        cls.city_token = issue_access_token(cls.city_acct)[0]

        prov = Region.objects.create(name="范围省", code="900000", level=1)
        city = Region.objects.create(
            name="范围市", code="900100", level=2, parent=prov
        )
        prov2 = Region.objects.create(name="外省", code="902000", level=1)
        cls.school_a = School.objects.create(
            name="范围一小", code="SCH-900100-301", school_type="PRIMARY", region=city
        )
        school_a2 = School.objects.create(
            name="范围二小", code="SCH-900100-302", school_type="PRIMARY", region=city
        )
        cls.school_a2 = school_a2
        cls.school_b = School.objects.create(
            name="界外中学", code="SCH-900200-901", school_type="JUNIOR", region=prov2
        )
        Organization.objects.create(
            id="ORG-PS", code="P-900", name="范围省组织", org_type="PROVINCE"
        )
        Organization.objects.create(
            id="ORG-CT", code="C-900100", name="范围市组织", org_type="CITY", parent_id="ORG-PS"
        )
        Organization.objects.create(
            id="ORG-SA", code="SCH-900100-301", name="范围一小组织",
            org_type="SCHOOL", parent_id="ORG-CT",
        )
        Organization.objects.create(
            id="ORG-SA2", code="SCH-900100-302", name="范围二小组织",
            org_type="SCHOOL", parent_id="ORG-CT",
        )
        Organization.objects.create(
            id="ORG-SB", code="SCH-900200-901", name="界外中学组织",
            org_type="SCHOOL", parent_id="ORG-PS",
        )
        Organization.objects.create(
            id="ORG-DEAD", code="C-DEAD", name="已删组织", org_type="CITY", parent_id="ORG-PS"
        )
        # 软删组织绑定：模拟组织被删除后账号绑定未清理（幽灵绑定）
        Organization.objects.filter(pk="ORG-DEAD").update(deleted_at=timezone.now())
        cls.ORG_SA_ID = "ORG-SA"
        cls.ORG_SA2_ID = "ORG-SA2"
        cls.ORG_SB_ID = "ORG-SB"

        def make_person(no, name, id_number, school):
            return Person.objects.create(
                person_no=f"P2026{no:08d}",
                full_name=name,
                id_card_cipher=encrypt_text(id_number),
                id_card_hash=lookup_hash(id_number),
                id_card_masked=mask_id_number(id_number),
                person_category=enums.PersonCategory.ATHLETE,
                audit_status=enums.AuditStatus.APPROVED,
                school=school,
            )

        cls.p_a1 = make_person(1, "树内一", "110101201301011111", cls.school_a)
        cls.p_a2 = make_person(2, "树内二", "110101201301012222", cls.school_a)
        cls.p_a2school = make_person(3, "树内三", "110101201301013333", school_a2)
        cls.p_b = make_person(4, "树外人", "110101201301014444", cls.school_b)
        cls.p_none = make_person(5, "无校人", "110101201301015555", None)

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

    def _delete(self, path, version=None, token=None):
        if version is not None:
            path = f"{path}?version={version}"
        return self.client.delete(path, **self._auth(token))

    def _create_payload(self, real_name, id_number, school_org_id="keep"):
        payload = {
            "kinds": ["ATHLETE"],
            "realName": real_name,
            "idNumber": id_number,
        }
        if school_org_id != "keep":
            payload["schoolOrgId"] = school_org_id
        return payload

    # ------------------------------------------------------------ 列表/搜索

    def test_global_admin_list_covers_all_persons(self):
        resp = self._get(PEOPLE, token=self.global_token)
        self.assertEqual(resp.status_code, 200)
        names = {i["realName"] for i in resp.json()["data"]["items"]}
        # 全库：树内、树外、无学校归属人员均可见
        self.assertLessEqual({"树内一", "树内二", "树内三", "树外人", "无校人"}, names)

    def test_scoped_admin_list_limited_to_subtree(self):
        resp = self._get(PEOPLE, token=self.scoped_admin_token)
        self.assertEqual(resp.status_code, 200)
        names = {i["realName"] for i in resp.json()["data"]["items"]}
        self.assertEqual(names, {"树内一", "树内二", "树内三"})
        # 数据库不因过滤而改变：树外人员仍然存在，只是不可见
        self.assertTrue(Person.objects.filter(pk=self.p_b.pk).exists())

    def test_scoped_admin_search_limited_to_subtree(self):
        resp = self._post(PEOPLE + "/search", {"keyword": "树"}, token=self.scoped_admin_token)
        self.assertEqual(resp.status_code, 200)
        names = {p["realName"] for p in resp.json()["data"]}
        self.assertEqual(names, {"树内一", "树内二", "树内三"})

    def test_scoped_admin_person_without_school_invisible(self):
        # school=None 的人员对挂组织 ADMIN 不可见（deny-by-default），
        # 全局 ADMIN 仍可见
        resp = self._get(
            PEOPLE, token=self.scoped_admin_token, query={"keyword": "无校人"}
        )
        self.assertEqual(resp.json()["data"]["total"], 0)
        resp = self._get(PEOPLE, token=self.global_token, query={"keyword": "无校人"})
        self.assertEqual(resp.json()["data"]["total"], 1)

    # ------------------------------------------------------------ 详情

    def test_scoped_admin_detail_in_tree_ok_out_of_tree_404(self):
        resp = self._get(f"{PEOPLE}/{self.p_a1.pk}", token=self.scoped_admin_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["realName"], "树内一")
        resp = self._get(f"{PEOPLE}/{self.p_b.pk}", token=self.scoped_admin_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_global_admin_detail_out_of_tree_ok(self):
        resp = self._get(f"{PEOPLE}/{self.p_b.pk}", token=self.global_token)
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------ 创建

    def test_scoped_admin_create_in_scope_ok(self):
        resp = self._post(
            PEOPLE,
            self._create_payload("范围内新人", "110101201302011234", self.ORG_SA_ID),
            token=self.scoped_admin_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person = Person.objects.get(full_name="范围内新人")
        self.assertEqual(person.school_id, self.school_a.pk)

    def test_scoped_admin_create_out_of_scope_403_no_partial_write(self):
        before = Person.objects.count()
        resp = self._post(
            PEOPLE,
            self._create_payload("越界新人", "110101201302021234", self.ORG_SB_ID),
            token=self.scoped_admin_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        self.assertEqual(Person.objects.count(), before)
        self.assertFalse(Person.objects.filter(full_name="越界新人").exists())

    def test_scoped_admin_create_without_school_403(self):
        # 挂组织 ADMIN 创建必须指定范围内学校：school=None 落在子树之外
        before = Person.objects.count()
        resp = self._post(
            PEOPLE, self._create_payload("无校新人", "110101201302031234", None),
            token=self.scoped_admin_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        self.assertEqual(Person.objects.count(), before)

    def test_scoped_admin_create_unknown_org_403(self):
        # schoolOrgId 指向不存在组织：范围校验先于解析 → DATA_SCOPE_DENIED
        before = Person.objects.count()
        resp = self._post(
            PEOPLE,
            self._create_payload("幽灵校新人", "110101201302041234", "999999"),
            token=self.scoped_admin_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        self.assertEqual(Person.objects.count(), before)

    def test_global_admin_create_without_school_still_ok(self):
        # 全局 ADMIN 保持原语义：schoolOrgId 可选
        resp = self._post(
            PEOPLE, self._create_payload("全局无校新人", "110101201302051234", None),
            token=self.global_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        person = Person.objects.get(full_name="全局无校新人")
        self.assertIsNone(person.school_id)

    # ------------------------------------------------------------ 转校

    def test_scoped_admin_transfer_in_scope_ok(self):
        from apps.persons.schemas import person_version

        resp = self._put(
            f"{PEOPLE}/{self.p_a1.pk}",
            {
                "kinds": ["ATHLETE"],
                "realName": "树内一",
                "schoolOrgId": self.ORG_SA2_ID,  # 市子树内另一所学校
                "version": person_version(self.p_a1),
            },
            token=self.scoped_admin_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.p_a1.refresh_from_db()
        self.assertEqual(self.p_a1.school_id, self.school_a2.pk)

    def test_scoped_admin_transfer_reflects_school_name_in_response(self):
        """契约：转校成功后，响应与详情的 schoolName 均派生自实际学校关联。"""
        from apps.persons.schemas import person_version

        resp = self._put(
            f"{PEOPLE}/{self.p_a1.pk}",
            {
                "kinds": ["ATHLETE"],
                "realName": "树内一",
                "schoolOrgId": self.ORG_SA2_ID,  # 市子树内另一所学校
                "version": person_version(self.p_a1),
            },
            token=self.scoped_admin_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["schoolName"], "范围二小")  # 更新响应跟随新归属
        self.p_a1.refresh_from_db()
        self.assertEqual(self.p_a1.school_id, self.school_a2.pk)  # DB 指向目标学校
        detail = self._get(f"{PEOPLE}/{self.p_a1.pk}", token=self.scoped_admin_token)
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()["data"]["schoolName"], "范围二小")  # 详情同源

    def test_scoped_admin_transfer_out_of_scope_403_school_unchanged(self):
        from apps.persons.schemas import person_version

        resp = self._put(
            f"{PEOPLE}/{self.p_a1.pk}",
            {
                "kinds": ["ATHLETE"],
                "realName": "树内一",
                "schoolOrgId": self.ORG_SB_ID,  # 市子树之外的学校
                "version": person_version(self.p_a1),
            },
            token=self.scoped_admin_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        self.p_a1.refresh_from_db()
        self.assertEqual(self.p_a1.school_id, self.school_a.pk)  # 学校归属未变

    def test_scoped_admin_update_without_schoolorgid_keeps_school(self):
        # 未传 schoolOrgId：保持"不修改学校归属"语义，且不因范围校验被拒
        from apps.persons.schemas import person_version

        resp = self._put(
            f"{PEOPLE}/{self.p_a1.pk}",
            {
                "kinds": ["ATHLETE"],
                "realName": "树内一改名",
                "version": person_version(self.p_a1),
            },
            token=self.scoped_admin_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.p_a1.refresh_from_db()
        self.assertEqual(self.p_a1.full_name, "树内一改名")
        self.assertEqual(self.p_a1.school_id, self.school_a.pk)

    # ------------------------------------------------------------ 删除

    def test_scoped_admin_delete_in_tree_ok_out_of_tree_404(self):
        from apps.persons.schemas import person_version

        resp = self._delete(
            f"{PEOPLE}/{self.p_a2school.pk}",
            version=person_version(self.p_a2school),
            token=self.scoped_admin_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.p_a2school.refresh_from_db()
        self.assertTrue(self.p_a2school.is_deleted)
        resp = self._delete(
            f"{PEOPLE}/{self.p_b.pk}",
            version=person_version(self.p_b),
            token=self.scoped_admin_token,
        )
        self.assertEqual(resp.status_code, 404)
        self.p_b.refresh_from_db()
        self.assertFalse(self.p_b.is_deleted)  # 树外人员未被删除

    # ------------------------------------------------------------ 幽灵绑定

    def test_nonexistent_org_binding_unit_denied(self):
        """绑定不存在的组织（幽灵绑定）：默认拒绝，不得退化为全库。

        Account.organization 为真实外键，"绑定不存在组织"在 DB 层不可能出现，
        故以未保存 Account 实例直接验证 apply_person_scope：scope_org_codes
        对该实例返回 []（空范围），在评估 queryset 前即抛 DATA_SCOPE_DENIED，
        不产生任何落库（也无 FK 违例）。API 层可表达的幽灵绑定（组织已软删）
        由 test_soft_deleted_binding_admin_denied_everywhere 覆盖。
        """
        from apps.common.errors import ApiError
        from apps.persons.services import apply_person_scope

        ghost = Account(organization_id="ORG-GHOST", roles=["ADMIN"])
        with self.assertRaises(ApiError) as ctx:
            apply_person_scope(Person.objects.all(), ghost)
        self.assertEqual(ctx.exception.code, "DATA_SCOPE_DENIED")

    def test_soft_deleted_binding_admin_denied_everywhere(self):
        # 绑定已软删除的组织：同样默认拒绝
        resp = self._get(PEOPLE, token=self.dead_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        resp = self._post(
            PEOPLE,
            self._create_payload("死链管理员新人", "110101201302071234", self.ORG_SA_ID),
            token=self.dead_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Person.objects.filter(full_name="死链管理员新人").exists())

    # ------------------------------------------------------------ 非 ADMIN 不变

    def test_non_admin_roles_still_denied(self):
        """A′ 第三批拆分后本类保留的拒绝面：TEAM/CITY 读仍拒绝。

        UNIVERSITY 本校读已开放（正向矩阵见 PersonThirdBatchScopeTests）；
        本测试保留：TEAM/CITY 的列表 403 + 三角色写链 FORBIDDEN。
        """
        for token in (self.team_token, self.city_token):
            # 查询类权限点含 TEAM/CITY → 落到数据范围层拒绝
            resp = self._get(PEOPLE, token=token)
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        for token in (self.uni_token, self.team_token, self.city_token):
            # 写链权限点仅登记 ADMIN → 角色层即 FORBIDDEN（与既有
            # test_create_forbidden_for_university 口径一致）
            resp = self._post(
                PEOPLE,
                self._create_payload("非管理员新人", "110101201302081234", self.ORG_SA_ID),
                token=token,
            )
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json()["code"], "FORBIDDEN")
        self.assertFalse(Person.objects.filter(full_name="非管理员新人").exists())


class PersonThirdBatchScopeTests(TestCase):
    """A′ 第三批（2026-10-29）：UNIVERSITY 本校 / TEAM 本队 CURRENT 成员读开放。

    口径（成员4 专项①，业务决定 5+6 项确认）：
    - UNIVERSITY：绑定节点须为学校类（SCHOOL/UNIVERSITY），仅见子树内
      学校类节点 code 对应人员（Person.school.code 映射；无 school 归属
      不可见，不经任职球队的学校归属反推）；
    - TEAM：绑定节点须为 TEAM（code==team_no），仅见绑定队正式 CURRENT
      球员/教练（TeamMember/TeamCoach 各自独立按 CURRENT 过滤），EXITED
      不可见，不因子树扩展到子球队，不经学校归属反推；
    - CITY/PROVINCE_REVIEWER：403 DATA_SCOPE_DENIED（市级范围未开放）；
    - 范围外对象 404 不泄露存在性；写链不开放（403 FORBIDDEN，零落库）；
    - ADMIN 行为不变（PersonAdminScopeTests 继续覆盖第一批口径）。

    组织树夹具：
        ORG-3PS(省,P-950) → ORG-3CT(市,C-950100)
            → ORG-3SA(SCHOOL,SCH-950100-401)
                → ORG-3TA(TEAM,T-950100-01)      [team_a，绑定队]
                    → ORG-3TA2(TEAM,T-950100-02) [team_a2，病态子球队]
            → ORG-3UA(UNIVERSITY,SCH-950100-501) [高校节点]
        ORG-3PS → ORG-3SB(SCHOOL,SCH-950200-901)
            → ORG-3TB(TEAM,T-950200-91)          [team_b，界外队]
    """

    @classmethod
    def setUpTestData(cls):
        cls._seq = 0

        def make_account(username, roles, org_id=None):
            cls._seq += 1
            return Account.objects.create(
                id=new_account_id(),
                username=username,
                display_name=username,
                roles=roles,
                password=make_password("pass-123456"),
                enabled=True,
                organization_id=org_id,
            )

        cls.global_admin = make_account("tb-global-admin", ["ADMIN"])
        cls.uni_sa = make_account("tb-uni-sa", ["UNIVERSITY"], org_id="ORG-3SA")
        cls.uni_ua = make_account("tb-uni-ua", ["UNIVERSITY"], org_id="ORG-3UA")
        cls.uni_city = make_account("tb-uni-city", ["UNIVERSITY"], org_id="ORG-3CT")
        cls.team_ta = make_account("tb-team-ta", ["TEAM"], org_id="ORG-3TA")
        cls.team_ghost = make_account("tb-team-ghost", ["TEAM"], org_id="ORG-3SA")
        cls.city_acct = make_account("tb-city", ["CITY"], org_id="ORG-3CT")
        cls.prov_acct = make_account(
            "tb-prov", ["PROVINCE_REVIEWER"], org_id="ORG-3PS"
        )
        cls.global_token = issue_access_token(cls.global_admin)[0]
        cls.uni_sa_token = issue_access_token(cls.uni_sa)[0]
        cls.uni_ua_token = issue_access_token(cls.uni_ua)[0]
        cls.uni_city_token = issue_access_token(cls.uni_city)[0]
        cls.team_ta_token = issue_access_token(cls.team_ta)[0]
        cls.team_ghost_token = issue_access_token(cls.team_ghost)[0]
        cls.city_token = issue_access_token(cls.city_acct)[0]
        cls.prov_token = issue_access_token(cls.prov_acct)[0]

        prov = Region.objects.create(name="三批省", code="950000", level=1)
        city = Region.objects.create(name="三批市", code="950100", level=2, parent=prov)
        prov2 = Region.objects.create(name="三批外省", code="952000", level=1)
        cls.school_a = School.objects.create(
            name="三批一小", code="SCH-950100-401", school_type="PRIMARY", region=city
        )
        school_u = School.objects.create(
            name="三批高校", code="SCH-950100-501", school_type="PRIMARY", region=city
        )
        school_b = School.objects.create(
            name="三批界外校", code="SCH-950200-901", school_type="JUNIOR", region=prov2
        )
        Organization.objects.create(id="ORG-3PS", code="P-950", name="三批省组织", org_type="PROVINCE")
        Organization.objects.create(
            id="ORG-3CT", code="C-950100", name="三批市组织", org_type="CITY", parent_id="ORG-3PS"
        )
        Organization.objects.create(
            id="ORG-3SA", code="SCH-950100-401", name="三批一小组织",
            org_type="SCHOOL", parent_id="ORG-3CT",
        )
        Organization.objects.create(
            id="ORG-3UA", code="SCH-950100-501", name="三批高校组织",
            org_type="UNIVERSITY", parent_id="ORG-3CT",
        )
        Organization.objects.create(
            id="ORG-3TA", code="T-950100-01", name="三批一队组织",
            org_type="TEAM", parent_id="ORG-3SA",
        )
        # 病态子球队：TEAM 节点下再挂 TEAM 节点（模型不禁止），
        # 用于断言 TEAM 范围不因子树放大
        Organization.objects.create(
            id="ORG-3TA2", code="T-950100-02", name="三批子队组织",
            org_type="TEAM", parent_id="ORG-3TA",
        )
        Organization.objects.create(
            id="ORG-3SB", code="SCH-950200-901", name="三批界外校组织",
            org_type="SCHOOL", parent_id="ORG-3PS",
        )
        Organization.objects.create(
            id="ORG-3TB", code="T-950200-91", name="三批界外队组织",
            org_type="TEAM", parent_id="ORG-3SB",
        )

        team_a = Team.objects.create(
            team_no="T-950100-01", name="三批一队", category_code="U12",
            school=cls.school_a, region=city,
        )
        team_a2 = Team.objects.create(
            team_no="T-950100-02", name="三批子队", category_code="U12",
            school=cls.school_a, region=city,
        )
        Team.objects.create(
            team_no="T-950200-91", name="三批界外队", category_code="U12",
            school=school_b, region=prov2,
        )
        joined = date(2026, 1, 1)

        def make_person(no, name, id_number, school, category):
            return Person.objects.create(
                person_no=f"P2026{no:08d}",
                full_name=name,
                id_card_cipher=encrypt_text(id_number),
                id_card_hash=lookup_hash(id_number),
                id_card_masked=mask_id_number(id_number),
                person_category=category,
                audit_status=enums.AuditStatus.APPROVED,
                school=school,
            )

        cls.cur_player = make_person(
            1, "现役球员", "110101201501011111", cls.school_a, enums.PersonCategory.ATHLETE
        )
        cls.exited_player = make_person(
            2, "离队球员", "110101201501012222", cls.school_a, enums.PersonCategory.ATHLETE
        )
        cls.no_rel = make_person(
            3, "无关联员", "110101201501013333", cls.school_a, enums.PersonCategory.ATHLETE
        )
        cls.a2_player = make_person(
            4, "子队球员", "110101201501014444", cls.school_a, enums.PersonCategory.ATHLETE
        )
        cls.cur_coach = make_person(
            5, "现役教练", "110101198801015555", cls.school_a, enums.PersonCategory.COACH
        )
        cls.exited_coach = make_person(
            6, "离队教练", "110101198801016666", cls.school_a, enums.PersonCategory.COACH
        )
        cls.none_coach = make_person(
            7, "无校教练", "110101198801017777", None, enums.PersonCategory.COACH
        )
        cls.u_player = make_person(
            8, "高校球员", "110101201501018888", school_u, enums.PersonCategory.ATHLETE
        )
        cls.b_player = make_person(
            9, "界外球员", "110101201501019999", school_b, enums.PersonCategory.ATHLETE
        )

        TeamMember.objects.create(team=team_a, person=cls.cur_player, joined_at=joined)
        TeamMember.objects.create(
            team=team_a, person=cls.exited_player, joined_at=joined,
            status=enums.RelationStatus.EXITED,
        )
        TeamCoach.objects.create(
            team=team_a, person=cls.cur_coach, duty=enums.CoachDuty.HEAD_COACH,
            joined_at=joined,
        )
        TeamCoach.objects.create(
            team=team_a, person=cls.exited_coach, duty=enums.CoachDuty.HEAD_COACH,
            joined_at=joined, status=enums.RelationStatus.EXITED,
        )
        TeamMember.objects.create(team=team_a2, person=cls.a2_player, joined_at=joined)
        TeamMember.objects.create(
            team=Team.objects.get(team_no="T-950200-91"), person=cls.b_player,
            joined_at=joined,
        )

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

    # ------------------------------------------------------------ 鉴权与拒绝面

    def test_unauthenticated_list_401(self):
        resp = self._get(PEOPLE)
        self.assertEqual(resp.status_code, 401)

    def test_city_and_province_reviewer_denied(self):
        for token in (self.city_token, self.prov_token):
            resp = self._get(PEOPLE, token=token)
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
            self.assertEqual(resp.json()["data"], None)
        # 数据未被过滤动作改变
        self.assertEqual(Person.objects.count(), 9)

    def test_team_ghost_or_missing_binding_denied(self):
        # TEAM 绑定学校节点（类型不符）：默认拒绝，不得按子树映射放行
        resp = self._get(PEOPLE, token=self.team_ghost_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    # ------------------------------------------------------------ UNIVERSITY 本校

    def test_university_list_school_scope(self):
        resp = self._get(PEOPLE, token=self.uni_sa_token)
        self.assertEqual(resp.status_code, 200)
        names = {i["realName"] for i in resp.json()["data"]["items"]}
        # 本校全部人员（含 EXITED/无关联/教练）：学校归属口径，与球队关系无关
        self.assertEqual(
            names,
            {"现役球员", "离队球员", "无关联员", "子队球员", "现役教练", "离队教练"},
        )
        self.assertNotIn("界外球员", names)
        self.assertNotIn("无校教练", names)

    def test_university_university_type_node_scope(self):
        # 绑定高校节点（org_type=UNIVERSITY）：仅见该校人员
        resp = self._get(PEOPLE, token=self.uni_ua_token)
        self.assertEqual(resp.status_code, 200)
        names = {i["realName"] for i in resp.json()["data"]["items"]}
        self.assertEqual(names, {"高校球员"})

    def test_university_wrong_node_type_denied(self):
        # UNIVERSITY 绑定市节点（非学校类）：403
        resp = self._get(PEOPLE, token=self.uni_city_token)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")

    def test_university_detail_in_scope_200_out_404(self):
        resp = self._get(f"{PEOPLE}/{self.cur_player.pk}", token=self.uni_sa_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["realName"], "现役球员")
        for person in (self.b_player, self.u_player, self.none_coach):
            resp = self._get(f"{PEOPLE}/{person.pk}", token=self.uni_sa_token)
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_university_cannot_see_schoolless_coach(self):
        # 无 school 归属教练：不经其球队关系反推（本类夹具中该教练无球队关系）
        resp = self._get(PEOPLE, token=self.uni_sa_token, query={"keyword": "无校教练"})
        self.assertEqual(resp.json()["data"]["total"], 0)
        # 全局 ADMIN 仍可见（ADMIN 行为不变）
        resp = self._get(PEOPLE, token=self.global_token, query={"keyword": "无校教练"})
        self.assertEqual(resp.json()["data"]["total"], 1)

    # ------------------------------------------------------------ TEAM 本队 CURRENT

    def test_team_list_only_current_members(self):
        resp = self._get(PEOPLE, token=self.team_ta_token)
        self.assertEqual(resp.status_code, 200)
        names = {i["realName"] for i in resp.json()["data"]["items"]}
        # 仅绑定队 CURRENT 球员+教练（两条关联路径各自过滤）
        self.assertEqual(names, {"现役球员", "现役教练"})
        # 数据库不因过滤而改变：EXITED 行仍存在，只是不可见
        self.assertTrue(
            TeamMember.objects.filter(
                person=self.exited_player, status=enums.RelationStatus.EXITED
            ).exists()
        )
        self.assertTrue(
            TeamCoach.objects.filter(
                person=self.exited_coach, status=enums.RelationStatus.EXITED
            ).exists()
        )

    def test_team_current_member_and_coach_detail_ok(self):
        resp = self._get(f"{PEOPLE}/{self.cur_player.pk}", token=self.team_ta_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["realName"], "现役球员")
        # 教练路径独立验证（TeamCoach CURRENT）
        resp = self._get(f"{PEOPLE}/{self.cur_coach.pk}", token=self.team_ta_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["realName"], "现役教练")

    def test_team_exited_members_404(self):
        for person in (self.exited_player, self.exited_coach):
            resp = self._get(f"{PEOPLE}/{person.pk}", token=self.team_ta_token)
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_team_unrelated_persons_404(self):
        # 同校但无任何正式关联的人员、无校教练：均不可见（不经学校归属反推）
        for person in (self.no_rel, self.none_coach):
            resp = self._get(f"{PEOPLE}/{person.pk}", token=self.team_ta_token)
            self.assertEqual(resp.status_code, 404)

    def test_team_subtree_other_team_not_visible(self):
        # 病态子球队（TEAM 节点下挂 TEAM 节点）成员：不因子树放大而可见
        resp = self._get(f"{PEOPLE}/{self.a2_player.pk}", token=self.team_ta_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_team_cross_team_404(self):
        resp = self._get(f"{PEOPLE}/{self.b_player.pk}", token=self.team_ta_token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    # ------------------------------------------------------------ 写链与 ADMIN 回归

    def test_write_chain_closed_for_non_admin(self):
        before = Person.objects.count()
        payload = {
            "kinds": ["ATHLETE"],
            "realName": "三批越权新人",
            "idNumber": "110101201901011234",
            "schoolOrgId": "ORG-3SA",
        }
        for token in (self.uni_sa_token, self.team_ta_token, self.city_token):
            resp = self._post(PEOPLE, payload, token=token)
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json()["code"], "FORBIDDEN")
        self.assertEqual(Person.objects.count(), before)
        self.assertFalse(Person.objects.filter(full_name="三批越权新人").exists())

    def test_global_admin_unchanged(self):
        # ADMIN 行为不变：全局管理员可见全部（含无校归属人员）
        resp = self._get(PEOPLE, token=self.global_token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 9)


class PersonReviewTests(TestCase):
    """P0-2（2026-10-28）：学校端档案审核 POST /people/{id}/review。

    口径（统一业务规则 + D1 对齐）：
    - 权限点 personReview 仅 UNIVERSITY（ADMIN 不因数据范围获得审核权，
      CITY/PROVINCE_REVIEWER 只审报名，TEAM 维护≠审核）；
    - 数据范围：UNIVERSITY 所属组织子树（Person.school.code 编码映射，
      同材料域 D1/D5 口径），范围外 404 不泄露存在性；无有效组织范围 403；
    - 状态机：仅 PENDING → APPROVED/REJECTED，其余 409；REJECT 必填 reason；
    - 留痕：PersonChangeLog(change_type=AUDIT) 记 audit_status old/new 与
      审核意见；changed_by 恒空（D4 待批）。
    写测试断言数据库最终状态，不只看 HTTP 状态码。

    组织树夹具（同 PersonAdminScopeTests 风格）：
        ORG-RPS(省,P-950) → ORG-RCT(市,C-950100) → ORG-RSA(学校A,SCH-950100-401)
        ORG-RPS → ORG-RSB(学校B,SCH-950200-402，市子树之外)
        ORG-RDEAD（软删组织，幽灵绑定用）
    """

    @classmethod
    def setUpTestData(cls):
        def make_account(username, roles, org_id=None):
            return Account.objects.create(
                id=new_account_id(),
                username=username,
                display_name=username,
                roles=roles,
                password=make_password("pass-123456"),
                enabled=True,
                organization_id=org_id,
            )

        cls.uni_a = make_account("rv-uni-a", ["UNIVERSITY"], org_id="ORG-RSA")
        cls.uni_dead = make_account("rv-uni-dead", ["UNIVERSITY"], org_id="ORG-RDEAD")
        cls.admin = make_account("rv-admin", ["ADMIN"])
        cls.city_acct = make_account("rv-city", ["CITY"], org_id="ORG-RCT")
        cls.prov_acct = make_account("rv-prov", ["PROVINCE_REVIEWER"], org_id="ORG-RPS")
        cls.team_acct = make_account("rv-team", ["TEAM"], org_id="ORG-RSA")
        cls.uni_a_token = issue_access_token(cls.uni_a)[0]
        cls.uni_dead_token = issue_access_token(cls.uni_dead)[0]
        cls.admin_token = issue_access_token(cls.admin)[0]
        cls.city_token = issue_access_token(cls.city_acct)[0]
        cls.prov_token = issue_access_token(cls.prov_acct)[0]
        cls.team_token = issue_access_token(cls.team_acct)[0]

        prov = Region.objects.create(name="审核省", code="950000", level=1)
        city = Region.objects.create(name="审核市", code="950100", level=2, parent=prov)
        prov2 = Region.objects.create(name="审核界外省", code="952000", level=1)
        cls.school_a = School.objects.create(
            name="审核一小", code="SCH-950100-401", school_type="PRIMARY", region=city
        )
        cls.school_b = School.objects.create(
            name="审核界外校", code="SCH-950200-402", school_type="JUNIOR", region=prov2
        )
        Organization.objects.create(
            id="ORG-RPS", code="P-950", name="审核省组织", org_type="PROVINCE"
        )
        Organization.objects.create(
            id="ORG-RCT", code="C-950100", name="审核市组织",
            org_type="CITY", parent_id="ORG-RPS",
        )
        Organization.objects.create(
            id="ORG-RSA", code="SCH-950100-401", name="审核一小组织",
            org_type="SCHOOL", parent_id="ORG-RCT",
        )
        Organization.objects.create(
            id="ORG-RSB", code="SCH-950200-402", name="审核界外校组织",
            org_type="SCHOOL", parent_id="ORG-RPS",
        )
        Organization.objects.create(
            id="ORG-RDEAD", code="C-RDEAD", name="审核已删组织",
            org_type="CITY", parent_id="ORG-RPS",
        )
        Organization.objects.filter(pk="ORG-RDEAD").update(deleted_at=timezone.now())

        def make_person(no, name, id_number, school, audit_status):
            return Person.objects.create(
                person_no=f"P2026{no:08d}",
                full_name=name,
                id_card_cipher=encrypt_text(id_number),
                id_card_hash=lookup_hash(id_number),
                id_card_masked=mask_id_number(id_number),
                person_category=enums.PersonCategory.ATHLETE,
                audit_status=audit_status,
                school=school,
            )

        cls.p_pending_a = make_person(
            1, "待审甲", "110101201401011111", cls.school_a, enums.AuditStatus.PENDING
        )
        cls.p_draft_a = make_person(
            2, "草稿乙", "110101201401012222", cls.school_a, enums.AuditStatus.DRAFT
        )
        cls.p_approved_a = make_person(
            3, "已过丙", "110101201401013333", cls.school_a, enums.AuditStatus.APPROVED
        )
        cls.p_pending_b = make_person(
            4, "界外丁", "110101201401014444", cls.school_b, enums.AuditStatus.PENDING
        )

        # P1-5 防自审：直接 DB 创建的人员无 AuditLog，需补录创建审计记录
        # （操作人为 admin，非审核人 uni_a），否则自审校验因无审计证据而阻断。
        for person in (cls.p_pending_a, cls.p_draft_a, cls.p_approved_a, cls.p_pending_b):
            AuditLog.objects.create(
                id=f"log-p-review-{person.pk}",
                log_type="OPERATION",
                operator_account_id=str(cls.admin.pk),
                action="person.create",
                object_type="Person",
                object_id=str(person.pk),
                outcome="SUCCESS",
            )

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}

    def _post(self, path, payload, token=None):
        return self.client.post(
            path, data=json.dumps(payload), content_type="application/json",
            **self._auth(token),
        )

    def _body(self, person, decision="APPROVE", **overrides):
        from apps.persons.schemas import person_version

        payload = {"decision": decision, "version": person_version(person)}
        payload.update(overrides)
        return payload

    def _audit_log(self, person):
        return PersonChangeLog.objects.filter(
            person_id=person.pk, change_type=enums.ChangeType.AUDIT
        )

    # ------------------------------------------------------------ 权限点

    def test_review_permission_roles_only_university(self):
        from apps.common.permissions import operation_roles

        # D1 口径：普通审核权限仅学校端（对齐 materialReview）。
        self.assertEqual(operation_roles("personReview"), ("UNIVERSITY",))

    def test_review_denied_for_non_university_roles(self):
        """ADMIN/CITY/PROVINCE_REVIEWER/TEAM 调审核 → 权限点 403 FORBIDDEN，
        先于数据范围判定；档案状态与审核留痕零变更。"""
        for token in (
            self.admin_token,
            self.city_token,
            self.prov_token,
            self.team_token,
        ):
            resp = self._post(
                f"{PEOPLE}/{self.p_pending_a.pk}/review",
                self._body(self.p_pending_a),
                token=token,
            )
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json()["code"], "FORBIDDEN")
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.PENDING)
        self.assertFalse(self._audit_log(self.p_pending_a).exists())

    # ------------------------------------------------------------ 审核正向（本校）

    def test_review_own_school_person_approved(self):
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.APPROVED)
        entry = self._audit_log(self.p_pending_a).get()
        self.assertEqual(
            entry.field_changes,
            [
                {
                    "field": "audit_status",
                    "old": enums.AuditStatus.PENDING.value,
                    "new": enums.AuditStatus.APPROVED.value,
                }
            ],
        )
        self.assertEqual(entry.reason, "审核通过")
        # changed_by 恒空：底座账号 ID 映射未定（D4 待批），同材料域口径
        self.assertIsNone(entry.changed_by)

    def test_review_reject_requires_reason(self):
        """REJECT 未填 reason → 400，状态与留痕零变更。"""
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a, decision="REJECT"),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 400)
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.PENDING)
        self.assertFalse(self._audit_log(self.p_pending_a).exists())

    def test_review_reject_with_reason_persists(self):
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(
                self.p_pending_a, decision="REJECT", reason="资格证明材料不全"
            ),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.REJECTED)
        self.assertEqual(self._audit_log(self.p_pending_a).get().reason, "资格证明材料不全")

    # ------------------------------------------------------------ 状态机与越权

    def test_review_non_pending_state_conflict(self):
        """DRAFT（未提交复核）与 APPROVED（重复审核）→ 409，零变更。"""
        for person, label in (
            (self.p_draft_a, "DRAFT"),
            (self.p_approved_a, "APPROVED"),
        ):
            resp = self._post(
                f"{PEOPLE}/{person.pk}/review", self._body(person), token=self.uni_a_token
            )
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
            person.refresh_from_db()
            self.assertEqual(person.audit_status, enums.AuditStatus(label).value)

    def test_review_version_conflict(self):
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a, version=1),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.PENDING)

    def test_review_other_school_person_404(self):
        """范围外（他校）档案 404：不泄露存在性，状态与留痕零变更。"""
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_b.pk}/review",
            self._body(self.p_pending_b),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 404)
        self.p_pending_b.refresh_from_db()
        self.assertEqual(self.p_pending_b.audit_status, enums.AuditStatus.PENDING)
        self.assertFalse(self._audit_log(self.p_pending_b).exists())

    def test_review_university_ghost_binding_denied(self):
        """无有效组织范围（绑定已软删）→ 403 DATA_SCOPE_DENIED，不退化全库。"""
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a),
            token=self.uni_dead_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "DATA_SCOPE_DENIED")
        self.assertFalse(self._audit_log(self.p_pending_a).exists())

    # ------------------------------------------------------------ P1-5 防自审
    # （2026-10-31 裁定 B 口径移植）：证据=person.create（创建人）+
    # person.self_confirm（最近一次提交人，created_at 倒序取首条，仅 SUCCESS）；
    # person.update / person.self_update 不构成证据；缺日志=无可比较操作者
    # 身份，放行（数据范围/状态机/乐观锁仍生效）。

    def _set_creator_audit(self, person, action, operator_account_id):
        """覆盖/补录一条人员创建/修改审计记录（用于自审场景构造）。"""
        AuditLog.objects.filter(
            object_type="Person", object_id=str(person.pk), action=action
        ).delete()
        AuditLog.objects.create(
            id=f"log-p-self-{person.pk}-{action}",
            log_type="OPERATION",
            operator_account_id=str(operator_account_id),
            action=action,
            object_type="Person",
            object_id=str(person.pk),
            outcome="SUCCESS",
        )

    def test_self_review_blocked_when_creator_is_reviewer(self):
        """同账号 person.create 后由该账号审核 → 403，状态/留痕零变更。"""
        self._set_creator_audit(self.p_pending_a, "person.create", self.uni_a.pk)
        before = self._audit_log(self.p_pending_a).count()
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.PENDING)
        self.assertEqual(self._audit_log(self.p_pending_a).count(), before)

    def test_self_review_allowed_when_updater_is_reviewer(self):
        """person.update 不构成自审证据（B 口径）：他人 create + 审核人本人
        update 过该档案 → 审核 200 放行。"""
        self._set_creator_audit(self.p_pending_a, "person.update", self.uni_a.pk)
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.APPROVED)

    def test_self_review_allowed_when_self_updater_is_reviewer(self):
        """person.self_update 不构成自审证据（B 口径）：审核人本人
        self_update 过该档案 → 审核 200 放行。"""
        self._set_creator_audit(self.p_pending_a, "person.self_update", self.uni_a.pk)
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.APPROVED)

    def test_self_review_blocked_when_latest_submitter_is_reviewer(self):
        """person.self_confirm 构成提交证据（B 口径）：他人 create + 审核人
        最近一次 self_confirm → 审核 403。"""
        AuditLog.objects.create(
            id=f"log-p-confirm-{self.p_pending_a.pk}",
            log_type="OPERATION",
            operator_account_id=str(self.uni_a.pk),
            action="person.self_confirm",
            object_type="Person",
            object_id=str(self.p_pending_a.pk),
            outcome="SUCCESS",
        )
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.PENDING)

    def test_self_review_allowed_without_audit_log(self):
        """无任何创建/提交审计记录 → 放行 200（B 口径：缺日志=无可比较
        操作者身份，属"无证据可判"而非绕过）。"""
        AuditLog.objects.filter(
            object_type="Person", object_id=str(self.p_pending_a.pk)
        ).delete()
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.APPROVED)

    def test_self_review_uses_latest_submitter_only(self):
        """多次提交取最近一次：早期 uni_a self_confirm + 最新 admin
        self_confirm → uni_a 审核 200 放行（早期提交不构成证据）。"""
        AuditLog.objects.create(
            id=f"log-p-confirm-early-{self.p_pending_a.pk}",
            log_type="OPERATION",
            operator_account_id=str(self.uni_a.pk),
            action="person.self_confirm",
            object_type="Person",
            object_id=str(self.p_pending_a.pk),
            outcome="SUCCESS",
        )
        # 时序保证：早期条目 created_at 提前（AuditLog.id 为随机 uuid，不可作排序键）
        AuditLog.objects.filter(
            id=f"log-p-confirm-early-{self.p_pending_a.pk}"
        ).update(created_at=timezone.now() - timedelta(minutes=5))
        AuditLog.objects.create(
            id=f"log-p-confirm-latest-{self.p_pending_a.pk}",
            log_type="OPERATION",
            operator_account_id=str(self.admin.pk),
            action="person.self_confirm",
            object_type="Person",
            object_id=str(self.p_pending_a.pk),
            outcome="SUCCESS",
        )
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a),
            token=self.uni_a_token,
        )
        self.assertEqual(resp.status_code, 200)
        self.p_pending_a.refresh_from_db()
        self.assertEqual(self.p_pending_a.audit_status, enums.AuditStatus.APPROVED)

    def test_self_review_blocked_for_multi_role_account(self):
        """同账号拥有 ADMIN+UNIVERSITY 双角色，自己创建后自己审核 → 403。"""
        multi = Account.objects.create(
            id=new_account_id(),
            username="p-review-multi",
            display_name="双角色账号",
            roles=["ADMIN", "UNIVERSITY"],
            password=make_password("multi-pass-123"),
            enabled=True,
            organization_id="ORG-RSA",
        )
        multi_token = issue_access_token(multi)[0]
        self._set_creator_audit(self.p_pending_a, "person.create", multi.pk)
        resp = self._post(
            f"{PEOPLE}/{self.p_pending_a.pk}/review",
            self._body(self.p_pending_a),
            token=multi_token,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")


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
        # A′-min 映射夹具：org.code = School.code（schoolOrgId 输出按组织节点 ID）
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
        self.assertEqual(log.reason, "本人更新档案")

        # education：E1 持久化——响应回显 + 数据库读回 + ChangeLog 留痕
        self.assertEqual(data["education"], "本科")
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.education, "本科")
        self.assertEqual(fields["education"]["old"], None)
        self.assertEqual(fields["education"]["new"], "本科")
        # coachCategory 仍声明不持久化不回显（恢复原口径）；schoolName 为派生展示字段
        self.assertNotIn("coachCategory", data)
        self.assertEqual(data["schoolName"], "甲一小")

    # -------------------------------------------- 批次 A：schoolName 明确拒绝
    #（coachCategory 已恢复原口径：声明不持久化，提交静默忽略，见 test_put_success_updates_own_profile）

    def test_put_school_name_rejected_400(self):
        """批次 A：schoolName 不再声明 → 400 INVALID_ARGUMENT 且字段可定位。"""
        from apps.persons.schemas import person_version

        resp = self._put(
            {
                "realName": "张小明",
                "schoolName": "乙一小",
                "version": person_version(self.athlete),
            }
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertEqual(body["code"], "INVALID_ARGUMENT")
        self.assertIn("schoolName", json.dumps(body.get("data") or {}))
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.school.name, "甲一小")  # 归属未被改动

    def test_put_mixed_payload_rejected_without_partial_write(self):
        """批次 A：合法变更 + schoolName 混合 → 400 且零落库零留痕。"""
        from apps.persons.schemas import person_version

        before_version = person_version(self.athlete)
        logs_before = PersonChangeLog.objects.filter(person=self.athlete).count()
        audit_before = AuditLog.objects.count()
        resp = self._put(
            {
                "realName": "张混合",
                "education": "本科",
                "schoolName": "乙一小",
                "version": before_version,
            }
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.assertIn("schoolName", json.dumps(resp.json().get("data") or {}))
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.full_name, "张小明")  # 合法字段未写入
        self.assertIsNone(self.athlete.education)
        self.assertEqual(self.athlete.school.name, "甲一小")
        self.assertEqual(person_version(self.athlete), before_version)  # 版本未前进
        self.assertEqual(
            PersonChangeLog.objects.filter(person=self.athlete).count(), logs_before
        )
        self.assertEqual(AuditLog.objects.count(), audit_before)

    def test_put_education_absent_keeps_value(self):
        """E1：本人端更新其他字段未传 education → 原值不丢失。"""
        from apps.persons.schemas import person_version

        self.athlete.education = "本科"
        self.athlete.save()
        resp = self._put(
            {"realName": "张小改2", "version": person_version(self.athlete)}
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.education, "本科")
        self.assertEqual(resp.json()["data"]["education"], "本科")

    def test_put_education_too_long_rejected_no_write(self):
        """E1：本人端超长 education → 400 校验错误，数据库不变。"""
        from apps.persons.schemas import person_version

        resp = self._put(
            {
                "realName": "张超长",
                "education": "长" * 101,
                "version": person_version(self.athlete),
            }
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.athlete.refresh_from_db()
        self.assertIsNone(self.athlete.education)  # 未被写入

    def test_put_school_org_id_rejected_not_own_scope(self):
        """E1 口径：本人端不能修改学校归属——schoolOrgId 不在本人可改集，
        StrictSchema extra=forbid 直接 400（越权归零，无需服务层分支）。"""
        from apps.persons.schemas import person_version

        school_before = self.athlete.school_id
        resp = self._put(
            {
                "realName": "张越权",
                "schoolOrgId": "org-other-1",
                "version": person_version(self.athlete),
            }
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "INVALID_ARGUMENT")
        self.athlete.refresh_from_db()
        self.assertEqual(self.athlete.school_id, school_before)  # 归属未变

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
        """问题 11：身份变更走管理员链路，本人提交 kinds=额外字段拒绝。"""
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

    def test_confirm_rejected_resubmits_to_pending(self):
        """REJECTED → PENDING（P0-2 统一规则 2026-10-28 用户批准决策 3，
        取代原"未决项 B3 保守拒绝"口径）：本人有效修改后重新提交进入待审；
        无修改直接重提被 409 拦截，场景断言详见 P0UnifiedFlowTests。"""
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
        token = issue_access_token(rejected_user)[0]
        # 前置：驳回后的有效修改（本人链，realName 实际变化 → field_changes 非空）
        from apps.persons import services

        services.update_own_person(
            str(rejected.pk),
            {"realName": "王驳回改", "version": person_version(rejected)},
        )
        resp = self._post_confirm(
            {"version": person_version(Person.objects.get(pk=rejected.pk))},
            token=token,
        )
        self.assertEqual(resp.status_code, 200)
        rejected.refresh_from_db()
        self.assertEqual(rejected.audit_status, enums.AuditStatus.PENDING)

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

    def test_my_teams_unbound_account_returns_404(self):
        resp = self.client.get(self.ME_TEAMS, **self._auth(self.unbound_token))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "NOT_FOUND")

    def test_my_teams_university_role_forbidden(self):
        """UNIVERSITY 全校分支依赖 D1 组织映射（A′ 未批），暂缓→未登记 403。"""
        resp = self.client.get(self.ME_TEAMS, **self._auth(self.uni_token))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "FORBIDDEN")

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


class DossierApiTests(TestCase):
    """§6.8 档案聚合（批次 D-1）：POST /selection-dossiers/search、
    GET /selection-dossiers/{athleteId}。

    契约依据：接口文档 L1736-1773 + §7 CandidateDossier/DossierSearch（L4692-4718）。
    - 角色 ADMIN/PROVINCE_REVIEWER（文档 L1740/L1762）；数据范围一期仅 ADMIN
      （D5 同 persons apply_person_scope 口径 → PROVINCE_REVIEWER 403）；
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

    def test_search_data_scope_denied_for_province_reviewer(self):
        """PROVINCE_REVIEWER 权限点通过、数据范围一期仅 ADMIN（D5）→ 403
        DATA_SCOPE_DENIED（复用 persons apply_person_scope 同口径）。"""
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


class P0UnifiedFlowTests(TestCase):
    """P0-2/P0-4 统一链路（2026-10-28 用户批准决策 3/4）：

    - 驳回重提（决策 A）：REJECTED → 本人 my_profile_confirm → PENDING，
      原 AUDIT 留痕 append-only 保留，新 UPDATE 记 reason="驳回后重新提交"；
    - 关键字段（realName/birthDate/school/personCategory/kinds/coachCategory/gender）
      变更使原审核结论失效：APPROVED → PENDING，与字段写入同一事务、同一份
      PersonChangeLog（UPDATE）同时含字段与 audit_status 条目；
    - 非关键字段（education/avatar/phone）不变更审核结论；
    - 并发：过期 version 在 ensure_version 入口拦截（409），不产生过期审核结论。

    coachCategory 例外在案：接受但不持久化 → 无法检测变化 → 不触发失效
    （报告项，不在本类断言范围）。
    """

    @classmethod
    def setUpTestData(cls):
        def make_account(username, roles, person=None):
            return Account.objects.create(
                id=new_account_id(),
                username=username,
                display_name=username,
                roles=roles,
                password=make_password("p0-pass-123"),
                enabled=True,
                organization_id="ORG-P0S",
                person_id=str(person.pk) if person else "",
            )

        cls.admin = make_account("p0-admin", ["ADMIN"])
        cls.uni = make_account("p0-uni", ["UNIVERSITY"])
        cls.admin_token = issue_access_token(cls.admin)[0]
        cls.uni_token = issue_access_token(cls.uni)[0]

        region = Region.objects.create(name="P0省", code="960000", level=1)
        city = Region.objects.create(name="P0市", code="960100", level=2, parent=region)
        school = School.objects.create(
            name="P0测试校", code="SCH-960100-001", school_type="PRIMARY", region=city
        )
        Organization.objects.create(
            id="ORG-P0S", code="SCH-960100-001", name="P0测试校组织",
            org_type="SCHOOL", parent_id=None,
        )
        cls.school = school

        def make_person(no, name, id_number, audit_status):
            return Person.objects.create(
                person_no=f"P2026{no:08d}",
                full_name=name,
                id_card_cipher=encrypt_text(id_number),
                id_card_hash=lookup_hash(id_number),
                id_card_masked=mask_id_number(id_number),
                person_category=enums.PersonCategory.ATHLETE,
                audit_status=audit_status,
                school=school,
            )

        # REJECTED：本人重提链路用（含历史 AUDIT 留痕，模拟已驳回审核）
        cls.p_rejected = make_person(
            1, "驳回甲", "110101201401011111", enums.AuditStatus.REJECTED
        )
        PersonChangeLog.objects.create(
            person=cls.p_rejected,
            change_type=enums.ChangeType.AUDIT,
            field_changes=[
                {"field": "audit_status", "old": "PENDING", "new": "REJECTED"}
            ],
            reason="材料不清晰",
        )
        cls.rejected_user = make_account("p0-rejected", ["ATHLETE"], cls.p_rejected)
        cls.rejected_token = issue_access_token(cls.rejected_user)[0]

        # APPROVED：管理员/本人关键字段链路用（补扩展档案行使 kinds 合成非空）
        cls.p_approved = make_person(
            2, "已过乙", "110101201401012222", enums.AuditStatus.APPROVED
        )
        AthleteProfile.objects.create(person=cls.p_approved)
        cls.p_approved_own = make_person(
            3, "已过丙", "110101201401013333", enums.AuditStatus.APPROVED
        )
        AthleteProfile.objects.create(person=cls.p_approved_own)
        cls.own_user = make_account("p0-own", ["ATHLETE"], cls.p_approved_own)
        cls.own_token = issue_access_token(cls.own_user)[0]

        # P1-5 防自审：补录创建审计记录（操作人为 admin，非审核人 uni）。
        for person in (cls.p_rejected, cls.p_approved, cls.p_approved_own):
            AuditLog.objects.create(
                id=f"log-p0-{person.pk}",
                log_type="OPERATION",
                operator_account_id=str(cls.admin.pk),
                action="person.create",
                object_type="Person",
                object_id=str(person.pk),
                outcome="SUCCESS",
            )

    @staticmethod
    def _auth(token):
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def _person_version(self, person):
        from apps.persons.schemas import person_version

        return person_version(person)

    def _update_logs(self, person):
        return PersonChangeLog.objects.filter(
            person_id=person.pk, change_type=enums.ChangeType.UPDATE
        ).order_by("created_at", "id")

    # ---------------------------------------------- 驳回 → 修改 → 重提 → 再审

    def test_rejected_resubmit_via_confirmation_keeps_history(self):
        """决策 A + 防滥用加固：REJECTED 本人有效修改后确认 → PENDING；
        旧 AUDIT 保留，UPDATE 留痕两条（本人修改 + 重提 reason="驳回后重新提交"）。
        未修改即重提被 409 拦截的场景见下方专用用例。"""
        old_audit = PersonChangeLog.objects.filter(
            person_id=self.p_rejected.pk, change_type=enums.ChangeType.AUDIT
        )
        self.assertEqual(old_audit.count(), 1)

        # 前置：驳回后的有效修改（本人链，realName 实际变化）
        from apps.persons import services
        from apps.persons.schemas import person_version

        services.update_own_person(
            str(self.p_rejected.pk),
            {"realName": "被驳甲改", "version": person_version(self.p_rejected)},
        )

        resp = self.client.post(
            ME_PROFILE_CONFIRM,
            data=json.dumps(
                {"version": person_version(Person.objects.get(pk=self.p_rejected.pk))}
            ),
            content_type="application/json",
            **self._auth(self.rejected_token),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        self.p_rejected.refresh_from_db()
        self.assertEqual(self.p_rejected.audit_status, enums.AuditStatus.PENDING)
        # 旧审核留痕不覆盖（append-only）
        self.assertEqual(old_audit.count(), 1)
        resubmits = self._update_logs(self.p_rejected)
        self.assertEqual(resubmits.count(), 2)
        first, second = list(resubmits)
        # 本人有效修改：realName 实际变化（构成重提证据）
        self.assertEqual(
            first.field_changes,
            [{"field": "realName", "old": "驳回甲", "new": "被驳甲改"}],
        )
        # 重提留痕：仅 audit_status（不构成下一次重提的修改证据）
        self.assertEqual(second.reason, "驳回后重新提交")
        self.assertEqual(
            second.field_changes,
            [{"field": "audit_status", "old": "REJECTED", "new": "PENDING"}],
        )

        # 再审核：学校端 APPROVE 通过 → APPROVED，AUDIT 留痕新增（共 2 条）
        resp = self.client.post(
            f"{PEOPLE}/{self.p_rejected.pk}/review",
            {"decision": "APPROVE", "version": self._person_version(self.p_rejected)},
            content_type="application/json",
            **self._auth(self.uni_token),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.p_rejected.refresh_from_db()
        self.assertEqual(self.p_rejected.audit_status, enums.AuditStatus.APPROVED)
        self.assertEqual(old_audit.count(), 2)
        latest = old_audit.order_by("created_at", "id").last()
        self.assertEqual(
            latest.field_changes,
            [{"field": "audit_status", "old": "PENDING", "new": "APPROVED"}],
        )

    # --------------------------------- 防滥用：驳回后未修改不得重提（2026-10-28）

    def _confirm_rejected(self, person, token):
        from apps.persons.schemas import person_version

        return self.client.post(
            ME_PROFILE_CONFIRM,
            data=json.dumps(
                {"version": person_version(Person.objects.get(pk=person.pk))}
            ),
            content_type="application/json",
            **self._auth(token),
        )

    def test_rejected_resubmit_without_change_blocked(self):
        """REJECTED 未做任何修改直接重提 → 409 STATE_CONFLICT；状态不变、
        无新增 UPDATE 留痕（不产生重提日志）。"""
        before = self._update_logs(self.p_rejected).count()
        resp = self._confirm_rejected(self.p_rejected, self.rejected_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.assertIn("未发生有效资料修改", resp.json()["message"])
        self.p_rejected.refresh_from_db()
        self.assertEqual(self.p_rejected.audit_status, enums.AuditStatus.REJECTED)
        self.assertEqual(self._update_logs(self.p_rejected).count(), before)

    def test_rejected_noop_update_not_resubmit_evidence(self):
        """无效字段修改（本人更新传相同值 → field_changes 为 None 的日志）
        不构成修改证据：重提仍 409。"""
        from apps.persons import services
        from apps.persons.schemas import person_version

        services.update_own_person(
            str(self.p_rejected.pk),
            {"realName": "驳回甲", "version": person_version(self.p_rejected)},
        )
        # 无变化也写 UPDATE 日志（field_changes=None），但不得视为有效修改
        self.assertTrue(self._update_logs(self.p_rejected).exists())
        resp = self._confirm_rejected(self.p_rejected, self.rejected_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.p_rejected.refresh_from_db()
        self.assertEqual(self.p_rejected.audit_status, enums.AuditStatus.REJECTED)

    def test_resubmit_log_alone_not_evidence_for_next_cycle(self):
        """重复重提防绕过：有效修改→重提→再驳回→此时"最近 AUDIT"为新驳回，
        此前的重提日志（audit_status-only）在驳回之前，不再构成证据 → 未修改
        再次重提被 409；同一次修改不可重复消费。"""
        from apps.persons import services
        from apps.persons.schemas import person_version

        # 第一轮：有效修改 + 重提
        services.update_own_person(
            str(self.p_rejected.pk),
            {"realName": "被驳甲改", "version": person_version(self.p_rejected)},
        )
        self.assertEqual(
            self._confirm_rejected(self.p_rejected, self.rejected_token).status_code,
            200,
        )
        self.p_rejected.refresh_from_db()
        self.assertEqual(self.p_rejected.audit_status, enums.AuditStatus.PENDING)

        # 学校端再次驳回（AUDIT 记录新增，成为"最近一次驳回"）
        resp = self.client.post(
            f"{PEOPLE}/{self.p_rejected.pk}/review",
            {"decision": "REJECT", "reason": "仍不符合要求",
             "version": person_version(self.p_rejected)},
            content_type="application/json",
            **self._auth(self.uni_token),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.p_rejected.refresh_from_db()
        self.assertEqual(self.p_rejected.audit_status, enums.AuditStatus.REJECTED)

        # 未做新一轮修改即重提 → 409（旧修改/旧重提日志均不可重复消费）
        resp = self._confirm_rejected(self.p_rejected, self.rejected_token)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "STATE_CONFLICT")
        self.p_rejected.refresh_from_db()
        self.assertEqual(self.p_rejected.audit_status, enums.AuditStatus.REJECTED)

    def test_rejected_resubmit_stale_version_blocked(self):
        """旧 version 重提 → 409 VERSION_CONFLICT（乐观锁先于重提校验生效）。"""
        resp = self.client.post(
            ME_PROFILE_CONFIRM,
            data=json.dumps({"version": 1}),
            content_type="application/json",
            **self._auth(self.rejected_token),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        self.p_rejected.refresh_from_db()
        self.assertEqual(self.p_rejected.audit_status, enums.AuditStatus.REJECTED)

    # -------------------------------------------- 关键字段变更 → 结论失效

    def test_admin_update_key_field_invalidates_approval(self):
        """管理员 PUT /people/{id} 改 realName：APPROVED → PENDING，同一份
        UPDATE ChangeLog 同时含 realName 与 audit_status 两条变更。"""
        resp = self.client.put(
            f"{PEOPLE}/{self.p_approved.pk}",
            data=json.dumps(
                {
                    "kinds": ["ATHLETE"],
                    "realName": "已过乙改",
                    "version": self._person_version(self.p_approved),
                }
            ),
            content_type="application/json",
            **self._auth(self.admin_token),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        self.p_approved.refresh_from_db()
        self.assertEqual(self.p_approved.audit_status, enums.AuditStatus.PENDING)
        logs = self._update_logs(self.p_approved)
        self.assertEqual(logs.count(), 1)
        fields = {c["field"]: c for c in logs.first().field_changes}
        self.assertIn("realName", fields)
        self.assertEqual(fields["realName"]["old"], "已过乙")
        self.assertEqual(fields["realName"]["new"], "已过乙改")
        self.assertEqual(fields["audit_status"]["old"], "APPROVED")
        self.assertEqual(fields["audit_status"]["new"], "PENDING")

    def test_admin_update_non_key_field_keeps_approval(self):
        """改 education（非关键字段）：仍 APPROVED，ChangeLog 仅 education 条目。"""
        resp = self.client.put(
            f"{PEOPLE}/{self.p_approved.pk}",
            data=json.dumps(
                {
                    "kinds": ["ATHLETE"],
                    "realName": "已过乙",  # 与原值相同 → 无 realName 变更
                    "education": "本科",
                    "version": self._person_version(self.p_approved),
                }
            ),
            content_type="application/json",
            **self._auth(self.admin_token),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        self.p_approved.refresh_from_db()
        self.assertEqual(self.p_approved.audit_status, enums.AuditStatus.APPROVED)
        logs = self._update_logs(self.p_approved)
        self.assertEqual(logs.count(), 1)
        fields = [c["field"] for c in logs.first().field_changes]
        self.assertEqual(fields, ["education"])
        self.assertNotIn("audit_status", fields)

    def test_own_update_key_field_invalidates_approval(self):
        """本人 PUT /me/profile 改 realName：与管理员链路同规则失效。"""
        resp = self.client.put(
            ME_PROFILE,
            data=json.dumps(
                {
                    "realName": "已过丙改",
                    "version": self._person_version(self.p_approved_own),
                }
            ),
            content_type="application/json",
            **self._auth(self.own_token),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        self.p_approved_own.refresh_from_db()
        self.assertEqual(self.p_approved_own.audit_status, enums.AuditStatus.PENDING)
        logs = self._update_logs(self.p_approved_own)
        self.assertEqual(logs.count(), 1)
        entry = logs.first()
        self.assertEqual(entry.reason, "本人更新档案")
        fields = {c["field"] for c in entry.field_changes}
        self.assertEqual(fields, {"realName", "audit_status"})

    def test_own_update_non_key_field_keeps_approval(self):
        """本人改 education（非关键）：仍 APPROVED，无 audit_status 条目。"""
        resp = self.client.put(
            ME_PROFILE,
            data=json.dumps(
                {
                    "realName": "已过丙",
                    "education": "硕士",
                    "version": self._person_version(self.p_approved_own),
                }
            ),
            content_type="application/json",
            **self._auth(self.own_token),
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        self.p_approved_own.refresh_from_db()
        self.assertEqual(self.p_approved_own.audit_status, enums.AuditStatus.APPROVED)
        fields = [c["field"] for c in self._update_logs(self.p_approved_own).first().field_changes]
        self.assertEqual(fields, ["education"])

    def test_stale_version_conflict_keeps_approval(self):
        """并发：过期 version → 409，审核结论与留痕零变更（无过期结论落库）。"""
        stale = self._person_version(self.p_approved) - 1
        resp = self.client.put(
            f"{PEOPLE}/{self.p_approved.pk}",
            data=json.dumps({"kinds": ["ATHLETE"], "realName": "并发写", "version": stale}),
            content_type="application/json",
            **self._auth(self.admin_token),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "VERSION_CONFLICT")
        self.p_approved.refresh_from_db()
        self.assertEqual(self.p_approved.audit_status, enums.AuditStatus.APPROVED)
        self.assertEqual(self.p_approved.full_name, "已过乙")
        self.assertFalse(self._update_logs(self.p_approved).exists())
