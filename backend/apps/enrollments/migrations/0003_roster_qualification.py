# E-2 报名名单资格确认（设计文档 v7）。
# 手写迁移：RosterItem 增加资格确认凭证两列 + 新增追加式资格操作日志表。
# 注意：本迁移仅随阶段 4 验收后的独立迁移审批执行，编码阶段禁止 migrate。

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("enrollments", "0002_alter_enrollment_subject_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="rosteritem",
            name="confirmed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="rosteritem",
            name="confirmed_by",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.CreateModel(
            name="RosterQualificationLog",
            fields=[
                ("version", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("id", models.CharField(max_length=64, primary_key=True, serialize=False)),
                (
                    "roster_item",
                    models.ForeignKey(
                        blank=True,
                        db_column="roster_item_id",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="qualification_logs",
                        to="enrollments.rosteritem",
                    ),
                ),
                ("roster_item_id_snapshot", models.CharField(db_index=True, max_length=64)),
                ("athlete_id", models.CharField(blank=True, default="", max_length=64)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("CONFIRM", "确认资格"),
                            ("REJECT", "驳回资格"),
                            ("EXCLUDE", "剔除名单"),
                            ("REVOKE", "撤销资格凭证"),
                        ],
                        max_length=20,
                    ),
                ),
                ("reason", models.TextField(blank=True, default="")),
                (
                    "operator_account_id",
                    models.CharField(blank=True, default="", max_length=64),
                ),
            ],
            options={
                "verbose_name": "名单资格操作日志",
                "verbose_name_plural": "名单资格操作日志",
                "db_table": "enrollments_roster_qualification_log",
                "ordering": ["-created_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["roster_item", "-created_at"],
                        name="enrollments_roster__28d218_idx",
                    ),
                ],
            },
        ),
    ]
