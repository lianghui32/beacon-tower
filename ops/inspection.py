"""
ops/inspection.py — 巡检引擎：健康检查清单 + 容量预测 + 评分

检查维度：主机资源水位、磁盘/内存耗尽预测（线性外推）、接口错误率与 P95、
慢查询、遗留告警、拨测可用性与证书、ERROR 日志、SLO 错误预算。
每项给出 ok/warn/fail 状态、当前值与处置建议，汇总为 0-100 健康分。
"""
import json
from datetime import timedelta

from django.utils import timezone

from monitor.registry import metric_value

WARN, FAIL, OK = 'warn', 'fail', 'ok'


def _fit_days_to(values, target):
    """对分钟序列做线性外推，返回达到 target 还需的天数（下降趋势返回 None）"""
    n = len(values)
    if n < 30:
        return None
    xs = list(range(n))
    ys = values
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    slope_per_min = (sxy / sxx) if sxx else 0.0  # %/分钟
    if slope_per_min <= 1e-6:
        return None
    gap = target - ys[-1]
    if gap <= 0:
        return 0.0
    return round(gap / slope_per_min / 60 / 24, 1)


def _host_latest(field, max_age_min=10):
    """取最新的主机指标字段值；数据过期（采集线程停摆）视为缺失，避免拿陈旧值报 OK"""
    from hosts.models import HostMetric
    m = HostMetric.objects.filter(
        created_at__gte=timezone.now() - timedelta(minutes=max_age_min),
    ).order_by('-created_at').first()
    return getattr(m, field, None) if m else None


def _host_disk_series(hours=24):
    from django.db.models import Max
    from django.db.models.functions import TruncMinute

    from hosts.models import HostMetric
    since = timezone.now() - timedelta(hours=hours)
    rows = (
        HostMetric.objects.filter(created_at__gte=since)
        .annotate(bucket=TruncMinute('created_at'))
        .values('bucket').annotate(v=Max('disk_percent')).order_by('bucket')
    )
    return [float(r['v']) for r in rows]


def _host_mem_series(hours=24):
    from django.db.models import Max
    from django.db.models.functions import TruncMinute

    from hosts.models import HostMetric
    since = timezone.now() - timedelta(hours=hours)
    rows = (
        HostMetric.objects.filter(created_at__gte=since)
        .annotate(bucket=TruncMinute('created_at'))
        .values('bucket').annotate(v=Max('mem_percent')).order_by('bucket')
    )
    return [float(r['v']) for r in rows]


