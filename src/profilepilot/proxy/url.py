"""Parsing and formatting of upstream proxy specifications.

Accepted inputs (scheme defaults to ``http`` unless ``default_scheme`` says otherwise):

* ``socks5://user:pass@host:port`` (also ``socks5h``, ``socks4``, ``socks4a``, ``http``, ``https``)
* ``user:pass@host:port``
* ``host:port``
* ``host:port:user:pass`` (the common "provider list" format)

Credentials may be percent-encoded in URL form.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

SCHEMES = ("http", "https", "socks4", "socks5")
_ALIASES = {"socks5h": "socks5", "socks4a": "socks4", "socks": "socks5"}


class ProxyParseError(ValueError):
    """Raised when a proxy string cannot be understood."""


@dataclass(frozen=True)
class ProxyEndpoint:
    """A single upstream proxy server."""

    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        if self.scheme not in SCHEMES:
            raise ProxyParseError(f"unsupported proxy scheme {self.scheme!r}; expected one of {SCHEMES}")
        if not self.host:
            raise ProxyParseError("proxy host is empty")
        if not (0 < int(self.port) < 65536):
            raise ProxyParseError(f"proxy port out of range: {self.port}")

    @property
    def has_auth(self) -> bool:
        return bool(self.username)

    @property
    def is_socks(self) -> bool:
        return self.scheme.startswith("socks")

    def _host_for_url(self) -> str:
        try:
            if ipaddress.ip_address(self.host).version == 6:
                return f"[{self.host}]"
        except ValueError:
            pass
        return self.host

    def to_url(self, *, with_auth: bool = True, remote_dns: bool = False) -> str:
        """Render as a URL. ``remote_dns`` emits ``socks5h`` (used by curl / curl_cffi / requests)."""
        scheme = self.scheme
        if remote_dns and scheme == "socks5":
            scheme = "socks5h"
        auth = ""
        if with_auth and self.username:
            auth = quote(self.username, safe="")
            if self.password is not None:
                auth += ":" + quote(self.password, safe="")
            auth += "@"
        return f"{scheme}://{auth}{self._host_for_url()}:{self.port}"

    def redacted(self) -> str:
        """URL safe to show to a model or a log: credentials are masked."""
        if not self.username:
            return self.to_url(with_auth=False)
        return f"{self.scheme}://{self.username[:3]}***:***@{self._host_for_url()}:{self.port}"


def parse_proxy(value: str, default_scheme: str = "http") -> ProxyEndpoint:
    """Parse a proxy string in any of the supported formats."""
    text = (value or "").strip()
    if not text:
        raise ProxyParseError("empty proxy string")
    default_scheme = _ALIASES.get(default_scheme.lower(), default_scheme.lower())

    if "://" in text:
        parts = urlsplit(text)
        scheme = _ALIASES.get(parts.scheme.lower(), parts.scheme.lower())
        try:
            port = parts.port
        except ValueError as exc:
            raise ProxyParseError(f"invalid proxy port in {value!r}") from exc
        if not parts.hostname or port is None:
            raise ProxyParseError(f"proxy URL must include host and port: {value!r}")
        return ProxyEndpoint(
            scheme=scheme,
            host=parts.hostname,
            port=port,
            username=unquote(parts.username) if parts.username else None,
            password=unquote(parts.password) if parts.password is not None else None,
        )

    if "@" in text:
        creds, _, hostport = text.rpartition("@")
        user, sep, pwd = creds.partition(":")
        host, port = _split_host_port(hostport, value)
        return ProxyEndpoint(default_scheme, host, port, user or None, pwd if sep else None)

    pieces = text.split(":")
    if len(pieces) == 2:
        host, port = _split_host_port(text, value)
        return ProxyEndpoint(default_scheme, host, port)
    if len(pieces) >= 4:
        host, port_s, user = pieces[0], pieces[1], pieces[2]
        pwd = ":".join(pieces[3:])
        return ProxyEndpoint(default_scheme, host, _to_port(port_s, value), user or None, pwd)
    raise ProxyParseError(f"unrecognised proxy format: {value!r}")


def _split_host_port(hostport: str, original: str) -> tuple[str, int]:
    if hostport.startswith("["):
        host, _, rest = hostport[1:].partition("]")
        return host, _to_port(rest.lstrip(":"), original)
    host, sep, port = hostport.rpartition(":")
    if not sep:
        raise ProxyParseError(f"proxy must include a port: {original!r}")
    return host, _to_port(port, original)


def _to_port(port: str, original: str) -> int:
    try:
        return int(port)
    except ValueError as exc:
        raise ProxyParseError(f"invalid proxy port in {original!r}") from exc
