"""Native-fingerprint regression probe (docs/audit/FIX-PLAN.md step 1; findings of
docs/FINGERPRINT-AUDIT.md).

A local probe page (:mod:`tests.native_probe`) measures, from inside the page, what an attached
automation client gives away: the ``Runtime.enable`` side channels (``Error.prepareStackTrace``
called by console / uncaught / rejection formatting, console timing - in the page and in a
dedicated worker), main-world DOM calls made by the reading tools, user activation, focus and the
request headers. Baselines are measured on the same machine in the same run, never as absolutes:

* ``B``  - a plain Chrome launched by :func:`tests.chrome_helper.launch_chrome` with the probe as its
  URL; nothing ever attaches (the DevTools HTTP endpoint is only polled).
* ``P0`` - a ProfilePilot profile started by the host with ``launch.start_url`` = the probe; no
  client ever attaches.
* ``P``  - a profile driven like an agent drives it: the MCP tool functions (``browser_navigate``,
  then the reading tools) over an in-process MCP client, once per CDP driver: patchright (the
  default since FIX-PLAN step 2) and the Playwright fallback, which stays detectable (F1, F3).

Assertions that fail today are ``xfail(strict=True)`` with the finding id; the fix step that
closes a finding removes its marker (a strict xfail that starts passing fails the run).
Off-screen windows only. ``document.hasFocus()`` is measured against the idle profile on the same
desktop: Chrome reports focus for its active window even off-screen and while another application
has the OS foreground (P0 and B are focused), so no visible window is needed (FIX-PLAN step 8).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import statistics
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import psutil
import pytest
from mcp import Client
from mcp.types import TextContent

from profilepilot.automation.driver import DRIVER, ENV_DRIVER, installed
from profilepilot.browser.runtime import RuntimeManager
from profilepilot.server.app import create_server
from profilepilot.store import Store

from .chrome_helper import find_test_browser, launch_chrome
from .native_probe import PAGE_GLOBAL, PAGE_GLOBAL_VALUE, ProbeServer

pytestmark = [pytest.mark.chrome]

F1 = "F1: the attached client's Runtime.enable is page-visible (prepareStackTrace, console timing, worker)"
F3 = "F3: the reading tools evaluate in the page's main world (trapped DOM calls with UtilityScript stacks)"
F4 = ("F4: the driver evaluates with Runtime.callFunctionOn(userGesture: true), which gives the page sticky user "
      "activation without any input (ProfilePilot's patchright patch 'evaluate-without-user-gesture' removes it)")


def drivers(xfail_playwright: str | None = None) -> list[Any]:
    """Both CDP drivers as parameters; the Playwright fallback is a strict xfail for ``xfail_playwright``."""
    params = []
    for name in ("patchright", "playwright"):
        marks = [pytest.mark.skipif(not installed(name), reason=f"{name} is not installed")]
        if name == "playwright" and xfail_playwright:
            marks.append(pytest.mark.xfail(strict=True, raises=AssertionError, reason=xfail_playwright))
        params.append(pytest.param(name, marks=marks))
    return params

#: Request headers that make up the browser's identity (network-estimate hints vary run to run).
IDENTITY_HEADERS = ("user-agent", "accept", "accept-language", "accept-encoding", "upgrade-insecure-requests",
                    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-ch-ua-full-version-list",
                    "sec-ch-ua-platform-version", "sec-ch-ua-arch", "sec-ch-ua-bitness", "sec-ch-ua-model",
                    "sec-ch-ua-wow64", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user")
TIMING_FACTOR, TIMING_SLACK_MS = 3.0, 5.0


@dataclass
class Run:
    """Everything one probe configuration recorded."""

    cfg: str
    version: dict[str, Any]
    """``/json/version`` of the browser (``Browser``, ``User-Agent``) - no CDP attach needed."""
    main: dict[str, Any]
    final: dict[str, Any]
    bfcache: dict[str, Any] | None
    start_headers: dict[str, str]
    probe_headers: dict[str, str]
    tools: dict[str, str] = field(default_factory=dict)

    def identity_headers(self, which: str) -> dict[str, str | None]:
        headers = self.start_headers if which == "start" else self.probe_headers
        return {name: headers.get(name) for name in IDENTITY_HEADERS}

    def timing_ms(self) -> float:
        """Median console-preview timing of the late samples (x100 console.debug of a big object)."""
        return statistics.median(s["consoleTimingMs"] for s in self.final["samples"])

    def cdp_flags(self) -> dict[str, bool]:
        """Every Runtime.enable detector of the run (True = a client was seen)."""
        return {
            "console prepareStackTrace at T0": self.main["early"]["prepareStackTrace"],
            "console prepareStackTrace after collecting": self.main["cdpAfter"]["prepareStackTrace"],
            "console prepareStackTrace, late samples": any(s["prepareStackTrace"] for s in self.final["samples"]),
            "uncaught exception prepareStackTrace": self.main["async"]["uncaughtPrepare"],
            "unhandled rejection prepareStackTrace": self.main["async"]["rejectionPrepare"],
            "dedicated worker prepareStackTrace": bool(self.main["worker"].get("prepareStackTrace")
                                                       or self.final["worker"].get("prepareStackTrace")),
        }


def _version(http_url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{http_url}/json/version", timeout=5) as resp:
        return json.loads(resp.read())


def _collect(probe: ProbeServer, cfg: str, version: dict[str, Any], tools: dict[str, str] | None = None) -> Run:
    record = probe.result(cfg)
    return Run(cfg=cfg, version=version, main=record["phases"]["main"], final=record["phases"]["final"],
               bfcache=record["phases"].get("bfcache"), start_headers=probe.headers(cfg, "/start"),
               probe_headers=probe.headers(cfg, "/probe"), tools=tools or {})


def _kill_leftovers(marker: Path) -> None:
    """Kill processes started here (their command line contains our temp dir) - never the user's."""
    from profilepilot.browser.runtime import kill_tree

    needle = str(marker).lower()
    me = psutil.Process().pid
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
        except psutil.Error:
            continue
        if needle in cmdline and proc.pid != me:
            kill_tree(proc.pid)


