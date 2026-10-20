"""
Django settings for django-ninja-template project.

Django + django-ninja RESTful API 后端模板项目配置
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 环境变量（项目根目录）
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-key-change-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() == "true"

_allowed_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts.split(",") if h.strip()]

# Application definition
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "ninja",
    "corsheaders",
]

LOCAL_APPS = [
    "api",
    "apps.example",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

# 请求追踪 ID（common.logging 不可用时跳过）
try:
    import common.logging  # noqa: F401

    MIDDLEWARE.insert(4, "common.logging.RequestIdMiddleware")
except ImportError:
    pass

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases
# 支持两种配置方式（二选一）：
#   方式 A: DATABASE_URL 单行 URL（方便，推荐）
#   方式 B: 逐项设置 DB_ENGINE / DB_NAME / DB_USER / ...（完全自定义）
import dj_database_url  # noqa: E402

_DATABASE_URL = os.environ.get("DATABASE_URL")

if _DATABASE_URL:
    # 方式 A：单行 DATABASE_URL
    DATABASES = {
        "default": dj_database_url.config(
            default=_DATABASE_URL,
            conn_max_age=int(os.environ.get("DATABASE_CONN_MAX_AGE", "600")),
        )
    }
else:
    # 方式 B：逐项手动配置
    _DB_ENGINE = os.environ.get("DB_ENGINE")
    if not _DB_ENGINE:
        raise RuntimeError(
            "❌ 未配置数据库！请在 .env 中设置：\n"
            "   方式 A: DATABASE_URL=sqlite:///db.sqlite3\n"
            "   方式 B: DB_ENGINE=django.db.backends.sqlite3\n"
            "           DB_NAME=db.sqlite3"
        )
    DATABASES = {
        "default": {
            "ENGINE": _DB_ENGINE,
            "NAME": os.environ.get("DB_NAME", ""),
            "USER": os.environ.get("DB_USER", ""),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": os.environ.get("DB_HOST", ""),
            "PORT": os.environ.get("DB_PORT", ""),
            "CONN_MAX_AGE": int(os.environ.get("DATABASE_CONN_MAX_AGE", "600")),
        }
    }

# Password validation (已禁用，因项目不使用 Django 内置认证系统)
AUTH_PASSWORD_VALIDATORS: list[dict[str, str]] = []

# Internationalization
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Media files
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# CORS 配置
_cors_origins = os.environ.get(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173",
)
CORS_ALLOWED_ORIGINS = [o.strip() for o in _cors_origins.split(",") if o.strip()]

# 如需允许所有来源（仅开发环境），取消下面注释
# CORS_ALLOW_ALL_ORIGINS = DEBUG

# ─── 日志配置 ──────────────────────────────────────────────────
# 日志格式由 LOG_FORMAT 环境变量控制:
#   "text" (默认) → 可读文本格式，适合开发
#   "json"       → JSON 格式，适合生产（ELK/Datadog 等）
#
# common.logging 不可用时（如新克隆项目缺少该文件），自动降级为基本配置。

_LOG_FORMAT = os.environ.get("LOG_FORMAT", "text")
_USE_JSON = _LOG_FORMAT == "json"

try:
    # 尝试启用增强日志（request_id 追踪 + JSON 格式）
    # 如果 common.logging 模块不存在，自动降级
    import common.logging  # noqa: F401

    _FILTERS = {
        "request_id": {
            "()": "common.logging.RequestIdFilter",
        },
    }

    _FORMATTERS = {
        "verbose": {
            "format": (
                "[%(request_id)s] %(levelname)s %(asctime)s"
                " %(module)s:%(lineno)d %(message)s"
            ),
        },
        "simple": {
            "format": "[%(request_id)s] %(levelname)s %(message)s",
        },
    }

    if _USE_JSON:
        _FORMATTERS["json"] = {
            "()": "common.logging.SimpleJsonFormatter",
        }

    _HANDLERS = {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "json" if _USE_JSON else "simple",
            "filters": ["request_id"],
        },
        "file": {
            "level": "DEBUG",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(BASE_DIR / "logs" / "django.log"),
            "maxBytes": 1024 * 1024 * 5,
            "backupCount": 5,
            "formatter": "json" if _USE_JSON else "verbose",
            "encoding": "utf-8",
            "filters": ["request_id"],
        },
        "error_file": {
            "level": "ERROR",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(BASE_DIR / "logs" / "error.log"),
            "maxBytes": 1024 * 1024 * 5,
            "backupCount": 5,
            "formatter": "json" if _USE_JSON else "verbose",
            "encoding": "utf-8",
            "filters": ["request_id"],
        },
    }
except ImportError:
    # 降级: 基本日志配置（无 request_id、无 JSON、无 error 独立文件）
    _FILTERS = {}
    _FORMATTERS = {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s",
        },
        "simple": {
            "format": "%(levelname)s %(message)s",
        },
    }
    _HANDLERS = {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "file": {
            "level": "DEBUG",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(BASE_DIR / "logs" / "django.log"),
            "maxBytes": 1024 * 1024 * 5,
            "backupCount": 5,
            "formatter": "verbose",
            "encoding": "utf-8",
        },
    }

_common_handlers = ["console", "file"] if not DEBUG else ["console"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": _FILTERS,
    "formatters": _FORMATTERS,
    "handlers": _HANDLERS,
    "root": {
        "handlers": _common_handlers,
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": _common_handlers,
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": _common_handlers
            + (["error_file"] if _HANDLERS.get("error_file") else []),
            "level": "ERROR",
            "propagate": False,
        },
        "apps": {
            "handlers": _common_handlers,
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
    },
}

# 安全头部（生产环境建议通过反向代理启用）
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"

# Ninja API 配置（django-ninja 支持的配置项）
NINJA_PAGINATION_PER_PAGE = 20
