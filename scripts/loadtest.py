#!/usr/bin/env python3
"""
scripts/loadtest.py — 平台接入链路压测（纯标准库，无第三方依赖）

三个场景：
  1. GET  /api/health/            公开探活（轻读路径，反映框架+中间件开销）
  2. POST /api/ingest/metrics/    指标上报（令牌鉴权 + 校验 + 写入）
  3. GET  /forum/optimized/       真实业务页面（走 APM 采集中间件的读写全路径）

场景 3 需要登录会话（整站门禁）：账号密码从 --credentials 文件读取
（默认 demo_credentials.txt，格式为 username= / password= 两行）。

用法：
    python scripts/loadtest.py --threads 16 --seconds 20 --url http://127.0.0.1:8015
    python scripts/loadtest.py --scenario page --path /forum/optimized/

设计：每线程一条 HTTP keep-alive 连接闭环压测（连接复用），
延迟记录到毫秒，输出 QPS / p50 / p95 / p99 / 错误率，并输出分位数明细便于对比。
压测会把真实写入指标表——压完可等保留期清理，或删库重建。
"""
import argparse
import http.client
import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * p / 100))
    return sorted_vals[idx]


def _host_port(url):
    parts = urlsplit(url)
    return parts.hostname or '127.0.0.1', parts.port or 80


def _cookies_of(response):
    """从响应的 Set-Cookie 里提取 name=value 对（http.client 只给列表，需自己拼）"""
    pairs = []
    for raw in (response.msg.get_all('Set-Cookie') or []):
        first = raw.split(';')[0].strip()
        if '=' in first:
            pairs.append(first)
    return pairs


def login_session(url, username, password):
    """登录一次，返回可复用的 Cookie 头（sessionid + csrftoken）

    整站登录门禁下匿名请求只会拿到 302，压不到业务页面与采集中间件——
    所以场景 3 必须带会话。用标准库走完整 CSRF 流程，与浏览器行为一致。
    """
    host, port = _host_port(url)
    conn = http.client.HTTPConnection(host, port, timeout=15)
    conn.request('GET', '/accounts/login/')
    resp = conn.getresponse()
    body = resp.read().decode('utf-8', 'replace')
    jar = {}
    for pair in _cookies_of(resp):
        k, _, v = pair.partition('=')
        jar[k] = v
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    csrf = m.group(1) if m else jar.get('csrftoken', '')
    if not csrf:
        raise SystemExit('登录失败：页面里没取到 CSRF token')

    form = urlencode({'csrfmiddlewaretoken': csrf, 'username': username,
                      'password': password})
    headers = {'Content-Type': 'application/x-www-form-urlencoded',
               'Cookie': '; '.join(f'{k}={v}' for k, v in jar.items()),
               'Referer': f'{url}/accounts/login/'}
    conn.request('POST', '/accounts/login/', body=form.encode('utf-8'), headers=headers)
    resp = conn.getresponse()
    resp.read()
    for pair in _cookies_of(resp):
        k, _, v = pair.partition('=')
        jar[k] = v
    conn.close()
    if 'sessionid' not in jar:
        raise SystemExit(f'登录失败：未获得会话（HTTP {resp.status}），请检查 --credentials')
    return '; '.join(f'{k}={v}' for k, v in jar.items())


def run_worker(url, path, method, body, headers, seconds, latencies, errors):
    host, port = _host_port(url)
    deadline = time.monotonic() + seconds
    payload = body.encode('utf-8') if body else None
    conn = None
    while time.monotonic() < deadline:
        try:
            if conn is None:
                conn = http.client.HTTPConnection(host, port, timeout=10)
            start = time.perf_counter()
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            resp.read()
            elapsed = (time.perf_counter() - start) * 1000
            if resp.status >= 400:
                errors[0] += 1
            latencies.append(elapsed)
        except Exception:
            errors[0] += 1
            try:
                conn.close()
            except Exception:
                pass
            conn = None  # 断连重建（模拟真实客户端行为）
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def report(title, args, latencies, errors, wall):
    latencies.sort()
    n = len(latencies)
    print(f'\n=== {title} ===')
    print(f'并发 {args.threads} 线程 × {args.seconds}s（keep-alive）')
    print(f'总请求 {n + errors[0]}，成功 {n}，错误 {errors[0]}'
          f'（错误率 {errors[0] / max(1, n + errors[0]) * 100:.2f}%）')
    print(f'QPS = {n / max(0.001, wall):.0f}')
    print(f'延迟 p50 = {percentile(latencies, 50):.1f} ms，'
          f'p95 = {percentile(latencies, 95):.1f} ms，'
          f'p99 = {percentile(latencies, 99):.1f} ms，'
          f'max = {latencies[-1]:.1f} ms')


def main():
    parser = argparse.ArgumentParser(description='平台接入链路压测')
    parser.add_argument('--url', default='http://127.0.0.1:8015')
    parser.add_argument('--threads', type=int, default=16)
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--token', default='')
    parser.add_argument('--credentials', default='demo_credentials.txt',
                        help='场景 page 的登录账号文件（username=/password= 两行）')
    parser.add_argument('--path', default='/forum/optimized/',
                        help='场景 page 压测的业务路径')
    parser.add_argument('--scenario', choices=['health', 'ingest', 'page', 'both', 'all'],
                        default='both')
    args = parser.parse_args()

    scenarios = []
    if args.scenario in ('health', 'both', 'all'):
        scenarios.append(('GET /api/health/ (公开探活)', 'GET', '/api/health/', None, {}))
    if args.scenario in ('ingest', 'both', 'all'):
        if not args.token:
            print('ingest 场景需要 --token（平台根目录 ingest_token.txt）')
            sys.exit(2)
        body = json.dumps([{'name': 'loadtest.metric', 'value': 1.0}])
        scenarios.append(('POST /api/ingest/metrics/ (令牌上报)', 'POST',
                          '/api/ingest/metrics/', body,
                          {'Content-Type': 'application/json', 'X-OBS-Token': args.token}))
    if args.scenario in ('page', 'all'):
        cred = Path(args.credentials)
        if not cred.exists():
            print(f'page 场景需要登录账号文件：{cred}（或用 --credentials 指定，'
                  f'或 python manage.py create_demo_account 生成）')
            sys.exit(2)
        kv = dict(line.strip().split('=', 1) for line in cred.read_text(
            encoding='utf-8').splitlines() if '=' in line and not line.startswith('#'))
        cookie = login_session(args.url, kv['username'], kv['password'])
        scenarios.append((f'GET {args.path} (业务页面，走 APM 采集中间件)',
                          'GET', args.path, None, {'Cookie': cookie}))

    for title, method, path, body, headers in scenarios:
        latencies, errors = [], [0]
        threads = [
            threading.Thread(target=run_worker,
                             args=(args.url, path, method, body, headers,
                                   args.seconds, latencies, errors))
            for _ in range(args.threads)
        ]
        wall_start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        report(title, args, latencies, errors, time.monotonic() - wall_start)


if __name__ == '__main__':
    main()
