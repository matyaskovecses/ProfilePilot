"""The HTTP identity of a profile's running browser, for requests made next to the browser.

``http_fetch`` (both engines) and the Scrapling ``fetcher_session`` send requests on the profile's
IP and cookies, so they must look like the browser that runs the profile - Chrome, Edge, Brave,
Chromium or a pre-release channel - and never like another browser or OS
(docs/FINGERPRINT-AUDIT.md F9). Nothing here is made up; every value is read from that browser:

* ``User-Agent`` from ``Browser.getVersion`` (the browser's own default user agent);
* ``sec-ch-ua``, ``sec-ch-ua-mobile`` and ``sec-ch-ua-platform`` from ``navigator.userAgentData``
  (``getHighEntropyValues(["fullVersionList", "platformVersion"])``, which also returns the
  low-entropy brands, mobile flag and platform), serialised the way Chrome sends them;
* ``Accept-Language`` from ``navigator.languages``, in Chrome's format (``q=0.9``, ``0.8`` ...).

:func:`read_http_identity` reads them over a short raw CDP connection of its own, once per running
browser (callers cache by :func:`identity_key`). ``navigator.userAgentData`` exists only in secure
contexts - not on ``about:blank``, where a profile starts - so the page values come from a *hidden*
target (``Target.createTarget(hidden=True)``) on ``chrome://version``: a secure page that needs no
network, is in no window or tab strip, is ignored by the CDP driver (target type ``other``, so it never
shows up as a tab) and is closed right after. It runs ``IDENTITY_JS`` in an isolated world
(``Page.createIsolatedWorld`` + ``Runtime.evaluate``: no ``Runtime.enable``). No site's page is touched;
only if hidden targets are not available (an older browser, or chrome:// pages blocked by policy) is
an open https / loopback tab read the same way. Without any secure context the client hints are left
out (Chrome sends none to an insecure origin either) and read again next time.

curl_cffi (``engine="scrapling"``) also needs a TLS / HTTP/2 target: :func:`impersonate_target`
picks the closest one curl_cffi has for the browser's brand and version. Its built-in request headers
for that target (a macOS Chrome) are then replaced by the browser's own; without client hints they
are removed (:func:`curl_headers`). The TLS ClientHello stays curl_cffi's: close to, but not the same
as, the browser's (documented residual).
"""

from __future__ import annotations

import asyncio
import logging
import re
import typing
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

log = logging.getLogger("profilepilot.automation")

CLIENT_HINT_HEADERS = ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")
"""The low-entropy client hints Chrome sends with every request to a secure origin."""

WORLD_NAME = "profilepilot-identity"
HIDDEN_PAGE = "chrome://version/"
"""A secure context that needs no network (Edge shows it as edge://version, Brave as brave://version)."""
READ_TIMEOUT_S = 3.0
MAX_TABS = 3

# One evaluate in an isolated world of the hidden page (or of a secure tab; see the module docstring).
IDENTITY_JS = """(async () => {
  const data = navigator.userAgentData;
  let hints = null;
  if (data) {
    try {
      hints = await data.getHighEntropyValues(["fullVersionList", "platformVersion"]);
    } catch (e) {
      hints = {brands: data.brands, mobile: data.mobile, platform: data.platform};
    }
    hints = JSON.parse(JSON.stringify(hints));
  }
  return {languages: Array.from(navigator.languages || []), hints};
})()"""

_LANG_RE = re.compile(r"^[A-Za-z0-9-]+$")
_TARGET_RE = re.compile(r"^(chrome|edge)(\d+)$")
# Brands of navigator.userAgentData -> browser family (the GREASE brand and "Chromium" are skipped).
_BRAND_FAMILIES = (("microsoft edge", "edge"), ("brave", "brave"), ("google chrome", "chrome"))


def _pairs(items: Any, key: str) -> tuple[tuple[str, str], ...]:
    out = []
    for item in items or ():
        if isinstance(item, Mapping) and isinstance(item.get("brand"), str) and isinstance(item.get(key), str):
            out.append((item["brand"], item[key]))
    return tuple(out)


