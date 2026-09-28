"""
monitor/middleware.py — 自研请求计时中间件（APM 数据采集核心）

职责：
1. 统计每个请求的总耗时，生成本次请求的 TraceID；
2. 通过 force_debug_cursor 让 Django 记录本请求内的全部 SQL（不依赖 DEBUG 开关），
   并把每条 SQL 的时间切片组装成"调用链 span"（请求 span + SQL span + 视图/渲染 span）；
3. 筛出超过阈值的慢查询，连同耗时、查询数一起交给批量缓冲（monitor/buffer.py）落库；
4. 捕获错误请求（状态码 >= 400 / 视图抛异常），供错误率统计与告警；
5. 生成一个"CPU 占用"推算指标，供面板折线图演示（真实主机 CPU 见主机监控）。

注意：跳过平台自身路径（settings.OBSERVABILITY.SKIP_PATHS），避免面板请求污染采集数据。
"""
import json
import logging
import random
import re
import time

from django.conf import settings
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.db import connection
from django.http import Http404

from . import buffer, geoip, tracing

MON = settings.OBSERVABILITY
SLOW_QUERY_MS = MON['SLOW_QUERY_MS']
SKIP_PATHS = MON['SKIP_PATHS']
# 只有确认部署在可信反向代理之后才设 OBS_TRUST_XFORWARDED_FOR=1，
# 否则客户端可伪造 XFF 头污染 IP 统计与审计
TRUST_XFF = __import__('os').environ.get('OBS_TRUST_XFORWARDED_FOR') == '1'

logger = logging.getLogger(__name__)

# SQL 文本中的敏感字面量脱敏：password='xxx' / secret_key: xxx / token LIKE 'sk-%' 等。
# 关键词允许带常见后缀（password_hash / api_key_id）并原样保留后缀（避免 author_id
# 这类普通列名被改写——不收录裸 auth/pass 等易误伤词，改用 auth_token/authorization）。
_SENSITIVE_SQL_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|credential|api[_-]?key|authorization|auth[_-]?token)"
    r"([a-z0-9_-]*)"
    r"(\s*(?:=|!=|<>|like|ilike|in)\s*|\s*:\s*)"
    r"('[^']*'|\"[^\"]*\"|\([^)]*\)|[^\s,)]+)"
)


def _sanitize_sql(sql):
    """脱敏 SQL 中的敏感字面量（落库展示前调用，避免凭据进入监控数据）；
    列名后缀原样保留：password_hash='x' -> password_hash='***'"""
    return _SENSITIVE_SQL_RE.sub(r"\1\2\3'***'", sql)


def _client_ip(request):
    """取客户端 IP：默认只信 REMOTE_ADDR；仅在可信反代后开启 OBS_TRUST_XFORWARDED_FOR"""
    if TRUST_XFF:
        fwd = request.META.get('HTTP_X_FORWARDED_FOR')
        if fwd:
            return fwd.split(',')[0].strip()[:60]
    return (request.META.get('REMOTE_ADDR') or '')[:60]


def _status_for_exception(exc):
    """把 Django 信号异常映射为真实状态码，避免 404/403 被计成 500 污染错误率"""
    if isinstance(exc, Http404):
        return 404
    if isinstance(exc, PermissionDenied):
        return 403
    if isinstance(exc, SuspiciousOperation):
        return 400
    return 500


