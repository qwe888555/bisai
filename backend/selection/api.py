"""django-ninja API for the selection-scoring and publicity modules.

The business logic still lives in ``selection/services/``.  This module only
handles routing, request parsing, the unified response envelope and error
translation.  Keeping the services independent makes them reusable by the main
project (DRF, Ninja or plain Django views).
"""

import logging
from typing import Any, Optional, Union

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from ninja import Body, NinjaAPI, Router, Schema
from ninja.errors import ValidationError
from ninja.renderers import JSONRenderer

from .auth import SelectionAuth, main_auth_available
from .common import (
    BusinessError,
    fail,
    ok,
    parse_event_id,
    parse_int,
    parse_page,
)
from .enums import GroupType, RoundType
from .services import excel as excel_service
from .services import publicity as publicity_service
from .services import results as result_service
from .services import scores as score_service
from .services import templates as template_service
from .services.current_user import get_current_user_id

logger = logging.getLogger("selection")


class ResponseEnvelope(Schema):
    """Supported envelopes.

    Standalone: ``{code: 0, message: "ok", data}``.
    Main app:  ``{code: "OK", message: "成功", data, requestId}``.
    """

    code: Union[int, str] = 0
    message: str = "ok"
    data: Any = None
    requestId: str | None = None


class UTF8JSONRenderer(JSONRenderer):
    json_dumps_params = {"ensure_ascii": False}


api = NinjaAPI(
    title="省级中小学足球系统 · 成员6接口",
    version="1.0.0",
    description="遴选评分（模块6.2）与报名公示（模块7.3）",
    urls_namespace="selection",
    renderer=UTF8JSONRenderer(),
)
selection_auth = SelectionAuth()

# 所有管理接口默认继承此鉴权；公开的门户公示接口在装饰器里显式 auth=None。
router = Router(auth=selection_auth)
api.add_router("", router)

# 并入主程序时登记权限点；独立运行时 apps.common 不存在，保留原有无登录演示模式。
try:  # pragma: no cover - depends on deployment layout
    from apps.common import permissions as main_permissions
except Exception:  # pragma: no cover - standalone deployment
    main_permissions = None

_ADMIN_ONLY_OPERATIONS = (
    "selectionTemplateList",
    "selectionTemplateCreate",
    "selectionTemplateGet",
    "selectionTemplateUpdate",
    "selectionScoreList",
    "selectionScoreCreate",
    "selectionScoreSummary",
    "selectionScoreExport",
    "selectionScoreGet",
    "selectionScoreUpdate",
    "selectionResultGenerate",
    "selectionResultLatest",
    "selectionResultBatches",
    "selectionResultExport",
)
_PUBLICITY_MANAGE_OPERATIONS = (
    "selectionPublicityPublish",
    "selectionPublicityManage",
    "selectionPublicityManageGet",
    "selectionPublicityBatch",
    "selectionPublicityResultBatch",
    "selectionPublicityCancel",
)

if main_permissions is not None:  # pragma: no cover - registration side effect
    for _operation in _ADMIN_ONLY_OPERATIONS:
        main_permissions.register_operation(
            _operation, (main_permissions.ADMIN,)
        )
    for _operation in _PUBLICITY_MANAGE_OPERATIONS:
        main_permissions.register_operation(
            _operation,
            (
                main_permissions.ADMIN,
                main_permissions.PROVINCE_REVIEWER,
            ),
        )
    main_permissions.register_operation(
        "selectionPublicityMine", main_permissions.ALL_ROLES
    )


def _require_operation(operation_id: str) -> Optional[object]:
    """Enforce the main project's role gate when running merged.

    The standalone/demo modes intentionally have no account context, so they
    keep working without roles.  ``auto`` and ``main`` are production modes and
    must go through ``apps.common.permissions.require``.
    """
    mode = getattr(settings, "SELECTION_AUTH_MODE", "auto")
    if mode in ("none", "demo", "header"):
        return None
    if main_permissions is None or not main_auth_available():
        return None
    return main_permissions.require(operation_id)


@api.exception_handler(BusinessError)
def handle_business_error(request: HttpRequest, exc: BusinessError):
    return JsonResponse(
        fail(exc.code, exc.message),
        status=exc.code,
        json_dumps_params={"ensure_ascii": False},
    )


@api.exception_handler(ValidationError)
def handle_validation_error(request: HttpRequest, exc: ValidationError):
    return JsonResponse(
        fail(400, "请求参数校验失败"),
        status=400,
        json_dumps_params={"ensure_ascii": False},
    )


@api.exception_handler(Exception)
def handle_unexpected_error(request: HttpRequest, exc: Exception):
    logger.exception("遴选评分接口发生未处理异常")
    return JsonResponse(
        fail(500, "服务器内部错误"),
        status=500,
        json_dumps_params={"ensure_ascii": False},
    )


