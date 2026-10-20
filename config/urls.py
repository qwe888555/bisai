"""URL 配置文件.

集中管理 URL 路由、NinjaAPI 实例化、认证后端、异常处理器。
"""

import os

from django.conf import settings
from django.contrib import admin
from django.http import HttpRequest, HttpResponse
from django.urls import path
from ninja import NinjaAPI
from ninja.errors import HttpError, ValidationError
from ninja.security import HttpBearer

from api.api import router as demo_router
from api.exceptions import ItemNotFoundError
from apps.example.api import router as example_router
from common.base.exceptions import BusinessError

# ─── 认证后端 ────────────────────────────────────────────────────


class AuthBearer(HttpBearer):
    """API Token 认证（从环境变量读取 API_TOKEN）.

    用于管理端接口的简单 bearer token 认证。
    生产环境请设置强密码并定期轮换。
    """

    def authenticate(self, request: HttpRequest, token: str) -> str | None:
        expected = os.environ.get("API_TOKEN", "dev-token")
        if token == expected:
            return token
        return None


# ─── NinjaAPI 实例 ───────────────────────────────────────────────

api = NinjaAPI(
    title="Django Ninja Template API",
    description="基于 Django + django-ninja 的 RESTful API 后端模板",
    version="1.0.0",
    csrf=False,
    # 生产环境自动关闭 API 文档
    docs_url="/docs/" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
    openapi_extra={
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                },
                "apiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                },
            }
        }
    },
)

api.add_router("/demo/", demo_router)
api.add_router("/example/", example_router)


# ─── 异常处理器 ──────────────────────────────────────────────────


@api.exception_handler(ItemNotFoundError)
def on_item_not_found(request: HttpRequest, exc: ItemNotFoundError) -> HttpResponse:
    """商品不存在 → 404."""
    return api.create_response(
        request,
        {"detail": exc.message, "code": exc.code},
        status=404,
    )


@api.exception_handler(BusinessError)
def on_business_error(request: HttpRequest, exc: BusinessError) -> HttpResponse:
    """业务异常 → 422."""
    return api.create_response(
        request,
        {"detail": exc.message, "code": exc.code},
        status=422,
    )


@api.exception_handler(HttpError)
def on_http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
    """HTTP 层错误 → 透传状态码."""
    return api.create_response(
        request,
        {"detail": str(exc), "code": "http_error"},
        status=exc.status_code,
    )


@api.exception_handler(ValidationError)
def on_validation_error(request: HttpRequest, exc: ValidationError) -> HttpResponse:
    """请求参数校验失败 → 422."""
    return api.create_response(
        request,
        {"detail": str(exc), "code": "validation_error"},
        status=422,
    )


@api.exception_handler(Exception)
def on_generic_error(request: HttpRequest, exc: Exception) -> HttpResponse:
    """未预期的服务器错误 → 500（生产环境不暴露详情）."""
    detail = str(exc) if settings.DEBUG else "服务器内部错误"
    return api.create_response(
        request,
        {"detail": detail, "code": "internal_error"},
        status=500,
    )


# ─── 健康检查 ────────────────────────────────────────────────────


@api.get("/", tags=["Health"], summary="服务健康检查")
def health_check(request: HttpRequest) -> dict[str, str]:
    """返回服务健康状态."""
    return {"status": "ok", "service": "django-ninja-template"}


# ─── URL 路由 ────────────────────────────────────────────────────

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
