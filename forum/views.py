"""
forum/views.py — 演示目标应用的视图

提供"问题版 / 优化版"两个路由做同功能对比：
- /forum/problem/   故意埋了典型性能问题
- /forum/optimized/ 用 Django 标准手段修复后的问题

两个视图渲染同一个模板，页面底部会显示本次请求的 SQL 查询数与耗时，
方便直观对比（也可用 /benchmark/ 或 run_benchmark 命令批量压测）。
"""
import time

from django.db import connection
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from .models import Reply, Topic


def _request_debug(start):
    """返回本次请求的 SQL 统计信息（给模板底部展示用）"""
    return {
        'sql_count': len(connection.queries),
        'duration_ms': round((time.perf_counter() - start) * 1000, 1),
    }


# ---------------------------------------------------------------
# 问题版：V2EX 上那位楼主写的代码（还原度 100%）
# ---------------------------------------------------------------
def topic_list_problem(request):
    """帖子列表——问题版

    埋了 4 类典型问题：
    1. 无索引过滤：category 字段没有 db_index，且逐条在 Python 里过滤；
    2. N+1 查询：循环里逐条取作者、逐条 count 回复数；
    3. 重复计算：每条帖子都重新统计一遍"各版块帖子数"，结果却从未缓存；
    4. 无 limit 的 all()：把全表捞到内存后再切片。
    """
    start = time.perf_counter()

    topics = Topic.objects.all()          # 问题 4：全表加载，无 limit
    rows = []
    for t in topics:                      # 问题 2：循环体里都是查询
        if t.category != '闲聊':           # 问题 1：无索引字段过滤（还是 Python 层过滤）
            continue
        author_name = t.author.username   # 问题 2：每条触发一次作者查询（N+1）
        reply_count = Reply.objects.filter(topic=t).count()  # 问题 2：每条再 count 一次
        # 问题 3：重复计算——每条帖子都统计各版块帖子数，循环 100 次就算 100 遍
        stats = {
            c: Topic.objects.filter(category=c).count()
            for c in ('闲聊', '技术', '求助')
        }
        rows.append({
            'id': t.id,
            'title': t.title,
            'author': author_name,
            'category': t.category,
            'reply_count': reply_count,
            'views': t.views,
            'created_at': t.created_at,
            'stats': stats,               # 顺手把重复计算的结果塞进行数据
        })
    rows = rows[:20]                      # 捞了全表却只用前 20 条

    context = {
        'version': 'problem',
        'version_label': '问题版',
        'topics': rows,
        'debug': _request_debug(start),
    }
    return render(request, 'forum/topic_list.html', context)


# ---------------------------------------------------------------
# 优化版：同样的功能，标准 Django 优化手段
# ---------------------------------------------------------------
def topic_list_optimized(request):
    """帖子列表——优化版

    对应修复：
    1. 数据库层过滤（建议配合 Meta.indexes 为 category 加索引，见 README）；
    2. select_related 预取作者外键 + annotate 一次性统计回复数，消灭 N+1；
    3. 版块统计只算一次，并放在数据库聚合里完成；
    4. QuerySet 直接 [:20] 切片，SQL 带 LIMIT，不捞全表。
    """
    start = time.perf_counter()

    rows = list(
        Topic.objects
        .filter(category='闲聊')                     # 修复 1/4：数据库过滤
        .select_related('author')                    # 修复 2：JOIN 预取作者
        .annotate(reply_count=Count('replies'))      # 修复 2：聚合代替循环 count
        .values('id', 'title', 'category', 'views',
                'created_at', 'author__username', 'reply_count')[:20]  # 修复 4：LIMIT
    )
    # 修复 3：版块统计只算一次
    category_counts = dict(
        Topic.objects.values_list('category')
        .annotate(n=Count('id'))
        .values_list('category', 'n')
    )

    topics = [
        {
            'id': r['id'],
            'title': r['title'],
            'author': r['author__username'],
            'category': r['category'],
            'reply_count': r['reply_count'],
            'views': r['views'],
            'created_at': r['created_at'],
            'stats': category_counts,
        }
        for r in rows
    ]

    context = {
        'version': 'optimized',
        'version_label': '优化版',
        'topics': topics,
        'debug': _request_debug(start),
    }
    return render(request, 'forum/topic_list.html', context)


def topic_detail(request, pk):
    """帖子详情页：展示回复列表（顺手演示 reply 预取）"""
    start = time.perf_counter()
    topic = get_object_or_404(Topic.objects.select_related('author'), pk=pk)
    replies = topic.replies.select_related('author').all()
    context = {
        'topic': topic,
        'replies': replies,
        'debug': _request_debug(start),
    }
    return render(request, 'forum/topic_detail.html', context)


# ---------------------------------------------------------------
# 故障演练：给观测平台制造真实的慢请求 / 错误 / 日志数据
# ---------------------------------------------------------------
# 故障演练：给观测平台制造真实的慢请求 / 错误 / 日志数据
# 演练端点一律 POST（防爬虫/预取链接误触发）+ 简单节流（防脚本刷爆平台自身）
# ---------------------------------------------------------------

