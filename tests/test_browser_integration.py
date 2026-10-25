"""Opt-in Chromium regressions using synthetic DOM and loopback servers only."""
import asyncio
import json
import os
import threading
import tempfile
import time
from pathlib import Path
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from playwright.async_api import Error, async_playwright

from jd_taobao_mcp.browser import BrowserController, _LINKS_SCRIPT
from jd_taobao_mcp.config import Settings
from jd_taobao_mcp.extractors.detail import extract_product_detail
from jd_taobao_mcp.extractors.search import extract_jd_search


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

    async def test_jd_detail_selects_product_not_recommendation_or_calculator(self):
        """Read modern and legacy product prices without unrelated page widgets."""
        await self.context.route("**/*", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
        await self.page.goto("https://item.jd.com/123.html")
        unrelated = '''<h1>Example calculator</h1>
            <div class="top-name">Example store</div>
            <span class="cross_platform_card_price">¥999.00</span>
            <span class="service-price">19.00</span>'''
        for markup, expected in [
            ('<title>Example mouse</title><div class="product-price-panel"><span class="product-price--main"><span>¥</span><span>123.45</span></span></div>', 123.45),
            ('<div class="sku-name">Example mouse</div><div class="summary-price"><span class="price">¥89.00</span></div>', 89.0),
            ('<meta property="og:title" content="Example mouse">', None),
        ]:
            with self.subTest(expected=expected):
                await self.page.set_content(unrelated + markup)
                result = await extract_product_detail(self.page, "jd")
                self.assertEqual(result["title"], "Example mouse")
                self.assertEqual(result["price"], expected)

    async def test_jd_search_decodes_metadata_and_keeps_prices_in_each_card(self):
        """Modern search cards expose only their own price and decoded metadata."""
        from urllib.parse import urlencode, quote
        await self.context.route("**/*", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
        await self.page.goto("https://search.jd.com/Search")
        def card(pid, price):
            metadata = urlencode({"pid": pid, "wname": quote("示例鼠标"), "seller": quote("示例店铺"), "commentNum": "5000"})
            return f'''<div class="plugin_goodsCardWrapper"><div class="_card_fixture_1">
                <a href="https://chat.jd.com/?{metadata}">Contact</a>
                {price}<div class="comments">5000</div>
                </div></div>'''
        await self.page.set_content('<div class="_price_unrelated_1">¥999</div>' +
                                    card("123", '<span class="_price_fixture_31"><i>¥</i>123<span>.45</span></span>') +
                                    card("456", ''))
        items = await extract_jd_search(self.page, 2)
        self.assertEqual([item["price"] for item in items], [123.45, None])
        self.assertEqual(items[0]["title"], "示例鼠标")
        self.assertEqual(items[0]["shop"], "示例店铺")
        self.assertEqual(items[0]["url"], "https://item.jd.com/123.html")

    async def test_jd_fallback_does_not_treat_model_number_as_price(self):
        """An unrecognized card without a currency amount has no known price."""
        await self.context.route("**/*", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
        await self.page.goto("https://search.jd.com/Search")
        await self.page.set_content('<article><a href="https://item.jd.com/123.html">Mouse model 123</a> 5000 reviews</article>')
        items = await extract_jd_search(self.page, 1)
        self.assertIsNone(items[0]["price"])

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

    async def test_login_page_exchanges_fragment_and_completes_human_flow(self):
        import socket
        import uvicorn
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from jd_taobao_mcp.login_portal import LoginPortal
        fake = SimpleNamespace(start_qr_login=AsyncMock(), checkpoint_login=AsyncMock(return_value=False),
                               login_qr_png=AsyncMock(return_value=b"fixture"), login_until=0, login_navigation_blocked=False,
                               end_login=AsyncMock(),submit_password=AsyncMock(),human_login_view=AsyncMock(return_value=b"fixture"))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        portal = LoginPortal(fake, origin+"/login")
        server = uvicorn.Server(uvicorn.Config(portal, log_level="error", access_log=False, lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async def ready():
                while not server.started:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(ready(),5)
            await self.page.goto(portal.issue()["login_url"])
            start = self.page.get_by_role("button",name="显示二维码",exact=True)
            await start.wait_for()
            for _ in range(50):
                if await start.is_enabled(): break
                await asyncio.sleep(0.02)
            self.assertTrue(await start.is_enabled())
            self.assertEqual(self.page.url,origin+"/login")
            if os.getenv("MCP_LOGIN_TEST_SCREENSHOT"):
                await self.page.screenshot(path=os.environ["MCP_LOGIN_TEST_SCREENSHOT"])
            await start.click()
            await self.page.get_by_role("button",name="刷新二维码",exact=True).wait_for()
            fake.start_qr_login.assert_awaited_once()
            fake.checkpoint_login.return_value=True
            await self.page.get_by_text("登录状态已保存，可以回到 Rakazo 查询商品。有效性以实际查询为准。",exact=True).wait_for(timeout=10000)
            self.assertTrue(await self.page.locator("#qr").is_hidden())
            await self.page.get_by_role("button",name="关闭入口",exact=True).click()
            await self.page.get_by_text("入口已关闭，登录状态已保存。",exact=True).wait_for()
            self.assertIsNone(portal.session)
            await self.page.goto(portal.issue()["login_url"])
            await self.page.get_by_role("button",name="关闭入口",exact=True).click()
            await self.page.get_by_text("入口已关闭，本次未确认登录成功。请重新生成链接并完成登录。",exact=True).wait_for()
            fake.checkpoint_login.return_value=False
            await self.page.goto(portal.issue()["login_url"])
            await self.page.get_by_role("button",name="账号密码登录",exact=True).click()
            if os.getenv("MCP_LOGIN_TEST_SCREENSHOT"):
                await self.page.screenshot(path=str(Path(os.environ["MCP_LOGIN_TEST_SCREENSHOT"]).with_name("jd-password-form.png")))
            await self.page.get_by_label("账号",exact=True).fill("FAKE_LOGIN_ACCOUNT")
            await self.page.get_by_label("密码",exact=True).fill("FAKE_LOGIN_PASSWORD")
            await self.page.get_by_role("button",name="登录",exact=True).click()
            for _ in range(100):
                if fake.submit_password.await_count:break
                await asyncio.sleep(0.02)
            fake.submit_password.assert_awaited_once_with("FAKE_LOGIN_ACCOUNT","FAKE_LOGIN_PASSWORD")
            self.assertEqual(await self.page.get_by_label("密码",exact=True).input_value(),"")
            await self.page.locator("#verification").wait_for(state="visible")
            fake.checkpoint_login.return_value=True
            await self.page.get_by_text("登录状态已保存，可以回到 Rakazo 查询商品。有效性以实际查询为准。",exact=True).wait_for(timeout=10000)
            self.assertTrue(await self.page.locator("#verification").is_hidden())
        finally:
            server.should_exit=True
            await asyncio.wait_for(task,5)
            sock.close()

    async def test_portal_drag_updates_before_release_and_supports_touch_cancel(self):
        import socket
        import uvicorn
        from jd_taobao_mcp.login_portal import LoginPortal
        remote_context=await self.browser.new_context(viewport={'width':1000,'height':600},service_workers='block')
        remote=await remote_context.new_page()
        fixture='''<style>body{margin:0;background:white}#knob{position:absolute;left:100px;top:200px;width:40px;height:40px;background:#112233}</style>
          <form id="formlogin" onsubmit="return false"><input id="loginname"><input id="nloginpwd" type="password"><button id="loginsubmit" type="button">Login</button></form>
          <div id="knob"></div><script>
          window.dragging=false;window.dragX=120;window.releases=0;
          document.getElementById('knob').onmousedown=e=>{e.preventDefault();window.dragging=true;};
          document.onmousemove=e=>{if(window.dragging){window.dragX=e.clientX;document.getElementById('knob').style.left=(e.clientX-20)+'px';}};
          document.onmouseup=()=>{if(window.dragging)window.releases++;window.dragging=false;};
          </script>'''
        await remote_context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=fixture))
        controller=BrowserController(replace(Settings.from_env(),action_delay_ms=0))
        controller._context,controller._page=remote_context,remote
        sock=socket.socket();sock.bind(('127.0.0.1',0))
        portal=LoginPortal(controller,f'http://127.0.0.1:{sock.getsockname()[1]}/login')
        server=uvicorn.Server(uvicorn.Config(portal,log_level='error',access_log=False,lifespan='off'))
        task=asyncio.create_task(server.serve(sockets=[sock]))
        async def front_until(predicate):
            async with asyncio.timeout(8):
                while not await self.page.evaluate(predicate):
                    await asyncio.sleep(0.02)
        try:
            async def ready():
                while not server.started: await asyncio.sleep(0.01)
            await asyncio.wait_for(ready(),5)
            await self.page.goto(portal.issue()['login_url'])
            await self.page.get_by_role('button',name='账号密码登录',exact=True).click()
            await self.page.get_by_label('账号',exact=True).fill('FAKE_USER')
            await self.page.get_by_label('密码',exact=True).fill('FAKE_PASSWORD')
            await self.page.get_by_role('button',name='登录',exact=True).click()
            await front_until("document.getElementById('view').naturalWidth===1000")
            await self.page.locator('#view').scroll_into_view_if_needed()
            box=await self.page.locator('#view').bounding_box()
            def position(x):return {'x':box['x']+x/1000*box['width'],'y':box['y']+220/600*box['height']}
            await self.page.mouse.move(**position(120));await self.page.mouse.down()
            await remote.wait_for_function('window.dragging===true')
            await self.page.mouse.move(**position(300),steps=5)
            await remote.wait_for_function('window.dragX>=295')
            # The displayed remote frame must change while the button is still held.
            await front_until('''() => {
              const img=document.getElementById('view');if(!img.complete)return false;
              const c=document.createElement('canvas');c.width=1000;c.height=600;
              const ctx=c.getContext('2d');ctx.drawImage(img,0,0);
              return ctx.getImageData(300,220,1,1).data[2]===51;
            }''')
            self.assertEqual(await remote.evaluate('window.releases'),0)
            await self.page.mouse.move(**position(350));await self.page.mouse.up()
            await remote.wait_for_function('window.releases===1&&!window.dragging')
            await front_until('gesture===null')
            cdp=await self.context.new_cdp_session(self.page)
            await cdp.send('Emulation.setTouchEmulationEnabled',{'enabled':True,'maxTouchPoints':1})
            await cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[position(350)]})
            await remote.wait_for_function('window.dragging===true')
            await cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[position(430)]})
            await remote.wait_for_function('window.dragX>=425')
            await cdp.send('Input.dispatchTouchEvent',{'type':'touchCancel','touchPoints':[]})
            await remote.wait_for_function('window.releases===2&&!window.dragging')
            await front_until('gesture===null')
            self.assertIsNone(controller._human_pointer_id)
        finally:
            await controller.end_login()
            server.should_exit=True
            await asyncio.wait_for(task,5)
            sock.close()
            await remote_context.close()

    async def test_password_and_manual_controls_stay_on_fixed_login_document(self):
        await self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="""
            <form id="formlogin" onsubmit="return false">
              <input id="loginname"><input id="nloginpwd" type="password">
              <button type="button" id="loginsubmit" onclick="window.submitted=(window.submitted||0)+1">Login</button>
              <input id="authcode">
            </form>"""))
        await self.page.goto("https://passport.jd.com/new/login.aspx")
        controller=BrowserController(Settings.from_env())
        controller._context=self.context
        controller._page=self.page
        controller.login_until=time.monotonic()+60
        await controller.submit_password("FAKE_USER","FAKE_PASSWORD")
        self.assertEqual(await self.page.locator('#loginname').input_value(),'FAKE_USER')
        self.assertEqual(await self.page.locator('#nloginpwd').input_value(),'FAKE_PASSWORD')
        self.assertEqual(await self.page.evaluate('window.submitted'),1)
        self.assertTrue((await controller.human_login_view()).startswith(b'\x89PNG'))
        await self.page.locator('#nloginpwd').focus()
        from jd_taobao_mcp.safety import SafetyError
        with self.assertRaises(SafetyError):await controller.human_login_code('123456')
        await self.page.locator('#authcode').focus()
        await controller.human_login_code('123456')
        self.assertEqual(await self.page.locator('#authcode').input_value(),'123456')
        box=await self.page.locator('#loginsubmit').bounding_box()
        size=self.page.viewport_size
        point={'x':(box['x']+box['width']/2)/size['width'],'y':(box['y']+box['height']/2)/size['height']}
        await controller.human_login_gesture('down','fixture-gesture-id',point)
        await controller.human_login_gesture('up','fixture-gesture-id',point)
        self.assertEqual(await self.page.evaluate('window.submitted'),2)
        for verification_url in ("https://cfe.m.jd.com/privatedomain/risk_handler/03101900/?token=FAKE",
                                 "https://aq.jd.com/certified/index?ticket=FAKE"):
            await self.page.goto(verification_url)
            self.assertTrue((await controller.human_login_view()).startswith(b'\x89PNG'))
            with self.assertRaises(SafetyError):
                await controller.submit_password('FAKE_USER','FAKE_PASSWORD')
            self.assertEqual(await self.page.locator('#nloginpwd').input_value(),'')
            box=await self.page.locator('#loginsubmit').bounding_box()
            point={'x':(box['x']+box['width']/2)/size['width'],'y':(box['y']+box['height']/2)/size['height']}
            await controller.human_login_gesture('down','fixture-gesture-id',point)
            await controller.human_login_gesture('up','fixture-gesture-id',point)
            self.assertEqual(await self.page.evaluate('window.submitted'),1)
        await controller.end_login()
        self.assertEqual(self.page.url,'about:blank')
        with self.assertRaises(SafetyError):await controller.human_login_view()

    async def test_saved_session_cookie_survives_browser_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = replace(Settings.from_env(), headless=True, profile_dir=Path(tmp)/"profile",
                               browser_executable_path=Path(os.environ["MCP_BROWSER_TEST_EXECUTABLE"])
                               if os.getenv("MCP_BROWSER_TEST_EXECUTABLE") else None,
                               browser_channel=None, proxy=None)
            first = BrowserController(settings)
            second = BrowserController(settings)
            try:
                await first.start()
                await first._context.add_cookies([{"name":"thor","value":"FAKE_SESSION_ONLY_COOKIE",
                                                  "domain":".jd.com","path":"/","httpOnly":True,"secure":True}])
                self.assertTrue(await first.checkpoint_login())
                await first.close()
                await second.start()
                self.assertTrue((await second.check_login("jd"))["likely_logged_in"])
                cookies = await second._context.cookies()
                self.assertEqual(next(c["value"] for c in cookies if c["name"] == "thor"), "FAKE_SESSION_ONLY_COOKIE")
                # A clean logout must replace the snapshot rather than resurrect it.
                await second._context.clear_cookies()
                await second.close()
                await first.start()
                self.assertFalse((await first.check_login("jd"))["likely_logged_in"])
            finally:
                await first.close()
                await second.close()

    async def test_redirect_chain_and_subresources_never_reach_forbidden_endpoint(self):
        visited = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                visited.append(self.path)
                if self.path in ("/start", "/next", "/allowed-start"):
                    self.send_response(302)
                    self.send_header("Location", {"/start": "/next", "/next": "/collect", "/allowed-start": "/ok"}[self.path])
                    if self.path == "/allowed-start":
                        self.send_header("Set-Cookie", "fixture_session=FAKE_SESSION; HttpOnly; Path=/")
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
                cookies = await self.context.cookies(origin)
                self.assertTrue(any(c["name"] == "fixture_session" and c["value"] == "FAKE_SESSION"
                                    and c["httpOnly"] for c in cookies))
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()
