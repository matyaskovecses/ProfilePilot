"""Playwright side of ProfilePilot: attach to running profiles over CDP and track their tabs.

:class:`BrowserManager` (one per MCP server / API client) keeps one CDP connection per profile and
hands out :class:`ProfileSession` objects. It never launches Chrome itself (that is the
:class:`~profilepilot.browser.runtime.RuntimeManager`'s job, or ShardX's for ``shardx:`` refs) and
never closes a browser: on CDP connections ``Browser.close()`` only disconnects.

Attach rules (verified with Playwright 1.63 on Chrome 154):

* ``connect_over_cdp(url, no_defaults=True)`` - keeps Chrome's native download, focus and media
  behaviour.
* Always ``browser.contexts[0]``: the persistent profile context with the profile's cookies.
  ``new_context()`` would create an in-memory incognito-like context and is never used.
* Downloads are pointed at the profile's ``downloads/`` folder with
  ``Browser.setDownloadBehavior`` on a browser CDP session that stays attached.
* ``launch.timezone`` (opt-in) is applied with ``Emulation.setTimezoneOverride`` per page; the
  override lives as long as its CDP session, so those sessions are kept attached.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import anyio.to_thread
from playwright.async_api import Browser, BrowserContext, CDPSession, Dialog, Locator, Page, Playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from ..errors import LaunchError, NotFoundError, ProfileNotRunningError, ProfilePilotError
from ..models import Profile, RuntimeInfo, WindowMode
from .content import InvalidTargetError, normalize_ref, stale_ref_error

if TYPE_CHECKING:
    from ..browser.runtime import RuntimeManager
    from ..integrations.shardx import ShardXClient
    from ..store import Store

log = logging.getLogger("profilepilot.automation")

SHARDX_PREFIX = "shardx:"
TITLE_TIMEOUT = 2.0


def is_shardx_ref(ref: str) -> bool:
    return (ref or "").strip().lower().startswith(SHARDX_PREFIX)


class ProfileSession:
    """A live CDP connection to one running profile, with active-tab tracking.

    Attributes: ``key`` (profile id, or ``shardx:<id>``), ``label`` (human name), ``runtime``
    (:class:`RuntimeInfo`, None for ShardX), ``browser`` and ``context`` (``contexts[0]``).
    """

    def __init__(
        self,
        *,
        key: str,
        label: str,
        browser: Browser,
        context: BrowserContext,
        endpoint: str,
        runtime: RuntimeInfo | None = None,
        profile: Profile | None = None,
        downloads_dir: Path | None = None,
        timezone: str | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.browser = browser
        self.context = context
        self.endpoint = endpoint
        self.runtime = runtime
        self.profile = profile
        self.downloads_dir = downloads_dir
        self.timezone = timezone
        self.dialogs: deque[dict[str, Any]] = deque(maxlen=20)
        """Recent JavaScript dialogs (alert/confirm/prompt/beforeunload) that were auto-answered."""
        self.new_tabs: deque[Page] = deque(maxlen=20)
        """Tabs opened since the last :meth:`drain_new_tabs` (popups, window.open, new tabs)."""
        self._active: Page | None = None
        self._disconnected = False
        self._browser_cdp: CDPSession | None = None
        self._tz_sessions: dict[Page, CDPSession] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        browser.on("disconnected", self._on_disconnected)
        context.on("page", self._on_page)
        context.on("dialog", self._on_dialog)
        for page in context.pages:
            page.on("close", self._on_page_close)

    def __repr__(self) -> str:
        return f"<ProfileSession {self.label!r} key={self.key} connected={self.is_connected}>"

    # ------------------------------------------------------------------ state

    @property
    def is_connected(self) -> bool:
        return not self._disconnected and self.browser.is_connected()

    async def setup(self) -> None:
        """Per-connection setup: download folder, timezone override, initial active tab."""
        if self.downloads_dir is not None:
            try:
                self._browser_cdp = await self.browser.new_browser_cdp_session()
                await self._browser_cdp.send("Browser.setDownloadBehavior", {
                    "behavior": "allow", "downloadPath": str(self.downloads_dir), "eventsEnabled": True,
                })
            except PlaywrightError as exc:
                log.warning("%s: could not set the download folder: %s", self.label, _first_line(exc))
        if self.timezone:
            await asyncio.gather(*(self._apply_timezone(p) for p in self._pages()))
        self._active = await self._guess_foreground()

    async def _guess_foreground(self) -> Page | None:
        """The tab the user is looking at (``visibilityState == "visible"``), else the newest tab."""
        pages = self._pages()

        async def visible(page: Page) -> bool:
            try:
                return await asyncio.wait_for(page.evaluate("document.visibilityState"), 1.0) == "visible"
            except Exception:
                return False

        flags = await asyncio.gather(*(visible(p) for p in pages))
        for page, flag in zip(pages, flags):
            if flag:
                return page
        return pages[-1] if pages else None

    async def close(self) -> None:
        """Disconnect from the browser (the browser itself keeps running)."""
        self._disconnected = True
        for task in list(self._tasks):
            task.cancel()
        try:
            await self.browser.close()
        except PlaywrightError as exc:
            log.debug("%s: disconnect: %s", self.label, _first_line(exc))
        self._tz_sessions.clear()
        self._browser_cdp = None

    def _ensure_open(self) -> None:
        if not self.is_connected:
            raise ProfileNotRunningError(
                f"The connection to '{self.label}' was lost (the browser was closed or restarted). Try again."
            )

    # ------------------------------------------------------------------ events

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_disconnected(self, _browser: Browser) -> None:
        self._disconnected = True
        log.info("%s: CDP connection closed", self.label)

    def _on_page(self, page: Page) -> None:
        # New tabs and popups (window.open, target=_blank) become the active tab.
        self._active = page
        self.new_tabs.append(page)
        page.on("close", self._on_page_close)
        if self.timezone:
            self._spawn(self._apply_timezone(page))

    def _on_page_close(self, page: Page) -> None:
        self._tz_sessions.pop(page, None)
        if self._active is page:
            self._active = None

    def _on_dialog(self, dialog: Dialog) -> None:
        entry = {"type": dialog.type, "message": dialog.message[:500],
                 "url": dialog.page.url if dialog.page else None}
        self.dialogs.append(entry)
        log.info("%s: auto-answered a %s dialog", self.label, dialog.type)
        # Same outcome as Playwright's default auto-dismiss, except that leaving the page is allowed.
        self._spawn(dialog.accept() if dialog.type == "beforeunload" else dialog.dismiss())

    def drain_dialogs(self) -> list[dict[str, Any]]:
        """Return and forget the dialogs recorded since the last call."""
        items = list(self.dialogs)
        self.dialogs.clear()
        return items

    def drain_new_tabs(self) -> list[Page]:
        """Return and forget the tabs opened since the last call (still open ones only)."""
        items = [p for p in self.new_tabs if not p.is_closed()]
        self.new_tabs.clear()
        return items

    def is_active(self, page: Page) -> bool:
        """Is ``page`` the tab the tools act on by default?"""
        return self._current(self._pages()) is page

    async def _apply_timezone(self, page: Page) -> None:
        if page in self._tz_sessions or page.is_closed() or not self.timezone:
            return
        try:
            cdp = await self.context.new_cdp_session(page)
            await cdp.send("Emulation.setTimezoneOverride", {"timezoneId": self.timezone})
            self._tz_sessions[page] = cdp  # the override lasts only while this session is attached
        except PlaywrightError as exc:
            log.warning("%s: timezone override %r failed: %s", self.label, self.timezone, _first_line(exc))

    # ------------------------------------------------------------------ tabs

    def _pages(self) -> list[Page]:
        return [p for p in self.context.pages if not p.is_closed()]

    def _page_at(self, index: int, pages: list[Page] | None = None) -> Page:
        pages = self._pages() if pages is None else pages
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(pages):
            have = f"0..{len(pages) - 1}" if pages else "none"
            raise NotFoundError(f"Tab {index} does not exist (open tabs: {have}). Use browser_tabs to list them.")
        return pages[index]

    def _current(self, pages: list[Page]) -> Page | None:
        if self._active is not None and not self._active.is_closed() and self._active in pages:
            return self._active
        return pages[-1] if pages else None

    async def page(self, tab: int | None = None, *, interactive: bool = True) -> Page:
        """The page to act on: tab ``tab`` if given (it becomes the active tab), else the active
        tab. Opens a new tab when the browser has none.

        For ``interactive`` use the page is brought to the front when it is not visible: Chrome
        stops ``requestAnimationFrame`` in background tabs, and Playwright's actionability checks
        (clicks, typing) wait for animation frames, so acting on a hidden tab would hang.

        A *minimized* window is never restored: ``bringToFront``, ``Browser.setWindowBounds`` and
        even ``ShowWindow(SW_SHOWNOACTIVATE)`` give Chrome the keyboard focus (verified on Windows
        11 / Chrome 154), so the user's typing would go into the profile's window. Read-only callers
        (``interactive=False``) work on the hidden page; interactive callers get a clear error.
        """
        self._ensure_open()
        pages = self._pages()
        current = self._page_at(tab, pages) if tab is not None else self._current(pages)
        if current is None:
            current = await self.context.new_page()
        self._active = current
        await self._ensure_foreground(current, interactive=interactive)
        return current

    async def _ensure_foreground(self, page: Page, *, interactive: bool = True) -> None:
        try:
            state = await asyncio.wait_for(page.evaluate("document.visibilityState"), TITLE_TIMEOUT)
        except Exception:  # navigation in flight or a dialog is open: leave it alone
            return
        if state == "visible":
            return
        if await self.window_minimized(page):
            if interactive:
                raise ProfilePilotError(minimized_message(self.label))
            return
        await _bring_to_front(page)  # a background tab of a normal window: no focus change

    async def window_minimized(self, page: Page) -> bool:
        """Is the browser window holding ``page`` minimized? (CDP ``Browser.getWindowForTarget``)"""
        try:
            cdp = await self.context.new_cdp_session(page)
        except PlaywrightError:
            return False
        try:
            window = await cdp.send("Browser.getWindowForTarget")
            return (window.get("bounds") or {}).get("windowState") == "minimized"
        except PlaywrightError:
            return False
        finally:
            try:
                await cdp.detach()
            except PlaywrightError:
                pass

    async def tabs(self) -> list[dict[str, Any]]:
        """``[{index, url, title, active}]`` for every open tab."""
        self._ensure_open()
        pages = self._pages()
        active = self._current(pages)
        titles = await asyncio.gather(*(_safe_title(p) for p in pages))
        return [
            {"index": i, "url": p.url, "title": title, "active": p is active}
            for i, (p, title) in enumerate(zip(pages, titles))
        ]

    def index_of(self, page: Page) -> int | None:
        try:
            return self._pages().index(page)
        except ValueError:
            return None

    async def new_tab(self, url: str | None = None, *, wait_until: str = "domcontentloaded",
                      timeout: float | None = None) -> Page:
        """Open a tab (it becomes active) and optionally navigate it. URL policy checks are the
        caller's job."""
        self._ensure_open()
        page = await self.context.new_page()
        self._active = page
        if url:
            await page.goto(url, wait_until=wait_until, timeout=timeout)  # type: ignore[arg-type]
        return page

    async def select_tab(self, index: int) -> Page:
        """Make tab ``index`` active and bring it to the front."""
        self._ensure_open()
        page = self._page_at(index)
        if not await self.window_minimized(page):  # bringing a minimized window up would steal the focus
            await _bring_to_front(page)
        self._active = page
        return page

    async def close_tab(self, index: int) -> None:
        """Close tab ``index``. The last remaining tab is never closed (closing it would quit the
        browser): it is navigated to ``about:blank`` instead."""
        self._ensure_open()
        pages = self._pages()
        page = self._page_at(index, pages)
        if len(pages) <= 1:
            await page.goto("about:blank")
            self._active = page
            return
        was_active = self._current(pages) is page
        await page.close()
        if was_active:
            remaining = self._pages()
            if remaining:
                self._active = remaining[min(index, len(remaining) - 1)]
                if not await self.window_minimized(self._active):  # never steal the focus
                    await _bring_to_front(self._active)

    # ------------------------------------------------------------------ elements

    async def locate(self, page: Page, ref: str | None = None, selector: str | None = None) -> Locator:
        """Resolve a snapshot ``ref`` (``e12``) or a ``selector`` (CSS, or Playwright ``text=...``)."""
        if ref:
            ref_id = normalize_ref(ref)
            locator = page.locator(f"aria-ref={ref_id}")
            try:
                count = await locator.count()
            except PlaywrightError:  # e.g. the frame of an "f1e3" ref is gone
                count = 0
            if count == 0:
                raise stale_ref_error(ref_id)
            return locator
        if selector and selector.strip():
            return page.locator(selector.strip())
        raise InvalidTargetError("Give either 'ref' (from browser_snapshot, e.g. 'e12') or a CSS 'selector'.")


