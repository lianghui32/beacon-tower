"""
config/settings.py — 烽火台 Beacon Tower · 全栈可观测运维监控平台
端口 8014，SQLite 数据库，开箱即用。

平台自身采集所有数据（主机 / 请求 / 前端 RUM / 日志），自研分析与告警引擎，
不依赖任何外部 SaaS；psutil 为唯一推荐的采集依赖（未安装时自动退化为模拟指标）。
"""
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# 数据目录：容器部署时指向挂载卷（密钥/令牌文件、SQLite 兜底库都写到卷里，
# 容器可随时重建）；默认项目根。见 Dockerfile / docker-compose.yml
DATA_DIR = Path(os.environ.get('OBS_DATA_DIR') or BASE_DIR)


def _ingest_token():
    """接入令牌：优先环境变量；否则首启自动生成并缓存到本地文件（勿提交）"""
    token = os.environ.get('OBS_INGEST_TOKEN')
    if token:
        return token
    token_file = DATA_DIR / 'ingest_token.txt'
    if token_file.exists():
        return token_file.read_text(encoding='utf-8').strip()
    import secrets
    token = secrets.token_urlsafe(32)
    token_file.write_text(token, encoding='utf-8')
    return token

# 密钥：优先读环境变量；开发环境首次启动自动生成并缓存到数据目录 .secret_key（勿提交）
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    _key_file = DATA_DIR / '.secret_key'
    if _key_file.exists():
        SECRET_KEY = _key_file.read_text(encoding='utf-8').strip()
    else:
        import secrets
        SECRET_KEY = secrets.token_urlsafe(64)
        _key_file.write_text(SECRET_KEY, encoding='utf-8')
# 调试开关：环境变量 DJANGO_DEBUG=1 开启（默认开，便于开箱演示）；生产部署务必设 0
DEBUG = os.environ.get('DJANGO_DEBUG', '1') == '1'
# 允许的主机：默认本机演示；生产部署用环境变量列出，如 "obs.example.com,10.0.0.5"
ALLOWED_HOSTS = [h.strip() for h in
                 os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]').split(',')
                 if h.strip()]
# 演示场景允许通过 Host 直连本机 IP 访问面板；不需要时可删除 '*' 走白名单
if os.environ.get('DJANGO_ALLOW_ALL_HOSTS', '1') == '1':
    ALLOWED_HOSTS.append('*')

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

# 密码策略：创建/修改密码时强制强度校验（登录不限，仅约束设新密码）
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# 生产环境（DEBUG=0）自动启用的安全加固
if not DEBUG:
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    # 反代/HTTPS 场景可再打开：SESSION_COOKIE_SECURE / CSRF_COOKIE_SECURE / SECURE_SSL_REDIRECT
    if os.environ.get('DJANGO_SECURE_COOKIES', '0') == '1':
        SESSION_COOKIE_SECURE = True
        CSRF_COOKIE_SECURE = True
        SECURE_HSTS_SECONDS = 31536000
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# ---------------- 登录认证（自用系统：整站需要登录） ----------------
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'
# 会话安全：HttpOnly 防 JS 读取；SameSite=Lax 防 CSRF；自用场景 14 天免重新登录
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 14 * 24 * 3600

# ---------------- 应用 ----------------
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # 本项目应用
    'forum.apps.ForumConfig',      # 演示目标应用（故意埋了性能问题的"论坛"）
    'monitor.apps.MonitorConfig',  # 监控总览 / APM / 自定义大盘 / 采集中间件 / Prometheus
    'hosts.apps.HostsConfig',      # 主机监控（psutil 采集线程）
    'loghub.apps.LoghubConfig',    # 日志服务（入库 Handler + 接入 API + 查询）
    'rum.apps.RumConfig',          # 前端性能监控 RUM（自研 JS SDK + 上报 + 分析页）
    'alerts.apps.AlertsConfig',    # 告警中心（策略 / 评估引擎 / 事件 / 通知）
    'analytics.apps.AnalyticsConfig',  # 智能分析（异常检测 / 预测 / 相关性 / 日志挖掘 / 报表）
    'ops.apps.OpsConfig',          # 运维中心（拨测 / 故障单 / 巡检 / SLO / 资产 / 自愈 / 审计 / 通知渠道）
    'cleaner.apps.CleanerConfig',  # 清理加速中心（磁盘分析 / 垃圾清理 / 内存整理）
]

