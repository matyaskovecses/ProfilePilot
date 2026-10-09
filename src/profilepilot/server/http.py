"""Remote mode: the MCP server over Streamable HTTP (``profilepilot serve --http``).

Designed for ChatGPT (public HTTPS via a tunnel) and for one shared server used by several local
clients (DESIGN section 6):

* ``json_response=True`` and ``stateless_http=True`` (tunnels such as cloudflared quick tunnels do
  not stream SSE reliably);
* DNS-rebinding protection: only the loopback names and the given ``public_hosts`` are accepted
  in the ``Host`` header, and only ``https://<public host>`` (plus loopback) as ``Origin``;
* authentication:

  - ``secret-path`` (default): the endpoint becomes ``<path>/<43-char random secret>``. ChatGPT
    supports only "no auth" or OAuth, so an unguessable URL is the practical protection. The
    secret is kept in the secret store so the URL survives restarts (``new_secret`` rotates it).
  - ``token``: a static bearer token (Claude Code / Codex ``--header "Authorization: Bearer ..."``).
    Taken from ``token`` or ``PROFILEPILOT_TOKEN``; generated and announced once if missing.
  - ``none``: refused unless the server binds a loopback address *and* ``i_understand`` is set.

* the URL policy runs in remote mode: localhost / private-network targets are blocked unless
  ``allow_private``.

Uvicorn's access log is disabled: request lines would contain the secret path.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import sys
from dataclasses import dataclass, field
from hmac import compare_digest
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from ..errors import ProfilePilotError
from ..store import Store

log = logging.getLogger("profilepilot.server.http")

AuthMode = Literal["secret-path", "token", "none"]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8931
DEFAULT_PATH = "/mcp"
ENV_TOKEN = "PROFILEPILOT_TOKEN"
PATH_SECRET_KEY = "http:path-secret"
MIN_TOKEN_LENGTH = 16
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


class StaticTokenVerifier(TokenVerifier):
    """Accepts exactly one shared bearer token (constant-time comparison)."""

    def __init__(self, token: str) -> None:
        self._token = token.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        if compare_digest(token.encode("utf-8"), self._token):
            return AccessToken(token=token, client_id="profilepilot-http", scopes=[])
        return None


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IP literals."""
    name = (host or "").strip().strip("[]").lower()
    if name in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def normalize_public_host(value: str) -> str:
    """``https://Example.com/`` -> ``example.com`` (host[:port] only)."""
    text = (value or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].strip().lower()
    if not text or any(ch in text for ch in " \t\r\n@"):
        raise ProfilePilotError(f"Invalid public host: {value!r} (expected a host name such as my-tunnel.example.com).")
    return text


def transport_security(public_hosts: Sequence[str] = ()) -> TransportSecuritySettings:
    """Host/Origin allow-lists: loopback (any port) plus the public host names."""
    hosts: list[str] = []
    origins: list[str] = []
    for name in LOOPBACK_NAMES:
        hosts += [name, f"{name}:*"]
        origins += [f"http://{name}", f"http://{name}:*", f"https://{name}", f"https://{name}:*"]
    for raw in public_hosts:
        host = normalize_public_host(raw)
        hosts += [host, f"{host}:*"] if ":" not in host.split("]")[-1] else [host]
        origins.append(f"https://{host}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(hosts)),
        allowed_origins=list(dict.fromkeys(origins)),
    )


def path_secret(store: Store, *, rotate: bool = False) -> str:
    """The persistent secret path component (created on first use, rotated with ``rotate``)."""
    current = None if rotate else store.secrets.get(PATH_SECRET_KEY)
    if current and len(current) >= 32:
        return current
    value = secrets.token_urlsafe(32)
    store.secrets.set(PATH_SECRET_KEY, value)
    return value


@dataclass
class HttpPlan:
    """Everything needed to run (or test) the HTTP server. ``urls`` contain secrets: show them
    to the user once, never log them."""

    app: Any
    server: Any
    host: str
    port: int
    path: str
    auth: AuthMode
    token: str | None = None
    token_generated: bool = False
    urls: list[str] = field(default_factory=list)


