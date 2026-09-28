"""
monitor/workers.py — 后台采集/告警线程的启动器

runserver 下只在自动重载的子进程里启动一次（RUN_MAIN=1），
migrate / shell / init_data 等管理命令不启动；gunicorn/uwsgi 等部署方式默认启动。
线程全部为 daemon，随进程退出。

多进程/多副本部署：这些线程各自去抢任务租约（monitor/leadership.py），
全集群同一任务只有一个进程真正干活，其余热待命并在持有者宕机后自动接管。
OBS_WORKERS_ENABLED=1/0 仍可强制指定/排除某个进程参与（老部署习惯的兜底）。
"""
import logging
import os
import sys
import threading

from django.conf import settings

from . import leadership

logger = logging.getLogger(__name__)

_started = False
_start_lock = threading.Lock()


def _should_start():
    if os.environ.get('OBS_DISABLE_WORKERS') == '1':
        return False
    # 多进程部署仲裁：显式设置 OBS_WORKERS_ENABLED（1/0）优先
    forced = os.environ.get('OBS_WORKERS_ENABLED')
    if forced is not None:
        return forced == '1'
    if 'runserver' in sys.argv:
        # 自动重载模式：仅子进程（RUN_MAIN=1）启动；--noreload 模式：本进程即服务进程
        return os.environ.get('RUN_MAIN') == '1' or '--noreload' in sys.argv
    command = (sys.argv[1] if len(sys.argv) > 1 else '')
    if command in settings.WORKER_START_EXCLUDE_CMDS:
        return False
    return True


def start_workers():
    """启动：主机采集 / 告警评估 / 拨测 / 定时巡检 线程（幂等、线程安全）"""
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        # 先完成全部 import，避免导入到一半失败导致部分线程已启动、状态不一致
        from hosts.collector import host_collector_loop
        from alerts.engine import alert_engine_loop
        from ops.workers import probe_loop, inspect_loop

        threads = (
            ('obs-host-collector', host_collector_loop, 'host-collector'),
            ('obs-alert-engine', alert_engine_loop, 'alert-engine'),
            ('obs-probe', probe_loop, 'probe'),
            ('obs-inspect', inspect_loop, 'inspect'),
        )
        started = 0
        leases = []
        for name, target, lease_name in threads:
            try:
                threading.Thread(target=target, name=name, daemon=True).start()
                started += 1
                leases.append(lease_name)
            except Exception:
                logger.exception('后台线程 %s 启动失败', name)
        _started = True
        logger.info('后台线程已启动：%d/%d（进程身份 %s）',
                    started, len(threads), leadership.holder())
        # 正常退出时交还租约，让其它副本立即接管而不是等 TTL 过期
        leadership.register_exit_release(leases)


def maybe_start_workers():
    # 请求指标缓冲线程在 Web 进程里也必须起：指标由 Web 进程产生，
    # 而 Web 角色通常设 OBS_DISABLE_WORKERS=1（不跑采集/评估线程）。
    from . import buffer
    try:
        buffer.start()
    except Exception:
        logger.exception('请求指标缓冲线程启动失败')
    if _should_start():
        try:
            start_workers()
        except Exception:
            # 工作线程启动失败不影响 Web 服务
            logger.exception('后台线程启动失败')
