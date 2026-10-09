"""Scrapling sessions bound to ProfilePilot profiles.

Scrapling's own ``cdp_url`` mode attaches to a browser but then calls ``browser.new_context()``,
which loses the profile's cookies and storage and applies context overrides (dark color scheme,
device scale factor 2, a synthetic user agent, a Google referer). The classes here keep the
profile intact instead:

* :class:`AsyncProfileSession` / :class:`ProfileSession` subclass Scrapling's browser sessions.
  They attach over CDP to the profile's running Chrome, **reuse** ``browser.contexts[0]`` (the
  persistent profile context), open their own tabs in it, and on close only close those tabs and
  disconnect: the user's context and browser keep running. No context options are applied, so
  pages see the genuine Chrome fingerprint. The base class follows the CDP driver
  (:mod:`profilepilot.automation.driver`, chosen at import): with patchright (the default) it is
  Scrapling's patchright-based ``AsyncStealthySession`` / ``StealthySession`` - no
  ``Runtime.enable``, so an attached session is not CDP-detectable through console side channels;
  their stealth options are launch flags and do not apply to a profile ProfilePilot launches. With
  the Playwright driver it is ``AsyncDynamicSession`` / ``DynamicSession``, which a page can detect
  while attached (docs/FINGERPRINT-AUDIT.md F1).
* :func:`fetcher_session` returns a Scrapling ``FetcherSession`` (curl_cffi) whose requests leave
  through the profile's local relay (same exit IP as the browser, no credentials in URLs or logs),
  start with the browser's cookies and carry the running browser's identity: its user agent, client
  hints and languages replace curl_cffi's built-in ones (a macOS Chrome), and the TLS / HTTP/2 target
  is the curl_cffi target closest to that browser (:mod:`profilepilot.automation.http_identity`,
  docs/FINGERPRINT-AUDIT.md F9).
* :func:`fetch` is a one-shot async convenience.

Requires ``pip install 'profilepilot[scrapling]'`` (Scrapling with its fetchers).
"""

from __future__ import annotations

import logging
from contextlib import suppress
from http.cookiejar import Cookie, CookieJar
from typing import Any, Iterable, Mapping

try:
    from scrapling.fetchers import (
        AsyncDynamicSession,
        AsyncStealthySession,
        DynamicSession,
        FetcherSession,
        StealthySession,
    )
except ImportError as exc:  # pragma: no cover - only without the optional extra
    raise ImportError(
        "ProfilePilot's Scrapling integration needs Scrapling with its fetchers. "
        "Install it with: pip install 'profilepilot[scrapling]'"
    ) from exc

from ..automation.driver import DRIVER, async_playwright, sync_playwright
from ..automation.http_identity import HttpIdentity, curl_headers, identity_key, impersonate_target, merge_headers
from ..client import ProfilePilot
from ..errors import LaunchError, ProfilePilotError
from ..models import Profile, WindowMode

log = logging.getLogger("profilepilot.scrapling")

SESSION_DRIVER = DRIVER
"""The CDP driver of the browser sessions (fixed at import: it decides their Scrapling base class)."""
_STEALTHY = SESSION_DRIVER == "patchright"
_AsyncBrowserSession: Any = AsyncStealthySession if _STEALTHY else AsyncDynamicSession
_BrowserSession: Any = StealthySession if _STEALTHY else DynamicSession

