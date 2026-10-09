"""Cookie conversions and cookie files.

Three shapes are involved:

* **Playwright cookies** - what ``BrowserContext.cookies()`` returns
  (``{name, value, domain, path, expires(-1 = session), httpOnly, secure, sameSite, partitionKey?}``)
  and what ``BrowserContext.add_cookies()`` accepts (``SetCookieParam``).
* **Portable JSON** - the ShardX/ShardBrowser shape used for files and tool input:
  ``{domain, name, value, path, expires (unix seconds | null), secure, httpOnly, sameSite}``.
  On input the common browser-extension export shape (Cookie-Editor / EditThisCookie:
  ``expirationDate``, ``hostOnly``, ``session``, ``sameSite: "no_restriction"``), ``http_only`` /
  ``same_site`` aliases and Playwright ``storage_state`` files are accepted too.
* **Netscape cookies.txt** - the curl / wget / yt-dlp format, including ``#HttpOnly_`` lines.

Cookies always move through CDP (never the SQLite file). Cookie *values* are secrets: they are
written to files on request but :func:`cookie_summary` - the model-facing view - omits them.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import math
import os
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from ..errors import ProfilePilotError

log = logging.getLogger("profilepilot.cookies")

CookieFormat = Literal["json", "netscape"]
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"
_SAME_SITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}


class CookieFormatError(ProfilePilotError, ValueError):
    """A cookie file or cookie object could not be understood."""


# ---------------------------------------------------------------------- field helpers


def _same_site(value: Any) -> str | None:
    if value is None:
        return None
    return _SAME_SITE.get(str(value).strip().lower())


def _expires(value: Any) -> int | None:
    """Normalise an expiry to unix seconds, or None for a session cookie."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            return None
        return int(value)
    text = str(value).strip()
    if not text or text.lower() in ("session", "-1", "0", "null", "none"):
        return None
    try:
        return _expires(float(text))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise CookieFormatError("Unrecognised cookie expiry.") from None  # never echo file content
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _first(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


# ---------------------------------------------------------------------- conversions


def to_portable(cookie: Mapping[str, Any]) -> dict[str, Any]:
    """Playwright (or any accepted) cookie -> portable JSON shape (ShardX compatible)."""
    if not isinstance(cookie, Mapping):
        raise CookieFormatError(f"A cookie must be an object, got {type(cookie).__name__}.")
    name = _first(cookie, "name")
    if not name:
        raise CookieFormatError("Cookie is missing its 'name'.")
    domain = str(_first(cookie, "domain", "host") or "").strip()
    url = _first(cookie, "url")
    if not domain and url:
        domain = (urlsplit(str(url)).hostname or "").lower()
    if not domain:
        raise CookieFormatError(f"Cookie '{name}' needs a 'domain' (or 'url').")
    host_only = cookie.get("hostOnly", cookie.get("host_only"))
    if host_only is not None:
        domain = domain.lstrip(".") if _bool(host_only) else "." + domain.lstrip(".")
    expires = None if _bool(cookie.get("session", False)) else _expires(
        _first(cookie, "expires", "expirationDate", "expiry", "expiration")
    )
    out: dict[str, Any] = {
        "domain": domain,
        "name": str(name),
        "value": "" if cookie.get("value") is None else str(cookie.get("value")),
        "path": str(_first(cookie, "path") or "/"),
        "expires": expires,
        "secure": _bool(_first(cookie, "secure") or False),
        "httpOnly": _bool(_first(cookie, "httpOnly", "http_only", "httponly") or False),
        "sameSite": _same_site(_first(cookie, "sameSite", "same_site", "samesite")),
    }
    partition = cookie.get("partitionKey")
    if isinstance(partition, str) and partition:
        out["partitionKey"] = partition
    return out


def to_playwright(cookie: Mapping[str, Any]) -> dict[str, Any]:
    """Any accepted cookie shape -> Playwright ``SetCookieParam`` for ``context.add_cookies``."""
    portable = to_portable(cookie)
    out: dict[str, Any] = {
        "name": portable["name"],
        "value": portable["value"],
        "domain": portable["domain"],
        "path": portable["path"],
        "secure": portable["secure"],
        "httpOnly": portable["httpOnly"],
    }
    if portable["expires"] is not None:
        out["expires"] = float(portable["expires"])
    same_site = portable["sameSite"]
    if same_site == "None" and not portable["secure"]:
        # Chrome rejects SameSite=None without Secure; fall back to the browser default (Lax).
        log.debug("dropping SameSite=None from non-secure cookie %s", portable["name"])
        same_site = None
    if same_site:
        out["sameSite"] = same_site
    if portable.get("partitionKey"):
        out["partitionKey"] = portable["partitionKey"]
    return out


def to_playwright_list(cookies: Iterable[Mapping[str, Any]], *, quiet: bool = False) -> list[dict[str, Any]]:
    """Convert many cookies, naming the offending entry (by position) on error.

    ``quiet`` drops the detail of the error (it may quote a cookie name from the input): used for
    files, whose content must not reach a model."""
    out = []
    for i, cookie in enumerate(cookies):
        try:
            out.append(to_playwright(cookie))
        except CookieFormatError as exc:
            detail = "missing or invalid name, domain or expiry" if quiet else str(exc)
            raise CookieFormatError(f"Cookie #{i + 1}: {detail}") from None
    return out


def to_cookiejar(cookies: Iterable[Mapping[str, Any]], jar: http.cookiejar.CookieJar | None = None) -> http.cookiejar.CookieJar:
    """Build an ``http.cookiejar.CookieJar`` (domain/path scoping preserved) from cookies."""
    jar = jar if jar is not None else http.cookiejar.CookieJar()
    for raw in cookies:
        c = to_portable(raw)
        domain = c["domain"]
        dotted = domain.startswith(".")
        rest: dict[str, str | None] = {}
        if c["httpOnly"]:
            rest["HttpOnly"] = None
        if c["sameSite"]:
            rest["SameSite"] = c["sameSite"]
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0, name=c["name"], value=c["value"], port=None, port_specified=False,
                domain=domain, domain_specified=dotted, domain_initial_dot=dotted,
                path=c["path"], path_specified=True, secure=c["secure"], expires=c["expires"],
                discard=c["expires"] is None, comment=None, comment_url=None, rest=rest,
            )
        )
    return jar


