"""BrowserManager / ProfileSession against a real Chrome launched by the test (off-screen, isolated
user-data-dir, fixed CDP port). The RuntimeManager and the ShardX client are small stubs."""

import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field

import psutil
import pytest
import pytest_asyncio

from profilepilot.automation.content import InvalidTargetError, RefNotFoundError, snapshot
from profilepilot.automation.cookies import to_playwright_list, to_portable
from profilepilot.automation.manager import BrowserManager, ProfileSession
from profilepilot.errors import NotFoundError, ProfileNotRunningError, ProfilePilotError
from profilepilot.models import RuntimeInfo

from .chrome_helper import launch_chrome
from .fakes import OriginServer

pytestmark = [pytest.mark.chrome, pytest.mark.asyncio]

MAIN = """<!doctype html><html><head><title>Main page</title></head><body>
<h1>Main</h1>
<button onclick="document.getElementById('out').textContent='clicked ' + (++window.n)">Press me</button>
<div id="out">not yet</div>
<label>Name <input id="name"></label>
<a href="/popup" target="_blank">Open popup</a>
<button onclick="window.open('/other')">Window open</button>
<button onclick="alert('hello from alert')">Alert</button>
<script>window.n = 0;</script>
</body></html>"""


class StubRuntime:
    """Implements the two RuntimeManager methods BrowserManager uses."""

    def __init__(self, info: RuntimeInfo) -> None:
        self.info = info
        self.running = True
        self.starts = 0
        self.last_window = None

    def status(self, ref: str) -> RuntimeInfo | None:
        assert ref == self.info.profile_id
        return self.info if self.running else None

    def start(self, ref: str, *, timeout: float = 60.0, window=None) -> RuntimeInfo:
        assert ref == self.info.profile_id
        self.starts += 1
        self.last_window = window
        self.running = True
        return self.info


@dataclass
class StubShardX:
    """Sync ShardX client stub (resolve/start/cdp), pointing at the test Chrome."""

    http_url: str
    port: int
    calls: list[str] = field(default_factory=list)
    started: bool = False

    def resolve(self, ref: str) -> dict:
        self.calls.append(f"resolve:{ref}")
        if ref.lower() not in ("sx1", "work"):
            raise NotFoundError(f"ShardX profile '{ref}' not found.")
        return {"id": "sx1", "name": "Work"}

    def start(self, profile_id: str, headless: bool = False) -> dict:
        self.calls.append(f"start:{profile_id}:{headless}")
        self.started = True
        return {"port": self.port, "http_url": self.http_url, "web_socket_debugger_url": None}

    def cdp(self, profile_id: str) -> dict | None:
        self.calls.append(f"cdp:{profile_id}")
        return {"port": self.port, "http_url": self.http_url} if self.started else None


@dataclass
class Env:
    manager: BrowserManager
    runtime: StubRuntime
    chrome: object
    origin: OriginServer
    profile_id: str
    store: object


def _runtime_info(profile, chrome) -> RuntimeInfo:
    version = chrome.version_info()
    return RuntimeInfo(
        profile_id=profile.id, profile_name=profile.name, state="running", host_pid=os.getpid(),
        chrome_pid=chrome.proc.pid, chrome_create_time=psutil.Process(chrome.proc.pid).create_time(),
        cdp_port=chrome.port, cdp_http_url=chrome.http_url, cdp_ws_url=version["webSocketDebuggerUrl"],
        window="offscreen",
    )


@pytest_asyncio.fixture
async def env(store):
    profile = store.create_profile("Work", launch={"timezone": "Asia/Tokyo"})
    pages = {"/": MAIN, "/popup": "<title>Popup</title><p>popup page</p>", "/other": "<title>Other</title><p>other</p>",
             "/download": '<a download="hello.txt" href="data:text/plain,hello%20download">Get file</a>'}
    with OriginServer(pages) as origin, launch_chrome(
        store.user_data_dir(profile.id), "--disable-backgrounding-occluded-windows"
    ) as chrome:
        runtime = StubRuntime(_runtime_info(profile, chrome))
        manager = BrowserManager(store, runtime)  # type: ignore[arg-type]
        try:
            async with manager:
                yield Env(manager, runtime, chrome, origin, profile.id, store)
        finally:
            await manager.aclose()


