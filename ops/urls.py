from django.urls import path

from . import views

app_name = 'ops'

urlpatterns = [
    # 拨测
    path('probe/', views.probe_page, name='probe'),
    path('probe/create/', views.probe_create, name='probe_create'),
    path('probe/<int:pk>/toggle/', views.probe_toggle, name='probe_toggle'),
    path('probe/<int:pk>/delete/', views.probe_delete, name='probe_delete'),
    path('probe/<int:pk>/run/', views.probe_run_now, name='probe_run'),
    path('probe/<int:pk>/', views.probe_detail, name='probe_detail'),
    path('probe/<int:pk>/api/', views.api_probe_detail, name='probe_api'),
    # 故障事件
    path('incidents/', views.incidents_page, name='incidents'),
    path('incidents/<int:pk>/', views.incident_detail, name='incident_detail'),
    path('incidents/<int:pk>/update/', views.incident_update, name='incident_update'),
    path('incidents/<int:pk>/close/', views.incident_close, name='incident_close'),
    path('incidents/<int:pk>/reopen/', views.incident_reopen, name='incident_reopen'),
    path('incidents/<int:pk>/export/', views.incident_export, name='incident_export'),
    # 巡检
    path('inspection/', views.inspection_page, name='inspection'),
    path('inspection/run/', views.inspection_run_now, name='inspection_run'),
    path('inspection/<int:pk>/', views.inspection_detail, name='inspection_detail'),
    path('inspection/<int:pk>/export/', views.inspection_export, name='inspection_export'),
    # SLO
    path('slo/', views.slo_page, name='slo'),
    path('slo/create/', views.slo_create, name='slo_create'),
    path('slo/<int:pk>/toggle/', views.slo_toggle, name='slo_toggle'),
    path('slo/<int:pk>/delete/', views.slo_delete, name='slo_delete'),
    path('slo/<int:pk>/export/', views.slo_export, name='slo_export'),
    # 资产
    path('assets/', views.assets_page, name='assets'),
    path('assets/save/', views.asset_save, name='asset_save'),
    path('assets/<int:pk>/delete/', views.asset_delete, name='asset_delete'),
    # 自愈
    path('heal/', views.heal_page, name='heal'),
    path('heal/create/', views.heal_create, name='heal_create'),
    path('heal/<int:pk>/toggle/', views.heal_toggle, name='heal_toggle'),
    path('heal/<int:pk>/delete/', views.heal_delete, name='heal_delete'),
    path('heal/<int:pk>/test/', views.heal_test, name='heal_test'),
    # 通知渠道
    path('notify/', views.notify_page, name='notify'),
    path('notify/save/', views.notify_save, name='notify_save'),
    path('notify/test/', views.notify_test, name='notify_test'),
    # 审计
    path('audit/', views.audit_page, name='audit'),
]