class BrowserManager:
    """Owns the Playwright driver and one cached :class:`ProfileSession` per profile.

    ``runtime`` is the :class:`RuntimeManager` (only ``status(ref)`` and ``start(ref, timeout=,
    window=)`` are used). ``shardx`` is an optional ShardX client (sync or async) used for
    ``shardx:<id-or-name>`` refs.
    """

    def __init__(
        self,
        store: "Store",
        runtime: "RuntimeManager",
        shardx: "ShardXClient | None" = None,
        *,
        start_timeout: float = 60.0,
        connect_timeout: float = 30.0,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.shardx = shardx
        self.start_timeout = start_timeout
        self.connect_timeout = connect_timeout
        self._playwright: Playwright | None = None
        self._pw_lock = asyncio.Lock()
        self._sessions: dict[str, ProfileSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._shardx_alias: dict[str, str] = {}

    async def __aenter__(self) -> "BrowserManager":
        await self._ensure_playwright()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Disconnect every session and stop the Playwright driver (browsers keep running)."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)
        if self._playwright is not None:
            pw, self._playwright = self._playwright, None
            try:
                await pw.stop()
            except Exception as exc:  # driver already gone
                log.debug("playwright stop: %s", exc)

    async def _ensure_playwright(self) -> Playwright:
        async with self._pw_lock:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            return self._playwright

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    @property
    def sessions(self) -> dict[str, ProfileSession]:
        """Currently cached sessions by key (read-only view)."""
        return dict(self._sessions)

    # ------------------------------------------------------------------ sessions

    async def session(self, ref: str, *, autostart: bool = True, window: WindowMode | None = None) -> ProfileSession:
        """Connected session for a profile ref (id, name, id prefix) or ``shardx:<id-or-name>``.

        Reuses the cached connection unless it was disconnected or the runtime changed (Chrome was
        restarted: different pid / CDP port). With ``autostart`` a stopped profile is started
        (``window`` overrides its window mode for that run).
        """
        ref = (ref or "").strip()
        if is_shardx_ref(ref):
            return await self._shardx_session(ref[len(SHARDX_PREFIX):].strip(), autostart=autostart)
        profile = await _to_thread(self.store.get_profile, ref)
        key = profile.id
        async with self._lock(key):
            cached = self._sessions.get(key)
            info = await _to_thread(self.runtime.status, key)
            if cached is not None:
                if cached.is_connected and info is not None and _same_runtime(cached.runtime, info):
                    cached.runtime, cached.profile = info, profile
                    return cached
                log.info("%s: cached connection is stale; reconnecting", profile.name)
                await self._drop(key)
            if info is None:
                if not autostart:
                    raise ProfileNotRunningError(f"Profile '{profile.name}' is not running. Start it with profile_start.")
                info = await _to_thread(partial(self.runtime.start, key, timeout=self.start_timeout, window=window))
            try:
                session = await self._attach_profile(profile, info)
            except LaunchError:
                # The runtime may have died between status() and the attach: re-check once.
                fresh = await _to_thread(self.runtime.status, key)
                if fresh is None and autostart:
                    fresh = await _to_thread(partial(self.runtime.start, key, timeout=self.start_timeout, window=window))
                if fresh is None or _same_runtime(info, fresh):
                    raise
                session = await self._attach_profile(profile, fresh)
            self._sessions[key] = session
            return session

    async def _attach_profile(self, profile: Profile, info: RuntimeInfo) -> ProfileSession:
        endpoint = info.cdp_http_url or (f"http://127.0.0.1:{info.cdp_port}" if info.cdp_port else None)
        if not endpoint:
            raise LaunchError(f"Profile '{profile.name}' is running but has no DevTools endpoint yet. Try again.")
        downloads = await _to_thread(self.store.downloads_dir, profile.id)
        return await self._attach(
            key=profile.id, label=profile.name, endpoint=endpoint, runtime=info, profile=profile,
            downloads_dir=downloads, timezone=profile.launch.timezone,
        )

    async def _attach(self, *, key: str, label: str, endpoint: str, runtime: RuntimeInfo | None,
                      profile: Profile | None, downloads_dir: Path | None, timezone: str | None) -> ProfileSession:
        pw = await self._ensure_playwright()
        try:
            browser = await pw.chromium.connect_over_cdp(endpoint, no_defaults=True,
                                                         timeout=self.connect_timeout * 1000)
        except PlaywrightError as exc:
            raise LaunchError(f"Could not attach to '{label}' over CDP: {_first_line(exc)}") from None
        if not browser.contexts:
            await _quiet_close(browser)
            raise LaunchError(f"'{label}' exposes no default browser context (is it a headless shell?).")
        session = ProfileSession(
            key=key, label=label, browser=browser, context=browser.contexts[0], endpoint=endpoint,
            runtime=runtime, profile=profile, downloads_dir=downloads_dir, timezone=timezone,
        )
        try:
            await session.setup()
        except Exception:
            await session.close()
            raise
        log.info("attached to %s (%s)", label, key)
        return session

    async def _shardx_session(self, ref: str, *, autostart: bool) -> ProfileSession:
        if self.shardx is None:
            raise ProfilePilotError(
                "The ShardX integration is not enabled. Enable it in ProfilePilot's config (shardx.enabled) "
                "and save a ShardX API token with 'profilepilot shardx login'."
            )
        if not ref:
            raise NotFoundError("No ShardX profile given (use shardx:<name-or-id>).")
        alias = self._shardx_alias.get(ref.casefold())
        if alias:
            cached = self._sessions.get(alias)
            if cached is not None and cached.is_connected:
                return cached
        info = await _call(self.shardx.resolve, ref)
        sid = str(info.get("id") or "").strip()
        if not sid:
            raise NotFoundError(f"ShardX profile '{ref}' has no id.")
        name = str(info.get("name") or sid)
        key = SHARDX_PREFIX + sid
        self._shardx_alias[ref.casefold()] = key
        async with self._lock(key):
            cached = self._sessions.get(key)
            if cached is not None:
                if cached.is_connected:
                    return cached
                await self._drop(key)
            if autostart:
                cdp = await _call(self.shardx.start, sid, headless=False)
            else:
                getter = getattr(self.shardx, "cdp", None)
                cdp = await _call(getter, sid) if getter is not None else None
                if not cdp:
                    raise ProfileNotRunningError(f"ShardX profile '{name}' is not running. Start it with shardx_start.")
            endpoint = _shardx_endpoint(cdp or {})
            if not endpoint:
                raise LaunchError(f"ShardX did not report a DevTools endpoint for '{name}'.")
            session = await self._attach(key=key, label=f"ShardX {name}", endpoint=endpoint, runtime=None,
                                         profile=None, downloads_dir=None, timezone=None)
            self._sessions[key] = session
            return session

    async def disconnect(self, ref: str) -> None:
        """Drop the cached connection for ``ref`` (call after stopping a profile). Never raises
        for unknown refs."""
        ref = (ref or "").strip()
        keys: set[str] = set()
        if is_shardx_ref(ref):
            name = ref[len(SHARDX_PREFIX):].strip()
            alias = self._shardx_alias.pop(name.casefold(), None)
            keys.update(k for k in (alias, SHARDX_PREFIX + name) if k)
            keys.update(k for k, s in self._sessions.items() if k.startswith(SHARDX_PREFIX)
                        and s.label.casefold() == f"shardx {name}".casefold())
            for a, k in list(self._shardx_alias.items()):
                if k in keys:
                    self._shardx_alias.pop(a, None)
        else:
            try:
                keys.add((await _to_thread(self.store.get_profile, ref)).id)
            except ProfilePilotError:
                keys.add(ref.lower())
        for key in keys:
            if key in self._sessions:
                async with self._lock(key):
                    await self._drop(key)

    async def _drop(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session is not None:
            await session.close()


# ---------------------------------------------------------------------- helpers


def _same_runtime(old: RuntimeInfo | None, new: RuntimeInfo) -> bool:
    if old is None:
        return False
    return (old.chrome_pid, old.chrome_create_time, old.cdp_port, old.cdp_http_url) == (
        new.chrome_pid, new.chrome_create_time, new.cdp_port, new.cdp_http_url
    )


def _shardx_endpoint(cdp: dict[str, Any]) -> str | None:
    if cdp.get("http_url"):
        return str(cdp["http_url"])
    if cdp.get("port"):
        return f"http://127.0.0.1:{int(cdp['port'])}"
    if cdp.get("web_socket_debugger_url"):
        return str(cdp["web_socket_debugger_url"])
    return None


async def _to_thread(fn: Callable[..., Any], *args: Any) -> Any:
    return await anyio.to_thread.run_sync(partial(fn, *args))


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a sync (in a worker thread) or async client method."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))
    if inspect.isawaitable(result):
        result = await result
    return result


async def _safe_title(page: Page) -> str:
    try:
        return await asyncio.wait_for(page.title(), TITLE_TIMEOUT)
    except Exception:  # busy page, open dialog, navigation in flight
        return ""


def minimized_message(label: str) -> str:
    return (
        f"The window of '{label}' is minimized, and restoring it would take the user's keyboard focus. Ask the "
        "user to restore it, or use offscreen mode (profile_update window='offscreen', then profile_stop + "
        "profile_start). Reading tools (browser_read, browser_snapshot, browser_extract, browser_navigate) still work."
    )


async def _bring_to_front(page: Page) -> None:
    try:
        await page.bring_to_front()
    except PlaywrightError as exc:
        log.debug("bring_to_front failed: %s", _first_line(exc))


async def _quiet_close(browser: Browser) -> None:
    try:
        await browser.close()
    except PlaywrightError:
        pass


def _first_line(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return lines[0][:300] if lines else type(exc).__name__
