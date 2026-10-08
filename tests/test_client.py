"""ProfilePilot sync facade (client.py).

The real ``RuntimeManager`` (owned by the browser runtime) is replaced by :class:`StubRuntime`,
which reports a Chrome launched by ``tests/chrome_helper.launch_chrome`` as the profile's
running browser. That keeps these tests independent of the host process while still talking
to a real Chrome over CDP.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from typing import Any

import pytest

from profilepilot import ProfilePilot
from profilepilot.errors import (
    ConflictError,
    LaunchError,
    NotFoundError,
    ProfileNotRunningError,
    ProfilePilotError,
)
from profilepilot.models import RuntimeInfo, WindowMode

from .chrome_helper import LaunchedChrome, launch_chrome
from .fakes import OriginServer

# --------------------------------------------------------------------------- stub runtime


class StubRuntime:
    """Stand-in for ``RuntimeManager``: profiles listed in ``infos`` are "running".

    ``start()`` of a profile that is not running "launches" it by returning ``launchable[id]``
    (if registered), mirroring the idempotent start of the real manager.
    """

    def __init__(self) -> None:
        self.infos: dict[str, RuntimeInfo] = {}
        self.launchable: dict[str, RuntimeInfo] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.upstreams: dict[str, str | None] = {}
        self.relayed: set[str] = set()

    def status(self, ref: str) -> RuntimeInfo | None:
        self.calls.append(("status", ref, {}))
        return self.infos.get(ref)

    def list_running(self) -> list[RuntimeInfo]:
        return list(self.infos.values())

    def start(self, ref: str, *, timeout: float = 60.0, window: WindowMode | None = None) -> RuntimeInfo:
        self.calls.append(("start", ref, {"timeout": timeout, "window": window}))
        if ref in self.infos:
            return self.infos[ref]
        if ref not in self.launchable:
            raise LaunchError(f"stub cannot launch {ref}")
        info = self.launchable[ref]
        if window is not None:
            info = info.model_copy(update={"window": window})
        self.infos[ref] = info
        return info

    def stop(self, ref: str, *, timeout: float = 20.0) -> bool:
        self.calls.append(("stop", ref, {"timeout": timeout}))
        return self.infos.pop(ref, None) is not None

    def set_upstream(self, ref: str, proxy_id: str | None) -> None:
        from profilepilot.errors import RestartRequiredError

        if ref not in self.infos:
            raise ProfileNotRunningError("not running")
        if ref not in self.relayed:
            raise RestartRequiredError("launched without a relay")
        self.upstreams[ref] = proxy_id

    def started(self, ref: str) -> int:
        return sum(1 for name, r, _ in self.calls if name == "start" and r == ref)


def runtime_info(profile_id: str, name: str, chrome: LaunchedChrome | None = None, *,
                 relay_port: int | None = None, upstream: str | None = None) -> RuntimeInfo:
    """A RuntimeInfo describing ``chrome`` (or a fake endpoint) as the profile's browser."""
    if chrome is not None:
        version = chrome.version_info()
        port, pid, ws = chrome.port, chrome.proc.pid, version["webSocketDebuggerUrl"]
        browser_version = version.get("Browser")
    else:
        port, pid, ws, browser_version = 9, None, None, None
    return RuntimeInfo(
        profile_id=profile_id, profile_name=name, state="running", host_pid=os.getpid(), chrome_pid=pid,
        cdp_port=port, cdp_http_url=f"http://127.0.0.1:{port}", cdp_ws_url=ws, relay_port=relay_port,
        upstream=upstream, browser_version=browser_version, window="offscreen",
    )


@pytest.fixture
def stub() -> StubRuntime:
    return StubRuntime()


@pytest.fixture
def pilot(tmp_path, stub) -> ProfilePilot:
    return ProfilePilot(tmp_path / "pp-home", runtime=stub)


@pytest.fixture
def chrome(tmp_path):
    with launch_chrome(tmp_path / "chrome-udd") as launched:
        yield launched


# --------------------------------------------------------------------------- no browser needed


