# 省级中小学足球赛事系统 · 后端仓库

后端技术栈：**Django 5.2 LTS + django-ninja 1.3（Python 3.10）**；MySQL（本地开发默认 SQLite，零依赖即可启动）、Redis、MinIO，部署走 Docker Compose。
> 技术栈由 Spring Boot 3 变更为 Django + django-ninja，原因与对每个人的影响见 `docs/技术栈变更通知-后端.md`；**对外接口契约不变**。

## 文档索引（唯一源，先读这些再写代码）

| 文件 | 用途 |
| --- | --- |
| `docs/后端接口文档.md` | **接口契约唯一源**。路径、请求/响应、错误码、角色权限、业务规则、模型定义 |
| `docs/省级中小学足球系统功能设计.xlsx` | 需求源（244 条功能与优先级） |
| `docs/省级中小学足球系统_10天版优化分工.xlsx` | 分工与工期（认领自己的页） |
| `docs/技术栈变更通知-后端.md` | 技术栈变更说明与对每个人的影响 |
| `docs/后端开工包.md` | 群公告、每人开工指令、公共能力调用速查、验收标准 |

## 目录结构

```
backend/
  config/                 # 全局配置（settings / urls / wsgi / asgi）——只允许成员2 改
  apps/
    common/               # 公共能力：统一信封、错误码、鉴权、权限、加解密、幂等、中间件——只允许成员2 改
    accounts/             # 账号、组织、字典、审计日志、登录
    notices/              # 消息中心
    attachments/          # 文件上传下载
    verification/         # 身份核验
    policyqa/             # 政策问答
  deploy/nginx.conf       # 反向代理示例
  Dockerfile / docker-compose.yml
  requirements.txt / .env.example
  manage.py
```

每个应用的固定分层：`models.py`（ORM 模型）→ `schemas.py`（请求模型）→ `services.py`（业务逻辑）→ `api.py`（路由与视图）。
**禁止在视图里写业务规则、禁止在视图里自己拼信封、禁止自己写角色 if 判断。**

## 本地启动

```powershell
cd backend
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py seed_demo      # 写入 8 个组织、8 类角色演示账号、14 条字典项
python manage.py runserver
python -m pytest               # 接口冒烟：登录/权限/400/401/403/404/409/幂等/文件/字典缓存
```

- 默认 `DATABASE_URL=sqlite:///./data/football.db`，无需装 MySQL 即可启动；
- 交互文档 http://127.0.0.1:8000/api/v1/docs ，OpenAPI 契约 http://127.0.0.1:8000/api/v1/openapi.json （Apifox 直接导入）；
- 演示账号：`admin` / `province` / `city` / `university` / `team` / `coach` / `athlete` / `recorder`，初始密码统一 `Foot@2026`；
- 切正式环境：`DATABASE_URL` 改 MySQL、`STORAGE_BACKEND` 改 `minio`、填 `REDIS_URL`，或直接 `docker compose up -d`。

## 一个接口长什么样（照这个写）

```python
from ninja import Router
from apps.common.authentication import JWTAuth
from apps.common.envelope import ok
from apps.common.pagination import paginate
from apps.common.permissions import ADMIN, PROVINCE_REVIEWER, register_operation, require
from apps.common.schemas import EnvelopeOut

router = Router(tags=["accounts"])
register_operation("accountList", (ADMIN, PROVINCE_REVIEWER))  # 导入期登记权限点
_auth = JWTAuth()


@router.get("/accounts", auth=_auth, response=EnvelopeOut, summary="账号列表")
def list_accounts(request):
    actor = require("accountList")          # 角色 + 权限点校验，返回当前账号
    return paginate(request, services.list_accounts(actor))
```

请求体模型继承 `StrictSchema`（多传未知字段直接 400）；失败一律 `raise ApiError(码, 说明)`；写操作按需声明 `audit_state(...)`。

## 分支与协作规约

- `main` 只放可运行代码，**禁止直接推 main**；
- 分支：`feature/2-foundation`、`feature/3-enrollment`、`feature/4-people`、`feature/5-events`、`feature/6-selection`；
- 一个模块一个 PR，**成员2 负责合并与冲突处理**；合并前必须本地能启动、`python manage.py check` 通过、手动冒烟过；
- 接口契约变更必须先在群里广播并同步更新 `docs/后端接口文档.md`，禁止只改代码不改契约。

## 三条硬规定

1. 身份证/手机号的加解密、脱敏、查重索引一律调用 `apps/common/crypto.py`，**禁止自行实现**；
2. 完整证件号、手机号、密码、令牌、附件正文不得进日志、不得进导出、不得进公开接口；
3. 受保护接口必须用 `apps/common/permissions.py` 的 `register_operation` + `require` 完成「角色 → 权限点 → 数据范围 → 对象归属/状态」四层校验，**禁止在接口里自己写角色 if 判断**。

## 提交前自检（DoD）

- [ ] `python manage.py check` 无问题，`python manage.py runserver` 能起，`/api/v1/docs` 能看到自己的接口
- [ ] `python -m pytest` 全绿（新增接口补一条冒烟用例）
- [ ] 请求模型用 `StrictSchema`，未知字段被拒
- [ ] 越权返回 403 且是契约信封（不是 500），版本冲突返回 409
- [ ] 响应体是 `{code, message, data, requestId}`，分页 data 是 `{items, page, pageSize, total}`
- [ ] 日志与响应中无完整证件号/手机号
