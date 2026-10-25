"""Private cookie persistence; no credential values are returned through MCP."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from .safety import SafetyError


_COOKIE_ROOTS = ("jd.com", "taobao.com", "tmall.com")


def usable_cookies(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only unexpired first-party cookies, including browser session cookies."""
    now = time.time()
    result = []
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lstrip(".")
        expires = cookie.get("expires", -1)
        if (any(domain == root or domain.endswith("." + root) for root in _COOKIE_ROOTS)
                and isinstance(expires, (int, float)) and (expires == -1 or expires > now)):
            result.append(cookie)
    return result


def likely_logged_in(cookies: list[dict[str, Any]], platform: str) -> bool:
    """Report credential presence, not a guarantee of server-side validity."""
    root = {"jd": "jd.com", "taobao": "taobao.com"}.get(platform)
    if root is None:
        raise ValueError("platform 仅支持 jd 或 taobao")
    names = {c.get("name") for c in usable_cookies(cookies) if c.get("value")
             and (c["domain"].lstrip(".") == root or c["domain"].lstrip(".").endswith("." + root))}
    if platform == "jd":
        return "thor" in names or {"pt_key", "pt_pin"} <= names
    return {"cookie2", "unb"} <= names


def save_cookies(directory: Path, cookies: list[dict[str, Any]]) -> None:
    """Atomically persist session cookies in a mode-0600 file in the private profile."""
    if directory.is_symlink():
        raise SafetyError("浏览器资料目录不能是符号链接。")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".cookies-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "cookies": usable_cookies(cookies)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "login-cookies.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_cookies(directory: Path) -> list[dict[str, Any]]:
    """Load only a private, bounded regular file; reject unsafe credential storage."""
    path = directory / "login-cookies.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > 2_000_000:
            raise SafetyError("登录状态文件权限或格式不安全。")
        try:
            data = json.load(stream)
            if data.get("version") != 1 or not isinstance(data.get("cookies"), list):
                raise ValueError()
            return usable_cookies(data["cookies"])
        except (ValueError, TypeError, AttributeError, KeyError):
            raise SafetyError("登录状态文件损坏，请重新登录。") from None
