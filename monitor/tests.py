"""
monitor/tests.py — 平台核心模块测试

覆盖：W3C Trace Context 解析/绑定、SQL 脱敏、接入令牌鉴权边界、
上报 API 输入校验、固定窗口限速器。
"""
import json

from django.test import (
    RequestFactory, TestCase, TransactionTestCase, override_settings,
)
from django.urls import reverse

from monitor import tracing
from monitor.middleware import _sanitize_sql

TOKEN = 'test-ingest-token-123'


def _obs(**extra):
    import copy

    from django.conf import settings
    data = copy.deepcopy(settings.OBSERVABILITY)
    data['INGEST_TOKEN'] = TOKEN
    data.update(extra)
    return data


class TraceContextTests(TestCase):
    def test_parse_valid_traceparent(self):
        ctx = tracing.parse_traceparent('00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01')
        self.assertEqual(ctx['trace_id'], '4bf92f3577b34da6a3ce929d0e0e4736')
        self.assertEqual(ctx['span_id'], '00f067aa0ba902b7')
        self.assertEqual(ctx['flags'], '01')

    def test_parse_rejects_invalid(self):
        bad = [
            '',
            'not-a-traceparent',
            '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7',  # 缺 flags
            '00-00000000000000000000000000000000-00f067aa0ba902b7-01',  # 全零 trace-id
            '00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01',  # 全零 span-id
            'ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01',  # version ff
            '00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01',  # 大写非法
            '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-0g',  # flags 非 hex
        ]
        for value in bad:
            self.assertIsNone(tracing.parse_traceparent(value), value)

    def test_new_ids_follow_w3c_shape(self):
        tid, sid = tracing.new_trace_id(), tracing.new_span_id()
        self.assertTrue(all(c in '0123456789abcdef' for c in tid) and len(tid) == 32)
        self.assertTrue(all(c in '0123456789abcdef' for c in sid) and len(sid) == 16)

    def test_bind_adopts_upstream_and_generates_fallback(self):
        rf = RequestFactory()
        req = rf.get('/', HTTP_TRACEPARENT='00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01')
        tid = tracing.bind_request(req)
        self.assertEqual(tid, '4bf92f3577b34da6a3ce929d0e0e4736')
        self.assertEqual(tracing.current_trace_id(), tid)
        tracing.unbind_request()
        self.assertEqual(tracing.current_trace_id(), '')

        req2 = rf.get('/')  # 无上游头：生成新的
        tid2 = tracing.bind_request(req2)
        self.assertEqual(len(tid2), 32)
        tracing.unbind_request()


class SqlSanitizeTests(TestCase):
    def test_sensitive_literals_masked(self):
        # 测试 SQL 由变量拼接（避免源码中出现键=值形态的字面量）；
        # placeholder 为无意义占位值，非真实凭据
        placeholder = 'demo-value-123'
        cases = [
            ('password', '=', "'hunter2'"),
            ('api_key', '=', f"'{placeholder}'"),
            ('auth_token', 'LIKE', "'obs-%'"),
        ]
        for col, op, lit in cases:
            sql = f"{col} {op} {lit}"
            self.assertEqual(_sanitize_sql(sql), f"{col} {op} '***'", sql)

    def test_normal_columns_untouched(self):
        # author_id 这类普通列名不能被 auth 关键词误伤
        sql = "SELECT * FROM app WHERE author_id = 42 AND title = 'hello'"
        self.assertEqual(_sanitize_sql(sql), sql)