def _quoted(value: str) -> str:
    """An RFC 8941 string, as in Chrome's ``sec-ch-ua`` (brands never contain quotes, but be safe)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def expand_languages(languages: Iterable[str]) -> list[str]:
    """Chrome's ``ExpandLanguageList`` (net/http/http_util.cc): after each run of languages that share
    a base language, the base itself is added unless the list already has it - so
    ``navigator.languages == ["en-US"]`` is sent as ``en-US,en`` (verified on Chrome 154)."""
    langs = [str(x) for x in languages or () if isinstance(x, str) and x and _LANG_RE.match(x)]
    present = {lang.lower() for lang in langs}
    out: list[str] = []
    for index, lang in enumerate(langs):
        if lang.lower() not in {o.lower() for o in out}:
            out.append(lang)
        base = lang.split("-", 1)[0]
        next_base = langs[index + 1].split("-", 1)[0].lower() if index + 1 < len(langs) else None
        if base.lower() != lang.lower() and base.lower() not in present and next_base != base.lower():
            out.append(base)
            present.add(base.lower())
    return out


def accept_language(languages: Iterable[str]) -> str | None:
    """``Accept-Language`` exactly as Chrome builds it from ``navigator.languages``: expanded with base
    languages (:func:`expand_languages`), the first without a weight, then ``q=0.9``, ``0.8`` ...
    (never below 0.1)."""
    langs = expand_languages(languages)
    if not langs:
        return None
    return ",".join([langs[0]] + [f"{lang};q={max(0.1, 1 - 0.1 * i):.1f}" for i, lang in enumerate(langs[1:], 1)])


@dataclass(frozen=True)
class HttpIdentity:
    """What the running browser sends about itself (see the module docstring)."""

    user_agent: str
    product: str = ""
    """``Browser.getVersion`` ``product``, e.g. ``Chrome/154.0.8037.98`` (Edge reports ``Chrome/`` too)."""
    brands: tuple[tuple[str, str], ...] = ()
    """``navigator.userAgentData.brands`` as (brand, major version), in the browser's order."""
    full_version_list: tuple[tuple[str, str], ...] = ()
    mobile: bool = False
    platform: str = ""
    platform_version: str = ""
    languages: tuple[str, ...] = ()

    @classmethod
    def from_values(cls, user_agent: str, product: str = "", hints: Mapping[str, Any] | None = None,
                    languages: Iterable[str] = ()) -> "HttpIdentity":
        hints = hints or {}
        return cls(
            user_agent=user_agent, product=product,
            brands=_pairs(hints.get("brands"), "version"),
            full_version_list=_pairs(hints.get("fullVersionList"), "version"),
            mobile=bool(hints.get("mobile")),
            platform=str(hints.get("platform") or ""),
            platform_version=str(hints.get("platformVersion") or ""),
            languages=tuple(str(x) for x in languages or () if isinstance(x, str)),
        )

    @property
    def has_client_hints(self) -> bool:
        return bool(self.brands and self.platform)

    @property
    def family(self) -> str:
        """``edge``, ``brave``, ``chrome`` or ``chromium``: from the brands, else from the user agent."""
        names = [brand.lower() for brand, _ in self.brands]
        for name, family in _BRAND_FAMILIES:
            if name in names:
                return family
        if " Edg/" in self.user_agent:
            return "edge"
        return "chromium" if "chromium" in names else "chrome"

    @property
    def major(self) -> int | None:
        """The Chromium major version (``product``, else the ``Chromium`` brand, else the user agent)."""
        for text in (self.product, dict(self.brands).get("Chromium", ""), self.user_agent):
            match = re.search(r"(?:Chrome/|^)(\d+)", text or "")
            if match:
                return int(match.group(1))
        return None

    def sec_ch_ua(self) -> str:
        return ", ".join(f"{_quoted(brand)};v={_quoted(version)}" for brand, version in self.brands)

    def accept_language(self) -> str | None:
        return accept_language(self.languages)

    def headers(self) -> dict[str, str]:
        """The identity headers, named as Chrome names them."""
        out = {"User-Agent": self.user_agent}
        if self.has_client_hints:
            out["sec-ch-ua"] = self.sec_ch_ua()
            out["sec-ch-ua-mobile"] = "?1" if self.mobile else "?0"
            out["sec-ch-ua-platform"] = _quoted(self.platform)
        languages = self.accept_language()
        if languages:
            out["Accept-Language"] = languages
        return out

    def describe(self) -> str:
        """Short model-facing summary, e.g. ``Microsoft Edge 154 on Windows``."""
        brand = next((b for b, _ in self.brands if b.lower() not in ("chromium",) and "brand" not in b.lower()), "")
        name = brand or {"edge": "Microsoft Edge"}.get(self.family, "Chrome")
        major = self.major
        return f"{name}{f' {major}' if major else ''}{f' on {self.platform}' if self.platform else ''}"


