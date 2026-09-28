"""
cleaner/services.py — 磁盘清理与系统加速服务（运维级、可审计）

清理项（全部白名单化，绝不触碰业务目录）：
- system_temp     系统临时目录中超过 24 小时的文件
- pycache         项目内全部 __pycache__ 字节码缓存（可随时重建，删除无风险）
- pip_cache       pip 下载缓存（纯缓存，删除仅影响下次装包速度）
- platform_data   平台自身过期采集数据（按保留期清理后执行 SQLite VACUUM，
                  真正回收磁盘空间——删除行不缩小文件，必须 VACUUM）

内存加速（Windows）：对当前用户进程调用 EmptyWorkingSet 修剪工作集
（系统经典"内存整理"原理，把不活跃页换出到待命列表），只处理当前用户
自己的进程，跳过系统进程与平台自身。

安全设计：
- 所有删除先"扫描预估"再执行；文件数/时长双上限，超限中止并报告；
- 跳过符号链接、只读、无法访问的条目；逐条 try/except 不中断；
- 每次执行写 CleanupRun 留痕（调用方另写 AuditLog）。
"""
import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == 'win32'

# 单项扫描/清理的边界
SCAN_DEADLINE_SEC = 10.0      # 单项预估扫描时限
MAX_WALK_FILES = 3000         # 单项遍历文件数上限
MAX_CLEAN_FILES = 800         # 单次清理删除文件数上限
TEMP_MIN_AGE_SEC = 24 * 3600  # 临时文件至少 24h 未修改才清理

ITEM_LABELS = {
    'system_temp': '系统临时文件',
    'pycache': 'Python 字节码缓存',
    'pip_cache': 'pip 下载缓存',
    'platform_data': '平台过期采集数据',
}


def _is_reparse(path):
    """符号链接或 Windows Junction（挂载点）都返回 True。

    注意 os.path.islink 不识别 Junction——junction 可把遍历/删除引到白名单根之外，
    必须一并剪枝（os.path.isjunction 为 Python 3.12+，缺失时退化为仅 islink）。
    """
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, 'isjunction', None)
        if isjunction and isjunction(path):
            return True
        # 兜底：文件/目录带 REPARSE_POINT 属性一律跳过
        st = os.lstat(path)
        return bool(getattr(st, 'st_file_attributes', 0) & 0x400)
    except OSError:
        return True  # 判定失败按危险处理（fail-closed）


def _walk_bounded(root, deadline=None, max_files=MAX_WALK_FILES):
    """有界遍历：剪枝链接/Junction，限时限量（目录层与文件层都检查），逐条容错。
    yield (path, stat)"""
    deadline = deadline or (time.monotonic() + SCAN_DEADLINE_SEC)
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        # 目录层也必须检查时限：否则"无文件的 Junction 环"会让 os.walk 永不产出
        if time.monotonic() > deadline:
            return
        dirnames[:] = [d for d in dirnames
                       if not _is_reparse(os.path.join(dirpath, d))]
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                if _is_reparse(p):
                    continue
                yield p, os.stat(p, follow_symlinks=False)
                count += 1
            except OSError:
                continue
            if count >= max_files or time.monotonic() > deadline:
                return


# ---------------------------------------------------------------------------
# 各清理项：预估（不删）与执行
# ---------------------------------------------------------------------------

def _temp_root():
    import tempfile
    return Path(tempfile.gettempdir())


def _estimate_system_temp():
    root, deadline = _temp_root(), time.monotonic() + SCAN_DEADLINE_SEC
    total, files, cutoff = 0, 0, time.time() - TEMP_MIN_AGE_SEC
    for _p, st in _walk_bounded(root, deadline):
        if st.st_mtime < cutoff:
            total += st.st_size
            files += 1
    return {'bytes': total, 'files': files,
            'detail': f'{root}（>24h 未修改）'}


