"""
monitor/services.py — 数据聚合与基准压测服务

总览 / APM / 自定义大盘的所有图表数据都由这里的函数提供；
run_benchmark 命令与 /monitor/benchmark/ 页面共用 run_benchmark_suite()。
"""
import json
import statistics
import time
from datetime import timedelta

from django.conf import settings
from django.db.models import Avg, Count, Max, Sum
from django.test import Client
from django.utils import timezone

from . import buffer
from .models import RequestMetric

MON = settings.OBSERVABILITY


def _mon_settings():
    return settings.OBSERVABILITY


def _since(minutes):
    return timezone.now() - timedelta(minutes=minutes)


def _pctl(values, p=95):
    if not values:
        return 0.0
    vs = sorted(values)
    k = max(0, min(len(vs) - 1, int(len(vs) * p / 100)))
    return float(vs[k])


# ===================== 监控总览 =====================

def overview_data(minutes=60):
    """总览页全部数据：KPI + 趋势 + 主机 + 错误分布 + 服务健康 + 最近告警"""
    from alerts.models import AlertEvent
    from hosts.models import HostMetric
    from monitor.registry import series

    qs = RequestMetric.objects.filter(created_at__gte=_since(minutes))
    total = qs.count()
    errors = qs.filter(is_error=True).count()
    slow = qs.filter(duration_ms__gt=MON['SLOW_REQUEST_MS']).count()

    latest_host = HostMetric.objects.order_by('-created_at').first()
    firing = AlertEvent.objects.filter(status='firing').select_related('policy')[:10]

    # 服务健康表：按路径聚合
    from django.db.models import Q
    services = list(
        qs.values('path').annotate(
            n=Count('id'), avg_ms=Avg('duration_ms'), errs=Count('id', filter=Q(is_error=True)),
        ).order_by('-n')[:12]
    )
    for s in services:
        s['avg_ms'] = round(s['avg_ms'] or 0, 1)
        s['error_rate'] = round(s['errs'] * 100.0 / s['n'], 2) if s['n'] else 0
        s['health'] = ('down' if s['error_rate'] > 5 else
                       'warn' if s['error_rate'] > 0 or s['avg_ms'] > MON['SLOW_REQUEST_MS'] else 'up')

    error_dist = list(
        qs.filter(is_error=True).values('status_code').annotate(n=Count('id')).order_by('-n')
    )

    return {
        'kpi': {
            'total': total,
            'avg_ms': round(qs.aggregate(v=Avg('duration_ms'))['v'] or 0, 1),
            'p95_ms': round(_pctl(list(qs.values_list('duration_ms', flat=True)[:5000])), 1),
            'error_rate': round(errors * 100.0 / total, 2) if total else 0,
            'errors': errors,
            'slow': slow,
            'sql_total': qs.aggregate(v=Sum('sql_count'))['v'] or 0,
        },
        'host': {
            'cpu': latest_host.cpu_percent if latest_host else 0,
            'mem': latest_host.mem_percent if latest_host else 0,
            'disk': latest_host.disk_percent if latest_host else 0,
        },
        'firing_alerts': [
            {'level': e.level, 'summary': e.summary,
             'since': timezone.localtime(e.started_at).strftime('%H:%M:%S')}
            for e in firing
        ],
        'series': {
            'request_count': series('http.request_count', minutes)['points'],
            'avg_duration': series('http.avg_duration', minutes)['points'],
            'error_rate': series('http.error_rate', minutes)['points'],
            'host_cpu': series('host.cpu_percent', minutes)['points'],
            'host_mem': series('host.mem_percent', minutes)['points'],
        },
        'services': services,
        'error_dist': error_dist,
    }


# ===================== APM =====================

