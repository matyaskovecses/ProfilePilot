"""``profilepilot cookies ...``: in-process ``cli.main`` against a fake runtime and the small fake of
Chrome's cookie store from ``test_cookiejar`` (the real cookiejar logic runs on top of it), then one
round trip against a real throwaway Chrome (a temporary Store profile, started off-screen, nothing
left running).

Cookie values are secrets: no output may contain one unless it was asked for (``--values``,
``export -``).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from profilepilot import cli
from profilepilot.browser import cookiejar
from profilepilot.browser.devtools import CdpError
from profilepilot.control import ActivityLog, ControlStore
from profilepilot.models import RuntimeInfo
from profilepilot.store import Store

from .test_cookiejar import FakeCdp

SECRET = "s3cr3t-cookie-value"
OTHER_SECRET = "an0ther-s3cret"
FUTURE = int(time.time()) + 86400 * 30
TOP = {"topLevelSite": "https://top.test", "hasCrossSiteAncestor": False}


def cookie(name: str, domain: str = "example.com", path: str = "/", **kw: Any) -> dict[str, Any]:
    return {"name": name, "value": kw.pop("value", f"{name}-value"), "domain": domain, "path": path,
            "expires": kw.pop("expires", FUTURE), "secure": kw.pop("secure", True), "httpOnly": kw.pop("httpOnly", False),
            **kw}


class FakeRuntime:
    """Stand-in for ``RuntimeManager``: ``running`` maps profile ids to their window mode."""

    def __init__(self) -> None:
        self.running: dict[str, str] = {}
        self.calls: list[tuple] = []

    def _info(self, pid: str) -> RuntimeInfo:
        return RuntimeInfo(profile_id=pid, profile_name="x", state="running", host_pid=1, cdp_port=9,
                           cdp_ws_url="ws://127.0.0.1:9/devtools/browser/fake", window=self.running[pid])

    def status(self, pid: str) -> RuntimeInfo | None:
        return self._info(pid) if pid in self.running else None

    def start(self, pid: str, *, timeout: float = 60.0, window: str | None = None) -> RuntimeInfo:
        self.calls.append(("start", window))
        self.running[pid] = window or "normal"
        return self._info(pid)

    def stop(self, pid: str, *, timeout: float = 20.0) -> bool:
        self.calls.append(("stop",))
        return self.running.pop(pid, None) is not None


class Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.store = Store(tmp_path / "home")
        self.profile = self.store.create_profile("shop")
        self.runtime = FakeRuntime()
        self.chrome = FakeCdp()

        @contextlib.asynccontextmanager
        async def connect(ws_url: str, **_kw: Any):
            yield self.chrome

        monkeypatch.setenv("PROFILEPILOT_HOME", str(self.store.root))
        monkeypatch.setattr(cli, "_runtime", lambda store: self.runtime)
        monkeypatch.setattr(cookiejar, "browser_connection", connect)

    def seed(self, *cookies: dict[str, Any]) -> None:
        for c in cookies:
            self.chrome._put(dict(c))

    def __call__(self, *argv: str, stdin: str | None = None) -> tuple[int, str, str]:
        if stdin is not None:
            self.monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8"))
        rc = cli.main(["cookies", *argv])
        out, err = self.capsys.readouterr()
        return rc, out, err

    def jar(self) -> dict[tuple, dict[str, Any]]:
        return {(c["name"], c["domain"], c["path"]): c for c in self.chrome.jar.values()}

    def activity(self) -> list[Any]:
        return ActivityLog(self.store.root).tail(50)


@pytest.fixture
def env(tmp_path, monkeypatch, capsys) -> Env:
    return Env(tmp_path, monkeypatch, capsys)


def no_secret(*texts: str) -> None:
    for text in texts:
        assert SECRET not in text and OTHER_SECRET not in text


# ---------------------------------------------------------------------- the browser for a command


def test_list_hides_values_and_runs_a_stopped_profile_off_screen_for_the_command(env):
    env.seed(cookie("sid", value=SECRET, httpOnly=True, sameSite="Lax"), cookie("pref", ".example.com"),
             cookie("chips", "widget.test", partitionKey=TOP), cookie("o", "other.test", value=OTHER_SECRET))
    rc, out, err = env("list", "shop")
    assert rc == 0 and env.runtime.calls == [("start", "offscreen"), ("stop",)] and not env.runtime.running
    assert "Starting 'shop' in the background (off-screen window) for this command" in err
    no_secret(out, err)
    assert "sid" in out and "HttpOnly Secure SameSite=Lax" in out and "Partitioned(https://top.test)" in out
    assert "4 cookies on 3 sites. Values are hidden; add --values to show them." in out

    rc, out, _ = env("list", "shop", "--values")
    assert rc == 0 and SECRET in out and OTHER_SECRET in out and "VALUE" in out

    rc, out, _ = env("list", "shop", "--domain", ".EXAMPLE.com", "--json")
    data = json.loads(out)
    assert [c["name"] for c in data["cookies"]] == ["sid", "pref"] and data["total"] == 4
    assert all("value" not in c for c in data["cookies"])
    assert data["domains"] == [{"domain": "example.com", "count": 2}, {"domain": "other.test", "count": 1},
                               {"domain": "widget.test", "count": 1}]
    sid = data["cookies"][0]
    assert (sid["host_only"], sid["http_only"], sid["same_site"], sid["key"]) == (
        True, True, "Lax", cookiejar.cookie_key(cookie("sid")))
    rc, out, _ = env("list", "shop", "--domain", "other.test", "--json", "--values")
    assert [c["value"] for c in json.loads(out)["cookies"]] == [OTHER_SECRET]
    rc, out, _ = env("list", "shop", "--domain", "nothing.test")
    assert out.strip() == "No cookies on nothing.test (4 cookies in total)."


def test_running_profiles_stay_running_headless_profiles_start_headless_and_keep_running(env):
    env.runtime.running[env.profile.id] = "normal"
    assert env("list", "shop")[0] == 0
    assert env.runtime.calls == [] and env.runtime.running == {env.profile.id: "normal"}

    env.runtime.running.clear()
    env.store.update_profile(env.profile.id, launch={"window": "headless"})
    rc, _, err = env("list", "shop", "--keep-running")
    assert rc == 0 and env.runtime.calls == [("start", "headless")]
    assert env.runtime.running == {env.profile.id: "headless"} and "(headless)" in err
    assert "'shop' keeps running in the background; stop it with: profilepilot profile stop shop" in err


def test_failures_never_leave_the_browser_behind(env, monkeypatch):
    real_call = env.chrome.call

    async def broken(method: str, *a: Any, **kw: Any) -> dict[str, Any]:
        if method == "Storage.getCookies":
            raise CdpError("could not connect to the browser (ConnectionRefusedError)")
        return await real_call(method, *a, **kw)

    monkeypatch.setattr(env.chrome, "call", broken)
    rc, _, err = env("list", "shop", "--keep-running")
    assert rc == 1 and "error: The profile's browser did not answer" in err
    assert env.runtime.calls == [("start", "offscreen"), ("stop",)] and not env.runtime.running
    monkeypatch.setattr(env.chrome, "call", real_call)

    env.runtime.calls.clear()  # Chrome stores no domain cookie for a public suffix: refused, browser stopped anyway
    rc, out, err = env("set", "shop", "p", SECRET, "--domain", "co.uk", "--keep-running")
    assert rc == 1 and "error: Chrome did not store this cookie: 'p' on co.uk" in err
    assert env.runtime.calls == [("start", "offscreen"), ("stop",)] and env.jar() == {}
    no_secret(out, err)

    env.runtime.calls.clear()  # invalid input never starts the browser
    for argv, message in [
        (("set", "shop", "a", SECRET, "--domain", "example.com", "--same-site", "none"), "SameSite=None needs Secure"),
        (("set", "shop", "a", SECRET, "--domain", "exa mple.com"), "not a valid domain"),
        (("set", "shop", "a", SECRET, "--domain", "example.com", "--expires", "next week"), "--expires takes"),
        (("set", "shop", "a", SECRET, "--domain", "example.com", "--expires", "2001-01-01"), "already expired"),
        (("list", "shop", "--domain", " "), "Give a domain"),
        (("delete", "shop"), "Say which cookies to delete"),
        (("delete", "shop", "--all", "--name", "a"), "--all deletes every cookie"),
        (("import", "shop", str(env.tmp / "missing.json")), "Cannot read"),
        (("list", "nope"), "nope"),
    ]:
        rc, out, err = env(*argv)
        assert rc == 1 and message in err, (argv, err)
        no_secret(out, err)
    assert env.runtime.calls == []


def test_every_cookie_command_respects_the_pause(env):
    ControlStore(env.store).pause(env.profile.id, note="logging in")
    target = env.tmp / "c.json"
    target.write_text(json.dumps([cookie("a")]), encoding="utf-8")
    for argv in (("list", "shop"), ("export", "shop", "-"), ("import", "shop", str(target)),
                 ("set", "shop", "a", "b", "--domain", "example.com"), ("delete", "shop", "--all")):
        rc, _, err = env(*argv)
        assert rc == 1 and "'shop' is paused for the AI" in err and "--ignore-pause" in err, argv
    assert env.runtime.calls == [] and env.jar() == {}
    assert env("set", "shop", "a", "b", "--domain", "example.com", "--ignore-pause")[0] == 0
    assert list(env.jar()) == [("a", ".example.com", "/")]


# ---------------------------------------------------------------------- set / delete


def test_set_builds_the_cookie_from_flags_and_never_prints_the_value(env):
    rc, out, err = env("set", "shop", "sid", "-", "--domain", "Example.com", "--path", "/app",
                       "--expires", "2030-01-01T00:00:00Z", "--secure", "--http-only", "--same-site", "lax",
                       stdin=SECRET + "\r\n")
    assert rc == 0, err
    no_secret(out, err)
    assert out.strip() == ("Set cookie 'sid' for example.com and its subdomains: path /app, expires "
                           "2030-01-01T00:00:00Z, HttpOnly Secure SameSite=Lax.")
    stored = env.jar()[("sid", ".example.com", "/app")]
    assert (stored["value"], stored["expires"], stored["secure"], stored["httpOnly"], stored["sameSite"]) == (
        SECRET, 1893456000, True, True, "Lax")

    rc, out, _ = env("set", "shop", "sid", OTHER_SECRET, "--domain", ".example.com", "--host-only",
                     "--expires", str(FUTURE), "--json")
    data = json.loads(out)
    assert rc == 0 and "value" not in data["cookie"] and data["cookie"]["host_only"] is True
    assert env.jar()[("sid", "example.com", "/")]["expires"] == FUTURE
    no_secret(out)

    assert env("set", "shop", "dev", "1", "--domain", "127.0.0.1")[0] == 0  # an IP address: host-only by itself
    assert env("set", "shop", "dev", "1", "--domain", "localhost", "--expires", "session")[0] == 0
    assert {("dev", "127.0.0.1", "/"), ("dev", "localhost", "/")} <= set(env.jar())
    assert env.jar()[("dev", "localhost", "/")]["session"] is True

    entries = [e for e in env.activity() if e.tool == "add cookie"]
    assert [e.summary for e in entries] == ["Set cookie 'sid' on example.com.", "Set cookie 'sid' on example.com.",
                                            "Set cookie 'dev' on 127.0.0.1.", "Set cookie 'dev' on localhost."]
    assert all(e.source == "cli" and e.profile_id == env.profile.id for e in entries)
    no_secret(*(e.model_dump_json() for e in env.activity()))


def test_a_session_cookie_in_a_browser_that_closes_now_gets_a_note(env):
    env.store.update_profile(env.profile.id, launch={"restore_session": False})
    _, out, _ = env("set", "shop", "s", "v", "--domain", "example.com")
    assert "does not restore its last session, so this session cookie ends when its browser closes" in out
    _, out, _ = env("set", "shop", "s", "v", "--domain", "example.com", "--keep-running")
    assert "does not restore" not in out


def test_delete_by_name_path_domain_or_all(env):
    env.seed(cookie("sid", value=SECRET), cookie("sid", ".example.com"), cookie("sid", path="/x"),
             cookie("sub", "a.example.com"), cookie("sid", "other.test"), cookie("keep", "other.test"))
    rc, out, _ = env("delete", "shop", "--name", "sid", "--path", "/x")
    assert rc == 0 and out.strip() == "Deleted 1 cookie on example.com."
    assert ("sid", "example.com", "/x") not in env.jar()
    rc, out, _ = env("delete", "shop", "--name", "sid", "--domain", "example.com", "--json")
    assert json.loads(out) == {"profile": "shop", "deleted": 2}
    assert set(env.jar()) == {("sub", "a.example.com", "/"), ("sid", "other.test", "/"), ("keep", "other.test", "/")}
    rc, out, _ = env("delete", "shop", "--name", "nope")
    assert rc == 0 and out.strip() == "No cookies matched; nothing was deleted."
    rc, out, _ = env("delete", "shop", "--domain", "example.com")
    assert out.strip() == "Cleared 1 cookie on example.com." and set(env.jar()) == {("sid", "other.test", "/"),
                                                                                   ("keep", "other.test", "/")}
    rc, out, _ = env("delete", "shop", "--all")
    assert rc == 0 and out.strip() == "Cleared all cookies (2)." and env.jar() == {}
    assert [e.tool for e in env.activity()] == ["delete cookies", "delete cookies", "clear cookies", "clear cookies"]
    assert [e.summary for e in env.activity()][-1] == "Cleared all cookies (2)."
    no_secret(*(e.model_dump_json() for e in env.activity()))


# ---------------------------------------------------------------------- export / import


def test_export_to_a_file_with_owner_only_permissions_and_force(env, monkeypatch):
    env.seed(cookie("sid", value=SECRET, expires=FUTURE + 0.25, httpOnly=True, sameSite="Strict", priority="High"),
             cookie("chips", "widget.test", partitionKey=TOP), cookie("o", "other.test", value=OTHER_SECRET))
    target = env.tmp / "out.json"
    rc, out, err = env("export", "shop", str(target))
    assert rc == 0, err
    no_secret(out, err)
    assert f"Exported 3 cookies on 3 sites to {target} (json). The file contains secrets: keep it private." in out
    exported = json.loads(target.read_text(encoding="utf-8"))
    assert {c["name"]: c["value"] for c in exported} == {"sid": SECRET, "chips": "chips-value", "o": OTHER_SECRET}
    assert next(c for c in exported if c["name"] == "sid")["expires"] == FUTURE + 0.25  # Chrome's exact expiry
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600

    env.runtime.calls.clear()
    rc, _, err = env("export", "shop", str(target))
    assert rc == 1 and "already exists; add --force" in err and env.runtime.calls == []  # checked before starting
    target.write_text("old", encoding="utf-8")
    rc, out, _ = env("export", "shop", str(target), "--force", "--domain", "example.com", "--json")
    assert json.loads(out) == {"profile": "shop", "file": str(target), "format": "json", "count": 1, "omitted": 0}
    assert [c["name"] for c in json.loads(target.read_text(encoding="utf-8"))] == ["sid"]

    txt = env.tmp / "cookies.txt"  # cookies.txt has no partition column: left out, and the file says so
    rc, out, _ = env("export", "shop", str(txt))
    assert rc == 0 and "Exported 2 cookies on 2 sites" in out and "Left out 1 partitioned cookie" in out
    body = txt.read_text(encoding="utf-8")
    assert body.startswith("# Netscape HTTP Cookie File") and "Left out 1 partitioned" in body and SECRET in body

    folder = env.tmp / "exports"
    folder.mkdir()
    monkeypatch.chdir(folder)
    rc, out, _ = env("export", "shop", "--format", "netscape")  # no FILE: a timestamped name here
    assert rc == 0 and [p.name.startswith("cookies-shop-") and p.suffix == ".txt" for p in folder.iterdir()] == [True]
    rc, _, _ = env("export", "shop", str(folder))
    assert rc == 0 and sorted(p.suffix for p in folder.iterdir()) == [".json", ".txt"]
    rc, _, err = env("export", "shop", str(env.tmp / "missing" / "x.json"))
    assert rc == 1 and "does not exist" in err
    assert [e.summary for e in env.activity() if e.tool == "export cookies"][:3] == [
        "Exported 3 cookies on 3 sites as JSON.", "Exported 1 cookie on example.com as JSON.",
        "Exported 2 cookies on 2 sites as cookies.txt."]
    no_secret(*(e.model_dump_json() for e in env.activity()))


def test_export_to_stdout_imports_back_exactly(env):
    env.seed(cookie("sid", value=SECRET, expires=FUTURE + 0.5, httpOnly=True, sameSite="Lax", priority="High"),
             cookie("pref", ".example.com", "/app", expires=None), cookie("chips", "widget.test", partitionKey=TOP))
    listed = sorted((cookiejar.normalize_cookie(c) for c in env.chrome.jar.values()), key=lambda c: c["name"])
    rc, out, err = env("export", "shop", "-", "--json")
    assert rc == 0 and "Exported 3 cookies on 2 sites to stdout (json)." in err and SECRET not in err
    assert SECRET in out
    env.chrome.jar.clear()
    rc, out2, err = env("import", "shop", "-", stdin=out)
    assert rc == 0 and out2.strip() == "Imported 3 cookies on 2 sites." and SECRET not in out2 + err
    again = sorted((cookiejar.normalize_cookie(c) for c in env.chrome.jar.values()), key=lambda c: c["name"])
    assert again == listed  # every attribute

    rc, out, err = env("export", "shop", "-", "--format", "netscape", "--domain", "example.com")
    assert rc == 0 and out.startswith("# Netscape HTTP Cookie File") and SECRET in out
    assert "Exported 2 cookies on example.com to stdout (netscape)." in err


def test_import_modes_problems_and_stdin(env):
    env.seed(cookie("old", value=SECRET), cookie("old", "a.example.com"), cookie("stay", "other.test"))
    source = env.tmp / "in.json"
    source.write_bytes(json.dumps({"cookies": [
        cookie("new", value=OTHER_SECRET), {"name": "x"}, cookie("gone", expires=1000),
        cookie("q", ".shop.test", httpOnly=True), cookie("p", ".co.uk", value=OTHER_SECRET),
    ]}).encode("utf-8-sig"))
    rc, out, err = env("import", "shop", str(source), "--replace")
    assert rc == 0, err
    no_secret(out, err)
    lines = out.strip().splitlines()
    assert lines[0] == "Imported 2 cookies on 2 sites, replacing 2 older cookies of those sites."
    assert lines[1] == "Skipped 3 entries:"
    assert lines[2].startswith("  Cookie #2: ") and "'gone' on example.com" in lines[3] and "expired" in lines[3]
    assert lines[4] == "  Chrome did not accept 'p' on co.uk."
    assert set(env.jar()) == {("new", "example.com", "/"), ("q", ".shop.test", "/"), ("stay", "other.test", "/")}
    entry = [e for e in env.activity() if e.tool == "import cookies"][-1]
    assert entry.summary == "Imported 2 cookies on 2 sites, replacing 2 older cookies of those sites."

    netscape = ("# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tns\t" + SECRET
                + "\nother.test\tFALSE\t/\tFALSE\t0\to\tv\n")
    rc, out, _ = env("import", "shop", "-", "--domain", "example.com", "--json", stdin=netscape)
    data = json.loads(out)
    assert rc == 0 and data == {"profile": "shop", "imported": 1, "skipped": 1, "removed": 0, "problems": [],
                                "domains": [{"domain": "example.com", "count": 1}], "not_removed": []}
    assert env.jar()[("ns", ".example.com", "/")]["value"] == SECRET

    rc, out, _ = env("import", "shop", "-", "--replace-all", stdin=json.dumps([cookie("only", "solo.test")]))
    assert rc == 0 and out.strip() == "Imported 1 cookie on solo.test, replacing all other cookies (4)."
    assert set(env.jar()) == {("only", "solo.test", "/")}

    env.runtime.calls.clear()  # nothing importable: an error before the browser is started
    rc, _, err = env("import", "shop", "-", stdin="[]")
    assert rc == 1 and "No cookies to import." in err
    rc, _, err = env("import", "shop", "-", "--domain", "nowhere.test", stdin=json.dumps([cookie("a")]))
    assert rc == 1 and "No cookies to import for nowhere.test." in err
    rc, _, err = env("import", "shop", "-", stdin="{not json")
    assert rc == 1 and "No cookies to import: Invalid JSON" in err
    rc, _, err = env("import", "shop", "-", stdin=json.dumps([cookie("x", value=SECRET, expires=1000)]))
    assert rc == 1 and "expired" in err and SECRET not in err
    assert env.runtime.calls == []

    rc, out, err = env("import", "shop", "-", stdin=json.dumps([cookie("p", ".co.uk")]))  # every cookie refused
    assert rc == 1 and "Imported 0 cookies." in out and "Chrome did not accept 'p' on co.uk." in out
    with pytest.raises(SystemExit):
        env("import", "shop", "-", "--replace", "--replace-all", stdin="[]")


# ---------------------------------------------------------------------- real Chrome


def _running(home: Path) -> list[str]:
    from profilepilot.browser.runtime import RuntimeManager

    return [i.profile_name for i in RuntimeManager(Store(home)).list_running()]


@pytest.mark.chrome
def test_chrome_round_trip_through_the_cli(tmp_path, monkeypatch, capsys):
    from tests.chrome_helper import find_test_browser

    from .test_cli import _kill_leftovers

    find_test_browser()
    store = Store(tmp_path / "home")
    store.create_profile("jar")
    monkeypatch.setenv("PROFILEPILOT_HOME", str(store.root))

    def run(*argv: str, stdin: str | None = None) -> str:
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8"))
        rc = cli.main(["cookies", *argv])
        out, err = capsys.readouterr()
        assert rc == 0, f"{argv}: {err}"
        assert SECRET not in err and (SECRET not in out or "--values" in argv)
        return out

    try:
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(FUTURE))
        run("set", "jar", "sid", "-", "--domain", "example.test", "--expires", expires, "--secure", "--http-only",
            "--same-site", "lax", stdin=SECRET)
        assert _running(store.root) == []  # started off-screen for the command, stopped again
        run("set", "jar", "pref", "v2", "--domain", "example.test", "--host-only", "--path", "/app",
            "--expires", str(FUTURE))
        run("set", "jar", "sess", "s1", "--domain", "other.test")  # a session cookie: the profile restores its session
        listed = json.loads(run("list", "jar", "--json", "--values"))
        assert _running(store.root) == []
        got = {(c["name"], c["domain"], c["path"]): c for c in listed["cookies"]}
        assert set(got) == {("sid", ".example.test", "/"), ("pref", "example.test", "/app"), ("sess", ".other.test", "/")}
        sid = got[("sid", ".example.test", "/")]
        assert (sid["value"], sid["expires"], sid["secure"], sid["http_only"], sid["same_site"]) == (
            SECRET, expires, True, True, "Lax")
        assert got[("sess", ".other.test", "/")]["session"] is True

        target = tmp_path / "jar.json"
        run("export", "jar", str(target), "--keep-running")
        assert _running(store.root) == ["jar"]
        assert "Cleared all cookies (3)." in run("delete", "jar", "--all")
        assert json.loads(run("list", "jar", "--json"))["total"] == 0
        assert "Imported 3 cookies on 2 sites." in run("import", "jar", str(target))
        assert json.loads(run("list", "jar", "--json", "--values")) == listed
        assert "Deleted 1 cookie on example.test." in run("delete", "jar", "--name", "pref")
        assert _running(store.root) == ["jar"]  # it was running already: left running
    finally:
        from profilepilot.browser.runtime import RuntimeManager

        with contextlib.suppress(Exception):
            RuntimeManager(store).stop_all(timeout=15)
        _kill_leftovers(tmp_path)
    assert _running(store.root) == []
    assert not any(SECRET in e.model_dump_json() for e in ActivityLog(store.root).tail(100))
    assert os.path.exists(target)
