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
the ``connect`` terminal, ProfilePilot Manager and ``profilepilot connect status``; it rotates when
the server starts and after every successful use. That is what makes a public tunnel URL safe: only
someone who can see the user's screen can approve.

**Guessing.** At most 5 wrong codes are checked per 10 minutes (all sign-in requests together; the
check and the count are one step, so concurrent requests cannot slip past it), and a sign-in request
is closed after 5 wrong codes. When someone else used up those attempts, the owner is not locked
out: ``profilepilot connect unlock`` on this computer makes a new, longer code (12 characters,
about 59 bits) and lets it through for 5 minutes (:func:`unlock_sign_in`). Wrong codes are not
counted against everyone in that window (so a guesser cannot lock the owner out again), which is
why that code is long: guessing it within 5 minutes is hopeless however fast someone tries.

**Pending sign-ins** are not stored: the consent link carries the request, signed with a
per-process key, so nobody can push the owner's sign-in out of a table by flooding ``/authorize``.

**Redirects.** Redirect URIs must be https, http to loopback, or an app scheme (``cursor``,
``vscode``, ``claude`` or the reverse-DNS form of RFC 8252). Error and "deny" redirects only go to
clients the user approved before (or to ChatGPT / Claude / loopback); anything else gets an error
page, so the server cannot be used as an open redirector.

Integration (see docs/design/WIRE-IN.md, "ChatGPT")::

    setup = build_oauth(store, public_base_url(host, port, public_hosts), mcp_path="/mcp")
    server = create_server(..., auth_server_provider=setup.provider, auth=setup.settings)
    app = setup.wrap(server.streamable_http_app(...))
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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
from urllib.parse import parse_qs, urlsplit, urlunsplit

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
"""Seconds a consent link stays valid."""
ATTEMPT_LIMIT = 5
"""Wrong pairing codes checked per ``ATTEMPT_WINDOW``, all sign-in requests together. Beyond that no
code is checked (not even the right one) until the window passes or the owner runs
``profilepilot connect unlock``."""
ATTEMPT_WINDOW = 600
REQUEST_ATTEMPT_LIMIT = 5
"""Wrong pairing codes per sign-in request; then the request is closed."""
UNLOCK_TTL = 300
"""Seconds after ``profilepilot connect unlock`` during which the pairing code is checked even when
the wrong-code limit is used up (the code is new, so earlier guesses are worthless, and it has
``UNLOCK_CODE_LENGTH`` characters, so guesses made in the window are worthless too)."""
MAX_REQUEST_ID = 8192
MAX_CLOSED_REQUESTS = 10_000
MAX_CLIENTS = 100
MAX_REGISTRATIONS_PER_HOUR = 30
"""Dynamic client registrations per hour for all redirect hosts except ChatGPT's and Claude's,
together (so junk registrations cannot block those)."""
REGISTRATIONS_PER_HOST_PER_HOUR = 10
FIRST_PARTY_REGISTRATIONS_PER_HOUR = 60
MAX_REDIRECT_URIS = 10
MAX_FORM_BYTES = 16 * 1024

CHATGPT_REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
"""ChatGPT's stable redirect URI (used because this server supports RFC 9207 ``iss``)."""
KNOWN_REDIRECT_URIS = {
    CHATGPT_REDIRECT_URI: "ChatGPT",
    "https://chat.openai.com/connector_platform_oauth_redirect": "ChatGPT",
    "https://claude.ai/api/mcp/auth_callback": "Claude",
    "https://claude.com/api/mcp/auth_callback": "Claude",
}
"""Exact redirect URIs that earn the green product badge on the consent page."""
FIRST_PARTY_HOSTS = ("chatgpt.com", "chat.openai.com", "openai.com", "claude.ai", "claude.com", "anthropic.com")
"""Hosts with their own registration / metadata-fetch budgets (not shared with arbitrary hosts)."""
APP_SCHEMES = frozenset({"cursor", "vscode", "vscode-insiders", "claude"})
"""Private-use redirect schemes accepted besides the reverse-DNS form of RFC 8252 7.1."""
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")

PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
"""31 characters without look-alikes (no 0/O, 1/I/L)."""
PAIRING_LENGTH = 8
UNLOCK_CODE_LENGTH = 12
"""Length of the code ``connect unlock`` makes (about 59 bits): it is checked without the global
wrong-code limit for ``UNLOCK_TTL`` seconds, so it must resist unlimited guessing for that long."""

CIMD_MAX_BYTES = 32 * 1024
CIMD_TIMEOUT = 5.0
CIMD_CACHE_TTL = 3600
CIMD_NEGATIVE_TTL = 60
CIMD_FETCHES_PER_MINUTE = 10
"""Client metadata document fetches per minute and host."""
CIMD_FIRST_PARTY_FETCHES_PER_MINUTE = 30
CIMD_GLOBAL_FETCHES_PER_MINUTE = 30
"""Fetches per minute for all hosts except ChatGPT's and Claude's, together."""

Clock = Callable[[], float]
CimdFetcher = Callable[[str], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------- pairing code


def new_pairing_code(length: int = PAIRING_LENGTH) -> str:
    """A fresh pairing code such as ``ABCD-2345`` (8 characters, about 40 bits; ``length`` 12 gives
    ``ABCD-2345-EFGH``, about 59 bits)."""
    raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(length))
    return "-".join(raw[i:i + 4] for i in range(0, length, 4))


