"""修正历史「赛果快讯」摘要与标题重复的问题。

背景：早期 _sync_result_news 用 f"{title}（{event_name}）" 作摘要，门户卡片上
标题与摘要两行文字几乎一样，出现重复显示。现在摘要改为「赛事名 + 进球球员 + 红黄牌」。

本命令按比赛现有记录重算摘要，只写 summary 字段：
- 不动 published_at / revision / body_html，不触发版本号变化；
- 比赛已被删除的快讯跳过并列出，交给人工判断；
- 幂等：已正确的条目不会重复写入。

用法（backend/ 目录下）：
    python manage.py fix_result_news_summary --dry-run   # 先看会改哪些
    python manage.py fix_result_news_summary             # 实际写入
"""

from django.core.management.base import BaseCommand

from apps.events.models import Content, Match
from apps.events.services import result_news_summary


class Command(BaseCommand):
    help = "按比赛现有记录重算赛果快讯摘要，修掉「摘要与标题重复」"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="只打印将要变更的条目，不写库"
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        contents = Content.all_objects.exclude(source_match_id="").order_by("id")
        total = contents.count()
        self.stdout.write(f"赛果快讯共 {total} 条")

        changed = same = missing = 0
        for content in contents:
            match = Match.objects.filter(pk=content.source_match_id).first()
            if match is None:
                match = Match.all_objects.filter(pk=content.source_match_id).first()
            if match is None:
                missing += 1
                self.stdout.write(
                    f"  ! 跳过 {content.id}：来源比赛 {content.source_match_id} 不存在"
                )
                continue

            new_summary = result_news_summary(match)
            if new_summary == content.summary:
                same += 1
                continue

            changed += 1
            self.stdout.write(
                f"  ~ {content.id}「{content.title[:24]}」\n"
                f"      旧：{content.summary[:70] or '<空>'}\n"
                f"      新：{new_summary[:70]}"
            )
            if not dry_run:
                # 用 update() 绕开 save()，避免动 revision / updated_at
                Content.all_objects.filter(pk=content.pk).update(summary=new_summary)

        action = "待更新" if dry_run else "已更新"
        self.stdout.write(
            self.style.SUCCESS(
                f"完成：{action} {changed} 条，无需变更 {same} 条，跳过 {missing} 条"
            )
        )
        if dry_run and changed:
            self.stdout.write("这是 --dry-run，未写库；去掉该参数即可实际执行")