# ---------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Iterator[Path]:
    find_test_browser()
    path = tmp_path_factory.mktemp("native")
    try:
        yield path
    finally:
        with contextlib.suppress(Exception):
            RuntimeManager(Store(path / "home")).stop_all(timeout=15)
        _kill_leftovers(path)


@pytest.fixture(scope="module")
def home(root) -> Store:
    store = Store(root / "home")
    config = store.load_config()
    config.default_window = "offscreen"
    store.save_config(config)
    return store


@pytest.fixture(scope="module")
def probe() -> Iterator[ProbeServer]:
    with ProbeServer() as server:
        yield server


@pytest.fixture(scope="module")
def idle(root, home, probe) -> dict[str, Run]:
    """B (plain unattached Chrome) and P0 (host-launched profile, no client), side by side."""
    profile = home.create_profile("p0", launch={"window": "offscreen", "start_url": probe.url("P0")})
    runtime = RuntimeManager(home)
    with launch_chrome(root / "plain-udd", url=probe.url("B")) as plain:
        info = runtime.start(profile.id)
        try:
            for cfg in ("B", "P0"):
                probe.wait_phase(cfg, "main")
            time.sleep(2.0)  # a few late samples
            for cfg in ("B", "P0"):
                probe.finish(cfg)
            for cfg in ("B", "P0"):
                probe.wait_phase(cfg, "final")
            versions = {"B": plain.version_info(), "P0": _version(info.cdp_http_url)}
        finally:
            runtime.stop(profile.id)
    return {cfg: _collect(probe, cfg, versions[cfg]) for cfg in ("B", "P0")}


def _text(result: Any) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


async def _call(client: Client, name: str, args: dict[str, Any]) -> str:
    result = await client.call_tool(name, args)
    out = _text(result)
    if result.is_error:  # not an AssertionError: the xfail markers below only accept failed checks
        raise RuntimeError(f"{name}: {out}")
    return out


#: The reading tools that must not touch the page's main world (FIX-PLAN step 1).
READING_TOOLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("browser_snapshot", {}),
    ("browser_read", {}),
    ("browser_screenshot", {}),
    ("browser_scroll", {"direction": "down"}),
    ("browser_evaluate", {"expression": "document.title"}),
)


