"""events 业务逻辑：赛事与赛事组别的查询、创建、更新、软删除。

规则集中在本层，视图只做权限校验与信封包装；写操作带乐观锁 version，
删除为软删除，存在受保护历史关联时返回 409 STATE_CONFLICT，不级联破坏。
"""

from __future__ import annotations

from typing import Optional

from django.utils import timezone

from apps.common.errors import (
    BUSINESS_VALIDATION_FAILED,
    NOT_FOUND,
    STATE_CONFLICT,
    ApiError,
)
from apps.common.ids import new_id
from apps.common.versioning import bump, check_version

from .models import Event, EventGroup


# ---------------------------------------------------------------- 内部校验

def _validate_window(opens, closes) -> None:
    if closes <= opens:
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "报名截止时间必须晚于报名开始时间",
            [{"field": "registrationClosesAt", "reason": "必须晚于 registrationOpensAt"}],
        )


def _validate_age_range(min_age: Optional[int], max_age: Optional[int]) -> None:
    if min_age is not None and max_age is not None and min_age > max_age:
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "最小年龄不能大于最大年龄",
            [{"field": "maxAge", "reason": "maxAge 不能小于 minAge"}],
        )


# ---------------------------------------------------------------- 赛事

def list_events(filters: dict):
    queryset = Event.objects.all()
    if filters.get("name"):
        queryset = queryset.filter(name__icontains=filters["name"])
    if filters.get("season"):
        queryset = queryset.filter(season=filters["season"])
    if filters.get("status"):
        queryset = queryset.filter(status=filters["status"])
    if filters.get("eventCategory"):
        queryset = queryset.filter(event_category=filters["eventCategory"])
    return queryset.order_by("-created_at", "id")


def create_event(data: dict) -> Event:
    _validate_window(data["registrationOpensAt"], data["registrationClosesAt"])
    return Event.objects.create(
        id=new_id("evt"),
        name=data["name"],
        season=data["season"],
        event_category=data.get("eventCategory", ""),
        registration_opens_at=data["registrationOpensAt"],
        registration_closes_at=data["registrationClosesAt"],
        status=data.get("status", "DRAFT"),
        description=data.get("description", ""),
    )


def get_event(event_id: str) -> Event:
    event = Event.objects.filter(pk=event_id).first()
    if event is None:
        raise ApiError(NOT_FOUND)
    return event


def update_event(event_id: str, data: dict) -> Event:
    event = get_event(event_id)
    check_version(event, data.get("version"))
    _validate_window(data["registrationOpensAt"], data["registrationClosesAt"])
    event.name = data["name"]
    event.season = data["season"]
    event.event_category = data.get("eventCategory", "")
    event.registration_opens_at = data["registrationOpensAt"]
    event.registration_closes_at = data["registrationClosesAt"]
    event.status = data.get("status", event.status)
    event.description = data.get("description", "")
    bump(event)
    event.save()
    return event


def delete_event(event_id: str, version: Optional[int]) -> Event:
    event = get_event(event_id)
    check_version(event, version)
    if EventGroup.objects.filter(event=event).exists():
        raise ApiError(STATE_CONFLICT, "该赛事下仍有组别，不能删除")
    event.deleted_at = timezone.now()
    bump(event)
    event.save()
    return event


def available_events(filters: dict):
    """报名可见赛事：默认只看报名进行中；registrationOpen=false 时取其余状态。"""
    queryset = Event.objects.all()
    if filters.get("registrationOpen") is True:
        queryset = queryset.filter(status="REGISTRATION_OPEN")
    elif filters.get("registrationOpen") is False:
        queryset = queryset.exclude(status="REGISTRATION_OPEN")
    if filters.get("season"):
        queryset = queryset.filter(season=filters["season"])
    if filters.get("eventCategory"):
        queryset = queryset.filter(event_category=filters["eventCategory"])
    return queryset.order_by("-created_at", "id")


