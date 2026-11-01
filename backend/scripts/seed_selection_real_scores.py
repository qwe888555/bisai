"""Seed roster-backed selection scores so result -> publicity publishing works.

The original demo scores use synthetic ids (10001/20001/30001) that do not
exist in the enrollment roster.  Result batches generated from them cannot be
published to publicity because the bridge maps result candidates to real
registration dossiers by candidate id.

This script picks real roster candidates for event_01 (PRIMARY and MIDDLE),
creates high-scoring INITIAL/FINAL records with the active templates, regenerates
the FINAL result batches, and verifies every selected candidate can be resolved
by the publicity bridge.  Existing scores are kept; only missing records are
created, so the script is safe to re-run.

Usage:
    python scripts/seed_selection_real_scores.py [--apply]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import OrderedDict
from decimal import Decimal

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from selection.enums import GroupType, PublicityType, RoundType  # noqa: E402
from selection.models import ResultBatch, Score, ScoreTemplate  # noqa: E402
from selection.services import results as result_service  # noqa: E402
from selection.services import scores as score_service  # noqa: E402
from selection.services.archive import get_archive_client  # noqa: E402
from selection.services.publicity import publish_result_batch  # noqa: E402
from selection.services.publicity_source import (  # noqa: E402
    get_publicity_source_client,
)

EVENT_ID = "event_01"
GROUPS = (GroupType.PRIMARY, GroupType.MIDDLE)
TARGET_CANDIDATES = 15
QUOTA = 11
PREFERRED_CANDIDATE_IDS = {"PRIMARY": ["53"]}
SEED_USER = "seed_selection"


def _publishable_candidate_ids(group_type: str) -> set[str]:
    """Only candidates with a city-backed publicity source can be published."""
    records = get_publicity_source_client().list_registrations(
        group_type=group_type,
        publicity_type=PublicityType.ATHLETE,
        event_id=EVENT_ID,
    )
    return {
        str(record.candidate_id)
        for record in records
        if str(record.candidate_id) and (record.city or "").strip()
    }


def _cleanup_unpublishable_seed_scores(
    group_type: str, publishable_ids: set[str], apply: bool
) -> int:
    """Remove scores this script previously created for unpublishable rows."""
    queryset = Score.objects.filter(
        event_id=EVENT_ID,
        group_type=group_type,
        created_by=SEED_USER,
    ).exclude(candidate_id__in=publishable_ids)
    count = queryset.count()
    if apply and count:
        queryset.delete()
    return count


def _pick_candidates(group_type: str, publishable_ids: set[str]):
    """Pick 15 publishable roster candidates spread across teams."""
    client = get_archive_client()
    candidates = [
        item
        for item in client.list_candidates(group_type)
        if str(item.event_id) == EVENT_ID
        and str(item.candidate_id) in publishable_ids
    ]

    unique = OrderedDict()
    for item in candidates:
        unique.setdefault(str(item.candidate_id), item)

    by_team: OrderedDict[str, list] = OrderedDict()
    for item in unique.values():
        by_team.setdefault(item.team_name or "未分组", []).append(item)

    picked = []
    seen = set()

    for candidate_id in PREFERRED_CANDIDATE_IDS.get(group_type, []):
        item = unique.get(candidate_id)
        if item:
            picked.append(item)
            seen.add(candidate_id)

    # Round-robin so the demo result contains players from many teams.
    indexes = {team: 0 for team in by_team}
    while len(picked) < TARGET_CANDIDATES:
        progressed = False
        for team, team_candidates in by_team.items():
            index = indexes[team]
            if index >= len(team_candidates):
                continue
            item = team_candidates[index]
            indexes[team] = index + 1
            progressed = True
            if str(item.candidate_id) in seen:
                continue
            picked.append(item)
            seen.add(str(item.candidate_id))
            if len(picked) >= TARGET_CANDIDATES:
                break
        if not progressed:
            break

    return picked[:TARGET_CANDIDATES]


def _score_values(items, index):
    """Deterministic high scores, all clearly above the legacy demo scores."""
    values = []
    for offset, item in enumerate(items):
        code = item.dimension_code.upper()
        if code == "TECH":
            value = 99 - (index % 3)
        elif code == "MATCH":
            value = 98 - (index % 3)
        elif code == "PHYSICAL":
            value = 97 - (index % 2)
        else:
            value = 98 - ((index + offset) % 2)
        values.append(
            {
                "dimensionCode": item.dimension_code,
                "score": Decimal(str(value)),
            }
        )
    return values


def _active_template(group_type, score_round):
    template = (
        ScoreTemplate.objects.prefetch_related("items")
        .filter(
            group_type=group_type,
            score_round=score_round,
            status=1,
        )
        .order_by("-id")
        .first()
    )
    if not template:
        raise SystemExit(
            f"缺少启用的评分模板: {group_type} / {score_round}"
        )
    return template


def _ensure_scores(group_type, candidates, apply):
    created = 0
    skipped = 0
    for score_round in (RoundType.INITIAL, RoundType.FINAL):
        template = _active_template(group_type, score_round)
        template_items = list(template.items.order_by("sort_no", "id"))
        existing = set(
            Score.objects.filter(
                event_id=EVENT_ID,
                group_type=group_type,
                score_round=score_round,
            ).values_list("candidate_id", flat=True)
        )
        for index, candidate in enumerate(candidates):
            candidate_id = str(candidate.candidate_id)
            if candidate_id in existing:
                skipped += 1
                continue
            payload = {
                "event_id": EVENT_ID,
                "group_type": group_type,
                "round": score_round,
                "template_id": template.id,
                "candidate_id": candidate_id,
                "scores": _score_values(template_items, index),
            }
            if not apply:
                print(
                    f"  [plan] {score_round:7} {candidate_id:>5} "
                    f"{candidate.name:<6} {candidate.team_name}"
                )
                continue
            score_service.create_score(payload, user_id=SEED_USER)
            created += 1
            existing.add(candidate_id)
    return created, skipped


def _candidate_ids_in_roster(group_type, items):
    client = get_archive_client()
    missing = []
    for item in items:
        candidate = client.get_candidate(
            item.candidate_id, event_id=EVENT_ID
        )
        if not candidate:
            missing.append(item.candidate_id)
    return missing


def _has_publishable_active_batch(group_type, publishable_ids):
    batch = (
        ResultBatch.objects.prefetch_related("items")
        .filter(
            event_id=EVENT_ID,
            group_type=group_type,
            active=True,
            stale=False,
        )
        .order_by("-version_no")
        .first()
    )
    if not batch or batch.items.count() != QUOTA:
        return None
    item_ids = {item.candidate_id for item in batch.items.all()}
    if item_ids and item_ids <= publishable_ids:
        return batch
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="写入数据库（默认仅预览）"
    )
    args = parser.parse_args()

    for group_type in GROUPS:
        print(f"\n=== {GroupType.label(group_type)} ===")
        publishable_ids = _publishable_candidate_ids(group_type)
        removed = _cleanup_unpublishable_seed_scores(
            group_type, publishable_ids, args.apply
        )
        if removed:
            print(f"清理无市州来源的本脚本评分 {removed} 条")
        candidates = _pick_candidates(group_type, publishable_ids)
        print(
            f"候选人数 {len(candidates)}，"
            f"球队数 {len({item.team_name for item in candidates})}"
        )
        created, skipped = _ensure_scores(
            group_type, candidates, args.apply
        )
        print(f"新增评分 {created}，已存在跳过 {skipped}")

        if not args.apply:
            continue

        current = _has_publishable_active_batch(
            group_type, publishable_ids
        )
        if current:
            print(
                f"现行结果批次 #{current.id} v{current.version_no} "
                f"已全部可公示，跳过重新生成"
            )
            continue

        detail = result_service.generate_result(
            {
                "event_id": EVENT_ID,
                "group_type": group_type,
                "result_round": RoundType.FINAL,
                "quota": QUOTA,
                "initial_weight": "0.4",
                "final_weight": "0.6",
            },
            user_id=SEED_USER,
        )
        batch = ResultBatch.objects.prefetch_related("items").get(
            pk=detail["id"]
        )
        missing = _candidate_ids_in_roster(
            group_type, list(batch.items.all())
        )
        print(
            f"生成结果批次 #{batch.id} v{batch.version_no}，"
            f"入围 {batch.items.count()} 人，未匹配档案 {missing or '无'}"
        )

        # Dry-run the bridge and roll it back: verification must not publish.
        with transaction.atomic():
            outcome = publish_result_batch(
                {"result_batch_id": batch.id}, user_id=SEED_USER
            )
            transaction.set_rollback(True)
        print(f"公示桥预演: {outcome}")

    print("\n完成。")
    if not args.apply:
        print("这是预览。确认无误后加 --apply 写入。")


if __name__ == "__main__":
    main()
