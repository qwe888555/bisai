"""报名域业务逻辑与事务。

视图层零 ORM、零信封拼接；本模块不接触任何 HTTP 对象（request 由 api 层在
幂等/审计处使用），只做业务规则、事务与序列化。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.common.crypto import encrypt_text, mask_phone, normalize_phone
from apps.common.errors import (
    BATCH_LIMIT_EXCEEDED,
    BUSINESS_VALIDATION_FAILED,
    DATA_SCOPE_DENIED,
    DUPLICATE_RESOURCE,
    FORBIDDEN,
    INVALID_ARGUMENT,
    NOT_FOUND,
    STATE_CONFLICT,
    ApiError,
)
from apps.common.ids import new_id
from apps.common.versioning import bump, check_version

from . import precheck, scope
from .clients import (
    event_config_client,
    file_client,
    message_client,
    person_client,
    team_client,
)
from .models import (
    ACTIVE_STATUSES,
    EDITABLE_STATUSES,
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_PENDING_CITY,
    STATUS_PENDING_PROVINCE,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
    Enrollment,
    EnrollmentAttachment,
    EnrollmentSnapshot,
    RosterItem,
)

# ---------------------------------------------------------------- 序列化


def _latest_snapshot(enrollment: Enrollment) -> Optional[EnrollmentSnapshot]:
    return (
        EnrollmentSnapshot.objects.filter(enrollment_id=enrollment.pk)
        .order_by("-submission_no", "-created_at")
        .first()
    )


def _latest_review_reason(enrollment: Enrollment) -> str:
    record = (
        enrollment.reviews.exclude(reason="")
        .order_by("-created_at", "-id")
        .first()
    )
    return record.reason if record else ""


def to_roster(item: RosterItem) -> dict:
    """契约 RosterItem；qualification_status 暂不出（TODO 确认项#3）。"""
    return {
        "id": item.pk,
        "athleteId": item.athlete_id,
        "jerseyNumber": item.jersey_number,
        "position": item.position,
        "photoFileId": item.photo_file_id,
        "studentStatusFileIds": list(item.student_status_file_ids or []),
        "athleteName": item.athlete_name,
        "maskedIdNumber": item.masked_id_number,
        "version": int(item.version or 0),
        "createdAt": item.created_at,
        "updatedAt": item.updated_at,
    }


def to_attachment(attachment: EnrollmentAttachment) -> dict:
    return {
        "id": attachment.pk,
        "materialType": attachment.material_type,
        "fileId": attachment.file_id,
        "athleteId": attachment.athlete_id or None,
        "version": int(attachment.version or 0),
        "createdAt": attachment.created_at,
        "updatedAt": attachment.updated_at,
    }


def to_review(record) -> dict:
    return {
        "id": record.pk,
        "enrollmentId": record.enrollment_id,
        "submissionNo": record.submission_no,
        "stage": record.stage,
        "action": record.action,
        "fromStatus": record.from_status,
        "toStatus": record.to_status,
        "reason": record.reason,
        "operatorAccountId": record.operator_account_id,
        "snapshotId": record.snapshot_id,
        "version": int(record.version or 0),
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
    }


def _event_display_names(enrollment: Enrollment) -> tuple[str, str]:
    """赛事/组别名：优先取成员5 events 主档，联调期回退赛事配置客户端。"""
    from apps.events import display as event_display

    event = event_display.event_name(enrollment.event_id, "")
    group = event_display.group_name(enrollment.group_id, "")
    if event and group:
        return event, group
    from .clients import mock_enabled

    if mock_enabled():
        try:
            event = event or event_config_client.get_event(enrollment.event_id).get("name") or ""
        except Exception:  # noqa: BLE001 - 展示名缺失不应阻断报名列表
            event = ""
        try:
            group = group or event_config_client.get_group(
                enrollment.event_id, enrollment.group_id
            ).get("name") or ""
        except Exception:  # noqa: BLE001
            group = ""
    return event or enrollment.event_id, group or enrollment.group_id


def _org_display_name(org_id: str) -> str:
    if not org_id:
        return ""
    from apps.accounts.models import Organization

    return Organization.objects.filter(pk=org_id).values_list("name", flat=True).first() or ""


