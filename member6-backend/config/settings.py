"""
Django settings for the football selection-scoring module.

This project can run on its own (SQLite + demo data) or be merged into the
group's main Django project.  Integration points are controlled by the
SELECTION_* settings at the bottom of this file.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Basic Django settings
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-only-change-me-football-selection",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "selection",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Default: SQLite, zero configuration for local demo / tests.
# MySQL example:
#   DB_ENGINE=mysql DB_NAME=football_system DB_USER=root \
#   DB_PASSWORD=your_password python manage.py migrate
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 20},
    }
}

if os.environ.get("DB_ENGINE", "").lower() == "mysql":
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("DB_NAME", "football_system"),
        "USER": os.environ.get("DB_USER", "root"),
        "PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("DB_PORT", "3306"),
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# i18n / time zone
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# API URLs are always written with a trailing slash.  Disable automatic
# slash-appending so a POST without the slash returns a clear 404 instead of
# a redirect that loses the request body.
APPEND_SLASH = False

# ---------------------------------------------------------------------------
# Selection scoring module settings
# ---------------------------------------------------------------------------
# demo  : use the built-in DemoSelectionArchiveClient (45 fake candidates)
# none  : do not create a demo client; the main project must provide one
SELECTION_ARCHIVE_MODE = os.environ.get("SELECTION_ARCHIVE_MODE", "demo")

# Dotted path of a callable that returns a SelectionArchiveClient instance.
# Example: "apps.archives.services.ArchiveClient"
SELECTION_ARCHIVE_CLIENT = os.environ.get("SELECTION_ARCHIVE_CLIENT", "")

# header : read user id from the X-User-Id request header (demo only)
# django : use request.user.id (set this after merging into the main project)
# none   : the main project must provide SELECTION_CURRENT_USER_PROVIDER
SELECTION_CURRENT_USER_MODE = os.environ.get("SELECTION_CURRENT_USER_MODE", "header")

# Dotted path of a callable: (request) -> int user id.
# Example: "apps.accounts.utils.get_current_user_id"
SELECTION_CURRENT_USER_PROVIDER = os.environ.get(
    "SELECTION_CURRENT_USER_PROVIDER", ""
)

# D1 确认后修改这些默认值；也可以在生成结果时按请求传参覆盖。
SELECTION_DEFAULT_QUOTA = int(os.environ.get("SELECTION_DEFAULT_QUOTA", "11"))
SELECTION_DEFAULT_INITIAL_WEIGHT = os.environ.get(
    "SELECTION_DEFAULT_INITIAL_WEIGHT", "0.4"
)
SELECTION_DEFAULT_FINAL_WEIGHT = os.environ.get(
    "SELECTION_DEFAULT_FINAL_WEIGHT", "0.6"
)
SELECTION_TIE_BREAK_DIMENSION_CODE = os.environ.get(
    "SELECTION_TIE_BREAK_DIMENSION_CODE", "TECH"
)

# ---------------------------------------------------------------------------
# Registration publicity (module 7.3, T6-04)
# ---------------------------------------------------------------------------
# demo : use the built-in DemoPublicitySourceClient (45 athletes, 15 coaches,
#        9 teams, event_id=1)
# none : the main project must provide SELECTION_PUBLICITY_SOURCE_CLIENT
SELECTION_PUBLICITY_SOURCE_MODE = os.environ.get(
    "SELECTION_PUBLICITY_SOURCE_MODE", "demo"
)

# Dotted path of a callable that returns a PublicitySourceClient instance.
# Example: "apps.registrations.services.RegistrationPublicitySource"
SELECTION_PUBLICITY_SOURCE_CLIENT = os.environ.get(
    "SELECTION_PUBLICITY_SOURCE_CLIENT", ""
)

# Fields copied from member 4's registration archive into the public snapshot.
# The raw ID-card field "id_card" is transformed to id_card_masked and is
# never stored as submitted.  Member 4 can replace this dict directly.
SELECTION_PUBLICITY_FIELD_WHITELIST = {
    "ATHLETE": (
        "name",
        "gender",
        "id_card",
        "team_name",
        "school_name",
        "position",
        "jersey_number",
    ),
    "COACH": (
        "name",
        "gender",
        "id_card",
        "team_name",
        "school_name",
        "coach_role",
    ),
    "TEAM": (
        "team_name",
        "school_name",
        "coach_name",
    ),
}

# CORS (only needed when the frontend runs on a different origin):
#   pip install django-cors-headers
#   add "corsheaders" to INSTALLED_APPS and
#   "corsheaders.middleware.CorsMiddleware" before CommonMiddleware
#   CORS_ALLOW_ALL_ORIGINS = DEBUG
# Never allow all origins in production.

# Logging: keep API errors visible in the console during development.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "selection": {
            "handlers": ["console"],
            "level": os.environ.get("SELECTION_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}
