"""Scrapling sessions bound to a profile (integrations/scrapling.py) against a real Chrome.

Setup per test: an authenticated ``FakeSocks5Server`` plays the profile's upstream proxy, a
``LocalRelay`` in front of it plays the profile host's relay, and a throwaway Chrome (own
user-data-dir, fixed CDP port, off-screen) uses that relay for all traffic (loopback included)
and plays the profile's browser. The runtime is a :class:`StubRuntime` reporting that Chrome as
the running profile, so no ProfilePilot host process is involved.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request
from typing import Any, Coroutine, Iterator, TypeVar

import pytest

from profilepilot import ProfilePilot
from profilepilot.errors import ProfilePilotError
from profilepilot.proxy.relay import LocalRelay
from profilepilot.proxy.url import ProxyEndpoint

pytest.importorskip("scrapling.fetchers", reason="profilepilot[scrapling] is not installed")

from profilepilot.automation.driver import driver_of  # noqa: E402
from profilepilot.integrations.scrapling import (  # noqa: E402
    SESSION_DRIVER,
    AsyncProfileSession,
    ProfileFetcherSession,
    ProfileSession,
    fetch,
    fetcher_session,
)

from .chrome_helper import LaunchedChrome, launch_chrome  # noqa: E402
from .fakes import FakeSocks5Server, OriginServer  # noqa: E402
from .test_client import StubRuntime, runtime_info  # noqa: E402

T = TypeVar("T")

# Everything a page could use to tell an automated/overridden browser from the user's own.
FINGERPRINT_JS = """() => ({
    webdriver: navigator.webdriver,
    dark: matchMedia('(prefers-color-scheme: dark)').matches,
    light: matchMedia('(prefers-color-scheme: light)').matches,
    dpr: devicePixelRatio,
    ua: navigator.userAgent,
    lang: navigator.language,
    languages: navigator.languages,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: [screen.width, screen.height],
    touch: navigator.maxTouchPoints,
})"""


# --------------------------------------------------------------------------- infrastructure


class LoopThread:
    """An asyncio loop on a background thread (hosts the relay and the fake upstream)."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.loop.set_exception_handler(self._on_error)
        self._thread = threading.Thread(target=self.loop.run_forever, name="relay-loop", daemon=True)
        self._thread.start()

    @staticmethod
    def _on_error(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        # Chrome resets idle proxy connections; the Proactor transport logs that as an error.
        if not isinstance(context.get("exception"), ConnectionError):
            loop.default_exception_handler(context)

    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 15.0) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        async def cancel_leftovers() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            self.run(cancel_leftovers(), timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(5)
            self.loop.close()


class Proxied:
    """Upstream SOCKS5 server (with auth) + the credential-free relay in front of it."""

    def __init__(self) -> None:
        self.loops = LoopThread()
        self.socks = self.loops.run(FakeSocks5Server().start())
        self.endpoint = ProxyEndpoint("socks5", "127.0.0.1", self.socks.port, self.socks.username, self.socks.password)
        self.relay = LocalRelay(self.endpoint)
        self.loops.run(self.relay.start())

    def targets_to(self, port: int) -> int:
        return sum(1 for _host, p in list(self.socks.targets) if p == port)

    def close(self) -> None:
        try:
            self.loops.run(self.relay.stop())
            self.loops.run(asyncio.wait_for(self.socks.stop(), 5))
        finally:
            self.loops.close()


@pytest.fixture
def origin() -> Iterator[OriginServer]:
    with OriginServer({"/fp": "<html><body><h1 id=t>fingerprint</h1></body></html>"}) as server:
        yield server


@pytest.fixture
def proxied() -> Iterator[Proxied]:
    p = Proxied()
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def chrome(tmp_path, origin, proxied) -> Iterator[LaunchedChrome]:
    """The profile's browser. Its first tab sets ``pp_sid`` through a real Set-Cookie response."""
    with launch_chrome(
        tmp_path / "udd",
        f"--proxy-server=socks5://127.0.0.1:{proxied.relay.port}",
        "--proxy-bypass-list=<-loopback>",  # send even 127.0.0.1 through the relay
        url=f"{origin.url}/set-cookie?pp_sid=abc123",
    ) as launched:
        yield launched


@pytest.fixture
def pilot(tmp_path, chrome, proxied) -> ProfilePilot:
    """A ProfilePilot whose profile 'scrape' is "running" in ``chrome`` behind ``proxied``."""
    stub = StubRuntime()
    pp = ProfilePilot(tmp_path / "pp-home", runtime=stub)
    profile = pp.create("scrape", proxy=proxied.endpoint.to_url(), window="offscreen")
    stub.infos[profile.id] = runtime_info(profile.id, profile.name, chrome, relay_port=proxied.relay.port,
                                          upstream=proxied.endpoint.redacted())
    deadline = time.monotonic() + 20
    while not any(c["name"] == "pp_sid" for c in pp.cookies("scrape")):
        assert time.monotonic() < deadline, "the start page did not set its cookie"
        time.sleep(0.2)
    return pp


def tabs(chrome: LaunchedChrome) -> list[str]:
    with urllib.request.urlopen(f"{chrome.http_url}/json/list", timeout=5) as resp:
        return [t["url"] for t in json.loads(resp.read()) if t.get("type") == "page"]


def native_fingerprint(chrome: LaunchedChrome) -> dict[str, Any]:
    """Ground truth: evaluate in the tab Chrome opened itself, over raw CDP (no Playwright)."""
    from websockets.sync.client import connect

    with urllib.request.urlopen(f"{chrome.http_url}/json/list", timeout=5) as resp:
        page = next(t for t in json.loads(resp.read()) if t.get("type") == "page")
    with connect(page["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": f"({FINGERPRINT_JS})()", "returnByValue": True}}))
        while True:
            msg = json.loads(ws.recv(timeout=10))
            if msg.get("id") == 1:
                return msg["result"]["result"]["value"]