try:  # Only present when selection is merged into the main backend.
    from apps.common.errors import ApiError as MainApiError
    from apps.common.errors import error_envelope as main_error_envelope
except Exception:  # pragma: no cover - standalone deployment
    MainApiError = None
    main_error_envelope = None


if MainApiError is not None:

    @api.exception_handler(MainApiError)
    def handle_main_api_error(request: HttpRequest, exc: MainApiError):
        """Main JWT/auth raises apps.common.errors.ApiError."""
        return JsonResponse(
            main_error_envelope(exc.code, exc.message, exc.data),
            status=exc.status,
            json_dumps_params={"ensure_ascii": False},
        )


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------
def _query(request, *names):
    for name in names:
        value = request.GET.get(name)
        if value is not None and value != "":
            return value
    return None


def _parse_score_query(request):
    event_id = parse_event_id(
        _query(request, "event_id", "eventId"),
        "event_id",
    )
    group_type = GroupType.parse(
        _query(request, "group_type", "groupType")
    )
    score_round = RoundType.parse(
        _query(request, "round", "score_round", "scoreRound")
    )
    return event_id, group_type, score_round


def _parse_result_query(request):
    event_id = parse_event_id(
        _query(request, "event_id", "eventId"),
        "event_id",
    )
    group_type = GroupType.parse(
        _query(request, "group_type", "groupType")
    )
    return event_id, group_type


def _publicity_event_id(request):
    return parse_event_id(
        _query(request, "event_id", "eventId"),
        "event_id",
    )


def _publicity_group_type(request):
    return _query(request, "group_type", "groupType")


def _publicity_type(request):
    return _query(request, "publicity_type", "publicityType", "type")


def _publicity_city(request):
    return _query(request, "city", "city_name", "cityName")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
@router.get("/templates/", response=ResponseEnvelope)
def list_templates(request):
    _require_operation("selectionTemplateList")
    status = None
    status_raw = _query(request, "status")
    if status_raw is not None:
        status = parse_int(status_raw, "status", min_value=0, max_value=1)

    return ok(
        template_service.list_templates(
            group_type=_query(request, "group_type", "groupType"),
            score_round=_query(
                request, "round", "score_round", "scoreRound"
            ),
            status=status,
        )
    )


@router.post("/templates/", response=ResponseEnvelope)
def create_template(request, payload: dict = Body(...)):
    _require_operation("selectionTemplateCreate")
    return ok(
        template_service.create_template(
            payload, get_current_user_id(request)
        )
    )


@router.get("/templates/{template_id}/", response=ResponseEnvelope)
def get_template(request, template_id: int):
    _require_operation("selectionTemplateGet")
    return ok(template_service.get_template_detail(template_id))


@router.put("/templates/{template_id}/", response=ResponseEnvelope)
def update_template(request, template_id: int, payload: dict = Body(...)):
    _require_operation("selectionTemplateUpdate")
    return ok(
        template_service.update_template(
            template_id, payload, get_current_user_id(request)
        )
    )


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------
@router.get("/scores/", response=ResponseEnvelope)
def list_scores(request):
    _require_operation("selectionScoreList")
    event_id, group_type, score_round = _parse_score_query(request)
    page, size = parse_page(request)
    return ok(
        score_service.list_scores(
            event_id,
            group_type,
            score_round,
            request.GET.get("keyword", ""),
            page,
            size,
        )
    )


@router.post("/scores/", response=ResponseEnvelope)
def create_score(request, payload: dict = Body(...)):
    _require_operation("selectionScoreCreate")
    return ok(
        score_service.create_score(
            payload, get_current_user_id(request)
        )
    )


@router.get("/scores/summary/", response=ResponseEnvelope)
def score_summary(request):
    _require_operation("selectionScoreSummary")
    event_id, group_type, score_round = _parse_score_query(request)
    return ok(score_service.summary(event_id, group_type, score_round))


@router.get("/scores/export/")
def score_export(request):
    _require_operation("selectionScoreExport")
    event_id, group_type, score_round = _parse_score_query(request)
    return excel_service.export_scores(
        event_id, group_type, score_round
    )


@router.get("/scores/{score_id}/", response=ResponseEnvelope)
def get_score(request, score_id: int):
    _require_operation("selectionScoreGet")
    return ok(score_service.get_score_detail(score_id))


@router.put("/scores/{score_id}/", response=ResponseEnvelope)
def update_score(request, score_id: int, payload: dict = Body(...)):
    _require_operation("selectionScoreUpdate")
    return ok(
        score_service.update_score(
            score_id, payload, get_current_user_id(request)
        )
    )


# ---------------------------------------------------------------------------
# Selection results
# ---------------------------------------------------------------------------
@router.post("/results/generate/", response=ResponseEnvelope)
def generate_result(request, payload: dict = Body(...)):
    _require_operation("selectionResultGenerate")
    return ok(
        result_service.generate_result(
            payload, get_current_user_id(request)
        )
    )


