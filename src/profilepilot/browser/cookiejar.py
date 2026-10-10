"""The live cookie jar of a running profile, over raw CDP (the Manager's and the CLI's cookie editor).

Chrome encrypts cookies on disk, so they are only read and written through the profile's running
browser, with browser-level ``Storage.*`` commands on its websocket (``RuntimeInfo.cdp_ws_url``):
``Storage.getCookies``, ``Storage.setCookies`` and ``Storage.clearCookies``. No domain is ever
enabled and no script runs, so pages cannot notice (tests assert that no ``*.enable`` is sent).

**Shape.** :func:`list_cookies` returns the portable JSON shape of
:mod:`profilepilot.automation.cookies` plus Chrome's extra attributes, which every function here
keeps on the way back::

    {domain, name, value, path, expires (unix seconds, null = session), secure, httpOnly,
     sameSite ("Strict" | "Lax" | "None" | null), partitionKey? ({topLevelSite, hasCrossSiteAncestor}),
     priority?, sourceScheme?, sourcePort?, size}

A leading dot in ``domain`` makes a domain cookie (sent to subdomains too); no dot is a host-only
cookie. ``expires`` keeps Chrome's sub-second precision.

**Identity** of a cookie is ``(name, domain, path, partitionKey)`` (both partition fields count:
Chrome keeps ``hasCrossSiteAncestor`` true and false apart); :func:`cookie_key` makes it an opaque,
URL-safe id. Changing any of these in :func:`save_cookie` sets the new cookie first and deletes the
old one only once Chrome accepted the new one.

**Deleting exactly one cookie**: :func:`delete_cookies` writes an already expired copy of exactly
that cookie with ``Storage.setCookies``. Chrome then removes the cookie with that identity and
nothing else - not its host-only / domain twin, not the same name on another path or in another
partition (checked against real Chrome by ``tests/test_cookiejar.py``). It works on the browser
target, in one batch for many cookies, and needs no page session. The one case it cannot reach is
a path Chrome would rewrite on a set (a server may store ``/a b``; a set makes it ``/a%20b``): those
cookies are deleted with ``Network.deleteCookies`` (exact name, domain, path and partition) in a
short flat session on a page target, attached and detached like a Manager thumbnail.

**Errors**: cookies are checked before anything is sent and problems raise
:class:`InvalidCookieError` with a plain message (empty name, bad domain, SameSite=None without
Secure, ``__Secure-`` / ``__Host-`` rules, control characters, sizes Chrome refuses ...). Chrome
refuses some cookies with one error for a whole batch, silently drops others and turns a domain
cookie for a public suffix (``.co.uk``) into a host-only one, so writes are read back: anything
Chrome did not store as asked raises :class:`CookieRefusedError` naming those cookies (and such a
stray host-only cookie is undone).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ipaddress
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlsplit

from ..automation.cookies import (
    CookieFormat,
    CookieFormatError,
    domain_matches,
    parse_cookies_report,
    partition_site,
    to_portable,
)
from ..errors import ConflictError, NotFoundError, ProfilePilotError
from .devtools import CdpConnection, CdpError, browser_connection

MAX_NAME_VALUE = 4096
"""Chrome refuses a cookie whose name and value together are longer (bytes)."""
MAX_ATTRIBUTE = 1024
"""Chrome refuses a longer path or domain attribute (bytes)."""
MAX_BATCH = 10_000
"""At most this many cookies in one write (a whole Chrome profile keeps about 3300)."""
MAX_KEY = 8192
"""Longest cookie id :func:`cookie_key` can produce for a cookie Chrome accepts."""
SET_TIMEOUT = 30.0
EXPIRED = 1.0
"""``expires`` of the copy that deletes a cookie (1970-01-01T00:00:01Z)."""

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_LABEL = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_PATH = re.compile(r"^/[A-Za-z0-9\-._~!$&'()*+,=:@%/]*$")
_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

ImportMode = Literal["merge", "replace", "replace_all"]


class InvalidCookieError(ProfilePilotError, ValueError):
    """A cookie Chrome would refuse or silently change; raised before anything is sent."""


class CookieRefusedError(ProfilePilotError):
    """Chrome did not store (or delete) some cookies. ``refused`` labels them (``'sid' on
    example.com``, never values); ``done`` is how many of the others went through."""

    def __init__(self, message: str, *, refused: list[str], done: int = 0) -> None:
        super().__init__(message)
        self.refused = refused
        self.done = done


class CookieExistsError(ConflictError):
    """An edit would turn a cookie into another cookie that already exists (``existing`` labels it)."""

    def __init__(self, message: str, *, existing: str) -> None:
        super().__init__(message)
        self.existing = existing


# ---------------------------------------------------------------------- identity


def _partition_identity(cookie: Mapping[str, Any]) -> Any:
    if cookie.get("partitionKeyOpaque"):
        return "opaque"
    partition = cookie.get("partitionKey")
    if isinstance(partition, Mapping) and partition.get("topLevelSite"):
        return [str(partition["topLevelSite"]), bool(partition.get("hasCrossSiteAncestor"))]
    if isinstance(partition, str) and partition:
        return [partition, False]
    return None


def cookie_key(cookie: Mapping[str, Any]) -> str:
    """Stable opaque id of a cookie's identity ``(name, domain, path, partitionKey)``: URL-safe
    base64 (no padding), so it fits in a query string and never contains a comma."""
    identity = [str(cookie.get("name") or ""), str(cookie.get("domain") or "").strip().lower(),
                str(cookie.get("path") or "/"), _partition_identity(cookie)]
    raw = json.dumps(identity, separators=(",", ":"), ensure_ascii=True)
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def parse_key(key: str) -> dict[str, Any]:
    """The identity behind a :func:`cookie_key`: ``{name, domain, path, partitionKey}``
    (``partitionKey``: the CDP object, ``"opaque"`` or None). Raises :class:`InvalidCookieError`."""
    if not isinstance(key, str) or not key or len(key) > MAX_KEY or not _KEY.match(key):
        raise InvalidCookieError("That is not a cookie id.")
    try:
        data = json.loads(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)))
    except (binascii.Error, ValueError):
        raise InvalidCookieError("That is not a cookie id.") from None
    if not (isinstance(data, list) and len(data) == 4 and all(isinstance(x, str) for x in data[:3])):
        raise InvalidCookieError("That is not a cookie id.")
    partition = data[3]
    if isinstance(partition, list) and len(partition) == 2:
        partition = {"topLevelSite": str(partition[0]), "hasCrossSiteAncestor": bool(partition[1])}
    elif partition != "opaque":
        partition = None
    return {"name": data[0], "domain": data[1], "path": data[2], "partitionKey": partition}


def cookie_label(cookie: Mapping[str, Any]) -> str:
    """``'sid' on example.com`` (+ partition), for messages: never the value."""
    label = f"'{cookie.get('name') or ''}' on {str(cookie.get('domain') or '').lstrip('.') or '?'}"
    path = str(cookie.get("path") or "/")
    if path != "/":
        label += f" (path {path})"
    site = partition_site(cookie)
    if site:
        label += f" (partitioned for {site})"
    return label


def _input_label(cookie: Any) -> str:
    """Best-effort label of a cookie that may not even parse."""
    if not isinstance(cookie, Mapping):
        return "not an object"
    name = cookie.get("name")
    domain = cookie.get("domain") or cookie.get("host") or cookie.get("url")
    return f"'{str(name)[:80]}' on {str(domain).lstrip('.')[:120]}" if name and domain else (
        f"'{str(name)[:80]}'" if name else "no name")


# ---------------------------------------------------------------------- normalising


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else float(value)


def normalize_cookie(cookie: Mapping[str, Any]) -> dict[str, Any]:
    """Chrome's ``Network.Cookie`` -> the shape described in the module docstring."""
    expires = cookie.get("expires")
    session = bool(cookie.get("session")) or not (
        isinstance(expires, (int, float)) and not isinstance(expires, bool) and math.isfinite(expires) and expires > 0)
    name, value = str(cookie.get("name") or ""), str(cookie.get("value") or "")
    same_site = cookie.get("sameSite")
    out: dict[str, Any] = {
        "domain": str(cookie.get("domain") or ""),
        "name": name,
        "value": value,
        "path": str(cookie.get("path") or "/"),
        "expires": None if session else _number(expires),  # type: ignore[arg-type]
        "secure": bool(cookie.get("secure")),
        "httpOnly": bool(cookie.get("httpOnly")),
        "sameSite": same_site if same_site in ("Strict", "Lax", "None") else None,
    }
    partition = cookie.get("partitionKey")
    if isinstance(partition, Mapping) and partition.get("topLevelSite"):
        out["partitionKey"] = dict(partition)
    if cookie.get("partitionKeyOpaque"):
        out["partitionKeyOpaque"] = True  # partitioned for an opaque site: listed, but not addressable
    for key in ("priority", "sourceScheme"):
        if isinstance(cookie.get(key), str) and cookie[key]:
            out[key] = cookie[key]
    port = cookie.get("sourcePort")
    if isinstance(port, int) and not isinstance(port, bool):
        out["sourcePort"] = port
    size = cookie.get("size")
    out["size"] = size if isinstance(size, int) and not isinstance(size, bool) else \
        len(name.encode("utf-8")) + len(value.encode("utf-8"))
    return out


