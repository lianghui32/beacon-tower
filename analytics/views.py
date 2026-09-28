"""
analytics/views.py — 智能分析页面与数据接口

- /analytics/            智能分析工作台（异常检测 / 趋势预测 / 相关性 / 日志挖掘 四个 Tab）
- /analytics/report/     报表中心（周期报表生成 + Markdown 导出）
"""
from datetime import timedelta

from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from . import algorithms
from .reporting import build_report, report_markdown

CORRELATION_KEYS = [
    'http.avg_duration', 'http.sql_avg', 'http.error_rate',
    'host.cpu_percent', 'host.mem_percent',
    'rum.load_time_avg', 'log.error_count',
]


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@require_GET
def analytics_page(request):
    from monitor.registry import REGISTRY
    catalog = [{'key': k, 'label': v[0]} for k, v in REGISTRY.items()]
    from monitor.models import CustomMetric
    names = set(CustomMetric.objects.values_list('name', flat=True)[:30])
    catalog += [{'key': f'custom.{n}', 'label': f'自定义指标 {n}'} for n in sorted(names)]
    return render(request, 'analytics/analytics.html', {
        'catalog': catalog,
        'minutes': request.GET.get('minutes', '60'),
    })


@require_GET
def report_page(request):
    days = _days(request)
    report = build_report(days)
    return render(request, 'analytics/report.html', {
        'report': report,
        'days': days,
    })


