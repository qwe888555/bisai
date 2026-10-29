# Models 落地核对报告（依据 DDL V1.4-r1 冻结版）

> 配套文件：《成员4-DDL草案-V1.4.sql》（V1.4-r1，冻结，唯一数据库依据）
> 阶段声明：Models 阶段（不连库、不迁移）→ 迁移阶段（经用户下令，2026-10-21 执行，见 E 节）。
> 分支：team-archive；自测环境：Django 5.2.18（临时 .pylibs，隔离安装）。
> 状态：**Models 与迁移均已完成并全链路验证通过（SQLite），未提交 git，待人工确认后提交。**

---

## A. 交付物清单

| 交付 | 位置 | 说明 |
|------|------|------|
| persons app | `backend/apps/persons/` | 10 模型（人员域 + 基础数据 + 敏感访问审计） |
| teams app | `backend/apps/teams/` | 4 模型（球队域） |
| materials app | `backend/apps/materials/` | 5 模型（通用材料引擎） |
| infra app | `backend/apps/infra/` | 3 模型（导入批次/失败明细/通用审计） |
| 统一枚举 | `backend/apps/common/enums.py` | TextChoices/IntegerChoices 唯一出处（§4.0/§十一） |
| 精确类型字段 | `backend/apps/common/fields.py` | TinyIntegerField(tinyint)、TextField(text) |
| 应用注册 | `backend/config/settings.py` | 仅追加 4 行 INSTALLED_APPS，其余零改动 |

自测结果：
- `manage.py check` → 0 issues（Django 5.2.18）；
- 模型注册表核对：**模型 22**（persons 10 / teams 4 / materials 5 / infra 3）、**外键 28**（PROTECT 25 + CASCADE 3）、**UNIQUE 13**（10 单列 + 3 联合）、**命名索引 43**，名称/列序与冻结 DDL 逐项一致；
- 迁移验证：`makemigrations --check` 无漂移；SQLite 全链路 `migrate` 通过，DDL 22 表全部落库，ORM 增改删冒烟正常（详见 E 执行记录）。

---

## B. 22 表 → 模型映射清单

| # | DDL 表名 | Model（app.Model） | 归属 | 特性 |
|---|----------|--------------------|------|------|
| 1 | base_region | persons.Region | persons | 自引用树形 PROTECT |
| 2 | base_school | persons.School | persons | code 可空唯一；(name,region) 唯一 |
| 3 | base_dict_type | persons.DictType | persons | |
| 4 | base_dict_item | persons.DictItem | persons | (type,value) 唯一 |
| 5 | material_type | materials.MaterialType | materials | 只停用不删 |
| 6 | import_batch | infra.ImportBatch | infra | batch_no 幂等键 |
| 7 | person | persons.Person | persons | 软删；敏感三件套；id_card_hash 全局唯一 |
| 8 | person_athlete_profile | persons.AthleteProfile | persons | 1:1，person_id 即主键，CASCADE |
| 9 | person_coach_profile | persons.CoachProfile | persons | 1:1，person_id 即主键，CASCADE |
| 10 | person_change_log | persons.PersonChangeLog | persons | append-only |
| 11 | team | teams.Team | teams | 软删 |
| 12 | team_transfer | teams.TeamTransfer | teams | 仅运动员（F1 方案B），禁物理删除 |
| 13 | team_member | teams.TeamMember | teams | 含历史 N:M，禁物理删除 |
| 14 | team_coach | teams.TeamCoach | teams | 无 transfer_id（F1），禁物理删除 |
| 15 | person_merge_record | persons.PersonMergeRecord | persons | 双 FK→Person（merges_kept/merges_merged） |
| 16 | person_material | materials.PersonMaterial | materials | 软删；幂等键唯一 |
| 17 | team_material | materials.TeamMaterial | materials | 软删；幂等键唯一 |
| 18 | material_audit_log | materials.MaterialAuditLog | materials | append-only；多态逻辑引用无 FK |
| 19 | material_file_version | materials.MaterialFileVersion | materials | append-only；F5 已删冗余索引 |
| 20 | sensitive_access_log | persons.SensitiveAccessLog | persons | append-only |
| 21 | audit_log | infra.AuditLog | infra | append-only；无外键（A5 定稿取舍） |
| 22 | import_row_error | infra.ImportRowError | infra | append-only；随批次 CASCADE |

表名/列名完全沿用 DDL（FK 字段 Django 命名 `region`→列 `region_id`，与 DDL 列名一致）。

