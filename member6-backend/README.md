# 省级中小学足球系统 · 成员6 Day4–D6 后端

本目录是 **成员6 T6-03「遴选评分」（模块 6.2，D4–D6）和 T6-04「公示」（模块 7.3，D6）** 的 Django 实现，代码按“可独立运行、可并入主程序”的方式组织。

## 对应需求

### T6-03 遴选评分

> 评分模板（初选/复选用“轮次”参数）、评分录入与修改、汇总（固定加权公式）、导出 Excel；按高校/中学/小学组生成最佳球员遴选结果并可追溯评分明细；规则以 D1 确认结果为准。

原始 Excel 分解出的 12 个功能点，本模块全部覆盖：

| 原功能点 | 实现位置 |
|---|---|
| 初选评分录入 | `POST /api/selection/scores/`，`round=INITIAL` |
| 初选评分修改 | `PUT /api/selection/scores/<id>/` |
| 初选评分汇总 | `GET /api/selection/scores/summary/` |
| 初选评分导出 | `GET /api/selection/scores/export/` |
| 复选评分录入 | `POST /api/selection/scores/`，`round=FINAL` |
| 复选评分修改 | `PUT /api/selection/scores/<id>/` |
| 复选评分汇总 | `GET /api/selection/scores/summary/` |
| 复选评分导出 | `GET /api/selection/scores/export/` |
| 生成最佳球员结果 | `POST /api/selection/results/generate/` |
| 高校组结果 | 生成时传 `group_type=HIGHER` |
| 中学组结果 | 生成时传 `group_type=MIDDLE` |
| 小学组结果 | 生成时传 `group_type=PRIMARY` |

### T6-04 公示

> 运动员/教练员/球队报名信息公示与取消、按组别/市州查询；身份证脱敏；字段白名单。

| 原功能点 | 实现位置 |
|---|---|
| 运动员报名信息公示 | `POST /api/selection/publicity/`，`publicity_type=ATHLETE` |
| 教练员报名信息公示 | `POST /api/selection/publicity/`，`publicity_type=COACH` |
| 球队报名信息公示 | `POST /api/selection/publicity/`，`publicity_type=TEAM` |
| 取消公示 | `POST /api/selection/publicity/<id>/cancel/` |
| 按组别查询 | 公示列表传 `group_type=HIGHER/MIDDLE/PRIMARY` |
| 按市州查询 | 公示列表传 `city=江城市` 等，或先调 `publicity/cities/` |
| 身份证脱敏 | 服务层写入前统一转为 `id_card_masked`，原文不落库 |
| 字段白名单 | `SELECTION_PUBLICITY_FIELD_WHITELIST`，按运动员/教练员/球队分别配置 |
| 取消后门户立即不可见 | 所有公开查询强制 `status=PUBLISHED`；取消记录只留在管理列表 |

---

## 1. 技术栈

- Python > 3.10.9（本机验证：Python 3.12.7）
- **Django 5.2 LTS**（本机验证：Django 5.2.18）
- **django-ninja >=1.6,<2.0**（与主程序 `backend/requirements.txt` 保持一致；本机验证：1.7.1）
- SQLite（默认，零配置演示）/ MySQL 8（正式环境）
- openpyxl 3.1（Excel 导出）
- HTTP 层使用 `NinjaAPI` + `Router`，**不依赖 DRF**；业务逻辑集中在 `selection/services/`，主程序即使使用 DRF 也可以直接复用

---

## 2. 目录结构

