# 运行镜像：Python 3.10 + Django 4.2 + gunicorn
FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖清单单独拷贝，利用 Docker 层缓存
COPY requirements.txt .
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

COPY . .

# 静态文件（仅文档页需要）
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# 生产用 gunicorn；本地开发直接 python manage.py runserver
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "60"]