# ---------------- 中间件 ----------------
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # 整站登录门禁：未登录一律跳转登录页（白名单见 monitor/security.py）
    'monitor.security.AuthRequiredMiddleware',
    # 自研请求计时中间件：采集耗时 / SQL / 慢查询 / 调用链 span，写入 RequestMetric
    'monitor.middleware.RequestTimingMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'monitor.context_processors.obs_flags',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ---------------- 数据库 ----------------
# 默认 SQLite（WAL，零配置演示）；生产设置 OBS_DATABASE_URL 切换 PostgreSQL，
# 如 postgres://obs:pass@db:5432/obs（见 docker-compose.yml / requirements-prod.txt）
_db_url = os.environ.get('OBS_DATABASE_URL')
if _db_url:
    from urllib.parse import urlparse

    _u = urlparse(_db_url)
    if _u.scheme not in ('postgres', 'postgresql'):
        raise ImproperlyConfigured(
            f'OBS_DATABASE_URL 仅支持 postgres:// 形式，当前 scheme: {_u.scheme!r}')
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': _u.path.lstrip('/'),
            'USER': _u.username or '',
            'PASSWORD': _u.password or '',
            'HOST': _u.hostname or '',
            'PORT': _u.port or 5432,
            'CONN_MAX_AGE': 60,   # 常驻连接，避免每请求握手
            'CONN_HEALTH_CHECKS': True,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': DATA_DIR / 'db.sqlite3',
            'OPTIONS': {'timeout': 20},  # 后台采集线程与请求线程并发写入时的等待余量
        }
    }

# ---------------- 国际化 ----------------
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

# ---------------- 静态文件 ----------------
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static'] if (BASE_DIR / 'static').exists() else []
# collectstatic 目标（容器/反代部署用；开发 runserver 不需要）
STATIC_ROOT = Path(os.environ.get('OBS_STATIC_ROOT') or (BASE_DIR / 'staticfiles'))
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------- 日志 ----------------
# 控制台日志带 [trace=...] 后缀：请求上下文中的日志可凭 trace_id 到 APM 查调用链
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'trace': {'()': 'monitor.tracing.TraceLogFilter'},
    },
    'formatters': {
        'obs': {
            '()': 'monitor.tracing.TraceFormatter',
            'format': '%(levelname)s %(name)s %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'filters': ['trace'],
            'formatter': 'obs',
        },
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
}

