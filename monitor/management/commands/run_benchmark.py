"""
run_benchmark 命令：对问题版 / 优化版路由做对比压测，输出对比表

用法：
    python manage.py run_benchmark                # 每个路由压 20 次
    python manage.py run_benchmark --iterations 50
"""
from django.core.management.base import BaseCommand

from monitor.services import run_benchmark_suite


class Command(BaseCommand):
    help = '对比压测论坛问题版 / 优化版路由，输出性能对比表'

    def add_arguments(self, parser):
        parser.add_argument('--iterations', type=int, default=20, help='每个路由的请求次数（默认20）')

    def handle(self, *args, **options):
        # 与页面口径一致：上限 100，防止 --iterations 过大拖垮采集库
        iterations = min(100, max(1, options['iterations']))
        self.stdout.write(f'开始压测：每个路由 {iterations} 次（进程内请求，同时写入采集表）...\n')
        results = run_benchmark_suite(iterations=iterations)

        header = (
            f'{"版本":<8}{"路由":<22}{"平均(ms)":>10}{"最小(ms)":>10}'
            f'{"最大(ms)":>10}{"P95(ms)":>10}{"平均SQL":>10}'
        )
        self.stdout.write(header)
        self.stdout.write('-' * len(header))
        for r in results:
            self.stdout.write(
                f'{r["label"]:<8}{r["url"]:<22}{r["avg_ms"]:>10}{r["min_ms"]:>10}'
                f'{r["max_ms"]:>10}{r["p95_ms"]:>10}{r["avg_sql"]:>10}'
            )
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            '压测完成。采集数据已写入 RequestMetric，可在 /monitor/ 面板查看趋势。'
        ))
