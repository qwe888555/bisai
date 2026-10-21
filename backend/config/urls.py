"""项目 URL 总入口：业务接口统一挂在 /api/v1 下。

与《后端接口文档.md》第 2 节一致：前缀 /api/v1；/public/* 为公众免登录接口。
交互文档 /api/v1/docs，OpenAPI 契约 /api/v1/openapi.json（Apifox 直接导入）。
"""

from django.urls import path, re_path

from apps.accounts.api import router as accounts_router
from apps.events.api import router as events_router
from apps.attachments.api import router as attachments_router
from apps.common.api import api_not_found
from apps.common.api import router as common_router
from apps.common.ninja_api import api
from apps.notices.api import router as notices_router
from apps.policyqa.api import router as policyqa_router
from apps.verification.api import router as verification_router

# 路由必须在读取 api.urls 之前注册完毕
api.add_router("", common_router, tags=["system"])
api.add_router("", accounts_router, tags=["accounts"])
api.add_router("", notices_router, tags=["notices"])
api.add_router("", attachments_router, tags=["files"])
api.add_router("", verification_router, tags=["identity"])
api.add_router("", policyqa_router, tags=["policy"])
api.add_router("", events_router, tags=["events"])

urlpatterns = [
    path("api/v1/", api.urls),
    # 未匹配的 /api/v1 路径也返回契约信封，而不是 Django 的 HTML 404
    re_path(r"^api/v1/.*$", api_not_found),
]

