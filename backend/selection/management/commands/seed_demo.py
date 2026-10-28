"""Create deterministic demo data for the standalone project.

Usage:
    python manage.py seed_demo
    python manage.py seed_demo --reset

The command refuses to run unless SELECTION_ARCHIVE_MODE=demo, so it cannot
accidentally seed a production database.
"""

from decimal import Decimal
from types import SimpleNamespace

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from selection.common import BusinessError
from selection.enums import GroupType, PublicityType, RoundType
from selection.models import (
    PublicityRecord,
    ResultBatch,
    ResultItem,
    Score,
    ScoreItem,
    ScoreTemplate,
    TemplateItem,
)
from selection.services.archive import get_archive_client
from selection.services.publicity import publish_registration
from selection.services.publicity_source import get_publicity_source_client
from selection.services.results import generate_result
from selection.services.scoring import calculate_total

GROUPS = [GroupType.MIDDLE, GroupType.PRIMARY]
ROUNDS = [RoundType.INITIAL, RoundType.FINAL]

TEMPLATE_ITEMS = [
    {
        "dimension_code": "TECH",
        "dimension_name": "技术能力",
        "weight": Decimal("30"),
        "max_score": Decimal("100"),
        "sort_no": 0,
    },
    {
        "dimension_code": "MATCH",
        "dimension_name": "比赛表现",
        "weight": Decimal("30"),
        "max_score": Decimal("100"),
        "sort_no": 1,
    },
    {
        "dimension_code": "PHYSICAL",
        "dimension_name": "身体素质",
        "weight": Decimal("20"),
        "max_score": Decimal("100"),
        "sort_no": 2,
    },
    {
        "dimension_code": "TACTIC",
        "dimension_name": "战术意识",
        "weight": Decimal("20"),
        "max_score": Decimal("100"),
        "sort_no": 3,
    },
]