```
football-systerm/
├── manage.py
├── requirements.txt              # Django 5.2 + django-ninja >=1.6,<2.0 + openpyxl
├── requirements-mysql.txt        # 追加 PyMySQL
├── config/
│   ├── settings.py               # 数据库、时区、SELECTION_* 集成配置
│   ├── urls.py                   # /api/selection/ 入口
│   ├── wsgi.py
│   └── asgi.py
├── selection/                    # 成员6的遴选评分 + 公示 App
│   ├── models.py                 # 7 张表（6 张遴选表 + 1 张公示表）
│   ├── api.py                    # NinjaAPI + Router，统一响应与异常处理
│   ├── views.py                  # 兼容层，转发到 api.py
│   ├── urls.py                   # 挂载 api.urls
│   ├── admin.py                  # 后台查看/录入
│   ├── common.py                 # 统一响应、参数校验、异常处理
│   ├── enums.py                  # 组别/轮次/公示类型/公示状态及别名
│   ├── services/
│   │   ├── scoring.py            # 固定加权公式
│   │   ├── templates.py          # 评分模板
│   │   ├── scores.py             # 评分录入/修改/汇总
│   │   ├── results.py            # 最佳球员结果生成
│   │   ├── excel.py              # Excel 导出
│   │   ├── archive.py            # 成员4球员档案适配器（含演示实现）
│   │   ├── publicity.py          # 公示、取消、白名单、脱敏、查询
│   │   ├── publicity_source.py   # 成员4报名档案适配器（含演示实现）
│   │   └── current_user.py       # 成员2登录用户适配器
│   ├── management/commands/
│   │   └── seed_demo.py          # 一键演示数据
│   ├── migrations/
│   └── tests/                    # 30 个自动化测试
└── docs/
    ├── API.md                    # 正式接口文档（参数、返回字段、错误码）
    └── selection-api.http        # 可直接在 IDE 里执行的接口示例
```

---

## 3. 快速启动（独立演示）

在 `D:\桌面\football-systerm` 下执行：

```bash
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

浏览器或接口工具访问：

```
http://127.0.0.1:8000/api/selection/docs          # Swagger UI（自动生成）
http://127.0.0.1:8000/api/selection/openapi.json  # OpenAPI 3 描述
```

`seed_demo` 会生成：

- 3 个组别 × 2 个轮次 = **6 套评分模板**
  - 维度：技术能力 30、比赛表现 30、身体素质 20、战术意识 20，满分均为 100
- 3 个组别 × 15 名球员 × 2 个轮次 = **90 条评分**
- 3 个组别各 1 个复选结果批次，默认名额 11
- 3 个组别 ×（3 名运动员 + 1 名教练员 + 1 支球队）= **15 条公示记录**

演示报名档案 ID：

| 类型 | 高校组 | 中学组 | 小学组 |
|---|---|---|---|
| 运动员 ATHLETE | 10001–10015 | 20001–20015 | 30001–30015 |
| 教练员 COACH | C1001–C1005 | C2001–C2005 | C3001–C3005 |
| 球队 TEAM | T1001–T1003 | T2001–T2003 | T3001–T3003 |

演示市州为 `江城市`、`南湖市`、`东林市`。所有演示评分和公示记录属于 `event_id=1`。

重置演示数据：

```bash
python manage.py seed_demo --reset
```

只生成模板、不生成评分、结果和公示（想用接口手动体验“录入评分”时用）：

```bash
python manage.py seed_demo --reset --templates-only
```

后台管理（可选）：

```bash
python manage.py createsuperuser
# 然后访问 http://127.0.0.1:8000/admin/
```

---

## 4. API 约定

### 4.1 框架与自动文档

- 路由由 `selection/api.py` 中的 `NinjaAPI` + `Router` 提供，`selection/urls.py` 只负责挂载 `api.urls`。
- 每个路径仍然带结尾斜杠，完整前缀为 `/api/selection/`。
- django-ninja 会自动生成 OpenAPI 文档：
  - Swagger UI：`/api/selection/docs`
  - OpenAPI JSON：`/api/selection/openapi.json`
- 业务异常、参数校验异常和未捕获异常都通过 Ninja 的 `exception_handler` 转成下面的统一响应，HTTP 状态码与 `code` 一致。
- Excel 导出接口直接返回 `HttpResponse`，Ninja 会原样透传文件流。

### 4.2 统一响应

成功：

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```

失败时 `code` 与 HTTP 状态码一致，例如 400、404、409、500：

```json
{
  "code": 400,
  "message": "各维度权重之和必须为100，当前为90.00",
  "data": null
}
```

### 4.3 参数命名

- 请求体同时接受 `snake_case` 和 `camelCase`：
  - `group_type` / `groupType`
  - `template_id` / `templateId`
  - `candidate_id` / `candidateId`
  - `result_round` / `resultRound`
  - `initial_weight` / `initialWeight`
