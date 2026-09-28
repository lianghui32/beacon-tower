"""
ops/audit.py — 操作审计：关键变更埋点 + 登录信号
"""
import logging

from django.contrib.auth.signals import (
    user_logged_in, user_logged_out, user_login_failed,
)
from django.dispatch import receiver

from monitor.middleware import TRUST_XFF

from .models import AuditLog

logger = logging.getLogger(__name__)


def _client_ip(request):
    """审计 IP：默认只信 REMOTE_ADDR（审计日志必须可信，XFF 可被客户端伪造）；
    确认部署在可信反代后（OBS_TRUST_XFORWARDED_FOR=1）才解析 XFF"""
    if request is None:
        return ''
    if TRUST_XFF:
        fwd = request.META.get('HTTP_X_FORWARDED_FOR')
        if fwd:
            return fwd.split(',')[-1].strip()[:60]  # 取最后一跳（最接近服务器的代理写入）
    return (request.META.get('REMOTE_ADDR') or '')[:60]


def audit(request, action, target='', detail=''):
    """在任意变更点调用：audit(request, '创建告警策略', policy.name)"""
    try:
        user = ''
        if request is not None and getattr(request, 'user', None) is not None:
            user = request.user.username if request.user.is_authenticated else '匿名'
        AuditLog.objects.create(user=user[:60], action=action[:60],
                                target=str(target)[:150], detail=str(detail)[:1000],
                                ip=_client_ip(request))
    except Exception:
        # 审计写入失败不能无声无息——它本身就是安全日志
        logger.exception('审计写入失败: %s %s', action, target)


def audit_system(action, target='', detail=''):
    """后台线程/引擎产生的动作（无 request）"""
    audit(None, action, target, detail)


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    # 成功登录清零该用户名与本 IP 的失败计数（避免"剩 1 次额度时一次失误即锁"）
    try:
        from django.core.cache import cache
        cache.delete(f'obs-login-fail:u:{(user.username or "").strip().lower()[:40]}')
        cache.delete(f'obs-login-fail:ip:{_client_ip(request) or "unknown"}')
    except Exception:
        logger.exception('登录成功清零计数失败')
    audit(request, '登录成功', user.username)


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    if user and getattr(user, 'is_authenticated', False):
        audit(request, '退出登录', user.username)


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request, **kwargs):
    audit(request, '登录失败', credentials.get('username', ''), '用户名或密码错误')
    # 暴力破解计数：按 用户名+IP 记失败次数（10 分钟窗口），login_page 读取并限流
    try:
        from django.core.cache import cache
        username = (credentials.get('username') or '').strip().lower()[:40]
        ip = _client_ip(request) or 'unknown'
        for key in (f'obs-login-fail:u:{username}', f'obs-login-fail:ip:{ip}'):
            try:
                cache.add(key, 0, 600)
                cache.incr(key)
            except Exception:
                pass
    except Exception:
        logger.exception('登录失败计数异常')