class Command(BaseCommand):
    help = "生成遴选评分演示数据（模板、评分、结果）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="先清空遴选评分相关表，再重新生成演示数据",
        )
        parser.add_argument(
            "--templates-only",
            action="store_true",
            help="只生成 6 套模板，不生成评分和结果（便于手动测试录入接口）",
        )

    def handle(self, *args, **options):
        mode = getattr(settings, "SELECTION_ARCHIVE_MODE", "auto")
        if mode not in ("demo", "auto"):
            raise CommandError(
                "seed_demo 仅用于独立演示，请先把 "
                "SELECTION_ARCHIVE_MODE 设置为 demo"
            )
        if mode == "auto":
            from selection.services.main_adapters import main_data_available

            if main_data_available():
                raise CommandError(
                    "检测到主程序成员4数据表，拒绝向主库写入演示数据。"
                    "如确需演示，请显式设置 SELECTION_ARCHIVE_MODE=demo "
                    "并使用独立数据库。"
                )

        if options["reset"]:
            self._reset()
            self.stdout.write("已清空遴选评分演示数据")

        template_count = self._seed_templates()
        if options["templates_only"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"已生成 {template_count} 套模板，未生成评分和结果"
                )
            )
            return

        score_count = self._seed_scores()
        result_count = self._seed_results()
        publicity_count = self._seed_publicity()

        self.stdout.write(
            self.style.SUCCESS(
                "演示数据准备完成："
                f"模板 {template_count} 套，"
                f"评分 {score_count} 条，"
                f"结果批次 {result_count} 个，"
                f"公示 {publicity_count} 条"
            )
        )

    @transaction.atomic
    def _reset(self):
        PublicityRecord.objects.all().delete()
        ResultItem.objects.all().delete()
        ResultBatch.objects.all().delete()
        ScoreItem.objects.all().delete()
        Score.objects.all().delete()
        TemplateItem.objects.all().delete()
        ScoreTemplate.objects.all().delete()

    @transaction.atomic
    def _seed_templates(self):
        if ScoreTemplate.objects.exists():
            self.stdout.write("评分模板已存在，跳过模板生成")
            return 0

        created = 0
        for group_type in GROUPS:
            for score_round in ROUNDS:
                template = ScoreTemplate.objects.create(
                    name=(
                        f"{GroupType.label(group_type)}"
                        f"{RoundType.label(score_round)}评分模板"
                    ),
                    group_type=group_type,
                    score_round=score_round,
                    status=1,
                    created_by="1",
                )
                TemplateItem.objects.bulk_create(
                    [
                        TemplateItem(template=template, **item)
                        for item in TEMPLATE_ITEMS
                    ]
                )
                created += 1
        return created

    @transaction.atomic
    def _seed_scores(self):
        if Score.objects.exists():
            self.stdout.write("评分记录已存在，跳过评分生成")
            return 0

        client = get_archive_client()
        created = 0

        for group_type in GROUPS:
            templates = {
                template.score_round: template
                for template in ScoreTemplate.objects.filter(
                    group_type=group_type, status=1
                ).prefetch_related("items")
            }
            if (
                RoundType.INITIAL not in templates
                or RoundType.FINAL not in templates
            ):
                self.stdout.write(
                    self.style.WARNING(
                        f"{GroupType.label(group_type)} 缺少初选/复选模板，跳过"
                    )
                )
                continue

            for candidate in client.list_candidates(group_type):
                for score_round in ROUNDS:
                    template = templates[score_round]
                    items = list(template.items.all())
                    values = self._dimension_values(
                        candidate.candidate_id, score_round, items
                    )
                    total = calculate_total(
                        [
                            SimpleNamespace(
                                score=values[item.dimension_code],
                                max_score=item.max_score,
                                weight=item.weight,
                            )
                            for item in items
                        ]
                    )

                    score = Score.objects.create(
                        event_id=candidate.event_id,
                        group_type=group_type,
                        score_round=score_round,
                        template=template,
                        candidate_id=candidate.candidate_id,
                        candidate_name=candidate.name,
                        team_name=candidate.team_name,
                        player_position=candidate.position,
                        total_score=total,
                        version=0,
                        created_by="1",
                        updated_by="1",
                    )
                    ScoreItem.objects.bulk_create(
                        [
                            ScoreItem(
                                score_record=score,
                                dimension_code=item.dimension_code,
                                dimension_name=item.dimension_name,
                                weight=item.weight,
                                max_score=item.max_score,
                                score=values[item.dimension_code],
                                sort_no=item.sort_no,
                            )
                            for item in items
                        ]
                    )
                    created += 1

        return created

    def _seed_results(self):
        created = 0
        for group_type in GROUPS:
            if ResultBatch.objects.filter(
                event_id="1", group_type=group_type, active=True
            ).exists():
                continue
            try:
                generate_result(
                    {
                        "event_id": "1",
                        "group_type": group_type,
                        "result_round": RoundType.FINAL,
                        "quota": 11,
                        "initial_weight": "0.4",
                        "final_weight": "0.6",
                    },
                    user_id="1",
                )
                created += 1
            except BusinessError as exc:
                self.stdout.write(
                    self.style.WARNING(
                        f"{GroupType.label(group_type)} 结果生成失败："
                        f"{exc.message}"
                    )
                )
        return created

    def _seed_publicity(self):
        """Publish a small, representative set of registrations."""
        if PublicityRecord.objects.exists():
            self.stdout.write("公示记录已存在，跳过公示生成")
            return 0

        client = get_publicity_source_client()
        created = 0

        for group_type in GROUPS:
            registrations = client.list_registrations(
                group_type=group_type
            )
            selected = []
            selected += [
                record
                for record in registrations
                if record.publicity_type == PublicityType.ATHLETE
            ][:3]
            selected += [
                record
                for record in registrations
                if record.publicity_type == PublicityType.COACH
            ][:1]
            selected += [
                record
                for record in registrations
                if record.publicity_type == PublicityType.TEAM
            ][:1]

            for source in selected:
                try:
                    publish_registration(
                        {
                            "event_id": source.event_id,
                            "publicity_type": source.publicity_type,
                            "source_id": source.source_id,
                        },
                        user_id="1",
                    )
                    created += 1
                except BusinessError as exc:
                    self.stdout.write(
                        self.style.WARNING(
                            "公示生成失败："
                            f"{source.publicity_type}/{source.source_id} "
                            f"{exc.message}"
                        )
                    )

        return created

    @staticmethod
    def _dimension_values(candidate_id, score_round, items):
        try:
            numeric_id = int(str(candidate_id))
        except (TypeError, ValueError):
            numeric_id = sum(ord(char) for char in str(candidate_id))
        base = 60 + (
            (
                numeric_id * 7
                + (0 if score_round == RoundType.INITIAL else 6)
            )
            % 30
        )
        offsets = {
            "TECH": 5,
            "MATCH": 3,
            "PHYSICAL": -2,
            "TACTIC": 1,
        }
        values = {}
        for index, item in enumerate(items):
            value = (
                base
                + offsets.get(item.dimension_code, 0)
                + ((numeric_id + index) % 4)
            )
            value = max(0, min(100, value))
            values[item.dimension_code] = Decimal(value)
        return values
