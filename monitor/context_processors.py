"""
monitor/context_processors.py — 给模板注入角色标识（演示模式徽章 / 侧边栏裁剪）

is_demo_user 的结果缓存在 user 对象上，与 AuthRequiredMiddleware 共用，
每个请求最多查一次库。
"""
from .security import is_demo_user


def obs_flags(request):
    return {'is_demo': is_demo_user(getattr(request, 'user', None))}
