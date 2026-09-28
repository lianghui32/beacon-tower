"""
loghub/views.py — 日志查询页 / 搜索 API / 接入 API
"""
import json
import re
from datetime import timedelta

from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from monitor.security import rate_limit, require_ingest

from .models import LogEntry

# 平台认可的日志级别白名单：接入 API 只收这几档，
# 否则任意级别字符串入库后会让直方图/过滤页出现脏档位
VALID_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')

# 合法 TraceID：W3C 规范为 32 个小写 hex；兼容到 1~32 位，供调用方自行截短
_TRACE_ID_RE = re.compile(r'^[0-9a-f]{1,32}$')


def _range_minutes(request, default=1440):
    try:
        m = int(request.GET.get('minutes', default))
    except (TypeError, ValueError):
        m = default
    return max(5, min(20160, m))


@require_GET
def log_page(request):
    minutes = _range_minutes(request)
    return render(request, 'loghub/logs.html', {'minutes': minutes})


@require_GET
def api_search(request):
    """日志搜索：关键字 / 级别 / 来源 / trace_id / 时间范围，返回条数直方图 + 明细"""
    minutes = _range_minutes(request)
    q = (request.GET.get('q') or '').strip()
    level = (request.GET.get('level') or '').strip()
    source = (request.GET.get('source') or '').strip()
    trace_id = (request.GET.get('trace_id') or '').strip().lower()
    try:
        page = max(1, int(request.GET.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    page_size = 50

    qs = LogEntry.objects.filter(created_at__gte=timezone.now() - timedelta(minutes=minutes))
    if q:
        # LIKE 通配符转义，防止 %/_ 被用户输入放大扫描成本
        safe_q = q.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')
        qs = qs.filter(message__icontains=safe_q)
    if level:
        qs = qs.filter(level=level)
    if source:
        qs = qs.filter(source=source)
    if trace_id:
        # 按链路 ID 串日志：APM 瀑布图页可直接跳转查看同一 trace 的全部日志
        qs = qs.filter(trace_id=trace_id[:32])

    total = qs.count()
    rows = qs[(page - 1) * page_size: page * page_size]

    # 级别分布（当前过滤条件下）
    level_dist = list(qs.values('level').annotate(n=count_fn()).order_by('-n'))

    # 按分钟直方图（用 setdefault 累加，容忍库中存在白名单外的历史脏级别）
    from django.db.models.functions import TruncMinute
    hist = list(
        qs.annotate(bucket=TruncMinute('created_at'))
        .values('bucket', 'level')
        .annotate(n=count_fn())
        .order_by('bucket')
    )
    hist_map = {}
    for h in hist:
        t = timezone.localtime(h['bucket']).strftime('%H:%M')
        entry = hist_map.setdefault(t, {'t': t, 'ERROR': 0, 'WARNING': 0, 'INFO': 0, 'CRITICAL': 0})
        entry[h['level']] = entry.get(h['level'], 0) + h['n']

    items = []
    for r in rows:
        items.append({
            'id': r.id,
            'source': r.source,
            'level': r.level,
            'logger': r.logger,
            'message': r.message[:1000],
            'time': timezone.localtime(r.created_at).strftime('%m-%d %H:%M:%S'),
        })

    pages = max(1, (total + page_size - 1) // page_size)
    return JsonResponse({
        'total': total,
        'page': page,
        'pages': pages,
        'level_dist': level_dist,
        'hist': list(hist_map.values()),
        'items': items,
    })


def count_fn():
    from django.db.models import Count
    return Count('id')


@require_GET
def api_export(request):
    """按当前过滤条件导出日志 CSV（带 BOM，Excel 直接打开；防公式注入）"""
    import csv as csv_mod

    minutes = _range_minutes(request)
    q = (request.GET.get('q') or '').strip()
    level = (request.GET.get('level') or '').strip()
    source = (request.GET.get('source') or '').strip()
    qs = LogEntry.objects.filter(created_at__gte=timezone.now() - timedelta(minutes=minutes))
    if q:
        safe_q = q.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')
        qs = qs.filter(message__icontains=safe_q)
    if level:
        qs = qs.filter(level=level)
    if source:
        qs = qs.filter(source=source)

    def _csv_safe(s):
        # Excel 把 =/-/+/@ 开头的单元格当公式执行（CSV 注入），加前缀阻断
        if s[:1] in ('=', '-', '+', '@'):
            return "'" + s
        return s

    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = 'attachment; filename="logs_export.csv"'
    resp.write('\ufeff')
    writer = csv_mod.writer(resp)
    writer.writerow(['时间', '级别', '来源', '记录器', '内容'])
    for r in qs[:20000].iterator():
        msg = _csv_safe(r.message.replace('\n', ' ')[:800])
        writer.writerow([
            timezone.localtime(r.created_at).strftime('%Y-%m-%d %H:%M:%S'),
            _csv_safe(r.level), _csv_safe(r.source), _csv_safe(r.logger), msg,
        ])
    return resp


@csrf_exempt
@require_POST
@require_ingest
@rate_limit('log-ingest', rate=120, per=60)
def api_ingest(request):
    """日志接入 API（单条或批量）

    POST /logs/api/ingest/
    Content-Type: application/json
    [
      {"level": "ERROR", "source": "app", "logger": "payment",
       "message": "订单 20260926 创建失败", "extra": {"order_id": 42}},
      ...
    ]
    """
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'invalid json'}, status=400)
    except RequestDataTooBig:
        return JsonResponse({'ok': False, 'error': 'payload too large'}, status=413)
    items = payload if isinstance(payload, list) else [payload]
    # W3C Trace Context：上游服务携带 traceparent 头时，本批日志归入同一调用链；
    # 也可在每条日志里给 trace_id 字段（只收合法 hex，防止任意串入库）
    from monitor.tracing import parse_traceparent
    tp = parse_traceparent(request.headers.get('traceparent', ''))
    header_trace = tp['trace_id'] if tp else ''
    objs = []
    for it in items[:500]:  # 单批上限 500 条
        if not isinstance(it, dict) or not it.get('message'):
            continue
        level = (it.get('level') or 'INFO').upper()[:10]
        if level not in VALID_LEVELS:
            level = 'INFO'
        trace_id = header_trace or str(it.get('trace_id') or '').strip().lower()
        if not _TRACE_ID_RE.fullmatch(trace_id):
            trace_id = ''
        objs.append(LogEntry(
            source=(it.get('source') or 'api')[:16],
            level=level,
            logger=(it.get('logger') or 'external')[:120],
            message=str(it['message'])[:4000],
            extra=json.dumps(it.get('extra', {}), ensure_ascii=False)[:2000],
            trace_id=trace_id,
        ))
    if objs:
        LogEntry.objects.bulk_create(objs, batch_size=200)
    return JsonResponse({'ok': True, 'accepted': len(objs)})
