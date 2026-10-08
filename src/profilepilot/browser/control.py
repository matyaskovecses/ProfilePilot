"""Token-protected loopback control API of a profile host.

The host runs a tiny HTTP/1.1 JSON server on ``127.0.0.1:<port>``. Every request must carry
``X-ProfilePilot-Token: <token>`` (compared in constant time); the token lives only in
``runtime.json`` inside the user's data directory. Browsers cannot reach it from web pages: a
custom header forces a CORS preflight, which is never answered with CORS headers.

Endpoints (implemented by the host, see :mod:`profilepilot.browser.host`)::

    GET  /status    -> {"ok": true, "relay": RelayStats|null, "upstream": str|null, "chrome_pid": int}
    POST /stop      -> {"ok": true}              (graceful stop in the background)
    POST /upstream  {"proxy_id": id|null} or {"url": proxy-url|null} -> {"ok": true, "upstream": ...}

The module also holds the Playwright-free DevTools helpers shared by the host and the runtime
manager: :func:`cdp_version` (``/json/version``) and :func:`cdp_browser_close` (CDP
``Browser.close`` over the browser websocket), each with an async twin.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from typing import Any, Awaitable, Callable, Mapping

import httpx

from ..errors import ProfileNotRunningError, ProfilePilotError
from ..models import RuntimeInfo

log = logging.getLogger("profilepilot.browser.control")

TOKEN_HEADER = "X-ProfilePilot-Token"
MAX_HEAD = 16 * 1024
MAX_BODY = 64 * 1024
READ_TIMEOUT = 10.0

Handler = Callable[[Any], Awaitable[dict[str, Any]]]
"""Route handler: receives the decoded JSON body (or None) and returns the JSON response."""

_REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed",
    409: "Conflict", 413: "Payload Too Large", 500: "Internal Server Error", 503: "Service Unavailable",
}


class ControlError(Exception):
    """Raised by a route handler to answer with ``status`` and ``{"ok": false, "error": message}``.

    ``message`` is returned to the caller verbatim, so it must never contain secrets.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ControlCallError(ProfilePilotError):
    """A control API call failed. ``status`` is the HTTP status, or None if the host was unreachable."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ControlServer:
    """Minimal asyncio HTTP/1.1 server (one request per connection) dispatching to ``routes``."""

    def __init__(self, token: str, routes: Mapping[tuple[str, str], Handler], host: str = "127.0.0.1") -> None:
        if not token:
            raise ValueError("a control token is required")
        self._token = token.encode("utf-8")
        self._routes = {(m.upper(), p): h for (m, p), h in routes.items()}
        self.host = host
        self._server: asyncio.base_events.Server | None = None

    @property
    def port(self) -> int:
        if not self._server or not self._server.sockets:
            raise RuntimeError("control server is not running")
        return self._server.sockets[0].getsockname()[1]

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, port, limit=MAX_HEAD + 1024)
        log.info("control API listening on %s:%s", self.host, self.port)
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, payload = 500, {"ok": False, "error": "internal error"}
        try:
            status, payload = await asyncio.wait_for(self._dispatch(reader), READ_TIMEOUT)
        except asyncio.TimeoutError:
            status, payload = 400, {"ok": False, "error": "request timed out"}
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        except Exception:  # never let one request kill the host
            log.exception("control request failed")
        body = json.dumps(payload).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        with contextlib.suppress(ConnectionError, OSError):
            writer.write(head + body)
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()

    async def _dispatch(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, Any]]:
        raw = await reader.readuntil(b"\r\n\r\n")
        if len(raw) > MAX_HEAD:
            return 400, {"ok": False, "error": "request head too large"}
        lines = raw.decode("latin-1").split("\r\n")
        try:
            method, target, _version = lines[0].split(" ", 2)
        except ValueError:
            return 400, {"ok": False, "error": "malformed request line"}
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()

        supplied = headers.get(TOKEN_HEADER.lower(), "").encode("utf-8")
        if not hmac.compare_digest(supplied, self._token):
            return 401, {"ok": False, "error": "missing or invalid control token"}

        try:
            length = int(headers.get("content-length", "0") or 0)
        except ValueError:
            return 400, {"ok": False, "error": "invalid Content-Length"}
        if length < 0 or length > MAX_BODY:
            return 413, {"ok": False, "error": "request body too large"}
        body: Any = None
        if length:
            data = await reader.readexactly(length)
            try:
                body = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"ok": False, "error": "body is not valid JSON"}
            if body is not None and not isinstance(body, dict):
                return 400, {"ok": False, "error": "body must be a JSON object"}

        path = target.split("?", 1)[0]
        handler = self._routes.get((method.upper(), path))
        if handler is None:
            if any(p == path for (_m, p) in self._routes):
                return 405, {"ok": False, "error": f"method {method} not allowed for {path}"}
            return 404, {"ok": False, "error": f"unknown endpoint {path}"}
        try:
            result = await handler(body)
        except ControlError as exc:
            return exc.status, {"ok": False, "error": exc.message}
        result.setdefault("ok", True)
        return 200, result


def control_call(info: RuntimeInfo, method: str, path: str, body: dict | None = None, timeout: float = 5.0) -> dict:
    """Call a running host's control API (sync). Raises :class:`ControlCallError` on failure."""
    if not info.control_port or not info.control_token:
        raise ProfileNotRunningError(f"Profile '{info.profile_name}' has no control API (not running).")
    url = f"http://127.0.0.1:{info.control_port}{path}"
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            resp = client.request(method.upper(), url, json=body, headers={TOKEN_HEADER: info.control_token})
    except httpx.HTTPError as exc:
        raise ControlCallError(
            f"Could not reach the host process of profile '{info.profile_name}' ({type(exc).__name__}).", None
        ) from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if resp.status_code >= 400 or not data.get("ok", False):
        message = data.get("error") or f"HTTP {resp.status_code}"
        raise ControlCallError(f"Profile '{info.profile_name}': {message}", resp.status_code)
    return data


