# ============================================
# Stage 1: Builder — 安装依赖
# ============================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# 优先拷贝依赖声明，利用 Docker 层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 拷贝项目源码，安装项目本身
COPY . .
RUN uv sync --frozen --no-dev

# ============================================
# Stage 2: Runtime — 最小化运行镜像
# ============================================
FROM python:3.12-slim-bookworm

WORKDIR /app

# 安装系统级运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 阶段拷贝 .venv
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 拷贝应用代码
COPY . .

# 收集静态文件
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# 默认使用 gunicorn 启动（可通过 CMD 覆盖）
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4"]
