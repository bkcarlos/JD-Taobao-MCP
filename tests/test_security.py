import json
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from jd_taobao_mcp.browser import BrowserController
from jd_taobao_mcp.config import Settings
from jd_taobao_mcp.extractors.detail import extract_product_detail
from jd_taobao_mcp.extractors.search import extract_jd_search, extract_taobao_search
from jd_taobao_mcp.http_auth import BearerAuth, http_transport_security
from jd_taobao_mcp.safety import (
    SafetyError, display_url, ensure_product_url, is_allowed_request,
    is_allowed_url, is_jd_login_callback, is_jd_verification_page, is_readable_url, public_url,
)


class UrlTests(unittest.TestCase):
    def test_only_public_pages_are_readable(self):
        for url in ("https://item.jd.com/123.html", "https://search.jd.com/Search?keyword=test",
                    "https://item.taobao.com/item.htm?id=123", "https://detail.tmall.com/item.htm?id=1"):
            with self.subTest(url=url):
                self.assertTrue(is_readable_url(url))
        for url in ("https://order.jd.com/center/list.action", "https://www.jd.com/account",
                    "https://passport.jd.com/new/login.aspx", "https://login.taobao.com/",
                    "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm"):
            with self.subTest(url=url):
                self.assertFalse(is_readable_url(url))
                with self.assertRaises(SafetyError):
                    ensure_product_url(url)

    def test_url_parser_rejects_ambiguous_or_insecure_targets(self):
        for url in ("http://item.jd.com/123.html", "https://item.jd.com.evil.example/123.html",
                    "https://eviljd.com/123.html", "https://user:pass@item.jd.com/123.html",
                    "https://item.jd.com:8443/123.html", "https://127.0.0.1/",
                    "file:///tmp/private", "javascript:alert(1)",
                    "https://item.jd.com/../account", "https://item.jd.com/%2e%2e/account"):
            with self.subTest(url=url):
                self.assertFalse(is_allowed_url(url))

    def test_login_allowed_but_never_readable(self):
        url = "https://passport.jd.com/new/login.aspx?code=FAKE_CODE#FAKE_TOKEN"
        self.assertTrue(is_allowed_url(url))
        self.assertFalse(is_readable_url(url))
        self.assertEqual(display_url(url), "https://passport.jd.com/new/login.aspx")

    def test_tracking_and_auth_parameters_not_exported(self):
        self.assertEqual(public_url("https://item.taobao.com/item.htm?id=1&token=FAKE#SECRET"),
                         "https://item.taobao.com/item.htm?id=1")
        self.assertEqual(display_url("file:///tmp/private"), "")

    def test_resource_domains_do_not_grant_navigation_access(self):
        self.assertTrue(is_allowed_request("https://img.alicdn.com/a.png", navigation=False))
        self.assertFalse(is_allowed_request("https://img.alicdn.com/a.png", navigation=True))
        self.assertFalse(is_allowed_request("https://collector.example/x", navigation=False))

    def test_legacy_write_mode_fails_closed(self):
        with patch.dict("os.environ", {"ALLOW_STATE_CHANGING_ACTIONS": "true"}):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_sso_callback_is_exact_and_never_public(self):
        valid = "https://sso.jd.com/setCookie?token=FAKE_TOKEN#FAKE_FRAGMENT"
        self.assertTrue(is_jd_login_callback(valid))
        self.assertFalse(is_allowed_url(valid))
        self.assertFalse(is_readable_url(valid))
        self.assertEqual(display_url(valid), "")
        for url in ("http://sso.jd.com/setCookie", "https://sso.jd.com.evil.example/setCookie",
                    "https://user:pass@sso.jd.com/setCookie", "https://sso.jd.com:8443/setCookie",
                    "https://sso.jd.com/account", "https://sso.jd.com/setCookie/../account",
                    "https://sso.jd.com/%73etCookie", "https://order.jd.com/setCookie"):
            self.assertFalse(is_jd_login_callback(url), url)

    def test_risk_verification_is_exact_and_never_public(self):
        url = "https://cfe.m.jd.com/privatedomain/risk_handler/03101900/?token=FAKE"
        self.assertTrue(is_jd_verification_page(url))
        self.assertFalse(is_allowed_url(url))
        self.assertFalse(is_readable_url(url))
        self.assertEqual(display_url(url), "")
        for invalid in (url.replace("https:", "http:"), url.replace("cfe.m.jd.com", "evil.example"),
                        url.replace("cfe.m.jd.com", "user:password@cfe.m.jd.com"),
                        url.replace("03101900", "other"), url.replace("/privatedomain/", "/account/")):
            self.assertFalse(is_jd_verification_page(invalid))


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, url="https://item.jd.com/123.html"):
        c = BrowserController(replace(Settings.from_env(), action_delay_ms=0))
        p = SimpleNamespace(url=url, evaluate=AsyncMock(return_value=[]))
        c._active_page_unlocked = AsyncMock(return_value=p)
        return c, p

    async def test_sso_callback_requires_live_human_main_frame_get(self):
        c, page = self.controller()
        page.main_frame = object()
        c._page = page
        url = "https://sso.jd.com/setCookie?token=FAKE"
        request = SimpleNamespace(url=url, frame=page.main_frame, method="GET",
                                  is_navigation_request=lambda: True)
        self.assertFalse(c._request_allowed(request, url))
        c.login_until = time.monotonic() + 60
        self.assertTrue(c._request_allowed(request, url))
        request.method = "POST"
        self.assertFalse(c._request_allowed(request, url))
        request.method, request.frame = "GET", object()
        self.assertFalse(c._request_allowed(request, url))
        request.frame = page.main_frame
        c.login_until = time.monotonic() - 1
        self.assertFalse(c._request_allowed(request, url))

    async def test_sso_handoff_rechecks_redirect_destination(self):
        c, page = self.controller()
        page.main_frame = object()
        c._page, c.login_until = page, time.monotonic() + 60
        request = SimpleNamespace(url="https://sso.jd.com/setCookie?token=FAKE", method="GET",
                                  frame=page.main_frame, is_navigation_request=lambda: True)
        for target, allowed in (("https://www.jd.com/", True),
                                ("https://order.jd.com/list", False),
                                ("https://collector.example/", False)):
            response = SimpleNamespace(status=302, headers={"location": target}, dispose=AsyncMock())
            route = SimpleNamespace(request=request, abort=AsyncMock(), fetch=AsyncMock(return_value=response),
                                    fulfill=AsyncMock())
            await c._guard_request(route)
            route.fetch.assert_awaited_once()
            if allowed:
                route.fulfill.assert_awaited_once()
                route.abort.assert_not_awaited()
            else:
                route.abort.assert_awaited_once_with("blockedbyclient")
                route.fulfill.assert_not_awaited()
            self.assertNotIn("FAKE", json.dumps(c._login_navigation_issue))

    async def test_verification_requires_human_session_and_rejects_password_fill(self):
        url = "https://cfe.m.jd.com/privatedomain/risk_handler/03101900/?token=FAKE"
        c, page = self.controller(url)
        page.main_frame = object()
        page.locator = Mock()
        c._page = page
        request = SimpleNamespace(url=url, frame=page.main_frame, method="GET",
                                  is_navigation_request=lambda: True)
        self.assertFalse(c._request_allowed(request, url))
        c.login_until = time.monotonic() + 60
        self.assertTrue(c._request_allowed(request, url))
        self.assertIs(await c._human_login_page_unlocked(), page)
        with self.assertRaises(SafetyError):
            await c.submit_password("FAKE_USER", "FAKE_PASSWORD")
        page.locator.assert_not_called()
        request.frame = object()
        self.assertFalse(c._request_allowed(request, url))
        c.login_until = 0
        with self.assertRaises(SafetyError):
            await c._human_login_page_unlocked()
        with self.assertRaises(SafetyError):
            await c.snapshot()

    async def test_checkpoint_does_not_interrupt_pending_sso_or_verification(self):
        for url in ("https://sso.jd.com/setCookie?token=FAKE",
                    "https://cfe.m.jd.com/privatedomain/risk_handler/03101900/?token=FAKE"):
            c, page = self.controller(url)
            c._page = page
            c._context = SimpleNamespace(cookies=AsyncMock())
            c._end_login_unlocked = AsyncMock()
            self.assertFalse(await c.checkpoint_login())
            c._context.cookies.assert_not_awaited()
            c._end_login_unlocked.assert_not_awaited()

    async def test_sensitive_pages_not_evaluated(self):
        for url in ("https://passport.jd.com/new/login.aspx", "https://order.jd.com/list"):
            c, page = self.controller(url)
            for operation in (c.snapshot, c.list_elements, c.get_page):
                with self.assertRaises(SafetyError):
                    await operation()
            page.evaluate.assert_not_awaited()

    async def test_page_changed_during_snapshot_is_not_exported(self):
        c, page = self.controller()
        async def changed(*args):
            page.url = "https://login.taobao.com/"
            return [{"ref": "e1", "href": "https://item.jd.com/1.html"}]
        page.evaluate.side_effect = changed
        with self.assertRaises(SafetyError):
            await c.snapshot()

    async def test_only_allowed_links_returned(self):
        c, page = self.controller()
        page.evaluate.return_value = [
            {"ref": "e1", "href": "https://item.jd.com/123.html?token=FAKE"},
            {"ref": "e2", "href": "https://order.jd.com/list"},
            {"ref": "e3", "href": "https://collector.example/"},
        ]
        result = await c.list_elements()
        self.assertEqual(result["elements"], [{"ref": "e1", "href": "https://item.jd.com/123.html"}])
        self.assertNotIn("FAKE", json.dumps(result))

    async def test_click_navigates_without_executing_handlers(self):
        c, page = self.controller()
        locator = SimpleNamespace(count=AsyncMock(return_value=1),
                                  get_attribute=AsyncMock(return_value="https://item.jd.com/9.html"),
                                  click=AsyncMock())
        page.locator = Mock(return_value=locator)
        c._navigate_unlocked = AsyncMock(return_value={"success": True})
        await c.click("e1")
        locator.click.assert_not_awaited()
        c._navigate_unlocked.assert_awaited_once_with(page, "https://item.jd.com/9.html")
        with self.assertRaises(ValueError):
            await c.click('e1"] button')

    async def test_cookie_values_never_returned_and_domain_boundary_exact(self):
        c, page = self.controller()
        c._context = SimpleNamespace(cookies=AsyncMock(return_value=[
            {"name": "pt_key", "value": "FAKE_SECRET", "domain": ".eviljd.com"},
        ]))
        self.assertFalse((await c.check_login("jd"))["likely_logged_in"])
        c._context.cookies.return_value.append(
            {"name": "thor", "value": "FAKE_SECRET", "domain": ".jd.com"})
        result = await c.check_login("jd")
        self.assertTrue(result["likely_logged_in"])
        self.assertNotIn("FAKE_SECRET", json.dumps(result))
        self.assertNotIn("cookie_names", result)

    async def test_request_and_redirect_blocked_before_external_fetch(self):
        c, _ = self.controller()
        request = SimpleNamespace(url="https://collector.example/", is_navigation_request=lambda: False)
        route = SimpleNamespace(request=request, abort=AsyncMock(), fetch=AsyncMock(), fulfill=AsyncMock())
        await c._guard_request(route)
        route.fetch.assert_not_awaited()
        route.abort.assert_awaited_once_with("blockedbyclient")
        route.abort.reset_mock()
        request.url = "https://item.jd.com/123.html"
        response = SimpleNamespace(status=302, headers={"location": "https://collector.example/"},
                                   dispose=AsyncMock())
        route.fetch.return_value = response
        await c._guard_request(route)
        self.assertEqual(route.fetch.call_args.kwargs["max_redirects"], 0)
        route.fulfill.assert_not_awaited()
        route.abort.assert_awaited_once_with("blockedbyclient")
        response.dispose.assert_awaited_once()

    async def test_websocket_is_closed_without_connecting(self):
        c, _ = self.controller()
        ws = SimpleNamespace(close=AsyncMock(), connect_to_server=Mock())
        await c._block_websocket(ws)
        ws.close.assert_awaited_once()
        ws.connect_to_server.assert_not_called()

    async def test_login_redirect_diagnostics_exclude_secrets_and_preserve_boundary(self):
        c, page = self.controller()
        page.main_frame = object()
        c._page, c._context = page, object()
        c.login_until = time.monotonic() + 60
        request = SimpleNamespace(url="https://passport.jd.com/new/login.aspx", method="GET",
                                  frame=page.main_frame, is_navigation_request=lambda: True)
        for target, host, scheme in (
            ("https://safe.jd.com/FAKE_PATH?token=FAKE_QUERY#FAKE_FRAGMENT", "safe.jd.com", "https"),
            ("http://FAKE_USER:FAKE_PASSWORD@www.jd.com/", "www.jd.com", "http"),
            ("https://FAKE_ACCOUNT.jd.com/", "other", "https"),
        ):
            response = SimpleNamespace(status=302, headers={"location": target}, dispose=AsyncMock())
            route = SimpleNamespace(request=request, abort=AsyncMock(),
                                    fetch=AsyncMock(return_value=response), fulfill=AsyncMock())
            await c._guard_request(route)
            route.abort.assert_awaited_once_with("blockedbyclient")
            route.fulfill.assert_not_awaited()
            result = await c.status()
            self.assertTrue(result["login_active"])
            self.assertEqual(result["login_navigation_issue"],
                             {"reason": "redirect_denied", "host": host, "scheme": scheme})
            self.assertNotIn("FAKE", json.dumps(result))

    async def test_login_diagnostics_ignore_iframes_and_expired_sessions(self):
        c, page = self.controller()
        page.main_frame = object()
        c._page = page
        c.login_until = time.monotonic() + 60
        request = SimpleNamespace(url="https://collector.example/", frame=object(),
                                  is_navigation_request=lambda: True)
        route = SimpleNamespace(request=request, abort=AsyncMock(), fetch=AsyncMock())
        await c._guard_request(route)
        self.assertIsNone(c._login_navigation_issue)
        self.assertFalse(c.login_navigation_blocked)
        request.frame = page.main_frame
        c.login_until = 0
        await c._guard_request(route)
        self.assertIsNone(c._login_navigation_issue)

    async def test_product_output_excludes_raw_page_and_jsonld(self):
        page = SimpleNamespace(
            url="https://item.jd.com/123.html",
            evaluate=AsyncMock(return_value={
                "title": "Example product", "price_text": "99",
                "body_text": "FAKE_ACCOUNT_INFORMATION",
                "json_ld": [{"@type": "Product", "name": "Example product", "secret": "FAKE_TOKEN"}],
            }),
        )
        data = await extract_product_detail(page, "jd")
        self.assertNotIn("FAKE_ACCOUNT_INFORMATION", json.dumps(data))
        self.assertNotIn("FAKE_TOKEN", json.dumps(data))
        self.assertNotIn("page_text_excerpt", data)
        self.assertNotIn("json_ld_product", data)
        self.assertEqual(data["title"], "Example product")

    async def test_sensitive_product_url_rejected_before_evaluation(self):
        page = SimpleNamespace(url="https://order.jd.com/list", evaluate=AsyncMock())
        with self.assertRaises(SafetyError):
            await extract_product_detail(page, "jd")
        page.evaluate.assert_not_awaited()

    async def test_search_filters_product_and_image_links(self):
        for extract in (extract_jd_search, extract_taobao_search):
            page = SimpleNamespace(url="https://search.jd.com/Search", evaluate=AsyncMock(return_value=[
                {"title": "Example product", "url": "https://item.jd.com/123.html?token=FAKE",
                 "image": "https://collector.example/pixel?token=FAKE"},
                {"title": "Spoofed product", "url": "https://collector.example/item.jd.com/123.html"},
            ]))
            items = await extract(page, 10)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["url"], "https://item.jd.com/123.html")
            self.assertIsNone(items[0]["image"])
            self.assertNotIn("FAKE", json.dumps(items))

    async def test_search_rechecks_page_after_read(self):
        for extract in (extract_jd_search, extract_taobao_search):
            page = SimpleNamespace(url="https://search.jd.com/Search", evaluate=AsyncMock())
            async def changed(*args):
                page.url = "https://login.taobao.com/"
                return []
            page.evaluate.side_effect = changed
            with self.assertRaises(SafetyError):
                await extract(page, 10)

    async def test_resource_and_post_preserving_redirects_are_blocked(self):
        c, _ = self.controller()
        for navigation, method, status in ((False, "GET", 302), (True, "POST", 307), (True, "POST", 308)):
            response = SimpleNamespace(status=status, headers={"location": "https://item.jd.com/123.html"},
                                       dispose=AsyncMock())
            route = SimpleNamespace(
                request=SimpleNamespace(url="https://item.jd.com/123.html", method=method,
                                        is_navigation_request=lambda: navigation),
                abort=AsyncMock(), fetch=AsyncMock(return_value=response), fulfill=AsyncMock())
            await c._guard_request(route)
            route.abort.assert_awaited_once_with("blockedbyclient")
            route.fulfill.assert_not_awaited()


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_applies_to_all_routes_methods_and_sessions(self):
        from starlette.responses import PlainTextResponse
        inner_calls = []
        async def inner(scope, receive, send):
            inner_calls.append(scope)
            await PlainTextResponse("ok")(scope, receive, send)
        token = "test-only-" + "x" * 40
        app = BearerAuth(inner, token)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for method in ("GET", "POST", "DELETE"):
                for path in ("/mcp", "/?token=" + token):
                    r = await client.request(method, path, headers={"Mcp-Session-Id": "fake-session"})
                    self.assertEqual(r.status_code, 401)
                    self.assertNotIn(token, r.text)
            self.assertEqual(inner_calls, [])
            self.assertEqual((await client.get("/mcp", headers={"Authorization": "Bearer wrong"})).status_code, 401)
            r = await client.get("/mcp", headers=[("Authorization", "Bearer " + token),
                                                 ("Authorization", "Bearer " + token)])
            self.assertEqual(r.status_code, 401)
            self.assertEqual((await client.get("/mcp", headers={"Authorization": "Bearer " + token})).status_code, 200)
            self.assertEqual(len(inner_calls), 1)

    def test_missing_or_weak_token_rejected_at_startup(self):
        for token in ("", "short", "x" * 31, "x" * 32 + " ", "密" * 32, "x" * 32 + "\x00"):
            with self.subTest(token_length=len(token)), self.assertRaises(ValueError):
                BearerAuth(Mock(), token)

    async def test_authenticated_mcp_initialization_and_session_reuse(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("fixture", host="127.0.0.1")
        inner = mcp.streamable_http_app()
        token = "test-only-" + "x" * 40
        app = BearerAuth(inner, token)
        async with inner.router.lifespan_context(inner):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
                headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
                r = await client.post("/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "fixture", "version": "1"}},
                })
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIn('"result"', r.text)
                session_id = r.headers["mcp-session-id"]
                r = await client.post("/mcp", headers={"Mcp-Session-Id": session_id},
                                      json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                self.assertEqual(r.status_code, 401)

    def test_extra_hosts_reject_broad_or_invalid_values(self):
        for value in ("*", "shopping-mcp:*", "https://shopping-mcp:8000", "user@shopping-mcp",
                      "shopping-mcp/path", "shopping-mcp:0", "shopping-mcp:65536", "a..b",
                      "-bad:8000", "a\r\nb", "shopping-mcp:8000,"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                http_transport_security(value)
        settings = http_transport_security("shopping-mcp:8000,other.example:9000")
        self.assertIn("127.0.0.1:*", settings.allowed_hosts)
        self.assertIn("other.example:9000", settings.allowed_hosts)
        self.assertNotIn("http://shopping-mcp:8000", settings.allowed_origins)

    async def test_exact_service_host_can_initialize_without_broadening_origin_access(self):
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("fixture", transport_security=http_transport_security("shopping-mcp:8000"))
        inner = mcp.streamable_http_app()
        token = "test-only-" + "x" * 40
        app = BearerAuth(inner, token)
        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                              "clientInfo": {"name": "fixture", "version": "1"}}}
        async with inner.router.lifespan_context(inner):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://shopping-mcp:8000") as client:
                headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
                self.assertEqual((await client.post("/mcp", headers=headers, json=payload)).status_code, 200)
                for host in ("unknown.example:8000", "shopping-mcp:9000", "shopping-mcp.evil.example:8000"):
                    response = await client.post("/mcp", headers={**headers, "Host": host}, json=payload)
                    self.assertEqual(response.status_code, 421)
                response = await client.post("/mcp", headers={**headers, "Origin": "https://evil.example"}, json=payload)
                self.assertEqual(response.status_code, 403)
                self.assertEqual((await client.post("/mcp", json=payload)).status_code, 401)

    async def test_removed_tools_are_not_advertised(self):
        import server
        names = {t.name for t in await server.mcp.list_tools()}
        self.assertTrue({"search_products", "get_product_detail", "check_login"} <= names)
        self.assertFalse({"type_into_element", "take_screenshot", "go_back", "submit_password", "human_login_view", "human_login_pointer", "human_login_code"} & names)
        self.assertEqual(server.mcp.settings.host, "127.0.0.1")

    async def test_generic_navigation_cannot_call_login_endpoints(self):
        import server
        with patch.object(server.service, "_browser_for_platform") as select_browser:
            with self.assertRaises(SafetyError):
                await server.open_url("https://passport.jd.com/logout")
            select_browser.assert_not_called()

    def test_container_binding_requires_token_and_retains_host_validation(self):
        import server
        env = {"MCP_HTTP_HOST": "0.0.0.0", "MCP_HTTP_PORT": "8000", "MCP_HTTP_TOKEN": "test-only-" + "x" * 40}
        with patch.dict("os.environ", env), patch("sys.argv", ["server.py", "--http"]):
            with patch.object(server.uvicorn, "run") as run:
                server.main()
                self.assertEqual(run.call_args.kwargs["host"], "0.0.0.0")
                self.assertIsInstance(run.call_args.args[0], BearerAuth)
                self.assertTrue(server.mcp.settings.transport_security.enable_dns_rebinding_protection)
            with patch.dict("os.environ", {"MCP_HTTP_TOKEN": ""}), self.assertRaises(ValueError):
                server.main()
            with patch.dict("os.environ", {"MCP_HTTP_HOST": "untrusted.example"}), self.assertRaises(ValueError):
                server.main()


if __name__ == "__main__":
    unittest.main()
