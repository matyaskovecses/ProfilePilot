"""Optional ShardX launcher backend.

ShardX (ShardBrowser) is an anti-detect browser launcher. Its engine is a patched Chromium that
always runs with a *spoofed* fingerprint, so it is the opposite of ProfilePilot's "native
Chrome" principle; it is supported only as an explicit, clearly labelled opt-in backend.

The launcher exposes a local HTTP API on ``127.0.0.1:40325`` (configurable in its settings).
Every route except ``GET /health`` needs ``Authorization: Bearer <HS256 JWT>``; a missing or
invalid token gets ``401`` with an empty body. Errors are ``{"error": "..."}`` JSON. Launching
through ``POST /profiles/{id}/start`` returns the profile's DevTools endpoint, which ProfilePilot
attaches to with Playwright ``connect_over_cdp`` exactly like a native profile.

Token sources (never logged, never put in exception messages):

* the token shown in ShardX *Settings -> Automation API*, stored in the OS keyring under
  ``shardx:token`` (``profilepilot shardx login --token ...``, see :func:`save_token`), or
* explicit opt-in: mint short-lived tokens from the ``api_secret`` in ShardX's own
  ``settings.json`` (:class:`SettingsTokenMinter`). That reads another application's secret,
  which is why it is off by default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote

import httpx

from ..errors import AmbiguousError, ConflictError, LaunchError, NotFoundError, ProfilePilotError

if TYPE_CHECKING:
    from ..secrets import SecretStore
    from ..store import Store

log = logging.getLogger("profilepilot.shardx")

DEFAULT_BASE_URL = "http://127.0.0.1:40325"
DEFAULT_PORT = 40325
TOKEN_KEY = "shardx:token"
"""Secret-store key of the pasted ShardX API token."""
REF_PREFIX = "shardx:"
"""Prefix that marks a profile reference as a ShardX profile (``shardx:<id-or-name>``)."""
TOKEN_TTL = 300
"""Lifetime in seconds of tokens minted from settings.json."""
SPOOFED_ENGINE_NOTE = (
    "ShardX profiles run ShardX's patched Chromium with a spoofed fingerprint "
    "(not the native Chrome that ProfilePilot profiles use)."
)

TokenProvider = Callable[[], "str | None"]


# --------------------------------------------------------------------------- errors


class ShardXError(ProfilePilotError):
    """Base class for errors reported by (or about) the ShardX launcher."""


class ShardXUnavailableError(ShardXError):
    """The launcher's API is not reachable (not running, API disabled, wrong port)."""


class ShardXAuthError(ShardXError):
    """No token is configured, or the launcher rejected it (401)."""


class ShardXNotFoundError(ShardXError, NotFoundError):
    """The referenced ShardX profile or proxy does not exist."""


class ShardXConflictError(ShardXError, ConflictError):
    """The request conflicts with the launcher's state (e.g. profile already open in its UI)."""


class ShardXLaunchError(ShardXError, LaunchError):
    """The launcher started a browser but did not hand out a usable DevTools endpoint."""


# --------------------------------------------------------------------------- redaction

_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/'\"<>]*@")
_BARE_CREDENTIALS = re.compile(r"(?<![\w/:@.\-*])[^\s:/@'\"<>*]+:[^\s/'\"<>]*@(?=[\w\[])")
_HOST_PORT_USER_PASS = re.compile(
    r"(?i)\b((?:\d{1,3}\.){3}\d{1,3}|localhost|(?:[a-z0-9\-]+\.)+[a-z]{2,}):(\d{1,5}):[^\s:'\"<>]+:[^\s'\"<>]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s'\"<>]+")
_JWT = re.compile(r"\beyJ[\w\-]*\.[\w\-]+\.[\w\-]+")
_JSON_SECRET_FIELD = re.compile(r'(?i)("(?:password|api_secret|token|secret)"\s*:\s*")[^"]*(")')


