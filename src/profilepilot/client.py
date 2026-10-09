"""Synchronous Python API: ``ProfilePilot`` is a small facade over the store and the runtime.

Example::

    from profilepilot import ProfilePilot

    pp = ProfilePilot()
    pp.create("shop-de", proxy="socks5://user:pass@de.example.net:1080", lang="de-DE")
    info = pp.start("shop-de")              # native Chrome, own user-data-dir, local relay
    pp.cdp_url("shop-de")                   # http://127.0.0.1:<port> for Playwright / Scrapling
    pp.proxy_url("shop-de")                 # http://127.0.0.1:<relay> (same exit IP, no credentials)
    pp.cookies("shop-de", "https://example.com/")

Everything here is blocking and safe to call from any thread, including a thread that runs an
asyncio event loop (the short-lived Playwright connections run on a private worker thread).
Async code should still prefer ``await anyio.to_thread.run_sync(...)`` to keep its loop free.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, Mapping, TypeVar

from pydantic import ValidationError

from .errors import LaunchError, ProfileNotRunningError, ProfilePilotError
from .models import LaunchOptions, Profile, ProxyRecord, RuntimeInfo, TrashEntry, WindowMode
from .proxy.url import ProxyParseError
from .store import Store

if TYPE_CHECKING:
    from .automation.http_identity import HttpIdentity
    from .browser.runtime import RuntimeManager

log = logging.getLogger("profilepilot.client")

T = TypeVar("T")
ProxyKind = Literal["http", "socks5"]

_LAUNCH_FIELDS = frozenset(LaunchOptions.model_fields)


class ProfilePilot:
    """Blocking facade for scripts, notebooks and Scrapling.

    :param root: data directory (default: ``PROFILEPILOT_HOME`` or the platform data dir).
    :param store: an existing :class:`~profilepilot.store.Store` (overrides ``root``).
    :param runtime: an object with the ``RuntimeManager`` interface (``status``, ``list_running``,
        ``start``, ``stop``); by default ``profilepilot.browser.runtime.RuntimeManager`` is created
        lazily on first use.
    :param ignore_pause: act on profiles the user took control of (or that wait for the user's help
        after a ``profile_request_help``) too. Off by default: like the AI's tools, everything that
        drives a profile's browser, its cookies, exit IP or data (:meth:`start`, :meth:`stop`,
        :meth:`ensure_running` and so :meth:`cdp_url` / :meth:`proxy_url` / :meth:`cookies` /
        :meth:`set_cookies` / :meth:`http_identity`, :meth:`set_proxy`, :meth:`delete`) raises
        :class:`~profilepilot.control.ProfilePausedError` on a paused profile. Pass True only in
        your own scripts, never for an agent.
    """

    def __init__(self, root: Path | str | None = None, *, store: Store | None = None,
                 runtime: "RuntimeManager | Any | None" = None, ignore_pause: bool = False) -> None:
        self.store = store if store is not None else Store(root)
        self._runtime = runtime
        self._runtime_lock = threading.Lock()
        self.ignore_pause = bool(ignore_pause)

    def _check_not_paused(self, profile: Profile) -> None:
        """Refuse to act on a profile the user controls (see ``ignore_pause``)."""
        if self.ignore_pause:
            return
        from .control import ControlStore

        ControlStore(self.store).check_not_paused(profile.id)

    def __repr__(self) -> str:
        return f"ProfilePilot(root={str(self.store.root)!r})"

    def __enter__(self) -> "ProfilePilot":
        return self

    def __exit__(self, *exc: Any) -> None:
        """Nothing to release: browsers keep running (stop them explicitly with :meth:`stop`)."""

    @property
    def root(self) -> Path:
        return self.store.root

    @property
    def runtime(self) -> "RuntimeManager":
        """The runtime manager (created on first use; imports the browser runtime lazily)."""
        if self._runtime is None:
            with self._runtime_lock:
                if self._runtime is None:
                    from .browser.runtime import RuntimeManager

                    self._runtime = RuntimeManager(self.store)
        return self._runtime

    # ------------------------------------------------------------------ profiles

    def profiles(self, tag: str | None = None) -> list[Profile]:
        """All profiles (optionally only those carrying ``tag``), sorted by name."""
        return self.store.list_profiles(tag)

    def profile(self, ref: str) -> Profile:
        """Resolve a profile by id, name or unique id prefix."""
        return self.store.get_profile(ref)

    def create(self, name: str, proxy: str | None = None, **kw: Any) -> Profile:
        """Create a profile.

        :param proxy: a saved proxy (id or name) or a proxy spec in any format accepted by
            :func:`~profilepilot.proxy.url.parse_proxy` (it is saved first, credentials go to the
            secret store). A bare ``host:port`` without scheme defaults to ``http``; pass
            ``proxy_scheme="socks5"`` to change that.
        :param kw: ``notes``, ``tags``, ``browser``, ``color``, ``launch`` (dict or
            :class:`LaunchOptions`) and any ``LaunchOptions`` field directly (``window``, ``lang``,
            ``timezone``, ``webrtc``, ``disable_quic``, ``restore_session``, ``start_url``,
            ``extra_args``).
        """
        proxy_scheme = kw.pop("proxy_scheme", "http")
        launch = kw.pop("launch", None)
        launch_patch: dict[str, Any] = {}
        for key in [k for k in kw if k in _LAUNCH_FIELDS]:
            value = kw.pop(key)
            if value is not None:  # None means "keep the default"
                launch_patch[key] = value
        if launch is not None or launch_patch:
            base = launch.model_dump() if isinstance(launch, LaunchOptions) else dict(launch or {})
            if "window" not in base and "window" not in launch_patch:
                base["window"] = self.store.load_config().default_window
            try:
                launch = LaunchOptions.model_validate({**base, **launch_patch})
            except ValidationError as exc:
                problems = "; ".join(
                    f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
                )
                raise ProfilePilotError(f"Invalid launch option(s): {problems}") from None
        allowed = {"notes", "tags", "browser", "color"}
        unknown = sorted(set(kw) - allowed)
        if unknown:
            raise ProfilePilotError(f"Unknown profile option(s): {', '.join(unknown)}")
        proxy_id = self._proxy_id(proxy, proxy_scheme) if proxy else None
        return self.store.create_profile(name, proxy_id=proxy_id, launch=launch, **kw)

    def delete(self, ref: str) -> TrashEntry:
        """Move a stopped profile to the trash (restorable with ``store.restore_profile``)."""
        self._check_not_paused(self.store.get_profile(ref))
        return self.store.delete_profile(ref)

    # ------------------------------------------------------------------ proxies

    def proxies(self, tag: str | None = None) -> list[ProxyRecord]:
        return self.store.list_proxies(tag)

    def add_proxy(self, spec: str, name: str | None = None, *, default_scheme: str = "http",
                  tags: Iterable[str] = (), notes: str = "") -> ProxyRecord:
        """Save an upstream proxy (password goes to the secret store, never to JSON files)."""
        try:
            return self.store.add_proxy(spec, name, default_scheme=default_scheme, tags=tags, notes=notes)
        except ProxyParseError:
            raise ProfilePilotError(
                "Could not parse that proxy. Use scheme://user:pass@host:port, user:pass@host:port, "
                "host:port or host:port:user:pass."
            ) from None

    def set_proxy(self, ref: str, proxy: str | None, *, proxy_scheme: str = "http") -> Profile:
        """Bind ``proxy`` (saved proxy id/name or a proxy spec; None = direct) to the profile.

        The change is saved first. If the profile is running with a relay the upstream is
        switched live (new connections only); if it runs without a relay (it was started
        without a proxy) :class:`~profilepilot.errors.RestartRequiredError` is raised and the
        new proxy applies on the next start.
        """
        profile = self.store.get_profile(ref)
        self._check_not_paused(profile)
        proxy_id = self._proxy_id(proxy, proxy_scheme) if proxy else None
        profile = self.store.update_profile(profile.id, proxy_id=proxy_id)
        if self.runtime.status(profile.id) is not None:
            self.runtime.set_upstream(profile.id, proxy_id)
        return profile

    def _proxy_id(self, proxy: str, default_scheme: str) -> str:
        ref = proxy.strip()
        for record in self.store.list_proxies():  # exact id / name first (names may contain ':')
            if record.id == ref.lower() or record.name.casefold() == ref.casefold():
                return record.id
        if "://" in ref or "@" in ref or ":" in ref:
            return self.add_proxy(ref, default_scheme=default_scheme).id
        return self.store.get_proxy(ref).id

    # ------------------------------------------------------------------ runtime

    def start(self, ref: str, window: WindowMode | None = None, *, timeout: float = 60.0) -> RuntimeInfo:
        """Start the profile's browser (idempotent) and return its runtime info.

        :param window: override the window mode for this run only (``normal``/``offscreen``/``headless``).
        """
        profile = self.store.get_profile(ref)
        self._check_not_paused(profile)
        return self.runtime.start(profile.id, timeout=timeout, window=window)

    def stop(self, ref: str, *, timeout: float = 20.0) -> bool:
        """Close the profile's browser gracefully. Returns False if it was not running."""
        profile = self.store.get_profile(ref)
        self._check_not_paused(profile)
        return self.runtime.stop(profile.id, timeout=timeout)

    def info(self, ref: str) -> RuntimeInfo | None:
        """Runtime info of a running profile, or None if it is stopped."""
        profile = self.store.get_profile(ref)
        return self.runtime.status(profile.id)

    def running(self) -> list[RuntimeInfo]:
        """Runtime info of every running profile."""
        return self.runtime.list_running()

    def ensure_running(self, ref: str, window: WindowMode | None = None, *, start: bool = True,
                       timeout: float = 60.0) -> RuntimeInfo:
        """Current runtime info; starts the profile if needed (or raises if ``start`` is False)."""
        profile = self.store.get_profile(ref)
        self._check_not_paused(profile)
        info = self.runtime.status(profile.id)
        if info is not None and info.state == "running":
            return info
        if not start:
            raise ProfileNotRunningError(f"Profile '{profile.name}' is not running. Start it first.")
        return self.runtime.start(profile.id, timeout=timeout, window=window)

    def cdp_url(self, ref: str, *, start: bool = True, window: WindowMode | None = None) -> str:
        """DevTools HTTP endpoint (``http://127.0.0.1:<port>``) for ``connect_over_cdp``.

        Starts the profile if needed. Always use ``browser.contexts[0]`` (the persistent profile
        context) on the resulting connection, never ``new_context()``.
        """
        info = self.ensure_running(ref, window, start=start)
        if not info.cdp_http_url:
            raise LaunchError(f"Profile '{info.profile_name}' is running but exposes no DevTools endpoint.")
        return info.cdp_http_url

    def proxy_url(self, ref: str, kind: ProxyKind = "http", *, start: bool = True,
                  window: WindowMode | None = None) -> str | None:
        """Credential-free URL of the profile's local relay (same exit IP as the browser).

        ``kind="http"`` for curl / httpx / Scrapling ``Fetcher``, ``"socks5"`` for SOCKS clients.
        Returns None when the profile has no proxy (the browser connects directly). Starts the
        profile if needed: the relay lives as long as the browser.
        """
        if kind not in ("http", "socks5"):
            raise ValueError("kind must be 'http' or 'socks5'")
        info = self.ensure_running(ref, window, start=start)
        return info.http_proxy_url if kind == "http" else info.proxy_url

    # ------------------------------------------------------------------ cookies (CDP)

    def cookies(self, ref: str, url: str | Iterable[str] | None = None, *, start: bool = True,
                timeout: float = 30.0) -> list[dict[str, Any]]:
        """Cookies of the profile's live browser (``context.cookies()`` over CDP).

        :param url: only cookies that would be sent to this URL (or any of these URLs).
        :returns: Playwright cookie dicts ``{name, value, domain, path, expires, httpOnly,
            secure, sameSite}``. Values are secrets: do not log them.
        """
        info = self.ensure_running(ref, start=start)
        urls = [url] if isinstance(url, str) else (list(url) if url else None)
        cdp, driver = self._cdp_endpoint(info), self._driver()
        return _run_isolated(lambda: _cdp_get_cookies(cdp, urls, timeout, driver), timeout + 15,
                             f"read cookies of profile '{info.profile_name}'")

    def set_cookies(self, ref: str, cookies: Iterable[Mapping[str, Any]], *, start: bool = True,
                    timeout: float = 30.0) -> int:
        """Add/replace cookies in the profile's live browser (Playwright ``add_cookies`` shape:
        ``name``, ``value`` and either ``url`` or ``domain`` + ``path``). Returns the count."""
        items = [dict(c) for c in cookies]
        if not items:
            return 0
        info = self.ensure_running(ref, start=start)
        cdp, driver = self._cdp_endpoint(info), self._driver()
        _run_isolated(lambda: _cdp_add_cookies(cdp, items, timeout, driver), timeout + 15,
                      f"write cookies of profile '{info.profile_name}'")
        return len(items)

    # ------------------------------------------------------------------ identity (CDP)

    def http_identity(self, ref: str, *, start: bool = True, timeout: float = 30.0) -> "HttpIdentity":
        """The identity of the profile's running browser for HTTP clients: its user agent
        (``Browser.getVersion``), client hints (``navigator.userAgentData``, read in an isolated world)
        and languages. Send :meth:`HttpIdentity.headers` with requests made next to the browser
        (:func:`profilepilot.integrations.scrapling.fetcher_session` does)."""
        info = self.ensure_running(ref, start=start)
        cdp, driver = self._cdp_endpoint(info), self._driver()
        what = f"read the browser identity of profile '{info.profile_name}'"
        identity = _run_isolated(lambda: asyncio.run(_cdp_http_identity(cdp, timeout, driver)), timeout + 15, what)
        if identity is None:
            raise LaunchError(f"Could not {what}: the browser did not answer Browser.getVersion.")
        return identity

    def _driver(self) -> str:
        """The CDP driver for this store (see :mod:`profilepilot.automation.driver`)."""
        from .automation.driver import select_driver

        return select_driver(self.store.load_config())

    @staticmethod
    def _cdp_endpoint(info: RuntimeInfo) -> str:
        if not info.cdp_http_url:
            raise LaunchError(f"Profile '{info.profile_name}' is running but exposes no DevTools endpoint.")
        return info.cdp_http_url


