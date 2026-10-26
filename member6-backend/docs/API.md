# 省级中小学足球系统 · 成员6后端 API 文档

> 模块：T6-03 遴选评分（模块 6.2） + T6-04 报名公示（模块 7.3）  
> 版本：1.1.0  
> 更新时间：2026-10-26  
> 技术基线：Python > 3.10.9、Django 5.2 LTS、django-ninja >=1.6,<2.0（与主程序一致）  
> 代码目录：`member6-backend/selection/`

---

## 1. 模块概述

本模块负责两类业务：

1. **遴选评分**：评分模板、初选/复选评分录入与修改、汇总排名、最佳球员结果生成、结果版本管理、Excel 导出。
2. **报名公示**：运动员/教练员/球队报名信息公示、取消公示、按组别/市州查询、身份证脱敏、字段白名单、批量公示。

接口统一使用 JSON；Excel 导出接口返回文件流。

---

## 2. 运行环境与文档入口

### 2.1 启动项目

```bash
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver 8001
```

> 独立运行使用 8001，避免与主程序默认的 8000 冲突。

### 2.2 基础地址

```text
独立运行：  http://127.0.0.1:8001/api/selection
并入主程序：http://127.0.0.1:8000/api/v1/selection
```

### 2.3 自动接口文档

项目启动后可访问：

| 类型 | 独立运行 | 并入主程序 |
|---|---|---|
| Swagger UI | `http://127.0.0.1:8001/api/selection/docs` | `http://127.0.0.1:8000/api/v1/docs` |
| OpenAPI JSON | `http://127.0.0.1:8001/api/selection/openapi.json` | `http://127.0.0.1:8000/api/v1/openapi.json` |

### 2.4 可执行请求示例

[selection-api.http](selection-api.http) 中提供了 30 多组可直接执行的请求示例，适用于 VS Code REST Client 或 JetBrains HTTP Client。

> 本文示例中的记录 ID（如模板 ID、评分 ID、公示 ID）仅用于展示字段结构，实际调用时请使用接口返回的真实 ID。执行 `seed_demo --reset` 后，自增 ID 会重新分配。

---

## 3. 通用约定

### 3.1 请求格式

- JSON 接口使用 `Content-Type: application/json`。
- 请求体编码为 UTF-8。
- 所有 URL 以 `/` 结尾。
- 请求体字段同时接受 `snake_case` 和 `camelCase`，例如：
  - `event_id` / `eventId`
  - `group_type` / `groupType`
  - `template_id` / `templateId`
  - `candidate_id` / `candidateId`
  - `result_round` / `resultRound`
  - `initial_weight` / `initialWeight`
- 响应字段统一使用 `snake_case`。

### 3.2 身份与鉴权

**独立演示**：通过请求头记录操作人；没有令牌时所有接口可用。

```http
X-User-Id: 1
```

该值只写入 `created_by`、`updated_by`、`published_by`、`cancelled_by`、`generated_by` 等审计字段。

**并入主程序**：`SELECTION_AUTH_MODE=auto` 会自动导入主程序 `apps.common.authentication.JWTAuth`，管理接口需要：

```http
Authorization: Bearer <成员2签发的 accessToken>
```

未登录返回 HTTP 401 + `{"code":"UNAUTHENTICATED",...}`。以下门户接口无需登录：

- `GET /publicity/`
- `GET /publicity/cities/`
- `GET /publicity/<id>/`

> `X-User-Id` 可伪造，只适合本地演示，正式环境不要作为身份凭证。

### 3.3 统一响应结构

响应格式随部署方式自动切换（`SELECTION_ENVELOPE_MODE=auto`）。

**独立运行**：

```json
{
  "code": 0,
  "message": "ok",
  "data": {}
}
```

失败时 `code` 与 HTTP 状态码一致：

```json
{
  "code": 409,
  "message": "该报名信息已在公示中",
  "data": null
}
```

**并入主程序**：

```json
{
  "code": "OK",
  "message": "成功",
  "data": {},
  "requestId": "req_..."
}
```

失败时 `code` 使用主程序错误码（如 `INVALID_ARGUMENT`、`NOT_FOUND`、`STATE_CONFLICT`、`UNAUTHENTICATED`），HTTP 状态码与主程序契约一致。

### 3.4 状态码约定

| HTTP / code | 含义 |
|---|---|
| 200 | 成功 |
| 400 | 参数错误、业务校验失败、数据不匹配 |
| 404 | 模板、评分、结果、公示记录或报名档案不存在 |
| 405 | 请求方法不支持 |
| 409 | 重复录入、并发修改、结果已过期 |
| 500 | 服务器内部错误或适配器配置错误 |

### 3.5 分页

分页接口使用查询参数：

| 参数 | 默认值 | 约束 |
|---|---|---|
| `page` | 1 | 最小 1 |
| `size` | 10 | 最小 1，最大 100，超过 100 按 100 处理 |

独立运行分页返回：

```json
{
  "records": [],
  "total": 0,
  "page": 1,
  "size": 10
}
```

并入主程序后自动使用主程序分页字段：

