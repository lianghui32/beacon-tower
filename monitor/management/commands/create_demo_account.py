"""
create_demo_account 命令：创建/重置只读演示账号（用于向他人展示平台）

用法：
    python manage.py create_demo_account                     # 用户名 demo，随机密码
    python manage.py create_demo_account --username guest    # 自定义用户名
    python manage.py create_demo_account --password MyPass123 # 指定密码（不推荐，会留在命令历史）
    python manage.py create_demo_account --revoke            # 停用演示账号

演示账号属于"演示访客"组：仅开放查询与可视化页面，
管理配置 / 数据上报 / 写操作被中间件拦截（见 monitor/security.py）。

安全约束：
- 拒绝操作 staff / superuser 账号（防止把管理员降权劫持为已知密码的演示号）；
- --revoke 不再"先创建再停用"，不存在的账号直接提示退出；
- 凭据文件权限收紧到仅属主可读（Unix）。
"""
import os
import secrets
import sys
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError

from monitor.security import DEMO_GROUP_NAME


class Command(BaseCommand):
    help = '创建/重置/停用只读演示账号'

    def add_arguments(self, parser):
        parser.add_argument('--username', default='demo', help='演示账号用户名（默认 demo）')
        parser.add_argument('--password', default='', help='指定密码；缺省自动生成随机密码')
        parser.add_argument('--revoke', action='store_true', help='停用该演示账号')
        parser.add_argument('--force', action='store_true',
                            help='连同已存在的 staff/超管同名账号一起重置（危险，默认拒绝）')

    def handle(self, *args, **options):
        username = options['username'][:30]
        existing = User.objects.filter(username=username).first()

        if options['revoke']:
            if not existing:
                raise CommandError(f'账号 {username} 不存在，无需停用。')
            existing.is_active = False
            existing.save(update_fields=['is_active'])
            self.stdout.write(self.style.SUCCESS(f'演示账号 {username} 已停用。'))
            return

        if existing and (existing.is_staff or existing.is_superuser) and not options['force']:
            raise CommandError(
                f'账号 {username} 是管理员账号，拒绝重置为演示账号（会把管理员降权为'
                f'已知密码的只读账号）。确认无误请加 --force。'
            )

        group, _ = Group.objects.get_or_create(name=DEMO_GROUP_NAME)
        if existing:
            user = existing
            created = False
        else:
            user = User.objects.create(username=username, first_name='演示访客')
            created = True

        user.is_active = True
        user.is_staff = False
        user.is_superuser = False
        password = options['password'] or secrets.token_urlsafe(9)
        user.set_password(password)
        user.save()
        user.groups.add(group)

        cred_file = Path(settings.BASE_DIR) / 'demo_credentials.txt'
        cred_file.write_text(
            f'# 只读演示账号（生成时间 {datetime.now():%Y-%m-%d %H:%M}）\n'
            f'username={username}\npassword={password}\n'
            f'# 权限：仅查询与可视化页面；管理入口/数据上报/写操作已被拦截\n',
            encoding='utf-8',
        )
        if sys.platform != 'win32':
            try:
                os.chmod(cred_file, 0o600)
            except OSError:
                pass
        self.stdout.write(self.style.SUCCESS(
            f'演示账号就绪：{username}（{"新建" if created else "重置"}，'
            f'凭据见 {cred_file.name}，演示模式自动生效）'
        ))