def _local_team_display(team_id: str) -> dict:
    """成员4 的 Team 主档只补展示字段，不改变 mock 客户端返回的 orgId/城市范围。"""
    from apps.teams.models import Team

    raw = str(team_id or "")
    if not raw:
        return {}
    queryset = Team.objects.filter(is_deleted=False).select_related("school", "region")
    team = queryset.filter(pk=int(raw)).first() if raw.isdigit() else None
    if team is None:
        team = queryset.filter(team_no=raw).first()
    if team is None:
        return {}
    return {
        "name": team.name,
        "schoolName": team.school.name if team.school_id else "",
        "cityName": team.region.name if team.region_id else "",
    }


def _resolve_player_count(enrollment: Enrollment, player_count: Optional[int]) -> int:
    if player_count is not None:
        return int(player_count)
    annotated = getattr(enrollment, "player_count", None)
    if annotated is not None:
        return int(annotated)
    return enrollment.players.count()


def to_summary(
    enrollment: Enrollment, player_count: Optional[int] = None
) -> dict:
    team = team_client.get_team(enrollment.team_id)
    local_team = _local_team_display(enrollment.team_id)
    event_name, group_name = _event_display_names(enrollment)
    snapshot = _latest_snapshot(enrollment)
    city_org_id = team.get("cityOrgId") or enrollment.city_org_id or ""
    return {
        "id": enrollment.pk,
        "eventId": enrollment.event_id,
        "eventName": event_name,
        "groupId": enrollment.group_id,
        "groupName": group_name,
        "teamId": enrollment.team_id,
        "teamName": local_team.get("name") or team.get("name"),
        "subjectType": enrollment.subject_type,
        "schoolOrgId": team.get("orgId") or None,
        "schoolName": local_team.get("schoolName") or team.get("schoolName") or "",
        "cityOrgId": city_org_id or None,
        "cityName": _org_display_name(city_org_id)
        or local_team.get("cityName")
        or team.get("cityName")
        or "",
        "status": enrollment.status,
        "submissionNo": int(enrollment.submission_no or 0),
        "contactName": enrollment.contact_name,
        "maskedContactPhone": enrollment.contact_phone_masked,
        "playerCount": _resolve_player_count(enrollment, player_count),
        "editable": enrollment.editable,
        "snapshotId": snapshot.pk if snapshot else None,
        "version": int(enrollment.version or 0),
        "createdAt": enrollment.created_at,
        "updatedAt": enrollment.updated_at,
    }


def to_detail(enrollment: Enrollment) -> dict:
    players = [to_roster(item) for item in enrollment.players.all()]
    data = to_summary(enrollment, player_count=len(players))
    data["players"] = players
    data["attachments"] = [
        to_attachment(item) for item in enrollment.attachments.all()
    ]
    data["latestReviewReason"] = _latest_review_reason(enrollment)
    return data


def to_snapshot(snapshot: EnrollmentSnapshot) -> dict:
    payload = snapshot.payload or {}
    summary = dict(payload.get("enrollment") or {})
    summary["snapshotId"] = snapshot.pk
    return {
        "id": snapshot.pk,
        "enrollmentId": snapshot.enrollment_id,
        "submissionNo": snapshot.submission_no,
        "submittedAt": snapshot.submitted_at,
        "enrollment": summary,
        "team": payload.get("team") or {},
        "players": payload.get("players") or [],
        "coachIds": payload.get("coachIds") or [],
        "attachments": payload.get("attachments") or [],
    }


# ---------------------------------------------------------------- 通用校验


def _duplicate_active(
    team_id: str, group_id: str, exclude_id: Optional[str] = None
) -> Optional[Enrollment]:
    queryset = Enrollment.objects.filter(
        team_id=team_id, group_id=group_id, status__in=ACTIVE_STATUSES
    )
    if exclude_id:
        queryset = queryset.exclude(pk=exclude_id)
    return queryset.first()


def _ensure_not_duplicate(
    team_id: str, group_id: str, exclude_id: Optional[str] = None
) -> None:
    duplicate = _duplicate_active(team_id, group_id, exclude_id=exclude_id)
    if duplicate is not None:
        raise ApiError(
            DUPLICATE_RESOURCE,
            "该球队在该组别已有进行中的报名单",
            [{"field": "groupId", "reason": f"重复报名：{duplicate.pk}"}],
        )


def _ensure_editable(enrollment: Enrollment) -> None:
    if enrollment.status not in EDITABLE_STATUSES:
        raise ApiError(
            STATE_CONFLICT,
            "当前状态不允许修改，仅草稿/已驳回/已撤回可编辑",
            [{"field": "status", "reason": enrollment.status}],
        )


