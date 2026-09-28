"""
ops/crypto.py — 敏感配置（SMTP 授权码等）的对称加密存储

使用 Fernet（AES-128-CBC + HMAC），密钥由 Django SECRET_KEY 派生，
密文以 "enc:v1:" 前缀落库，读取时自动解密：

- 未安装 cryptography 时退回明文存储（行为与旧版本一致，可随时 pip install 加固）；
- SECRET_KEY 变化会导致旧密文无法解密，重新保存一次配置即可。
"""
import base64
import hashlib
import logging

from django.conf import settings

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover
    _HAS_CRYPTO = False

logger = logging.getLogger(__name__)

_PREFIX = 'enc:v1:'


def _fernet():
    digest = hashlib.sha256((settings.SECRET_KEY + ':obs-notify').encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value):
    """明文 -> 密文（幂等：已加密的值原样返回）"""
    if not value:
        return ''
    if not _HAS_CRYPTO or value.startswith(_PREFIX):
        return value
    try:
        return _PREFIX + _fernet().encrypt(value.encode('utf-8')).decode('ascii')
    except Exception:
        logger.exception('敏感配置加密失败，回退明文存储')
        return value


def decrypt(value):
    """密文 -> 明文（兼容历史明文：无前缀原样返回）"""
    if not value:
        return ''
    if not _HAS_CRYPTO or not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode('ascii')).decode('utf-8')
    except InvalidToken:
        logger.warning('敏感配置解密失败（SECRET_KEY 已变化？），请重新保存通知渠道配置')
        return ''
    except Exception:
        logger.exception('敏感配置解密异常')
        return ''