```json
{
  "items": [],
  "page": 1,
  "pageSize": 10,
  "total": 0
}
```

### 3.6 时间与数字

- 时间格式：`YYYY-MM-DD HH:MM:SS`，时区为 `Asia/Shanghai`。
- 分数、权重、综合分等按浮点数返回，保留两位小数。
- `max_score`、`weight`、`score` 的入库精度为两位小数。

### 3.7 CSRF、CORS 与权限

- `NinjaAPI` 使用默认 `csrf=False`，便于前后端分离和接口工具直接调用。
- 独立运行自带 `SelectionCorsMiddleware`，默认放行 `localhost:5173`、`localhost:3000`；可用 `SELECTION_CORS_ORIGINS` 覆盖。
- 并入主程序后由主程序 `apps.common.middleware.CorsMiddleware` 处理 CORS。
- 管理接口已接入主程序 JWT；赛事运营/管理员角色权限由成员2的鉴权层负责。
- 正式环境不要把演示用的 `X-User-Id` 当作身份凭证。

---

## 4. 枚举与别名

### 4.1 组别 `group_type`

| 代码 | 名称 | 可接受的别名 |
|---|---|---|
| `HIGHER` | 高校组 | `higher`、`UNIVERSITY`、`university`、`高校`、`大学组`、`大学` |
| `MIDDLE` | 中学组 | `middle`、`中学`、`初中组`、`初中`、`高中组`、`高中` |
| `PRIMARY` | 小学组 | `primary`、`小学`、`小学组` |

> 主程序 `EventGroup.category` 用 `UNIVERSITY` 表示高校组；传入 `UNIVERSITY` 会统一按 `HIGHER` 处理，响应中的 `group_type` 仍为 `HIGHER`。

### 4.2 轮次 `round` / `result_round`

| 代码 | 名称 | 可接受的别名 |
|---|---|---|
| `INITIAL` | 初选 | `initial`、`初赛`、`初轮`、`初选轮` |
| `FINAL` | 复选 | `final`、`复赛`、`决赛`、`终选`、`复选轮` |

### 4.3 公示类型 `publicity_type`

| 代码 | 名称 | 可接受的别名 |
|---|---|---|
| `ATHLETE` | 运动员 | `athlete`、`球员`、`运动员信息` |
| `COACH` | 教练员 | `coach`、`教练`、`领队` |
| `TEAM` | 球队 | `team`、`运动队`、`队伍`、`球队信息` |

### 4.4 公示状态 `status`

| 代码 | 名称 | 可接受的别名 |
|---|---|---|
| `PUBLISHED` | 已公示 | `published`、`PUBLISH`、`公示中` |
| `CANCELLED` | 已取消 | `cancelled`、`CANCELED`、`CANCEL`、`取消` |

---

## 5. 接口总览