def _validate_enrollment_files(
    actor, enrollment: Enrollment, file_ids: list[str], category: str
) -> None:
    for file_id in file_ids:
        if not file_id:
            continue
        record = file_client.get_record(actor, file_id)
        if record.owner_type != "ENROLLMENT" or record.owner_id != enrollment.pk:
            raise ApiError(
                INVALID_ARGUMENT,
                "文件不属于本报名对象",
                [{"field": "fileId", "reason": "归属不匹配"}],
            )
        if category and record.category != category:
            raise ApiError(
                INVALID_ARGUMENT,
                "文件类别与用途不匹配",
                [{"field": "fileId", "reason": f"需要 {category}"}],
            )


def _ensure_jersey_unique(
    enrollment: Enrollment, jersey: Optional[int], exclude_id: Optional[str] = None
) -> None:
    """默认规则（确认项#10）：球衣号码同队唯一。"""
    if jersey is None:
        return
    queryset = RosterItem.objects.filter(
        enrollment__event_id=enrollment.event_id,
        enrollment__team_id=enrollment.team_id,
        jersey_number=jersey,
        enrollment__status__in=ACTIVE_STATUSES,
    ).exclude(qualification_status="EXCLUDED")
    if exclude_id:
        queryset = queryset.exclude(pk=exclude_id)
    if queryset.exists():
        raise ApiError(
            DUPLICATE_RESOURCE,
            f"球衣号码 {jersey} 在该队已被占用",
            [{"field": "jerseyNumber", "reason": "同队唯一"}],
        )


def _locked_enrollment(actor, enrollment_id: str) -> Enrollment:
    enrollment = (
        Enrollment.objects.select_for_update().filter(pk=enrollment_id).first()
    )
    if enrollment is None:
        from apps.common.errors import NOT_FOUND

        raise ApiError(NOT_FOUND)
    scope.ensure_enrollment_access(actor, enrollment)
    return enrollment


# ---------------------------------------------------------------- 报名单


def list_enrollments(actor, filters: Optional[dict] = None):
    queryset = scope.scoped_queryset(actor, Enrollment.objects.all())
    queryset = scope.apply_filters(queryset, filters)
    # 列表响应补 playerCount（审核工作台"球员"列），避免逐行 N+1
    return queryset.annotate(
        player_count=Count("players", distinct=True)
    ).order_by("-created_at", "id")


def get_enrollment(actor, enrollment_id: str) -> dict:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    return to_detail(enrollment)


def create_enrollment(actor, payload) -> dict:
    scope.subject_type_allowed(actor, payload.subjectType)
    # 关联主档取不到时明确报错，不 or {} 兜底
    team = team_client.get_team(payload.teamId)
    event_config_client.get_event(payload.eventId)
    event_config_client.get_group(payload.eventId, payload.groupId)
    scope.ensure_team_in_scope(actor, team)
    precheck.ensure_window_open(payload.eventId)
    _ensure_not_duplicate(payload.teamId, payload.groupId)

    enrollment = Enrollment.objects.create(
        id=new_id("enr"),
        event_id=payload.eventId,
        group_id=payload.groupId,
        team_id=payload.teamId,
        subject_type=payload.subjectType,
        status=STATUS_DRAFT,
        contact_name=payload.contactName,
        contact_phone_cipher=encrypt_text(normalize_phone(payload.contactPhone)),
        contact_phone_masked=mask_phone(payload.contactPhone),
        city_org_id=team.get("cityOrgId") or "",
        remarks=payload.remarks or "",
        created_by=actor.pk,
    )
    return to_detail(enrollment)


def update_enrollment(actor, enrollment_id: str, payload) -> dict:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    scope.ensure_enrollment_creator(actor, enrollment)
    _ensure_editable(enrollment)
    check_version(enrollment, payload.version)

    if (
        payload.eventId != enrollment.event_id
        or payload.teamId != enrollment.team_id
        or payload.subjectType != enrollment.subject_type
    ):
        raise ApiError(
            INVALID_ARGUMENT,
            "eventId/teamId/subjectType 不可通过修改迁移，请另建报名",
            [{"field": "eventId", "reason": "不可变更业务对象"}],
        )

    team = team_client.get_team(payload.teamId)
    scope.subject_type_allowed(actor, payload.subjectType)
    scope.ensure_team_in_scope(actor, team)
    precheck.ensure_window_open(enrollment.event_id)
    if payload.groupId != enrollment.group_id:
        _ensure_not_duplicate(enrollment.team_id, payload.groupId, exclude_id=enrollment.pk)

    enrollment.group_id = payload.groupId
    enrollment.contact_name = payload.contactName
    enrollment.contact_phone_cipher = encrypt_text(
        normalize_phone(payload.contactPhone)
    )
    enrollment.contact_phone_masked = mask_phone(payload.contactPhone)
    enrollment.remarks = payload.remarks or ""
    bump(enrollment)
    enrollment.save()
    return to_detail(enrollment)


