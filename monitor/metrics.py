"""
monitor/metrics.py — 简版 Prometheus 文本格式输出（自研，不依赖 django-prometheus）

访问 /metrics 即可得到 Prometheus 可抓取的指标（主机 / APM / RUM / 日志 / 自定义）。

格式约定（保证 Prometheus 能解析）：
- 每个 metric family 的 HELP/TYPE 只输出一次，样本行跟随其后；
- 指标名用正则白名单校验（[a-zA-Z_:][a-zA-Z0-9_:]*），脏名跳过而不是输出非法行；
- 标签值做 \\ " \n 转义。
"""
import re
import time

from django.db.models import Avg, Count, Max, Q, Sum
from django.utils import timezone

from . import buffer
from .models import CustomMetric, RequestMetric

_METRIC_NAME_RE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*$')

# 进程内缓存：Prometheus 默认 15s 抓一次，缓存 5s 内的直接复用，
# 避免每次抓取全表聚合（表增长后 /metrics 会变成最重端点）
_cache = {'text': None, 'at': 0.0}
_CACHE_TTL = 5.0


def _escape_label(v):
    """Prometheus 标签值转义：\\ -> \\\\  " -> \\" 换行 -> \\n"""
    return (str(v).replace('\\', '\\\\').replace('"', '\\"')
            .replace('\n', '\\n'))


class _Exposition:
    """帮助把样本聚簇到 metric family 下，HELP/TYPE 每个 family 只出现一次"""

    def __init__(self):
        self._families = {}   # name -> (mtype, help_text)
        self._samples = []    # [(name, labels, value)]

    def emit(self, name, mtype, help_text, value, labels=''):
        if not _METRIC_NAME_RE.match(name):
            return  # 非法指标名（如含中文/空格/数字开头）直接跳过，避免整份输出不可解析
        if value is None:
            return
        if name not in self._families:
            self._families[name] = (mtype, help_text)
        self._samples.append((name, labels, value))

    def render(self):
        lines = []
        for name, (mtype, help_text) in self._families.items():
            lines.append(f'# HELP {name} {help_text}')
            lines.append(f'# TYPE {name} {mtype}')
        for name, labels, value in self._samples:
            lines.append(f'{name}{labels} {value}')
        return '\n'.join(lines) + '\n'


def prometheus_text():
    """把各观测域数据渲染成 Prometheus exposition format"""
    return _prometheus_text_uncached()