def redact_secrets(text: str) -> str:
    """Mask proxy credentials, bearer tokens and JWTs in free text (error messages, logs).

    Handles ``scheme://user:pass@host``, ``user:pass@host``, the provider-list format
    ``host:port:user:pass``, ``Bearer <token>``, bare JWTs and JSON ``"password": "..."`` fields.
    """
    if not text:
        return text
    text = _URL_CREDENTIALS.sub(r"\1***:***@", text)
    text = _BARE_CREDENTIALS.sub("***:***@", text)
    text = _HOST_PORT_USER_PASS.sub(r"\1:\2:***:***", text)
    text = _BEARER.sub("Bearer ***", text)
    text = _JWT.sub("***", text)
    text = _JSON_SECRET_FIELD.sub(r"\1***\2", text)
    return text


# --------------------------------------------------------------------------- tokens


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_token(secret: str, *, ttl: int = TOKEN_TTL, now: int | None = None) -> str:
    """Mint a ShardX API token: HS256 JWT ``{sub: "shardx-api", iat, exp: iat + ttl}``.

    The launcher signs with the UTF-8 bytes of ``settings.api_secret`` (the hex string itself,
    not its decoded bytes) and verifies only the signature and ``exp``.
    """
    if not secret:
        raise ShardXAuthError("Cannot mint a ShardX token: the API secret is empty.")
    issued = int(time.time()) if now is None else int(now)
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"sub": "shardx-api", "iat": issued, "exp": issued + int(ttl)}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def default_settings_path() -> Path:
    """Location of the ShardX launcher's ``settings.json`` (``dirs::config_dir()/shardx-launcher``)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "shardx-launcher" / "settings.json"


def read_settings(path: Path | str | None = None) -> dict[str, Any]:
    """Read ShardX's settings.json (read-only; tolerates a UTF-8 BOM and UTF-16 like ShardX does).

    Error messages name the file but never include its contents.
    """
    settings_path = Path(path) if path else default_settings_path()
    try:
        raw = settings_path.read_bytes()
    except FileNotFoundError:
        raise ShardXAuthError(
            f"ShardX settings file not found at {settings_path}. Is the ShardX launcher installed? "
            "Alternatively paste the API token: profilepilot shardx login --token <token>."
        ) from None
    except OSError as exc:
        raise ShardXError(f"Could not read ShardX settings file {settings_path}: {type(exc).__name__}.") from None
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ShardXError(f"ShardX settings file {settings_path} is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ShardXError(f"ShardX settings file {settings_path} has an unexpected format.")
    return data


def base_url_from_settings(path: Path | str | None = None) -> str:
    """``http://127.0.0.1:<api_port>`` as configured in ShardX's settings.json."""
    data = read_settings(path)
    port = data.get("api_port") or DEFAULT_PORT
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return f"http://127.0.0.1:{port}"


