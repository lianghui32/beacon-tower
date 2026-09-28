"""
monitor/tracing.py — W3C Trace Context 支持（跨服务链路关联）

让平台的 APM 从"单请求 TraceID"升级为可对接标准追踪生态的链路语义：

1. 上游服务（网关 / 其他业务服务 / 自研 Agent）按 W3C Trace Context 规范
   携带 `traceparent: 00-<trace-id>-<parent-span-id>-<flags>` 请求头访问平台时，
   中间件**采纳上游 trace_id**（同一调用链跨服务可串联）；
2. 无上游链路时生成符合规范的 128bit trace_id（旧数据 64bit TraceID 仍可查询）；
3. 响应回写 `traceresponse`（W3C 规范草案）与 `X-Trace-Id`，供调用方与排障使用；
4. 请求处理期间通过 contextvars 暴露当前 trace_id：
   - 日志 Handler 把 trace_id 写入 LogEntry，实现"日志 ↔ 调用链"按 ID 互查；
   - 后台线程 / shell 中 context 为空，日志不携带 trace 前缀，互不污染。

纯标准库实现，无外部依赖；协议细节见
https://www.w3.org/TR/trace-context/
"""
import contextvars
import logging
import secrets

# W3C traceparent：version(2hex)-trace-id(32hex)-parent-span-id(16hex)-flags(2hex)
# version 0xff 非法；trace-id / span-id 全零非法（规范 §3.2 / §3.3）
_TRACEPARENT_LEN = 55
_HEX = set('0123456789abcdef')

_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar('obs_trace_id', default='')


def _is_hex(value, n):
    return len(value) == n and all(c in _HEX for c in value)


def new_trace_id():
    """生成符合 W3C 规范的 128bit trace_id（32 个小写 hex，非全零）"""
    tid = secrets.token_hex(16)
    # 全零 id 非法（W3C §3.2）；概率可忽略但归一处理
    return tid if tid.strip('0') else new_trace_id()


def new_span_id():
    """生成 64bit span_id（16 个小写 hex，非全零）"""
    sid = secrets.token_hex(8)
    return sid if sid.strip('0') else new_span_id()


def parse_traceparent(value):
    """解析并校验 traceparent 头。

    合法返回 {'trace_id':…, 'span_id':…, 'flags':…}；任何不规范输入返回 None
    （规范要求：无法解析时不得采信，也不得因此拒绝请求）。
    """
    if not value or len(value) != _TRACEPARENT_LEN:
        return None
    parts = value.split('-')
    if len(parts) != 4:
        return None
    version, trace_id, span_id, flags = parts
    if not _is_hex(version, 2) or version == 'ff':
        return None
    if not _is_hex(trace_id, 32) or trace_id.strip('0') == '':
        return None
    if not _is_hex(span_id, 16) or span_id.strip('0') == '':
        return None
    if not _is_hex(flags, 2):
        return None
    return {'trace_id': trace_id, 'span_id': span_id, 'flags': flags}


def format_traceparent(trace_id, span_id, sampled=True):
    return f'00-{trace_id}-{span_id}-{"01" if sampled else "00"}'


def bind_request(request):
    """从请求中提取/生成 trace 上下文，并绑定到当前执行上下文。

    返回 trace_id（32 hex）。写的是 contextvars 而不是 threading.local——
    async 视图（ASGI）下同样正确。
    """
    ctx = parse_traceparent(request.headers.get('traceparent', ''))
    trace_id = ctx['trace_id'] if ctx else new_trace_id()
    request.obs_trace_id = trace_id
    request.obs_parent_span_id = ctx['span_id'] if ctx else ''
    _current_trace_id.set(trace_id)
    return trace_id


def unbind_request():
    """请求结束后清除绑定，避免同线程的下一个请求/延迟任务读到过期 trace"""
    _current_trace_id.set('')


def current_trace_id():
    """当前执行上下文关联的 trace_id（无则空串）"""
    return _current_trace_id.get()


class TraceLogFilter(logging.Filter):
    """给日志记录附加 obs_trace_id 属性（当前请求有链路时才有值）"""

    def filter(self, record):
        record.obs_trace_id = current_trace_id()
        return True


class TraceFormatter(logging.Formatter):
    """控制台日志格式：请求上下文中的日志追加 [trace=...] 后缀，便于本地排障"""

    def format(self, record):
        text = super().format(record)
        trace_id = getattr(record, 'obs_trace_id', '')
        if trace_id:
            text = f'{text} [trace={trace_id}]'
        return text
