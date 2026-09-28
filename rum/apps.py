from django.apps import AppConfig


class RumConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'rum'
    verbose_name = '前端性能监控 RUM'
