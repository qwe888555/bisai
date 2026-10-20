.PHONY: install dev migrate run test lint format typecheck security clean \
        precommit docker-up docker-down

# ─── 安装 ────────────────────────────────────────────────────────

install:          ## 安装生产+开发依赖
	uv sync --dev

install-all:      ## 安装全部可选依赖
	uv sync --dev --extra db --extra redis --extra storage

# ─── 开发 ────────────────────────────────────────────────────────

dev:              ## 启动开发服务器（热重载）
	uv run python manage.py runserver

migrate:          ## 运行数据库迁移
	uv run python manage.py migrate

makemigrations:   ## 创建新的迁移文件
	uv run python manage.py makemigrations

shell:            ## Django shell
	uv run python manage.py shell

admin:            ## 创建管理员用户
	uv run python manage.py createsuperuser

# ─── 测试 ────────────────────────────────────────────────────────

test:             ## 运行测试（带覆盖率）
	uv run pytest --cov --cov-report=term

test-ci:          ## CI 环境运行测试（XML 报告）
	uv run pytest --cov --cov-report=xml --cov-report=term

# ─── 代码质量 ────────────────────────────────────────────────────

lint:             ## 代码检查
	uv run ruff check .

format:           ## 代码格式化
	uv run ruff format .

format-check:     ## 检查代码格式
	uv run ruff format --check .

typecheck:        ## 类型检查
	uv run mypy .

security:         ## 安全扫描
	uv run bandit -c pyproject.toml -r .

check: lint format-check typecheck security test
	@echo "✅ 所有检查通过"

# ─── 清理 ────────────────────────────────────────────────────────

clean:            ## 清理缓存文件
	rm -rf .mypy_cache .ruff_cache .pytest_cache __pycache__
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

clean-db:         ## 重置数据库
	rm -f db.sqlite3
	$(MAKE) migrate

# ─── Docker ──────────────────────────────────────────────────────

docker-up:        ## 启动 Docker 开发环境
	docker compose up -d

docker-down:      ## 停止 Docker 环境
	docker compose down

docker-build:     ## 构建 Docker 镜像
	docker compose build

docker-logs:      ## 查看容器日志
	docker compose logs -f

# ─── Git 钩子 ────────────────────────────────────────────────────

precommit:        ## 运行 pre-commit 检查
	pre-commit run --all-files

precommit-install: ## 安装 pre-commit 钩子
	pre-commit install

# ─── 帮助 ────────────────────────────────────────────────────────

help:             ## 显示帮助信息
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
