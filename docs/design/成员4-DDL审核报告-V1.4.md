# 成员4 DDL 审核报告 V1.4-r1（F1/F5 已决议）

> 配套文件：《成员4-DDL草案-V1.4.sql》（V1.4-r1，禁止执行，仅供人工审核）
> 依据：V1.2 冻结候选版 + V1.3 结构审核报告，已交叉核对。
> 修订记录见文末。

---

## A. 自检十项结果

| # | 检查项 | 结果 |
|---|--------|------|
| 1 | 表数量 = 22 | ✅（base_region/base_school/base_dict_type/base_dict_item/material_type/import_batch/person/person_athlete_profile/person_coach_profile/person_change_log/team/team_transfer/team_member/team_coach/person_merge_record/person_material/team_material/material_audit_log/material_file_version/sensitive_access_log/audit_log/import_row_error） |
| 2 | 外键 = 28（A7 勘误：原"26 条"清单漏列 person_merge_record 的 2 条 FK，V1.2 PMR 字段表明确定义，SQL 忠实实现），逐条对应 V1.3 修订后清单 | ✅（映射见 C 节；F1 已决议：team_coach 无 transfer_id） |
| 3 | PROTECT/CASCADE/SET_NULL 实现 | ✅ PROTECT→`ON DELETE RESTRICT ON UPDATE RESTRICT`×25；CASCADE×3（athlete/coach→person、row_error→batch）；SET_NULL 0 |
| 4 | UNIQUE/索引/服务层规则完整性 | ✅ 单列 UNIQUE 10、联合 UNIQUE 3、普通索引按 V1.3 清单、6 条服务层规则未建索引且已注释标注（D 节） |
| 5 | 字段类型/长度/NULL/默认/字符集/时间精度 | ✅ VARCHAR 长度、TEXT/JSON/DATE/SMALLINT/TINYINT(1)、DATETIME(6)+CURRENT_TIMESTAMP(6)、utf8mb4_0900_ai_ci，与 V1.2 逐字段一致 |
| 6 | 外键列索引 | ✅ 全部 28 个外键列有可用索引；5 处为 MySQL FK 附属索引（非业务冗余）：tm.transfer_id、pm.type_id、tmat.type_id、pmr.primary/merged_person_id |
| 7 | 建表顺序满足依赖 | ✅ 9 组顺序：基础数据→import_batch→person/team→档案/日志→关系/单据→材料→审计→明细；**无循环依赖**（Region 自引用同表内合法） |
| 8 | 重复/冗余索引、语法 | ✅ 已裁定（F5）：删除 material_file_version.idx_mfv_material（uk_mfv_version 最左前缀完全覆盖其全部查询场景，无独立用途）；material_audit_log.idx_mal_material **保留**（该表无联合唯一键，此索引是唯一检索入口）；其余无重复；MySQL 8.0 语法全部合法 |
| 9 | 逻辑引用与应用层规则标注 | ✅ 文件头第 3/4 条 + 行内 COMMENT 均注明"逻辑引用/服务层强制"，未伪称 DB 约束 |
| 10 | 遗漏/矛盾/语法错误 | ✅ 已决议（F1 方案B + A7 勘误）：team_coach 不含 transfer_id；外键总数修正为 28（原清单漏列 PMR×2）；SQL 与文档已对齐 |

## B. 逐表核对（主键/外键/唯一/索引）

| 表 | PK | FK 数 | UNIQUE | 索引数 | 备注 |
|----|----|----|--------|--------|------|
| base_region | id | 1(自引用) | code | 2 | 树形 |
| base_school | id | 1 | code; (name,region_id) | 2 | |
| base_dict_type | id | 0 | code | 0 | |
| base_dict_item | id | 1 | (type_id,value) | 1 | |
| material_type | id | 0 | code | 0 | |
| import_batch | id | 0 | batch_no | 0 | |
| person | id | 3 | person_no; id_card_hash | 7 | 敏感加密 |
| person_athlete_profile | person_id | 1(CASCADE) | — | 1 | 1:1 |
| person_coach_profile | person_id | 1(CASCADE) | — | 1 | 1:1 |
| person_change_log | id | 1 | — | 1 | append-only |
| team | id | 3 | team_no | 5 | |
| team_transfer | id | 3 | — | 3 | R5 服务层 |
| team_member | id | 3 | — | 4 | R2/R3 服务层 |
| team_coach | id | 2 | — | 2 | R4 服务层；F1 已决议（方案B，无 transfer_id） |
| person_merge_record | id | 2 | — | 2 | |
| person_material | id | 2 | idempotency_key | 3 | R6 服务层 |
| team_material | id | 2 | idempotency_key | 3 | R6 服务层 |
| material_audit_log | id | 0(逻辑) | — | 1 | append-only |
| material_file_version | id | 0(逻辑) | (kind,id,version) | 0(仅UK) | append-only；F5 已删冗余索引 |
| sensitive_access_log | id | 1 | — | 2 | append-only |
| audit_log | id | 0(定稿取舍) | — | 2 | append-only |
| import_row_error | id | 1(CASCADE) | — | 1 | append-only |