def normalize_pairing_code(text: str | None) -> str:
    """Upper-case, without separators or spaces (``abcd 2345`` == ``ABCD-2345``)."""
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()[:32]


def pairing_code(store: Any, *, rotate: bool = False) -> str:
    """The current pairing code (created on first use; ``rotate`` makes a new one).

    It lives in the secret store so that the server, ``profilepilot connect status`` and the
    Manager (separate processes) all show the same code."""
    current = None if rotate else store.secrets.get(PAIRING_KEY)
    if current and len(normalize_pairing_code(current)) in (PAIRING_LENGTH, UNLOCK_CODE_LENGTH):
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


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _first_party_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in FIRST_PARTY_HOSTS)


def _redirect_base(uri: str) -> str:
    """``scheme://host[:port]/path`` of a redirect URI (no query): what is compared against the
    registered / known redirect URIs."""
    try:
        parts = urlsplit(str(uri))
    except ValueError:
        return ""
    netloc = parts.netloc.lower()
    base = f"{parts.scheme.lower()}:"
    if netloc or parts.scheme.lower() in ("http", "https"):
        base += f"//{netloc}"
    return base + parts.path


def _is_loopback_redirect(uri: str) -> bool:
    try:
        parts = urlsplit(str(uri))
    except ValueError:
        return False
    return parts.scheme == "http" and (parts.hostname or "").lower() in _LOOPBACK_HOSTS


def _redirect_label(uri: str) -> tuple[str, str | None]:
    """``(host or scheme shown to the user, known product name or None)``.

    The product name ("ChatGPT", "Claude") is given only for their exact redirect URIs, never for
    any path on their hosts."""
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    if parts.scheme in ("http", "https") and host:
        known = None if parts.query else KNOWN_REDIRECT_URIS.get(_redirect_base(uri))
        return host, known
    return f"{parts.scheme}: (an app on this computer)", None


