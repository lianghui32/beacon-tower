"""
loghub/models.py — 日志服务的存储模型
"""
from django.db import models
from django.utils import timezone


class LogEntry(models.Model):
    """一条日志：来源可以是系统 Handler / 接入 API / 前端 RUM"""

    SOURCES = [
        ('system', '系统'),
        ('app', '应用'),
        ('api', '接入API'),
        ('rum', '前端RUM'),
    ]
    source = models.CharField('来源', max_length=16, choices=SOURCES, default='app', db_index=True)
    level = models.CharField('级别', max_length=10, db_index=True,
                             default='INFO')  # DEBUG/INFO/WARNING/ERROR/CRITICAL
    logger = models.CharField('记录器', max_length=120, blank=True, default='')
    message = models.TextField('内容')
    extra = models.TextField('附加信息(JSON)', blank=True, default='')
    # 请求上下文日志自动携带（monitor.tracing 绑定）；接入 API 可按 W3C traceparent 关联，
    # 实现"日志 ↔ APM 调用链"按同一 trace_id 互查
    trace_id = models.CharField('TraceID', max_length=32, blank=True, default='', db_index=True)
    created_at = models.DateTimeField('时间', default=timezone.now, db_index=True)

    class Meta:
        verbose_name = '日志'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.level}] {self.message[:40]}'
