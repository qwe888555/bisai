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
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase

from apps.infra.management.commands.init_base_data import DICT_TYPES
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


class VerifyTests(TestCase):
    """--verify 只读验收：退出码、分级定位、--phase 组合、只读零写入。

    运行方式与 InitBaseDataTests 相同（内存 SQLite，不触碰 MySQL）。
    """

    MODELS = {"region": Region, "school": School, "dict_type": DictType,
              "dict_item": DictItem, "material": MaterialType}

    def call_verify(self, *args, **kwargs) -> tuple[str, int]:
        """执行 --verify，返回 (输出, 退出码)。sys.exit(1) 转为退出码 1。"""
        out = StringIO()
        try:
            call_command("init_base_data", *args, **kwargs, stdout=out)
            code = 0
        except SystemExit as exc:  # noqa: B904 测试内捕获为主命令退出码
            code = exc.code or 1
        return out.getvalue(), code

    def snapshot(self) -> dict:
        """全表内容快照（逐字段值），用于断言 verify 零写入。"""
        return {name: list(model.objects.order_by("pk").values()) for name, model in self.MODELS.items()}

    @staticmethod
    def make_region(code: str, level: int, parent=None, is_active: bool = True) -> Region:
        return Region.objects.create(code=code, name=f"区划{code}", level=level,
                                     parent=parent, sort_order=0, is_active=is_active)

    @staticmethod
    def make_school(name: str, region: Region, stype: str = "PRIMARY", code: str | None = None) -> School:
        return School.objects.create(name=name, region=region, school_type=stype, code=code)

    # ------------------------------------------------------------ 正常与空库

    def test_verify_empty_db_info_exit_zero(self):
        out, code = self.call_verify(verify=True)
        self.assertEqual(out.count("[INFO ]"), 5)  # 5 张基础表全部空表提示
        self.assertNotIn("[ERROR", out)
        self.assertNotIn("[WARN", out)
        self.assertEqual(code, 0)

    def test_verify_healthy_data_exit_zero(self):
        prov = self.make_region("540000", 1)
        city = self.make_region("540100", 2, parent=prov)
        self.make_region("540121", 3, parent=city)
        self.make_school("林周县中学", Region.objects.get(code="540121"))
        for item in DICT_TYPES:  # 5 骨架齐全
            DictType.objects.create(code=item["code"], name=item["name"])
        DictItem.objects.create(type=DictType.objects.get(code="position"), value="GK", label="守门员")
        MaterialType.objects.create(code="photo", name="个人照片", applies_to="PERSON")
        out, code = self.call_verify(verify=True)  # 5 表均非空且合法 -> 无任何发现项
        self.assertNotIn("[ERROR", out)
        self.assertNotIn("[WARN", out)
        self.assertIn("(全部检查通过, 无发现项)", out)
        self.assertEqual(code, 0)

    # ------------------------------------------------------------ 硬错误定位

    def test_verify_orphan_city_error_exit_one(self):
        self.make_region("540100", 2)  # 市级无父级
        out, code = self.call_verify(verify=True)
        self.assertIn("[ERROR] base_region: code=540100 level=2 非省级缺少父级", out)
        self.assertEqual(code, 1)

    def test_verify_province_with_parent_error(self):
        prov = self.make_region("540000", 1)
        other = self.make_region("510000", 1)
        Region.objects.filter(pk=prov.pk).update(parent=other)  # 绕过校验直改：省级挂父
        out, code = self.call_verify(verify=True)
        self.assertIn("[ERROR] base_region: code=540000 level=1 省级不得有父级(现父=510000)", out)
        self.assertEqual(code, 1)

    def test_verify_parent_level_mismatch_error(self):
        prov = self.make_region("540000", 1)
        county = self.make_region("540121", 3)
        Region.objects.filter(pk=county.pk).update(parent=prov)  # 县挂省：跳级
        out, code = self.call_verify(verify=True)
        self.assertIn("level=3 父级 level=1 不满足 level-1", out)
        self.assertEqual(code, 1)

    def test_verify_school_bad_type_error(self):
        prov = self.make_region("540000", 1)
        School.objects.create(name="坏学校", region=prov, school_type="VOC", code=None)
        out, code = self.call_verify(verify=True)
        self.assertIn("[ERROR] base_school: pk=", out)
        self.assertIn("school_type='VOC' 非法", out)
        self.assertEqual(code, 1)

    def test_verify_material_bad_applies_error(self):
        MaterialType.objects.create(code="bad", name="坏类型", applies_to="BOTH")
        out, code = self.call_verify(verify=True)
        self.assertIn("[ERROR] material_type: code=bad applies_to='BOTH' 非法", out)
        self.assertEqual(code, 1)

    def test_verify_region_bad_level_error(self):
        self.make_region("999999", 3)
        Region.objects.filter(code="999999").update(level=0)  # 绕过 choices 直改
        out, code = self.call_verify(verify=True)
        self.assertIn("[ERROR] base_region: code=999999 level=0 非法(允许: 1/2/3)", out)
        self.assertEqual(code, 1)

    # ------------------------------------------------------------ 引用完整性（仅绕过 ORM 直改可达）
    # 机制说明：Django 5.1+ 的 SQLite 强制 FK，悬空行无法在约束生效时合法存在；
    # 借助 constraint_checks_disabled 在测试连接内临时切换 pragma，verify 于悬空窗口内执行，
    # 退出前把引用改回有效值——约束与数据在恢复时均完整，不改任何 DDL/真实库约束。

    def test_verify_region_dangling_parent_ref_error(self):
        prov = self.make_region("540000", 1)
        city = self.make_region("540100", 2, parent=prov)
        with connection.constraint_checks_disabled():  # Django 内部 try/finally 保证 pragma 必然恢复
            Region.objects.filter(pk=city.pk).update(parent_id=99999)
            try:
                out, code = self.call_verify(verify=True)  # 悬空窗口内执行验收
            finally:
                Region.objects.filter(pk=city.pk).update(parent_id=prov.pk)  # 异常时也恢复，避免污染
        self.assertIn("[ERROR] base_region: code=540100 引用的父区划 id=99999 不存在", out)
        self.assertEqual(code, 1)
        self.assertEqual(Region.objects.get(pk=city.pk).parent_id, prov.pk)  # 数据已还原

    def test_verify_school_dangling_region_ref_error(self):
        region = self.make_region("540000", 1)
        school = self.make_school("悬空学校", region, code="SCH9")
        with connection.constraint_checks_disabled():  # Django 内部 try/finally 保证 pragma 必然恢复
            School.objects.filter(pk=school.pk).update(region_id=99999)
            try:
                out, code = self.call_verify(verify=True)  # 悬空窗口内执行验收
            finally:
                School.objects.filter(pk=school.pk).update(region_id=region.pk)  # 异常时也恢复，避免污染
        self.assertIn("[ERROR] base_school: code=SCH9 引用的区划 id=99999 不存在", out)
        self.assertEqual(code, 1)
        self.assertEqual(School.objects.get(pk=school.pk).region_id, region.pk)  # 数据已还原

    def test_verify_dict_item_dangling_type_ref_error(self):
        dtype = DictType.objects.create(code="position", name="场上位置")
        item = DictItem.objects.create(type=dtype, value="GK", label="守门员")
        with connection.constraint_checks_disabled():
            DictItem.objects.filter(pk=item.pk).update(type_id=99999)
            try:
                out, code = self.call_verify(verify=True)
            finally:
                DictItem.objects.filter(pk=item.pk).update(type_id=dtype.pk)  # 异常时也恢复
        self.assertIn(f"[ERROR] base_dict_item: pk={item.pk} value=GK 引用的字典类型 id=99999 不存在", out)
        self.assertEqual(code, 1)
        self.assertEqual(DictItem.objects.get(pk=item.pk).type_id, dtype.pk)  # 数据已还原

    # ------------------------------------------------------------ 警告与 strict

    def test_verify_school_empty_code_warn_not_error(self):
        prov = self.make_region("540000", 1)
        School.objects.create(name="无码学校", region=prov, school_type="PRIMARY", code="")
        out, code = self.call_verify(verify=True)
        self.assertIn("[WARN ] base_school: pk=", out)
        self.assertIn("空串占唯一键, 应为 NULL", out)
        self.assertNotIn("[ERROR", out)
        self.assertEqual(code, 0)  # 非 strict 时 WARN 不影响退出码

    def test_verify_missing_skeleton_warn_only(self):
        DictType.objects.create(code="team_category", name="球队组别")  # 只建 1/5
        out, code = self.call_verify(verify=True)
        self.assertEqual(out.count("骨架缺失"), 4)
        self.assertEqual(code, 0)

    def test_verify_dict_item_unknown_type_warn(self):
        dtype = DictType.objects.create(code="custom_ext", name="扩展类型")  # 骨架外类型
        DictItem.objects.create(type=dtype, value="X1", label="扩展项")
        out, code = self.call_verify(verify=True)
        self.assertIn("[WARN ] base_dict_item: type=custom_ext value=X1 挂在骨架外类型", out)
        self.assertEqual(code, 0)

    def test_verify_inactive_parent_warn(self):
        prov = self.make_region("540000", 1, is_active=False)
        self.make_region("540100", 2, parent=prov)  # 父停用：合法但提示
        out, code = self.call_verify(verify=True)
        self.assertIn("父级 540000 已停用", out)
        self.assertNotIn("[ERROR", out)
        self.assertEqual(code, 0)

    def test_verify_strict_warns_exit_nonzero(self):
        DictType.objects.create(code="team_category", name="球队组别")  # 4 个骨架缺失 -> WARN
        out, code = self.call_verify(verify=True, strict=True)
        self.assertEqual(out.count("骨架缺失"), 4)
        self.assertEqual(code, 1)  # strict 下 WARN 也非零

    def test_verify_region_partial_import_no_gap_warn(self):
        prov = self.make_region("540000", 1)
        self.make_region("540100", 2, parent=prov)  # 合法部分导入：只到市级不算缺口
        out, code = self.call_verify(verify=True)
        self.assertNotIn("层级覆盖缺口", out)
        self.assertEqual(code, 0)

    def test_verify_region_coverage_gap_warn_on_bypass(self):
        prov = self.make_region("540000", 1)
        city = self.make_region("540100", 2, parent=prov)
        self.make_region("540121", 3, parent=city)
        Region.objects.filter(pk=city.pk).update(level=3)  # 绕过校验直改：父链断裂
        out, code = self.call_verify(verify=True)
        self.assertIn("层级覆盖缺口", out)  # 缺口 WARN（正常导入流程不可触发，仅直改数据出现）
        self.assertEqual(code, 1)  # 同时命中 level-1 硬错

    # ------------------------------------------------------------ 范围与安全

    def test_verify_phase_limits_scope(self):
        prov = self.make_region("540000", 1)
        School.objects.create(name="坏学校", region=prov, school_type="BAD", code=None)  # 学校硬错
        self.make_region("540100", 2)  # 区划硬错
        out, code = self.call_verify(verify=True, phase="regions")  # 只查区划
        self.assertIn("code=540100 level=2 非省级缺少父级", out)
        self.assertNotIn("school_type='BAD'", out)  # 学校检查不在范围内
        self.assertEqual(code, 1)

    def test_verify_writes_nothing(self):
        prov = self.make_region("540000", 1)
        self.make_region("540100", 2, parent=prov)
        self.make_school("测试学校", prov)
        MaterialType.objects.create(code="bad", name="坏类型", applies_to="BOTH")
        before = self.snapshot()
        self.call_verify(verify=True)
        self.call_verify(verify=True, strict=True)
        self.assertEqual(self.snapshot(), before)  # 逐字段内容一致（非仅行数）

    def test_verify_mutually_exclusive_with_execute(self):
        with self.assertRaises(CommandError):
            call_command("init_base_data", verify=True, execute=True, stdout=StringIO())