def delete_enrollment(actor, enrollment_id: str, version: int) -> tuple[str, int]:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    scope.ensure_enrollment_creator(actor, enrollment)
    _ensure_editable(enrollment)
    check_version(enrollment, version)
    if (
        enrollment.status == STATUS_APPROVED
        or EnrollmentSnapshot.objects.filter(enrollment_id=enrollment.pk).exists()
        or enrollment.reviews.filter(action="APPROVE").exists()
    ):
        raise ApiError(
            STATE_CONFLICT,
            "存在受保护历史关联（已审批/快照），不能删除",
            [{"field": "enrollmentId", "reason": "受保护历史关联"}],
        )
    enrollment.deleted_at = timezone.now()
    bump(enrollment)
    enrollment.save()
    return enrollment.pk, enrollment.version


# ---------------------------------------------------------------- 名单


def list_roster(actor, enrollment_id: str, athlete_name: Optional[str] = None):
    enrollment = scope.get_enrollment(actor, enrollment_id)
    queryset = enrollment.players.all()
    name = (athlete_name or "").strip()
    if name:
        queryset = queryset.filter(athlete_name__icontains=name)
    return queryset.order_by("created_at", "id")


def get_roster(actor, enrollment_id: str, roster_id: str) -> dict:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    item = RosterItem.objects.filter(pk=roster_id, enrollment_id=enrollment.pk).first()
    if item is None:
        from apps.common.errors import NOT_FOUND

        raise ApiError(NOT_FOUND)
    return to_roster(item)


def _get_roster_item(enrollment: Enrollment, roster_id: str) -> RosterItem:
    item = RosterItem.objects.filter(pk=roster_id, enrollment_id=enrollment.pk).first()
    if item is None:
        from apps.common.errors import NOT_FOUND

        raise ApiError(NOT_FOUND)
    return item


def create_roster(actor, enrollment_id: str, payload) -> dict:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    _ensure_editable(enrollment)
    precheck.ensure_window_open(enrollment.event_id)
    person = person_client.get_person(payload.athleteId)
    if RosterItem.objects.filter(
        enrollment_id=enrollment.pk, athlete_id=payload.athleteId
    ).exists():
        raise ApiError(
            DUPLICATE_RESOURCE,
            "同一报名单内不可重复添加同一运动员",
            [{"field": "athleteId", "reason": "已存在"}],
        )
    _ensure_jersey_unique(enrollment, payload.jerseyNumber)
    _validate_enrollment_files(
        actor, enrollment, list(payload.studentStatusFileIds or []), "STUDENT_STATUS"
    )
    if payload.photoFileId:
        _validate_enrollment_files(
            actor, enrollment, [payload.photoFileId], "ROSTER_PHOTO"
        )
    item = RosterItem.objects.create(
        id=new_id("rst"),
        enrollment_id=enrollment.pk,
        athlete_id=payload.athleteId,
        athlete_name=person.get("name") or "",
        masked_id_number=person.get("maskedIdNumber") or "",
        jersey_number=payload.jerseyNumber,
        position=payload.position or "",
        photo_file_id=payload.photoFileId or "",
        student_status_file_ids=list(payload.studentStatusFileIds or []),
    )
    return to_roster(item)


