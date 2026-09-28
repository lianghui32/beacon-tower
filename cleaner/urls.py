from django.urls import path

from . import views

app_name = 'cleaner'

urlpatterns = [
    path('', views.cleaner_page, name='page'),
    path('api/estimate/<str:item>/', views.api_estimate, name='estimate'),
    path('api/clean/<str:item>/', views.api_clean, name='clean'),
    path('api/disk/', views.api_disk_scan, name='disk'),
    path('api/processes/', views.api_processes, name='processes'),
    path('api/trim/', views.api_trim_memory, name='trim'),
]
