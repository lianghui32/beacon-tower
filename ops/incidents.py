"""
ops/incidents.py — 故障事件管理：告警自动聚合为 Incident

规则：新告警触发时，若存在 30 分钟内开始且尚未关闭的故障单，则并入
（级别取更高者）；否则新建故障单。故障单下所有告警恢复后自动标记恢复，
人工填写根因/改进后可关闭并导出复盘报告。
"""
from datetime import timedelta

from django.utils import timezone

LEVEL_ORDER = {'P0': 0, 'P1': 1, 'P2': 2, '提示': 3}
_LEVEL_NAME = {v: k for k, v in LEVEL_ORDER.items()}


def attach_or_create(event):
    """把一条新触发的告警并入现有故障单（30 分钟窗口）或新建故障单"""
    from .models import Incident

    if event.policy_id:
        recent = Incident.objects.filter(
            status='open',
            started_at__gte=timezone.now() - timedelta(minutes=30),
        ).order_by('-started_at').first()
        if recent:
            event.incident = recent
            event.save(update_fields=['incident'])
            if LEVEL_ORDER.get(event.level, 9) < LEVEL_ORDER.get(recent.level, 9):
                Incident.objects.filter(pk=recent.pk).update(level=event.level)
            # 并入时补全涉及服务（首次创建只取了首条策略的域）
            svc = event.policy.metric_key.split('.')[0] if event.policy else ''
            if svc and svc not in recent.services.split(','):
                new_services = f'{recent.services},{svc}'.strip(',')[:200]
                Incident.objects.filter(pk=recent.pk).update(services=new_services)
            return recent
    incident = Incident.objects.create(
        title=event.summary[:150],
        level=event.level,
        started_at=event.started_at,
        services=event.policy.metric_key.split('.')[0] if event.policy else '',
    )
    event.incident = incident
    event.save(update_fields=['incident'])
    return incident


def maybe_resolve(incident):
    """故障单下所有告警恢复后，自动标记恢复"""
    firing = incident.alerts.filter(status='firing').count()
    if firing == 0 and incident.status == 'open':
        incident.status = 'resolved'
        incident.resolved_at = timezone.now()
        incident.save(update_fields=['status', 'resolved_at'])
        return True
    return False


def incident_markdown(incident):
    """导出故障复盘报告（Markdown）"""
    from django.utils import timezone as tz

    alerts = incident.alerts.select_related('policy').order_by('started_at')
    resolved = incident.resolved_at or timezone.now()
    lines = [
        f'# 故障复盘报告：{incident.title}',
        '',
        f'- 级别：**{incident.level}**  状态：{incident.get_status_display()}',
        f'- 开始：{tz.localtime(incident.started_at):%Y-%m-%d %H:%M:%S}',
        f'- 恢复：{tz.localtime(resolved):%Y-%m-%d %H:%M:%S}' if incident.resolved_at else '- 恢复：未恢复',
        f'- 持续：**{incident.duration_min} 分钟**',
        f'- 涉及服务：{incident.services or "-"}',
        '',
        '## 告警时间线',
        '',
        '| 时间 | 级别 | 告警 | 状态 |',
        '|---|---|---|---|',
    ]
    for a in alerts:
        lines.append(
            f"| {tz.localtime(a.started_at):%H:%M:%S} | {a.level} | {a.summary} | "
            f"{'触发' if a.status == 'firing' else '恢复'} |"
        )
    lines += ['', '## 根因分析', '', incident.root_cause or '（待填写）', '',
              '## 改进措施', '', incident.lessons or '（待填写）', '']
    return '\n'.join(lines)
