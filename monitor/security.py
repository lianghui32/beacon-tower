"""
monitor/security.py — 平台访问控制

1. AuthRequiredMiddleware：整站登录门禁（自用系统），白名单外一律跳转登录页；
2. require_ingest 装饰器：上报类接口（主机 Agent / 日志 / 自定义指标 / RUM beacon）
   要求"已登录会话"或"携带接入令牌"（X-OBS-Token 头 / Authorization: Bearer / ?token=）；
   /metrics 供 Prometheus 抓取，同样接受 Bearer 令牌。

接入令牌来自 settings.OBSERVABILITY['INGEST_TOKEN']（环境变量 OBS_INGEST_TOKEN 或
首次启动自动生成的 ingest_token.txt），在"接入中心"页面可查看完整用法。

安全设计：
- 令牌比较统一使用 secrets.compare_digest（防时序侧信道）；
- 令牌只放行 上报 API 与 /metrics，不能用于访问页面（防止令牌持有者越权读面板）；
- ?token= 查询串仅为兼容旧 Agent/文档保留，推荐一律用请求头（避免进入访问日志/Referer）；
- 演示账号组判定结果缓存在 user 对象上，避免每个请求重复查库。
"""
import secrets
import time
from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse

# 无需登录即可访问的路径前缀
PUBLIC_PATHS = (
    '/accounts/login/',
    '/static/',
    '/favicon.ico',
    '/admin/login/',
    '/api/health/',      # 健康检查探活端点（仅返回 ok + 时间，无敏感信息）
)

# API / 上报类路径：未认证时直接返回 401（而不是 302 跳登录页）
API_PATHS = (
    '/api/',
    '/rum/beacon/',
    '/logs/api/',
    '/hosts/api/',
    '/monitor/api/',
    '/cleaner/api/',     # 清理加速 AJAX 接口（匿名 401 JSON，非 302 页面跳转）
)

# 接入令牌可访问的路径：仅"写数据"的上报端点 + Prometheus 抓取。
# 读类数据 API（日志检索/导出、APM 查询等）一律只认登录会话——
# 令牌分发在各 Agent 主机上，泄露半径必须限制为"只能写监控数据"。
TOKEN_PATHS = (
    '/api/ingest/',      # 自定义指标 / 主机 Agent 上报
    '/logs/api/ingest/', # 日志接入
    '/rum/beacon/',      # 前端 RUM 上报
)
TOKEN_EXACT_PATHS = ('/metrics',)

# ---------------- 演示模式（只读展示账号） ----------------
DEMO_GROUP_NAME = '演示访客'

# 演示账号禁止访问的路径前缀：管理入口 / 含令牌或密钥的页面 / 会写数据的页面
DEMO_BLOCKED_PREFIXES = (
    '/ops/notify/',      # 通知渠道配置（含 SMTP 密码）
    '/ops/audit/',       # 操作审计
    '/cleaner/',         # 清理加速（高危维护操作）
    '/ops/heal/',        # 自愈动作管理
    '/ops/probe/',       # 拨测管理（任务 URL 可能含接入令牌）
    '/alerts/policies/', # 告警策略管理
    '/integration/',     # 接入中心（含接入令牌明文）
    '/forum/chaos/',     # 故障演练（会写数据）
    '/metrics',          # Prometheus 全量指标
    '/admin/',           # 后台
)

LOGOUT_PATH = '/accounts/logout/'


def is_demo_user(user):
    """判断是否演示账号：属于"演示访客"组即可（结果缓存在 user 对象上）"""
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    cached = getattr(user, '_obs_is_demo', None)
    if cached is not None:
        return cached
    try:
        result = user.groups.filter(name=DEMO_GROUP_NAME).exists()
    except Exception:
        # 判定失败时按"非演示"放行会绕过写保护，这里保守地按演示账号处理（fail-closed）
        return True
    try:
        user._obs_is_demo = result
    except AttributeError:
        pass
    return result


def _demo_403(request):
    from django.shortcuts import render
    return render(request, 'demo_403.html', status=403)


def _token_ok(request):
    """校验请求携带的接入令牌（常量时间比较，防时序侧信道）"""
    expected = settings.OBSERVABILITY.get('INGEST_TOKEN') or ''
    if not expected:
        return False
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if auth.startswith('Bearer ') and secrets.compare_digest(
            auth[len('Bearer '):].strip(), expected):
        return True
    if secrets.compare_digest(request.headers.get('X-OBS-Token', ''), expected):
        return True
    # 兼容旧文档/旧 Agent 的 ?token= 传递方式（推荐改用请求头，避免泄露到日志）
    if secrets.compare_digest(request.GET.get('token', ''), expected):
        return True
    return False


