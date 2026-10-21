"""公共接口：健康检查（免鉴权，供部署探活与本地自检）。"""

from __future__ import annotations

from django.http import JsonResponse
from ninja import Router

from .envelope import ok
from .errors import NOT_FOUND, error_envelope
from .schemas import EnvelopeOut

router = Router(tags=["system"])


@router.get("/health", auth=None, response=EnvelopeOut, summary="健康检查")
def health(request):
    return ok({"status": "UP"})


def api_not_found(request):
    """兜底：/api/v1 下未匹配的路径也返回契约信封（而不是 Django 的 HTML 404）。"""
    return JsonResponse(error_envelope(NOT_FOUND), status=404)