def _sort_key(cookie: Mapping[str, Any]) -> tuple:
    return (str(cookie.get("domain") or "").lstrip("."), str(cookie.get("domain") or "").startswith("."),
            str(cookie.get("name") or ""), str(cookie.get("path") or ""), json.dumps(_partition_identity(cookie)))


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(ts, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    spec = "seconds" if float(ts).is_integer() else "milliseconds"
    return dt.isoformat(timespec=spec).replace("+00:00", "Z")


def cookie_view(cookie: Mapping[str, Any]) -> dict[str, Any]:
    """The Manager's view of one cookie (see ``docs/design/COOKIES.md``). ``domain`` is Chrome's
    (leading dot for a domain cookie); ``expires`` is ISO 8601 UTC or None for a session cookie."""
    partition = cookie.get("partitionKey")
    opaque = bool(cookie.get("partitionKeyOpaque"))
    expires = cookie.get("expires")
    name, value = str(cookie.get("name") or ""), str(cookie.get("value") or "")
    size = cookie.get("size")
    return {
        "key": cookie_key(cookie),
        "name": name,
        "value": value,
        "domain": str(cookie.get("domain") or ""),
        "host_only": not str(cookie.get("domain") or "").startswith("."),
        "path": str(cookie.get("path") or "/"),
        "expires": _iso(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
        "session": not (isinstance(expires, (int, float)) and expires > 0),
        "http_only": bool(cookie.get("httpOnly")),
        "secure": bool(cookie.get("secure")),
        "same_site": cookie.get("sameSite") or None,
        "partitioned": bool(partition) or opaque,
        "partition_site": partition_site(cookie),
        "partition_cross_site": bool(partition.get("hasCrossSiteAncestor")) if isinstance(partition, Mapping) else None,
        "size": size if isinstance(size, int) else len(name.encode("utf-8")) + len(value.encode("utf-8")),
        "priority": cookie.get("priority") or "Medium",
    }


def domain_counts(cookies: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``[{domain, count}]`` per site (host-only and domain cookies of one host count together)."""
    counts: dict[str, int] = {}
    for c in cookies:
        site = str(c.get("domain") or "").lstrip(".")
        counts[site] = counts.get(site, 0) + 1
    return [{"domain": d, "count": n} for d, n in sorted(counts.items())]


def filter_domain(domain: str) -> str:
    """A domain typed as a filter (``Example.com``, ``.example.com``, ``bücher.de``) -> the form
    :func:`~profilepilot.automation.cookies.domain_matches` compares with."""
    text = str(domain or "").strip().strip(".").lower()
    if not text:
        raise InvalidCookieError("Give a domain, e.g. example.com.")
    if not text.isascii():
        try:
            text = text.encode("idna").decode("ascii")
        except UnicodeError:
            raise InvalidCookieError(f"'{domain}' is not a valid domain.") from None
    return text


# ---------------------------------------------------------------------- validation


def _check_domain(domain: str) -> str:
    text = domain.strip().lower()
    dotted = text.startswith(".")
    host = text[1:] if dotted else text
    if not host:
        raise InvalidCookieError("The cookie needs a domain, e.g. example.com.")
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise InvalidCookieError(f"'{domain}' is not a valid IPv6 address.") from None
        if dotted:
            raise InvalidCookieError("An IP address can only have host-only cookies (no leading dot).")
        return host
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise InvalidCookieError(f"'{domain}' is not a valid domain.") from None
    if len(host) > 253 or host.startswith(".") or host.endswith(".") or ".." in host:
        raise InvalidCookieError(f"'{domain}' is not a valid domain.")
    if _IPV4.match(host):
        if any(int(part) > 255 for part in host.split(".")):
            raise InvalidCookieError(f"'{domain}' is not a valid IP address.")
        if dotted:
            raise InvalidCookieError("An IP address can only have host-only cookies (no leading dot).")
        return host
    if not all(_LABEL.match(label) for label in host.split(".")):
        raise InvalidCookieError(f"'{domain}' is not a valid domain (letters, digits, '-' and '.' only).")
    if dotted and "." not in host:
        raise InvalidCookieError(f"A cookie for every site under '.{host}' is not allowed; give a site such as "
                                 f"example.{host}, or make it host-only.")
    return ("." if dotted else "") + host


def _check_path(path: str) -> str:
    if len(path.encode("utf-8")) > MAX_ATTRIBUTE:
        raise InvalidCookieError(f"The path is too long (max {MAX_ATTRIBUTE} characters).")
    if not _PATH.match(path) or _BAD_ESCAPE.search(path):
        raise InvalidCookieError("The path must start with / and contain only URL characters (write a space as %20).")
    if any(seg in (".", "..") for seg in path.split("/")):
        raise InvalidCookieError("The path can't contain '.' or '..' segments.")
    return path


def _check_partition(partition: Any) -> dict[str, Any] | None:
    if partition in (None, ""):
        return None
    if isinstance(partition, str):
        partition = {"topLevelSite": partition, "hasCrossSiteAncestor": False}
    if not isinstance(partition, Mapping):
        raise InvalidCookieError("The partition must be a site such as https://example.com.")
    site = str(partition.get("topLevelSite") or "").strip()
    if site and "://" not in site:
        site = "https://" + site
    parts = urlsplit(site)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidCookieError("The partition must be a site such as https://example.com.")
    host = parts.hostname if parts.hostname.isascii() else _check_domain(parts.hostname)
    return {"topLevelSite": f"{parts.scheme}://{host}", "hasCrossSiteAncestor": bool(partition.get("hasCrossSiteAncestor"))}


def validate_cookie(cookie: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Check one cookie (any shape :func:`~profilepilot.automation.cookies.to_portable` accepts, with
    an optional ``partitionKey``, ``priority``, ``sourceScheme``, ``sourcePort``) the way Chrome
    would, and return it normalised (domain lower-case / punycode). Raises
    :class:`InvalidCookieError` with a plain-language reason; never quotes the value."""
    if isinstance(cookie, Mapping) and cookie.get("partitionKeyOpaque"):
        raise InvalidCookieError("Cookies partitioned for an opaque site can't be written.")
    try:
        c = to_portable(cookie, exact_expiry=True)  # keeps Chrome's sub-second expiry on a round trip
    except CookieFormatError as exc:
        raise InvalidCookieError(str(exc).replace("Cookie is missing its 'name'.", "The cookie needs a name.")) from None
    name, value = c["name"], c["value"]
    if name != name.strip() or _CONTROL.search(name) or ";" in name or "=" in name:
        raise InvalidCookieError("The name can't contain ';', '=', control characters or leading / trailing spaces.")
    if value != value.strip(" \t") or _CONTROL.search(value) or ";" in value:
        raise InvalidCookieError("The value can't contain ';', control characters or leading / trailing spaces.")
    if len(name.encode("utf-8")) + len(value.encode("utf-8")) > MAX_NAME_VALUE:
        raise InvalidCookieError(f"Too large: name and value together can be at most {MAX_NAME_VALUE} bytes.")
    c["domain"] = _check_domain(c["domain"])
    c["path"] = _check_path(c["path"])
    if c["sameSite"] == "None" and not c["secure"]:
        raise InvalidCookieError("SameSite=None needs Secure (or pick Lax / Strict).")
    lowered = name.lower()
    if lowered.startswith("__secure-") and not c["secure"]:
        raise InvalidCookieError("Cookies named __Secure-... must be Secure.")
    if lowered.startswith("__host-") and not (c["secure"] and c["path"] == "/" and not c["domain"].startswith(".")):
        raise InvalidCookieError("Cookies named __Host-... must be Secure, host-only (no leading dot) and have the "
                                 "path /.")
    partition = _check_partition(c.pop("partitionKey", None))
    if partition is not None:
        if not c["secure"]:
            raise InvalidCookieError("A partitioned cookie must be Secure.")
        c["partitionKey"] = partition
    if c["expires"] is not None and c["expires"] <= (time.time() if now is None else now):
        raise InvalidCookieError("It has already expired (pick a future date, or make it a session cookie).")
    if c["secure"] and c.get("sourceScheme") == "NonSecure":
        c.pop("sourceScheme")  # Chrome refuses this pair; it derives the scheme from Secure instead
        c.pop("sourcePort", None)
    c["size"] = len(name.encode("utf-8")) + len(value.encode("utf-8"))
    return c


def _cdp_param(cookie: Mapping[str, Any], *, expires: float | None = None) -> dict[str, Any]:
    """A normalised cookie -> CDP ``Network.CookieParam`` (``expires`` overrides, for deleting)."""
    param: dict[str, Any] = {
        "name": cookie["name"], "value": cookie["value"], "domain": cookie["domain"], "path": cookie["path"],
        "secure": bool(cookie.get("secure")), "httpOnly": bool(cookie.get("httpOnly")),
    }
    when = expires if expires is not None else cookie.get("expires")
    if when is not None:
        param["expires"] = when
    if cookie.get("sameSite"):
        param["sameSite"] = cookie["sameSite"]
    partition = cookie.get("partitionKey")
    if isinstance(partition, Mapping):
        param["partitionKey"] = {"topLevelSite": partition["topLevelSite"],
                                 "hasCrossSiteAncestor": bool(partition.get("hasCrossSiteAncestor"))}
    for key in ("priority", "sourceScheme", "sourcePort"):
        if cookie.get(key) is not None:
            param[key] = cookie[key]
    if param["secure"] and param.get("sourceScheme") == "NonSecure":
        del param["sourceScheme"]  # Chrome refuses this pair in a set (and derives the scheme from Secure)
    return param


# ---------------------------------------------------------------------- edits from the Manager


_ALIASES = {
    "name": "name", "value": "value", "domain": "domain", "path": "path", "secure": "secure",
    "httpOnly": "httpOnly", "http_only": "httpOnly", "httponly": "httpOnly",
    "sameSite": "sameSite", "same_site": "sameSite", "samesite": "sameSite",
    "priority": "priority", "sourceScheme": "sourceScheme", "source_scheme": "sourceScheme",
    "sourcePort": "sourcePort", "source_port": "sourcePort",
}
_TRUE, _FALSE = (True, "true", 1), (False, "false", 0)


def _same_expiry(value: Any, current: Any) -> bool:
    """True when an edit sends back the expiry it was shown (the view's ISO text, or the number)."""
    if not isinstance(current, (int, float)) or isinstance(current, bool) or current <= 0:
        return False
    if isinstance(value, str):
        return value.strip() == _iso(current)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value - current) < 0.001


def merge_cookie(base: Mapping[str, Any] | None, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Apply an edit (view field names - ``host_only``, ``http_only``, ``same_site``, ``session``,
    ``expires`` (ISO / unix / null), ``partition_site``, ``partition_cross_site``, ``partitioned``
    - or the portable / CDP / extension names) to ``base``, keeping every attribute the edit does
    not mention; ``base`` None makes a new cookie. An expiry sent back as it was shown keeps Chrome's
    exact value (the view has milliseconds, Chrome microseconds)."""
    if not isinstance(changes, Mapping):
        raise InvalidCookieError("The cookie must be an object.")
    if base is None:
        out: dict[str, Any] = {k: changes[k] for k in ("url", "host") if changes.get(k)}  # domain sources
    else:
        out = {k: v for k, v in base.items() if k not in ("size", "partitionKeyOpaque")}
    for key, value in changes.items():
        if key in _ALIASES:
            out[_ALIASES[key]] = value
    for key in ("host_only", "hostOnly"):
        if changes.get(key) is not None:
            host = str(out.get("domain") or out.get("host") or "").strip().lstrip(".")
            out["domain"] = host if changes[key] in _TRUE else "." + host
    if changes.get("session") in _TRUE:
        out["expires"] = None
    else:
        for key in ("expires", "expirationDate", "expiry", "expiration"):
            if key in changes:
                if base is None or not _same_expiry(changes[key], base.get("expires")):
                    out["expires"] = changes[key]
                break
        if changes.get("session") in _FALSE and out.get("expires") in (None, ""):
            raise InvalidCookieError("Pick when the cookie expires, or make it a session cookie.")
    if "partitionKey" in changes:
        out["partitionKey"] = changes["partitionKey"]
    elif changes.get("partitioned") in _FALSE:
        out.pop("partitionKey", None)
    elif "partition_site" in changes:
        site = changes.get("partition_site")
        if not site:
            out.pop("partitionKey", None)
        else:
            cross = changes.get("partition_cross_site")
            if cross is None:
                current = (base or {}).get("partitionKey")
                cross = bool(current.get("hasCrossSiteAncestor")) if isinstance(current, Mapping) and \
                    partition_site(base or {}) == site else False
            out["partitionKey"] = {"topLevelSite": str(site), "hasCrossSiteAncestor": cross in _TRUE}
    if changes.get("partitioned") in _TRUE and not out.get("partitionKey"):
        raise InvalidCookieError("A partitioned cookie needs the site it is partitioned for, e.g. https://example.com.")
    if base is not None and not any(k in changes for k in ("sourceScheme", "source_scheme", "sourcePort", "source_port")):
        # Chrome's record of where the cookie came from only stays true while Secure and the domain stay.
        if bool(out.get("secure")) != bool(base.get("secure")) or \
                str(out.get("domain") or "").lower() != str(base.get("domain") or "").lower():
            out.pop("sourceScheme", None)
            out.pop("sourcePort", None)
    return out


# ---------------------------------------------------------------------- CDP


@contextlib.asynccontextmanager
async def _connection(ws_url: str, cdp: CdpConnection | None) -> AsyncIterator[CdpConnection]:
    if cdp is not None:
        yield cdp
        return
    async with browser_connection(ws_url) as conn:
        yield conn


async def _get(cdp: CdpConnection) -> list[dict[str, Any]]:
    result = await cdp.call("Storage.getCookies", timeout=SET_TIMEOUT)
    raw = result.get("cookies")
    return [normalize_cookie(c) for c in raw if isinstance(c, Mapping)] if isinstance(raw, list) else []


def _refusal(exc: CdpError) -> bool:
    return "invalid" in str(exc).lower()


async def _write(cdp: CdpConnection, params: list[dict[str, Any]]) -> list[int]:
    """``Storage.setCookies``; returns the indexes Chrome refused. One refused cookie fails the
    whole batch (nothing is written), so a refused batch is retried one cookie at a time."""
    if not params:
        return []
    try:
        await cdp.call("Storage.setCookies", {"cookies": params}, timeout=SET_TIMEOUT)
        return []
    except CdpError as exc:
        if not _refusal(exc):
            raise
        if len(params) == 1:
            return [0]
    refused = []
    for i, param in enumerate(params):
        try:
            await cdp.call("Storage.setCookies", {"cookies": [param]}, timeout=SET_TIMEOUT)
        except CdpError as exc:
            if not _refusal(exc):
                raise
            refused.append(i)
    return refused


def _find(wanted: Mapping[str, Any], stored: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The stored cookie that ``wanted`` became. Chrome reduces a partition's site to scheme +
    registrable domain (``https://www.top.example`` -> ``https://top.example``), so a partitioned
    cookie also matches a stored one whose site is a parent of the wanted site."""
    key = cookie_key(wanted)
    for c in stored:
        if cookie_key(c) == key:
            return c
    site = partition_site(wanted)
    if not site:
        return None
    scheme, _, host = site.partition("://")
    cross = bool((wanted.get("partitionKey") or {}).get("hasCrossSiteAncestor")) \
        if isinstance(wanted.get("partitionKey"), Mapping) else False
    for c in stored:
        stored_site = partition_site(c)
        if (not stored_site or c["name"] != wanted["name"] or c["domain"] != wanted["domain"]
                or c["path"] != wanted["path"] or bool(c["partitionKey"].get("hasCrossSiteAncestor")) != cross):
            continue
        s_scheme, _, s_host = stored_site.partition("://")
        if s_scheme == scheme and host.endswith("." + s_host):
            return c
    return None


def _refused_message(verb: str, labels: list[str]) -> str:
    shown = ", ".join(labels[:5]) + (f" and {len(labels) - 5} more" if len(labels) > 5 else "")
    one = len(labels) == 1
    return (f"Chrome did not {verb} {'this cookie' if one else f'{len(labels)} cookies'}: {shown}."
            + (" It keeps no domain cookies for public suffixes such as .co.uk, and at most 180 cookies per site."
               if verb == "store" else ""))


async def _undo_strays(cdp: CdpConnection, refused: list[dict[str, Any]], before: list[dict[str, Any]],
                       jar: list[dict[str, Any]]) -> None:
    """Chrome stores a domain cookie for a public suffix (``.co.uk``) as a host-only cookie
    (``co.uk``) instead of refusing it: put that host-only cookie back as it was (or delete it)."""
    old = {cookie_key(c): c for c in before}
    now = {cookie_key(c): c for c in jar}
    restore, remove = [], []
    for wanted in refused:
        if not str(wanted["domain"]).startswith("."):
            continue
        twin = cookie_key({**wanted, "domain": wanted["domain"][1:]})
        stray = now.get(twin)
        if stray is None or stray["value"] != wanted["value"] or old.get(twin) == stray:
            continue
        (restore if twin in old else remove).append(old.get(twin) or stray)
    with contextlib.suppress(CdpError):  # best effort: the refusal is reported either way
        await _write(cdp, [_cdp_param(c) for c in restore])
    with contextlib.suppress(CdpError, CookieRefusedError):
        await _remove(cdp, remove)


async def _store(cdp: CdpConnection, cookies: list[dict[str, Any]], *,
                 before: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Write validated cookies and read them back: ``(stored cookies as Chrome has them, refused)``.
    ``before``: the jar as just read, if the caller has it."""
    unique = list({cookie_key(c): c for c in cookies}.values())  # a later duplicate wins, as in Chrome
    if before is None:
        before = await _get(cdp)
    refused_at = set(await _write(cdp, [_cdp_param(c) for c in unique]))
    jar = await _get(cdp)
    stored, refused = [], []
    for i, wanted in enumerate(unique):
        found = None if i in refused_at else _find(wanted, jar)
        if found is None or any(found[k] != wanted[k] for k in ("value", "secure", "httpOnly")):
            refused.append(wanted)
        else:
            stored.append(found)
    if refused:
        await _undo_strays(cdp, refused, before, jar)
    return stored, refused


def _plain_path(path: str) -> bool:
    """A path Chrome keeps exactly as written in a set, so that an expired copy reaches the cookie
    (a server may store ``/a b``, which a set would turn into ``/a%20b`` - another cookie's path)."""
    return bool(_PATH.match(path)) and "%" not in path and not any(seg in (".", "..") for seg in path.split("/"))


async def _delete_in_page(cdp: CdpConnection, cookies: list[dict[str, Any]]) -> bool:
    """``Network.deleteCookies`` (exact name, domain, path and partition) in a short flat session on
    a page target; no domain is enabled. False when the browser has no page to attach to."""
    found = await cdp.call("Target.getTargets", timeout=5.0)
    page = next((t for t in found.get("targetInfos") or [] if t.get("type") == "page" and t.get("targetId")), None)
    if page is None:
        return False
    attached = await cdp.call("Target.attachToTarget", {"targetId": page["targetId"], "flatten": True}, timeout=5.0)
    session_id = attached.get("sessionId")
    if not session_id:
        return False
    try:
        for c in cookies:
            params: dict[str, Any] = {"name": c["name"], "domain": c["domain"], "path": c["path"]}
            if isinstance(c.get("partitionKey"), Mapping):
                params["partitionKey"] = dict(c["partitionKey"])
            await cdp.call("Network.deleteCookies", params, session_id=session_id, timeout=SET_TIMEOUT)
    finally:
        with contextlib.suppress(CdpError):
            await cdp.call("Target.detachFromTarget", {"sessionId": session_id}, timeout=2.0)
    return True


async def _remove(cdp: CdpConnection, targets: list[dict[str, Any]]) -> None:
    """Delete exactly these cookies: each by an already expired copy of itself, or - for a path a
    copy cannot reach - with :func:`_delete_in_page`. Raises :class:`CookieRefusedError` for any
    that are still there (also cookies partitioned for an opaque site, which nothing can address)."""
    if not targets:
        return
    keys = {cookie_key(c) for c in targets}
    copies = [c for c in targets if not c.get("partitionKeyOpaque") and _plain_path(c["path"])]
    await _write(cdp, [_cdp_param(c, expires=EXPIRED) for c in copies])
    left = [c for c in await _get(cdp) if cookie_key(c) in keys]
    reachable = [c for c in left if not c.get("partitionKeyOpaque")]
    if reachable and await _delete_in_page(cdp, reachable):
        left = [c for c in await _get(cdp) if cookie_key(c) in keys]
    if left:
        labels = [cookie_label(c) for c in left]
        raise CookieRefusedError(_refused_message("delete", labels), refused=labels, done=len(targets) - len(left))


# ---------------------------------------------------------------------- public API


async def list_cookies(ws_url: str, *, cdp: CdpConnection | None = None) -> list[dict[str, Any]]:
    """Every cookie of the browser (``Storage.getCookies``), normalised, sorted by site / name / path."""
    async with _connection(ws_url, cdp) as conn:
        return sorted(await _get(conn), key=_sort_key)


async def set_cookies(ws_url: str, cookies: Iterable[Mapping[str, Any]], *, cdp: CdpConnection | None = None) -> int:
    """Add or replace cookies (``Storage.setCookies``). All of them are validated first (nothing is
    sent if one is invalid); returns how many distinct cookies Chrome stored. Raises
    :class:`InvalidCookieError` / :class:`CookieRefusedError` (``done`` = how many were stored)."""
    items = list(cookies)
    if len(items) > MAX_BATCH:
        raise InvalidCookieError(f"Too many cookies at once ({len(items)}; at most {MAX_BATCH}).")
    now = time.time()
    valid = []
    for i, cookie in enumerate(items, 1):
        try:
            valid.append(validate_cookie(cookie, now=now))
        except InvalidCookieError as exc:
            if len(items) == 1:
                raise
            raise InvalidCookieError(f"Cookie #{i} ({_input_label(cookie)}): {exc}") from None
    if not valid:
        return 0
    async with _connection(ws_url, cdp) as conn:
        stored, refused = await _store(conn, valid)
    if refused:
        labels = [cookie_label(c) for c in refused]
        raise CookieRefusedError(_refused_message("store", labels), refused=labels, done=len(stored))
    return len(stored)


async def delete_cookies(ws_url: str, keys: Iterable[str], *, cdp: CdpConnection | None = None) -> int:
    """Delete the cookies with these :func:`cookie_key` ids, and nothing else. Returns how many of
    them existed (unknown ids are ignored)."""
    wanted = {str(k) for k in keys}
    if not wanted:
        return 0
    async with _connection(ws_url, cdp) as conn:
        targets = [c for c in await _get(conn) if cookie_key(c) in wanted]
        await _remove(conn, targets)
    return len(targets)


async def clear_cookies(ws_url: str, *, domain: str | None = None, cdp: CdpConnection | None = None) -> int:
    """Delete all cookies (``Storage.clearCookies``), or those of ``domain`` and its subdomains
    (except cookies partitioned for an opaque site, which only clearing everything reaches).
    Returns how many were deleted."""
    site = filter_domain(domain) if domain is not None else None
    async with _connection(ws_url, cdp) as conn:
        if site is None:
            count = len(await _get(conn))
            await conn.call("Storage.clearCookies", timeout=SET_TIMEOUT)
            return count
        targets = [c for c in await _get(conn) if domain_matches(c["domain"], site) and not c.get("partitionKeyOpaque")]
        await _remove(conn, targets)
    return len(targets)


async def save_cookie(ws_url: str, cookie: Mapping[str, Any], *, replace: str | None = None,
                      overwrite: bool = False, cdp: CdpConnection | None = None) -> dict[str, Any]:
    """Create a cookie, or edit the one whose :func:`cookie_key` is ``replace`` (``cookie`` then only
    needs the changed fields, see :func:`merge_cookie`). A changed identity sets the new cookie first
    and deletes the old one only after Chrome stored the new one. Returns the cookie as stored.
    Raises :class:`~profilepilot.errors.NotFoundError` when ``replace`` no longer exists, and
    :class:`CookieExistsError` when an edit would land on another existing cookie (a host-only and
    a domain ``sid`` merging into one) unless ``overwrite``."""
    new = validate_cookie(merge_cookie(None, cookie)) if replace is None else None  # before connecting
    async with _connection(ws_url, cdp) as conn:
        jar = await _get(conn)
        base = None
        if replace is not None:
            base = next((c for c in jar if cookie_key(c) == replace), None)
            if base is None:
                raise NotFoundError("That cookie no longer exists (it expired or was deleted). Refresh the list.")
            if base.get("partitionKeyOpaque"):
                raise InvalidCookieError("Cookies partitioned for an opaque site can't be edited; delete it instead.")
        valid = new if new is not None else validate_cookie(merge_cookie(base, cookie))
        if base is not None and not overwrite and cookie_key(valid) != replace:
            other = next((c for c in jar if cookie_key(c) == cookie_key(valid)), None)
            if other is not None:
                host = str(other["domain"]).lstrip(".")
                scope = f"for {host} and its subdomains" if str(other["domain"]).startswith(".") else f"for {host} only"
                path = f", path {other['path']}" if other["path"] != "/" else ""
                raise CookieExistsError(f"There already is a cookie '{other['name']}' {scope}{path}. Saving this edit "
                                        "would replace it with the edited one; delete or edit that cookie instead, "
                                        "or confirm to replace it.", existing=cookie_label(other))
        stored, refused = await _store(conn, [valid], before=jar)
        if refused:
            labels = [cookie_label(valid)]
            raise CookieRefusedError(_refused_message("store", labels), refused=labels)
        saved = stored[0]
        if base is not None and cookie_key(saved) != replace:
            await _remove(conn, [base])
        return saved


@dataclass
class ParsedCookies:
    """A cookie file or pasted text, ready to import (see :func:`parse_import`)."""

    format: CookieFormat
    cookies: list[dict[str, Any]] = field(default_factory=list)
    """Validated cookies (the last of several with one identity)."""
    problems: list[str] = field(default_factory=list)
    """Entries that were left out and why (by position and name, never values)."""
    skipped: int = 0
    """Entries left out: problems plus cookies of other domains (``domain=``)."""


def parse_import(text: str, fmt: CookieFormat | None = None, *, domain: str | None = None,
                 now: float | None = None) -> ParsedCookies:
    """Parse and check cookie text (JSON in any accepted shape, or cookies.txt; detected unless
    ``fmt``) without failing on single bad entries: they become ``problems``. ``domain`` keeps only
    the cookies of that domain and its subdomains."""
    detected, portable, problems = parse_cookies_report(text, fmt)
    site = filter_domain(domain) if domain else None
    now = time.time() if now is None else now
    result = ParsedCookies(format=detected, problems=list(problems), skipped=len(problems))
    by_key: dict[str, dict[str, Any]] = {}
    for cookie in portable:
        if site and not domain_matches(str(cookie.get("domain") or ""), site):
            result.skipped += 1
            continue
        try:
            valid = validate_cookie(cookie, now=now)
        except InvalidCookieError as exc:
            result.problems.append(f"Cookie {cookie_label(cookie)}: {exc}")
            result.skipped += 1
            continue
        key = cookie_key(valid)
        if key in by_key:
            result.skipped += 1
            by_key.pop(key)
        by_key[key] = valid
    result.cookies = list(by_key.values())
    return result


@dataclass
class ImportResult:
    imported: int
    """Cookies Chrome stored."""
    refused: list[str] = field(default_factory=list)
    """Labels of the cookies Chrome did not store."""
    removed: int = 0
    """Cookies deleted by a replace import."""
    not_removed: list[str] = field(default_factory=list)
    """Labels of older cookies a replace import could not delete (the new ones are stored anyway)."""


async def import_cookies(ws_url: str, cookies: Iterable[Mapping[str, Any]], *, mode: ImportMode = "merge",
                         cdp: CdpConnection | None = None) -> ImportResult:
    """Write cookies (validated by :func:`parse_import` or raw). ``merge`` keeps everything else;
    ``replace`` then deletes the other cookies of the imported sites (each site and its subdomains);
    ``replace_all`` deletes every other cookie. Old cookies are only removed after the new ones were
    written, so a refused import never empties the jar."""
    if mode not in ("merge", "replace", "replace_all"):
        raise InvalidCookieError("mode must be merge, replace or replace_all.")
    items = list(cookies)
    if len(items) > MAX_BATCH:
        raise InvalidCookieError(f"Too many cookies at once ({len(items)}; at most {MAX_BATCH}).")
    now = time.time()
    valid = [validate_cookie(c, now=now) for c in items]
    async with _connection(ws_url, cdp) as conn:
        stored, refused = await _store(conn, valid)
        removed, not_removed = 0, []
        if mode != "merge" and stored:
            keep = {cookie_key(c) for c in stored + refused}  # never drop an old cookie that was not replaced
            # Only sites that really received cookies are replaced: a site whose cookies Chrome refused
            # keeps its old ones (a refused login must not log the profile out of that site).
            sites = {str(c["domain"]).lstrip(".") for c in stored}
            current = await _get(conn)
            stale = [c for c in current if cookie_key(c) not in keep and not c.get("partitionKeyOpaque")
                     and (mode == "replace_all" or any(domain_matches(c["domain"], s) for s in sites))]
            try:
                await _remove(conn, stale)
                removed = len(stale)
            except CookieRefusedError as exc:  # the new cookies are stored: report, do not fail the import
                removed, not_removed = exc.done, list(exc.refused)
    return ImportResult(imported=len(stored), refused=[cookie_label(c) for c in refused], removed=removed,
                        not_removed=not_removed)


__all__ = [
    "CookieExistsError",
    "CookieRefusedError",
    "ImportResult",
    "InvalidCookieError",
    "ParsedCookies",
    "clear_cookies",
    "cookie_key",
    "cookie_label",
    "cookie_view",
    "delete_cookies",
    "domain_counts",
    "filter_domain",
    "import_cookies",
    "list_cookies",
    "merge_cookie",
    "normalize_cookie",
    "parse_import",
    "parse_key",
    "save_cookie",
    "set_cookies",
    "validate_cookie",
]