def _app_scheme_allowed(scheme: str) -> bool:
    """``cursor`` & co, or the reverse-DNS form of RFC 8252 7.1 (``com.example.app``). Single-word
    schemes such as ``search-ms``, ``ms-officecmd`` or ``smb`` are refused: on Windows many of them
    start programs or reach network shares."""
    return scheme in APP_SCHEMES or bool(re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+", scheme))


def validate_redirect_uri(uri: str) -> str:
    """Raise :class:`ValueError` unless ``uri`` is an acceptable redirect URI (an allowlist).

    https anywhere; http only to loopback (native apps); private-use schemes only for desktop apps
    (``cursor``, ``vscode``, ``vscode-insiders``, ``claude`` or ``com.example.app``); never a fragment
    or credentials.
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
    if parts.username or parts.password:
        raise ValueError("redirect URIs must not contain credentials")
    if scheme in ("http", "https"):
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError(f"redirect URI {text!r} has no host")
        if scheme == "http" and host not in _LOOPBACK_HOSTS:
            raise ValueError("http redirect URIs are only allowed for localhost (use https)")
    elif not _app_scheme_allowed(scheme):
        raise ValueError(f"redirect URI scheme {scheme!r} is not allowed: use https, http://localhost or an "
                         "app scheme in reverse-DNS form (e.g. com.example.app:/callback)")
    return text


def _redirect_allowed(uri: Any) -> bool:
    try:
        validate_redirect_uri(str(uri))
    except ValueError:
        return False
    return True


def _with_allowed_redirects(client: OAuthClientInformationFull) -> OAuthClientInformationFull | None:
    """A stored client without the redirect URIs that :func:`validate_redirect_uri` refuses (a record
    saved by an older version, before the allowlist). None when no redirect URI is left."""
    uris = list(client.redirect_uris or [])
    allowed = [u for u in uris if _redirect_allowed(u)]
    if len(allowed) == len(uris):
        return client
    log.warning("OAuth: ignoring %d redirect URI(s) of a stored client that are no longer allowed",
                len(uris) - len(allowed))
    if not allowed:
        return None
    return client.model_copy(update={"redirect_uris": allowed})


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
                "access": {}, "refresh": {}, "used_refresh": {}, "meta": {}}

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
        # the salt that re-derives a rotated refresh token's successors is only needed in the grace window
        for record in data["used_refresh"].values():
            if "salt" in record and not _within_grace(record, now):
                del record["salt"]
        # a grant ends when its last refresh token (or, without one, access token) has expired
        live = {v.get("grant_id") for v in data["access"].values()} | {
            v.get("grant_id") for v in data["refresh"].values()}
        for gid in [g for g in data["grants"] if g not in live]:
            del data["grants"][gid]
        meta = data["meta"]
        for key in ("locked_until", "unlock_until"):
            try:
                expired = float(meta.get(key) or 0) < now
            except (TypeError, ValueError):
                expired = True
            if key in meta and expired:
                del meta[key]

    # -- summaries (used by `connect status`, the Manager and the wizard)

    def grants(self) -> list[dict[str, Any]]:
        """Active connections: ``client_id``, ``client_name``, ``created_at``, ``last_used_at`` and
        ``resource`` (the MCP URL the connection was approved for; it only works there)."""
        data = self.read()
        now = self.clock()
        live = {v.get("grant_id") for v in data["refresh"].values() if float(v.get("expires_at") or 0) >= now}
        live |= {v.get("grant_id") for v in data["access"].values() if float(v.get("expires_at") or 0) >= now}
        out = []
        for gid, grant in data["grants"].items():
            if gid in live:
                out.append({"grant_id": gid, **{k: grant.get(k) for k in
                                                ("client_id", "client_name", "created_at", "last_used_at",
                                                 "resource")}})
        return sorted(out, key=lambda g: g.get("created_at") or 0)

    def sign_in_lock(self) -> dict[str, float | None]:
        """``locked_until`` (wrong-code limit used up, as reported by the server) and ``unlock_until``
        (``connect unlock`` window); ``None`` when not in effect."""
        meta = self.read()["meta"]
        now = self.clock()
        out: dict[str, float | None] = {}
        for key in ("locked_until", "unlock_until"):
            try:
                value = float(meta.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            out[key] = value if value > now else None
        return out

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
    """An ``/authorize`` request waiting for the user's decision on the consent page.

    Nothing is stored server-side: ``request_id`` is the signed request itself (see
    :meth:`PairingOAuthProvider.authorize`) and ``key`` a short digest of it for bookkeeping."""

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
    key: str = ""


class SignInLocked(Exception):
    """Too many wrong pairing codes: no code is checked for ``wait`` seconds (or until
    ``profilepilot connect unlock``)."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"sign-in locked for {wait:.0f} s")
        self.wait = wait


@dataclass
class _RateLimiter:
    """Sliding window: at most ``limit`` events per ``window`` seconds (only the newest ``limit``
    events are kept, which is all the check needs)."""

    limit: int
    window: float
    events: deque[float] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.events = deque(self.events, maxlen=max(1, self.limit))

    def _trim(self, now: float) -> None:
        while self.events and self.events[0] <= now - self.window:
            self.events.popleft()

    def blocked_for(self, now: float) -> float:
        """Seconds until another attempt is allowed (0 = allowed now)."""
        self._trim(now)
        if len(self.events) < self.limit:
            return 0.0
        return max(0.0, self.events[0] + self.window - now)

    def hit(self, now: float) -> float:
        self._trim(now)
        self.events.append(now)
        return now

    def release(self, event: float) -> None:
        """Forget one recorded event (a reserved attempt that turned out not to count)."""
        try:
            self.events.remove(event)
        except ValueError:
            pass

    def idle(self, now: float) -> bool:
        self._trim(now)
        return not self.events


def _budget(table: dict[str, _RateLimiter], key: str, limit: int, window: float, now: float) -> _RateLimiter:
    """The limiter of ``key`` in ``table`` (bounded: idle limiters are dropped when it grows)."""
    limiter = table.get(key)
    if limiter is None:
        if len(table) >= 512:
            for name in [k for k, v in table.items() if v.idle(now)]:
                del table[name]
            if len(table) >= 512:
                table.clear()
        limiter = table[key] = _RateLimiter(limit, window)
    return limiter


def _redirect_group(uri: str) -> str:
    """What registration budgets are counted per: the redirect host (or the app scheme)."""
    parts = urlsplit(str(uri))
    if parts.scheme in ("http", "https"):
        return (parts.hostname or "").lower()
    return parts.scheme.lower() + ":"


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
        self._key = secrets.token_bytes(32)  # signs consent links; they die with the process
        self._closed: dict[str, float] = {}  # request key -> when it stops mattering
        self._fails: dict[str, int] = {}  # request key -> wrong codes
        self._attempts = _RateLimiter(ATTEMPT_LIMIT, ATTEMPT_WINDOW)
        self._registrations = _RateLimiter(MAX_REGISTRATIONS_PER_HOUR, 3600)
        self._registrations_by_host: dict[str, _RateLimiter] = {}
        self._cimd_fetches = _RateLimiter(CIMD_GLOBAL_FETCHES_PER_MINUTE, 60)
        self._cimd_by_host: dict[str, _RateLimiter] = {}
        self._cimd_cache: dict[str, tuple[float, OAuthClientInformationFull | None]] = {}
        self._cimd_good: dict[str, float] = {}  # metadata URLs that worked before -> when
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
            client = PairingClient.model_validate(record.get("info") or {})
        except ValidationError:
            log.warning("ignoring an unreadable OAuth client record")
            return None
        return _with_allowed_redirects(client)

    def _registration_allowed(self, uris: Sequence[str], now: float) -> bool:
        """Budgets per redirect host (so junk registrations for other hosts cannot use up ChatGPT's or
        Claude's) plus one shared budget for every host that is not theirs."""
        groups = sorted({_redirect_group(u) for u in uris})
        first_party = all(_first_party_host(g) for g in groups)
        limiters = [
            _budget(self._registrations_by_host, g,
                    FIRST_PARTY_REGISTRATIONS_PER_HOUR if _first_party_host(g) else REGISTRATIONS_PER_HOST_PER_HOUR,
                    3600, now)
            for g in groups
        ]
        if any(limiter.blocked_for(now) for limiter in limiters):
            return False
        if not first_party and self._registrations.blocked_for(now):
            return False
        for limiter in limiters:
            limiter.hit(now)
        if not first_party:
            self._registrations.hit(now)
        return True

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        now = self.clock()
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
        if not self._registration_allowed(uris, now):
            raise RegistrationError("invalid_client_metadata", "Too many registrations; try again later.")
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

    def _cimd_fetch_allowed(self, url: str, host: str, now: float) -> bool:
        """A URL that worked before may always be refreshed (it is cached for an hour); others share a
        per-host budget, and hosts other than ChatGPT's / Claude's also one global budget."""
        if url in self._cimd_good:
            return True
        first_party = _first_party_host(host)
        per_host = _budget(self._cimd_by_host, host,
                           CIMD_FIRST_PARTY_FETCHES_PER_MINUTE if first_party else CIMD_FETCHES_PER_MINUTE, 60, now)
        if per_host.blocked_for(now) or (not first_party and self._cimd_fetches.blocked_for(now)):
            return False
        per_host.hit(now)
        if not first_party:
            self._cimd_fetches.hit(now)
        return True

    def _remember_cimd(self, url: str, now: float, client: OAuthClientInformationFull | None) -> None:
        if len(self._cimd_cache) > 256:
            self._cimd_cache.clear()
        self._cimd_cache[url] = (now, client)
        if client is not None:
            if len(self._cimd_good) > 256:
                for old in sorted(self._cimd_good, key=self._cimd_good.__getitem__)[:128]:
                    del self._cimd_good[old]
            self._cimd_good[url] = now

    async def _cimd_client(self, url: str) -> OAuthClientInformationFull | None:
        now = self.clock()
        cached = self._cimd_cache.get(url)
        if cached is not None:
            ttl = CIMD_CACHE_TTL if cached[1] is not None else CIMD_NEGATIVE_TTL
            if now - cached[0] < ttl:
                return cached[1]
        try:
            check_cimd_url(url)  # before any budget or cache slot is spent: malformed URLs cost nothing
        except ValueError as exc:
            log.debug("OAuth: client metadata document URL rejected: %s", str(exc)[:200])
            return None
        host = (urlsplit(url).hostname or "").lower()
        if not self._cimd_fetch_allowed(url, host, now):
            return cached[1] if cached else None
        client: OAuthClientInformationFull | None = None
        try:
            document = await self._fetch_cimd(url)
            client = client_from_metadata_document(url, document)
        except (ValueError, ProfilePilotError) as exc:
            log.warning("OAuth: client metadata document rejected: %s", str(exc)[:200])
        except Exception as exc:  # network errors and the like
            log.warning("OAuth: could not fetch a client metadata document: %s", type(exc).__name__)
        self._remember_cimd(url, now, client)
        return client

    def _approved_redirects(self) -> set[str]:
        """Redirect URIs (``_redirect_base`` form) of clients the user has approved (a live grant)."""
        data = self.db.read()
        out: set[str] = set()
        for grant in data["grants"].values():
            cid = grant.get("client_id")
            uris: list[str] = []
            record = data["clients"].get(cid) if isinstance(cid, str) else None
            if isinstance(record, dict):
                uris = [str(u) for u in (record.get("info") or {}).get("redirect_uris") or []]
            elif isinstance(cid, str) and self._cimd_cache.get(cid, (0, None))[1] is not None:
                uris = [str(u) for u in self._cimd_cache[cid][1].redirect_uris or []]  # type: ignore[union-attr]
            out |= {_redirect_base(u) for u in uris if _redirect_allowed(u)}
        return out

    def trusted_redirect(self, uri: str) -> bool:
        """May an error / "denied" result be sent to ``uri`` without a pairing code? Only to
        ChatGPT's and Claude's own redirect URIs, to loopback (an app on this computer) and to
        clients the user approved before; never an open redirect for anyone else."""
        if _is_loopback_redirect(uri):
            return True
        base = _redirect_base(uri)
        if base in KNOWN_REDIRECT_URIS:
            return True
        try:
            return base in self._approved_redirects()
        except Exception as exc:  # pragma: no cover - unreadable store: refuse
            log.debug("could not read approved clients: %s", exc)
            return False

    # -- authorization

    def _sign(self, label: str, text: str) -> str:
        return _b64(hmac.new(self._key, f"{label}|{text}".encode(), hashlib.sha256).digest())

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self._resource_ok(params.resource):
            raise AuthorizeError("invalid_target", "This authorization server only issues tokens for "
                                                   f"{self.resource}.")
        # One scope exists. Others (e.g. "openid") are ignored rather than refused (RFC 6749 3.3: the
        # server may grant fewer scopes); the token response tells the client what it got.
        body = {
            "c": client.client_id, "n": _display_name(client.client_name), "r": str(params.redirect_uri),
            "x": bool(params.redirect_uri_provided_explicitly), "s": params.state, "h": params.code_challenge,
            "t": round(self.clock(), 3), "i": secrets.token_urlsafe(9),
        }
        blob = _b64(json.dumps(body, separators=(",", ":")).encode("utf-8"))
        request_id = f"{blob}.{self._sign('request', blob)}"
        return f"{self.issuer}{CONSENT_PATH}?request={request_id}"

    def pending(self, request_id: str | None) -> PendingAuthorization | None:
        """The sign-in request behind a consent link, or None (forged, expired or already used)."""
        if not request_id or len(request_id) > MAX_REQUEST_ID or "." not in request_id:
            return None
        blob, _, signature = request_id.rpartition(".")
        if not compare_digest(signature.encode(), self._sign("request", blob).encode()):
            return None
        try:
            body = json.loads(_unb64(blob))
            created = float(body["t"])
            pending = PendingAuthorization(
                request_id=request_id, client_id=str(body["c"]), client_name=body.get("n"),
                redirect_uri=str(body["r"]), redirect_uri_provided_explicitly=bool(body.get("x")),
                state=body.get("s"), scopes=[SCOPE], code_challenge=str(body["h"]), resource=self.resource,
                csrf="", created_at=created, key=_hash(request_id)[:32],
            )
        except (ValueError, KeyError, TypeError):
            return None
        now = self.clock()
        if not -60 <= now - created <= PENDING_TTL:
            return None
        self._expire(now)
        if pending.key in self._closed:
            return None
        pending.csrf = self._sign("csrf", pending.key)
        return pending

    def _expire(self, now: float) -> None:
        if len(self._closed) > MAX_CLOSED_REQUESTS:
            for key in [k for k, until in self._closed.items() if until < now]:
                del self._closed[key]
            if len(self._closed) > MAX_CLOSED_REQUESTS:  # only reachable while unlocked: forget the oldest
                for key in sorted(self._closed, key=self._closed.__getitem__)[: MAX_CLOSED_REQUESTS // 2]:
                    del self._closed[key]
        if len(self._fails) > MAX_CLOSED_REQUESTS:
            self._fails = {k: v for k, v in self._fails.items() if k not in self._closed}
            if len(self._fails) > MAX_CLOSED_REQUESTS:
                self._fails.clear()

    def _close(self, pending: PendingAuthorization) -> None:
        self._closed[pending.key] = pending.created_at + PENDING_TTL + 60
        self._fails.pop(pending.key, None)

    def _unlocked(self, now: float) -> bool:
        try:
            until = float(self.db.read()["meta"].get("unlock_until") or 0)
        except Exception:  # unreadable: no unlock (the limit stays)
            return False
        return now < until <= now + UNLOCK_TTL + 60

    def attempts_blocked_for(self) -> float:
        """Seconds until a pairing code is checked again (0 = now)."""
        now = self.clock()
        return 0.0 if self._unlocked(now) else self._attempts.blocked_for(now)

    def attempts_left(self, pending: PendingAuthorization | None = None) -> int:
        now = self.clock()
        per_request = REQUEST_ATTEMPT_LIMIT - (self._fails.get(pending.key, 0) if pending else 0)
        if self._unlocked(now):
            return max(0, per_request)
        self._attempts.blocked_for(now)
        return max(0, min(per_request, ATTEMPT_LIMIT - len(self._attempts.events)))

    def _reserve(self, now: float) -> float | None:
        """Count an attempt *before* the code is checked (synchronously, before any await), so
        concurrent requests cannot all pass the limit. Raises :class:`SignInLocked`. While the owner's
        unlock is active nothing is counted (attempts from before it still count afterwards): the
        unlock code has ``UNLOCK_CODE_LENGTH`` characters, so guessing is pointless meanwhile."""
        if self._unlocked(now):
            return None
        wait = self._attempts.blocked_for(now)
        if wait > 0:
            raise SignInLocked(wait)
        return self._attempts.hit(now)

    def _release(self, reservation: float | None) -> None:
        if reservation is not None:
            self._attempts.release(reservation)

    def _unfail(self, key: str) -> None:
        """Give back a per-request attempt counted in advance (the code was right, or not checked)."""
        left = self._fails.get(key, 0) - 1
        if left > 0:
            self._fails[key] = left
        else:
            self._fails.pop(key, None)

    async def approve(self, pending: PendingAuthorization, code_text: str) -> str | None:
        """Check the pairing code; on success return the redirect URL with a new authorization
        code (and rotate the pairing code). ``None`` = wrong code (counted) or the request is no
        longer open. Raises :class:`SignInLocked` when too many wrong codes were entered.

        Both limits (all requests together, and this request's own) are counted *before* the first
        await, so concurrent submissions cannot slip past either of them."""
        now = self.clock()
        if pending.key in self._closed or self._fails.get(pending.key, 0) >= REQUEST_ATTEMPT_LIMIT:
            return None
        reservation = self._reserve(now)
        self._fails[pending.key] = self._fails.get(pending.key, 0) + 1
        try:
            expected = normalize_pairing_code(await anyio.to_thread.run_sync(self.pairing_code))
        except BaseException:
            self._release(reservation)
            self._unfail(pending.key)
            raise
        if reservation is None and len(expected) != UNLOCK_CODE_LENGTH:
            # Let through by `connect unlock`, but its long code was replaced meanwhile (the server
            # restarted, `connect stop --revoke`, ...): a normal code gets the normal limit. Checked and
            # counted in one step (no await in between), so concurrent requests cannot slip past.
            wait = self._attempts.blocked_for(now)
            if wait > 0:
                self._unfail(pending.key)
                raise SignInLocked(wait)
            reservation = self._attempts.hit(now)
        given = normalize_pairing_code(code_text)
        if not given or not compare_digest(given.encode(), expected.encode()):
            await self._wrong_code(pending, now, counted=reservation is not None)
            return None
        self._release(reservation)  # the right code does not use up an attempt
        self._unfail(pending.key)
        if pending.key in self._closed:  # approved by a concurrent submission meanwhile
            return None
        self._close(pending)
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

        def apply(data: dict[str, Any]) -> None:
            data["codes"][_hash(code)] = record
            data["meta"].pop("unlock_until", None)  # an unlock window ends with the sign-in it was for
            data["meta"].pop("locked_until", None)

        await self._mutate(apply)
        await anyio.to_thread.run_sync(lambda: rotate_pairing_code(self.store))
        log.info("OAuth: the user approved %r", (pending.client_name or pending.client_id)[:80])
        return construct_redirect_uri(pending.redirect_uri, code=code, state=pending.state, iss=self.issuer)

    async def _wrong_code(self, pending: PendingAuthorization, now: float, *, counted: bool = True) -> None:
        """Bookkeeping after a wrong code. ``counted``: it was counted against the global limit
        (False while a ``connect unlock`` code is in effect)."""
        log.warning("OAuth: wrong pairing code entered on the consent page")
        if self._fails.get(pending.key, 0) >= REQUEST_ATTEMPT_LIMIT:  # counted in approve()
            self._close(pending)
        if not counted:
            return
        wait = self._attempts.blocked_for(now)
        if wait <= 0:
            return
        # the limit is used up: tell `connect status`, the wizard and the Manager (another process)
        until = now + wait

        def apply(data: dict[str, Any]) -> None:
            data["meta"]["locked_until"] = max(float(data["meta"].get("locked_until") or 0), until)

        try:
            await self._mutate(apply)
        except Exception as exc:  # bookkeeping only
            log.debug("could not record the sign-in lock: %s", exc)

    def deny(self, pending: PendingAuthorization) -> str:
        self._close(pending)
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
        resource = record.get("resource")
        if resource and _canonical(resource) != _canonical(self.resource):
            return None  # approved for another URL (e.g. an earlier tunnel address): sign in again
        return StoredRefreshToken(
            token=refresh_token, client_id=client.client_id, scopes=list(record.get("scopes") or [SCOPE]),
            expires_at=int(record["expires_at"]), resource=resource, subject="owner",
            grant_id=str(record.get("grant_id")),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: StoredRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Rotate: a new access + refresh token, the old refresh token is spent.

        A spent refresh token presented again within ``REFRESH_GRACE`` (a response lost on a flaky
        tunnel) gets the *same* successors again, re-derived from the old token and a salt that is
        kept only for the grace window. So a stolen token can never mint a second, independent
        token family; whoever uses the successors second later trips the reuse detection."""
        key = _hash(refresh_token.token)
        now = self.clock()
        requested = [s for s in (scopes or refresh_token.scopes) if s in refresh_token.scopes] or refresh_token.scopes

        def apply(data: dict[str, Any]) -> tuple[dict[str, str | None], list[str], float] | None:
            record = data["refresh"].pop(key, None)
            if record is None:  # already rotated: replay within the grace window
                used = data["used_refresh"].get(key)
                if not (used and _within_grace(used, now) and used.get("salt")):
                    return None
                if data["grants"].get(used.get("grant_id")) is None:
                    return None
                successors = _successor_tokens(refresh_token.token, str(used["salt"]))
                known = (_hash(str(successors["access"])) in data["access"]
                         or _hash(str(successors["refresh"])) in data["refresh"]
                         or _hash(str(successors["refresh"])) in data["used_refresh"])
                if not known:
                    return None  # the successors were revoked meanwhile
                granted = list(used.get("granted") or used.get("scopes") or [SCOPE])
                return successors, granted, float(used.get("rotated_at") or now)
            grant = data["grants"].get(refresh_token.grant_id)
            if grant is None:
                return None
            salt = secrets.token_urlsafe(16)
            successors = _successor_tokens(refresh_token.token, salt)
            data["used_refresh"][key] = {**record, "rotated_at": now, "expires_at": now + REFRESH_TTL, "salt": salt,
                                         "granted": requested}
            grant["last_used_at"] = now
            _store_tokens(data, successors, client.client_id, refresh_token.grant_id, requested, self.resource, now)
            return successors, requested, now

        outcome = await self._mutate(apply)
        if outcome is None:
            raise TokenError("invalid_grant", "refresh token does not exist")
        tokens, granted, issued_at = outcome
        return _token_response(tokens, granted, expires_in=max(1, int(issued_at + ACCESS_TTL - now)))

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


def _successor_tokens(old_refresh: str, salt: str) -> dict[str, str | None]:
    """The access + refresh token that replace ``old_refresh`` (deterministic for one rotation:
    HMAC keyed with the old token itself, which is never stored, and a per-rotation salt)."""
    key = old_refresh.encode("utf-8")

    def derive(label: str) -> str:
        return _b64(hmac.new(key, f"profilepilot-rotation|{label}|{salt}".encode(), hashlib.sha256).digest())

    return {"access": "ppat_" + derive("access"), "refresh": "pprt_" + derive("refresh")}


def _token_response(tokens: dict[str, str | None], scopes: list[str], *, expires_in: int = ACCESS_TTL) -> OAuthToken:
    return OAuthToken(access_token=str(tokens["access"]), token_type="Bearer", expires_in=expires_in,
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


async def _resolve_public(host: str, port: int) -> list[str]:
    """Every address of ``host`` must be public (blind SSRF guard for the metadata fetch). Returns
    the checked addresses: the fetch connects to one of them, so a second DNS answer (rebinding)
    cannot send it somewhere else."""
    from ..safety import is_private_address

    infos = await anyio.to_thread.run_sync(lambda: socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP))
    if not infos:
        raise ValueError("client_id host does not resolve")
    addresses: list[str] = []
    for info in infos:
        text = str(info[4][0]).split("%", 1)[0]
        if is_private_address(ipaddress.ip_address(text)):
            raise ValueError("client_id host resolves to a local or private address")
        if text not in addresses:
            addresses.append(text)
    return addresses


async def fetch_client_metadata_document(url: str, *, transport: Any = None) -> dict[str, Any]:
    """Fetch a client ID metadata document: https only, public addresses only (the connection goes
    to the address that was checked; TLS still verifies the host name), no redirects, at most
    32 KB, 5 s timeout, JSON object. ``transport`` is for tests."""
    import httpx

    check_cimd_url(url)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    address = (await _resolve_public(host, 443))[0]
    target = urlunsplit(("https", f"[{address}]" if ":" in address else address, parts.path, "", ""))
    host_header = f"[{host}]" if ":" in host else host
    async with httpx.AsyncClient(follow_redirects=False, timeout=CIMD_TIMEOUT, trust_env=False, transport=transport,
                                 headers={"Accept": "application/json", "User-Agent": "ProfilePilot-OAuth"}) as http:
        async with http.stream("GET", target, headers={"Host": host_header},
                               extensions={"sni_hostname": host}) as response:
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


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''} ago"


def render_consent_page(
    pending: PendingAuthorization | None,
    *,
    nonce: str,
    error: str | None = None,
    message: str | None = None,
    lead: str | None = None,
    now: float | None = None,
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
        body += f'<p class="lead">{_esc(lead or "Go back to the app you were connecting (for example ChatGPT) and start the connection again.")}</p>'
        if error:
            body += f'<p class="note error" role="alert">{_esc(error)}</p>'
        return head + body + tail

    host, known = _redirect_label(pending.redirect_uri)
    loopback = _is_loopback_redirect(pending.redirect_uri)
    name = pending.client_name or "An app"
    badge = f'<span class="badge">{_esc(known)}</span>' if known else ""
    asked = _age((time.time() if now is None else now) - pending.created_at)
    parts = [
        f"<h1>Allow {_esc(name)} to use ProfilePilot?</h1>",
        '<p class="lead">It will be able to start and control the browser profiles on this computer, '
        "including their saved logins, cookies and proxies.</p>",
        "<dl>",
        f"<dt>App</dt><dd>{_esc(name)} <span class=\"muted\">(the name the app gave itself)</span></dd>",
        f"<dt>Returns to</dt><dd><strong>{_esc(host)}</strong>{badge}"
        f'<div class="uri">{_esc(pending.redirect_uri)}</div></dd>',
        f"<dt>Requested</dt><dd>{_esc(asked)}</dd>",
        "</dl>",
    ]
    if loopback:
        warning = ("Access goes to a program on this computer (for example Claude Code). Any local program could "
                   "ask for this: only approve if you just started the connection yourself.")
    elif known:
        warning = (f"Only approve if you started connecting {known} to ProfilePilot yourself, here, just now. "
                   "If someone sent you this link, click Deny.")
    else:
        warning = ("This app is not ChatGPT or Claude. Only approve if you started this connection yourself, just "
                   "now. If someone sent you this link, click Deny.")
    parts.append(f'<p class="note" role="note">{_esc(warning)}</p>')
    if error:
        parts.append(f'<p class="note error" role="alert">{_esc(error)}</p>')
    parts += [
        f'<form method="post" action="{CONSENT_PATH}" autocomplete="off">',
        f'<input type="hidden" name="request" value="{_esc(pending.request_id)}">',
        f'<input type="hidden" name="csrf" value="{_esc(pending.csrf)}">',
        '<label for="code">Pairing code</label>',
        '<input type="text" id="code" name="code" required maxlength="24" autocapitalize="characters" '
        'spellcheck="false" autocomplete="off" placeholder="XXXX-XXXX" autofocus aria-describedby="code-help">',
        '<p id="code-help" class="help">Shown in the terminal running <code>profilepilot connect chatgpt</code>, '
        "in ProfilePilot Manager under Connections, and by <code>profilepilot connect status</code>. Enter it "
        "only on this page: ProfilePilot never asks for it anywhere else, not in a chat and not by e-mail.</p>",
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
        """``iss`` on every redirect back to a client (RFC 9207); an *error* redirect to a client the
        user never approved becomes an error page instead (no open redirect via ``/authorize``)."""
        issuer = self.setup.issuer
        consent_prefix = f"{issuer}{CONSENT_PATH}"
        replaced = False

        async def wrapped(message: dict[str, Any]) -> None:
            nonlocal replaced
            if replaced:  # the SDK's own redirect body is dropped
                return
            if message.get("type") == "http.response.start" and message.get("status") in (302, 303, 307):
                headers = []
                for name, value in message.get("headers") or []:
                    if name.lower() == b"location":
                        location = value.decode("latin-1")
                        query = parse_qs(urlsplit(location).query)
                        if not location.startswith(consent_prefix) and "iss" not in query and (
                            "code" in query or "error" in query
                        ):
                            if "error" in query and not self.setup.provider.trusted_redirect(location):
                                replaced = True
                                await self._error_page(send, query)
                                return
                            location = construct_redirect_uri(location, iss=issuer)
                        value = location.encode("latin-1")
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        return wrapped

    async def _error_page(self, send: Any, query: dict[str, list[str]]) -> None:
        nonce = secrets.token_urlsafe(16)
        error = (query.get("error") or ["invalid_request"])[0][:80]
        description = (query.get("error_description") or [""])[0][:300]
        detail = f"{error}: {description}" if description else error
        page = render_consent_page(None, nonce=nonce, message="This sign-in request can't be completed",
                                   lead="Go back to the app you were connecting and try again. If you did not "
                                        "start a connection to ProfilePilot, close this page.",
                                   error=detail).encode("utf-8")
        headers = [(k.lower().encode("latin-1"), v.encode("latin-1"))
                   for k, v in self._security_headers(nonce).items()]
        headers += [(b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(page)).encode())]
        await send({"type": "http.response.start", "status": 400, "headers": headers})
        await send({"type": "http.response.body", "body": page, "more_body": False})

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
            response = HTMLResponse(render_consent_page(pending, nonce=nonce, now=provider.clock(), **kw),
                                    status_code=status, headers=self._security_headers(nonce, targets))
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
            if provider.trusted_redirect(target):
                await RedirectResponse(target, status_code=303, headers=headers)(scope, receive, send)
            else:  # never an open redirect: an app the user never approved gets nothing back
                await page(None, 200, message="You denied the request",
                           lead="Nothing was shared. You can close this page.")(scope, receive, send)
            return
        try:
            target = await provider.approve(pending, form.get("code", ""))
        except SignInLocked as locked:
            minutes = max(1, int(locked.wait // 60) + (1 if locked.wait % 60 else 0))
            await page(pending, 429, error=(
                f"Too many wrong pairing codes were entered, so no code is accepted for {minutes} minute(s). If that "
                "was not you, someone else knows this address. To sign in now, run `profilepilot connect unlock` "
                "on the computer running ProfilePilot, then enter the new pairing code it shows."
            ))(scope, receive, send)
            return
        if target is None:
            still = provider.pending(pending.request_id)
            if still is None:
                await page(None, 400, message="This sign-in request is closed",
                           error="Too many wrong pairing codes (or it was already used). Start the connection again "
                                 "in the app.")(scope, receive, send)
                return
            left = provider.attempts_left(still)
            hint = f" {left} attempt(s) left." if left else " No attempts left for now."
            await page(still, 400, error="That pairing code is not right." + hint)(scope, receive, send)
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

    def describe(self, *, show_code: bool = True) -> list[str]:
        """Banner lines for ``serve``. With ``show_code`` they contain the pairing code (only for an
        interactive terminal: show them once, never log); otherwise they say where to find it."""
        code = (f"{self.pairing_code()}   (asked on the sign-in page; it changes after each use; never share it)"
                if show_code else "run `profilepilot connect status` on this computer to see it")
        return [
            "Auth: OAuth with a pairing code. In ChatGPT choose Authentication: OAuth.",
            f"  MCP URL:      {self.resource}",
            f"  Pairing code: {code}",
        ]


def build_oauth(
    store: Any,
    public_url: str,
    *,
    mcp_path: str = "/mcp",
    cimd: bool = True,
    clock: Clock = time.time,
    cimd_fetcher: CimdFetcher | None = None,
    rotate_code: bool = False,
) -> OAuthSetup:
    """Provider + :class:`AuthSettings` + gateway for ``public_url`` (``https://<tunnel host>``).

    ``public_url`` may also be the full MCP URL; its origin becomes the issuer and
    ``<origin><mcp_path>`` the protected resource. ``rotate_code`` makes a new pairing code
    (``serve --auth oauth`` does, so a code seen earlier never outlives a restart)."""
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
    pairing_code(store, rotate=rotate_code)  # make sure a (fresh) code exists before anyone looks for it
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
    """Pairing code, connected clients and the sign-in lock (for ``connect status`` and the Manager).

    ``locked_until``: too many wrong pairing codes were entered (a timestamp, or None); the owner
    can sign in anyway after :func:`unlock_sign_in`. ``unlock_until``: such an unlock is active."""
    db = OAuthStore(store.root)
    return {"pairing_code": pairing_code(store), "connections": db.grants(), **db.sign_in_lock()}


def unlock_sign_in(store: Any, *, clock: Clock = time.time) -> str:
    """The owner's way past the wrong-code limit (``profilepilot connect unlock``, run on this
    computer): a new, longer pairing code (``UNLOCK_CODE_LENGTH`` characters), checked for the next
    ``UNLOCK_TTL`` seconds even if someone else used up the attempts. Wrong codes are not counted
    against everyone meanwhile, so the code's length is what stops guessing. Returns the new code
    (show it to the user only); the next successful sign-in rotates back to a normal code."""
    code = new_pairing_code(UNLOCK_CODE_LENGTH)
    store.secrets.set(PAIRING_KEY, code)
    now = clock()

    def apply(data: dict[str, Any]) -> None:
        data["meta"]["unlock_until"] = now + UNLOCK_TTL
        data["meta"].pop("locked_until", None)

    OAuthStore(store.root, clock=clock).mutate(apply)
    log.info("OAuth: sign-in unlocked by the owner for %d s", UNLOCK_TTL)
    return code


__all__ = [
    "ACCESS_TTL", "CHATGPT_REDIRECT_URI", "CONSENT_PATH", "OAuthGateway", "OAuthSetup", "OAuthStore",
    "PairingClient", "PairingOAuthProvider", "REFRESH_TTL", "SCOPE", "SignInLocked", "UNLOCK_TTL", "build_oauth",
    "build_oauth_settings", "client_from_metadata_document", "fetch_client_metadata_document",
    "loopback_redirect_matches", "new_pairing_code", "normalize_pairing_code", "oauth_status", "pairing_code",
    "public_base_url", "render_consent_page", "rotate_pairing_code", "unlock_sign_in", "validate_redirect_uri",
]
