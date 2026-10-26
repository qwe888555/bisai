"""把已有的已发布 POLICY 内容回填/校正到政策问答库。

用法：
    python manage.py sync_policy_docs          # 只统计
    python manage.py sync_policy_docs --apply  # 实际写入
"""

from django.core.management.base import BaseCommand

from apps.events.models import Content
from apps.policyqa.services import sync_policy_document


class Command(BaseCommand):
    help = "把 CMS 中已发布的 POLICY 内容同步到 policyqa_policy_document"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="实际写入，默认只统计")

    def handle(self, *args, **options):
        contents = Content.all_objects.filter(content_type="POLICY")
        published = [
            item for item in contents if item.status == "PUBLISHED" and not item.deleted_at
        ]
        self.stdout.write(f"POLICY 内容共 {contents.count()} 条，已发布 {len(published)} 条")
        if not options["apply"]:
            self.stdout.write("只统计，未写入；加 --apply 执行")
            return
        written = 0
        for content in contents:
            document = sync_policy_document(content)
            if document is not None and document.enabled:
                written += 1
        self.stdout.write(self.style.SUCCESS(f"已同步启用 {written} 条政策快照"))
