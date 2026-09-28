"""
obs_workers — 独立后台 worker 进程：主机采集 / 告警评估 / 拨测 / 定时巡检

生产部署的推荐形态（web 与后台任务分离）：
- web 角色（gunicorn，可多副本）：设 OBS_DISABLE_WORKERS=1，只处理请求；
- worker 角色（本命令）：承担周期性后台任务。副本数不限——四个任务各自有一把
  租约（monitor/leadership.py），全集群同一任务只有一个进程执行，
  持有者宕机后其它 worker 在 TTL 内自动接管。

    python manage.py obs_workers

配合 docker-compose.yml 的 worker 服务使用（--scale worker=N 安全）。
"""
import time

from django.core.management.base import BaseCommand

from monitor.workers import start_workers


class Command(BaseCommand):
    help = '启动后台采集/告警/拨测/巡检线程并常驻（worker 角色，副本数不限，内部租约选主）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once', action='store_true',
            help='启动线程后立即退出（供冒烟测试/CI 验证线程可拉起）',
        )

    def handle(self, *args, **opts):
        start_workers()
        self.stdout.write(self.style.SUCCESS('后台 worker 线程已启动'))
        if opts['once']:
            return
        try:
            while True:
                time.sleep(3600)  # daemon 线程干活，主线程保活即可
        except KeyboardInterrupt:
            self.stdout.write('worker 收到退出信号')
