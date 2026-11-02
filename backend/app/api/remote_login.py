"""Authenticated HTTP viewer and binary WebSocket relay for an isolated desktop."""
import asyncio
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse

from app.api.routes import require_admin, require_writable
from app.core.auth import COOKIE_NAME, auth_required, current_user, validate_origin
from app.core.config import settings
from app.services import remote_login as remote

router = APIRouter(prefix="/remote-login")
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}


def admin(request: Request):
    # Production browser control must fail closed even if personal login was
    # accidentally disabled on the host. Local Windows still uses local Connect.
    if not auth_required():
        raise HTTPException(403, "Enable individual dashboard login before using remote login.")
    if request.url.scheme != "https" and request.url.hostname not in {"localhost", "127.0.0.1"}:
        raise HTTPException(403, "Open the dashboard over HTTPS to connect an account.")
    require_writable()
    user = current_user(request.cookies.get(COOKIE_NAME))
    if not user["authenticated"] or not user["is_admin"]:
        raise HTTPException(403, "Administrator sign-in required.")
    validate_origin(request)
    return user


def owned(sid: str, user: dict):
    s = remote.desktop
    if s is None or s.id != sid or s.owner != user["email"]:
        raise HTTPException(404, "Login window not found.")
    return s


@router.get("/config", dependencies=[Depends(require_admin)])
def config():
    # Linux always uses the browser flow; missing dependencies are an actionable
    # setup error rather than accidentally opening Chrome on an unseen display.
    import sys
    return {"remote": sys.platform.startswith("linux"), "installed": remote.available()}


@router.post("/start")
async def start(user=Depends(admin)):
    try:
        return (await remote.start(user["email"])).public()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/{sid}/status")
def status(sid: str, user=Depends(admin)):
    return owned(sid, user).public()


@router.post("/{sid}/finish")
def finish(sid: str, user=Depends(admin)):
    s = owned(sid, user)
    if s.state == "ready":
        s.finish.set()
    return s.public()


@router.post("/{sid}/cancel")
def cancel(sid: str, user=Depends(admin)):
    s = owned(sid, user)
    s.cancel.set()
    return s.public()


@router.get("/{sid}/assets/{asset:path}")
def assets(sid: str, asset: str, user=Depends(admin)):
    owned(sid, user)
    root = remote.NOVNC.resolve()
    path = (root / asset).resolve()
    if (not path.is_relative_to(root) or not path.is_file()
            or Path(asset).parts[0] not in {"core", "vendor"} or path.suffix != ".js"):
        raise HTTPException(404)
    return FileResponse(path, media_type="text/javascript", headers=HEADERS)


@router.get("/{sid}/viewer", response_class=HTMLResponse)
def viewer(sid: str, user=Depends(admin)):
    owned(sid, user)
    # No credentials/tokens in URLs, CDN dependencies, or third-party scripts.
    return HTMLResponse(VIEWER, headers={**HEADERS, "Content-Security-Policy":
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'"})


@router.websocket("/{sid}/socket")
async def socket_relay(ws: WebSocket, sid: str):
    user = await asyncio.to_thread(current_user, ws.cookies.get(COOKIE_NAME))
    # WebSockets bypass HTTP middleware. Check the live role, owner, exact
    # Origin, session expiry and read-only mode explicitly here.
    origin = urlsplit(ws.headers.get("origin", ""))
    same_origin = origin.scheme in ("http", "https") and origin.netloc == ws.headers.get("host")
    s = remote.desktop
    if (not auth_required() or settings.read_only or not same_origin
            or not user["authenticated"] or not user["is_admin"] or s is None
            or s.id != sid or s.owner != user["email"] or s.state != "ready" or s.attached):
        await ws.close(code=1008)
        return
    s.attached = True
    writer = None
    tasks = []
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", s.port)
        await ws.accept(subprotocol="binary" if "binary" in ws.scope.get("subprotocols", []) else None)

        async def incoming():
            while True:
                data = await ws.receive_bytes()
                writer.write(data)
                await writer.drain()

        async def outgoing():
            while data := await reader.read(65536):
                await ws.send_bytes(data)

        async def authorised():
            while s.state == "ready":
                await asyncio.sleep(2)
                live = await asyncio.to_thread(current_user, ws.cookies.get(COOKIE_NAME))
                if not live["authenticated"] or not live["is_admin"]:
                    return

        tasks = [asyncio.create_task(fn()) for fn in (incoming, outgoing, authorised)]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if writer:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
        s.attached = False
        with suppress(Exception):
            await ws.close()


VIEWER = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect DevelopmentAid</title>
<style>body{margin:0;background:#101827;color:#fff;font:15px system-ui}header{padding:14px;display:flex;gap:12px;align-items:center}button{padding:10px 18px;cursor:pointer}#message{flex:1}#screen{height:calc(100vh - 78px)}</style></head>
<body><header><span id="message">Opening the login window…</span>
<button id="done" disabled>Done — save login</button><button id="cancel">Cancel</button></header>
<div id="screen"></div><script type="module">
import RFB from './assets/core/rfb.js';
const base = location.pathname.replace(/\\/viewer$/, '');
const message = document.querySelector('#message'), done = document.querySelector('#done');
let rfb, stopped = false, saving = false;
async function action(name) {
  const r = await fetch(base + '/' + name, {method:'POST'});
  if (!r.ok) throw new Error('Your session ended. Return to the dashboard and sign in again.');
}
done.onclick = async () => { done.disabled = true; saving = true;
  try { await action('finish'); } catch(e) { message.textContent = e.message; } };
document.querySelector('#cancel').onclick = async () => {
  try { await action('cancel'); } catch(e) { message.textContent = e.message; }
};
async function poll() {
  if(stopped) return;
  try {
    const r = await fetch(base + '/status', {cache:'no-store'});
    if(!r.ok) throw new Error('Login access ended. Return to the dashboard.');
    const s = await r.json(); message.textContent = s.message;
    done.disabled = saving || s.state !== 'ready';
    if(s.state === 'ready' && !rfb) {
      const url = new URL(base + '/socket', location.origin);
      url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
      rfb = new RFB(document.querySelector('#screen'), url.href);
      rfb.scaleViewport = true; rfb.resizeSession = false;
      rfb.addEventListener('disconnect', () => {
        if(!stopped && !saving) message.textContent = 'Viewer disconnected. Refresh this tab to reconnect.';
      });
    }
    if(['connected','unverified','cancelled','expired','error'].includes(s.state)) {
      stopped = true; rfb?.disconnect(); document.querySelector('#cancel').disabled = true;
    }
  } catch(e) { stopped = true; rfb?.disconnect(); message.textContent = e.message; }
  if(!stopped) setTimeout(poll, 1000);
}
poll();
</script></body></html>"""
