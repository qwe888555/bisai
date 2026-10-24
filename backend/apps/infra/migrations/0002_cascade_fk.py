"""DB 层级联外键补齐（按冻结 DDL V1.4-r1，MySQL 专用）。

Django 迁移生成的 FK 无法携带 ON DELETE 子句（MySQL 默认 RESTRICT），
且约束名不可自定义。为精确还原 DDL 的 fk_ire_batch
（import_row_error → import_batch，ON DELETE CASCADE ON UPDATE RESTRICT），
0001 中该列以 db_constraint=False 建列，本迁移在 MySQL 上显式补建；
SQLite 开发库跳过。

回滚：删除 fk_ire_batch（名称确定，可直接 DROP FOREIGN KEY）。
"""

from django.db import migrations


def add_cascade_fk(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    schema_editor.execute(
        "ALTER TABLE `import_row_error` ADD CONSTRAINT `fk_ire_batch` "
        "FOREIGN KEY (`batch_id`) REFERENCES `import_batch` (`id`) "
        "ON DELETE CASCADE ON UPDATE RESTRICT"
    )


def drop_cascade_fk(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    schema_editor.execute("ALTER TABLE `import_row_error` DROP FOREIGN KEY `fk_ire_batch`")


class Migration(migrations.Migration):
    # MySQL 的 ALTER TABLE 会导致隐式提交，禁止包在事务里
    atomic = False

    dependencies = [
        ("infra", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(add_cascade_fk, drop_cascade_fk),
    ]