async def _wait_for(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


async def test_attach_snapshot_click_type_and_native_webdriver(env: Env):
    session = await env.manager.session("work")  # name lookup is case-insensitive
    assert isinstance(session, ProfileSession)
    assert env.runtime.starts == 0
    assert session.key == env.profile_id and session.label == "Work"
    assert session.context is session.browser.contexts[0]
    page = await session.page()
    await page.goto(env.origin.url + "/")
    assert await page.evaluate("navigator.webdriver") is False
    assert await page.evaluate("document.visibilityState") == "visible"

    snap = await snapshot(page)
    assert "[ref=" in snap
    button = re.search(r'button "Press me" \[ref=(e\d+)\]', snap).group(1)
    textbox = re.search(r'textbox "Name" \[ref=(e\d+)\]', snap).group(1)
    await (await session.locate(page, button, None)).click(timeout=5000)
    assert await page.locator("#out").text_content() == "clicked 1"
    await (await session.locate(page, f"[ref={textbox}]", None)).fill("Ada Lovelace", timeout=5000)
    assert await page.input_value("#name") == "Ada Lovelace"
    await (await session.locate(page, None, "text=Press me")).click(timeout=5000)
    assert await page.locator("#out").text_content() == "clicked 2"

    # opt-in timezone override applies to existing and new pages
    assert await page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == "Asia/Tokyo"
    other = await session.new_tab(env.origin.url + "/other")
    await _wait_for(lambda: other.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone === 'Asia/Tokyo'"))


async def test_stale_and_invalid_refs_fail_fast_with_a_clear_message(env: Env):
    session = await env.manager.session(env.profile_id)
    page = await session.page()
    await page.goto(env.origin.url + "/")
    snap = await snapshot(page)
    button = re.search(r'button "Press me" \[ref=(e\d+)\]', snap).group(1)
    await page.goto(env.origin.url + "/other")
    started = time.monotonic()
    with pytest.raises(RefNotFoundError, match="new browser_snapshot"):
        await session.locate(page, button, None)
    assert time.monotonic() - started < 3
    with pytest.raises(InvalidTargetError):
        await session.locate(page, None, None)
    with pytest.raises(InvalidTargetError):
        await session.locate(page, "#not-a-ref", None)


async def test_tabs_new_select_close(env: Env):
    session = await env.manager.session(env.profile_id)
    first = await session.page()
    await first.goto(env.origin.url + "/")
    second = await session.new_tab(env.origin.url + "/other")
    assert await session.page() is second
    tabs = await session.tabs()
    assert [t["active"] for t in tabs] == [False, True]
    assert tabs[0]["title"] == "Main page" and tabs[1]["title"] == "Other"
    assert tabs[1]["url"] == env.origin.url + "/other"

    assert await session.select_tab(0) is first
    assert (await session.page()) is first
    assert await first.evaluate("document.visibilityState") == "visible"

    # page(tab=N) acts on that tab, makes it active and brings it to the front
    assert await session.page(1) is second
    assert await second.evaluate("document.visibilityState") == "visible"
    assert [t["active"] for t in await session.tabs()] == [False, True]

    await session.close_tab(1)
    assert second.is_closed()
    assert len(await session.tabs()) == 1
    assert await session.page() is first

    # the last tab is never closed: it is blanked instead
    await session.close_tab(0)
    assert not first.is_closed()
    tabs = await session.tabs()
    assert len(tabs) == 1 and tabs[0]["url"] == "about:blank" and tabs[0]["active"]

    with pytest.raises(NotFoundError):
        await session.select_tab(5)
    with pytest.raises(NotFoundError):
        await session.page(-1)


async def test_popups_become_the_active_tab(env: Env):
    session = await env.manager.session(env.profile_id)
    page = await session.page()
    await page.goto(env.origin.url + "/")
    snap = await snapshot(page)
    link = re.search(r'link "Open popup" \[ref=(e\d+)\]', snap).group(1)
    await (await session.locate(page, link, None)).click(timeout=5000)
    popup = await _wait_for(_active_with_suffix(session, "/popup"))
    assert await popup.title() == "Popup"
    tabs = await session.tabs()
    assert len(tabs) == 2 and tabs[1]["active"]

    await session.select_tab(0)
    await page.click("text=Window open", timeout=5000)
    await _wait_for(_active_with_suffix(session, "/other"))
    assert [t["active"] for t in await session.tabs()] == [False, False, True]

    # closing the active popup falls back to a neighbour
    await session.close_tab(2)
    assert (await session.page()) is popup


def _active_with_suffix(session: ProfileSession, suffix: str):
    async def check():
        page = await session.page()
        return page if page.url.endswith(suffix) else None

    return check


async def test_dialogs_are_answered_and_recorded(env: Env):
    session = await env.manager.session(env.profile_id)
    page = await session.page()
    await page.goto(env.origin.url + "/")
    await page.click("text=Alert", timeout=5000)
    await _wait_for(lambda: _truthy(session.dialogs))
    assert session.drain_dialogs() == [{"type": "alert", "message": "hello from alert", "url": env.origin.url + "/"}]
    assert list(session.dialogs) == []
    assert await page.evaluate("1 + 1") == 2  # the page is not blocked


async def _truthy(value):
    return bool(value)


async def test_reconnects_when_disconnected_or_runtime_changed(env: Env):
    s1 = await env.manager.session(env.profile_id)
    assert await env.manager.session(env.profile_id) is s1  # cached

    await s1.browser.close()  # drop the CDP connection behind the manager's back
    assert not s1.is_connected
    with pytest.raises(ProfilePilotError):
        await s1.page()
    s2 = await env.manager.session(env.profile_id)
    assert s2 is not s1 and s2.is_connected
    page = await s2.page()
    await page.goto(env.origin.url + "/other")
    assert await page.title() == "Other"

    # Chrome "restarted": same profile, different pid -> the cached connection is replaced
    env.runtime.info = env.runtime.info.model_copy(update={"chrome_create_time": 1.0})
    s3 = await env.manager.session(env.profile_id)
    assert s3 is not s2 and not s2.is_connected and s3.is_connected

    # concurrent calls share one connection (per-profile lock)
    await env.manager.disconnect(env.profile_id)
    results = await asyncio.gather(*(env.manager.session(env.profile_id) for _ in range(5)))
    assert all(r is results[0] for r in results)


async def test_autostart_and_not_running(env: Env):
    env.runtime.running = False
    with pytest.raises(ProfileNotRunningError):
        await env.manager.session(env.profile_id, autostart=False)
    assert env.runtime.starts == 0
    session = await env.manager.session(env.profile_id, window="offscreen")
    assert env.runtime.starts == 1 and env.runtime.last_window == "offscreen"
    assert session.is_connected
    with pytest.raises(NotFoundError):
        await env.manager.session("no-such-profile")


async def test_disconnect_and_close_never_close_the_browser(env: Env):
    session = await env.manager.session(env.profile_id)
    await (await session.page()).goto(env.origin.url + "/other")
    await env.manager.disconnect("Work")
    assert not session.is_connected
    assert env.manager.sessions == {}
    assert env.chrome.version_info()["Browser"]  # Chrome still answers
    await env.manager.disconnect("unknown-profile")  # no error

    again = await env.manager.session(env.profile_id)
    tabs = await again.tabs()
    assert any(t["url"].endswith("/other") for t in tabs)  # same browser, same tabs
    await env.manager.aclose()
    assert env.chrome.proc.poll() is None and env.chrome.version_info()["Browser"]


async def test_downloads_land_in_the_profile_downloads_folder(env: Env):
    session = await env.manager.session(env.profile_id)
    assert session.downloads_dir == env.store.downloads_dir(env.profile_id)
    assert session.downloads_dir.is_dir()
    page = await session.page()
    await page.goto(env.origin.url + "/download")
    await page.click("text=Get file", timeout=5000)
    target = session.downloads_dir / "hello.txt"

    async def landed():
        return target.is_file() and target.read_text() == "hello download"

    await _wait_for(landed, timeout=15)


async def test_cookie_round_trip_through_the_profile_context(env: Env):
    session = await env.manager.session(env.profile_id)
    expires = int(time.time()) + 3600
    cookies = [
        {"domain": "127.0.0.1", "name": "sid", "value": "s3cr3t", "path": "/", "expires": expires,
         "secure": False, "httpOnly": True, "sameSite": "Lax"},
        {"domain": ".example.test", "name": "pref", "value": "dark", "path": "/", "expires": None,
         "secure": True, "httpOnly": False, "sameSite": "None"},
    ]
    await session.context.add_cookies(to_playwright_list(cookies))
    back = sorted((to_portable(c) for c in await session.context.cookies()), key=lambda c: c["name"])
    assert [(c["domain"], c["name"], c["value"], c["expires"], c["httpOnly"], c["secure"], c["sameSite"]) for c in back] == [
        (".example.test", "pref", "dark", None, False, True, "None"),
        ("127.0.0.1", "sid", "s3cr3t", expires, True, False, "Lax"),
    ]
    page = await session.page()
    await page.goto(env.origin.url + "/echo")
    assert "sid=s3cr3t" in await page.content()


async def test_shardx_refs_use_the_shardx_client(env: Env):
    shardx = StubShardX(env.chrome.http_url, env.chrome.port)
    manager = BrowserManager(env.store, env.runtime, shardx)  # type: ignore[arg-type]
    try:
        with pytest.raises(ProfileNotRunningError):
            await manager.session("shardx:Work", autostart=False)
        session = await manager.session("shardx:Work")
        assert session.key == "shardx:sx1" and session.label == "ShardX Work" and session.runtime is None
        assert "start:sx1:False" in shardx.calls
        calls = len(shardx.calls)
        assert await manager.session("shardx:work") is session  # cached via alias: no ShardX calls
        assert len(shardx.calls) == calls
        page = await session.page()
        await page.goto(env.origin.url + "/other")
        assert await page.title() == "Other"
        with pytest.raises(NotFoundError):
            await manager.session("shardx:missing")
        await manager.disconnect("shardx:Work")
        assert manager.sessions == {}
    finally:
        await manager.aclose()
    with pytest.raises(ProfilePilotError, match="ShardX integration is not enabled"):
        await env.manager.session("shardx:Work")


def _chrome_windows(pid: int) -> list[int]:
    import win32gui
    import win32process

    found: list[int] = []

    def visit(hwnd, _arg):
        if (win32process.GetWindowThreadProcessId(hwnd)[1] == pid and win32gui.GetClassName(hwnd) == "Chrome_WidgetWin_1"
                and win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd)):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(visit, None)
    return found


@pytest.mark.skipif(sys.platform != "win32", reason="Windows window management")
async def test_minimized_window_never_takes_the_focus(env: Env):
    """Restoring a minimized window (bringToFront, setWindowBounds, even SW_SHOWNOACTIVATE) gives
    Chrome the keyboard focus: reading works on the hidden page, actions get a clear error."""
    import win32con
    import win32gui

    session = await env.manager.session(env.profile_id)
    page = await session.page()
    await page.goto(env.origin.url + "/")
    second = await session.new_tab(env.origin.url + "/other")
    hwnd = _chrome_windows(env.chrome.proc.pid)[0]
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    await _wait_for(lambda: second.evaluate("document.visibilityState === 'hidden'"))
    foreground = win32gui.GetForegroundWindow()

    assert await session.page(interactive=False) is second  # read-only callers work on the hidden page
    assert await second.title() == "Other"
    with pytest.raises(ProfilePilotError, match="minimized"):
        await session.page()
    assert await session.select_tab(0) is page  # no bringToFront while minimized
    with pytest.raises(ProfilePilotError, match="minimized"):
        await session.page(0)
    await session.close_tab(0)
    assert win32gui.IsIconic(hwnd)
    assert win32gui.GetForegroundWindow() == foreground != hwnd
