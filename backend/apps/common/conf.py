"""业务配置读取口。

settings 顶层刻意保留 JWT_SECRET / STORAGE_* / AI_* 这类业务常量，
本模块统一暴露，业务代码只 import 这里，不直接摸 settings，
方便将来把配置搬到数据库或配置中心。
"""

from __future__ import annotations

from django.conf import settings as _s

# ------------------------------------------------------------------ 鉴权
JWT_SECRET: str = getattr(_s, "JWT_SECRET", _s.SECRET_KEY)
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL_MINUTES: int = getattr(_s, "ACCESS_TOKEN_TTL_MINUTES", 120)

# ------------------------------------------------------------------ 加密
CRYPTO_KEY: str = getattr(_s, "CRYPTO_KEY", "")

# ------------------------------------------------------------------ 跨域
CORS_ORIGINS: list[str] = list(getattr(_s, "CORS_ORIGINS", []))

# ------------------------------------------------------------------ 文件
STORAGE_BACKEND: str = getattr(_s, "STORAGE_BACKEND", "local")
STORAGE_LOCAL_ROOT: str = getattr(_s, "STORAGE_LOCAL_ROOT", "./data/files")
MAX_FILE_SIZE_MB: int = getattr(_s, "MAX_FILE_SIZE_MB", 10)
MAX_BATCH_FILES: int = getattr(_s, "MAX_BATCH_FILES", 100)
MAX_BATCH_TOTAL_MB: int = getattr(_s, "MAX_BATCH_TOTAL_MB", 50)
FILE_TICKET_TTL_SECONDS: int = getattr(_s, "FILE_TICKET_TTL_SECONDS", 300)
MINIO_ENDPOINT: str = getattr(_s, "MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY: str = getattr(_s, "MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY: str = getattr(_s, "MINIO_SECRET_KEY", "")
MINIO_BUCKET: str = getattr(_s, "MINIO_BUCKET", "football")
MINIO_SECURE: bool = getattr(_s, "MINIO_SECURE", False)

# ------------------------------------------------------------------ 人脸核验
FACE_MODE: str = getattr(_s, "FACE_MODE", "MANUAL")
FACE_API_BASE: str = getattr(_s, "FACE_API_BASE", "")
FACE_API_KEY: str = getattr(_s, "FACE_API_KEY", "")
FACE_THRESHOLD: float = getattr(_s, "FACE_THRESHOLD", 0.75)

# ------------------------------------------------------------------ 政策问答
AI_ENABLED: bool = getattr(_s, "AI_ENABLED", False)
AI_BASE_URL: str = getattr(_s, "AI_BASE_URL", "")
AI_API_KEY: str = getattr(_s, "AI_API_KEY", "")
AI_MODEL: str = getattr(_s, "AI_MODEL", "")
AI_TIMEOUT_SECONDS: int = getattr(_s, "AI_TIMEOUT_SECONDS", 15)
AI_MAX_QUESTION_CHARS: int = getattr(_s, "AI_MAX_QUESTION_CHARS", 1000)
AI_RATE_LIMIT_PER_HOUR: int = getattr(_s, "AI_RATE_LIMIT_PER_HOUR", 20)

# ------------------------------------------------------------------ 分页
DEFAULT_PAGE_SIZE: int = getattr(_s, "DEFAULT_PAGE_SIZE", 20)
MAX_PAGE_SIZE = 100