- 响应统一使用 `snake_case`。
- 所有 URL 以 `/` 结尾（项目关闭了 `APPEND_SLASH`，避免 POST 被重定向后丢失请求体）。
- 组别和轮次同时接受英文代码和中文：
  - `HIGHER` / `高校组` / `高校` / `大学组`
  - `MIDDLE` / `中学组` / `中学` / `初中组` / `高中组`
  - `PRIMARY` / `小学组` / `小学`
  - `INITIAL` / `初选` / `初赛` / `初轮`
  - `FINAL` / `复选` / `复赛` / `决赛` / `终选`

### 4.4 接口清单

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/selection/templates/` | 模板列表，可选 `group_type`、`round`、`status` |
| POST | `/api/selection/templates/` | 新建模板，自动停用同组别同轮次的旧模板 |
| GET | `/api/selection/templates/<id>/` | 模板详情（含维度） |
| PUT | `/api/selection/templates/<id>/` | 修改模板名称/维度，不能改组别和轮次 |
| GET | `/api/selection/scores/` | 分页查询评分，必填 `event_id`、`group_type`、`round`；可选 `keyword`、`page`、`size` |
| POST | `/api/selection/scores/` | 录入评分 |
| GET | `/api/selection/scores/<id>/` | 评分详情（含各维度得分） |
| PUT | `/api/selection/scores/<id>/` | 修改评分（只改维度得分） |
| GET | `/api/selection/scores/summary/` | 本轮汇总排名，必填 `event_id`、`group_type`、`round` |
| GET | `/api/selection/scores/export/` | 导出本轮评分 Excel，参数同上 |
| POST | `/api/selection/results/generate/` | 生成最佳球员结果（新版本） |
| GET | `/api/selection/results/latest/` | 当前有效结果，必填 `event_id`、`group_type` |
| GET | `/api/selection/results/batches/` | 结果历史批次，可选 `limit`（默认 20，最大 100） |
| GET | `/api/selection/results/export/` | 导出当前结果 Excel；结果过期时返回 409 |
| GET | `/api/selection/publicity/` | 门户公示列表，只返回已公示记录；必填 `event_id`，可选 `group_type`、`city`、`publicity_type`、`keyword`、`page`、`size` |
| POST | `/api/selection/publicity/` | 公示一条运动员/教练员/球队报名信息 |
| GET | `/api/selection/publicity/<id>/` | 门户公示详情；已取消记录返回 404 |
| POST | `/api/selection/publicity/<id>/cancel/` | 取消公示，可传 `reason` |
| POST | `/api/selection/publicity/batch/` | 按类型/组别/市州批量公示；已公示的自动跳过 |
| GET | `/api/selection/publicity/cities/` | 已公示记录的市州选项，可选 `group_type`、`publicity_type` |
| GET | `/api/selection/publicity/manage/` | CMS 公示管理列表，包含已取消记录，可选 `status` |
| GET | `/api/selection/publicity/manage/<id>/` | CMS 公示详情，包含取消原因和操作人 |

### 4.5 请求示例

新建高校组初选模板：

```json
POST /api/selection/templates/
{
  "name": "高校组初选评分模板",
  "group_type": "HIGHER",
  "round": "INITIAL",
  "items": [
    {"dimension_code": "TECH", "dimension_name": "技术能力", "weight": 30, "max_score": 100},
    {"dimension_code": "MATCH", "dimension_name": "比赛表现", "weight": 30, "max_score": 100},
    {"dimension_code": "PHYSICAL", "dimension_name": "身体素质", "weight": 20, "max_score": 100},
    {"dimension_code": "TACTIC", "dimension_name": "战术意识", "weight": 20, "max_score": 100}
  ]
}
```

录入评分：

```json
POST /api/selection/scores/
{
  "event_id": 1,
  "group_type": "HIGHER",
  "round": "INITIAL",
  "template_id": 1,
  "candidate_id": 10001,
  "scores": [
    {"dimension_code": "TECH", "score": 92},
    {"dimension_code": "MATCH", "score": 88},
    {"dimension_code": "PHYSICAL", "score": 85},
    {"dimension_code": "TACTIC", "score": 90}
  ]
}
```

修改评分（只传维度得分）：

```json
PUT /api/selection/scores/1/
{
  "scores": [
    {"dimension_code": "TECH", "score": 95},
    {"dimension_code": "MATCH", "score": 90},
    {"dimension_code": "PHYSICAL", "score": 85},
    {"dimension_code": "TACTIC", "score": 90}
  ]
}
```

生成高校组复选结果（不传 `quota`、权重时使用 settings 默认值）：

```json
POST /api/selection/results/generate/
{
  "event_id": 1,
  "group_type": "HIGHER",
  "result_round": "FINAL",
  "quota": 11,
  "initial_weight": 0.4,
  "final_weight": 0.6
}
```

公示一条运动员报名信息（演示档案 `10001`）：

```json
POST /api/selection/publicity/
{
  "event_id": 1,
  "publicity_type": "ATHLETE",
  "source_id": "10001"
}
```

返回中只包含白名单字段和脱敏身份证：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "id": 1,
    "name": "张伟",
    "group_type": "HIGHER",
    "group_type_label": "高校组",
    "city": "江城市",
    "publicity_type": "ATHLETE",
    "publicity_type_label": "运动员",
    "id_card_masked": "510101********0011",
    "fields": {
      "name": "张伟",
      "gender": "男",
      "team_name": "江城大学",
      "school_name": "江城大学",
      "position": "前锋",
      "jersey_number": 1
    },
    "status": "PUBLISHED",
    "status_label": "已公示",
    "published_at": "2026-10-21 11:00:00"
  }
}
```