def _clean_system_temp():
    root, deadline = _temp_root(), time.monotonic() + SCAN_DEADLINE_SEC
    freed, files, errors, cutoff = 0, 0, 0, time.time() - TEMP_MIN_AGE_SEC
    for p, st in _walk_bounded(root, deadline, max_files=MAX_CLEAN_FILES):
        if st.st_mtime >= cutoff:
            continue
        try:
            size = st.st_size
            os.unlink(p)
            freed += size
            files += 1
        except OSError:
            errors += 1
    # 顺手删掉清空后的空目录（一层，失败忽略）
    try:
        for d in root.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
    return {'freed_bytes': freed, 'files': files,
            'detail': f'清理 {root}，失败 {errors} 项'}


def _pycache_dirs():
    base = Path(settings.BASE_DIR)
    dirs, deadline = [], time.monotonic() + SCAN_DEADLINE_SEC
    for dirpath, dirnames, _ in os.walk(base, topdown=True):
        keep = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if _is_reparse(full):
                continue
            if d == '__pycache__':
                dirs.append(Path(full))
            else:
                keep.append(d)
        dirnames[:] = keep
        if time.monotonic() > deadline:
            break
    return dirs


def _estimate_pycache():
    dirs = _pycache_dirs()
    total, files, deadline = 0, 0, time.monotonic() + SCAN_DEADLINE_SEC
    for d in dirs:
        for _p, st in _walk_bounded(d, deadline, max_files=200):
            total += st.st_size
            files += 1
    return {'bytes': total, 'files': files, 'detail': f'{len(dirs)} 个 __pycache__ 目录'}


def _clean_pycache():
    dirs = _pycache_dirs()
    freed, files = 0, 0
    for d in dirs:
        for _p, st in _walk_bounded(d, max_files=500):
            try:
                freed += st.st_size
                files += 1
            except OSError:
                continue
        shutil.rmtree(d, ignore_errors=True)
    return {'freed_bytes': freed, 'files': files, 'detail': f'删除 {len(dirs)} 个 __pycache__ 目录'}


def _pip_cache_dir():
    try:
        out = subprocess.run(
            [sys.executable, '-m', 'pip', 'cache', 'dir'],
            capture_output=True, text=True, timeout=15,
        )
        d = out.stdout.strip()
        return Path(d) if out.returncode == 0 and d and os.path.isdir(d) else None
    except Exception:
        return None


def _estimate_pip_cache():
    d = _pip_cache_dir()
    if not d:
        return {'bytes': 0, 'files': 0, 'detail': '未找到 pip 缓存目录'}
    total, files = 0, 0
    deadline = time.monotonic() + SCAN_DEADLINE_SEC
    for _p, st in _walk_bounded(d, deadline):
        total += st.st_size
        files += 1
    return {'bytes': total, 'files': files, 'detail': str(d)}


def _clean_pip_cache():
    d = _pip_cache_dir()
    if not d:
        return {'freed_bytes': 0, 'files': 0, 'detail': '未找到 pip 缓存目录'}
    freed, files = 0, 0
    for p, st in _walk_bounded(d, max_files=MAX_CLEAN_FILES):
        try:
            freed += st.st_size
            files += 1
            os.unlink(p)
        except OSError:
            continue
    return {'freed_bytes': freed, 'files': files, 'detail': f'清理 {d}'}


def _db_files_size():
    base = Path(settings.BASE_DIR)
    return sum(f.stat().st_size for f in
               (base / 'db.sqlite3', base / 'db.sqlite3-wal', base / 'db.sqlite3-shm')
               if f.exists())


def _estimate_platform_data():
    from datetime import timedelta


    from hosts.models import HostMetric
    from loghub.models import LogEntry
    from monitor.models import CustomMetric, RequestMetric
    from rum.models import RumEvent

    deadline = timezone.now() - timedelta(days=settings.OBSERVABILITY['RETENTION_DAYS'])
    rows = sum(m.objects.filter(created_at__lt=deadline).count()
               for m in (RequestMetric, HostMetric, RumEvent, LogEntry, CustomMetric))
    db_mb = _db_files_size() / 1024 / 1024
    return {'bytes': 0, 'files': rows,
            'detail': f'{rows} 行过期数据（保留期 {settings.OBSERVABILITY["RETENTION_DAYS"]} 天）；'
                      f'数据库文件 {db_mb:.1f} MB（VACUUM 后回收）'}


