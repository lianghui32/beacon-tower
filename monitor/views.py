"""
monitor/views.py — 监控总览 / APM / 自定义大盘 / 诊断报告 / 压测 / Prometheus 指标
"""
import json
import math

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from . import services
from .diagnoser import full_report, report_markdown
from .metrics import prometheus_text_cached
from .models import DashCard
from .registry import catalog, series
from .security import rate_limit, require_ingest


def _minutes(request, default=60):
    try:
        m = int(request.GET.get('minutes', default))
    except (TypeError, ValueError):
        m = default
    return max(5, min(4320, m))


def login_page_sensitive(view):
    """DEBUG 报错页不展示本视图局部变量（demo_pass 等敏感值）"""
    return sensitive_variables()(view)


# ===================== 登录页（展示只读演示账号 + 暴力破解限流） =====================

# 失败锁定阈值：同一用户名 10 次 / 同一 IP 30 次（10 分钟窗口，计数见 ops/audit 信号）
_LOGIN_FAIL_USER_LIMIT = 10
_LOGIN_FAIL_IP_LIMIT = 30


def _login_ip_locked(request):
    """IP 维度预检（锁定攻击者自己的 IP）；用户名维度不做预检——
    否则攻击者拿用户名就能把受害者（含知道正确密码的本人）锁在门外（DoS）。
    用户名维度在认证失败后由自定义逻辑拦截：错密码 429，正确密码始终放行。"""
    from django.core.cache import cache
    ip = (request.META.get('REMOTE_ADDR') or 'unknown')[:40]
    ip_fails = cache.get(f'obs-login-fail:ip:{ip}', 0) or 0
    return ip_fails >= _LOGIN_FAIL_IP_LIMIT


def _username_locked(request):
    from django.core.cache import cache
    username = (request.POST.get('username') or '').strip().lower()[:40]
    if not username:
        return False
    fails = cache.get(f'obs-login-fail:u:{username}', 0) or 0
    return fails >= _LOGIN_FAIL_USER_LIMIT


def _clear_login_counters(username, request):
    from django.core.cache import cache
    ip = (request.META.get('REMOTE_ADDR') or 'unknown')[:40]
    cache.delete(f'obs-login-fail:u:{(username or "").strip().lower()[:40]}')
    cache.delete(f'obs-login-fail:ip:{ip}')


@login_page_sensitive
def login_page(request):
    """登录视图：读取 demo_credentials.txt，把只读演示账号展示在登录页，
    方便向他人演示时直接登录；文件不存在则不展示。

    展示前提（防止凭据文件被误写入管理员账号时公开泄露）：
    - 仅在 DEBUG 模式展示；
    - 只取文件中的第一组凭据；
    - 该账号必须真实存在、已启用、且属于"演示访客"组。

    暴力破解防护（见 ops/audit 信号计数）：
    - IP 维度：失败 30 次/10 分钟 -> 该 IP 直接 429（攻击者自锁）；
    - 用户名维度：失败 10 次/10 分钟 -> 后续尝试先验密，错密码 429、
      正确密码正常放行并清零（防止"拿用户名锁人"的 DoS）。
    """
    from django.contrib.auth import authenticate, login as auth_login
    from django.contrib.auth.views import LoginView
    from django.http import HttpResponse
    from pathlib import Path

    if request.method == 'POST' and _login_ip_locked(request):
        return HttpResponse('该 IP 登录失败次数过多，请 10 分钟后再试。', status=429)

    if request.method == 'POST' and _username_locked(request):
        # 锁定期内：先验密。正确密码放行并清零；错密码 429（信号继续计数维持锁定）
        username = (request.POST.get('username') or '').strip()
        user = authenticate(request, username=username,
                            password=request.POST.get('password', ''))
        if user is not None:
            _clear_login_counters(username, request)
            auth_login(request, user)
            return redirect('/')
        return HttpResponse(
            '该账号因多次登录失败已被临时锁定（10 分钟），'
            '使用正确密码可直接登录。', status=429)

    demo_user = demo_pass = ''
    if settings.OBSERVABILITY['SHOW_DEMO_ACCOUNT']:
        # 凭据文件在 DATA_DIR（部署时可写卷）；本机开发历史位置在 BASE_DIR，兼容读一下
        cred_file = Path(settings.DATA_DIR) / 'demo_credentials.txt'
        if not cred_file.exists():
            cred_file = Path(settings.BASE_DIR) / 'demo_credentials.txt'
        if cred_file.exists():
            # 只解析第一组 username/password（遇到第二组即停止），并校验账号确属演示组
            parsed_user = parsed_pass = ''
            for line in cred_file.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line.startswith('username=') and not parsed_user:
                    parsed_user = line.split('=', 1)[1].strip()
                elif line.startswith('password=') and parsed_user and not parsed_pass:
                    parsed_pass = line.split('=', 1)[1].strip()
                    break
            if parsed_user and parsed_pass:
                from django.contrib.auth.models import User
                from .security import DEMO_GROUP_NAME
                ok = User.objects.filter(
                    username=parsed_user, is_active=True,
                    groups__name=DEMO_GROUP_NAME,
                ).exists()
                if ok:
                    demo_user, demo_pass = parsed_user, parsed_pass
    return LoginView.as_view(
        template_name='registration/login.html',
        extra_context={'demo_user': demo_user, 'demo_pass': demo_pass},
    )(request)


