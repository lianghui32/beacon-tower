"""
init_data 命令：生成全平台演示数据

用法：
    python manage.py init_data                # 论坛数据 + 全部观测域演示数据
    python manage.py init_data --topics 300   # 自定义帖子数量
    python manage.py init_data --flush        # 连论坛旧数据一起清空

生成的数据覆盖六个观测域（时间范围见 --hours 参数）：
    RequestMetric 请求指标（含调用链 span） / HostMetric 主机指标 /
    RumEvent 前端事件 / LogEntry 日志 / AlertPolicy 告警策略 /
    DashCard 大盘卡片 / CustomMetric 自定义指标
种子完成后自动跑一轮告警评估，立即产生告警事件与通知记录。

说明：演示用户不设置任何密码（论坛无登录界面，账号仅作为帖子作者外键），
     Django 会为其写入不可用的密码哈希。
"""
import json
import math
import random
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from forum.models import Reply, Topic

CATEGORIES = ['闲聊', '技术', '求助']

TITLE_WORDS = [
    'Django', 'ORM', 'N+1', '查询优化', '部署', 'Celery', '缓存', '索引',
    '中间件', '信号', '迁移', '测试', 'Redis', 'PostgreSQL', '性能', '报错',
]
CONTENT_SENTENCES = [
    '最近线上 CPU 经常打满，排查了半天没找到原因。',
    '求助：帖子列表页打开特别慢，怀疑是数据库查询太多。',
    '分享一个用 select_related 把查询次数从 200 降到 3 的经历。',
    '日志里看到大量相同的 SQL，感觉像 N+1 查询，怎么确认？',
    '加了缓存之后速度确实快了，但缓存失效的时候还是会抖动。',
    '请问 Django ORM 怎么看实际执行的 SQL？',
    '压测发现 QPS 上不去，瓶颈好像在数据库。',
    '用 explain 分析了一下，原来缺了个索引。',
]
REPLY_SENTENCES = [
    '先用 django-debug-toolbar 看一下每条 SQL 吧。',
    '八成是循环里查库，经典 N+1。',
    '建议加个 annotate 聚合，一次查询搞定。',
    '索引别忘了，category 这种过滤字段很有必要。',
    '同遇到，最后是靠 prefetch_related 解决的。',
    'mark 一下，回去试试。',
]


def _uid(n=16):
    """演示数据的随机 ID（非安全用途）"""
    return uuid.uuid4().hex[:n]