# Session options that would replace what the profile owns (or alter its fingerprint).
_PROFILE_OWNED: dict[str, str] = {
    "cdp_url": "the session always attaches to the profile's own DevTools endpoint",
    "proxy": "the profile's proxy is used; set it on the profile (ProfilePilot.create(proxy=...))",
    "proxy_rotator": "rotation needs a fresh browser context per request; a profile has one sticky proxy",
    "useragent": "the browser's genuine user agent is kept",
    "locale": "the profile's language is set on the profile (lang=...)",
    "timezone_id": "the profile's timezone is set on the profile (timezone=...)",
    "additional_args": "no new browser context is created, so context options do not apply",
    "cookies": "cookies live in the profile; use ProfilePilot.set_cookies()",
    "init_script": "it would be injected into the user's own profile context",
    "user_data_dir": "the browser is launched by ProfilePilot",
    "real_chrome": "the browser is launched by ProfilePilot",
    "executable_path": "the browser is launched by ProfilePilot",
    "extra_flags": "the browser is launched by ProfilePilot (use launch extra_args on the profile)",
    "dns_over_https": "the browser is launched by ProfilePilot",
    "hide_canvas": "the browser is launched by ProfilePilot, and pages see its genuine canvas",
    "allow_webgl": "the browser is launched by ProfilePilot, and pages see its genuine WebGL",
    "block_webrtc": "the profile's WebRTC policy is set on the profile (launch webrtc)",
}
# Launch-only options that are meaningless when attaching; accepted and ignored.
_IGNORED = frozenset({"headless"})
# Profile-owned options whose value is what the genuine browser does anyway: accepted.
_NATIVE_VALUES: dict[str, Any] = {"allow_webgl": True}
_FETCH_KEYS = frozenset({
    "load_dom", "wait", "network_idle", "google_search", "timeout", "disable_resources",
    "wait_selector", "page_action", "page_setup", "selector_config", "extra_headers",
    "wait_selector_state", "blocked_domains", *(("solve_cloudflare",) if _STEALTHY else ()),
})
_FETCHER_PROXY_KEYS = ("proxy", "proxies", "proxy_auth", "proxy_rotator")


def _is_set(value: Any) -> bool:
    return value not in (None, "", False, {}, [], ())


def _clean_browser_kwargs(kwargs: dict[str, Any], owner: str) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key in _IGNORED:
            continue
        if key in _PROFILE_OWNED:
            if (value != _NATIVE_VALUES[key]) if key in _NATIVE_VALUES else _is_set(value):
                raise ProfilePilotError(f"{owner} does not accept '{key}': {_PROFILE_OWNED[key]}.")
            continue
        clean[key] = value
    clean.setdefault("google_search", False)  # no fake "came from Google" referer by default
    clean["headless"] = False  # keeps Scrapling from computing a synthetic user agent
    return clean


def _reject_fetch_proxy(kwargs: Mapping[str, Any], owner: str) -> None:
    if _is_set(kwargs.get("proxy")):
        raise ProfilePilotError(
            f"{owner}.fetch() does not accept 'proxy': a per-request proxy needs a new browser context; "
            "requests always use the profile's proxy."
        )


def _profile_context(browser: Any, profile: Profile) -> Any:
    contexts = browser.contexts
    if not contexts:
        raise LaunchError(f"Profile '{profile.name}' exposes no default browser context over CDP.")
    return contexts[0]


def _attach_error(profile: Profile, exc: BaseException) -> LaunchError:
    text = str(exc).strip()
    first = text.splitlines()[0] if text else ""
    return LaunchError(f"Could not attach to profile '{profile.name}' over CDP: {type(exc).__name__}: {first}")


class _ProfileBound:
    """Plumbing shared by the browser sessions bound to a ProfilePilot profile."""

    _pp_pilot: ProfilePilot
    _pp_profile: Profile
    _pp_autostart: bool
    _pp_window: WindowMode | None

    def _pp_setup(self, ref: str, pilot: ProfilePilot | None, root: Any, autostart: bool,
                  window: WindowMode | None, kwargs: dict[str, Any]) -> dict[str, Any]:
        self._pp_pilot = pilot if pilot is not None else ProfilePilot(root)
        self._pp_profile = self._pp_pilot.profile(ref)  # fail fast on a typo; nothing is started yet
        self._pp_autostart = autostart
        self._pp_window = window
        return _clean_browser_kwargs(kwargs, type(self).__name__)

    @property
    def profile(self) -> Profile:
        """The ProfilePilot profile this session drives."""
        return self._pp_profile

    @property
    def pilot(self) -> ProfilePilot:
        return self._pp_pilot

    def _pp_cdp_url(self) -> str:
        return self._pp_pilot.cdp_url(self._pp_profile.id, start=self._pp_autostart, window=self._pp_window)