共 17 个路径、22 个操作。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/templates/` | 评分模板列表 |
| POST | `/templates/` | 新建评分模板 |
| GET | `/templates/{template_id}/` | 模板详情 |
| PUT | `/templates/{template_id}/` | 修改模板名称或维度 |
| GET | `/scores/` | 评分列表（分页） |
| POST | `/scores/` | 录入评分 |
| GET | `/scores/{score_id}/` | 评分详情 |
| PUT | `/scores/{score_id}/` | 修改评分 |
| GET | `/scores/summary/` | 单轮汇总排名 |
| GET | `/scores/export/` | 导出评分 Excel |
| POST | `/results/generate/` | 生成遴选结果 |
| GET | `/results/latest/` | 当前有效结果 |
| GET | `/results/batches/` | 结果历史批次 |
| GET | `/results/export/` | 导出结果 Excel |
| GET | `/publicity/` | 门户公示列表（分页） |
| POST | `/publicity/` | 公示一条报名信息 |
| GET | `/publicity/{record_id}/` | 门户公示详情 |
| POST | `/publicity/{record_id}/cancel/` | 取消公示 |
| GET | `/publicity/cities/` | 已公示市州选项 |
| POST | `/publicity/batch/` | 批量公示 |
| GET | `/publicity/manage/` | CMS 公示管理列表（分页） |
| GET | `/publicity/manage/{record_id}/` | CMS 公示详情 |

---

## 6. 评分模板 API

### 6.1 查询模板列表

```http
GET /templates/
```

#### 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `group_type` | 否 | 组别，支持代码或中文别名 |
| `round` | 否 | 轮次，支持 `INITIAL` / `FINAL` |
| `status` | 否 | `1` 启用，`0` 停用 |

#### 返回

`data` 为模板对象数组，按创建时间倒序排列。模板对象字段见 10.1。

#### 示例

```http
GET /api/selection/templates/?group_type=HIGHER&round=INITIAL
```

---

### 6.2 新建评分模板

```http
POST /templates/
```

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 | 模板名称，最长 100 字符 |
| `group_type` | string | 是 | 组别 |
| `round` | string | 是 | 轮次 |
| `items` | array | 是 | 评分维度，1–20 个 |
| `items[].dimension_code` | string | 是 | 维度编码，最长 32 字符，自动转大写 |
| `items[].dimension_name` | string | 是 | 维度名称，最长 64 字符 |
| `items[].weight` | number | 是 | 权重，0.01–100 |
| `items[].max_score` | number | 否 | 维度满分，默认 100，必须大于 0 |
| `items[].sort_no` | integer | 否 | 排序号，默认按数组顺序 |

#### 规则

- 所有维度权重之和必须**严格等于 100**。
- 同一模板内维度编码不能重复。
- 新建模板时，同组别、同轮次、状态为启用的旧模板会自动停用。
- 历史评分使用自己的维度快照，不受模板停用影响。

#### 请求示例

```json
{
  "name": "高校组初选评分模板",
  "group_type": "HIGHER",
  "round": "INITIAL",
  "items": [
    {
      "dimension_code": "TECH",
      "dimension_name": "技术能力",
      "weight": 30,
      "max_score": 100
    },
    {
      "dimension_code": "MATCH",
      "dimension_name": "比赛表现",
      "weight": 30,
      "max_score": 100
    },
    {
      "dimension_code": "PHYSICAL",
      "dimension_name": "身体素质",
      "weight": 20,
      "max_score": 100
    },
    {
      "dimension_code": "TACTIC",
      "dimension_name": "战术意识",
      "weight": 20,
      "max_score": 100
    }
  ]
}
```

#### 返回

`data` 为新建的模板对象。

#### 主要错误

| 状态码 | 场景 |
|---|---|
| 400 | 名称为空、维度为空、权重和不为 100、维度重复、组别/轮次非法 |
| 404 | 不适用 |

---

### 6.3 查询模板详情

```http
GET /templates/{template_id}/
```

#### 返回

`data` 为模板对象，包含 `items` 维度数组。

#### 错误

| 状态码 | 场景 |
|---|---|
| 404 | 模板不存在 |

---

### 6.4 修改模板

```http
PUT /templates/{template_id}/
```

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 否 | 新模板名称 |
| `group_type` | string | 否 | 只能传原值，不能修改 |
| `round` | string | 否 | 只能传原值，不能修改 |
| `items` | array | 否 | 传入后会整体替换原维度 |

#### 规则

- 模板创建后不能修改组别和轮次。
- `items` 如果传入，会删除原维度并重新创建，权重之和仍必须为 100。
- 只传 `name` 时仅修改名称。
- 修改模板不会影响已录入评分，因为评分保存的是录入时的维度快照。

#### 返回

`data` 为修改后的模板对象。

---

## 7. 评分 API

### 7.1 查询评分列表

```http
GET /scores/
```

#### 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `event_id` | 是 | 赛事 ID，不透明字符串（演示为 `"1"`，主程序为 `evt_...`） |
| `group_type` | 是 | 组别 |
| `round` | 是 | 轮次 |
| `keyword` | 否 | 按球员姓名或球队模糊搜索，最长 50 字符 |
| `page` | 否 | 页码，默认 1 |
| `size` | 否 | 每页数量，默认 10，最大 100 |

#### 返回

分页对象，`records` 为评分对象数组（见 10.2）。排序规则：

```text
总分降序 → 球员档案 ID 升序
```

#### 示例

```http
GET /api/selection/scores/?event_id=1&group_type=HIGHER&round=INITIAL&page=1&size=10
```

---

### 7.2 录入评分

```http
POST /scores/
```

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `event_id` | string | 是 | 赛事 ID，最长 64，不透明字符串 |
| `group_type` | string | 是 | 组别 |
| `round` | string | 是 | 轮次 |
| `template_id` | integer | 是 | 评分模板 ID |
| `candidate_id` | string | 是 | 球员档案 ID / 报名名单 ID，最长 64 |
| `scores` | array | 是 | 维度得分数组 |
| `scores[].dimension_code` | string | 是 | 维度编码，必须与模板一致 |
| `scores[].score` | number | 是 | 维度得分，0 到该维度满分 |

#### 规则

- 模板必须存在且处于启用状态。
- 模板的组别、轮次必须与请求一致。
- 请求中的维度必须与模板维度**完全一致**，不能缺少或多余。
- 同一赛事、同一轮次、同一球员只能有一条评分。
- 球员档案必须存在，且档案中的赛事、组别与请求一致。
- 录入成功后，该赛事该组别的当前有效结果会被标记为 `stale=true`。

#### 请求示例

```json
{
  "event_id": "1",
  "group_type": "HIGHER",
  "round": "INITIAL",
  "template_id": 1,
  "candidate_id": "10001",
  "scores": [
    {"dimension_code": "TECH", "score": 92},
    {"dimension_code": "MATCH", "score": 88},
    {"dimension_code": "PHYSICAL", "score": 85},
    {"dimension_code": "TACTIC", "score": 90}
  ]
}
```

#### 返回

`data` 为新建的评分对象（见 10.2）。

#### 主要错误

| 状态码 | 场景 |
|---|---|
| 400 | 参数缺失、模板停用、模板组别/轮次不一致、维度缺失/多余、得分越界、球员档案不存在或组别/赛事不一致 |
| 404 | 模板不存在 |
| 409 | 该球员本轮评分已存在 |

---

### 7.3 查询评分详情

```http
GET /scores/{score_id}/
```

#### 返回

`data` 为评分对象，包含各维度得分明细。

#### 错误

| 状态码 | 场景 |
|---|---|
| 404 | 评分记录不存在 |

---

### 7.4 修改评分

```http
PUT /scores/{score_id}/
```

#### 请求体

```json
{
  "scores": [
    {"dimension_code": "TECH", "score": 95},
    {"dimension_code": "MATCH", "score": 90},
    {"dimension_code": "PHYSICAL", "score": 85},
    {"dimension_code": "TACTIC", "score": 90}
  ]
}
```

#### 规则

- 只修改维度得分，不能修改球员、模板、组别、轮次。
- 必须提交当前评分记录的全部维度。
- 每次修改 `version` 加 1。
- 使用乐观锁：如果并发请求已经修改了该评分，返回 409。
- 修改成功后，当前有效结果批次会被标记为 `stale=true`。

#### 返回

`data` 为修改后的评分对象。

#### 主要错误

| 状态码 | 场景 |
|---|---|
| 400 | 维度缺失/多余、得分越界 |
| 404 | 评分记录不存在 |
| 409 | 评分已被其他人修改 |

---

### 7.5 单轮汇总排名

```http
GET /scores/summary/
```

#### 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `event_id` | 是 | 赛事 ID |
| `group_type` | 是 | 组别 |
| `round` | 是 | 轮次 |

#### 返回

`data` 为评分对象数组，每个对象额外包含 `rank_no`。

排序规则：

1. 总分降序；
2. `SELECTION_TIE_BREAK_DIMENSION_CODE` 指定的维度（默认 `TECH`）得分降序；
3. 球员档案 ID 升序。

#### 示例

```http
GET /api/selection/scores/summary/?event_id=1&group_type=HIGHER&round=INITIAL
```

---

### 7.6 导出评分 Excel

```http
GET /scores/export/
```

#### 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `event_id` | 是 | 赛事 ID |
| `group_type` | 是 | 组别 |
| `round` | 是 | 轮次 |

#### 返回

成功时返回 `.xlsx` 文件流：

```http
Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
Content-Disposition: attachment; filename="selection.xlsx"; filename*=UTF-8''...
```

Excel 列：

```text
排名、姓名、球队、位置、组别、轮次、各维度得分、总分、评分时间
```

#### 错误

| 状态码 | 场景 |
|---|---|
| 404 | 没有可导出的评分数据 |

---

## 8. 遴选结果 API

### 8.1 生成遴选结果

```http
POST /results/generate/
```

#### 请求体

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `event_id` | string | 是 | - | 赛事 ID（不透明字符串，最长 64） |
| `group_type` | string | 是 | - | 组别 |
| `result_round` | string | 否 | `FINAL` | 结果依据轮次 |
| `quota` | integer | 否 | 11 | 遴选名额，最小 1 |
| `initial_weight` | number | 否 | 0.4 | 初选权重，最小 0 |
| `final_weight` | number | 否 | 0.6 | 复选权重，最小 0 |

#### 规则

- `result_round=FINAL`：
  - 必须存在复选评分；
  - 初选评分可选，缺失时直接使用复选总分；
  - `initial_weight + final_weight` 必须大于 0。
- `result_round=INITIAL`：
  - 只使用初选评分；
  - 权重会被内部固定为初选 1、复选 0。
- 每次生成都会创建新版本，旧版本 `active=false`，新版本 `active=true`。
- 生成结果不会清除历史批次。
- 新结果生成后 `stale=false`。

#### 请求示例

```json
{
  "event_id": "1",
  "group_type": "HIGHER",
  "result_round": "FINAL",
  "quota": 11,
  "initial_weight": 0.4,
  "final_weight": 0.6
}
```

#### 返回

`data` 为结果批次对象（见 10.3），包含按排名排列的 `items`。

#### 主要错误

| 状态码 | 场景 |
|---|---|
| 400 | 没有可用评分、权重和小于等于 0、参数非法 |
| 409 | 并发生成冲突 |

---

### 8.2 查询当前有效结果

```http
GET /results/latest/
```

#### 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `event_id` | 是 | 赛事 ID |
| `group_type` | 是 | 组别 |

#### 返回

`data` 为当前 `active=true` 的结果批次，包含结果明细。

#### 错误

| 状态码 | 场景 |
|---|---|
| 404 | 该组别尚未生成结果 |

---

### 8.3 查询结果历史批次

```http
GET /results/batches/
```

#### 查询参数

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `event_id` | 是 | - | 赛事 ID |
| `group_type` | 是 | - | 组别 |
| `limit` | 否 | 20 | 返回条数，1–100 |

#### 返回

`data` 为结果批次数组，每个批次不包含 `items`，额外包含 `item_count`。

#### 示例

```http
GET /api/selection/results/batches/?event_id=1&group_type=HIGHER&limit=20
```

---

### 8.4 导出结果 Excel

```http
GET /results/export/
```

#### 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `event_id` | 是 | 赛事 ID |
| `group_type` | 是 | 组别 |

#### 返回

成功时返回 `.xlsx` 文件流。

Excel 列：

```text
排名、姓名、球队、位置、组别、结果依据、初选总分、复选总分、综合分、生成时间
```

文件末尾额外包含：

- 计算规则 `formula_snapshot`
- 结果批次版本和生成时间

#### 错误

| 状态码 | 场景 |
|---|---|
| 404 | 尚未生成结果 |
| 409 | 评分已变更，结果已过期，需要重新生成 |

---

## 9. 报名公示 API

### 9.1 门户公示列表

```http
GET /publicity/
```

#### 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `event_id` | 是 | 赛事 ID |
| `group_type` | 否 | 组别 |
| `city` | 否 | 市州，精确匹配 |
| `publicity_type` | 否 | 公示类型 |
| `keyword` | 否 | 按公示对象、球队、学校、报名档案 ID 模糊搜索 |
| `page` | 否 | 页码，默认 1 |
| `size` | 否 | 每页数量，默认 10，最大 100 |

#### 返回

分页对象，`records` 为公开公示对象（见 10.4）。

**只返回 `status=PUBLISHED` 的记录。** 已取消记录不会出现在门户列表。

#### 示例

```http
GET /api/selection/publicity/?event_id=1&group_type=HIGHER&city=江城市&page=1&size=10
```

---

### 9.2 公示一条报名信息

```http
POST /publicity/
```

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `event_id` | string | 是 | 赛事 ID（不透明字符串，最长 64） |
| `publicity_type` | string | 是 | `ATHLETE` / `COACH` / `TEAM` |
| `source_id` | string | 是 | 成员4报名/档案记录 ID |

#### 数据流

```text
请求只提交赛事、公示类型、报名档案 ID
        ↓