def from_cookiejar(jar: Iterable[http.cookiejar.Cookie]) -> list[dict[str, Any]]:
    """``http.cookiejar`` cookies -> portable JSON shape."""
    out = []
    for c in jar:
        same_site = None
        http_only = False
        if hasattr(c, "_rest"):
            rest = {k.lower(): v for k, v in c._rest.items()}  # type: ignore[attr-defined]
            http_only = "httponly" in rest
            same_site = _same_site(rest.get("samesite"))
        out.append({
            "domain": c.domain,
            "name": c.name,
            "value": c.value or "",
            "path": c.path or "/",
            "expires": _expires(c.expires),
            "secure": bool(c.secure),
            "httpOnly": http_only,
            "sameSite": same_site,
        })
    return out


# ---------------------------------------------------------------------- Netscape format


def to_netscape(cookies: Iterable[Mapping[str, Any]]) -> str:
    """Render cookies as a Netscape ``cookies.txt`` document."""
    lines = [NETSCAPE_HEADER, "# Exported by ProfilePilot. This file contains secrets - keep it private.", ""]
    for raw in cookies:
        c = to_portable(raw)
        domain = c["domain"]
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        prefix = "#HttpOnly_" if c["httpOnly"] else ""
        fields = [
            prefix + domain, include_sub, c["path"], "TRUE" if c["secure"] else "FALSE",
            str(c["expires"] or 0), c["name"], c["value"],
        ]
        if any(("\t" in f or "\n" in f or "\r" in f) for f in fields):
            raise CookieFormatError(f"Cookie '{c['name']}' contains tabs or newlines and cannot be written as cookies.txt.")
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"


def parse_netscape(text: str) -> list[dict[str, Any]]:
    """Parse a Netscape ``cookies.txt`` document into portable cookies."""
    cookies: list[dict[str, Any]] = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip("\r\n")
        if not line.strip():
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]
        elif line.lstrip().startswith("#"):
            continue
        # Tabs only: a whitespace fallback would turn any line of prose into a "cookie".
        fields = line.split("\t")
        if len(fields) == 6:
            fields.append("")
        if len(fields) != 7:
            raise CookieFormatError(f"cookies.txt line {lineno}: expected 7 tab-separated fields, got {len(fields)}.")
        domain, include_sub, path, secure, expires, name, value = fields
        domain = domain.strip()
        if not domain or not name:
            raise CookieFormatError(f"cookies.txt line {lineno}: missing domain or name.")
        if include_sub.strip().upper() == "TRUE":
            domain = "." + domain.lstrip(".")
        else:
            domain = domain.lstrip(".")
        try:
            exp = _expires(int(expires.strip() or 0))
        except ValueError:
            raise CookieFormatError(f"cookies.txt line {lineno}: invalid expiry.") from None  # no file content
        cookies.append({
            "domain": domain,
            "name": name,
            "value": value,
            "path": path or "/",
            "expires": exp,
            "secure": secure.strip().upper() == "TRUE",
            "httpOnly": http_only,
            "sameSite": None,
        })
    return cookies


# ---------------------------------------------------------------------- JSON / detection


def parse_json_cookies(data: Any, *, quiet: bool = False) -> list[dict[str, Any]]:
    """Portable cookies from decoded JSON: a list, ``{"cookies": [...]}`` (ShardX API,
    Playwright ``storage_state``) or a single cookie object. ``quiet``: see :func:`to_playwright_list`."""
    if isinstance(data, Mapping):
        if isinstance(data.get("cookies"), list):
            data = data["cookies"]
        elif "name" in data:
            data = [data]
        else:
            raise CookieFormatError("JSON cookie data must be a list of cookies or an object with a 'cookies' list.")
    if not isinstance(data, list):
        raise CookieFormatError("JSON cookie data must be a list of cookies.")
    out = []
    for i, item in enumerate(data):
        try:
            out.append(to_portable(item))
        except CookieFormatError as exc:
            detail = "missing or invalid name, domain or expiry" if quiet else str(exc)
            raise CookieFormatError(f"Cookie #{i + 1}: {detail}") from None
    return out