@router.get("/results/latest/", response=ResponseEnvelope)
def latest_result(request):
    _require_operation("selectionResultLatest")
    event_id, group_type = _parse_result_query(request)
    return ok(result_service.latest_result(event_id, group_type))


@router.get("/results/batches/", response=ResponseEnvelope)
def result_history(request):
    _require_operation("selectionResultBatches")
    event_id, group_type = _parse_result_query(request)
    limit = parse_int(
        _query(request, "limit"),
        "limit",
        required=False,
        default=20,
        min_value=1,
        max_value=100,
    )
    return ok(
        result_service.result_history(event_id, group_type, limit)
    )


@router.get("/results/export/")
def result_export(request):
    _require_operation("selectionResultExport")
    event_id, group_type = _parse_result_query(request)
    return excel_service.export_results(event_id, group_type)


# ---------------------------------------------------------------------------
# Registration publicity
# ---------------------------------------------------------------------------
@router.get("/publicity/", response=ResponseEnvelope, auth=None)
def list_publicity(request):
    event_id = _publicity_event_id(request)
    page, size = parse_page(request)
    return ok(
        publicity_service.list_public(
            event_id,
            group_type=_publicity_group_type(request),
            city=_publicity_city(request),
            publicity_type=_publicity_type(request),
            keyword=request.GET.get("keyword", ""),
            page=page,
            size=size,
        )
    )


@router.post("/publicity/", response=ResponseEnvelope)
def publish_publicity(request, payload: dict = Body(...)):
    _require_operation("selectionPublicityPublish")
    return ok(
        publicity_service.publish_registration(
            payload, get_current_user_id(request)
        )
    )


@router.get("/publicity/manage/", response=ResponseEnvelope)
def manage_publicity(request):
    _require_operation("selectionPublicityManage")
    event_id = _publicity_event_id(request)
    page, size = parse_page(request)
    return ok(
        publicity_service.list_management(
            event_id,
            group_type=_publicity_group_type(request),
            city=_publicity_city(request),
            publicity_type=_publicity_type(request),
            status=_query(request, "status"),
            keyword=request.GET.get("keyword", ""),
            page=page,
            size=size,
        )
    )


@router.get(
    "/publicity/manage/{record_id}/", response=ResponseEnvelope
)
def manage_publicity_detail(request, record_id: int):
    _require_operation("selectionPublicityManageGet")
    return ok(publicity_service.get_management_detail(record_id))


@router.get("/publicity/cities/", response=ResponseEnvelope, auth=None)
def publicity_cities(request):
    event_id = _publicity_event_id(request)
    return ok(
        publicity_service.list_cities(
            event_id,
            group_type=_publicity_group_type(request),
            publicity_type=_publicity_type(request),
        )
    )


@router.post("/publicity/batch/", response=ResponseEnvelope)
def batch_publish_publicity(request, payload: dict = Body(...)):
    _require_operation("selectionPublicityBatch")
    return ok(
        publicity_service.batch_publish(
            payload, get_current_user_id(request)
        )
    )


@router.post("/publicity/result-batch/", response=ResponseEnvelope)
def publish_result_publicity(request, payload: dict = Body(...)):
    """只发布某一现行结果批次的入围运动员，而不是全部已审报名。"""
    _require_operation("selectionPublicityResultBatch")
    return ok(
        publicity_service.publish_result_batch(
            payload, get_current_user_id(request)
        )
    )


@router.get("/publicity/mine/", response=ResponseEnvelope)
def my_publicity(request):
    """当前人员（运动员/教练）的全部公示记录，含已撤销。"""
    account = _require_operation("selectionPublicityMine")
    page, size = parse_page(request)
    return ok(
        publicity_service.list_my_publications(
            account, page=page, size=size
        )
    )


@router.get("/publicity/{record_id}/", response=ResponseEnvelope, auth=None)
def publicity_detail(request, record_id: int):
    return ok(publicity_service.get_public_detail(record_id))


@router.post(
    "/publicity/{record_id}/cancel/", response=ResponseEnvelope
)
def cancel_publicity(request, record_id: int, payload: dict | None = Body(None)):
    _require_operation("selectionPublicityCancel")
    return ok(
        publicity_service.cancel_publicity(
            record_id, payload or {}, get_current_user_id(request)
        )
    )


def register_routes(target_api, prefix="selection"):
    """Mount member6 on the main project's existing NinjaAPI.

    Call this in ``backend/config/urls.py`` **before** ``api.urls`` is read::

        from selection.api import register_routes
        register_routes(api)

    The main project keeps its own JWT auth, envelope and error handling; this
    function only adds the router and the module's ``BusinessError`` handler.
    """
    target_api.add_router(prefix, router, tags=["selection"])
    target_api.exception_handler(BusinessError)(handle_business_error)
    return router