取消公示：

```json
POST /api/selection/publicity/1/cancel/
{
  "reason": "报名信息有误，退回修改"
}
```

按组别和市州查询门户公示：

```
GET /api/selection/publicity/?event_id=1&group_type=HIGHER&city=江城市&page=1&size=10
```

批量公示高校组全部运动员：

```json
POST /api/selection/publicity/batch/
{
  "event_id": 1,
  "group_type": "HIGHER",
  "publicity_type": "ATHLETE"
}
```

完整接口文档（参数、返回字段、错误码、数据字典）见 [docs/API.md](docs/API.md)，可直接执行的请求示例见 [docs/selection-api.http](docs/selection-api.http)。

---

## 5. 评分与排名规则

### 5.1 单轮总分（固定加权公式）

每个维度先换算成百分制，再按权重加权：

```
维度百分比 = 维度得分 / 该维度满分 × 100
单轮总分 = Σ(维度百分比 × 权重) / Σ(权重)
```

结果保留 2 位小数，四舍五入（`ROUND_HALF_UP`）。

示例：技术 90（权重 30）、比赛 80（权重 30）、身体 70（权重 20）、战术 60（权重 20）

```
(90×30 + 80×30 + 70×20 + 60×20) / 100 = 77.00
```

> 公式是固定的，不做“公式可配置”，符合优化表中“固定加权平均公式”的降级要求。

### 5.2 综合分（初选 + 复选）

```
综合分 = (初选总分 × 初选权重 + 复选总分 × 复选权重) / (初选权重 + 复选权重)
```

- 默认初选 0.4、复选 0.6，可在生成结果时覆盖。
- 如果某球员只有一轮成绩，则直接使用该轮总分。
- 生成“初选结果”时只使用初选总分，不要求复选成绩。
- 生成“复选结果”时要求有复选成绩；初选成绩可以缺失，缺失时直接使用复选总分。

### 5.3 同分处理

复选结果依次比较：

1. 综合分（高者优先）
2. `TECH` 维度得分（高者优先，可通过 `SELECTION_TIE_BREAK_DIMENSION_CODE` 修改）
3. 复选总分
4. 初选总分
5. 球员档案 ID（小者优先，保证结果稳定）

初选结果依次比较：

1. 初选总分
2. `TECH` 维度得分
3. 球员档案 ID

汇总接口与结果生成使用同一套排序规则，因此“汇总排名”和“结果排名”在相同数据下保持一致。

### 5.4 名额

默认名额 11，可在生成结果时传 `quota` 覆盖。D1 确认后修改 `config/settings.py` 中的 `SELECTION_DEFAULT_QUOTA`。

---

## 6. 数据一致性设计

### 6.1 模板快照