# ---------------- 可观测平台配置 ----------------
OBSERVABILITY = {
    'SLOW_QUERY_MS': 100,       # 慢查询阈值：单条 SQL 超过 100ms 记为慢查询
    'SLOW_REQUEST_MS': 500,     # 慢请求阈值：请求总耗时超过 500ms 记为慢请求
    'CPU_WARN_PERCENT': 80,     # CPU 告警参考线
    'TREND_MINUTES': 30,        # 趋势图默认统计最近 N 分钟
    'HOST_INTERVAL_SEC': 15,    # 主机指标采集周期（秒）
    'ALERT_INTERVAL_SEC': 30,   # 告警策略评估周期（秒）
    'ALERT_WINDOW_MIN': 5,      # 告警评估窗口（分钟）：取窗口内均值与阈值比较
    'RETENTION_DAYS': 7,        # 采集数据保留天数（后台线程定期清理）
    'SKIP_PATHS': (             # 平台自身路径不采集，避免面板请求污染业务数据
        '/monitor/', '/metrics', '/static/', '/admin/', '/api/',
        '/rum/', '/logs/', '/alerts/', '/analytics/', '/hosts/',
        '/integration/', '/diagnose/', '/favicon',
    ),
    # 主机数据接入令牌：优先环境变量 OBS_INGEST_TOKEN；
    # 否则首次启动自动生成并缓存到 BASE_DIR/ingest_token.txt（勿提交、勿泄露）。
    # 所有上报端点（主机 Agent / 日志 / 自定义指标 / RUM beacon / /metrics）均要求
    # "已登录会话" 或 "携带令牌"（X-OBS-Token 头 / Bearer / ?token=）二者其一。
    'INGEST_TOKEN': _ingest_token(),
    # 是否在本机运行 psutil 采集线程（平台所在机器默认开启）
    'LOCAL_COLLECTOR': os.environ.get('OBS_LOCAL_COLLECTOR', '1') != '0',
    # 登录页是否直接展示只读演示账号密码（对外演示用）。
    # 默认跟随 DEBUG；生产部署要公开演示入口时显式设 OBS_SHOW_DEMO_ACCOUNT=1，
    # 前提是那个账号确实只读（演示组的写操作在 security.py 里一律 403）。
    'SHOW_DEMO_ACCOUNT': os.environ.get('OBS_SHOW_DEMO_ACCOUNT',
                                        '1' if os.environ.get('DJANGO_DEBUG', '1') == '1' else '0') == '1',
    # ---- 请求指标批量缓冲（monitor/buffer.py）----
    # 请求线程只入队，后台线程按"攒满 N 条或到期 T 秒"bulk_create 批量落库。
    # 关闭（或管理命令/测试进程）时退化为同步写库，保持"请求结束即落库"的语义。
    'METRIC_BUFFER_BATCH_SIZE': max(1, int(os.environ.get('OBS_METRIC_BUFFER_BATCH', '200'))),
    'METRIC_BUFFER_FLUSH_SEC': max(0.1, float(os.environ.get('OBS_METRIC_BUFFER_FLUSH_SEC', '1'))),
    'METRIC_BUFFER_QUEUE_SIZE': max(1, int(os.environ.get('OBS_METRIC_BUFFER_QUEUE_SIZE', '20000'))),
    'METRIC_BUFFER_ENABLED': os.environ.get('OBS_METRIC_BUFFER', '1') != '0',
}

# 向后兼容旧命名（diagnoser / services 里引用 MONITORING）
MONITORING = OBSERVABILITY

# ---------------- 后台采集线程 ----------------
# runserver 下仅子进程启动一次；migrate / shell 等命令不启动。
# 设置环境变量 OBS_DISABLE_WORKERS=1 可强制关闭。
WORKER_START_EXCLUDE_CMDS = {
    'migrate', 'makemigrations', 'collectstatic', 'shell', 'test',
    'init_data', 'run_benchmark', 'check', 'createsuperuser', 'inspectdb',
}

# 管理命令与测试进程里请求指标走同步写库：这些场景必须"写完立刻读得到"，
# 且 migrate 期间表可能还不存在，缓冲线程反而添乱。
_current_cmd = sys.argv[1] if len(sys.argv) > 1 else ''
if _current_cmd in WORKER_START_EXCLUDE_CMDS:
    OBSERVABILITY['METRIC_BUFFER_ENABLED'] = False

# ---------------- 可选组件（try-import，未安装自动跳过） ----------------
# 安装 django-debug-toolbar 后无需改代码即可自动启用
try:
    import debug_toolbar  # noqa: F401
    INSTALLED_APPS += ['debug_toolbar']
    MIDDLEWARE.insert(0, 'debug_toolbar.middleware.DebugToolbarMiddleware')
    INTERNAL_IPS = ['127.0.0.1', 'localhost']
    DEBUG_TOOLBAR_CONFIG = {'SHOW_TOOLBAR_CALLBACK': lambda request: DEBUG}
except ImportError:
    pass

# 安装 silk 后无需改代码即可自动启用
try:
    import silk  # noqa: F401
    INSTALLED_APPS += ['silk']
    MIDDLEWARE.insert(0, 'silk.middleware.SilkyMiddleware')
    SILKY_PYTHON_PROFILER = True
    SILKY_INTERCEPT_PERCENT = 100
except ImportError:
    pass