class AsyncProfileSession(_ProfileBound, _AsyncBrowserSession):
    """Scrapling browser session (``AsyncStealthySession`` with patchright, else
    ``AsyncDynamicSession``) running inside a ProfilePilot profile's own browser.

    Usage::

        async with AsyncProfileSession("shop-de", max_pages=3) as session:
            page = await session.fetch("https://example.com/account", network_idle=True)
            print(page.css("h1::text").get())

    Accepts Scrapling's session options except those the profile owns (``proxy``, ``cdp_url``,
    ``useragent``, ``locale``, ``timezone_id``, ``cookies``, ``additional_args``, launch flags).
    ``google_search`` defaults to False. Starting the session starts the profile if needed
    (``autostart``); closing it closes only the tabs the session opened.
    """

    def __init__(self, ref: str, *, pilot: ProfilePilot | None = None, root: Any = None,
                 autostart: bool = True, window: WindowMode | None = None, **kwargs: Any) -> None:
        super().__init__(**self._pp_setup(ref, pilot, root, autostart, window, kwargs))

    async def start(self) -> None:
        """Attach to the profile's browser and adopt its persistent context (no new context)."""
        if self.playwright:
            raise RuntimeError("Session has been already started")
        import anyio.to_thread

        cdp_url = await anyio.to_thread.run_sync(self._pp_cdp_url)
        self._config.cdp_url = cdp_url
        playwright = await async_playwright(SESSION_DRIVER).start()
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_url, no_defaults=True)
            context = _profile_context(browser, self._pp_profile)
        except BaseException as exc:
            with suppress(Exception):
                await playwright.stop()
            if isinstance(exc, Exception) and not isinstance(exc, ProfilePilotError):
                raise _attach_error(self._pp_profile, exc) from exc
            raise
        self.playwright, self.browser, self.context = playwright, browser, context
        self._is_alive = True

    async def close(self) -> None:
        """Close the session's own tabs and disconnect. The profile's context and browser stay open."""
        if not self._is_alive:
            return
        try:
            await self.close_pages()
        finally:
            browser, playwright = self.browser, self.playwright
            self.context = self.browser = self.playwright = None
            self._is_alive = False
            if browser is not None:
                with suppress(Exception):
                    await browser.close()  # CDP connection: disconnects only
            if playwright is not None:
                with suppress(Exception):
                    await playwright.stop()

    async def fetch(self, url: str, **kwargs: Any) -> Any:
        """Scrapling ``fetch`` in a tab of the profile (``proxy`` is not accepted)."""
        _reject_fetch_proxy(kwargs, type(self).__name__)
        return await super().fetch(url, **kwargs)