# ---------------------------------------------------------------------- CDP helpers


def _run_isolated(fn: Callable[[], T], timeout: float, what: str) -> T:
    """Run ``fn`` on a private thread that has no asyncio loop (Playwright's sync API refuses to
    run on a thread with a running loop, or nested in another sync Playwright)."""
    results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            results.put((True, fn()))
        except BaseException as exc:  # handed to the caller
            results.put((False, exc))

    thread = threading.Thread(target=worker, name="profilepilot-cdp", daemon=True)
    thread.start()
    try:
        ok, value = results.get(timeout=timeout)
    except queue.Empty:
        raise LaunchError(f"Timed out trying to {what} over CDP.") from None
    if ok:
        return value
    if isinstance(value, ProfilePilotError):
        raise value
    first_line = str(value).strip().splitlines()[0] if str(value).strip() else ""
    raise LaunchError(f"Could not {what} over CDP: {type(value).__name__}: {first_line}") from None


def _with_profile_context(cdp_url: str, timeout: float, fn: Callable[[Any], T], driver: str | None = None) -> T:
    from .automation.driver import sync_playwright

    with sync_playwright(driver) as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url, timeout=timeout * 1000, no_defaults=True)
        try:
            if not browser.contexts:
                raise LaunchError("The browser exposes no default context over CDP.")
            return fn(browser.contexts[0])
        finally:
            browser.close()  # a CDP connection: this only disconnects, Chrome keeps running


async def _cdp_http_identity(cdp_url: str, timeout: float, driver: str | None = None) -> "HttpIdentity | None":
    """Read over a short raw CDP connection of its own (no Playwright attach, no ``Runtime.enable``)."""
    from .automation.http_identity import read_http_identity, websocket_url

    del timeout, driver  # read_http_identity bounds every call itself and needs no CDP driver
    return await read_http_identity(await websocket_url(cdp_url))


def _cdp_get_cookies(cdp_url: str, urls: list[str] | None, timeout: float,
                     driver: str | None = None) -> list[dict[str, Any]]:
    def read(context: Any) -> list[dict[str, Any]]:
        return [dict(c) for c in (context.cookies(urls) if urls else context.cookies())]

    return _with_profile_context(cdp_url, timeout, read, driver)


def _cdp_add_cookies(cdp_url: str, cookies: list[dict[str, Any]], timeout: float, driver: str | None = None) -> None:
    _with_profile_context(cdp_url, timeout, lambda context: context.add_cookies(cookies), driver)


__all__ = ["ProfilePilot"]