class SettingsTokenMinter:
    """Token provider that mints short-lived tokens from ShardX's ``settings.json`` (opt-in).

    The secret is re-read for every mint, so a secret rotated in the launcher ("Regenerate
    token") is picked up after :meth:`invalidate` (the client calls it on a 401).
    """

    def __init__(self, settings_path: Path | str | None = None, *, ttl: int = TOKEN_TTL,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings_path = Path(settings_path) if settings_path else default_settings_path()
        self.ttl = int(ttl)
        self._clock = clock
        self._token: str | None = None
        self._expires = 0
        self._lock = threading.Lock()

    def __call__(self) -> str | None:
        with self._lock:
            now = int(self._clock())
            if self._token is not None and now < self._expires - 30:
                return self._token
            data = read_settings(self.settings_path)
            secret = str(data.get("api_secret") or "")
            if not secret:
                raise ShardXAuthError(
                    f"ShardX settings file {self.settings_path} has no api_secret yet. Start the ShardX "
                    "launcher once so it generates one, or paste the token: profilepilot shardx login --token <token>."
                )
            self._token = mint_token(secret, ttl=self.ttl, now=now)
            self._expires = now + self.ttl
            return self._token

    def invalidate(self) -> None:
        """Forget the cached token (the next call re-reads the secret and mints a new one)."""
        with self._lock:
            self._token = None
            self._expires = 0


def keyring_token_provider(secrets: "SecretStore") -> TokenProvider:
    """Token provider reading the pasted token from ProfilePilot's secret store (``shardx:token``)."""

    def provider() -> str | None:
        return secrets.get(TOKEN_KEY)

    return provider


_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$")


def normalize_token(token: str) -> str:
    """Strip whitespace and a ``Bearer`` prefix; validate that it looks like a JWT."""
    value = (token or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not _TOKEN_SHAPE.match(value):
        raise ProfilePilotError(
            "That does not look like a ShardX API token (expected a JWT like xxxxx.yyyyy.zzzzz). "
            "Copy it from ShardX -> Settings -> Automation API."
        )
    return value


def save_token(secrets: "SecretStore", token: str) -> None:
    """Store the ShardX API token in the OS keyring / encrypted secret file."""
    secrets.set(TOKEN_KEY, normalize_token(token))


def delete_token(secrets: "SecretStore") -> None:
    """Forget the stored ShardX API token."""
    secrets.delete(TOKEN_KEY)


# --------------------------------------------------------------------------- client


def _strip_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if ref.lower().startswith(REF_PREFIX):
        ref = ref[len(REF_PREFIX):].strip()
    return ref


def _cdp_result(profile_id: str, cdp: dict[str, Any], pid: Any = None) -> dict[str, Any]:
    return {
        "port": cdp.get("port"),
        "http_url": cdp.get("http_url"),
        "web_socket_debugger_url": cdp.get("web_socket_debugger_url"),
        "profile_id": profile_id,
        "pid": pid,
    }


class ShardXClient:
    """Synchronous client for the ShardX launcher's local automation API (httpx).

    Thread-safe. Async callers use :class:`AsyncShardXClient` (same methods, awaited).

    :param base_url: launcher API base URL (default ``http://127.0.0.1:40325``).
    :param token: a fixed API token (takes precedence over ``token_provider``).
    :param token_provider: callable returning the current token (re-evaluated per request). If it
        has an ``invalidate()`` method it is called on a 401 and the request is retried once.
    :param timeout: per-request timeout in seconds.
    :param start_timeout: timeout for ``POST /profiles/{id}/start`` (the launcher waits up to
        30 s for the browser's DevTools port).
    :param cdp_wait: how long :meth:`start` polls ``/running`` when the launcher returned no
        DevTools endpoint.
    :param transport: optional httpx transport (tests).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        *,
        token_provider: TokenProvider | None = None,
        timeout: float = 10.0,
        start_timeout: float = 60.0,
        cdp_wait: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._token = normalize_token(token) if token else None
        self._provider = token_provider
        self.timeout = timeout
        self.start_timeout = start_timeout
        self.cdp_wait = cdp_wait
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport,
                                  trust_env=False, follow_redirects=False)

    # ------------------------------------------------------------------ construction

    @classmethod
    def from_store(cls, store: "Store", *, settings_path: Path | str | None = None, **kwargs: Any) -> "ShardXClient":
        """Build a client from ProfilePilot's config (``config.shardx``) and secret store."""
        cfg = store.load_config().shardx
        base_url = cfg.base_url or DEFAULT_BASE_URL
        if cfg.token_source == "settings":
            provider: TokenProvider = SettingsTokenMinter(settings_path)
            if base_url.rstrip("/") == DEFAULT_BASE_URL:
                try:  # follow a port changed in the launcher's settings
                    base_url = base_url_from_settings(settings_path)
                except ShardXError:
                    pass
        else:
            provider = keyring_token_provider(store.secrets)
        return cls(base_url, token_provider=provider, **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ShardXClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # never shows the token
        return f"ShardXClient(base_url={self.base_url!r})"

    # ------------------------------------------------------------------ plumbing

    def _current_token(self) -> str:
        token = self._token
        if token is None and self._provider is not None:
            token = self._provider()
        if not token:
            raise ShardXAuthError(
                "No ShardX API token configured. Copy the token from ShardX -> Settings -> Automation API "
                "and run: profilepilot shardx login --token <token>"
            )
        return token

    def _request(self, method: str, path: str, *, auth: bool = True, json_body: Any = None,
                 timeout: float | None = None) -> Any:
        retried = False
        while True:
            headers = {"Accept": "application/json"}
            if auth:
                headers["Authorization"] = f"Bearer {self._current_token()}"
            try:
                resp = self._http.request(
                    method, path, headers=headers, json=json_body,
                    timeout=timeout if timeout is not None else self.timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                    raise ShardXUnavailableError(
                        f"The ShardX launcher at {self.base_url} did not answer {method} {path} "
                        f"({type(exc).__name__})."
                    ) from None
                raise ShardXUnavailableError(
                    f"The ShardX launcher is not reachable at {self.base_url} ({type(exc).__name__}). "
                    "Open the ShardX launcher and check that Settings -> Automation API is enabled "
                    "(changing its port needs a launcher restart)."
                ) from None
            except httpx.HTTPError as exc:
                raise ShardXUnavailableError(
                    f"The ShardX launcher is not reachable at {self.base_url} ({type(exc).__name__}). "
                    "Open the ShardX launcher and check that Settings -> Automation API is enabled "
                    "(changing its port needs a launcher restart)."
                ) from None
            log.debug("shardx %s %s -> %s", method, path, resp.status_code)
            if resp.status_code == 401 and auth and not retried and self._token is None:
                invalidate = getattr(self._provider, "invalidate", None)
                if callable(invalidate):
                    invalidate()
                    retried = True
                    continue
            return self._handle(resp, method, path)

    def _handle(self, resp: httpx.Response, method: str, path: str) -> Any:
        status = resp.status_code
        if 200 <= status < 300:
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                raise ShardXError(f"ShardX returned a non-JSON response for {method} {path}.") from None
        message = redact_secrets(_error_message(resp))
        if status == 401:
            if isinstance(self._provider, SettingsTokenMinter) and self._token is None:
                hint = ("The API secret in ShardX's settings.json was rejected; if it was just regenerated, "
                        "restart the ShardX launcher.")
            else:
                hint = ("The token was probably regenerated in ShardX -> Settings -> Automation API. Copy the "
                        "new one and run: profilepilot shardx login --token <token>")
            raise ShardXAuthError(f"ShardX rejected the API token (401). {hint}")
        detail = f": {message}" if message else ""
        if status == 404:
            raise ShardXNotFoundError(f"ShardX: not found ({method} {path}){detail}")
        if status == 409:
            raise ShardXConflictError(f"ShardX refused {method} {path} (409){detail}")
        if status == 501:
            raise ShardXError(f"This ShardX launcher build does not support {method} {path}{detail}")
        raise ShardXError(f"ShardX API error {status} on {method} {path}{detail}")

    # ------------------------------------------------------------------ API

    def health(self) -> dict[str, Any]:
        """``GET /health`` (no auth). Raises :class:`ShardXUnavailableError` if it is not ShardX."""
        try:
            data = self._request("GET", "/health", auth=False)
        except ShardXUnavailableError:
            raise
        except ShardXError:
            data = None
        if not isinstance(data, dict) or data.get("name") != "shardx-launcher" or not data.get("ok"):
            raise ShardXUnavailableError(
                f"Something answers at {self.base_url}, but it is not the ShardX launcher API."
            )
        return data

    def is_available(self) -> bool:
        """True if the launcher's API answers ``/health``."""
        try:
            self.health()
            return True
        except ShardXError:
            return False

    def status(self) -> dict[str, Any]:
        """Model-safe summary that never raises: reachability, version, auth state, running count."""
        out: dict[str, Any] = {"base_url": self.base_url, "reachable": False, "authenticated": False,
                               "note": SPOOFED_ENGINE_NOTE}
        try:
            out["version"] = self.health().get("version")
            out["reachable"] = True
            out["running"] = len(self.running())
            out["authenticated"] = True
        except ShardXError as exc:
            out["error"] = str(exc)
        return out

    def list_profiles(self) -> list[dict[str, Any]]:
        """``GET /profiles``: ``[{id, name, notes, proxy_id, folder, running, pid, cdp, ...}]``."""
        return list(self._request("GET", "/profiles") or [])

    def running(self) -> list[dict[str, Any]]:
        """``GET /running``: ``[{profile_id, pid, cdp?, uptime_ms}]`` (``cdp`` only for API launches)."""
        return list(self._request("GET", "/running") or [])

    def cdp(self, profile_id: str) -> dict[str, Any] | None:
        """DevTools endpoint of a running, API-launched profile (``None`` if not attachable)."""
        pid = _strip_ref(profile_id)
        for entry in self.running():
            if entry.get("profile_id") == pid and entry.get("cdp"):
                return _cdp_result(pid, entry["cdp"], entry.get("pid"))
        return None

    def start(self, profile_id: str, headless: bool = False) -> dict[str, Any]:
        """Launch a profile with remote debugging and return its DevTools endpoint.

        Returns ``{"port", "http_url", "web_socket_debugger_url", "profile_id", "pid"}``.
        Idempotent when the profile is already running from an API launch.
        """
        pid = _strip_ref(profile_id)
        if not pid:
            raise ShardXNotFoundError("No ShardX profile given.")
        current = self.cdp(pid)
        if current is not None:
            return current
        try:
            data = self._request("POST", f"/profiles/{quote(pid, safe='')}/start",
                                 json_body={"headless": bool(headless)}, timeout=self.start_timeout)
        except ShardXError as exc:
            if "already running" not in str(exc).lower():
                raise
            current = self.cdp(pid)
            if current is not None:
                return current
            raise ShardXConflictError(
                f"ShardX profile {pid} is already running without remote debugging (it was probably opened "
                "from the ShardX launcher window). Close it there, or stop it, then start it again through "
                "ProfilePilot."
            ) from None
        data = data if isinstance(data, dict) else {}
        cdp = data.get("cdp")
        if cdp and cdp.get("http_url"):
            return _cdp_result(pid, cdp, data.get("pid"))
        deadline = time.monotonic() + self.cdp_wait
        while time.monotonic() < deadline:
            current = self.cdp(pid)
            if current is not None:
                return current
            time.sleep(0.25)
        reason = redact_secrets(str(data.get("cdp_error") or "no DevTools endpoint was reported"))
        raise ShardXLaunchError(
            f"ShardX started profile {pid} (pid {data.get('pid')}) but it is not attachable: {reason}. "
            "Stop it and try again."
        )

    def stop(self, profile_id: str) -> bool:
        """``POST /profiles/{id}/stop``. Returns False if the profile was not running."""
        pid = _strip_ref(profile_id)
        data = self._request("POST", f"/profiles/{quote(pid, safe='')}/stop")
        return bool((data or {}).get("stopped"))

    def list_proxies(self) -> list[dict[str, Any]]:
        """``GET /proxies``: ``[{id, name, kind, host, port, country}]`` (credentials never returned)."""
        return list(self._request("GET", "/proxies") or [])

    def add_proxy(self, proxy: str, *, name: str | None = None, country: str | None = None,
                  notes: str | None = None) -> dict[str, Any]:
        """``POST /proxies`` with a proxy string (``scheme://user:pass@host:port`` or
        ``host:port:user:pass``). Returns the launcher's credential-free summary."""
        body: dict[str, Any] = {"proxy": proxy}
        if name:
            body["name"] = name
        if country is not None:
            body["country"] = country
        if notes is not None:
            body["notes"] = notes
        return dict(self._request("POST", "/proxies", json_body=body) or {})

    def resolve(self, ref: str) -> dict[str, Any]:
        """Find a ShardX profile by id, name (case-insensitive) or unique id prefix (>= 3 chars).

        ``ref`` may carry the ``shardx:`` prefix.
        """
        key = _strip_ref(ref)
        if not key:
            raise ShardXNotFoundError("No ShardX profile given.")
        profiles = self.list_profiles()
        for p in profiles:
            if str(p.get("id", "")).lower() == key.lower():
                return p
        by_name = [p for p in profiles if str(p.get("name", "")).casefold() == key.casefold()]
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise AmbiguousError(
                f"'{key}' matches several ShardX profiles: " + ", ".join(f"{p.get('name')} ({p.get('id')})" for p in by_name)
            )
        if len(key) >= 3:
            by_prefix = [p for p in profiles if str(p.get("id", "")).lower().startswith(key.lower())]
            if len(by_prefix) == 1:
                return by_prefix[0]
            if len(by_prefix) > 1:
                raise AmbiguousError(
                    f"'{key}' matches several ShardX profiles: " + ", ".join(f"{p.get('name')} ({p.get('id')})" for p in by_prefix)
                )
        names = ", ".join(str(p.get("name")) for p in profiles[:20]) or "none"
        raise ShardXNotFoundError(f"ShardX profile '{key}' not found. ShardX profiles: {names}.")


def _error_message(resp: httpx.Response) -> str:
    if not resp.content:
        return ""
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()[:300]
    if isinstance(data, dict) and data.get("error") is not None:
        return str(data["error"])[:500]
    return json.dumps(data)[:300]


class AsyncShardXClient:
    """Async facade over :class:`ShardXClient` (each call runs in a worker thread via anyio)."""

    def __init__(self, client: ShardXClient | None = None, **kwargs: Any) -> None:
        self.sync = client if client is not None else ShardXClient(**kwargs)

    @classmethod
    def from_store(cls, store: "Store", **kwargs: Any) -> "AsyncShardXClient":
        return cls(ShardXClient.from_store(store, **kwargs))

    @property
    def base_url(self) -> str:
        return self.sync.base_url

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        import anyio.to_thread

        return await anyio.to_thread.run_sync(lambda: fn(*args))

    async def health(self) -> dict[str, Any]:
        return await self._run(self.sync.health)

    async def is_available(self) -> bool:
        return await self._run(self.sync.is_available)

    async def status(self) -> dict[str, Any]:
        return await self._run(self.sync.status)

    async def list_profiles(self) -> list[dict[str, Any]]:
        return await self._run(self.sync.list_profiles)

    async def running(self) -> list[dict[str, Any]]:
        return await self._run(self.sync.running)

    async def cdp(self, profile_id: str) -> dict[str, Any] | None:
        return await self._run(self.sync.cdp, profile_id)

    async def start(self, profile_id: str, headless: bool = False) -> dict[str, Any]:
        return await self._run(self.sync.start, profile_id, headless)

    async def stop(self, profile_id: str) -> bool:
        return await self._run(self.sync.stop, profile_id)

    async def list_proxies(self) -> list[dict[str, Any]]:
        return await self._run(self.sync.list_proxies)

    async def resolve(self, ref: str) -> dict[str, Any]:
        return await self._run(self.sync.resolve, ref)

    async def aclose(self) -> None:
        self.sync.close()

    async def __aenter__(self) -> "AsyncShardXClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.sync.close()


__all__ = [
    "DEFAULT_BASE_URL", "TOKEN_KEY", "REF_PREFIX", "SPOOFED_ENGINE_NOTE",
    "ShardXClient", "AsyncShardXClient", "SettingsTokenMinter",
    "ShardXError", "ShardXUnavailableError", "ShardXAuthError", "ShardXNotFoundError",
    "ShardXConflictError", "ShardXLaunchError",
    "mint_token", "read_settings", "default_settings_path", "base_url_from_settings",
    "keyring_token_provider", "normalize_token", "save_token", "delete_token", "redact_secrets",
]
