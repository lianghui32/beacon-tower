"""
cleaner/tests.py — 清理中心的"删除边界"回归测试

清理中心是全平台唯一会真删文件的地方，风险不对称：少删只是没腾出空间，
多删就是数据事故。这里锁死四条边界：
1. 遍历绝不穿过符号链接 / Windows Junction——Junction 能把删除引到白名单根之外，
   而 os.path.islink 认不出它，必须靠 REPARSE_POINT 属性位/重解析标记判定；
2. 只删超过龄期的文件，别人正在用的新文件不能碰；
3. 遍历有限时限量，不能一路扫到整块盘；
4. 未知清理项一律拒绝，不"顺手全清"。

所有用例都在 tempfile.mkdtemp() 的沙箱里跑，绝不碰沙箱外的路径。
"""
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.test import TestCase, TransactionTestCase

from cleaner import services

REPARSE_ATTR = 0x400                    # FILE_ATTRIBUTE_REPARSE_POINT
# Junction 的重解析标记：标准库 os.path.isjunction 就是比这个值（不是 0x3！）
MOUNT_POINT_TAG = getattr(stat, 'IO_REPARSE_TAG_MOUNT_POINT', 0xA0000003)


def fake_junction(target_path, reparse_tag=MOUNT_POINT_TAG, attrs=REPARSE_ATTR):
    """把指定路径伪装成 Junction：只改这一个路径的 stat/lstat 结果，其余走真实调用。

    lstat 与 stat 都要改：项目的 _is_reparse 读 lstat，而标准库 os.path.isjunction
    读的是 stat——两边都给假数据，才能顺带验证"伪装确实像真的 Junction"。
    刻意不 patch _is_reparse 本身——那样测的是"我 patch 了什么"，而不是生产判定逻辑。
    """
    real_lstat, real_stat = os.lstat, os.stat
    wanted = os.path.abspath(str(target_path))

    class _FakeSt:
        st_file_attributes = attrs
        st_reparse_tag = reparse_tag
        st_size = 0
        st_mode = 0o40777

    def _is_target(path):
        return os.path.abspath(str(path)) == wanted

    def fake_lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        return _FakeSt() if _is_target(path) else st

    def fake_stat(path, *args, **kwargs):
        kwargs.pop('follow_symlinks', None)
        st = real_stat(path, *args, **kwargs)
        return _FakeSt() if _is_target(path) else st

    return _patch_chain(
        mock.patch.object(os, 'lstat', fake_lstat),
        mock.patch.object(os, 'stat', fake_stat),
    )


class _patch_chain:
    """同时进入多个 mock patch 的极简上下文管理器"""

    def __init__(self, *patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.__enter__()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)
        return False