async def _attached_run(home: Store, probe: ProbeServer, cfg: str, *, driver: str, window: str, read: bool,
                        started: bool = False) -> Run:
    """A profile driven over MCP with ``driver``: browser_navigate to the probe (``started``: on a
    profile that profile_start opened on its blank tab before), then (``read``) the reading tools while
    the probe watches, then the probe's final report, then (``read``) a read of a page global in each
    world (after the report: main-world code is detectable by design)."""
    profile = home.create_profile(cfg.lower(), launch={"window": window})
    tools: dict[str, str] = {}
    saved = os.environ.get(ENV_DRIVER)
    os.environ[ENV_DRIVER] = driver  # the server's BrowserManager selects its driver when it starts
    try:
        async with Client(create_server(store=home)) as client:
            if started:
                await _call(client, "profile_start", {"profile": profile.name})
                tools["browser_tabs before"] = await _call(client, "browser_tabs", {"profile": profile.name})
            tools["browser_navigate"] = await _call(client, "browser_navigate",
                                                    {"profile": profile.name, "url": probe.url(cfg)})
            await probe.await_phase(cfg, "main")
            if read:
                for name, args in READING_TOOLS:
                    tools[name] = await _call(client, name, {"profile": profile.name, **args})
            else:
                await asyncio.sleep(1.5)
            probe.finish(cfg)
            await probe.await_phase(cfg, "final")
            if read:
                for world in ("isolated", "main"):
                    tools[f"global in the {world} world"] = await _call(client, "browser_evaluate", {
                        "profile": profile.name, "expression": f"window.{PAGE_GLOBAL} ?? null", "world": world})
            tools["browser_tabs"] = await _call(client, "browser_tabs", {"profile": profile.name})
            info = RuntimeManager(home).status(profile.id)
            if info is None or not info.cdp_http_url:
                raise RuntimeError(f"{cfg}: the profile is not running any more")
            version = _version(info.cdp_http_url)
    finally:
        if saved is None:
            os.environ.pop(ENV_DRIVER, None)
        else:
            os.environ[ENV_DRIVER] = saved
        await asyncio.to_thread(RuntimeManager(home).stop, profile.id)
    return _collect(probe, cfg, version, tools)


class Runs:
    """Attached runs, made once per module on first use (each takes ~15 s)."""

    def __init__(self, home: Store, probe: ProbeServer) -> None:
        self.home, self.probe = home, probe
        self._done: dict[str, Run | BaseException] = {}

    async def get(self, cfg: str, *, driver: str = DRIVER, window: str = "offscreen", read: bool = True,
                  started: bool = False) -> Run:
        """Run ``cfg`` (e.g. ``P-patchright``) with ``driver``, once per module."""
        if cfg not in self._done:
            try:
                self._done[cfg] = await _attached_run(self.home, self.probe, cfg, driver=driver, window=window,
                                                      read=read, started=started)
            except Exception as exc:  # reported by every test that needs this run, made only once
                self._done[cfg] = exc
        done = self._done[cfg]
        if isinstance(done, BaseException):
            raise done
        return done


@pytest.fixture(scope="module")
def runs(home, probe) -> Runs:
    return Runs(home, probe)


# ---------------------------------------------------------------------- tests