class ProfileSession(_ProfileBound, _BrowserSession):
    """Synchronous twin of :class:`AsyncProfileSession` (Scrapling ``StealthySession`` with
    patchright, else ``DynamicSession``; one tab).

    Like every Playwright sync API it must not be used on a thread that runs an asyncio loop.
    """

    def __init__(self, ref: str, *, pilot: ProfilePilot | None = None, root: Any = None,
                 autostart: bool = True, window: WindowMode | None = None, **kwargs: Any) -> None:
        super().__init__(**self._pp_setup(ref, pilot, root, autostart, window, kwargs))

    def start(self) -> None:
        if self.playwright:
            raise RuntimeError("Session has been already started")
        cdp_url = self._pp_cdp_url()
        self._config.cdp_url = cdp_url
        playwright = sync_playwright(SESSION_DRIVER).start()
        try:
            browser = playwright.chromium.connect_over_cdp(cdp_url, no_defaults=True)
            context = _profile_context(browser, self._pp_profile)
        except BaseException as exc:
            with suppress(Exception):
                playwright.stop()
            if isinstance(exc, Exception) and not isinstance(exc, ProfilePilotError):
                raise _attach_error(self._pp_profile, exc) from exc
            raise
        self.playwright, self.browser, self.context = playwright, browser, context
        self._is_alive = True

    def close(self) -> None:
        if not self._is_alive:
            return
        try:
            self.close_pages()
        finally:
            browser, playwright = self.browser, self.playwright
            self.context = self.browser = self.playwright = None
            self._is_alive = False
            if browser is not None:
                with suppress(Exception):
                    browser.close()
            if playwright is not None:
                with suppress(Exception):
                    playwright.stop()

    def fetch(self, url: str, **kwargs: Any) -> Any:
        _reject_fetch_proxy(kwargs, type(self).__name__)
        return super().fetch(url, **kwargs)


# ---------------------------------------------------------------------- HTTP (curl_cffi)


def _jar_cookie(c: Mapping[str, Any]) -> Cookie | None:
    """Playwright cookie dict -> ``http.cookiejar.Cookie`` keeping host-only vs domain scope."""
    if c.get("partitionKey"):  # CHIPS cookies belong to a top-level site; never send unpartitioned
        return None
    domain = str(c.get("domain") or "")
    name = str(c.get("name") or "")
    if not domain or not name:
        return None
    expires = c.get("expires")
    exp = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
    dotted = domain.startswith(".")
    return Cookie(
        version=0, name=name, value=str(c.get("value", "")), port=None, port_specified=False,
        domain=domain, domain_specified=dotted, domain_initial_dot=dotted,
        path=str(c.get("path") or "/"), path_specified=True, secure=bool(c.get("secure")),
        expires=exp, discard=exp is None, comment=None, comment_url=None,
        rest={"HttpOnly": None} if c.get("httpOnly") else {}, rfc2109=False,
    )


def _load_cookies(jar: CookieJar, cookies: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, str], str]:
    loaded: dict[tuple[str, str, str], str] = {}
    for c in cookies:
        cookie = _jar_cookie(c)
        if cookie is None:
            continue
        jar.set_cookie(cookie)
        loaded[(cookie.domain, cookie.path, cookie.name)] = cookie.value or ""
    return loaded


def _http_only(cookie: Cookie) -> bool:
    rest = getattr(cookie, "_rest", {}) or {}
    return "HttpOnly" in rest or str(rest.get("http_only", "")).lower() == "true"


def _changed_cookies(jar: CookieJar, loaded: Mapping[tuple[str, str, str], str]) -> list[dict[str, Any]]:
    """Cookies added or changed during the session, in Playwright ``add_cookies`` shape."""
    out: list[dict[str, Any]] = []
    for cookie in jar:
        if loaded.get((cookie.domain, cookie.path, cookie.name)) == (cookie.value or ""):
            continue
        domain = cookie.domain  # Playwright: "example.com" = host-only, ".example.com" = domain cookie
        if cookie.domain_specified and not domain.startswith("."):
            domain = "." + domain
        item: dict[str, Any] = {
            "name": cookie.name, "value": cookie.value or "", "domain": domain, "path": cookie.path or "/",
            "secure": bool(cookie.secure), "httpOnly": _http_only(cookie),
        }
        if cookie.expires:
            item["expires"] = int(cookie.expires)
        out.append(item)
    return out


