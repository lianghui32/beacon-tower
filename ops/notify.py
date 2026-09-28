"""
ops/notify.py — 真实通知渠道：SMTP 邮件 / 企业微信 / 钉钉 / 通用 Webhook

由 alerts.engine 在告警触发/恢复时调用 send_notify()；
所有发送都会记录成功/失败，配置在 /ops/notify/ 页面维护与测试。

安全约束：
- Webhook 地址经 ops/urlsafe 统一校验（http/https、域名解析逐 IP 阻断
  链路本地/云元数据/保留地址；环回与私网可用环境变量关闭）；
- 请求禁止重定向（30x 直接视为失败），防止跳板绕过 IP 黑名单；
- SMTP 授权码从数据库读出时自动解密（见 ops/crypto.py），非 SSL 连接
  若服务器支持 STARTTLS 则先升级加密再登录。
"""
import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from email.utils import formataddr

from .crypto import decrypt as _decrypt_secret
from .models import NotifyConfig
from .urlsafe import validate_url

logger = logging.getLogger(__name__)

_TIMEOUT = 8


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 30x 一律不跟随


_OPENER = urllib.request.build_opener(_NoRedirect)


def _validate_webhook(url):
    """webhook 地址校验（统一走 ops/urlsafe）；返回错误信息或 None"""
    return validate_url(url)


def _send_mail(cfg, subject, content):
    if not (cfg.smtp_host and cfg.smtp_user and cfg.smtp_pass and cfg.mail_to):
        return 'skipped: SMTP 未配置完整'
    password = _decrypt_secret(cfg.smtp_pass)
    if not password:
        return 'failed: SMTP 授权码解密失败，请重新保存配置'
    receivers = [x.strip() for x in cfg.mail_to.split(',') if x.strip()]
    msg = MIMEText(content, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = formataddr(('可观测平台', cfg.mail_from or cfg.smtp_user))
    msg['To'] = ','.join(receivers)
    if cfg.smtp_ssl:
        client = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=_TIMEOUT)
    else:
        client = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=_TIMEOUT)
        try:
            # 明文连接时若服务器支持 STARTTLS，先升级为 TLS 再发凭据
            client.starttls(context=__import__('ssl').create_default_context())
        except smtplib.SMTPException:
            logger.warning('SMTP 服务器不支持 STARTTLS，将以明文发送（建议改用 SSL 端口）')
    try:
        client.login(cfg.smtp_user, password)
        client.sendmail(cfg.mail_from or cfg.smtp_user, receivers, msg.as_string())
    finally:
        try:
            client.quit()
        except Exception:
            pass
    return 'sent'


def _post_json(url, payload):
    err = _validate_webhook(url)
    if err:
        return f'failed: {err}'
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with _OPENER.open(req, timeout=_TIMEOUT) as resp:
            return f'sent (HTTP {resp.status})'
    except urllib.error.HTTPError as e:
        return f'failed: HTTP {e.code}（不跟随重定向）'
    except (urllib.error.URLError, OSError) as e:
        # 网络层失败（连接拒绝/DNS/超时）统一给出可读文案，而不是原始异常上抛
        return f'failed: {getattr(e, "reason", e)}'


def _wecom_text(title, content):
    return {'msgtype': 'text', 'text': {'content': f'{title}\n{content}'[:2000]}}


def _ding_text(title, content):
    return {'msgtype': 'text', 'text': {'content': f'{title}\n{content}'[:2000]}}


def send_notify(title, content):
    """按配置把一条通知发到所有已配置渠道。

    返回 [(渠道, 状态), ...]；调用方（告警引擎）负责写 NotificationRecord。
    """
    results = []
    try:
        cfg = NotifyConfig.load()
    except Exception as e:
        return [('配置读取', f'failed: {e}')]

    if not cfg.enabled:
        return results

    if cfg.smtp_host:
        try:
            results.append(('邮件', _send_mail(cfg, title, content)))
        except Exception as e:
            results.append(('邮件', f'failed: {e}'))
    if cfg.wecom_webhook:
        try:
            results.append(('企业微信', _post_json(cfg.wecom_webhook, _wecom_text(title, content))))
        except Exception as e:
            results.append(('企业微信', f'failed: {e}'))
    if cfg.ding_webhook:
        try:
            results.append(('钉钉', _post_json(cfg.ding_webhook, _ding_text(title, content))))
        except Exception as e:
            results.append(('钉钉', f'failed: {e}'))
    if cfg.webhook_generic:
        try:
            payload = {'title': title, 'content': content}
            results.append(('通用Webhook', _post_json(cfg.webhook_generic, payload)))
        except Exception as e:
            results.append(('通用Webhook', f'failed: {e}'))
    return results


def send_test(channel):
    """配置页"发送测试通知"按钮"""
    title = '[可观测平台] 测试通知'
    content = f'这是一条测试通知，发送时间见服务器时间。渠道：{channel}'
    cfg = NotifyConfig.load()
    if channel == '邮件':
        return _send_mail(cfg, title, content)
    if channel == '企业微信':
        return _post_json(cfg.wecom_webhook, _wecom_text(title, content))
    if channel == '钉钉':
        return _post_json(cfg.ding_webhook, _ding_text(title, content))
    if channel == '通用Webhook':
        return _post_json(cfg.webhook_generic, {'title': title, 'content': content})
    return 'unknown channel'