def body(response: Any) -> str:
    """Visible text of a Scrapling response (rendered DOM for browsers, raw page for curl)."""
    return str(response.get_all_text(strip=True))


# --------------------------------------------------------------------------- browser sessions


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_async_session_reuses_profile_context_and_leaves_browser_running(pilot, chrome, origin, proxied):
    start_tab = tabs(chrome)
    assert len(start_tab) == 1 and "/set-cookie" in start_tab[0]
    async with AsyncProfileSession("scrape", pilot=pilot, max_pages=2) as session:
        assert session.profile.name == "scrape"
        assert driver_of(session.context) == SESSION_DRIVER  # patchright by default: no Runtime.enable
        # the session drives the profile's own persistent context, not a fresh one
        assert any(c["name"] == "pp_sid" for c in await session.context.cookies(origin.url))
        page = await session.fetch(f"{origin.url}/echo")
        assert page.status == 200
        assert body(page) == "echo /echo cookie=pp_sid=abc123"
        second = await session.fetch(f"{origin.url}/again")
        assert "cookie=pp_sid=abc123" in body(second)
        assert any(u.endswith("/echo") or u.endswith("/again") for u in tabs(chrome))
    assert session.browser is None and session.context is None and session.playwright is None
    # the browser traffic went through the profile's relay and upstream proxy
    assert proxied.targets_to(origin.port) >= 3
    # closing the session closed only its own tabs; Chrome, the user's tab and the cookies remain
    assert chrome.proc.poll() is None and chrome.version_info()["Browser"]
    assert tabs(chrome) == start_tab
    assert [c["value"] for c in pilot.cookies("scrape", origin.url)] == ["abc123"]


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_page_fingerprint_is_the_native_one(pilot, chrome, origin):
    native = native_fingerprint(chrome)
    assert native["webdriver"] is False

    seen: dict[str, Any] = {}

    async def grab(page: Any) -> None:
        seen.update(await page.evaluate(FINGERPRINT_JS))

    async with AsyncProfileSession("scrape", pilot=pilot) as session:
        page = await session.fetch(f"{origin.url}/fp", page_action=grab, network_idle=True)
    assert page.css("#t::text").get() == "fingerprint"
    assert seen == native  # no dark scheme, DPR, UA, locale or timezone override
    request = next(r for r in origin.requests if r["path"] == "/fp")
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert "referer" not in headers  # google_search is off by default
    assert headers["user-agent"] == native["ua"]


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_vanilla_scrapling_cdp_attach_would_alter_the_page(pilot, chrome, origin):
    """Control experiment: why the integration subclasses Scrapling instead of passing cdp_url."""
    from scrapling.fetchers import AsyncDynamicSession

    native = native_fingerprint(chrome)
    seen: dict[str, Any] = {}

    async def grab(page: Any) -> None:
        seen.update(await page.evaluate(FINGERPRINT_JS))

    async with AsyncDynamicSession(cdp_url=chrome.http_url, retries=1) as session:
        page = await session.fetch(f"{origin.url}/vanilla", page_action=grab)
    assert "cookie=pp_sid" not in body(page)  # new_context(): the profile's cookies are gone
    assert seen["dark"] is True and seen["light"] is False and seen["dpr"] == 2  # forced context overrides
    assert native["webdriver"] is False  # (the overrides above are the part that differs per machine)
    assert chrome.proc.poll() is None


