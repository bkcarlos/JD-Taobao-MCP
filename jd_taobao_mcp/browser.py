from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import (
    BrowserContext, Page, Playwright, Route, WebSocketRoute,
    Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, async_playwright,
)

from .config import Settings
from .login_state import load_cookies, save_cookies, likely_logged_in, save_navigation_diagnostic
from .safety import (
    SafetyError, display_url, ensure_allowed_url, ensure_readable_url,
    is_allowed_request, is_jd_login_callback, is_jd_verification_page, is_readable_url, public_url,
)


# Never enumerate inputs or export their values, even when the user typed them manually.
_LINKS_SCRIPT = r"""
(limit) => {
  for (const el of document.querySelectorAll('[data-mcp-ref]'))
    el.removeAttribute('data-mcp-ref');
  const out = [];
  for (const el of document.querySelectorAll('a[href]')) {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || r.width < 2 || r.height < 2)
      continue;
    const ref = 'e' + (out.length + 1);
    el.setAttribute('data-mcp-ref', ref);
    out.push({ref, href: el.href});
    if (out.length >= limit) break;
  }
  return out;
}
"""


class BrowserController:
    """A private persistent browser exposing public product reads only."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self.login_until = 0.0
        self.login_navigation_blocked = False
        self._login_navigation_issue: dict[str, str] | None = None
        self._login_expiry_task: asyncio.Task | None = None

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            page = await self._active_page_unlocked()
            return {"success": True, "headless": self.settings.headless,
                    "current_url": display_url(page.url)}

    def _record_login_navigation_issue(self, request: Any, url: str, reason: str) -> None:
        """Keep bounded main-frame diagnostics without URLs or account identifiers."""
        if (self.login_until <= time.monotonic() or self._page is None
                or not request.is_navigation_request() or request.frame != self._page.main_frame):
            return
        if reason != "request_failed":
            self.login_navigation_blocked = True
        # Even arbitrary hostnames can contain identifiers. Export only these
        # literal public hostnames; this list does not grant navigation access.
        known_hosts = {"passport.jd.com", "plogin.m.jd.com", "safe.jd.com", "sso.jd.com", "cfe.m.jd.com",
                       "www.jd.com", "item.jd.com", "search.jd.com", "login.taobao.com"}
        try:
            parsed = urlparse(url)
            host = parsed.hostname if parsed.hostname in known_hosts else "other"
            scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "other"
        except ValueError:
            host, scheme = "other", "other"
        self._login_navigation_issue = {"reason": reason, "host": host, "scheme": scheme}
        with suppress(OSError, SafetyError, ValueError):
            save_navigation_diagnostic(self.settings.profile_dir, url, reason)

    def _request_allowed(self, request: Any, url: str) -> bool:
        """Permit fixed authentication hops only for an active human's main-frame GET."""
        navigation = request.is_navigation_request()
        if is_allowed_request(url, navigation=navigation):
            return True
        return (navigation and self.login_until > time.monotonic() and self._page is not None
                and request.frame == self._page.main_frame and request.method == "GET"
                and (is_jd_login_callback(url) or is_jd_verification_page(url)))

    async def _guard_request(self, route: Route) -> None:
        """Inspect redirects without automatically following them outside the allowlist."""
        request = route.request
        navigation = request.is_navigation_request()
        if not self._request_allowed(request, request.url):
            self._record_login_navigation_issue(request, request.url, "navigation_denied")
            await route.abort("blockedbyclient")
            return
        try:
            response = await route.fetch(max_redirects=0, timeout=self.settings.navigation_timeout_ms)
            try:
                location = response.headers.get("location")
                if 300 <= response.status < 400 and location:
                    target = urljoin(request.url, location)
                    if not self._request_allowed(request, target):
                        self._record_login_navigation_issue(request, target, "redirect_denied")
                        await route.abort("blockedbyclient")
                        return
                    # Chromium does not re-enter Playwright routing for an HTTP
                    # redirect chain. Never forward a 3xx response to the browser.
                    # A fresh document navigation is intercepted again. Do not
                    # replay POST bodies or synthesize subresource redirects.
                    if not navigation or (response.status in (307, 308) and request.method not in ("GET", "HEAD")):
                        self._record_login_navigation_issue(request, target, "redirect_replay_denied")
                        await route.abort("blockedbyclient")
                        return
                    await route.fulfill(
                        status=200,
                        headers={
                            "content-type": "text/html; charset=utf-8",
                            "cache-control": "no-store",
                            "content-security-policy": "default-src 'none'; base-uri 'none'; form-action 'none'",
                            "referrer-policy": "no-referrer",
                        },
                        body='<meta http-equiv="refresh" content="0;url=' + escape(target, quote=True) + '">',
                    )
                    return
                await route.fulfill(response=response)
            finally:
                with suppress(PlaywrightError):
                    await response.dispose()
        except PlaywrightError:
            self._record_login_navigation_issue(request, request.url, "request_failed")
            with suppress(PlaywrightError):
                await route.abort("failed")

    async def _block_websocket(self, route: WebSocketRoute) -> None:
        """No tools require a websocket connection or arbitrary outbound tunnel."""
        await route.close()

    async def _start_unlocked(self) -> None:
        if self._context is not None:
            return
        directory = self.settings.profile_dir
        if directory.is_symlink():
            raise SafetyError("浏览器资料目录不能是符号链接。")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self._playwright = await async_playwright().start()
        options: dict[str, Any] = {
            "headless": self.settings.headless, "slow_mo": self.settings.slow_mo_ms,
            "locale": "zh-CN", "timezone_id": "Asia/Shanghai", "accept_downloads": False,
            "service_workers": "block", "chromium_sandbox": True,
            "viewport": {"width": 1440, "height": 1000},
        }
        if self.settings.browser_channel:
            options["channel"] = self.settings.browser_channel
        executable_path = self.settings.browser_executable_path or _default_edge_path()
        if executable_path:
            options.pop("channel", None)
            options["executable_path"] = str(executable_path)
        if self.settings.proxy:
            options["proxy"] = {"server": self.settings.proxy}
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.settings.profile_dir), **options
            )
            cookies = load_cookies(self.settings.profile_dir)
            if cookies:
                # Never overwrite newer cookies already persisted by Chromium.
                existing = {(c["name"], c["domain"], c["path"]) for c in await self._context.cookies()}
                missing = [c for c in cookies if (c["name"], c["domain"], c["path"]) not in existing]
                if missing:
                    await self._context.add_cookies(missing)
            await self._context.route("**/*", self._guard_request)
            await self._context.route_web_socket("**/*", self._block_websocket)
            self._context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
            self._context.set_default_timeout(self.settings.action_timeout_ms)
            old_pages = list(self._context.pages)
            self._page = await self._context.new_page()
            for page in old_pages:
                await page.close()
        except Exception:
            if self._context is not None:
                await self._context.close()
            await self._playwright.stop()
            self._context = None
            self._playwright = None
            raise

    async def _active_page_unlocked(self) -> Page:
        await self._start_unlocked()
        assert self._context is not None
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    def ensure_not_logging_in(self) -> None:
        """Prevent MCP navigation from disturbing the human login session."""
        if self.login_until > time.monotonic():
            raise SafetyError("正在等待人工登录，请完成或关闭登录页后再查询商品。")

    async def _readable_page_unlocked(self) -> Page:
        self.ensure_not_logging_in()
        page = await self._active_page_unlocked()
        ensure_readable_url(page.url)
        return page

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            if self._context is None:
                return {"running": False}
            page = await self._active_page_unlocked()
            return {"running": True, "current_url": display_url(page.url),
                    "login_active": self.login_until > time.monotonic(),
                    "login_navigation_issue": self._login_navigation_issue}

    async def _navigate_unlocked(self, page: Page, url: str) -> dict[str, Any]:
        ensure_allowed_url(url)
        if is_readable_url(url):
            url = public_url(url)
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            response = None
        except PlaywrightError:
            raise SafetyError("页面请求失败或跳转被安全策略阻止；请在本机浏览器中检查。") from None
        await self._settle(page)
        if not (self.login_until > time.monotonic() and is_jd_verification_page(page.url)):
            ensure_allowed_url(page.url)
        return {"success": True, "url": display_url(page.url),
                "http_status": response.status if response else None,
                "requires_manual_login": not is_readable_url(page.url)}

    async def navigate(self, url: str) -> dict[str, Any]:
        ensure_allowed_url(url)
        async with self._lock:
            self.ensure_not_logging_in()
            return await self._navigate_unlocked(await self._active_page_unlocked(), url)

    async def open_login(self, platform: str) -> dict[str, Any]:
        url = {"jd": "https://passport.jd.com/new/login.aspx",
               "taobao": "https://login.taobao.com/member/login.jhtml"}.get(platform)
        if not url:
            raise ValueError("platform 仅支持 jd 或 taobao")
        result = await self.navigate(url)
        result["message"] = "请直接在浏览器中完成登录；登录页面内容不会返回给 MCP。"
        return result

    async def check_login(self, platform: str) -> dict[str, Any]:
        """Return a credential-presence hint without exposing account data."""
        async with self._lock:
            await self._active_page_unlocked()
            assert self._context is not None
            present = likely_logged_in(await self._context.cookies(), platform)
            return {"platform": platform, "likely_logged_in": present,
                    "message": "仅检测未过期登录凭据；服务端是否仍接受登录以实际查询为准。"}

    async def start_qr_login(self, deadline: float) -> None:
        """Open only the official JD login page for a short-lived human session."""
        async with self._lock:
            self.login_until = deadline
            self.login_navigation_blocked = False
            self._login_navigation_issue = None
            if self._login_expiry_task:
                self._login_expiry_task.cancel()
            self._login_expiry_task = asyncio.create_task(self._expire_login(deadline))
            await self._navigate_unlocked(await self._active_page_unlocked(),
                "https://passport.jd.com/new/login.aspx?ReturnUrl=https%3A%2F%2Fwww.jd.com%2F")

    async def _expire_login(self, deadline: float) -> None:
        """Stop the official login flow and clear transient input at the deadline."""
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        async with self._lock:
            if self.login_until == deadline:
                try:
                    if self._context is not None:
                        cookies = await self._context.cookies()
                        if likely_logged_in(cookies, "jd"):
                            save_cookies(self.settings.profile_dir, cookies)
                except (OSError, PlaywrightError):
                    pass
                finally:
                    await self._end_login_unlocked()

    async def _end_login_unlocked(self) -> None:
        """Discard transient password/verification fields without deleting cookies."""
        self.login_until = 0.0
        if self._login_expiry_task and self._login_expiry_task is not asyncio.current_task():
            self._login_expiry_task.cancel()
        self._login_expiry_task = None
        if self._page is not None:
            with suppress(PlaywrightError):
                await self._page.goto("about:blank")

    async def end_login(self) -> None:
        """Cancel human interaction and any outstanding QR polling in the page."""
        async with self._lock:
            await self._end_login_unlocked()

    async def _human_login_page_unlocked(self) -> Page:
        """Human interaction is confined to fixed official login/verification documents."""
        page = await self._active_page_unlocked()
        if self.login_until <= time.monotonic() or not self._is_human_login_document(page.url):
            raise SafetyError("登录页已失效或跳转；请重新打开登录入口。")
        return page

    async def submit_password(self, username: str, password: str) -> None:
        """Submit human-supplied credentials to JD's fixed form; never log or return them."""
        async with self._lock:
            page = await self._human_login_page_unlocked()
            if not self._is_jd_login_page(page.url):
                raise SafetyError("请先完成人工验证，再提交登录。")
            # Never choose an arbitrary form/selector supplied by the caller.
            await page.locator("#formlogin #loginname").fill(username, timeout=5000)
            await page.locator("#formlogin #nloginpwd").fill(password, timeout=5000)
            if not self._is_jd_login_page(page.url):
                raise SafetyError("登录页面已改变。")
            await page.locator("#loginsubmit").click(timeout=5000)

    async def human_login_view(self) -> bytes:
        """Send a view only to the authenticated human, never to MCP or a screenshot file."""
        async with self._lock:
            page = await self._human_login_page_unlocked()
            data = await page.screenshot(type="png", timeout=5000)
            if not self._is_human_login_document(page.url):
                raise SafetyError("登录页面已改变。")
            return data

    async def human_login_pointer(self, points: list[dict[str, float]]) -> None:
        """Replay only a person's bounded pointer gesture; do not infer or solve challenges."""
        async with self._lock:
            page = await self._human_login_page_unlocked()
            viewport = page.viewport_size
            if not viewport:
                raise SafetyError("登录窗口尺寸不可用。")
            await page.mouse.move(points[0]["x"]*viewport["width"], points[0]["y"]*viewport["height"])
            await page.mouse.down()
            try:
                previous = points[0]["t"]
                for point in points[1:]:
                    if self.login_until <= time.monotonic() or not self._is_human_login_document(page.url):
                        raise SafetyError("登录页面已改变或过期。")
                    await asyncio.sleep(min(0.1, max(0, (point["t"]-previous)/1000)))
                    await page.mouse.move(point["x"]*viewport["width"], point["y"]*viewport["height"])
                    previous = point["t"]
            finally:
                await page.mouse.up()

    async def human_login_code(self, code: str) -> None:
        """Fill only a focused non-password verification input selected by the human."""
        async with self._lock:
            page = await self._human_login_page_unlocked()
            field = page.locator("input:focus")
            if await field.count() != 1:
                raise SafetyError("请先在京东页面中点选验证码输入框。")
            kind = await field.get_attribute("type")
            identity = await field.get_attribute("id")
            if kind not in (None, "text", "tel", "number") or identity in {"loginname", "nloginpwd", "mobile-number"}:
                raise SafetyError("这里只能填写验证码。")
            await field.fill(code, timeout=3000)

    async def login_qr_png(self) -> bytes:
        """Render only JD's QR image to the authenticated human portal, never MCP."""
        async with self._lock:
            page = await self._active_page_unlocked()
            if self.login_until <= time.monotonic() or not self._is_jd_login_page(page.url):
                raise SafetyError("扫码页面已关闭或过期。")
            image = page.locator("#passport-main-qrcode-img")
            source = urljoin(page.url, await image.get_attribute("src", timeout=3000) or "")
            parsed = urlparse(source)
            if (parsed.scheme != "https" or parsed.hostname != "qr.m.jd.com" or parsed.path != "/show"
                    or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)):
                raise SafetyError("二维码尚未就绪；如需验证码，请在官方页面人工处理。")
            data = await image.screenshot(type="png", timeout=5000)
            if not self._is_jd_login_page(page.url):
                raise SafetyError("登录页面已改变。")
            return data

    @staticmethod
    def _is_human_login_document(url: str) -> bool:
        """Allow a human to view the fixed challenge, without exporting it to MCP."""
        return BrowserController._is_jd_login_page(url) or is_jd_verification_page(url)

    @staticmethod
    def _is_jd_login_page(url: str) -> bool:
        """Match a fixed login document, not arbitrary pages on the account host."""
        parsed = urlparse(url)
        return (parsed.scheme == "https" and parsed.netloc == "passport.jd.com"
                and parsed.path == "/new/login.aspx")

    async def checkpoint_login(self) -> bool:
        """Save detected credentials atomically before reporting a completed scan."""
        async with self._lock:
            if self._context is None:
                return False
            if self._page is not None and (is_jd_login_callback(self._page.url)
                                           or is_jd_verification_page(self._page.url)):
                # Cookie presence must not interrupt an unfinished handoff or
                # close the human's challenge before JD finishes processing it.
                return False
            cookies = await self._context.cookies()
            if not likely_logged_in(cookies, "jd"):
                return False
            save_cookies(self.settings.profile_dir, cookies)
            await self._end_login_unlocked()
            return True

    async def _links_unlocked(self, page: Page, limit: int) -> list[dict[str, str]]:
        links = await page.evaluate(_LINKS_SCRIPT, limit)
        ensure_readable_url(page.url)
        return [{"ref": item["ref"], "href": public_url(item["href"])}
                for item in links if is_readable_url(item.get("href", ""))]

    async def snapshot(self, max_chars: int | None = None) -> dict[str, Any]:
        """Return public links only, never raw body text, forms or field values."""
        async with self._lock:
            page = await self._readable_page_unlocked()
            links = await self._links_unlocked(page, 120)
            return {"url": public_url(page.url), "links": links}

    async def list_elements(self, limit: int = 80) -> dict[str, Any]:
        async with self._lock:
            page = await self._readable_page_unlocked()
            links = await self._links_unlocked(page, max(1, min(limit, 200)))
            return {"url": public_url(page.url), "count": len(links), "elements": links}

    async def click(self, ref: str) -> dict[str, Any]:
        """Navigate an allowed link without executing its click handlers or submitting forms."""
        if not ref.startswith("e") or not ref[1:].isdigit():
            raise ValueError("无效的元素 ref。")
        async with self._lock:
            page = await self._readable_page_unlocked()
            locator = page.locator(f'a[data-mcp-ref="{ref}"]')
            if await locator.count() != 1:
                raise ValueError("请重新获取页面链接。")
            href = await locator.get_attribute("href")
            url = public_url(urljoin(page.url, href or ""))
            return await self._navigate_unlocked(page, url)

    async def scroll(self, direction: str = "down", amount: int = 900) -> dict[str, Any]:
        if direction not in {"up", "down"}:
            raise ValueError("direction 仅支持 up 或 down")
        async with self._lock:
            page = await self._readable_page_unlocked()
            delta = max(100, min(amount, 5_000)) * (1 if direction == "down" else -1)
            await page.mouse.wheel(0, delta)
            await self._settle(page, short=True)
            ensure_readable_url(page.url)
            return {"success": True}

    async def get_page(self) -> Page:
        """Internal extractors hold lock and may read public pages only."""
        return await self._readable_page_unlocked()

    async def close(self) -> dict[str, Any]:
        async with self._lock:
            self.ensure_not_logging_in()
            if self._context is not None:
                save_cookies(self.settings.profile_dir, await self._context.cookies())
                await self._context.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._context = None
            self._playwright = None
            self._page = None
            return {"success": True}

    async def _settle(self, page: Page, short: bool = False) -> None:
        delay = min(self.settings.action_delay_ms, 300 if short else self.settings.action_delay_ms)
        if delay:
            await page.wait_for_timeout(delay)


def _default_edge_path() -> Path | None:
    candidates = (
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    )
    return next((path for path in candidates if path.exists()), None)
