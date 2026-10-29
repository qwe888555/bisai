"""django-ninja API for the selection-scoring and publicity modules.

The business logic still lives in ``selection/services/``.  This module only
handles routing, request parsing, the unified response envelope and error
translation.  Keeping the services independent makes them reusable by the main
project (DRF, Ninja or plain Django views).
"""

import logging
from typing import Any, Union

from django.http import HttpRequest, JsonResponse
from ninja import Body, NinjaAPI, Router, Schema
from ninja.errors import ValidationError
from ninja.renderers import JSONRenderer

from .auth import SelectionAuth
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
    from apps.common import permissions as main_permissions
    from apps.common.errors import ApiError as MainApiError
    from apps.common.errors import error_envelope as main_error_envelope
except Exception:  # pragma: no cover - standalone deployment
    main_permissions = None
    MainApiError = None
    main_error_envelope = None

if main_permissions is not None:
    # 管理接口权限点登记（2026-10-29 成员4专项修复）。
    # 角色映射依据 docs/后端接口文档.md 6.20 公示 / 6.21 遴选：
    # - 遴选评分与结果全部接口：ADMIN；
    # - 公示管理接口（publicationList/Create/Cancel）：ADMIN / PROVINCE_REVIEWER。
    # 未在文档单独定义的端点按同类操作归并：批量发布→publicationCreate、
    # 公示管理详情→publicationList、结果查询/导出→selectionResults/selectionExport。
    _P = main_permissions
    _P.register_operation("selectionRuleList", (_P.ADMIN,))
    _P.register_operation("selectionRuleCreate", (_P.ADMIN,))
    _P.register_operation("selectionRuleGet", (_P.ADMIN,))
    _P.register_operation("selectionRuleUpdate", (_P.ADMIN,))
    _P.register_operation("selectionScoreList", (_P.ADMIN,))
    _P.register_operation("selectionScoreCreate", (_P.ADMIN,))
    _P.register_operation("selectionScoreGet", (_P.ADMIN,))
    _P.register_operation("selectionScoreUpdate", (_P.ADMIN,))
    _P.register_operation("selectionSummary", (_P.ADMIN,))
    _P.register_operation("selectionExport", (_P.ADMIN,))
    _P.register_operation("selectionGenerate", (_P.ADMIN,))
    _P.register_operation("selectionResults", (_P.ADMIN,))
    _P.register_operation("selectionResultGet", (_P.ADMIN,))
    _P.register_operation("publicationList", (_P.ADMIN, _P.PROVINCE_REVIEWER))
    _P.register_operation("publicationCreate", (_P.ADMIN, _P.PROVINCE_REVIEWER))
    _P.register_operation("publicationCancel", (_P.ADMIN, _P.PROVINCE_REVIEWER))


def _require_management(operation_id: str) -> None:
    """管理接口守卫：复用主项目"角色→权限点→登录态"校验，fail-closed。

    - 合并部署（SELECTION_AUTH_MODE=auto/main）：JWTAuth 已将账号写入线程
      上下文，main_permissions.require() 校验登录态、启用状态与角色；
      未登录 401 / 角色不符 403，错误经 handle_main_api_error 输出主项目
      统一错误信封；
    - 独立部署（无 apps.common）：fail-closed 401，匿名/演示模式不得触达
      管理面；公开门户接口（auth=None）不受影响；
    - 认证模式 none/demo/header 即使被误配，也因线程上下文无账号而 401，
      不依赖 SelectionAuth 的返回值放行。
    """
    if main_permissions is None:
        raise BusinessError(401, "管理接口需要接入主项目认证体系，独立部署不支持匿名管理")
    main_permissions.require(operation_id)


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
    _require_management("selectionRuleList")
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
    _require_management("selectionRuleCreate")
    return ok(
        template_service.create_template(
            payload, get_current_user_id(request)
        )
    )


@router.get("/templates/{template_id}/", response=ResponseEnvelope)
def get_template(request, template_id: int):
    _require_management("selectionRuleGet")
    return ok(template_service.get_template_detail(template_id))


@router.put("/templates/{template_id}/", response=ResponseEnvelope)
def update_template(request, template_id: int, payload: dict = Body(...)):
    _require_management("selectionRuleUpdate")
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
    _require_management("selectionScoreList")
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
    _require_management("selectionScoreCreate")
    return ok(
        score_service.create_score(
            payload, get_current_user_id(request)
        )
    )


@router.get("/scores/summary/", response=ResponseEnvelope)
def score_summary(request):
    _require_management("selectionSummary")
    event_id, group_type, score_round = _parse_score_query(request)
    return ok(score_service.summary(event_id, group_type, score_round))


@router.get("/scores/export/")
def score_export(request):
    _require_management("selectionExport")
    event_id, group_type, score_round = _parse_score_query(request)
    return excel_service.export_scores(
        event_id, group_type, score_round
    )


@router.get("/scores/{score_id}/", response=ResponseEnvelope)
def get_score(request, score_id: int):
    _require_management("selectionScoreGet")
    return ok(score_service.get_score_detail(score_id))


@router.put("/scores/{score_id}/", response=ResponseEnvelope)
def update_score(request, score_id: int, payload: dict = Body(...)):
    _require_management("selectionScoreUpdate")
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
    _require_management("selectionGenerate")
    return ok(
        result_service.generate_result(
            payload, get_current_user_id(request)
        )
    )


@router.get("/results/latest/", response=ResponseEnvelope)
def latest_result(request):
    _require_management("selectionResults")
    event_id, group_type = _parse_result_query(request)
    return ok(result_service.latest_result(event_id, group_type))


@router.get("/results/batches/", response=ResponseEnvelope)
def result_history(request):
    _require_management("selectionResults")
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
    _require_management("selectionExport")
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
    _require_management("publicationCreate")
    return ok(
        publicity_service.publish_registration(
            payload, get_current_user_id(request)
        )
    )


@router.get("/publicity/manage/", response=ResponseEnvelope)
def manage_publicity(request):
    _require_management("publicationList")
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
    _require_management("publicationList")
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
    _require_management("publicationCreate")
    return ok(
        publicity_service.batch_publish(
            payload, get_current_user_id(request)
        )
    )


@router.get("/publicity/{record_id}/", response=ResponseEnvelope, auth=None)
def publicity_detail(request, record_id: int):
    return ok(publicity_service.get_public_detail(record_id))


@router.post(
    "/publicity/{record_id}/cancel/", response=ResponseEnvelope
)
def cancel_publicity(request, record_id: int, payload: dict | None = Body(None)):
    _require_management("publicationCancel")
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
