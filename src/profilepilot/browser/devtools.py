"""A tiny raw CDP client on a browser websocket (``RuntimeInfo.cdp_ws_url``).

Shared by the Manager (:mod:`profilepilot.ui.cdp`: thumbnails, tabs, window focus) and the cookie
jar (:mod:`profilepilot.browser.cookiejar`). It only sends the commands its callers ask for: it never
enables a domain (``Runtime.enable``, ``Network.enable`` ...) and ignores events, so pages cannot
notice it. :attr:`CdpConnection.sent` records every method for tests that assert exactly that.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from typing import Any, AsyncIterator


class CdpError(Exception):
    """A DevTools call failed (``message`` is Chrome's error text or a timeout)."""


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


__all__ = ["CdpConnection", "CdpError", "browser_connection"]
