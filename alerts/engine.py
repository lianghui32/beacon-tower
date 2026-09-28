"""
alerts/engine.py — 自研告警评估引擎（后台线程）

每 ALERT_INTERVAL_SEC 秒执行一轮：
1. 遍历启用且未在静默窗口内的策略，从指标注册表取最近评估窗口（默认 5 分钟）的均值；
2. 满足条件且该策略无未恢复事件 -> 生成 AlertEvent + 故障单聚合 + 通知 + 自愈动作；
3. 有未恢复事件且条件解除 -> 标记恢复 + 发恢复通知（故障单可能随之自动恢复）；
4. 顺带清理过期的采集数据（保留 RETENTION_DAYS，分批删除避免长事务锁库）。

并发设计：
- 进程内 evaluate_lock 保证多线程（后台引擎 + "立即评估"请求）不并发评估；
- 跨进程由任务租约选主（monitor/leadership.py）：多副本 worker 只有一个在评估，
  持有者宕机后其它进程在 TTL 内自动接管；
- 告警线程内同步发通知/自愈可能阻塞，均设有独立超时（见 ops.notify / ops.heal）。
"""
import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

MON = settings.OBSERVABILITY

# 单轮评估互斥锁：后台引擎线程与页面"立即评估"共用，
# 避免 TOCTOU 产生重复事件/重复通知/重复自愈
evaluate_lock = threading.Lock()


def _fmt_value(v):
    if v is None:
        return '-'
    return f'{v:g}'


def _notify(event, kind):
    """产生通知记录：站内信 + 模拟 Webhook + 真实渠道（邮件/企业微信/钉钉/通用）

    触发与恢复各生成一条记录（kind 参与去重键），恢复通知不会再被触发记录吞掉。
    """
    action = '触发' if kind == 'fire' else '恢复'
    title = f'[{event.level}] {event.summary}'
    content = (
        f'告警{action}通知\n'
        f'策略：{event.policy.name}\n'
        f'条件：{event.policy.describe()}\n'
        f'当前值：{_fmt_value(event.value)}\n'
        f'时间：{timezone.localtime(event.started_at if kind == "fire" else event.resolved_at):%Y-%m-%d %H:%M:%S}'
    )
    from .models import NotificationRecord
    NotificationRecord.objects.get_or_create(
        event=event, channel='站内信', kind=kind,
        defaults={'title': title[:150], 'content': content},
    )
    NotificationRecord.objects.get_or_create(
        event=event, channel='webhook', kind=kind,
        defaults={'title': title[:150], 'content': f'POST https://hooks.example.com/obs {content[:800]}'},
    )
    # 真实渠道（ops.NotifyConfig 启用后生效）
    try:
        from ops.notify import send_notify
        for channel, status in send_notify(title, content):
            NotificationRecord.objects.get_or_create(
                event=event, channel=channel[:16], kind=kind,
                defaults={'title': f'{title[:120]} [{status[:20]}]', 'content': content[:1500]},
            )
    except Exception:
        logger.exception('外部通知渠道发送失败')


def _condition(value, policy):
    if value is None:
        return False
    return value > policy.threshold if policy.operator == '>' else value < policy.threshold


def _merge_duplicate_firing_events():
    """自愈：多线程/多进程同时评估可能为同一策略产生多条触发中事件，合并保留最新一条"""
    from .models import AlertEvent
    for policy_id in AlertEvent.objects.filter(status='firing').values_list('policy_id', flat=True).distinct():
        dupes = list(AlertEvent.objects.filter(policy_id=policy_id, status='firing').order_by('-started_at'))
        for extra in dupes[1:]:
            extra.delete()


def _recovered(value, policy):
    """恢复判定：指标回到安全侧才算恢复（恢复迟滞）。

    未配置恢复阈值时，条件解除（breach=False）即恢复；
    配置后要求 value 严格越过 resolve_threshold 的安全侧——
    指标在触发阈值附近抖动时不会反复触发/恢复（防 flapping）。
    """
    if policy.resolve_threshold is None:
        return True
    if value is None:
        return True  # 数据中断视为解除，事件挂起无意义
    return (value < policy.resolve_threshold if policy.operator == '>'
            else value > policy.resolve_threshold)