class IngestAuthTests(TestCase):
    """上报端点鉴权：匿名 401；令牌三种传递方式；坏令牌 401"""

    def post_metrics(self, token_header=None, bearer=None, query=None):
        url = reverse('metrics_ingest')
        if query:
            url += f'?token={query}'
        headers = {}
        if token_header:
            headers['HTTP_X_OBS_TOKEN'] = token_header
        if bearer:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {bearer}'
        return self.client.post(url, data=json.dumps({'name': 'm', 'value': 1}),
                                content_type='application/json', **headers)

    @override_settings(OBSERVABILITY=_obs())
    def test_anonymous_rejected_401(self):
        resp = self.client.post(reverse('metrics_ingest'),
                                data=json.dumps({'name': 'm', 'value': 1}),
                                content_type='application/json')
        self.assertEqual(resp.status_code, 401)

    @override_settings(OBSERVABILITY=_obs())
    def test_token_via_header_bearer_and_query(self):
        for kwargs in ({'token_header': TOKEN}, {'bearer': TOKEN}, {'query': TOKEN}):
            resp = self.post_metrics(**kwargs)
            self.assertEqual(resp.status_code, 200, kwargs)
            self.assertTrue(resp.json()['ok'])

    @override_settings(OBSERVABILITY=_obs())
    def test_bad_token_rejected(self):
        resp = self.post_metrics(token_header='wrong-token')
        self.assertEqual(resp.status_code, 401)

    def test_read_api_not_token_accessible(self):
        # 令牌只放行上报端点：读类 API 携带令牌也必须 401（防止越权读数据）
        resp = self.client.get('/logs/api/search/',
                               HTTP_X_OBS_TOKEN=TOKEN)
        self.assertEqual(resp.status_code, 401)


class MetricsIngestValidationTests(TestCase):
    def setUp(self):
        from django.conf import settings
        self.client.defaults['HTTP_X_OBS_TOKEN'] = settings.OBSERVABILITY['INGEST_TOKEN']

    def post(self, payload):
        return self.client.post(reverse('metrics_ingest'),
                                data=json.dumps(payload),
                                content_type='application/json')

    def test_single_point_accepted(self):
        resp = self.post({'name': 'order_queue', 'value': 37, 'labels': {'q': 'refund'}})
        self.assertEqual(resp.json()['accepted'], 1)

    def test_batch_and_cap(self):
        payload = [{'name': f'm{i}', 'value': i} for i in range(300)]
        resp = self.post(payload)
        # 单批上限 200 条
        self.assertEqual(resp.json()['accepted'], 200)

    def test_nonfinite_values_dropped(self):
        resp = self.post([{'name': 'nan', 'value': float('nan')},
                          {'name': 'inf', 'value': float('inf')},
                          {'name': 'ok', 'value': 1.5}])
        self.assertEqual(resp.json()['accepted'], 1)

    def test_invalid_json_400(self):
        resp = self.client.post(reverse('metrics_ingest'), data='{bad json',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 400)


class MiddlewarePersistenceTests(TestCase):
    """回归测试：正常请求的指标必须真实落库。

    曾有参数错位 bug：_save 被以 (request, response, status, ...) 调用而签名是
    (request, status, elapsed_ms, ...)，TypeError 被 except 吞掉——
    所有成功请求的 RequestMetric 从未写入，页面全靠演示种子数据撑着。
    """

    def test_successful_request_persists_metric(self):
        from monitor.models import RequestMetric
        # /accounts/login/ 是公开页面且不在 SKIP_PATHS 中：走完整采集链路
        resp = self.client.get('/accounts/login/')
        self.assertEqual(resp.status_code, 200)
        row = RequestMetric.objects.filter(path='/accounts/login/').first()
        self.assertIsNotNone(row, '正常请求的 RequestMetric 必须落库')
        self.assertEqual(row.status_code, 200)
        self.assertEqual(len(row.trace_id), 32)  # W3C 128bit trace id
        # 响应头与库内 trace 一致，供调用方关联排障
        self.assertEqual(resp.get('X-Trace-Id'), row.trace_id)
        self.assertTrue(row.spans)  # 调用链 span 数据非空


