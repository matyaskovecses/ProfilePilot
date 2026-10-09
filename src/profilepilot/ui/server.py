"""The ProfilePilot Manager web server: security layer, static files and the ASGI app.

The Manager controls logged-in browsers and secrets, so the server is strict even on loopback:

* **Token auth.** A random 32-byte token is minted per server start. The launcher opens
  ``/?t=<code>`` once, where ``<code>`` is a single-use launch code (or the token itself); the
  server answers with an ``HttpOnly; SameSite=Strict`` session cookie and redirects to ``/``.
  Every ``/api`` call needs that cookie or the ``X-ProfilePilot-Token`` header. Launch codes
  expire after two minutes and work once, so the code that appears on the browser's command line
  is useless afterwards.
* **DNS rebinding.** The ``Host`` header must be ``127.0.0.1:<port>`` or ``localhost:<port>``.
* **CSRF.** State-changing requests need a matching ``Origin`` (or the token header, which a
  browser cannot send cross-site without a CORS preflight that is never granted), and
  ``Sec-Fetch-Site`` must not be cross-site.
* **CSP** ``default-src 'self'`` without inline script, ``frame-ancestors 'none'``; no CORS headers.
* uvicorn's access log is off (URLs carry launch codes) and forwarded headers are ignored.
"""

from __future__ import annotations

import hmac
import logging
import mimetypes
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import anyio.to_thread
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..store import Store
from .api import Checks, ManagerAPI, error_response

log = logging.getLogger("profilepilot.ui.server")

# Windows maps .js to text/plain in some registries, which breaks ES modules (strict MIME check).
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

STATIC_DIR = Path(__file__).with_name("static")
TOKEN_HEADER = "X-ProfilePilot-Token"
CODE_TTL = 120.0
SESSION_TTL = 7 * 24 * 3600.0
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
]

