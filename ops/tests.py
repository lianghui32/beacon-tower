"""
ops/tests.py — 运维中心安全关键路径回归测试

覆盖自愈与出站的三道闸门（这三处都是"一旦写错就能删错文件/打到内网"的级别）：
- ops/urlsafe.py：SSRF 校验（协议/凭据/元数据地址/私网开关/DNS 解析结果复核）、禁重定向；
- ops/heal.py：命令白名单（未登记即禁用、拒绝 .bat、参数不经 shell）、
  临时目录清理的作用域限制、冷却期原子抢占；
- ops/crypto.py：SMTP 授权码加密落库、幂等、历史明文兼容、换密钥不炸。
"""
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings

from ops import urlsafe


class SsrfGuardTests(TestCase):
    def test_non_http_scheme_rejected(self):
        for url in ('file:///etc/passwd', 'gopher://127.0.0.1:11211/_stats',
                    'http://', 'ftp://10.0.0.1/x', ''):
            self.assertIsNotNone(urlsafe.validate_url(url), url)

    def test_credentials_in_url_rejected(self):
        self.assertIsNotNone(urlsafe.validate_url('http://admin:pw@127.0.0.1/x'))

    def test_metadata_and_reserved_always_blocked(self):
        """链路本地/保留/组播不受开关影响——云元数据地址是 SSRF 的头号目标"""
        for ip in ('169.254.169.254', '224.0.0.1', '240.0.0.1', 'fe80::1'):
            self.assertIsNotNone(urlsafe.check_ip(ip), ip)

    def test_loopback_and_private_follow_switches(self):
        saved = (urlsafe._ALLOW_LOOPBACK, urlsafe._ALLOW_PRIVATE)
        try:
            urlsafe._ALLOW_LOOPBACK = urlsafe._ALLOW_PRIVATE = True
            self.assertIsNone(urlsafe.check_ip('127.0.0.1'))
            self.assertIsNone(urlsafe.check_ip('10.0.0.5'))
            urlsafe._ALLOW_LOOPBACK = False
            self.assertIsNotNone(urlsafe.check_ip('127.0.0.1'), '关闭环回开关后必须阻断')
            self.assertIsNotNone(urlsafe.check_ip('::1'), 'IPv6 环回同样受开关约束')
            urlsafe._ALLOW_PRIVATE = False
            self.assertIsNotNone(urlsafe.check_ip('192.168.1.10'))
            self.assertIsNotNone(urlsafe.check_ip('172.16.5.5'))
            self.assertIsNotNone(urlsafe.check_ip('fc00::1'), 'IPv6 内网段同样受开关约束')
        finally:
            urlsafe._ALLOW_LOOPBACK, urlsafe._ALLOW_PRIVATE = saved

    def test_public_ip_allowed(self):
        self.assertIsNone(urlsafe.check_ip('93.184.216.34'))

    def test_dns_result_is_rechecked_not_trusted_as_hostname(self):
        """域名解析到元数据地址必须被拦下（DNS 名字好看 ≠ 目标安全）"""
        # getaddrinfo 返回五元组：(family, type, proto, canonname, sockaddr)
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 80))]
        with mock.patch.object(socket, 'getaddrinfo', return_value=fake):
            self.assertIsNotNone(urlsafe.validate_url('http://good-looking.example.com/hook'))

    def test_unresolvable_host_reports_error_instead_of_raising(self):
        with mock.patch.object(socket, 'getaddrinfo',
                               side_effect=OSError('Name or service not known')):
            self.assertIn('解析失败', urlsafe.validate_url('http://nope.invalid/x'))

    def test_open_no_redirect_rejects_before_touching_network(self):
        resp, err = urlsafe.open_no_redirect('file:///etc/passwd')
        self.assertIsNone(resp)
        self.assertIn('http/https', err)

    def test_redirect_handler_refuses_to_follow(self):
        """302 若被自动跟随，攻击者就能用外网跳板绕开整套 IP 黑名单"""
        handler = urlsafe._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, 'Found', {},
                                                   'http://169.254.169.254/'))
        self.assertTrue(any(isinstance(h, urlsafe._NoRedirect) for h in urlsafe._OPENER.handlers),
                        '出站必须走禁重定向的 opener')


