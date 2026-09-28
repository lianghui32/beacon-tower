from django.apps import AppConfig


class LoghubConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'loghub'
    verbose_name = '日志服务'

    def ready(self):
        from .handler import attach_log_handler
        attach_log_handler()