LOCKED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ProfilePilot Manager</title><link rel="stylesheet" href="/static/styles.css"></head>
<body class="locked-page"><main class="locked-card">
<img src="/static/logo.svg" alt="" width="56" height="56">
<h1>This link has expired</h1>
<p>For your security, ProfilePilot Manager links work only once. Open the Manager again from the
<strong>ProfilePilot Manager</strong> shortcut, or run <code>profilepilot ui</code> in a terminal.</p>
</main></body></html>"""


class Auth:
    """Master token, single-use launch codes and browser sessions (all in memory)."""

    def __init__(self, token: str) -> None:
        if len(token) < 32:
            raise ValueError("the UI token must be at least 32 characters")
        self.token = token
        self._codes: dict[str, float] = {}
        self._sessions: dict[str, float] = {}

    def check_token(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value.encode("utf-8"), self.token.encode("utf-8"))  # type: ignore[union-attr]

    def new_code(self, ttl: float = CODE_TTL) -> str:
        now = time.monotonic()
        self._codes = {c: t for c, t in self._codes.items() if t > now}
        code = secrets.token_urlsafe(24)
        self._codes[code] = now + ttl
        return code

    def redeem(self, value: str) -> str | None:
        """A new session id for a valid launch code (single use) or the master token, else None."""
        now = time.monotonic()
        valid = False
        for code, expires in list(self._codes.items()):
            if hmac.compare_digest(code.encode("utf-8"), value.encode("utf-8")):
                del self._codes[code]
                valid = expires > now
                break
        if not valid and not self.check_token(value):
            return None
        sid = secrets.token_urlsafe(32)
        self._sessions = {s: t for s, t in self._sessions.items() if t > now}
        self._sessions[sid] = now + SESSION_TTL
        return sid

    def check_session(self, sid: str | None) -> bool:
        if not sid:
            return False
        now = time.monotonic()
        for known, expires in self._sessions.items():
            if hmac.compare_digest(known.encode("utf-8"), sid.encode("utf-8")):
                return expires > now
        return False


class SecurityMiddleware:
    """Host / auth / Origin checks for every request, plus security headers on every response."""

    def __init__(self, app: Any, *, auth: Auth, port: int, cookie_name: str) -> None:
        self.app = app
        self.auth = auth
        self.port = port
        self.cookie_name = cookie_name
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        self.api: ManagerAPI | None = None

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websockets
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        headers = Headers(scope=scope)
        path: str = scope.get("path") or "/"
        method: str = scope.get("method") or "GET"

        async def send_secure(message: dict) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers") or [])
                present = {k.lower() for k, _ in raw}
                raw.extend((k, v) for k, v in SECURITY_HEADERS if k not in present)
                if path.startswith("/static/"):
                    raw = [(k, v) for k, v in raw if k.lower() != b"cache-control"] + [(b"cache-control", b"no-cache")]
                message["headers"] = raw
            await send(message)

        if (headers.get("host") or "").lower() not in self.hosts:
            await error_response(421, "Invalid Host header.", "bad_host")(scope, receive, send_secure)
            return
        token_ok = self.auth.check_token(headers.get(TOKEN_HEADER.lower()))
        session_ok = self.auth.check_session(_cookie(headers, self.cookie_name))
        fetch_site = headers.get("sec-fetch-site")
        if path.startswith("/api/"):
            if not (token_ok or session_ok):
                await error_response(401, "Not signed in. Open ProfilePilot Manager again (profilepilot ui).",
                                     "unauthorized")(scope, receive, send_secure)
                return
            if fetch_site == "cross-site" or (method in UNSAFE_METHODS and fetch_site == "same-site"):
                await error_response(403, "Cross-site requests are not allowed.", "forbidden")(scope, receive, send_secure)
                return
            if method in UNSAFE_METHODS:
                origin = headers.get("origin")
                if origin is not None:
                    if origin not in self.origins:
                        await error_response(403, "Cross-origin requests are not allowed.", "forbidden")(
                            scope, receive, send_secure)
                        return
                elif not token_ok:
                    await error_response(403, "Missing Origin header.", "forbidden")(scope, receive, send_secure)
                    return
            if method == "OPTIONS":
                await error_response(403, "CORS is not supported.", "forbidden")(scope, receive, send_secure)
                return
        scope.setdefault("state", {})
        scope["state"]["token_auth"] = token_ok
        await self.app(scope, receive, send_secure)


def _cookie(headers: Headers, name: str) -> str | None:
    for part in (headers.get("cookie") or "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return None


def create_app(
    store: Store,
    *,
    token: str,
    port: int,
    runtime: Any | None = None,
    locations: Any | None = None,
    checks: Checks | None = None,
    focuser: Callable[[int | None], bool] | None = None,
    opener: Callable[[Path], None] | None = None,
    poll_interval: float = 1.0,
) -> SecurityMiddleware:
    """The Manager ASGI app for ``store`` served on ``127.0.0.1:<port>``.

    The returned object is the ASGI callable; ``.api`` is the :class:`ManagerAPI` and ``.auth`` the
    :class:`Auth` (the launcher mints launch codes with ``app.auth.new_code()``)."""
    auth = Auth(token)
    api = ManagerAPI(store, runtime=runtime, locations=locations, checks=checks, focuser=focuser, opener=opener,
                     port=port, poll_interval=poll_interval)
    cookie_name = f"pp_session_{port}"

    async def index(request: Request) -> Response:
        code = request.query_params.get("t")
        if code is not None:
            sid = auth.redeem(code)
            if sid is None:
                return HTMLResponse(LOCKED_PAGE, status_code=401, headers={"Cache-Control": "no-store"})
            response = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store"})
            response.set_cookie(cookie_name, sid, httponly=True, samesite="strict", path="/", secure=False)
            return response
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def favicon(request: Request) -> Response:
        return FileResponse(STATIC_DIR / "logo.svg", media_type="image/svg+xml")

    icons: dict[int, bytes] = {}

    async def png_icon(request: Request) -> Response:
        size = int(request.path_params["size"])
        if size not in (16, 32, 48, 64, 128, 180, 192, 256, 512):
            return Response(status_code=404)
        if size not in icons:
            from .shortcut import png_bytes

            icons[size] = await anyio.to_thread.run_sync(png_bytes, size)
        return Response(icons[size], media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    async def launch_code(request: Request) -> Response:
        if not request.scope.get("state", {}).get("token_auth"):
            return error_response(403, "Launch codes need the token header.", "forbidden")
        return JSONResponse({"code": auth.new_code()}, headers={"Cache-Control": "no-store"})

    async def not_found(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            code = "not_found" if exc.status_code == 404 else "method_not_allowed"
            return error_response(exc.status_code, "Not found." if exc.status_code == 404 else "Method not allowed.",
                                  code)
        return Response(status_code=exc.status_code)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await api.aclose()

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
        Route("/icon-{size:int}.png", png_icon, methods=["GET"]),
        Route("/api/launch-code", launch_code, methods=["POST"]),
        *api.routes(),
        Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan,
                    exception_handlers={404: not_found, 405: not_found})  # type: ignore[dict-item]
    secured = SecurityMiddleware(app, auth=auth, port=port, cookie_name=cookie_name)
    secured.api = api
    return secured


__all__ = ["Auth", "SecurityMiddleware", "TOKEN_HEADER", "create_app"]
