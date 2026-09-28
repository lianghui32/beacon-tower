"""
hosts/views.py — 主机监控页 + AJAX 数据 + Agent 上报端点

多主机：页面通过 ?hostname= 选择主机（下拉框）；
远程服务器跑 agent/obs_agent.py 推送数据到 /api/ingest/host/（令牌保护）。
"""
import logging
import socket

from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from monitor.security import rate_limit, require_ingest

from .collector import sample_once
from .models import HostMetric


def _range_minutes(request, default=60):
    try:
        m = int(request.GET.get('minutes', default))
    except (TypeError, ValueError):
        m = default
    return max(5, min(4320, m))


def _known_hosts():
    """保留期内（默认 7 天）有过数据的主机名，按最新采样时间倒序"""
    from datetime import timedelta

    from django.conf import settings
    from django.db.models import Max

    days = settings.OBSERVABILITY.get('RETENTION_DAYS', 7)
    since = timezone.now() - timedelta(days=days)
    return list(
        HostMetric.objects.filter(created_at__gte=since)
        .values('hostname')
        .annotate(last=Max('created_at'))
        .order_by('-last')
        .values_list('hostname', flat=True)
    )


def _local_hostname():
    try:
        return socket.gethostname() or 'local'
    except Exception:
        return 'local'


@require_GET
def host_page(request):
    minutes = _range_minutes(request)
    hosts = _known_hosts()
    hostname = request.GET.get('hostname') or (hosts[0] if hosts else '')
    return render(request, 'hosts/host.html', {
        'minutes': minutes,
        'latest': HostMetric.objects.order_by('-created_at').first(),
        'hosts': hosts,
        'hostname': hostname,
    })


@require_GET
def api_host(request):
    """主机页 AJAX：趋势序列 + 汇总 + 最近采样（?hostname= 选择主机）"""
    minutes = _range_minutes(request)
    hosts = _known_hosts()
    hostname = request.GET.get('hostname') or (hosts[0] if hosts else '')
    since = timezone.now() - timezone.timedelta(minutes=minutes)
    qs = HostMetric.objects.filter(created_at__gte=since).order_by('created_at')
    if hostname:
        qs = qs.filter(hostname=hostname)

    def pts(field, rnd=1):
        return [
            {'t': timezone.localtime(m.created_at).strftime('%H:%M:%S'),
             'v': round(getattr(m, field), rnd)}
            for m in qs
        ]

    latest = (HostMetric.objects.filter(hostname=hostname).order_by('-created_at').first()
              if hostname else HostMetric.objects.order_by('-created_at').first())
    summary = {}
    if latest:
        summary = {
            'hostname': latest.hostname,
            'cpu': latest.cpu_percent,
            'cores': latest.cpu_cores,
            'load': latest.load_avg,
            'mem_percent': latest.mem_percent,
            'mem_used_gb': round(latest.mem_used_mb / 1024, 2),
            'mem_total_gb': round(latest.mem_total_mb / 1024, 2),
            'disk_percent': latest.disk_percent,
            'disk_used_gb': latest.disk_used_gb,
            'disk_total_gb': latest.disk_total_gb,
            'net_sent_kbps': latest.net_sent_kbps,
            'net_recv_kbps': latest.net_recv_kbps,
            'proc_count': latest.proc_count,
            'tcp_conns': latest.tcp_conns,
            'simulated': latest.simulated,
            'time': timezone.localtime(latest.created_at).strftime('%H:%M:%S'),
        }
    is_local = (hostname == _local_hostname())
    return JsonResponse({
        'summary': summary,
        'hosts': hosts,
        'hostname': hostname,
        'is_local': is_local,
        'cpu': pts('cpu_percent'),
        'mem': pts('mem_percent'),
        'disk': pts('disk_percent'),
        'net_sent': pts('net_sent_kbps'),
        'net_recv': pts('net_recv_kbps'),
        'load': pts('load_avg'),
        'procs': _top_processes() if is_local else [],
    })


