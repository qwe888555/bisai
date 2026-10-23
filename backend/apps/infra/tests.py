"""init_base_data 命令自动化测试（SQLite 内存库，不触碰 MySQL）。

运行方式：
    DATABASE_URL=sqlite:///:memory: python manage.py test apps.infra

覆盖：预览绝不写入 / --execute 确实写入 / 幂等重跑 / 缺数据源明确跳过 /
行级校验失败报告 / 父级先于子级 / 行级保存点隔离（数据库级错误不污染整批）。
"""

from __future__ import annotations

import csv
import os
from io import StringIO
from tempfile import NamedTemporaryFile

from django.core.management import call_command
from django.test import TestCase

from apps.materials.models import MaterialType
from apps.persons.models import DictItem, DictType, Region, School


def make_csv(rows: list[list[str]], header: list[str]) -> str:
    """用 csv.writer 规范写入（值内含逗号会被正确引号包裹）。"""
    f = NamedTemporaryFile("w+", suffix=".csv", delete=False, encoding="utf-8", newline="")
    writer = csv.writer(f)
    writer.writerow(header)
    writer.writerows(rows)
    f.close()
    return f.name


class InitBaseDataTests(TestCase):
    def call_cmd(self, *args, **kwargs) -> str:
        out = StringIO()
        call_command("init_base_data", *args, stdout=out, **kwargs)
        return out.getvalue()

    # ------------------------------------------------------------ 预览与执行

    def test_preview_never_writes(self):
        out = self.call_cmd()
        self.assertIn("只读预览", out)
        self.assertEqual(DictType.objects.count(), 0)
        self.assertEqual(Region.objects.count(), 0)

    def test_execute_dict_types_idempotent(self):
        self.call_cmd(phase="dict_types", execute=True)
        self.assertEqual(DictType.objects.count(), 5)
        self.assertTrue(DictType.objects.filter(code="team_category", name="球队组别").exists())
        out = self.call_cmd(phase="dict_types", execute=True)  # 重跑：全部跳过
        self.assertIn("新增=0", out)
        self.assertIn("跳过=5", out)
        self.assertEqual(DictType.objects.count(), 5)

    def test_missing_csv_skips_explicitly(self):
        for phase in ("regions", "schools", "dict_items", "material_types"):
            out = self.call_cmd(phase=phase, execute=True)
            self.assertIn("[跳过] 缺少", out, msg=phase)
        self.assertEqual(Region.objects.count() + School.objects.count()
                         + DictItem.objects.count() + MaterialType.objects.count(), 0)

    # ------------------------------------------------------------ 区划

    def test_regions_validation_failure_report_and_idempotent(self):
        csv = make_csv(
            [
                ["110000", "北京市", "1", "", "0", "1"],
                ["110101", "东城区", "2", "110000", "", "1"],
                ["999999", "孤儿区", "2", "000000", "", "1"],   # 父级不存在 -> 失败
                ["110101", "重复码", "2", "110000", "", "1"],   # code 已存在 -> 跳过
                ["130000", "坏级别", "9", "", "", "1"],          # level 非法 -> 失败
            ],
            ["code", "name", "level", "parent_code", "sort_order", "is_active"],
        )
        out = self.call_cmd(phase="regions", regions_csv=csv, execute=True)
        self.assertIn("新增=2", out)
        self.assertIn("跳过=1", out)
        self.assertIn("失败=2", out)
        self.assertEqual(Region.objects.count(), 2)
        out2 = self.call_cmd(phase="regions", regions_csv=csv, execute=True)  # 重跑幂等
        self.assertIn("新增=0", out2)
        self.assertIn("跳过=3", out2)
        self.assertIn("失败=2", out2)
        self.assertEqual(Region.objects.count(), 2)

    def test_regions_child_before_parent(self):
        csv = make_csv(
            [
                ["110101", "东城区", "2", "110000", "", "1"],  # 子行在前
                ["110000", "北京市", "1", "", "0", "1"],
            ],
            ["code", "name", "level", "parent_code", "sort_order", "is_active"],
        )
        out = self.call_cmd(phase="regions", regions_csv=csv, execute=True)
        self.assertIn("新增=2", out)
        self.assertIn("失败=0", out)
        self.assertEqual(Region.objects.get(code="110101").parent.code, "110000")

    def test_regions_preview_with_csv_writes_nothing(self):
        csv = make_csv([["110000", "北京市", "1", "", "0", "1"]],
                       ["code", "name", "level", "parent_code", "sort_order", "is_active"])
        out = self.call_cmd(phase="regions", regions_csv=csv)  # 预览 + 有 CSV
        self.assertIn("新增=1", out)  # 仅报告"将新增"
        self.assertEqual(Region.objects.count(), 0)  # 但未写入

    # ------------------------------------------------------------ 学校

    def test_schools_validation_and_idempotent(self):
        Region.objects.create(code="110000", name="北京市", level=1)
        csv = make_csv(
            [
                ["测试一中", "PRIMARY", "110000", "SCH001", "", "1"],
                ["测试一中", "JUNIOR", "110000", "", "", "1"],   # (name,region) 已存在 -> 跳过
                ["测试二中", "BAD_TYPE", "110000", "", "", "1"],  # school_type 非法 -> 失败
                ["测试三中", "PRIMARY", "999999", "", "", "1"],   # 区划不存在 -> 失败
            ],
            ["name", "school_type", "region_code", "code", "address", "is_active"],
        )
        out = self.call_cmd(phase="schools", schools_csv=csv, execute=True)
        self.assertIn("新增=1", out)
        self.assertIn("跳过=1", out)
        self.assertIn("失败=2", out)
        self.assertEqual(School.objects.count(), 1)
        self.assertEqual(School.objects.first().code, "SCH001")

    def test_schools_row_savepoint_isolation(self):
        """数据库级唯一冲突只影响本行，后续有效行照常入库。"""
        Region.objects.create(code="110000", name="北京市", level=1)
        School.objects.create(code="SCH001", name="占位学校", school_type="PRIMARY",
                              region=Region.objects.get(code="110000"))
        csv = make_csv(
            [
                ["新学校A", "PRIMARY", "110000", "SCH001", "", "1"],  # code 唯一冲突 -> DB 级失败
                ["新学校B", "PRIMARY", "110000", "SCH002", "", "1"],  # 有效行应照常写入
            ],
            ["name", "school_type", "region_code", "code", "address", "is_active"],
        )
        out = self.call_cmd(phase="schools", schools_csv=csv, execute=True)
        self.assertIn("新增=1", out)   # 只有新学校B
        self.assertIn("失败=1", out)   # 新学校A
        self.assertTrue(School.objects.filter(name="新学校B").exists())
        self.assertFalse(School.objects.filter(name="新学校A").exists())

    # ------------------------------------------------------------ 字典项

    def test_dict_items_validation(self):
        DictType.objects.create(code="position", name="场上位置")
        csv = make_csv(
            [
                ["position", "GK", "守门员", "1", "1", ""],
                ["position", "GK", "守门员", "1", "1", ""],       # (type,value) 已存在 -> 跳过
                ["nope", "X", "未知类型", "", "1", ""],            # type_code 不存在 -> 失败
                ["position", "DF", "后卫", "2", "1", "{bad"],      # ext_json 非法 -> 失败
                ["position", "MF", "中场", "3", "1", '{"group":"U12"}'],  # 合法 JSON
            ],
            ["type_code", "value", "label", "sort_order", "is_active", "ext_json"],
        )
        out = self.call_cmd(phase="dict_items", dict_items_csv=csv, execute=True)
        self.assertIn("新增=2", out)
        self.assertIn("跳过=1", out)
        self.assertIn("失败=2", out)
        item = DictItem.objects.get(value="MF")
        self.assertEqual(item.ext_json, {"group": "U12"})  # 存对象而非字符串

    # ------------------------------------------------------------ 材料类型

    def test_material_types_validation_and_idempotent(self):
        csv = make_csv(
            [
                ["birth_cert", "出生证明", "PERSON", "1", "1", "0", "jpg,pdf", "5", "", "1", ""],
                ["bad_row", "坏行", "BOTH", "", "", "", "", "", "", "", ""],  # applies_to 非法 -> 失败
            ],
            ["code", "name", "applies_to", "is_required", "need_audit", "allow_multiple",
             "allowed_ext", "max_size_mb", "sort_order", "is_active", "remark"],
        )
        out = self.call_cmd(phase="material_types", material_csv=csv, execute=True)
        self.assertIn("新增=1", out)
        self.assertIn("失败=1", out)
        mt = MaterialType.objects.get(code="birth_cert")
        self.assertEqual(mt.max_size_mb, 5)
        self.assertEqual(mt.allowed_ext, "jpg,pdf")
        out2 = self.call_cmd(phase="material_types", material_csv=csv, execute=True)
        self.assertIn("新增=0", out2)
        self.assertIn("跳过=1", out2)
        self.assertEqual(MaterialType.objects.count(), 1)
