from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


class SafetyError(RuntimeError):
    """An operation exceeds the product reader's access boundary."""


# Public pages only: sharing a domain with a shop does not make account pages safe.
_PUBLIC_PATHS = {
    "www.jd.com": r"/",
    "item.jd.com": r"/\d+\.html",
    "search.jd.com": r"/Search/?",
    "www.taobao.com": r"/",
    "s.taobao.com": r"/search/?",
    "s.m.taobao.com": r"/h5/?",
    "item.taobao.com": r"/item\.htm",
    "item.m.taobao.com": r"/item\.htm",
    "new.m.taobao.com": r"/detail\.htm",
    "detail.tmall.com": r"/item\.htm",
}
_LOGIN_HOSTS = {"passport.jd.com", "plogin.m.jd.com", "login.taobao.com"}
_RESOURCE_ROOTS = (
    "jd.com", "360buy.com", "360buyimg.com", "jdimg.com", "jdstatic.com",
    "taobao.com", "tmall.com", "alicdn.com", "aliimg.com", "tbcdn.cn",
)
_QUERY_KEYS = {"id", "sku", "keyword", "q", "page", "s", "sort"}
_VERIFICATION_RE = re.compile(
    r"滑块|安全验证|异常访问|访问过于频繁|请完成验证|请先验证|输入验证码|获取验证码|"
    r"验证码错误|拖动下方滑块|拖动到最右边|验证失败|点击框体重试|"
    r"captcha|人机验证|drag.*verify|verification failed", re.I,
)


def _parse_https(url: str):
    """Reject credentials, ambiguous authorities and insecure transports."""
    try:
        if any(ord(c) <= 32 for c in url) or "\\" in url:
            return None
        parsed = urlparse(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            return None
        return parsed
    except ValueError:
        return None


def is_readable_url(url: str) -> bool:
    """Only public product, search and home pages can return data to MCP."""
    parsed = _parse_https(url)
    if parsed is None:
        return False
    pattern = _PUBLIC_PATHS.get(parsed.hostname, r"(?!)")
    return re.fullmatch(pattern, parsed.path or "/") is not None


def is_allowed_url(url: str) -> bool:
    """Allow public pages and official login pages for manual sign-in."""
    parsed = _parse_https(url)
    return parsed is not None and (is_readable_url(url) or parsed.hostname in _LOGIN_HOSTS)


def is_jd_login_callback(url: str) -> bool:
    """Recognize JD's fixed SSO cookie handoff, never a readable product URL."""
    parsed = _parse_https(url)
    return (parsed is not None and parsed.hostname == "sso.jd.com"
            and parsed.path == "/setCookie")


def is_jd_verification_page(url: str) -> bool:
    """Recognize only verification documents observed in JD login/search redirects."""
    parsed = _parse_https(url)
    paths = {"cfe.m.jd.com": "/privatedomain/risk_handler/03101900/",
             "aq.jd.com": "/certified/index"}
    return parsed is not None and parsed.path == paths.get(parsed.hostname)


def ensure_product_url(url: str) -> str:
    """Detail extraction is not a generic account-page scraper."""
    parsed = _parse_https(url)
    if not is_readable_url(url) or parsed.hostname not in {
        "item.jd.com", "item.taobao.com", "item.m.taobao.com",
        "new.m.taobao.com", "detail.tmall.com",
    }:
        raise SafetyError("详情工具仅接受公开商品链接。")
    return url


def ensure_allowed_url(url: str) -> str:
    """Validate navigation before any request is made."""
    if not is_allowed_url(url):
        raise SafetyError("仅允许 HTTPS 商品、搜索、首页及官方登录页；禁止账户和第三方页面。")
    return url


def ensure_readable_url(url: str) -> str:
    """Do not export login, verification, account or unrelated page contents."""
    if not is_readable_url(url):
        raise SafetyError("此页面不允许向 MCP 导出内容；请在本机浏览器中处理登录或账户信息。")
    return url


def public_url(url: str) -> str:
    """Return a public navigation URL without fragments or tracking/auth parameters."""
    ensure_readable_url(url)
    parsed = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if k in _QUERY_KEYS])
    return urlunparse(("https", parsed.hostname, parsed.path or "/", "", query, ""))


def display_url(url: str) -> str:
    """Status responses must not expose OAuth codes or other URL credentials."""
    if is_readable_url(url):
        return public_url(url)
    if is_allowed_url(url):
        parsed = urlparse(url)
        return f"https://{parsed.hostname}{parsed.path}"
    return ""


def product_link(url: str | None) -> str | None:
    """Filter extracted product links before returning them or navigating."""
    try:
        return public_url(ensure_product_url(url or ""))
    except SafetyError:
        return None


def image_link(url: str | None) -> str | None:
    """Do not make MCP clients load third-party tracking URLs from page content."""
    if not url or not is_allowed_request(url, navigation=False):
        return None
    parsed = urlparse(url)
    return urlunparse(("https", parsed.hostname, parsed.path, "", "", ""))


def is_allowed_request(url: str, *, navigation: bool) -> bool:
    """Check each request, including redirects, before leaving the browser."""
    if navigation:
        return is_allowed_url(url)
    parsed = _parse_https(url)
    return parsed is not None and any(
        parsed.hostname == root or parsed.hostname.endswith("." + root)
        for root in _RESOURCE_ROOTS
    )


def page_requires_user_verification(text: str, url: str = "") -> bool:
    """Detect common challenges as a usability hint, not an authorization check."""
    return bool(_VERIFICATION_RE.search(f"{url}\n{text[:5000]}"))
