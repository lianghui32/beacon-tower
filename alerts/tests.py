"""
alerts/tests.py — 告警引擎行为测试

用 unittest.mock 打桩 monitor.registry.metric_value（引擎每轮实时导入），
验证：触发/去重/恢复、for-duration 防抖、恢复迟滞（hysteresis）、静默窗口、
事件并发合并自愈。
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from alerts.engine import evaluate_once
from alerts.models import AlertEvent, AlertPolicy, NotificationRecord


def _policy(**kw):
    defaults = dict(
        name='测试策略', metric_key='host.cpu_percent', operator='>',
        threshold=80.0, level='P2',
    )
    defaults.update(kw)
    return AlertPolicy.objects.create(**defaults)


class EngineBaseTests(TestCase):
    def test_fires_event_once_and_dedups(self):
        policy = _policy()
        with mock.patch('monitor.registry.metric_value', return_value=95.0):
            fired, _ = evaluate_once()
            self.assertEqual(fired, 1)
            # 第二轮仍越限：不产生重复事件
            fired, _ = evaluate_once()
            self.assertEqual(fired, 0)
        events = AlertEvent.objects.filter(policy=policy)
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().status, 'firing')
        # 触发通知已生成（站内信 + webhook）
        self.assertTrue(NotificationRecord.objects.filter(event=events.first(),
                                                          kind='fire').exists())

    def test_recovers_when_condition_clears(self):
        policy = _policy()
        with mock.patch('monitor.registry.metric_value', return_value=95.0):
            evaluate_once()
        with mock.patch('monitor.registry.metric_value', return_value=30.0):
            _, recovered = evaluate_once()
            self.assertEqual(recovered, 1)
        event = AlertEvent.objects.get(policy=policy)
        self.assertEqual(event.status, 'resolved')
        self.assertIsNotNone(event.resolved_at)
        self.assertTrue(NotificationRecord.objects.filter(event=event,
                                                          kind='resolve').exists())

    def test_silenced_policy_skipped(self):
        policy = _policy(silenced_until=timezone.now() + timedelta(hours=1))
        with mock.patch('monitor.registry.metric_value', return_value=95.0):
            fired, _ = evaluate_once()
        self.assertEqual(fired, 0)
        self.assertEqual(AlertEvent.objects.filter(policy=policy).count(), 0)

    def test_disabled_and_value_none_never_fire(self):
        _policy(enabled=False)
        _policy(name='空数据策略', metric_key='host.cpu_percent2')
        with mock.patch('monitor.registry.metric_value', return_value=None):
            fired, _ = evaluate_once()
        self.assertEqual(fired, 0)
        self.assertEqual(AlertEvent.objects.count(), 0)


class ForDurationTests(TestCase):
    """for-duration 防抖：持续越限满 N 分钟才触发，毛刺不产生事件"""

    def test_breach_with_zero_for_fires_immediately(self):
        _policy(for_minutes=0)
        with mock.patch('monitor.registry.metric_value', return_value=95.0):
            fired, _ = evaluate_once()
        self.assertEqual(fired, 1)

    def test_short_spike_does_not_fire(self):
        policy = _policy(for_minutes=10)
        with mock.patch('monitor.registry.metric_value', return_value=95.0):
            fired, _ = evaluate_once()          # 首轮：进入观察期
            self.assertEqual(fired, 0)
            fired, _ = evaluate_once()          # 观察期未满
            self.assertEqual(fired, 0)
            self.assertEqual(AlertEvent.objects.count(), 0)
            # 观察期内条件解除：计时重置
        with mock.patch('monitor.registry.metric_value', return_value=30.0):
            evaluate_once()
        policy.refresh_from_db()
        self.assertIsNone(policy.breach_since)
        self.assertEqual(AlertEvent.objects.count(), 0)

    def test_sustained_breach_fires_after_duration(self):
        policy = _policy(for_minutes=10)
        with mock.patch('monitor.registry.metric_value', return_value=95.0):
            evaluate_once()
            self.assertEqual(AlertEvent.objects.count(), 0)
            # 时间快进：观察期已满 -> 触发
            AlertPolicy.objects.filter(pk=policy.pk).update(
                breach_since=timezone.now() - timedelta(minutes=11))
            fired, _ = evaluate_once()
            self.assertEqual(fired, 1)
        self.assertEqual(AlertEvent.objects.count(), 1)
        policy.refresh_from_db()
        self.assertIsNone(policy.breach_since)  # 触发后清空计时


class HysteresisTests(TestCase):
    """恢复迟滞：指标回到恢复阈值的安全侧才算恢复，阈值附近抖动不反复"""

    def _firing(self, resolve_threshold):
        policy = _policy(threshold=80.0, operator='>', resolve_threshold=resolve_threshold)
        with mock.patch('monitor.registry.metric_value', return_value=95.0):
            evaluate_once()
        return policy

    def test_without_threshold_recovers_immediately(self):
        policy = self._firing(None)
        with mock.patch('monitor.registry.metric_value', return_value=79.0):
            _, recovered = evaluate_once()
        self.assertEqual(recovered, 1)
        self.assertEqual(AlertEvent.objects.get(policy=policy).status, 'resolved')

    def test_between_thresholds_stays_firing(self):
        policy = self._firing(resolve_threshold=60.0)
        with mock.patch('monitor.registry.metric_value', return_value=70.0):
            _, recovered = evaluate_once()
        self.assertEqual(recovered, 0)
        self.assertEqual(AlertEvent.objects.get(policy=policy).status, 'firing')

    def test_recovers_after_crossing_resolve_threshold(self):
        policy = self._firing(resolve_threshold=60.0)
        with mock.patch('monitor.registry.metric_value', return_value=50.0):
            _, recovered = evaluate_once()
        self.assertEqual(recovered, 1)
        self.assertEqual(AlertEvent.objects.get(policy=policy).status, 'resolved')


class MergeDuplicateTests(TestCase):
    def test_duplicate_firing_events_merged(self):
        policy = _policy()
        AlertEvent.objects.create(policy=policy, status='firing', level='P2',
                                  value=1, summary='旧')
        AlertEvent.objects.create(policy=policy, status='firing', level='P2',
                                  value=2, summary='新')
        from alerts.engine import _merge_duplicate_firing_events
        _merge_duplicate_firing_events()
        self.assertEqual(AlertEvent.objects.filter(policy=policy, status='firing').count(), 1)
