"""
monitor/buffer.py — 请求指标批量缓冲（APM 热路径与写库解耦）

改造前：中间件在响应返回前同步 INSERT 一行 RequestMetric。单写者数据库
（SQLite）下所有请求线程排队抢同一把写锁、每条 INSERT 各自 fsync，
压测里表现为 p50 5ms 而 p99 800ms 的长尾。

改造后：请求线程只做一次 put_nowait（微秒级、不碰数据库），
后台线程按"攒满 N 条 或 到期 T 秒"用一条 bulk_create 批量落库——
写库次数从"每请求一次"降到"每批一次"，事务数（fsync 数）同样被摊薄。

代价与边界（都是有意选择，且可观测）：
- 指标可见延迟最多 FLUSH_SEC 秒（大盘/告警读的是库，不是实时总线）；
- 队列满时丢新点并计数：观测数据可以采样，采集不允许反压业务；
  丢弃数经 /metrics 的 obs_metric_buffer_dropped_total 暴露；
- 后台落库遇瞬时故障（单写者锁、连接抖动）先重试 2 次，仍失败才整批丢弃并计数；
- 进程被 SIGKILL 时缓冲区里未落库的点会丢（atexit 只覆盖正常退出）。

`OBSERVABILITY['METRIC_BUFFER_ENABLED']` 为假时 submit() 直接同步写库，
管理命令与测试因此仍看到"请求结束即落库"的语义。
"""
import atexit
import logging
import os
import queue
import threading
import time

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 200
DEFAULT_FLUSH_SEC = 1.0
DEFAULT_QUEUE_SIZE = 20000

# 后台线程落库遇到瞬时故障（单写者锁、连接抖动）时的重试预算：最多 1+2 次尝试
WRITE_RETRIES = 2
WRITE_RETRY_SEC = 0.05

# 队列里放的是构造 RequestMetric 的字段字典（请求线程不实例化模型，省一次开销）
_q = queue.Queue()
_q_sized = False

_stats = {'written': 0, 'dropped': 0, 'batches': 0, 'errors': 0}

# 可重入：start() 持锁期间会调用 _ensure_queue_size()
_lock = threading.RLock()
_flusher = None
_stop = threading.Event()
_last_drop_log = 0.0


def _cfg(key, default):
    return settings.OBSERVABILITY.get(key, default)


def _ensure_queue_size():
    """按配置确定队列容量（Queue 容量建好后不可改，故延迟到首次使用）"""
    global _q, _q_sized
    if _q_sized:
        return
    with _lock:
        if _q_sized:
            return
        size = int(_cfg('METRIC_BUFFER_QUEUE_SIZE', DEFAULT_QUEUE_SIZE))
        if size > 0:
            _q = queue.Queue(maxsize=size)
        _q_sized = True


def enabled():
    """缓冲区是否启用（关闭时 submit 退化为同步写库）"""
    return bool(_cfg('METRIC_BUFFER_ENABLED', False))


def queued():
    return _q.qsize()


def stats():
    """缓冲区自观测：落库/丢弃条数、批次数、失败批次数、当前积压"""
    return dict(_stats, pending=queued(), enabled=enabled(),
                batch_size=int(_cfg('METRIC_BUFFER_BATCH_SIZE', DEFAULT_BATCH_SIZE)),
                flush_sec=float(_cfg('METRIC_BUFFER_FLUSH_SEC', DEFAULT_FLUSH_SEC)))


def write_rows(rows, retries=0):
    """把一批字段字典写入库，返回落库条数。

    `retries` > 0 时，失败先重试（间隔逐次翻倍）再放弃——SQLite 的单写者锁
    "database table is locked" 属于典型瞬时故障，让整批点因为一次锁竞争蒸发
    说不过去。最终仍写不进去才整批丢弃并计数：采集组件不能因为自己写不进去
    而把调用方拖垮。失败后关闭本线程连接，下一批自动重连（数据库重启/连接失效）。
    """
    from .models import RequestMetric

    if not rows:
        return 0
    objs = [RequestMetric(**r) for r in rows]
    batch_size = max(1, int(_cfg('METRIC_BUFFER_BATCH_SIZE', DEFAULT_BATCH_SIZE)))
    for attempt in range(retries + 1):
        try:
            with transaction.atomic():
                RequestMetric.objects.bulk_create(objs, batch_size=batch_size)
            _stats['written'] += len(objs)
            _stats['batches'] += 1
            return len(objs)
        except Exception as exc:
            if attempt < retries:
                logger.warning('RequestMetric 批量落库失败（第 %d/%d 次尝试）：%s',
                               attempt + 1, retries + 1, exc)
                time.sleep(WRITE_RETRY_SEC * (2 ** attempt))
                continue
            _stats['errors'] += 1
            _stats['dropped'] += len(objs)
            logger.exception('RequestMetric 批量落库失败，本批 %d 条已丢弃', len(objs))
    try:
        connection.close()
    except Exception:
        pass
    return 0