def merge_headers(base: Mapping[str, Any], override: Mapping[str, Any] | None) -> dict[str, Any]:
    """``base`` updated with ``override``, header names compared case-insensitively (``override``'s
    spelling wins)."""
    out = dict(base)
    for key, value in (override or {}).items():
        for existing in [k for k in out if k.lower() == str(key).lower()]:
            del out[existing]
        out[str(key)] = value
    return out


def curl_headers(identity: HttpIdentity) -> dict[str, str | None]:
    """The identity as curl_cffi session headers: they replace the impersonation target's built-in
    headers of the same name in place; ``None`` removes a built-in client hint the browser would not
    send (no client hints could be read)."""
    headers: dict[str, str | None] = dict(identity.headers())
    if not identity.has_client_hints:
        headers.update({name: None for name in CLIENT_HINT_HEADERS})
    return headers


# ---------------------------------------------------------------------- curl_cffi targets


def curl_targets() -> list[str]:
    """curl_cffi's impersonation targets (empty when curl_cffi is not installed)."""
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
    except ImportError:
        return []
    return [str(name) for name in typing.get_args(BrowserTypeLiteral)]


def impersonate_target(family: str, major: int | None, targets: Iterable[str] | None = None) -> str:
    """The curl_cffi target closest to a Chromium-family browser: the newest Chrome target (for Edge:
    Chrome or Edge target, an Edge target winning a tie) that is not newer than the browser; for a
    browser older than every target, the oldest one. Edge, Brave and Chromium use Chrome's network
    stack, so a newer Chrome target is closer to them than an older Edge target."""
    available = curl_targets() if targets is None else list(targets)
    families = ("chrome", "edge") if family == "edge" else ("chrome",)
    found = []
    for name in available:
        match = _TARGET_RE.match(name)
        if match and match.group(1) in families:
            found.append((int(match.group(2)), match.group(1) == "edge", name))
    if not found:
        return "chrome"  # curl_cffi's alias for its newest Chrome target
    pool = [f for f in found if major is None or f[0] <= major] or [min(found)]
    return max(pool)[2]


# ---------------------------------------------------------------------- reading it from the browser


def identity_key(key: str, runtime: Any = None, session: Any = None) -> str:
    """Cache key of one running browser: the profile's key plus its browser process (a restart, or a
    profile switched to another browser, gets a new identity)."""
    pid = getattr(runtime, "chrome_pid", None)
    if pid:
        return f"{key}:{pid}:{getattr(runtime, 'chrome_create_time', None)}"
    return f"{key}:{id(session)}"


def _secure_tab(url: str) -> bool:
    """Tabs whose document is a secure context (``navigator.userAgentData`` exists there)."""
    lowered = (url or "").lower()
    return lowered.startswith("https:") or bool(
        re.match(r"^http://(127\.\d+\.\d+\.\d+|localhost|\[::1\]|[^/:]+\.localhost)(:\d+)?(/|$)", lowered))


async def _evaluate_isolated(cdp: Any, session_id: str) -> Any:
    """:data:`IDENTITY_JS` in a new isolated world of the target's main frame."""
    tree = await cdp.call("Page.getFrameTree", session_id=session_id, timeout=READ_TIMEOUT_S)
    frame_id = tree["frameTree"]["frame"]["id"]
    world = await cdp.call("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": WORLD_NAME,
                                                        "grantUniveralAccess": False},
                           session_id=session_id, timeout=READ_TIMEOUT_S)
    result = await cdp.call("Runtime.evaluate", {"expression": IDENTITY_JS, "contextId": world["executionContextId"],
                                                 "awaitPromise": True, "returnByValue": True},
                            session_id=session_id, timeout=READ_TIMEOUT_S)
    if result.get("exceptionDetails"):
        return None
    value = (result.get("result") or {}).get("value")
    return value if isinstance(value, dict) else None


