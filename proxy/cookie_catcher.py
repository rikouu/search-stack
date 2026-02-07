"""Cookie Catcher — remote browser CDP session for capturing login cookies.

Uses browser-level CDP connection to Browserless (ws://browserless:3000?token=...)
with Target.createTarget / Target.attachToTarget for page management.
"""

import asyncio
import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

log = logging.getLogger("cookie-catcher")

MAX_SESSIONS = 2
SESSION_TIMEOUT = 600  # 10 minutes

_sessions: Dict[str, "CatcherSession"] = {}


class CatcherSession:
    """Manages a remote browser page via Chrome DevTools Protocol."""

    def __init__(self, browserless_http: str, browserless_token: str):
        self.id = secrets.token_hex(8)
        self.browserless_http = browserless_http
        self.browserless_token = browserless_token
        self._ws: Any = None
        self._session_id: str = ""  # CDP session ID for the page target
        self._target_id: str = ""
        self._msg_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.Task] = None
        self._timer: Optional[asyncio.Task] = None
        self.created_at = time.time()
        self.closed = False
        self.url = ""
        self.title = ""
        # Callbacks
        self.on_frame: Optional[Callable[[str, dict], Awaitable]] = None
        self.on_url: Optional[Callable[[str], Awaitable]] = None
        self.on_title: Optional[Callable[[str], Awaitable]] = None
        self.on_close: Optional[Callable[[], Awaitable]] = None

    async def start(self, url: str) -> None:
        """Connect to Browserless browser-level CDP, create page, start screencast."""
        import websockets

        # Connect to browser-level CDP endpoint
        bl = urlparse(self.browserless_http)
        host = bl.hostname or "browserless"
        port = bl.port or 3000
        ws_url = f"ws://{host}:{port}?token={self.browserless_token}"

        log.info("[%s] CDP browser connecting → %s", self.id, ws_url)
        self._ws = await websockets.connect(ws_url, max_size=10_000_000)
        self._reader = asyncio.create_task(self._read_loop())

        # Create a new page target
        result = await self._cdp_browser("Target.createTarget", url="about:blank")
        self._target_id = result["targetId"]

        # Attach to target with flatten=true (page commands go over same WS)
        result = await self._cdp_browser(
            "Target.attachToTarget", targetId=self._target_id, flatten=True,
        )
        self._session_id = result["sessionId"]
        log.info("[%s] attached session=%s target=%s", self.id, self._session_id, self._target_id)

        # Setup page
        await self._cdp("Page.enable")
        await self._cdp("Network.enable")
        await self._cdp(
            "Emulation.setDeviceMetricsOverride",
            width=1280, height=800, deviceScaleFactor=1, mobile=False,
        )
        await self._cdp("Page.navigate", url=url)
        self.url = url
        await asyncio.sleep(0.5)
        await self._cdp(
            "Page.startScreencast",
            format="jpeg", quality=60, maxWidth=1280, maxHeight=800,
        )

        self._timer = asyncio.create_task(self._auto_timeout())
        _sessions[self.id] = self
        log.info("[%s] session started → %s", self.id, url)

    # ---- CDP transport ----

    async def _cdp_browser(self, method: str, **params: Any) -> Any:
        """Send a browser-level CDP command (no sessionId)."""
        if self.closed or not self._ws:
            raise RuntimeError("Session closed")
        self._msg_id += 1
        mid = self._msg_id
        msg: Dict[str, Any] = {"id": mid, "method": method}
        if params:
            msg["params"] = params
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise

    async def _cdp(self, method: str, **params: Any) -> Any:
        """Send a page-level CDP command (with sessionId)."""
        if self.closed or not self._ws:
            raise RuntimeError("Session closed")
        self._msg_id += 1
        mid = self._msg_id
        msg: Dict[str, Any] = {"id": mid, "method": method, "sessionId": self._session_id}
        if params:
            msg["params"] = params
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                RuntimeError(str(msg["error"].get("message", msg["error"])))
                            )
                        else:
                            fut.set_result(msg.get("result"))
                elif "method" in msg:
                    # Only handle events from our page session
                    if msg.get("sessionId") == self._session_id or not msg.get("sessionId"):
                        asyncio.create_task(
                            self._on_event(msg["method"], msg.get("params", {}))
                        )
        except Exception as e:
            if not self.closed:
                log.warning("[%s] CDP connection lost: %s", self.id, e)
                await self.close()

    async def _on_event(self, method: str, params: dict) -> None:
        try:
            if method == "Page.screencastFrame":
                # Acknowledge frame to receive next one
                sid = params.get("sessionId")
                if sid is not None:
                    self._msg_id += 1
                    ack: Dict[str, Any] = {
                        "id": self._msg_id,
                        "method": "Page.screencastFrameAck",
                        "params": {"sessionId": sid},
                        "sessionId": self._session_id,
                    }
                    await self._ws.send(json.dumps(ack))
                if self.on_frame:
                    await self.on_frame(params.get("data", ""), params.get("metadata", {}))

            elif method == "Page.frameNavigated":
                frame = params.get("frame", {})
                if not frame.get("parentId"):  # top-level frame only
                    u = frame.get("url", "")
                    if u:
                        self.url = u
                        if self.on_url:
                            await self.on_url(u)

            elif method in ("Page.loadEventFired", "Page.domContentEventFired"):
                try:
                    r = await self._cdp("Runtime.evaluate", expression="document.title")
                    t = r.get("result", {}).get("value", "")
                    if t and t != self.title:
                        self.title = t
                        if self.on_title:
                            await self.on_title(t)
                except Exception:
                    pass
        except Exception as e:
            log.debug("[%s] event error %s: %s", self.id, method, e)

    # ---- Input forwarding ----

    async def navigate(self, url: str) -> None:
        await self._cdp("Page.navigate", url=url)

    async def mouse(self, action: str, x: float, y: float,
                    button: str = "left", click_count: int = 1) -> None:
        await self._cdp(
            "Input.dispatchMouseEvent",
            type=action, x=x, y=y, button=button, clickCount=click_count,
        )

    async def keyboard(self, action: str, key: str = "", code: str = "",
                       text: str = "", modifiers: int = 0,
                       key_code: int = 0) -> None:
        p: Dict[str, Any] = {"type": action}
        if key:
            p["key"] = key
        if code:
            p["code"] = code
        if text:
            p["text"] = text
        if modifiers:
            p["modifiers"] = modifiers
        if key_code:
            p["windowsVirtualKeyCode"] = key_code
            p["nativeVirtualKeyCode"] = key_code
        await self._cdp("Input.dispatchKeyEvent", **p)

    async def scroll(self, x: float, y: float, dx: float, dy: float) -> None:
        await self._cdp(
            "Input.dispatchMouseEvent",
            type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy,
        )

    # ---- Cookie extraction ----

    async def extract_cookies(self, domain: str = "") -> Dict[str, Any]:
        result = await self._cdp("Network.getAllCookies")
        all_c: List[dict] = result.get("cookies", [])

        if not domain:
            h = urlparse(self.url).hostname or ""
            domain = h[4:] if h.startswith("www.") else h
        domain = domain.lower().lstrip(".")

        if not domain:
            return {"domain": "", "cookies": [], "total": len(all_c)}

        matched: List[Dict[str, Any]] = []
        for c in all_c:
            cd = c.get("domain", "").lower().lstrip(".")
            if cd == domain or cd.endswith("." + domain) or domain.endswith("." + cd):
                cookie: Dict[str, Any] = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", f".{domain}"),
                    "path": c.get("path", "/"),
                }
                # Preserve flags needed by Browserless/puppeteer (especially for __Secure- cookies)
                if c.get("secure"):
                    cookie["secure"] = True
                if c.get("httpOnly"):
                    cookie["httpOnly"] = True
                if c.get("sameSite") and c["sameSite"] != "None":
                    cookie["sameSite"] = c["sameSite"]
                matched.append(cookie)
        return {"domain": domain, "cookies": matched, "total": len(all_c)}

    # ---- Lifecycle ----

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        _sessions.pop(self.id, None)

        if self._timer:
            self._timer.cancel()

        try:
            if self._ws:
                # Close the target (page)
                if self._target_id:
                    try:
                        await asyncio.wait_for(
                            self._cdp_browser("Target.closeTarget", targetId=self._target_id),
                            timeout=2,
                        )
                    except Exception:
                        pass
                await self._ws.close()
        except Exception:
            pass

        if self._reader:
            self._reader.cancel()

        for f in self._pending.values():
            if not f.done():
                f.set_exception(RuntimeError("closed"))
        self._pending.clear()

        if self.on_close:
            try:
                await self.on_close()
            except Exception:
                pass

        log.info("[%s] closed", self.id)

    async def _auto_timeout(self) -> None:
        await asyncio.sleep(SESSION_TIMEOUT)
        log.info("[%s] auto-timeout after %ds", self.id, SESSION_TIMEOUT)
        await self.close()


# ---- Module-level helpers ----

def can_create() -> bool:
    now = time.time()
    for s in list(_sessions.values()):
        if now - s.created_at > SESSION_TIMEOUT:
            asyncio.create_task(s.close())
    return len(_sessions) < MAX_SESSIONS


def active_count() -> int:
    return len(_sessions)
