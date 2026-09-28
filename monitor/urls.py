from django.urls import path

from . import views

app_name = 'monitor'

urlpatterns = [
    # 监控总览
    path('overview/', views.overview, name='overview'),
    path('api/overview/', views.api_overview, name='api_overview'),
    # 自定义大盘
    path('dashboard/', views.dashboard_page, name='dashboard'),
    path('dashboard/add/', views.dashboard_add, name='dashboard_add'),
    path('dashboard/<int:pk>/delete/', views.dashboard_delete, name='dashboard_delete'),
    path('dashboard/<int:pk>/move/<str:direction>/', views.dashboard_move, name='dashboard_move'),
    path('api/metric/', views.api_metric, name='api_metric'),
    # APM
    path('apm/', views.apm_page, name='apm'),
    path('apm/api/transactions/', views.api_apm_transactions, name='api_transactions'),
    path('apm/api/transactions/export/', views.apm_transactions_export, name='apm_export'),
    path('apm/api/traces/', views.api_apm_traces, name='api_traces'),
    path('apm/trace/<str:trace_id>/', views.trace_page, name='trace'),
    path('apm/database/', views.apm_database_page, name='database'),
    # 性能诊断
    path('diagnose/', views.diagnose_view, name='diagnose'),
    path('diagnose/export/', views.diagnose_export, name='diagnose_export'),
    # 基准压测
    path('benchmark/', views.benchmark_view, name='benchmark'),
]
