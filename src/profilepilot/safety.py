"""URL safety policy for model-driven navigation and HTTP fetches.

Two threats are handled here:

* **Browser internals and local files** - ``file:``, ``chrome:``, ``devtools:``, ``view-source:``,
  ``javascript:`` and similar schemes are always refused. Only ``http(s)``, ``about:blank`` and
  (outside remote mode) ``data:`` URLs may be opened.
* **Server-side request forgery in remote mode** - when the MCP server is reachable over HTTP
  (ChatGPT, tunnels), a remote model must not be able to make the user's browser or the
  ``http_fetch`` tool reach ``localhost``, the LAN or cloud metadata endpoints. Unless
  ``allow_private`` is set, hosts that are (or resolve to) loopback, private, link-local, CGNAT,
  multicast or reserved addresses are refused. Numeric host spellings that browsers accept
  (``2130706433``, ``0x7f.1``, ``0177.0.0.1``, ``127.1``, ``[::ffff:127.0.0.1]``, percent-encoded
  or full-width digits) are decoded the way Chrome decodes them before the check.

Local stdio mode (the default) allows ``localhost`` so people can drive their dev servers.

Proxied profiles (``resolve=False``; docs/FINGERPRINT-AUDIT.md F8): resolving a host name here sends it to
this machine's resolver (the ISP), which would defeat the proxy's privacy. For a profile whose traffic
leaves through an upstream proxy, remote mode therefore runs the static checks only: every scheme rule,
``localhost`` / ``*.localhost`` / the local suffixes, and every loopback, private, link-local, CGNAT or
reserved literal in any numeric spelling stay blocked (Chrome's implicit proxy bypass sends ``localhost``
and loopback literals direct, so those checks are still needed). Other names are resolved by the proxy
and connected from the proxy's network. Trade-off: a name that resolves to a private address *on the
proxy's side* is reachable through the proxy - a network that is not the user's.

Limitation: the check happens before navigation. A public page can still redirect or script its
way to a private address afterwards, and DNS answers can change between the check and the
connection (DNS rebinding). This is a guard-rail for model-initiated requests, not a firewall.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import unicodedata
from typing import Iterable
from urllib.parse import unquote, urlsplit

from .errors import PolicyError

log = logging.getLogger("profilepilot.safety")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Schemes that are refused in every mode, with the reason shown to the model.
BLOCKED_SCHEMES: dict[str, str] = {
    "file": "it would expose files on this computer",
    "chrome": "it opens browser-internal pages",
    "chrome-extension": "it opens browser extension pages",
    "chrome-untrusted": "it opens browser-internal pages",
    "chrome-search": "it opens browser-internal pages",
    "chrome-devtools": "it opens the browser's developer tools",
    "devtools": "it opens the browser's developer tools",
    "edge": "it opens browser-internal pages",
    "brave": "it opens browser-internal pages",
    "view-source": "it opens browser-internal pages",
    "javascript": "use browser_evaluate to run scripts",
    "filesystem": "it exposes browser storage",
}

#: Host names that always mean "this machine / this network", without DNS.
_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_DOT_LIKE = str.maketrans({"。": ".", "．": ".", "｡": "."})
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_HOST_PORT_RE = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[^:/?#\\\s]+):\d+(?:[/?#\\]|$)")
_SPECIAL_RE = re.compile(r"(?is)^(https?:)([^?#]*)(.*)$")


def whatwg_slashes(text: str) -> str:
    r"""Treat ``\`` as ``/`` before the query/fragment of an http(s) URL, as browsers do.

    The WHATWG URL standard (which Chrome follows) reads a backslash in a special-scheme URL as a
    path separator, while :func:`urllib.parse.urlsplit` keeps it as part of the authority. Without
    this, ``http://127.0.0.1:8080\@example.com/`` would be checked as host ``example.com`` but
    requested from ``127.0.0.1:8080``.
    """
    match = _SPECIAL_RE.match(text)
    if not match:
        return text
    return match.group(1) + match.group(2).replace("\\", "/") + match.group(3)


class UrlPolicy:
    """Validates URLs before ProfilePilot opens or fetches them.

    ``remote`` is True when serving MCP over HTTP; ``allow_private`` (``--allow-private-network``)
    lifts the private-address restriction of remote mode. Scheme rules always apply.
    """

    def __init__(self, remote: bool = False, allow_private: bool = False) -> None:
        self.remote = bool(remote)
        self.allow_private = bool(allow_private)

    @property
    def restricts_private(self) -> bool:
        """True when loopback / private targets are refused."""
        return self.remote and not self.allow_private

    def describe(self) -> str:
        if not self.remote:
            return "local mode: http(s), about:blank and data: URLs allowed, including localhost"
        if self.allow_private:
            return "remote mode: http(s) and about:blank allowed, private network access explicitly enabled"
        return "remote mode: only public http(s) hosts (localhost and private networks are blocked)"

    # ------------------------------------------------------------------ checks

    def check(self, url: str, *, resolve: bool = True) -> None:
        """Raise :class:`PolicyError` if ``url`` may not be opened. Resolves DNS synchronously, unless
        ``resolve`` is False (a proxied profile: static checks only, see the module docstring)."""
        host, port = self._check_static(url)
        if host is None or not resolve:
            return
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError) as exc:
            log.debug("could not resolve %s (%s); leaving it to the browser/proxy", host, exc)
            return
        self._check_resolved(host, (info[4][0] for info in infos))

    async def acheck(self, url: str, *, resolve: bool = True) -> None:
        """Async variant of :meth:`check` (DNS resolution does not block the event loop)."""
        host, port = self._check_static(url)
        if host is None or not resolve:
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError, OSError) as exc:
            log.debug("could not resolve %s (%s); leaving it to the browser/proxy", host, exc)
            return
        self._check_resolved(host, (info[4][0] for info in infos))

    def check_host(self, host: str, port: int) -> None:
        """Remote mode: refuse a proxy (or other TCP) endpoint on a local / private network.

        Same rules as :meth:`check` (local names, numeric IP spellings, DNS answers); a no-op when
        private targets are allowed. Resolves DNS synchronously.
        """
        if not self.restricts_private:
            return
        self.check(_host_url(host, port))

    async def acheck_host(self, host: str, port: int) -> None:
        """Async variant of :meth:`check_host`."""
        if not self.restricts_private:
            return
        await self.acheck(_host_url(host, port))

    def is_allowed(self, url: str) -> bool:
        try:
            self.check(url)
        except PolicyError:
            return False
        return True

    # ------------------------------------------------------------------ internals

    def _check_static(self, url: str) -> tuple[str | None, int | None]:
        """Scheme and literal-host checks. Returns (host, port) when DNS still has to be checked."""
        text = (url or "").strip()
        if not text:
            raise PolicyError("No URL given.")
        if any(ord(ch) < 32 for ch in text):
            raise PolicyError("URL contains control characters.")
        text = whatwg_slashes(text)  # check the host the browser will really contact
        try:
            parts = urlsplit(text)
        except ValueError as exc:
            raise PolicyError(f"Invalid URL: {exc}") from None
        scheme = parts.scheme.lower()
        if not scheme:
            raise PolicyError(f"URL needs a scheme such as https:// ({_shorten(text)}).")
        if scheme in BLOCKED_SCHEMES:
            raise PolicyError(f"Opening '{scheme}:' URLs is blocked by ProfilePilot's safety policy ({BLOCKED_SCHEMES[scheme]}).")
        if scheme == "about":
            if text.lower() in ("about:blank", "about:srcdoc"):
                return None, None
            raise PolicyError("Only about:blank is allowed among about: URLs.")
        if scheme == "data":
            if self.restricts_private:
                raise PolicyError("data: URLs are blocked in remote mode.")
            return None, None
        if scheme not in ("http", "https"):
            raise PolicyError(f"Unsupported URL scheme '{scheme}:'. Only http:// and https:// URLs can be opened.")

        try:
            raw_host = parts.hostname
            port = parts.port
        except ValueError as exc:
            raise PolicyError(f"Invalid URL: {exc}") from None
        if not raw_host:
            raise PolicyError(f"URL has no host: {_shorten(text)}")
        if not self.restricts_private:
            return None, None

        host = canonical_host(raw_host)
        if host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES):
            raise _private_error(raw_host)
        literal = parse_ip_host(host)
        if literal is not None:
            if is_private_address(literal):
                raise _private_error(raw_host, literal)
            return None, None
        return host, port or (443 if scheme == "https" else 80)

    def _check_resolved(self, host: str, addresses: Iterable[str]) -> None:
        for raw in addresses:
            try:
                addr = ipaddress.ip_address(str(raw).split("%", 1)[0])
            except ValueError:
                continue
            if is_private_address(addr):
                raise _private_error(host, addr)


# ---------------------------------------------------------------------- helpers


def canonical_host(host: str) -> str:
    """Lower-case, percent-decoded, NFKC-normalised host without a trailing dot (like Chrome)."""
    value = unquote(host or "").translate(_DOT_LIKE)
    value = unicodedata.normalize("NFKC", value).strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value.rstrip(".") if value not in (".", "") else value


def parse_ip_host(host: str) -> IPAddress | None:
    """Interpret ``host`` as an IP literal the way browsers do (WHATWG URL host parsing).

    Returns ``None`` for ordinary domain names. Raises :class:`PolicyError` for hosts that browsers
    would treat as a (malformed) IPv4 address, such as ``1.2.3.999``.
    """
    host = canonical_host(host)
    if ":" in host:
        try:
            return ipaddress.IPv6Address(host.split("%", 1)[0])
        except ValueError:
            raise PolicyError(f"Invalid IPv6 host: {host}") from None
    parts = host.split(".")
    if len(parts) > 1 and parts[-1] == "":
        parts.pop()
    if not parts or _ipv4_number(parts[-1]) is None:
        return None  # does not "end in a number": a domain name
    numbers = [_ipv4_number(p) for p in parts]
    if len(parts) > 4 or any(n is None for n in numbers):
        raise PolicyError(f"Invalid IPv4 host: {host}")
    nums = [int(n) for n in numbers if n is not None]
    if any(n > 255 for n in nums[:-1]) or nums[-1] >= 256 ** (5 - len(nums)):
        raise PolicyError(f"Invalid IPv4 host: {host}")
    value = nums[-1]
    for i, n in enumerate(nums[:-1]):
        value += n * 256 ** (3 - i)
    return ipaddress.IPv4Address(value)


def _ipv4_number(part: str) -> int | None:
    if part == "":
        return None
    try:
        if part[:2] in ("0x", "0X"):
            return int(part[2:], 16) if part[2:] else 0
        if len(part) > 1 and part.startswith("0"):
            return int(part[1:], 8)
        if part.isdigit() and part.isascii():
            return int(part, 10)
    except ValueError:
        return None
    return None


def is_private_address(addr: IPAddress) -> bool:
    """True for anything that is not a globally routable unicast address."""
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = addr.ipv4_mapped or addr.sixtofour or (addr.teredo[1] if addr.teredo else None)
        if embedded is not None and is_private_address(embedded):
            return True
        if addr in _NAT64:
            if is_private_address(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)):
                return True
    return (
        not addr.is_global
        or addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def _private_error(host: str, addr: IPAddress | None = None) -> PolicyError:
    where = f" ({addr})" if addr is not None and str(addr) != host else ""
    return PolicyError(
        f"'{host}'{where} is a local or private network address. ProfilePilot is running in remote mode, "
        "where access to localhost and private networks is blocked (start the server with "
        "--allow-private-network to permit it)."
    )


def _host_url(host: str, port: int) -> str:
    host = (host or "").strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 literal
    if not host or any(ch in host for ch in "/?#@\\") or any(ord(ch) < 33 for ch in host):
        raise PolicyError("Invalid proxy host.")
    return f"http://{host}:{int(port)}/"


def _shorten(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def normalize_url(value: str) -> str:
    """Turn what a model typed into a navigable URL.

    ``example.com/x`` becomes ``https://example.com/x``; ``localhost:3000`` and bare IPs get
    ``http://``. URLs that already carry a scheme (including ``about:blank``) are returned as is.
    """
    text = (value or "").strip()
    if not text:
        raise PolicyError("No URL given.")
    return whatwg_slashes(_with_scheme(text))  # the URL that is checked is the URL that is opened


def _with_scheme(text: str) -> str:
    if text.startswith(("//", "\\\\", "/\\", "\\/")):
        return "https://" + text[2:]
    if _SCHEME_RE.match(text) and not _HOST_PORT_RE.match(text):
        return text
    if text.startswith("[") and "]" in text:
        host = text[1 : text.index("]")].lower()
    else:
        host = re.split(r"[/:?#\\]", text, maxsplit=1)[0].lower()
    local = host in _LOCAL_NAMES or host.endswith(".localhost")
    if not local:
        try:
            local = parse_ip_host(host) is not None
        except PolicyError:
            local = False
    return ("http://" if local else "https://") + text
