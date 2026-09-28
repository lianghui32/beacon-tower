from django.urls import path

from . import views

app_name = 'alerts'

urlpatterns = [
    path('policies/', views.policies_page, name='policies'),
    path('policies/create/', views.policy_create, name='create'),
    path('policies/<int:pk>/toggle/', views.policy_toggle, name='toggle'),
    path('policies/<int:pk>/delete/', views.policy_delete, name='delete'),
    path('policies/<int:pk>/silence/', views.policy_silence, name='silence'),
    path('policies/<int:pk>/unsilence/', views.policy_unsilence, name='unsilence'),
    path('policies/evaluate/', views.policy_evaluate_now, name='evaluate'),
    path('events/', views.events_page, name='events'),
    path('events/<int:pk>/ack/', views.event_ack, name='ack'),
    path('events/<int:pk>/note/', views.event_note, name='note'),
    path('notifications/', views.notifications_page, name='notifications'),
]
