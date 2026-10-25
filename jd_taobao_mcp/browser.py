from __future__ import annotations

import asyncio
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import (
    BrowserContext, Page, Playwright, Route, WebSocketRoute,
    Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, async_playwright,
)

from .config import Settings
from .safety import (
    SafetyError, display_url, ensure_allowed_url, ensure_readable_url,
    is_allowed_request, is_readable_url, public_url,
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

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            page = await self._active_page_unlocked()
            return {"success": True, "headless": self.settings.headless,
                    "current_url": display_url(page.url)}

    async def _guard_request(self, route: Route) -> None:
        """Inspect redirects without automatically following them outside the allowlist."""
        request = route.request
        navigation = request.is_navigation_request()
        if not is_allowed_request(request.url, navigation=navigation):
            await route.abort("blockedbyclient")
            return
        try:
            response = await route.fetch(max_redirects=0, timeout=self.settings.navigation_timeout_ms)
            try:
                location = response.headers.get("location")
                if 300 <= response.status < 400 and location:
                    target = urljoin(request.url, location)
                    if not is_allowed_request(target, navigation=navigation):
                        await route.abort("blockedbyclient")
                        return
                    # Chromium does not re-enter Playwright routing for an HTTP
                    # redirect chain. Never forward a 3xx response to the browser.
                    # A fresh document navigation is intercepted again. Do not
                    # replay POST bodies or synthesize subresource redirects.
                    if not navigation or (response.status in (307, 308) and request.method not in ("GET", "HEAD")):
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
                await response.dispose()
        except PlaywrightError:
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

    async def _readable_page_unlocked(self) -> Page:
        page = await self._active_page_unlocked()
        ensure_readable_url(page.url)
        return page

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            if self._context is None:
                return {"running": False}
            page = await self._active_page_unlocked()
            return {"running": True, "current_url": display_url(page.url)}

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
        ensure_allowed_url(page.url)
        return {"success": True, "url": display_url(page.url),
                "http_status": response.status if response else None,
                "requires_manual_login": not is_readable_url(page.url)}

    async def navigate(self, url: str) -> dict[str, Any]:
        ensure_allowed_url(url)
        async with self._lock:
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
        domains = {"jd": "jd.com", "taobao": "taobao.com"}
        names = {"jd": {"pt_key", "pin", "thor"}, "taobao": {"tracknick", "lgc", "_tb_token_"}}
        if platform not in domains:
            raise ValueError("platform 仅支持 jd 或 taobao")
        async with self._lock:
            await self._active_page_unlocked()
            assert self._context is not None
            cookies = await self._context.cookies()
            root = domains[platform]
            present = {
                c["name"] for c in cookies
                if c.get("domain", "").lstrip(".") == root
                or c.get("domain", "").lstrip(".").endswith("." + root)
            }
            return {"platform": platform, "likely_logged_in": bool(present & names[platform]),
                    "message": "仅检测登录 Cookie 是否存在；以实际商品查询结果为准。"}

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
            if self._context is not None:
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