def apm_transactions(minutes=60):
    """接口（事务）分析表：每条路径的量 / 耗时分布 / 错误率 / SQL

    数值聚合（量/均值/最大/错误率/慢查询）下推到数据库一条 GROUP BY 完成；
    P95 需要原始耗时分布，仅拉取时间窗内最近 20000 行（有界内存）。
    """
    from django.db.models import Q
    qs = RequestMetric.objects.filter(created_at__gte=_since(minutes))
    agg_rows = list(
        qs.values('path').annotate(
            n=Count('id'),
            avg_ms=Avg('duration_ms'), max_ms=Max('duration_ms'),
            errs=Count('id', filter=Q(is_error=True)),
            avg_sql=Avg('sql_count'), slow=Sum('slow_query_count'),
        )
    )
    agg_by_path = {r['path']: r for r in agg_rows}

    # P95 分位数：拉取窗口内（最近优先、上限 2 万行）的耗时样本分组
    durs_by_path = {}
    for path, dur in qs.order_by('-created_at').values_list('path', 'duration_ms')[:20000]:
        durs_by_path.setdefault(path, []).append(dur)

    rows = []
    for path, a in agg_by_path.items():
        n = a['n'] or 1
        durs = durs_by_path.get(path) or []
        rows.append({
            'path': path,
            'n': a['n'],
            'avg_ms': round(a['avg_ms'] or 0, 1),
            'p95_ms': round(_pctl(durs), 1),
            'max_ms': round(a['max_ms'] or 0, 1),
            'error_rate': round((a['errs'] or 0) * 100.0 / n, 2),
            'avg_sql': round(a['avg_sql'] or 0, 1),
            'slow_queries': a['slow'] or 0,
        })
    rows.sort(key=lambda r: -r['avg_ms'])
    return rows


def apm_traces(minutes=60, only_slow=False, only_error=False, limit=50):
    """最近调用链列表"""
    qs = RequestMetric.objects.filter(created_at__gte=_since(minutes))
    if only_slow:
        qs = qs.filter(duration_ms__gt=MON['SLOW_REQUEST_MS'])
    if only_error:
        qs = qs.filter(is_error=True)
    return [
        {
            'trace_id': m.trace_id,
            'path': m.path,
            'method': m.method,
            'status': m.status_code,
            'duration_ms': m.duration_ms,
            'sql_count': m.sql_count,
            'is_error': m.is_error,
            'time': timezone.localtime(m.created_at).strftime('%m-%d %H:%M:%S'),
        }
        for m in qs.order_by('-created_at')[:limit]
    ]


def get_trace(trace_id):
    """单条调用链：指标 + span 切片"""
    m = RequestMetric.objects.filter(trace_id=trace_id).first()
    if not m:
        return None
    try:
        spans = json.loads(m.spans)
    except (ValueError, TypeError):
        spans = []
    try:
        slow = json.loads(m.slow_queries)
    except (ValueError, TypeError):
        slow = []
    return {'metric': m, 'spans': spans, 'slow_queries': slow}


def apm_database(minutes=1440):
    """数据库分析：慢查询模板聚合 + 各路径 SQL 统计"""
    qs = RequestMetric.objects.filter(created_at__gte=_since(minutes))
    by_path = list(
        qs.values('path').annotate(
            n=Count('id'), avg_sql=Avg('sql_count'), avg_sql_ms=Avg('sql_time_ms'),
            slow_total=Sum('slow_query_count'),
        ).order_by('-avg_sql')[:10]
    )
    for row in by_path:
        row['avg_sql'] = round(row['avg_sql'] or 0, 1)
        row['avg_sql_ms'] = round(row['avg_sql_ms'] or 0, 1)

    # 慢查询模板聚合（按语句前缀归并）
    templates = {}
    examples = (
        RequestMetric.objects.filter(created_at__gte=_since(minutes), slow_query_count__gt=0)
        .order_by('-created_at').values_list('slow_queries', 'path', 'created_at')[:300]
    )
    for payload, path, ts in examples:
        try:
            items = json.loads(payload)
        except (ValueError, TypeError):
            continue
        for it in items:
            sql = ' '.join((it.get('sql') or '').split())
            key = sql[:90]
            t = templates.setdefault(key, {'sql': key, 'n': 0, 'max_ms': 0, 'paths': set(), 'last': ts})
            t['n'] += 1
            t['max_ms'] = max(t['max_ms'], it.get('time_ms', 0))
            t['paths'].add(path)
            t['last'] = max(t['last'], ts)
    slow_templates = [
        {'sql': t['sql'], 'n': t['n'], 'max_ms': round(t['max_ms'], 1),
         'paths': sorted(t['paths'])[:3],
         'last': timezone.localtime(t['last']).strftime('%m-%d %H:%M')}
        for t in sorted(templates.values(), key=lambda x: -x['n'])[:12]
    ]
    return {'by_path': by_path, 'slow_templates': slow_templates}


