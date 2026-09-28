"""
analytics/reporting.py — 周期运维报表：汇总一段时间的全部观测数据

build_report(days) 返回 dict（页面渲染用）；
report_markdown(report) 把同一份 dict 渲染成 Markdown（导出用）。
"""
from datetime import timedelta

from django.db.models import Avg, Count, Max, Sum
from django.utils import timezone


def build_report(days=1):
    since = timezone.now() - timedelta(days=days)
    from alerts.models import AlertEvent
    from hosts.models import HostMetric
    from loghub.models import LogEntry
    from monitor.models import RequestMetric
    from rum.models import RumEvent

    # ---- 请求 / APM ----
    req_agg = RequestMetric.objects.filter(created_at__gte=since).aggregate(
        total=Count('id'), avg_ms=Avg('duration_ms'), max_ms=Max('duration_ms'),
        sql=Sum('sql_count'), slow=Sum('slow_query_count'),
    )
    req_total = req_agg['total'] or 0
    errors = RequestMetric.objects.filter(created_at__gte=since, is_error=True).count()
    top_slow = list(
        RequestMetric.objects.values('path')
        .annotate(n=Count('id'), avg_ms=Avg('duration_ms'), max_ms=Max('duration_ms'))
        .order_by('-avg_ms')[:8]
    )

    # ---- 主机 ----
    host_agg = HostMetric.objects.filter(created_at__gte=since).aggregate(
        cpu_avg=Avg('cpu_percent'), cpu_max=Max('cpu_percent'),
        mem_avg=Avg('mem_percent'), mem_max=Max('mem_percent'),
        disk_max=Max('disk_percent'),
    )

    # ---- 前端 RUM ----
    pv_qs = RumEvent.objects.filter(created_at__gte=since, type='pv')
    pv = pv_qs.count()
    uv = pv_qs.values('session_id').distinct().count()
    js_errors = RumEvent.objects.filter(created_at__gte=since, type='error').count()
    perf_agg = RumEvent.objects.filter(created_at__gte=since, type='perf').aggregate(
        avg_load=Avg('load_ms'), max_load=Max('load_ms'))

    # ---- 日志 ----
    log_total = LogEntry.objects.filter(created_at__gte=since).count()
    log_err = LogEntry.objects.filter(created_at__gte=since, level__in=['ERROR', 'CRITICAL']).count()
    top_logs = list(
        LogEntry.objects.filter(created_at__gte=since)
        .values('message').annotate(n=Count('id')).order_by('-n')[:5]
    )

    # ---- 告警 ----
    alerts = list(
        AlertEvent.objects.filter(started_at__gte=since)
        .select_related('policy').order_by('-started_at')[:20]
    )

    return {
        'days': days,
        'since': since,
        'generated_at': timezone.now(),
        'requests': {
            'total': req_total,
            'avg_ms': round(req_agg['avg_ms'] or 0, 1),
            'max_ms': round(req_agg['max_ms'] or 0, 1),
            'sql_total': req_agg['sql'] or 0,
            'slow_total': req_agg['slow'] or 0,
            'errors': errors,
            'error_rate': round(errors * 100.0 / req_total, 2) if req_total else 0,
        },
        'top_slow': top_slow,
        'host': {
            'cpu_avg': round(host_agg['cpu_avg'] or 0, 1),
            'cpu_max': round(host_agg['cpu_max'] or 0, 1),
            'mem_avg': round(host_agg['mem_avg'] or 0, 1),
            'mem_max': round(host_agg['mem_max'] or 0, 1),
            'disk_max': round(host_agg['disk_max'] or 0, 1),
        },
        'rum': {
            'pv': pv, 'uv': uv, 'js_errors': js_errors,
            'avg_load': round(perf_agg['avg_load'] or 0, 1),
            'max_load': round(perf_agg['max_load'] or 0, 1),
        },
        'logs': {
            'total': log_total, 'errors': log_err,
            'top': [{'message': t['message'][:120], 'n': t['n']} for t in top_logs],
        },
        'alerts': [
            {
                'summary': a.summary, 'level': a.level, 'status': a.status,
                'started_at': timezone.localtime(a.started_at),
                'duration_min': a.duration_min,
            }
            for a in alerts
        ],
    }


def report_markdown(report):
    r = report
    lines = [
        '# 运维周期报表',
        '',
        f'- 统计范围：最近 {r["days"]} 天（自 {timezone.localtime(r["since"]):%Y-%m-%d %H:%M} 起）',
        f'- 生成时间：{timezone.localtime(r["generated_at"]):%Y-%m-%d %H:%M:%S}',
        '',
        '## 一、服务（APM）',
        '',
        f'- 请求总数 **{r["requests"]["total"]}**，平均耗时 **{r["requests"]["avg_ms"]} ms**，'
        f'最慢 **{r["requests"]["max_ms"]} ms**',
        f'- 错误请求 **{r["requests"]["errors"]}**（错误率 {r["requests"]["error_rate"]}%）',
        f'- SQL 查询总数 {r["requests"]["sql_total"]}，慢查询 {r["requests"]["slow_total"]} 条',
        '',
        '| 路径 | 请求数 | 平均耗时(ms) | 最慢(ms) |',
        '|---|---|---|---|',
    ]
    for t in r['top_slow']:
        lines.append(f"| `{t['path']}` | {t['n']} | {t['avg_ms']:.1f} | {t['max_ms']:.1f} |")
    lines += [
        '',
        '## 二、主机资源',
        '',
        f'- CPU 平均 {r["host"]["cpu_avg"]}% / 峰值 **{r["host"]["cpu_max"]}%**',
        f'- 内存平均 {r["host"]["mem_avg"]}% / 峰值 {r["host"]["mem_max"]}%',
        f'- 系统盘峰值使用率 {r["host"]["disk_max"]}%',
        '',
        '## 三、前端体验（RUM）',
        '',
        f'- PV **{r["rum"]["pv"]}**，UV {r["rum"]["uv"]}',
        f'- 页面平均加载 {r["rum"]["avg_load"]} ms（最慢 {r["rum"]["max_load"]} ms）',
        f'- JS 错误 {r["rum"]["js_errors"]} 条',
        '',
        '## 四、日志',
        '',
        f'- 日志总量 {r["logs"]["total"]} 条，其中 ERROR/CRITICAL **{r["logs"]["errors"]}** 条',
    ]
    for t in r['logs']['top']:
        lines.append(f'- （{t["n"]} 次）`{t["message"]}`')
    lines += ['', '## 五、告警事件', '']
    if not r['alerts']:
        lines.append('- 期间无告警事件。')
    else:
        lines.append('| 级别 | 摘要 | 状态 | 触发时间 | 持续(分钟) |')
        lines.append('|---|---|---|---|---|')
        for a in r['alerts']:
            lines.append(
                f'| {a["level"]} | {a["summary"]} | {a["status"]} '
                f'| {a["started_at"]:%m-%d %H:%M} | {a["duration_min"]} |'
            )
    return '\n'.join(lines) + '\n'