def test_idle_profile_is_native(idle):
    """P0 (host launch, ProfilePilot's switches, no client) equals an unattached plain launch."""
    plain, p0 = idle["B"], idle["P0"]
    for run in (plain, p0):
        ident = run.main["identity"]
        assert ident["webdriver"] is False and not ident["webdriverOwnProperty"], run.cfg
        ua = run.version["User-Agent"]
        assert ident["userAgent"] == ua == run.probe_headers["user-agent"] == run.start_headers["user-agent"], run.cfg
        assert run.main["worker"]["userAgent"] == ua, run.cfg
        version = run.version["Browser"].split("/", 1)[1]  # e.g. 154.0.8037.98
        assert version in [b["version"] for b in ident["uaData"]["high"]["fullVersionList"]], run.cfg
        assert f'v="{version}"' in run.probe_headers["sec-ch-ua-full-version-list"], run.cfg
        assert ident["globals"]["suspicious"] == [], run.cfg
        assert not any(run.cdp_flags().values()), (run.cfg, run.cdp_flags())
        assert run.main["externalCalls"]["count"] == 0 and run.final["externalCalls"]["count"] == 0, run.cfg
    assert plain.version["User-Agent"] == p0.version["User-Agent"]
    assert plain.main["identity"]["uaData"] == p0.main["identity"]["uaData"]
    assert plain.main["identity"]["globals"] == p0.main["identity"]["globals"]
    assert plain.main["identity"]["chromeKeys"] == p0.main["identity"]["chromeKeys"]
    assert plain.identity_headers("start") == p0.identity_headers("start")
    assert plain.identity_headers("probe") == p0.identity_headers("probe")
    assert p0.start_headers["sec-fetch-site"] == "none"
    # A documented, deliberate difference (F6): --disable-back-forward-cache.
    assert p0.bfcache is not None and p0.bfcache["persisted"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", drivers(xfail_playwright=F1))
async def test_attached_session_is_not_cdp_detectable(runs, idle, driver):
    run = await runs.get(f"P-{driver}", driver=driver)
    assert not any(run.cdp_flags().values()), run.cdp_flags()
    # A ratio against the idle profile on the same machine, not an absolute threshold.
    limit = TIMING_FACTOR * idle["P0"].timing_ms() + TIMING_SLACK_MS
    assert run.timing_ms() <= limit, (run.timing_ms(), idle["P0"].timing_ms())
    worker_limit = TIMING_FACTOR * idle["P0"].main["worker"]["consoleTimingMs"] + TIMING_SLACK_MS
    assert run.main["worker"]["consoleTimingMs"] <= worker_limit, (run.main["worker"], idle["P0"].main["worker"])


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", drivers(xfail_playwright=F3))
async def test_reading_tools_leave_no_main_world_traces(runs, driver):
    run = await runs.get(f"P-{driver}", driver=driver)
    assert "ProfilePilot native probe" in run.tools["browser_snapshot"]
    assert "Results are sent to the local probe server only" in run.tools["browser_read"]
    assert '"probe ready"' in run.tools["browser_evaluate"]  # the default world still reads the DOM
    assert run.main["externalCalls"]["count"] == 0  # navigating alone never touched the main world
    assert run.final["externalCalls"]["count"] == 0, run.final["externalCalls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", drivers())
async def test_evaluate_reaches_page_globals_only_in_the_main_world(runs, driver):
    """browser_evaluate(world="main") reads the page's own variables (documented as detectable); the
    default isolated world sees the DOM only. The Playwright fallback has no isolated world."""
    run = await runs.get(f"P-{driver}", driver=driver)
    assert f'"{PAGE_GLOBAL_VALUE}"' in run.tools["global in the main world"]
    isolated = run.tools["global in the isolated world"]
    if driver == "patchright":
        assert isolated.endswith("Result:\nnull"), isolated
    else:
        assert f'"{PAGE_GLOBAL_VALUE}"' in isolated


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["autostart", "running"])
@pytest.mark.parametrize("check", ["sec_fetch_site", "user_activation", "audio_context", "history_length", "has_focus"])
async def test_first_navigation_is_like_a_typed_url(runs, idle, check, path):
    """The first browser_navigate is opened by Chrome itself from its command line (FIX-PLAN step 8):
    on a stopped profile at launch (``autostart``), on a profile that profile_start left on its blank
    tab by handing the URL to the running browser (``running``: a new tab replaces the blank one). The
    first page is then P0's: no user activation (F4), the tab has the focus (F5, was the omnibox of the
    initial about:blank tab), no extra about:blank history entry, ``Sec-Fetch-Site: none``."""
    if path == "autostart":
        run = await runs.get(f"P-{DRIVER}", driver=DRIVER)
    else:
        run = await runs.get(f"PS-{DRIVER}", driver=DRIVER, read=False, started=True)
        assert "1 tab(s)" in run.tools["browser_tabs before"] and "about:blank" in run.tools["browser_tabs before"]
        assert "Opened like a link from another app" in run.tools["browser_navigate"], run.tools["browser_navigate"]
        assert "1 tab(s)" in run.tools["browser_tabs"] and "about:blank" not in run.tools["browser_tabs"]
    p0 = idle["P0"]
    if check == "sec_fetch_site":
        headers = run.start_headers
        assert (headers["sec-fetch-site"], headers["sec-fetch-mode"], headers["sec-fetch-dest"],
                headers.get("sec-fetch-user")) == ("none", "navigate", "document", "?1"), headers
        assert run.identity_headers("start") == p0.identity_headers("start")
    elif check == "user_activation":
        assert run.main["firstUse"]["hasBeenActive"] is False, run.main["firstUse"]
    elif check == "audio_context":
        assert run.main["firstUse"]["audioState"] == "suspended", run.main["firstUse"]
    elif check == "history_length":
        assert run.main["firstUse"]["historyLength"] == p0.main["firstUse"]["historyLength"], (
            run.main["firstUse"], p0.main["firstUse"])
    else:
        # The late samples, as in the audit (right at load even a plain Chrome may not have it yet).
        baseline = [s["hasFocus"] for s in p0.final["samples"]]
        if not (baseline and all(baseline)):
            pytest.skip(f"the idle profile has no focus on this desktop either ({baseline}): not measurable")
        focus = [s["hasFocus"] for s in run.final["samples"]]
        assert focus and all(focus), focus


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", drivers(xfail_playwright=F4))
async def test_reading_tools_never_activate_the_page(runs, driver):
    """No input event, no user activation: the page stays un-activated while browser_navigate and every
    reading tool (snapshot, read, screenshot, scroll, evaluate) run on it, as for a person who only looks."""
    run = await runs.get(f"P-{driver}", driver=driver)
    assert run.main["firstUse"]["hasBeenActive"] is False, run.main["firstUse"]
    active = [s["hasBeenActive"] for s in run.final["samples"]]
    assert active and not any(active), active


