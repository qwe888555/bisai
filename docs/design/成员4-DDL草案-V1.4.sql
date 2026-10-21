-- ============================================================================
-- 成员4数据库建表 DDL 草案 V1.4-r1（F1/F5 已决议，冻结候选，禁止直接执行）
-- 依据：《成员4数据库设计 V1.2 冻结候选版》《成员4-ER图与结构审核-V1.3.md》
-- 目标库：MySQL 8.0 / InnoDB / utf8mb4 / utf8mb4_0900_ai_ci / DATETIME(6)
--
-- ⚠️ 使用限制：
--   1. 本文件仅为设计草案，未经项目组审核确认前不得在任何环境执行。
--   2. PROTECT 统一实现为 ON DELETE RESTRICT ON UPDATE RESTRICT（InnoDB 即时约束）。
--   3. 以下 6 条"现役唯一"规则为【服务层强制】，故意不建数据库唯一索引
--      （历史行需共存，MySQL 无条件唯一索引），实现方式见 DDL 审核报告 D 节：
--      R1 (school,category_code) 现役球队唯一      R2 person 全局现役唯一
--      R3 (team,jersey_number) 现役球衣号唯一      R4 (team,HEAD_COACH) 现役主教练唯一
--      R5 person 最多一条 PENDING 转队单           R6 allow_multiple=false 时 (归属,type) 有效材料唯一
--   4. 逻辑引用（无外键，应用层校验）：
--      字典：category_code/position_code/coach_level/guardian_relation/school_type 等 → dict_item(type,value)
--      文件：*_file_id → 文件服务成员；用户：created_by/updated_by/audited_by 等 → 认证底座 user
--   5. append-only 表（行内已注释）：应用层禁 update/delete；
--      部署层建议对业务账号收回这 6 张表的 UPDATE/DELETE 权限。
--   6. F1 已决议（方案B，2026-10-20）：team_coach 不设 transfer_id，外键维持 26 条；
--      team_transfer 仅用于运动员转队，不扩展为教练转队单据；
--      教练调岗历史由 team_coach 关系行（joined/left/原因/状态）+ audit_log（操作人/时间/行为）追溯。
--      V1.2"同构"勘误：仅指结构形式，不代表必须拥有 transfer_id。
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 第 1 组：基础数据（被引用优先）
-- ----------------------------------------------------------------------------

