"""Tiny CORS middleware for the standalone member6 project.

The main backend already ships ``apps.common.middleware.CorsMiddleware``; when
this app is copied into ``backend/`` that middleware handles CORS and this one
is not installed (main settings are used instead).  Keeping it here means the
standalone project can be called directly from the Vite dev server without
pulling in an extra dependency.
"""

from django.conf import settings
from django.http import HttpResponse

DEFAULT_HEADERS = (
    "Authorization, Content-Type, Idempotency-Key, X-Request-Id, X-User-Id"
)


def _allowed_origins():
    origins = getattr(settings, "SELECTION_CORS_ORIGINS", None)
    if isinstance(origins, str):
        origins = [item.strip() for item in origins.split(",") if item.strip()]
    if origins:
        return set(origins)
    return {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    }


class SelectionCorsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get("Origin", "")
        if request.method == "OPTIONS" and request.headers.get(
            "Access-Control-Request-Method"
        ):
            response = HttpResponse(status=200)
            self._apply_headers(response, origin, preflight=True)
            return response

        response = self.get_response(request)
        self._apply_headers(response, origin, preflight=False)
        return response

    def _apply_headers(self, response, origin, preflight=False):
        if not origin:
            return
        allow_all = bool(getattr(settings, "SELECTION_CORS_ALLOW_ALL", False))
        if not allow_all and origin not in _allowed_origins():
            return
        response["Access-Control-Allow-Origin"] = origin
        response["Vary"] = "Origin"
        response["Access-Control-Allow-Credentials"] = "true"
        if preflight:
            response["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            )
            response["Access-Control-Allow-Headers"] = (
                getattr(settings, "SELECTION_CORS_ALLOW_HEADERS", "")
                or DEFAULT_HEADERS
            )
            response["Access-Control-Max-Age"] = "86400"
        response["Access-Control-Expose-Headers"] = "X-Request-Id"
