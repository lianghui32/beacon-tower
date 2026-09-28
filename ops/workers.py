"""
ops/workers.py — 运维中心后台任务：拨测循环 + 定时巡检循环
由 monitor.workers.start_workers 一并拉起；跨进程只由持有租约的副本执行。
"""
import logging
import time

from django.conf import settings

MON = settings.OBSERVABILITY

logger = logging.getLogger(__name__)

INSPECT_INTERVAL_HOURS = float(__import__('os').environ.get('OBS_INSPECT_INTERVAL_HOURS', '12'))

_last_prune = [0.0]  # 拨测清理节拍（可变容器，便于测试重置）


def probe_round():
    """一轮拨测：跑到期任务 + 每小时清理结果与审计日志"""
    from datetime import timedelta

    from django.utils import timezone

    from .models import AuditLog
    from .probing import probe_due_tasks, prune_results
    probe_due_tasks()
    if time.time() - _last_prune[0] > 3600:
        _last_prune[0] = time.time()
        prune_results(days=MON['RETENTION_DAYS'])
        # 审计日志保留 90 天，防止无限膨胀拖慢审计页
        n, _ = AuditLog.objects.filter(
            created_at__lt=timezone.now() - timedelta(days=90)).delete()
        if n:
            logger.info('已清理 %d 条过期审计日志', n)


def probe_loop():
    """拨测调度循环（5 秒扫描到期任务）：租约保证多副本只拨测一份"""
    from monitor.leadership import LeaseLoop

    LeaseLoop('probe', probe_round, 5).run()


def inspect_round():
    """一轮巡检。返回下次等待秒数：失败缩短到 10 分钟重试，
    免得一次异常就要再等一个完整周期。"""
    from .audit import audit_system
    from .inspection import run_inspection
    try:
        run, result = run_inspection(trigger='scheduled')
        audit_system('定时巡检完成', f'评分 {result["score"]}',
                     f"不合格 {result['fails']} 项 / 警告 {result['warns']} 项")
        return INSPECT_INTERVAL_HOURS * 3600
    except Exception:
        logger.exception('定时巡检异常')
        return min(600, INSPECT_INTERVAL_HOURS * 3600)


def inspect_loop():
    """定时巡检循环：启动即巡检一次，之后每 OBS_INSPECT_INTERVAL_HOURS 小时一次"""
    from monitor.leadership import LeaseLoop

    LeaseLoop('inspect', inspect_round, INSPECT_INTERVAL_HOURS * 3600).run()