从成员4报名档案读取权威数据
        ↓
按公示类型应用字段白名单
        ↓
身份证脱敏
        ↓
写入 sel_publicity
```

前端不能直接提交姓名、身份证等公示内容。

#### 请求示例

```json
{
  "event_id": "1",
  "publicity_type": "ATHLETE",
  "source_id": "10001"
}
```

#### 返回

`data` 为管理端公示对象（见 10.5），并额外包含：

| 字段 | 说明 |
|---|---|
| `action` | `published`（首次公示）或 `republished`（取消后重新公示） |

#### 主要错误

| 状态码 | 场景 |
|---|---|
| 400 | 报名档案赛事/组别/类型不匹配、缺市州、缺姓名/球队名称、缺身份证号、字段格式错误 |
| 404 | 报名档案不存在 |
| 409 | 该报名信息已在公示中 |
| 500 | 公示数据源未配置或初始化失败 |

#### 重复公示

同一赛事 + 公示类型 + 报名档案 ID 唯一。已公示时返回 409；已取消的记录可以重新公示，重新公示会刷新快照并清空取消信息。

---

### 9.3 门户公示详情

```http
GET /publicity/{record_id}/
```

#### 返回

`data` 为公开公示对象。

#### 错误

| 状态码 | 场景 |
|---|---|
| 404 | 公示记录不存在或已取消 |

取消后门户详情立即返回 404。

---

### 9.4 取消公示

```http
POST /publicity/{record_id}/cancel/
```

#### 请求体（可选）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `reason` | string | 否 | 取消原因，最长 200 字符 |

也接受 `cancel_reason` 或 `cancelReason`。

#### 请求示例

```json
{
  "reason": "报名信息有误，退回修改"
}
```

#### 规则

- 取消只把 `status` 改为 `CANCELLED`，不删除记录。
- 记录取消人、取消时间、取消原因。
- 重复取消是幂等的，不会报错。
- 取消后门户列表、详情、市州选项立即不再返回该记录。
- CMS 管理列表和详情仍可查看。

#### 返回

`data` 为管理端公示对象，并额外包含：

| 字段 | 说明 |
|---|---|
| `already_cancelled` | `true` 表示本次调用前已经是取消状态 |

#### 错误

| 状态码 | 场景 |
|---|---|
| 404 | 公示记录不存在 |

---

### 9.5 查询已公示市州选项

```http
GET /publicity/cities/
```

#### 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `event_id` | 是 | 赛事 ID |
| `group_type` | 否 | 组别 |
| `publicity_type` | 否 | 公示类型 |

#### 返回

`data` 为字符串数组，例如：

```json
["东林市", "南湖市", "江城市"]
```

只统计 `status=PUBLISHED` 的记录，适合前端筛选下拉框。

---

### 9.6 批量公示

```http
POST /publicity/batch/
```

#### 请求体

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `event_id` | string | 是 | 赛事 ID（不透明字符串，最长 64） |
| `group_type` | string | 否 | 组别 |
| `publicity_type` | string | 否 | 公示类型 |
| `city` | string | 否 | 市州 |

#### 规则

- `group_type`、`publicity_type`、`city` **至少传一个**，避免误公示全部报名信息。
- 已公示的记录会被跳过，不视为失败。
- 接口可以重复调用，适合 CMS 的“一键公示”按钮。
- 单条失败不会中断整个批次，失败信息进入 `errors`。

#### 请求示例

```json
{
  "event_id": "1",
  "group_type": "MIDDLE",
  "publicity_type": "COACH"
}
```

#### 返回

```json
{
  "published": 4,
  "skipped": 1,
  "failed": 0,
  "errors": [],
  "records": []
}
```

| 字段 | 说明 |
|---|---|
| `published` | 本次新公示数量 |
| `skipped` | 已公示而跳过的数量 |
| `failed` | 失败数量 |
| `errors` | 失败明细，元素包含 `source_id` 和 `message` |
| `records` | 本次新公示/重新公示的记录数组 |

#### 主要错误

| 状态码 | 场景 |
|---|---|
| 400 | 未指定任何筛选条件、组别/类型非法 |
| 500 | 公示数据源未配置 |

---

### 9.7 CMS 公示管理列表

```http
GET /publicity/manage/
```

#### 查询参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `event_id` | 是 | 赛事 ID |
| `group_type` | 否 | 组别 |
| `city` | 否 | 市州 |
| `publicity_type` | 否 | 公示类型 |
| `status` | 否 | `PUBLISHED` / `CANCELLED` |
| `keyword` | 否 | 按公示对象、球队、学校、报名档案 ID 模糊搜索 |
| `page` | 否 | 页码，默认 1 |
| `size` | 否 | 每页数量，默认 10，最大 100 |

#### 返回

分页对象，`records` 为管理端公示对象数组（见 10.5），**包含已取消记录**。

---

### 9.8 CMS 公示详情

```http
GET /publicity/manage/{record_id}/
```

#### 返回

`data` 为管理端公示对象，包含：

- 报名档案 ID `source_id`
- 公示操作人 `published_by`
- 取消操作人 `cancelled_by`
- 取消时间 `cancelled_at`
- 取消原因 `cancel_reason`
- 创建/更新时间

#### 错误

| 状态码 | 场景 |
|---|---|
| 404 | 公示记录不存在 |

---

## 10. 数据字典

### 10.1 模板对象 `Template`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 模板 ID |
| `name` | string | 模板名称 |
| `group_type` | string | 组别代码 |
| `group_type_label` | string | 组别中文名 |
| `round` | string | 轮次代码 |
| `round_label` | string | 轮次中文名 |
| `status` | integer | 1 启用，0 停用 |
| `created_by` | integer | 创建人 ID |
| `created_at` | string | 创建时间 |
| `updated_at` | string | 更新时间 |
| `items` | array | 模板维度数组 |

`items` 元素：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 维度 ID |
| `dimension_code` | string | 维度编码 |
| `dimension_name` | string | 维度名称 |
| `weight` | number | 权重 |
| `max_score` | number | 维度满分 |
| `sort_no` | integer | 排序号 |

---

### 10.2 评分对象 `Score`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 评分 ID |
| `event_id` | string | 赛事 ID（不透明字符串） |
| `group_type` | string | 组别代码 |
| `group_type_label` | string | 组别中文名 |
| `round` | string | 轮次代码 |
| `round_label` | string | 轮次中文名 |
| `template_id` | integer | 模板 ID |
| `candidate_id` | string | 球员档案 ID（不透明字符串） |
| `candidate_name` | string | 球员姓名 |
| `team_name` | string | 所属球队 |
| `position` | string | 场上位置 |
| `total_score` | number | 加权总分 |
| `version` | integer | 乐观锁版本号 |
| `created_by` | string | 录入人 ID |
| `created_at` | string | 录入时间 |
| `updated_by` | string | 修改人 ID |
| `updated_at` | string | 修改时间 |
| `items` | array | 维度得分明细 |

`items` 元素：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 明细 ID |
| `dimension_code` | string | 维度编码 |
| `dimension_name` | string | 维度名称 |
| `weight` | number | 录入时的权重快照 |
| `max_score` | number | 录入时的满分快照 |
| `score` | number | 维度得分 |
| `sort_no` | integer | 排序号 |

---

### 10.3 结果批次对象 `ResultBatch`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 批次 ID |
| `event_id` | string | 赛事 ID（不透明字符串） |
| `group_type` | string | 组别代码 |
| `group_type_label` | string | 组别中文名 |
| `result_round` | string | 结果依据轮次 |
| `result_round_label` | string | 轮次中文名 |
| `quota` | integer | 遴选名额 |
| `initial_weight` | number | 初选权重 |
| `final_weight` | number | 复选权重 |
| `formula_snapshot` | string | 公式与同分规则快照 |
| `version_no` | integer | 结果版本号 |
| `active` | boolean | 是否当前有效结果 |
| `stale` | boolean | 是否已因评分变更而过期 |
| `generated_by` | string | 生成人 ID |
| `generated_at` | string | 生成时间 |
| `items` | array | 结果明细数组；历史批次接口中不返回 |
| `item_count` | integer | 仅历史批次接口返回，表示明细数量 |

`items` 元素：

| 字段 | 类型 | 说明 |
|---|---|---|
| `rank_no` | integer | 排名 |
| `candidate_id` | string | 球员档案 ID（不透明字符串） |
| `candidate_name` | string | 球员姓名 |
| `team_name` | string | 所属球队 |
| `position` | string | 场上位置 |
| `initial_score` | number/null | 初选总分 |
| `final_score` | number/null | 复选总分 |
| `composite_score` | number | 综合分 |
| `detail` | object | 初选/复选维度明细快照 |

`detail` 结构：

```json
{
  "initial": {
    "total": 85.9,
    "items": []
  },
  "final": {
    "total": 91.9,
    "items": []
  }
}
```

---

### 10.4 公开公示对象 `PublicityPublic`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer | 公示记录 ID |
| `event_id` | string | 赛事 ID（不透明字符串） |
| `group_type` | string | 组别代码 |
| `group_type_label` | string | 组别中文名 |
| `city` | string | 市州 |
| `publicity_type` | string | 公示类型 |
| `publicity_type_label` | string | 公示类型中文名 |
| `name` | string | 公示对象名称（运动员/教练员姓名或球队名称） |
| `team_name` | string | 所属球队 |
| `school_name` | string | 学校 |
| `id_card_masked` | string | 脱敏身份证号；球队为空字符串 |
| `fields` | object | 白名单字段快照 |
| `status` | string | 公示状态 |
| `status_label` | string | 状态中文名 |
| `published_at` | string | 公示时间 |

#### 白名单字段示例

运动员：

```json
{
  "name": "张伟",
  "gender": "男",
  "team_name": "江城大学",
  "school_name": "江城大学",
  "position": "前锋",
  "jersey_number": 1
}
```

教练员：

```json
{
  "name": "陈志强",
  "gender": "男",
  "team_name": "江城大学",
  "school_name": "江城大学",
  "coach_role": "主教练"
}
```

球队：

```json
{
  "team_name": "江城大学",
  "school_name": "江城大学",
  "coach_name": "陈志强"
}
```

> 身份证号不会出现在 `fields` 中，只会以脱敏形式出现在顶层 `id_card_masked`。

---

### 10.5 管理端公示对象 `PublicityManagement`

在公开公示对象基础上增加：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_id` | string | 成员4报名档案 ID |
| `published_by` | string | 公示操作人 ID |
| `cancelled_by` | string | 取消操作人 ID，未取消为空字符串 |
| `cancelled_at` | string/null | 取消时间 |
| `cancel_reason` | string | 取消原因 |
| `created_at` | string | 创建时间 |
| `updated_at` | string | 更新时间 |