class Command(BaseCommand):
    help = '生成论坛演示数据与全观测域演示数据（请求/主机/RUM/日志/告警/大盘）'

    def add_arguments(self, parser):
        parser.add_argument('--topics', type=int, default=200)
        parser.add_argument('--users', type=int, default=12)
        parser.add_argument('--hours', type=int, default=24,
                            help='采集数据回溯小时数（默认24）')
        parser.add_argument('--flush', action='store_true', help='连论坛旧数据一起清空')

    # ------------------------------------------------------------------
    def handle(self, *args, **options):
        import logging

        random.seed(20260926)  # 固定种子，结果可复现
        # 封顶 168 小时（7 天）：避免 --hours 过大构造千万级行拖死进程
        hours = min(168, max(2, options['hours']))
        if hours != options['hours']:
            self.stdout.write(f'--hours 已调整到 {hours}（上限 168 小时 / 7 天保留期）。')

        try:
            from django.db import transaction
            with transaction.atomic():
                self._seed_forum(options)
                self._flush_obs()
                self._seed_request_metrics(hours)
                self._seed_host_metrics(hours)
                self._seed_rum(hours)
                self._seed_logs(hours)
                self._seed_custom_metrics()
                self._seed_policies_and_dashcards()
                self._seed_ops()
        except Exception:
            logging.getLogger(__name__).exception('init_data 失败，已回滚，未留下半初始化数据')
            raise

        # 种子完成后立即评估一轮告警，产生事件与通知
        from alerts.engine import evaluate_once
        fired, recovered = evaluate_once()
        self.stdout.write(f'告警评估完成：新触发 {fired} 条，恢复 {recovered} 条。')

        self.stdout.write(self.style.SUCCESS(
            'init_data 完成：访问 http://127.0.0.1:8014/ 查看监控总览。'
        ))

    # ------------------------------------------------------------------
    def _seed_forum(self, options):
        """论坛用户 / 帖子 / 回复（用户不设密码，不可登录，仅作作者外键）"""
        if options['flush']:
            Reply.objects.all().delete()
            Topic.objects.all().delete()
            # 只删除本命令生成的演示用户（user00、user01…），不动真实账号/演示访客账号
            deleted, _ = User.objects.filter(username__regex=r'^user\d+$').delete()
            self.stdout.write(f'论坛旧数据已清空（含 {deleted} 条演示用户及关联）。')

        try:
            from faker import Faker
            faker = Faker('zh_CN')
        except ImportError:
            faker = None
            self.stdout.write('未安装 faker，使用内置随机词库。')

        n_users = options['users']
        existing = set(User.objects.values_list('username', flat=True))
        users = []
        for i in range(n_users):
            username = f'user{i:02d}'
            if username in existing:
                users.append(User.objects.get(username=username))
                continue
            first = faker.last_name() + faker.first_name() if faker else f'用户{i:02d}'
            users.append(User.objects.create_user(username=username, first_name=first))
        if not users:
            users = list(User.objects.all())
        self.stdout.write(f'用户就绪：{len(users)} 个（不可登录的演示账号）。')

        n_topics = options['topics']
        if Topic.objects.count() < n_topics:
            now = timezone.now()
            topics = []
            for i in range(n_topics):
                if faker:
                    title = f'{faker.word()} {random.choice(TITLE_WORDS)} 问题讨论（第{i + 1}贴）'
                    content = faker.paragraph()
                else:
                    title = (random.choice(TITLE_WORDS) + random.choice(['求助', '分享', '讨论', '踩坑'])
                             + f'（第{i + 1}贴）')
                    content = ''.join(random.choice(CONTENT_SENTENCES) for _ in range(3))
                topics.append(Topic(
                    title=title[:200], content=content,
                    category=random.choices(CATEGORIES, weights=[6, 2, 2])[0],
                    author=random.choice(users),
                    views=random.randint(0, 5000),
                    created_at=now - timedelta(minutes=random.randint(0, 60 * 24 * 7)),
                ))
            Topic.objects.bulk_create(topics, batch_size=200)
        self.stdout.write(f'帖子就绪：{Topic.objects.count()} 条。')

        if Reply.objects.count() < 300:
            replies = []
            for tid in Topic.objects.values_list('id', flat=True):
                for _ in range(random.randint(0, 8)):
                    content = faker.sentence() if faker else random.choice(REPLY_SENTENCES)
                    replies.append(Reply(topic_id=tid, author=random.choice(users), content=content))
            Reply.objects.bulk_create(replies, batch_size=500)
        self.stdout.write(f'回复就绪：{Reply.objects.count()} 条。')

    # ------------------------------------------------------------------
    def _flush_obs(self):
        """清空观测域旧数据（保留论坛内容）"""
        from alerts.models import AlertEvent, AlertPolicy, NotificationRecord
        from hosts.models import HostMetric
        from loghub.models import LogEntry
        from monitor.models import CustomMetric, DashCard, RequestMetric
        from rum.models import RumEvent

        for model in (RequestMetric, HostMetric, RumEvent, LogEntry,
                      NotificationRecord, AlertEvent, CustomMetric):
            model.objects.all().delete()
        # 策略与大盘卡片重建，保证演示配置固定
        AlertPolicy.objects.all().delete()
        DashCard.objects.all().delete()
        self.stdout.write('观测域旧数据已清空。')

    # ------------------------------------------------------------------
    # 请求指标（APM）
    # ------------------------------------------------------------------
    PROFILE = {
        '/forum/problem/':   {'base': 420, 'spread': 160, 'sql': 58, 'weight': 4},
        '/forum/optimized/': {'base': 46, 'spread': 22, 'sql': 5, 'weight': 4},
        '/forum/topic/':     {'base': 90, 'spread': 40, 'sql': 9, 'weight': 2},
        '/':                 {'base': 24, 'spread': 10, 'sql': 3, 'weight': 1},
    }
    SLOW_SQL_SAMPLES = [
        'SELECT ... FROM forum_topic WHERE category = "闲聊" ORDER BY created_at DESC',
        'SELECT COUNT(*) AS "__count" FROM forum_reply WHERE forum_reply.topic_id = <N>',
        'SELECT ... FROM forum_topic ORDER BY created_at DESC  （全表扫描）',
    ]
    # 常见国内运营商公网 IP 首段（用于生成真实感的访客 IP）
    IP_HEADS = [36, 39, 42, 58, 59, 60, 61, 101, 106, 110, 111, 112, 113, 114, 115,
                116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 171, 175, 180,
                182, 183, 184, 202, 203, 210, 211, 218, 219, 220, 221, 222, 223]

    def _random_public_ip(self):
        """返回 (ip, 省份/海外, 城市或国家)"""
        from monitor.geoip import resolve
        # 有界重试：IP_HEADS 全部被判为内网时兜底返回，避免死循环
        for _ in range(50):
            h = random.choice(self.IP_HEADS)
            ip = f'{h}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}'
            prov, city, src = resolve(ip)
            if src != 'local':
                return ip, prov, city
        return '203.0.113.1', '未知', ''

    def _visitor_pool(self, size=160):
        """预生成"回头客"IP 池（少量局域网 IP + 大量公网 IP），让 UV/PV/行为数据更真实"""
        pool = []
        for _ in range(size):
            ip, prov, city = self._random_public_ip()
            pool.append((ip, prov, city))
        for i in range(8):
            pool.append((f'192.168.1.{10 + i}', '局域网', ''))
        return pool

    def _load_factor(self, dt):
        """昼夜流量曲线：白天高、深夜低，再叠加一个傍晚尖峰"""
        h = dt.hour + dt.minute / 60.0
        base = 0.35 + 0.65 * max(0.0, math.sin((h - 6) / 24 * 2 * math.pi))
        peak = 1.8 if 19 <= h < 21 else 1.0
        return base * peak

    def _seed_request_metrics(self, hours):
        from monitor.models import RequestMetric
        now = timezone.now()
        paths = list(self.PROFILE)
        weights = [self.PROFILE[p]['weight'] for p in paths]
        visitor_pool = self._visitor_pool()  # 回头客 IP 池
        rows = []
        # hours × 每分钟若干条请求；每分钟条数与流量曲线成正比
        for minute in range(hours * 60, -1, -1):
            ts = now - timedelta(minutes=minute)
            lf = self._load_factor(ts)
            n = max(0, int(random.gauss(6 * lf, 2)))
            # 演练窗口：最后 40~10 分钟插入慢请求与错误尖峰
            spike = (40 >= minute >= 10)
            for _ in range(n):
                path = random.choices(paths, weights=weights)[0]
                p = self.PROFILE[path]
                mul = random.uniform(2.5, 4.5) if (spike and random.random() < 0.25) else 1.0
                dur = max(4.0, random.gauss(p['base'], p['spread'] / 2) * mul)
                sql_n = max(1, int(random.gauss(p['sql'], max(1, p['sql'] / 5))))
                # 回头客：热门访客出现频率更高（Zipf 式权重）
                client_ip, province, geo_city = random.choices(
                    visitor_pool, weights=[1.0 / (i + 1) ** 0.3 for i in range(len(visitor_pool))])[0]

                is_error = False
                status = 200
                if path == '/':
                    status = random.choice([200, 200, 200, 302])
                elif spike and random.random() < 0.08:
                    # 演练窗口制造错误流量
                    path = '/forum/chaos/error/'
                    status = 500
                    is_error = True
                    dur = random.uniform(120, 800)
                    sql_n = random.randint(1, 4)
                elif random.random() < 0.01:
                    status = 404
                    is_error = True
                    dur = random.uniform(15, 60)
                    sql_n = 1

                # SQL 明细与 span
                sqls, spans, offset = [], [], 0.0
                sql_time = 0.0
                for _si in range(min(sql_n, 14)):
                    t = random.uniform(0.5, 14) * (mul if spike else 1.0)
                    if path.endswith('problem/') and random.random() < 0.12:
                        t = random.uniform(120, 380)
                    sql_time += t
                    sqls.append({'sql': random.choice(self.SLOW_SQL_SAMPLES),
                                 'time_ms': round(t, 1)})
                    spans.append({'kind': 'sql',
                                  'name': random.choice(self.SLOW_SQL_SAMPLES)[:110],
                                  'off': round(offset, 1), 'dur': round(t, 2)})
                    offset += t
                spans.insert(0, {'kind': 'request', 'name': f'GET {path}',
                                 'off': 0.0, 'dur': round(dur, 2)})
                spans.append({'kind': 'view', 'name': 'forum.views',
                              'off': round(sql_time, 1),
                              'dur': round(max(0.0, dur - sql_time), 2)})
                slow = [s for s in sqls if s['time_ms'] > 100]
                rows.append(RequestMetric(
                    path=path[:200], method='GET', status_code=status,
                    duration_ms=round(dur, 2), sql_count=sql_n,
                    sql_time_ms=round(sql_time, 2),
                    slow_queries=json.dumps(slow[:5], ensure_ascii=False),
                    slow_query_count=len(slow),
                    cpu_percent=round(min(95.0, dur / 25 + sql_n * 0.6 + random.uniform(2, 10)), 1),
                    trace_id=_uid(16),
                    view_name='forum.views.topic_list' if 'forum' in path else 'monitor.views',
                    is_error=is_error,
                    client_ip=client_ip,
                    geo_province=province,
                    geo_city=geo_city,
                    spans=json.dumps(spans, ensure_ascii=False),
                    created_at=ts + timedelta(seconds=random.randint(0, 59)),
                ))
        RequestMetric.objects.bulk_create(rows, batch_size=500)
        self.stdout.write(f'请求指标就绪：{len(rows)} 条（近 {hours} 小时，含演练窗口）。')

    # ------------------------------------------------------------------
    # 主机指标
    # ------------------------------------------------------------------
    def _seed_host_metrics(self, hours):
        from hosts.models import HostMetric
        import os
        import socket
        try:
            hostname = socket.gethostname() or 'local'
        except Exception:
            hostname = 'local'
        try:
            import psutil
            mem = psutil.virtual_memory()
            mem_total = mem.total / 1024 / 1024
            cores = psutil.cpu_count() or 4
            du = psutil.disk_usage(os.path.abspath(os.sep))
            disk_pct = du.percent
            disk_used = du.used / 1024 ** 3
            disk_total = du.total / 1024 ** 3
        except Exception:
            mem_total, cores = 8192.0, 8
            disk_pct, disk_used, disk_total = 61.0, 300.0, 512.0

        now = timezone.now()
        rows = []
        for minute in range(hours * 60, -1, -1):
            ts = now - timedelta(minutes=minute)
            lf = self._load_factor(ts)
            phase = ts.timestamp() / 1800
            cpu = 18 + 26 * lf * (0.7 + 0.3 * math.sin(phase)) + random.uniform(0, 8)
            if 40 >= minute >= 10:  # 演练窗口 CPU 抬升
                cpu += random.uniform(8, 18)
            cpu = min(96.0, cpu)
            mem_pct = min(90.0, 48 + lf * 14 + random.uniform(0, 4))
            rows.append(HostMetric(
                hostname=hostname, cpu_percent=round(cpu, 1), cpu_cores=cores,
                load_avg=round(cpu / 100 * cores, 2),
                mem_percent=round(mem_pct, 1),
                mem_used_mb=round(mem_total * mem_pct / 100, 1),
                mem_total_mb=round(mem_total, 1),
                disk_percent=round(disk_pct, 1),
                disk_used_gb=round(disk_used, 2), disk_total_gb=round(disk_total, 2),
                net_sent_kbps=round(20 + 140 * lf * random.random(), 1),
                net_recv_kbps=round(80 + 700 * lf * random.random(), 1),
                proc_count=random.randint(180, 260),
                tcp_conns=random.randint(30, 120),
                simulated=False, created_at=ts,
            ))
        HostMetric.objects.bulk_create(rows, batch_size=500)
        self.stdout.write(f'主机指标就绪：{len(rows)} 条（{hostname}，真实内存/磁盘基线 + 曲线模拟）。')

    # ------------------------------------------------------------------
    # 前端 RUM
    # ------------------------------------------------------------------
    RUM_PAGES = [
        'http://127.0.0.1:8014/forum/problem/', 'http://127.0.0.1:8014/forum/optimized/',
        'http://127.0.0.1:8014/forum/topic/12/', 'http://127.0.0.1:8014/',
    ]
    RUM_DEVICES = [
        'Chrome/Windows', 'Chrome/Windows', 'Chrome/Windows', 'Edge/Windows',
        'Chrome/macOS', 'Safari/macOS', 'Chrome/Android', 'Safari/iOS',
    ]
    JS_ERRORS = [
        "TypeError: Cannot read properties of undefined (reading 'reply_count')",
        'UnhandledRejection: fetch timeout after 30000ms',
        'ReferenceError: obsConfig is not defined',
        'TypeError: Failed to fetch',
    ]
    RUM_APIS = [
        ('GET', '/forum/api/stats/', 0.95),
        ('GET', '/monitor/api/metric/', 0.98),
        ('POST', '/rum/beacon/', 1.0),
        ('GET', '/api/recommend/', 0.88),
    ]

    def _seed_rum(self, hours):
        from rum.models import RumEvent
        now = timezone.now()
        hours_rum = min(hours, 6)  # RUM 只造近 6 小时
        rows = []
        for minute in range(hours_rum * 60, -1, -1):
            ts = now - timedelta(minutes=minute)
            lf = self._load_factor(ts)
            n_pv = max(0, int(random.gauss(4 * lf, 1.5)))
            spike = (40 >= minute >= 10)
            for _ in range(n_pv):
                page = random.choice(self.RUM_PAGES)
                device = random.choice(self.RUM_DEVICES)
                sid = 's' + _uid(12)
                ref = random.choice(['', 'https://www.google.com/', page])

                rows.append(RumEvent(
                    type='pv', app='forum', page_url=page, referrer=ref,
                    session_id=sid, device=device,
                    screen=random.choice(['1920x1080', '2560x1440', '1440x900', '390x844']),
                    created_at=ts,
                ))
                # 性能事件
                load = max(120.0, random.gauss(900, 300) * (random.uniform(1.8, 3.0) if spike else 1.0))
                rows.append(RumEvent(
                    type='perf', app='forum', page_url=page, session_id=sid, device=device,
                    ttfb_ms=round(random.uniform(20, 180), 1),
                    dom_ready_ms=round(load * random.uniform(0.5, 0.8), 1),
                    load_ms=round(load, 1),
                    fp_ms=round(load * random.uniform(0.2, 0.4), 1),
                    fcp_ms=round(load * random.uniform(0.25, 0.5), 1),
                    lcp_ms=round(load * random.uniform(0.4, 0.8), 1),
                    created_at=ts,
                ))
                # API 事件 2~3 条
                for _ in range(random.randint(2, 3)):
                    method, url, ok_rate = random.choice(self.RUM_APIS)
                    ok = random.random() < ok_rate
                    rows.append(RumEvent(
                        type='api', app='forum', page_url=page, session_id=sid, device=device,
                        api_url=url, api_method=method,
                        api_status=200 if ok else random.choice([500, 502, 0]),
                        api_duration_ms=round(max(8.0, random.gauss(180, 90)), 1),
                        api_ok=ok, created_at=ts,
                    ))
                # 偶发 JS 错误（演练窗口加密）
                if random.random() < (0.12 if spike else 0.02):
                    rows.append(RumEvent(
                        type='error', app='forum', page_url=page, session_id=sid, device=device,
                        err_message=random.choice(self.JS_ERRORS)[:280],
                        err_stack='TypeError: ...\n    at renderList (topic_list.js:87:23)\n'
                                  '    at init (main.js:12:5)',
                        created_at=ts,
                    ))
                # 慢资源
                if random.random() < 0.15:
                    rtype = random.choice(['script', 'css', 'img'])
                    rows.append(RumEvent(
                        type='resource', app='forum', page_url=page, session_id=sid,
                        r_type=rtype,
                        r_url=f'/static/{rtype}/bundle.{_uid(3)}.js'[:256],
                        r_duration_ms=round(random.uniform(110, 900), 1),
                        r_size_kb=round(random.uniform(20, 600), 1),
                        created_at=ts,
                    ))
        # 自定义事件
        for minute in range(hours_rum * 60, -1, -3):
            ts = now - timedelta(minutes=minute)
            rows.append(RumEvent(
                type='custom', app='forum', page_url=random.choice(self.RUM_PAGES),
                event_name=random.choice(['点击点赞', '提交回复', '切换版块', '搜索帖子']),
                payload=json.dumps({'dur': random.randint(100, 2000)}, ensure_ascii=False),
                created_at=ts,
            ))
        RumEvent.objects.bulk_create(rows, batch_size=500)
        self.stdout.write(f'RUM 事件就绪：{len(rows)} 条（PV/性能/API/错误/资源/自定义）。')

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    LOG_PATTERNS = [
        ('ERROR', 'app', 'payment', '订单 payment-<N> 创建失败，退款队列积压 <N> 条', 6),
        ('ERROR', 'app', 'email', '验证码邮件发送失败：SMTP connect timeout after 10000ms', 3),
        ('WARNING', 'app', 'cache', '缓存命中率下降至 <N>%，key 过期风暴', 8),
        ('WARNING', 'app', 'db', '慢查询告警：SELECT ... FROM forum_topic 耗时 <N>ms', 5),
        ('WARNING', 'system', 'django.security', 'Not Found: /wp-login.php', 4),
        ('CRITICAL', 'app', 'worker', 'Celery worker 心跳丢失：<ID>', 1),
        ('INFO', 'app', 'payment', '订单 payment-<N> 支付成功，金额 <N>.<N> 元', 10),
    ]

    def _seed_logs(self, hours):
        from loghub.models import LogEntry
        now = timezone.now()
        hours_log = min(hours, 6)
        rows = []
        for minute in range(hours_log * 60, -1, -1):
            ts = now - timedelta(minutes=minute)
            spike = (40 >= minute >= 10)
            for level, source, logger, tpl, per_hour in self.LOG_PATTERNS:
                rate = per_hour / 60.0 * (4 if spike else 1.0)
                if random.random() < rate:
                    msg = (tpl
                           .replace('<N>', str(random.randint(2, 999)))
                           .replace('<ID>', _uid(8)))
                    rows.append(LogEntry(
                        source=source, level=level, logger=logger,
                        message=msg, created_at=ts,
                    ))
        LogEntry.objects.bulk_create(rows, batch_size=500)
        self.stdout.write(f'日志就绪：{len(rows)} 条（含 7 类模板，演练窗口加密）。')

    # ------------------------------------------------------------------
    # 自定义指标 / 告警策略 / 大盘卡片
    # ------------------------------------------------------------------
    def _seed_custom_metrics(self):
        from monitor.models import CustomMetric
        now = timezone.now()
        rows = []
        for minute in range(120, -1, -5):
            ts = now - timedelta(minutes=minute)
            rows.append(CustomMetric(
                name='order_queue_length', labels='{"queue":"refund"}',
                value=round(max(0, random.gauss(30, 12)), 1), created_at=ts,
            ))
            rows.append(CustomMetric(
                name='cache_hit_rate', labels='{"cache":"redis-main"}',
                value=round(random.uniform(78, 96), 1), created_at=ts,
            ))
        CustomMetric.objects.bulk_create(rows, batch_size=100)
        self.stdout.write(f'自定义指标就绪：order_queue_length / cache_hit_rate 各 {len(rows) // 2} 点。')

    def _seed_policies_and_dashcards(self):
        from alerts.models import AlertPolicy
        from monitor.models import DashCard

        policies = [
            ('主机 CPU 过高', 'host.cpu_percent', '>', 80, 'P1', 'CPU 持续高于 80% 需要扩容或排查热点'),
            ('接口错误率过高', 'http.error_rate', '>', 5, 'P0', '错误率超过 5% 视为服务故障'),
            ('接口平均耗时过高', 'http.avg_duration', '>', 500, 'P1', '平均耗时超过 500ms 影响体验'),
            ('前端 JS 错误增多', 'rum.js_error_count', '>', 10, 'P2', '近 5 分钟 JS 错误条/分'),
            ('ERROR 日志激增', 'log.error_count', '>', 15, 'P2', '近 5 分钟 ERROR 日志条/分'),
            ('前端 API 错误率', 'rum.api_error_rate', '>', 8, 'P1', '用户视角接口失败率'),
            ('退款队列积压', 'custom.order_queue_length', '>', 45, '提示', '业务自定义指标：退款队列长度'),
        ]
        AlertPolicy.objects.bulk_create([
            AlertPolicy(name=n, metric_key=k, operator=o, threshold=t, level=lvl, note=note)
            for n, k, o, t, lvl, note in policies
        ])
        self.stdout.write(f'告警策略就绪：{len(policies)} 条（其中业务自定义指标 1 条）。')

        cards = [
            ('主机 CPU 使用率', 'host.cpu_percent', 'area', 60, 1),
            ('接口平均耗时', 'http.avg_duration', 'line', 60, 1),
            ('请求数/分钟', 'http.request_count', 'bar', 60, 1),
            ('接口错误率', 'http.error_rate', 'line', 60, 1),
            ('前端 PV/分钟', 'rum.pv', 'area', 60, 1),
            ('ERROR 日志数', 'log.error_count', 'bar', 60, 1),
            ('退款队列长度（自定义）', 'custom.order_queue_length', 'line', 120, 2),
        ]
        DashCard.objects.bulk_create([
            DashCard(title=t, metric_key=k, chart_type=c, minutes=m, span=s, order=i)
            for i, (t, k, c, m, s) in enumerate(cards)
        ])
        self.stdout.write(f'大盘卡片就绪：{len(cards)} 张。')

    # ------------------------------------------------------------------
    # 运维中心种子：拨测 / SLO / 资产 / 自愈示例 / 首份巡检报告
    # ------------------------------------------------------------------
    def _seed_ops(self):
        from ops.models import Asset, HealAction, ProbeTask, SLO
        from ops.inspection import run_inspection

        # 清空旧配置重建
        ProbeTask.objects.all().delete()
        SLO.objects.all().delete()
        HealAction.objects.all().delete()

        probe_base = 'http://127.0.0.1:8014'
        # 平台受登录门禁保护：令牌只对上报 API 与 /metrics 生效（不放行页面路径）。
        # 探活用 /api/health/（令牌或会话均可）；令牌放在请求头而不是拼进 URL，避免明文落库。
        token = settings.OBSERVABILITY.get('INGEST_TOKEN', '')
        probes = [
            ('平台健康检查', f'{probe_base}/api/health/', 200, 'ok', 60, True, {}),
            ('Prometheus 端点', f'{probe_base}/metrics', 200, 'django_requests_total', 60, True,
             {'X-OBS-Token': token}),
            ('HTTPS 证书检查（示例）', 'https://www.baidu.com/', 200, '', 300, False, {}),
        ]
        ProbeTask.objects.bulk_create([
            ProbeTask(name=n, url=u, expect_status=s, keyword=k,
                      interval_sec=i, enabled=e, headers=h)
            for n, u, s, k, i, e, h in probes
        ])
        self.stdout.write(f'拨测任务就绪：{len(probes)} 条（其中 1 条 HTTPS 示例默认停用）。')

        SLO.objects.create(name='论坛服务可用性', target_availability=99.9,
                           target_p95_ms=500, window_days=30)
        self.stdout.write('SLO 就绪：论坛服务可用性 99.9% / P95 500ms / 30 天窗口。')

        # 资产：本机登记并补标签（其余主机由 Agent 上报自动登记）
        from hosts.models import HostMetric
        latest_host = HostMetric.objects.order_by('-created_at').first()
        if latest_host:
            asset, _ = Asset.objects.get_or_create(
                hostname=latest_host.hostname,
                defaults={'label': '平台宿主机', 'env': '生产', 'owner': 'ops',
                          'auto': True, 'last_seen_at': latest_host.created_at})
            self.stdout.write(f'资产登记：{asset.hostname}（生产 · ops）。')

        HealAction.objects.create(
            name='示例：清理临时目录', action_type='cleanup_tmp',
            param='', enabled=False, cooldown_min=60,
        )
        self.stdout.write('自愈示例就绪：清理临时目录（默认停用，可启用测试）。')

        # 生成首份巡检报告
        run, result = run_inspection(trigger='manual')
        self.stdout.write(f'巡检报告就绪：健康评分 {result["score"]} / 100'
                          f'（不合格 {result["fails"]} 项，警告 {result["warns"]} 项）。')
