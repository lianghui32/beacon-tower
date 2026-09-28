"""
loghub/handler.py — 把 Python logging 体系接入日志服务

SQLiteLogHandler 挂到 root logger（WARNING 及以上），
Django / django.request 的异常与告警会自动进入日志库，无需业务代码改造。
"""
import json
import logging
import traceback

from monitor.tracing import current_trace_id

from .models import LogEntry

_ATTACHED = False


class SQLiteLogHandler(logging.Handler):
    """写入 LogEntry 的 logging.Handler（写库失败时静默，避免日志风暴拖垮请求）"""

    def emit(self, record):
        try:
            message = self.format(record)[:4000]
            extra = {}
            if record.exc_info:
                extra['exc'] = ''.join(traceback.format_exception(*record.exc_info))[:4000]
            LogEntry.objects.create(
                source=getattr(record, 'obs_source', None) or 'system',
                level=record.levelname,
                logger=record.name[:120],
                message=message,
                extra=json.dumps(extra, ensure_ascii=False) if extra else '',
                # 请求上下文中的日志自动携带链路 ID（后台线程/CLI 为空串）
                trace_id=current_trace_id()[:32],
            )
        except Exception:
            self.handleError(record)


def attach_log_handler():
    """挂载日志 Handler（幂等；WARNING 及以上入库）"""
    global _ATTACHED
    if _ATTACHED:
        return
    root = logging.getLogger()
    # 防重复挂载（跨 reload 的进程内存标记 + 实例标记双保险）
    for h in root.handlers:
        if isinstance(h, SQLiteLogHandler):
            _ATTACHED = True
            return
    handler = SQLiteLogHandler()
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))
    root.addHandler(handler)
    _ATTACHED = True
