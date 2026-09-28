"""
alerts/models.py — 告警中心的存储模型
"""
from django.db import models
from django.utils import timezone


class AlertPolicy(models.Model):
    """告警策略：对某个注册表指标设定阈值条件

    metric_key 见 monitor/registry.py；custom.<name> 亦可告警。
    评估窗口默认 5 分钟（取窗口内均值与阈值比较），由 alerts.engine 周期执行。
    """

    OPERATORS = [('>', '大于'), ('<', '小于')]
    LEVELS = [
        ('P0', 'P0 紧急'),
        ('P1', 'P1 严重'),
        ('P2', 'P2 警告'),
        ('提示', '提示'),
    ]

    name = models.CharField('策略名称', max_length=80)
    metric_key = models.CharField('监控指标', max_length=64, db_index=True)
    operator = models.CharField('比较符', max_length=2, choices=OPERATORS, default='>')
    threshold = models.FloatField('阈值')
    level = models.CharField('告警级别', max_length=4, choices=LEVELS, default='P2')
    enabled = models.BooleanField('启用', default=True)
    note = models.CharField('备注', max_length=200, blank=True, default='')
    # 持续越限 N 分钟才触发（for-duration，防抖动误报）；0 表示立即触发
    for_minutes = models.FloatField('持续N分钟才触发', default=0)
    # 恢复迟滞（hysteresis）：配置后，指标回到"恢复阈值"的安全侧才算恢复，
    # 避免指标在阈值附近抖动导致事件反复触发/恢复；为空则条件解除即恢复
    resolve_threshold = models.FloatField('恢复阈值', null=True, blank=True)
    # 引擎工作字段：for-duration 观察期内首次越限时间（非用户输入）
    breach_since = models.DateTimeField('首次越限时间', null=True, blank=True)
    # 静默窗口：此时间之前评估引擎跳过该策略（发版/维护期间防轰炸）
    silenced_until = models.DateTimeField('静默至', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '告警策略'
        verbose_name_plural = verbose_name
        ordering = ['level', 'id']

    def __str__(self):
        return f'{self.name}（{self.metric_key} {self.operator} {self.threshold}）'

    def describe(self):
        op = ' > ' if self.operator == '>' else ' < '
        text = f'{self.metric_key}{op}{self.threshold}'
        if self.for_minutes:
            text += f' 持续{self.for_minutes:g}分钟'
        if self.resolve_threshold is not None:
            text += f' 恢复阈值{self.resolve_threshold:g}'
        return text


class AlertEvent(models.Model):
    """一次告警事件：触发(firing) -> 确认(ack) -> 恢复(resolved)；可关联故障单"""

    STATUS = [('firing', '触发中'), ('resolved', '已恢复')]

    policy = models.ForeignKey(AlertPolicy, on_delete=models.CASCADE, related_name='events',
                               verbose_name='所属策略')
    status = models.CharField('状态', max_length=10, choices=STATUS, default='firing')
    level = models.CharField('级别', max_length=4, default='P2')
    value = models.FloatField('触发时数值', default=0.0)
    summary = models.CharField('摘要', max_length=250, blank=True, default='')
    started_at = models.DateTimeField('触发时间', default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField('恢复时间', null=True, blank=True)
    last_value_at = models.DateTimeField('最近评估', null=True, blank=True)
    # ---- 处置闭环 ----
    ack_by = models.CharField('确认人', max_length=60, blank=True, default='')
    ack_at = models.DateTimeField('确认时间', null=True, blank=True)
    handle_note = models.TextField('处理备注', blank=True, default='')
    # ---- 故障事件关联（ops.Incident，用字符串引用避免循环依赖） ----
    incident = models.ForeignKey('ops.Incident', on_delete=models.SET_NULL,
                                 related_name='alerts', null=True, blank=True,
                                 verbose_name='所属故障单')

    class Meta:
        verbose_name = '告警事件'
        verbose_name_plural = verbose_name
        ordering = ['-started_at']

    def __str__(self):
        return f'[{self.status}] {self.summary}'

    @property
    def duration_min(self):
        end = self.resolved_at or timezone.now()
        return round((end - self.started_at).total_seconds() / 60, 1)

    @property
    def ack_delay_min(self):
        if not self.ack_at:
            return None
        return round((self.ack_at - self.started_at).total_seconds() / 60, 1)


class NotificationRecord(models.Model):
    """通知记录：告警触发/恢复时各生成一条（站内信；webhook 为模拟演示）"""

    CHANNELS = [('站内信', '站内信'), ('webhook', 'Webhook(模拟)'), ('邮件(模拟)', '邮件(模拟)')]
    KINDS = [('fire', '触发'), ('resolve', '恢复')]

    event = models.ForeignKey(AlertEvent, on_delete=models.CASCADE, related_name='notifications',
                              verbose_name='关联事件')
    channel = models.CharField('渠道', max_length=16, choices=CHANNELS, default='站内信')
    kind = models.CharField('类型', max_length=10, choices=KINDS, default='fire')
    title = models.CharField('标题', max_length=150)
    content = models.TextField('内容')
    created_at = models.DateTimeField('时间', auto_now_add=True)

    class Meta:
        verbose_name = '通知记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return self.title