def _prometheus_text_uncached():
    from alerts.models import AlertEvent
    from hosts.models import HostMetric
    from loghub.models import LogEntry
    from rum.models import RumEvent

    exp = _Exposition()

    # ---------- APM：请求指标 ----------
    agg = RequestMetric.objects.aggregate(
        total=Count('id'),
        sum_ms=Sum('duration_ms'),
        max_ms=Max('duration_ms'),
        sql_total=Sum('sql_count'),
        slow_total=Sum('slow_query_count'),
    )
    total = agg['total'] or 0
    err_total = RequestMetric.objects.filter(is_error=True).count()

    exp.emit('django_requests_total', 'counter', '采集到的请求总数.', total)
    exp.emit('django_request_errors_total', 'counter', '错误请求(>=400)总数.', err_total)
    exp.emit(
        'django_request_duration_seconds_sum', 'counter',
        '请求总耗时(秒).', round((agg['sum_ms'] or 0) / 1000, 4),
    )
    exp.emit(
        'django_request_duration_seconds_count', 'counter',
        '请求次数.', total,
    )
    exp.emit(
        'django_request_duration_seconds_max', 'gauge',
        '最慢请求耗时(秒).', round((agg['max_ms'] or 0) / 1000, 4),
    )
    exp.emit('django_sql_queries_total', 'counter', 'SQL 查询总次数.', agg['sql_total'] or 0)
    exp.emit('django_slow_queries_total', 'counter', '慢查询(>100ms)总次数.', agg['slow_total'] or 0)

    # ---------- 采集管道自观测（请求指标批量缓冲，monitor/buffer.py）----------
    # 平台自己也在被监控：缓冲丢弃/积压必须能被看见，不能静默丢数据。
    bs = buffer.stats()
    exp.emit('obs_metric_buffer_written_total', 'counter', '缓冲批量落库的请求指标条数.', bs['written'])
    exp.emit('obs_metric_buffer_dropped_total', 'counter',
             '缓冲丢弃的请求指标条数(队列满或落库失败).', bs['dropped'])
    exp.emit('obs_metric_buffer_errors_total', 'counter', '缓冲批量落库失败的批次数.', bs['errors'])
    exp.emit('obs_metric_buffer_pending', 'gauge', '缓冲当前待落库条数.', bs['pending'])

    # 按路径拆分（path 归一化为只含安全字符，控制标签基数）
    by_path = (
        RequestMetric.objects.values('path')
        .annotate(
            cnt=Count('id'), sum_ms=Sum('duration_ms'),
            avg_ms=Avg('duration_ms'), avg_sql=Avg('sql_count'),
            errs=Count('id', filter=Q(is_error=True)),
        )[:200]
    )
    for row in by_path:
        label = f'{{path="{_escape_label(row["path"])[:120]}"}}'
        exp.emit('django_requests_by_path_total', 'counter', '按路径统计请求数.', row['cnt'], label)
        exp.emit(
            'django_request_duration_seconds_by_path_sum', 'counter',
            '按路径统计请求总耗时(秒).', round((row['sum_ms'] or 0) / 1000, 4), label,
        )
        exp.emit(
            'django_request_duration_seconds_by_path_avg', 'gauge',
            '按路径平均请求耗时(秒).', round((row['avg_ms'] or 0) / 1000, 4), label,
        )
        exp.emit(
            'django_sql_queries_by_path_avg', 'gauge',
            '按路径平均 SQL 查询次数.', round(row['avg_sql'] or 0, 2), label,
        )
        exp.emit(
            'django_request_errors_by_path_total', 'counter',
            '按路径统计错误请求数.', row['errs'], label,
        )

    # ---------- 主机（按主机分标签；近 5 分钟活跃的主机） ----------
    from datetime import timedelta as _td
    _cutoff = timezone.now() - _td(minutes=5)
    # 一次查询取每主机最新一行（SQLite 用两次聚合近似窗口函数，避免 N+1）
    active_hosts = list(
        HostMetric.objects.filter(created_at__gte=_cutoff)
        .values_list('hostname', flat=True).distinct()[:20]
    )
    if active_hosts:
        latest_ids = (
            HostMetric.objects.filter(hostname__in=active_hosts)
            .values('hostname').annotate(latest=Max('id'))
        )
        latest_rows = {
            r['hostname']: r['latest'] for r in latest_ids
        }
        host_rows = HostMetric.objects.filter(id__in=latest_rows.values())
        for latest_host in host_rows:
            host_label = f'{{host="{_escape_label(latest_host.hostname)}"}}'
            exp.emit('host_cpu_percent', 'gauge', '主机 CPU 使用率(%).', latest_host.cpu_percent, host_label)
            exp.emit('host_memory_percent', 'gauge', '主机内存使用率(%).', latest_host.mem_percent, host_label)
            exp.emit('host_disk_percent', 'gauge', '系统盘使用率(%).', latest_host.disk_percent, host_label)
            exp.emit('host_load_avg_1m', 'gauge', '主机 1 分钟负载.', latest_host.load_avg, host_label)
            exp.emit('host_net_sent_kbps', 'gauge', '上行速率(KB/s).', latest_host.net_sent_kbps, host_label)
            exp.emit('host_net_recv_kbps', 'gauge', '下行速率(KB/s).', latest_host.net_recv_kbps, host_label)
            exp.emit('host_process_count', 'gauge', '进程数.', latest_host.proc_count, host_label)
            exp.emit('host_tcp_connections', 'gauge', 'TCP 连接数.', latest_host.tcp_conns, host_label)

    # ---------- RUM ----------
    pv_total = RumEvent.objects.filter(type='pv').count()
    js_err_total = RumEvent.objects.filter(type='error').count()
    api_total = RumEvent.objects.filter(type='api').count()
    api_err = RumEvent.objects.filter(type='api', api_ok=False).count()
    exp.emit('rum_pageviews_total', 'counter', '前端 PV 总数.', pv_total)
    exp.emit('rum_js_errors_total', 'counter', '前端 JS 错误总数.', js_err_total)
    exp.emit('rum_api_calls_total', 'counter', '前端 API 调用总数.', api_total)
    exp.emit('rum_api_errors_total', 'counter', '前端 API 失败总数.', api_err)

    # ---------- 日志 ----------
    exp.emit('log_entries_total', 'counter', '日志总量.', LogEntry.objects.count())
    exp.emit('log_errors_total', 'counter',
             'ERROR 及以上日志总量.', LogEntry.objects.filter(level__in=['ERROR', 'CRITICAL']).count())

    # ---------- 告警 ----------
    exp.emit('alert_events_firing', 'gauge', '当前触发中的告警数.',
             AlertEvent.objects.filter(status='firing').count())

    # ---------- 后台任务租约（多副本部署时谁在干活）----------
    from . import leadership
    for lease in leadership.status():
        label = f'{{task="{_escape_label(lease["name"])}"}}'
        exp.emit('obs_task_is_leader', 'gauge', '本进程是否持有该任务租约.',
                 1 if lease['mine'] else 0, label)
        exp.emit('obs_task_lease_term', 'gauge', '任务租约任期（每次易主 +1）.',
                 lease['term'], label)
        exp.emit('obs_task_lease_ttl_seconds', 'gauge', '当前持有者剩余租约(秒).',
                 lease['ttl_sec'], label)

    # ---------- 自定义指标 ----------
    names = CustomMetric.objects.values_list('name', flat=True).distinct()[:50]
    for name in names:
        latest = CustomMetric.objects.filter(name=name).order_by('-created_at').first()
        if latest and latest.value is not None:
            safe = re.sub(r'[^a-zA-Z0-9_]', '_', name).strip('_').lower() or 'metric'
            if safe[0].isdigit():
                safe = 'm_' + safe
            exp.emit(f'custom_metric_{safe}', 'gauge', f'自定义指标 {name}（最新值）.',
                     latest.value, f'{{name="{_escape_label(name)}"}}')

    return exp.render()


def prometheus_text_cached():
    """带 TTL 缓存的版本（/metrics 端点实际使用）"""
    now = time.monotonic()
    if _cache['text'] is None or now - _cache['at'] > _CACHE_TTL:
        _cache['text'] = _prometheus_text_uncached()
        _cache['at'] = now
    return _cache['text']