def _foreign_zone() -> str:
    """A time zone whose current UTC offset differs from this machine's (so alignment is visible)."""
    import datetime
    import zoneinfo

    local = datetime.datetime.now().astimezone().utcoffset()
    for name in ("Asia/Tokyo", "America/New_York", "Europe/Berlin"):
        if datetime.datetime.now(zoneinfo.ZoneInfo(name)).utcoffset() != local:
            return name
    raise AssertionError("no candidate zone differs from the local one")


@pytest.mark.asyncio
async def test_timezone_reaches_oopif_and_workers(home, probe):
    """FIX-PLAN step 6 (F7): with ``launch.timezone`` every context of an attached profile reports it - the
    page from its first script, its dedicated, shared and service workers, and a cross-site iframe
    (``localhost`` inside a ``127.0.0.1`` page: out of process) with its own worker. The out-of-process
    iframe gets its override after its commit, so only its later readings are asserted (its first
    script may still see the OS zone: the documented race). ``--time-zone-for-testing`` would have
    covered everything at launch, but branded Chrome 154 does not have it."""
    zone, cfg = _foreign_zone(), "TZ"
    profile = home.create_profile("tz", launch={"window": "offscreen", "timezone": zone})
    try:
        async with Client(create_server(store=home)) as client:
            out = await _call(client, "browser_navigate", {"profile": profile.name, "url": probe.tz_url(cfg)})
            # with a timezone the first URL is navigated in the already overridden tab (not opened at launch)
            assert "Navigated" in out and "Opened at launch" not in out, out
            ready = await probe.await_phase(cfg, "ready")
            await asyncio.sleep(3.0)  # the iframe's override follows its commit (slack for a busy machine)
            probe.finish(cfg)
            late = await probe.await_phase(cfg, "late")
            info = RuntimeManager(home).status(profile.id)
            assert info is not None and info.cdp_http_url
            with urllib.request.urlopen(f"{info.cdp_http_url}/json/list", timeout=5) as resp:
                targets = json.loads(resp.read())
    finally:
        await asyncio.to_thread(RuntimeManager(home).stop, profile.id)
    assert any(t.get("type") == "iframe" and t.get("url", "").startswith("http://localhost:") for t in targets), (
        "the cross-site iframe is not out of process", [(t.get("type"), t.get("url")) for t in targets])
    assert ready["frame"] != "timeout" and ready["frame"]["origin"].startswith("http://localhost:"), ready
    assert ready["early"]["tz"] == zone, ready  # the page's first script
    readings = {
        "main frame": late["main"],
        "dedicated worker": late["dedicated"]["now"],
        "shared worker": late["shared"]["now"],
        "service worker": late["service"]["now"] if isinstance(late["service"], dict) else late["service"],
        "cross-site iframe": late["frame"]["now"] if isinstance(late["frame"], dict) else late["frame"],
        "worker of the cross-site iframe": (late["frame"]["worker"]["now"] if isinstance(late["frame"], dict)
                                            and isinstance(late["frame"]["worker"], dict) else late["frame"]),
    }
    wrong = {where: value for where, value in readings.items() if not isinstance(value, dict) or value["tz"] != zone}
    assert not wrong, (zone, wrong, late)
