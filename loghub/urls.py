from django.urls import path

from . import views

app_name = 'loghub'

urlpatterns = [
    path('', views.log_page, name='page'),
    path('api/search/', views.api_search, name='search'),
    path('api/export/', views.api_export, name='export'),
    path('api/ingest/', views.api_ingest, name='ingest'),
]
