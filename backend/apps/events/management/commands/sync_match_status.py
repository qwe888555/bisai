"""sync_match_status - 按开球时间推进比赛状态（可定时执行）。

门户展示层会按开球时间自行推算状态，但数据库里的 status 仍是录单时写的值：
过期的“未开赛/进行中”会让管理端、赛果录入列表与统计口径一起失真。
本命令把这类记录改成与时间一致的状态：

- SCHEDULED 且已过开球时间：150 分钟内改为 LIVE，超过则改为 FINISHED；
- LIVE 且开球已超过 360 分钟：改为 FINISHED（直播未收尾的场次）；
- FINISHED / CANCELLED 不再改动，已取消的比赛不会被时间改写。

只改状态，不动比分：没有录入赛果的比赛改完仍是空比分，
管理端按“待补录赛果”处理，不会凭空产生 0:0 结果。

用法：
  python manage.py sync_match_status --dry-run     # 先看会改哪些
  python manage.py sync_match_status               # 落库
  python manage.py sync_match_status --event-id evt_xxx
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.common.versioning import bump

# 与前端 utils/portal.ts 的 MATCH_LIVE_WINDOW_MINUTES / MATCH_STALE_LIVE_MINUTES 保持一致
LIVE_WINDOW_MINUTES = 150
STALE_LIVE_MINUTES = 360


class Command(BaseCommand):
    help = "按开球时间把过期未收尾的比赛推进为进行中/已结束"

    def add_arguments(self, parser):
        parser.add_argument("--event-id", default="", help="只处理指定赛事下的比赛")
        parser.add_argument("--dry-run", action="store_true", help="只列出将变更的比赛，不落库")

    def handle(self, *args, **options):
        from apps.events.models import Match

        event_id = (options.get("event_id") or "").strip()
        dry_run = bool(options.get("dry_run"))
        now = timezone.now()

        queryset = Match.objects.filter(status__in=["SCHEDULED", "LIVE"])
        if event_id:
            queryset = queryset.filter(event_id=event_id)

        changed = 0
        for match in queryset.order_by("kickoff_at"):
            elapsed = (now - match.kickoff_at).total_seconds() / 60
            if elapsed < 0:
                continue  # 还没到开球时间，保持原状态

            if match.status == "LIVE":
                if elapsed <= STALE_LIVE_MINUTES:
                    continue
                target = "FINISHED"
            elif elapsed <= LIVE_WINDOW_MINUTES:
                target = "LIVE"
            else:
                target = "FINISHED"

            kickoff = timezone.localtime(match.kickoff_at).strftime("%Y-%m-%d %H:%M")
            self.stdout.write(
                f"{match.pk}（{kickoff}）{match.status} -> {target}"
                f"{'（比分待补录）' if target == 'FINISHED' and match.home_score is None else ''}"
            )
            changed += 1
            if dry_run:
                continue

            match.status = target
            bump(match)
            match.save(update_fields=["status", "version", "updated_at"])

        prefix = "将变更" if dry_run else "已变更"
        self.stdout.write(self.style.SUCCESS(f"{prefix} {changed} 场比赛状态"))