class ProfileFetcherSession(FetcherSession):
    """Scrapling ``FetcherSession`` bound to a profile (create it with :func:`fetcher_session`).

    Requests leave through the profile's local relay, i.e. with the same exit IP as its browser,
    and every ``with`` / ``async with`` block starts with the browser's current cookies. With
    ``write_back=True`` cookies that were added or changed during the block are copied back into
    the browser when it exits (deletions are not propagated).

    Requests carry the identity of the profile's running browser (:attr:`identity`, read once per
    browser start, or given as ``identity``): its ``User-Agent``, ``sec-ch-ua`` / ``-mobile`` /
    ``-platform`` and ``Accept-Language`` replace the impersonation target's built-in headers, and
    ``impersonate`` defaults to the curl_cffi target closest to that browser. Session ``headers`` and an
    explicit ``impersonate`` still win.
    """

    # No __slots__ here on purpose: FetcherSession.__enter__ builds its config from
    # ``self.__slots__`` and must keep seeing the parent's slot names.

    def __init__(self, ref: str, *, pilot: ProfilePilot | None = None, root: Any = None,
                 autostart: bool = True, window: WindowMode | None = None, write_back: bool = False,
                 cookie_urls: Iterable[str] | None = None, identity: HttpIdentity | None = None,
                 **kwargs: Any) -> None:
        for key in _FETCHER_PROXY_KEYS:
            if _is_set(kwargs.pop(key, None)):
                raise ProfilePilotError(
                    f"fetcher_session does not accept '{key}': requests always use the profile's proxy "
                    "(its local relay), so they share the browser's exit IP."
                )
        kwargs.setdefault("stealthy_headers", False)  # no fake Google referer / generated headers
        self._pp_headers: dict[str, Any] = dict(kwargs.pop("headers", None) or {})  # the caller's: they win
        self._pp_impersonate = kwargs.get("impersonate")  # None: the target closest to the browser
        self._pp_identity = identity
        self._pp_identity_given = identity is not None
        self._pp_identity_key: str | None = None
        self._pp_pilot = pilot if pilot is not None else ProfilePilot(root)
        self._pp_profile = self._pp_pilot.profile(ref)
        self._pp_autostart = autostart
        self._pp_window = window
        self._pp_write_back = write_back
        self._pp_cookie_urls = list(cookie_urls) if cookie_urls else None
        self._pp_loaded: dict[tuple[str, str, str], str] = {}
        info = self._pp_pilot.ensure_running(self._pp_profile.id, window, start=autostart)
        super().__init__(proxy=info.http_proxy_url, headers=dict(self._pp_headers), **kwargs)

    @property
    def profile(self) -> Profile:
        return self._pp_profile

    @property
    def identity(self) -> HttpIdentity | None:
        """The browser identity the requests carry (None until the first block starts)."""
        return self._pp_identity

    @property
    def proxy_url(self) -> str | None:
        """The relay URL requests use (None: the profile has no proxy and connects directly)."""
        return self._default_proxy

    def _pp_prepare(self) -> list[dict[str, Any]]:
        """Refresh the relay URL (it changes when the profile restarts) and the browser identity, and
        read the cookies."""
        info = self._pp_pilot.ensure_running(self._pp_profile.id, self._pp_window, start=self._pp_autostart)
        self._default_proxy = info.http_proxy_url
        self._pp_apply_identity(info)
        return self._pp_pilot.cookies(self._pp_profile.id, self._pp_cookie_urls, start=False)

    def _pp_apply_identity(self, info: Any) -> None:
        """Session headers and impersonation target from the running browser's identity (read again
        when the browser restarted, or while its client hints are unknown)."""
        if not self._pp_identity_given:
            key = identity_key(self._pp_profile.id, info)
            current = self._pp_identity
            if current is None or key != self._pp_identity_key or not current.has_client_hints:
                self._pp_identity = self._pp_pilot.http_identity(self._pp_profile.id, start=False)
                self._pp_identity_key = key
        identity = self._pp_identity
        assert identity is not None
        self._default_headers = merge_headers(curl_headers(identity), self._pp_headers)
        if self._pp_impersonate is None:
            self._default_impersonate = impersonate_target(identity.family, identity.major)

    def _pp_write(self, jar: CookieJar) -> None:
        changed = _changed_cookies(jar, self._pp_loaded)
        if changed:
            count = self._pp_pilot.set_cookies(self._pp_profile.id, changed, start=False)
            log.debug("wrote %d cookie(s) back to profile %s", count, self._pp_profile.id)

    def __enter__(self) -> Any:
        cookies = self._pp_prepare()
        client = super().__enter__()
        try:
            self._pp_loaded = _load_cookies(client._curl_session.cookies.jar, cookies)
        except BaseException:
            super().__exit__(None, None, None)
            raise
        return client

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        client = self._client
        try:
            if self._pp_write_back and client is not None and getattr(client, "_curl_session", None) is not None:
                try:
                    self._pp_write(client._curl_session.cookies.jar)
                except Exception as exc:
                    log.warning("could not write cookies back to profile %s: %s", self._pp_profile.id, type(exc).__name__)
        finally:
            super().__exit__(exc_type, exc_val, exc_tb)

    async def __aenter__(self) -> Any:
        import anyio.to_thread

        cookies = await anyio.to_thread.run_sync(self._pp_prepare)
        client = await super().__aenter__()
        try:
            self._pp_loaded = _load_cookies(client._async_curl_session.cookies.jar, cookies)
        except BaseException:
            await super().__aexit__(None, None, None)
            raise
        return client

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        import anyio.to_thread

        client = self._client
        try:
            if self._pp_write_back and client is not None and getattr(client, "_async_curl_session", None) is not None:
                jar = client._async_curl_session.cookies.jar
                try:
                    await anyio.to_thread.run_sync(lambda: self._pp_write(jar))
                except Exception as exc:
                    log.warning("could not write cookies back to profile %s: %s", self._pp_profile.id, type(exc).__name__)
        finally:
            await super().__aexit__(exc_type, exc_val, exc_tb)


