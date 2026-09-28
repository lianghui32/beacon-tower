from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _set_sqlite_pragma(sender, connection, **kwargs):
    """SQLite 开启 WAL：后台采集线程与请求线程并发读写更稳"""
    if connection.vendor != 'sqlite':
        return
    try:
        cursor = connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        cursor.close()
    except Exception:
        pass


class MonitorConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'monitor'
    verbose_name = '全栈可观测运维平台'

    def ready(self):
        connection_created.connect(_set_sqlite_pragma, dispatch_uid='obs-sqlite-wal')
        from .workers import maybe_start_workers
        maybe_start_workers()
