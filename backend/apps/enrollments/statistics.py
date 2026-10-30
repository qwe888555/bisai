"""报名统计（市州 / 学校 / 组别）。

口径（默认实现，TODO 确认项#11）：
- 数据源：非软删除、status ∉ {DRAFT, WITHDRAWN}；
- 去重键：每个 (teamId, groupId) 取 submission_no 最大、created_at 最新的一条；
- teamCount = 去重后球队数；athleteCount = 这些报名的 roster_item 合计（不含 EXCLUDED）；
- enrollmentCount = 去重后报名条数（解释"球队按组别重复计数"的口径差）；
- pending/approved/rejected 按该报名当前状态分桶；
- 维度：CITY 按市州组织、SCHOOL（别名 UNIVERSITY，兼容旧前端）按学校、
  GROUP 按赛事组别——均不按 subject_type 过滤，否则市州端（全部
  SCHOOL_TEAM）调 dimension=UNIVERSITY 恒返回空。
聚合用 values().annotate()，不做逐条查询。
"""

from __future__ import annotations

from typing import Optional

from django.db.models import Count

from . import scope
from .clients import event_config_client, team_client
from .models import (
    QUALIFICATION_EXCLUDED,
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_PENDING_CITY,
    STATUS_PENDING_PROVINCE,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
    Enrollment,
    RosterItem,
)

EXCLUDED_STATUSES = (STATUS_DRAFT, STATUS_WITHDRAWN)


def _organization_names(org_ids: list[str]) -> dict[str, str]:
    if not org_ids:
        return {}
    from apps.accounts.models import Organization

    return dict(
        Organization.objects.filter(pk__in=org_ids).values_list("pk", "name")
    )


def enrollment_statistics(
    actor,
    event_id: str,
    dimension: str,
    city_org_id: Optional[str] = None,
    group_id: Optional[str] = None,
) -> list[dict]:
    queryset = scope.scoped_queryset(actor, Enrollment.objects.all())
    queryset = queryset.filter(event_id=event_id).exclude(
        status__in=EXCLUDED_STATUSES
    )
    if city_org_id:
        queryset = queryset.filter(city_org_id=city_org_id)
    if group_id:
        queryset = queryset.filter(group_id=group_id)
    # UNIVERSITY 是历史前端取值，等价于按学校的 SCHOOL 维度；
    # 均不按 subject_type 过滤——市州端数据全是 SCHOOL_TEAM，过滤即空列表

    ordered = queryset.order_by(
        "team_id", "group_id", "-submission_no", "-created_at", "-id"
    )
    latest: dict[tuple[str, str], Enrollment] = {}
    for enrollment in ordered:
        key = (enrollment.team_id, enrollment.group_id)
        if key not in latest:
            latest[key] = enrollment
    deduped = list(latest.values())

    counts: dict[str, int] = {}
    if deduped:
        rows = (
            RosterItem.objects.filter(
                enrollment_id__in=[item.pk for item in deduped]
            )
            .exclude(qualification_status=QUALIFICATION_EXCLUDED)
            .values("enrollment_id")
            .annotate(total=Count("id"))
        )
        counts = {row["enrollment_id"]: int(row["total"]) for row in rows}

    grouped: dict[str, dict] = {}
    for enrollment in deduped:
        key, label = _group_key(dimension, enrollment)
        row = grouped.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "teamCount": 0,
                "athleteCount": 0,
                "enrollmentCount": 0,
                "pendingCount": 0,
                "approvedCount": 0,
                "rejectedCount": 0,
                "_teams": set(),
            },
        )
        row["_teams"].add(enrollment.team_id)
        row["enrollmentCount"] += 1
        row["athleteCount"] += counts.get(enrollment.pk, 0)
        if enrollment.status in {STATUS_PENDING_CITY, STATUS_PENDING_PROVINCE}:
            row["pendingCount"] += 1
        elif enrollment.status == STATUS_APPROVED:
            row["approvedCount"] += 1
        elif enrollment.status == STATUS_REJECTED:
            row["rejectedCount"] += 1

    result: list[dict] = []
    for row in grouped.values():
        row["teamCount"] = len(row.pop("_teams"))
        result.append(row)
    result.sort(key=lambda item: str(item["key"]))
    return result


def _group_key(dimension: str, enrollment: Enrollment) -> tuple[str, str]:
    if dimension == "CITY":
        org_id = enrollment.city_org_id or ""
        names = _organization_names([org_id])
        return org_id, names.get(org_id) or org_id or "未分配市州"
    if dimension in ("UNIVERSITY", "SCHOOL"):
        # 按学校分组（原按 team_id 分组导致"每队一行"，看不出学校汇总）
        team = team_client.get_team(enrollment.team_id)
        school_code = team.get("schoolCode") or ""
        if school_code:
            return f"sch:{school_code}", (
                team.get("schoolName") or school_code
            )
        return enrollment.team_id, team.get("name") or enrollment.team_id
    group = event_config_client.get_group(enrollment.event_id, enrollment.group_id)
    return enrollment.group_id, group.get("name") or enrollment.group_id
