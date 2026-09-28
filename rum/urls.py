from django.urls import path

from . import views

app_name = 'rum'

urlpatterns = [
    # 页面
    path('', views.rum_overview_page, name='overview'),
    path('perf/', views.rum_perf_page, name='perf'),
    path('errors/', views.rum_errors_page, name='errors'),
    path('api/', views.rum_api_page, name='api'),
    path('resources/', views.rum_resources_page, name='resources'),
    path('custom/', views.rum_custom_page, name='custom'),
    # 数据接口
    path('api/overview/', views.api_overview, name='api_overview'),
    path('api/perf/', views.api_perf, name='api_perf'),
    path('api/errors/', views.api_errors, name='api_errors'),
    path('api/calls/', views.api_calls, name='api_calls'),
    path('api/resources/', views.api_resources, name='api_resources'),
    path('api/custom/', views.api_custom, name='api_custom'),
    # 数据接收（浏览器 SDK 上报）
    path('beacon/', views.beacon, name='beacon'),
]