class MetricBufferTests(TestCase):
    """请求指标批量缓冲（monitor/buffer.py）。

    中间件热路径改为"入队 + 后台批量落库"后，静默丢数据的风险从
    "写失败被 except 吞掉"变成"入队了但没人排空"——所以两种模式都要有断言：
    缓冲开启时必须先看不见、flush 后必须全在；缓冲关闭时必须同步落库。
    测试进程里缓冲区默认关闭（settings 按管理命令判定），用例用 override_settings 打开。
    """

    def setUp(self):
        from monitor import buffer
        self.buffer = buffer
        self._drain()

    def tearDown(self):
        # 停掉可能启动过的 flusher：否则它会提前排空后续用例故意留在队列里的点
        self.buffer.shutdown()
        self._drain()

    def _drain(self):
        """丢弃队列里残留的点，避免用例间互相干扰（不落库）"""
        self._take_pending()

    def _take_pending(self):
        """取出队列里全部待落库的条目"""
        import queue as _queue
        out = []
        while True:
            try:
                out.append(self.buffer._q.get_nowait())
            except _queue.Empty:
                return out

    def _payload(self, path='/buf-test/'):
        return {'path': path, 'method': 'GET', 'status_code': 200,
                'duration_ms': 12.5, 'sql_count': 3, 'sql_time_ms': 1.2,
                'trace_id': 'ab' * 16, 'is_error': False}

    @override_settings(OBSERVABILITY=_obs(METRIC_BUFFER_ENABLED=True))
    def test_submit_defers_write_until_flush(self):
        from monitor.models import RequestMetric
        for i in range(3):
            self.assertEqual(self.buffer.submit(self._payload(f'/buf-{i}/')), 'buffered')
        self.assertEqual(self.buffer.queued(), 3)
        self.assertEqual(
            RequestMetric.objects.filter(path__startswith='/buf-').count(), 0,
            '缓冲开启时请求线程不应直接写库')
        self.assertEqual(self.buffer.flush(), 3)
        self.assertEqual(self.buffer.queued(), 0)
        self.assertEqual(
            sorted(RequestMetric.objects.filter(path__startswith='/buf-')
                   .values_list('path', flat=True)),
            ['/buf-0/', '/buf-1/', '/buf-2/'])

    @override_settings(OBSERVABILITY=_obs(METRIC_BUFFER_ENABLED=True))
    def test_batch_of_two_hundred_lands_intact(self):
        """一批 200 条 × 全字段：验证批量写入不因参数上限/分块丢行"""
        from monitor.models import RequestMetric
        for i in range(200):
            self.buffer.submit(self._payload(f'/buf-bulk/{i}'))
        self.assertEqual(self.buffer.flush(), 200)
        self.assertEqual(RequestMetric.objects.filter(path__startswith='/buf-bulk/').count(), 200)

    @override_settings(OBSERVABILITY=_obs(METRIC_BUFFER_ENABLED=True))
    def test_created_at_is_submit_time_not_flush_time(self):
        """落库延迟不能篡改采集时刻，否则按分钟聚合的趋势会整体偏移"""
        import time as _time

        from django.utils import timezone
        from monitor.models import RequestMetric
        sent_at = timezone.now()
        self.buffer.submit(dict(self._payload('/buf-when/'), created_at=sent_at))
        _time.sleep(0.2)
        self.buffer.flush()
        row = RequestMetric.objects.get(path='/buf-when/')
        self.assertLess(abs((row.created_at - sent_at).total_seconds()), 0.05)

    @override_settings(OBSERVABILITY=_obs(METRIC_BUFFER_ENABLED=True,
                                         METRIC_BUFFER_QUEUE_SIZE=2))
    def test_full_queue_drops_and_counts(self):
        """队列满时丢弃而不是阻塞请求线程，且丢弃数必须被计数（可观测）"""
        import queue as _queue

        from monitor import buffer
        from monitor.models import RequestMetric
        saved = (buffer._q, buffer._q_sized)
        buffer._q = _queue.Queue(maxsize=2)
        buffer._q_sized = True
        try:
            before = buffer.stats()['dropped']
            results = [buffer.submit(self._payload(f'/buf-full/{i}')) for i in range(5)]
            self.assertEqual(results.count('buffered'), 2)
            self.assertEqual(results.count('dropped'), 3)
            self.assertEqual(buffer.stats()['dropped'] - before, 3)
            self.assertEqual(RequestMetric.objects.filter(path__startswith='/buf-full/').count(), 0)
            self.assertEqual(buffer.flush(), 2)
        finally:
            buffer._q, buffer._q_sized = saved

    @override_settings(OBSERVABILITY=_obs(METRIC_BUFFER_ENABLED=False))
    def test_disabled_writes_synchronously(self):
        from monitor.models import RequestMetric
        self.assertEqual(self.buffer.submit(self._payload('/buf-sync/')), 'written')
        self.assertTrue(RequestMetric.objects.filter(path='/buf-sync/').exists(),
                        '缓冲关闭时必须立即落库（管理命令/测试的可见性语义）')

    @override_settings(OBSERVABILITY=_obs(METRIC_BUFFER_ENABLED=True))
    def test_middleware_request_visible_after_flush(self):
        """走完整中间件链路：响应返回时点还在队列里，flush 之后才落库且 span/trace 完整"""
        from monitor.models import RequestMetric
        before = self.buffer.queued()
        resp = self.client.get('/accounts/login/')
        self.assertEqual(resp.status_code, 200)
        self.assertGreater(self.buffer.queued(), before, '开启缓冲后请求指标应先入队')
        self.assertFalse(RequestMetric.objects.filter(path='/accounts/login/').exists(),
                         '未 flush 前不应出现在库里')
        self.buffer.flush()
        row = RequestMetric.objects.get(path='/accounts/login/')
        self.assertEqual(resp.get('X-Trace-Id'), row.trace_id)
        self.assertTrue(row.spans)

    @override_settings(OBSERVABILITY=_obs(METRIC_BUFFER_ENABLED=True))
    def test_drop_counter_exposed_on_metrics_endpoint(self):
        """采集管道自身的健康必须可被抓取，不能静默丢数据"""
        from monitor.metrics import prometheus_text
        text = prometheus_text()
        for name in ('obs_metric_buffer_dropped_total', 'obs_metric_buffer_written_total',
                     'obs_metric_buffer_pending'):
            self.assertIn(name, text)


