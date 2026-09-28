"""
cleaner/models.py — 清理加速中心的存储模型
"""
from django.db import models
from django.utils import timezone


class CleanupRun(models.Model):
    """一次清理/加速动作的执行留痕"""

    ITEMS = [
        ('system_temp', '系统临时文件'),
        ('pycache', 'Python 字节码缓存'),
        ('pip_cache', 'pip 下载缓存'),
        ('platform_data', '平台过期采集数据'),
        ('memory', '内存整理'),
    ]

    item = models.CharField('清理项', max_length=32, choices=ITEMS)
    ok = models.BooleanField('是否成功', default=True)
    freed_bytes = models.BigIntegerField('释放空间(字节)', default=0)
    files = models.IntegerField('处理文件数', default=0)
    detail = models.TextField('明细', blank=True, default='')
    duration_ms = models.IntegerField('耗时(ms)', default=0)
    created_at = models.DateTimeField('时间', default=timezone.now, db_index=True)

    class Meta:
        verbose_name = '清理记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_item_display()} 释放 {self.freed_bytes / 1024 / 1024:.1f} MB'
