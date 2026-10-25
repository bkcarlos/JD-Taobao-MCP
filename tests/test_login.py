import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from playwright.async_api import Error as PlaywrightError

from jd_taobao_mcp.browser import BrowserController
from jd_taobao_mcp.config import Settings
from jd_taobao_mcp.http_auth import BearerAuth
from jd_taobao_mcp.login_portal import LoginPortal, WithLoginPortal
from jd_taobao_mcp.login_state import load_cookies, save_cookies, likely_logged_in, save_navigation_diagnostic
from jd_taobao_mcp.safety import SafetyError


def fixture_cookie(name="thor", expires=-1):
    return {"name": name, "value": "FAKE_TEST_CREDENTIAL", "domain": ".jd.com", "path": "/",
            "expires": expires, "httpOnly": True, "secure": True, "sameSite": "Lax"}


class StateTests(unittest.TestCase):
    def test_navigation_record_is_private_bounded_and_excludes_url_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)/"profile"
            save_navigation_diagnostic(directory,
                "https://FAKE_USER:FAKE_PASSWORD@login.example.test/callback?token=FAKE_QUERY#FAKE_FRAGMENT",
                "navigation_denied")
            path = directory/"login-navigation.json"
            data = json.loads(path.read_text())
            self.assertEqual(data['host'], 'login.example.test')
            self.assertEqual(data['path'], '/callback')
            self.assertNotIn('FAKE', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            outside = Path(tmp)/"outside"
            outside.write_text('untouched')
            path.unlink()
            path.symlink_to(outside)
            save_navigation_diagnostic(directory, 'https://login.example.test/'+('a'*2000), 'request_failed')
            self.assertEqual(outside.read_text(), 'untouched')
            self.assertFalse(path.is_symlink())
            self.assertEqual(len(json.loads(path.read_text())['path']), 512)

    def test_restart_loads_session_cookies_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)/"profile"
            save_cookies(directory, [fixture_cookie(), fixture_cookie("expired", time.time()-1),
                                     {**fixture_cookie("foreign"), "domain": ".eviljd.com"}])
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(directory/"login-cookies.json").st_mode & 0o777, 0o600)
            self.assertEqual([c['name'] for c in load_cookies(directory)], ["thor"])
            save_cookies(directory, [])
            self.assertEqual(load_cookies(directory), [])

    def test_unsafe_file_permissions_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            save_cookies(directory, [fixture_cookie()])
            path = directory/"login-cookies.json"
            path.chmod(0o644)
            with self.assertRaises(SafetyError): load_cookies(directory)
            path.unlink(); path.symlink_to(directory/"other")
            with self.assertRaises(OSError): load_cookies(directory)

    def test_visitor_identifiers_and_expired_credentials_do_not_indicate_login(self):
        for cookies in ([fixture_cookie("pin")], [fixture_cookie("pt_key")],
                        [fixture_cookie(expires=time.time()-1)], [{**fixture_cookie(), "domain": ".eviljd.com"}]):
            self.assertFalse(likely_logged_in(cookies, "jd"))
        self.assertTrue(likely_logged_in([fixture_cookie("pt_key"),fixture_cookie("pt_pin")], "jd"))
        self.assertFalse(likely_logged_in([{**fixture_cookie("_tb_token_"), "domain": ".taobao.com"}], "taobao"))


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.browser = SimpleNamespace(start_qr_login=AsyncMock(), checkpoint_login=AsyncMock(return_value=False),
                                       login_qr_png=AsyncMock(return_value=b"fake-qr"), login_until=0, login_navigation_blocked=False,
                                       end_login=AsyncMock(), submit_password=AsyncMock(),human_login_view=AsyncMock(return_value=b"fake-view"),
                                       human_login_pointer=AsyncMock(),human_login_code=AsyncMock())
        self.portal = LoginPortal(self.browser, "https://login.example.test/login")
        self.headers = {"Origin":"https://login.example.test", "X-Login-Action":"1"}
        async def inner(scope, receive, send):
            from starlette.responses import PlainTextResponse
            await PlainTextResponse("MCP")(scope, receive, send)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=WithLoginPortal(
            BearerAuth(inner, "test-only-"+"x"*40), self.portal)), base_url="https://login.example.test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def authorize(self):
        result = self.portal.issue()
        self.assertNotIn("FAKE_TEST_CREDENTIAL", json.dumps(result))
        self.assertNotIn("?", result["login_url"])
        ticket=result["login_url"].split("#")[1]
        r=await self.client.post("/login/session",json={"ticket":ticket},headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/login"):
            self.assertIn(flag,r.headers["set-cookie"])
        return ticket

    async def test_one_use_exchange_host_origin_expiry_and_mcp_isolation(self):
        result=self.portal.issue();ticket=result["login_url"].split("#")[1]
        for path in ("/login/qr", "/login/status", "/mcp"):
            self.assertEqual((await self.client.get(path)).status_code,401)
        self.assertEqual((await self.client.post('/login/session',json={"ticket":ticket})).status_code,403)
        self.assertEqual((await self.client.post('/login/session',json={"ticket":ticket},headers={**self.headers,"Origin":"https://evil.example"})).status_code,403)
        self.assertEqual((await self.client.get('/login',headers={"Host":"evil.example"})).status_code,421)
        self.assertEqual((await self.client.post('/login/session',json={"ticket":"中文"},headers=self.headers)).status_code,401)
        self.assertEqual((await self.client.post('/login/session',content=b'x'*1025,headers=self.headers)).status_code,413)
        ticket=await self.authorize()
        self.assertEqual((await self.client.post('/login/session',json={"ticket":ticket},headers=self.headers)).status_code,401)
        self.assertEqual((await self.client.get('/mcp')).status_code,401)
        self.portal.session.expires=time.monotonic()-1
        self.assertEqual((await self.client.get('/login/status')).status_code,401)
        self.assertEqual((await self.client.post('/login/start',json={},headers=self.headers)).status_code,401)
        self.browser.start_qr_login.assert_not_awaited()

    async def test_human_starts_qr_and_success_is_checkpointed_before_reporting(self):
        await self.authorize()
        shell=await self.client.get('/login')
        self.assertEqual(shell.headers['cache-control'],'no-store')
        self.assertIn("frame-ancestors 'none'",shell.headers['content-security-policy'])
        self.assertEqual((await self.client.get('/login/qr')).status_code,409)
        r=await self.client.post('/login/start',json={},headers=self.headers)
        self.assertEqual(r.status_code,200)
        self.browser.start_qr_login.assert_awaited_once()
        with self.assertRaises(SafetyError): self.portal.issue()
        qr=await self.client.get('/login/qr')
        self.assertEqual(qr.content,b'fake-qr'); self.assertEqual(qr.headers['cache-control'],'no-store')
        self.assertEqual((await self.client.get('/login/status')).json()['status'],'waiting')
        self.browser.checkpoint_login.side_effect=OSError('test disk failure')
        self.assertEqual((await self.client.get('/login/status')).status_code,503)
        self.assertEqual(self.portal.session.phase,'waiting')
        self.browser.checkpoint_login.side_effect=None;self.browser.checkpoint_login.return_value=True
        self.assertEqual((await self.client.get('/login/status')).json()['status'],'saved')
        self.assertEqual((await self.client.get('/login/qr')).status_code,409)
        closed=await self.client.post('/login/close',json={},headers=self.headers)
        self.assertEqual(closed.status_code,200)
        self.assertTrue(closed.json()['login_saved'])
        self.assertEqual((await self.client.get('/login/status')).status_code,401)

    async def test_close_without_credentials_never_claims_login_was_saved(self):
        for start in (False, True):
            await self.authorize()
            if start:
                await self.client.post('/login/start',json={},headers=self.headers)
            closed=await self.client.post('/login/close',json={},headers=self.headers)
            self.assertEqual(closed.status_code,200)
            self.assertFalse(closed.json()['login_saved'])
            self.assertIsNone(self.portal.session)

    async def test_password_is_human_only_bounded_and_never_echoed(self):
        payload={"username":"FAKE_USER","password":"FAKE_PRIVATE_PASSWORD"}
        for path in ('password','pointer','code'):
            response=await self.client.post('/login/'+path,json=payload,headers=self.headers)
            self.assertEqual(response.status_code,401)
        self.assertEqual((await self.client.get('/login/view')).status_code,401)
        await self.authorize()
        response=await self.client.post('/login/password',json=payload)
        self.assertEqual(response.status_code,403)
        self.browser.submit_password.assert_not_awaited()
        response=await self.client.post('/login/password',json={**payload,"password":"x"*257},headers=self.headers)
        self.assertEqual(response.status_code,400)
        response=await self.client.post('/login/password',content=b'x'*9000,headers=self.headers)
        self.assertEqual(response.status_code,400)
        self.browser.submit_password.side_effect=PlaywrightError('FAKE_PRIVATE_PASSWORD')
        response=await self.client.post('/login/password',json=payload,headers=self.headers)
        self.assertEqual(response.status_code,409)
        self.assertNotIn('FAKE_',response.text)
        self.browser.submit_password.side_effect=None
        for _ in range(2):
            response=await self.client.post('/login/password',json=payload,headers=self.headers)
            self.assertEqual(response.status_code,200)
            self.assertNotIn('FAKE_',response.text)
        self.browser.submit_password.assert_awaited_with('FAKE_USER','FAKE_PRIVATE_PASSWORD')
        response=await self.client.post('/login/password',json=payload,headers=self.headers)
        self.assertEqual(response.status_code,429)
        self.assertEqual((await self.client.get('/login/status')).json()['method'],'password')
        view=await self.client.get('/login/view')
        self.assertEqual(view.content,b'fake-view')
        self.assertEqual(view.headers['cache-control'],'no-store')

    async def test_manual_gestures_and_codes_are_scoped_and_validated(self):
        await self.authorize()
        points=[{"x":0.1,"y":0.2,"t":0},{"x":0.5,"y":0.2,"t":350}]
        self.assertEqual((await self.client.post('/login/pointer',json={"points":points},headers=self.headers)).status_code,409)
        await self.client.post('/login/start',json={},headers=self.headers)
        for invalid in ([],points*41,[{"x":-1,"y":0,"t":0}],[{"x":0,"y":0,"t":-1}],
                        [{"x":0,"y":0,"t":15000}],[{"x":0,"y":0,"t":0,"selector":"button"}]):
            response=await self.client.post('/login/pointer',json={"points":invalid},headers=self.headers)
            self.assertEqual(response.status_code,400)
        response=await self.client.post('/login/pointer',json={"points":points},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.browser.human_login_pointer.assert_awaited_once_with(points)
        self.assertEqual((await self.client.post('/login/code',json={"code":"invalid code"},headers=self.headers)).status_code,400)
        response=await self.client.post('/login/code',json={"code":"123456"},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('123456',response.text)
        self.browser.human_login_code.assert_awaited_once_with('123456')
        await self.client.post('/login/close',json={},headers=self.headers)
        self.assertEqual((await self.client.get('/login/view')).status_code,401)
        self.browser.end_login.assert_awaited_once()

    async def test_invalid_public_url_is_rejected(self):
        for url in ('http://public.example/login','https://user:pass@example.test/login',
                    'https://example.test/login?token=x','https://example.test/other','https://example.test/login#x'):
            with self.assertRaises(ValueError): LoginPortal(self.browser,url)


class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_survives_controller_replacement_and_mcp_cannot_navigate_during_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings=replace(Settings.from_env(),profile_dir=Path(tmp)/'profile')
            first=BrowserController(settings)
            first._context=SimpleNamespace(cookies=AsyncMock(return_value=[fixture_cookie()]))
            first.login_until=time.monotonic()+600
            with self.assertRaises(SafetyError): await first.navigate('https://www.jd.com/')
            with self.assertRaises(SafetyError): await first.close()
            self.assertTrue(await first.checkpoint_login())
            second=BrowserController(settings)
            self.assertTrue(likely_logged_in(load_cookies(second.settings.profile_dir),'jd'))
            self.assertEqual(first.login_until,0)

    async def test_human_controls_reject_non_login_pages_and_expired_sessions(self):
        c=BrowserController(Settings.from_env());c.login_until=time.monotonic()+600
        page=SimpleNamespace(url='https://order.jd.com/list',screenshot=AsyncMock())
        c._active_page_unlocked=AsyncMock(return_value=page)
        for call in (lambda:c.submit_password('FAKE_USER','FAKE_PASSWORD'),c.human_login_view,
                     lambda:c.human_login_pointer([{"x":0,"y":0,"t":0}]),lambda:c.human_login_code('123456')):
            with self.assertRaises(SafetyError):await call()
        page.screenshot.assert_not_awaited()
        page.url='https://passport.jd.com/new/login.aspx';c.login_until=time.monotonic()-1
        with self.assertRaises(SafetyError):await c.human_login_view()

    async def test_end_login_clears_transient_page_and_expiry_task(self):
        c=BrowserController(Settings.from_env());c.login_until=time.monotonic()+600
        c._page=SimpleNamespace(goto=AsyncMock())
        c._login_expiry_task=asyncio.create_task(asyncio.sleep(600))
        await c.end_login()
        self.assertEqual(c.login_until,0)
        self.assertIsNone(c._login_expiry_task)
        c._page.goto.assert_awaited_once_with('about:blank')

    async def test_expired_login_stops_the_official_page(self):
        c=BrowserController(Settings.from_env())
        c.login_until=time.monotonic()+0.01
        c._context=SimpleNamespace(cookies=AsyncMock(return_value=[]))
        c._page=SimpleNamespace(goto=AsyncMock())
        await c._expire_login(c.login_until)
        self.assertEqual(c.login_until,0)
        c._page.goto.assert_awaited_once_with('about:blank')

    async def test_qr_capture_rejects_placeholder_or_other_page(self):
        c=BrowserController(Settings.from_env());c.login_until=time.monotonic()+600
        image=SimpleNamespace(get_attribute=AsyncMock(return_value='https://storage.jd.com/placeholder.png'),screenshot=AsyncMock())
        page=SimpleNamespace(url='https://passport.jd.com/new/login.aspx',locator=lambda _:image)
        c._active_page_unlocked=AsyncMock(return_value=page)
        with self.assertRaises(SafetyError): await c.login_qr_png()
        image.screenshot.assert_not_awaited()
        page.url='https://order.jd.com/list'
        with self.assertRaises(SafetyError): await c.login_qr_png()
        image.screenshot.assert_not_awaited()
