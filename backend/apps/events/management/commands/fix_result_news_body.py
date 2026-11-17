"""重建历史「赛果快讯」正文，修掉透出到公开页的内部备注。

背景：早期 _result_news_body 把比赛事件的 reason 自由文本（种子命令写入的
「种子数据：验收红黄牌统计链路」等）原样拼进红黄牌段落并已落库。现在生成器
只输出结构化信息，历史文章需要重建正文才能同步干净。

本命令按比赛现有记录重算 body_html / body_text / summary：
- 不动 title / published_at / revision，不触发版本号变化；
- 比赛已被删除的快讯跳过并列出，交给人工判断；
- 幂等：三项均一致的条目不会重复写入。

用法（backend/ 目录下）：
    python manage.py fix_result_news_body --dry-run   # 先看会改哪些
    python manage.py fix_result_news_body             # 实际写入
"""

from django.core.management.base import BaseCommand

from apps.events.models import Content, Match
from apps.events.services import result_news_body


class Command(BaseCommand):
    help = "按比赛现有记录重建赛果快讯正文，清理透出到公开页的内部备注"

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

            new_html, new_text, new_summary = result_news_body(match)
            if (
                new_html == content.body_html
                and new_text == content.body_text
                and new_summary == content.summary
            ):
                same += 1
                continue

            changed += 1
            self.stdout.write(
                f"  ~ {content.id}「{content.title[:24]}」正文需要重建"
            )
            if not dry_run:
                # 用 update() 绕开 save()，避免动 revision / updated_at
                Content.all_objects.filter(pk=content.pk).update(
                    body_html=new_html, body_text=new_text, summary=new_summary
                )

        action = "待更新" if dry_run else "已更新"
        self.stdout.write(
            self.style.SUCCESS(
                f"完成：{action} {changed} 条，无需变更 {same} 条，跳过 {missing} 条"
            )
        )
        if dry_run and changed:
            self.stdout.write("这是 --dry-run，未写库；去掉该参数即可实际执行")