def update_roster(actor, enrollment_id: str, roster_id: str, payload) -> dict:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    _ensure_editable(enrollment)
    precheck.ensure_window_open(enrollment.event_id)
    item = _get_roster_item(enrollment, roster_id)
    check_version(item, payload.version)

    person = person_client.get_person(payload.athleteId)
    duplicate = RosterItem.objects.filter(
        enrollment_id=enrollment.pk, athlete_id=payload.athleteId
    ).exclude(pk=item.pk)
    if duplicate.exists():
        raise ApiError(
            DUPLICATE_RESOURCE,
            "同一报名单内不可重复添加同一运动员",
            [{"field": "athleteId", "reason": "已存在"}],
        )
    _ensure_jersey_unique(enrollment, payload.jerseyNumber, exclude_id=item.pk)
    _validate_enrollment_files(
        actor, enrollment, list(payload.studentStatusFileIds or []), "STUDENT_STATUS"
    )
    if payload.photoFileId:
        _validate_enrollment_files(
            actor, enrollment, [payload.photoFileId], "ROSTER_PHOTO"
        )

    item.athlete_id = payload.athleteId
    item.athlete_name = person.get("name") or ""
    item.masked_id_number = person.get("maskedIdNumber") or ""
    item.jersey_number = payload.jerseyNumber
    item.position = payload.position or ""
    item.photo_file_id = payload.photoFileId or ""
    item.student_status_file_ids = list(payload.studentStatusFileIds or [])
    bump(item)
    item.save()
    return to_roster(item)


def delete_roster(
    actor, enrollment_id: str, roster_id: str, version: int
) -> tuple[str, int]:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    _ensure_editable(enrollment)
    item = _get_roster_item(enrollment, roster_id)
    check_version(item, version)
    item.deleted_at = timezone.now()
    bump(item)
    item.save()
    return item.pk, item.version


# ---------------------------------------------------------------- 预检 / 提交 / 撤回 / 审核


def precheck_enrollment(actor, enrollment_id: str, version: int) -> dict:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    check_version(enrollment, version)
    return precheck.run_precheck(enrollment)


def build_snapshot_payload(enrollment: Enrollment) -> dict:
    team = team_client.get_team(enrollment.team_id)
    players: list[dict] = []
    for item in enrollment.players.all():
        person = person_client.get_person(item.athlete_id)
        players.append(
            {
                **to_roster(item),
                # TODO(确认项#4): 契约 Snapshot 缺性别/出生日期/学籍号等字段，
                # 默认从人员主档取脱敏副本冻入 payload，供 PDF/Word 渲染。
                "gender": person.get("gender"),
                "birthDate": person.get("birthDate"),
                "maskedStudentNumber": person.get("maskedStudentNumber"),
            }
        )
    coach_ids = list(team.get("coachIds") or [])
    coaches = [team_client.get_coach(coach_id) for coach_id in coach_ids]
    payload = {
        "enrollment": to_summary(enrollment, player_count=len(players)),
        "team": team,
        "players": players,
        "coachIds": coach_ids,
        "coaches": coaches,
        "attachments": [
            to_attachment(item) for item in enrollment.attachments.all()
        ],
    }
    # 快照落 JSONField：datetime 等先转 ISO 字符串，冻结后不再变化
    return json.loads(json.dumps(payload, cls=DjangoJSONEncoder))


def _submitted_status(subject_type: str) -> str:
    if subject_type == "SCHOOL_TEAM":
        return STATUS_PENDING_CITY
    # 默认契约（确认项#5）：UNIVERSITY / CITY 直省审
    return STATUS_PENDING_PROVINCE


@transaction.atomic
def submit_enrollment(actor, enrollment_id: str, version: int) -> dict:
    enrollment = _locked_enrollment(actor, enrollment_id)
    scope.ensure_enrollment_creator(actor, enrollment)
    check_version(enrollment, version)
    if enrollment.status not in EDITABLE_STATUSES:
        raise ApiError(
            STATE_CONFLICT,
            "已提交或已通过的报名不能重复提交",
            [{"field": "status", "reason": enrollment.status}],
        )
    precheck.ensure_window_open(enrollment.event_id)
    _ensure_not_duplicate(
        enrollment.team_id, enrollment.group_id, exclude_id=enrollment.pk
    )

    result = precheck.run_precheck(enrollment)
    if not result["passed"]:
        errors = [i for i in result["issues"] if i["severity"] == precheck.ERROR]
        raise ApiError(BUSINESS_VALIDATION_FAILED, "规则预检未通过", errors)

    from_status = enrollment.status
    enrollment.submission_no = int(enrollment.submission_no or 0) + 1
    # 归属回填：提交时以球队主档最新 cityOrgId 为准，修历史空值，
    # 保证市州端按 cityOrgId 过滤能查到本条报名
    refreshed_city = (team_client.get_team(enrollment.team_id) or {}).get(
        "cityOrgId"
    ) or ""
    if refreshed_city and refreshed_city != enrollment.city_org_id:
        enrollment.city_org_id = refreshed_city
    # 快照记录提交后的状态，避免从快照看到“草稿/已驳回”
    enrollment.status = _submitted_status(enrollment.subject_type)
    payload = build_snapshot_payload(enrollment)
    snapshot = EnrollmentSnapshot.objects.create(
        id=new_id("snp"),
        enrollment_id=enrollment.pk,
        submission_no=enrollment.submission_no,
        submitted_at=timezone.now(),
        payload=payload,
    )
    bump(enrollment)
    enrollment.save()

    enrollment.reviews.create(
        id=new_id("rev"),
        submission_no=enrollment.submission_no,
        stage="SUBMISSION",
        action="SUBMIT",
        from_status=from_status,
        to_status=enrollment.status,
        operator_account_id=actor.pk,
        snapshot_id=snapshot.pk,
    )
    message_client.notify_enrollment(enrollment, "enrollment.submitted")
    return to_detail(enrollment)


