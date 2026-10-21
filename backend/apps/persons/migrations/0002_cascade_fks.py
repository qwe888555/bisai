"""DB 层级联外键补齐（按冻结 DDL V1.4-r1，MySQL 专用）。

Django 迁移生成的 FK 无法携带 ON DELETE 子句（MySQL 默认 RESTRICT），
且约束名不可自定义。为精确还原 DDL 的 fk_athlete_person / fk_coach_person
（ON DELETE CASCADE ON UPDATE RESTRICT），0001 中两列以 db_constraint=False 建列，
本迁移在 MySQL 上显式补建；SQLite 开发库跳过（无对应能力，开发期无级联需求）。

回滚：删除这两个具名约束（名称确定，可直接 DROP FOREIGN KEY）。
"""

from django.db import migrations

# (表名, 列名, 引用表, 约束名)
CASCADE_FKS = (
    ("person_athlete_profile", "person_id", "person", "fk_athlete_person"),
    ("person_coach_profile", "person_id", "person", "fk_coach_person"),
)


def add_cascade_fks(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    for table, column, ref, name in CASCADE_FKS:
        schema_editor.execute(
            f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` "
            f"FOREIGN KEY (`{column}`) REFERENCES `{ref}` (`id`) "
            f"ON DELETE CASCADE ON UPDATE RESTRICT"
        )


def drop_cascade_fks(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    for table, _, _, name in CASCADE_FKS:
        schema_editor.execute(f"ALTER TABLE `{table}` DROP FOREIGN KEY `{name}`")


class Migration(migrations.Migration):
    # MySQL 的 ALTER TABLE 会导致隐式提交，禁止包在事务里
    atomic = False

    dependencies = [
        ("persons", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(add_cascade_fks, drop_cascade_fks),
    ]
