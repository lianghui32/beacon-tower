"""
monitor/leadership.py — 后台任务租约选主（多副本安全）

平台有四类周期任务：主机采集 / 告警评估 / 拨测 / 定时巡检。它们必须"全集群同时只有
一个进程在跑"，否则同一台机器被采两次、同一条策略告警两次、同一个目标被拨测两次。
改造前这件事靠部署纪律兜着（compose 里写死 worker 单副本 + "切勿 --scale" 的注释），
那是人治不是机制：想扩容就得先记住这条口头约定。

现在用一张租约表（monitor.TaskLease）选主：
- 每个任务一行，进程身份是 "主机名:pid:随机后缀"；
- 抢约/续约都是一条 compare-and-swap 条件 UPDATE，SQLite 的单写者与 Postgres 的
  行锁都保证竞争者中只有一个命中，其余更新 0 行即落选；
- 租约到期自动失效：持有进程被杀/宕机，其它进程在下一次抢约时接管，无需人工介入；
- term（任期）每次易主 +1，便于识别"我是不是已经被取代了"。

边界（诚实版）：这是租约不是围栏（fencing）。旧持有者若在续约间隙里正跑着一轮长任务，
它仍可能把那轮做完——最坏情况是"一个周期内重复执行一次"。四类周期任务都是幂等写
（告警事件按策略去重合并、采集点多一条只是曲线略密、拨测结果本身就是多次采样），
所以可接受。将来若加入非幂等处置（比如自愈执行），必须带着 term 做写前校验。
"""
import atexit
import logging
import os
import socket
import threading
import time
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# 租约默认 60 秒：远大于最快任务周期（拨测 5s），又不至于故障后要等太久才接管
DEFAULT_TTL_SEC = max(5, int(os.environ.get('OBS_LEASE_TTL_SEC', '60')))

_holder = None
_holder_lock = threading.Lock()


def holder():
    """本进程身份：主机名:pid:随机后缀（同机多进程、容器重启都能区分开）"""
    global _holder
    if _holder is None:
        with _holder_lock:
            if _holder is None:
                host, pid, tag = socket.gethostname()[:40], os.getpid(), uuid.uuid4().hex[:8]
                _holder = f'{host}:{pid}:{tag}'
    return _holder


def _model():
    from .models import TaskLease
    return TaskLease


def acquire(name, holder_id=None, ttl=None):
    """抢占或续约任务租约，返回 (是否持有, 任期)。

    三步都是单条条件 UPDATE/INSERT（不用 SELECT ... FOR UPDATE，
    因此 SQLite 与 Postgres 共用同一套代码）：
    1. 建行：首次使用时没有这一行，直接带自己为持有者插入；撞唯一约束就说明
       别的进程抢先建了，本轮落选；
    2. 续约：已经是我的 → 只推到期时间，任期不变；
    3. 抢占：CAS，条件同时写上"持有者与我观察到的一致"和"确实已过期"；
       竞争失败者条件不再成立 → 更新 0 行 → 落选。
    """
    me = holder_id or holder()
    ttl = int(ttl or DEFAULT_TTL_SEC)
    TaskLease = _model()
    now = timezone.now()

    lease = TaskLease.objects.filter(name=name).first()
    if lease is None:
        try:
            with transaction.atomic():
                TaskLease.objects.create(
                    name=name, holder=me, term=1,
                    acquired_at=now, renewed_at=now, expires_at=now + timedelta(seconds=ttl))
            _log_change(name, me, 1, '取得')
            return True, 1
        except IntegrityError:
            return False, 0  # 别的进程同时建了行，本轮落选，下一轮走续约/抢占

    if lease.holder == me:
        updated = TaskLease.objects.filter(name=name, holder=me).update(
            renewed_at=now, expires_at=now + timedelta(seconds=ttl))
        if updated:
            return True, lease.term
        return False, lease.term  # 持有者已被改写（续约间隙里被人抢走）

    if lease.holder and lease.expires_at and lease.expires_at > now:
        return False, lease.term  # 明确还在别人手里，不必尝试 CAS

    # 抢占：CAS 条件必须把"我观察到的持有者 + 已过期"一起写上，
    # 竞争失败者的条件不再成立 → 更新 0 行 → 落选
    qs = TaskLease.objects.filter(name=name, holder=lease.holder)
    if lease.expires_at is None:
        qs = qs.filter(expires_at__isnull=True)
    else:
        qs = qs.filter(expires_at__lte=now)
    taken = qs.update(holder=me, term=lease.term + 1, acquired_at=now,
                      renewed_at=now, expires_at=now + timedelta(seconds=ttl))
    if taken:
        _log_change(name, me, lease.term + 1, '接管')
        return True, lease.term + 1
    return False, lease.term  # CAS 未命中：已有更近的持有者


