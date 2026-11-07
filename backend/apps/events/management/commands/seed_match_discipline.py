"""seed_match_discipline - 补充红黄牌种子数据（P2）。

真实赛事只有进球事件、没有判罚事件时，用本命令为 FINISHED（可选已发布）
比赛补一组幂等种子判罚（黄牌 x2、红牌 x1），用于验收「赛况记录—红黄牌—球员统计」链路。

幂等规则：目标比赛已存在任一 PENALTY 事件时跳过；重复执行不重复插入。
被罚球员优先取该队报名名单（RosterItem），查不到运动员时跳过该场比赛并说明。
用法：
  python manage.py seed_match_discipline --event-id evt_xxx
  python manage.py seed_match_discipline --match-id m_xxx --dry-run
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.common.ids import new_id


class Command(BaseCommand):
    help = "为已结束比赛补红黄牌种子数据（幂等）"

    def add_arguments(self, parser):
        parser.add_argument("--event-id", default="", help="赛事 ID：处理该赛事下所有 FINISHED 比赛")
        parser.add_argument("--match-id", default="", help="单场比赛 ID（优先于 --event-id）")
        parser.add_argument("--dry-run", action="store_true", help="只列出将补充的判罚，不落库")

    def handle(self, *args, **options):
        from apps.events.models import Match, MatchEvent
        from apps.enrollments.models import RosterItem

        event_id = (options.get("event_id") or "").strip()
        match_id = (options.get("match_id") or "").strip()
        dry_run = bool(options.get("dry_run"))

        if match_id:
            matches = list(Match.objects.filter(pk=match_id))
            if not matches:
                raise CommandError(f"比赛不存在：{match_id}")
        elif event_id:
            matches = list(
                Match.objects.filter(event_id=event_id, status="FINISHED")
                .order_by("kickoff_at")
            )
            if not matches:
                raise CommandError(f"赛事下无 FINISHED 比赛：{event_id}")
        else:
            raise CommandError("必须指定 --event-id 或 --match-id")

        created = 0
        skipped_no_penalty_scope = []
        for match in matches:
            if MatchEvent.objects.filter(match_id=match.pk, event_type="PENALTY").exists():
                continue  # 已有判罚：幂等跳过

            # 取主客队报名运动员作为被罚球员（按创建顺序前两名，黄牌用；红牌用第一名）
            def roster_athletes(team_id):
                return list(
                    RosterItem.objects.filter(deleted_at__isnull=True, enrollment__team_id=team_id)
                    .order_by("created_at")
                    .values_list("athlete_id", flat=True)[:3]
                )

            home_players = roster_athletes(match.home_team_id)
            away_players = roster_athletes(match.away_team_id)
            if not home_players and not away_players:
                skipped_no_penalty_scope.append(match.pk)
                continue

            seeds = []
            minute = 30
            if home_players:
                seeds.append((match.home_team_id, home_players[0], "YELLOW_CARD", minute))
                seeds.append((match.home_team_id, home_players[1 % len(home_players)], "YELLOW_CARD", minute + 10))
                if len(home_players) > 1:
                    seeds.append((match.home_team_id, home_players[1], "RED_CARD", minute + 20))
            if away_players:
                seeds.append((match.away_team_id, away_players[0], "YELLOW_CARD", minute + 5))
                seeds.append((match.away_team_id, away_players[1 % len(away_players)], "YELLOW_CARD", minute + 15))

            if dry_run:
                self.stdout.write(f"[dry-run] {match.pk}: {len(seeds)} 条判罚待补")
                continue

            for team_id, athlete_id, penalty_type, minute_no in seeds:
                MatchEvent.objects.create(
                    id=new_id("evt"),
                    match_id=match.pk,
                    event_type="PENALTY",
                    minute=minute_no,
                    team_id=team_id,
                    athlete_id=athlete_id,
                    penalty_type=penalty_type,
                    reason="种子数据：验收红黄牌统计链路",
                    note="由 seed_match_discipline 生成",
                    match_version=int(match.version or 0),
                )
                created += 1
            self.stdout.write(self.style.SUCCESS(f"{match.pk}: 补 {len(seeds)} 条"))

        summary = f"完成：新增 {created} 条判罚种子"
        if skipped_no_penalty_scope:
            summary += f"；{len(skipped_no_penalty_scope)} 场无报名运动员可被罚，已跳过（{skipped_no_penalty_scope}）"
        self.stdout.write(self.style.SUCCESS(summary))