CREATE TABLE base_region (
  id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  name        VARCHAR(50)  NOT NULL COMMENT '区划名',
  code        VARCHAR(12)  NOT NULL COMMENT '国标区划码',
  level       SMALLINT     NOT NULL COMMENT '1省/2市州/3区县',
  parent_id   BIGINT       NULL COMMENT '上级区划（省级为NULL）',
  sort_order  INT          NOT NULL DEFAULT 0 COMMENT '排序',
  is_active   TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '是否启用',
  created_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  created_by  BIGINT       NULL COMMENT '创建人(底座用户ID,逻辑引用)',
  updated_by  BIGINT       NULL COMMENT '更新人(逻辑引用)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_region_code (code),
  KEY idx_region_level (level),
  KEY idx_region_parent (parent_id, is_active),
  CONSTRAINT fk_region_parent FOREIGN KEY (parent_id) REFERENCES base_region (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='行政区划(基础数据,只停用不删)';

CREATE TABLE base_school (
  id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  name        VARCHAR(100) NOT NULL COMMENT '校名',
  code        VARCHAR(20)  NULL COMMENT '学校标识码(可空唯一,多个NULL合法)',
  school_type VARCHAR(20)  NOT NULL COMMENT 'PRIMARY/JUNIOR/SENIOR/NINE_YEAR/COMPLETE/MIDDLE_VOC(字典逻辑引用)',
  region_id   BIGINT       NOT NULL COMMENT '所属区县',
  address     VARCHAR(200) NULL COMMENT '地址',
  is_active   TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '是否启用',
  created_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  created_by  BIGINT       NULL COMMENT '创建人(逻辑引用)',
  updated_by  BIGINT       NULL COMMENT '更新人(逻辑引用)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_school_code (code),
  UNIQUE KEY uk_school_name_region (name, region_id),
  KEY idx_school_name (name),
  KEY idx_school_region (region_id),
  CONSTRAINT fk_school_region FOREIGN KEY (region_id) REFERENCES base_region (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='学校(基础数据)';

CREATE TABLE base_dict_type (
  id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  code        VARCHAR(50)  NOT NULL COMMENT '字典类型编码 如team_category/position',
  name        VARCHAR(100) NOT NULL COMMENT '类型名称',
  remark      VARCHAR(200) NULL DEFAULT '' COMMENT '备注',
  created_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  created_by  BIGINT       NULL COMMENT '创建人(逻辑引用)',
  updated_by  BIGINT       NULL COMMENT '更新人(逻辑引用)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_dict_type_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='字典类型(基础数据)';

CREATE TABLE base_dict_item (
  id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  type_id     BIGINT       NOT NULL COMMENT '所属字典类型',
  value       VARCHAR(50)  NOT NULL COMMENT '稳定编码,含义禁改',
  label       VARCHAR(100) NOT NULL COMMENT '展示名,可改',
  sort_order  INT          NOT NULL DEFAULT 0 COMMENT '排序',
  is_active   TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '只停用不删',
  ext_json    JSON         NULL COMMENT '结构化扩展(组别属性,等成员5清单)',
  created_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  created_by  BIGINT       NULL COMMENT '创建人(逻辑引用)',
  updated_by  BIGINT       NULL COMMENT '更新人(逻辑引用)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_dict_item_value (type_id, value),
  KEY idx_dict_item_active (type_id, is_active),
  CONSTRAINT fk_dict_item_type FOREIGN KEY (type_id) REFERENCES base_dict_type (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='字典项(基础数据)';

CREATE TABLE material_type (
  id             BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  code           VARCHAR(50)  NOT NULL COMMENT '材料类型编码',
  name           VARCHAR(100) NOT NULL COMMENT '类型名称',
  applies_to     VARCHAR(20)  NOT NULL COMMENT 'PERSON/TEAM',
  is_required    TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '是否必备',
  need_audit     TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '是否需审核',
  allow_multiple TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '是否允许多份(R6服务层规则依据)',
  allowed_ext    VARCHAR(200) NOT NULL DEFAULT 'jpg,jpeg,png,pdf' COMMENT '允许扩展名',
  max_size_mb    SMALLINT     NOT NULL DEFAULT 10 COMMENT '单文件上限MB',
  sort_order     INT          NOT NULL DEFAULT 0 COMMENT '排序',
  is_active      TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '只停用不删',
  remark         VARCHAR(200) NULL DEFAULT '' COMMENT '备注',
  created_at     DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at     DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  created_by     BIGINT       NULL COMMENT '创建人(逻辑引用)',
  updated_by     BIGINT       NULL COMMENT '更新人(逻辑引用)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_material_type_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='材料类型配置(通用材料引擎,新类型零代码)';

-- ----------------------------------------------------------------------------
-- 第 2 组：导入批次（person/team 依赖其外键）
-- ----------------------------------------------------------------------------

CREATE TABLE import_batch (
  id            BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  batch_no      VARCHAR(50) NOT NULL COMMENT '批次号(导入幂等键,重复提交返回已有批次)',
  import_type   VARCHAR(20) NOT NULL COMMENT 'REGION/SCHOOL/DICT/MATERIAL_TYPE/PERSON/TEAM',
  status        VARCHAR(20) NOT NULL DEFAULT 'PROCESSING' COMMENT 'PROCESSING/COMPLETED/PARTIAL/FAILED',
  total         INT         NOT NULL DEFAULT 0 COMMENT '总行数',
  success_count INT         NOT NULL DEFAULT 0 COMMENT '成功行数',
  failed_count  INT         NOT NULL DEFAULT 0 COMMENT '失败行数',
  source_file_id VARCHAR(64) NULL COMMENT '源文件(逻辑引用文件服务)',
  created_by    BIGINT      NULL COMMENT '操作人(逻辑引用)',
  finished_at   DATETIME(6) NULL COMMENT '完成时间',
  created_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_import_batch_no (batch_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='批量导入批次';

-- ----------------------------------------------------------------------------
-- 第 3 组：主数据 person / team
-- ----------------------------------------------------------------------------

CREATE TABLE person (
  id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  person_no       VARCHAR(20)  NOT NULL COMMENT '档案号 P+年份+8位id(应用层回填)',
  full_name       VARCHAR(50)  NOT NULL COMMENT '姓名',
  gender          TINYINT      NOT NULL DEFAULT 0 COMMENT '0未知/1男/2女',
  birth_date      DATE         NULL COMMENT '出生日期(身份证解析)',
  id_card_cipher  VARCHAR(256) NOT NULL COMMENT '身份证号AES密文',
  id_card_hash    VARCHAR(64)  NOT NULL COMMENT '身份证号SHA256+盐,全局唯一(含软删行→复用不重建)',
  id_card_masked  VARCHAR(18)  NOT NULL COMMENT '身份证掩码',
  phone_cipher    VARCHAR(256) NULL COMMENT '手机号AES密文',
  phone_hash      VARCHAR(64)  NULL COMMENT '手机号哈希(精确查重,可空非唯一)',
  phone_masked    VARCHAR(11)  NULL COMMENT '手机号掩码',
  person_category VARCHAR(20)  NOT NULL COMMENT 'ATHLETE/COACH(一人一身份,#3)',
  audit_status    VARCHAR(20)  NOT NULL DEFAULT 'DRAFT' COMMENT 'DRAFT/PENDING/APPROVED/REJECTED',
  status          VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE/DISABLED',
  region_id       BIGINT       NULL COMMENT '所属市州区划(创建时业务必填,服务层)',
  school_id       BIGINT       NULL COMMENT '所属学校(ATHLETE创建时业务必填,服务层)',
  photo_file_id   VARCHAR(64)  NULL COMMENT '照片(逻辑引用文件服务)',
  import_batch_id BIGINT       NULL COMMENT '导入批次(追溯)',
  remark          TEXT         NULL COMMENT '备注',
  created_by      BIGINT       NULL COMMENT '创建人(逻辑引用)',
  updated_by      BIGINT       NULL COMMENT '更新人(逻辑引用)',
  is_deleted      TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '软删标记',
  deleted_at      DATETIME(6)  NULL COMMENT '软删时间',
  created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_person_no (person_no),
  UNIQUE KEY uk_person_id_card_hash (id_card_hash),
  KEY idx_person_name (full_name),
  KEY idx_person_cat_audit (person_category, audit_status),
  KEY idx_person_status_del (status, is_deleted),
  KEY idx_person_region (region_id),
  KEY idx_person_school (school_id),
  KEY idx_person_phone_hash (phone_hash),
  KEY idx_person_import (import_batch_id),
  CONSTRAINT fk_person_region FOREIGN KEY (region_id) REFERENCES base_region (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_person_school FOREIGN KEY (school_id) REFERENCES base_school (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_person_import FOREIGN KEY (import_batch_id) REFERENCES import_batch (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人员主档(唯一主表,软删,敏感字段加密)';

CREATE TABLE team (
  id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  team_no         VARCHAR(20)  NOT NULL COMMENT '球队编号 T+年份+8位id(应用层回填)',
  name            VARCHAR(100) NOT NULL COMMENT '球队名',
  category_code   VARCHAR(20)  NOT NULL COMMENT '组别(字典team_category逻辑引用)',
  region_id       BIGINT       NULL COMMENT '所属市州(逻辑隔离维度)',
  school_id       BIGINT       NULL COMMENT '所属学校(创建时业务必填,服务层)',
  logo_file_id    VARCHAR(64)  NULL COMMENT '队徽(逻辑引用文件服务)',
  flag_file_id    VARCHAR(64)  NULL COMMENT '队旗(逻辑引用)',
  group_photo_file_id VARCHAR(64) NULL COMMENT '合照(逻辑引用)',
  introduction    TEXT         NULL COMMENT '球队简介',
  audit_status    VARCHAR(20)  NOT NULL DEFAULT 'APPROVED' COMMENT '字段保留,审核流程不启用(Q2)',
  status          VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE/DISABLED',
  import_batch_id BIGINT       NULL COMMENT '导入批次(追溯)',
  created_by      BIGINT       NULL COMMENT '创建人(逻辑引用)',
  updated_by      BIGINT       NULL COMMENT '更新人(逻辑引用)',
  is_deleted      TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '软删标记',
  deleted_at      DATETIME(6)  NULL COMMENT '软删时间',
  created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_team_no (team_no),
  KEY idx_team_name (name),
  KEY idx_team_region_cat (region_id, category_code),
  KEY idx_team_school (school_id),
  KEY idx_team_status_del (status, is_deleted),
  KEY idx_team_import (import_batch_id),
  CONSTRAINT fk_team_region FOREIGN KEY (region_id) REFERENCES base_region (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_team_school FOREIGN KEY (school_id) REFERENCES base_school (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_team_import FOREIGN KEY (import_batch_id) REFERENCES import_batch (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='球队主表(唯一主表,软删)';

-- ----------------------------------------------------------------------------
-- 第 4 组：扩展档案 / 变更日志（依赖 person）
-- ----------------------------------------------------------------------------

CREATE TABLE person_athlete_profile (
  person_id             BIGINT       NOT NULL COMMENT '人员ID(OneToOne,同时为主键)',
  grade_level           VARCHAR(20)  NOT NULL COMMENT '学段 PRIMARY_LOW/PRIMARY_HIGH/JUNIOR/SENIOR(字典逻辑引用)',
  student_no            VARCHAR(30)  NULL COMMENT '学籍号',
  position_code         VARCHAR(20)  NULL COMMENT '场上位置(字典逻辑引用)',
  guardian_name         VARCHAR(50)  NULL COMMENT '监护人姓名',
  guardian_relation     VARCHAR(20)  NULL COMMENT '与监护人关系(字典逻辑引用)',
  guardian_phone_cipher VARCHAR(256) NULL COMMENT '监护人手机号密文',
  guardian_phone_hash   VARCHAR(64)  NULL COMMENT '监护人手机号哈希',
  guardian_phone_masked VARCHAR(11)  NULL COMMENT '监护人手机号掩码',
  ext_json              JSON         NULL COMMENT '预留扩展',
  created_at            DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at            DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (person_id),
  KEY idx_athlete_student (student_no),
  CONSTRAINT fk_athlete_person FOREIGN KEY (person_id) REFERENCES person (id)
    ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='运动员扩展档案(随主档,1:1)';

CREATE TABLE person_coach_profile (
  person_id              BIGINT      NOT NULL COMMENT '人员ID(OneToOne,同时为主键)',
  coach_level            VARCHAR(20) NULL COMMENT '教练等级(字典逻辑引用)',
  certificate_no         VARCHAR(50) NULL COMMENT '证书编号',
  certificate_expire_date DATE       NULL COMMENT '证书有效期至',
  ext_json               JSON        NULL COMMENT '预留扩展',
  created_at             DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at             DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (person_id),
  KEY idx_coach_cert (certificate_no),
  CONSTRAINT fk_coach_person FOREIGN KEY (person_id) REFERENCES person (id)
    ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='教练员扩展档案(随主档,1:1)';

CREATE TABLE person_change_log (
  id            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  person_id     BIGINT       NOT NULL COMMENT '人员ID',
  change_type   VARCHAR(20)  NOT NULL COMMENT 'CREATE/UPDATE/AUDIT/MERGE/DISABLE/ENABLE',
  field_changes JSON         NULL COMMENT '字段级diff [{field,old,new}],敏感字段只写掩码',
  changed_by    BIGINT       NULL COMMENT '操作人(逻辑引用)',
  reason        VARCHAR(200) NULL DEFAULT '' COMMENT '变更原因',
  created_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC,append-only实际不更新)',
  PRIMARY KEY (id),
  KEY idx_pcl_person (person_id, created_at),
  CONSTRAINT fk_pcl_person FOREIGN KEY (person_id) REFERENCES person (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人员字段级变更日志【append-only:禁update/delete,部署层收回权限】';

-- ----------------------------------------------------------------------------
-- 第 5 组：球队关系与单据（依赖 person/team）
-- ----------------------------------------------------------------------------

CREATE TABLE team_transfer (
  id            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  person_id     BIGINT       NOT NULL COMMENT '转队人员',
  from_team_id  BIGINT       NOT NULL COMMENT '原球队',
  to_team_id    BIGINT       NOT NULL COMMENT '目标球队',
  transfer_date DATE         NOT NULL COMMENT '转队日期',
  apply_reason  VARCHAR(200) NOT NULL COMMENT '转队原因',
  status        VARCHAR(20)  NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING/COMPLETED/CANCELLED(条件更新流转,R5服务层规则)',
  verified_by   BIGINT       NULL COMMENT '核验人(赛事管理员,逻辑引用)',
  verified_at   DATETIME(6)  NULL COMMENT '核验时间',
  completed_at  DATETIME(6)  NULL COMMENT '完成时间',
  created_by    BIGINT       NULL COMMENT '创建人(逻辑引用)',
  created_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (id),
  KEY idx_tt_person_status (person_id, status),
  KEY idx_tt_from (from_team_id),
  KEY idx_tt_to (to_team_id),
  CONSTRAINT fk_tt_person FOREIGN KEY (person_id) REFERENCES person (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_tt_from FOREIGN KEY (from_team_id) REFERENCES team (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_tt_to FOREIGN KEY (to_team_id) REFERENCES team (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='转队单据(仅运动员,F1方案B;登记制,核验后直办,禁止物理删除,纠错=反向单Q6)';

CREATE TABLE team_member (
  id            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  team_id       BIGINT       NOT NULL COMMENT '球队',
  person_id     BIGINT       NOT NULL COMMENT '球员(服务层校验为ATHLETE,R2现役唯一服务层强制)',
  jersey_number SMALLINT     NULL COMMENT '球衣号(R3队内现役唯一,服务层强制)',
  position_code VARCHAR(20)  NULL COMMENT '场上位置(字典逻辑引用)',
  status        VARCHAR(20)  NOT NULL DEFAULT 'CURRENT' COMMENT 'CURRENT/EXITED(单向流转,历史关系禁改)',
  joined_at     DATE         NOT NULL COMMENT '加入日期',
  left_at       DATETIME(6)  NULL COMMENT '离队时间',
  left_reason   VARCHAR(200) NULL DEFAULT '' COMMENT '离队原因',
  source        VARCHAR(20)  NOT NULL DEFAULT 'DIRECT' COMMENT 'DIRECT/TRANSFER',
  transfer_id   BIGINT       NULL COMMENT '转队单(转队产生时关联)',
  created_by    BIGINT       NULL COMMENT '创建人(逻辑引用)',
  updated_by    BIGINT       NULL COMMENT '更新人(逻辑引用)',
  created_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (id),
  KEY idx_tm_person_status (person_id, status),
  KEY idx_tm_team_status (team_id, status),
  KEY idx_tm_team_jersey (team_id, jersey_number),
  KEY idx_tm_transfer (transfer_id),
  CONSTRAINT fk_tm_team FOREIGN KEY (team_id) REFERENCES team (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_tm_person FOREIGN KEY (person_id) REFERENCES person (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_tm_transfer FOREIGN KEY (transfer_id) REFERENCES team_transfer (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='球员-球队关系(含历史,N:M中间表,禁止物理删除)';

-- F1 已决议（方案B）：team_coach 不含 transfer_id；team_transfer 仅用于运动员转队（不扩展为教练转队单据）。
-- 教练调岗历史记录：joined_at/left_at/left_reason/status（本表）+ 操作人/时间/行为（audit_log，business_ref 关联跨队链路）。

CREATE TABLE team_coach (
  id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  team_id     BIGINT       NOT NULL COMMENT '球队',
  person_id   BIGINT       NOT NULL COMMENT '教练(服务层校验为COACH)',
  duty        VARCHAR(20)  NOT NULL COMMENT 'HEAD_COACH/ASSISTANT_COACH/GOALKEEPER_COACH(R4现役主教练唯一,服务层强制)',
  status      VARCHAR(20)  NOT NULL DEFAULT 'CURRENT' COMMENT 'CURRENT/EXITED(单向流转,历史禁改)',
  joined_at   DATE         NOT NULL COMMENT '加入日期',
  left_at     DATETIME(6)  NULL COMMENT '离队时间',
  left_reason VARCHAR(200) NULL DEFAULT '' COMMENT '离队原因',
  source        VARCHAR(20)  NOT NULL DEFAULT 'DIRECT' COMMENT '来源(仅DIRECT;教练调岗经audit_log留痕,F1方案B)',
  created_by  BIGINT       NULL COMMENT '创建人(逻辑引用)',
  updated_by  BIGINT       NULL COMMENT '更新人(逻辑引用)',
  created_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (id),
  KEY idx_tc_person_status (person_id, status),
  KEY idx_tc_team_status (team_id, status),
  CONSTRAINT fk_tc_team FOREIGN KEY (team_id) REFERENCES team (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_tc_person FOREIGN KEY (person_id) REFERENCES person (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='教练-球队关系(含历史,N:M中间表,禁止物理删除)';

CREATE TABLE person_merge_record (
  id                BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  primary_person_id BIGINT       NOT NULL COMMENT '保留档人员',
  merged_person_id  BIGINT       NOT NULL COMMENT '被合并档人员(生效后软删)',
  merged_by         BIGINT       NULL COMMENT '合并操作人(逻辑引用)',
  reason            VARCHAR(200) NOT NULL COMMENT '合并原因',
  confirm_status    VARCHAR(20)  NOT NULL DEFAULT 'PENDING_CONFIRM' COMMENT 'PENDING_CONFIRM/CONFIRMED/CANCELLED(映射接口仅CONFIRMED后生效)',
  confirmed_by      BIGINT       NULL COMMENT '签字确认人(队伍/核心成员)',
  confirmed_at      DATETIME(6)  NULL COMMENT '确认时间',
  confirm_file_id   VARCHAR(64)  NULL COMMENT '签字凭证(逻辑引用文件服务)',
  created_at        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (id),
  KEY idx_pmr_primary (primary_person_id),
  KEY idx_pmr_merged (merged_person_id),
  CONSTRAINT fk_pmr_primary FOREIGN KEY (primary_person_id) REFERENCES person (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_pmr_merged FOREIGN KEY (merged_person_id) REFERENCES person (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人员合并记录(保留原ID+映射,生效后不可自动撤销Q7,禁止物理删除)';

-- ----------------------------------------------------------------------------
-- 第 6 组：材料实例（依赖 person/team/material_type）
-- ----------------------------------------------------------------------------

CREATE TABLE person_material (
  id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  person_id       BIGINT       NOT NULL COMMENT '所属人员(归属禁改)',
  type_id         BIGINT       NOT NULL COMMENT '材料类型(归属禁改)',
  file_id         VARCHAR(64)  NOT NULL COMMENT '文件(逻辑引用文件服务,不存URL)',
  file_name       VARCHAR(200) NOT NULL COMMENT '文件名',
  file_size       INT          NULL COMMENT '文件大小字节',
  file_ext        VARCHAR(10)  NULL COMMENT '扩展名',
  status          VARCHAR(20)  NOT NULL DEFAULT 'DRAFT' COMMENT 'DRAFT/PENDING/APPROVED/REJECTED/WITHDRAWN',
  audit_remark    VARCHAR(200) NULL DEFAULT '' COMMENT '审核意见',
  audited_by      BIGINT       NULL COMMENT '审核人(逻辑引用)',
  audited_at      DATETIME(6)  NULL COMMENT '审核时间',
  uploaded_by     BIGINT       NULL COMMENT '上传人(逻辑引用)',
  updated_by      BIGINT       NULL COMMENT '更新人(逻辑引用)',
  idempotency_key VARCHAR(64)  NULL COMMENT '上传幂等键(可空唯一,重复提交返回已有记录)',
  remark          VARCHAR(200) NULL DEFAULT '' COMMENT '备注',
  is_deleted      TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '软删标记',
  deleted_at      DATETIME(6)  NULL COMMENT '软删时间',
  created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_pm_idempotency (idempotency_key),
  KEY idx_pm_person_type_status (person_id, type_id, status),
  KEY idx_pm_status_del (status, is_deleted),
  KEY idx_pm_type (type_id),
  CONSTRAINT fk_pm_person FOREIGN KEY (person_id) REFERENCES person (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_pm_type FOREIGN KEY (type_id) REFERENCES material_type (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人员材料(R6唯一性服务层强制,替换留版本表,软删)';

CREATE TABLE team_material (
  id              BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  team_id         BIGINT       NOT NULL COMMENT '所属球队(归属禁改)',
  type_id         BIGINT       NOT NULL COMMENT '材料类型(归属禁改)',
  file_id         VARCHAR(64)  NOT NULL COMMENT '文件(逻辑引用文件服务)',
  file_name       VARCHAR(200) NOT NULL COMMENT '文件名',
  file_size       INT          NULL COMMENT '文件大小字节',
  file_ext        VARCHAR(10)  NULL COMMENT '扩展名',
  status          VARCHAR(20)  NOT NULL DEFAULT 'DRAFT' COMMENT 'DRAFT/PENDING/APPROVED/REJECTED/WITHDRAWN',
  audit_remark    VARCHAR(200) NULL DEFAULT '' COMMENT '审核意见',
  audited_by      BIGINT       NULL COMMENT '审核人(逻辑引用)',
  audited_at      DATETIME(6)  NULL COMMENT '审核时间',
  uploaded_by     BIGINT       NULL COMMENT '上传人(逻辑引用)',
  updated_by      BIGINT       NULL COMMENT '更新人(逻辑引用)',
  idempotency_key VARCHAR(64)  NULL COMMENT '上传幂等键(可空唯一)',
  remark          VARCHAR(200) NULL DEFAULT '' COMMENT '备注',
  is_deleted      TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '软删标记',
  deleted_at      DATETIME(6)  NULL COMMENT '软删时间',
  created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_tm_idempotency (idempotency_key),
  KEY idx_tm2_team_type_status (team_id, type_id, status),
  KEY idx_tm2_status_del (status, is_deleted),
  KEY idx_tm2_type (type_id),
  CONSTRAINT fk_tmat_team FOREIGN KEY (team_id) REFERENCES team (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT fk_tmat_type FOREIGN KEY (type_id) REFERENCES material_type (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='球队材料(R6唯一性服务层强制,软删)';

-- ----------------------------------------------------------------------------
-- 第 7 组：材料审计与版本（多态逻辑引用,无外键,append-only）
-- ----------------------------------------------------------------------------

CREATE TABLE material_audit_log (
  id            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  material_kind VARCHAR(20)  NOT NULL COMMENT 'PERSON/TEAM(多态逻辑引用,无FK)',
  material_id   BIGINT       NOT NULL COMMENT '材料ID(指向person_material或team_material)',
  from_status   VARCHAR(20)  NOT NULL COMMENT '原状态',
  to_status     VARCHAR(20)  NOT NULL COMMENT '新状态',
  auditor       BIGINT       NULL COMMENT '审核人(逻辑引用)',
  comment       VARCHAR(200) NULL DEFAULT '' COMMENT '审核意见',
  created_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC,append-only实际不更新)',
  PRIMARY KEY (id),
  KEY idx_mal_material (material_kind, material_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='材料审核历史【append-only,引用完整性由应用层+定期校验任务保证】';

CREATE TABLE material_file_version (
  id            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  material_kind VARCHAR(20)  NOT NULL COMMENT 'PERSON/TEAM(多态逻辑引用,无FK)',
  material_id   BIGINT       NOT NULL COMMENT '材料ID',
  version       INT          NOT NULL DEFAULT 1 COMMENT '版本号,替换+1',
  file_id       VARCHAR(64)  NOT NULL COMMENT '该版本文件(旧文件留痕,逻辑引用文件服务)',
  file_name     VARCHAR(200) NOT NULL COMMENT '文件名',
  uploaded_by   BIGINT       NULL COMMENT '上传人(逻辑引用)',
  created_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC,append-only实际不更新)',
  PRIMARY KEY (id),
  UNIQUE KEY uk_mfv_version (material_kind, material_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='材料文件版本(替换留痕)【append-only;F5已删冗余索引idx_mfv_material,检索走uk_mfv_version最左前缀】';

-- ----------------------------------------------------------------------------
-- 第 8 组：敏感访问 / 通用审计（append-only）
-- ----------------------------------------------------------------------------

CREATE TABLE sensitive_access_log (
  id              BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  accessed_by     BIGINT      NOT NULL COMMENT '访问人(底座用户ID,逻辑引用)',
  target_person_id BIGINT     NOT NULL COMMENT '被查看人员',
  scene           VARCHAR(50) NOT NULL COMMENT 'IDCARD_VIEW/PDF_EXPORT/VERIFICATION',
  business_ref    VARCHAR(50) NULL COMMENT '业务依据(如报名单号)',
  ip_address      VARCHAR(39) NULL COMMENT '来源IP(IPv6兼容)',
  created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC,append-only实际不更新)',
  PRIMARY KEY (id),
  KEY idx_sal_person (target_person_id, created_at),
  KEY idx_sal_actor (accessed_by, created_at),
  CONSTRAINT fk_sal_person FOREIGN KEY (target_person_id) REFERENCES person (id)
    ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='明文身份证访问审计【append-only,单笔限流,响应不落日志】';

CREATE TABLE audit_log (
  id            BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  actor_user_id BIGINT      NULL COMMENT '操作人(底座用户ID,逻辑引用,无FK-设计取舍A5)',
  action        VARCHAR(50) NOT NULL COMMENT 'TEAM_CREATE/MEMBER_EXIT/TRANSFER_VERIFY/IMPORT/EXPORT/DISABLE等',
  target_type   VARCHAR(50) NOT NULL COMMENT '目标对象类型(如team/team_member,逻辑引用)',
  target_id     BIGINT      NULL COMMENT '目标对象ID',
  detail_json   JSON        NULL COMMENT '动作摘要(敏感字段只写掩码)',
  ip_address    VARCHAR(39) NULL COMMENT '来源IP',
  created_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC,append-only实际不更新)',
  PRIMARY KEY (id),
  KEY idx_audit_target (target_type, target_id),
  KEY idx_audit_actor (actor_user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='通用操作审计【append-only,无外键为定稿取舍;部署层收回业务账号UPDATE/DELETE权限】';

-- ----------------------------------------------------------------------------
-- 第 9 组：导入失败明细（依赖 import_batch）
-- ----------------------------------------------------------------------------

CREATE TABLE import_row_error (
  id            BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  batch_id      BIGINT       NOT NULL COMMENT '所属批次',
  row_no        INT          NOT NULL COMMENT '源文件行号',
  raw_data      JSON         NULL COMMENT '原始数据(敏感字段只写掩码)',
  error_code    VARCHAR(50)  NOT NULL COMMENT '错误码',
  error_message VARCHAR(200) NULL DEFAULT '' COMMENT '错误信息',
  created_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间(UTC)',
  updated_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间(UTC,append-only实际不更新)',
  PRIMARY KEY (id),
  KEY idx_ire_batch (batch_id),
  CONSTRAINT fk_ire_batch FOREIGN KEY (batch_id) REFERENCES import_batch (id)
    ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='导入失败行明细【append-only,明细随批次CASCADE】';

-- ============================================================================
-- 自检摘要（详见《成员4-DDL审核报告-V1.4.md》）：
-- 表 22 张 | 外键 28 条(PROTECT→RESTRICT 25,CASCADE 3,SET_NULL 0;A7勘误:含PMR×2补录)
-- 单列UNIQUE 10 / 联合UNIQUE 3 / 服务层规则 6 条(未建索引,注释标注)
-- F1 已决议(方案B,team_coach 无 transfer_id) / F5 已执行(删 idx_mfv_material)
-- 状态：DDL 冻结候选,待项目组终审后进入 Django Models 阶段
-- ============================================================================