评分录入时，会把模板中的维度编码、维度名称、权重、满分复制到 `sel_score_item`。之后修改模板不会改变历史评分，保证可追溯。

### 6.2 同组同轮次唯一

`sel_score` 有唯一约束：

```
(event_id, score_round, candidate_id)
```

同一球员同一轮次只能有一条评分，初选和复选互相独立。重复录入返回 409。

### 6.3 修改评分使用乐观锁

`sel_score.version` 每次修改 +1。更新时执行：

```sql
UPDATE sel_score
SET ..., version = version + 1
WHERE id = ? AND version = ?
```

如果影响行数为 0，说明已被其他人修改，返回 409，前端刷新后重试。

### 6.4 结果批次与“过期”标记

- 每次生成结果都会新建一个 `sel_result_batch`，版本号 `version_no` 递增。
- 旧批次 `active=0`，新批次 `active=1`。
- 任何评分新增/修改都会把当前有效批次标记为 `stale=1`。
- 结果导出时如果 `stale=1`，返回 409，提示“评分已变更，请重新生成后再导出”。
- 重新生成后得到新版本，旧版本仍保留在历史批次中，可追溯。

### 6.5 结果明细可追溯

`sel_result_item.detail_json` 保存该球员初选/复选每一维度的得分、权重、满分，以及两轮总分。即使之后评分被修改，历史结果批次仍保留当时的明细快照。

---

## 7. 公示（T6-04）

### 7.1 数据流

浏览器只提交“赛事 + 公示类型 + 报名档案 ID”，不直接提交姓名、身份证等报名字段：

```
POST /api/selection/publicity/
        ↓
PublicitySourceClient（成员4报名档案）
        ↓
字段白名单过滤 → 身份证脱敏 → 写入 sel_publicity
        ↓
门户公开列表/详情（只查 status=PUBLISHED）
```

这样即使前端被篡改，也无法把非白名单字段或身份证原文塞进公示表。

### 7.2 字段白名单

白名单位于 `config/settings.py` 的 `SELECTION_PUBLICITY_FIELD_WHITELIST`，按三种类型分别配置：

```python
SELECTION_PUBLICITY_FIELD_WHITELIST = {
    "ATHLETE": (
        "name", "gender", "id_card", "team_name",
        "school_name", "position", "jersey_number",
    ),
    "COACH": (
        "name", "gender", "id_card", "team_name",
        "school_name", "coach_role",
    ),
    "TEAM": ("team_name", "school_name", "coach_name"),
}
```

- 只有白名单内的字段会进入 `profile_json`。
- 演示数据里的 `phone`、`birth_date`、`contact_phone`、`team_type` 不在白名单，测试会验证它们既不落库也不返回。
- 成员4可以按 D1 确认的公示字段直接替换这个配置，不需要改业务代码。

### 7.3 身份证脱敏

- 18 位身份证：保留前 6 位和后 4 位，中间 8 位替换为 `*`，例如 `510101********0011`。
- 15 位身份证：保留前 6 位和后 3 位，中间 6 位替换为 `*`。
- 只会保存 `id_card_masked`，原始号码在构建快照时即被丢弃，不写入数据库、不返回接口。
- 白名单里若使用 `id_number`、`身份证号` 等别名，也会按身份证字段处理并脱敏。

### 7.4 公示、取消与即时不可见

- 同一赛事 + 公示类型 + 报名档案 ID 唯一。重复公示返回 409。
- 已取消的记录可以重新公示，重新公示会刷新快照并清空取消原因。
- 取消后 `status=CANCELLED`，同时记录取消人、取消时间和原因。
- 门户列表、详情、市州选项都强制过滤 `status=PUBLISHED`，所以取消后立即不可见。
- CMS 管理列表仍能看到已取消记录，用于审计和重新公示。
- 重复点击取消是幂等的，不会报错。

### 7.5 查询与批量公示

- 公开列表必填 `event_id`，可选 `group_type`、`city`、`publicity_type`、`keyword`、`page`、`size`。
- `publicity/cities/` 返回当前已公示记录的市州选项，避免前端硬编码。
- `publicity/batch/` 按类型/组别/市州批量公示；已经公示的记录计入 `skipped`，不会重复插入。
- 管理列表可选 `status=PUBLISHED/CANCELLED`，并可看到 `source_id`、操作人和取消原因。