def _top_processes(n=10):
    """本机 Top CPU 进程列表（psutil 可用时；远程主机无进程明细）"""
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                procs.append(p.info)
            except Exception:
                continue
        procs.sort(key=lambda x: (x.get('cpu_percent') or 0), reverse=True)
        return [
            {
                'pid': p['pid'],
                'name': (p.get('name') or '?')[:30],
                'cpu': round(p.get('cpu_percent') or 0, 1),
                'mem': round(p.get('memory_percent') or 0, 1),
            }
            for p in procs[:n]
        ]
    except Exception:
        return []


@require_GET
@rate_limit('host-sample', rate=20, per=60)
def api_sample_now(request):
    """立即手动触发一次本机采样（演示/调试用；psutil 全进程扫描有开销，限速防滥用）"""
    m = sample_once()
    return JsonResponse({'ok': True, 'cpu': m.cpu_percent, 'mem': m.mem_percent})


# ---------------------------------------------------------------------------
# 远程主机 Agent 上报端点
# ---------------------------------------------------------------------------

_NUM_FIELDS = [
    'cpu_percent', 'load_avg', 'mem_percent', 'mem_used_mb', 'mem_total_mb',
    'disk_percent', 'disk_used_gb', 'disk_total_gb',
    'net_sent_kbps', 'net_recv_kbps',
]
_INT_FIELDS = ['cpu_cores', 'proc_count', 'tcp_conns']


@csrf_exempt
@require_POST
@require_ingest
@rate_limit('host-ingest', rate=120, per=60)
def api_ingest_host(request):
    """远程主机 Agent 数据接收

    POST /api/ingest/host/
    Headers: X-OBS-Token: <接入令牌>
    Body: {"hostname": "web-1", "cpu_percent": 23.5, ...}（字段与 HostMetric 一致）
    """
    import json
    import math

    from .models import HostMetric
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'invalid json'}, status=400)
    if not isinstance(payload, dict) or not payload.get('hostname'):
        return JsonResponse({'ok': False, 'error': 'hostname required'}, status=400)

    # SQLite INTEGER 上限（2^63-1）：超界数值会让 create 抛 OverflowError
    _LIMIT = 2 ** 62

    def num(key, default=0.0):
        try:
            v = float(payload.get(key, default))
        except (TypeError, ValueError, OverflowError):
            return default
        if not math.isfinite(v) or abs(v) > _LIMIT:
            return default
        return v

    def integer(key, default=0):
        try:
            v = int(float(payload.get(key, default)))
        except (TypeError, ValueError, OverflowError):
            return default
        if abs(v) > _LIMIT:
            return default
        return v

    def pct(key, default=0.0):
        """百分比字段：钳制 0-100，防脏数据破坏均值/告警"""
        return round(min(100.0, max(0.0, num(key, default))), 1)

    try:
        m = HostMetric.objects.create(
            hostname=str(payload['hostname'])[:128],
            cpu_percent=pct('cpu_percent'),
            cpu_cores=max(0, integer('cpu_cores')),
            load_avg=round(max(0.0, num('load_avg')), 2),
            mem_percent=pct('mem_percent'),
            mem_used_mb=round(max(0.0, num('mem_used_mb')), 1),
            mem_total_mb=round(max(0.0, num('mem_total_mb')), 1),
            disk_percent=pct('disk_percent'),
            disk_used_gb=round(max(0.0, num('disk_used_gb')), 2),
            disk_total_gb=round(max(0.0, num('disk_total_gb')), 2),
            net_sent_kbps=round(max(0.0, num('net_sent_kbps')), 1),
            net_recv_kbps=round(max(0.0, num('net_recv_kbps')), 1),
            proc_count=max(0, integer('proc_count')),
            tcp_conns=max(0, integer('tcp_conns')),
            simulated=False,
            created_at=timezone.now(),
        )
    except Exception:
        logging.getLogger(__name__).exception('主机指标入库失败 hostname=%s', payload.get('hostname'))
        return JsonResponse({'ok': False, 'error': 'invalid metrics'}, status=400)
    try:
        from ops.models import Asset
        Asset.auto_register(m.hostname)
    except Exception:
        pass
    return JsonResponse({'ok': True, 'id': m.id, 'hostname': m.hostname})