# --------------------------------------------------------------------------- DevTools helpers
#
# Plain HTTP / websocket access to Chrome's DevTools endpoint, shared by the host and the runtime
# manager (neither may depend on Playwright). ``trust_env=False`` / ``proxy=None``: a system or
# environment proxy must never be used for loopback DevTools traffic.


def cdp_version(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """``GET http://127.0.0.1:<port>/json/version`` (sync); None if nothing usable answers."""
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            resp = client.get(f"http://127.0.0.1:{int(port)}/json/version")
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("webSocketDebuggerUrl") else None


async def async_cdp_version(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """Async variant of :func:`cdp_version`."""
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            resp = await client.get(f"http://127.0.0.1:{int(port)}/json/version")
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("webSocketDebuggerUrl") else None


def cdp_browser_close(ws_url: str, timeout: float = 5.0) -> bool:
    """Send ``Browser.close`` over the browser websocket (sync). True if Chrome acknowledged it.

    Chrome flushes cookies and session state and exits cleanly (``exit_type = Normal``). The
    connection usually drops before the reply arrives; that still counts as success.
    """
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    try:
        with connect(ws_url, proxy=None, open_timeout=timeout, close_timeout=1, max_size=None) as conn:
            conn.send(json.dumps({"id": 1, "method": "Browser.close"}))
            try:
                while True:
                    message = json.loads(conn.recv(timeout=timeout))
                    if message.get("id") == 1:
                        return "error" not in message
            except (ConnectionClosed, TimeoutError):
                return True
    except Exception as exc:  # unreachable / handshake refused / invalid URL
        log.debug("Browser.close via %s failed: %s", ws_url, exc)
        return False


async def async_cdp_browser_close(ws_url: str, timeout: float = 5.0) -> bool:
    """Async variant of :func:`cdp_browser_close`."""
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    try:
        async with connect(ws_url, proxy=None, open_timeout=timeout, close_timeout=1, max_size=None) as conn:
            await conn.send(json.dumps({"id": 1, "method": "Browser.close"}))
            try:
                while True:
                    message = json.loads(await asyncio.wait_for(conn.recv(), timeout))
                    if message.get("id") == 1:
                        return "error" not in message
            except (ConnectionClosed, asyncio.TimeoutError):
                return True
    except Exception as exc:
        log.debug("Browser.close via %s failed: %s", ws_url, exc)
        return False
