"""
ops/views.py — 运维中心页面：拨测 / 故障单 / 巡检 / SLO / 资产 / 自愈 / 通知渠道 / 审计

权限：自愈（含命令执行）、通知渠道（含 SMTP 凭据）、操作审计属于高危/敏感
功能，仅对 staff 账号开放（演示访客已被中间件拦截，这里再收一层）。
"""
import json
from datetime import timedelta

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from .audit import audit
from .models import (
    Asset, AuditLog, HealAction, HealRun, Incident, InspectionRun,
    NotifyConfig, ProbeTask, SLO,
)


def _staff_required(view):
    """staff 专属视图：非 staff 跳转登录页"""
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_staff:
            return redirect_to_login(request.get_full_path())
        return view(request, *args, **kwargs)
    return wrapped


def _int(value, default, lo=None, hi=None):
    """POST 数值安全解析：非法/超界回落默认值"""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _float(value, default, lo=None, hi=None):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# ===================== 拨测（写操作 staff 专属：防止借平台做内网探测） =====================

@require_GET
def probe_page(request):
    tasks = ProbeTask.objects.all()
    return render(request, 'ops/probe.html', {'tasks': tasks})


@require_POST
@_staff_required
def probe_create(request):
    task = ProbeTask.objects.create(
        name=(request.POST.get('name') or '未命名任务')[:80],
        url=(request.POST.get('url') or '').strip()[:300],
        expect_status=_int(request.POST.get('expect_status'), 200, lo=100, hi=599),
        keyword=(request.POST.get('keyword') or '')[:100],
        interval_sec=_int(request.POST.get('interval_sec'), 60, lo=15, hi=86400),
        enabled=True,
    )
    audit(request, '创建拨测任务', task.name, task.url)
    return redirect('ops:probe')


@require_POST
@_staff_required
def probe_toggle(request, pk):
    task = get_object_or_404(ProbeTask, pk=pk)
    task.enabled = not task.enabled
    task.save(update_fields=['enabled'])
    audit(request, f'{"启用" if task.enabled else "停用"}拨测任务', task.name)
    return redirect('ops:probe')


@require_POST
@_staff_required
def probe_delete(request, pk):
    task = get_object_or_404(ProbeTask, pk=pk)
    audit(request, '删除拨测任务', task.name, task.url)
    task.delete()
    return redirect('ops:probe')


@require_POST
@_staff_required
def probe_run_now(request, pk):
    from .probing import run_probe
    task = get_object_or_404(ProbeTask, pk=pk)
    result = run_probe(task)
    audit(request, '手动拨测', task.name, f'ok={result.ok} {result.duration_ms}ms')
    return JsonResponse({'ok': result.ok, 'status': result.status_code,
                         'ms': result.duration_ms, 'error': result.error,
                         'cert_days': result.cert_days})


@require_GET
def probe_detail(request, pk):
    task = get_object_or_404(ProbeTask, pk=pk)
    results = task.results.all()[:200]
    return render(request, 'ops/probe_detail.html', {
        'task': task,
        'results': results,
        'availability': _probe_availability(task),
    })


def _probe_availability(task, hours=24):
    from django.db.models import Count, Q
    agg = task.results.filter(
        created_at__gte=timezone.now() - timedelta(hours=hours),
    ).aggregate(total=Count('id'), ok=Count('id', filter=Q(ok=True)))
    total = agg['total'] or 0
    if not total:
        return None
    return round((agg['ok'] or 0) * 100.0 / total, 2)


@require_GET
def api_probe_detail(request, pk):
    """拨测详情 AJAX：可用率/延迟趋势"""
    task = get_object_or_404(ProbeTask, pk=pk)
    since = timezone.now() - timedelta(hours=12)
    results = list(task.results.filter(created_at__gte=since).order_by('created_at'))
    from django.utils.timezone import localtime
    return JsonResponse({
        'series': [
            {'t': localtime(r.created_at).strftime('%H:%M'),
             'ok': 100 if r.ok else 0, 'ms': r.duration_ms}
            for r in results
        ],
        'availability': _probe_availability(task),
    })