@pytest.mark.chrome
def test_sync_profile_session(pilot, chrome, origin):
    start_tab = tabs(chrome)
    with ProfileSession("scrape", pilot=pilot) as session:
        page = session.fetch(f"{origin.url}/sync")
        assert body(page) == "echo /sync cookie=pp_sid=abc123"
    assert session.browser is None
    assert chrome.proc.poll() is None and tabs(chrome) == start_tab


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_fetch_helper_splits_session_and_fetch_options(pilot, chrome, origin):
    page = await fetch("scrape", f"{origin.url}/helper", pilot=pilot, retries=1, network_idle=True,
                       extra_headers={"X-Test": "1"})
    assert body(page) == "echo /helper cookie=pp_sid=abc123"
    assert origin.requests[-1]["headers"].get("X-Test") == "1"
    assert len(tabs(chrome)) == 1 and chrome.proc.poll() is None


@pytest.mark.chrome
def test_spider_with_profile_sessions(pilot, chrome, origin, proxied):
    """Scrapling spiders: register profile sessions in ``configure_sessions``."""
    from scrapling.spiders import Request, Spider

    class ProfileSpider(Spider):
        name = "profile-spider"
        start_urls = [f"{origin.url}/spider-start"]
        logging_level = 30

        def configure_sessions(self, manager):
            manager.add("browser", AsyncProfileSession("scrape", pilot=pilot), default=True)
            manager.add("http", fetcher_session("scrape", pilot=pilot), lazy=True)

        async def parse(self, response):
            yield {"via": "browser", "body": body(response)}
            yield Request(f"{origin.url}/spider-http", sid="http", callback=self.parse_http)

        async def parse_http(self, response):
            yield {"via": "http", "body": body(response)}

    result = ProfileSpider().start()
    items = {item["via"]: item["body"] for item in result.items}
    assert items == {
        "browser": "echo /spider-start cookie=pp_sid=abc123",
        "http": "echo /spider-http cookie=pp_sid=abc123",
    }
    assert proxied.targets_to(origin.port) >= 2
    assert chrome.proc.poll() is None and len(tabs(chrome)) == 1


# --------------------------------------------------------------------------- HTTP sessions (curl_cffi)


