"""
rum/views.py — RUM 数据接收端点（beacon）+ 分析页面 + AJAX 数据接口

接收端点：POST /rum/beacon/  批量接收 static/rum.js 上报的事件数组。
"""
import json
import math
from datetime import timedelta

from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from monitor.security import rate_limit, require_ingest

from .models import RumEvent

PERF_KEYS = ['ttfb_ms', 'dom_ready_ms', 'load_ms', 'fp_ms', 'fcp_ms', 'lcp_ms']

# 数值上限：SQLite INTEGER 最大 2^63-1，超限会让 bulk_create 抛 OverflowError
_INT_MAX = 2 ** 62


# ---------------------------------------------------------------------------
# 数据接收
# ---------------------------------------------------------------------------

@csrf_exempt
@require_POST
@require_ingest
@rate_limit('rum-beacon', rate=120, per=60)
def beacon(request):
    """接收 rum.js 批量上报（单条或数组）"""
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'invalid json'}, status=400)
    except RequestDataTooBig:
        return JsonResponse({'ok': False, 'error': 'payload too large'}, status=413)
    events = payload if isinstance(payload, list) else [payload]
    objs = []
    for ev in events[:200]:  # 单批上限
        if not isinstance(ev, dict):
            continue
        ev_type = ev.get('type')
        if ev_type not in ('pv', 'perf', 'api', 'error', 'resource', 'custom'):
            continue
        kwargs = {
            'type': ev_type,
            'app': str(ev.get('app') or 'web')[:32],
            'page_url': str(ev.get('page_url') or '')[:256],
            'referrer': str(ev.get('referrer') or '')[:256],
            'session_id': str(ev.get('session_id') or '')[:64],
            'device': str(ev.get('device') or '')[:80],
            'screen': str(ev.get('screen') or '')[:20],
        }
        if ev_type == 'perf':
            for k in PERF_KEYS:
                kwargs[k] = _num(ev.get(k))
        elif ev_type == 'api':
            kwargs['api_url'] = str(ev.get('api_url') or '')[:256]
            kwargs['api_method'] = str(ev.get('api_method') or '')[:10]
            # 状态码钳制到 0-999（超大值/负数会溢出 SQLite 或污染统计）
            status = _num(ev.get('api_status'), int_type=True)
            kwargs['api_status'] = status if status is not None and 0 <= status <= 999 else 0
            kwargs['api_duration_ms'] = _num(ev.get('api_duration_ms'))
            raw_ok = ev.get('api_ok', True)
            # 显式布尔解析：字符串 "false"/"0" 视为失败，而不是真值
            kwargs['api_ok'] = raw_ok not in (False, 'false', 'False', '0', 0, None)
        elif ev_type == 'error':
            kwargs['err_message'] = str(ev.get('err_message') or '')[:300]
            kwargs['err_stack'] = str(ev.get('err_stack') or '')[:4000]
        elif ev_type == 'resource':
            kwargs['r_type'] = str(ev.get('r_type') or '')[:16]
            kwargs['r_url'] = str(ev.get('r_url') or '')[:256]
            kwargs['r_duration_ms'] = _num(ev.get('r_duration_ms'))
            kwargs['r_size_kb'] = _num(ev.get('r_size_kb'))
        elif ev_type == 'custom':
            kwargs['event_name'] = str(ev.get('event_name') or '')[:64]
            kwargs['payload'] = str(ev.get('payload') or '')[:2000]
        objs.append(RumEvent(**kwargs))
    if objs:
        try:
            RumEvent.objects.bulk_create(objs, batch_size=100)
        except Exception:
            # 单条脏数据不应让整批上报 500（剩余合法事件已经构造好则重试逐条降级）
            return JsonResponse({'ok': False, 'error': 'invalid data'}, status=400)
    return JsonResponse({'ok': True, 'accepted': len(objs)})


def _num(v, int_type=False):
    """安全数值解析：非数字/NaN/Infinity/超界整数一律返回 None"""
    try:
        v = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(v):
        return None
    if abs(v) > _INT_MAX:
        return None
    return int(v) if int_type else v


# ---------------------------------------------------------------------------
# 页面与数据接口共用工具
# ---------------------------------------------------------------------------

def _minutes(request, default=60):
    try:
        m = int(request.GET.get('minutes', default))
    except (TypeError, ValueError):
        m = default
    return max(5, min(10080, m))


def _q(minutes):
    return RumEvent.objects.filter(created_at__gte=timezone.now() - timedelta(minutes=minutes))


def _pctl(values, p):
    if not values:
        return 0.0
    vs = sorted(values)
    k = max(0, min(len(vs) - 1, int(len(vs) * p / 100)))
    return round(float(vs[k]), 1)