def test_package_exports_profilepilot_lazily():
    code = (
        "import sys, profilepilot\n"
        "assert 'profilepilot.client' not in sys.modules\n"
        "from profilepilot import ProfilePilot, __version__\n"
        "assert ProfilePilot.__module__ == 'profilepilot.client' and __version__\n"
        "import profilepilot.integrations\n"
        "heavy = [m for m in ('profilepilot.browser.runtime', 'playwright', 'scrapling', 'mcp') if m in sys.modules]\n"
        "assert not heavy, heavy\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PROFILEPILOT_SECRETS": "file"})
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_create_with_proxy_spec_and_launch_options(pilot):
    prof = pilot.create("Shop DE", proxy="socks5://alice:s3cr3t@10.0.0.1:1080", lang="de-DE",
                        window="offscreen", tags=["shop"], notes="n")
    assert prof.launch.lang == "de-DE" and prof.launch.window == "offscreen" and prof.tags == ["shop"]
    record = pilot.store.get_proxy(prof.proxy_id)
    assert (record.scheme, record.host, record.port, record.username) == ("socks5", "10.0.0.1", 1080, "alice")
    assert pilot.store.proxy_endpoint(record.id).password == "s3cr3t"
    assert "s3cr3t" not in (pilot.root / "proxies.json").read_text()
    assert "s3cr3t" not in json.dumps(prof.summary())

    # an existing proxy is referenced by name (names may contain ':'), not re-parsed
    second = pilot.create("Shop DE 2", proxy=record.name)
    assert second.proxy_id == record.id
    assert len(pilot.proxies()) == 1
    # provider-list format with an explicit default scheme
    third = pilot.create("Shop US", proxy="1.2.3.4:8000:bob:pw", proxy_scheme="socks5")
    assert pilot.store.get_proxy(third.proxy_id).scheme == "socks5"


def test_create_validation_errors(pilot):
    with pytest.raises(ProfilePilotError, match="Unknown profile option"):
        pilot.create("x", colour="red")
    with pytest.raises(NotFoundError):
        pilot.create("y", proxy="no-such-proxy")
    with pytest.raises(ProfilePilotError, match="Could not parse that proxy") as info:
        pilot.create("z", proxy="socks9://carol:topsecret@h:1")
    assert "topsecret" not in str(info.value)
    pilot.create("dup")
    with pytest.raises(ConflictError):
        pilot.create("DUP")
    assert [p.name for p in pilot.profiles()] == ["dup"]


def test_launch_dict_and_default_window_from_config(pilot):
    cfg = pilot.store.load_config()
    cfg.default_window = "offscreen"
    pilot.store.save_config(cfg)
    assert pilot.create("a").launch.window == "offscreen"
    b = pilot.create("b", launch={"timezone": "Europe/Berlin"})
    assert b.launch.timezone == "Europe/Berlin" and b.launch.window == "offscreen"
    c = pilot.create("c", launch={"window": "normal"}, lang="fr-FR")
    assert c.launch.window == "normal" and c.launch.lang == "fr-FR"
    d = pilot.create("d", window=None, lang=None)  # None = keep the default
    assert d.launch.window == "offscreen" and d.launch.lang is None
    with pytest.raises(ProfilePilotError, match="Invalid launch option.*window"):
        pilot.create("e", window="fullscreen")
    assert "e" not in {p.name for p in pilot.profiles()}


def test_runtime_delegation_and_urls(pilot, stub):
    prof = pilot.create("p1")
    assert pilot.info("p1") is None
    with pytest.raises(ProfileNotRunningError, match="not running"):
        pilot.cdp_url("p1", start=False)
    stub.launchable[prof.id] = runtime_info(prof.id, prof.name, relay_port=5555, upstream="socks5://ali***:***@h:1")

    assert pilot.cdp_url("p1", window="headless") == "http://127.0.0.1:9"
    assert stub.calls[-1] == ("start", prof.id, {"timeout": 60.0, "window": "headless"})
    assert pilot.proxy_url("p1") == "http://127.0.0.1:5555"
    assert pilot.proxy_url("p1", "socks5") == "socks5://127.0.0.1:5555"
    assert stub.started(prof.id) == 1  # already running: no second start
    with pytest.raises(ValueError):
        pilot.proxy_url("p1", "ftp")  # type: ignore[arg-type]

    assert pilot.start(prof.id[:4]).profile_id == prof.id
    assert [i.profile_id for i in pilot.running()] == [prof.id]
    assert pilot.info("P1").relay_port == 5555
    assert pilot.stop("p1") is True and pilot.stop("p1") is False
    assert pilot.info("p1") is None


def test_proxy_url_is_none_without_proxy(pilot, stub):
    prof = pilot.create("direct")
    stub.infos[prof.id] = runtime_info(prof.id, prof.name)
    assert pilot.proxy_url("direct") is None
    assert pilot.proxy_url("direct", "socks5") is None


def test_set_proxy_updates_store_and_switches_live(pilot, stub):
    prof = pilot.create("live", proxy="http://u:p@1.1.1.1:3128")
    first = prof.proxy_id
    updated = pilot.set_proxy("live", "socks5://v:q@2.2.2.2:1080")
    assert updated.proxy_id != first and pilot.profile("live").proxy_id == updated.proxy_id
    assert stub.upstreams == {}  # not running: store only

    stub.infos[prof.id] = runtime_info(prof.id, prof.name, relay_port=7000)
    stub.relayed.add(prof.id)
    pilot.set_proxy("live", first)
    assert stub.upstreams == {prof.id: first}
    pilot.set_proxy("live", None)
    assert stub.upstreams == {prof.id: None} and pilot.profile("live").proxy_id is None


