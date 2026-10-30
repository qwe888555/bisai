"""基础数据初始化命令（成员4，幂等可重跑，默认只读预览）。

用法：
    python manage.py init_base_data
        # 预览模式（默认，只读）：显示目标库、当前行数、各阶段就绪状态与缺失项
    python manage.py init_base_data --phase dict_types --execute
        # 第一阶段：写入字典类型骨架（code 由冻结 DDL/总体设计确认）
    python manage.py init_base_data --phase regions --regions-csv 路径.csv --execute
        # 第二阶段：导入区划（列: code,name,level,parent_code,sort_order,is_active）
    python manage.py init_base_data --phase schools --schools-csv 路径.csv --execute
        # 第二阶段：导入学校（列: name,school_type,region_code,code,address,is_active）
    python manage.py init_base_data --phase dict_items --dict-items-csv 路径.csv --execute
        # 第三阶段：导入字典项（列: type_code,value,label,sort_order,is_active,ext_json）
    python manage.py init_base_data --phase material_types --material-csv 路径.csv --execute
        # 导入材料类型（列: code,name,applies_to,is_required,need_audit,allow_multiple,
        #                 allowed_ext,max_size_mb,sort_order,is_active,remark）
    python manage.py init_base_data [--phase <名>] --verify [--strict]
        # 只读验收：按冻结约束+已确认规则体检基础数据，绝不写入；
        # --phase 限定检查范围；退出码 0=通过 1=存在ERROR(--strict 时含WARN)

约定：
1. 不带 --execute 一律只读预览，任何阶段（含提供了 CSV 时）都不写数据；
1a. --verify 为只读验收：与 --execute 互斥；全程仅查询，不做任何修复；
2. 幂等：按业务唯一键 get_or_create，已存在的一律跳过，不覆盖人工修改；
3. 不编造数据：regions/schools/dict_items/material_types 无 CSV 或无确认清单时明确跳过并记录；
4. 外键依赖：区划按 level 升序处理（父级先于子级）；学校依赖已导入的区划；字典项依赖字典类型；
5. 行级隔离：每行独立保存点，单行失败（含数据库级错误）只回滚该行并记入失败报告，不污染整批事务。
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.materials.models import MaterialType
from apps.persons.models import DictItem, DictType, Region, School

# 材料类型默认清单（P1-10 定案，2026-10-30 方案A）：仓库根 docs/csv/material_types.csv，
# 13 行正式清单已于 2026-10-26 批文签发（2026-10-29 问题⑥确认维持 13 类）。
# --material-csv 显式参数优先；未传时回退到该默认路径；默认文件缺失则维持跳过不编造。
DEFAULT_MATERIAL_CSV = Path(settings.BASE_DIR).parent / "docs" / "csv" / "material_types.csv"

# 字典项 value 稳定编码格式：字母/数字开头，仅含字母数字下划线连字符，≤50（列宽上限）。
# 现有全部既有值域（GK/DF/MF/FW、HEAD/ASSISTANT/...、PRIMARY/MIDDLE_VOC 等）均满足。
_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")

# 字典类型骨架：code 为冻结 DDL（V1.4-r1）与《成员4后端总体设计 V1.0》确认的 5 类引用，
# 加 coach_category（2026-10-24 用户规划候选：HEAD/ASSISTANT/GOALKEEPER/FITNESS/MANAGER/OTHER，
# 字典项清单待最终确认后走 --dict-items-csv 导入，模板见 docs/初始化数据模板与缺失数据清单.md；
# 注意：TeamCoach.duty 冻结枚举仅 3 值（CoachDuty），coach_category 的消费端承载方案待批准，
# 本骨架仅建字典类型行（数据非 DDL），不改变任何冻结结构）。
# name 为建议名称，预览中醒目标注，可在执行前人工确认/修改（DDL 对 name 无硬性规定）。
DICT_TYPES: tuple[dict, ...] = (
    {"code": "team_category", "name": "球队组别"},
    {"code": "position", "name": "场上位置"},
    {"code": "coach_level", "name": "教练等级"},
    {"code": "coach_category", "name": "教练岗位"},
    {"code": "guardian_relation", "name": "监护人关系"},
    {"code": "school_type", "name": "学校类型"},
)

# 学校类型编码：DDL base_school.school_type 列注释枚举（编码已确认，字典项 label 待成员5确认）。
SCHOOL_TYPE_CODES = ("PRIMARY", "JUNIOR", "SENIOR", "NINE_YEAR", "COMPLETE", "MIDDLE_VOC")

# 5 骨架字典类型 code 集合（验收用：字典项挂到骨架外类型仅提示人工确认，不判硬错）。
SKELETON_CODES = frozenset(item["code"] for item in DICT_TYPES)

TRUE_SET = {"1", "true", "yes", "on", "是", "y"}


def _to_bool(raw: str | None, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in TRUE_SET


def _read_csv(path: str) -> list[dict]:
    """读取 CSV（兼容 Excel 的 utf-8-sig BOM；多余列丢弃，缺失值按空串）。"""
    file = Path(path)
    if not file.exists():
        raise CommandError(f"CSV 文件不存在: {file}")
    with file.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    def clean(v) -> str:
        return v.strip() if isinstance(v, str) else ""

    return [{str(k).strip(): clean(v) for k, v in row.items() if k is not None} for row in rows]


class Command(BaseCommand):
    help = "基础数据初始化（幂等；默认只读预览，--execute 才写入）"

    def add_arguments(self, parser):
        parser.add_argument("--phase", default="all",
                            choices=["all", "dict_types", "regions", "schools", "dict_items", "material_types"])
        parser.add_argument("--execute", action="store_true", help="实际写入（缺省只预览）")
        parser.add_argument("--regions-csv")
        parser.add_argument("--schools-csv")
        parser.add_argument("--dict-items-csv")
        parser.add_argument("--material-csv")
        parser.add_argument("--verify", action="store_true",
                            help="只读验收：检查基础数据一致性，不写入不修复")
        parser.add_argument("--strict", action="store_true",
                            help="验收严格模式：存在 WARN 也返回非零退出码")

    # ---------------------------------------------------------------- 预览

    def _print_target(self) -> None:
        if connection.vendor == "mysql":
            with connection.cursor() as c:
                c.execute("SELECT DATABASE(), VERSION()")
                db, ver = c.fetchone()
        else:  # sqlite 等后端：DATABASE()/VERSION() 为 MySQL 专有
            db = connection.settings_dict["NAME"]
            with connection.cursor() as c:
                c.execute("SELECT sqlite_version()")
                ver = f"SQLite {c.fetchone()[0]}"
        mode = "【写入】" if self.execute else ("只读验收" if self.verify else "只读预览")
        self.stdout.write(f"目标库: {db} ({ver})  模式: {mode}")

    def _print_counts(self) -> None:
        stats = []
        for name, model in [("base_region", Region), ("base_school", School),
                            ("base_dict_type", DictType), ("base_dict_item", DictItem),
                            ("material_type", MaterialType)]:
            stats.append(f"{name}={model.objects.count()}")
        self.stdout.write("当前行数: " + " | ".join(stats))

    def _process_rows(self, rows: list[dict], row_fn) -> dict:
        """逐行处理骨架：预览模式只探测不写入；执行模式每行独立保存点，失败只回滚该行。"""
        report = {"created": 0, "skipped": 0, "failed": []}
        for idx, row in enumerate(rows, 2):
            try:
                status = row_fn(row)
                report[status] += 1
            except Exception as exc:  # noqa: BLE001 单行失败不阻断整批
                report["failed"].append({"row": idx, "key": row.get("code") or row.get("name") or row.get("value"),
                                         "reason": str(exc)})
        return report

    # ---------------------------------------------------------------- 各阶段

    def _run_dict_types(self) -> dict:
        self.stdout.write("\n== 阶段1: base_dict_type 字典类型骨架 ==")
        self.stdout.write(
            f"  唯一键: code (uk_dict_type_code) | 来源: 冻结DDL+总体设计V1.0 确认的5类引用"
            f"+coach_category(2026-10-24 规划候选) 共{len(DICT_TYPES)}类"
        )
        report = {"created": 0, "skipped": 0, "failed": []}
        for item in DICT_TYPES:
            exists = DictType.objects.filter(code=item["code"]).exists()
            mark = "跳过(已存在)" if exists else "新建(名称为建议值,可先改)"
            self.stdout.write(f"  - {item['code']:<20} name={item['name']:<10} -> {mark}")
            if self.execute and not exists:
                DictType.objects.create(code=item["code"], name=item["name"])
                report["created"] += 1
            elif exists:
                report["skipped"] += 1
        return report

    def _run_regions(self) -> dict:
        self.stdout.write("\n== 阶段2a: base_region 区划导入 ==")
        if not self.csv_paths.get("regions"):
            self.stdout.write("  [跳过] 缺少 --regions-csv（正式区划数据未提供，不编造）")
            return {"created": 0, "skipped": 0, "failed": []}
        rows = _read_csv(self.csv_paths["regions"])
        self.stdout.write(f"  唯一键: code | 行数: {len(rows)} | 依赖: 按 level 升序处理(父级先于子级,省级 parent 留空)")
        rows = sorted(rows, key=lambda r: int(r.get("level") or 0))  # 父级先于子级
        cache: dict[str, Region] = {}

        def row_fn(row: dict) -> str:
            code, name = row.get("code", ""), row.get("name", "")
            level_raw = row.get("level", "")
            if not code or not name:
                raise ValueError("code/name 必填")
            if level_raw not in {"1", "2", "3"}:
                raise ValueError(f"level 非法: {level_raw!r}(须为1/2/3)")
            parent_code = row.get("parent_code", "")
            if int(level_raw) == 1 and parent_code:
                raise ValueError("省级(level=1)不得有 parent_code")
            if int(level_raw) > 1 and not parent_code:
                raise ValueError("非省级必须提供 parent_code")
            parent = cache.get(parent_code)
            if parent is None and parent_code:
                parent = Region.objects.filter(code=parent_code).first()
            if int(level_raw) > 1 and parent is None:
                raise ValueError(f"parent_code {parent_code!r} 不存在(父级须先导入)")
            if parent is not None:
                cache[parent_code] = parent
            defaults = {"name": name, "level": int(level_raw), "parent": parent,
                        "sort_order": int(row.get("sort_order") or 0),
                        "is_active": _to_bool(row.get("is_active"), True)}
            if not self.execute:
                exists = Region.objects.filter(code=code).exists()
                return "skipped" if exists else "created"
            with transaction.atomic():  # 行级保存点：失败只回滚本行
                _, created = Region.objects.get_or_create(code=code, defaults=defaults)
            return "created" if created else "skipped"

        return self._process_rows(rows, row_fn)

    def _run_schools(self) -> dict:
        self.stdout.write("\n== 阶段2b: base_school 学校导入 ==")
        if not self.csv_paths.get("schools"):
            self.stdout.write("  [跳过] 缺少 --schools-csv（正式学校名单未提供，不编造）")
            return {"created": 0, "skipped": 0, "failed": []}
        rows = _read_csv(self.csv_paths["schools"])
        self.stdout.write(f"  唯一键: (name,region) + code可空唯一 | 行数: {len(rows)} | 前置: 区划须已导入")

        def row_fn(row: dict) -> str:
            name, stype = row.get("name", ""), row.get("school_type", "")
            if not name:
                raise ValueError("name 必填")
            if stype not in SCHOOL_TYPE_CODES:
                raise ValueError(f"school_type 非法: {stype!r}(允许: {','.join(SCHOOL_TYPE_CODES)})")
            region = Region.objects.filter(code=row.get("region_code", "")).first()
            if region is None:
                raise ValueError(f"region_code {row.get('region_code')!r} 不存在(先导入区划)")
            defaults = {"code": row.get("code", "") or None, "school_type": stype,
                        "address": row.get("address") or None,
                        "is_active": _to_bool(row.get("is_active"), True)}
            if not self.execute:
                exists = School.objects.filter(name=name, region=region).exists()
                return "skipped" if exists else "created"
            with transaction.atomic():
                _, created = School.objects.get_or_create(name=name, region=region, defaults=defaults)
            return "created" if created else "skipped"

        return self._process_rows(rows, row_fn)

    def _run_dict_items(self) -> dict:
        self.stdout.write("\n== 阶段3: base_dict_item 字典项导入 ==")
        if not self.csv_paths.get("dict_items"):
            self.stdout.write("  [跳过] 缺少 --dict-items-csv（成员5清单未确认，不编造）")
            return {"created": 0, "skipped": 0, "failed": []}
        rows = _read_csv(self.csv_paths["dict_items"])
        self.stdout.write(f"  唯一键: (type,value) | 行数: {len(rows)} | 前置: 字典类型须已存在")

        # 跨行预检：同一 type_code 下 label 重复 = 下拉出现同名两行，属数据质量问题，
        # 导入前按 CSV 行号整体拦截（value 唯一由 (type,value) 键保证，label 无唯一约束）。
        seen_labels: dict[tuple[str, str], int] = {}
        for line_no, row in enumerate(rows, start=2):  # 行号含表头偏移
            key = (row.get("type_code", ""), row.get("label", ""))
            if key[1] and key in seen_labels:
                raise CommandError(
                    f"dict_items 第 {seen_labels[key]} 行与第 {line_no} 行 label 重复"
                    f"（type_code={key[0]!r}, label={key[1]!r}），请修正后再导入"
                )
            if key[1]:
                seen_labels[key] = line_no

        def row_fn(row: dict) -> str:
            dtype = DictType.objects.filter(code=row.get("type_code", "")).first()
            if dtype is None:
                raise ValueError(f"type_code {row.get('type_code')!r} 不存在")
            value, label = row.get("value", ""), row.get("label", "")
            if not value or not label:
                raise ValueError("value/label 必填")
            if not _VALUE_RE.match(value):
                raise ValueError(
                    f"value 编码格式非法: {value!r}（仅限字母/数字/下划线/连字符，"
                    f"以字母或数字开头，≤50 字符，与列 VARCHAR(50) 一致）"
                )
            ext_raw = row.get("ext_json", "") or None
            ext = None
            if ext_raw:
                try:
                    ext = json.loads(ext_raw)  # 存对象而非字符串
                except json.JSONDecodeError as exc:
                    raise ValueError(f"ext_json 非法: {exc}") from exc
            defaults = {"label": label, "sort_order": int(row.get("sort_order") or 0),
                        "is_active": _to_bool(row.get("is_active"), True), "ext_json": ext}
            if not self.execute:
                exists = DictItem.objects.filter(type=dtype, value=value).exists()
                return "skipped" if exists else "created"
            with transaction.atomic():
                _, created = DictItem.objects.get_or_create(type=dtype, value=value, defaults=defaults)
            return "created" if created else "skipped"

        return self._process_rows(rows, row_fn)

    def _run_material_types(self) -> dict:
        self.stdout.write("\n== 阶段4: material_type 材料类型导入 ==")
        csv_path = self.csv_paths.get("material")
        source = "参数 --material-csv"
        if not csv_path:
            csv_path = (str(DEFAULT_MATERIAL_CSV)
                        if DEFAULT_MATERIAL_CSV.is_file() else None)
            if csv_path:
                source = "默认路径（已签发 13 类清单）"
        if not csv_path:
            self.stdout.write(
                "  [跳过] 缺少 --material-csv 且默认清单不存在"
                "（材料类型规则未经产品确认，不编造）")
            return {"created": 0, "skipped": 0, "failed": []}
        self.stdout.write(f"  数据源: {csv_path} ({source})")
        rows = _read_csv(csv_path)
        self.stdout.write(f"  唯一键: code | 行数: {len(rows)}")

        def row_fn(row: dict) -> str:
            code, name = row.get("code", ""), row.get("name", "")
            applies = row.get("applies_to", "")
            if not code or not name:
                raise ValueError("code/name 必填")
            if applies not in {"PERSON", "TEAM"}:
                raise ValueError(f"applies_to 非法: {applies!r}(PERSON/TEAM)")
            defaults = {"name": name, "applies_to": applies,
                        "is_required": _to_bool(row.get("is_required"), False),
                        "need_audit": _to_bool(row.get("need_audit"), True),
                        "allow_multiple": _to_bool(row.get("allow_multiple"), False),
                        "allowed_ext": row.get("allowed_ext") or "jpg,jpeg,png,pdf",
                        "max_size_mb": int(row.get("max_size_mb") or 10),
                        "sort_order": int(row.get("sort_order") or 0),
                        "is_active": _to_bool(row.get("is_active"), True),
                        "remark": row.get("remark") or ""}
            if not self.execute:
                exists = MaterialType.objects.filter(code=code).exists()
                return "skipped" if exists else "created"
            with transaction.atomic():
                _, created = MaterialType.objects.get_or_create(code=code, defaults=defaults)
            return "created" if created else "skipped"

        return self._process_rows(rows, row_fn)

    # ---------------------------------------------------------------- 只读验收

    def _run_verify(self, phase: str) -> None:
        """只读体检：仅查询（select_related/values_list/exists），无任何 save/create/update/delete；
        硬错=违反冻结约束或已确认服务层规则；WARN=可疑但合法/依赖未确认清单；INFO=空表提示。"""
        findings: list[tuple[str, str]] = []
        scopes = (["dict_types", "regions", "schools", "dict_items", "material_types"]
                  if phase == "all" else [phase])

        if "dict_types" in scopes:
            if DictType.objects.count() == 0:
                findings.append(("INFO", "base_dict_type: 空表, 未初始化"))
            else:
                for item in DICT_TYPES:  # 骨架缺失仅 WARN：名称/清单尚未最终确认，不判硬错
                    if not DictType.objects.filter(code=item["code"]).exists():
                        findings.append(("WARN", f"base_dict_type: 骨架缺失 {item['code']}"
                                                 f" (阶段 dict_types 未执行或被人工清理)"))

        if "regions" in scopes:  # 严格层级规则已在 base-data 接口确认 → 违反即硬错
            if Region.objects.count() == 0:
                findings.append(("INFO", "base_region: 空表, 未初始化"))
            # 与学校/字典段一致：不能用 select_related——其 JOIN 会把悬空父级引用行从结果集丢弃
            parent_by_pk = {r.pk: r for r in Region.objects.all()}
            for r in Region.objects.order_by("code"):
                loc = f"code={r.code}"
                if r.level not in (1, 2, 3):  # 与模型 choices/导入校验/服务层同源；仅绕过写入可达
                    findings.append(("ERROR", f"base_region: {loc} level={r.level} 非法(允许: 1/2/3)"))
                    continue  # level 无效时后续父子规则无从判断
                parent = parent_by_pk.get(r.parent_id) if r.parent_id is not None else None
                if parent is None and r.parent_id is not None:  # 仅防绕过 ORM 的直改
                    findings.append(("ERROR", f"base_region: {loc} 引用的父区划 id={r.parent_id} 不存在"))
                    continue  # 父级无效时后续父子规则无从判断
                if r.level == 1 and parent is not None:
                    findings.append(("ERROR", f"base_region: {loc} level=1 省级不得有父级(现父={parent.code})"))
                if r.level > 1 and parent is None:
                    findings.append(("ERROR", f"base_region: {loc} level={r.level} 非省级缺少父级"))
                if parent is not None:
                    if parent.level != r.level - 1:
                        findings.append(("ERROR", f"base_region: {loc} level={r.level}"
                                                  f" 父级 level={parent.level} 不满足 level-1"))
                    if not parent.is_active:  # 停用不级联，子区划仍合法 → 仅提示
                        findings.append(("WARN", f"base_region: {loc} 父级 {parent.code} 已停用(请人工确认)"))
            levels = set(Region.objects.values_list("level", flat=True))
            if 3 in levels and 2 not in levels:
                findings.append(("WARN", "base_region: 存在 level=3 但缺失 level=2 (层级覆盖缺口, 允许部分导入)"))
            if 2 in levels and 1 not in levels:
                findings.append(("WARN", "base_region: 存在 level=2 但缺失 level=1 (层级覆盖缺口, 允许部分导入)"))

        if "schools" in scopes:
            if School.objects.count() == 0:
                findings.append(("INFO", "base_school: 空表, 未初始化"))
            # 注意：不能用 select_related——其 JOIN 会把悬空引用行从结果集丢弃，导致防御检查失效
            regions_by_pk = {r.pk: r for r in Region.objects.all()}
            for s in School.objects.order_by("pk"):
                loc = f"code={s.code}" if s.code else f"pk={s.pk}"
                if s.school_type not in SCHOOL_TYPE_CODES:  # DDL 列注释枚举（冻结值）
                    findings.append(("ERROR", f"base_school: {loc} school_type={s.school_type!r}"
                                              f" 非法(允许: {','.join(SCHOOL_TYPE_CODES)})"))
                if s.code == "":  # 空串占唯一键、阻塞后续导入合法行 → 可疑但不违规
                    findings.append(("WARN", f"base_school: {loc}(空串占唯一键, 应为 NULL) name={s.name}"))
                if s.region_id is not None and s.region_id not in regions_by_pk:  # 仅防绕过 ORM 的直改
                    findings.append(("ERROR", f"base_school: {loc} 引用的区划 id={s.region_id} 不存在"))

        if "dict_items" in scopes:
            if DictItem.objects.count() == 0:
                findings.append(("INFO", "base_dict_item: 空表, 未初始化"))
            types_by_pk = {t.pk: t for t in DictType.objects.all()}  # 同上：预加载防 JOIN 丢行
            for d in DictItem.objects.order_by("pk"):
                dtype = types_by_pk.get(d.type_id) if d.type_id is not None else None
                if d.type_id is not None and dtype is None:  # 仅防绕过 ORM 的直改
                    findings.append(("ERROR", f"base_dict_item: pk={d.pk} value={d.value}"
                                              f" 引用的字典类型 id={d.type_id} 不存在"))
                elif dtype is not None and dtype.code not in SKELETON_CODES:
                    # 正式字典清单未确认，骨架外类型合法 → 人工确认级
                    findings.append(("WARN", f"base_dict_item: type={dtype.code} value={d.value}"
                                             f" 挂在骨架外类型(正式清单未确认, 请人工确认)"))

        if "material_types" in scopes:
            if MaterialType.objects.count() == 0:
                findings.append(("INFO", "material_type: 空表, 未初始化"))
            for m in MaterialType.objects.order_by("pk"):
                if m.applies_to not in ("PERSON", "TEAM"):  # 与导入校验同源
                    findings.append(("ERROR", f"material_type: code={m.code}"
                                              f" applies_to={m.applies_to!r} 非法(允许: PERSON/TEAM)"))

        errors = [f for lv, f in findings if lv == "ERROR"]
        warns = [f for lv, f in findings if lv == "WARN"]
        infos = [f for lv, f in findings if lv == "INFO"]
        self.stdout.write("\n== 只读验收报告 (--verify) ==")
        for lv, msg in findings:
            self.stdout.write(f"  [{lv:<5}] {msg}")
        if not findings:
            self.stdout.write("  (全部检查通过, 无发现项)")
        self.stdout.write(f"  合计: 错误={len(errors)} 警告={len(warns)} 提示={len(infos)}")
        self.stdout.write("[提示] --verify 为只读验收, 未写入/未修复任何数据。")
        if errors or (self.strict and warns):
            sys.exit(1)

    # ---------------------------------------------------------------- 入口

    def handle(self, *args, **options):
        self.execute = options["execute"]
        self.verify = options["verify"]
        self.strict = options["strict"]
        self.csv_paths = {k: options[f"{k}_csv"] for k in ("regions", "schools", "dict_items", "material")}
        if self.verify and self.execute:
            raise CommandError("--verify 与 --execute 互斥：验收模式绝不写入")
        self._print_target()
        self._print_counts()

        phase = options["phase"]
        if self.verify:  # 只读验收分支：仅查询，与初始化写入路径完全隔离
            self._run_verify(phase)
            return

        runners = {"dict_types": self._run_dict_types, "regions": self._run_regions,
                   "schools": self._run_schools, "dict_items": self._run_dict_items,
                   "material_types": self._run_material_types}
        order = ["dict_types", "regions", "schools", "dict_items", "material_types"]
        results = {}
        for key in (order if phase == "all" else [phase]):
            if self.execute:
                with transaction.atomic():
                    results[key] = runners[key]()
            else:
                results[key] = runners[key]()

        self.stdout.write("\n== 初始化报告 ==")
        total = {"created": 0, "skipped": 0, "failed": 0}
        for key, rep in results.items():
            fails = "; ".join(f"行{f['row']}[{f['key']}]:{f['reason']}" for f in rep["failed"]) or "无"
            self.stdout.write(f"  {key:<16} 新增={rep['created']} 跳过={rep['skipped']} 失败={len(rep['failed'])}"
                              + (f" | 失败明细: {fails}" if rep["failed"] else ""))
            total["created"] += rep["created"]
            total["skipped"] += rep["skipped"]
            total["failed"] += len(rep["failed"])
        self.stdout.write(f"  合计: 新增={total['created']} 跳过={total['skipped']} 失败={total['failed']}")
        if not self.execute:
            self.stdout.write("\n[提示] 本次为只读预览，未写入任何数据；确认无误后加 --execute 执行。")
