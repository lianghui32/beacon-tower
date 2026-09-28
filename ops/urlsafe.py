"""
ops/urlsafe.py — 出站 URL 统一安全校验（拨测 / 自愈回调 / 通知 Webhook 共用）

规则：
- 仅允许 http/https 且必须带主机名；
- 解析域名后逐 IP 检查：链路本地（含 169.254.169.254 云元数据）、保留、
  组播地址始终阻断；环回与私网地址默认允许（自用/内网拨测的合法目标），
  可分别用环境变量关闭：OBS_ALLOW_LOOPBACK_URL=0 / OBS_ALLOW_PRIVATE_URL=0；
- 所有出站请求必须经 open_no_redirect() 发出：禁止 30x 自动重定向
  （否则攻击者用外网跳板 302 到内网即可绕过全部 IP 黑名单）。

已知边界：校验与请求之间存在 DNS rebinding TOCTOU 窗口（校验时解析一次、
连接时再解析一次）。内网部署如需彻底封堵，可固定解析结果直连 IP。
"""
import ipaddress
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_ALLOW_LOOPBACK = os.environ.get('OBS_ALLOW_LOOPBACK_URL', '1') == '1'
_ALLOW_PRIVATE = os.environ.get('OBS_ALLOW_PRIVATE_URL', '1') == '1'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """30x 一律不跟随：redirect_request 返回 None 会让 urlopen 抛 HTTPError"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def check_ip(ip):
    """单 IP 分类检查：返回错误信息或 None"""
    ip = ipaddress.ip_address(ip)
    if ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return f'目标地址被禁止: {ip}（链路本地/保留/组播地址）'
    if ip.is_loopback and not _ALLOW_LOOPBACK:
        return f'目标为环回地址 {ip}，且已禁用环回 URL'
    if ip.is_private and not _ALLOW_PRIVATE:
        return f'目标为私网地址 {ip}，且已禁用私网 URL'
    return None


def validate_url(url):
    """出站 URL 校验：返回错误信息或 None"""
    parts = urlsplit(url or '')
    if parts.scheme not in ('http', 'https'):
        return 'URL 仅允许 http/https'
    if not parts.hostname:
        return 'URL 缺少主机名'
    if parts.username or parts.password:
        return 'URL 不应包含用户名/密码'
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except OSError as e:
        return f'域名解析失败: {e}'
    for info in infos:
        err = check_ip(info[4][0])
        if err:
            return err
    return None


def open_no_redirect(url, timeout=10, headers=None):
    """校验 URL 并以"禁止重定向"的 opener 发起 GET。

    返回 (response, None)；校验失败返回 (None, 错误信息)；
    3xx 重定向会作为 HTTPError 抛出（不跟随），由调用方按失败处理。
    """
    err = validate_url(url)
    if err:
        return None, err
    req = urllib.request.Request(url, headers=headers or {})
    try:
        resp = _OPENER.open(req, timeout=timeout)
        return resp, None
    except urllib.error.HTTPError:
        raise  # 含 3xx：让调用方与 4xx/5xx 同样按失败处理
    except OSError as e:
        return None, f'请求失败: {e}'