def test_set_proxy_on_unrelayed_running_profile_needs_restart(pilot, stub):
    from profilepilot.errors import RestartRequiredError

    prof = pilot.create("norelay")
    stub.infos[prof.id] = runtime_info(prof.id, prof.name)
    with pytest.raises(RestartRequiredError):
        pilot.set_proxy("norelay", "http://1.1.1.1:3128")
    assert pilot.profile("norelay").proxy_id is not None  # saved; applies on next start


def test_add_proxy_and_delete(pilot):
    rec = pilot.add_proxy("user:pw@proxy.example:8080", "dc-1", tags=["dc"])
    assert rec.name == "dc-1" and rec.has_password and rec.scheme == "http"
    assert [p.name for p in pilot.proxies(tag="dc")] == ["dc-1"]
    with pytest.raises(ProfilePilotError, match="Could not parse") as info:
        pilot.add_proxy("hunter2-no-port")
    assert "hunter2" not in str(info.value)
    pilot.create("gone")
    entry = pilot.delete("gone")
    assert entry.name == "gone" and pilot.profiles() == []


def test_runtime_is_created_lazily(tmp_path, monkeypatch):
    import profilepilot.browser.runtime as runtime_mod

    created: list[Any] = []

    class FakeManager:
        def __init__(self, store):
            created.append(store)

    monkeypatch.setattr(runtime_mod, "RuntimeManager", FakeManager)
    pp = ProfilePilot(tmp_path / "home")
    assert created == []
    assert isinstance(pp.runtime, FakeManager) and pp.runtime is pp.runtime
    assert created == [pp.store]
    assert repr(pp) == f"ProfilePilot(root={str(pp.root)!r})"


def test_cookies_raise_clearly_when_cdp_is_unreachable(pilot, stub):
    prof = pilot.create("dead")
    stub.infos[prof.id] = runtime_info(prof.id, prof.name)  # port 9: nothing listens
    with pytest.raises(LaunchError, match="Could not read cookies of profile 'dead' over CDP"):
        pilot.cookies("dead", timeout=5)
    del stub.infos[prof.id]
    with pytest.raises(ProfileNotRunningError):
        pilot.cookies("dead", start=False)
    assert stub.started(prof.id) == 0


# --------------------------------------------------------------------------- real Chrome over CDP


@pytest.mark.chrome
def test_cookies_roundtrip_over_cdp(pilot, stub, chrome):
    prof = pilot.create("cookie-jar")
    stub.infos[prof.id] = runtime_info(prof.id, prof.name, chrome)
    with OriginServer() as origin:
        count = pilot.set_cookies("cookie-jar", [
            {"name": "sid", "value": "v-123", "url": origin.url},
            {"name": "other", "value": "x", "domain": "example.com", "path": "/"},
        ])
        assert count == 2
        assert pilot.set_cookies("cookie-jar", []) == 0
        mine = pilot.cookies("cookie-jar", origin.url + "/any/path")
        assert [(c["name"], c["value"], c["domain"]) for c in mine] == [("sid", "v-123", "127.0.0.1")]
        everything = {c["name"] for c in pilot.cookies("cookie-jar")}
        assert {"sid", "other"} <= everything
        both = pilot.cookies("cookie-jar", [origin.url, "https://example.com/"])
        assert {c["name"] for c in both} == {"sid", "other"}
    # the attach was only a disconnecting CDP client: Chrome and its tab are still there
    assert chrome.proc.poll() is None
    assert chrome.version_info()["Browser"]


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_cookies_from_a_thread_running_an_event_loop(pilot, stub, chrome):
    """The sync facade must work inside an asyncio loop (Playwright's sync API alone refuses)."""
    import anyio.to_thread

    prof = pilot.create("loop")
    stub.infos[prof.id] = runtime_info(prof.id, prof.name, chrome)
    pilot.set_cookies("loop", [{"name": "a", "value": "1", "url": "http://127.0.0.1/"}])  # in-loop call
    assert [c["name"] for c in pilot.cookies("loop", "http://127.0.0.1/")] == ["a"]
    from_worker = await anyio.to_thread.run_sync(lambda: pilot.cookies("loop", "http://127.0.0.1/"))
    assert [c["value"] for c in from_worker] == ["1"]

    errors: list[BaseException] = []

    def plain_thread() -> None:
        try:
            assert pilot.cookies("loop", "http://127.0.0.1/")[0]["name"] == "a"
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    t = threading.Thread(target=plain_thread)
    t.start()
    await asyncio.to_thread(t.join, 60)
    assert not errors
    assert chrome.proc.poll() is None
