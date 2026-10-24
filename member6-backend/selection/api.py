"""django-ninja API for the selection-scoring and publicity modules.

The business logic still lives in ``selection/services/``.  This module only
handles routing, request parsing, the unified response envelope and error
translation.  Keeping the services independent makes them reusable by the main
project (DRF, Ninja or plain Django views).
"""

import logging
from typing import Any

from django.http import HttpRequest, JsonResponse
from ninja import Body, NinjaAPI, Router, Schema
from ninja.errors import ValidationError
from ninja.renderers import JSONRenderer

from .common import BusinessError, fail, ok, parse_int, parse_page
from .enums import GroupType, RoundType
from .services import excel as excel_service
from .services import publicity as publicity_service
from .services import results as result_service
from .services import scores as score_service
from .services import templates as template_service
from .services.current_user import get_current_user_id

logger = logging.getLogger("selection")


class ResponseEnvelope(Schema):
    """Every JSON endpoint keeps the original unified envelope."""

    code: int = 0
    message: str = "ok"
    data: Any = None


class UTF8JSONRenderer(JSONRenderer):
    json_dumps_params = {"ensure_ascii": False}


api = NinjaAPI(
    title="省级中小学足球系统 · 成员6接口",
    version="1.0.0",
    description="遴选评分（模块6.2）与报名公示（模块7.3）",
    urls_namespace="selection",
    renderer=UTF8JSONRenderer(),
)
router = Router()
api.add_router("", router)


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
    event_id = parse_int(
        _query(request, "event_id", "eventId"),
        "event_id",
        required=True,
        min_value=1,
    )
    group_type = GroupType.parse(
        _query(request, "group_type", "groupType")
    )
    score_round = RoundType.parse(
        _query(request, "round", "score_round", "scoreRound")
    )
    return event_id, group_type, score_round


def _parse_result_query(request):
    event_id = parse_int(
        _query(request, "event_id", "eventId"),
        "event_id",
        required=True,
        min_value=1,
    )
    group_type = GroupType.parse(
        _query(request, "group_type", "groupType")
    )
    return event_id, group_type


def _publicity_event_id(request):
    return parse_int(
        _query(request, "event_id", "eventId"),
        "event_id",
        required=True,
        min_value=1,
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
    return ok(
        template_service.create_template(
            payload, get_current_user_id(request)
        )
    )


@router.get("/templates/{template_id}/", response=ResponseEnvelope)
def get_template(request, template_id: int):
    return ok(template_service.get_template_detail(template_id))


@router.put("/templates/{template_id}/", response=ResponseEnvelope)
def update_template(request, template_id: int, payload: dict = Body(...)):
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
    return ok(
        score_service.create_score(
            payload, get_current_user_id(request)
        )
    )


@router.get("/scores/summary/", response=ResponseEnvelope)
def score_summary(request):
    event_id, group_type, score_round = _parse_score_query(request)
    return ok(score_service.summary(event_id, group_type, score_round))


@router.get("/scores/export/")
def score_export(request):
    event_id, group_type, score_round = _parse_score_query(request)
    return excel_service.export_scores(
        event_id, group_type, score_round
    )


@router.get("/scores/{score_id}/", response=ResponseEnvelope)
def get_score(request, score_id: int):
    return ok(score_service.get_score_detail(score_id))


@router.put("/scores/{score_id}/", response=ResponseEnvelope)
def update_score(request, score_id: int, payload: dict = Body(...)):
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
    return ok(
        result_service.generate_result(
            payload, get_current_user_id(request)
        )
    )


@router.get("/results/latest/", response=ResponseEnvelope)
def latest_result(request):
    event_id, group_type = _parse_result_query(request)
    return ok(result_service.latest_result(event_id, group_type))


@router.get("/results/batches/", response=ResponseEnvelope)
def result_history(request):
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
    event_id, group_type = _parse_result_query(request)
    return excel_service.export_results(event_id, group_type)


# ---------------------------------------------------------------------------
# Registration publicity
# ---------------------------------------------------------------------------
@router.get("/publicity/", response=ResponseEnvelope)
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
    return ok(
        publicity_service.publish_registration(
            payload, get_current_user_id(request)
        )
    )


@router.get("/publicity/manage/", response=ResponseEnvelope)
def manage_publicity(request):
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
    return ok(publicity_service.get_management_detail(record_id))


@router.get("/publicity/cities/", response=ResponseEnvelope)
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
    return ok(
        publicity_service.batch_publish(
            payload, get_current_user_id(request)
        )
    )


@router.get("/publicity/{record_id}/", response=ResponseEnvelope)
def publicity_detail(request, record_id: int):
    return ok(publicity_service.get_public_detail(record_id))


@router.post(
    "/publicity/{record_id}/cancel/", response=ResponseEnvelope
)
def cancel_publicity(request, record_id: int, payload: dict | None = Body(None)):
    return ok(
        publicity_service.cancel_publicity(
            record_id, payload or {}, get_current_user_id(request)
        )
    )
