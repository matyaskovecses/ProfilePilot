"""Raw DevTools helpers for ProfilePilot Manager: thumbnails, tab lists, tab activation, window focus.

Everything here speaks plain CDP over the profile browser's own websocket and HTTP endpoints. It
never sends ``Runtime.enable``, ``Page.enable`` or any other domain-enabling command, and never
evaluates script: a page cannot notice that the Manager looked at it. A thumbnail attaches a
short-lived flat session to one page target, asks ``Page.getLayoutMetrics`` and
``Page.captureScreenshot`` (JPEG, scaled to ~480 px wide) and detaches again.

Window focus is only ever done on the user's request (the Focus button): it restores a minimized
window, moves an off-screen window on-screen and brings it to the front (Windows: Win32
``SetForegroundWindow`` with the usual foreground-lock workarounds).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import logging
import subprocess
import sys
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("profilepilot.ui.cdp")

THUMB_WIDTH = 480
THUMB_QUALITY = 60
OFFSCREEN_LIMIT = -5000
"""Windows placed further left/up than this are off-screen (``--window-position=-32000,-32000``)."""


class CdpError(Exception):
    """A DevTools call failed (``message`` is Chrome's error text or a timeout)."""


class ThumbnailUnavailable(CdpError):
    """No picture can be taken right now (``reason``: minimized, no-page, ...)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------- HTTP endpoints


async def page_targets(port: int, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Page targets of the browser on ``port`` (``/json/list``), most recently active first."""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            resp = await client.get(f"http://127.0.0.1:{int(port)}/json/list")
        data = resp.json() if resp.status_code == 200 else []
    except (httpx.HTTPError, ValueError) as exc:
        raise CdpError(f"DevTools endpoint did not answer ({type(exc).__name__})") from None
    if not isinstance(data, list):
        return []
    pages = []
    for item in data:
        if not isinstance(item, dict) or item.get("type") != "page":
            continue
        url = str(item.get("url") or "")
        if url.startswith(("devtools://", "chrome-extension://")):
            continue
        pages.append(item)
    return pages


async def _target_endpoint(port: int, action: str, target_id: str, timeout: float = 3.0) -> bool:
    if not target_id or any(ch in target_id for ch in "/?#\\ "):
        raise CdpError("invalid target id")
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            resp = await client.get(f"http://127.0.0.1:{int(port)}/json/{action}/{target_id}")
    except httpx.HTTPError as exc:
        raise CdpError(f"DevTools endpoint did not answer ({type(exc).__name__})") from None
    return resp.status_code == 200


async def activate_target(port: int, target_id: str) -> bool:
    """Make ``target_id`` the active tab of its window (``/json/activate``)."""
    return await _target_endpoint(port, "activate", target_id)


async def close_target(port: int, target_id: str) -> bool:
    """Close the tab ``target_id`` (``/json/close``)."""
    return await _target_endpoint(port, "close", target_id)


# --------------------------------------------------------------------------- websocket client


class CdpConnection:
    """A tiny CDP client on the browser websocket (flat sessions; events are ignored)."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._ids = itertools.count(1)
        self.sent: list[str] = []
        """Methods sent so far (tests assert that nothing enables a domain)."""

    async def call(self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None,
                   timeout: float = 5.0) -> dict[str, Any]:
        msg_id = next(self._ids)
        message: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self.sent.append(method)
        await self._ws.send(json.dumps(message))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise CdpError(f"{method} timed out")
            try:
                raw = await asyncio.wait_for(self._ws.recv(), remaining)
            except asyncio.TimeoutError:
                raise CdpError(f"{method} timed out") from None
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if data.get("id") != msg_id:
                continue
            if "error" in data:
                error = data["error"]
                raise CdpError(str(error.get("message") if isinstance(error, dict) else error))
            result = data.get("result")
            return result if isinstance(result, dict) else {}


@contextlib.asynccontextmanager
async def browser_connection(ws_url: str, *, timeout: float = 3.0) -> AsyncIterator[CdpConnection]:
    """Connect to a browser websocket (``RuntimeInfo.cdp_ws_url``); never through a proxy."""
    from websockets.asyncio.client import connect

    try:
        ws = await connect(ws_url, proxy=None, open_timeout=timeout, close_timeout=1, max_size=None)
    except Exception as exc:
        raise CdpError(f"could not connect to the browser ({type(exc).__name__})") from None
    try:
        yield CdpConnection(ws)
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


async def _window_of(cdp: CdpConnection, target_id: str) -> dict[str, Any] | None:
    try:
        return await cdp.call("Browser.getWindowForTarget", {"targetId": target_id}, timeout=3.0)
    except CdpError:
        return None


async def capture_thumbnail(ws_url: str, target_id: str, *, width: int = THUMB_WIDTH, quality: int = THUMB_QUALITY,
                            timeout: float = 4.0, cdp: CdpConnection | None = None) -> bytes:
    """A JPEG of the visible part of ``target_id``, about ``width`` pixels wide.

    Raises :class:`ThumbnailUnavailable` for a minimized window (Chrome paints nothing then) and
    :class:`CdpError` for other failures."""
    if cdp is None:
        async with browser_connection(ws_url) as conn:
            return await capture_thumbnail(ws_url, target_id, width=width, quality=quality, timeout=timeout, cdp=conn)
    window = await _window_of(cdp, target_id)
    if window and (window.get("bounds") or {}).get("windowState") == "minimized":
        raise ThumbnailUnavailable("minimized")
    attached = await cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=3.0)
    session_id = attached.get("sessionId")
    if not session_id:
        raise CdpError("could not attach to the tab")
    try:
        metrics = await cdp.call("Page.getLayoutMetrics", session_id=session_id, timeout=3.0)
        viewport = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
        css_w = float(viewport.get("clientWidth") or 0) or 1280.0
        css_h = float(viewport.get("clientHeight") or 0) or 800.0
        page_x = float(viewport.get("pageX") or 0)
        page_y = float(viewport.get("pageY") or 0)
        device = metrics.get("visualViewport") or {}
        # clip.scale multiplies CSS pixels; Chrome also applies the device pixel ratio.
        dpr = 1.0
        if device.get("clientWidth") and viewport.get("clientWidth"):
            dpr = max(0.5, float(device["clientWidth"]) / float(viewport["clientWidth"]))
        scale = max(0.05, min(1.0, width / (css_w * dpr)))
        shot = await cdp.call("Page.captureScreenshot", {
            "format": "jpeg",
            "quality": int(quality),
            "clip": {"x": page_x, "y": page_y, "width": css_w, "height": css_h, "scale": scale},
            "captureBeyondViewport": False,
            "optimizeForSpeed": True,
        }, session_id=session_id, timeout=timeout)
        data = shot.get("data")
        if not data:
            raise CdpError("empty screenshot")
        return base64.b64decode(data)
    finally:
        with contextlib.suppress(Exception):
            await cdp.call("Target.detachFromTarget", {"sessionId": session_id}, timeout=2.0)


async def bring_onscreen(ws_url: str, target_id: str) -> dict[str, Any] | None:
    """Restore a minimized window and move an off-screen one into view (user-requested only).
    Returns the window bounds after the change (None if Chrome did not say)."""
    async with browser_connection(ws_url) as cdp:
        window = await _window_of(cdp, target_id)
        if not window or "windowId" not in window:
            return None
        window_id = window["windowId"]
        bounds = window.get("bounds") or {}
        if bounds.get("windowState") == "minimized":
            await cdp.call("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})
            bounds = ((await _window_of(cdp, target_id)) or {}).get("bounds") or bounds
        left, top = bounds.get("left"), bounds.get("top")
        if (isinstance(left, (int, float)) and left < OFFSCREEN_LIMIT) or (isinstance(top, (int, float)) and top < OFFSCREEN_LIMIT):
            new = {"left": 80, "top": 60}
            if (bounds.get("width") or 0) < 400 or (bounds.get("height") or 0) < 300:
                new.update(width=1280, height=820)
            await cdp.call("Browser.setWindowBounds", {"windowId": window_id, "bounds": new})
            bounds = {**bounds, **new}
        return bounds


# --------------------------------------------------------------------------- OS window focus


def focus_native_window(chrome_pid: int | None) -> bool:
    """Bring the top-level window of the browser process ``chrome_pid`` to the front.

    Windows: restores it if minimized and uses ``SetForegroundWindow`` (attaching to the current
    foreground thread's input queue first, the documented way to take the foreground on a user's
    request). macOS: ``osascript``. Elsewhere: not supported (False)."""
    if not chrome_pid:
        return False
    if sys.platform == "win32":
        try:
            return _focus_windows(int(chrome_pid))
        except Exception as exc:  # pywin32 missing or the window vanished
            log.debug("focusing the window of pid %s failed: %s", chrome_pid, exc)
            return False
    if sys.platform == "darwin":
        script = f'tell application "System Events" to set frontmost of (first process whose unix id is {int(chrome_pid)}) to true'
        try:
            return subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def browser_windows(pid: int) -> list[int]:
    """Visible top-level Chrome windows of process ``pid`` in Z-order (Windows only)."""
    import win32gui  # type: ignore[import-not-found]
    import win32process  # type: ignore[import-not-found]

    found: list[int] = []

    def visit(hwnd: int, _extra: Any) -> bool:
        try:
            if not (win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd)):
                return True
            _tid, owner = win32process.GetWindowThreadProcessId(hwnd)
            if owner != pid or not win32gui.GetClassName(hwnd).startswith("Chrome_WidgetWin"):
                return True
            if not win32gui.GetWindowText(hwnd):
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if win32gui.IsIconic(hwnd) or (right - left > 200 and bottom - top > 150):
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(visit, None)
    return found


def _focus_windows(pid: int) -> bool:
    import win32api  # type: ignore[import-not-found]
    import win32con  # type: ignore[import-not-found]
    import win32gui  # type: ignore[import-not-found]
    import win32process  # type: ignore[import-not-found]

    windows = browser_windows(pid)
    if not windows:
        return False
    hwnd = windows[0]
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    foreground = win32gui.GetForegroundWindow()
    if foreground == hwnd:
        return True
    current = win32api.GetCurrentThreadId()
    target_thread = win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
    attached = False
    try:
        if target_thread and target_thread != current:
            attached = bool(win32process.AttachThreadInput(current, target_thread, True))
        with contextlib.suppress(Exception):
            win32gui.BringWindowToTop(hwnd)
        with contextlib.suppress(Exception):
            win32gui.SetForegroundWindow(hwnd)
    finally:
        if attached:
            with contextlib.suppress(Exception):
                win32process.AttachThreadInput(current, target_thread, False)
    if win32gui.GetForegroundWindow() == hwnd:
        return True
    # Last resort: a synthetic Alt tap releases the foreground lock for the next call.
    with contextlib.suppress(Exception):
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)
    return win32gui.GetForegroundWindow() == hwnd


__all__ = [
    "CdpConnection",
    "CdpError",
    "ThumbnailUnavailable",
    "activate_target",
    "bring_onscreen",
    "browser_connection",
    "capture_thumbnail",
    "close_target",
    "focus_native_window",
    "page_targets",
]