### 7.6 接入成员4的报名档案

在主程序中实现一个适配器，返回成员4的原始报名字段即可：

```python
from selection.services.publicity_source import (
    PublicitySourceClient,
    PublicitySourceRecord,
)


class RegistrationPublicitySource(PublicitySourceClient):
    def get_registration(self, publicity_type, source_id):
        row = load_registration(publicity_type, source_id)  # 成员4的表
        if row is None:
            return None
        return PublicitySourceRecord(
            source_id=str(row.id),
            event_id=row.event_id,
            group_type=row.group_type,   # HIGHER/MIDDLE/PRIMARY
            city=row.city,
            publicity_type=publicity_type,
            data={
                "name": row.name,
                "gender": row.gender,
                "id_card": row.id_card,  # 原文只在这里出现，服务层会脱敏
                "team_name": row.team_name,
                "school_name": row.school_name,
                "position": row.position,
                "jersey_number": row.jersey_number,
                # 其他非白名单字段会被自动丢弃
            },
        )
```

然后在主程序 `settings.py` 中设置：

```python
SELECTION_PUBLICITY_SOURCE_MODE = "none"
SELECTION_PUBLICITY_SOURCE_CLIENT = (
    "apps.registrations.services.RegistrationPublicitySource"
)
```

`SELECTION_PUBLICITY_SOURCE_CLIENT` 指向一个“可调用对象”，可以是类，也可以是返回实例的函数。

---

## 8. 并入主程序

### 8.1 基本步骤

1. 把整个 `selection/` 目录复制到主程序的 Django 项目根目录。
2. 在主程序 `settings.py` 的 `INSTALLED_APPS` 中加入 `"selection"`。
3. 在主程序 `urls.py` 中加入：
   ```python
   path("api/selection/", include("selection.urls")),
   ```
4. 执行：
   ```bash
   python manage.py migrate
   ```
   会创建 7 张 `sel_*` 表（6 张遴选评分表 + `sel_publicity` 公示表）。
5. 主程序需要安装 `django-ninja` 和 `openpyxl`（本模块的 `requirements.txt` 已固定版本范围）：
   ```bash
   python -m pip install "django-ninja>=1.6,<2.0" "openpyxl>=3.1,<4"
   ```
   > 注意：主程序成员5的代码使用了 `from ninja import Status`，要求 django-ninja >= 1.6。不要把本模块依赖固定回 1.3，否则会降级同一环境中的 django-ninja，导致主程序启动时报 `ImportError: cannot import name 'Status' from 'ninja'`。
   如果主程序本身也提供 Ninja API，可以保留本模块独立的 `NinjaAPI` 实例，也可以把 `selection.api.router` 挂到主程序的 `NinjaAPI` 上。
6. 如果主程序已有同名 `selection` App，请重命名本 App 并同步修改 import（或把本 App 作为子模块放入一个独立命名空间）。

### 8.2 接入成员4的球员档案

遴选评分只依赖一个很小的接口（`selection/services/archive.py`）；公示的报名档案适配器见 7.6。

```python
class SelectionArchiveClient(ABC):
    def get_candidate(self, candidate_id: int) -> SelectionCandidate | None: ...
    def list_candidates(self, group_type: str) -> list[SelectionCandidate]: ...
```

在主程序中实现它，例如：

```python
from selection.services.archive import SelectionArchiveClient, SelectionCandidate
from apps.players.models import Player


class PlayerArchiveClient(SelectionArchiveClient):
    def get_candidate(self, candidate_id):
        player = Player.objects.filter(pk=candidate_id).first()
        if not player:
            return None
        return SelectionCandidate(
            candidate_id=player.id,
            event_id=player.event_id,
            name=player.name,
            team_name=player.team.name if player.team else "",
            group_type=player.group_type,       # HIGHER / MIDDLE / PRIMARY
            position=player.position,
        )

    def list_candidates(self, group_type):
        return [
            self.get_candidate(player.id)
            for player in Player.objects.filter(group_type=group_type)
        ]
```

然后在 `settings.py` 中：