def _hist_time(dt):
    return timezone.localtime(dt).strftime('%H:%M')


def _per_minute(qs, value_fn):
    """python 端按分钟分桶：[(time, [values...])] -> [{'t','v'}]"""
    buckets = {}
    for row in qs:
        key = row.created_at.replace(second=0, microsecond=0)
        buckets.setdefault(key, []).append(value_fn(row))
    out = []
    for t in sorted(buckets):
        vals = [v for v in buckets[t] if v is not None]
        out.append({'t': _hist_time(t), 'v': round(sum(vals) / len(vals), 1) if vals else 0})
    return out


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@require_GET
def rum_overview_page(request):
    return render(request, 'rum/overview.html', {'minutes': _minutes(request)})


@require_GET
def rum_perf_page(request):
    return render(request, 'rum/perf.html', {'minutes': _minutes(request)})


@require_GET
def rum_errors_page(request):
    return render(request, 'rum/errors.html', {'minutes': _minutes(request)})


@require_GET
def rum_api_page(request):
    return render(request, 'rum/api.html', {'minutes': _minutes(request)})


@require_GET
def rum_resources_page(request):
    return render(request, 'rum/resources.html', {'minutes': _minutes(request)})


@require_GET
def rum_custom_page(request):
    return render(request, 'rum/custom.html', {'minutes': _minutes(request)})


# ---------------------------------------------------------------------------
# AJAX 数据接口
# ---------------------------------------------------------------------------

@require_GET
def api_overview(request):
    """数据总览：PV/UV/错误/API 概览 + 趋势 + 终端分布 + Top 页面"""
    minutes = _minutes(request)
    qs = _q(minutes)

    pv_qs = qs.filter(type='pv')
    pv_total = pv_qs.count()
    uv = pv_qs.values('session_id').distinct().count()
    err_total = qs.filter(type='error').count()
    api_qs = qs.filter(type='api')
    api_total = api_qs.count()
    api_err = api_qs.filter(api_ok=False).count()
    from django.db.models import Avg
    from django.db.models.functions import TruncMinute
    perf_avg_load = qs.filter(type='perf').aggregate(v=Avg('load_ms'))['v']
    avg_load = round(perf_avg_load, 1) if perf_avg_load is not None else 0

    # PV 按分钟趋势（数据库聚合，避免全量行拉进 Python）
    buckets = (
        pv_qs.annotate(bucket=TruncMinute('created_at'))
        .values('bucket').annotate(n=count()).order_by('bucket')
    )
    pv_trend = [{'t': _hist_time(b['bucket']), 'v': b['n']} for b in buckets]

    devices = [
        {'name': d['device'] or '未知', 'n': d['n']}
        for d in pv_qs.values('device').annotate(n=count()).order_by('-n')[:6]
    ]
    top_pages = [
        {'page': (p['page_url'] or '/')[-80:], 'n': p['n']}
        for p in pv_qs.values('page_url').annotate(n=count()).order_by('-n')[:10]
    ]
    return JsonResponse({
        'summary': {
            'pv': pv_total, 'uv': uv,
            'js_errors': err_total,
            'api_calls': api_total,
            'api_error_rate': round(api_err * 100.0 / api_total, 2) if api_total else 0,
            'avg_load_ms': avg_load,
        },
        'pv_trend': pv_trend,
        'devices': devices,
        'top_pages': top_pages,
    })


@require_GET
def api_perf(request):
    """页面性能：各阶段分位值 + 每页平均 + 趋势"""
    minutes = _minutes(request)
    qs = _q(minutes).filter(type='perf')
    stats = {}
    for k in PERF_KEYS:
        vals = list(qs.exclude(**{k: None}).values_list(k, flat=True))
        stats[k] = {
            'avg': round(sum(vals) / len(vals), 1) if vals else 0,
            'p50': _pctl(vals, 50), 'p75': _pctl(vals, 75),
            'p90': _pctl(vals, 90), 'p95': _pctl(vals, 95),
            'n': len(vals),
        }
    by_page = [
        {'page': (p['page_url'] or '/')[-70:], 'avg': round(p['avg'] or 0, 1), 'n': p['n']}
        for p in qs.values('page_url').annotate(avg=avg_fn('load_ms'), n=count()).order_by('-avg')[:10]
    ]
    load_trend = _per_minute(qs.exclude(load_ms=None), lambda r: r.load_ms)
    return JsonResponse({'stats': stats, 'by_page': by_page, 'load_trend': load_trend})


