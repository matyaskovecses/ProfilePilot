"""RuntimeManager + host against the real installed Chrome (off-screen windows, temp stores).

Every test cleans up the processes it started, even on failure: the fixture stops all hosts and
then kills any process whose command line mentions the test's temporary directory (only our own
hosts and Chromes can match it - the user's browser is never touched).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Coroutine, Iterator

import psutil
import pytest

from profilepilot.browser.control import cdp_browser_close
from profilepilot.browser.host import wait_for_devtools
from profilepilot.browser.runtime import RuntimeManager, kill_tree, process_alive, process_tree
from profilepilot.errors import ConflictError, LaunchError, ProfileNotRunningError, RestartRequiredError
from profilepilot.models import RuntimeInfo
from profilepilot.proxy.url import ProxyEndpoint
from profilepilot.store import Store

from .chrome_helper import find_test_browser, free_port, launch_chrome
from .fakes import FakeSocks5Server, OriginServer

WEBRTC_JS = """async () => {
  const pc = new RTCPeerConnection({iceServers: []});
  pc.createDataChannel('x');
  const found = [];
  const done = new Promise(res => {
    pc.onicecandidate = e => e.candidate ? found.push(e.candidate.candidate) : res();
    setTimeout(res, 4000);
  });
  await pc.setLocalDescription(await pc.createOffer());
  await done;
  pc.close();
  return found;
}"""


# --------------------------------------------------------------------------- fixtures


def _kill_leftovers(marker: Path) -> None:
    """Kill processes started by this test (their command line contains our temp dir)."""
    needle = str(marker).lower()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
        except psutil.Error:
            continue
        if needle in cmdline and proc.pid != psutil.Process().pid:
            kill_tree(proc.pid)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Iterator[tuple[Store, RuntimeManager]]:
    find_test_browser()  # skips when no Chromium-family browser is installed
    home = tmp_path / "home"
    monkeypatch.setenv("PROFILEPILOT_HOME", str(home))
    store = Store(home)
    config = store.load_config()
    config.default_window = "offscreen"  # never pop windows up on the user's screen
    store.save_config(config)
    manager = RuntimeManager(store)
    try:
        yield store, manager
    finally:
        with contextlib.suppress(Exception):
            manager.stop_all(timeout=15)
        _kill_leftovers(tmp_path)


@pytest.fixture(scope="module")
def pw():
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        yield playwright
    finally:
        playwright.stop()


@contextlib.contextmanager
def attach(pw, info: RuntimeInfo):
    """Attach Playwright over CDP to the profile's persistent context (disconnects, never closes)."""
    browser = pw.chromium.connect_over_cdp(info.cdp_http_url, no_defaults=True)
    try:
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        yield page
    finally:
        browser.close()


