"""
hosts/collector.py — 主机指标采集线程

默认使用 psutil 读取真实系统指标（CPU / 内存 / 磁盘 / 网络速率 / 进程 / 连接）；
psutil 未安装时自动退化为按正弦波生成的模拟指标，保证平台在任何环境可用。

每 OBSERVABILITY.HOST_INTERVAL_SEC 秒采样一次写入 HostMetric，
并顺手清理超过 RETENTION_DAYS 的过期采集数据。
"""
import logging
import os
import random
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

try:
    import psutil
    if psutil:
        psutil.cpu_percent(interval=None)  # 预热：首次调用恒为 0，丢弃
except ImportError:
    psutil = None

logger = logging.getLogger(__name__)

MON = settings.OBSERVABILITY

# 网络速率需要上一次采样的累计字节数（采集线程与手动 api_sample_now 并发调用，加锁保护）
_last_net = {'sent': None, 'recv': None, 'ts': None}
_last_net_lock = threading.Lock()

# 资产自动登记节流：同一主机 5 分钟内不重复 update last_seen
_asset_last_seen = {}
_asset_lock = threading.Lock()


def _disk_root():
    return os.path.abspath(os.sep)  # Windows -> C:\


def _hostname():
    name = os.environ.get('COMPUTERNAME')
    if not name and hasattr(os, 'uname'):
        name = os.uname().nodename
    return name or 'local'


def sample_once():
    """采样一次并入库，返回 HostMetric 实例"""
    now = timezone.now()
    hostname = _hostname()
    if psutil:
        return _sample_real(now, hostname)
    return _sample_simulated(now, hostname)


def _sample_real(now, hostname):
    cpu = psutil.cpu_percent(interval=None)
    if cpu < 0.5:
        # 进程刚启动时非阻塞读数不可靠（预热后仍接近 0），阻塞 150ms 重测
        cpu = psutil.cpu_percent(interval=0.15)
    mem = psutil.virtual_memory()
    try:
        load1 = psutil.getloadavg()[0]
    except (AttributeError, OSError):
        load1 = cpu / 100.0 * os.cpu_count()

    du = psutil.disk_usage(_disk_root())

    sent_kbps = recv_kbps = 0.0
    try:
        io = psutil.net_io_counters()
        with _last_net_lock:
            if _last_net['sent'] is not None and _last_net['ts'] is not None:
                dt = max(0.001, (now - _last_net['ts']).total_seconds())
                sent_kbps = max(0.0, (io.bytes_sent - _last_net['sent']) / dt / 1024)
                recv_kbps = max(0.0, (io.bytes_recv - _last_net['recv']) / dt / 1024)
            _last_net['sent'], _last_net['recv'], _last_net['ts'] = io.bytes_sent, io.bytes_recv, now
    except Exception:
        pass

    proc_count = len(psutil.pids())
    try:
        tcp = len(psutil.net_connections(kind='inet'))
    except Exception:
        tcp = 0

    return _save(now, hostname, cpu, load1, mem, du, sent_kbps, recv_kbps,
                 proc_count, tcp, simulated=False)


def _sample_simulated(now, hostname):
    """psutil 缺失时的正弦波模拟指标"""
    phase = now.timestamp() / 600
    cpu = 25 + 18 * (1 + __import__('math').sin(phase)) / 2 + random.uniform(0, 10)
    mem_pct = 55 + 10 * random.random()
    total_mb = 8192.0
    used_mb = total_mb * mem_pct / 100
    du_pct = 62.0
    return _save(now, hostname, cpu, cpu / 100,
                 _Mem(used_mb, total_mb, mem_pct),
                 _Disk(du_pct, du_pct * 5.12, 512),
                 random.uniform(10, 200), random.uniform(50, 800),
                 random.randint(120, 260), random.randint(20, 90),
                 simulated=True)


class _Mem:
    def __init__(self, used, total, percent):
        self.used = used
        self.total = total
        self.percent = percent


class _Disk:
    def __init__(self, percent, used_gb, total_gb):
        self.percent = percent
        self.used_gb = used_gb
        self.total_gb = total_gb


def _save(now, hostname, cpu, load1, mem, du, sent_kbps, recv_kbps,
          proc_count, tcp, simulated):
    from .models import HostMetric
    return HostMetric.objects.create(
        hostname=hostname[:128],
        cpu_percent=round(cpu, 1),
        cpu_cores=os.cpu_count() or 0,
        load_avg=round(float(load1), 2),
        mem_percent=round(mem.percent, 1),
        mem_used_mb=round(mem.used / 1024 / 1024, 1),
        mem_total_mb=round(mem.total / 1024 / 1024, 1),
        disk_percent=round(du.percent, 1),
        disk_used_gb=round(du.used / 1024 / 1024 / 1024, 2),
        disk_total_gb=round(du.total / 1024 / 1024 / 1024, 2),
        net_sent_kbps=round(sent_kbps, 1),
        net_recv_kbps=round(recv_kbps, 1),
        proc_count=proc_count,
        tcp_conns=tcp,
        simulated=simulated,
        created_at=now,
    )


def _register_asset(hostname):
    """资产自动登记（节流：同一主机 5 分钟才 update 一次 last_seen，减少写库）"""
    now = time.monotonic()
    with _asset_lock:
        last = _asset_last_seen.get(hostname, 0.0)
        if now - last < 300:
            return
        _asset_last_seen[hostname] = now
    try:
        from ops.models import Asset
        Asset.auto_register(hostname)
    except Exception:
        logger.exception('资产登记失败 hostname=%s', hostname)
    # 缓存过大时清理（理论上主机数量有限，防御性兜底）
    with _asset_lock:
        if len(_asset_last_seen) > 500:
            _asset_last_seen.clear()


def _prune():
    """清理超过保留期的主机采集数据"""
    from .models import HostMetric
    deadline = timezone.now() - timedelta(days=MON['RETENTION_DAYS'])
    HostMetric.objects.filter(created_at__lt=deadline).delete()


_last_prune = [0.0]  # 清理节拍（可变容器，便于测试里重置）


def host_collect_round():
    """一轮本地采集：采样 + 资产登记 + 低频清理。

    异常不在这里吞：LeaseLoop 统一记录并保住循环（采集线程是平台自身的命脉）。
    """
    m = sample_once()
    _register_asset(m.hostname)
    if time.time() - _last_prune[0] > 3600:
        _prune()
        _last_prune[0] = time.time()


def host_collector_loop():
    """后台线程主循环：只有持有租约的进程采集（多副本 worker 不会重复采同一台机器）"""
    from monitor.leadership import LeaseLoop

    LeaseLoop('host-collector', host_collect_round, MON['HOST_INTERVAL_SEC']).run()