# ---------------------------------------------------------------- 赛事组别

def list_groups(event_id: str, filters: dict):
    event = get_event(event_id)
    queryset = EventGroup.objects.filter(event=event)
    if filters.get("category"):
        queryset = queryset.filter(category=filters["category"])
    return queryset.order_by("created_at", "id")


def create_group(event_id: str, data: dict) -> EventGroup:
    event = get_event(event_id)
    _validate_age_range(data.get("minAge"), data.get("maxAge"))
    return EventGroup.objects.create(
        id=new_id("evg"),
        event=event,
        name=data["name"],
        category=data["category"],
        gender=data["gender"],
        quota=data.get("quota", 0),
        min_age=data.get("minAge"),
        max_age=data.get("maxAge"),
        max_players=data["maxPlayers"],
        required_material_types=list(data.get("requiredMaterialTypes") or []),
    )


def get_group(event_id: str, group_id: str) -> EventGroup:
    group = EventGroup.objects.filter(event_id=event_id, pk=group_id).first()
    if group is None:
        raise ApiError(NOT_FOUND)
    return group


def update_group(event_id: str, group_id: str, data: dict) -> EventGroup:
    group = get_group(event_id, group_id)
    check_version(group, data.get("version"))
    _validate_age_range(data.get("minAge"), data.get("maxAge"))
    group.name = data["name"]
    group.category = data["category"]
    group.gender = data["gender"]
    group.quota = data.get("quota", 0)
    group.min_age = data.get("minAge")
    group.max_age = data.get("maxAge")
    group.max_players = data["maxPlayers"]
    group.required_material_types = list(data.get("requiredMaterialTypes") or [])
    bump(group)
    group.save()
    return group


def delete_group(event_id: str, group_id: str, version: Optional[int]) -> EventGroup:
    group = get_group(event_id, group_id)
    check_version(group, version)
    # D1 尚无报名快照/比赛事件引用；存在受保护关联时在此追加 409 判断
    group.deleted_at = timezone.now()
    bump(group)
    group.save()
    return group

# ---------------------------------------------------------------- 比赛对阵（5.1）

def list_matches(filters: dict):
    from django.db.models import Q

    from .models import Match

    queryset = Match.objects.all()
    if filters.get("eventId"):
        queryset = queryset.filter(event_id=filters["eventId"])
    if filters.get("groupId"):
        queryset = queryset.filter(group_id=filters["groupId"])
    if filters.get("status"):
        queryset = queryset.filter(status=filters["status"])
    if filters.get("teamId"):
        tid = filters["teamId"]
        queryset = queryset.filter(Q(home_team_id=tid) | Q(away_team_id=tid))
    # teamName 依赖成员4球队维度，本库不冗余球队名；待球队服务接入后在此补名称模糊匹配
    return queryset.order_by("kickoff_at", "id")


def _validate_match_refs(event_id: str, group_id: str) -> None:
    from .models import Event, EventGroup

    if not Event.objects.filter(pk=event_id, deleted_at__isnull=True).exists():
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "赛事不存在",
            [{"field": "eventId", "reason": "eventId 指向的赛事不存在或已删除"}],
        )
    if not EventGroup.objects.filter(pk=group_id, deleted_at__isnull=True).exists():
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "组别不存在",
            [{"field": "groupId", "reason": "groupId 指向的组别不存在或已删除"}],
        )


def _validate_sides(home_team_id: str, away_team_id: str) -> None:
    if home_team_id == away_team_id:
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "主队与客队不能为同一支球队",
            [{"field": "awayTeamId", "reason": "awayTeamId 不能等于 homeTeamId"}],
        )


