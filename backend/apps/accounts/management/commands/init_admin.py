"""初始化全局管理员账号（正式环境第一次部署时执行一次）。

用法：
    python manage.py init_admin
    python manage.py init_admin --username admin --password 自定义密码
    ADMIN_INIT_PASSWORD=xxx python manage.py init_admin

全局管理员 = 不挂组织的 ADMIN：数据范围覆盖全库（见 apps/common/scopes.py）。
创建后 must_change_password=True，首次登录应强制改密。
"""

from __future__ import annotations

import os

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Account
from apps.common.ids import new_account_id


class Command(BaseCommand):
    help = "初始化全局管理员账号（不挂组织，数据范围全库）"

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin", help="登录名，默认 admin")
        parser.add_argument("--password", default=None, help="初始密码；不传则读 ADMIN_INIT_PASSWORD")
        parser.add_argument("--display-name", default="系统管理员", help="显示名")

    def handle(self, *args, **options):
        username = options["username"]
        password = options["password"] or os.environ.get("ADMIN_INIT_PASSWORD", "")
        if not password:
            raise CommandError("请通过 --password 或环境变量 ADMIN_INIT_PASSWORD 提供初始密码")
        if len(password) < 8:
            raise CommandError("初始密码至少 8 位")
        if Account.all_objects.filter(username=username).exists():
            raise CommandError(f"账号 {username} 已存在，命令拒绝重复创建")

        Account.objects.create(
            id=new_account_id(),
            username=username,
            display_name=options["display_name"],
            roles=["ADMIN"],
            organization=None,
            enabled=True,
            password=make_password(password),
            must_change_password=True,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"已创建全局管理员 {username}（无组织，全库范围，首次登录需改密）"
            )
        )
