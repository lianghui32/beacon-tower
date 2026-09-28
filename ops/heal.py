"""
ops/heal.py — 自愈动作执行器

告警触发后，若该策略绑定了启用的自愈动作且已过冷却期，则执行：
- cleanup_tmp: 清理临时目录中的过期文件（默认系统临时目录，仅删普通文件；
  自定义目录必须位于系统临时目录之下，防止误删业务目录）
- http_callback: GET 一个回调 URL（经 ops/urlsafe 统一校验，禁止重定向，
  阻断链路本地/元数据地址；环回与私网可用环境开关）
- command: 执行白名单内的命令——argv[0] 必须命中 OBS_HEAL_CMD_ALLOWLIST
  环境变量登记的可执行文件绝对路径（Windows 分号 / Unix 冒号分隔），
  未配置白名单时 command 类型整体禁用；以参数列表 + shell=False 运行，
  带 30 秒超时与输出留痕

执行结果写 HealRun 留痕，并写操作审计。
冷却期用条件 UPDATE 原子占位，避免并发评估重复执行。
"""
import logging
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .audit import audit_system
from .models import HealAction, HealRun
from .urlsafe import open_no_redirect

logger = logging.getLogger(__name__)

# 可执行文件白名单（绝对路径）：未配置时 command 类型禁用。
# 例：Windows  set OBS_HEAL_CMD_ALLOWLIST=C:\ops\clean.bat;C:\ops\restartsvc.exe
#      Linux   export OBS_HEAL_CMD_ALLOWLIST=/usr/local/bin/clean-tmp.sh:/usr/bin/systemctl
_CMD_ALLOWLIST = [
    str(Path(p).resolve())
    for p in (__import__('os').environ.get('OBS_HEAL_CMD_ALLOWLIST') or '').split(
        ';' if sys.platform == 'win32' else ':')
    if p.strip()
]


def run_action(action, reason=''):
    """执行一个自愈动作并留痕，返回 HealRun"""
    started = time.perf_counter()
    ok, output = True, ''
    try:
        if action.action_type == 'cleanup_tmp':
            output = _cleanup_tmp(action.param)
        elif action.action_type == 'http_callback':
            output = _http_callback(action.param)
        elif action.action_type == 'command':
            output = _run_command(action.param)
        else:
            ok, output = False, f'未知动作类型 {action.action_type}'
    except Exception as e:
        ok, output = False, f'执行异常: {e}'
    output = f'触发原因：{reason}\n耗时 {round((time.perf_counter() - started) * 1000)}ms\n{output}'[:4000]
    run = HealRun.objects.create(action=action, ok=ok, output=output)
    HealAction.objects.filter(pk=action.pk).update(last_run_at=timezone.now())
    audit_system('自愈动作执行', action.name, f'成功={ok} {output[:200]}')
    return run


def _cleanup_tmp(target_dir=''):
    """清理目录下超过 24 小时的普通文件（默认系统临时目录），返回删除清单

    自定义目录只允许系统临时目录及其子目录（resolve 后校验前缀），
    防止把项目目录/用户目录配进来误删业务文件。
    """
    temp_root = Path(tempfile.gettempdir()).resolve()
    if target_dir:
        directory = Path(target_dir).resolve()
        try:
            directory.relative_to(temp_root)
        except ValueError:
            return (f'目录不在允许范围：仅允许清理系统临时目录 {temp_root} '
                    f'及其子目录，收到 {directory}')
    else:
        directory = temp_root
    deadline = time.time() - 24 * 3600
    deleted, errors = [], []
    if not directory.exists():
        return f'目录不存在: {directory}'
    for p in list(directory.iterdir())[:500]:
        try:
            if p.is_file() and p.stat().st_mtime < deadline:
                size = p.stat().st_size
                p.unlink()
                deleted.append(f'{p.name}({size}B)')
        except Exception as e:
            errors.append(str(e)[:60])
    return (f'清理 {directory}：删除 {len(deleted)} 个过期文件\n'
            + '\n'.join(deleted[:30])
            + (f'\n错误 {len(errors)} 项' if errors else ''))


def _http_callback(url):
    try:
        resp, err = open_no_redirect(url, timeout=10)
        if err:
            return f'回调 URL 校验失败: {err}'
        with resp:
            return f'HTTP {resp.status}，响应 {len(resp.read())} 字节'
    except Exception as e:
        return f'回调失败: {e}'


def _run_command(cmd_line):
    """以参数列表 + shell=False 执行白名单内的命令

    - OBS_HEAL_CMD_ALLOWLIST 未配置 -> command 类型整体禁用；
    - argv[0] 必须 resolve 后命中白名单（拒绝 cmd/powershell 等解释器
      未经登记的任意调用）；Windows 下用 posix=False 解析，保留反斜杠路径。
    """
    if not _CMD_ALLOWLIST:
        return ('command 类型已禁用：请先用环境变量 OBS_HEAL_CMD_ALLOWLIST 登记可执行文件'
                '的绝对路径（Windows 分号 / Unix 冒号分隔）')
    if not cmd_line.strip():
        return '命令为空'
    # Windows 路径含反斜杠，POSIX 模式会把 \ 当转义符吃掉
    argv = shlex.split(cmd_line, posix=(sys.platform != 'win32'))
    if not argv:
        return '命令为空'
    try:
        exe = Path(argv[0]).resolve()
    except OSError:
        return f'命令路径无法解析: {argv[0]}'
    if str(exe) not in _CMD_ALLOWLIST:
        allowed = '\n'.join(_CMD_ALLOWLIST)
        return f'命令不在白名单内: {exe}\n白名单：\n{allowed}'
    # .bat/.cmd 会被 Windows CreateProcess 交给 cmd.exe 重新解析，
    # 参数中的 & | %VAR% 等元字符即成注入点（Python 不对其做转义）——一律拒绝
    if exe.suffix.lower() in ('.bat', '.cmd', '.btm'):
        return (f'拒绝执行 {exe.suffix} 脚本：批处理会经 cmd.exe 重新解析参数，'
                '存在命令注入风险。请改用 .exe，或用登记的 python.exe 执行 .py 脚本。')
    try:
        proc = subprocess.run(
            argv, shell=False, capture_output=True, text=True,
            timeout=30, cwd=str(settings.BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        return '命令执行超时（>30s），已终止'
    except OSError as e:
        return f'命令启动失败: {e}'
    out = (proc.stdout or '') + (('\n[stderr] ' + proc.stderr) if proc.stderr else '')
    return f'exit={proc.returncode}\n{out[:3000]}'


def process_heal_for_event(event):
    """告警触发后调用：执行该策略绑定的启用动作（冷却期用条件 UPDATE 原子占位）"""
    actions = HealAction.objects.filter(policy=event.policy, enabled=True)
    ran = 0
    for action in actions:
        now = timezone.now()
        # 原子抢占冷却期：仅当 last_run_at 早于冷却线（或为空）时占位成功，
        # 并发评估下只有一个线程能拿到受影响行数 1
        deadline = now - timedelta(minutes=max(0, action.cooldown_min))
        claimed = HealAction.objects.filter(
            pk=action.pk,
        ).exclude(
            last_run_at__gt=deadline,
        ).update(last_run_at=now)
        if not claimed:
            continue
        run_action(action, reason=event.summary)
        ran += 1
    return ran