def create_match(data: dict) -> "Match":
    from .models import Match

    _validate_match_refs(data["eventId"], data["groupId"])
    _validate_sides(data["homeTeamId"], data["awayTeamId"])
    return Match.objects.create(
        id=new_id("mtc"),
        event_id=data["eventId"],
        group_id=data["groupId"],
        home_team_id=data["homeTeamId"],
        away_team_id=data["awayTeamId"],
        kickoff_at=data["kickoffAt"],
        venue=data.get("venue", ""),
        stage=data.get("stage", ""),
        status=data.get("status", "SCHEDULED"),
        referees=list(data.get("referees") or []),
    )


def get_match(match_id: str) -> "Match":
    from .models import Match

    match = Match.objects.filter(pk=match_id).first()
    if match is None:
        raise ApiError(NOT_FOUND)
    return match


def update_match(match_id: str, data: dict) -> "Match":
    match = get_match(match_id)
    check_version(match, data.get("version"))
    _validate_match_refs(data["eventId"], data["groupId"])
    _validate_sides(data["homeTeamId"], data["awayTeamId"])
    # PUT 整份替换：未提交的可选字段回到默认，不做 PATCH 合并
    match.event_id = data["eventId"]
    match.group_id = data["groupId"]
    match.home_team_id = data["homeTeamId"]
    match.away_team_id = data["awayTeamId"]
    match.kickoff_at = data["kickoffAt"]
    match.venue = data.get("venue", "")
    match.stage = data.get("stage", "")
    match.status = data.get("status", match.status)
    match.referees = list(data.get("referees") or [])
    bump(match)
    match.save()
    return match


def delete_match(match_id: str, version: Optional[int]) -> "Match":
    from .models import Match, MatchEvent

    match = get_match(match_id)
    check_version(match, version)
    # 存在未删除的比赛事件流时 409，不级联破坏历史事件
    if MatchEvent.objects.filter(match_id=match_id, deleted_at__isnull=True).exists():
        raise ApiError(STATE_CONFLICT, "该比赛已存在事件记录，不能删除")
    match.deleted_at = timezone.now()
    bump(match)
    match.save()
    return match


def publish_match(match_id: str, version: int) -> "Match":
    match = get_match(match_id)
    check_version(match, version)
    match.published = True
    bump(match)
    match.save()
    return match


def unpublish_match(match_id: str, version: int, reason: str) -> "Match":
    match = get_match(match_id)
    check_version(match, version)
    match.published = False
    bump(match)
    match.save()
    # 下线理由写入审计，不落冗余字段
    match._unpublish_reason = reason
    return match

# ---------------------------------------------------------------- 比赛事件流（5.2）

def _rescore(match) -> None:
    """由未撤销 GOAL 事件重算比分；不接受手填比分。teamId 即得分队。"""
    from .models import MatchEvent

    goals = MatchEvent.objects.filter(match_id=match.pk, event_type="GOAL")
    match.home_score = sum(1 for g in goals if g.team_id == match.home_team_id)
    match.away_score = sum(1 for g in goals if g.team_id == match.away_team_id)


def _validate_event_team(match, team_id: str) -> None:
    if team_id not in (match.home_team_id, match.away_team_id):
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "teamId 不属于本场双方",
            [{"field": "teamId", "reason": "只能填写主队或客队 ID"}],
        )


def _validate_event_payload(data: dict) -> None:
    etype = data["eventType"]
    if etype == "GOAL" and not data.get("scorerAthleteId"):
        raise ApiError(BUSINESS_VALIDATION_FAILED, "进球必须填写 scorerAthleteId",
                       [{"field": "scorerAthleteId", "reason": "GOAL 必填"}])
    elif etype == "PENALTY":
        if not data.get("penaltyType"):
            raise ApiError(BUSINESS_VALIDATION_FAILED, "判罚必须填写 penaltyType",
                           [{"field": "penaltyType", "reason": "PENALTY 必填"}])
        if not (data.get("reason") or "").strip():
            raise ApiError(BUSINESS_VALIDATION_FAILED, "判罚必须填写 reason",
                           [{"field": "reason", "reason": "PENALTY 必填"}])
    elif etype == "SUBSTITUTION":
        if not data.get("outAthleteId") or not data.get("inAthleteId"):
            raise ApiError(BUSINESS_VALIDATION_FAILED, "换人必须填写 outAthleteId 与 inAthleteId",
                           [{"field": "event", "reason": "SUBSTITUTION 双方球员必填"}])