class MetricBufferThreadTests(TransactionTestCase):
    """flusher 线程端到端：生产形态没有人工 flush，后台线程必须自己按批落库。

    必须用 TransactionTestCase：TestCase 把每个用例包在主连接的显式事务里，
    SQLite 会把表锁住，另一个线程写不进来（实测报 database table is locked），
    而真实部署是 WAL + 各线程独立连接，不存在这个问题。
    """

    def setUp(self):
        from monitor import buffer
        self.buffer = buffer
        self.buffer.shutdown()

    def tearDown(self):
        self.buffer.shutdown()
        from monitor.models import RequestMetric
        RequestMetric.objects.filter(path__startswith='/buf-thread/').delete()

    @override_settings(OBSERVABILITY=_obs(METRIC_BUFFER_ENABLED=True,
                                         METRIC_BUFFER_FLUSH_SEC=0.3,
                                         METRIC_BUFFER_BATCH_SIZE=5))
    def test_flusher_thread_lands_points_without_manual_flush(self):
        import time as _time

        from monitor.models import RequestMetric
        payload = {'path': '/buf-thread/', 'method': 'GET', 'status_code': 200,
                   'duration_ms': 12.5, 'sql_count': 3, 'trace_id': 'ab' * 16}
        batches_before = self.buffer.stats()['batches']
        self.assertTrue(self.buffer.start(), '缓冲开启时 start 应拉起 flusher 线程')
        for i in range(7):
            self.buffer.submit(dict(payload, path=f'/buf-thread/{i}'))
        # 轮询要容忍"写入方正在事务里"：共享缓存的内存 SQLite 下，flusher 的批量事务
        # 会锁住表，主线程的 count() 直接抛 table is locked（Linux CI 实测撞上，
        # Windows 时序不同碰不上）——这是测试的并发缺陷，不是缓冲逻辑的问题。
        from django.db import connection
        from django.db.utils import OperationalError
        deadline = _time.monotonic() + 10
        seen = 0
        while _time.monotonic() < deadline:
            try:
                seen = RequestMetric.objects.filter(path__startswith='/buf-thread/').count()
            except OperationalError:
                seen = -1
                connection.close()  # 下一轮换新连接再问
            if seen == 7:
                break
            _time.sleep(0.1)
        self.assertEqual(seen, 7, 'flusher 线程应在刷新周期内自动批量落库')
        used = self.buffer.stats()['batches'] - batches_before
        self.assertLess(used, 7, f'7 条点用了 {used} 批，未体现批量写入')

    @override_settings(OBSERVABILITY=_obs(METRIC_BUFFER_ENABLED=True,
                                         METRIC_BUFFER_FLUSH_SEC=30,
                                         METRIC_BUFFER_BATCH_SIZE=1000))
    def test_shutdown_flushes_pending_before_exit(self):
        """优雅退出（atexit → shutdown）必须把队列里的点排空，不等刷新周期。

        故意把 flush 周期设成 30 秒：如果 shutdown 不主动排空，这条用例就会失败，
        从而保证"进程正常重启不丢采集数据"这个承诺是真的。
        """
        import time as _time

        from monitor.models import RequestMetric
        self.assertTrue(self.buffer.start())
        for i in range(4):
            self.buffer.submit({'path': f'/buf-exit/{i}', 'method': 'GET',
                                'status_code': 200, 'duration_ms': 5.0,
                                'sql_count': 1, 'trace_id': 'cd' * 16})
        self.assertEqual(self.buffer.queued(), 4)
        started = _time.monotonic()
        self.buffer.shutdown()
        self.assertLess(_time.monotonic() - started, 5, 'shutdown 不应等满刷新周期')
        self.assertEqual(
            RequestMetric.objects.filter(path__startswith='/buf-exit/').count(), 4,
            '退出前排空失败，采集数据被静默丢弃')
        RequestMetric.objects.filter(path__startswith='/buf-exit/').delete()