```python
SELECTION_ARCHIVE_MODE = "none"
SELECTION_ARCHIVE_CLIENT = "apps.players.services.PlayerArchiveClient"
```

`SELECTION_ARCHIVE_CLIENT` 指向一个“可调用对象”，可以是类，也可以是返回实例的函数。

### 8.3 接入成员2的登录用户

本模块需要记录 `created_by` / `updated_by` / `generated_by`。有两种方式：

**方式一（推荐）**：主程序已经使用 Django 登录系统，设置：

```python
SELECTION_CURRENT_USER_MODE = "django"
```

此时会读取 `request.user.id`。

**方式二**：自定义函数：

```python
# apps/accounts/utils.py
def get_current_user_id(request):
    return request.user.id if request.user.is_authenticated else 0
```

```python
SELECTION_CURRENT_USER_MODE = "none"
SELECTION_CURRENT_USER_PROVIDER = "apps.accounts.utils.get_current_user_id"
```

独立演示默认使用 `X-User-Id` 请求头，**只适合本地演示，不能用于正式环境**。

### 8.4 接入成员5的赛事

所有评分、结果和公示都带 `event_id`。前端调用时传当前赛事 ID；档案适配器返回的 `SelectionCandidate.event_id` 或 `PublicitySourceRecord.event_id` 也会被校验，不一致会返回 400。

### 8.5 权限与 CSRF

- 本模块的 `NinjaAPI` 没有配置 `auth`，与独立演示一致；并入主程序后必须补上权限校验。
- 建议用 Ninja 的 `auth`（例如 `HttpBearer`）统一校验成员2签发的登录凭证，或在中间件/反向代理层做角色校验，也可以用 Ninja 的 `Router` 级 `auth` 只保护管理类接口。
- 公示的 `POST /publicity/`、`/publicity/batch/`、`/publicity/<id>/cancel/` 属于管理操作，必须加管理员/赛事运营权限；门户的 `GET` 列表和详情可以公开。
- django-ninja 的 `NinjaAPI` 默认 `csrf=False`，本模块保持该默认值，便于接口工具直接调用。如果主程序使用 Session 登录并希望沿用 CSRF 防护，创建 API 时传 `csrf=True`；纯 Token 鉴权的前后端分离场景可以继续保持 `csrf=False`。
- 如果前后端不同源，安装 `django-cors-headers` 并按 `config/settings.py` 底部注释配置；生产环境不要 `CORS_ALLOW_ALL_ORIGINS=True`。

### 8.6 时区

主程序使用 `USE_TZ=True` 和 `TIME_ZONE="Asia/Shanghai"` 时可直接兼容。Excel 和接口中的时间都按上海时区格式化。

---

## 9. MySQL

默认 SQLite 已足够演示。正式环境使用 MySQL：

```bash
python -m pip install -r requirements-mysql.txt
```

设置环境变量后执行迁移：

```bash
# Windows PowerShell 示例
$env:DB_ENGINE="mysql"
$env:DB_NAME="football_system"
$env:DB_USER="root"
$env:DB_PASSWORD="你的密码"
$env:DB_HOST="127.0.0.1"
$env:DB_PORT="3306"
python manage.py migrate
```

数据库请使用 `utf8mb4` 字符集。表名统一为 `sel_*`，不会和主程序已有表冲突。

---

## 10. 测试

```bash
python manage.py test selection
```

当前共 **30 个测试**，覆盖：

- 加权总分、权重归一化、非 100 满分、综合分、单轮回退
- 模板创建、列表、修改、组别/轮次不可变、权重之和校验、重复维度
- 评分录入、重复 409、未知球员、超满分、缺维度
- 评分修改、版本号递增、汇总排序、同分按 TECH 维度排序
- 初选/复选结果生成、名额、明细快照、Excel 导出
- 评分修改后结果 `stale`、过期导出 409、重新生成版本递增、历史批次
- 中英文组别/轮次别名、camelCase 输入
- 三类报名信息公示、字段白名单、身份证脱敏、重复公示 409
- 取消后门户列表/详情立即不可见、CMS 仍可审计、重新公示
- 按组别/市州/类型查询、市州选项、批量公示幂等
- 公示参数校验（未知类型、档案不存在、赛事不匹配）

本机运行结果：`Ran 30 tests ... OK`。