def _clean_platform_data():
    """清理过期采集数据 + VACUUM：以数据库文件前后大小差为真实释放量"""
    from alerts.engine import prune_old_data

    from django.db import connection

    before = _db_files_size()
    t0 = time.perf_counter()
    prune_old_data()
    with connection.cursor() as cur:
        cur.execute('PRAGMA wal_checkpoint(TRUNCATE);')
        cur.execute('VACUUM;')
    duration = int((time.perf_counter() - t0) * 1000)
    after = _db_files_size()
    return {'freed_bytes': max(0, before - after), 'files': 0,
            'detail': f'数据库文件 {before / 1024 / 1024:.1f} MB -> '
                      f'{after / 1024 / 1024:.1f} MB（VACUUM 耗时 {duration}ms）'}


_ESTIMATORS = {
    'system_temp': _estimate_system_temp,
    'pycache': _estimate_pycache,
    'pip_cache': _estimate_pip_cache,
    'platform_data': _estimate_platform_data,
}
_CLEANERS = {
    'system_temp': _clean_system_temp,
    'pycache': _clean_pycache,
    'pip_cache': _clean_pip_cache,
    'platform_data': _clean_platform_data,
}


def estimate_item(key):
    """扫描预估某个清理项可回收的空间（不做任何删除）"""
    fn = _ESTIMATORS.get(key)
    if not fn:
        return None
    try:
        return fn()
    except Exception:
        logger.exception('清理项预估失败: %s', key)
        return None


def clean_item(key):
    """执行一个清理项，返回 (结果dict, 耗时ms)"""
    fn = _CLEANERS.get(key)
    if not fn:
        return None, 0
    t0 = time.perf_counter()
    result = fn()
    duration = int((time.perf_counter() - t0) * 1000)
    return result, duration


# ---------------------------------------------------------------------------
# 磁盘空间分析（预设范围 + 有界扫描）
# ---------------------------------------------------------------------------

def disk_scopes():
    """可分析的预设范围"""
    drive = os.path.abspath(os.sep)
    home = os.path.expanduser('~')
    return [
        {'key': 'temp', 'label': '系统临时目录', 'path': str(_temp_root())},
        {'key': 'project', 'label': '项目目录', 'path': str(settings.BASE_DIR)},
        {'key': 'home', 'label': '用户目录', 'path': home},
        {'key': 'drive', 'label': f'系统盘 {drive}', 'path': drive},
    ]


def disk_scan(scope='temp', custom_path=''):
    """分析目录占用：返回 top 目录与 top 大文件（有界：限时 25s / 深度 4）。

    磁盘扫描属于只读操作，但整盘可能耗时，故限时并提示用户。
    """
    if custom_path:
        root = Path(custom_path)
        if not root.is_dir():
            return {'error': f'目录不存在: {custom_path}'}
        root = root.resolve()
    else:
        root = Path(dict((s['key'], s['path']) for s in disk_scopes()).get(scope, _temp_root()))

    deadline = time.monotonic() + 25.0
    dir_sizes = {}   # 深度<=4 的目录 -> (size, files)
    top_files = []   # (size, path)
    scanned = 0

    def _dir_depth(path):
        try:
            return str(path).rstrip(os.sep).count(os.sep) - str(root).rstrip(os.sep).count(os.sep)
        except Exception:
            return 99

    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        depth = _dir_depth(dirpath)
        dirnames[:] = [d for d in dirnames
                       if not _is_reparse(os.path.join(dirpath, d))
                       and depth < 4]
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                if _is_reparse(p):
                    continue
                st = os.stat(p, follow_symlinks=False)
            except OSError:
                continue
            scanned += 1
            size = st.st_size
            # 汇总到各级父目录（限深度）
            cur = Path(dirpath)
            for _ in range(depth + 1):
                if cur == root:
                    break
                a = dir_sizes.setdefault(str(cur), [0, 0])
                a[0] += size
                a[1] += 1
                cur = cur.parent
            if len(top_files) < 400 or size > top_files[-1][0]:
                top_files.append((size, p))
                top_files.sort(reverse=True)
                del top_files[400:]
            if time.monotonic() > deadline:
                break
        if time.monotonic() > deadline:
            break

    top_dirs = sorted(dir_sizes.items(), key=lambda x: -x[1][0])[:20]
    drive_free = None
    try:
        usage = shutil.disk_usage(str(root))
        drive_free = {'free': usage.free, 'total': usage.total}
    except OSError:
        pass
    return {
        'root': str(root),
        'truncated': time.monotonic() > deadline,
        'scanned_files': scanned,
        'drive': drive_free,
        'top_dirs': [{'path': p, 'size': v[0], 'files': v[1]} for p, v in top_dirs],
        'top_files': [{'path': p, 'size': s} for s, p in top_files[:20]],
    }