合计：FK 28（自引用 1 + 其余 27；A7 勘误补录 PMR×2）✓

## C. 外键策略映射（28 条）

实现约定：**PROTECT → `ON DELETE RESTRICT ON UPDATE RESTRICT`**（InnoDB 即时拒绝，等价于设计意图"先在 DB 层报错"）；CASCADE → `ON DELETE CASCADE ON UPDATE RESTRICT`（更新始终 RESTRICT，防主键漂移——主键为 AUTO_INCREMENT 实际不会更新，显式声明为防御）。

| 设计条目（V1.3#） | SQL 实现 |
|---|---|
| #1 region.parent（可空） | fk_region_parent RESTRICT/RESTRICT |
| #2 school.region | fk_school_region RESTRICT/RESTRICT |
| #7 dict_item.type | fk_dict_item_type RESTRICT/RESTRICT |
| #3/#5 person.region/school | fk_person_region、fk_person_school RESTRICT/RESTRICT |
| #25 person.import_batch | fk_person_import RESTRICT/RESTRICT |
| #8/#9 athlete/coach.person | fk_athlete_person、fk_coach_person **CASCADE**/RESTRICT |
| #10 change_log.person | fk_pcl_person RESTRICT/RESTRICT |
| #4/#6 team.region/school | fk_team_region、fk_team_school RESTRICT/RESTRICT |
| #26 team.import_batch | fk_team_import RESTRICT/RESTRICT |
| #16/#17/#18 transfer.person/from/to | fk_tt_person、fk_tt_from、fk_tt_to RESTRICT/RESTRICT |
| #12/#13/#19 member.team/person/transfer | fk_tm_team、fk_tm_person、fk_tm_transfer RESTRICT/RESTRICT |
| #14/#15 coach.team/person | fk_tc_team、fk_tc_person RESTRICT/RESTRICT |
| #23 sal.target_person | fk_sal_person RESTRICT/RESTRICT |
| #20/#21 材料表.type | fk_pm_type、fk_tmat_type RESTRICT/RESTRICT |
| #11/#22 材料表.归属 | fk_pm_person、fk_tmat_team RESTRICT/RESTRICT |
| #24 row_error.batch | fk_ire_batch **CASCADE**/RESTRICT |
| #27 PMR.primary_person（A7 补录） | fk_pmr_primary RESTRICT/RESTRICT |
| #28 PMR.merged_person（A7 补录） | fk_pmr_merged RESTRICT/RESTRICT |

未映射：#（无）——V1.3 修订后全部 28 条均有对应约束；多态逻辑引用（material_audit_log/material_file_version）与 audit_log 按定稿不建 FK。

## D. 6 条服务层唯一规则实现方案（数据库侧不做唯一索引）

| 规则 | 并发保证机制（Service 层） |
|------|--------------------------|
| R1 同校同组别现役球队唯一 | 事务内 `SELECT ... FROM team WHERE school_id=? AND category_code=? AND status='ACTIVE' AND is_deleted=0 FOR UPDATE` 锁定候选集后校验再插入 |
| R2 person 全局现役唯一 | 事务内锁该 person 的全部 team_member/team_coach 行（`WHERE person_id=? FOR UPDATE`）后校验 CURRENT 存在性；id_card_hash 唯一索引兜底防同人双档 |
| R3 (team,jersey) 现役唯一 | 事务内 `SELECT ... WHERE team_id=? AND status='CURRENT' FOR UPDATE` 后校验；命中 (team_id,jersey_number) 索引 |
| R4 现役主教练唯一 | 同 R3 模式，锁 (team_id, duty) 集合后校验 |
| R5 person 单条 PENDING 转队单 | 创建时 `SELECT ... WHERE person_id=? AND status='PENDING' FOR UPDATE`；办理时条件更新 `UPDATE ... SET status='COMPLETED' WHERE id=? AND status='PENDING'`，影响行数=0 即拒绝 |
| R6 allow_multiple=false 材料唯一 | 事务内 `SELECT ... WHERE 归属=? AND type_id=? AND is_deleted=0 FOR UPDATE` 后校验有效（非 WITHDRAWN）材料；idempotency_key 唯一索引兜底重复提交 |