def list_match_events(match_id: str, event_type: Optional[str]):
    from .models import MatchEvent

    get_match(match_id)
    queryset = MatchEvent.objects.filter(match_id=match_id)
    if event_type:
        queryset = queryset.filter(event_type=event_type)
    # 时间序列：按分钟、补时、录入顺序
    return queryset.order_by("minute", "stoppage_minute", "created_at", "id")


def get_match_event(match_id: str, event_id: str):
    from .models import MatchEvent

    event = MatchEvent.objects.filter(pk=event_id, match_id=match_id).first()
    if event is None:
        raise ApiError(NOT_FOUND)
    return event


def create_match_event(match_id: str, data: dict):
    from django.db import transaction

    from .models import MatchEvent

    match = get_match(match_id)
    check_version(match, data.get("matchVersion"))
    _validate_event_team(match, data["teamId"])
    _validate_event_payload(data)
    with transaction.atomic():
        snapshot_version = int(match.version or 0)
        event = MatchEvent.objects.create(
            id=new_id("mev"),
            match_id=match.pk,
            event_type=data["eventType"],
            minute=data["minute"],
            stoppage_minute=data.get("stoppageMinute"),
            team_id=data["teamId"],
            scorer_athlete_id=data.get("scorerAthleteId") or "",
            own_goal=bool(data.get("ownGoal", False)),
            athlete_id=data.get("athleteId") or "",
            penalty_type=data.get("penaltyType"),
            reason=data.get("reason") or "",
            out_athlete_id=data.get("outAthleteId") or "",
            in_athlete_id=data.get("inAthleteId") or "",
            note=data.get("note", ""),
            match_version=snapshot_version,
        )
        _rescore(match)
        bump(match)
        match.score_revision = int(match.score_revision or 0) + 1
        match.save()
    return event


def update_match_event(match_id: str, event_id: str, version: int, data: dict):
    from django.db import transaction

    event = get_match_event(match_id, event_id)
    check_version(event, version)
    match = get_match(match_id)
    check_version(match, data.get("matchVersion"))
    _validate_event_team(match, data["teamId"])
    _validate_event_payload(data)
    with transaction.atomic():
        event.event_type = data["eventType"]
        event.minute = data["minute"]
        event.stoppage_minute = data.get("stoppageMinute")
        event.team_id = data["teamId"]
        event.scorer_athlete_id = data.get("scorerAthleteId") or ""
        event.own_goal = bool(data.get("ownGoal", False))
        event.athlete_id = data.get("athleteId") or ""
        event.penalty_type = data.get("penaltyType")
        event.reason = data.get("reason") or ""
        event.out_athlete_id = data.get("outAthleteId") or ""
        event.in_athlete_id = data.get("inAthleteId") or ""
        event.note = data.get("note", "")
        event.match_version = int(match.version or 0)
        bump(event)
        event.save()
        _rescore(match)
        bump(match)
        match.score_revision = int(match.score_revision or 0) + 1
        match.save()
    return event


def delete_match_event(match_id: str, event_id: str, version: int, match_version: int):
    from django.db import transaction

    event = get_match_event(match_id, event_id)
    check_version(event, version)
    match = get_match(match_id)
    check_version(match, match_version)
    with transaction.atomic():
        event.deleted_at = timezone.now()
        bump(event)
        event.save()
        _rescore(match)
        bump(match)
        match.score_revision = int(match.score_revision or 0) + 1
        match.save()
    return event

# ---------------------------------------------------------------- 赛事文件（5.3）

