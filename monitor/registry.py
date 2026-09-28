"""
monitor/registry.py — 指标注册表（全平台统一取数入口）

总览页、自定义大盘、告警策略下拉、智能分析共用：
每个指标 key 对应 {标签, 单位, series(minutes)->按分钟聚合的点列}。

新增一个可观测指标只需在这里注册一项，大盘/告警/分析立即生效。
"""
from django.db.models import Avg, Count, Sum
from django.db.models.functions import TruncMinute
from django.utils import timezone

# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _since(minutes):
    return timezone.now() - timezone.timedelta(minutes=minutes)


def _bucket(qs, annotate_map, ts_field='created_at'):
    """按分钟聚合，返回 [(时间字符串, dict(...))] 升序"""
    rows = (
        qs.annotate(bucket=TruncMinute(ts_field))
        .values('bucket')
        .annotate(**annotate_map)
        .order_by('bucket')
    )
    return [(r['bucket'], r) for r in rows]


def _fmt(dt):
    from django.utils import timezone as tz
    return tz.localtime(dt).strftime('%H:%M')


def _p95(values):
    if not values:
        return 0.0
    vs = sorted(values)
    k = max(0, min(len(vs) - 1, int(len(vs) * 0.95)))
    return float(vs[k])


# ---------------------------------------------------------------------------
# HTTP / APM 指标（RequestMetric）
# ---------------------------------------------------------------------------

def http_request_count(minutes):
    from .models import RequestMetric
    pts = _bucket(
        RequestMetric.objects.filter(created_at__gte=_since(minutes)),
        {'v': Count('id')},
    )
    return [{'t': _fmt(t), 'v': float(r['v'] or 0)} for t, r in pts]


def http_avg_duration(minutes):
    from .models import RequestMetric
    pts = _bucket(
        RequestMetric.objects.filter(created_at__gte=_since(minutes)),
        {'v': Avg('duration_ms')},
    )
    return [{'t': _fmt(t), 'v': round(float(r['v'] or 0), 1)} for t, r in pts]


def http_p95_duration(minutes):
    from .models import RequestMetric
    qs = RequestMetric.objects.filter(created_at__gte=_since(minutes))
    per_min = {}
    for ts, dur in qs.values_list('created_at', 'duration_ms'):
        key = ts.replace(second=0, microsecond=0)
        per_min.setdefault(key, []).append(dur)
    out = []
    for t in sorted(per_min):
        out.append({'t': _fmt(t), 'v': round(_p95(per_min[t]), 1)})
    return out


def http_error_rate(minutes):
    from django.db.models import Case, When
    from .models import RequestMetric
    pts = _bucket(
        RequestMetric.objects.filter(created_at__gte=_since(minutes)),
        {'total': Count('id'),
         'errs': Sum(Case(When(is_error=True, then=1), default=0))},
    )
    return [
        {'t': _fmt(t), 'v': round((r['errs'] or 0) * 100.0 / r['total'], 2) if r['total'] else 0.0}
        for t, r in pts
    ]


def http_sql_avg(minutes):
    from .models import RequestMetric
    pts = _bucket(
        RequestMetric.objects.filter(created_at__gte=_since(minutes)),
        {'v': Avg('sql_count')},
    )
    return [{'t': _fmt(t), 'v': round(float(r['v'] or 0), 1)} for t, r in pts]


def http_slow_query_count(minutes):
    from .models import RequestMetric
    pts = _bucket(
        RequestMetric.objects.filter(created_at__gte=_since(minutes)),
        {'v': Sum('slow_query_count')},
    )
    return [{'t': _fmt(t), 'v': float(r['v'] or 0)} for t, r in pts]


# ---------------------------------------------------------------------------
# 主机指标（hosts.HostMetric）
# ---------------------------------------------------------------------------

def _host_series(field, minutes, scale=1.0):
    """主机指标序列：多主机时取各主机分钟峰值的最大值（单主机等价于该机读数）"""
    from django.db.models import Max

    from hosts.models import HostMetric
    pts = _bucket(
        HostMetric.objects.filter(created_at__gte=_since(minutes)),
        {'v': Max(field)},
    )
    return [{'t': _fmt(t), 'v': round(float(r['v'] or 0) * scale, 1)} for t, r in pts]


def host_cpu(minutes):
    return _host_series('cpu_percent', minutes)


def host_mem(minutes):
    return _host_series('mem_percent', minutes)


def host_disk(minutes):
    return _host_series('disk_percent', minutes)


def host_net_recv(minutes):
    return _host_series('net_recv_kbps', minutes)


def host_net_sent(minutes):
    return _host_series('net_sent_kbps', minutes)


# ---------------------------------------------------------------------------
# 前端 RUM 指标（rum.RumEvent）
# ---------------------------------------------------------------------------

def rum_pv(minutes):
    from rum.models import RumEvent
    pts = _bucket(
        RumEvent.objects.filter(type='pv', created_at__gte=_since(minutes)),
        {'v': Count('id')},
    )
    return [{'t': _fmt(t), 'v': float(r['v'] or 0)} for t, r in pts]


def rum_load_time(minutes):
    from rum.models import RumEvent
    pts = _bucket(
        RumEvent.objects.filter(type='perf', created_at__gte=_since(minutes)),
        {'v': Avg('load_ms')},
    )
    return [{'t': _fmt(t), 'v': round(float(r['v'] or 0), 1)} for t, r in pts]


def rum_js_errors(minutes):
    from rum.models import RumEvent
    pts = _bucket(
        RumEvent.objects.filter(type='error', created_at__gte=_since(minutes)),
        {'v': Count('id')},
    )
    return [{'t': _fmt(t), 'v': float(r['v'] or 0)} for t, r in pts]