import threading as _threading  # noqa: E402  （模块尾部常量初始化后延迟导入）

_chaos_lock = _threading.Lock()
_chaos_last = 0.0
_CHAOS_MIN_INTERVAL = 15  # 两次演练的最小间隔（秒）


def _chaos_throttled():
    """节流检查：间隔未到返回 True"""
    global _chaos_last
    now = time.monotonic()
    with _chaos_lock:
        if now - _chaos_last < _CHAOS_MIN_INTERVAL:
            return True
        _chaos_last = now
        return False


def chaos_page(request):
    """故障演练控制台页面"""
    drills = [
        {'title': '慢请求', 'url': '/forum/chaos/slow/',
         'desc': '请求人为 sleep 1.2 秒——在 APM 看到耗时尖刺，触发"慢请求"告警。'},
        {'title': 'N+1 查询', 'url': '/forum/chaos/nplus1/',
         'desc': '30 条帖子逐条查作者与回复数——在数据库分析看 SQL 次数抬升。'},
        {'title': '未捕获异常（500）', 'url': '/forum/chaos/error/',
         'desc': '视图直接抛异常——错误率、ERROR 日志（django.request 自动落库）同时出现。'},
        {'title': '日志风暴', 'url': '/forum/chaos/log/',
         'desc': '批量写入 40 条 ERROR/WARNING——日志查询与日志模式挖掘立即出现聚类模板。'},
        {'title': 'CPU 空转', 'url': '/forum/chaos/cpu/',
         'desc': '纯计算 0.8 秒——主机监控的 CPU 曲线出现一个可见的抬升。'},
        {'title': '批量制造（前 4 项）', 'url': '/forum/chaos/all/',
         'desc': '依次触发慢请求、N+1、500 错误、日志风暴各一次，一按钮看全链路反应。'},
    ]
    return render(request, 'forum/chaos.html', {'drills': drills})


@require_POST
def chaos_slow(request):
    """演练 1：人为拖慢请求 1.2 秒（验证慢请求指标与告警）"""
    if _chaos_throttled():
        return JsonResponse({'chaos': 'slow', 'throttled': True}, status=429)
    return _do_slow()


def _do_slow():
    time.sleep(1.2)
    return JsonResponse({'chaos': 'slow', 'sleeped_ms': 1200})


@require_POST
def chaos_nplus1(request):
    """演练 2：现场制造 N+1 查询（30 条帖子逐条查询）"""
    if _chaos_throttled():
        return JsonResponse({'chaos': 'nplus1', 'throttled': True}, status=429)
    return _do_nplus1()


def _do_nplus1():
    rows = Topic.objects.all()[:30]
    n = 0
    for t in rows:
        _ = t.author.username
        _ = Reply.objects.filter(topic=t).count()
        n += 1
    return JsonResponse({'chaos': 'nplus1', 'rows': n})


@require_POST
def chaos_error(request):
    """演练 3：抛出未捕获异常（验证 500 错误捕获、错误日志、错误率告警）"""
    if _chaos_throttled():
        return JsonResponse({'chaos': 'error', 'throttled': True}, status=429)
    raise RuntimeError('故障演练：人为抛出的未捕获异常')


@require_POST
def chaos_log_storm(request):
    """演练 4：批量写入 ERROR/WARNING 日志（验证日志接入与日志告警）"""
    if _chaos_throttled():
        return JsonResponse({'chaos': 'log_storm', 'throttled': True}, status=429)
    return _do_log_storm()


def _do_log_storm():
    import logging
    logger = logging.getLogger('chaos')
    for i in range(30):
        logger.error('故障演练：订单 payment-%d 处理失败，退款队列积压 %d 条', i, i * 3)
        if i % 3 == 0:
            logger.warning('故障演练：缓存命中率下降至 %d%%', 40 + i)
    return JsonResponse({'chaos': 'log_storm', 'written': 40})


@require_POST
def chaos_cpu(request):
    """演练 5：CPU 空转 0.8 秒（验证主机 CPU 曲线抬升）"""
    if _chaos_throttled():
        return JsonResponse({'chaos': 'cpu', 'throttled': True}, status=429)
    end = time.perf_counter() + 0.8
    x = 0
    while time.perf_counter() < end:
        x = (x * 31 + 7) % 99991
    return JsonResponse({'chaos': 'cpu', 'result': x})


@require_POST
def chaos_all(request):
    """批量演练：依次触发前 4 项（错误那一项在内部"消化"掉，避免整页 500）"""
    if _chaos_throttled():
        return JsonResponse({'chaos': 'all', 'throttled': True}, status=429)
    _do_slow()
    _do_nplus1()
    _do_log_storm()
    # 模拟一次"被平台捕获的异常"：写一条同款 ERROR 日志
    import logging
    logging.getLogger('chaos').error('故障演练：模拟未捕获异常（批量演练中已内部捕获）')
    return JsonResponse({'chaos': 'all', 'done': ['slow', 'nplus1', 'log', 'error(simulated)']})