@transaction.atomic
def withdraw_enrollment(actor, enrollment_id: str, version: int, reason: str) -> dict:
    enrollment = _locked_enrollment(actor, enrollment_id)
    check_version(enrollment, version)
    if enrollment.status not in {STATUS_PENDING_CITY, STATUS_PENDING_PROVINCE}:
        raise ApiError(
            STATE_CONFLICT,
            "仅待审核报名可撤回，已通过不可撤回",
            [{"field": "status", "reason": enrollment.status}],
        )
    scope.ensure_enrollment_creator(actor, enrollment)
    from_status = enrollment.status
    stage = "CITY" if from_status == STATUS_PENDING_CITY else "PROVINCE"
    snapshot = _latest_snapshot(enrollment)
    enrollment.status = STATUS_WITHDRAWN
    bump(enrollment)
    enrollment.save()
    enrollment.reviews.create(
        id=new_id("rev"),
        submission_no=enrollment.submission_no,
        stage=stage,
        action="WITHDRAW",
        from_status=from_status,
        to_status=STATUS_WITHDRAWN,
        reason=reason,
        operator_account_id=actor.pk,
        snapshot_id=snapshot.pk if snapshot else "",
    )
    message_client.notify_enrollment(enrollment, "enrollment.withdrawn")
    return to_detail(enrollment)


def _review_stage(actor, enrollment: Enrollment) -> tuple[str, str]:
    """审核层级由登录角色 + 当前状态推导，绝不接收客户端 stage。"""
    roles = scope.role_set(actor)
    if enrollment.status == STATUS_PENDING_CITY:
        if not (roles & {"CITY", "ADMIN"}):
            raise ApiError(FORBIDDEN, "当前状态仅市州端可审核")
        return "CITY", STATUS_PENDING_PROVINCE
    if enrollment.status == STATUS_PENDING_PROVINCE:
        if not (roles & {"PROVINCE_REVIEWER", "ADMIN"}):
            raise ApiError(FORBIDDEN, "当前状态仅省级可审核")
        return "PROVINCE", STATUS_APPROVED
    raise ApiError(
        STATE_CONFLICT,
        "仅待审核报名可审核",
        [{"field": "status", "reason": enrollment.status}],
    )


@transaction.atomic
def review_enrollment(actor, enrollment_id: str, payload) -> dict:
    enrollment = _locked_enrollment(actor, enrollment_id)
    check_version(enrollment, payload.version)
    stage, approve_target = _review_stage(actor, enrollment)
    if payload.decision in {"REJECT", "RETURN_FOR_CORRECTION"} and not payload.reason:
        raise ApiError(
            INVALID_ARGUMENT,
            "驳回/退回补正必须填写原因",
            [{"field": "reason", "reason": "必填"}],
        )

    from_status = enrollment.status
    if payload.decision == "APPROVE":
        target_status = approve_target
        if stage == "PROVINCE":
            # 省级终审：绑定具体名单版本 + 双轨合规（8.1）
            result = precheck.run_precheck(enrollment)
            if not result["passed"]:
                errors = [
                    i for i in result["issues"] if i["severity"] == precheck.ERROR
                ]
                raise ApiError(BUSINESS_VALIDATION_FAILED, "预检未通过，不能终审通过", errors)
            track_issues = precheck.double_track_issues(enrollment)
            if track_issues:
                raise ApiError(
                    BUSINESS_VALIDATION_FAILED, "双轨资格校验未通过", track_issues
                )
        action = "APPROVE"
    elif payload.decision == "REJECT":
        target_status = STATUS_REJECTED
        action = "REJECT"
    else:
        target_status = STATUS_DRAFT
        action = "RETURN_FOR_CORRECTION"

    snapshot = _latest_snapshot(enrollment)
    enrollment.status = target_status
    bump(enrollment)
    enrollment.save()
    enrollment.reviews.create(
        id=new_id("rev"),
        submission_no=enrollment.submission_no,
        stage=stage,
        action=action,
        from_status=from_status,
        to_status=target_status,
        reason=payload.reason or "",
        operator_account_id=actor.pk,
        snapshot_id=snapshot.pk if snapshot else "",
    )
    message_client.notify_enrollment(enrollment, f"enrollment.review.{action}")
    return to_detail(enrollment)


