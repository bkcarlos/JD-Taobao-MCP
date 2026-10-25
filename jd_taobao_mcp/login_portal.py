"""Short-lived human login portal separate from bearer-protected MCP routes."""
from __future__ import annotations

import asyncio
import json
import math
import re
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
    method: str = "qr"
    password_attempts: int = 0


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
            Route("/login/password", self.password, methods=["POST"]),
            Route("/login/view", self.view),
            Route("/login/gesture", self.gesture, methods=["POST"]),
            Route("/login/code", self.code, methods=["POST"]),
            Route("/login/status", self.status),
            Route("/login/qr", self.qr),
            Route("/login/close", self.close, methods=["POST"]),
        ])

    def issue(self) -> dict:
        """Issue a human-login capability, never the MCP token or account credentials."""
        if self.session and self.session.expires > time.monotonic() and self.session.phase == "waiting":
            raise SafetyError("已有扫码页面正在使用，请完成或关闭后再申请入口。")
        self.session = LoginSession(secrets.token_urlsafe(32), time.monotonic() + 600)
        return {"login_url": self.base_url + "#" + self.session.ticket, "expires_in_seconds": 600,
                "platform": "jd", "message": "请本人打开链接，选择扫码或账号密码登录。链接仅限一次打开，10 分钟内有效。密码、验证码和页面只能由本人在私有入口操作，不要发送给模型。"}

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
            self.session.method = "qr"
            try:
                await self.browser.start_qr_login(self.session.expires)
            except (SafetyError, PlaywrightError):
                return JSONResponse({"error": "无法打开京东登录页，请稍后重试。"}, status_code=503)
            return JSONResponse({"status": "waiting"})

    async def _body(self, request: Request, limit: int = 8192) -> dict:
        """Read a bounded object without request-body logs or value-bearing errors."""
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > limit:
                raise ValueError("Request too large")
        try:
            data = json.loads(raw)
        finally:
            raw.clear()
        if not isinstance(data, dict):
            raise ValueError("Invalid request")
        return data

    async def password(self, request: Request) -> Response:
        """Accept credentials only through the human session, never an MCP tool."""
        async with self.lock:
            if not self.authorized(request):
                return JSONResponse({"error": "登录页已过期，请重新生成链接。"}, status_code=401)
            assert self.session is not None
            if self.session.phase == "saved":
                return JSONResponse({"status": "saved"})
            if self.session.password_attempts >= 3:
                return JSONResponse({"error": "本次入口提交次数已达上限，请关闭入口后重新生成链接。"}, status_code=429)
            data = {}
            username = password = ""
            try:
                data = await self._body(request)
                username, password = data.get("username"), data.get("password")
                if (not isinstance(username, str) or not isinstance(password, str)
                        or not 1 <= len(username.strip()) <= 256 or not 1 <= len(password) <= 256
                        or any(ord(c) < 32 for c in username+password)):
                    raise ValueError("Invalid credentials")
                self.session.password_attempts += 1
                self.session.phase = "waiting"
                self.session.method = "password"
                await self.browser.start_qr_login(self.session.expires)
                await self.browser.submit_password(username.strip(), password)
                return JSONResponse({"status": "waiting", "method": "password"})
            except (ValueError, TypeError):
                return JSONResponse({"error": "请输入有效的账号和密码。"}, status_code=400)
            except (SafetyError, PlaywrightError):
                return JSONResponse({"error": "提交未完成，请在下方京东页面查看并处理验证。"}, status_code=409)
            finally:
                # Immutable strings cannot be securely zeroed; keep no stored references.
                data.clear()
                username = password = ""

    async def view(self, request: Request) -> Response:
        """Expose the official login document only to the authenticated human."""
        async with self.lock:
            if not self.authorized(request):
                return Response(status_code=401)
            assert self.session is not None
            if self.session.phase != "waiting":
                return Response(status_code=409)
            try:
                return Response(await self.browser.human_login_view(), media_type="image/png")
            except (SafetyError, PlaywrightError):
                return JSONResponse({"error": "登录页面已跳转或过期，请检查登录状态。"}, status_code=409)

    async def gesture(self, request: Request) -> Response:
        """Authenticate and bound live human pointer events; never synthesize a solution."""
        async with self.lock:
            if not self.authorized(request):
                return JSONResponse({"error": "登录页已过期。"}, status_code=401)
            assert self.session is not None
            if self.session.phase != "waiting":
                return JSONResponse({"error": "请先开始登录。"}, status_code=409)
            try:
                data = await self._body(request, limit=1024)
                phase, gesture_id, point = data.get("phase"), data.get("id"), data.get("point")
                if (set(data) != {"phase", "id", "point"}
                        or phase not in ("down", "move", "up", "cancel") or not isinstance(gesture_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", gesture_id)
                        or not isinstance(point, dict) or set(point) != {"x", "y"}
                        or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                               for v in point.values())):
                    raise ValueError()
                await self.browser.human_login_gesture(phase, gesture_id, point)
                return JSONResponse({"ok": True})
            except (ValueError, TypeError, OverflowError):
                return JSONResponse({"error": "无效的鼠标操作。"}, status_code=400)
            except (SafetyError, PlaywrightError):
                return JSONResponse({"error": "拖动已结束或页面已改变，请重新操作。"}, status_code=409)

    async def code(self, request: Request) -> Response:
        """Deliver human-entered verification text without retaining or echoing it."""
        async with self.lock:
            if not self.authorized(request):
                return JSONResponse({"error": "登录页已过期。"}, status_code=401)
            assert self.session is not None
            if self.session.phase != "waiting":
                return JSONResponse({"error": "请先开始登录。"}, status_code=409)
            data = {}
            try:
                data = await self._body(request)
                value = data.get("code")
                if not isinstance(value, str) or not 1 <= len(value) <= 16 or not value.isascii() or not value.isalnum():
                    raise ValueError()
                await self.browser.human_login_code(value)
                return JSONResponse({"ok": True})
            except (ValueError, TypeError):
                return JSONResponse({"error": "请输入有效验证码。"}, status_code=400)
            except (SafetyError, PlaywrightError):
                return JSONResponse({"error": "请先在京东页面点选验证码输入框，再填入验证码。"}, status_code=409)
            finally:
                data.clear()

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
            return JSONResponse({"status": self.session.phase, "method": self.session.method,
                                 "navigation_blocked": self.browser.login_navigation_blocked})

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
            await self.browser.end_login()
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