class RequestTimingMiddleware:
    """记录每个请求的耗时 / SQL 查询数 / 慢查询 / 调用链 span / TraceID"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 平台自身路径不采集
        if request.path.startswith(SKIP_PATHS):
            return self.get_response(request)

        start = time.perf_counter()
        # W3C Trace Context：采纳上游 trace_id（跨服务串联）或生成新的；
        # 同时绑定到 contextvars，请求内的日志会自动携带 trace_id
        trace_id = tracing.bind_request(request)
        # 打开 SQL 调试游标：无论 DEBUG 与否都会记录 SQL 到 connection.queries
        connection.force_debug_cursor = True
        try:
            response = self.get_response(request)
            status = getattr(response, 'status_code', 0)
        except Exception as exc:  # 视图抛异常：记录一条指标后原样抛出
            elapsed_ms = (time.perf_counter() - start) * 1000
            self._save(request, _status_for_exception(exc), elapsed_ms,
                       error=type(exc).__name__,  # 只留异常类名，不落异常消息（可能含敏感上下文）
                       queries=list(connection.queries),
                       trace_id=trace_id)
            raise
        finally:
            queries = list(connection.queries)
            # 关闭调试游标，避免后续内部请求（如本次落库）也被记录
            connection.force_debug_cursor = False
            tracing.unbind_request()

        elapsed_ms = (time.perf_counter() - start) * 1000
        # 调用方可凭响应头拿到本次链路 ID：X-Trace-Id 便于人工排障，
        # traceresponse 为 W3C Trace Context 规范草案的响应头形式
        response['X-Trace-Id'] = trace_id
        response['traceresponse'] = tracing.format_traceparent(
            trace_id, tracing.new_span_id())
        self._save(request, status, elapsed_ms, queries=queries,
                   trace_id=trace_id)
        return response

    def _save(self, request, status, elapsed_ms, error=None, queries=None,
              trace_id=''):
        queries = queries or []
        sql_total_ms = 0.0
        slow = []
        spans = []
        offset = 0.0
        for q in queries:
            t_ms = float(q.get('time') or 0) * 1000
            sql_total_ms += t_ms
            sql_text = ' '.join((q.get('sql') or '').split())[:120]
            spans.append({'kind': 'sql', 'name': _sanitize_sql(sql_text),
                          'off': round(offset, 1), 'dur': round(t_ms, 2)})
            offset += t_ms
            if t_ms > SLOW_QUERY_MS:
                slow.append({'sql': _sanitize_sql(q['sql'])[:500], 'time_ms': round(t_ms, 1)})

        # 视图与渲染 span：总耗时减去 SQL 时间（近似切分，页面已有标注）
        view_ms = max(0.0, elapsed_ms - sql_total_ms)
        spans.insert(0, {'kind': 'request', 'name': f'{request.method} {request.path}',
                         'off': 0.0, 'dur': round(elapsed_ms, 2)})
        spans.append({'kind': 'view', 'name': (getattr(request.resolver_match, 'view_name', '') or '')[:80],
                      'off': round(sql_total_ms, 1), 'dur': round(view_ms, 2)})

        # 模拟 CPU 占用：由耗时与查询数推算（仅演示用，真实主机 CPU 见主机监控）
        cpu = min(95.0, elapsed_ms / 25 + len(queries) * 0.6 + random.uniform(2, 12))

        # 访客地域解析（带进程内缓存；真实库 geoip2/qqwry 可插拔，见 monitor/geoip.py）
        ip = _client_ip(request)
        province, city, _src = geoip.resolve(ip)

        # TraceID：上游有 traceparent 时采纳上游 trace_id（跨服务同链路），
        # 否则为本次请求新生成的 W3C 128bit id；trace_id 兜底为空时用绑定值
        trace_id = trace_id or getattr(request, 'obs_trace_id', '') or tracing.new_trace_id()
        try:
            # 交给批量缓冲（monitor/buffer.py）：请求线程不碰数据库写锁，
            # 后台线程按批 bulk_create。缓冲关闭时 submit 内部退化为同步写库。
            buffer.submit({
                'path': request.path[:200],
                'method': request.method,
                'status_code': status,
                'duration_ms': round(elapsed_ms, 2),
                'sql_count': len(queries),
                'sql_time_ms': round(sql_total_ms, 2),
                'slow_queries': json.dumps(slow, ensure_ascii=False),
                'slow_query_count': len(slow),
                'cpu_percent': round(cpu, 1),
                'trace_id': trace_id,
                'view_name': (getattr(request.resolver_match, 'view_name', '') or '')[:100],
                'is_error': (status >= 400) or (error is not None),
                'client_ip': ip,
                'geo_province': province,
                'geo_city': city,
                'spans': json.dumps(spans, ensure_ascii=False),
            })
        except Exception:
            # 采集失败不影响正常请求，但不能静默——平台自己就是监控工具
            logger.exception('RequestMetric 采集提交失败 path=%s', request.path)