def fetcher_session(ref: str, *, pilot: ProfilePilot | None = None, root: Any = None, autostart: bool = True,
                    window: WindowMode | None = None, write_back: bool = False,
                    cookie_urls: Iterable[str] | None = None, identity: HttpIdentity | None = None,
                    **kwargs: Any) -> ProfileFetcherSession:
    """A Scrapling ``FetcherSession`` that uses the profile's relay, the browser's cookies and the
    browser's identity (user agent, client hints, languages; ``impersonate`` defaults to the closest
    curl_cffi target).

    Starts the profile if needed (its relay lives as long as its browser). ``kwargs`` are
    ``FetcherSession`` options (``impersonate``, ``timeout``, ``headers``, ``retries``, ...) except
    the proxy options; ``stealthy_headers`` defaults to False. ``identity`` skips reading it from the
    browser (the MCP server passes the one it read). Use it with ``with`` (sync) or ``async with``
    (async); each block starts from the browser's current cookies.
    """
    return ProfileFetcherSession(ref, pilot=pilot, root=root, autostart=autostart, window=window,
                                 write_back=write_back, cookie_urls=cookie_urls, identity=identity, **kwargs)


async def fetch(ref: str, url: str, *, pilot: ProfilePilot | None = None, root: Any = None,
                autostart: bool = True, window: WindowMode | None = None, **kwargs: Any) -> Any:
    """Fetch one page in a new tab of the profile's browser and return Scrapling's ``Response``.

    ``kwargs`` may mix session options (e.g. ``max_pages``, ``retries``) and fetch options
    (e.g. ``network_idle``, ``wait_selector``, ``page_action``).
    """
    fetch_kwargs = {k: kwargs.pop(k) for k in list(kwargs) if k in _FETCH_KEYS}
    async with AsyncProfileSession(ref, pilot=pilot, root=root, autostart=autostart, window=window,
                                   **kwargs) as session:
        return await session.fetch(url, **fetch_kwargs)


__all__ = ["AsyncProfileSession", "ProfileSession", "ProfileFetcherSession", "fetcher_session", "fetch"]