---

## 11. 核心业务规则

### 11.1 单轮总分

```text
维度百分比 = 维度得分 / 该维度满分 × 100
单轮总分 = Σ(维度百分比 × 权重) / Σ(权重)
```

结果保留两位小数，四舍五入。

### 11.2 综合分

```text
综合分 = (初选总分 × 初选权重 + 复选总分 × 复选权重)
         / (初选权重 + 复选权重)
```

- 只有一轮成绩时，直接使用该轮总分。
- 初选结果只使用初选成绩。
- 复选结果要求有复选成绩，初选成绩可选。

### 11.3 同分排序

复选结果：

1. 综合分降序；
2. `TECH` 维度得分降序（可通过 `SELECTION_TIE_BREAK_DIMENSION_CODE` 修改）；
3. 复选总分降序；
4. 初选总分降序；
5. 球员档案 ID 升序。

初选结果：

1. 初选总分降序；
2. `TECH` 维度得分降序；
3. 球员档案 ID 升序。

### 11.4 结果过期

- 评分新增或修改后，当前有效结果批次 `stale=true`。
- 结果导出遇到 `stale=true` 返回 409。
- 重新生成结果会产生新版本，旧版本保留。

### 11.5 身份证脱敏

| 长度 | 规则 | 示例 |
|---|---|---|
| 18 位 | 保留前 6 位和后 4 位，中间替换为 `*` | `510101********0011` |
| 15 位 | 保留前 6 位和后 3 位，中间替换为 `*` | `510101******011` |
| 7–14 位 | 保留前 2 位和后 2 位 | `51******11` |
| 少于 7 位 | 全部替换为 `*` | `***` |

