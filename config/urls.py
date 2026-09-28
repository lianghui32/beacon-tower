"""config/urls.py — 全站路由"""
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path
from django.views.generic import RedirectView

from monitor import views as monitor_views
from hosts import views as hosts_views


def favicon(request):
    """空 favicon：避免浏览器自动请求产生 404 日志噪音"""
    return HttpResponse(status=204)


urlpatterns = [
    path('admin/', admin.site.urls),
    # 登录页：带只读演示账号展示（读取 demo_credentials.txt）
    path('accounts/login/', monitor_views.login_page, name='login'),
    # 登录 / 注销 / 改密（Django 内置 auth 视图，整站门禁的白名单路径）
    path('accounts/', include('django.contrib.auth.urls')),
    path('favicon.ico', favicon),
    # 监控总览（首页）
    path('', monitor_views.overview, name='home'),
    # 监控平台：APM / 自定义大盘 / 压测 / 诊断 / Prometheus 指标
    path('monitor/', include('monitor.urls')),
    path('metrics', monitor_views.metrics_endpoint, name='metrics'),
    # 接入中心
    path('integration/', monitor_views.integration_page, name='integration'),
    # 上报 OpenAPI（自定义指标 / 远程主机 Agent）
    path('api/ingest/metrics/', monitor_views.custom_metrics_ingest, name='metrics_ingest'),
    path('api/ingest/host/', hosts_views.api_ingest_host, name='host_ingest'),
    # 健康检查探活端点（公开：供拨测/负载均衡，仅返回 ok + 时间）
    path('api/health/', monitor_views.api_health, name='health'),
    # 各观测域
    path('hosts/', include('hosts.urls')),
    path('rum/', include('rum.urls')),
    path('logs/', include('loghub.urls')),
    path('alerts/', include('alerts.urls')),
    path('analytics/', include('analytics.urls')),
    # 运维中心（拨测 / 故障单 / 巡检 / SLO / 资产 / 自愈 / 通知 / 审计）
    path('ops/', include('ops.urls')),
    # 清理加速中心（磁盘分析 / 垃圾清理 / 内存整理）
    path('cleaner/', include('cleaner.urls')),
    # 旧版路径兼容重定向
    path('diagnose/', RedirectView.as_view(url='/monitor/diagnose/', permanent=False)),
    path('benchmark/', RedirectView.as_view(url='/monitor/benchmark/', permanent=False)),
    # 演示目标应用（含性能问题的论坛 + 故障演练）
    path('forum/', include('forum.urls')),
]
