"""OAuth 2.1 for ``profilepilot serve --http --auth oauth`` (ChatGPT, Claude and other remote clients).

ProfilePilot runs its own tiny, **single-user** authorization server next to the MCP endpoint:

* discovery: OAuth protected resource metadata (RFC 9728) at the root *and* path-inserted
  ``/.well-known/oauth-protected-resource[/mcp]``, authorization server metadata (RFC 8414) at
  ``/.well-known/oauth-authorization-server`` with ``authorization_response_iss_parameter_supported``
  (RFC 9207: ``iss`` is added to every redirect back to the client, success or error);
* client registration: dynamic client registration (RFC 7591) and client ID metadata documents
  (an ``https://`` URL as ``client_id``, fetched with SSRF guards; ChatGPT prefers them);
* authorization code + PKCE ``S256`` only; refresh tokens (rotated, reuse revokes the grant) and
  revocation (RFC 7009); RFC 8707 ``resource`` indicators bind every token to this server's URL;
* access tokens live 1 hour, refresh tokens 30 days. Tokens and codes are 32 random bytes and only
  their SHA-256 hashes are stored, in ``<data root>/oauth.json``.

**Consent.** ``/authorize`` sends the browser to ``/oauth/consent``: a server-rendered page (no
external assets, strict CSP, ``frame-ancestors 'none'``, CSRF token, every value HTML-escaped) that
shows the client and where it will be redirected, and asks for the **pairing code**: 8 characters
such as ``ABCD-2345`` from an alphabet without look-alikes. The code is shown only to the user, in
the ``serve`` / ``connect`` terminal, ProfilePilot Manager and ``profilepilot connect status``; it
rotates after every successful use, and at most 5 wrong codes are accepted per 10 minutes. That is
what makes a public tunnel URL safe: only someone who can see the user's screen can approve.

Integration (see docs/design/WIRE-IN.md, "ChatGPT")::

    setup = build_oauth(store, public_base_url(host, port, public_hosts), mcp_path="/mcp")
    server = create_server(..., auth_server_provider=setup.provider, auth=setup.settings)
    app = setup.wrap(server.streamable_http_app(...))
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from hmac import compare_digest
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence
from urllib.parse import parse_qs, urlsplit

import anyio
import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl, ValidationError

from ..errors import ProfilePilotError
from ..jsonio import lock_for, read_json, write_json

log = logging.getLogger("profilepilot.server.oauth")

# ---------------------------------------------------------------------- constants

OAUTH_FILE = "oauth.json"
PAIRING_KEY = "oauth:pairing-code"
SCOPE = "profilepilot"
CONSENT_PATH = "/oauth/consent"
PRM_ROOT_PATH = "/.well-known/oauth-protected-resource"
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
REFRESH_GRACE = 60
"""Seconds during which a just-rotated refresh token still works (a response lost on a flaky tunnel
must not force the user to pair again). Reuse after that revokes the whole connection."""
PENDING_TTL = 600
ATTEMPT_LIMIT = 5
ATTEMPT_WINDOW = 600
MAX_PENDING = 64
MAX_CLIENTS = 100
MAX_REGISTRATIONS_PER_HOUR = 30
MAX_REDIRECT_URIS = 10
MAX_FORM_BYTES = 16 * 1024

CHATGPT_REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
"""ChatGPT's stable redirect URI (used because this server supports RFC 9207 ``iss``)."""
KNOWN_REDIRECT_HOSTS = {
    "chatgpt.com": "ChatGPT",
    "chat.openai.com": "ChatGPT",
    "claude.ai": "Claude",
    "claude.com": "Claude",
}
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")
_FORBIDDEN_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "filesystem", "ftp", "ws", "wss",
                      "chrome", "chrome-extension", "view-source", "intent"}

PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
"""31 characters without look-alikes (no 0/O, 1/I/L)."""
PAIRING_LENGTH = 8

CIMD_MAX_BYTES = 32 * 1024
CIMD_TIMEOUT = 5.0
CIMD_CACHE_TTL = 3600
CIMD_NEGATIVE_TTL = 60
CIMD_FETCHES_PER_MINUTE = 10

Clock = Callable[[], float]
CimdFetcher = Callable[[str], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------- pairing code


def new_pairing_code() -> str:
    """A fresh pairing code such as ``ABCD-2345`` (about 40 bits)."""
    raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_LENGTH))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_pairing_code(text: str | None) -> str:
    """Upper-case, without separators or spaces (``abcd 2345`` == ``ABCD-2345``)."""
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()[:32]


def pairing_code(store: Any, *, rotate: bool = False) -> str:
    """The current pairing code (created on first use; ``rotate`` makes a new one).

    It lives in the secret store so that the server, ``profilepilot connect status`` and the
    Manager (separate processes) all show the same code."""
    current = None if rotate else store.secrets.get(PAIRING_KEY)
    if current and len(normalize_pairing_code(current)) == PAIRING_LENGTH:
        return current
    value = new_pairing_code()
    store.secrets.set(PAIRING_KEY, value)
    return value


def rotate_pairing_code(store: Any) -> str:
    return pairing_code(store, rotate=True)


# ---------------------------------------------------------------------- URLs


def public_base_url(host: str = "127.0.0.1", port: int = 8931, public_hosts: Sequence[str] = ()) -> str:
    """The issuer (origin) the OAuth server announces: ``https://<first public host>``, or the
    loopback URL when there is none (local clients only: ChatGPT cannot reach it)."""
    for raw in public_hosts:
        text = (raw or "").strip()
        if "://" in text:
            text = text.split("://", 1)[1]
        text = text.split("/", 1)[0].strip().lower()
        if text:
            return f"https://{text}"
    local = "127.0.0.1" if host in ("0.0.0.0", "::", "") else (f"[{host}]" if ":" in host else host)
    return f"http://{local}:{int(port)}"


def _split_public_url(public_url: str, mcp_path: str) -> tuple[str, str]:
    """``(issuer, resource)`` for a public URL (origin or full MCP URL)."""
    text = (public_url or "").strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ProfilePilotError(f"Invalid public URL {public_url!r}: use e.g. https://my-tunnel.example.com")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ProfilePilotError("The public URL must not contain credentials, a query or a fragment.")
    host = parts.hostname.lower()
    if parts.scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise ProfilePilotError("OAuth needs an https:// public URL (plain http is only allowed for localhost).")
    netloc = f"[{host}]" if ":" in host else host
    if parts.port and not (parts.scheme == "https" and parts.port == 443) and not (
        parts.scheme == "http" and parts.port == 80
    ):
        netloc += f":{parts.port}"
    issuer = f"{parts.scheme}://{netloc}"
    path = "/" + (mcp_path or "/mcp").strip().strip("/")
    return issuer, issuer + path


