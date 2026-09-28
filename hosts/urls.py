from django.urls import path

from . import views

app_name = 'hosts'

urlpatterns = [
    path('', views.host_page, name='page'),
    path('api/', views.api_host, name='api'),
    path('api/sample/', views.api_sample_now, name='sample'),
    # 远程主机 Agent 上报（令牌保护）
    path('api/ingest/', views.api_ingest_host, name='ingest'),
]