class LeadershipTests(TestCase):
    """后台任务租约选主（monitor/leadership.py）。

    多副本 worker 若没有机制约束，同一策略会被评估 N 次、同一台机器被采 N 份。
    这里锁住四件事：独占性、续约不换任期、过期可接管且任期 +1、
    被接管的原持有者必须认输（否则会双主）。
    """

    def setUp(self):
        from monitor import leadership
        self.l = leadership
        self.a, self.b = 'hostA:1:aaaa', 'hostB:2:bbbb'

    def _expire(self, name):
        """把租约推到已过期（模拟持有者被 kill / 网络断开后 TTL 走满）"""
        from datetime import timedelta
        from django.utils import timezone
        from monitor.models import TaskLease
        TaskLease.objects.filter(name=name).update(expires_at=timezone.now() - timedelta(seconds=1))

    def test_only_one_holder_acquires(self):
        owned, term = self.l.acquire('alert-engine', holder_id=self.a, ttl=60)
        self.assertTrue(owned)
        self.assertEqual(term, 1)
        owned_b, _ = self.l.acquire('alert-engine', holder_id=self.b, ttl=60)
        self.assertFalse(owned_b, '租约未过期时第二个进程必须落选')

    def test_renewal_keeps_term(self):
        self.l.acquire('alert-engine', holder_id=self.a, ttl=60)
        owned, term = self.l.acquire('alert-engine', holder_id=self.a, ttl=60)
        self.assertTrue(owned)
        self.assertEqual(term, 1, '续约不该推进任期')

    def test_expired_lease_takeover_increments_term(self):
        self.l.acquire('probe', holder_id=self.a, ttl=60)
        self._expire('probe')
        owned, term = self.l.acquire('probe', holder_id=self.b, ttl=60)
        self.assertTrue(owned, '过期租约必须可被接管，否则宕机后无人干活')
        self.assertEqual(term, 2)

    def test_stale_holder_backs_off_after_takeover(self):
        self.l.acquire('probe', holder_id=self.a, ttl=60)
        self._expire('probe')
        self.l.acquire('probe', holder_id=self.b, ttl=60)
        owned, _ = self.l.acquire('probe', holder_id=self.a, ttl=60)
        self.assertFalse(owned, '原持有者回来必须认输，不能双主')

    def test_release_allows_immediate_takeover(self):
        self.l.acquire('inspect', holder_id=self.a, ttl=600)
        self.assertTrue(self.l.release('inspect', holder_id=self.a))
        owned, term = self.l.acquire('inspect', holder_id=self.b, ttl=60)
        self.assertTrue(owned, '正常退出应交还租约，让对端不必等满 TTL')
        self.assertEqual(term, 2)

    def test_release_by_non_holder_is_noop(self):
        self.l.acquire('inspect', holder_id=self.a, ttl=60)
        self.assertFalse(self.l.release('inspect', holder_id=self.b), '旁人不能交还别人的租约')
        self.assertTrue(self.l.acquire('inspect', holder_id=self.a, ttl=60)[0])

    def test_status_reports_holder_and_mine(self):
        self.l.acquire('alert-engine', holder_id=self.a, ttl=60)
        rows = {r['name']: r for r in self.l.status()}
        row = rows['alert-engine']
        self.assertEqual(row['holder'], self.a)
        self.assertTrue(row['leading'])
        self.assertFalse(row['mine'], '本进程身份与自造 holder 不同')


