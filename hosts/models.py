"""
hosts/models.py — 主机资源指标（由 hosts.collector 后台线程周期写入）
"""
from django.db import models
from django.utils import timezone


class HostMetric(models.Model):
    """一次主机采样：CPU / 内存 / 磁盘 / 网络 / 进程与连接数"""

    hostname = models.CharField('主机名', max_length=128, default='local')
    cpu_percent = models.FloatField('CPU 使用率(%)')
    cpu_cores = models.IntegerField('逻辑核心数', default=0)
    load_avg = models.FloatField('负载(1min)', default=0.0)
    mem_percent = models.FloatField('内存使用率(%)')
    mem_used_mb = models.FloatField('已用内存(MB)', default=0.0)
    mem_total_mb = models.FloatField('总内存(MB)', default=0.0)
    disk_percent = models.FloatField('系统盘使用率(%)', default=0.0)
    disk_used_gb = models.FloatField('系统盘已用(GB)', default=0.0)
    disk_total_gb = models.FloatField('系统盘总量(GB)', default=0.0)
    net_sent_kbps = models.FloatField('上行速率(KB/s)', default=0.0)
    net_recv_kbps = models.FloatField('下行速率(KB/s)', default=0.0)
    proc_count = models.IntegerField('进程数', default=0)
    tcp_conns = models.IntegerField('TCP 连接数', default=0)
    simulated = models.BooleanField('是否模拟数据', default=False)
    created_at = models.DateTimeField('采集时间', default=timezone.now, db_index=True)

    class Meta:
        verbose_name = '主机指标'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.hostname} cpu={self.cpu_percent}% @{self.created_at:%H:%M:%S}'