def rum_api_error_rate(minutes):
    from django.db.models import Case, When
    from rum.models import RumEvent
    pts = _bucket(
        RumEvent.objects.filter(type='api', created_at__gte=_since(minutes)),
        {'total': Count('id'),
         'errs': Sum(Case(When(api_ok=False, then=1), default=0))},
    )
    return [
        {'t': _fmt(t), 'v': round((r['errs'] or 0) * 100.0 / r['total'], 2) if r['total'] else 0.0}
        for t, r in pts
    ]


# ---------------------------------------------------------------------------
# 日志指标（loghub.LogEntry）
# ---------------------------------------------------------------------------

def _log_count(level, minutes):
    from loghub.models import LogEntry
    pts = _bucket(
        LogEntry.objects.filter(level=level, created_at__gte=_since(minutes)),
        {'v': Count('id')},
    )
    return [{'t': _fmt(t), 'v': float(r['v'] or 0)} for t, r in pts]


def log_errors(minutes):
    return _log_count('ERROR', minutes)


def log_warnings(minutes):
    return _log_count('WARNING', minutes)


# ---------------------------------------------------------------------------
# 自定义上报指标（monitor.CustomMetric）：key 形如 custom.<name>
# ---------------------------------------------------------------------------

def custom_metric(name, minutes):
    """自定义指标按分钟序列；指标名不存在时返回 None（区别于"存在但窗口内无数据"）"""
    from .models import CustomMetric
    if not CustomMetric.objects.filter(name=name).exists():
        return None
    pts = _bucket(
        CustomMetric.objects.filter(name=name, created_at__gte=_since(minutes)),
        {'v': Avg('value')},
    )
    return [{'t': _fmt(t), 'v': round(float(r['v'] or 0), 3)} for t, r in pts]


# ---------------------------------------------------------------------------
# 注册表本体
# ---------------------------------------------------------------------------

# key -> (标签, 单位, series 函数)
REGISTRY = {
    'http.request_count': ('请求数/分钟', '次/分', http_request_count),
    'http.avg_duration': ('接口平均耗时', 'ms', http_avg_duration),
    'http.p95_duration': ('接口 P95 耗时', 'ms', http_p95_duration),
    'http.error_rate': ('接口错误率', '%', http_error_rate),
    'http.sql_avg': ('平均 SQL 次数', '条/请求', http_sql_avg),
    'http.slow_query_count': ('慢查询数', '条/分', http_slow_query_count),
    'host.cpu_percent': ('主机 CPU 使用率', '%', host_cpu),
    'host.mem_percent': ('主机内存使用率', '%', host_mem),
    'host.disk_percent': ('主机磁盘使用率', '%', host_disk),
    'host.net_recv_kbps': ('下行流量', 'KB/s', host_net_recv),
    'host.net_sent_kbps': ('上行流量', 'KB/s', host_net_sent),
    'rum.pv': ('前端 PV', '次/分', rum_pv),
    'rum.load_time_avg': ('页面平均加载时长', 'ms', rum_load_time),
    'rum.js_error_count': ('JS 错误数', '条/分', rum_js_errors),
    'rum.api_error_rate': ('前端 API 错误率', '%', rum_api_error_rate),
    'log.error_count': ('ERROR 日志数', '条/分', log_errors),
    'log.warn_count': ('WARNING 日志数', '条/分', log_warnings),
}


def series(key, minutes):
    """按 key 取指标序列；custom.<name> 与 probe.<task_id>.<field> 动态分发。
    返回 None 表示 key 不存在。"""
    if key.startswith('custom.'):
        name = key[len('custom.'):]
        pts = custom_metric(name, minutes)
        if pts is None:
            return None  # 自定义指标不存在：与未知 registry key 一致返回 None
        label = f'自定义指标 {name}'
        return {'key': key, 'label': label, 'unit': '', 'points': pts}
    if key.startswith('probe.'):
        try:
            parts = key.split('.')
            task_id, field = int(parts[1]), parts[2]
        except (IndexError, ValueError):
            return None
        if field not in ('ok_rate', 'latency', 'duration_ms', 'cert_days'):
            return None  # 拨测指标字段白名单
        from ops import probing
        pts = probing.task_series(task_id, field, minutes)
        return {'key': key, 'label': f'拨测指标 {key}', 'unit': '%' if field == 'ok_rate' else 'ms',
                'points': pts}
    item = REGISTRY.get(key)
    if not item:
        return None
    label, unit, fn = item
    return {'key': key, 'label': label, 'unit': unit, 'points': fn(minutes)}


def metric_value(key, minutes=5):
    """取最近 N 分钟的均值（告警引擎用）；无数据返回 None"""
    s = series(key, minutes)
    if not s or not s['points']:
        return None
    vals = [p['v'] for p in s['points'] if p['v'] is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def catalog():
    """给下拉框用：[{key,label,unit}]，含 custom.* 与 probe.* 动态指标"""
    import logging

    items = [
        {'key': k, 'label': f'{v[0]} ({v[1]})', 'unit': v[1]}
        for k, v in REGISTRY.items()
    ]
    from .models import CustomMetric
    names = set(CustomMetric.objects.values_list('name', flat=True).distinct()[:50])
    items += [{'key': f'custom.{n}', 'label': f'自定义指标 {n}', 'unit': ''} for n in sorted(names)]
    try:
        from ops.probing import probe_catalog
        items += probe_catalog()
    except Exception:
        logging.getLogger(__name__).exception('probe_catalog 取数失败')
    return items