def _canonical(url: str) -> str:
    """Comparable form of a resource URL (case-insensitive scheme/host, no trailing slash)."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc or parts.fragment:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _redirect_label(uri: str) -> tuple[str, str | None]:
    """``(host or scheme shown to the user, known product name or None)``."""
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    if parts.scheme in ("http", "https") and host:
        known = KNOWN_REDIRECT_HOSTS.get(host)
        if known and parts.scheme != "https":
            known = None
        if host in _LOOPBACK_HOSTS:
            known = "an app on this computer"
        return host, known
    return f"{parts.scheme}: (an app on this computer)", None


def validate_redirect_uri(uri: str) -> str:
    """Raise :class:`ValueError` unless ``uri`` is an acceptable redirect URI.

    https anywhere; http only to loopback (native apps); private-use schemes such as
    ``cursor://`` for desktop apps; never ``javascript:``/``data:``/``file:`` & co, never a fragment.
    """
    text = str(uri)
    if len(text) > 2000:
        raise ValueError("redirect URI too long")
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if not scheme or not re.fullmatch(r"[a-z][a-z0-9+.\-]*", scheme):
        raise ValueError(f"redirect URI {text!r} has no valid scheme")
    if parts.fragment or "#" in text:
        raise ValueError("redirect URIs must not contain a fragment")
    if scheme in _FORBIDDEN_SCHEMES:
        raise ValueError(f"redirect URI scheme {scheme!r} is not allowed")
    if scheme in ("http", "https"):
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError(f"redirect URI {text!r} has no host")
        if parts.username or parts.password:
            raise ValueError("redirect URIs must not contain credentials")
        if scheme == "http" and host not in _LOOPBACK_HOSTS:
            raise ValueError("http redirect URIs are only allowed for localhost (use https)")
    return text


# ---------------------------------------------------------------------- persistent store


class OAuthStore:
    """``<root>/oauth.json``: registered clients, grants and the **hashes** of codes and tokens.

    Every change is a locked read-modify-write (safe across processes); lookups read a cached copy
    that is refreshed when the file changes on disk.
    """

    VERSION = 1

    def __init__(self, root: Path | str, *, clock: Clock = time.time) -> None:
        self.path = Path(root) / OAUTH_FILE
        self.clock = clock
        self._cache: tuple[tuple[int, int], dict[str, Any]] | None = None

    # -- low level

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": OAuthStore.VERSION, "clients": {}, "grants": {}, "codes": {}, "used_codes": {},
                "access": {}, "refresh": {}, "used_refresh": {}}

    def _normalise(self, data: Any) -> dict[str, Any]:
        base = self._empty()
        if isinstance(data, dict):
            for key in base:
                if key != "version" and isinstance(data.get(key), dict):
                    base[key] = data[key]
        return base

    def read(self) -> dict[str, Any]:
        """Current contents (treat as read-only)."""
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            return self._empty()
        if self._cache is not None and self._cache[0] == sig:
            return self._cache[1]
        data = self._normalise(read_json(self.path, {}))
        self._cache = (sig, data)
        return data

    def mutate(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        """Apply ``fn(data)`` under the file lock and save (expired entries are pruned)."""
        with lock_for(self.path):
            data = self._normalise(read_json(self.path, {}))
            result = fn(data)
            self._prune(data)
            write_json(self.path, data)
            self._cache = None
        return result

    def _prune(self, data: dict[str, Any]) -> None:
        now = self.clock()
        for section in ("codes", "used_codes", "access", "refresh", "used_refresh"):
            items = data[section]
            for key in [k for k, v in items.items() if float(v.get("expires_at") or 0) < now]:
                del items[key]
        # a grant ends when its last refresh token (or, without one, access token) has expired
        live = {v.get("grant_id") for v in data["access"].values()} | {
            v.get("grant_id") for v in data["refresh"].values()}
        for gid in [g for g in data["grants"] if g not in live]:
            del data["grants"][gid]

    # -- summaries (used by `connect status`, the Manager and the wizard)

    def grants(self) -> list[dict[str, Any]]:
        """Active connections: ``client_id``, ``client_name``, ``created_at``, ``last_used_at``."""
        data = self.read()
        now = self.clock()
        live = {v.get("grant_id") for v in data["refresh"].values() if float(v.get("expires_at") or 0) >= now}
        live |= {v.get("grant_id") for v in data["access"].values() if float(v.get("expires_at") or 0) >= now}
        out = []
        for gid, grant in data["grants"].items():
            if gid in live:
                out.append({"grant_id": gid, **{k: grant.get(k) for k in
                                                ("client_id", "client_name", "created_at", "last_used_at")}})
        return sorted(out, key=lambda g: g.get("created_at") or 0)

    def revoke_all(self) -> int:
        """Disconnect every client (all tokens and grants). Returns the number of grants revoked."""

        def apply(data: dict[str, Any]) -> int:
            count = len(data["grants"])
            for section in ("grants", "codes", "access", "refresh"):
                data[section] = {}
            return count

        return int(self.mutate(apply))

    def forget_clients(self) -> None:
        """Remove every registered client too (clients must register again)."""
        self.mutate(lambda data: data.update(self._empty()))


# ---------------------------------------------------------------------- clients and token models


def loopback_redirect_matches(given: str, registered: Iterable[str]) -> bool:
    """RFC 8252 7.3: a loopback redirect (``http://127.0.0.1``, ``http://[::1]`` and, for Claude Code,
    ``http://localhost``) matches a registered one whatever its port; scheme, host, path and query
    must still be identical."""
    try:
        g = urlsplit(given)
    except ValueError:
        return False
    if g.scheme != "http" or (g.hostname or "").lower() not in _LOOPBACK_HOSTS or g.fragment:
        return False
    for item in registered:
        try:
            r = urlsplit(str(item))
        except ValueError:
            continue
        if (r.scheme == "http" and (r.hostname or "").lower() == (g.hostname or "").lower()
                and (r.path or "/") == (g.path or "/") and r.query == g.query):
            return True
    return False


class PairingClient(OAuthClientInformationFull):
    """A registered client whose loopback redirect URIs match on any port (native apps such as
    Claude Code listen on a random port per sign-in)."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is not None and self.redirect_uris and loopback_redirect_matches(
            str(redirect_uri), [str(u) for u in self.redirect_uris]
        ):
            return redirect_uri
        return super().validate_redirect_uri(redirect_uri)


