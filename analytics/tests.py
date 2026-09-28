"""
analytics/tests.py — 智能分析算法库单元测试

覆盖四类算法的正常路径与边界：异常检测、线性预测、相关性、日志模板挖掘。
算法全部是纯函数，不需要数据库。
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from analytics.algorithms import (
    correlation_matrix,
    detect_anomalies,
    linear_forecast,
    mine_log_patterns,
    normalize_log,
    pearson,
)


def _series(values, start=None, step_min=1):
    start = start or (timezone.now() - timedelta(minutes=len(values)))
    return [{'t': (start + timedelta(minutes=i * step_min)).strftime('%m-%d %H:%M'), 'v': v}
            for i, v in enumerate(values)]


class AnomalyDetectionTests(TestCase):
    def test_flat_series_has_no_anomaly(self):
        points, desc = detect_anomalies(_series([10.0] * 40))
        self.assertEqual(desc['anomalies'], 0)

    def test_spike_over_flat_baseline_is_flagged(self):
        # 完全平稳基线（std=0）：3σ 失效，零方差规则兜底判定；z 无定义记 None
        values = [10.0] * 40 + [80.0] + [10.0] * 5
        points, desc = detect_anomalies(_series(values))
        self.assertGreaterEqual(desc['anomalies'], 1)
        flagged = [p for p in points if p.get('a')]
        self.assertTrue(flagged)
        self.assertIsNone(flagged[0]['z'])

    def test_spike_over_noisy_baseline_has_z_score(self):
        # 有波动的基线：正常 3σ 判据，z 为有方向的偏离倍数
        values = [10.0 + (i % 3) * 0.5 for i in range(40)] + [80.0] + [10.0] * 5
        points, desc = detect_anomalies(_series(values))
        self.assertGreaterEqual(desc['anomalies'], 1)
        flagged = [p for p in points if p.get('a') and p['z'] is not None]
        self.assertTrue(flagged)
        self.assertGreater(flagged[0]['z'], 0)  # 正向尖峰 z > 0

    def test_window_warmup_not_judged(self):
        # 前 window 个点只有热身统计，不允许判异常
        points, _ = detect_anomalies(_series([50.0] * 10 + [5.0]), window=20)
        self.assertFalse(any(p.get('a') for p in points))

    def test_none_value_treated_as_zero(self):
        points, desc = detect_anomalies(_series([10.0] * 30 + [None] * 5))
        self.assertEqual(len(points), 35)
        self.assertIn('anomalies', desc)


class LinearForecastTests(TestCase):
    def test_insufficient_samples(self):
        desc, future = linear_forecast(_series([1.0, 2.0, 3.0]))
        self.assertEqual(future, [])
        self.assertIn('不足', desc['note'])

    def test_upward_trend_detected(self):
        values = [float(i) for i in range(0, 60)]  # 严格线性上升
        desc, future = linear_forecast(_series(values))
        self.assertEqual(desc['trend'], '上升')
        self.assertGreater(desc['r2'], 0.99)
        self.assertTrue(future)
        # 预测值应继续沿斜率抬升
        self.assertGreater(future[-1]['v'], values[-1])

    def test_future_points_capped_at_zero(self):
        values = [float(-i) for i in range(20)]
        _, future = linear_forecast(_series(values))
        for p in future:
            self.assertGreaterEqual(p['v'], 0)


class CorrelationTests(TestCase):
    def test_perfect_correlation(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        r = pearson(xs, [2.0 * v for v in xs])
        self.assertEqual(r, 1.0)

    def test_inverse_correlation(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        r = pearson(xs, [6.0 - v for v in xs])
        self.assertEqual(r, -1.0)

    def test_degenerate_series_returns_none(self):
        self.assertIsNone(pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))  # 方差为 0
        self.assertIsNone(pearson([1.0], [1.0]))  # 样本不足
        self.assertIsNone(pearson([1, 2, 3], [1, 2]))  # 长度不等

    def test_matrix_diagonal_is_one(self):
        s = _series([1.0, 2.0, 3.0, 4.0])
        keys, mat = correlation_matrix({'a': s, 'b': s})
        self.assertEqual(keys, ['a', 'b'])
        self.assertEqual(mat[0][0], 1.0)
        self.assertEqual(mat[0][1], 1.0)  # 自身与自身完全相关


class LogMiningTests(TestCase):
    def _entry(self, message, level='ERROR', logger='app'):
        from loghub.models import LogEntry
        return LogEntry(level=level, logger=logger, message=message)

    def test_normalize_collapses_variables(self):
        self.assertEqual(normalize_log('user 42 login from 10.1.2.3'),
                         'user <N> login from <IP>')
        self.assertEqual(normalize_log('timeout for "order-9f2c1a"'), 'timeout for "S"')

    def test_same_pattern_merged(self):
        entries = [
            self._entry('order 1 failed'),
            self._entry('order 999 failed'),
            self._entry('order 1 failed'),
        ]
        out = mine_log_patterns(entries)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['n'], 3)
        self.assertEqual(out[0]['levels'], {'ERROR': 3})

    def test_top_n_sorted_by_count(self):
        entries = [self._entry(f'a {i}') for i in range(5)] + \
                  [self._entry('b 1')] + [self._entry('b 2')]
        out = mine_log_patterns(entries)
        self.assertEqual(out[0]['pattern'], 'a <N>')
        self.assertEqual(out[0]['n'], 5)
