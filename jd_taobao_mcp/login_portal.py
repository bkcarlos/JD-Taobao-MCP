"""Short-lived, human-only QR portal separate from bearer-protected MCP routes."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .browser import BrowserController
from .safety import SafetyError


@dataclass
class LoginSession:
    ticket: str
    expires: float
    cookie: str = ""
    phase: str = "ready"


class LoginPortal:
    """Exchange single-use links for short-lived HttpOnly browser sessions."""

    def __init__(self, browser: BrowserController, base_url: str):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/login"
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("LOGIN_PUBLIC_URL must be an HTTPS /login URL, or HTTP on loopback.")
        if any(ord(c) <= 32 for c in base_url) or "\\" in base_url:
            raise ValueError("Invalid LOGIN_PUBLIC_URL.")
        _ = parsed.port
        self.base_url = base_url.rstrip("/")
        self.authority = parsed.netloc
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.secure = parsed.scheme == "https"
        self.browser = browser
        self.session: LoginSession | None = None
        self.lock = asyncio.Lock()
        self.app = Starlette(routes=[
            Route("/login", self.page),
            Route("/login/", self.page),
            Route("/login/session", self.exchange, methods=["POST"]),
            Route("/login/start", self.start, methods=["POST"]),
            Route("/login/status", self.status),
            Route("/login/qr", self.qr),
            Route("/login/close", self.close, methods=["POST"]),
        ])

    def issue(self) -> dict:
        """Issue a QR-only capability, never the MCP token or account credentials."""
        if self.session and self.session.expires > time.monotonic() and self.session.phase == "waiting":
            raise SafetyError("已有扫码页面正在使用，请完成或关闭后再申请入口。")
        self.session = LoginSession(secrets.token_urlsafe(32), time.monotonic() + 600)
        return {"login_url": self.base_url + "#" + self.session.ticket, "expires_in_seconds": 600,
                "platform": "jd", "message": "请本人打开链接，用京东 App 扫码并确认。链接仅限一次打开，10 分钟内有效。不要将二维码发送给模型。"}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope)
        if request.headers.get("host") != self.authority:
            response = JSONResponse({"error": "Invalid Host"}, status_code=421)
        elif request.headers.get("origin") not in (None, self.origin):
            response = JSONResponse({"error": "Invalid Origin"}, status_code=403)
        elif request.method == "POST" and (request.headers.get("origin") != self.origin
                                           or request.headers.get("x-login-action") != "1"):
            response = JSONResponse({"error": "Invalid login request"}, status_code=403)
        else:
            async def secured(message):
                if message["type"] == "http.response.start":
                    message["headers"] += [(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
                                           (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY")]
                await send(message)
            await self.app(scope, receive, secured)
            return
        await response(scope, receive, send)

    def authorized(self, request: Request) -> bool:
        """Require the exchanged cookie; links cannot directly call privileged routes."""
        current = self.session
        cookie = request.cookies.get("shopping_login", "")
        return bool(cookie.isascii() and current and current.cookie and current.expires > time.monotonic()
                    and secrets.compare_digest(cookie, current.cookie))

    async def page(self, request: Request) -> Response:
        """Serve a static shell; no QR or account data is embedded in the document."""
        nonce = secrets.token_urlsafe(18)
        response = HTMLResponse(_HTML.replace("NONCE", nonce))
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        return response

    async def exchange(self, request: Request) -> Response:
        """Consume a link once without putting its capability in access logs or cookies."""
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 1024:
                return JSONResponse({"error": "Request too large"}, status_code=413)
        try:
            token = json.loads(raw).get("ticket", "")
        except (ValueError, AttributeError):
            token = ""
        async with self.lock:
            current = self.session
            if (not isinstance(token, str) or not token.isascii() or not current or not current.ticket
                    or current.expires <= time.monotonic() or not secrets.compare_digest(token, current.ticket)):
                return JSONResponse({"error": "链接已失效，请让 bot 重新生成。"}, status_code=401)
            current.ticket = ""
            current.cookie = secrets.token_urlsafe(32)
            response = JSONResponse({"ok": True})
            response.set_cookie("shopping_login", current.cookie, max_age=max(1, int(current.expires-time.monotonic())),
                                httponly=True, secure=self.secure, samesite="strict", path="/login")
            return response

    async def start(self, request: Request) -> Response:
        """Only the human's explicit button starts or refreshes QR sign-in."""
        async with self.lock:
            if not self.authorized(request):
                return JSONResponse({"error": "登录页已过期，请让 bot 重新生成链接。"}, status_code=401)
            assert self.session is not None
            if self.session.phase == "saved":
                return JSONResponse({"status": "saved"})
            self.session.phase = "waiting"
            try:
                await self.browser.start_qr_login(self.session.expires)
            except (SafetyError, PlaywrightError):
                return JSONResponse({"error": "无法打开京东登录页，请稍后重试。"}, status_code=503)
            return JSONResponse({"status": "waiting"})

    async def status(self, request: Request) -> Response:
        """Checkpoint only a detected login; return no cookies, names or account IDs."""
        async with self.lock:
            if not self.authorized(request):
                return JSONResponse({"error": "登录页已过期，请让 bot 重新生成链接。"}, status_code=401)
            assert self.session is not None
            if self.session.phase == "waiting":
                try:
                    if await self.browser.checkpoint_login():
                        self.session.phase = "saved"
                except (OSError, SafetyError, PlaywrightError):
                    return JSONResponse({"error": "登录状态未保存，请重试。"}, status_code=503)
            return JSONResponse({"status": self.session.phase})

    async def qr(self, request: Request) -> Response:
        """Return a QR crop to the authenticated human only, without a generic screenshot API."""
        async with self.lock:
            if not self.authorized(request):
                return Response(status_code=401)
            assert self.session is not None
            if self.session.phase != "waiting":
                return Response(status_code=409)
            try:
                data = await self.browser.login_qr_png()
            except (SafetyError, PlaywrightError):
                return JSONResponse({"error": "二维码尚未就绪或需要人工验证，请刷新重试。"}, status_code=409)
            return Response(data, media_type="image/png")

    async def close(self, request: Request) -> Response:
        """Revoke the portal session without deleting the user's saved login state."""
        async with self.lock:
            if not self.authorized(request):
                return Response(status_code=401)
            saved = bool(self.session and self.session.phase == "saved")
            if self.session and self.session.phase == "waiting":
                try:
                    saved = await self.browser.checkpoint_login()
                except (OSError, SafetyError, PlaywrightError):
                    return JSONResponse({"error": "登录状态未保存，请重试关闭。"}, status_code=503)
            self.browser.login_until = 0.0
            self.session = None
            response = JSONResponse({"ok": True, "login_saved": saved})
            response.delete_cookie("shopping_login", path="/login", secure=self.secure, httponly=True, samesite="strict")
            return response


