"""REST API of ProfilePilot Manager (JSON over the loopback UI server).

Conventions:

* Errors are ``{"error": "<message>", "code": "<code>"}`` with a 4xx status;
  :class:`~profilepilot.errors.ProfilePilotError` messages are user-facing (and scrubbed).
* Blocking store / runtime calls run in worker threads.
* **Secrets are never returned**: proxy passwords and identity card / CVV / SSN / password values
  are write-only (``PATCH /api/proxies/{id}`` with ``password``, ``PUT
  /api/identities/{id}/secret/{field}``); responses only say whether they are set, masked like
  ``visa •••• 4242``. Proxy usernames are shown masked too.
* Executable paths and extra Chrome switches cannot be set through this API (a compromised page
  could otherwise make a profile run any program); the CLI keeps those for the user.

Routes are listed in :meth:`ManagerAPI.routes`; the security layer (token, Host / Origin checks,
CSP) lives in :mod:`profilepilot.ui.server`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial, wraps
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import anyio.to_thread
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import __version__
from ..control import ActivityEvent, ActivityLog, ControlStore
from ..errors import (
    AmbiguousError,
    ConflictError,
    NotFoundError,
    PolicyError,
    ProfileNotRunningError,
    ProfilePilotError,
    RestartRequiredError,
)
from ..identity import FIELDS, IdentityStore, card_brand, mask
from ..jsonio import lock_for, read_json, write_json
from ..models import AppConfig, Profile, ProxyCheck, ProxyRecord, RuntimeInfo
from ..paths import list_browsers
from ..procs import host_alive, process_alive
from ..proxy.url import SCHEMES, ProxyEndpoint, ProxyParseError, parse_proxy
from ..store import Store
from . import cdp
from .events import EventHub, sse_format

log = logging.getLogger("profilepilot.ui.api")

PROXY_FORMAT_HELP = (
    "Could not parse that proxy. Use scheme://user:pass@host:port, user:pass@host:port, host:port or "
    "host:port:user:pass (schemes: http, https, socks4, socks5)."
)
WINDOW_MODES = ("normal", "offscreen", "headless")
BROWSER_KINDS = ("auto", "chrome", "edge", "brave", "chromium")
BROWSER_LABELS = {"chrome": "Google Chrome", "edge": "Microsoft Edge", "brave": "Brave", "chromium": "Chromium"}
CLIENT_INFO: dict[str, tuple[str, str]] = {
    "claude-desktop": ("Claude Desktop", "The Claude app for Windows and macOS."),
    "claude-code": ("Claude Code", "Anthropic's coding agent, in the terminal and in your IDE."),
    "codex": ("Codex", "OpenAI's Codex CLI and IDE extension (also read by the ChatGPT desktop app)."),
    "cursor": ("Cursor", "The agent in the Cursor editor."),
}
CLIENT_DONE: dict[str, dict[str, str]] = {
    "register": {
        "claude-desktop": "Added to Claude Desktop. Quit Claude Desktop completely and start it again to use it.",
        "claude-code": "Added to Claude Code. Start a new Claude Code session to use it.",
        "codex": "Added to Codex. Restart Codex (or the ChatGPT desktop app) to use it.",
        "cursor": "Added to Cursor. Restart Cursor to use it.",
    },
    "unregister": {
        "claude-desktop": "Removed from Claude Desktop. Restart it to apply.",
        "claude-code": "Removed from Claude Code.",
        "codex": "Removed from Codex. Restart it to apply.",
        "cursor": "Removed from Cursor. Restart it to apply.",
    },
}
"""Short toast texts after a successful (un)registration (the full report goes under "Details")."""
THUMB_INTERVAL = 2.0
"""At most one capture per profile in this many seconds (later requests get the cached image)."""
HISTORY_FILE = "proxy_history.json"
HISTORY_KEEP = 24
TEST_CONCURRENCY = 4
MAX_BODY = 1024 * 1024
MAX_IMPORT_BODY = 8 * 1024 * 1024
CLIENTS_TTL = 60.0
BROWSERS_TTL = 300.0


class ApiError(Exception):
    """An error answered as ``{"error": message, "code": code}`` with ``status``."""

    def __init__(self, status: int, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def error_response(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status, headers={"Cache-Control": "no-store"})


def _scrub(text: str) -> str:
    try:
        from ..integrations.shardx import redact_secrets

        return redact_secrets(text)
    except Exception:  # pragma: no cover
        return re.sub(r"://[^\s/@]*@", "://***@", text)


def endpoint(fn: Callable[..., Awaitable[Response]]) -> Callable[..., Awaitable[Response]]:
    """Map exceptions raised by a handler method to consistent JSON errors."""

    @wraps(fn)
    async def wrapper(self: Any, request: Request) -> Response:
        try:
            return await fn(self, request)
        except ApiError as exc:
            return error_response(exc.status, exc.message, exc.code)
        except cdp.CdpError as exc:
            return error_response(502, f"The profile's browser did not answer ({_scrub(str(exc))}). Is it still open?",
                                  "devtools")
        except ProxyParseError:  # the parser may quote its input (with the password)
            return error_response(400, PROXY_FORMAT_HELP, "proxy_format")
        except NotFoundError as exc:
            return error_response(404, _scrub(str(exc)), "not_found")
        except AmbiguousError as exc:
            return error_response(409, _scrub(str(exc)), "ambiguous")
        except ConflictError as exc:
            return error_response(409, _scrub(str(exc)), "conflict")
        except ProfileNotRunningError as exc:
            return error_response(409, _scrub(str(exc)), "not_running")
        except PolicyError as exc:
            return error_response(400, _scrub(str(exc)), "policy")
        except ProfilePilotError as exc:
            return error_response(400, _scrub(str(exc)), "invalid")
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err.get('loc', ())) or 'value'}: {str(err.get('msg', 'invalid')).removeprefix('Value error, ')}"
                for err in exc.errors()
            )
            return error_response(422, f"Invalid value(s): {problems}", "validation")
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the page
            log.exception("unexpected error in %s", fn.__name__)
            return error_response(500, f"Internal error ({type(exc).__name__}). Details are in ui.log.", "internal")

    return wrapper


def ok(data: Any = None, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True} if data is None else data, status_code=status,
                        headers={"Cache-Control": "no-store"})


async def run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))


async def read_body(request: Request, *, limit: int = MAX_BODY) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > limit:
        raise ApiError(413, "Request body is too large.", "too_large")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(400, "Request body is not valid JSON.", "bad_json") from None
    if not isinstance(data, dict):
        raise ApiError(400, "Request body must be a JSON object.", "bad_json")
    return data


def _str(data: dict[str, Any], key: str, *, limit: int = 2000, default: str | None = None) -> str | None:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ApiError(400, f"'{key}' must be text.", "invalid")
    text = str(value)
    if len(text) > limit:
        raise ApiError(400, f"'{key}' is too long (max {limit} characters).", "invalid")
    return text


def _bool(data: dict[str, Any], key: str) -> bool | None:
    if key not in data or data[key] is None:
        return None
    if not isinstance(data[key], bool):
        raise ApiError(400, f"'{key}' must be true or false.", "invalid")
    return data[key]


def _tags(data: dict[str, Any], key: str = "tags") -> list[str] | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, str):
        value = [t for t in re.split(r"[,\n]", value)]
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise ApiError(400, "'tags' must be a list of strings.", "invalid")
    return [t.strip() for t in value if t.strip()][:32]


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def mask_username(username: str | None) -> str | None:
    if not username:
        return None
    return (username[:2] if len(username) > 3 else username[:1]) + "•••"


LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


def origin_is_secure(origin: str) -> bool:
    """https://, or http:// on this computer (local development servers)."""
    parts = urlsplit(origin)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and (host in LOCAL_HOSTS or host.endswith(".localhost"))


def plural(n: int, one: str, many: str | None = None) -> str:
    """``1 proxy`` / ``3 proxies``."""
    return f"{n} {one if n == 1 else (many or one + 's')}"


_AUTH_FAILED = re.compile(
    r"\b407\b|proxy authentication required|authentication (?:failed|required|error)|"
    r"invalid (?:username|user name|password|credentials)|wrong (?:username|password)|"
    r"username and password|auth(?:entication)? method", re.I)
_DNS_FAILED = re.compile(
    r"getaddrinfo|errno 1100[14]|errno -[235]\b|name or service not known|nodename nor servname|"
    r"temporary failure in name resolution|no address associated|could not resolve|name resolution|"
    r"no such host", re.I)
_REFUSED = re.compile(
    r"refused the connection|connection refused|actively refused|refused to connect|errno 111\b|10061|"
    r"connect call failed|winerror 1225", re.I)
_UNREACHABLE = re.compile(r"unreachable|errno 1005[01]|10065|no route to host", re.I)
_RESET = re.compile(r"connection reset|reset by peer|10054|server disconnected|closed the connection|"
                    r"connection was closed|unexpectedly closed", re.I)
_TIMEOUT = re.compile(r"timed out(?: after)?:?\s*(\d+(?:\.\d+)?)|timeout", re.I)
_TLS = re.compile(r"\bssl\b|certificate|\btls\b", re.I)


def friendly_proxy_error(error: str | None, host: str | None = None) -> str | None:
    """A short, plain-language reason for a failed proxy / route check (the raw text stays available
    as "details"): "The proxy didn't answer within 8 s.", "Wrong proxy username or password." ..."""
    if not error:
        return None
    text = str(error)
    if _AUTH_FAILED.search(text):
        return "Wrong proxy username or password."
    if _DNS_FAILED.search(text):
        return f"Can't find the server {host} – check the address." if host else \
            "Can't find the proxy's server – check the address."
    if _REFUSED.search(text):
        return "The proxy refused the connection."
    if _UNREACHABLE.search(text):
        return "The proxy's network can't be reached from this computer."
    if _RESET.search(text):
        return "The proxy closed the connection."
    match = _TIMEOUT.search(text)
    if match:
        seconds = round(float(match.group(1))) if match.group(1) else 8
        return f"The proxy didn't answer within {seconds} s."
    if _TLS.search(text):
        return "The secure (TLS) connection through the proxy failed."
    return "Couldn't reach the internet through this proxy."


def check_view(check: ProxyCheck | None, *, host: str | None = None) -> dict[str, Any] | None:
    if check is None:
        return None
    error = _scrub(check.error)[:300] if check.error else None
    return {
        "ok": check.ok, "ip": check.ip, "country": check.country, "country_code": check.country_code,
        "region": check.region, "city": check.city, "isp": check.isp, "timezone": check.timezone,
        "latency_ms": check.latency_ms, "provider": check.provider,
        "error": error, "reason": None if check.ok else friendly_proxy_error(error or "no answer", host),
        "checked_at": _iso(check.checked_at),
    }


