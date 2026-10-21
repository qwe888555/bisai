"""NinjaAPI 实例与全局协议。

放在中立模块里（不 import 任何业务应用），避免与 apps.* 形成循环引用；
各应用的路由在 config/urls.py 里统一 add_router。
"""

from __future__ import annotations

from ninja import NinjaAPI

from .errors import register_exception_handlers

api = NinjaAPI(
    title="省级中小学足球赛事系统 API",
    version="1.0.0",
    description="契约来源：docs/后端接口文档.md；统一前缀 /api/v1。",
    urls_namespace="football-api",
)

register_exception_handlers(api)