_HTML = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>京东登录</title>
<style nonce="NONCE">
*{box-sizing:border-box}body{font:16px system-ui;margin:5vh auto;padding:20px;color:#171717;background:#fafafa;max-width:980px}main{max-width:460px;margin:auto;background:white;border:1px solid #ddd;border-radius:16px;padding:24px}body.wide main{max-width:940px}h1{font-size:24px;margin-top:0}button,input{font:inherit;padding:10px 14px;border:1px solid #999;border-radius:8px}button{margin:6px 6px 6px 0;background:white;cursor:pointer}button:disabled{opacity:.45}input{width:100%;margin:6px 0 12px}label{display:block}p{line-height:1.6;color:#555}small{display:block;line-height:1.5;color:#555}#qr{display:block;width:220px;max-width:100%;height:auto;margin:24px auto;image-rendering:pixelated}#view{display:block;width:100%;height:auto;touch-action:none;user-select:none;cursor:crosshair;border:0}#verification{border-top:1px solid #ddd;margin-top:18px;padding-top:16px}#credentials{max-width:410px}#code{max-width:220px;display:inline-block}h2{font-size:18px}[hidden]{display:none!important}
</style></head><body><main><h1>京东登录</h1><p id="status" role="status">正在验证入口…</p>
<div><button id="start" disabled>显示二维码</button><button id="passwordMode" disabled>账号密码登录</button></div>
<img id="qr" hidden alt="京东登录二维码">
<form id="credentials" hidden><p>账号密码仅提交到你的私有登录服务，由服务器上的京东官方页面处理；不写入日志或发送给模型。</p><label for="username">账号</label><input id="username" autocomplete="username" maxlength="256" required><label for="password">密码</label><input id="password" type="password" autocomplete="current-password" maxlength="256" required><button id="submitPassword" type="submit">登录</button></form>
<button id="showView" hidden>查看京东验证页面</button>
<section id="verification" hidden><h2>京东官方登录页面</h2><p>可直接点击或拖动下方页面，画面会同步更新。点击验证码输入框后，可填入验证码。</p><img id="view" alt="京东官方登录页面，仅供本人操作" draggable="false"><div><label for="code">验证码</label><input id="code" autocomplete="one-time-code" maxlength="16"><button id="sendCode">填入验证码</button></div></section>
<button id="close" disabled>关闭入口</button></main><script nonce="NONCE">
const el=id=>document.getElementById(id),status=el('status'),qr=el('qr'),view=el('view'),start=el('start'),close=el('close'),mode=el('passwordMode'),form=el('credentials'),showView=el('showView'),verification=el('verification');
let stopped=false,timer=null,epoch=0,qrUrl=null,viewUrl=null,misses=0,method='qr',inAction=false,gesture=null,viewLoading=false;
const ticket=location.hash.slice(1);history.replaceState(null,'','/login');
window.addEventListener('hashchange',()=>{if(location.hash)location.reload();});
async function api(path,body){
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),path==='gesture'?8000:45000);
  try{
    const response=await fetch('/login/'+path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',signal:controller.signal,headers:body?{'Content-Type':'application/json','X-Login-Action':'1'}:{},body:body?JSON.stringify(body):undefined});
    const data=await response.json();if(!response.ok)throw Error(data.error||'登录页已过期，请重新生成链接。');return data;
  }catch(error){if(controller.signal.aborted)throw Error('连接超时，请重试。');throw error;}
  finally{clearTimeout(timeout);}
}
function hideQr(){qr.hidden=true;if(qrUrl){URL.revokeObjectURL(qrUrl);qrUrl=null;}}
function clearView(){verification.hidden=true;document.body.classList.remove('wide');if(viewUrl){URL.revokeObjectURL(viewUrl);viewUrl=null;}}
function stop(){cancelGesture();stopped=true;epoch++;clearTimeout(timer);hideQr();clearView();el('password').value='';el('code').value='';}
function saved(){stop();form.hidden=true;start.disabled=true;mode.disabled=true;showView.hidden=true;status.textContent='登录状态已保存，可以回到 Rakazo 查询商品。有效性以实际查询为准。';}
async function refreshView(live=false){
  if(viewLoading||verification.hidden||stopped||(!live&&(gesture||inAction)))return;
  const mine=epoch,controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),8000);viewLoading=true;
  try{
    const response=await fetch('/login/view',{credentials:'same-origin',cache:'no-store',signal:controller.signal});
    if(response.ok){
      const blob=await response.blob();
      if(mine!==epoch||stopped||verification.hidden||(!live&&(gesture||inAction)))return;
      const next=URL.createObjectURL(blob);if(viewUrl)URL.revokeObjectURL(viewUrl);viewUrl=next;view.src=next;
    }else{
      const error=await response.json().catch(()=>({}));
      if(mine!==epoch||stopped)return;
      status.textContent=error.error||'验证页面不可用，请重新生成登录链接。';if(response.status===401)stop();
    }
  }finally{clearTimeout(timeout);viewLoading=false;}
}
async function poll(mine=epoch){if(stopped||mine!==epoch)return;try{const state=await api('status');if(stopped||mine!==epoch)return;if(state.status==='saved'){saved();return;}method=state.method||method;if(state.status==='waiting'){showView.hidden=false;if(state.navigation_blocked){status.textContent='京东登录跳转被安全规则阻止，尚未完成登录。请联系管理员。';hideQr();}else if(method==='qr'&&verification.hidden){const response=await fetch('/login/qr',{credentials:'same-origin',cache:'no-store'});if(stopped||mine!==epoch)return;if(response.ok){const next=URL.createObjectURL(await response.blob());if(qrUrl)URL.revokeObjectURL(qrUrl);qrUrl=next;qr.src=next;qr.hidden=false;misses=0;status.textContent='用京东 App 扫码，并在手机上确认；等待这里显示登录状态已保存。';}else{hideQr();if(++misses>3)status.textContent='尚未检测到登录。可打开京东验证页面查看是否需要确认或验证。';}}await refreshView();}if(!stopped&&mine===epoch)timer=setTimeout(()=>poll(mine),3000);}catch(error){if(mine===epoch){stop();status.textContent=error.message;}}}
start.onclick=async()=>{stop();form.hidden=true;el('username').value='';start.disabled=true;mode.disabled=true;status.textContent='正在加载京东二维码…';try{const state=await api('start',{});if(state.status==='saved'){saved();return;}method='qr';stopped=false;start.textContent='刷新二维码';await poll();}catch(error){status.textContent=error.message;}finally{start.disabled=false;mode.disabled=false;}};
mode.onclick=()=>{hideQr();form.hidden=false;el('username').focus();};
form.onsubmit=async(event)=>{event.preventDefault();const credentials={username:el('username').value,password:el('password').value};stop();start.disabled=true;mode.disabled=true;el('submitPassword').disabled=true;el('password').value='';status.textContent='正在向京东提交登录，请稍候…';let message='请在下方京东页面完成可能出现的验证，并等待登录状态已保存。';try{const state=await api('password',credentials);if(state.status==='saved'){saved();return;}}catch(error){message=error.message;}finally{credentials.password='';credentials.username='';el('username').value='';start.disabled=false;mode.disabled=false;el('submitPassword').disabled=false;}method='password';stopped=false;verification.hidden=false;document.body.classList.add('wide');showView.hidden=false;status.textContent=message;await poll();};
showView.onclick=async()=>{verification.hidden=false;hideQr();document.body.classList.add('wide');await refreshView();};
function point(event){const box=view.getBoundingClientRect();return{x:Math.max(0,Math.min(1,(event.clientX-box.left)/box.width)),y:Math.max(0,Math.min(1,(event.clientY-box.top)/box.height))};}
function finishGesture(g){clearTimeout(g.timer);if(gesture===g){gesture=null;inAction=false;}if(view.hasPointerCapture(g.pointerId))view.releasePointerCapture(g.pointerId);}
async function pumpGesture(g){
  if(g.busy)return;g.busy=true;
  try{
    while(gesture===g&&(g.pending||g.finish)){
      const phase=!g.started?'down':g.finish||'move',position=!g.started?g.first:g.latest;
      g.started=true;g.pending=false;
      await api('gesture',{phase,id:g.id,point:position});
      if(phase==='up'||phase==='cancel'){finishGesture(g);break;}
      await refreshView(true);
    }
  }catch(error){
    status.textContent=error.message;
    try{await api('gesture',{phase:'cancel',id:g.id,point:g.latest});}catch{}
    finishGesture(g);
  }finally{g.busy=false;try{await refreshView();}catch{status.textContent='画面更新失败，请重试。';}}
}
function cancelGesture(){if(!gesture)return;gesture.finish='cancel';void pumpGesture(gesture);}
view.onpointerdown=event=>{
  if(gesture||inAction||stopped||event.button!==0||!event.isPrimary)return;
  event.preventDefault();const position=point(event);
  const g={id:crypto.randomUUID(),pointerId:event.pointerId,first:position,latest:position,pending:true,started:false,busy:false,finish:null};
  gesture=g;inAction=true;view.setPointerCapture(event.pointerId);
  g.timer=setTimeout(()=>{if(gesture===g){status.textContent='拖动超时，请重新操作。';cancelGesture();}},14000);
  void pumpGesture(g);
};
view.onpointermove=event=>{if(!gesture||gesture.pointerId!==event.pointerId||gesture.finish)return;event.preventDefault();gesture.latest=point(event);gesture.pending=true;void pumpGesture(gesture);};
view.onpointerup=event=>{if(!gesture||gesture.pointerId!==event.pointerId)return;event.preventDefault();gesture.latest=point(event);gesture.finish='up';void pumpGesture(gesture);};
view.onpointercancel=event=>{if(gesture?.pointerId===event.pointerId)cancelGesture();};
view.onlostpointercapture=event=>{if(gesture?.pointerId===event.pointerId&&!gesture.finish)cancelGesture();};
window.addEventListener('blur',cancelGesture);
window.addEventListener('pagehide',cancelGesture);
window.addEventListener('resize',cancelGesture);
el('sendCode').onclick=async()=>{const payload={code:el('code').value};el('code').value='';inAction=true;try{await api('code',payload);}catch(error){status.textContent=error.message;}finally{payload.code='';inAction=false;await refreshView();}};
close.onclick=async()=>{try{const result=await api('close',{});stop();form.hidden=true;start.disabled=true;mode.disabled=true;showView.hidden=true;close.disabled=true;status.textContent=result.login_saved?'入口已关闭，登录状态已保存。':'入口已关闭，本次未确认登录成功。请重新生成链接并完成登录。';}catch(error){status.textContent=error.message;}};
(async()=>{try{if(ticket)await api('session',{ticket});const state=await api('status');close.disabled=false;if(state.status==='saved'){saved();return;}start.disabled=false;mode.disabled=false;status.textContent='请选择登录方式。入口 10 分钟内有效。';if(state.status==='waiting'){method=state.method||'qr';start.textContent='刷新二维码';if(method==='password'){verification.hidden=false;document.body.classList.add('wide');}poll();}}catch(error){stop();status.textContent=error.message;}})();
</script></body></html>
'''