# ===================== 故障事件 =====================

@require_GET
def incidents_page(request):
    from django.db.models import Avg, ExpressionWrapper, F, fields

    status = (request.GET.get('status') or '').strip()
    qs = Incident.objects.prefetch_related('alerts')
    if status in ('open', 'resolved', 'closed'):
        qs = qs.filter(status=status)
    # MTTR（近 7 天已恢复故障单）：聚合下推数据库
    week_ago = timezone.now() - timedelta(days=7)
    duration = ExpressionWrapper(
        F('resolved_at') - F('started_at'), output_field=fields.DurationField())
    mttr = Incident.objects.filter(
        resolved_at__isnull=False, started_at__gte=week_ago,
    ).annotate(dur=duration).aggregate(v=Avg('dur'))['v']
    mttr = round(mttr.total_seconds() / 60, 1) if mttr else None
    return render(request, 'ops/incidents.html', {
        'incidents': qs[:60], 'status': status, 'mttr': mttr,
    })


@require_GET
def incident_detail(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    alerts = incident.alerts.select_related('policy').order_by('started_at')
    # prefetch 通知记录，避免逐条告警再查一次（N+1）
    notifications = incident.alerts.select_related('policy').prefetch_related('notifications')
    flat = [n for a in notifications.all() for n in a.notifications.all()][:40]
    return render(request, 'ops/incident_detail.html', {
        'incident': incident,
        'alerts': alerts,
        'notifications': flat,
    })


@require_POST
def incident_update(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    incident.root_cause = request.POST.get('root_cause', incident.root_cause)[:4000]
    incident.lessons = request.POST.get('lessons', incident.lessons)[:4000]
    incident.services = request.POST.get('services', incident.services)[:200]
    incident.save(update_fields=['root_cause', 'lessons', 'services'])
    audit(request, '更新故障单复盘', incident.title)
    return redirect('ops:incident_detail', pk=pk)


@require_POST
def incident_close(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    incident.status = 'closed'
    if not incident.resolved_at:
        incident.resolved_at = timezone.now()
    incident.save(update_fields=['status', 'resolved_at'])
    audit(request, '关闭故障单', incident.title)
    return redirect('ops:incidents')


@require_POST
def incident_reopen(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    incident.status = 'open'
    incident.resolved_at = None
    incident.save(update_fields=['status', 'resolved_at'])
    audit(request, '重开故障单', incident.title)
    return redirect('ops:incidents')


@require_GET
def incident_export(request, pk):
    from .incidents import incident_markdown
    incident = get_object_or_404(Incident, pk=pk)
    md = incident_markdown(incident)
    resp = HttpResponse(md, content_type='text/markdown; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="postmortem_{pk}.md"'
    return resp


# ===================== 巡检 =====================

@require_GET
def inspection_page(request):
    runs = InspectionRun.objects.all()[:30]
    return render(request, 'ops/inspection.html', {'runs': runs})


@require_POST
def inspection_run_now(request):
    from .inspection import run_inspection
    run, result = run_inspection(trigger='manual')
    audit(request, '手动巡检', f'评分 {result["score"]}',
          f"不合格 {result['fails']} / 警告 {result['warns']}")
    return redirect('ops:inspection_detail', pk=run.pk)


@require_GET
def inspection_detail(request, pk):
    run = get_object_or_404(InspectionRun, pk=pk)
    try:
        data = json.loads(run.results or '{}')
    except (ValueError, TypeError):
        data = {'score': run.score, 'fails': 0, 'warns': 0, 'checks': [],
                'note': '结果数据损坏，无法展示明细'}
    return render(request, 'ops/inspection_detail.html', {
        'run': run,
        'data': data,
    })


@require_GET
def inspection_export(request, pk):
    from .inspection import inspection_markdown
    run = get_object_or_404(InspectionRun, pk=pk)
    md = inspection_markdown(run)
    resp = HttpResponse(md, content_type='text/markdown; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="inspection_{pk}.md"'
    return resp


# ===================== SLO =====================

@require_GET
def slo_page(request):
    from .slo import slo_status
    items = []
    for slo in SLO.objects.all():
        items.append({'slo': slo, 'status': slo_status(slo)})
    return render(request, 'ops/slo.html', {'items': items})


@require_POST
def slo_create(request):
    slo = SLO.objects.create(
        name=(request.POST.get('name') or '未命名 SLO')[:80],
        target_availability=_float(request.POST.get('target_availability'), 99.9, lo=0, hi=100),
        target_p95_ms=_float(request.POST.get('target_p95_ms'), 500, lo=1),
        window_days=_int(request.POST.get('window_days'), 30, lo=1, hi=90),
    )
    audit(request, '创建 SLO', slo.name)
    return redirect('ops:slo')


@require_POST
def slo_toggle(request, pk):
    slo = get_object_or_404(SLO, pk=pk)
    slo.enabled = not slo.enabled
    slo.save(update_fields=['enabled'])
    audit(request, f'{"启用" if slo.enabled else "停用"} SLO', slo.name)
    return redirect('ops:slo')


@require_POST
def slo_delete(request, pk):
    slo = get_object_or_404(SLO, pk=pk)
    audit(request, '删除 SLO', slo.name)
    slo.delete()
    return redirect('ops:slo')


@require_GET
def slo_export(request, pk):
    from .slo import slo_markdown, slo_status
    slo = get_object_or_404(SLO, pk=pk)
    md = slo_markdown(slo, slo_status(slo))
    resp = HttpResponse(md, content_type='text/markdown; charset=utf-8')
    resp['Content-Disposition'] = f'attachment; filename="slo_{pk}.md"'
    return resp


# ===================== 资产台账 =====================

@require_GET
def assets_page(request):
    assets = Asset.objects.all()
    return render(request, 'ops/assets.html', {'assets': assets})


@require_POST
def asset_save(request):
    hostname = (request.POST.get('hostname') or '').strip()[:128]
    if not hostname:
        return redirect('ops:assets')
    kind = request.POST.get('kind') or '主机'
    if kind not in ('主机', '应用', '数据库', '中间件', '其他'):
        kind = '主机'
    env = request.POST.get('env') or '未分类'
    if env not in ('生产', '测试', '开发', '未分类'):
        env = '未分类'
    obj, created = Asset.objects.update_or_create(
        hostname=hostname,
        defaults={
            'label': (request.POST.get('label') or '')[:80],
            'kind': kind,
            'env': env,
            'owner': (request.POST.get('owner') or '')[:40],
            'notes': (request.POST.get('notes') or '')[:200],
            'auto': False,
        },
    )
    audit(request, '新增资产' if created else '更新资产', hostname)
    return redirect('ops:assets')


@require_POST
def asset_delete(request, pk):
    obj = get_object_or_404(Asset, pk=pk)
    audit(request, '删除资产', obj.hostname)
    obj.delete()
    return redirect('ops:assets')


# ===================== 自愈动作（staff 专属：含命令执行能力） =====================

@require_GET
@_staff_required
def heal_page(request):
    from alerts.models import AlertPolicy
    actions = HealAction.objects.select_related('policy')
    runs = HealRun.objects.select_related('action')[:30]
    return render(request, 'ops/heal.html', {
        'actions': actions, 'runs': runs,
        'policies': AlertPolicy.objects.filter(enabled=True),
        'command_enabled': _command_enabled(),
    })


def _command_enabled():
    """command 类型自愈是否可用（取决于 OBS_HEAL_CMD_ALLOWLIST 是否配置了白名单）"""
    import os
    return bool(os.environ.get('OBS_HEAL_CMD_ALLOWLIST', '').strip())


@require_POST
@_staff_required
def heal_create(request):
    from alerts.models import AlertPolicy

    action_type = request.POST.get('action_type') or 'cleanup_tmp'
    if action_type not in ('cleanup_tmp', 'http_callback', 'command'):
        action_type = 'cleanup_tmp'
    policy = None
    pid = request.POST.get('policy')
    if pid:
        policy = AlertPolicy.objects.filter(pk=pid).first()
        if not policy:
            from django.contrib import messages
            messages.error(request, '所选告警策略不存在')
            return redirect('ops:heal')
    action = HealAction.objects.create(
        name=(request.POST.get('name') or '未命名动作')[:80],
        policy=policy,
        action_type=action_type,
        param=(request.POST.get('param') or '')[:300],
        enabled=False,
        cooldown_min=_int(request.POST.get('cooldown_min'), 30, lo=1, hi=10080),
    )
    audit(request, '创建自愈动作', action.name, f'{action.action_type}: {action.param}')
    return redirect('ops:heal')


@require_POST
@_staff_required
def heal_toggle(request, pk):
    action = get_object_or_404(HealAction, pk=pk)
    action.enabled = not action.enabled
    action.save(update_fields=['enabled'])
    audit(request, f'{"启用" if action.enabled else "停用"}自愈动作', action.name)
    return redirect('ops:heal')


@require_POST
@_staff_required
def heal_delete(request, pk):
    action = get_object_or_404(HealAction, pk=pk)
    audit(request, '删除自愈动作', action.name)
    action.delete()
    return redirect('ops:heal')


@require_POST
@_staff_required
def heal_test(request, pk):
    from .heal import run_action
    action = get_object_or_404(HealAction, pk=pk)
    run = run_action(action, reason='手动测试')
    audit(request, '手动测试自愈动作', action.name, f'成功={run.ok}')
    return redirect('ops:heal')


# ===================== 通知渠道（staff 专属：含 SMTP 凭据） =====================

@require_GET
@_staff_required
def notify_page(request):
    from alerts.models import NotificationRecord
    cfg = NotifyConfig.load()
    records = NotificationRecord.objects.order_by('-created_at')[:30]
    return render(request, 'ops/notify.html', {'cfg': cfg, 'records': records})


@require_POST
@_staff_required
@sensitive_post_parameters('smtp_pass')
def notify_save(request):
    cfg = NotifyConfig.load()
    cfg.enabled = request.POST.get('enabled') == 'on'
    cfg.smtp_host = (request.POST.get('smtp_host') or '').strip()[:120]
    cfg.smtp_port = _int(request.POST.get('smtp_port'), 465, lo=1, hi=65535)
    cfg.smtp_ssl = request.POST.get('smtp_ssl') == 'on'
    cfg.smtp_user = (request.POST.get('smtp_user') or '').strip()[:120]
    new_pass = (request.POST.get('smtp_pass') or '').strip()
    if new_pass:  # 留空表示不修改；save() 内部自动加密
        cfg.smtp_pass = new_pass[:120]
    cfg.mail_from = (request.POST.get('mail_from') or '').strip()[:120]
    cfg.mail_to = (request.POST.get('mail_to') or '').strip()[:300]
    cfg.webhook_generic = (request.POST.get('webhook_generic') or '').strip()[:300]
    cfg.wecom_webhook = (request.POST.get('wecom_webhook') or '').strip()[:300]
    cfg.ding_webhook = (request.POST.get('ding_webhook') or '').strip()[:300]
    cfg.save()
    audit(request, '更新通知渠道配置', f'enabled={cfg.enabled}')
    return redirect('ops:notify')


@require_POST
@_staff_required
def notify_test(request):
    from .notify import send_test
    channel = request.POST.get('channel', '邮件')
    try:
        status = send_test(channel)
    except Exception as e:
        status = f'failed: {e}'
    audit(request, '测试通知发送', channel, status)
    return JsonResponse({'channel': channel, 'status': status})


# ===================== 审计（staff 专属） =====================

@require_GET
@_staff_required
def audit_page(request):
    from django.db.models import Q
    q = (request.GET.get('q') or '').strip()
    action = (request.GET.get('action') or '').strip()
    logs = AuditLog.objects.all()
    if q:
        logs = logs.filter(Q(user__icontains=q) | Q(target__icontains=q) | Q(detail__icontains=q))
    if action:
        logs = logs.filter(action=action)
    actions = list(AuditLog.objects.values_list('action', flat=True).distinct()[:30])
    return render(request, 'ops/audit.html', {
        'logs': logs[:200], 'q': q, 'action': action, 'actions': actions,
    })