@pytest.mark.chrome
def test_fetcher_session_sends_profile_cookies_through_the_relay(pilot, origin, proxied):
    session = fetcher_session("scrape", pilot=pilot)
    assert isinstance(session, ProfileFetcherSession)
    assert session.proxy_url == proxied.relay.http_url  # credential-free local relay
    before = proxied.targets_to(origin.port)
    with session as client:
        resp = client.get(f"{origin.url}/plain")
        assert resp.status == 200
        assert body(resp) == "echo /plain cookie=pp_sid=abc123"
    assert proxied.targets_to(origin.port) == before + 1  # left through the profile's upstream
    headers = {k.lower(): v for k, v in origin.requests[-1]["headers"].items()}
    assert "referer" not in headers  # stealthy_headers (fake Google referer) is off by default
    # re-entering picks up cookies the browser received in the meantime
    pilot.set_cookies("scrape", [{"name": "later", "value": "2", "url": origin.url}])
    with session as client:
        assert "later=2" in body(client.get(f"{origin.url}/again"))


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_async_fetcher_session_with_write_back(pilot, origin, proxied):
    import anyio.to_thread

    before = proxied.targets_to(origin.port)
    async with fetcher_session("scrape", pilot=pilot, write_back=True) as client:
        first = await client.get(f"{origin.url}/set-cookie?from_http=42")
        assert first.status == 200
        echoed = await client.get(f"{origin.url}/echo")
        assert "pp_sid=abc123" in body(echoed) and "from_http=42" in body(echoed)
    assert proxied.targets_to(origin.port) == before + 2
    cookies = await anyio.to_thread.run_sync(lambda: pilot.cookies("scrape", origin.url))
    assert {c["name"]: c["value"] for c in cookies} == {"pp_sid": "abc123", "from_http": "42"}


# --------------------------------------------------------------------------- argument checks (no browser)


@pytest.fixture
def offline_pilot(tmp_path) -> ProfilePilot:
    stub = StubRuntime()
    pp = ProfilePilot(tmp_path / "offline", runtime=stub)
    profile = pp.create("offline")
    stub.infos[profile.id] = runtime_info(profile.id, profile.name, relay_port=1)
    return pp


@pytest.mark.parametrize("option", [
    {"proxy": "http://u:p@h:1"}, {"cdp_url": "http://127.0.0.1:9"}, {"useragent": "X"},
    {"locale": "de-DE"}, {"timezone_id": "UTC"}, {"cookies": [{"name": "a", "value": "b", "url": "http://x/"}]},
    {"additional_args": {"color_scheme": "dark"}}, {"user_data_dir": "C:/x"}, {"real_chrome": True},
    {"hide_canvas": True}, {"allow_webgl": False}, {"block_webrtc": True},  # stealth launch flags
])
def test_browser_sessions_refuse_profile_owned_options(offline_pilot, option):
    for cls in (AsyncProfileSession, ProfileSession):
        with pytest.raises(ProfilePilotError, match="does not accept") as info:
            cls("offline", pilot=offline_pilot, **option)
        assert "p@h" not in str(info.value)


def test_browser_sessions_ignore_launch_only_options_and_fail_fast_on_typos(offline_pilot):
    session = AsyncProfileSession("offline", pilot=offline_pilot, headless=True, google_search=True, max_pages=3,
                                  allow_webgl=True)  # the genuine browser's own setting: accepted
    assert session._config.headless is False and session._config.google_search is True
    assert session.max_pages == 3 and session._is_alive is False
    assert AsyncProfileSession("offline", pilot=offline_pilot)._config.google_search is False
    from profilepilot.errors import NotFoundError

    with pytest.raises(NotFoundError):
        AsyncProfileSession("no-such-profile", pilot=offline_pilot)


@pytest.mark.asyncio
async def test_fetch_refuses_a_per_request_proxy(offline_pilot):
    session = AsyncProfileSession("offline", pilot=offline_pilot)
    with pytest.raises(ProfilePilotError, match="per-request proxy"):
        await session.fetch("http://example.invalid/", proxy="http://other:1")


@pytest.mark.parametrize("key", ["proxy", "proxies", "proxy_auth"])
def test_fetcher_session_refuses_proxy_options(offline_pilot, key):
    value = {"proxy": "http://u:pw@h:1", "proxies": {"https": "http://h:1"}, "proxy_auth": ("u", "pw")}[key]
    with pytest.raises(ProfilePilotError, match="always use the profile's proxy"):
        fetcher_session("offline", pilot=offline_pilot, **{key: value})
    session = fetcher_session("offline", pilot=offline_pilot, impersonate="chrome", timeout=5)
    assert session.proxy_url == "http://127.0.0.1:1"