class LoopThread:
    """An asyncio loop in a daemon thread, for the asyncio fake proxy servers."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(15)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    """``asyncio.run`` in a worker thread (sync Playwright owns this thread's event loop)."""
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _gone(pid: int, timeout: float) -> bool:
    try:
        psutil.Process(pid).wait(timeout)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        return False
    return True


# --------------------------------------------------------------------------- tests


@pytest.mark.chrome
def test_start_is_idempotent_native_offscreen_and_stops_cleanly(env, pw):
    store, rm = env
    profile = store.create_profile("alpha", launch={"window": "normal"})
    info = rm.start("alpha", window="offscreen")  # per-run override; the profile stays "normal"
    assert info.state == "running" and info.window == "offscreen" and info.relay_port is None
    assert info.browser_version and info.cdp_http_url == f"http://127.0.0.1:{info.cdp_port}"
    assert store.get_profile("alpha").launch.window == "normal"
    assert rm.start("alpha").chrome_pid == info.chrome_pid  # idempotent
    assert rm.status("alpha") == info
    assert [i.profile_id for i in rm.list_running()] == [profile.id]

    cmdline = psutil.Process(info.chrome_pid).cmdline()
    assert f"--remote-debugging-port={info.cdp_port}" in cmdline
    assert "--window-position=-32000,-32000" in cmdline
    assert not any(a.startswith(("--enable-automation", "--remote-allow-origins", "--headless")) for a in cmdline)
    if sys.platform == "win32":
        from profilepilot.browser import winjob

        host = psutil.Process(info.host_pid)
        assert info.chrome_pid in {c.pid for c in host.children()}
        assert winjob.kill_on_close_job_supported()

    with attach(pw, info) as page:
        state = page.evaluate(
            "() => ({webdriver: navigator.webdriver, vis: document.visibilityState, ua: navigator.userAgent})"
        )
    assert state["webdriver"] is False
    assert state["vis"] == "visible"  # --disable-backgrounding-occluded-windows keeps off-screen pages live
    assert "Headless" not in state["ua"]

    with pytest.raises(RestartRequiredError):
        rm.set_upstream("alpha", None)
    assert rm.relay_stats("alpha") is None

    log_text = store.host_log(profile.id).read_text(encoding="utf-8")
    assert info.control_token and info.control_token not in log_text

    assert rm.stop("alpha") is True
    assert rm.status("alpha") is None
    assert not store.runtime_file(profile.id).exists()
    assert not process_alive(info.chrome_pid, info.chrome_create_time)
    assert not psutil.pid_exists(info.host_pid) or _gone(info.host_pid, 5)
    assert rm.stop("alpha") is False
    with pytest.raises(ProfileNotRunningError):
        rm.set_upstream("alpha", None)
    assert store.get_profile("alpha").last_started_at is not None


@pytest.mark.chrome
def test_session_cookies_and_tabs_survive_restart(env, pw):
    store, rm = env
    store.create_profile("sess")
    with OriginServer() as origin:
        info = rm.start("sess")
        with attach(pw, info) as page:
            page.goto(f"{origin.url}/set-cookie?pers=1")  # persistent (Max-Age)
            page.evaluate("() => { document.cookie = 'sess=1; path=/'; }")  # session cookie
            page.goto(f"{origin.url}/landing")
        restarted = rm.restart("sess")  # graceful stop (Browser.close) + start with --restore-last-session
        assert restarted.chrome_pid != info.chrome_pid
        with attach(pw, restarted) as page:
            context = page.context
            deadline = time.monotonic() + 10
            while not any(p.url.endswith("/landing") for p in context.pages) and time.monotonic() < deadline:
                time.sleep(0.1)
            urls = [p.url for p in context.pages]
            names = {c["name"] for c in context.cookies(origin.url)}
    assert any(u.endswith("/landing") for u in urls), urls
    assert "about:blank" not in urls  # no extra blank tab accumulates on restore
    assert {"pers", "sess"} <= names


@pytest.mark.chrome
def test_killing_the_host_kills_chrome_and_status_cleans_up(env, tmp_path):
    store, rm = env
    profile = store.create_profile("victim")
    info = rm.start("victim")
    tree = process_tree(info.chrome_pid)
    assert len(tree) > 1
    psutil.Process(info.host_pid).kill()
    _gone_procs, alive = psutil.wait_procs(tree, timeout=10)
    if sys.platform == "win32":
        assert not alive, "the kill-on-close job must take Chrome down with the host"
    assert store.runtime_file(profile.id).exists()  # the host had no chance to clean up
    assert rm.status("victim") is None
    assert not store.runtime_file(profile.id).exists()

    # A runtime.json left by a long-dead host is stale too.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(30)
    stale = RuntimeInfo(profile_id=profile.id, profile_name="victim", state="running", host_pid=dead.pid,
                        chrome_pid=dead.pid, cdp_port=free_port())
    store.runtime_file(profile.id).write_text(stale.model_dump_json(), encoding="utf-8")
    assert rm.list_running() == []
    assert not store.runtime_file(profile.id).exists()
    assert rm.start("victim").state == "running"  # and the profile starts normally again


@pytest.mark.chrome
def test_proxied_profile_remote_dns_auth_webrtc_and_live_upstream_switch(env, pw):
    store, rm = env
    loop = LoopThread()
    up1 = loop.run(FakeSocks5Server(username="user", password="p@ss:word").start())
    up2 = loop.run(FakeSocks5Server(username="second", password="pw-two!").start())
    try:
        with OriginServer() as origin, OriginServer() as origin2:
            p1 = store.add_proxy(ProxyEndpoint("socks5", "127.0.0.1", up1.port, "user", "p@ss:word"), "up-one")
            p2 = store.add_proxy(ProxyEndpoint("socks5", "127.0.0.1", up2.port, "second", "pw-two!"), "up-two")
            profile = store.create_profile("proxied", proxy_id=p1.id)
            info = rm.start("proxied")
            assert info.relay_port and info.proxy_id == p1.id and info.upstream and "p@ss" not in info.upstream

            cmdline = psutil.Process(info.chrome_pid).cmdline()
            assert f"--proxy-server=socks5://127.0.0.1:{info.relay_port}" in cmdline
            assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in cmdline and "--disable-quic" in cmdline
            assert not any("p@ss" in a for a in cmdline)

            with attach(pw, info) as page:
                # localhost.test only resolves at the fake proxy: proves remote DNS + authenticated upstream
                page.goto(f"http://localhost.test:{origin.port}/via-one")
                assert "echo /via-one" in page.content()
                assert ("localhost.test", origin.port) in up1.targets
                assert page.evaluate(WEBRTC_JS) == []  # no UDP candidates outside the proxy

                stats = rm.relay_stats("proxied")
                assert stats and stats["connections_total"] >= 1 and stats["proxy_id"] == p1.id

                rm.set_upstream("proxied", "up-two")  # live switch, new connections only
                switched = rm.status("proxied")
                assert switched and switched.proxy_id == p2.id and "pw-two" not in (switched.upstream or "")
                page.goto(f"http://localhost.test:{origin2.port}/via-two")
                assert "echo /via-two" in page.content()
            assert ("localhost.test", origin2.port) in up2.targets
            assert ("localhost.test", origin2.port) not in up1.targets
            assert up1.auth_failures == up2.auth_failures == 0

            files = [store.host_log(profile.id), store.runtime_file(profile.id)]
            text = "".join(f.read_text(encoding="utf-8") for f in files if f.exists())
            assert "p@ss:word" not in text and "pw-two!" not in text
            assert rm.stop("proxied")
    finally:
        loop.run(up1.stop())
        loop.run(up2.stop())
        loop.close()


@pytest.mark.chrome
def test_two_profiles_concurrently_isolated_cookies_and_max_running(env, pw):
    store, rm = env
    config = store.load_config()
    config.max_running = 2
    store.save_config(config)
    for name in ("one", "two", "three"):
        store.create_profile(name)
    with OriginServer() as origin:
        with concurrent.futures.ThreadPoolExecutor(3) as pool:  # concurrent starts are idempotent too
            one_a, one_b, two_f = pool.submit(rm.start, "one"), pool.submit(rm.start, "one"), pool.submit(rm.start, "two")
            one, two = one_a.result(), two_f.result()
            assert one_b.result().chrome_pid == one.chrome_pid
        assert one.cdp_port != two.cdp_port and one.chrome_pid != two.chrome_pid
        with attach(pw, one) as page:
            page.goto(f"{origin.url}/set-cookie?who=one")
            assert [c["value"] for c in page.context.cookies(origin.url)] == ["one"]
        with attach(pw, two) as page:
            page.goto(f"{origin.url}/peek")
            assert page.context.cookies(origin.url) == []
            assert "cookie=" in page.content() and "who=one" not in page.content()
    with pytest.raises(ConflictError, match="max_running"):
        rm.start("three")
    assert not store.runtime_file(store.get_profile("three").id).exists()
    assert sorted(rm.stop_all()) == ["one", "two"]
    assert rm.list_running() == []


@pytest.mark.chrome
def test_browser_closed_by_the_user_ends_the_host(env):
    store, rm = env
    profile = store.create_profile("closer")
    info = rm.start("closer")
    assert info.cdp_ws_url and cdp_browser_close(info.cdp_ws_url)  # same as closing the last window
    assert _gone(info.host_pid, 15)
    assert rm.status("closer") is None
    assert not store.runtime_file(profile.id).exists()
    assert store.get_profile("closer").total_runtime_s >= 0


@pytest.mark.chrome
def test_profile_already_open_in_a_foreign_chrome(env):
    store, rm = env
    profile = store.create_profile("busy")
    with launch_chrome(store.user_data_dir(profile.id)):
        with pytest.raises(LaunchError, match="already open in another browser"):
            rm.start("busy", timeout=30)
    assert rm.status("busy") is None
    assert not store.runtime_file(profile.id).exists()


@pytest.mark.chrome
def test_second_chrome_on_the_same_user_data_dir_is_detected_as_handoff(tmp_path):
    browser = find_test_browser()
    udd = tmp_path / "udd"
    with launch_chrome(udd):
        port = free_port()
        second = subprocess.Popen(
            [browser.path, f"--user-data-dir={udd}", f"--remote-debugging-port={port}", "--no-first-run",
             "--window-position=-32000,-32000", "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        try:
            with pytest.raises(LaunchError, match="handed the launch over"):
                run_async(wait_for_devtools(second, port, timeout=20))
        finally:
            kill_tree(second.pid)


def test_bogus_browser_path_raises_launch_error(env, tmp_path):
    store, rm = env
    bogus = tmp_path / "nope" / "chrome.exe"
    profile = store.create_profile("bogus", browser=str(bogus))
    with pytest.raises(LaunchError) as err:
        rm.start("bogus", timeout=30)
    message = str(err.value)
    assert "Could not start profile 'bogus'" in message and str(bogus) in message and "not found" in message
    assert rm.status("bogus") is None
    assert not store.runtime_file(profile.id).exists()


def test_executable_that_is_not_a_browser_fails_fast(env):
    store, rm = env
    store.create_profile("notchrome", browser=sys.executable)  # rejects Chrome's switches and exits 2
    started = time.monotonic()
    with pytest.raises(LaunchError, match="exited during startup"):
        rm.start("notchrome", timeout=30)
    assert time.monotonic() - started < 20
    assert rm.list_running() == []
