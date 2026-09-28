#!/usr/bin/env python3
"""
obs_agent.py — 全栈可观测运维平台 · 主机采集 Agent

放在任意要监控的服务器上运行，每 N 秒采集一次本机 psutil 指标，
推送到平台的 /api/ingest/host/ 端点（令牌保护）。仅依赖 psutil，
HTTP 用标准库 urllib，无需安装任何其他包。

配置（环境变量）：
    OBS_SERVER_URL     平台地址，默认 http://127.0.0.1:8014（仅允许 http/https）
    OBS_INGEST_TOKEN   接入令牌（平台根目录 ingest_token.txt，或接入中心页面查看）
    OBS_HOST_NAME      上报的主机名（默认取本机 hostname，可自定义别名）
    OBS_AGENT_INTERVAL 采集间隔秒数（默认 15）

用法：
    pip install psutil
    export OBS_SERVER_URL="http://平台IP:8014"
    export OBS_INGEST_TOKEN="你的接入令牌"
    python obs_agent.py                  # 前台运行
    nohup python obs_agent.py >/dev/null 2>&1 &    # Linux 后台运行

部署详见平台"接入中心"页面（含 systemd / Windows 计划任务示例）。
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

try:
    import psutil
except ImportError:
    print('[obs-agent] 缺少依赖：pip install psutil')
    sys.exit(1)


def _resolve_server():
    """启动时校验平台地址：仅允许 http/https 且必须带主机名（防误配，如 file: 等协议）"""
    raw = os.environ.get('OBS_SERVER_URL', 'http://127.0.0.1:8014')
    parts = urlsplit(raw)
    if parts.scheme not in ('http', 'https'):
        raise SystemExit(f'[obs-agent] OBS_SERVER_URL 仅允许 http/https，当前: {raw!r}')
    if not parts.hostname:
        raise SystemExit(f'[obs-agent] OBS_SERVER_URL 缺少主机名: {raw!r}')
    if parts.path not in ('', '/'):
        raise SystemExit(f'[obs-agent] OBS_SERVER_URL 不应包含路径: {raw!r}')
    netloc = parts.hostname
    if ':' in netloc and not netloc.startswith('['):
        netloc = f'[{netloc}]'  # IPv6 字面量
    if parts.port:
        netloc = f'{netloc}:{parts.port}'
    base = f'{parts.scheme}://{netloc}'
    if parts.username or parts.password:
        raise SystemExit('[obs-agent] OBS_SERVER_URL 不应包含用户名/密码，请用 OBS_INGEST_TOKEN')
    return base


SERVER = _resolve_server()          # 校验后的固定平台地址
ENDPOINT = SERVER + '/api/ingest/host/'   # 唯一上报地址（固定路径，不含任何外部输入）
TOKEN = os.environ.get('OBS_INGEST_TOKEN', '')
# 采集间隔：非法值回退默认，并钳制到 5-600s（0/负数会打爆服务端）
try:
    INTERVAL = int(float(os.environ.get('OBS_AGENT_INTERVAL', '15')))
except (TypeError, ValueError):
    INTERVAL = 15
INTERVAL = max(5, min(600, INTERVAL))
try:
    HOSTNAME = os.environ.get('OBS_HOST_NAME') or socket.gethostname() or 'unknown'
except Exception:
    HOSTNAME = 'unknown'

def _log(msg):
    print(f'[obs-agent {time.strftime("%H:%M:%S")}] {msg}', flush=True)

# psutil 的 cpu_percent 首次调用恒为 0，先预热一次
psutil.cpu_percent(interval=None)
_disk_root = os.path.abspath(os.sep)
_last_net = {'sent': None, 'recv': None, 'ts': None}


def _disk_usage():
    try:
        return psutil.disk_usage(_disk_root)
    except Exception:
        class _D:
            percent, used, total = 0.0, 0, 0
        return _D()


def sample():
    """采集一次本机指标，返回与平台 HostMetric 字段一致的 dict"""
    now = time.time()
    cpu = psutil.cpu_percent(interval=None)
    if cpu < 0.5:
        # 进程刚启动时非阻塞读数不可靠，阻塞 150ms 重测
        cpu = psutil.cpu_percent(interval=0.15)
    mem = psutil.virtual_memory()
    du = _disk_usage()
    try:
        load1 = psutil.getloadavg()[0]
    except (AttributeError, OSError):
        load1 = cpu / 100.0 * (os.cpu_count() or 1)

    sent_kbps = recv_kbps = 0.0
    try:
        io = psutil.net_io_counters()
        if _last_net['sent'] is not None and _last_net['ts'] is not None:
            dt = max(0.001, now - _last_net['ts'])
            sent_kbps = max(0.0, (io.bytes_sent - _last_net['sent']) / dt / 1024)
            recv_kbps = max(0.0, (io.bytes_recv - _last_net['recv']) / dt / 1024)
        _last_net['sent'], _last_net['recv'], _last_net['ts'] = io.bytes_sent, io.bytes_recv, now
    except Exception:
        pass

    try:
        tcp = len(psutil.net_connections(kind='inet'))
    except Exception:
        tcp = 0

    return {
        'hostname': HOSTNAME,
        'cpu_percent': round(cpu, 1),
        'cpu_cores': os.cpu_count() or 0,
        'load_avg': round(float(load1), 2),
        'mem_percent': round(mem.percent, 1),
        'mem_used_mb': round(mem.used / 1024 / 1024, 1),
        'mem_total_mb': round(mem.total / 1024 / 1024, 1),
        'disk_percent': round(du.percent, 1),
        'disk_used_gb': round(du.used / 1024 ** 3, 2),
        'disk_total_gb': round(du.total / 1024 ** 3, 2),
        'net_sent_kbps': round(sent_kbps, 1),
        'net_recv_kbps': round(recv_kbps, 1),
        'proc_count': len(psutil.pids()),
        'tcp_conns': tcp,
    }


def push(payload):
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={'Content-Type': 'application/json', 'X-OBS-Token': TOKEN},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        if not data.get('ok'):
            raise RuntimeError(data)


def main():
    if not TOKEN:
        _log('未设置 OBS_INGEST_TOKEN（平台根目录 ingest_token.txt）')
        sys.exit(1)
    _log(f'启动：host={HOSTNAME} -> {SERVER}，间隔 {INTERVAL}s')
    fail = 0
    while True:
        try:
            push(sample())
            if fail:
                _log('恢复上报')
                fail = 0
        except urllib.error.HTTPError as e:
            fail += 1
            # 4xx 是配置错误（令牌/地址），重试没有意义；5xx/网络错误才值得退避重试
            if 400 <= e.code < 500:
                _log(f'上报被拒绝（HTTP {e.code}）：请检查接入令牌 OBS_INGEST_TOKEN 是否正确')
                sys.exit(1)
            if fail <= 3 or fail % 20 == 0:
                _log(f'上报失败（第 {fail} 次）：HTTP {e.code}')
        except Exception as e:
            fail += 1
            if fail <= 3 or fail % 20 == 0:
                _log(f'上报失败（第 {fail} 次）：{e}')
        # 指数退避：连续失败时 15s -> 30s -> 60s -> ... 上限 5 分钟，避免死循环打爆服务端
        time.sleep(min(INTERVAL * (2 ** min(fail, 5)), 300))


if __name__ == '__main__':
    main()
