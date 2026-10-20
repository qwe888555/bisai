# Django + django-ninja RESTful API 模板

基于 Django + django-ninja 的 RESTful API 后端模板项目，提供**多 App 架构**、**三层分层**和**自动化脚手架**，**克隆即可运行**。

## 技术栈

| 类别 | 技术 |
|------|------|
| 框架 | Django 3.2 LTS |
| REST API | django-ninja 1.3.0 |
| 数据验证 | Pydantic v2 |
| 数据库 | SQLite（默认）/ PostgreSQL / MySQL |
| Python | 3.12+ |
| 环境配置 | python-dotenv + dj-database-url |
| 跨域 | django-cors-headers |
| 测试 | pytest + pytest-django + coverage |
| 类型检查 | mypy（严格模式） |
| 代码检查 | ruff + bandit |
| 容器化 | Docker + Docker Compose |
| CI/CD | GitHub Actions |
| 包管理 | uv |

## 快速开始

```bash
# 1. 安装依赖
uv sync --dev

# 2. 环境配置（默认 SQLite，无需修改）
cp .env.example .env

# 3. 数据库迁移
uv run python manage.py migrate

# 4. 启动服务
uv run python manage.py runserver
```

> 📖 更详细的安装指南见 [docs/getting-started.md](./docs/getting-started.md)。

## 访问 API 文档

- **Swagger UI**: http://127.0.0.1:8000/api/docs
- **健康检查**: http://127.0.0.1:8000/api/
- **Demo 接口**: http://127.0.0.1:8000/api/demo/items
- **Example 接口**: http://127.0.0.1:8000/api/example/tags

## 新建业务模块

一键创建完整的三层架构模块：

```bash
uv run python scaffold_app.py orders
```

自动生成 13 个文件 + 自动注册到 settings.py 和 urls.py：

```
apps/orders/
├── __init__.py
├── apps.py               # AppConfig
├── models.py             # 数据模型（继承 TimestampMixin）
├── schemas.py            # Pydantic Schema
├── services.py           # 业务逻辑（事务保护）
├── api.py                # CRUD 端点
├── admin.py              # Admin 配置
├── migrations/
└── tests/                # 三层测试 + conftest
```

然后只需填充模型、Schema、业务逻辑，执行迁移：

```bash
uv run python manage.py makemigrations orders
uv run python manage.py migrate orders
uv run pytest apps/orders/tests/ -v
```

## 项目结构

```
project_root/
├── api/                      # Demo/参考应用（路由: /api/demo/）
├── apps/                     # 业务模块命名空间
│   └── example/            # 参考 App（路由: /api/example/）
├── common/                   # 共享基础设施
│   └── base/
│       ├── models.py       # TimestampMixin, SoftDeleteMixin
│       ├── exceptions.py   # BusinessError
│       └── schemas.py      # PaginatedResponse, ErrorResponse
├── config/                   # Django 项目配置
├── deploy/                   # Nginx 生产配置
├── docs/                     # 📖 项目文档
│   ├── getting-started.md
│   ├── architecture.md
│   ├── multi-app-guide.md  # 多 App 开发指南
│   └── uv-best-practice.md
├── scaffold_app.py           # 🔧 一键创建业务模块
├── Makefile                  # 🔧 常用命令快捷
├── Dockerfile                # 🐳 多阶段构建
├── docker-compose.yml        # 🐳 Django + PostgreSQL + Redis
├── .github/workflows/ci.yml  # 🔄 CI/CD 流水线
├── manage.py
├── pyproject.toml
├── uv.lock
└── .env.example
```

## 开发命令

```bash
# 安装依赖
uv sync --dev

# 数据库迁移
uv run python manage.py migrate

# 新建业务模块
uv run python scaffold_app.py <模块名>

# 启动服务
uv run python manage.py runserver

# 运行测试（带覆盖率）
make test

# 代码检查 + 格式化 + 类型检查
make check

# 安全扫描
make security

# Docker 开发环境
make docker-up

# 查看所有命令
make help
```

## 代码规范

- **三层架构**: API 层不直接操作 ORM，Service 层不处理 HTTP 请求
- **多 App 独立**: 每个业务模块是一个独立的 Django App
- **共享基类**: `TimestampMixin`、`BusinessError`、`PaginatedResponse` 来自 `common/`
- **所有函数/方法**必须包含完整类型注解
- **使用 Google 风格 Docstring**
- **导入顺序**: 标准库 → 第三方库 → Django → 本地应用