def _path_is_api(path):
    """判断路径是否属于 API 面（决定匿名请求返回 401 JSON 还是跳登录页）"""
    if any(path.startswith(p) for p in API_PATHS):
        return True
    return any(path == p or path.startswith(p + '/')
               for p in TOKEN_EXACT_PATHS)


def _path_is_token_api(path):
    """判断路径是否属于令牌可访问的上报端点（精确白名单，防令牌读数据）"""
    if any(path == p or path.startswith(p + '/')
           for p in TOKEN_EXACT_PATHS):
        return True
    return any(path.startswith(p) for p in TOKEN_PATHS)


def ingest_allowed(request):
    """上报接口的准入判断：完整账号会话 或 令牌，二者其一。

    演示账号的会话不算数——它只有查询权限，不能向上报接口写数据。
    """
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and not is_demo_user(user):
        return True
    return _token_ok(request)


def require_ingest(view):
    """上报类接口装饰器：未登录且未携带令牌 -> 401"""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not ingest_allowed(request):
            return JsonResponse(
                {'ok': False,
                 'error': 'unauthorized: 需要登录会话，或携带接入令牌'
                          '（X-OBS-Token 头 / Authorization: Bearer / ?token=）'},
                status=401,
            )
        return view(request, *args, **kwargs)
    return wrapped


# ---------------- 轻量速率限制（防令牌/账号被用来灌爆采集库） ----------------

def rate_limit(prefix, rate, per=60):
    """按客户端 IP 的固定窗口限速装饰器（进程内 cache 实现）。

    rate 次 / per 秒，超限返回 429。LocMemCache 为进程级，
    多进程部署时各进程独立计数（演示场景足够；生产可用 Redis cache）。
    OBS_RATE_LIMIT_SCALE 可整体放宽倍率（容量压测用，默认 1 不放宽）。
    """
    import os

    from django.core.cache import cache

    scale = max(1, int(os.environ.get('OBS_RATE_LIMIT_SCALE', '1') or 1))
    rate = rate * scale

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            ip = (request.META.get('REMOTE_ADDR') or 'unknown')[:40]
            key = f'obs-rl:{prefix}:{ip}'
            now = int(time.time())
            window = now // per
            bucket_key = f'{key}:{window}'
            try:
                count = cache.get(bucket_key)
                if count is None:
                    cache.add(bucket_key, 1, per + 1)
                    count = 1
                else:
                    count = cache.incr(bucket_key)
            except Exception:
                count = 0  # cache 不可用时不阻断业务
            if count > rate:
                return JsonResponse(
                    {'ok': False, 'error': f'rate limited: 最多 {rate} 次/{per} 秒'},
                    status=429,
                )
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


class AuthRequiredMiddleware:
    """整站登录门禁：未登录访问白名单之外的任何页面都跳转登录页

    /metrics 与上报 API 例外：无会话但携带有效令牌时放行（供 Prometheus / Agent）。
    令牌不放行任何页面路径——页面访问只认登录会话。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            # ---- 演示模式：只读展示账号 ----
            if is_demo_user(user):
                # 写保护：除退出登录外，任何非 GET 请求一律 403
                if request.method not in ('GET', 'HEAD', 'OPTIONS') \
                        and path != LOGOUT_PATH:
                    return _demo_403(request)
                # 敏感页面拦截：管理入口 / 含令牌密钥的页面
                if any(path.startswith(p) for p in DEMO_BLOCKED_PREFIXES):
                    return _demo_403(request)
                return self.get_response(request)
            return self.get_response(request)
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            return self.get_response(request)
        # 携带有效令牌且目标确为上报端点时放行（读类 API 与页面路径不认令牌）
        if _path_is_token_api(path) and _token_ok(request):
            return self.get_response(request)
        # API / 上报类路径：返回 401 JSON；页面路径：跳转登录页
        if _path_is_api(path):
            return JsonResponse(
                {'ok': False,
                 'error': 'unauthorized: 需要登录会话，或携带接入令牌'
                          '（X-OBS-Token 头 / Authorization: Bearer / ?token=）'},
                status=401,
            )
        return redirect_to_login(request.get_full_path())