# ===================== 旧版兼容接口（面板 AJAX） =====================

def get_summary():
    """顶部汇总卡片数据"""
    agg = RequestMetric.objects.aggregate(
        total=Count('id'),
        avg_ms=Avg('duration_ms'),
        max_ms=Max('duration_ms'),
        sql_total=Sum('sql_count'),
    )
    slow_req = RequestMetric.objects.filter(
        duration_ms__gt=_mon_settings()['SLOW_REQUEST_MS'],
    ).count()
    total = agg['total'] or 0
    return {
        'total_requests': total,
        'avg_ms': round(agg['avg_ms'] or 0, 1),
        'max_ms': round(agg['max_ms'] or 0, 1),
        'sql_total': agg['sql_total'] or 0,
        'slow_requests': slow_req,
        'slow_ratio': round(slow_req * 100.0 / total, 1) if total else 0.0,
    }


def get_top_slow(n=10):
    """最慢的 N 个请求"""
    qs = RequestMetric.objects.order_by('-duration_ms')[:n]
    return [
        {
            'path': m.path,
            'method': m.method,
            'status': m.status_code,
            'duration_ms': m.duration_ms,
            'sql_count': m.sql_count,
            'slow_query_count': m.slow_query_count,
            'created_at': timezone.localtime(m.created_at).strftime('%m-%d %H:%M:%S'),
        }
        for m in qs
    ]


# ===================== 基准压测 =====================

def _bench_route(client, url, iterations, warmup=2):
    """对单个路由压测 iterations 次，返回耗时统计

    先跑 warmup 轮预热（建立会话/命中缓存，不计入统计），避免首请求
    的冷启动开销污染均值——首轮请求常比稳态慢两个数量级。
    """
    for _ in range(warmup):
        client.get(url)
    durations = []
    for _ in range(iterations):
        start = time.perf_counter()
        client.get(url)
        durations.append((time.perf_counter() - start) * 1000)
    return {
        'url': url,
        'iterations': iterations,
        'avg_ms': round(statistics.mean(durations), 1),
        'min_ms': round(min(durations), 1),
        'max_ms': round(max(durations), 1),
        'p95_ms': round(_pctl(durations), 1),
    }


def _benchmark_client():
    """压测专用客户端：整站登录门禁下，匿名请求只会被 302 到登录页，
    采集中间件根本不会运行——必须用已登录会话去压真实页面。

    使用固定的 benchmark 服务账号（密码不可用，无法被外部登录）。
    """
    from django.contrib.auth.models import User


    user = User.objects.filter(username='benchmark').first()
    if user is None:
        user = User.objects.create_user('benchmark', '', None)
    client = Client()
    client.force_login(user)
    return client


def run_benchmark_suite(iterations=20):
    """对比压测 问题版 / 优化版 帖子列表页

    使用 Django test Client 在进程内发请求（不走真实 HTTP，
    避免单线程 dev server 自请求死锁），中间件仍会正常采集。
    预热轮不计入统计也不计入指标窗口；指标聚合按"计量轮开始时间"切分，
    避免上一轮压测/预热的数据混入。
    """
    client = _benchmark_client()
    routes = [
        ('/forum/problem/', '问题版'),
        ('/forum/optimized/', '优化版'),
    ]
    results = []
    for url, label in routes:
        client.get(url)  # 预热 1 轮（不计入统计与指标窗口）
        buffer.flush()   # 预热点先落库并丢弃，别混进计量窗口
        bench_start = timezone.now()
        stats = _bench_route(client, url, iterations, warmup=0)
        # 请求指标走批量缓冲（monitor/buffer.py），聚合前必须排空，否则窗口里数不到本轮
        buffer.flush()
        agg = RequestMetric.objects.filter(path=url, created_at__gte=bench_start).aggregate(
            avg_sql=Avg('sql_count'), avg_ms=Avg('duration_ms'), n=Count('id'),
        )
        stats['label'] = label
        stats['avg_sql'] = round(agg['avg_sql'] or 0, 1)
        stats['metric_avg_ms'] = round(agg['avg_ms'] or 0, 1)
        stats['metric_count'] = agg['n'] or 0
        results.append(stats)
    return results
