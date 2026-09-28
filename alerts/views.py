"""
alerts/views.py — 告警中心页面：策略管理 / 事件列表 / 通知记录
"""
from datetime import timedelta

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .engine import evaluate_once
from .models import AlertEvent, AlertPolicy, NotificationRecord


@require_GET
def policies_page(request):
    from django.conf import settings
    mon = settings.OBSERVABILITY
    policies = AlertPolicy.objects.all()
    catalog = _catalog()
    return render(request, 'alerts/policies.html', {
        'policies': policies,
        'catalog': catalog,
        'alert_interval': mon['ALERT_INTERVAL_SEC'],
        'alert_window': mon['ALERT_WINDOW_MIN'],
        'now': timezone.now(),
    })


@require_POST
def policy_create(request):
    from django.contrib import messages

    from ops.audit import audit

    from monitor.registry import series
    import math

    name = (request.POST.get('name') or '未命名策略')[:80]
    metric_key = (request.POST.get('metric_key') or 'host.cpu_percent')[:64]
    operator = request.POST.get('operator') or '>'
    level = request.POST.get('level') or 'P2'
    try:
        threshold = float(request.POST.get('threshold', '0'))
    except (TypeError, ValueError):
        threshold = 0.0
    # for-duration：持续越限 N 分钟才触发（0=立即）；恢复阈值：留空=条件解除即恢复
    try:
        for_minutes = max(0.0, min(1440.0, float(request.POST.get('for_minutes', '0') or 0)))
    except (TypeError, ValueError):
        for_minutes = 0.0
    resolve_raw = (request.POST.get('resolve_threshold') or '').strip()
    resolve_threshold = None
    if resolve_raw:
        try:
            resolve_threshold = float(resolve_raw)
        except (TypeError, ValueError):
            resolve_threshold = None

    # 白名单校验：非法输入回流列表页并提示，而不是静默生成永不触发/报错的策略
    if operator not in ('>', '<'):
        operator = '>'
    if level not in ('P0', 'P1', 'P2', '提示'):
        level = 'P2'
    if not math.isfinite(threshold):
        messages.error(request, '阈值必须是有穷数（不能是 NaN/Infinity）')
        return redirect('alerts:policies')
    if resolve_threshold is not None and not math.isfinite(resolve_threshold):
        messages.error(request, '恢复阈值必须是有穷数（不能是 NaN/Infinity）')
        return redirect('alerts:policies')
    if not series(metric_key, 5):
        messages.error(request, f'未知指标 {metric_key}，请从指标目录中选择')
        return redirect('alerts:policies')

    policy = AlertPolicy.objects.create(
        name=name,
        metric_key=metric_key,
        operator=operator,
        threshold=threshold,
        level=level,
        for_minutes=for_minutes,
        resolve_threshold=resolve_threshold,
        note=(request.POST.get('note') or '')[:200],
    )
    audit(request, '创建告警策略', policy.name, policy.describe())
    return redirect('alerts:policies')


@require_POST
def policy_toggle(request, pk):
    from ops.audit import audit
    policy = get_object_or_404(AlertPolicy, pk=pk)
    policy.enabled = not policy.enabled
    policy.save(update_fields=['enabled'])
    audit(request, f'{"启用" if policy.enabled else "停用"}告警策略', policy.name)
    return redirect('alerts:policies')


@require_POST
def policy_delete(request, pk):
    from ops.audit import audit
    policy = get_object_or_404(AlertPolicy, pk=pk)
    audit(request, '删除告警策略', policy.name, policy.describe())
    policy.delete()
    return redirect('alerts:policies')


@require_POST
def policy_evaluate_now(request):
    """手动触发一轮评估（演示用）"""
    fired, recovered = evaluate_once()
    return JsonResponse({'ok': True, 'fired': fired, 'recovered': recovered})


@require_GET
def events_page(request):
    status = (request.GET.get('status') or '').strip()
    qs = AlertEvent.objects.select_related('policy', 'incident')
    if status in ('firing', 'resolved'):
        if status == 'firing':
            qs = qs.filter(status='firing')
        else:
            qs = qs.filter(status='resolved')
    events = qs[:100]
    firing = AlertEvent.objects.filter(status='firing').count()
    # MTTR / MTTA（近 7 天已恢复事件）
    week_ago = timezone.now() - timedelta(days=7)
    resolved = list(AlertEvent.objects.filter(
        status='resolved', resolved_at__isnull=False, started_at__gte=week_ago)[:300])
    mttr = round(sum((e.resolved_at - e.started_at).total_seconds() / 60 for e in resolved)
                 / len(resolved), 1) if resolved else None
    acked = [e for e in resolved if e.ack_at]
    mtta = round(sum((e.ack_at - e.started_at).total_seconds() / 60 for e in acked)
                 / len(acked), 1) if acked else None
    return render(request, 'alerts/events.html', {
        'events': events, 'status': status, 'firing': firing,
        'mttr': mttr, 'mtta': mtta,
    })


@require_POST
def event_ack(request, pk):
    """确认告警：记录确认人与时间，表示已介入处理"""
    event = get_object_or_404(AlertEvent, pk=pk)
    event.ack_by = (request.user.username if request.user.is_authenticated else '匿名')[:60]
    event.ack_at = timezone.now()
    if not event.handle_note:
        event.handle_note = request.POST.get('note', '')[:1000]
    event.save(update_fields=['ack_by', 'ack_at', 'handle_note'])
    from ops.audit import audit
    audit(request, '确认告警', event.policy.name, event.summary[:120])
    return redirect('alerts:events')


@require_POST
def event_note(request, pk):
    """追加处理备注"""
    event = get_object_or_404(AlertEvent, pk=pk)
    note = (request.POST.get('note') or '').strip()[:1000]
    if note:
        event.handle_note = ((event.handle_note + '\n') if event.handle_note else '') + note
        event.save(update_fields=['handle_note'])
        from ops.audit import audit
        audit(request, '告警处理备注', event.policy.name, note[:120])
    return redirect('alerts:events')


@require_POST
def policy_silence(request, pk):
    """静默策略 N 小时（维护/发版窗口）"""
    policy = get_object_or_404(AlertPolicy, pk=pk)
    try:
        hours = max(0.5, min(72, float(request.POST.get('hours', '2') or 2)))
    except (TypeError, ValueError):
        hours = 2
    policy.silenced_until = timezone.now() + timedelta(hours=hours)
    policy.save(update_fields=['silenced_until'])
    from ops.audit import audit
    audit(request, '静默告警策略', policy.name, f'静默 {hours} 小时')
    return redirect('alerts:policies')


@require_POST
def policy_unsilence(request, pk):
    policy = get_object_or_404(AlertPolicy, pk=pk)
    policy.silenced_until = None
    policy.save(update_fields=['silenced_until'])
    from ops.audit import audit
    audit(request, '解除静默', policy.name)
    return redirect('alerts:policies')


@require_GET
def notifications_page(request):
    channel = (request.GET.get('channel') or '').strip()
    qs = NotificationRecord.objects.select_related('event')
    if channel:
        qs = qs.filter(channel=channel)
    return render(request, 'alerts/notifications.html', {
        'records': qs[:100],
        'channel': channel,
    })


def _catalog():
    from monitor.registry import catalog
    return catalog()