---

## 11. D1 需要确认的参数

以下默认值集中在 `config/settings.py`，确认后直接改配置即可，不需要改业务代码：

| 参数 | 当前默认 | 位置 |
|---|---|---|
| 评分维度与权重 | 技术 30、比赛 30、身体 20、战术 20 | 模板数据（D1 确认后由管理员建模板） |
| 初选/复选权重 | 0.4 / 0.6 | `SELECTION_DEFAULT_INITIAL_WEIGHT`、`SELECTION_DEFAULT_FINAL_WEIGHT` |
| 遴选名额 | 11 | `SELECTION_DEFAULT_QUOTA` |
| 同分优先维度 | `TECH` | `SELECTION_TIE_BREAK_DIMENSION_CODE` |
| 维度满分 | 100 | 模板 `max_score` |
| 复选结果是否必须有初选成绩 | 不强制；缺失时用复选总分 | `selection/services/results.py` |
| 初选结果是否要求复选成绩 | 不要求 | `selection/services/results.py` |
| 公示字段白名单 | 运动员 7 项、教练员 6 项、球队 3 项 | `SELECTION_PUBLICITY_FIELD_WHITELIST` |
| 公示数据源 | demo（演示报名档案） | `SELECTION_PUBLICITY_SOURCE_MODE` / `SELECTION_PUBLICITY_SOURCE_CLIENT` |

---

## 12. 已知边界与注意事项

1. **演示档案是假的**：`DemoSelectionArchiveClient` 只有 45 名模拟球员；`DemoPublicitySourceClient` 只有 45 名运动员、15 名教练员、9 支球队（event_id=1）。正式联调必须替换为成员4的档案/报名实现。
2. **演示用户是请求头**：`X-User-Id` 可伪造，只用于本地演示。正式环境请用成员2的登录用户。
3. **没有做登录/权限**：这是成员2的职责；并入主程序时请给接口加权限。
4. **SQLite 适合演示**：正式多用户并发请使用 MySQL；评分修改已做乐观锁，但 SQLite 的并发能力有限。
5. **结果版本不会自动清理**：每次生成都会保留历史批次，便于追溯。如果数据量大，可在主程序里加定期归档策略。
6. **初选/复选模板独立**：同一组别的初选和复选可以有不同的维度和权重；评分明细保存的是录入时的快照。
7. **公示只存脱敏快照**：`sel_publicity` 不保存身份证原文，只保存 `id_card_masked` 和白名单字段。如果成员4的报名表需要保留原文，请留在成员4的表中，不要传给本模块。
8. **取消不是删除**：取消公示只把 `status` 改为 `CANCELLED`，记录仍保留用于审计；公开接口会自动过滤掉。
9. **Windows 控制台中文乱码**：如果 `seed_demo` 的输出在 cmd 中显示乱码，是终端编码问题，数据库内容本身是 UTF-8；可在 PowerShell 或 VS Code 终端中运行，或先执行 `chcp 65001`。

---

## 13. 本模块在整体系统中的效果

并入主程序后，成员6这部分会形成一条完整链路：

```
管理员建评分模板
      ↓
评委按高校/中学/小学组录入初选、复选评分
      ↓
系统按固定加权公式自动算总分并排名
      ↓
按名额生成各组最佳球员结果（带版本、可追溯明细）
      ↓
汇总表 / 结果表一键导出 Excel
      ↓
评分一旦修改，旧结果自动标记过期，必须重新生成才能导出

成员4报名档案
      ↓
管理员按运动员/教练员/球队点击公示（支持批量）
      ↓
字段白名单过滤 + 身份证脱敏
      ↓
门户按组别/市州查询公示；取消后立即不可见
```

对用户来说，最终看到的是：

- 一套可复用的评分标准（初选/复选分开）
- 每个球员每一轮的维度得分和总分
- 按组别自动生成的排名和最佳球员名单
- 可下载的评分汇总表和遴选结果表
- 评分修改后不会误用旧结果，保证“汇总、结果、导出”一致
- 门户上按组别/市州查看运动员、教练员、球队公示，身份证号已脱敏
- 取消公示后门户立即看不到，但后台仍保留取消原因，便于审计和重新公示
