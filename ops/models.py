"""
ops/models.py — 运维中心全部存储模型

拨测（黑盒监控）/ 故障事件 / 巡检报告 / SLO 错误预算 /
资产台账 / 自愈动作 / 操作审计 / 通知渠道配置
"""
from django.db import models
from django.utils import timezone


class ProbeTask(models.Model):
    """拨测任务：从平台视角定期请求一个 URL，验证可用性 / 延迟 / 证书"""

    name = models.CharField('任务名', max_length=80)
    url = models.CharField('拨测地址', max_length=300)
    method = models.CharField('方法', max_length=10, default='GET')
    headers = models.JSONField('请求头(JSON)', default=dict, blank=True,
                               help_text='如接入令牌用 {"X-OBS-Token": "..."} 放在请求头，避免拼进 URL 泄露')
    expect_status = models.IntegerField('期望状态码', default=200)
    keyword = models.CharField('页面须包含关键字(可选)', max_length=100, blank=True, default='')
    timeout_sec = models.IntegerField('超时(秒)', default=10)
    interval_sec = models.IntegerField('频率(秒)', default=60)
    enabled = models.BooleanField('启用', default=True)
    # 最近一次结果缓存（列表页直接显示）
    last_ok = models.BooleanField('最近成功', default=False)
    last_status = models.IntegerField('最近状态码', default=0)
    last_ms = models.FloatField('最近耗时(ms)', default=0.0)
    last_error = models.CharField('最近错误', max_length=200, blank=True, default='')
    last_cert_days = models.IntegerField('证书剩余天数', null=True, blank=True)
    consecutive_fails = models.IntegerField('连续失败次数', default=0)
    next_run_at = models.DateTimeField('下次执行', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '拨测任务'
        verbose_name_plural = verbose_name
        ordering = ['next_run_at', 'id']

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.method and self.method.upper() not in ('GET', 'HEAD', 'POST', 'PUT', 'OPTIONS'):
            raise ValidationError({'method': '不支持的请求方法'})

    def save(self, *args, **kwargs):
        self.method = (self.method or 'GET').upper()
        if self.method not in ('GET', 'HEAD', 'POST', 'PUT', 'OPTIONS'):
            self.method = 'GET'
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} ({self.url})'


class ProbeResult(models.Model):
    task = models.ForeignKey(ProbeTask, on_delete=models.CASCADE, related_name='results',
                             verbose_name='拨测任务')
    ok = models.BooleanField('是否成功')
    status_code = models.IntegerField('状态码', default=0)
    duration_ms = models.FloatField('耗时(ms)', default=0.0)
    error = models.CharField('错误', max_length=200, blank=True, default='')
    cert_days = models.IntegerField('证书剩余天数', null=True, blank=True)
    created_at = models.DateTimeField('时间', default=timezone.now, db_index=True)

    class Meta:
        verbose_name = '拨测结果'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['task', '-created_at'], name='idx_probe_task_time'),
        ]