数据库只保存 `id_card_masked`，不保存身份证原文。

### 11.6 公示字段白名单

默认白名单：

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

- 只有白名单字段会进入公示快照。
- `phone`、`birth_date`、`contact_phone`、`team_type` 等非白名单字段不会落库、不会返回。
- 身份证字段会在构建快照时被替换为脱敏值。
- 成员4可以在主程序 `settings.py` 中直接替换白名单，不需要修改业务代码。

### 11.7 取消公示

- 取消只改状态，不删除数据。
- 公开列表、详情、市州选项都强制 `status=PUBLISHED`。
- 取消后门户立即不可见。
- CMS 管理端仍可审计并重新公示。

---

## 12. 演示数据

执行：

```bash
python manage.py seed_demo --reset
```

会生成：

| 数据 | 数量 |
|---|---|
| 评分模板 | 6 套（3 组别 × 2 轮次） |
| 评分 | 90 条（3 组别 × 15 球员 × 2 轮次） |
| 结果批次 | 3 个（每组别 1 个复选结果） |
| 公示记录 | 15 条（每组别 3 运动员 + 1 教练员 + 1 球队） |

演示报名档案 ID：

| 类型 | 高校组 | 中学组 | 小学组 |
|---|---|---|---|
| 运动员 | 10001–10015 | 20001–20015 | 30001–30015 |
| 教练员 | C1001–C1005 | C2001–C2005 | C3001–C3005 |
| 球队 | T1001–T1003 | T2001–T2003 | T3001–T3003 |