def _registered_python() -> str:
    """The console interpreter to register with clients (never pythonw.exe: stdio needs a console exe)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(exe)


@dataclass
class Checks:
    """Network checks (injectable for tests: no internet in unit tests)."""

    proxy: Callable[[ProxyEndpoint | None], Awaitable[ProxyCheck]] | None = None
    relay: Callable[[str | None], Awaitable[ProxyCheck]] | None = None

    async def check_proxy(self, endpoint: ProxyEndpoint | None) -> ProxyCheck:
        if self.proxy is not None:
            return await self.proxy(endpoint)
        from ..proxy.check import check_proxy

        return await check_proxy(endpoint, timeout=12.0)

    async def check_relay(self, url: str | None) -> ProxyCheck:
        if self.relay is not None:
            return await self.relay(url)
        from ..proxy.check import check_via_relay

        return await check_via_relay(url, timeout=12.0)


@dataclass
class _Thumb:
    at: float
    data: bytes | None = None
    reason: str | None = None


@dataclass
class _TestJob:
    id: str
    total: int
    ids: list[str] = field(default_factory=list)
    done: int = 0
    cancelled: bool = False
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


def record_proxy_history(store: Store, proxy_id: str, check: ProxyCheck) -> None:
    """Append one check to ``<root>/proxy_history.json`` (the Manager's latency sparklines; the last
    :data:`HISTORY_KEEP` points per proxy). Safe to call from any process (``proxy_test``, the CLI)."""
    path = store.root / HISTORY_FILE
    with lock_for(path):
        data = read_json(path, {}) or {}
        proxies = data.get("proxies") if isinstance(data, dict) else None
        if not isinstance(proxies, dict):
            proxies = {}
        points = list(proxies.get(proxy_id) or [])
        points.append({"t": _iso(check.checked_at), "ok": check.ok, "ms": check.latency_ms})
        proxies[proxy_id] = points[-HISTORY_KEEP:]
        write_json(path, {"proxies": proxies})


# --------------------------------------------------------------------------- runtime state


def runtime_state(store: Store, profile_id: str) -> tuple[str, RuntimeInfo | None, str | None]:
    """Cheap running state from the files (no DevTools probe): ``(state, info, crash)``.

    ``state`` is running | starting | stopping | stopped | crashed."""
    data = read_json(store.runtime_file(profile_id))
    info: RuntimeInfo | None = None
    if isinstance(data, dict):
        try:
            info = RuntimeInfo.model_validate(data)
        except Exception:
            info = None
    if info is not None and host_alive(info):
        if info.state == "running":
            alive = process_alive(info.chrome_pid, info.chrome_create_time)
            return ("running" if alive else "stopping"), info, None
        return info.state, info, None
    last = read_json(store.profile_dir(profile_id) / "last_exit.json")
    if isinstance(last, dict) and last.get("crashed"):
        return "crashed", None, str(last.get("crash") or "the browser crashed")
    return "stopped", None, None


def liveness(store: Store, profile_id: str) -> tuple[bool, bool]:
    data = read_json(store.runtime_file(profile_id))
    try:
        info = RuntimeInfo.model_validate(data)
    except Exception:
        return (False, False)
    return (host_alive(info), process_alive(info.chrome_pid, info.chrome_create_time))


def runtime_view(info: RuntimeInfo | None) -> dict[str, Any] | None:
    if info is None:
        return None
    return {
        "state": info.state, "window": info.window, "browser_version": info.browser_version,
        "browser": Path(info.browser_path).name if info.browser_path else None,
        "chrome_pid": info.chrome_pid, "host_pid": info.host_pid, "started_at": _iso(info.started_at),
        "cdp_http_url": info.cdp_http_url, "upstream": info.upstream, "proxy_id": info.proxy_id,
        "relay": bool(info.relay_port), "client_job": bool(info.client_job),
    }


# --------------------------------------------------------------------------- the API


class ManagerAPI:
    """All ``/api`` handlers. Construct once per UI server."""

    def __init__(
        self,
        store: Store,
        *,
        runtime: Any | None = None,
        locations: Any | None = None,
        checks: Checks | None = None,
        focuser: Callable[[int | None], bool] | None = None,
        opener: Callable[[Path], None] | None = None,
        terminal: Callable[[list[str], dict[str, str]], None] | None = None,
        port: int = 0,
        poll_interval: float = 1.0,
    ) -> None:
        self.store = store
        if runtime is None:
            from ..browser.runtime import RuntimeManager

            runtime = RuntimeManager(store)
        self.runtime = runtime
        self.locations = locations
        self.checks = checks or Checks()
        self.focuser = focuser or cdp.focus_native_window
        self.opener = opener or _open_path
        self.terminal = terminal or _open_terminal
        self.port = port
        self.control = ControlStore(store)
        self.identities = IdentityStore(store)
        self.activity = ActivityLog(store.root)
        self.hub = EventHub(store.root, self.activity, profile_view=self.profile_view_by_id,
                            open_help=self.open_help_views, liveness=partial(liveness, store),
                            interval=poll_interval)
        self._thumbs: dict[str, _Thumb] = {}
        self._thumb_locks: dict[str, asyncio.Lock] = {}
        self._test_job: _TestJob | None = None
        self._clients_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._clients_refresh: asyncio.Task | None = None
        self._browsers_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ views

    def _proxies_by_id(self) -> dict[str, ProxyRecord]:
        return {p.id: p for p in self.store.list_proxies()}

    def _identity_names(self) -> dict[str, str]:
        return {i.id: i.name for i in self.identities.list()}

    def profile_view(self, p: Profile, *, proxies: dict[str, ProxyRecord] | None = None,
                     identities: dict[str, str] | None = None) -> dict[str, Any]:
        proxies = self._proxies_by_id() if proxies is None else proxies
        identities = self._identity_names() if identities is None else identities
        state, info, crash = runtime_state(self.store, p.id)
        control = self.control.state_by_id(p.id, strict=False)
        proxy: dict[str, Any] | None = None
        if p.proxy_id:
            rec = proxies.get(p.proxy_id)
            proxy = self._proxy_brief(rec) if rec else {"id": p.proxy_id, "name": p.proxy_id, "missing": True}
        identity = None
        if p.identity_id:
            name = identities.get(p.identity_id)
            identity = {"id": p.identity_id, "name": name or p.identity_id, "missing": name is None}
        launch = p.launch.model_dump(mode="json")
        return {
            "id": p.id, "name": p.name, "notes": p.notes, "tags": p.tags, "color": p.color, "browser": p.browser,
            "proxy_id": p.proxy_id, "identity_id": p.identity_id, "launch": launch, "window": p.launch.window,
            "created_at": _iso(p.created_at), "updated_at": _iso(p.updated_at),
            "last_started_at": _iso(p.last_started_at), "total_runtime_s": p.total_runtime_s,
            "state": state, "crash": crash, "runtime": runtime_view(info),
            "proxy": proxy, "identity": identity, "control": control.as_dict(),
            "path": str(self.store.profile_dir(p.id)),
        }

    def profile_view_by_id(self, profile_id: str) -> dict[str, Any] | None:
        try:
            data = read_json(self.store.profile_dir(profile_id) / "profile.json")
            if not data:
                return None
            return self.profile_view(Profile.model_validate(data))
        except Exception as exc:
            log.debug("profile view of %s failed: %s", profile_id, exc)
            return None

    def profiles_view(self) -> list[dict[str, Any]]:
        proxies, identities = self._proxies_by_id(), self._identity_names()
        return [self.profile_view(p, proxies=proxies, identities=identities) for p in self.store.list_profiles()]

    @staticmethod
    def _proxy_brief(rec: ProxyRecord) -> dict[str, Any]:
        c = rec.last_check
        return {
            "id": rec.id, "name": rec.name, "scheme": rec.scheme, "host": rec.host, "port": rec.port,
            "country_code": c.country_code if c else None, "city": c.city if c else None,
            "ip": c.ip if c else None, "ok": c.ok if c else None, "latency_ms": c.latency_ms if c else None,
            "checked_at": _iso(c.checked_at) if c else None,
            "reason": friendly_proxy_error(c.error, rec.host) if c and not c.ok else None,
        }

    def proxy_view(self, rec: ProxyRecord, *, used_by: list[dict[str, str]] | None = None,
                   history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "id": rec.id, "name": rec.name, "scheme": rec.scheme, "host": rec.host, "port": rec.port,
            "username": mask_username(rec.username), "has_username": bool(rec.username),
            "has_password": rec.has_password, "tags": rec.tags, "notes": rec.notes, "created_at": _iso(rec.created_at),
            "url": rec.redacted_url().replace(f"{rec.username}:***@", f"{mask_username(rec.username)}:***@")
            if rec.username else rec.redacted_url(),
            "last_check": check_view(rec.last_check, host=rec.host), "history": history or [], "used_by": used_by or [],
        }

    def proxies_view(self) -> list[dict[str, Any]]:
        users: dict[str, list[dict[str, str]]] = {}
        for p in self.store.list_profiles():
            if p.proxy_id:
                users.setdefault(p.proxy_id, []).append({"id": p.id, "name": p.name})
        history = self._history()
        return [self.proxy_view(r, used_by=users.get(r.id), history=history.get(r.id))
                for r in self.store.list_proxies()]

    def identity_view(self, ident: Any, *, used_by: list[dict[str, str]] | None = None) -> dict[str, Any]:
        if used_by is None:
            used_by = [{"id": p.id, "name": p.name} for p in self.store.profiles_using_identity(ident.id)]
        sensitive: dict[str, str] = {}
        for key in ident.sensitive_set:
            value = self.store.secrets.get(f"identity:{ident.id}:{key}")
            sensitive[key] = mask(key, value) if value else "missing"
        card = None
        number = self.store.secrets.get(f"identity:{ident.id}:card_number") if "card_number" in ident.sensitive_set else None
        if number:
            card = {"brand": card_brand(number), "last4": number[-4:]}
        return {
            "id": ident.id, "name": ident.name, "notes": ident.notes, "values": dict(sorted(ident.values.items())),
            "sensitive": sensitive, "card": card, "allowed_origins": list(ident.allowed_origins),
            "insecure_origins": [o for o in ident.allowed_origins if not origin_is_secure(o)],
            "chrome": self._chrome_link_view(ident),
            "created_at": _iso(ident.created_at), "updated_at": _iso(ident.updated_at), "used_by": used_by or [],
        }

    @staticmethod
    def _chrome_link_view(ident: Any) -> dict[str, Any] | None:
        """The identity's live link to a browser's saved addresses, as the page may see it: the source,
        the address's one-line summary (name and city/state/country only) and *which* fields come from
        the browser - never the browser's values themselves (they are read at fill time)."""
        source_ref = getattr(ident, "chrome_source", None)
        if not source_ref:
            return None
        from ..chrome_autofill import source_values
        from ..identity import merge_live

        address_id = getattr(ident, "chrome_address", None)
        view: dict[str, Any] = {"source": source_ref, "pinned": bool(address_id), "address_id": address_id,
                                "label": None, "address": None, "fields_from_chrome": [], "ok": False, "error": None}
        try:
            live, source, chosen = source_values(source_ref, address=ident.chrome_address)
        except ProfilePilotError as exc:
            view["error"] = _scrub(str(exc))[:300]
            return view
        except Exception as exc:  # noqa: BLE001 - a display path: never break the identities list
            log.debug("reading the browser link of %s failed: %s", ident.id, exc)
            view["error"] = f"The browser's saved addresses could not be read ({type(exc).__name__})."
            return view
        merged = merge_live(ident.values, live)
        view.update(ok=True, label=source.label, address=chosen.summary(),
                    fields_from_chrome=sorted(k for k in merged if k not in ident.values))
        return view

    @staticmethod
    def autofill_sources_view() -> list[dict[str, Any]]:
        """Browser profiles on this computer with saved addresses, each address as its one-line summary
        (name and city/state/country: no street, email or phone) - what the "Connect to browser" dialog
        lists. ``id`` is Chrome's GUID of the address (an opaque handle for picking it)."""
        from ..chrome_autofill import discover_sources, read_addresses

        out: list[dict[str, Any]] = []
        for src in discover_sources():
            entry: dict[str, Any] = {"ref": src.ref, "label": src.label, "browser": src.browser,
                                     "browser_label": BROWSER_LABELS.get(src.browser, src.browser),
                                     "profile_name": src.profile_name, "active": src.active, "addresses": [],
                                     "error": None}
            try:
                addresses = read_addresses(src)
            except ProfilePilotError as exc:
                entry["error"] = _scrub(str(exc))[:300]
            except Exception as exc:  # noqa: BLE001 - one unreadable profile must not hide the others
                log.debug("reading the saved addresses of %s failed: %s", src.ref, exc)
                entry["error"] = f"The saved addresses could not be read ({type(exc).__name__})."
            else:
                entry["addresses"] = [{"number": n, "id": a.guid, "summary": a.summary(), "uses": a.use_count}
                                      for n, a in enumerate(addresses[:50], 1)]
                entry["total"] = len(addresses)
            out.append(entry)
        return out

    def identities_view(self) -> list[dict[str, Any]]:
        users: dict[str, list[dict[str, str]]] = {}
        for p in self.store.list_profiles():
            if p.identity_id:
                users.setdefault(p.identity_id, []).append({"id": p.id, "name": p.name})
        return [self.identity_view(i, used_by=users.get(i.id, [])) for i in self.identities.list()]

    def open_help_views(self) -> list[dict[str, Any]]:
        names = {p.id: p.name for p in self.store.list_profiles()}
        out = []
        for req in self.control.help_requests(open_only=True):
            if req.profile_id not in names:
                continue
            data = req.model_dump(mode="json")
            data["profile_name"] = names[req.profile_id]
            out.append(data)
        return out

    def settings_view(self) -> dict[str, Any]:
        cfg = self.store.load_config()
        from ..integrations.shardx import TOKEN_KEY

        return {
            "default_window": cfg.default_window, "max_running": cfg.max_running, "browser_path": cfg.browser_path,
            "escape_client_job": cfg.escape_client_job,
            "autofill_from_browser": bool(getattr(cfg, "autofill_from_browser", True)),
            "shardx": {"enabled": cfg.shardx.enabled, "base_url": cfg.shardx.base_url,
                       "token_source": cfg.shardx.token_source,
                       "token_set": bool(self.store.secrets.get(TOKEN_KEY))},
            "automation": {"driver": cfg.automation.driver},
            "data_root": str(self.store.root), "secrets_backend": self.store.secrets.backend,
        }

    def browsers_view(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._browsers_cache and now - self._browsers_cache[0] < BROWSERS_TTL:
            return self._browsers_cache[1]
        out = [{"kind": b.kind, "label": BROWSER_LABELS.get(b.kind, b.kind), "path": b.path, "version": b.version}
               for b in list_browsers()]
        self._browsers_cache = (now, out)
        return out

    def trash_view(self) -> list[dict[str, Any]]:
        return [{"trash_id": e.trash_id, "profile_id": e.profile_id, "name": e.name, "deleted_at": _iso(e.deleted_at),
                 "size_bytes": e.size_bytes} for e in self.store.list_trash()]

    # ------------------------------------------------------------------ proxy history

    @property
    def _history_file(self) -> Path:
        return self.store.root / HISTORY_FILE

    def _history(self) -> dict[str, list[dict[str, Any]]]:
        data = read_json(self._history_file, {}) or {}
        proxies = data.get("proxies") if isinstance(data, dict) else None
        return proxies if isinstance(proxies, dict) else {}

    def record_history(self, proxy_id: str, check: ProxyCheck) -> None:
        record_proxy_history(self.store, proxy_id, check)

    def _forget_history(self, proxy_id: str) -> None:
        if not self._history_file.exists():
            return
        with lock_for(self._history_file):
            data = read_json(self._history_file, {}) or {}
            if isinstance(data, dict) and proxy_id in (data.get("proxies") or {}):
                data["proxies"].pop(proxy_id, None)
                write_json(self._history_file, data)

    # ------------------------------------------------------------------ helpers

    def _resolve(self, ref: str) -> Profile:
        return self.store.get_profile(ref)

    def _log(self, tool: str, summary: str, *, profile: Profile | None = None, ok: bool = True, ms: int = 0) -> None:
        try:
            self.activity.append(ActivityEvent(
                profile_id=profile.id if profile else None, profile_name=profile.name if profile else None,
                source="ui", tool=tool, summary=summary, ok=ok, ms=ms,
            ))
        except Exception as exc:  # pragma: no cover - logging must never fail an action
            log.debug("activity append failed: %s", exc)

    async def _publish_profile(self, profile_id: str) -> dict[str, Any] | None:
        view = await run(self.profile_view_by_id, profile_id)
        if view is not None:
            self.hub.publish("profile", view)
        return view

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def _running_info(self, profile: Profile) -> RuntimeInfo:
        state, info, _crash = await run(runtime_state, self.store, profile.id)
        if state != "running" or info is None or not info.cdp_port or not info.cdp_ws_url:
            raise ApiError(409, f"Profile '{profile.name}' is not running.", "not_running")
        return info

    # ================================================================== handlers

    @endpoint
    async def ping(self, request: Request) -> Response:
        return ok({"ok": True, "version": __version__, "pid": os.getpid()})

    @endpoint
    async def overview(self, request: Request) -> Response:
        def collect() -> dict[str, Any]:
            profiles = self.profiles_view()
            return {
                "version": __version__, "data_root": str(self.store.root), "platform": sys.platform,
                "secrets_backend": self.store.secrets.backend,
                "running": sum(1 for p in profiles if p["state"] in ("running", "starting")),
                "profiles": profiles, "proxies": self.proxies_view(), "identities": self.identities_view(),
                "help": self.open_help_views(), "browsers": self.browsers_view(), "settings": self.settings_view(),
                "trash_count": len(self.store.list_trash()), "chatgpt": self.chatgpt_status(),
                "clients": self._clients_cache[1] if self._clients_cache else None,
                "manager": {"pid": os.getpid(), "port": self.port},
            }

        data = await run(collect)
        if data["clients"] is None:
            self._refresh_clients_soon()  # the Connections count arrives with a "clients" event
        return ok(data)

    def _refresh_clients_soon(self) -> None:
        """Detect the AI apps in the background (``claude mcp get`` can take a second) and publish them."""
        if self._clients_refresh is not None and not self._clients_refresh.done():
            return

        async def refresh() -> None:
            try:
                clients = await run(self.clients_view)
            except Exception as exc:  # pragma: no cover - detection must never break the Manager
                log.debug("client detection failed: %s", exc)
                return
            self.hub.publish("clients", {"clients": clients})

        self._clients_refresh = self._spawn(refresh())

    @endpoint
    async def meta(self, request: Request) -> Response:
        groups = []
        for spec in FIELDS.values():
            group = "sensitive" if spec.sensitive and spec.group != "card" else spec.group
            groups.append({"key": spec.key, "label": spec.label, "sensitive": spec.sensitive, "group": group,
                           "help": spec.help})
        return ok({"version": __version__, "fields": groups, "window_modes": list(WINDOW_MODES),
                   "browser_kinds": list(BROWSER_KINDS), "schemes": list(SCHEMES),
                   "help_kinds": ["captcha", "login", "verification", "payment", "other"]})

    # ------------------------------------------------------------------ profiles

    @endpoint
    async def list_profiles(self, request: Request) -> Response:
        return ok({"profiles": await run(self.profiles_view)})

    @endpoint
    async def get_profile(self, request: Request) -> Response:
        profile = await run(self._resolve, request.path_params["pid"])
        return ok(await run(self.profile_view, profile))

    def _launch_patch(self, data: dict[str, Any]) -> dict[str, Any]:
        launch: dict[str, Any] = {}
        src = data.get("launch") if isinstance(data.get("launch"), dict) else data
        if "window" in src and src["window"] is not None:
            if src["window"] not in WINDOW_MODES:
                raise ApiError(400, "window must be normal, offscreen or headless.", "invalid")
            launch["window"] = src["window"]
        for key in ("lang", "timezone"):
            if key in src:
                value = _str(src, key, limit=64)
                launch[key] = value.strip() if value and value.strip() else None
        if "start_url" in src:
            value = _str(src, "start_url", limit=2000)
            if value and value.strip():
                from ..safety import normalize_url

                url = normalize_url(value)
                if urlsplit(url).scheme not in ("http", "https"):
                    raise ApiError(400, "The start page must be an http:// or https:// address.", "invalid")
                launch["start_url"] = url
            else:
                launch["start_url"] = None
        for key in ("restore_session", "disable_quic"):
            if key in src and src[key] is not None:
                if not isinstance(src[key], bool):
                    raise ApiError(400, f"'{key}' must be true or false.", "invalid")
                launch[key] = src[key]
        if "webrtc" in src and src["webrtc"] is not None:
            if src["webrtc"] not in ("auto", "proxy_only", "default"):
                raise ApiError(400, "webrtc must be auto, proxy_only or default.", "invalid")
            launch["webrtc"] = src["webrtc"]
        return launch

    def _browser_kind(self, data: dict[str, Any]) -> str | None:
        value = data.get("browser")
        if value is None:
            return None
        if value not in BROWSER_KINDS:
            raise ApiError(400, "browser must be auto, chrome, edge, brave or chromium (custom browser paths can be "
                                "set with the CLI).", "invalid")
        return value

    def _proxy_from(self, data: dict[str, Any], name_hint: str | None) -> tuple[bool, str | None]:
        """(given, proxy id or None) from ``proxy_id`` / ``proxy_url`` in a profile body."""
        if data.get("proxy_url"):
            text = _str(data, "proxy_url", limit=2000) or ""
            scheme = data.get("proxy_scheme") or "http"
            if scheme not in SCHEMES:
                raise ApiError(400, "proxy_scheme must be http, https, socks4 or socks5.", "invalid")
            endpoint = parse_proxy(text.strip(), scheme)
            label = (name_hint or "").strip() or None  # default: named after its address (host:port)
            try:
                rec = self.store.add_proxy(endpoint, label, default_scheme=scheme)
            except ConflictError:
                rec = self.store.add_proxy(endpoint, None, default_scheme=scheme)
            return True, rec.id
        if "proxy_id" in data:
            ref = data.get("proxy_id")
            if ref in (None, ""):
                return True, None
            return True, self.store.get_proxy(str(ref)).id
        return False, None

    @endpoint
    async def create_profile(self, request: Request) -> Response:
        data = await read_body(request)
        name = (_str(data, "name", limit=64) or "").strip()
        if not name:
            raise ApiError(400, "Give the profile a name.", "invalid")

        def create() -> Profile:
            if any(p.name.casefold() == name.casefold() for p in self.store.list_profiles()):
                raise ConflictError(f"A profile named '{name}' already exists.")
            launch = self._launch_patch(data)
            launch.setdefault("window", self.store.load_config().default_window)
            identity = _str(data, "identity_id", limit=64)
            _given, proxy_id = self._proxy_from(data, _str(data, "proxy_name", limit=64))
            return self.store.create_profile(
                name, notes=_str(data, "notes", limit=4000) or "", tags=_tags(data) or (), proxy_id=proxy_id,
                browser=self._browser_kind(data) or "auto", launch=launch, color=_str(data, "color", limit=16),
                identity_id=identity or None,
            )

        profile = await run(create)
        await run(self._log, "create profile", f"Created profile '{profile.name}'.", profile=profile)
        view = await self._publish_profile(profile.id)
        return ok(view, 201)

    @endpoint
    async def update_profile(self, request: Request) -> Response:
        data = await read_body(request)
        notes: list[str] = []

        def update() -> Profile:
            current = self._resolve(request.path_params["pid"])
            changes: dict[str, Any] = {}
            for key, limit in (("name", 64), ("notes", 4000), ("color", 16)):
                if key in data:
                    changes[key] = _str(data, key, limit=limit) or ("" if key == "notes" else None)
            if "tags" in data:
                changes["tags"] = _tags(data) or []
            if "identity_id" in data:
                changes["identity_id"] = _str(data, "identity_id", limit=64) or None
            kind = self._browser_kind(data)
            if kind is not None:
                changes["browser"] = kind
            given, proxy_id = self._proxy_from(data, _str(data, "proxy_name", limit=64))
            if given:
                changes["proxy_id"] = proxy_id
            launch = self._launch_patch(data)
            if launch:
                changes["launch"] = launch
            if "color" in changes and changes["color"] is None:
                changes.pop("color")
            updated = self.store.update_profile(current.id, **changes)
            state, info, _ = runtime_state(self.store, updated.id)
            if state == "running" and info is not None:
                if given and info.proxy_id != updated.proxy_id:
                    try:
                        self.runtime.set_upstream(updated.id, updated.proxy_id)
                        notes.append("The new proxy is live for new connections.")
                    except RestartRequiredError:
                        notes.append("The profile runs without a proxy relay: restart it to use the proxy.")
                    except ProfilePilotError as exc:
                        notes.append(f"Could not switch the proxy live ({_scrub(str(exc))}); restart the profile.")
                if launch or kind is not None:
                    notes.append("Launch settings apply the next time the profile starts.")
            return updated

        profile = await run(update)
        await run(self._log, "update profile", f"Changed settings of '{profile.name}'.", profile=profile)
        view = await self._publish_profile(profile.id)
        return ok({"profile": view, "notes": notes})

    @endpoint
    async def delete_profile(self, request: Request) -> Response:
        profile = await run(self._resolve, request.path_params["pid"])
        entry = await run(self.store.delete_profile, profile.id)
        await run(self._log, "delete profile", f"Moved '{entry.name}' to the trash.", profile=profile)
        self._thumbs.pop(profile.id, None)
        self.hub.publish("profile-removed", {"id": profile.id})
        self.hub.publish("trash", {})
        return ok({"trash_id": entry.trash_id, "name": entry.name})

    @endpoint
    async def clone_profile(self, request: Request) -> Response:
        data = await read_body(request)
        source = await run(self._resolve, request.path_params["pid"])
        name = (_str(data, "name", limit=64) or "").strip() or f"{source.name} copy"
        copy_data = bool(_bool(data, "copy_data"))
        clone = await run(partial(self.store.clone_profile, source.id, name, copy_data=copy_data))
        await run(self._log, "clone profile", f"Cloned '{source.name}' as '{clone.name}'"
                  + (" with its browser data." if copy_data else "."), profile=clone)
        return ok(await self._publish_profile(clone.id), 201)

    @endpoint
    async def start_profile(self, request: Request) -> Response:
        data = await read_body(request)
        window = data.get("window")
        if window is not None and window not in WINDOW_MODES:
            raise ApiError(400, "window must be normal, offscreen or headless.", "invalid")
        profile = await run(self._resolve, request.path_params["pid"])
        started = time.perf_counter()
        self.hub.publish("profile", {**(await run(self.profile_view, profile)), "state": "starting"})
        try:
            await run(partial(self.runtime.start, profile.id, window=window))
        except ProfilePilotError as exc:
            await run(self._log, "start", _scrub(str(exc)), profile=profile, ok=False,
                      ms=int((time.perf_counter() - started) * 1000))
            await self._publish_profile(profile.id)
            raise
        await run(self._log, "start", f"Started '{profile.name}'" + (f" ({window} window)." if window else "."),
                  profile=profile, ms=int((time.perf_counter() - started) * 1000))
        await self._check_untested_proxy(profile)
        return ok(await self._publish_profile(profile.id))

    async def _check_untested_proxy(self, profile: Profile) -> None:
        """A profile started on a proxy that was never tested: test it in the background, so a broken
        proxy shows up as "Proxy not reachable" instead of a blank page with no explanation."""
        if not profile.proxy_id:
            return
        try:
            rec = await run(self.store.get_proxy, profile.proxy_id)
        except ProfilePilotError:
            return
        if rec.last_check is not None:
            return

        async def test() -> None:
            try:
                await self._test_one(rec)
            except Exception as exc:  # a deleted proxy etc.
                log.debug("background test of %s failed: %s", rec.id, exc)
                return
            self.hub.publish("proxies", {})
            await self._publish_profile(profile.id)

        self._spawn(test())

    @endpoint
    async def stop_profile(self, request: Request) -> Response:
        profile = await run(self._resolve, request.path_params["pid"])
        self.hub.publish("profile", {**(await run(self.profile_view, profile)), "state": "stopping"})
        stopped = await run(self.runtime.stop, profile.id)
        self._thumbs.pop(profile.id, None)
        await run(self._log, "stop", f"Stopped '{profile.name}'." if stopped else f"'{profile.name}' was not running.",
                  profile=profile)
        return ok(await self._publish_profile(profile.id))

    @endpoint
    async def pause_profile(self, request: Request) -> Response:
        data = await read_body(request)
        note = _str(data, "note", limit=500) or ""
        profile = await run(self._resolve, request.path_params["pid"])
        info = await run(partial(self.control.pause, profile.id, note=note))
        await run(self._log, "take control", f"You took control of '{profile.name}'" + (f": {note}" if note else "."),
                  profile=profile)
        view = await self._publish_profile(profile.id)
        return ok({"pause": info.model_dump(mode="json"), "profile": view})

    @endpoint
    async def resume_profile(self, request: Request) -> Response:
        profile = await run(self._resolve, request.path_params["pid"])
        closed = await run(self.control.resume, profile.id)
        await run(self._log, "hand back", f"Handed '{profile.name}' back to the AI"
                  + (f" (closed {plural(len(closed), 'help request')})." if closed else "."), profile=profile)
        view = await self._publish_profile(profile.id)
        return ok({"resolved": [r.model_dump(mode="json") for r in closed], "profile": view})

    @endpoint
    async def focus_profile(self, request: Request) -> Response:
        data = await read_body(request)
        profile = await run(self._resolve, request.path_params["pid"])
        info = await self._running_info(profile)
        if info.window == "headless":
            raise ApiError(409, f"'{profile.name}' runs headless, so it has no window. Restart it with a normal "
                                "window to work in it.", "headless")
        target = _str(data, "target", limit=128)
        if not target:
            try:
                targets = await cdp.page_targets(info.cdp_port)  # type: ignore[arg-type]
            except cdp.CdpError:
                targets = []
            target = targets[0]["id"] if targets else None
        moved = None
        if target:
            with contextlib.suppress(cdp.CdpError):
                await cdp.activate_target(info.cdp_port, target)  # type: ignore[arg-type]
            with contextlib.suppress(cdp.CdpError):
                moved = await cdp.bring_onscreen(info.cdp_ws_url, target)  # type: ignore[arg-type]
        focused = await run(self.focuser, info.chrome_pid)
        return ok({"ok": True, "focused": bool(focused), "bounds": moved})

    @endpoint
    async def open_url(self, request: Request) -> Response:
        """Open a page in the profile's browser (a new tab, like a link from another app); a stopped
        profile is started with it."""
        data = await read_body(request)
        raw = (_str(data, "url", limit=4000) or "").strip()
        if not raw:
            raise ApiError(400, "Type an address, e.g. example.com", "invalid")
        from ..safety import normalize_url

        url = normalize_url(raw)
        if urlsplit(url).scheme not in ("http", "https"):
            raise ApiError(400, "Only http:// and https:// addresses can be opened.", "invalid")
        profile = await run(self._resolve, request.path_params["pid"])
        state, _info, _ = await run(runtime_state, self.store, profile.id)
        if state == "running":
            await run(self.runtime.open_url, profile.id, url)
            started = False
        else:
            self.hub.publish("profile", {**(await run(self.profile_view, profile)), "state": "starting"})
            await run(partial(self.runtime.start, profile.id, start_url=url))
            started = True
        await run(self._log, "open page", f"Opened {urlsplit(url).netloc} in '{profile.name}'.", profile=profile)
        return ok({"started": started, "profile": await self._publish_profile(profile.id)})

    @endpoint
    async def stop_all(self, request: Request) -> Response:
        stopped = await run(self.runtime.stop_all)
        self._thumbs.clear()
        await run(self._log, "stop all", f"Stopped {plural(len(stopped), 'profile')}.")
        for p in await run(self.store.list_profiles):
            await self._publish_profile(p.id)
        return ok({"stopped": stopped})

    @endpoint
    async def check_profile(self, request: Request) -> Response:
        profile = await run(self._resolve, request.path_params["pid"])
        state, info, _ = await run(runtime_state, self.store, profile.id)
        started = time.perf_counter()
        host: str | None = None
        if profile.proxy_id:
            with contextlib.suppress(ProfilePilotError):
                host = (await run(self.store.get_proxy, profile.proxy_id)).host
        if state == "running" and info is not None and info.relay_port:
            result = await self.checks.check_relay(info.http_proxy_url)
            route = f"live relay ({info.upstream or 'direct'})"
        elif profile.proxy_id:
            rec = await run(self.store.get_proxy, profile.proxy_id)
            endpoint_ = await run(self.store.proxy_endpoint, rec.id)
            result = await self.checks.check_proxy(endpoint_)
            await run(self.store.set_proxy_check, rec.id, result)
            await run(self.record_history, rec.id, result)
            self.hub.publish("proxies", {})
            route = f"saved proxy {rec.name}"
        else:
            result = await self.checks.check_proxy(None)
            route = "direct connection"
        view = check_view(result, host=host) or {}
        summary = (f"Exit IP {result.ip} {result.country_code or ''} via {route}" if result.ok
                   else f"Couldn't reach the internet via {route}: {view.get('reason')}")
        await run(self._log, "check route", summary, profile=profile, ok=result.ok,
                  ms=int((time.perf_counter() - started) * 1000))
        return ok({"route": route, "check": view})

    @endpoint
    async def screenshot(self, request: Request) -> Response:
        profile = await run(self._resolve, request.path_params["pid"])
        now = time.monotonic()
        cached = self._thumbs.get(profile.id)
        if cached is None or now - cached.at >= THUMB_INTERVAL:
            lock = self._thumb_locks.setdefault(profile.id, asyncio.Lock())
            async with lock:
                cached = self._thumbs.get(profile.id)
                if cached is None or time.monotonic() - cached.at >= THUMB_INTERVAL:
                    cached = await self._capture(profile)
                    self._thumbs[profile.id] = cached
        headers = {"Cache-Control": "no-store", "X-Thumb-Age": f"{max(0.0, time.monotonic() - cached.at):.1f}"}
        if cached.data:
            return Response(cached.data, media_type="image/jpeg", headers=headers)
        return Response(status_code=204, headers={**headers, "X-Thumb-State": cached.reason or "unavailable"})

    async def _capture(self, profile: Profile) -> _Thumb:
        state, info, _ = await run(runtime_state, self.store, profile.id)
        if state != "running" or info is None or not info.cdp_port or not info.cdp_ws_url:
            return _Thumb(time.monotonic(), None, "stopped")
        try:
            targets = await cdp.page_targets(info.cdp_port)
            if not targets:
                return _Thumb(time.monotonic(), None, "no-page")
            if str(targets[0].get("url") or "").startswith("chrome-error://"):
                # Chrome's own error page (usually a proxy that does not answer): say so instead of
                # showing a dark, unexplained thumbnail.
                return _Thumb(time.monotonic(), None, "page-error")
            data = await cdp.capture_thumbnail(info.cdp_ws_url, targets[0]["id"])
            return _Thumb(time.monotonic(), data, None)
        except cdp.ThumbnailUnavailable as exc:
            return _Thumb(time.monotonic(), None, exc.reason)
        except cdp.CdpError as exc:
            log.debug("thumbnail of %s failed: %s", profile.id, exc)
            return _Thumb(time.monotonic(), None, "error")

    @endpoint
    async def tabs(self, request: Request) -> Response:
        profile = await run(self._resolve, request.path_params["pid"])
        info = await self._running_info(profile)
        targets = await cdp.page_targets(info.cdp_port)  # type: ignore[arg-type]
        return ok({"tabs": [{"id": t.get("id"), "title": str(t.get("title") or "")[:300],
                             "url": str(t.get("url") or "")[:2000], "active": i == 0}
                            for i, t in enumerate(targets)]})

    @endpoint
    async def tab_action(self, request: Request) -> Response:
        profile = await run(self._resolve, request.path_params["pid"])
        info = await self._running_info(profile)
        target, action = request.path_params["target"], request.path_params["action"]
        if action == "activate":
            done = await cdp.activate_target(info.cdp_port, target)  # type: ignore[arg-type]
            if done and info.window != "headless":
                await run(self.focuser, info.chrome_pid)
        elif action == "close":
            targets = await cdp.page_targets(info.cdp_port)  # type: ignore[arg-type]
            if len(targets) <= 1:
                raise ApiError(409, "This is the profile's last tab; stop the profile instead.", "last_tab")
            done = await cdp.close_target(info.cdp_port, target)  # type: ignore[arg-type]
        else:
            raise ApiError(404, "Unknown tab action.", "not_found")
        if not done:
            raise ApiError(404, "That tab is gone.", "not_found")
        return ok()

    # ------------------------------------------------------------------ proxies

    @endpoint
    async def list_proxies(self, request: Request) -> Response:
        return ok({"proxies": await run(self.proxies_view)})

    @staticmethod
    def _parse_lines(text: str, scheme: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            spec, label = line, None
            if " #" in line:
                spec, _, label = line.partition(" #")
                spec, label = spec.strip(), label.strip() or None
            try:
                ep = parse_proxy(spec, scheme)
                out.append({"line": lineno, "ok": True, "scheme": ep.scheme, "host": ep.host, "port": ep.port,
                            "username": mask_username(ep.username), "has_password": ep.password is not None,
                            "name": label, "spec": spec})
            except ProxyParseError:
                out.append({"line": lineno, "ok": False, "error": "could not parse this line", "spec": spec})
        return out

    @endpoint
    async def parse_proxies(self, request: Request) -> Response:
        data = await read_body(request, limit=MAX_IMPORT_BODY)
        scheme = data.get("scheme") or "http"
        if scheme not in SCHEMES:
            raise ApiError(400, "scheme must be http, https, socks4 or socks5.", "invalid")
        parsed = self._parse_lines(_str(data, "text", limit=MAX_IMPORT_BODY) or "", scheme)
        for item in parsed:
            item.pop("spec", None)
        return ok({"lines": parsed[:500], "valid": sum(1 for p in parsed if p["ok"]),
                   "invalid": sum(1 for p in parsed if not p["ok"]), "total": len(parsed)})

    @endpoint
    async def add_proxies(self, request: Request) -> Response:
        data = await read_body(request, limit=MAX_IMPORT_BODY)
        scheme = data.get("scheme") or "http"
        if scheme not in SCHEMES:
            raise ApiError(400, "scheme must be http, https, socks4 or socks5.", "invalid")
        text = _str(data, "text", limit=MAX_IMPORT_BODY) or _str(data, "url", limit=4000) or ""
        tags = _tags(data) or []
        single_name = (_str(data, "name", limit=64) or "").strip() or None

        def add_all() -> dict[str, Any]:
            before = {p.id for p in self.store.list_proxies()}
            parsed = self._parse_lines(text, scheme)
            if not parsed:
                raise ApiError(400, "No proxy given.", "invalid")
            added: list[ProxyRecord] = []
            errors: list[str] = []
            for item in parsed:
                if not item["ok"]:
                    errors.append(f"line {item['line']}: could not parse the proxy")
                    continue
                label = item.get("name") or (single_name if len(parsed) == 1 else None)
                try:
                    endpoint_ = parse_proxy(item["spec"], scheme)
                    added.append(self.store.add_proxy(endpoint_, label, default_scheme=scheme, tags=tags))
                except ProxyParseError:
                    errors.append(f"line {item['line']}: could not parse the proxy")
                except ProfilePilotError as exc:
                    errors.append(f"line {item['line']}: {_scrub(str(exc))}")
            if not added:
                raise ApiError(400, "Nothing was imported (" + "; ".join(errors[:3])
                               + ("; ..." if len(errors) > 3 else "") + "). " + PROXY_FORMAT_HELP, "proxy_format")
            created = [r for r in added if r.id not in before]
            return {"added": [self.proxy_view(r) for r in added], "created": len(created),
                    "existing": len(added) - len(created), "errors": errors}

        result = await run(add_all)
        if result["created"]:
            await run(self._log, "add proxies", f"Added {plural(result['created'], 'proxy', 'proxies')}.")
        self.hub.publish("proxies", {})
        return ok(result, 201)

    @endpoint
    async def update_proxy(self, request: Request) -> Response:
        data = await read_body(request)

        def update() -> tuple[ProxyRecord, list[str]]:
            rec = self.store.get_proxy(request.path_params["xid"])
            notes: list[str] = []
            endpoint_keys = {"scheme", "host", "port", "username", "password", "clear_password", "url"}
            if endpoint_keys & set(data):
                if data.get("url"):
                    url = _str(data, "url", limit=2000) or ""
                    parse_proxy(url, data.get("scheme") or rec.scheme)  # validate (generic error on failure)
                    self.store.update_proxy(rec.id, url=url, default_scheme=data.get("scheme") or rec.scheme)
                else:
                    current = self.store.proxy_endpoint(rec.id)
                    scheme = data.get("scheme") or rec.scheme
                    if scheme not in SCHEMES:
                        raise ApiError(400, "scheme must be http, https, socks4 or socks5.", "invalid")
                    host = (_str(data, "host", limit=255) or rec.host).strip()
                    try:
                        port = int(data.get("port") or rec.port)
                    except (TypeError, ValueError):
                        raise ApiError(400, "port must be a number.", "invalid") from None
                    username = rec.username
                    if "username" in data:
                        username = (_str(data, "username", limit=255) or "").strip() or None
                    password = current.password
                    if data.get("password"):
                        password = _str(data, "password", limit=1024)
                    elif _bool(data, "clear_password"):
                        password = None
                    if password is not None and not username:
                        raise ApiError(400, "A proxy password needs a username.", "invalid")
                    endpoint_ = ProxyEndpoint(scheme, host, port, username, password)
                    self.store.update_proxy(rec.id, url=endpoint_.to_url(with_auth=True), default_scheme=scheme)
                users = [p for p in self.store.list_profiles() if p.proxy_id == rec.id]
                for p in users:
                    state, info, _ = runtime_state(self.store, p.id)
                    if state == "running" and info is not None and info.proxy_id == rec.id:
                        try:
                            self.runtime.set_upstream(p.id, rec.id)
                            notes.append(f"'{p.name}' uses the new address for new connections.")
                        except ProfilePilotError:
                            notes.append(f"Restart '{p.name}' to use the new address.")
            meta = {k: data[k] for k in ("name", "notes") if k in data}
            if "tags" in data:
                meta["tags"] = _tags(data) or []
            if meta:
                if "name" in meta:
                    meta["name"] = (_str(meta, "name", limit=64) or "").strip() or None
                if "notes" in meta:
                    meta["notes"] = _str(meta, "notes", limit=4000) or ""
                self.store.update_proxy(rec.id, **meta)
            return self.store.get_proxy(rec.id), notes

        rec, notes = await run(update)
        self.hub.publish("proxies", {})
        return ok({"proxy": self.proxy_view(rec), "notes": notes})

    @endpoint
    async def delete_proxy(self, request: Request) -> Response:
        force = request.query_params.get("force") in ("1", "true", "yes")
        rec = await run(self.store.get_proxy, request.path_params["xid"])
        unbound = await run(partial(self.store.remove_proxy, rec.id, force=force))
        await run(self._forget_history, rec.id)
        await run(self._log, "remove proxy", f"Removed proxy '{rec.name}'.")
        self.hub.publish("proxies", {})
        for p in await run(self.store.list_profiles):
            if p.name in unbound:
                await self._publish_profile(p.id)
        return ok({"unbound": unbound})

    async def _test_one(self, rec: ProxyRecord) -> dict[str, Any]:
        endpoint_ = await run(self.store.proxy_endpoint, rec.id)
        result = await self.checks.check_proxy(endpoint_)
        await run(self.store.set_proxy_check, rec.id, result)
        await run(self.record_history, rec.id, result)
        fresh = await run(self.store.get_proxy, rec.id)
        return self.proxy_view(fresh, history=(await run(self._history)).get(rec.id))

    @endpoint
    async def test_proxy(self, request: Request) -> Response:
        rec = await run(self.store.get_proxy, request.path_params["xid"])
        view = await self._test_one(rec)
        check = view["last_check"] or {}
        await run(self._log, "test proxy", (f"'{rec.name}': exit IP {check.get('ip')} {check.get('country_code') or ''} "
                                            f"{check.get('latency_ms')} ms") if check.get("ok")
                  else f"'{rec.name}' failed: {check.get('reason')}", ok=bool(check.get("ok")),
                  ms=int(check.get("latency_ms") or 0))
        self.hub.publish("proxies", {})
        return ok({"proxy": view})

    def _job_view(self, job: _TestJob, **extra: Any) -> dict[str, Any]:
        return {"job": job.id, "total": job.total, "done": job.done, "ids": list(job.ids), **extra}

    @endpoint
    async def test_all_proxies(self, request: Request) -> Response:
        if self._test_job is not None and self._test_job.running:
            return ok(self._job_view(self._test_job, already_running=True))
        data = await read_body(request)
        ids = data.get("ids")
        records = await run(self.store.list_proxies)
        if isinstance(ids, list) and ids:
            wanted = {str(i) for i in ids}
            records = [r for r in records if r.id in wanted]
        job = _TestJob(id=secrets.token_hex(4), total=len(records), ids=[r.id for r in records])
        self._test_job = job
        # Every open Manager window marks these rows "Testing" (also when the job came from a toast).
        self.hub.publish("proxy-test", self._job_view(job, started=True))

        async def run_all() -> None:
            sem = asyncio.Semaphore(TEST_CONCURRENCY)
            ok_count = 0

            async def one(rec: ProxyRecord) -> None:
                nonlocal ok_count
                async with sem:
                    try:
                        view = await self._test_one(rec)
                    except Exception as exc:  # a deleted proxy etc.
                        log.debug("test of %s failed: %s", rec.id, exc)
                        view = None
                job.done += 1
                if view and (view.get("last_check") or {}).get("ok"):
                    ok_count += 1
                self.hub.publish("proxy-test", {"job": job.id, "proxy": view, "proxy_id": rec.id, "done": job.done,
                                                "total": job.total})

            try:
                await asyncio.gather(*(one(r) for r in records))
            except asyncio.CancelledError:
                if not job.cancelled:
                    raise  # the Manager is shutting down
                return  # cancel_proxy_test reports it
            self.hub.publish("proxy-test", {"job": job.id, "done": job.done, "total": job.total, "finished": True,
                                            "ok": ok_count})
            self.hub.publish("proxies", {})
            await run(self._log, "test all proxies", f"Tested {plural(job.total, 'proxy', 'proxies')}: {ok_count} "
                                                     f"working, {job.total - ok_count} failed.", ok=ok_count == job.total)

        job.task = self._spawn(run_all())
        return ok(self._job_view(job), 202)

    @endpoint
    async def cancel_proxy_test(self, request: Request) -> Response:
        job = self._test_job
        if job is None or not job.running:
            return ok({"cancelled": False})
        job.cancelled = True
        assert job.task is not None
        job.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(job.task), 5)
        self.hub.publish("proxy-test", {"job": job.id, "done": job.done, "total": job.total, "finished": True,
                                        "cancelled": True})
        self.hub.publish("proxies", {})
        await run(self._log, "test all proxies", f"Stopped the proxy test after {job.done} of "
                                                 f"{plural(job.total, 'proxy', 'proxies')}.")
        return ok({"cancelled": True, "done": job.done, "total": job.total})

    # ------------------------------------------------------------------ identities

    @endpoint
    async def list_identities(self, request: Request) -> Response:
        return ok({"identities": await run(self.identities_view)})

    @endpoint
    async def get_identity(self, request: Request) -> Response:
        ident = await run(self.identities.get, request.path_params["iid"])
        return ok(await run(self.identity_view, ident))

    @staticmethod
    def _plain_values(data: dict[str, Any]) -> dict[str, str | None]:
        values = data.get("values") or {}
        if not isinstance(values, dict):
            raise ApiError(400, "'values' must be an object.", "invalid")
        from ..identity import field_key

        out: dict[str, str | None] = {}
        for key, value in values.items():
            canonical = field_key(str(key))
            if FIELDS[canonical].sensitive:
                raise ApiError(400, f"{FIELDS[canonical].label} is write-only: set it with its secure field.",
                               "sensitive")
            if value is not None and not isinstance(value, (str, int, float)):
                raise ApiError(400, f"{FIELDS[canonical].label} must be text.", "invalid")
            out[canonical] = None if value is None else str(value)[:500]
        return out

    @endpoint
    async def create_identity(self, request: Request) -> Response:
        data = await read_body(request)
        name = (_str(data, "name", limit=64) or "").strip()
        values = {k: v for k, v in self._plain_values(data).items() if v}
        ident = await run(partial(self.identities.create, name, values, notes=_str(data, "notes", limit=4000) or ""))
        await run(self._log, "create identity", f"Created identity '{ident.name}'.")
        self.hub.publish("identities", {})
        return ok(await run(self.identity_view, ident), 201)

    @endpoint
    async def update_identity(self, request: Request) -> Response:
        data = await read_body(request)
        values = self._plain_values(data)
        name = _str(data, "name", limit=64)
        notes = _str(data, "notes", limit=4000)
        ident = await run(partial(self.identities.update, request.path_params["iid"], values or None,
                                  name=name.strip() if name else None, notes=notes))
        self.hub.publish("identities", {})
        return ok(await run(self.identity_view, ident))

    @endpoint
    async def delete_identity(self, request: Request) -> Response:
        ident = await run(self.identities.get, request.path_params["iid"])

        def delete() -> list[str]:
            linked = self.store.profiles_using_identity(ident.id)
            self.identities.delete(ident.id)
            for p in linked:
                self.store.update_profile(p.id, identity_id=None)
            return [p.id for p in linked]

        unlinked = await run(delete)
        await run(self._log, "delete identity", f"Deleted identity '{ident.name}' and its stored secrets.")
        self.hub.publish("identities", {})
        for pid in unlinked:
            await self._publish_profile(pid)
        return ok({"deleted": ident.name, "unlinked": unlinked})

    @endpoint
    async def set_identity_secret(self, request: Request) -> Response:
        from ..identity import field_key

        key = field_key(request.path_params["field"])
        if not FIELDS[key].sensitive:
            raise ApiError(400, f"{FIELDS[key].label} is not a write-only field; edit it in the form.", "invalid")
        if request.method == "DELETE":
            value = None
        else:
            data = await read_body(request)
            value = _str(data, "value", limit=512)
            if not value or not value.strip():
                raise ApiError(400, f"{FIELDS[key].label}: value is empty.", "invalid")
        ident = await run(self.identities.set_sensitive, request.path_params["iid"], key, value)
        await run(self._log, "identity secret", f"{'Set' if value else 'Cleared'} {FIELDS[key].label} of '{ident.name}'.")
        self.hub.publish("identities", {})
        return ok(await run(self.identity_view, ident))

    @endpoint
    async def identity_origins(self, request: Request) -> Response:
        data = await read_body(request)
        origin = _str(data, "origin", limit=500) or request.query_params.get("origin") or ""
        if not origin.strip():
            raise ApiError(400, "Give a site, e.g. https://shop.example.com", "invalid")
        if request.method == "DELETE":
            ident = await run(self.identities.disallow_origin, request.path_params["iid"], origin)
        else:
            from ..identity import normalize_origin

            if not origin_is_secure(normalize_origin(origin)):
                raise ApiError(400, "Use an https:// address – card details are never filled on insecure pages.",
                               "insecure_origin")
            ident = await run(self.identities.allow_origin, request.path_params["iid"], origin)
        self.hub.publish("identities", {})
        return ok(await run(self.identity_view, ident))

    @endpoint
    async def autofill_sources(self, request: Request) -> Response:
        """Browser profiles with saved addresses (summaries only) for the "Connect to browser" dialog."""
        return ok({"sources": await run(self.autofill_sources_view)})

    @endpoint
    async def identity_chrome(self, request: Request) -> Response:
        """POST ``{source, address}``: take the identity's name, email, phone and address live from a
        browser profile's saved addresses (``address``: its number in the list, its id, or empty for the
        most used one). DELETE: unlink. Cards, passwords and IDs are never read from the browser."""
        from ..chrome_autofill import PROFILE_SOURCE, is_source_ref

        iid = request.path_params["iid"]
        if request.method == "DELETE":
            ident = await run(self.identities.disconnect_chrome, iid)
            await run(self._log, "identity browser link", f"Identity '{ident.name}' no longer takes details from a browser.")
            self.hub.publish("identities", {})
            return ok(await run(self.identity_view, ident))
        data = await read_body(request)
        source = (_str(data, "source", limit=200) or "chrome").strip()
        if not is_source_ref(source) or source.lower() == PROFILE_SOURCE:
            raise ApiError(400, "Pick one of your browser profiles (chrome, chrome:edge or chrome:chrome/Default).",
                           "invalid")
        address = data.get("address")
        if address is not None and (isinstance(address, bool) or not isinstance(address, (int, str))):
            raise ApiError(400, "'address' must be the number of an address in the list, or its id.", "invalid")
        if isinstance(address, str):
            address = address.strip()[:64] or None
        ident = await run(self.identities.connect_chrome, iid, source, address)
        view = await run(self.identity_view, ident)
        link = view.get("chrome") or {}
        which = "a chosen saved address" if link.get("pinned") else "the most used saved address"
        await run(self._log, "identity browser link",
                  f"Identity '{ident.name}' now takes its details from {link.get('label') or source} ({which}).")
        self.hub.publish("identities", {})
        return ok(view)

    # ------------------------------------------------------------------ help

    @endpoint
    async def list_help(self, request: Request) -> Response:
        return ok({"help": await run(self.open_help_views)})

    @endpoint
    async def resolve_help(self, request: Request) -> Response:
        data = await read_body(request)
        status = data.get("status") or "done"
        if status not in ("done", "dismissed"):
            raise ApiError(400, "status must be 'done' or 'dismissed'.", "invalid")
        profile = await run(self._resolve, request.path_params["pid"])
        req = await run(partial(self.control.resolve_help, profile.id, request.path_params["rid"], status=status,
                                note=_str(data, "note", limit=200) or ""))
        verb = "Marked as done" if status == "done" else "Dismissed"
        await run(self._log, "help request", f"{verb}: '{req.message}'.", profile=profile)
        view = await self._publish_profile(profile.id)
        return ok({"request": req.model_dump(mode="json"), "profile": view})

    # ------------------------------------------------------------------ activity & events

    @endpoint
    async def list_activity(self, request: Request) -> Response:
        q = request.query_params
        try:
            limit = max(1, min(int(q.get("limit") or 200), 2000))
        except ValueError:
            raise ApiError(400, "limit must be a number.", "invalid") from None
        profile_id = None
        if q.get("profile"):
            profile_id = (await run(self._resolve, q["profile"])).id
        ok_filter = {"ok": True, "error": False, "errors": False}.get(q.get("status") or "")
        events = await run(partial(self.activity.tail, limit, profile_id=profile_id, tool=q.get("tool") or None,
                                   ok=ok_filter))
        return ok({"events": [e.model_dump(mode="json") for e in reversed(events)]})

    async def events(self, request: Request) -> Response:
        q = request.query_params
        try:
            max_events = max(0, int(q.get("max_events") or 0))
            timeout = max(0.0, float(q.get("timeout") or 0))
        except ValueError:
            return error_response(400, "max_events / timeout must be numbers.", "invalid")
        queue = self.hub.subscribe()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout else None

        async def stream() -> Any:
            sent = 0
            event_id = 0
            try:
                yield "retry: 3000\n\n"
                yield sse_format("ready", {"version": __version__, "ts": datetime.now(timezone.utc).isoformat()})
                while True:
                    wait = 15.0
                    if deadline is not None:
                        wait = min(wait, deadline - loop.time())
                        if wait <= 0:
                            break
                    try:
                        item = await asyncio.wait_for(queue.get(), wait)
                    except asyncio.TimeoutError:
                        if deadline is not None and loop.time() >= deadline:
                            break
                        yield ": keep-alive\n\n"
                        continue
                    if item is None:
                        break
                    event_id += 1
                    yield sse_format(item[0], item[1], event_id)
                    sent += 1
                    if max_events and sent >= max_events:
                        break
            finally:
                self.hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ trash

    @endpoint
    async def list_trash(self, request: Request) -> Response:
        return ok({"trash": await run(self.trash_view)})

    @endpoint
    async def restore_trash(self, request: Request) -> Response:
        trash_id = request.path_params["tid"]
        if any(ch in trash_id for ch in "/\\") or trash_id.startswith("."):
            raise ApiError(400, "Invalid trash id.", "invalid")
        profile = await run(self.store.restore_profile, trash_id)
        await run(self._log, "restore profile", f"Restored '{profile.name}' from the trash.", profile=profile)
        self.hub.publish("trash", {})
        return ok(await self._publish_profile(profile.id))

    @endpoint
    async def empty_trash(self, request: Request) -> Response:
        removed = await run(partial(self.store.purge_trash, 0))
        await run(self._log, "empty trash", f"Permanently deleted {plural(removed, 'profile')} from the trash.")
        self.hub.publish("trash", {})
        return ok({"removed": removed})

    # ------------------------------------------------------------------ settings

    @endpoint
    async def get_settings(self, request: Request) -> Response:
        return ok(await run(self.settings_view))

    @endpoint
    async def update_settings(self, request: Request) -> Response:
        data = await read_body(request)

        def update() -> None:
            with lock_for(self.store.config_file):
                cfg = self.store.load_config().model_dump()
                if "default_window" in data:
                    if data["default_window"] not in WINDOW_MODES:
                        raise ApiError(400, "default_window must be normal, offscreen or headless.", "invalid")
                    cfg["default_window"] = data["default_window"]
                if "max_running" in data:
                    value = data["max_running"]
                    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 500:
                        raise ApiError(400, "max_running must be a whole number from 0 (no limit) to 500.", "invalid")
                    cfg["max_running"] = value
                if "escape_client_job" in data:
                    cfg["escape_client_job"] = bool(_bool(data, "escape_client_job"))
                if "autofill_from_browser" in data:
                    value = _bool(data, "autofill_from_browser")
                    if value is None:
                        raise ApiError(400, "'autofill_from_browser' must be true or false.", "invalid")
                    cfg["autofill_from_browser"] = value
                if "browser_path" in data:
                    value = data["browser_path"]
                    if value in (None, ""):
                        cfg["browser_path"] = None
                    else:
                        installed = {b["path"] for b in self.browsers_view()}
                        if value not in installed:
                            raise ApiError(400, "Pick one of the installed browsers (custom paths can be set with "
                                                "the CLI).", "invalid")
                        cfg["browser_path"] = value
                if isinstance(data.get("shardx"), dict):
                    sx = data["shardx"]
                    if "enabled" in sx:
                        cfg["shardx"]["enabled"] = bool(_bool(sx, "enabled"))
                    if "base_url" in sx and sx["base_url"]:
                        url = (_str(sx, "base_url", limit=200) or "").strip().rstrip("/")
                        parts = urlsplit(url)
                        if parts.scheme not in ("http", "https") or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
                            raise ApiError(400, "The ShardX launcher URL must be on this computer, e.g. "
                                                "http://127.0.0.1:40325", "invalid")
                        cfg["shardx"]["base_url"] = url
                if isinstance(data.get("automation"), dict) and "driver" in data["automation"]:
                    if data["automation"]["driver"] not in ("auto", "patchright", "playwright"):
                        raise ApiError(400, "driver must be auto, patchright or playwright.", "invalid")
                    cfg["automation"]["driver"] = data["automation"]["driver"]
                validated = AppConfig.model_validate(cfg)
                write_json(self.store.config_file, validated.model_dump(mode="json"))

        await run(update)
        await run(self._log, "settings", "Changed settings.")
        self.hub.publish("settings", {})
        return ok(await run(self.settings_view))

    @endpoint
    async def shardx_token(self, request: Request) -> Response:
        from ..integrations.shardx import delete_token, save_token

        if request.method == "DELETE":
            await run(delete_token, self.store.secrets)
        else:
            data = await read_body(request)
            token = _str(data, "token", limit=8192) or ""
            await run(save_token, self.store.secrets, token)
        return ok(await run(self.settings_view))

    @endpoint
    async def browsers(self, request: Request) -> Response:
        return ok({"browsers": await run(self.browsers_view)})

    @endpoint
    async def reveal(self, request: Request) -> Response:
        data = await read_body(request)
        what = data.get("what") or "data"
        if what == "data":
            path = self.store.root
        elif what in ("profile", "downloads", "log"):
            profile = await run(self._resolve, str(data.get("id") or ""))
            path = self.store.profile_dir(profile.id)
            if what == "downloads":
                path = await run(self.store.downloads_dir, profile.id)
            elif what == "log" and (path / "host.log").is_file():
                path = path / "host.log"
        else:
            raise ApiError(400, "what must be data, profile, downloads or log.", "invalid")
        await run(self.opener, path)
        return ok({"path": str(path)})

    # ------------------------------------------------------------------ clients

    def clients_view(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if not refresh and self._clients_cache and now - self._clients_cache[0] < CLIENTS_TTL:
            return self._clients_cache[1]
        from .. import install

        python = _registered_python()
        try:
            snippets = install.snippets(python)
        except Exception as exc:  # pragma: no cover
            log.debug("snippets failed: %s", exc)
            snippets = {}
        out = []
        for client in install.CLIENTS:
            label, description = CLIENT_INFO.get(client, (client, ""))
            try:
                registered = install.is_registered(client, self.locations)  # type: ignore[arg-type]
            except Exception:
                registered = None
            try:
                paths = [str(p) for p in install.config_paths(client, self.locations)]  # type: ignore[arg-type]
            except Exception:
                paths = []
            out.append({"client": client, "label": label, "description": description, "registered": registered,
                        "config_paths": paths, "snippet": snippets.get(client, "")})
        self._clients_cache = (now, out)
        return out

    @endpoint
    async def clients(self, request: Request) -> Response:
        refresh = request.query_params.get("refresh") in ("1", "true")
        return ok({"clients": await run(partial(self.clients_view, refresh=refresh))})

    @endpoint
    async def client_action(self, request: Request) -> Response:
        from .. import install

        client, action = request.path_params["client"], request.path_params["action"]
        if client not in install.CLIENTS:
            raise ApiError(404, f"Unknown client '{client}'.", "not_found")
        if action not in ("register", "unregister"):
            raise ApiError(404, "Unknown action.", "not_found")
        label = CLIENT_INFO.get(client, (client,))[0]
        python = _registered_python()
        locations = self.locations if self.locations is not None else await run(install.Locations.detect)
        if client == "claude-code" and locations.claude_cli is None:
            # Without the claude CLI nothing can be registered from here: never report success. The
            # user gets the exact command to run (and a "check again" button).
            command = (install.snippets(python, platform=locations.platform)["claude-code"] if action == "register"
                       else install.format_command(["claude", "mcp", "remove", "--scope", "user", "profilepilot"],
                                                   locations.platform))
            await run(self._log, f"{action} client", "Claude Code: setup command shown (CLI not found).", ok=False)
            clients = await run(partial(self.clients_view, refresh=True))
            return ok({"ok": False, "manual": True, "command": command, "clients": clients,
                       "summary": "The claude command was not found on this computer. Run this in a terminal:"})
        try:
            if action == "register":
                report = await run(partial(install.register, client, python=python,  # type: ignore[arg-type]
                                           locations=locations))
            else:
                report = await run(partial(install.unregister, client, locations=locations))  # type: ignore[arg-type]
        except install.InstallError as exc:
            if client != "claude-code":
                raise
            text = _scrub(str(exc))
            command = text.rsplit("\n", 1)[-1] if "\n" in text else install.snippets(python)["claude-code"]
            await run(self._log, f"{action} client", f"Claude Code: {text.splitlines()[0]}", ok=False)
            clients = await run(partial(self.clients_view, refresh=True))
            return ok({"ok": False, "manual": True, "command": command, "clients": clients,
                       "summary": text.splitlines()[0]})
        if client == "claude-code" and "Run this yourself" in report:  # e.g. a batch-file CLI it cannot run safely
            lines = _scrub(report).splitlines()
            await run(self._log, f"{action} client", "Claude Code: setup command shown.", ok=False)
            clients = await run(partial(self.clients_view, refresh=True))
            return ok({"ok": False, "manual": True, "command": lines[-1], "summary": lines[0], "clients": clients})
        await run(self._log, f"{action} client", f"{'Added ProfilePilot to' if action == 'register' else 'Removed ProfilePilot from'} "
                                                 f"{label}.")
        clients = await run(partial(self.clients_view, refresh=True))
        return ok({"ok": True, "report": _scrub(report), "summary": CLIENT_DONE[action].get(client, "Done."),
                   "clients": clients})

    # ------------------------------------------------------------------ ChatGPT

    def _chatgpt_fallback(self) -> dict[str, Any]:
        """Status from ``chatgpt.json`` alone (when :mod:`profilepilot.connect` is unavailable)."""
        data = read_json(self.store.root / "chatgpt.json", {}) or {}
        if not isinstance(data, dict):
            data = {}
        alive = False
        for key in ("server_pid", "pid"):
            try:
                alive = alive or (bool(data.get(key)) and process_alive(int(data[key]), data.get(f"{key}_create_time")))
            except (TypeError, ValueError):
                pass
        code = None
        try:
            from ..server.oauth import PAIRING_KEY  # type: ignore[attr-defined]

            code = self.store.secrets.get(PAIRING_KEY)
        except Exception:
            code = None
        return {"running": alive and bool(data.get("url")), "url": data.get("url"), "mcp_url": data.get("mcp_url"),
                "tunnel": data.get("tunnel"), "started_at": data.get("started_at"), "port": data.get("port"),
                "pairing_code": code, "connections": []}

    def chatgpt_status(self) -> dict[str, Any]:
        try:
            from .. import connect

            info = dict(connect.status_info(self.store))
        except Exception as exc:  # the ChatGPT modules are optional for the Manager
            log.debug("connect.status_info failed: %s", exc)
            info = self._chatgpt_fallback()
        url = info.get("url") if isinstance(info.get("url"), str) else None
        if url and not info.get("mcp_url"):
            info["mcp_url"] = url if url.rstrip("/").endswith("/mcp") else url.rstrip("/") + "/mcp"
        labels = {"cloudflared": "Cloudflare quick tunnel", "ngrok": "ngrok", "url": "your own public URL",
                  "tunnel-client": "OpenAI Secure MCP Tunnel"}
        connections = []
        for grant in info.get("connections") or []:
            if isinstance(grant, dict):
                connections.append({k: grant.get(k) for k in ("grant_id", "client_id", "client_name", "created_at",
                                                                 "last_used_at")})
        info["connections"] = connections
        info["method"] = labels.get(str(info.get("tunnel") or ""), info.get("tunnel"))
        info["tools"] = {name: bool(_which(name)) for name in ("tunnel-client", "cloudflared", "ngrok")}
        info["commands"] = {"connect": "profilepilot connect chatgpt", "status": "profilepilot connect status",
                            "stop": "profilepilot connect stop"}
        info["running"] = bool(info.get("running"))
        if not info["running"]:
            info["pairing_code"] = None  # only meaningful while the connection runs
        return info

    @endpoint
    async def chatgpt(self, request: Request) -> Response:
        return ok(await run(self.chatgpt_status))

    @endpoint
    async def chatgpt_start(self, request: Request) -> Response:
        """Open a terminal window that runs ``profilepilot connect chatgpt`` for this data folder (the
        wizard starts the secure tunnel and keeps it running while that window stays open)."""
        status = await run(self.chatgpt_status)
        if status.get("running"):
            return ok({"started": False, "status": status})
        argv = [_registered_python(), "-m", "profilepilot", "connect", "chatgpt"]
        env = {"PROFILEPILOT_HOME": str(self.store.root)}
        try:
            await run(self.terminal, argv, env)
        except Exception as exc:  # no terminal program found etc.
            log.debug("could not open a terminal: %s", exc)
            raise ApiError(501, "Couldn't open a terminal window here. Run this in a terminal yourself: "
                                f"{status.get('commands', {}).get('connect') or 'profilepilot connect chatgpt'}",
                           "no_terminal") from None
        await run(self._log, "chatgpt", "Opened a terminal to start the ChatGPT connection.")
        return ok({"started": True, "status": status})

    @endpoint
    async def chatgpt_stop(self, request: Request) -> Response:
        data = await read_body(request)
        revoke = bool(_bool(data, "revoke"))
        try:
            from .. import connect
        except Exception:
            raise ApiError(501, "The ChatGPT connection tools are not installed.", "unavailable") from None
        report = await run(partial(connect.stop_sharing, self.store, revoke=revoke))
        await run(self._log, "chatgpt", report)
        self.hub.publish("chatgpt", {})
        return ok({"report": report, "status": await run(self.chatgpt_status)})

    # ------------------------------------------------------------------ routes

    def routes(self) -> list[Route]:
        r = Route
        return [
            r("/api/ping", self.ping, methods=["GET"]),
            r("/api/overview", self.overview, methods=["GET"]),
            r("/api/meta", self.meta, methods=["GET"]),
            r("/api/profiles", self.list_profiles, methods=["GET"]),
            r("/api/profiles", self.create_profile, methods=["POST"]),
            r("/api/profiles/{pid}", self.get_profile, methods=["GET"]),
            r("/api/profiles/{pid}", self.update_profile, methods=["PATCH"]),
            r("/api/profiles/{pid}", self.delete_profile, methods=["DELETE"]),
            r("/api/profiles/{pid}/clone", self.clone_profile, methods=["POST"]),
            r("/api/profiles/{pid}/start", self.start_profile, methods=["POST"]),
            r("/api/profiles/{pid}/stop", self.stop_profile, methods=["POST"]),
            r("/api/profiles/{pid}/focus", self.focus_profile, methods=["POST"]),
            r("/api/profiles/{pid}/pause", self.pause_profile, methods=["POST"]),
            r("/api/profiles/{pid}/resume", self.resume_profile, methods=["POST"]),
            r("/api/profiles/{pid}/check", self.check_profile, methods=["POST"]),
            r("/api/profiles/{pid}/open", self.open_url, methods=["POST"]),
            r("/api/stop-all", self.stop_all, methods=["POST"]),
            r("/api/profiles/{pid}/screenshot", self.screenshot, methods=["GET"]),
            r("/api/profiles/{pid}/tabs", self.tabs, methods=["GET"]),
            r("/api/profiles/{pid}/tabs/{target}/{action}", self.tab_action, methods=["POST"]),
            r("/api/proxies", self.list_proxies, methods=["GET"]),
            r("/api/proxies", self.add_proxies, methods=["POST"]),
            r("/api/proxies/parse", self.parse_proxies, methods=["POST"]),
            r("/api/proxies/test", self.test_all_proxies, methods=["POST"]),
            r("/api/proxies/test", self.cancel_proxy_test, methods=["DELETE"]),
            r("/api/proxies/{xid}", self.update_proxy, methods=["PATCH"]),
            r("/api/proxies/{xid}", self.delete_proxy, methods=["DELETE"]),
            r("/api/proxies/{xid}/test", self.test_proxy, methods=["POST"]),
            r("/api/identities", self.list_identities, methods=["GET"]),
            r("/api/identities", self.create_identity, methods=["POST"]),
            r("/api/identities/{iid}", self.get_identity, methods=["GET"]),
            r("/api/identities/{iid}", self.update_identity, methods=["PATCH"]),
            r("/api/identities/{iid}", self.delete_identity, methods=["DELETE"]),
            r("/api/identities/{iid}/secret/{field}", self.set_identity_secret, methods=["PUT", "DELETE"]),
            r("/api/identities/{iid}/origins", self.identity_origins, methods=["POST", "DELETE"]),
            r("/api/identities/{iid}/chrome", self.identity_chrome, methods=["POST", "DELETE"]),
            r("/api/autofill/sources", self.autofill_sources, methods=["GET"]),
            r("/api/help", self.list_help, methods=["GET"]),
            r("/api/help/{pid}/{rid}", self.resolve_help, methods=["POST"]),
            r("/api/activity", self.list_activity, methods=["GET"]),
            r("/api/events", self.events, methods=["GET"]),
            r("/api/trash", self.list_trash, methods=["GET"]),
            r("/api/trash", self.empty_trash, methods=["DELETE"]),
            r("/api/trash/{tid}/restore", self.restore_trash, methods=["POST"]),
            r("/api/settings", self.get_settings, methods=["GET"]),
            r("/api/settings", self.update_settings, methods=["PATCH"]),
            r("/api/settings/shardx-token", self.shardx_token, methods=["PUT", "DELETE"]),
            r("/api/browsers", self.browsers, methods=["GET"]),
            r("/api/reveal", self.reveal, methods=["POST"]),
            r("/api/clients", self.clients, methods=["GET"]),
            r("/api/clients/{client}/{action}", self.client_action, methods=["POST"]),
            r("/api/chatgpt", self.chatgpt, methods=["GET"]),
            r("/api/chatgpt/start", self.chatgpt_start, methods=["POST"]),
            r("/api/chatgpt/stop", self.chatgpt_stop, methods=["POST"]),
        ]

    async def aclose(self) -> None:
        await self.hub.close()
        for task in list(self._bg):
            task.cancel()


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32" and name == "cloudflared":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
            if base:
                candidate = Path(base) / "cloudflared" / "cloudflared.exe"
                if candidate.is_file():
                    return str(candidate)
    return None


def _open_path(path: Path) -> None:
    """Show ``path`` in the file manager (the user clicked "Open folder")."""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _open_terminal(argv: list[str], env: dict[str, str]) -> None:
    """Run ``argv`` in a new, visible terminal window that stays open (the user clicked "Start
    connection"). Raises :class:`OSError` when no terminal program is found."""
    full_env = {**os.environ, **env}
    if sys.platform == "win32":
        # A string command line: cmd.exe does not understand list2cmdline's \" escapes, and /k keeps a
        # quoted executable path intact when the line holds exactly one quoted part.
        subprocess.Popen(f"cmd.exe /k {subprocess.list2cmdline(argv)}", env=full_env,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return
    import shlex

    command = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " " + shlex.join(argv)
    if sys.platform == "darwin":
        script = 'tell application "Terminal" to do script "' + command.replace("\\", "\\\\").replace('"', '\\"') + '"'
        subprocess.Popen(["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'])
        return
    for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["konsole", "-e"], ["xterm", "-e"]):
        if shutil.which(term[0]):
            subprocess.Popen([*term, "sh", "-c", f"{command}; exec \"${{SHELL:-sh}}\""], env=full_env)
            return
    raise OSError("no terminal program found")


__all__ = ["ApiError", "Checks", "ManagerAPI", "record_proxy_history", "runtime_state"]