# ===================== 监控总览 =====================

@require_GET
def overview(request):
    """全站监控总览（首页）"""
    minutes = _minutes(request, 60)
    return render(request, 'monitor/overview.html', {'minutes': minutes})


@require_GET
def api_overview(request):
    minutes = _minutes(request, 60)
    return JsonResponse(services.overview_data(minutes))


@require_GET
def api_health(request):
    """轻量健康检查端点：不查库、无业务逻辑，专供拨测/负载均衡探活。

    位于 /api/ 下，接入令牌或登录会话均可访问——
    平台对自身的可用性拨测不再需要携带令牌访问页面路径。
    """
    from django.utils import timezone as tz
    return JsonResponse({'ok': True, 'service': 'beacon-tower',
                         'time': tz.localtime(tz.now()).strftime('%Y-%m-%d %H:%M:%S')})


# ===================== 自定义大盘 =====================

@require_GET
def dashboard_page(request):
    cards = DashCard.objects.all()
    return render(request, 'monitor/dashboard.html', {
        'cards': cards,
        'catalog': catalog(),
    })


@require_POST
def dashboard_add(request):
    try:
        minutes = max(5, min(4320, int(request.POST.get('minutes', '60'))))
        span = max(1, min(3, int(request.POST.get('span', '1'))))
    except (TypeError, ValueError):
        minutes, span = 60, 1
    key = request.POST.get('metric_key') or 'host.cpu_percent'
    meta = series(key, 5)
    if not meta:
        # 不允许创建指向不存在指标的"死卡片"
        from django.contrib import messages
        messages.error(request, f'未知指标 {key}，请从指标目录中选择')
        return redirect('monitor:dashboard')
    chart_type = request.POST.get('chart_type') or 'line'
    if chart_type not in ('line', 'bar', 'area'):
        chart_type = 'line'
    from django.db.models import Max
    next_order = (DashCard.objects.aggregate(m=Max('order'))['m'] or 0) + 1
    DashCard.objects.create(
        title=(request.POST.get('title') or meta.get('label', key))[:64],
        metric_key=key,
        chart_type=chart_type,
        minutes=minutes,
        span=span,
        order=next_order,
    )
    return redirect('monitor:dashboard')


@require_POST
def dashboard_delete(request, pk):
    DashCard.objects.filter(pk=pk).delete()
    return redirect('monitor:dashboard')