演示市州：`江城市`、`南湖市`、`东林市`。

所有演示评分和公示数据属于 `event_id="1"`。接口中赛事 ID、球员 ID、报名档案 ID 一律按字符串传递。

只生成模板：

```bash
python manage.py seed_demo --reset --templates-only
```

---

## 13. 并入主程序

### 13.1 基本步骤

1. **只复制 `selection/`** 到主程序 `backend/`（与 `apps/` 同级）；不要复制 `config/`、`manage.py`、`db.sqlite3`。
2. 在主程序 `backend/config/settings.py` 的 `INSTALLED_APPS` 中加入 `"selection"`。
3. 在主程序 `backend/config/urls.py` 中注册（必须在读取 `api.urls` 之前）：
   ```python
   from selection.api import register_routes as register_selection_routes

   register_selection_routes(api)
   ```
   接口挂在 `/api/v1/selection/...`，复用主程序 JWT、统一信封和错误处理。
   也可以单独挂载 `path("api/selection/", include("selection.urls"))`。
4. 安装依赖：
   ```bash
   python -m pip install "django-ninja>=1.6,<2.0" "openpyxl>=3.1,<4"
   ```
   > 注意：主程序成员5使用 `from ninja import Status`，要求 django-ninja >= 1.6；不要把版本固定回 1.3，否则会降级依赖并导致主程序启动失败。