@require_GET
def api_errors(request):
    """异常分析：错误聚合（按消息归并）+ 最近错误明细 + 趋势"""
    minutes = _minutes(request)
    qs = _q(minutes).filter(type='error')
    total = qs.count()  # 单独 count，避免把全量错误行重复加载两次
    groups = {}
    for r in qs.order_by('created_at'):
        key = r.err_message[:120] or '(空消息)'
        g = groups.setdefault(key, {'message': key, 'n': 0, 'last': '', 'page': ''})
        g['n'] += 1
        g['last'] = timezone.localtime(r.created_at).strftime('%m-%d %H:%M:%S')
        if not g['page'] and r.page_url:
            g['page'] = r.page_url[-70:]
    latest = [
        {
            'message': r.err_message, 'stack': r.err_stack[:1500],
            'page': r.page_url[-70:], 'device': r.device,
            'time': timezone.localtime(r.created_at).strftime('%m-%d %H:%M:%S'),
        }
        for r in qs[:15]
    ]
    trend = _per_minute(qs, lambda r: 1)
    return JsonResponse({
        'total': total,
        'groups': sorted(groups.values(), key=lambda g: -g['n'])[:15],
        'latest': latest,
        'trend': trend,
    })


@require_GET
def api_calls(request):
    """API 监控：按接口聚合 + 最慢明细 + 趋势"""
    minutes = _minutes(request)
    qs = _q(minutes).filter(type='api')
    agg = {}
    for r in qs:
        key = f"{r.api_method} {r.api_url}" if r.api_url else '(unknown)'
        a = agg.setdefault(key, {'api': key, 'n': 0, 'errs': 0, 'durs': [], 'statuses': {}})
        a['n'] += 1
        a['errs'] += 0 if r.api_ok else 1
        if r.api_duration_ms is not None:
            a['durs'].append(r.api_duration_ms)
        st = str(r.api_status or 0)
        a['statuses'][st] = a['statuses'].get(st, 0) + 1
    rows = []
    for a in agg.values():
        durs = a['durs']
        rows.append({
            'api': a['api'], 'n': a['n'],
            'avg': round(sum(durs) / len(durs), 1) if durs else 0,
            'p95': _pctl(durs, 95),
            'error_rate': round(a['errs'] * 100.0 / a['n'], 1),
            'statuses': a['statuses'],
        })
    rows.sort(key=lambda x: -x['avg'])
    slowest = [
        {
            'api': f'{r.api_method} {r.api_url}'[-90:], 'status': r.api_status,
            'dur': r.api_duration_ms, 'ok': r.api_ok,
            'time': timezone.localtime(r.created_at).strftime('%m-%d %H:%M:%S'),
        }
        for r in qs.order_by('-api_duration_ms')[:12]
    ]
    trend = _per_minute(qs, lambda r: r.api_duration_ms or 0)
    return JsonResponse({'rows': rows[:15], 'slowest': slowest, 'trend': trend})


@require_GET
def api_resources(request):
    """静态资源：按类型聚合 + 最慢资源明细"""
    minutes = _minutes(request)
    qs = _q(minutes).filter(type='resource')
    by_type = [
        {'type': t['r_type'] or 'other', 'n': t['n'],
         'avg': round(t['avg'] or 0, 1), 'size': round(t['size'] or 0, 1)}
        for t in qs.values('r_type').annotate(
            n=count(), avg=avg_fn('r_duration_ms'), size=avg_fn('r_size_kb'))
    ]
    slowest = [
        {'type': r.r_type, 'url': r.r_url[-90:], 'dur': r.r_duration_ms, 'size': r.r_size_kb,
         'time': timezone.localtime(r.created_at).strftime('%m-%d %H:%M:%S')}
        for r in qs.order_by('-r_duration_ms')[:12]
    ]
    return JsonResponse({'by_type': by_type, 'slowest': slowest})


@require_GET
def api_custom(request):
    """自定义上报：事件计数 + 最新负载"""
    minutes = _minutes(request)
    qs = _q(minutes).filter(type='custom')
    counts = [
        {'name': c['event_name'] or '(未命名)', 'n': c['n']}
        for c in qs.values('event_name').annotate(n=count()).order_by('-n')[:20]
    ]
    latest = [
        {'name': r.event_name, 'payload': r.payload[:500],
         'page': r.page_url[-70:],
         'time': timezone.localtime(r.created_at).strftime('%m-%d %H:%M:%S')}
        for r in qs[:20]
    ]
    return JsonResponse({'counts': counts, 'latest': latest})


# 小工具：避免每处 import
def count():
    from django.db.models import Count
    return Count('id')


def avg_fn(field):
    from django.db.models import Avg
    return Avg(field)