def run_inspection(trigger='manual'):
    """执行一次巡检，返回 (InspectionRun, 结果dict)"""
    from .models import InspectionRun

    checks = []

    def add(name, status, value, detail, suggestion=''):
        checks.append({'name': name, 'status': status, 'value': value,
                       'detail': detail, 'suggestion': suggestion})

    # ---- 主机资源 ----
    cpu = _host_latest('cpu_percent')
    if cpu is not None:
        st = FAIL if cpu > 90 else WARN if cpu > 80 else OK
        add('主机 CPU 水位', st, f'{cpu}%', '当前真实 CPU 使用率',
            '排查高 CPU 进程（主机监控页 Top 进程）' if st != OK else '')
    else:
        add('主机 CPU 水位', WARN, '无数据',
            '最近 10 分钟没有主机采集数据（采集线程可能停摆）',
            '检查平台后台线程是否存活')
    mem = _host_latest('mem_percent')
    if mem is not None:
        st = FAIL if mem > 90 else WARN if mem > 85 else OK
        add('主机内存水位', st, f'{mem}%', '当前内存使用率',
            '检查内存泄漏 / 增加内存' if st != OK else '')

    # ---- 容量预测：磁盘 ----
    disk_series = _host_disk_series()
    disk_now = _host_latest('disk_percent')
    if disk_series:
        days = _fit_days_to(disk_series, 95)
        disk_disp = f'{disk_now}%' if disk_now is not None else '无数据'
        if disk_now is not None and disk_now > 90:
            add('磁盘容量预测', FAIL, disk_disp, '磁盘已超 90%，随时可能写满',
                '立即清理或扩容')
        elif days is None:
            add('磁盘容量预测', OK, disk_disp, '近 24h 趋势平稳或下降，暂无写满风险', '')
        elif days < 7:
            add('磁盘容量预测', FAIL, f'{days} 天', f'按当前增速约 {days} 天后磁盘达 95%',
                '立即扩容或清理大文件')
        elif days < 14:
            add('磁盘容量预测', WARN, f'{days} 天', f'按当前增速约 {days} 天后磁盘达 95%',
                '两周内安排扩容')
        else:
            add('磁盘容量预测', OK, f'{days} 天', '距 95% 水位尚有充足余量', '')

    # ---- 容量预测：内存（与磁盘同样的天数分档，避免任何正斜率都告警） ----
    mem_series = _host_mem_series()
    if mem_series:
        days = _fit_days_to(mem_series, 90)
        if days is not None and days < 7:
            add('内存容量预测', FAIL, f'{days} 天', f'按当前增速约 {days} 天后内存达 90%',
                '排查内存泄漏')
        elif days is not None and days < 14:
            add('内存容量预测', WARN, f'{days} 天', f'按当前增速约 {days} 天后内存达 90%',
                '两周内安排扩容/排查泄漏')
        elif days is not None:
            add('内存容量预测', OK, f'{days} 天', '距 90% 水位尚有充足余量', '')
        else:
            add('内存容量预测', OK, f'{mem}%' if mem is not None else '平稳',
                '近 24h 内存趋势平稳或下降', '')

    # ---- 接口质量 ----
    err_rate = metric_value('http.error_rate', 60)
    if err_rate is not None:
        st = FAIL if err_rate > 5 else WARN if err_rate > 1 else OK
        add('接口错误率（1h）', st, f'{err_rate}%', '近 1 小时错误请求占比',
            '去 APM 查看错误路径与调用链' if st != OK else '')
    p95 = metric_value('http.p95_duration', 60)
    if p95 is not None:
        st = FAIL if p95 > 2000 else WARN if p95 > 800 else OK
        add('接口 P95 耗时（1h）', st, f'{p95} ms', '近 1 小时 P95 请求耗时',
            '查看慢接口与数据库分析' if st != OK else '')
    slow = metric_value('http.slow_query_count', 60)
    if slow is not None and slow > 20:
        add('慢查询（1h）', WARN, f'{slow} 条', '近 1 小时慢查询（>100ms）条数',
            '去数据库分析页查看慢查询模板')

    # ---- 告警遗留 ----
    from alerts.models import AlertEvent
    firing = AlertEvent.objects.filter(status='firing').count()
    if firing > 5:
        add('遗留告警', FAIL, f'{firing} 条', '触发中的告警数量过多', '逐条确认处理')
    elif firing > 0:
        add('遗留告警', WARN, f'{firing} 条', '存在触发中的告警', '到告警事件页确认')
    else:
        add('遗留告警', OK, '0 条', '无触发中的告警', '')

    # ---- 拨测 ----
    from .models import ProbeTask
    enabled_tasks = list(ProbeTask.objects.filter(enabled=True))
    failing = [t for t in enabled_tasks if t.consecutive_fails > 0]
    if failing:
        add('拨测可用性', FAIL, f'{len(failing)} 个任务失败',
            '、'.join(t.name for t in failing[:3]), '检查目标服务或网络')
    https_tasks = [t for t in enabled_tasks if t.last_cert_days is not None]
    if https_tasks:
        warn_task = min(https_tasks, key=lambda t: t.last_cert_days)  # 证书剩余天数最少的任务
        min_cert = warn_task.last_cert_days
        st = FAIL if min_cert < 7 else WARN if min_cert < 14 else OK
        add('HTTPS 证书到期', st, f'{min_cert} 天',
            f'最近到期的证书（{warn_task.name}）',
            '尽快续签证书' if st != OK else '')

    # ---- 日志 ----
    log_err = metric_value('log.error_count', 60)
    if log_err is not None and log_err > 50:
        add('ERROR 日志（1h）', FAIL, f'{log_err} 条/分', '近 1 小时 ERROR 日志速率过高',
            '到日志服务页检索错误')
    elif log_err is not None and log_err > 10:
        add('ERROR 日志（1h）', WARN, f'{log_err} 条/分', '近 1 小时 ERROR 日志速率偏高',
            '到智能分析页做日志模式挖掘')

    # ---- SLO 错误预算 ----
    from .models import SLO
    from .slo import slo_status
    for slo in SLO.objects.filter(enabled=True):
        st_info = slo_status(slo)
        burn = st_info['budget_burn_pct']
        st = FAIL if burn >= 100 else WARN if burn >= 80 else OK
        add(f"SLO：{slo.name}", st, f'预算已用 {burn}%',
            f"可用性 {st_info['availability']}%（目标 {slo.target_availability}%）",
            '控制错误率，预算耗尽应冻结发布' if st != OK else '')

    fails = sum(1 for c in checks if c['status'] == FAIL)
    warns = sum(1 for c in checks if c['status'] == WARN)
    score = max(0, 100 - 8 * fails - 3 * warns)
    result = {
        'score': score, 'fails': fails, 'warns': warns,
        'checks': checks,
        'generated_at': timezone.now().isoformat(),
    }
    run = InspectionRun.objects.create(trigger=trigger, score=score,
                                       results=json.dumps(result, ensure_ascii=False))
    _prune_runs()
    return run, result


def _prune_runs(keep=60):
    from .models import InspectionRun
    ids = list(InspectionRun.objects.order_by('-created_at')
               .values_list('id', flat=True)[keep:keep + 200])
    if ids:
        InspectionRun.objects.filter(id__in=ids).delete()


def inspection_markdown(run):
    try:
        data = json.loads(run.results or '[]')
    except (ValueError, TypeError):
        return '# 系统巡检报告\n\n巡检结果数据损坏（results 字段无法解析），无法导出。\n'
    if not isinstance(data, dict) or 'checks' not in data:
        return '# 系统巡检报告\n\n巡检结果数据格式异常，无法导出。\n'
    lines = [
        '# 系统巡检报告',
        '',
        f"- 巡检时间：{timezone.localtime(run.created_at):%Y-%m-%d %H:%M:%S}（{run.get_trigger_display()}）",
        f"- 健康评分：**{data['score']} / 100**（不合格 {data['fails']} 项，警告 {data['warns']} 项）",
        '',
        '| 检查项 | 状态 | 当前值 | 说明 | 建议 |',
        '|---|---|---|---|---|',
    ]
    for c in data['checks']:
        status = {'ok': '✅ 正常', 'warn': '⚠️ 警告', 'fail': '❌ 不合格'}.get(c['status'], c['status'])
        lines.append(f"| {c['name']} | {status} | {c['value']} | {c['detail']} | {c['suggestion'] or '-'} |")
    return '\n'.join(lines) + '\n'