class HealCommandWhitelistTests(TestCase):
    """自愈 command 类型：白名单是唯一执行入口"""

    def setUp(self):
        from ops import heal
        self.heal = heal
        self.saved = heal._CMD_ALLOWLIST
        self.tmp = Path(tempfile.mkdtemp(prefix='obs-heal-test-'))

    def tearDown(self):
        self.heal._CMD_ALLOWLIST = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_command_disabled_when_allowlist_empty(self):
        self.heal._CMD_ALLOWLIST = []
        out = self.heal._run_command(f'{sys.executable} -c print(1)')
        self.assertIn('已禁用', out)

    def test_unlisted_executable_rejected(self):
        self.heal._CMD_ALLOWLIST = [str(Path(sys.executable).resolve())]
        out = self.heal._run_command('cmd /c echo pwned')
        self.assertIn('不在白名单内', out)
        out = self.heal._run_command('powershell -c "Get-Process"')
        self.assertIn('不在白名单内', out)

    def test_batch_scripts_rejected_even_when_whitelisted(self):
        """.bat 会被 CreateProcess 交给 cmd.exe 重新解析，参数里的元字符即注入点"""
        bat = self.tmp / 'clean.bat'
        bat.write_text('@echo off\r\n', encoding='utf-8')
        self.heal._CMD_ALLOWLIST = [str(bat.resolve())]
        self.assertIn('拒绝执行', self.heal._run_command(f'{bat} & echo pwned'))
        for suffix in ('.cmd', '.btm'):
            other = self.tmp / f'x{suffix}'
            other.write_text('x', encoding='utf-8')
            self.heal._CMD_ALLOWLIST = [str(other.resolve())]
            self.assertIn('拒绝执行', self.heal._run_command(str(other)))

    def test_whitelisted_command_runs_without_shell(self):
        script = self.tmp / 'echo.py'
        script.write_text('import sys; print("ARG=" + "|".join(sys.argv[1:]))',
                          encoding='utf-8')
        self.heal._CMD_ALLOWLIST = [str(Path(sys.executable).resolve())]
        out = self.heal._run_command(f'{sys.executable} {script} hello')
        self.assertIn('exit=0', out)
        self.assertIn('ARG=hello', out)

    def test_shell_metacharacters_stay_data_not_code(self):
        """参数里的 & 重定向若被 shell 解释，白名单就等于形同虚设"""
        script = self.tmp / 'echo.py'
        script.write_text('import sys; print("ARG=" + "|".join(sys.argv[1:]))',
                          encoding='utf-8')
        victim = self.tmp / 'victim.txt'
        victim.write_text('do-not-overwrite', encoding='utf-8')
        self.heal._CMD_ALLOWLIST = [str(Path(sys.executable).resolve())]
        out = self.heal._run_command(
            f'{sys.executable} {script} "x & echo pwned > {victim}"')
        self.assertIn('ARG=', out)
        self.assertIn('& echo pwned', out)
        self.assertEqual(victim.read_text(encoding='utf-8'), 'do-not-overwrite',
                         '参数里的重定向被当真执行了——说明走了 shell')

    def test_empty_command_line_rejected(self):
        self.heal._CMD_ALLOWLIST = [str(Path(sys.executable).resolve())]
        self.assertIn('命令为空', self.heal._run_command('   '))

    def test_timeout_is_reported_and_loop_survives(self):
        self.heal._CMD_ALLOWLIST = [str(Path(sys.executable).resolve())]
        with mock.patch.object(self.heal.subprocess, 'run',
                               side_effect=self.heal.subprocess.TimeoutExpired('x', 30)):
            self.assertIn('超时', self.heal._run_command(f'{sys.executable} a.py'))


class HealTmpScopeTests(TestCase):
    """cleanup_tmp 只能在系统临时目录之下：配错参数不能删到业务目录"""

    def setUp(self):
        from ops import heal
        self.heal = heal
        self.tmp_root = Path(tempfile.gettempdir()).resolve()
        self.sub = Path(tempfile.mkdtemp(prefix='obs-tmp-scope-'))

    def tearDown(self):
        shutil.rmtree(self.sub, ignore_errors=True)

    def test_outside_temp_root_refused(self):
        out = self.heal._cleanup_tmp(str(settings.BASE_DIR))
        self.assertIn('不在允许范围', out)
        self.assertTrue((settings.BASE_DIR / 'manage.py').exists(),
                        '拒绝之外还不能顺手删点东西')

    def test_absolute_path_escape_refused(self):
        home = Path(os.path.expanduser('~')).resolve()
        if home != self.tmp_root and self.tmp_root not in home.parents:
            out = self.heal._cleanup_tmp(str(home / '.ssh'))
            self.assertIn('不在允许范围', out)

    def test_temp_subdirectory_is_allowed_and_age_is_respected(self):
        old_file = self.sub / 'old.txt'
        old_file.write_text('x' * 100, encoding='utf-8')
        fresh = self.sub / 'fresh.txt'
        fresh.write_text('y' * 100, encoding='utf-8')
        past = time.time() - 48 * 3600
        os.utime(old_file, (past, past))
        out = self.heal._cleanup_tmp(str(self.sub))
        self.assertIn('删除 1 个过期文件', out)
        self.assertFalse(old_file.exists(), '超过 24h 的临时文件应被清理')
        self.assertTrue(fresh.exists(), '24h 内的文件绝不能删（可能是别人正在用的）')