class ReparseDetectionTests(TestCase):
    def test_plain_file_and_dir_are_not_reparse(self):
        d = Path(tempfile.mkdtemp(prefix='obs-clean-'))
        try:
            f = d / 'a.txt'
            f.write_text('x', encoding='utf-8')
            self.assertFalse(services._is_reparse(f))
            self.assertFalse(services._is_reparse(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_junction_is_detected(self):
        """os.path.islink 对 Junction 返回 False，识别全靠重解析标记"""
        d = Path(tempfile.mkdtemp(prefix='obs-clean-'))
        target = d / 'evil'
        target.mkdir()
        try:
            if os.path.islink(str(target)):  # 极少见，防御一下
                self.skipTest('测试目录本身成了链接')
            with fake_junction(target):
                self.assertTrue(services._is_reparse(target))
                if sys.platform == 'win32':
                    # 只在 Windows 上交叉验证标准库：POSIX 没有 Junction 概念，
                    # posixpath.isjunction 是恒返回 False 的桩，拿它断言必挂（CI 实测）
                    self.assertTrue(os.path.isjunction(str(target)),
                                    '伪装数据本身应被标准库认出是 Junction')
            self.assertFalse(services._is_reparse(target), '撤掉伪装后应判为普通目录')
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_reparse_bit_alone_is_enough_on_old_python(self):
        """Python < 3.12 没有 os.path.isjunction，只剩属性位这条路——必须仍然拦住"""
        d = Path(tempfile.mkdtemp(prefix='obs-clean-'))
        target = d / 'evil'
        target.mkdir()
        try:
            with fake_junction(target, reparse_tag=0), \
                    mock.patch.object(os.path, 'isjunction', None, create=True):
                self.assertTrue(services._is_reparse(target))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_lstat_failure_is_fail_closed(self):
        """判定不了就当作危险链接跳过——宁可少删，不能误删"""
        with mock.patch.object(os, 'lstat', side_effect=OSError('gone')):
            self.assertTrue(services._is_reparse(Path('whatever')))

    def test_real_symlink_detected_when_os_allows(self):
        d = Path(tempfile.mkdtemp(prefix='obs-clean-'))
        try:
            victim = d / 'real.txt'
            victim.write_text('x', encoding='utf-8')
            link = d / 'link.txt'
            try:
                os.symlink(str(victim), str(link))
            except (OSError, NotImplementedError):
                self.skipTest('当前系统/权限不允许创建符号链接（Windows 非管理员常见）')
            self.assertTrue(services._is_reparse(link))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class BoundedWalkTests(TestCase):
    """有界遍历：剪链接、限量、限时"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='obs-clean-'))
        for name in ('a.txt', 'b.txt', 'c.txt'):
            (self.root / name).write_text('x' * 10, encoding='utf-8')
        sub = self.root / 'sub'
        sub.mkdir()
        (sub / 'd.txt').write_text('x' * 10, encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_yields_all_plain_files(self):
        found = sorted(os.path.basename(p) for p, _ in services._walk_bounded(self.root))
        self.assertEqual(found, ['a.txt', 'b.txt', 'c.txt', 'd.txt'])

    def test_junction_subtree_is_not_traversed(self):
        """逃逸演示：sub 伪装成指向白名单外的 Junction，它下面的文件绝不能出现在结果里"""
        with fake_junction(self.root / 'sub'):
            found = sorted(os.path.basename(p) for p, _ in services._walk_bounded(self.root))
        self.assertEqual(found, ['a.txt', 'b.txt', 'c.txt'],
                         'Junction 子树被穿过了——删除会打到白名单根之外')

    def test_max_files_caps_the_walk(self):
        got = [p for p, _ in services._walk_bounded(self.root, max_files=2)]
        self.assertEqual(len(got), 2, '限量是防"扫到整块盘"的硬闸，不能超')

    def test_deadline_stops_the_walk(self):
        """超时必须立即返回，而不是把整棵目录树走完"""
        got = list(services._walk_bounded(self.root, deadline=time.monotonic() - 1))
        self.assertEqual(got, [])


class CleanSystemTempTests(TestCase):
    """临时目录清理：龄期是唯一的删除依据"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='obs-clean-'))
        self._patch = mock.patch.object(services, '_temp_root', lambda: self.root)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _make(self, name, age_hours, size=50):
        p = self.root / name
        p.write_text('x' * size, encoding='utf-8')
        t = time.time() - age_hours * 3600
        os.utime(p, (t, t))
        return p

    def test_only_aged_out_files_are_deleted(self):
        old = self._make('old.txt', 48)
        fresh = self._make('fresh.txt', 1)
        result = services._clean_system_temp()
        self.assertFalse(old.exists(), '超过 24h 的临时文件应被回收')
        self.assertTrue(fresh.exists(), '24h 内的文件可能是别的程序正在写的，绝不能删')
        self.assertEqual(result['files'], 1)
        self.assertEqual(result['freed_bytes'], 50)

    def test_junction_target_survives(self):
        """伪装成 Junction 的目录不能被子树删除——那正是白名单逃逸事故"""
        evil = self.root / 'evil'
        evil.mkdir()
        inside = evil / 'precious.txt'
        inside.write_text('do-not-touch' * 10, encoding='utf-8')
        old = self._make('old.txt', 48)
        with fake_junction(evil):
            services._clean_system_temp()
        self.assertTrue(inside.exists(), 'Junction 后面的文件被删了——删除越界')
        self.assertTrue(evil.exists(), 'Junction 目录本身也不该被当成空目录删掉')
        self.assertFalse(old.exists(), '正常过期文件仍应被清理')


class ItemDispatchTests(TestCase):
    """未知清理项一律拒绝：不能因为传错 key 就"全清一遍" """

    def test_unknown_key_returns_none(self):
        self.assertIsNone(services.estimate_item('whole_disk'))
        result, duration = services.clean_item('everything')
        self.assertIsNone(result)
        self.assertEqual(duration, 0)

    def test_known_items_are_the_whitelisted_set(self):
        self.assertEqual(set(services._ESTIMATORS), set(services._CLEANERS),
                         '预估与执行的清理项集合必须一致，否则页面能点到没实现的动作')
        self.assertEqual(set(services._ESTIMATORS),
                         {'system_temp', 'pycache', 'pip_cache', 'platform_data'},
                         '清理项只能是白名单里的这四项')

    def test_estimator_exception_becomes_none_not_500(self):
        with mock.patch.dict(services._ESTIMATORS, {'boom': lambda: 1 / 0}):
            self.assertIsNone(services.estimate_item('boom'))

    def test_cleaners_are_reachable_through_clean_item(self):
        """clean_item 必须真的派发到对应实现（页面点"清理"打的就是这个函数）"""
        sentinel = {'freed_bytes': 1, 'files': 2, 'detail': 'stub'}
        for key in services._CLEANERS:
            with mock.patch.dict(services._CLEANERS, {key: lambda: sentinel}):
                result, _duration = services.clean_item(key)
            self.assertEqual(result, sentinel, f'{key} 未被 clean_item 派发')

    def test_estimate_platform_data_counts_only_expired_rows(self):
        from datetime import timedelta

        from django.utils import timezone
        from monitor.models import RequestMetric
        days = services.settings.OBSERVABILITY['RETENTION_DAYS']
        RequestMetric.objects.create(
            path='/old/', method='GET', status_code=200, duration_ms=1,
            created_at=timezone.now() - timedelta(days=days + 1))
        RequestMetric.objects.create(
            path='/fresh/', method='GET', status_code=200, duration_ms=1)
        est = services.estimate_item('platform_data')
        self.assertEqual(est['files'], 1, '预估只应统计超过保留期的行')
        self.assertIn(f'保留期 {days} 天', est['detail'])


class PlatformDataRetentionTests(TestCase):
    """平台自清理：只删过保留期的采集数据，活跃数据一行都不能少"""

    def test_prune_keeps_recent_rows(self):
        from datetime import timedelta

        from django.utils import timezone
        from monitor.models import RequestMetric
        days = services.settings.OBSERVABILITY['RETENTION_DAYS']
        old = RequestMetric.objects.create(
            path='/old/', method='GET', status_code=200, duration_ms=1,
            created_at=timezone.now() - timedelta(days=days + 1))
        fresh = RequestMetric.objects.create(
            path='/fresh/', method='GET', status_code=200, duration_ms=1)
        from alerts.engine import prune_old_data
        prune_old_data()
        self.assertFalse(RequestMetric.objects.filter(pk=old.pk).exists(),
                         '超过保留期的数据应被清理')
        self.assertTrue(RequestMetric.objects.filter(pk=fresh.pk).exists(),
                        '保留期内的数据绝不能删')


class PlatformDataVacuumTests(TransactionTestCase):
    """VACUUM 必须在裸 autocommit 下跑（SQLite 不允许在事务里 VACUUM）。

    所以这个类刻意用 TransactionTestCase 而不是 TestCase——TestCase 会把整个用例
    包在主连接的显式事务里，VACUUM 在那里必然失败（实测报 database table is locked），
    正好等于给"清理入口不许套 @transaction.atomic"这条约束上了锁：
    将来谁给 cleaner 视图加上 atomic，这里就会先替生产环境炸出来。
    """

    def test_vacuum_ok_in_autocommit(self):
        from django.db import connection
        self.assertFalse(connection.in_atomic_block, '前提：当前是 autocommit')
        result, duration = services.clean_item('platform_data')
        self.assertIsNotNone(result, 'autocommit 下 VACUUM 应正常完成')
        self.assertIn('VACUUM', result['detail'])
        self.assertGreaterEqual(duration, 0)

    def test_vacuum_fails_inside_transaction(self):
        from django.db import transaction
        from django.db.utils import OperationalError
        with transaction.atomic(), self.assertRaises(OperationalError):
            services.clean_item('platform_data')