async def _from_hidden_page(cdp: Any) -> dict[str, Any] | None:
    """Read the values in a hidden target on ``chrome://version`` (a secure context that needs no
    network): it is in no window or tab strip, and the CDP driver ignores it (target type ``other``)."""
    target = await cdp.call("Target.createTarget", {"url": HIDDEN_PAGE, "background": True, "hidden": True},
                            timeout=READ_TIMEOUT_S)
    target_id = target["targetId"]
    try:
        attached = await cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True},
                                  timeout=READ_TIMEOUT_S)
        session_id = attached["sessionId"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READ_TIMEOUT_S
        while True:  # the new target starts on about:blank (not a secure context) before chrome://version commits
            try:
                value = await _evaluate_isolated(cdp, session_id)
            except Exception:  # the world went away with the navigation
                value = None
            if value and value.get("hints"):
                return value
            if loop.time() > deadline:
                return value
            await asyncio.sleep(0.05)
    finally:
        try:
            await cdp.call("Target.closeTarget", {"targetId": target_id}, timeout=READ_TIMEOUT_S)
        except Exception:
            log.debug("could not close the hidden identity target", exc_info=True)


async def _from_open_tab(cdp: Any) -> dict[str, Any] | None:
    """Fallback (a browser without hidden targets, or chrome:// pages blocked by policy): the first
    open tab on a secure origin, read in an isolated world through a short flat session."""
    targets = await cdp.call("Target.getTargets", timeout=READ_TIMEOUT_S)
    tabs = [t for t in targets.get("targetInfos") or [] if t.get("type") == "page" and _secure_tab(t.get("url", ""))]
    for tab in tabs[:MAX_TABS]:
        try:
            attached = await cdp.call("Target.attachToTarget", {"targetId": tab["targetId"], "flatten": True},
                                      timeout=READ_TIMEOUT_S)
        except Exception:
            continue
        session_id = attached.get("sessionId")
        try:
            value = await _evaluate_isolated(cdp, session_id)
        except Exception:  # a navigating or crashed tab: try the next one
            value = None
        finally:
            try:
                await cdp.call("Target.detachFromTarget", {"sessionId": session_id}, timeout=READ_TIMEOUT_S)
            except Exception:
                pass
        if value and value.get("hints"):
            return value
    return None


async def websocket_url(endpoint: str | None, runtime: Any = None) -> str | None:
    """The browser-level DevTools websocket URL: ``runtime.cdp_ws_url`` when known (ProfilePilot
    profiles), else asked from ``<endpoint>/json/version`` (e.g. ShardX profiles, or an http endpoint)."""
    ws = getattr(runtime, "cdp_ws_url", None)
    if ws:
        return str(ws)
    if not endpoint:
        return None
    if endpoint.startswith(("ws://", "wss://")):
        return endpoint
    import httpx

    try:
        async with httpx.AsyncClient(trust_env=False, timeout=READ_TIMEOUT_S) as client:
            response = await client.get(endpoint.rstrip("/") + "/json/version")
            response.raise_for_status()
            return response.json().get("webSocketDebuggerUrl") or None
    except Exception as exc:
        log.debug("could not ask %s for its websocket URL: %s", endpoint, type(exc).__name__)
        return None


async def read_http_identity(ws_url: str | None) -> HttpIdentity | None:
    """The identity of the browser whose DevTools websocket is ``ws_url`` (``RuntimeInfo.cdp_ws_url``),
    over a short raw CDP connection of its own (no ``Runtime.enable``, nothing in any page's own world).
    None when the browser does not answer ``Browser.getVersion``; without client hints when no secure
    context could be read."""
    if not ws_url:
        return None
    from ..browser.devtools import browser_connection  # the plain CDP client (flat sessions, no domain enabling)

    try:
        async with browser_connection(ws_url) as cdp:
            version = await cdp.call("Browser.getVersion", timeout=READ_TIMEOUT_S)
            user_agent = str(version.get("userAgent") or "")
            if not user_agent:
                return None
            best: dict[str, Any] = {}
            for read in (_from_hidden_page, _from_open_tab):
                try:
                    value = await read(cdp)
                except Exception as exc:  # e.g. no hidden targets in an older browser
                    log.debug("browser identity: %s failed: %s", read.__name__, exc)
                    value = None
                if value and (value.get("hints") or not best):
                    best = value
                if best.get("hints"):
                    break
            return HttpIdentity.from_values(user_agent, str(version.get("product") or ""), best.get("hints"),
                                            best.get("languages") or ())
    except Exception as exc:
        log.debug("could not read the browser identity: %s", type(exc).__name__)
        return None


__all__ = [
    "CLIENT_HINT_HEADERS", "HttpIdentity", "accept_language", "curl_headers", "curl_targets", "identity_key",
    "impersonate_target", "merge_headers", "read_http_identity", "websocket_url",
]