---

## C. 约束逐项核对

### C1 外键 28 条（on_delete 对应 DDL RESTRICT/CASCADE）

| DDL 约束 | 列 | 引用 | Django on_delete | 结果 |
|---|---|---|---|---|
| fk_region_parent | base_region.parent_id | base_region | PROTECT | ✅ |
| fk_school_region | base_school.region_id | base_region | PROTECT | ✅ |
| fk_dict_item_type | base_dict_item.type_id | base_dict_type | PROTECT | ✅ |
| fk_person_region / fk_person_school / fk_person_import | person.region_id / school_id / import_batch_id | base_region / base_school / import_batch | PROTECT | ✅ |
| fk_athlete_person | person_athlete_profile.person_id | person | **CASCADE** | ✅（0002 显式建约束，见 D3） |
| fk_coach_person | person_coach_profile.person_id | person | **CASCADE** | ✅（0002 显式建约束，见 D3） |
| fk_pcl_person | person_change_log.person_id | person | PROTECT | ✅ |
| fk_pmr_primary / fk_pmr_merged | person_merge_record.primary_person_id / merged_person_id | person | PROTECT | ✅（A7 补录两条） |
| fk_sal_person | sensitive_access_log.target_person_id | person | PROTECT | ✅ |
| fk_team_region / fk_team_school / fk_team_import | team.region_id / school_id / import_batch_id | base_region / base_school / import_batch | PROTECT | ✅ |
| fk_tt_person / fk_tt_from / fk_tt_to | team_transfer.person_id / from_team_id / to_team_id | person / team / team | PROTECT | ✅ |
| fk_tm_team / fk_tm_person / fk_tm_transfer | team_member.team_id / person_id / transfer_id | team / person / team_transfer | PROTECT | ✅ |
| fk_tc_team / fk_tc_person | team_coach.team_id / person_id | team / person | PROTECT | ✅ |
| fk_pm_person / fk_pm_type | person_material.person_id / type_id | person / material_type | PROTECT | ✅ |
| fk_tmat_team / fk_tmat_type | team_material.team_id / type_id | team / material_type | PROTECT | ✅ |
| fk_ire_batch | import_row_error.batch_id | import_batch | **CASCADE** | ✅（0002 显式建约束，见 D3） |

逻辑引用（无 FK，应用层校验）：全部 `*_by`/`actor_user_id`（底座用户）、`*_file_id`（文件服务）、`material_kind+material_id`（多态）均按 DDL 建为普通字段，未伪造成 FK。✅

### C2 UNIQUE 13 条（名称经 Meta.constraints 全部精确保留）

| DDL 名称 | 列 | 类型 | 结果 |
|---|---|---|---|
| uk_region_code / uk_school_code / uk_dict_type_code / uk_material_type_code / uk_import_batch_no | 各表 code / batch_no | 单列 | ✅ |
| uk_person_no / uk_team_no | person_no / team_no | 单列 | ✅ |
| uk_person_id_card_hash | id_card_hash | 单列（含软删行） | ✅ |
| uk_pm_idempotency / uk_tm_idempotency | idempotency_key | 单列可空（多 NULL 合法，与 DDL 语义一致） | ✅ |
| uk_school_name_region | (name, region_id) | 联合 | ✅ |
| uk_dict_item_value | (type_id, value) | 联合 | ✅ |
| uk_mfv_version | (material_kind, material_id, version) | 联合 | ✅ |

### C3 命名索引 43 条（Meta.indexes 精确保留名称与列序）