5. 执行迁移：
   ```bash
   python manage.py migrate
   ```
   会创建 7 张 `sel_*` 表。

### 13.2 自动适配（默认，无需写代码）

| 能力 | `auto` 下的行为 |
|---|---|
| 球员档案 | 读取成员4 `RosterItem`，只取 `Enrollment.status=APPROVED` |
| 报名档案 | 读取 `Enrollment/RosterItem/TeamCoach/Team/Person` |
| 鉴权 | 自动使用 `apps.common.authentication.JWTAuth` |
| 响应/分页 | 自动使用主程序信封和 `{items,page,pageSize,total}` |
| 当前用户 | 优先 `request.auth` / `request.user`，其次 `X-User-Id` |

需要自定义时，可用 `SELECTION_ARCHIVE_CLIENT` / `SELECTION_PUBLICITY_SOURCE_CLIENT` / `SELECTION_CURRENT_USER_PROVIDER` 指向自己的实现，并把对应 `MODE` 设为 `main`。

### 13.3 权限建议

- 模板、评分、结果生成/导出：建议限制为管理员或赛事运营角色。
- 公示发布、批量公示、取消公示：必须限制为管理员或赛事运营角色。
- 门户公示列表、详情、市州选项：公开（已显式 `auth=None`）。
- 使用 Session 登录时，可在创建 `NinjaAPI` 时传 `csrf=True`；使用 Token 时可保持默认 `csrf=False`。
- `SELECTION_AUTH_MODE=main` 可强制要求主程序 JWT，避免配置错误时静默放行。

---

## 14. 自动化测试

```bash
python manage.py test selection
```

当前共 33 个测试（独立运行时 3 个主程序适配器测试自动跳过），覆盖：

- 加权总分、综合分、同分排序；
- 模板创建、修改、权重校验；
- 评分录入、重复录入、修改、乐观锁；
- 结果生成、名额、版本、过期、历史批次；
- Excel 导出；
- 三类公示、字段白名单、身份证脱敏；
- 取消后立即不可见、管理端审计、重新公示；
- 组别/市州/类型查询、批量公示幂等；
- 参数校验和错误码；
- 字符串赛事/球员 ID、`UNIVERSITY` 组别别名；
- 并入主程序后的真实适配器（只取 `APPROVED`、主教练优先、DRAFT 不外泄）。

本机验证结果：

```text
独立运行：Ran 33 tests ... OK (skipped=3)
并入主程序：Ran 55 tests ... OK
System check identified no issues (0 silenced).
No changes detected
```

---

## 15. 变更记录

| 日期 | 内容 |
|---|---|
| D4–D6 | 完成 T6-03 遴选评分：模板、评分、汇总、结果、Excel |
| D6 | 完成 T6-04 报名公示：公示、取消、查询、脱敏、白名单 |
| 2026-10-21 | 升级 Django 5.2 LTS + django-ninja 1.3，保留原接口契约 |
| 2026-10-26 | django-ninja 约束调整为 >=1.6,<2.0，与主程序统一，修复依赖降级导致的启动失败 |
| 2026-10-26 | 赛事/球员 ID 改为字符串；自动复用主程序 JWT、信封、分页；自动读取成员4真实报名数据；新增 CORS 与 `UNIVERSITY` 别名；独立端口改为 8001 |
| 2026-10-23 | 整理正式 API 文档 |