class Incident(models.Model):
    """故障事件：时间相近/相关的告警自动聚合为一次故障"""

    STATUS = [('open', '处理中'), ('resolved', '已恢复'), ('closed', '已关闭')]

    title = models.CharField('标题', max_length=150)
    status = models.CharField('状态', max_length=10, choices=STATUS, default='open')
    level = models.CharField('最高级别', max_length=4, default='P2')
    started_at = models.DateTimeField('开始时间', default=timezone.now, db_index=True)
    resolved_at = models.DateTimeField('恢复时间', null=True, blank=True)
    services = models.CharField('涉及服务', max_length=200, blank=True, default='')
    root_cause = models.TextField('根因分析', blank=True, default='')
    lessons = models.TextField('改进措施', blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '故障事件'
        verbose_name_plural = verbose_name
        ordering = ['-started_at']

    @property
    def duration_min(self):
        end = self.resolved_at or timezone.now()
        return round((end - self.started_at).total_seconds() / 60, 1)


class InspectionRun(models.Model):
    """一次巡检报告：检查清单 + 评分 + 容量预测"""

    TRIGGERS = [('manual', '手动'), ('scheduled', '定时')]

    trigger = models.CharField('触发方式', max_length=10, choices=TRIGGERS, default='manual')
    score = models.IntegerField('健康评分', default=100)
    results = models.TextField('检查结果(JSON)', default='[]')
    created_at = models.DateTimeField('时间', default=timezone.now, db_index=True)

    class Meta:
        verbose_name = '巡检报告'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']


class SLO(models.Model):
    """服务等级目标：可用性 + 延迟目标，自动计算错误预算"""

    name = models.CharField('SLO 名称', max_length=80)
    target_availability = models.FloatField('可用性目标(%)', default=99.9)
    target_p95_ms = models.FloatField('P95 耗时目标(ms)', default=500)
    window_days = models.IntegerField('统计窗口(天)', default=30)
    enabled = models.BooleanField('启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = 'SLO'
        verbose_name_plural = verbose_name
        ordering = ['id']

    def __str__(self):
        return f'{self.name}（可用性 {self.target_availability}% / P95 {self.target_p95_ms}ms）'


class Asset(models.Model):
    """资产台账：主机由采集/Agent 自动登记，可补维护负责人与环境"""

    KINDS = [('主机', '主机'), ('应用', '应用'), ('数据库', '数据库'), ('中间件', '中间件'), ('其他', '其他')]
    ENVS = [('生产', '生产'), ('测试', '测试'), ('开发', '开发'), ('未分类', '未分类')]

    hostname = models.CharField('主机名/标识', max_length=128, db_index=True)
    label = models.CharField('名称', max_length=80, blank=True, default='')
    kind = models.CharField('类型', max_length=10, choices=KINDS, default='主机')
    env = models.CharField('环境', max_length=10, choices=ENVS, default='未分类')
    owner = models.CharField('负责人', max_length=40, blank=True, default='')
    notes = models.CharField('备注', max_length=200, blank=True, default='')
    auto = models.BooleanField('自动登记', default=True)
    last_seen_at = models.DateTimeField('最近上线', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '资产'
        verbose_name_plural = verbose_name
        ordering = ['env', 'hostname']

    def __str__(self):
        return f'{self.hostname}（{self.env}·{self.owner or "未分配"}）'

    @classmethod
    def auto_register(cls, hostname):
        """主机采集/Agent 上报时自动登记（幂等）"""
        if not hostname:
            return
        obj, created = cls.objects.get_or_create(
            hostname=hostname,
            defaults={'auto': True, 'kind': '主机', 'last_seen_at': timezone.now()},
        )
        if not created:
            cls.objects.filter(pk=obj.pk).update(last_seen_at=timezone.now())
        return obj


class HealAction(models.Model):
    """自愈动作：某告警策略触发时自动执行的白名单处置"""

    TYPES = [
        ('cleanup_tmp', '清理临时目录'),
        ('http_callback', 'HTTP 回调'),
        ('command', '自定义命令(谨慎)'),
    ]

    name = models.CharField('动作名称', max_length=80)
    policy = models.ForeignKey('alerts.AlertPolicy', on_delete=models.CASCADE,
                               related_name='heal_actions', verbose_name='触发策略', null=True, blank=True)
    action_type = models.CharField('类型', max_length=20, choices=TYPES, default='cleanup_tmp')
    param = models.CharField('参数(目录/URL/命令)', max_length=300, blank=True, default='')
    enabled = models.BooleanField('启用', default=False)
    cooldown_min = models.IntegerField('冷却(分钟)', default=30)
    last_run_at = models.DateTimeField('最近执行', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = '自愈动作'
        verbose_name_plural = verbose_name
        ordering = ['id']


class HealRun(models.Model):
    action = models.ForeignKey(HealAction, on_delete=models.CASCADE, related_name='runs',
                               verbose_name='动作')
    ok = models.BooleanField('是否成功', default=True)
    output = models.TextField('输出', blank=True, default='')
    created_at = models.DateTimeField('时间', default=timezone.now, db_index=True)

    class Meta:
        verbose_name = '自愈执行记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']


class AuditLog(models.Model):
    """操作审计：平台内的关键变更与登录行为"""

    user = models.CharField('用户', max_length=60, blank=True, default='')
    action = models.CharField('动作', max_length=60)
    target = models.CharField('对象', max_length=150, blank=True, default='')
    detail = models.TextField('详情', blank=True, default='')
    ip = models.CharField('IP', max_length=64, blank=True, default='')
    created_at = models.DateTimeField('时间', default=timezone.now, db_index=True)

    class Meta:
        verbose_name = '审计日志'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']


class NotifyConfig(models.Model):
    """通知渠道配置（单例）：站内信始终可用，其余按需启用

    smtp_pass 落库为 Fernet 密文（见 ops/crypto.py），读取时解密；
    页面不回显密码，数据库泄露时也不会直接暴露授权码。
    """

    enabled = models.BooleanField('启用外部通知', default=False)
    smtp_host = models.CharField('SMTP 服务器', max_length=120, blank=True, default='')
    smtp_port = models.IntegerField('SMTP 端口', default=465)
    smtp_ssl = models.BooleanField('SMTP 使用 SSL', default=True)
    smtp_user = models.CharField('SMTP 用户', max_length=120, blank=True, default='')
    smtp_pass = models.CharField('SMTP 密码/授权码(加密存储)', max_length=300, blank=True, default='')
    mail_from = models.CharField('发件人', max_length=120, blank=True, default='')
    mail_to = models.CharField('收件人(逗号分隔)', max_length=300, blank=True, default='')
    webhook_generic = models.CharField('通用 Webhook URL', max_length=300, blank=True, default='')
    wecom_webhook = models.CharField('企业微信 Webhook', max_length=300, blank=True, default='')
    ding_webhook = models.CharField('钉钉 Webhook', max_length=300, blank=True, default='')

    class Meta:
        verbose_name = '通知渠道配置'
        verbose_name_plural = verbose_name

    def save(self, *args, **kwargs):
        from .crypto import encrypt
        if self.smtp_pass and not self.smtp_pass.startswith('enc:v1:'):
            self.smtp_pass = encrypt(self.smtp_pass)
        super().save(*args, **kwargs)

    def smtp_password(self):
        from .crypto import decrypt
        return decrypt(self.smtp_pass)

    @classmethod
    def load(cls):
        # get_or_create 避免告警线程与请求线程并发首次调用时产生重复单例
        obj = cls.objects.first()
        if obj:
            return obj
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj
