"""Opt-in Chromium regressions using synthetic DOM and loopback servers only."""
import asyncio
import json
import os
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from playwright.async_api import Error, async_playwright

from jd_taobao_mcp.browser import BrowserController, _LINKS_SCRIPT
from jd_taobao_mcp.config import Settings


@unittest.skipUnless(os.getenv("MCP_BROWSER_TESTS") == "1", "opt-in Chromium tests")
class BrowserIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        options = {"headless": True}
        if os.getenv("MCP_BROWSER_TEST_EXECUTABLE"):
            options["executable_path"] = os.environ["MCP_BROWSER_TEST_EXECUTABLE"]
        self.browser = await self.pw.chromium.launch(**options)
        self.context = await self.browser.new_context(service_workers="block")
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_real_dom_never_exports_input_or_anchor_text(self):
        await self.context.route("**/*", lambda route: route.abort())
        await self.page.set_content('''
            <input type="password" value="FAKE_PASSWORD">
            <input autocomplete="one-time-code" value="FAKE_OTP">
            <textarea>FAKE_PRIVATE_MESSAGE</textarea>
            <div contenteditable>FAKE_PRIVATE_NOTE</div>
            <a href="https://item.jd.com/123.html">FAKE_ACCOUNT_NAME</a>
            <button>Submit</button>
        ''')
        links = await self.page.evaluate(_LINKS_SCRIPT, 80)
        self.assertEqual(links, [{"ref": "e1", "href": "https://item.jd.com/123.html"}])
        self.assertNotIn("FAKE_", json.dumps(links))

    async def test_redirect_chain_and_subresources_never_reach_forbidden_endpoint(self):
        visited = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                visited.append(self.path)
                if self.path in ("/start", "/next", "/allowed-start"):
                    self.send_response(302)
                    self.send_header("Location", {"/start": "/next", "/next": "/collect", "/allowed-start": "/ok"}[self.path])
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b'<img src="/collect"><script>fetch("/collect").catch(()=>{})</script>')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        controller = BrowserController(replace(Settings.from_env(), navigation_timeout_ms=3000))
        # Production rejects HTTP/loopback; substitute only the fixture's exact URLs.
        allowed = {origin + path for path in ("/start", "/next", "/ok", "/allowed-start")}
        try:
            with patch("jd_taobao_mcp.browser.is_allowed_request",
                       side_effect=lambda url, **kwargs: url in allowed):
                await self.context.route("**/*", controller._guard_request)
                async with self.page.expect_event("requestfailed", timeout=5000) as blocked:
                    try:
                        await self.page.goto(origin + "/start", timeout=5000)
                    except Error:
                        pass
                self.assertEqual((await blocked.value).url, origin + "/next")
                self.assertEqual(visited, ["/start", "/next"])
                await self.page.close()
                self.page = await self.context.new_page()
                await self.page.goto(origin + "/ok", wait_until="networkidle")
                self.assertEqual(visited, ["/start", "/next", "/ok"])
                async with self.page.expect_response(origin + "/ok", timeout=5000):
                    try:
                        await self.page.goto(origin + "/allowed-start")
                    except Error:
                        pass
                await self.page.wait_for_load_state("networkidle")
                self.assertEqual(self.page.url, origin + "/ok")
                self.assertEqual(visited, ["/start", "/next", "/ok", "/allowed-start", "/ok"])
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()