| 表 | 索引 |
|---|---|
| base_region(2) | idx_region_level；idx_region_parent(parent,is_active) |
| base_school(2) | idx_school_name；idx_school_region |
| base_dict_item(1) | idx_dict_item_active(type,is_active) |
| person(7) | idx_person_name；idx_person_cat_audit；idx_person_status_del；idx_person_region；idx_person_school；idx_person_phone_hash；idx_person_import |
| person_athlete_profile(1) / person_coach_profile(1) | idx_athlete_student；idx_coach_cert |
| person_change_log(1) | idx_pcl_person(person,created_at) |
| person_merge_record(2) | idx_pmr_primary；idx_pmr_merged |
| sensitive_access_log(2) | idx_sal_person(target_person,created_at)；idx_sal_actor(accessed_by,created_at) |
| team(5) | idx_team_name；idx_team_region_cat(region,category_code)；idx_team_school；idx_team_status_del；idx_team_import |
| team_transfer(3) | idx_tt_person_status；idx_tt_from；idx_tt_to |
| team_member(4) | idx_tm_person_status；idx_tm_team_status；idx_tm_team_jersey；idx_tm_transfer |
| team_coach(2) | idx_tc_person_status；idx_tc_team_status |
| person_material(3) | idx_pm_person_type_status(person,type,status)；idx_pm_status_del；idx_pm_type |
| team_material(3) | idx_tm2_team_type_status；idx_tm2_status_del；idx_tm2_type |
| material_audit_log(1) | idx_mal_material(material_kind,material_id) |
| material_file_version(0) | 仅 uk_mfv_version（F5 已删冗余索引，未建模） |
| audit_log(2) | idx_audit_target(target_type,target_id)；idx_audit_actor(actor_user_id,created_at) |
| import_row_error(1) | idx_ire_batch |

防重复索引处理：**全部 28 个外键列 `db_index=False`**，其索引需求由上表命名索引（多数为联合索引最左前缀）覆盖——Django MySQL 后端 `can_create_inline_fk=False`，建表时先建 Meta 索引、后补 FK 约束，FK 会复用既有最左前缀索引，不会产生 DDL 之外的附属索引。

### C4 服务层唯一规则（按冻结决策不建 DB 约束）

R1–R6 六条"现役唯一"规则未在 Models 中表达为 UniqueConstraint（历史行需共存），交付 Service 阶段用事务锁（`select_for_update`）+ 条件更新实现，口径见《DDL 审核报告-V1.4》D 节。idempotency_key 唯一索引作为 R6 兜底已就位。

---

## D. Django ORM 与最终 DDL 差异及处理建议

| # | 差异点 | 现状 | 处理建议 |
|---|--------|------|----------|
| D1 | 时间戳默认值 | DDL：`DEFAULT CURRENT_TIMESTAMP(6)`(+ON UPDATE)；Models：`auto_now_add/auto_now`（总体设计 §4.0 约定，迁移生成的列**无 DB 端默认值**） | ORM 写入路径完全等价（每次 save 自动维护）；纯 SQL 插入需显式给时间。**不建议**再叠加 RunSQL 补 DB 默认（双轨易漂移）；如项目组坚持 DB 级默认，迁移阶段补 RunSQL 即可 |
| D2 | FK 约束名 | 3 条 CASCADE 外键已按 DDL 具名（fk_athlete_person / fk_coach_person / fk_ire_batch，经 `db_constraint=False` + 0002 RunSQL 显式建约束）；其余 25 条由 Django 自动命名 | 自动命名与约束语义无关，备案即可 |
| D3 | CASCADE×3 | **已落地**：3 列 `db_constraint=False`，0002 迁移在 MySQL 显式建 `ON DELETE CASCADE ON UPDATE RESTRICT`（`atomic=False`，可回滚）；SQLite 开发库跳过（无 FK 约束，开发期无级联需求） | 无遗留 |
| D4 | 类型映射 | gender TINYINT→自定义 TinyIntegerField ✅；remark/introduction TEXT→自定义 TextField ✅（否则 longtext）；DATETIME(6)✅；TINYINT(1)→BooleanField ✅；JSON→json ✅；ip_address 用 CharField(39)（实测 Django 5.2 的 GenericIPAddressField 在 MySQL 生成 **char(39)** ≠ VARCHAR(39)，故弃用，IP 格式校验移至接口 Schema 层）✅ | 已在代码内解决，无遗留 |
| D5 | 枚举表达 | 状态类字段：TextChoices 统一收口 `apps/common/enums.py`（§4.0"唯一出处"）；字典驱动字段（school_type/category_code/grade_level/position_code/coach_level/guardian_relation）**不硬编码 choices**，按 DDL 冻结版走 base_dict_item（总体设计 §4.2/§4.5 曾列 choices，以 DDL 为准；F7 由服务层校验 code） | 已按 DDL 执行；设计文档差异在此备案 |
| D6 | 软删表达 | Person/Team/PersonMaterial/TeamMaterial 含 is_deleted/deleted_at，与 §4.0 一致；**未加过滤型默认 Manager**（现有模板 attachments 有 ActiveManager） | 服务层显式 `is_deleted=0` 过滤（与 DDL 审核报告 D 节 SQL 口径一致）；如需统一 Manager，后续可在 Service 阶段补充，不影响表结构 |
| D7 | append-only 6 表 | 模型层无法强制禁改 | 部署层收回 UPDATE/DELETE（F6）+ 服务层约定 + audit_log 无外键（A5）已按定稿建模 |
| D8 | nullable 唯一 | school.code / idempotency_key 可空唯一，MySQL 多 NULL 合法，与 DDL 注释一致 | 无处理需要 ✅ |
| D9 | person_no/team_no | 建表即唯一、应用层回填编号（DDL 一致） | 无处理需要 ✅ |
| D10 | **V1.5 冻结差异（E1 2026-10-27）：`person.education`** | **V1.4-r1 冻结版无此列**；组长批准 E1 解冻加列：`education VARCHAR(100) NULL`（可选自由文本 ≤100，业务未定字典），模型 `Person.education`（db_comment 与迁移 0003 一致），**A 节"逐项一致"基线自此存在 1 处经批准差异** | ①本条即 V1.5 差异登记备案，部署审批时随本报告提交；②迁移 `persons/0003_person_education`（AddField，`atomic=False` 与 0002 同口径）；③目标 MySQL 8.0 的 migrate 与 `SHOW CREATE TABLE person` 对照**尚未执行**（本地仅 SQLite 内存库验证），Online DDL 锁行为待目标库版本确认 |

