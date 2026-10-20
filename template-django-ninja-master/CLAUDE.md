# CLAUDE.md

<!-- Run /init to auto-populate interactive mode -->
<!-- Keep under 200 lines. If removing a line wouldn't cause mistakes, cut it -->
<!-- HTML comments are stripped from context — free for maintainer notes -->

## Project Overview

- **Name:** django-ninja-template
- **Stack:** Django 3.2 LTS + django-ninja 1.3.0 + Pydantic v2 + Python 3.12+
- **Description:** 基于 Django + django-ninja 的 RESTful API 后端模板项目，提供完整的项目结构、分层架构和最佳实践
- **Entry:** `manage.py` — Django management command entry point
- **Package Manager:** uv (see `uv.lock`)
- **Python:** 3.12 (`.python-version`)

## Commands

```bash
# 安装依赖（生产 + 开发）
uv sync --dev

# 安装可选依赖组
uv sync --dev --extra db      # PostgreSQL / MySQL 驱动
uv sync --dev --extra redis   # Redis 缓存
uv sync --dev --extra storage # 云存储（七牛/OSS）

# 环境配置
cp .env.example .env

# 数据库迁移
uv run python manage.py migrate

# 新建业务模块
uv run python scaffold_app.py <模块名>

# 业务模块迁移
uv run python manage.py makemigrations <模块短名>
uv run python manage.py migrate <模块短名>

# 启动开发服务器
uv run python manage.py runserver

# 运行测试
uv run pytest

# 代码检查
uv run ruff check .

# 代码格式化
uv run ruff format .

# 类型检查
uv run mypy .
```

## Project Architecture

```
project_root/
├── api/                      # Demo/参考应用（路由: /api/demo/）
├── apps/                     # 业务模块命名空间
│   └── example/            # 参考 App（路由: /api/example/）
├── common/                   # 共享基础设施（纯 Python 包）
│   └── base/
│       ├── models.py       # TimestampMixin, SoftDeleteMixin
│       ├── exceptions.py   # BusinessError
│       └── schemas.py      # PaginatedResponse[T], ErrorResponse
├── config/                   # Django 项目配置
│   ├── settings.py         # 配置文件（环境变量驱动）
│   ├── urls.py             # 主 URL 配置 + NinjaAPI 实例
│   ├── wsgi.py             # WSGI 入口
│   └── asgi.py             # ASGI 入口
├── deploy/                   # 部署配置
├── docs/                     # 项目文档
├── scaffold_app.py           # 🆕 一键创建新业务模块
├── logs/                     # 日志目录（自动创建）
├── manage.py                 # Django 管理命令入口
├── pyproject.toml            # 项目配置 + 依赖声明
├── uv.lock                   # 依赖锁定文件
└── .env.example              # 环境变量模板

Key Architecture — 三层分层模式（每个 App 独立）:
  API层 (api.py)     → 路由定义、参数校验、响应序列化
  Service层 (services.py) → 业务逻辑、数据校验、事务控制
  Model层 (models.py)  → Django ORM、数据持久化

所有 App 共享 common/ 中的基类。
新增模块: uv run python scaffold_app.py <name>
API Entry: config.urls → NinjaAPI instance → apps/xxx/api.router
API Docs: /api/docs (Swagger), /api/ (Health)
```

---

## Rules

- **Investigate first:** Never speculate about code you have not read. Read files and `rg` for usages before making claims
- **Scope to the request:** Do what is asked; nothing more
- **Verify before done:** Re-check each requirement. Run tests and lint (`ruff`, `mypy`, `pytest`)
- **File discipline:** Edit existing files in place. Do not create new files unless required
- **Safety:** Ask before destructive actions (deleting files/branches, force pushes)
- **Efficiency:** Parallelize independent tool calls; serialize dependent ones
- **Tools:** Use `rg` not grep, `fd` not find
- **Three-layer rule:** API layer must never directly access ORM — always go through Service layer. Service layer must never handle HTTP request/response directly
- **Type annotations:** All functions/methods must have complete type annotations (enforced by strict mypy)
- **Docstrings:** Use Google-style docstrings for all public functions
- **Import order:** standard library → third-party → Django → local apps
- **Line length:** 88 chars (enforced by ruff formatter)

---

## Memory Bank

This project uses CLAUDE-*.md files to retain context across sessions:

| File | Read When |
|------|-----------|
| `CLAUDE-activeContext.md` | Session start — current state and goals |
| `CLAUDE-patterns.md` | Before implementing — code patterns |
| `CLAUDE-decisions.md` | Before design choices — architecture ADRs |
| `CLAUDE-troubleshooting.md` | When debugging — known issues and fixes |

All optional — check existence first. Update after significant work.

## Context Layers

| Layer | Location | Loads | Survives Reset |
|-------|----------|-------|----------------|
| Core rules | This file | Always | No |
| Auto memory | `memory/MEMORY.md` | Always (first 200 lines) | Yes |
| Memory bank | CLAUDE-*.md | On demand | No — mirrored to auto memory |
