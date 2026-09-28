"""
rum/models.py — 前端性能监控（RUM）事件存储

一个宽表存全部事件类型：pv / perf / api / error / resource / custom。
浏览器端自研 SDK（static/rum.js）采集，POST /rum/beacon/ 批量上报。
"""
from django.db import models
from django.utils import timezone


class RumEvent(models.Model):
    TYPES = [
        ('pv', '页面访问'),
        ('perf', '页面性能'),
        ('api', 'API调用'),
        ('error', 'JS错误'),
        ('resource', '静态资源'),
        ('custom', '自定义事件'),
    ]

    type = models.CharField('事件类型', max_length=16, choices=TYPES, db_index=True)
    app = models.CharField('应用标识', max_length=32, default='forum')
    page_url = models.CharField('页面URL', max_length=256, blank=True, default='')
    referrer = models.CharField('来源页', max_length=256, blank=True, default='')
    session_id = models.CharField('会话ID', max_length=64, blank=True, default='', db_index=True)
    device = models.CharField('浏览器/系统', max_length=80, blank=True, default='')
    screen = models.CharField('分辨率', max_length=20, blank=True, default='')

    # perf：各阶段耗时（毫秒，可空）
    ttfb_ms = models.FloatField('TTFB(ms)', null=True, blank=True)
    dom_ready_ms = models.FloatField('DOMReady(ms)', null=True, blank=True)
    load_ms = models.FloatField('完整加载(ms)', null=True, blank=True)
    fp_ms = models.FloatField('首次绘制FP(ms)', null=True, blank=True)
    fcp_ms = models.FloatField('首次内容绘制FCP(ms)', null=True, blank=True)
    lcp_ms = models.FloatField('最大内容绘制LCP(ms)', null=True, blank=True)

    # api：请求明细
    api_url = models.CharField('API地址', max_length=256, blank=True, default='')
    api_method = models.CharField('请求方法', max_length=10, blank=True, default='')
    api_status = models.IntegerField('状态码', null=True, blank=True)
    api_duration_ms = models.FloatField('API耗时(ms)', null=True, blank=True)
    api_ok = models.BooleanField('是否成功', default=True)

    # error：异常明细
    err_message = models.CharField('错误信息', max_length=300, blank=True, default='')
    err_stack = models.TextField('错误堆栈', blank=True, default='')

    # resource：资源明细
    r_type = models.CharField('资源类型', max_length=16, blank=True, default='')
    r_url = models.CharField('资源地址', max_length=256, blank=True, default='')
    r_duration_ms = models.FloatField('加载耗时(ms)', null=True, blank=True)
    r_size_kb = models.FloatField('传输大小(KB)', null=True, blank=True)

    # custom：自定义事件
    event_name = models.CharField('事件名', max_length=64, blank=True, default='')
    payload = models.TextField('负载(JSON)', blank=True, default='')

    created_at = models.DateTimeField('上报时间', default=timezone.now, db_index=True)

    class Meta:
        verbose_name = 'RUM事件'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.type}] {self.page_url or self.event_name or self.err_message}'