@require_POST
def dashboard_move(request, pk, direction):
    """大盘卡片上移/下移（与相邻卡片交换 order，事务保证不出现重复序号）"""
    card = get_object_or_404(DashCard, pk=pk)
    if direction == 'up':
        neighbor = DashCard.objects.filter(order__lt=card.order).order_by('-order').first()
    else:
        neighbor = DashCard.objects.filter(order__gt=card.order).order_by('order').first()
    if neighbor:
        with transaction.atomic():
            card_order, neighbor_order = card.order, neighbor.order
            card.order, neighbor.order = -1, -2  # 先错开，规避可能的唯一约束冲突
            card.save(update_fields=['order'])
            neighbor.save(update_fields=['order'])
            card.order, neighbor.order = neighbor_order, card_order
            card.save(update_fields=['order'])
            neighbor.save(update_fields=['order'])
    return redirect('monitor:dashboard')


@require_GET
def api_metric(request):
    """指标序列查询（大盘卡片 / 通用）：?key=host.cpu_percent&minutes=60"""
    key = request.GET.get('key', '')
    minutes = _minutes(request, 60)
    s = series(key, minutes)
    if not s:
        return JsonResponse({'error': 'unknown metric key'}, status=400)
    return JsonResponse(s)


# ===================== 自定义指标上报 API =====================

@require_GET
def custom_metrics_page(request):
    """自定义指标列表（最近 50 条）"""
    from .models import CustomMetric
    rows = [
        {'name': m.name, 'value': m.value, 'labels': m.labels,
         'time': timezone.localtime(m.created_at).strftime('%m-%d %H:%M:%S')}
        for m in CustomMetric.objects.all()[:50]
    ]
    return JsonResponse({'rows': rows})


@require_POST
@csrf_exempt
@require_ingest
@rate_limit('metrics-ingest', rate=120, per=60)
def custom_metrics_ingest(request):
    """自定义指标接入 API

    POST /api/ingest/metrics/
    {"name": "order_queue_length", "value": 37, "labels": {"queue": "refund"}}
    也接受数组批量上报（上限 200 条）。
    """
    from django.utils import timezone as tz

    from .models import CustomMetric
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'invalid json'}, status=400)
    items = payload if isinstance(payload, list) else [payload]
    objs = []
    for it in items[:200]:
        if not isinstance(it, dict) or not it.get('name'):
            continue
        try:
            value = float(it.get('value'))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            # NaN / Infinity 入库会让聚合结果失效，直接丢弃
            continue
        objs.append(CustomMetric(
            name=str(it['name'])[:64],
            labels=json.dumps(it.get('labels', {}), ensure_ascii=False)[:1000],
            value=value,
            created_at=tz.now(),
        ))
    if objs:
        CustomMetric.objects.bulk_create(objs, batch_size=100)
    return JsonResponse({'ok': True, 'accepted': len(objs)})


# ===================== APM =====================

@require_GET
def apm_page(request):
    minutes = _minutes(request, 60)
    return render(request, 'monitor/apm.html', {'minutes': minutes})


@require_GET
def api_apm_transactions(request):
    minutes = _minutes(request, 60)
    return JsonResponse({'rows': services.apm_transactions(minutes)})


@require_GET
def apm_transactions_export(request):
    """接口分析表导出 CSV（带 BOM；防公式注入）"""
    import csv as csv_mod

    def _csv_safe(v):
        # Excel 会把 =/-/+/@ 开头的单元格当公式执行（CSV 注入），加前缀防解析
        s = str(v)
        if s[:1] in ('=', '-', '+', '@'):
            return "'" + s
        return s

    minutes = _minutes(request, 60)
    rows = services.apm_transactions(minutes)
    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="apm_transactions_{minutes}m.csv"'
    resp.write('\ufeff')
    writer = csv_mod.writer(resp)
    writer.writerow(['路径', '请求数', '平均耗时(ms)', 'P95(ms)', '最慢(ms)',
                     '错误率(%)', '平均SQL', '慢查询数'])
    for r in rows:
        writer.writerow([_csv_safe(r['path']), r['n'], r['avg_ms'], r['p95_ms'], r['max_ms'],
                         r['error_rate'], r['avg_sql'], r['slow_queries']])
    return resp