def build_http_app(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    public_hosts: Sequence[str] = (),
    auth: AuthMode = "secret-path",
    token: str | None = None,
    allow_private: bool = False,
    i_understand: bool = False,
    new_secret: bool = False,
    root: Path | str | None = None,
    store: Store | None = None,
    runtime: Any = None,
    log_level: str = "INFO",
) -> HttpPlan:
    """Build the Starlette app of the remote server (validating the auth choice)."""
    from .app import create_server

    if auth not in ("secret-path", "token", "none"):
        raise ProfilePilotError("auth must be 'secret-path', 'token' or 'none'.")
    if not 0 < int(port) < 65536:
        raise ProfilePilotError(f"Invalid port {port}.")
    publics = [normalize_public_host(h) for h in public_hosts]
    store = store if store is not None else Store(root)
    base_path = "/" + (path or DEFAULT_PATH).strip().strip("/")
    if base_path == "/":
        raise ProfilePilotError("The MCP path must not be '/'. Use e.g. /mcp.")

    verifier: StaticTokenVerifier | None = None
    auth_settings: AuthSettings | None = None
    generated = False
    endpoint = base_path
    if auth == "none":
        if not is_loopback(host) or not i_understand:
            raise ProfilePilotError(
                "Refusing to serve without authentication. --auth none is only allowed on a loopback --host "
                "together with --i-understand (anyone who can reach the port could drive your browsers)."
            )
        log.warning("serving MCP over HTTP WITHOUT authentication on %s:%s", host, port)
    elif auth == "token":
        token = (token or os.environ.get(ENV_TOKEN) or "").strip() or None
        if token is None:
            token = secrets.token_urlsafe(32)
            generated = True
        if len(token) < MIN_TOKEN_LENGTH:
            raise ProfilePilotError(f"The bearer token must be at least {MIN_TOKEN_LENGTH} characters long.")
        verifier = StaticTokenVerifier(token)
        issuer = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::', '') else host}:{port}"
        auth_settings = AuthSettings(issuer_url=issuer, resource_server_url=None, validate_token_resource=False)
    else:  # secret-path
        endpoint = f"{base_path}/{path_secret(store, rotate=new_secret)}"

    server = create_server(
        store=store, runtime=runtime, remote=True, allow_private=allow_private,
        token_verifier=verifier, auth=auth_settings, log_level=log_level.upper(),  # type: ignore[arg-type]
    )
    app = server.streamable_http_app(
        streamable_http_path=endpoint,
        json_response=True,
        stateless_http=True,
        transport_security=transport_security(publics),
        host=host,
    )
    local_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else (f"[{host}]" if ":" in host else host)
    urls = [f"http://{local_host}:{port}{endpoint}"] + [f"https://{h}{endpoint}" for h in publics]
    return HttpPlan(app=app, server=server, host=host, port=int(port), path=endpoint, auth=auth, token=token,
                    token_generated=generated, urls=urls)


def describe_plan(plan: HttpPlan) -> str:
    """Human-readable startup banner (contains the secret URL / generated token: show once)."""
    lines = [f"ProfilePilot MCP server (Streamable HTTP) listening on {plan.host}:{plan.port}"]
    if plan.auth == "secret-path":
        lines.append("Auth: secret path. Treat these URLs like passwords:")
    elif plan.auth == "token":
        lines.append("Auth: bearer token (send 'Authorization: Bearer <token>').")
    else:
        lines.append("Auth: NONE (loopback only).")
    lines += [f"  {u}" for u in plan.urls]
    if plan.auth == "token" and plan.token_generated:
        lines.append(f"Generated token (shown once; set {ENV_TOKEN} to keep a fixed one): {plan.token}")
    lines.append("Remote mode: localhost and private-network URLs are blocked unless --allow-private-network.")
    return "\n".join(lines)


def serve_http(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    public_hosts: Sequence[str] = (),
    auth: AuthMode = "secret-path",
    token: str | None = None,
    allow_private: bool = False,
    *,
    i_understand: bool = False,
    new_secret: bool = False,
    root: Path | str | None = None,
    log_level: str = "INFO",
    announce: Callable[[str], None] | None = None,
) -> None:
    """Run the remote MCP server until interrupted (blocking).

    ``announce`` receives the startup banner with the endpoint URL(s); by default it is written to
    stderr. It contains the secret path / generated token and is shown exactly once.
    """
    import anyio
    import uvicorn

    plan = build_http_app(
        host=host, port=port, path=path, public_hosts=public_hosts, auth=auth, token=token,
        allow_private=allow_private, i_understand=i_understand, new_secret=new_secret, root=root,
        log_level=log_level,
    )
    banner = describe_plan(plan)
    if announce is not None:
        announce(banner)
    else:
        sys.stderr.write(banner + "\n")
        sys.stderr.flush()
    config = uvicorn.Config(
        plan.app, host=host, port=int(port), log_level="warning", access_log=False, lifespan="on",
        proxy_headers=False, server_header=False,
    )
    from .app import quiet_http_client_logs

    quiet_http_client_logs()
    anyio.run(uvicorn.Server(config).serve)


__all__ = [
    "DEFAULT_PORT", "HttpPlan", "StaticTokenVerifier", "build_http_app", "describe_plan", "is_loopback",
    "path_secret", "serve_http", "transport_security",
]
