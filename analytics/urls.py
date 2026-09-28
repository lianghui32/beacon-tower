from django.urls import path

from . import views

app_name = 'analytics'

urlpatterns = [
    path('', views.analytics_page, name='page'),
    path('api/anomaly/', views.api_anomaly, name='anomaly'),
    path('api/forecast/', views.api_forecast, name='forecast'),
    path('api/correlation/', views.api_correlation, name='correlation'),
    path('api/logmining/', views.api_logmining, name='logmining'),
    path('report/', views.report_page, name='report'),
    path('report/export/', views.report_export, name='report_export'),
    path('geo/', views.geo_page, name='geo'),
    path('api/geo/', views.api_geo, name='api_geo'),
]
