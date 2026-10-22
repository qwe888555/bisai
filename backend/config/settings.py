"""Django 全局配置（Django + django-ninja 基线）。

约定：
1. 所有可变项走环境变量（.env → os.environ），本地与容器用同一套代码；
2. 默认 SQLite（零依赖启动）+ 进程内缓存，配 DATABASE_URL / REDIS_URL 即切生产组件；
3. 接口协议（统一信封、错误码、分页、鉴权）由 apps/common 实现并锁死，
   各业务模块不得自行改协议；settings 里只放常量。
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """优先读 backend/.env；没装 python-dotenv 也不影响运行。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(BASE_DIR / ".env")


_load_dotenv()


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def env_list(key: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(key, default).split(",") if item.strip()]


# ---------------------------------------------------------------- 基础

SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-change-me-please-32bytes-min")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "*")
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "ninja",
    "apps.common",
    "apps.accounts",
    "apps.notices",
    "apps.attachments",
    "apps.verification",
    "apps.policyqa",
    "apps.enrollments",
]

# 中间件顺序不可随意调整：requestId 必须最外层，审计/限流在其内。
MIDDLEWARE = [
    "apps.common.middleware.RequestIdMiddleware",
    "apps.common.middleware.CorsMiddleware",
    "apps.common.middleware.AuditMiddleware",
    "apps.common.middleware.RateLimitMiddleware",
]

# 只提供 JSON 接口；TEMPLATES 仅用于 django-ninja 自带的 swagger/redoc 文档页
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

# django.contrib.staticfiles 需要这些设置（仅文档页与容器内 collectstatic 用到）
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ---------------------------------------------------------------- 数据库

def _database_config(url: str) -> dict:
    """sqlite:///./data/football.db 或 mysql+pymysql://user:pass@host:3306/db。"""
    if url.startswith("sqlite"):
        path = url.split("///")[-1] or ":memory:"
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": path}
    parsed = urlparse(url)
    return {
        "ENGINE": "django.db.backends.mysql",
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "127.0.0.1",
        "PORT": parsed.port or 3306,
        "OPTIONS": {"charset": "utf8mb4"},
    }


DATABASES = {"default": _database_config(env("DATABASE_URL", "sqlite:///./data/football.db"))}

# ---------------------------------------------------------------- 缓存 / 令牌撤销

REDIS_URL = env("REDIS_URL", "")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    # 单机可用；多实例部署必须配 Redis，否则令牌撤销与限流不共享
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "football-local",
        }
    }

# ---------------------------------------------------------------- 国际化与时间

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = env("APP_TIMEZONE", "Asia/Shanghai")
USE_I18N = False
USE_TZ = True  # 契约要求时间带时区，统一 UTC 存储

# ---------------------------------------------------------------- 接口协议常量

# 分页：page 从 1 开始，pageSize 默认 20、上限 100
DEFAULT_PAGE_SIZE = env_int("DEFAULT_PAGE_SIZE", 20)

# 限流：登录/注册按 IP，政策问答按小时（超限 429 RATE_LIMITED）
THROTTLE_RATES: dict[str, str] = {
    "login": env("THROTTLE_LOGIN", "30/min"),
    "registration": env("THROTTLE_REGISTRATION", "20/min"),
    "policy_question": env("THROTTLE_POLICY_QUESTION", "20/hour"),
}

# ---------------------------------------------------------------- 业务配置（供 apps.common.conf 读取）

JWT_SECRET = env("JWT_SECRET", SECRET_KEY)
ACCESS_TOKEN_TTL_MINUTES = env_int("ACCESS_TOKEN_TTL_MINUTES", 120)
# 身份证/手机号 AES-GCM 主密钥：32 字节 base64；留空则由 JWT_SECRET 派生（仅开发便利）
CRYPTO_KEY = env("CRYPTO_KEY", "")

CORS_ORIGINS = env_list(
    "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
)

STORAGE_BACKEND = env("STORAGE_BACKEND", "local")
STORAGE_LOCAL_ROOT = env("STORAGE_LOCAL_ROOT", "./data/files")
MAX_FILE_SIZE_MB = env_int("MAX_FILE_SIZE_MB", 10)
MAX_BATCH_FILES = env_int("MAX_BATCH_FILES", 100)
MAX_BATCH_TOTAL_MB = env_int("MAX_BATCH_TOTAL_MB", 50)
FILE_TICKET_TTL_SECONDS = env_int("FILE_TICKET_TTL_SECONDS", 300)
MINIO_ENDPOINT = env("MINIO_ENDPOINT", "127.0.0.1:9000")
MINIO_ACCESS_KEY = env("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = env("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = env("MINIO_BUCKET", "football")
MINIO_SECURE = env_bool("MINIO_SECURE", False)

# 人脸核验：MANUAL = 现场拍照 + 人工确认（默认降级）；FACE_COMPARE = 接第三方比对
FACE_MODE = env("FACE_MODE", "MANUAL")
FACE_API_BASE = env("FACE_API_BASE", "")
FACE_API_KEY = env("FACE_API_KEY", "")
FACE_THRESHOLD = env_float("FACE_THRESHOLD", 0.75)

# 政策问答（项目唯一 AI，可一键关闭）
AI_ENABLED = env_bool("AI_ENABLED", False)
AI_BASE_URL = env("AI_BASE_URL", "")
AI_API_KEY = env("AI_API_KEY", "")
AI_MODEL = env("AI_MODEL", "")
AI_TIMEOUT_SECONDS = env_int("AI_TIMEOUT_SECONDS", 15)
AI_MAX_QUESTION_CHARS = env_int("AI_MAX_QUESTION_CHARS", 1000)
AI_RATE_LIMIT_PER_HOUR = env_int("AI_RATE_LIMIT_PER_HOUR", 20)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"sensitive": {"()": "apps.common.middleware.SensitiveLogFilter"}},
    "formatters": {
        "standard": {"format": "[%(asctime)s] %(levelname)s %(name)s %(message)s"}
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["sensitive"],
            "formatter": "standard",
        }
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
