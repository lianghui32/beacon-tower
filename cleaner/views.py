"""
cleaner/views.py — 清理加速中心页面与 AJAX 接口

权限：整个模块仅 staff 可访问（清理磁盘 / 结束类操作属于高危维护功能）。
所有执行动作写 CleanupRun 留痕 + AuditLog 审计。

并发与限速：清理/VACUUM/内存整理同一时刻只允许一个在跑（进程级互斥锁，
防止重复 VACUUM 长时间持 SQLite 写锁阻塞在线请求）；磁盘扫描等重接口
按 IP 限速，防并发扫描占满请求线程。
"""
import logging

from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from monitor.security import rate_limit

from ops.audit import audit

from . import services
from .models import CleanupRun

logger = logging.getLogger(__name__)

# 清理/整理互斥锁（180s 自动过期兜底，防止异常路径漏删导致永久锁死）
_BUSY_LOCK_KEY = 'obs-cleaner-busy'
_BUSY_LOCK_TTL = 180


def _acquire_busy_lock():
    if cache.add(_BUSY_LOCK_KEY, '1', _BUSY_LOCK_TTL):
        return True
    return False


def _release_busy_lock():
    cache.delete(_BUSY_LOCK_KEY)


def _fmt_mb(n):
    return round(n / 1024 / 1024, 1)


def _staff_required(view):
    """staff 专属视图：非 staff 跳转登录页"""
    from functools import wraps

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_staff:
            return redirect_to_login(request.get_full_path())
        return view(request, *args, **kwargs)
    return wrapped


@require_GET
@_staff_required
def cleaner_page(request):
    """清理加速主页：四类清理项预估 + 内存水位 + 最近执行记录"""
    items = []
    for key, label in services.ITEM_LABELS.items():
        est = services.estimate_item(key)
        items.append({
            'key': key,
            'label': label,
            'mb': _fmt_mb(est['bytes']) if est else '-',
            'files': est['files'] if est else '-',
            'detail': est['detail'] if est else '预估失败（查看服务日志）',
            'error': est is None,
        })
    mem = services.memory_overview()
    runs = CleanupRun.objects.all()[:20]
    return render(request, 'cleaner/clean.html', {
        'items': items,
        'mem': mem,
        'mem_total_mb': _fmt_mb(mem.get('total', 0)),
        'mem_used_mb': _fmt_mb(mem.get('used', 0)),
        'mem_avail_mb': _fmt_mb(mem.get('available_bytes', 0)),
        'runs': runs,
        'scopes': services.disk_scopes(),
        'is_windows': services.IS_WINDOWS,
    })


@require_GET
@_staff_required
def api_estimate(request, item):
    """重新扫描某个清理项的预估"""
    est = services.estimate_item(item)
    if est is None:
        return JsonResponse({'ok': False, 'error': '未知清理项或预估失败'}, status=400)
    return JsonResponse({'ok': True, 'item': item,
                         'mb': _fmt_mb(est['bytes']), 'files': est['files'],
                         'detail': est['detail']})


@require_POST
@_staff_required
@rate_limit('cleaner-clean', rate=10, per=60)
def api_clean(request, item):
    """执行一个清理项（前端有确认弹窗；服务端白名单 + 上限保护 + 互斥）"""
    if not _acquire_busy_lock():
        return JsonResponse({'ok': False, 'error': '上一次清理/整理仍在进行中，请稍候'}, status=429)
    try:
        result, duration = services.clean_item(item)
        if result is None:
            return JsonResponse({'ok': False, 'error': '未知清理项'}, status=400)
        run = CleanupRun.objects.create(
            item=item, ok=True,
            freed_bytes=result.get('freed_bytes', 0),
            files=result.get('files', 0),
            detail=result.get('detail', ''),
            duration_ms=duration,
        )
        audit(request, '执行清理', services.ITEM_LABELS.get(item, item),
              f'释放 {run.freed_bytes / 1024 / 1024:.1f} MB，耗时 {duration}ms')
        return JsonResponse({
            'ok': True, 'item': item,
            'freed_mb': _fmt_mb(run.freed_bytes), 'files': run.files,
            'detail': run.detail, 'duration_ms': duration,
        })
    finally:
        _release_busy_lock()


@require_GET
@_staff_required
@rate_limit('cleaner-disk', rate=6, per=60)
def api_disk_scan(request):
    """磁盘空间分析（只读，有界扫描）"""
    scope = request.GET.get('scope', 'temp')
    custom = (request.GET.get('path') or '').strip()
    result = services.disk_scan(scope=scope, custom_path=custom)
    if 'error' in result:
        return JsonResponse(result, status=400)
    for d in result['top_dirs']:
        d['size_mb'] = _fmt_mb(d['size'])
    for f in result['top_files']:
        f['size_mb'] = _fmt_mb(f['size'])
    if result.get('drive'):
        result['drive']['free_mb'] = _fmt_mb(result['drive']['free'])
        result['drive']['total_mb'] = _fmt_mb(result['drive']['total'])
    return JsonResponse(result)


@require_GET
@_staff_required
def api_processes(request):
    """内存占用 Top 进程（供加速面板）"""
    mem = services.memory_overview()
    if not mem.get('available'):
        return JsonResponse({'ok': False, 'error': mem.get('detail')}, status=400)
    for p in mem['top']:
        p['rss_mb'] = _fmt_mb(p['rss'])
    return JsonResponse({'ok': True,
                         'percent': mem['percent'],
                         'used_mb': _fmt_mb(mem['used']),
                         'total_mb': _fmt_mb(mem['total']),
                         'top': mem['top']})


@sensitive_post_parameters()
@require_POST
@_staff_required
@rate_limit('cleaner-trim', rate=4, per=60)
def api_trim_memory(request):
    """内存整理（Windows 工作集修剪；页面已标注为估算值）"""
    if not _acquire_busy_lock():
        return JsonResponse({'ok': False, 'error': '上一次清理/整理仍在进行中，请稍候'}, status=429)
    try:
        result = services.trim_memory()
        CleanupRun.objects.create(
            item='memory', ok=result.get('ok', False),
            freed_bytes=result.get('freed_bytes', 0),
            files=result.get('trimmed', 0),
            detail=result.get('detail', ''),
            duration_ms=0,
        )
        audit(request, '内存整理', 'trim_workingset',
              f'释放估算 {result.get("freed_bytes", 0) / 1024 / 1024:.0f} MB'
              f'，修剪 {result.get("trimmed", 0)} 个进程')
    finally:
        _release_busy_lock()
    result['freed_mb'] = _fmt_mb(result.get('freed_bytes', 0))
    return JsonResponse(result)
