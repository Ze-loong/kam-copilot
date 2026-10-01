"""员工密码校验工具。

与 kam_client 使用相同的 PBKDF2-SHA256 存储格式。侧边栏不依赖
kam_client，因此在这里保留一个最小化的校验实现。
"""

from __future__ import annotations

import base64
import hashlib
import hmac


_SCHEME = "pbkdf2_sha256"


def verify_password(password: str, stored_password: str | None) -> bool:
    """校验哈希密码，并在迁移决策前临时兼容历史明文记录。"""
    if not password or not stored_password:
        return False
    if not stored_password.startswith(f"{_SCHEME}$"):
        return hmac.compare_digest(password, stored_password)

    try:
        scheme, iterations_text, salt_text, digest_text = stored_password.split("$", 3)
        if scheme != _SCHEME:
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except (ValueError, UnicodeError):
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)