@require_GET
def report_export(request):
    days = _days(request)
    md = report_markdown(build_report(days))
    resp = HttpResponse(md, content_type='text/markdown; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="ops_report_{days}d.md"'
    return resp


def _days(request):
    try:
        d = int(request.GET.get('days', 1))
    except (TypeError, ValueError):
        d = 1
    return max(1, min(7, d))


# ---------------------------------------------------------------------------
# 智能分析 AJAX 接口
# ---------------------------------------------------------------------------

@require_GET
def api_anomaly(request):
    """异常检测：?metric=host.cpu_percent&minutes=120"""
    from monitor.registry import series
    key = request.GET.get('metric', 'host.cpu_percent')
    minutes = _minutes(request, 120)
    s = series(key, minutes)
    if not s:
        return JsonResponse({'error': 'unknown metric'}, status=400)
    marked, desc = algorithms.detect_anomalies(s['points'], window=20, k=3.0)
    anomalies = [p for p in marked if p.get('a')]
    return JsonResponse({
        'key': key, 'label': s['label'], 'unit': s['unit'],
        'points': marked, 'desc': desc, 'anomalies': anomalies[-20:],
    })


@require_GET
def api_forecast(request):
    """趋势预测：?metric=http.avg_duration&minutes=120&horizon=30"""
    from monitor.registry import series
    key = request.GET.get('metric', 'http.avg_duration')
    minutes = _minutes(request, 120)
    try:
        horizon = min(120, max(10, int(request.GET.get('horizon', 30) or 30)))
    except (TypeError, ValueError):
        horizon = 30
    s = series(key, minutes)
    if not s:
        return JsonResponse({'error': 'unknown metric'}, status=400)
    desc, future = algorithms.linear_forecast(s['points'], horizon=horizon)
    return JsonResponse({
        'key': key, 'label': s['label'], 'unit': s['unit'],
        'history': s['points'], 'future': future, 'desc': desc,
    })


@require_GET
def api_correlation(request):
    """相关性分析：固定指标集两两皮尔逊系数 + 解读"""
    minutes = _minutes(request, 360)
    from monitor.registry import series
    series_map = {}
    for key in CORRELATION_KEYS:
        s = series(key, minutes)
        if s and s['points']:
            series_map[key] = s['points']
    keys, mat = algorithms.correlation_matrix(series_map)
    # 找出最显著的一对给出解读
    best = None
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            r = mat[i][j]
            if r is None:
                continue
            if best is None or abs(r) > abs(best['r']):
                best = {'a': keys[i], 'b': keys[j], 'r': r}
    pairs = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            r = mat[i][j]
            if r is not None and abs(r) >= 0.5:
                pairs.append({'a': keys[i], 'b': keys[j], 'r': r})
    pairs.sort(key=lambda p: -abs(p['r']))
    return JsonResponse({'keys': keys, 'matrix': mat, 'best': best, 'pairs': pairs[:8]})


@require_GET
def api_logmining(request):
    """日志模式挖掘：模板聚类 Top N + 总量分布"""
    minutes = _minutes(request, 720)
    from loghub.models import LogEntry
    qs = LogEntry.objects.filter(created_at__gte=timezone.now() - timedelta(minutes=minutes))
    patterns = algorithms.mine_log_patterns(qs[:5000])[:12]
    level_dist = list(qs.values('level').annotate(n=Count('id')).order_by('-n'))
    return JsonResponse({
        'total': qs.count(),
        'patterns': [
            {
                'pattern': p['pattern'], 'n': p['n'], 'levels': p['levels'],
                'sample': p['sample'],
                'last': timezone.localtime(p['last']).strftime('%m-%d %H:%M'),
                'loggers': p['loggers'],
            }
            for p in patterns
        ],
        'level_dist': level_dist,
    })


def _minutes(request, default=60):
    try:
        m = int(request.GET.get('minutes', default))
    except (TypeError, ValueError):
        m = default
    return max(10, min(10080, m))


# ---------------------------------------------------------------------------
# IP 访问地图（地域分布 / 访问时段 / 访客行为）
# ---------------------------------------------------------------------------

def _is_lan_ip(ip):
    """局域网判定：用 ipaddress 标准库（手写前缀 '172.2' 会把公网 172.2.x.x 误判）"""
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local


@require_GET
def geo_page(request):
    from monitor.geoip import backend_name
    return render(request, 'analytics/geo.html', {
        'minutes': _minutes(request, 1440),
        'backend': backend_name(),
    })


@require_GET
def api_geo(request):
    """IP 访问分析数据：地域分布 / 小时分布 / 省份×小时热力 / Top IP 行为"""
    from datetime import timedelta

    from django.db.models import Min

    from monitor.models import RequestMetric

    minutes = _minutes(request, 1440)
    since = timezone.now() - timedelta(minutes=minutes)
    rows = list(
        RequestMetric.objects.filter(created_at__gte=since)
        .values_list('created_at', 'client_ip', 'geo_province', 'geo_city',
                     'path', 'duration_ms', 'is_error')
    )

    # ---- 聚合容器 ----
    prov_pv = {}          # 省份 -> pv（含局域网/海外，地图侧过滤）
    prov_ips = {}         # 省份 -> set(ip)
    hourly = {}           # hour(0-23) -> {'pv': n, 'ips': set()}
    prov_hour = {}        # (prov, hour) -> pv
    world_pv = {}         # 国家 -> pv（geo_province='海外' 时取 geo_city）
    world_ips = {}        # 国家 -> set(ip)
    world_hour = {}       # (国家, hour) -> pv
    per_ip = {}           # ip -> dict

    for ts, ip, prov, city, path, dur, is_err in rows:
        if not ip:
            continue
        prov = prov or '未知'
        hour = timezone.localtime(ts).hour
        prov_pv[prov] = prov_pv.get(prov, 0) + 1
        prov_ips.setdefault(prov, set()).add(ip)
        h = hourly.setdefault(hour, {'pv': 0, 'ips': set()})
        h['pv'] += 1
        h['ips'].add(ip)
        prov_hour[(prov, hour)] = prov_hour.get((prov, hour), 0) + 1
        if prov == '海外' and city:
            world_pv[city] = world_pv.get(city, 0) + 1
            world_ips.setdefault(city, set()).add(ip)
            world_hour[(city, hour)] = world_hour.get((city, hour), 0) + 1

        a = per_ip.setdefault(ip, {
            'n': 0, 'durs': [], 'errs': 0, 'paths': {}, 'prov': prov,
            'city': city, 'first': ts, 'last': ts, 'hours': {},
        })
        a['n'] += 1
        a['durs'].append(dur)
        a['errs'] += 1 if is_err else 0
        a['paths'][path] = a['paths'].get(path, 0) + 1
        a['hours'][hour] = a['hours'].get(hour, 0) + 1
        if ts < a['first']:
            a['first'] = ts
        if ts > a['last']:
            a['last'] = ts

    # ---- 地图数据（只保留能落在地图上的省份） ----
    non_geo = {'局域网', '未知', '海外'}
    map_data = [
        {'name': p, 'value': v}
        for p, v in sorted(prov_pv.items(), key=lambda x: -x[1])
        if p not in non_geo
    ]
    top_provinces = [
        {'name': p, 'pv': v, 'ips': len(prov_ips.get(p, set()))}
        for p, v in sorted(prov_pv.items(), key=lambda x: -x[1])[:10]
    ]

    # ---- 时段分布 ----
    hours = sorted(hourly)
    hourly_list = [
        {'hour': h, 'pv': hourly[h]['pv'], 'uv': len(hourly[h]['ips'])}
        for h in hours
    ]

    # ---- Top5 省份 × 小时 热力 ----
    top5 = [p['name'] for p in top_provinces[:5]]
    heat = [
        [hi, ti, prov_hour.get((p, hours[hi]), 0)]
        for ti, p in enumerate(top5)
        for hi in range(len(hours))
    ]

    # ---- 海外维度 ----
    world_sorted = sorted(world_pv.items(), key=lambda x: -x[1])
    world_data = [{'name': c, 'value': v} for c, v in world_sorted]
    top_countries = [
        {'name': c, 'pv': v, 'ips': len(world_ips.get(c, set()))}
        for c, v in world_sorted[:10]
    ]
    top5c = [c for c, _v in world_sorted[:5]]
    heat_world = [
        [hi, ti, world_hour.get((c, hours[hi]), 0)]
        for ti, c in enumerate(top5c)
        for hi in range(len(hours))
    ]

    # ---- Top IP 行为表 ----
    top_ips = sorted(per_ip.items(), key=lambda x: -x[1]['n'])[:20]
    ip_rows = []
    for ip, a in top_ips:
        durs = a['durs']
        top_path = max(a['paths'].items(), key=lambda x: x[1]) if a['paths'] else ('-', 0)
        # 该 IP 自身的访问高峰时段（此前误用了所属省份的峰值时段）
        peak_hour = max(a['hours'], key=lambda h: a['hours'][h]) if a['hours'] else '-'
        if a['prov'] == '海外':
            region = f"海外·{a['city']}" if a['city'] else '海外'
        else:
            region = f"{a['prov']}{('·' + a['city']) if a['city'] and a['city'] != a['prov'] else ''}"
        ip_rows.append({
            'ip': ip,
            'region': region,
            'n': a['n'],
            'paths_cnt': len(a['paths']),
            'top_path': top_path[0],
            'avg_ms': round(sum(durs) / len(durs), 1),
            'errs': a['errs'],
            'err_rate': round(a['errs'] * 100.0 / a['n'], 1),
            'first': timezone.localtime(a['first']).strftime('%m-%d %H:%M'),
            'last': timezone.localtime(a['last']).strftime('%m-%d %H:%M'),
            'peak_hour': peak_hour,
        })

    # ---- 汇总 ----
    all_ips = set(per_ip)
    lan_ips = {ip for ip in all_ips if _is_lan_ip(ip)}
    # 新增访客：窗口内首次出现的 IP（窗口内过滤，避免全表 GROUP BY）
    new_ips = RequestMetric.objects.filter(
        created_at__gte=since, client_ip__in=list(all_ips)[:1000],
    ).values('client_ip').annotate(first=Min('created_at')).filter(
        first__gte=since).count()

    overseas_pv = sum(world_pv.values())
    return JsonResponse({
        'summary': {
            'total_pv': sum(prov_pv.values()),
            'ip_count': len(all_ips),
            'lan_ips': len(lan_ips),
            'new_ips': new_ips,
            'top_province': (top_provinces[0]['name'] + f"（{top_provinces[0]['pv']} 次）")
                            if top_provinces else '-',
            'peak_hour': max(hourly_list, key=lambda x: x['pv'])['hour'] if hourly_list else '-',
            'overseas_pv': overseas_pv,
            'overseas_ratio': round(overseas_pv * 100.0 / len(rows), 1) if rows else 0,
            'top_country': (top_countries[0]['name'] + f"（{top_countries[0]['pv']} 次）")
                           if top_countries else '-',
            'overseas_countries': len(world_pv),
        },
        'map': map_data,
        'world': world_data,
        'top_provinces': top_provinces,
        'top_countries': top_countries,
        'hourly': hourly_list,
        'heat': {'provinces': top5, 'hours': [f'{h}时' for h in hours], 'data': heat},
        'heat_world': {'countries': top5c, 'hours': [f'{h}时' for h in hours], 'data': heat_world},
        'top_ips': ip_rows,
    })
