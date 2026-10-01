"""员工密码的哈希与校验工具。

密码统一使用 PBKDF2-SHA256 存储，不再兼容明文记录（ 确认：
历史 demo 账号已通过重新执行 mock_data_init.py 用相同密码值重写为
哈希格式，不存在需要过渡兼容的旧账号）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


_SCHEME = "pbkdf2_sha256"
_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    """把明文密码转成带随机盐的 PBKDF2-SHA256 字符串。"""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "$".join(
        (
            _SCHEME,
            str(_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, stored_password: str | None) -> bool:
    """校验密码。只接受 PBKDF2-SHA256 哈希格式，不再兼容明文记录——
    历史 demo 账号已用相同密码值重写为哈希，不存在需要过渡的旧数据。
    如果遇到非哈希格式的存量脏数据，视为校验失败（而不是当作明文比对），
    避免任何旧记录绕过哈希校验。"""
    if not password or not stored_password:
        return False
    if not stored_password.startswith(f"{_SCHEME}$"):
        return False

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