class HealCooldownTests(TestCase):
    """冷却期用条件 UPDATE 原子占位：并发评估不能把同一个动作跑两遍"""

    def setUp(self):
        from alerts.models import AlertPolicy
        from ops.models import HealAction
        self.policy = AlertPolicy.objects.create(
            name='冷却测试', metric_key='http.request_count', operator='>', threshold=1)
        self.action = HealAction.objects.create(
            name='回调（校验必失败，不落网）', policy=self.policy,
            action_type='http_callback', param='file:///etc/passwd',
            enabled=True, cooldown_min=60)

    def tearDown(self):
        from ops.models import HealAction, HealRun
        HealRun.objects.filter(action__policy=self.policy).delete()
        HealAction.objects.filter(policy=self.policy).delete()
        self.policy.delete()

    def _event(self):
        from alerts.models import AlertEvent
        return AlertEvent.objects.create(policy=self.policy, status='firing',
                                         level='P1', value=9, summary='冷却测试事件')

    def test_second_run_blocked_by_cooldown(self):
        from ops.heal import process_heal_for_event
        from ops.models import HealRun
        self.assertEqual(process_heal_for_event(self._event()), 1)
        self.assertEqual(process_heal_for_event(self._event()), 0, '冷却期内不得重复执行')
        self.assertEqual(HealRun.objects.filter(action=self.action).count(), 1)

    def test_expired_cooldown_allows_again(self):
        from datetime import timedelta

        from django.utils import timezone
        from ops.heal import process_heal_for_event
        process_heal_for_event(self._event())
        self.action.last_run_at = timezone.now() - timedelta(minutes=61)
        self.action.save(update_fields=['last_run_at'])
        self.assertEqual(process_heal_for_event(self._event()), 1)

    def test_disabled_action_never_runs(self):
        from ops.heal import process_heal_for_event
        self.action.enabled = False
        self.action.save(update_fields=['enabled'])
        self.assertEqual(process_heal_for_event(self._event()), 0)


class NotifyCryptoTests(TestCase):
    """SMTP 授权码加密落库：库被拖走也不该直接读出密码"""

    def setUp(self):
        from ops import crypto
        self.crypto = crypto
        if not crypto._HAS_CRYPTO:
            self.skipTest('未安装 cryptography，加密退化为明文（部署时需 pip install）')

    def test_roundtrip(self):
        cipher = self.crypto.encrypt('secret-smtp-pass')
        self.assertTrue(cipher.startswith('enc:v1:'))
        self.assertNotIn('secret-smtp-pass', cipher)
        self.assertEqual(self.crypto.decrypt(cipher), 'secret-smtp-pass')

    def test_encrypt_is_idempotent(self):
        once = self.crypto.encrypt('abc')
        self.assertEqual(self.crypto.encrypt(once), once, '重复保存不能二次加密')

    def test_empty_and_legacy_plaintext_compatible(self):
        self.assertEqual(self.crypto.encrypt(''), '')
        self.assertEqual(self.crypto.decrypt(''), '')
        self.assertEqual(self.crypto.decrypt('legacy-plain'), 'legacy-plain',
                         '历史明文行必须仍可读')

    def test_wrong_key_returns_empty_instead_of_raising(self):
        cipher = self.crypto.encrypt('abc')
        with override_settings(SECRET_KEY='a-completely-different-key'):
            self.assertEqual(self.crypto.decrypt(cipher), '',
                             '换密钥要优雅失败，不能把通知链路炸出异常')

    def test_notify_config_stores_cipher_and_reads_back_plain(self):
        """页面/发信用的一直是明文，库里躺着的必须是密文"""
        from ops.models import NotifyConfig
        cfg, _ = NotifyConfig.objects.get_or_create(id=1)
        cfg.smtp_pass = self.crypto.encrypt('qq-auth-code')
        cfg.save()
        raw = NotifyConfig.objects.get(id=1).smtp_pass
        self.assertTrue(raw.startswith('enc:v1:'), '落库必须是密文')
        self.assertNotIn('qq-auth-code', raw)
        self.assertEqual(self.crypto.decrypt(raw), 'qq-auth-code',
                         '取用时必须还原成可用明文')