def list_reviews(actor, enrollment_id: str):
    enrollment = scope.get_enrollment(actor, enrollment_id)
    return enrollment.reviews.all().order_by("-created_at", "-id")


def get_snapshot(actor, enrollment_id: str, snapshot_id: str) -> dict:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    snapshot = EnrollmentSnapshot.objects.filter(
        pk=snapshot_id, enrollment_id=enrollment.pk
    ).first()
    if snapshot is None:
        from apps.common.errors import NOT_FOUND

        raise ApiError(NOT_FOUND)
    return to_snapshot(snapshot)


def latest_snapshot(enrollment: Enrollment) -> Optional[EnrollmentSnapshot]:
    return _latest_snapshot(enrollment)


# ---------------------------------------------------------------- 材料关联


def list_attachments(actor, enrollment_id: str):
    enrollment = scope.get_enrollment(actor, enrollment_id)
    return enrollment.attachments.all().order_by("created_at", "id")


def create_attachment(actor, enrollment_id: str, payload) -> dict:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    _ensure_editable(enrollment)
    precheck.ensure_window_open(enrollment.event_id)
    record = file_client.get_record(actor, payload.fileId)
    if record.owner_type != "ENROLLMENT" or record.owner_id != enrollment.pk:
        raise ApiError(
            INVALID_ARGUMENT,
            "fileId 必须属于本报名对象",
            [{"field": "fileId", "reason": "归属不匹配"}],
        )
    if record.category != payload.materialType:
        raise ApiError(
            INVALID_ARGUMENT,
            "文件类别与材料类型不匹配",
            [{"field": "fileId", "reason": f"需要 {payload.materialType}"}],
        )
    if payload.athleteId and not RosterItem.objects.filter(
        enrollment_id=enrollment.pk, athlete_id=payload.athleteId
    ).exists():
        raise ApiError(
            INVALID_ARGUMENT,
            "关联运动员不在本报名名单内",
            [{"field": "athleteId", "reason": "不存在"}],
        )
    if EnrollmentAttachment.objects.filter(
        enrollment_id=enrollment.pk, file_id=payload.fileId
    ).exists():
        raise ApiError(DUPLICATE_RESOURCE, "该文件已关联到本报名")

    attachment = EnrollmentAttachment.objects.create(
        id=new_id("att"),
        enrollment_id=enrollment.pk,
        material_type=payload.materialType,
        file_id=payload.fileId,
        athlete_id=payload.athleteId or "",
    )
    return to_attachment(attachment)


def delete_attachment(
    actor, enrollment_id: str, attachment_id: str, version: int
) -> tuple[str, int]:
    enrollment = scope.get_enrollment(actor, enrollment_id)
    _ensure_editable(enrollment)
    attachment = EnrollmentAttachment.objects.filter(
        pk=attachment_id, enrollment_id=enrollment.pk
    ).first()
    if attachment is None:
        from apps.common.errors import NOT_FOUND

        raise ApiError(NOT_FOUND)
    check_version(attachment, version)
    attachment.deleted_at = timezone.now()
    bump(attachment)
    attachment.save()
    return attachment.pk, attachment.version


# ---------------------------------------------------------------- 表单配置 / 导出