# ---------------------------------------------------------------------- token models


class StoredCode(AuthorizationCode):
    grant_id: str
    client_name: str | None = None


class StoredRefreshToken(RefreshToken):
    grant_id: str


class StoredAccessToken(AccessToken):
    grant_id: str


@dataclass
class PendingAuthorization:
    """An ``/authorize`` request waiting for the user's decision on the consent page."""

    request_id: str
    client_id: str
    client_name: str | None
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    state: str | None
    scopes: list[str]
    code_challenge: str
    resource: str
    csrf: str
    created_at: float


@dataclass
class _RateLimiter:
    limit: int
    window: float
    events: deque[float] = field(default_factory=deque)

    def _trim(self, now: float) -> None:
        while self.events and self.events[0] <= now - self.window:
            self.events.popleft()

    def blocked_for(self, now: float) -> float:
        """Seconds until another attempt is allowed (0 = allowed now)."""
        self._trim(now)
        if len(self.events) < self.limit:
            return 0.0
        return max(0.0, self.events[0] + self.window - now)

    def hit(self, now: float) -> None:
        self._trim(now)
        self.events.append(now)


# ---------------------------------------------------------------------- provider


class PairingOAuthProvider(OAuthAuthorizationServerProvider[StoredCode, StoredRefreshToken, StoredAccessToken]):
    """Single-user authorization server: approval = the pairing code on the consent page."""

    def __init__(
        self,
        store: Any,
        *,
        issuer: str,
        resource: str,
        cimd: bool = True,
        clock: Clock = time.time,
        cimd_fetcher: CimdFetcher | None = None,
    ) -> None:
        self.store = store
        self.issuer = issuer
        self.resource = resource
        self.cimd = cimd
        self.clock = clock
        self.db = OAuthStore(store.root, clock=clock)
        self._fetch_cimd = cimd_fetcher or fetch_client_metadata_document
        self._pending: dict[str, PendingAuthorization] = {}
        self._attempts = _RateLimiter(ATTEMPT_LIMIT, ATTEMPT_WINDOW)
        self._registrations = _RateLimiter(MAX_REGISTRATIONS_PER_HOUR, 3600)
        self._cimd_fetches = _RateLimiter(CIMD_FETCHES_PER_MINUTE, 60)
        self._cimd_cache: dict[str, tuple[float, OAuthClientInformationFull | None]] = {}
        self._last_touch: dict[str, float] = {}

    # -- helpers

    async def _mutate(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        return await anyio.to_thread.run_sync(self.db.mutate, fn)

    def pairing_code(self) -> str:
        return pairing_code(self.store)

    def _resource_ok(self, resource: str | None) -> bool:
        if not resource:
            return True
        wanted = _canonical(resource)
        return bool(wanted) and wanted in (_canonical(self.resource), _canonical(self.issuer))

    # -- clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        if not client_id or len(client_id) > 2048:
            return None
        if client_id.startswith("https://"):
            return await self._cimd_client(client_id) if self.cimd else None
        record = self.db.read()["clients"].get(client_id)
        if not isinstance(record, dict):
            return None
        try:
            return PairingClient.model_validate(record.get("info") or {})
        except ValidationError:
            log.warning("ignoring an unreadable OAuth client record")
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        now = self.clock()
        if self._registrations.blocked_for(now):
            raise RegistrationError("invalid_client_metadata", "Too many registrations; try again later.")
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or len(uris) > MAX_REDIRECT_URIS:
            raise RegistrationError("invalid_redirect_uri",
                                    f"Register between 1 and {MAX_REDIRECT_URIS} redirect URIs.")
        for uri in uris:
            try:
                validate_redirect_uri(uri)
            except ValueError as exc:
                raise RegistrationError("invalid_redirect_uri", str(exc)) from None
        if client_info.client_name and len(client_info.client_name) > 200:
            raise RegistrationError("invalid_client_metadata", "client_name is too long.")
        self._registrations.hit(now)
        info = client_info.model_dump(mode="json", exclude_none=True)

        def apply(data: dict[str, Any]) -> None:
            clients = data["clients"]
            if len(clients) >= MAX_CLIENTS:
                in_use = {g.get("client_id") for g in data["grants"].values()}
                idle = sorted((c for c in clients if c not in in_use), key=lambda c: clients[c].get("created_at", 0))
                if not idle:
                    raise RegistrationError("invalid_client_metadata", "Too many connected clients.")
                for cid in idle[: len(clients) - MAX_CLIENTS + 1]:
                    del clients[cid]
            clients[client_info.client_id] = {"info": info, "created_at": now, "kind": "dcr"}

        await self._mutate(apply)
        log.info("OAuth: registered client %r", (client_info.client_name or "unnamed")[:80])

    async def _cimd_client(self, url: str) -> OAuthClientInformationFull | None:
        now = self.clock()
        cached = self._cimd_cache.get(url)
        if cached is not None:
            ttl = CIMD_CACHE_TTL if cached[1] is not None else CIMD_NEGATIVE_TTL
            if now - cached[0] < ttl:
                return cached[1]
        if self._cimd_fetches.blocked_for(now):
            return cached[1] if cached else None
        self._cimd_fetches.hit(now)
        client: OAuthClientInformationFull | None = None
        try:
            check_cimd_url(url)
            document = await self._fetch_cimd(url)
            client = client_from_metadata_document(url, document)
        except (ValueError, ProfilePilotError) as exc:
            log.warning("OAuth: client metadata document rejected: %s", str(exc)[:200])
        except Exception as exc:  # network errors and the like
            log.warning("OAuth: could not fetch a client metadata document: %s", type(exc).__name__)
        if len(self._cimd_cache) > 256:
            self._cimd_cache.clear()
        self._cimd_cache[url] = (now, client)
        return client

    # -- authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self._resource_ok(params.resource):
            raise AuthorizeError("invalid_target", "This authorization server only issues tokens for "
                                                   f"{self.resource}.")
        # One scope exists. Others (e.g. "openid") are ignored rather than refused (RFC 6749 3.3: the
        # server may grant fewer scopes); the token response tells the client what it got.
        scopes = [SCOPE]
        now = self.clock()
        self._expire_pending(now)
        if len(self._pending) >= MAX_PENDING:
            oldest = min(self._pending.values(), key=lambda p: p.created_at)
            self._pending.pop(oldest.request_id, None)
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = PendingAuthorization(
            request_id=request_id,
            client_id=client.client_id,
            client_name=_display_name(client.client_name),
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            state=params.state,
            scopes=scopes,
            code_challenge=params.code_challenge,
            resource=self.resource,
            csrf=secrets.token_urlsafe(24),
            created_at=now,
        )
        return f"{self.issuer}{CONSENT_PATH}?request={request_id}"

    def _expire_pending(self, now: float) -> None:
        for key in [k for k, p in self._pending.items() if now - p.created_at > PENDING_TTL]:
            del self._pending[key]

    def pending(self, request_id: str | None) -> PendingAuthorization | None:
        if not request_id:
            return None
        self._expire_pending(self.clock())
        return self._pending.get(request_id)

    def attempts_blocked_for(self) -> float:
        return self._attempts.blocked_for(self.clock())

    def attempts_left(self) -> int:
        now = self.clock()
        self._attempts.blocked_for(now)
        return max(0, ATTEMPT_LIMIT - len(self._attempts.events))

    async def approve(self, pending: PendingAuthorization, code_text: str) -> str | None:
        """Check the pairing code; on success return the redirect URL with a new authorization
        code (and rotate the pairing code). ``None`` = wrong code (counted)."""
        now = self.clock()
        expected = normalize_pairing_code(await anyio.to_thread.run_sync(self.pairing_code))
        given = normalize_pairing_code(code_text)
        if not given or not compare_digest(given.encode(), expected.encode()):
            self._attempts.hit(now)
            log.warning("OAuth: wrong pairing code entered on the consent page")
            return None
        self._pending.pop(pending.request_id, None)
        code = "ppac_" + secrets.token_urlsafe(32)
        grant_id = secrets.token_urlsafe(12)
        record = {
            "client_id": pending.client_id,
            "client_name": pending.client_name,
            "grant_id": grant_id,
            "scopes": pending.scopes,
            "expires_at": now + CODE_TTL,
            "code_challenge": pending.code_challenge,
            "redirect_uri": pending.redirect_uri,
            "redirect_uri_provided_explicitly": pending.redirect_uri_provided_explicitly,
            "resource": pending.resource,
        }
        await self._mutate(lambda data: data["codes"].__setitem__(_hash(code), record))
        await anyio.to_thread.run_sync(lambda: rotate_pairing_code(self.store))
        log.info("OAuth: the user approved %r", (pending.client_name or pending.client_id)[:80])
        return construct_redirect_uri(pending.redirect_uri, code=code, state=pending.state, iss=self.issuer)

    def deny(self, pending: PendingAuthorization) -> str:
        self._pending.pop(pending.request_id, None)
        return construct_redirect_uri(
            pending.redirect_uri, error="access_denied", error_description="The user denied the request.",
            state=pending.state, iss=self.issuer,
        )

    # -- codes and tokens

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> StoredCode | None:
        """Codes are single-use: loading consumes the code (a failed exchange burns it too).
        Presenting a consumed code again revokes everything issued from it (RFC 6749 10.5)."""
        key = _hash(authorization_code or "")
        now = self.clock()
        snapshot = self.db.read()
        if key not in snapshot["codes"] and key not in snapshot["used_codes"]:
            return None  # unknown code: no locked write (keeps /token spam cheap)

        def apply(data: dict[str, Any]) -> dict[str, Any] | None:
            record = data["codes"].pop(key, None)
            if record is None:
                used = data["used_codes"].get(key)
                if used:
                    _revoke_grant(data, used.get("grant_id"))
                    log.warning("OAuth: an authorization code was replayed; its tokens were revoked")
                return None
            data["used_codes"][key] = {"grant_id": record.get("grant_id"), "expires_at": now + 2 * CODE_TTL}
            return record

        record = await self._mutate(apply)
        if record is None or float(record.get("expires_at") or 0) < now:
            return None
        return StoredCode(
            code=authorization_code,
            scopes=list(record.get("scopes") or [SCOPE]),
            expires_at=float(record["expires_at"]),
            client_id=str(record.get("client_id")),
            code_challenge=str(record.get("code_challenge")),
            redirect_uri=AnyUrl(str(record.get("redirect_uri"))),
            redirect_uri_provided_explicitly=bool(record.get("redirect_uri_provided_explicitly")),
            resource=record.get("resource"),
            subject="owner",
            grant_id=str(record.get("grant_id")),
            client_name=record.get("client_name"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: StoredCode
    ) -> OAuthToken:
        if authorization_code.client_id != client.client_id:
            raise TokenError("invalid_grant", "authorization code does not exist")
        now = self.clock()
        grant = {
            "client_id": client.client_id,
            "client_name": authorization_code.client_name or _display_name(client.client_name),
            "scopes": authorization_code.scopes,
            "resource": self.resource,
            "created_at": now,
            "last_used_at": now,
        }
        with_refresh = "refresh_token" in (client.grant_types or [])
        tokens = _new_tokens(with_refresh)

        def apply(data: dict[str, Any]) -> None:
            data["grants"][authorization_code.grant_id] = grant
            _store_tokens(data, tokens, client.client_id, authorization_code.grant_id, authorization_code.scopes,
                          self.resource, now)

        await self._mutate(apply)
        return _token_response(tokens, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> StoredRefreshToken | None:
        key = _hash(refresh_token or "")
        data = self.db.read()
        now = self.clock()
        record = data["refresh"].get(key)
        if record is None:
            used = data["used_refresh"].get(key)
            if not used:
                return None
            same_client = used.get("client_id") == client.client_id
            if _within_grace(used, now) and same_client and used.get("grant_id") in data["grants"]:
                record = used
            else:  # a rotated refresh token came back: treat the connection as compromised
                await self._mutate(lambda d: _revoke_grant(d, used.get("grant_id")))
                log.warning("OAuth: a rotated refresh token was reused; the connection was revoked")
                return None
        if record.get("client_id") != client.client_id or float(record.get("expires_at") or 0) < now:
            return None
        return StoredRefreshToken(
            token=refresh_token, client_id=client.client_id, scopes=list(record.get("scopes") or [SCOPE]),
            expires_at=int(record["expires_at"]), resource=record.get("resource"), subject="owner",
            grant_id=str(record.get("grant_id")),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: StoredRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        key = _hash(refresh_token.token)
        now = self.clock()
        tokens = _new_tokens(True)
        granted = [s for s in (scopes or refresh_token.scopes) if s in refresh_token.scopes] or refresh_token.scopes

        def apply(data: dict[str, Any]) -> bool:
            record = data["refresh"].pop(key, None)
            if record is None:
                used = data["used_refresh"].get(key)
                if not (used and _within_grace(used, now)):
                    return False
            else:
                data["used_refresh"][key] = {**record, "rotated_at": now, "expires_at": now + REFRESH_TTL}
            grant = data["grants"].get(refresh_token.grant_id)
            if grant is None:
                return False
            grant["last_used_at"] = now
            _store_tokens(data, tokens, client.client_id, refresh_token.grant_id, granted, self.resource, now)
            return True

        if not await self._mutate(apply):
            raise TokenError("invalid_grant", "refresh token does not exist")
        return _token_response(tokens, granted)

    async def load_access_token(self, token: str) -> StoredAccessToken | None:
        if not token or not token.startswith("ppat_"):
            return None
        key = _hash(token)
        record = self.db.read()["access"].get(key)
        now = self.clock()
        if record is None or float(record.get("expires_at") or 0) < now:
            return None
        resource = record.get("resource")
        if resource and _canonical(resource) != _canonical(self.resource):
            return None  # issued for another URL (e.g. an earlier tunnel address)
        grant_id = str(record.get("grant_id"))
        if now - self._last_touch.get(grant_id, 0.0) > 300:
            self._last_touch[grant_id] = now

            def touch(data: dict[str, Any]) -> None:
                grant = data["grants"].get(grant_id)
                if grant is not None:
                    grant["last_used_at"] = now

            try:
                await self._mutate(touch)
            except Exception as exc:  # never fail a request over bookkeeping
                log.debug("could not record OAuth token use: %s", exc)
        return StoredAccessToken(
            token=token, client_id=str(record.get("client_id")), scopes=list(record.get("scopes") or [SCOPE]),
            expires_at=int(record["expires_at"]), resource=resource or self.resource, subject="owner",
            grant_id=grant_id,
        )

    async def revoke_token(self, token: StoredAccessToken | StoredRefreshToken) -> None:
        grant_id = getattr(token, "grant_id", None)
        key = _hash(token.token)

        def apply(data: dict[str, Any]) -> None:
            gid = grant_id
            for section in ("access", "refresh"):
                record = data[section].get(key)
                if record is not None:
                    gid = gid or record.get("grant_id")
            if gid:
                _revoke_grant(data, gid)
            else:
                data["access"].pop(key, None)
                data["refresh"].pop(key, None)

        await self._mutate(apply)
        log.info("OAuth: a connection was revoked")


def _display_name(name: str | None) -> str | None:
    if not name:
        return None
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", str(name)).strip()
    return clean[:80] or None


def _within_grace(used: dict[str, Any], now: float) -> bool:
    rotated = used.get("rotated_at")
    return rotated is not None and 0 <= now - float(rotated) <= REFRESH_GRACE


def _new_tokens(with_refresh: bool) -> dict[str, str | None]:
    return {
        "access": "ppat_" + secrets.token_urlsafe(32),
        "refresh": ("pprt_" + secrets.token_urlsafe(32)) if with_refresh else None,
    }


def _store_tokens(data: dict[str, Any], tokens: dict[str, str | None], client_id: str, grant_id: str,
                  scopes: list[str], resource: str, now: float) -> None:
    base = {"client_id": client_id, "grant_id": grant_id, "scopes": scopes, "resource": resource}
    data["access"][_hash(str(tokens["access"]))] = {**base, "expires_at": now + ACCESS_TTL}
    if tokens.get("refresh"):
        data["refresh"][_hash(str(tokens["refresh"]))] = {**base, "expires_at": now + REFRESH_TTL}


def _token_response(tokens: dict[str, str | None], scopes: list[str]) -> OAuthToken:
    return OAuthToken(access_token=str(tokens["access"]), token_type="Bearer", expires_in=ACCESS_TTL,
                      scope=" ".join(scopes), refresh_token=tokens.get("refresh"))


def _revoke_grant(data: dict[str, Any], grant_id: Any) -> None:
    if not grant_id:
        return
    data["grants"].pop(grant_id, None)
    for section in ("access", "refresh"):
        for key in [k for k, v in data[section].items() if v.get("grant_id") == grant_id]:
            del data[section][key]


# ---------------------------------------------------------------------- client ID metadata documents


def check_cimd_url(url: str) -> None:
    """Shape rules for a client ID metadata document URL (https, default port, a path, no
    credentials/query/fragment/dot segments, a public DNS name or address)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("client_id URLs must use https")
    if parts.username or parts.password or parts.fragment or parts.query:
        raise ValueError("client_id URLs must not contain credentials, a query or a fragment")
    if parts.port not in (None, 443):
        raise ValueError("client_id URLs must use the default https port")
    if not parts.path or parts.path == "/" or "/./" in parts.path + "/" or "/../" in parts.path + "/":
        raise ValueError("client_id URLs need a path without dot segments")
    host = parts.hostname.lower()
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None:
        if not address.is_global:
            raise ValueError("client_id URLs must not point to a local or private address")
    elif host in _LOOPBACK_HOSTS or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")) \
            or "." not in host:
        raise ValueError("client_id URLs must use a public host name")


async def _resolve_public(host: str, port: int) -> None:
    """Every address of ``host`` must be public (blind SSRF guard for the metadata fetch)."""
    from ..safety import is_private_address

    infos = await anyio.to_thread.run_sync(lambda: socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP))
    if not infos:
        raise ValueError("client_id host does not resolve")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if is_private_address(addr):
            raise ValueError("client_id host resolves to a local or private address")


async def fetch_client_metadata_document(url: str) -> dict[str, Any]:
    """Fetch a client ID metadata document: https only, public addresses only, no redirects,
    at most 32 KB, 5 s timeout, JSON object."""
    import httpx

    check_cimd_url(url)
    parts = urlsplit(url)
    await _resolve_public(parts.hostname or "", 443)
    async with httpx.AsyncClient(follow_redirects=False, timeout=CIMD_TIMEOUT, trust_env=False,
                                 headers={"Accept": "application/json", "User-Agent": "ProfilePilot-OAuth"}) as http:
        async with http.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError(f"client metadata document answered HTTP {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > CIMD_MAX_BYTES:
                    raise ValueError("client metadata document is too large")
    document = json.loads(bytes(body).decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("client metadata document is not a JSON object")
    return document


def client_from_metadata_document(url: str, document: dict[str, Any]) -> OAuthClientInformationFull:
    """Validate a fetched document and turn it into a public (``none``) client.

    ChatGPT's document prefers ``private_key_jwt`` but also lists ``none``; with PKCE and exact
    redirect URIs a public client is safe, so ``none`` is used whenever the document allows it."""
    if document.get("client_id") != url:
        raise ValueError("client_id in the metadata document does not match its URL")
    uris = document.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECT_URIS:
        raise ValueError("the metadata document needs 1-10 redirect_uris")
    clean = [validate_redirect_uri(str(u)) for u in uris]
    preferred = document.get("token_endpoint_auth_method")
    supported = document.get("token_endpoint_auth_methods_supported")
    methods = set(supported) if isinstance(supported, list) else set()
    if preferred:
        methods.add(str(preferred))
    if not methods:
        methods = {"none"}
    if "none" not in methods:
        raise ValueError("ProfilePilot accepts client metadata documents only for public clients "
                         "(token_endpoint_auth_method 'none')")
    grant_types = document.get("grant_types") or ["authorization_code", "refresh_token"]
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
        raise ValueError("the client must use the authorization_code grant")
    response_types = document.get("response_types") or ["code"]
    if not isinstance(response_types, list) or "code" not in response_types:
        raise ValueError("the client must use response_type 'code'")
    name = document.get("client_name")
    requested = document.get("scope") if isinstance(document.get("scope"), str) else ""
    scope = " ".join(dict.fromkeys([*requested.split(), SCOPE]))
    return PairingClient(
        client_id=url,
        redirect_uris=[AnyUrl(u) for u in clean],
        token_endpoint_auth_method="none",
        grant_types=[g for g in grant_types if g in ("authorization_code", "refresh_token")],
        response_types=["code"],
        client_name=_display_name(name if isinstance(name, str) else None) or urlsplit(url).hostname,
        scope=scope,
    )


# ---------------------------------------------------------------------- consent page


_CONSENT_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#16181d;--muted:#5d6472;--line:#e3e6eb;--accent:#3b5bfd;--accent-text:#fff;
--warn-bg:#fff6e5;--warn-line:#f2c46d;--warn-text:#6b4500;--err-bg:#fdecec;--err-line:#f1a3a3;--err-text:#8a1c1c;
--ok:#1f8a4c;--focus:#3b5bfd66}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--text:#eceef2;--muted:#a1a8b5;--line:#2a2f3a;
--accent:#6f8bff;--accent-text:#0b0d12;--warn-bg:#2b2210;--warn-line:#7a5a1c;--warn-text:#f3d08f;--err-bg:#2c1414;
--err-line:#7d2b2b;--err-text:#f4b4b4;--ok:#4cc483;--focus:#6f8bff77}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,
"Helvetica Neue",Arial,sans-serif}
main{max-width:460px;margin:48px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px;
box-shadow:0 1px 2px #0000000d,0 8px 24px #0000000f}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;color:var(--muted);font-size:13px;letter-spacing:.02em}
.mark{width:22px;height:22px;border-radius:7px;background:linear-gradient(135deg,var(--accent),#9b6bff);
position:relative;flex:none}
.mark:after{content:"";position:absolute;inset:6px;border-radius:3px;border:2px solid #fff}
h1{font-size:20px;line-height:1.3;margin:18px 0 6px}
p{margin:0 0 14px}
.lead{color:var(--muted)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;margin:18px 0;padding:14px;border:1px solid var(--line);
border-radius:10px}
dt{color:var(--muted);font-size:13px}
dd{margin:0;min-width:0;overflow-wrap:anywhere}
.uri{font:12px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);margin-top:2px}
.muted{color:var(--muted);font-size:13px}
.badge{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;background:#1f8a4c1a;color:var(--ok);
margin-left:6px}
.note{border:1px solid var(--warn-line);background:var(--warn-bg);color:var(--warn-text);border-radius:10px;
padding:10px 12px;font-size:14px;margin:0 0 14px}
.error{border-color:var(--err-line);background:var(--err-bg);color:var(--err-text)}
label{display:block;font-weight:600;margin:6px 0 6px}
input[type=text]{width:100%;font:600 22px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:.12em;
text-transform:uppercase;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--bg);
color:var(--text)}
input[type=text]:focus{outline:3px solid var(--focus);outline-offset:1px;border-color:var(--accent)}
.help{font-size:13px;color:var(--muted);margin:8px 0 18px}
code{font:12.5px ui-monospace,SFMono-Regular,Consolas,monospace;background:var(--bg);border:1px solid var(--line);
border-radius:5px;padding:0 4px}
.actions{display:flex;gap:10px;justify-content:flex-end}
.approve{order:2}.deny{order:1}
button{font:600 15px/1 inherit;font-family:inherit;padding:11px 18px;border-radius:10px;border:1px solid var(--line);
background:var(--card);color:var(--text);cursor:pointer}
button:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
button.approve{background:var(--accent);border-color:var(--accent);color:var(--accent-text)}
.foot{font-size:12.5px;color:var(--muted);margin:18px 0 0}
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_consent_page(
    pending: PendingAuthorization | None,
    *,
    nonce: str,
    error: str | None = None,
    message: str | None = None,
) -> str:
    """The consent page (or an error page when ``pending`` is None). Every dynamic value is escaped."""
    head = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex">'
        f"<title>Connect to ProfilePilot</title><style nonce=\"{_esc(nonce)}\">{_CONSENT_CSS}</style></head><body>"
        '<main><div class="card"><div class="brand"><span class="mark" aria-hidden="true"></span>'
        "<span>ProfilePilot</span></div>"
    )
    tail = "</div></main></body></html>"
    if pending is None:
        body = f"<h1>{_esc(message or 'This sign-in link is no longer valid')}</h1>"
        body += ('<p class="lead">Go back to the app you were connecting (for example ChatGPT) and start the '
                 "connection again.</p>")
        if error:
            body += f'<p class="note error" role="alert">{_esc(error)}</p>'
        return head + body + tail

    host, known = _redirect_label(pending.redirect_uri)
    name = pending.client_name or "An app"
    badge = f'<span class="badge">{_esc(known)}</span>' if known else ""
    parts = [
        f"<h1>Allow {_esc(name)} to use ProfilePilot?</h1>",
        '<p class="lead">It will be able to start and control the browser profiles on this computer, '
        "including their saved logins, cookies and proxies.</p>",
        "<dl>",
        f"<dt>App</dt><dd>{_esc(name)} <span class=\"muted\">(the name the app gave itself)</span></dd>",
        f"<dt>Returns to</dt><dd><strong>{_esc(host)}</strong>{badge}"
        f'<div class="uri">{_esc(pending.redirect_uri)}</div></dd>',
        "</dl>",
    ]
    if not known:
        parts.append('<p class="note" role="note">This app is not ChatGPT or Claude. Only approve if you '
                     "started this connection yourself.</p>")
    elif host in _LOOPBACK_HOSTS:
        parts.append('<p class="note" role="note">Access goes to a program on this computer (for example '
                     "Claude Code). Any local program could ask for this: only approve if you just started "
                     "the connection yourself.</p>")
    if error:
        parts.append(f'<p class="note error" role="alert">{_esc(error)}</p>')
    parts += [
        f'<form method="post" action="{CONSENT_PATH}" autocomplete="off">',
        f'<input type="hidden" name="request" value="{_esc(pending.request_id)}">',
        f'<input type="hidden" name="csrf" value="{_esc(pending.csrf)}">',
        '<label for="code">Pairing code</label>',
        '<input type="text" id="code" name="code" required maxlength="16" autocapitalize="characters" '
        'spellcheck="false" autocomplete="off" placeholder="XXXX-XXXX" autofocus aria-describedby="code-help">',
        '<p id="code-help" class="help">Shown in the terminal running <code>profilepilot connect chatgpt</code> '
        "or <code>profilepilot serve</code>, in ProfilePilot Manager under Connections, and by "
        "<code>profilepilot connect status</code>.</p>",
        '<div class="actions">',
        '<button type="submit" name="action" value="approve" class="approve">Approve</button>',
        '<button type="submit" name="action" value="deny" class="deny" formnovalidate>Deny</button>',
        "</div></form>",
        '<p class="foot">You can disconnect every app later with <code>profilepilot connect stop --revoke</code>.</p>',
    ]
    return head + "".join(parts) + tail


def _host_only(host_header: str) -> str:
    """``[::1]:8931`` -> ``[::1]``, ``127.0.0.1:8931`` -> ``127.0.0.1``."""
    if host_header.startswith("["):
        return host_header.split("]", 1)[0] + "]"
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def _csp_source(uri: str) -> str:
    parts = urlsplit(uri)
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return f"{parts.scheme}:"


# ---------------------------------------------------------------------- ASGI gateway


class OAuthGateway:
    """ASGI middleware in front of the SDK's Starlette app.

    * serves the consent page and the discovery documents (authorization server metadata with
      RFC 9207 and client-ID-metadata-document support, protected resource metadata at the root
      and path-inserted URLs);
    * adds ``iss`` to every ``/authorize`` redirect back to a client;
    * adds ``scope="profilepilot"`` to the MCP endpoint's ``WWW-Authenticate`` challenges.
    """

    def __init__(self, app: Any, setup: "OAuthSetup") -> None:
        self.app = app
        self.setup = setup
        provider = setup.provider
        issuer = setup.issuer
        self._metadata = {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "registration_endpoint": f"{issuer}/register",
            "revocation_endpoint": f"{issuer}/revoke",
            "scopes_supported": [SCOPE],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
            "revocation_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
            "client_id_metadata_document_supported": bool(provider.cimd),
        }
        self._resource_metadata = {
            "resource": setup.resource,
            "authorization_servers": [issuer],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "ProfilePilot",
        }
        mcp_path = urlsplit(setup.resource).path
        self._prm_paths = {PRM_ROOT_PATH, PRM_ROOT_PATH + mcp_path}
        self._mcp_path = mcp_path
        self._consent_hosts = {urlsplit(issuer).netloc.lower(), *(h for h in _LOOPBACK_HOSTS)}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if path == AS_METADATA_PATH:
            await self._json_document(scope, receive, send, self._metadata)
        elif path in self._prm_paths:
            await self._json_document(scope, receive, send, self._resource_metadata)
        elif path == CONSENT_PATH:
            await self._consent(scope, receive, send)
        elif path == "/authorize":
            await self.app(scope, receive, self._with_iss(send))
        elif path == self._mcp_path or path.startswith(self._mcp_path + "/"):
            await self.app(scope, receive, self._with_scope_challenge(send))
        else:
            await self.app(scope, receive, send)

    # -- discovery

    async def _json_document(self, scope: dict[str, Any], receive: Any, send: Any, document: dict[str, Any]) -> None:
        from starlette.responses import JSONResponse, Response

        cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "mcp-protocol-version"}
        method = scope.get("method")
        if method == "OPTIONS":
            response: Any = Response(status_code=204, headers=cors)
        elif method in ("GET", "HEAD"):
            response = JSONResponse(document, headers={**cors, "Cache-Control": "public, max-age=300"})
        else:
            response = Response(status_code=405, headers={"Allow": "GET, OPTIONS"})
        await response(scope, receive, send)

    # -- redirects

    def _with_iss(self, send: Any) -> Any:
        issuer = self.setup.issuer
        consent_prefix = f"{issuer}{CONSENT_PATH}"

        async def wrapped(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start" and message.get("status") in (302, 303, 307):
                headers = []
                for name, value in message.get("headers") or []:
                    if name.lower() == b"location":
                        location = value.decode("latin-1")
                        query = parse_qs(urlsplit(location).query)
                        if not location.startswith(consent_prefix) and "iss" not in query and (
                            "code" in query or "error" in query
                        ):
                            location = construct_redirect_uri(location, iss=issuer)
                        value = location.encode("latin-1")
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        return wrapped

    def _with_scope_challenge(self, send: Any) -> Any:
        async def wrapped(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start" and message.get("status") in (401, 403):
                headers = []
                for name, value in message.get("headers") or []:
                    if name.lower() == b"www-authenticate" and b"scope=" not in value:
                        value = value + f', scope="{SCOPE}"'.encode()
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        return wrapped

    # -- consent

    def _security_headers(self, nonce: str, form_targets: Iterable[str] = ()) -> dict[str, str]:
        targets = " ".join(dict.fromkeys(t for t in form_targets if t))
        csp = (
            f"default-src 'none'; style-src 'nonce-{nonce}'; img-src data:; base-uri 'none'; "
            f"form-action 'self'{(' ' + targets) if targets else ''}; frame-ancestors 'none'"
        )
        return {
            "Content-Security-Policy": csp,
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        }

    async def _consent(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        from starlette.requests import Request
        from starlette.responses import HTMLResponse, RedirectResponse, Response

        provider = self.setup.provider
        request = Request(scope, receive)
        host = (request.headers.get("host") or "").lower()
        if host not in self._consent_hosts and _host_only(host) not in _LOOPBACK_HOSTS:
            await Response("Misdirected request", status_code=421)(scope, receive, send)
            return
        nonce = secrets.token_urlsafe(16)
        cookie_name = "pp_consent"
        secure = self.setup.issuer.startswith("https://")

        def page(pending: PendingAuthorization | None, status: int, **kw: Any) -> HTMLResponse:
            targets = [_csp_source(pending.redirect_uri)] if pending else []
            response = HTMLResponse(render_consent_page(pending, nonce=nonce, **kw), status_code=status,
                                    headers=self._security_headers(nonce, targets))
            if pending is not None:
                response.set_cookie(cookie_name, pending.csrf, max_age=PENDING_TTL, path=CONSENT_PATH,
                                    httponly=True, samesite="strict", secure=secure)
            return response

        if request.method == "GET":
            pending = provider.pending(request.query_params.get("request"))
            response: Any = page(pending, 200 if pending else 400)
            await response(scope, receive, send)
            return
        if request.method != "POST":
            await Response(status_code=405, headers={"Allow": "GET, POST"})(scope, receive, send)
            return

        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_FORM_BYTES:
                await Response("Request too large", status_code=413)(scope, receive, send)
                return
        form = {k: v[0] for k, v in parse_qs(bytes(body).decode("utf-8", "replace"), max_num_fields=10).items() if v}
        pending = provider.pending(form.get("request"))
        if pending is None:
            await page(None, 400)(scope, receive, send)
            return
        csrf_form = form.get("csrf", "")
        csrf_cookie = request.cookies.get(cookie_name, "")
        if not (compare_digest(csrf_form.encode(), pending.csrf.encode())
                and compare_digest(csrf_cookie.encode(), pending.csrf.encode())):
            await page(pending, 403, error="This page expired or was opened in another browser. Reload it "
                                           "and try again.")(scope, receive, send)
            return
        headers = self._security_headers(nonce, [_csp_source(pending.redirect_uri)])
        if form.get("action") == "deny":
            target = provider.deny(pending)
            await RedirectResponse(target, status_code=303, headers=headers)(scope, receive, send)
            return
        wait = provider.attempts_blocked_for()
        if wait > 0:
            minutes = max(1, int(wait // 60) + (1 if wait % 60 else 0))
            await page(pending, 429, error=f"Too many wrong pairing codes. Wait {minutes} minute(s) and try "
                                           "again.")(scope, receive, send)
            return
        target = await provider.approve(pending, form.get("code", ""))
        if target is None:
            left = provider.attempts_left()
            hint = f" {left} attempt(s) left." if left else " No attempts left for now."
            await page(pending, 400, error="That pairing code is not right." + hint)(scope, receive, send)
            return
        await RedirectResponse(target, status_code=303, headers=headers)(scope, receive, send)


# ---------------------------------------------------------------------- public helpers


@dataclass
class OAuthSetup:
    """Everything the HTTP server needs for ``--auth oauth``."""

    provider: PairingOAuthProvider
    settings: AuthSettings
    issuer: str
    resource: str

    def wrap(self, app: Any) -> OAuthGateway:
        """Put the consent page and the discovery documents in front of the SDK's app."""
        return OAuthGateway(app, self)

    def pairing_code(self) -> str:
        return self.provider.pairing_code()

    def describe(self) -> list[str]:
        """Banner lines for ``serve`` (they contain the pairing code: show them once, never log)."""
        return [
            "Auth: OAuth with a pairing code. In ChatGPT choose Authentication: OAuth.",
            f"  MCP URL:      {self.resource}",
            f"  Pairing code: {self.pairing_code()}   (asked on the sign-in page; it changes after each use)",
        ]


def build_oauth(
    store: Any,
    public_url: str,
    *,
    mcp_path: str = "/mcp",
    cimd: bool = True,
    clock: Clock = time.time,
    cimd_fetcher: CimdFetcher | None = None,
) -> OAuthSetup:
    """Provider + :class:`AuthSettings` + gateway for ``public_url`` (``https://<tunnel host>``).

    ``public_url`` may also be the full MCP URL; its origin becomes the issuer and
    ``<origin><mcp_path>`` the protected resource."""
    issuer, resource = _split_public_url(public_url, mcp_path)
    provider = PairingOAuthProvider(store, issuer=issuer, resource=resource, cimd=cimd, clock=clock,
                                    cimd_fetcher=cimd_fetcher)
    settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=resource,
        client_registration_options=ClientRegistrationOptions(enabled=True, default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[SCOPE],
        validate_token_resource=True,
    )
    pairing_code(store)  # make sure a code exists before anyone looks for it
    return OAuthSetup(provider=provider, settings=settings, issuer=issuer, resource=resource)


def build_oauth_settings(public_url: str, *, store: Any = None, root: Path | str | None = None,
                         mcp_path: str = "/mcp") -> tuple[PairingOAuthProvider, AuthSettings]:
    """``(provider, AuthSettings)`` for ``MCPServer(auth_server_provider=..., auth=...)``.
    Prefer :func:`build_oauth`, which also returns the gateway that serves the consent page."""
    _split_public_url(public_url, mcp_path)  # validate before touching any data folder
    if store is None:
        from ..store import Store

        store = Store(root)
    setup = build_oauth(store, public_url, mcp_path=mcp_path)
    return setup.provider, setup.settings


def oauth_status(store: Any) -> dict[str, Any]:
    """Pairing code and connected clients (for ``connect status`` and the Manager)."""
    db = OAuthStore(store.root)
    return {"pairing_code": pairing_code(store), "connections": db.grants()}


__all__ = [
    "ACCESS_TTL", "CHATGPT_REDIRECT_URI", "CONSENT_PATH", "OAuthGateway", "OAuthSetup", "OAuthStore",
    "PairingClient", "PairingOAuthProvider", "REFRESH_TTL", "SCOPE", "build_oauth", "build_oauth_settings",
    "client_from_metadata_document", "fetch_client_metadata_document", "loopback_redirect_matches",
    "new_pairing_code",
    "normalize_pairing_code", "oauth_status", "pairing_code", "public_base_url", "render_consent_page",
    "rotate_pairing_code", "validate_redirect_uri",
]