def list_match_documents(match_id: str, document_type: Optional[str]):
    from .models import MatchDocument

    get_match(match_id)
    queryset = MatchDocument.objects.filter(match_id=match_id)
    if document_type:
        queryset = queryset.filter(document_type=document_type)
    return queryset.order_by("-created_at", "-id")


def create_match_document(match_id: str, data: dict):
    from .models import MatchDocument

    get_match(match_id)
    doc = MatchDocument.objects.create(
        id=new_id("mdoc"),
        match_id=match_id,
        document_type=data["documentType"],
        file_id=data["fileId"],
    )
    return doc


# ---------------------------------------------------------------- 图文直播条目（5.4）

def _bump_feed(match) -> int:
    match.feed_revision = int(match.feed_revision or 0) + 1
    match.save(update_fields=["feed_revision", "updated_at"])
    return match.feed_revision


def list_live_posts(match_id: str):
    from .models import LivePost

    get_match(match_id)
    return LivePost.objects.filter(match_id=match_id).order_by("-created_at", "-id")


def get_live_post(match_id: str, post_id: str):
    from .models import LivePost

    post = LivePost.objects.filter(pk=post_id, match_id=match_id).first()
    if post is None:
        raise ApiError(NOT_FOUND)
    return post


def create_live_post(match_id: str, data: dict):
    from django.db import transaction

    from .models import LivePost

    match = get_match(match_id)
    with transaction.atomic():
        revision = _bump_feed(match)
        post = LivePost.objects.create(
            id=new_id("lpost"),
            match_id=match_id,
            text=data["text"],
            image_file_ids=list(data.get("imageFileIds") or []),
            minute=data.get("minute"),
            cursor=revision,
        )
    return post


def update_live_post(match_id: str, post_id: str, data: dict):
    from django.db import transaction

    post = get_live_post(match_id, post_id)
    check_version(post, data.get("version"))
    match = get_match(match_id)
    with transaction.atomic():
        post.text = data["text"]
        post.image_file_ids = list(data.get("imageFileIds") or [])
        post.minute = data.get("minute")
        post.cursor = _bump_feed(match)
        bump(post)
        post.save()
    return post


def delete_live_post(match_id: str, post_id: str, version: int):
    from django.db import transaction

    post = get_live_post(match_id, post_id)
    check_version(post, version)
    match = get_match(match_id)
    with transaction.atomic():
        post.deleted_at = timezone.now()
        bump(post)
        post.save()
        _bump_feed(match)
    return post


# ---------------------------------------------------------------- 直播链接（5.4）

def _validate_live_url(url: str) -> None:
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "直播链接仅支持 http/https",
            [{"field": "url", "reason": "必须以 http:// 或 https:// 开头"}],
        )


def list_live_links(match_id: str):
    from .models import LiveLink

    get_match(match_id)
    return LiveLink.objects.filter(match_id=match_id).order_by("-created_at", "-id")


def get_live_link(match_id: str, link_id: str):
    from .models import LiveLink

    link = LiveLink.objects.filter(pk=link_id, match_id=match_id).first()
    if link is None:
        raise ApiError(NOT_FOUND)
    return link


def create_live_link(match_id: str, data: dict):
    from .models import LiveLink

    get_match(match_id)
    _validate_live_url(data["url"])
    link = LiveLink.objects.create(
        id=new_id("llink"),
        match_id=match_id,
        title=data["title"],
        url=data["url"],
        enabled=bool(data.get("enabled", True)),
    )
    return link


def update_live_link(match_id: str, link_id: str, data: dict):
    link = get_live_link(match_id, link_id)
    check_version(link, data.get("version"))
    _validate_live_url(data["url"])
    link.title = data["title"]
    link.url = data["url"]
    link.enabled = bool(data.get("enabled", True))
    bump(link)
    link.save()
    return link


def delete_live_link(match_id: str, link_id: str, version: int):
    link = get_live_link(match_id, link_id)
    check_version(link, version)
    link.deleted_at = timezone.now()
    bump(link)
    link.save()
    return link