"""
ops/probing.py — 拨测引擎（黑盒监控）

从"平台所在位置"主动请求目标 URL，验证可用性 / 延迟 / HTTPS 证书有效期。
由 ops.workers.probe_loop 按任务间隔调度；结果接入指标注册表（probe.<id>.*）
即可被告警策略、大盘、智能分析直接使用。

目标 URL 在请求前经 ops/urlsafe 统一校验（仅 http/https、域名解析逐 IP 检查），
且请求通过"禁止重定向"的 opener 发出——跟随 30x 会让外网跳板把请求引到
内网/元数据地址，绕过全部 IP 黑名单。
"""
import datetime
import logging
import socket
import ssl
import time
import urllib.error
from datetime import timedelta
from urllib.parse import urlsplit

from django.db.models import F, Q
from django.utils import timezone

from .audit import audit_system
from .models import ProbeResult, ProbeTask
from .urlsafe import open_no_redirect

_SSL_CONTEXT = ssl.create_default_context()


def run_probe(task):
    """执行一次拨测并入库，返回 ProbeResult"""
    start = time.perf_counter()
    ok, status, error, cert_days = False, 0, '', None
    parts = urlsplit(task.url)
    try:
        resp, err = open_no_redirect(task.url, timeout=task.timeout_sec,
                                     headers=task.headers or {})
        if err:
            error = err
        else:
            with resp:
                status = resp.status
                body = resp.read(65536)
            if parts.scheme == 'https':
                cert_days = _cert_days(parts.hostname, parts.port or 443)
            if task.keyword:
                text = body.decode('utf-8', 'ignore')
                if task.keyword not in text:
                    error = f'页面未包含关键字 {task.keyword!r}'
                else:
                    ok = True
            else:
                ok = True
    except urllib.error.HTTPError as e:
        status = e.code
        error = f'HTTP {e.code}（重定向/拒绝均视为失败）'
    except Exception as e:
        error = str(e)[:200] or type(e).__name__
    if status and status != task.expect_status and not error:
        error = f'状态码 {status} ≠ 期望 {task.expect_status}'
        ok = False

    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    result = ProbeResult.objects.create(
        task=task, ok=ok, status_code=status, duration_ms=duration_ms,
        error=error[:200] if error else '', cert_days=cert_days,
    )
    # consecutive_fails 用 F 表达式自增/清零：后台循环与"立即执行"并发时不会互相覆盖
    if ok:
        ProbeTask.objects.filter(pk=task.pk).update(
            last_ok=True, last_status=status, last_ms=duration_ms,
            last_error='', last_cert_days=cert_days,
            consecutive_fails=0,
            next_run_at=timezone.now() + timedelta(seconds=task.interval_sec),
        )
        if task.consecutive_fails > 0:
            audit_system('拨测恢复', task.name, f'{task.url} 恢复可用')
    else:
        ProbeTask.objects.filter(pk=task.pk).update(
            last_ok=False, last_status=status, last_ms=duration_ms,
            last_error=(error or '')[:200], last_cert_days=cert_days,
            consecutive_fails=F('consecutive_fails') + 1,
            next_run_at=timezone.now() + timedelta(seconds=task.interval_sec),
        )
    return result


def _cert_days(hostname, port=443):
    """HTTPS 证书剩余天数（端口从 URL 解析，非 443 的 https 站点也能查）"""
    try:
        with socket.create_connection((hostname, port), timeout=6) as sock:
            with _SSL_CONTEXT.wrap_socket(sock, server_hostname=hostname) as s:
                not_after = s.getpeercert().get('notAfter')
        if not not_after:
            return None
        # cert_time_to_seconds 不依赖进程 locale（strptime %b 在非英文 locale 下会失败）
        exp = datetime.datetime.fromtimestamp(
            ssl.cert_time_to_seconds(not_after), tz=datetime.timezone.utc)
        return (exp - timezone.now()).days
    except Exception:
        return None


def probe_due_tasks(now=None):
    """执行所有到期且启用的任务（probe_loop 每几秒调用一次）"""

    now = now or timezone.now()
    due = list(
        ProbeTask.objects.filter(enabled=True)
        .filter(Q(next_run_at__isnull=True) | Q(next_run_at__lte=now))[:20]
    )
    count = 0
    for task in due:
        try:
            run_probe(task)
            count += 1
        except Exception:
            logging.getLogger(__name__).exception('拨测任务执行失败: %s', task.name)
    return count


def prune_results(days=7):
    ProbeResult.objects.filter(created_at__lt=timezone.now() - timedelta(days=days)).delete()


def task_series(task_id, field, minutes):
    """注册表用：按分钟聚合拨测结果
    ok_rate 百分比 / latency（duration_ms 别名）/ cert_days 证书剩余天数"""
    from django.db.models import Avg

    from monitor.registry import _bucket, _fmt, _since
    from .models import ProbeResult

    qs = ProbeResult.objects.filter(task_id=task_id, created_at__gte=_since(minutes))
    if field == 'ok_rate':
        pts = _bucket(qs, {'v': Avg('ok')})
        return [{'t': _fmt(t), 'v': round(float(r['v'] or 0) * 100, 2)} for t, r in pts]
    if field == 'cert_days':
        pts = _bucket(qs.exclude(cert_days=None), {'v': Avg('cert_days')})
        return [{'t': _fmt(t), 'v': round(float(r['v'] or 0), 1)} for t, r in pts]
    # latency / duration_ms
    pts = _bucket(qs, {'v': Avg('duration_ms')})
    return [{'t': _fmt(t), 'v': round(float(r['v'] or 0), 1)} for t, r in pts]


def probe_catalog():
    """注册表用：把启用的拨测任务展开成可告警指标"""
    items = []
    for t in ProbeTask.objects.filter(enabled=True)[:30]:
        items.append({'key': f'probe.{t.id}.ok_rate',
                      'label': f'拨测[{t.name}] 可用率 (%)', 'unit': '%'})
        items.append({'key': f'probe.{t.id}.latency',
                      'label': f'拨测[{t.name}] 延迟 (ms)', 'unit': 'ms'})
    return items


def probe_value(task_id, field, minutes=5):
    """告警引擎用：窗口内可用率/平均延迟"""
    pts = task_series(task_id, field, minutes)
    if not pts:
        return None
    vals = [p['v'] for p in pts]
    return round(sum(vals) / len(vals), 2)