def release(name, holder_id=None):
    """主动交出租约（正常退出时调用，让对端立刻接管而不是等 TTL 过期）"""
    me = holder_id or holder()
    TaskLease = _model()
    n = TaskLease.objects.filter(name=name, holder=me).update(
        holder='', expires_at=timezone.now(), renewed_at=timezone.now())
    if n:
        logger.info('已交还任务租约 %s（%s）', name, me)
    return bool(n)


def status():
    """全集群租约一览（给 /metrics 与运维页看）"""
    TaskLease = _model()
    now = timezone.now()
    out = []
    for lease in TaskLease.objects.order_by('name'):
        held = bool(lease.holder) and lease.expires_at is not None and lease.expires_at > now
        out.append({
            'name': lease.name,
            'holder': lease.holder or '-',
            'mine': lease.holder == holder(),
            'leading': held,
            'term': lease.term,
            'ttl_sec': round((lease.expires_at - now).total_seconds(), 1) if held else 0,
        })
    return out


_logged = {}


def _log_change(name, me, term, verb):
    """接管/取得只在任期变化时打日志，避免续约刷屏"""
    key = f'{name}:{term}'
    if _logged.get(name) == key:
        return
    _logged[name] = key
    logger.info('任务租约 %s：%s 已%s（term=%d）', name, me, verb, term)


class LeaseLoop:
    """带租约的周期任务骨架：只有持有租约的进程执行 work()，其余热待命。

    续约节拍与任务节拍分开：tick 间隔取 min(interval, ttl/3)，保证远快于租约到期，
    而 work 的实际执行由 next_run 控制——这样 5 秒一次的拨测和 12 小时一次的巡检
    共用同一套骨架，巡检期间租约仍在正常续约，不会被别的进程误接管。

    work() 返回数字时作为"下一轮等多久"（巡检失败要缩短重试间隔）。
    """

    def __init__(self, name, work, interval, ttl=None, sleep=time.sleep, holder_id=None):
        self.name = name
        self.work = work
        self.interval = float(interval)
        self.ttl = int(ttl or DEFAULT_TTL_SEC)
        self.holder_id = holder_id
        self.sleep = sleep
        self.tick_interval = max(1.0, min(self.interval, self.ttl / 3))
        self.next_run = 0.0
        self.owned = False
        self.term = 0

    def tick(self, now=None):
        """续约一次；到点且持有租约时执行一轮。返回本轮是否真的执行了 work。

        now 用同一个时钟域（默认 time.monotonic）做调度，测试可注入假时钟。
        """
        now = time.monotonic() if now is None else now
        owned, term = acquire(self.name, holder_id=self.holder_id, ttl=self.ttl)
        self.owned, self.term = owned, term
        if not owned or now < self.next_run:
            return False
        try:
            result = self.work()
        except Exception:  # 任务异常不能带走循环——否则平台自己瞎了
            logger.exception('周期任务 %s 执行异常', self.name)
            result = None
        # work 返回数字即"下轮等待秒数"；bool 是返回值陷阱，不当作间隔
        delay = (float(result) if isinstance(result, (int, float)) and not isinstance(result, bool)
                 else self.interval)
        self.next_run = now + max(0.1, delay)
        return True

    def run(self):
        while True:
            self.tick()
            self.sleep(self.tick_interval)


def register_exit_release(names):
    """进程退出时交还自己持有的租约（对端不必等满 TTL 才能接管）"""
    names = tuple(names)

    def _release_all():
        for name in names:
            try:
                release(name)
            except Exception:
                logger.exception('退出时交还租约 %s 失败', name)

    atexit.register(_release_all)
