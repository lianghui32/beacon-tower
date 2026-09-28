"""
loghub/tests.py — 日志服务测试

覆盖：SQLiteLogHandler 的链路 ID 关联、接入 API 的鉴权与 traceparent 解析、
日志搜索的 trace_id 过滤。
"""
import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from loghub.handler import SQLiteLogHandler
from loghub.models import LogEntry

TRACE_ID = '4bf92f3577b34da6a3ce929d0e0e4736'


class HandlerTraceCorrelationTests(TestCase):
    def _emit(self, msg='test warning'):
        import logging
        record = logging.LogRecord(
            name='test.logger', level=logging.WARNING, pathname=__file__,
            lineno=1, msg=msg, args=(), exc_info=None,
        )
        handler = SQLiteLogHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        handler.emit(record)

    def test_log_without_trace_has_empty_trace_id(self):
        self._emit()
        entry = LogEntry.objects.latest('id')
        self.assertEqual(entry.trace_id, '')

    def test_log_in_request_context_carries_trace_id(self):
        from monitor.tracing import _current_trace_id
        token = _current_trace_id.set(TRACE_ID)
        try:
            self._emit()
        finally:
            _current_trace_id.reset(token)
        entry = LogEntry.objects.latest('id')
        self.assertEqual(entry.trace_id, TRACE_ID)


class LogIngestTests(TestCase):
    def setUp(self):
        # 搜索 API 是登录门禁后的读接口：建一个普通账号登录后访问
        from django.contrib.auth.models import User
        self.user = User.objects.create_user(username='viewer', password='viewer-pass-9527')
        self.client.force_login(self.user)

    def _post(self, payload, headers=None, token=None):
        from django.conf import settings
        h = {'HTTP_X_OBS_TOKEN': token or settings.OBSERVABILITY['INGEST_TOKEN']}
        h.update(headers or {})
        return self.client.post('/logs/api/ingest/', data=json.dumps(payload),
                                content_type='application/json', **h)

    def test_ingest_with_traceparent_header(self):
        resp = self._post(
            {'level': 'ERROR', 'message': 'db connection lost', 'source': 'app'},
            headers={'HTTP_TRACEPARENT': f'00-{TRACE_ID}-00f067aa0ba902b7-01'},
        )
        self.assertEqual(resp.json()['accepted'], 1)
        self.assertTrue(LogEntry.objects.filter(trace_id=TRACE_ID).exists())

    def test_ingest_with_payload_trace_id(self):
        resp = self._post({'message': 'boom', 'trace_id': TRACE_ID})
        self.assertEqual(resp.json()['accepted'], 1)
        self.assertTrue(LogEntry.objects.filter(trace_id=TRACE_ID).exists())

    def test_invalid_traceparent_ignored(self):
        resp = self._post({'message': 'boom'},
                          headers={'HTTP_TRACEPARENT': 'garbage-value'})
        self.assertEqual(resp.json()['accepted'], 1)
        self.assertFalse(LogEntry.objects.exclude(trace_id='').exists())

    def test_trace_id_sanitized(self):
        # 非法字符的 trace_id 不得入库（防任意串污染索引字段）
        resp = self._post({'message': 'x', 'trace_id': '<script>alert(1)</script>'})
        self.assertEqual(resp.json()['accepted'], 1)
        self.assertTrue(all(e.trace_id == '' for e in LogEntry.objects.all()))

    def test_search_filters_by_trace_id(self):
        LogEntry.objects.create(level='ERROR', message='a', trace_id=TRACE_ID)
        LogEntry.objects.create(level='ERROR', message='b', trace_id='a' * 32)
        resp = self.client.get(f'/logs/api/search/?trace_id={TRACE_ID}')
        data = resp.json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['items'][0]['message'], 'a')

    def test_retention_window_applies(self):
        LogEntry.objects.create(level='INFO', message='old', trace_id=TRACE_ID,
                                created_at=timezone.now() - timedelta(days=2))
        resp = self.client.get(f'/logs/api/search/?trace_id={TRACE_ID}&minutes=60')
        self.assertEqual(resp.json()['total'], 0)
