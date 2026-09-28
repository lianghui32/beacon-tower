"""config 包初始化：注册 SQLite 连接级 PRAGMA（WAL + busy_timeout）。

平台有 4 条后台采集/评估线程与请求线程并发写 SQLite，默认 journal 模式（delete）
下极易 "database is locked"；WAL 允许读写并发，busy_timeout 提供写锁等待余量。
"""
from django.db.backends.signals import connection_created
from django.dispatch import receiver


@receiver(connection_created)
def _sqlite_pragmas(sender, connection, **kwargs):
    if connection.vendor != 'sqlite':
        return
    with connection.cursor() as cursor:
        # WAL：读写不互斥；NORMAL：掉电最多丢最后一个事务，性能远好于 FULL
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        cursor.execute('PRAGMA busy_timeout=20000;')
        cursor.execute('PRAGMA foreign_keys=ON;')
