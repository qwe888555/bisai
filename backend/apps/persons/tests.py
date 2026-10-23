"""基础数据管理接口自动化测试（SQLite 内存库，不触碰 MySQL）。

运行方式：
    $env:DATABASE_URL="sqlite:///:memory:"; python manage.py test apps.persons

覆盖：JWT 鉴权 401 / 角色 403 / 区划严格层级校验与唯一约束 / 学校双唯一约束
与 school_type 值域 / 字典类型引用删除保护 / 字典项 (type,value) 唯一与 value
禁改 / 停用规则 / 列表过滤与分页 / 统一信封 / 审计留痕。
"""

from __future__ import annotations

import json

from django.test import TestCase

from apps.accounts.models import Account, AuditLog
from apps.common.authentication import issue_access_token, make_password
from apps.common.ids import new_account_id
from apps.persons.models import DictItem, DictType, Region, School

BASE = "/api/v1/base-data"


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