要点：所有规则都在**同一事务**内完成"锁定→校验→写入"，依赖 InnoDB 行锁+间隙锁；任何一条不得下沉为唯一索引（历史行共存需求，冻结决策）。

## E. 决议记录与剩余事项

### 已决议（2026-10-20）

| # | 决议 | 内容 |
|---|------|------|
| F1 ✅ | **方案 B 采纳** | ①team_coach 不增加 transfer_id，外键维持 26 条；②team_transfer 仅用于运动员转队，不扩展为教练转队单据；③教练加入/离队时间、原因、状态由 team_coach 记录，操作人/时间/行为经 audit_log 追溯（business_ref 关联跨队链路）；④V1.2"TeamCoach 与 TeamMember 同构"勘误：仅指结构形式，不代表必须拥有 transfer_id；⑤不新增字段、不新增外键、不改已确认业务规则 |
| F5 ✅ | **删除冗余索引** | material_file_version.idx_mfv_material 已删除（核实：字段顺序为 uk_mfv_version 严格前缀、四种查询场景全被 UK 覆盖、无独立性能/业务用途）；material_audit_log.idx_mal_material **保留**（两表索引不混淆） |

### 剩余事项（均不阻塞 DDL 冻结）

| # | 事项 | 阶段 |
|---|------|------|
| F2 | 学校管理员角色是否存在 | 应用层（Model/Service） |
| F3 | PDF 是否必须完整身份证号 | API 阶段（表已建） |
| F4 | 球队审核/Q6 反向转队/Q7 合并不撤销 签字 | 流程确认 |
| F6 | 部署层交付：①6 张 append-only 表对业务账号收回 UPDATE/DELETE；②各成员独立 DB 账号仅授权本方表 | 部署脚本 |
| F7 | 实现期 TODO：多态逻辑引用定期一致性校验（A3）、字典 code 校验（A4） | Model/Service |

## F. 最终结论

**【DDL 可正式冻结：是】**

- 自检十项全部 ✅（无 ⚠️ 遗留）；SQL 与 V1.2/V1.3 文档三方一致；
- 22 表、28 外键（A7 勘误后含 PMR×2）、10 单列 UNIQUE + 3 联合 UNIQUE、索引清单（含 F5 删减）全部对齐；
- 唯一阻塞项 F1 已决议（方案 B），无新增阻塞项。

下一步（经你下令后）：进入 **Django Models 阶段**，以冻结版 DDL 为唯一依据落地 Models；本阶段不生成、不执行任何数据库迁移。

---

## 修订记录

### V1.4 → V1.4-r1（2026-10-20，F1/F5 决议落地）

| 位置 | 修改前 | 修改后 |
|------|--------|--------|
| SQL 文件头 #6 | "待确认项：team_coach 的 transfer_id…确认后补 ALTER" | "F1 已决议（方案B）：不设 transfer_id，外键维持 26 条；教练调岗经关系行+audit_log 追溯；同构勘误" |
| SQL team_coach 前注释块 | "⚠️ 待确认项 F1…" | "F1 已决议…教练调岗历史记录方式" |
| SQL team_coach.source 注释 | "来源(当前仅DIRECT,见F1待确认)" | "来源(仅DIRECT;教练调岗经audit_log留痕,F1方案B)" |
| SQL team_transfer 表注释 | "转队单据(登记制…)" | "转队单据(**仅运动员**,F1方案B;登记制…)" |
| SQL material_file_version | 含 `KEY idx_mfv_material` + 尾注 | **删除该索引**；表注释加"F5已删冗余索引" |
| SQL 文件尾自检摘要 | "待确认 F1" | "F1 已决议 / F5 已执行 / 冻结候选" |
| 报告 A#8 | ⚠️ 待裁定 | ✅ F5 已裁定删除 |
| 报告 A#10 | ⚠️ F1 冲突 | ✅ F1 已决议（方案B） |
| 报告 B 表 | team_coach"待确认"、material_file_version 索引"1⚠" | "已决议"、"0(仅UK)" |
| 报告 E/F | 待确认清单 | 决议记录 + 剩余事项；结论改为"可正式冻结" |
| V1.3 | A 节新增 A6 勘误条目；五节索引清单同步 F5 | （另见 V1.3 修订） |
| **V1.4-r1 追加（A7）** | 外键总数勘误 26→28：V1.3 四节清单补录 #27/#28（PMR.primary/merged_person→Person.PROTECT），报告 A#2/#3/#6/#10、B/C/F 节、SQL 尾注同步；**表结构零变更**（SQL 一直是 28 条，错的是清单） | （V1.3 新增 A7 条目） |