def submit(payload):
    """提交一条请求指标。返回 'buffered' / 'written' / 'dropped'，永不抛异常。"""
    global _last_drop_log
    payload.setdefault('created_at', timezone.now())
    if not enabled():
        write_rows([payload])
        return 'written'
    _ensure_queue_size()
    try:
        _q.put_nowait(payload)
        return 'buffered'
    except queue.Full:
        _stats['dropped'] += 1
        now = time.monotonic()
        if now - _last_drop_log > 60:  # 限流日志：打满时每秒刷一条会淹没真错误
            _last_drop_log = now
            logger.error('请求指标缓冲区已满，累计丢弃 %d 条（写库跟不上采集）',
                         _stats['dropped'])
        return 'dropped'


def flush():
    """立即排空队列并落库（压测/管理命令/测试同步取数用），返回落库条数"""
    written = 0
    while True:
        rows, drained = [], False
        try:
            while True:
                rows.append(_q.get_nowait())
        except queue.Empty:
            drained = True
        written += write_rows(rows)
        if drained:
            return written


def _flusher_loop():
    batch_size = max(1, int(_cfg('METRIC_BUFFER_BATCH_SIZE', DEFAULT_BATCH_SIZE)))
    flush_sec = float(_cfg('METRIC_BUFFER_FLUSH_SEC', DEFAULT_FLUSH_SEC))
    batch = []
    deadline = time.monotonic() + flush_sec
    while not _stop.is_set():
        if batch and (len(batch) >= batch_size or time.monotonic() >= deadline):
            write_rows(batch, retries=WRITE_RETRIES)
            batch = []
            deadline = time.monotonic() + flush_sec
        try:
            batch.append(_q.get(timeout=0.2))  # 短等待，停服时能尽快退出
        except queue.Empty:
            continue
    write_rows(batch, retries=WRITE_RETRIES)


def start():
    """启动 flusher 线程（幂等）。缓冲区未启用时不启动，保持同步写库语义。"""
    global _flusher
    if not enabled():
        return False
    with _lock:
        if _flusher is not None:
            return True
        _ensure_queue_size()
        _stop.clear()
        _flusher = threading.Thread(target=_flusher_loop, name='obs-metric-buffer',
                                    daemon=True)
        _flusher.start()
        atexit.register(shutdown)
        logger.info('请求指标缓冲线程已启动：batch=%s flush=%ss',
                    _cfg('METRIC_BUFFER_BATCH_SIZE', DEFAULT_BATCH_SIZE),
                    _cfg('METRIC_BUFFER_FLUSH_SEC', DEFAULT_FLUSH_SEC))
        return True


def shutdown():
    """停止 flusher 并把剩余点排空（正常退出路径不丢数据）"""
    global _flusher
    thread = _flusher
    if thread is None:
        flush()
        return
    _flusher = None
    _stop.set()
    thread.join(timeout=5)
    flush()


def _reset_after_fork():
    """gunicorn/uwsgi 预加载（--preload）场景：master 进程在 ready() 里建好缓冲区，
    fork 出的 worker 只继承内存、不继承线程——_flusher 指向一个实际不存在的线程，
    且队列/锁可能在 fork 瞬间处于被持有状态。因此丢掉这些句柄并重新拉起。
    """
    global _lock, _flusher, _q, _q_sized
    _lock = threading.RLock()
    _flusher = None
    _q = queue.Queue()
    _q_sized = False
    _stop.clear()
    try:
        start()
    except Exception:  # pragma: no cover - fork 处理路径尽力而为
        logger.exception('fork 后重启请求指标缓冲失败')


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_after_fork)