class WithLoginPortal:
    """Keep MCP authentication and lifespan intact while isolating the login routes."""

    def __init__(self, protected_mcp, portal: LoginPortal):
        self.protected_mcp, self.portal = protected_mcp, portal

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        target = self.portal if path == "/login" or path.startswith("/login/") else self.protected_mcp
        await target(scope, receive, send)


_HTML = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>京东扫码登录</title>
<style nonce="NONCE">body{font:16px system-ui;max-width:420px;margin:8vh auto;padding:24px;color:#171717;background:#fafafa}main{background:white;border:1px solid #ddd;border-radius:16px;padding:28px}h1{font-size:24px}button{font:inherit;padding:10px 16px;margin:8px 6px 8px 0;border:1px solid #999;border-radius:8px;background:white;cursor:pointer}button:disabled{opacity:.45}img{display:block;width:220px;height:220px;margin:24px auto;image-rendering:pixelated}img[hidden]{display:none}p{line-height:1.6;color:#555}</style></head><body><main><h1>京东扫码登录</h1><p id="status" role="status">正在验证入口…</p><img id="qr" hidden alt="京东登录二维码"><button id="start" disabled>显示二维码</button><button id="close" disabled>关闭入口</button></main>
<script nonce="NONCE">
const status=document.querySelector('#status'),qr=document.querySelector('#qr'),start=document.querySelector('#start'),close=document.querySelector('#close');
let stopped=false,objectUrl=null,timer=null,misses=0;
const ticket=location.hash.slice(1);history.replaceState(null,'','/login');
window.addEventListener('hashchange',()=>{if(location.hash)location.reload();});
async function api(path,body){const r=await fetch('/login/'+path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',headers:body?{'Content-Type':'application/json','X-Login-Action':'1'}:{},body:body?JSON.stringify(body):undefined});const data=await r.json();if(!r.ok)throw Error(data.error||'登录页已过期，请重新生成链接。');return data;}
function hide(){qr.hidden=true;if(objectUrl){URL.revokeObjectURL(objectUrl);objectUrl=null;}}
function fail(e){status.textContent=e.message;stopped=true;clearTimeout(timer);hide();}
async function poll(){if(stopped)return;try{const s=await api('status');if(s.status==='saved'){status.textContent='登录状态已保存，可以回到 Rakazo 查询商品。有效性以实际查询为准。';hide();start.disabled=true;stopped=true;return;}if(s.status==='waiting'){const r=await fetch('/login/qr',{credentials:'same-origin',cache:'no-store'});if(r.ok){const next=URL.createObjectURL(await r.blob());if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl=next;qr.src=next;qr.hidden=false;misses=0;status.textContent='用京东 App 扫码，并在手机上确认。';}else{hide();if(++misses>3)status.textContent='二维码尚未就绪、已过期或需人工验证。可尝试刷新二维码。';}}timer=setTimeout(poll,3000);}catch(e){fail(e);}}
start.onclick=async()=>{start.disabled=true;clearTimeout(timer);stopped=true;hide();status.textContent='正在加载京东二维码…';try{const r=await api('start',{});if(r.status==='saved'){status.textContent='登录状态已保存。';return;}stopped=false;start.textContent='刷新二维码';await poll();}catch(e){fail(e);}finally{if(!stopped)start.disabled=false;else if(!status.textContent.includes('已保存'))start.disabled=false;}};
close.onclick=async()=>{try{const result=await api('close',{});stopped=true;clearTimeout(timer);hide();start.disabled=true;close.disabled=true;status.textContent=result.login_saved?'入口已关闭，登录状态已保存。':'入口已关闭，本次未确认登录成功。请重新生成链接并完成扫码。';}catch(e){fail(e);}};
(async()=>{try{if(ticket)await api('session',{ticket});const s=await api('status');close.disabled=false;if(s.status==='saved'){status.textContent='登录状态已保存，可以回到 Rakazo。';return;}start.disabled=false;status.textContent='二维码仅供你本人扫码。入口 10 分钟内有效。';if(s.status==='waiting'){start.textContent='刷新二维码';poll();}}catch(e){fail(e);}})();
</script></body></html>'''
