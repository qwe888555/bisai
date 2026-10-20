"""Gunicorn 生产配置文件.

使用方式:
    gunicorn config.wsgi:application -c gunicorn.conf.py
"""

import multiprocessing
import os

# 绑定地址
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")

# 工作进程
_workers_str: str | None = os.environ.get("GUNICORN_WORKERS")
workers: int
if _workers_str:
    workers = int(_workers_str)
else:
    workers = multiprocessing.cpu_count() * 2 + 1

# 工作模式（sync / uvicorn.workers.UvicornWorker）
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "sync")

# 超时设置
timeout: int = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout: int = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))

# 日志
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "-")
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# 其他
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "100"))