# ---------------------------------------------------------------------------
# 内存加速（Windows 工作集修剪）
# ---------------------------------------------------------------------------

def memory_overview():
    """内存水位 + 占用最高的进程（供加速面板展示）"""
    try:
        import psutil
    except ImportError:
        return {'available': False, 'detail': '未安装 psutil'}
    vm = psutil.virtual_memory()
    procs = []
    me = os.getpid()
    for p in psutil.process_iter(['pid', 'name', 'username', 'memory_info']):
        try:
            i = p.info
            rss = i['memory_info'].rss if i['memory_info'] else 0
            user = i['username'] or ''
            procs.append({
                'pid': i['pid'], 'name': (i['name'] or '?')[:40],
                'rss': rss, 'user': user[:32],
                'system': i['pid'] < 100 or me == i['pid'] or _is_system_user(user),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: -x['rss'])
    return {
        'available': True,
        'total': vm.total, 'used': vm.used, 'percent': vm.percent,
        'available_bytes': vm.available,
        'top': procs[:15],
    }


def _is_system_user(username):
    u = (username or '').lower()
    return ('system' in u or 'local service' in u or 'network service' in u
            or u.endswith('$') or 'trustedinstaller' in u)


def trim_memory():
    """内存整理：对当前用户的进程修剪工作集（EmptyWorkingSet）。

    仅处理当前用户自己的进程；跳过系统进程、平台自身、PID<100；
    返回修剪前后工作集总量差（即"释放"量级，页面已标注为估算值）。
    非 Windows 返回提示。
    """
    try:
        import psutil
    except ImportError:
        return {'ok': False, 'detail': '未安装 psutil，无法执行内存整理'}

    if not IS_WINDOWS:
        return {'ok': False, 'detail': '内存整理当前仅支持 Windows（类 Unix 系统由内核自主管理页缓存）'}

    class PROC_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_uint32), ('PageFaultCount', ctypes.c_uint32),
                    ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t),
                    ('PeakPagefileUsage', ctypes.c_size_t)]

    psapi = ctypes.WinDLL('Psapi.dll')
    kernel32 = ctypes.WinDLL('kernel32.dll')
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_INFORMATION = 0x0400
    me = os.getpid()
    before = after = 0
    trimmed = skipped = 0

    for p in psutil.process_iter(['pid', 'username', 'memory_info']):
        try:
            info = p.info
            if info['pid'] < 100 or info['pid'] == me:
                continue
            if _is_system_user(info['username'] or ''):
                continue
            rss = info['memory_info'].rss if info['memory_info'] else 0
            if rss < 30 * 1024 * 1024:  # 小于 30MB 的进程没修剪价值
                continue
            handle = kernel32.OpenProcess(
                PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, info['pid'])
            if not handle:
                skipped += 1
                continue
            try:
                pmc = PROC_MEMORY_COUNTERS()
                pmc.cb = ctypes.sizeof(PROC_MEMORY_COUNTERS)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                    before += pmc.WorkingSetSize
                    if psapi.EmptyWorkingSet(handle):
                        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                            after += pmc.WorkingSetSize
                            trimmed += 1
            finally:
                kernel32.CloseHandle(handle)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            skipped += 1

    freed = max(0, before - after)
    return {
        'ok': True,
        'freed_bytes': freed,
        'trimmed': trimmed,
        'skipped': skipped,
        'before': before,
        'after': after,
        'detail': f'修剪 {trimmed} 个进程工作集，工作集总量 '
                  f'{before / 1024 / 1024:.0f} MB -> {after / 1024 / 1024:.0f} MB'
                  '（估算值：被修剪页仍可能被进程再次触碰换回）',
    }
