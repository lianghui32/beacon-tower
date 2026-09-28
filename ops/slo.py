"""
ops/slo.py — SLO 达成与错误预算计算

错误预算模型：
- 允许错误数 = 窗口内总请求 × (100 - 可用性目标)%
- 已消耗 = 窗口内错误请求数
- budget_burn_pct = 已消耗 / 允许 × 100（>=100 表示预算烧穿）
延迟目标：窗口内 P95 与目标比较（达标不影响预算，仅展示）。
"""
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone


def slo_status(slo):
    from django.conf import settings as dj_settings

    from monitor.models import RequestMetric

    # 窗口受数据保留期约束：明细只保留 RETENTION_DAYS 天，超出部分无数据可算，
    # 否则"30 天可用性"实际只统计了库里剩下的 ≤7 天，错误预算被系统性低估
    retention_days = dj_settings.OBSERVABILITY.get('RETENTION_DAYS', 7)
    window_days = min(slo.window_days, retention_days)
    since = timezone.now() - timedelta(days=window_days)
    agg = RequestMetric.objects.filter(created_at__gte=since).aggregate(
        total=Count('id'),
        errors=Count('id', filter=Q(is_error=True)),
    )
    total = agg['total'] or 0
    errors = agg['errors'] or 0
    availability = round((1 - errors / total) * 100, 3) if total else 100.0
    allowed_errors = total * (100 - slo.target_availability) / 100.0
    if allowed_errors <= 0:
        # 零容错目标（如 100%）：有任何错误即预算烧穿；无错误记 0%
        burn_pct = 100.0 if errors > 0 else 0.0
    else:
        burn_pct = round(errors * 100.0 / allowed_errors, 1)

    # 窗口 P95（抽样最多 8000 条）
    durations = list(
        RequestMetric.objects.filter(created_at__gte=since)
        .order_by('-created_at').values_list('duration_ms', flat=True)[:8000]
    )
    p95 = 0.0
    if durations:
        vs = sorted(durations)
        p95 = round(float(vs[min(len(vs) - 1, int(len(vs) * 0.95))]), 1)

    # 近 1h 消耗速率（用于燃尽趋势）
    recent = RequestMetric.objects.filter(
        created_at__gte=timezone.now() - timedelta(hours=1)
    ).aggregate(total=Count('id'), errors=Count('id', filter=Q(is_error=True)))
    recent_burn = recent['errors'] or 0
    recent_total = recent['total'] or 0

    return {
        'availability': availability,
        'p95_ms': p95,
        'window_days': window_days,
        'total': total,
        'errors': errors,
        'allowed_errors': round(allowed_errors, 1),
        'budget_burn_pct': burn_pct,
        'budget_remaining_pct': round(max(0.0, 100 - burn_pct), 1),
        'avail_ok': availability >= slo.target_availability,
        'p95_ok': p95 <= slo.target_p95_ms,
        'recent_hour_errors': recent_burn,
        'recent_hour_total': recent_total,
    }


def slo_markdown(slo, status):
    return '\n'.join([
        f"# SLO 报告：{slo.name}",
        '',
        f'- 统计窗口：最近 {status.get("window_days", slo.window_days)} 天'
        f'（受数据保留期约束）',
        f"- 可用性：**{status['availability']}%**（目标 {slo.target_availability}%，"
        f"{'✅ 达标' if status['avail_ok'] else '❌ 未达标'}）",
        f"- P95 耗时：**{status['p95_ms']} ms**（目标 {slo.target_p95_ms} ms，"
        f"{'✅ 达标' if status['p95_ok'] else '❌ 未达标'}）",
        f"- 错误预算：已消耗 **{status['budget_burn_pct']}%**"
        f"（错误 {status['errors']} / 允许 {status['allowed_errors']}）",
        f"- 近 1 小时：{status['recent_hour_total']} 次请求中 {status['recent_hour_errors']} 次错误",
    ])