---

## E. 迁移实施与执行记录

### 已执行（2026-10-21，本地 SQLite 验证）

1. **生成迁移（E-1）**：4×`0001_initial`（infra/persons/teams/materials）+ 2×`0002_cascade`（infra/persons），跨 app 依赖由 Django 自动排序，迁移文件未手工改动结构；
2. **迁移审查（E-2）**：逐文件核对通过——`db_constraint=False`×3、具名约束/索引共 56 个（13 UK + 43 idx）与 C 节清单完全一致；
3. **CASCADE 补齐（E-3）**：0002 RunSQL 在 MySQL 显式建 fk_athlete_person / fk_coach_person / fk_ire_batch（ON DELETE CASCADE ON UPDATE RESTRICT，`atomic=False`，vendor 守卫，SQLite 跳过，可回滚）；
4. **落库验证（E-6 前半）**：`makemigrations --check` 无漂移；SQLite `migrate` 全链路 OK，DDL 22 表全部落库；`person` 建表语句抽样核对（varchar 长度、tinyint）无误；ORM 增/改/删/时间戳冒烟通过。
   - 注：SQLite 不支持列/表注释，migrate 时 W163/W046 告警属预期，`db_comment`/`db_table_comment` 在 MySQL 端生效。

### 剩余事项（部署 / 后续阶段）

| # | 事项 | 时机 |
|---|------|------|
| 1 | MySQL 8.0 目标库 migrate 后 `SHOW CREATE TABLE` 逐表对照冻结 DDL（列类型/索引名/UNIQUE 名/ON DELETE），并确认 `person.education` 按 D10 V1.5 差异落地 | 部署环境就绪 |
| 1a | **V1.5 差异登记（E1）**：`persons/0003_person_education` 在目标 MySQL 8.0 执行 migrate + 回滚演练（RemoveField 仅限未录业务数据时），Online DDL 实际锁行为以目标库版本实测为准 | 冻结差异登记审批后 |
| 2 | `0002_seed_dict` 数据迁移（get_or_create 幂等）+ `import_regions` / `import_schools` 命令 | 字典/区划初始化阶段（§十六） |
| 3 | 部署层 F6：6 张 append-only 表收回业务账号 UPDATE/DELETE | 部署脚本 |

---

## 修订记录

| 版本 | 日期 | 说明 |
|------|------|------|
| V1.0 | 2026-10-21 | 基于 DDL V1.4-r1 冻结版完成 22 模型落地与静态核对；自测通过，待审阅 |
| V1.1 | 2026-10-21 | 迁移阶段执行完毕：4×0001 + 2×0002 生成并审查；3 条 CASCADE 外键经 db_constraint=False + RunSQL 按 DDL 具名落地（D2/D3 更新）；SQLite 全链路 migrate + ORM 冒烟通过 |
| V1.5 | 2026-10-27 | **E1 冻结差异备案**：组长批准解冻加列 `Person.education`（可选自由文本 ≤100，迁移 0003），A 节基线存在 1 处经批准差异（D10）；目标 MySQL 迁移验证未执行，见 E 节事项 1a |