@require_GET
def api_apm_traces(request):
    minutes = _minutes(request, 60)
    only_slow = request.GET.get('slow') == '1'
    only_error = request.GET.get('error') == '1'
    return JsonResponse({'rows': services.apm_traces(
        minutes, only_slow=only_slow, only_error=only_error)})


@require_GET
def trace_page(request, trace_id):
    """调用链详情页（服务端渲染）"""
    trace = services.get_trace(trace_id)
    if not trace:
        return render(request, 'monitor/trace_missing.html', status=404)
    return render(request, 'monitor/trace.html', {'trace': trace})


@require_GET
def apm_database_page(request):
    minutes = _minutes(request, 1440)
    data = services.apm_database(minutes)
    return render(request, 'monitor/database.html', {
        'data': data,
        'minutes': minutes,
    })


# ===================== 性能诊断（保留原核心创新） =====================

@require_GET
def diagnose_view(request):
    """自动诊断报告页"""
    report = full_report()
    return render(request, 'monitor/diagnose.html', {'report': report})


@require_GET
def diagnose_export(request):
    """导出 Markdown 诊断报告"""
    md = report_markdown(full_report())
    resp = HttpResponse(md, content_type='text/markdown; charset=utf-8')
    resp['Content-Disposition'] = 'attachment; filename="diagnose_report.md"'
    return resp


# ===================== 基准压测 =====================

def benchmark_view(request):
    """基准压测页：POST 迭代次数即运行对比压测

    压测在请求线程内同步执行，限制上限并做节流（60 秒内不重复运行），
    防止并发/重复提交把 worker 占满。
    """
    results = None
    iterations = 20
    error = None
    if request.method == 'POST':
        try:
            iterations = max(1, min(100, int(request.POST.get('iterations', 20))))
        except (TypeError, ValueError):
            error = '迭代次数必须为整数'
        if error is None:
            import time as _time
            _last = getattr(benchmark_view, '_last_run', 0.0)
            if _time.monotonic() - _last < 60:
                error = '压测刚运行过，请稍候 60 秒再试（避免影响在线数据）'
            else:
                benchmark_view._last_run = _time.monotonic()
                results = services.run_benchmark_suite(iterations=iterations)
    return render(request, 'monitor/benchmark.html', {
        'results': results,
        'iterations': iterations,
        'error': error,
    })


# ===================== 接入中心 =====================

@sensitive_variables()
@require_GET
def integration_page(request):
    """接入中心：账号认证 / 接入令牌 / 多服务器 Agent / 各观测域接入方式

    完整令牌仅对 staff 展示；普通登录用户看到掩码令牌（前 4 位 + ****），
    并提示联系管理员获取，降低令牌泄露面。响应禁止缓存。
    sensitive_variables：DEBUG 报错页不展示局部变量中的令牌。
    """
    token = settings.OBSERVABILITY.get('INGEST_TOKEN', '')
    if request.user.is_staff:
        token_display = token
    else:
        token_display = (token[:4] + '…（掩码显示，完整令牌请联系管理员）') if token else ''
    resp = render(request, 'monitor/integration.html', {
        'token': token_display,
        'token_masked': not request.user.is_staff,
    })
    resp['Cache-Control'] = 'no-store'
    return resp


# ===================== Prometheus 指标 =====================

@require_GET
def metrics_endpoint(request):
    """简版 Prometheus 文本格式指标（5 秒进程内缓存，适配 15s 抓取周期）"""
    return HttpResponse(prometheus_text_cached(),
                        content_type='text/plain; version=0.0.4; charset=utf-8')