def detect_format(text: str) -> CookieFormat:
    """Guess whether ``text`` is JSON or Netscape cookies.txt."""
    stripped = text.lstrip("﻿ \t\r\n")
    if stripped.startswith(("[", "{")):
        return "json"
    return "netscape"


def parse_cookies_text(text: str, fmt: CookieFormat | None = None, *, quiet: bool = False) -> list[dict[str, Any]]:
    """Parse cookie text (format auto-detected unless given) into portable cookies."""
    text = text.lstrip("﻿")
    fmt = fmt or detect_format(text)
    if fmt == "json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CookieFormatError(f"Invalid JSON cookie file: {exc.msg} (line {exc.lineno}).") from None
        return parse_json_cookies(data, quiet=quiet)
    if fmt == "netscape":
        return parse_netscape(text)
    raise CookieFormatError(f"Unknown cookie format {fmt!r}; use 'json' or 'netscape'.")


def format_for_path(path: Path | str) -> CookieFormat:
    """``.txt`` / ``.cookies`` -> netscape, anything else -> json."""
    return "netscape" if Path(path).suffix.lower() in (".txt", ".cookies") else "json"


def dumps_cookies(cookies: Iterable[Mapping[str, Any]], fmt: CookieFormat = "json") -> str:
    if fmt == "json":
        return json.dumps([to_portable(c) for c in cookies], indent=2, ensure_ascii=False) + "\n"
    if fmt == "netscape":
        return to_netscape(cookies)
    raise CookieFormatError(f"Unknown cookie format {fmt!r}; use 'json' or 'netscape'.")


def export_cookies(cookies: Iterable[Mapping[str, Any]], path: Path | str, fmt: CookieFormat | None = None, *,
                   create_parents: bool = True) -> Path:
    """Write cookies to ``path`` (JSON list or Netscape cookies.txt; inferred from the suffix when
    ``fmt`` is None). The file is created with owner-only permissions where the OS supports it.
    ``create_parents=False`` refuses to create missing folders."""
    target = Path(path).expanduser()
    fmt = fmt or format_for_path(target)
    payload = dumps_cookies(list(cookies), fmt)
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.is_dir():
        raise CookieFormatError(f"The folder {target.parent} does not exist.")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if sys.platform != "win32":
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    return target


def load_cookie_file(path: Path | str, fmt: CookieFormat | None = None) -> list[dict[str, Any]]:
    """Read a cookie file (format auto-detected) and return Playwright ``SetCookieParam`` dicts,
    ready for ``BrowserContext.add_cookies``."""
    source = Path(path).expanduser()
    try:
        raw = source.read_bytes()
    except FileNotFoundError:
        raise CookieFormatError(f"Cookie file not found: {source}") from None
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise CookieFormatError(f"Cookie file is not UTF-8 text: {source}")
    # quiet: the file may be anything; its content must not be echoed in error messages
    return to_playwright_list(parse_cookies_text(text, fmt, quiet=True), quiet=True)


# ---------------------------------------------------------------------- model-facing views


def domain_matches(cookie_domain: str, domain: str) -> bool:
    """True if a cookie stored for ``cookie_domain`` belongs to ``domain`` or one of its subdomains."""
    cd = (cookie_domain or "").lstrip(".").lower()
    d = (domain or "").strip().lstrip(".").lower()
    return bool(d) and (cd == d or cd.endswith("." + d))


def filter_cookies(cookies: Iterable[Mapping[str, Any]], *, domain: str | None = None) -> list[dict[str, Any]]:
    return [dict(c) for c in cookies if domain is None or domain_matches(str(c.get("domain", "")), domain)]


def cookie_summary(cookies: Iterable[Mapping[str, Any]], *, names_only: bool = False,
                   include_values: bool = False) -> list[dict[str, Any]]:
    """Model-safe view of cookies: no values unless ``include_values`` (explicit opt-in)."""
    now = time.time()
    out = []
    for raw in cookies:
        c = to_portable(raw)
        if names_only:
            out.append({"domain": c["domain"], "name": c["name"]})
            continue
        item: dict[str, Any] = {
            "domain": c["domain"],
            "name": c["name"],
            "path": c["path"],
            "expires": (
                datetime.fromtimestamp(c["expires"], timezone.utc).isoformat().replace("+00:00", "Z")
                if c["expires"] else "session"
            ),
            "secure": c["secure"],
            "httpOnly": c["httpOnly"],
        }
        if c["sameSite"]:
            item["sameSite"] = c["sameSite"]
        if c["expires"] and c["expires"] < now:
            item["expired"] = True
        if include_values:
            item["value"] = c["value"]
        else:
            item["value_length"] = len(c["value"])
        out.append(item)
    return out
