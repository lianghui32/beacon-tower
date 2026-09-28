"""
monitor/models.py — 采集数据的存储模型
"""
from django.db import models
from django.utils import timezone

from .tracing import new_trace_id


def gen_trace_id():
    # W3C 128bit trace_id：与 traceparent 头同构，跨服务可直接串联
    return new_trace_id()


class RequestMetric(models.Model):
    """一次 HTTP 请求的性能指标（由 RequestTimingMiddleware 写入）

    同时也是一条 APM 调用链（Trace）：spans 字段保存
    [请求 span, SQL span...] 的耗时切片，trace_id 用于关联检索。
    """

    path = models.CharField('请求路径', max_length=200, db_index=True)
    method = models.CharField('HTTP 方法', max_length=10, default='GET')
    status_code = models.IntegerField('状态码', default=200)
    duration_ms = models.FloatField('总耗时(ms)')
    sql_count = models.IntegerField('SQL 查询次数', default=0)
    sql_time_ms = models.FloatField('SQL 累计耗时(ms)', default=0.0)
    # JSON 列表：[{sql, time_ms}, ...]，仅保存超过阈值的慢查询
    slow_queries = models.TextField('慢查询明细(JSON)', default='[]')
    slow_query_count = models.IntegerField('慢查询条数', default=0)
    # 模拟指标：由耗时 + 查询数推算，仅用于面板演示，不代表真实 CPU 读数
    cpu_percent = models.FloatField('CPU 占用(推算)', default=0.0)
    # ---- APM 调用链 ----
    trace_id = models.CharField('TraceID', max_length=32, default=gen_trace_id, db_index=True)
    view_name = models.CharField('视图名', max_length=100, blank=True, default='')
    is_error = models.BooleanField('是否错误请求', default=False, db_index=True)
    client_ip = models.CharField('客户端IP', max_length=64, blank=True, default='', db_index=True)
    # ---- 访客地域（采集时由 monitor.geoip 解析，见 /analytics/geo/）----
    geo_province = models.CharField('归属省份', max_length=20, blank=True, default='', db_index=True)
    geo_city = models.CharField('归属城市', max_length=20, blank=True, default='')
    # JSON 列表：[{kind, name, off, dur}, ...]（off=相对请求开始的偏移 ms）
    spans = models.TextField('调用链Span(JSON)', default='[]')
    created_at = models.DateTimeField('采集时间', default=timezone.now, db_index=True)

    class Meta:
        verbose_name = '请求指标'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['duration_ms'], name='idx_req_duration'),
            models.Index(fields=['path', 'created_at'], name='idx_req_path_time'),
            models.Index(fields=['slow_query_count'], name='idx_req_slowq'),
            models.Index(fields=['geo_province', 'created_at'], name='idx_req_geo_time'),
        ]

    def __str__(self):
        return f'{self.method} {self.path} {self.duration_ms}ms'


class CustomMetric(models.Model):
    """自定义上报指标（接入中心 OpenAPI 写入）：name + 标签 + 数值点"""

    name = models.CharField('指标名', max_length=64, db_index=True)
    labels = models.TextField('标签(JSON)', default='{}')
    value = models.FloatField('数值')
    created_at = models.DateTimeField('采集时间', db_index=True)

    class Meta:
        verbose_name = '自定义指标'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name}={self.value}'


class TaskLease(models.Model):
    """后台任务租约：多副本部署时保证同一任务全集群只有一个进程在跑。

    抢约/续约都是一条 compare-and-swap 条件更新（见 monitor/leadership.py），
    term 每次易主 +1，用于识别"我已经被人取代了"。
    """

    name = models.CharField('任务名', max_length=40, unique=True)
    holder = models.CharField('持有者', max_length=120, blank=True, default='')
    term = models.BigIntegerField('任期', default=0)
    acquired_at = models.DateTimeField('本次持有起点', null=True, blank=True)
    renewed_at = models.DateTimeField('最近续约', null=True, blank=True)
    expires_at = models.DateTimeField('租约到期', null=True, blank=True, db_index=True)

    class Meta:
        verbose_name = '任务租约'
        verbose_name_plural = verbose_name
        ordering = ['name']

    def __str__(self):
        return f'{self.name} -> {self.holder or "空闲"}'


class DashCard(models.Model):
    """自定义大盘卡片：选择一个注册表指标 key，按时间范围画图"""

    title = models.CharField('卡片标题', max_length=64)
    metric_key = models.CharField('指标Key', max_length=64)
    chart_type = models.CharField('图表类型', max_length=10, default='line',
                                  choices=[('line', '折线'), ('area', '面积'), ('bar', '柱状')])
    minutes = models.IntegerField('时间范围(分钟)', default=60)
    span = models.IntegerField('栅格宽度(1-3)', default=1)
    order = models.IntegerField('排序', default=0)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '大盘卡片'
        verbose_name_plural = verbose_name
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.title}({self.metric_key})'
