from django.urls import path

from . import views

app_name = 'forum'

urlpatterns = [
    path('problem/', views.topic_list_problem, name='problem'),
    path('optimized/', views.topic_list_optimized, name='optimized'),
    path('topic/<int:pk>/', views.topic_detail, name='detail'),
    # 故障演练（给观测平台制造真实数据）
    path('chaos/', views.chaos_page, name='chaos'),
    path('chaos/slow/', views.chaos_slow, name='chaos_slow'),
    path('chaos/nplus1/', views.chaos_nplus1, name='chaos_nplus1'),
    path('chaos/error/', views.chaos_error, name='chaos_error'),
    path('chaos/log/', views.chaos_log_storm, name='chaos_log'),
    path('chaos/cpu/', views.chaos_cpu, name='chaos_cpu'),
    path('chaos/all/', views.chaos_all, name='chaos_all'),
]