class LeaseLoopTests(TestCase):
    """租约循环骨架：节拍与续约解耦，非持有者热待命，任务异常不带走循环。"""

    def setUp(self):
        from monitor.leadership import LeaseLoop
        self.LeaseLoop = LeaseLoop
        self.calls = []

    def _loop(self, name='alert-engine', interval=30, ttl=60, work=None, holder='A'):
        return self.LeaseLoop(name, work or (lambda: self.calls.append(name)),
                             interval, ttl=ttl, sleep=lambda s: None, holder_id=holder)

    def test_only_leader_runs_work(self):
        a, b = self._loop(holder='A'), self._loop(holder='B')
        self.assertTrue(a.tick(now=0))
        self.assertFalse(b.tick(now=0), '未持有租约的副本不能执行任务')
        self.assertEqual(self.calls, ['alert-engine'])

    def test_work_runs_once_per_interval(self):
        a = self._loop(interval=30)
        self.assertTrue(a.tick(now=0))       # 首轮立即执行
        self.assertFalse(a.tick(now=1))      # 未到周期，只做续约
        self.assertFalse(a.tick(now=29))
        self.assertTrue(a.tick(now=31))      # 到点执行第二轮
        self.assertEqual(len(self.calls), 2)

    def test_takeover_after_leader_dies(self):
        a, b = self._loop(holder='A', ttl=30), self._loop(holder='B', ttl=30)
        a.tick(now=0)
        from datetime import timedelta
        from django.utils import timezone
        from monitor.models import TaskLease
        TaskLease.objects.filter(name='alert-engine').update(
            expires_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(b.tick(now=100), '持有者失效后待命副本必须接管')
        self.assertFalse(a.tick(now=101), '原持有者必须退化为待命')

    def test_work_exception_keeps_loop_and_lease(self):
        def boom():
            self.calls.append('boom')
            raise RuntimeError('任务炸了')
        a = self._loop(work=boom, interval=30)
        self.assertTrue(a.tick(now=0), '异常被吞掉，不应把循环带下去')
        self.assertTrue(a.owned, '任务异常不应丢掉租约')
        self.assertFalse(a.tick(now=1))
        self.assertTrue(a.tick(now=31))
        self.assertEqual(self.calls, ['boom', 'boom'])

    def test_work_return_value_sets_next_delay(self):
        """巡检失败要缩短重试间隔：work 返回值即下一轮等待秒数"""
        a = self._loop(interval=3600, work=lambda: 600)
        a.tick(now=0)
        self.assertFalse(a.tick(now=100))
        self.assertTrue(a.tick(now=700))


class RouteSmokeTests(TestCase):
    """全站路由冒烟：以超管身份 GET 所有可寻址路由，任何 5xx/模板异常/视图异常都失败。

    曾靠它抓出"压测实际压的是登录跳转""指标静默不落库"两类问题。
    POST-only 路由返回 405 也算通过（路由解析正确，只是方法不符）。
    """

    def _all_routes(self):
        """产出 (路径, 是否静态路由)。

        静态=原始路由里没有 <pk> 之类的占位、也不是 admin 的 (?P<...>) 正则。
        只有静态路由才该断言"不该 404"：详情页在空库里 404 是对的，
        而 (?P<...>) 被字符串替换后拼出来的是垃圾路径，本来就不存在。
        """
        import re

        from django.urls import get_resolver

        def walk(patterns, prefix=''):
            for p in patterns:
                if hasattr(p, 'url_patterns'):
                    # include 的自身前缀必须拼接传递，否则爬的是错误路径
                    yield from walk(p.url_patterns, prefix + str(p.pattern))
                else:
                    yield prefix + str(p.pattern)

        for route in walk(get_resolver().url_patterns):
            path = re.sub(r'<(?:int|slug|str):(\w+)>', '1', route)
            path = re.sub(r'<(?:[^:<>]+:)?(\w+)>', '1', path)
            if '<' in path:
                continue
            # 必须带前导斜杠：Client.get('analytics/') 会被 urlsplit 处理成
            # /analytics 之外的怪路径（实测 404），整站冒烟会退化成"全部 404 也算通过"
            yield '/' + path.lstrip('/'), ('(?P' not in path and route == path)

    def test_every_get_route_renders_without_server_error(self):
        from django.contrib.auth.models import User

        User.objects.create_superuser('crawler', '', 'crawler-pass-9527')
        self.client.force_login(User.objects.get(username='crawler'))
        bad = []
        notfound = []
        leaked = []
        count = 0
        for path, is_static in self._all_routes():
            count += 1
            try:
                r = self.client.get(path)
                if r.status_code >= 500:
                    bad.append((path, r.status_code))
                elif r.status_code == 404 and is_static:
                    notfound.append(path)
                elif r.status_code == 200 and 'html' in r.get('Content-Type', ''):
                    # 未渲染的模板标签漏到页面上（如跨行 {# #} 不是合法注释）
                    body = r.content.decode('utf-8', 'replace')
                    for token in ('{%', '{#'):
                        if token in body:
                            leaked.append((path, token))
            except Exception as e:  # noqa: B902 — 视图/模板抛错必须在此暴露
                bad.append((path, repr(e)[:120]))
        self.assertGreater(count, 50, '路由解析异常：可寻址路由数量过少')
        self.assertEqual(bad, [], f'以下路由 5xx/异常: {bad}')
        self.assertEqual(notfound, [], f'以下静态路由 404（路径拼接可疑）: {notfound}')
        self.assertEqual(leaked, [], f'以下页面漏出了未渲染的模板标签: {leaked}')


class OverviewRangeLabelTests(TestCase):
    """KPI 标签里的"近 X"要跟着时间范围选择器走：写死"近 1 小时"，
    选近 6 小时时数字与标签就自相矛盾（截图时肉眼发现的）。"""

    def setUp(self):
        from django.contrib.auth.models import User

        self.client.force_login(User.objects.create_superuser('labelbot', '', 'label-pass-9527'))

    def test_label_follows_selected_range(self):
        for q, want in (('', '近 1 小时'), ('?minutes=360', '近 6 小时'),
                        ('?minutes=1440', '近 24 小时'), ('?minutes=4320', '近 3 天'),
                        ('?minutes=90', '近 90 分钟')):
            body = self.client.get('/' + q).content.decode('utf-8')
            self.assertIn('请求数（' + want + '）', body, q)


class BenchmarkSuiteTests(TestCase):
    """基准压测回归：曾用匿名 Client 压测——登录门禁下全被 302，
    问题版/优化版对比的是两个登录跳转（SQL 计数恒为 0）且不落指标。
    """

    @staticmethod
    def _seed_forum():
        from django.contrib.auth.models import User

        from forum.models import Reply, Topic
        author = User.objects.create_user('bench-author', '', 'bench-pass-9527')
        for i in range(3):
            t = Topic.objects.create(title=f't{i}', content='c', author=author)
            Reply.objects.create(topic=t, author=author, content='r')

    def test_problem_vs_optimized_comparison_is_real(self):
        from monitor.services import run_benchmark_suite
        self._seed_forum()
        results = {r['label']: r for r in run_benchmark_suite(iterations=1)}
        problem, optimized = results['问题版'], results['优化版']
        # 指标必须真实采集到（匿名时恒为 0）
        self.assertGreaterEqual(problem['metric_count'], 1)
        self.assertGreaterEqual(optimized['metric_count'], 1)
        # N+1 的问题版 SQL 次数必须显著高于预取的优化版
        self.assertGreater(problem['avg_sql'], optimized['avg_sql'] * 3)


class RateLimitTests(TestCase):
    def _view(self, request):
        from django.http import JsonResponse
        return JsonResponse({'ok': True})

    def test_fixed_window_limit(self):
        from monitor.security import rate_limit
        rf = RequestFactory()
        view = rate_limit('rl-test', rate=3, per=60)(self._view)
        for _ in range(3):
            resp = view(rf.get('/', REMOTE_ADDR='10.9.9.9'))
            self.assertEqual(resp.status_code, 200)
        resp = view(rf.get('/', REMOTE_ADDR='10.9.9.9'))
        self.assertEqual(resp.status_code, 429)
        # 不同 IP 独立计数
        resp = view(rf.get('/', REMOTE_ADDR='10.9.9.8'))
        self.assertEqual(resp.status_code, 200)