# 固定字段元数据（不做动态表单设计器）
FORM_FIELDS: tuple[dict, ...] = (
    {"key": "eventId", "label": "赛事", "required": True, "component": "TEXT"},
    {"key": "groupId", "label": "组别", "required": True, "component": "SELECT"},
    {"key": "teamId", "label": "球队", "required": True, "component": "TEXT"},
    {"key": "subjectType", "label": "报名主体类型", "required": True, "component": "SELECT"},
    {"key": "contactName", "label": "联系人", "required": True, "component": "TEXT"},
    {"key": "contactPhone", "label": "联系电话", "required": True, "component": "TEXT"},
    {"key": "remarks", "label": "备注", "required": False, "component": "TEXT"},
    {"key": "athleteId", "label": "运动员", "required": True, "component": "TEXT"},
    {"key": "jerseyNumber", "label": "球衣号码", "required": False, "component": "NUMBER"},
    {
        "key": "position",
        "label": "场上位置",
        "required": False,
        "component": "SELECT",
        "dictionaryType": "POSITION",
    },
    {"key": "photoFileId", "label": "运动员照片", "required": False, "component": "FILE"},
    {"key": "studentStatusFileIds", "label": "学籍证明", "required": False, "component": "FILE"},
)


def form_config(actor, event_id: str, group_id: Optional[str], subject_type: str) -> dict:
    """报名表字段与材料要求；主体类型必须匹配账号实际报送权限。"""
    scope.subject_type_allowed(actor, subject_type)
    event_config_client.get_event(event_id)
    if group_id:
        event_config_client.get_group(event_id, group_id)
    rules = event_config_client.get_group_rules(event_id, group_id or "")
    return {
        "eventId": event_id,
        "groupId": group_id or None,
        "subjectType": subject_type,
        "fields": [dict(item) for item in FORM_FIELDS],
        "requiredMaterialTypes": list(rules.get("requiredMaterialTypes") or []),
        "submissionRoute": event_config_client.get_submission_route(subject_type),
    }


def resolve_snapshot(
    actor, enrollment_id: str, snapshot_id: Optional[str] = None
) -> tuple[Enrollment, Optional[EnrollmentSnapshot]]:
    """文档渲染取数：指定快照优先，否则取最新快照；草稿没有快照时返回 None。"""
    enrollment = scope.get_enrollment(actor, enrollment_id)
    if snapshot_id:
        snapshot = EnrollmentSnapshot.objects.filter(
            pk=snapshot_id, enrollment_id=enrollment.pk
        ).first()
        if snapshot is None:
            raise ApiError(NOT_FOUND)
        return enrollment, snapshot
    return enrollment, _latest_snapshot(enrollment)


EXPORT_LIMIT = 100


def export_enrollments(actor, payload) -> list[Enrollment]:
    """批量导出取数：筛选只能是权限的子集；SELECTED 必须全部命中，整批拒绝。"""
    filters = payload.filters.model_dump()
    queryset = scope.allowed_export_queryset(actor, filters)
    if payload.scope == "SELECTED":
        ids = list(dict.fromkeys(payload.enrollmentIds or []))
        if not ids:
            raise ApiError(
                INVALID_ARGUMENT,
                "scope=SELECTED 时必须提供 enrollmentIds",
                [{"field": "enrollmentIds", "reason": "不能为空"}],
            )
        if len(ids) > EXPORT_LIMIT:
            raise ApiError(
                BATCH_LIMIT_EXCEEDED,
                f"单次最多导出 {EXPORT_LIMIT} 个报名单，请缩小选择范围",
                [{"field": "enrollmentIds", "reason": f"最多 {EXPORT_LIMIT} 项"}],
            )
        found = {item.pk: item for item in queryset.filter(pk__in=ids)}
        missing = [enrollment_id for enrollment_id in ids if enrollment_id not in found]
        if missing:
            raise ApiError(
                DATA_SCOPE_DENIED,
                "存在超出筛选或数据范围的报名单，整批拒绝",
                [{"field": "enrollmentIds", "reason": f"不可导出：{missing[:5]}"}],
            )
        return [found[enrollment_id] for enrollment_id in ids]

    result = list(queryset.order_by("-created_at", "id"))
    if len(result) > EXPORT_LIMIT:
        raise ApiError(
            BATCH_LIMIT_EXCEEDED,
            f"单次最多导出 {EXPORT_LIMIT} 个报名单，请缩小筛选条件",
            [{"field": "filters", "reason": f"命中 {len(result)} 条"}],
        )
    return result


def safe_document_name(enrollment: Enrollment, suffix: str) -> str:
    """下载文件名：{学校编码}_{球队名称}_{suffix}，过滤路径非法字符。"""
    team = team_client.get_team(enrollment.team_id)
    school_code = str(team.get("schoolCode") or "school")
    team_name = str(team.get("name") or enrollment.team_id)
    raw = f"{school_code}_{team_name}_{suffix}"
    return "".join(char for char in raw if char not in '\\/:*?"<>|')