def evaluate_once():
    """单轮评估：返回 (触发数, 恢复数)。

    进程内加锁：与"立即评估"按钮、多个后台线程互斥。
    已在评估中的调用直接返回 (0, 0)，不排队等待（引擎下一轮会重新评估）。

    评估顺序（每条策略）：
    1. 静默窗口内的策略直接跳过；
    2. 越限且无事件：for_minutes>0 时先进入观察期（breach_since），
       持续越限满 N 分钟才升级为事件——瞬时毛刺不产生告警；
    3. 有事件且"回到安全侧"（未配置恢复阈值时=条件解除）：标记恢复。
    """
    from ops.heal import process_heal_for_event
    from ops.incidents import attach_or_create, maybe_resolve

    from .models import AlertEvent, AlertPolicy

    if not evaluate_lock.acquire(blocking=False):
        return 0, 0
    try:
        window = MON['ALERT_WINDOW_MIN']
        fired = recovered = 0

        _merge_duplicate_firing_events()

        for policy in AlertPolicy.objects.filter(enabled=True):
            try:
                # 静默窗口：维护/发版期间跳过评估
                if policy.silenced_until and policy.silenced_until > timezone.now():
                    continue
                from monitor.registry import metric_value
                value = metric_value(policy.metric_key, minutes=window)
                breach = _condition(value, policy)
                active = AlertEvent.objects.filter(policy=policy, status='firing').first()
                now = timezone.now()

                if breach and not active:
                    # for-duration 防抖：首次越限记时，持续满 for_minutes 才真正触发
                    if policy.for_minutes > 0:
                        if policy.breach_since is None:
                            policy.breach_since = now
                            policy.save(update_fields=['breach_since'])
                            continue
                        if (now - policy.breach_since).total_seconds() < policy.for_minutes * 60:
                            continue  # 观察期内，暂不触发
                    event = AlertEvent.objects.create(
                        policy=policy, status='firing', level=policy.level,
                        value=value or 0,
                        summary=(
                            f'{policy.name}：{policy.metric_key} 当前 '
                            f'{_fmt_value(value)}（{"高于" if policy.operator == ">" else "低于"}阈值 '
                            f'{_fmt_value(policy.threshold)}'
                            f'{f"，已持续 {policy.for_minutes:g} 分钟" if policy.for_minutes else ""}）'
                        )[:250],
                    )
                    if policy.breach_since is not None:
                        policy.breach_since = None
                        policy.save(update_fields=['breach_since'])
                    attach_or_create(event)          # 并入/创建故障单
                    _notify(event, 'fire')
                    process_heal_for_event(event)    # 自愈动作（尊重冷却期）
                    logger.warning('告警触发: %s (value=%s)', event.summary, value)
                    fired += 1
                elif not breach and active:
                    if not _recovered(value, policy):
                        # 恢复迟滞：指标未回到安全侧（在阈值附近抖动），保持 firing
                        active.last_value_at = now
                        if value is not None:
                            active.value = value
                        active.save(update_fields=['last_value_at', 'value'])
                        continue
                    if policy.breach_since is not None:
                        policy.breach_since = None
                        policy.save(update_fields=['breach_since'])
                    active.status = 'resolved'
                    active.resolved_at = now
                    active.value = value if value is not None else active.value
                    active.save(update_fields=['status', 'resolved_at', 'value'])
                    _notify(active, 'resolve')
                    if active.incident:
                        maybe_resolve(active.incident)
                    logger.info('告警恢复: %s', active.summary)
                    recovered += 1
                else:
                    if not breach and policy.breach_since is not None:
                        # 观察期内条件解除：重置计时（下一次越限重新观察）
                        policy.breach_since = None
                        policy.save(update_fields=['breach_since'])
                    if active:
                        active.last_value_at = now
                        if value is not None:
                            active.value = value
                        active.save(update_fields=['last_value_at', 'value'])
            except Exception:
                logger.exception('策略评估失败: %s', policy)
        return fired, recovered
    finally:
        evaluate_lock.release()


def _prune_model(model, deadline, batch=5000):
    """分批删除过期数据：SQLite 上一次大 DELETE 会长时间锁库，分批更平滑"""
    while True:
        pks = list(model.objects.filter(created_at__lt=deadline).values_list('pk', flat=True)[:batch])
        if not pks:
            break
        model.objects.filter(pk__in=pks).delete()
        if len(pks) < batch:
            break


def prune_old_data():
    """清理过期采集数据（请求指标 / 主机指标 / RUM 事件 / 日志 / 自定义指标）"""
    from loghub.models import LogEntry
    from hosts.models import HostMetric
    from monitor.models import CustomMetric, RequestMetric
    from rum.models import RumEvent

    deadline = timezone.now() - timedelta(days=MON['RETENTION_DAYS'])
    for model in (RequestMetric, HostMetric, RumEvent, LogEntry, CustomMetric):
        try:
            _prune_model(model, deadline)
        except Exception:
            logger.exception('清理 %s 过期数据失败', model.__name__)


_last_prune = [0.0]  # 清理节拍（可变容器，便于测试重置）


def alert_engine_round():
    """一轮：评估所有策略 + 每小时清理一次过期采集数据。

    顺序是"先评估再清理"，且清理单独兜异常——清理失败不能饿死告警评估。
    """
    evaluate_once()
    if time.time() - _last_prune[0] > 3600:
        _last_prune[0] = time.time()
        try:
            prune_old_data()
        except Exception:
            logger.exception('过期采集数据清理失败')


def alert_engine_loop():
    """告警评估线程主循环：只有持有租约的进程评估（多 worker 副本不会重复告警）"""
    from monitor.leadership import LeaseLoop

    LeaseLoop('alert-engine', alert_engine_round, MON['ALERT_INTERVAL_SEC']).run()
