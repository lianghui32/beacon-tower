"""
forum/models.py — 演示论坛的数据模型

注意：这里刻意保留了几个"性能陷阱"设计（与真实 V2EX 求助场景一致）：
1. Topic.category 没有加 db_index —— 列表页按分类过滤时会全表扫描；
2. Topic.author / Reply.topic / Reply.author 都是普通外键 ——
   不使用 select_related 时每访问一次都会触发一条额外 SQL。
"""
from django.conf import settings
from django.db import models


class Topic(models.Model):
    """论坛帖子"""

    title = models.CharField('标题', max_length=200)
    content = models.TextField('正文')
    # 陷阱：未加索引的过滤字段
    category = models.CharField('版块', max_length=32, default='闲聊')
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='topics', verbose_name='作者',
    )
    views = models.PositiveIntegerField('浏览量', default=0)
    created_at = models.DateTimeField('发布时间', auto_now_add=True)

    class Meta:
        verbose_name = '帖子'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return self.title


class Reply(models.Model):
    """帖子回复"""

    topic = models.ForeignKey(
        Topic, on_delete=models.CASCADE, related_name='replies', verbose_name='所属帖子',
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='replies', verbose_name='回复人',
    )
    content = models.TextField('回复内容')
    created_at = models.DateTimeField('回复时间', auto_now_add=True)

    class Meta:
        verbose_name = '回复'
        verbose_name_plural = verbose_name
        ordering = ['created_at']

    def __str__(self):
        return f'回复@{self.topic_id}'
