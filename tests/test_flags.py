"""Unit tests for the browser runtime building blocks (no real Chrome needed):
Chrome flags, user-data-dir preparation, the host control API and host start-up helpers."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import subprocess
import sys
from pathlib import Path

import pytest

from profilepilot.browser.control import (
    TOKEN_HEADER,
    ControlCallError,
    ControlError,
    ControlServer,
    control_call,
)
from profilepilot.browser.flags import FORBIDDEN_SWITCHES, build_chrome_args, validate_extra_args
from profilepilot.browser.prefs import has_saved_session, prepare_user_data_dir, profile_in_use
from profilepilot.errors import LaunchError, ProfileNotRunningError, ProfilePilotError
from profilepilot.models import LaunchOptions, RuntimeInfo
from profilepilot.paths import BrowserInfo

CHROME = BrowserInfo("chrome", r"C:\chrome.exe", "154.0.0.0")


def args_for(tmp_path: Path, relay_port: int | None = None, start_urls=(), session_exists=False, **launch) -> list[str]:
    return build_chrome_args(
        browser=CHROME, user_data_dir=tmp_path / "udd", cdp_port=9333, launch=LaunchOptions(**launch),
        relay_port=relay_port, start_urls=list(start_urls), session_exists=session_exists,
    )


# --------------------------------------------------------------------------- flags


def test_native_base_flags_and_nothing_detectable(tmp_path):
    args = args_for(tmp_path)
    assert args[:7] == [
        f"--user-data-dir={(tmp_path / 'udd').resolve()}",
        "--profile-directory=Default",
        "--remote-debugging-port=9333",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-search-engine-choice-screen",
        "--hide-crash-restore-bubble",
    ]
    assert args[-1] == "about:blank"
    names = {a[2:].split("=")[0] for a in args if a.startswith("--")}
    assert not names & (FORBIDDEN_SWITCHES | {"enable-automation", "headless", "proxy-server"})
    assert not any("webrtc" in a or "quic" in a for a in args)  # unproxied + webrtc auto = Chrome defaults


def test_proxied_flags(tmp_path):
    args = args_for(tmp_path, relay_port=4242)
    assert "--proxy-server=socks5://127.0.0.1:4242" in args
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert "--disable-quic" in args
    assert not any(a.startswith("--force-webrtc") for a in args)  # ignored by branded Chrome

    relaxed = args_for(tmp_path, relay_port=4242, webrtc="default", disable_quic=False)
    assert not any("webrtc" in a for a in relaxed) and "--disable-quic" not in relaxed


def test_unproxied_options(tmp_path):
    args = args_for(tmp_path, webrtc="proxy_only", disable_quic=True)
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in args and "--disable-quic" in args
    assert not any(a.startswith("--proxy-server") for a in args)


def test_language(tmp_path):
    args = args_for(tmp_path, lang="de-DE")
    assert "--lang=de-DE" in args and "--accept-lang=de-DE,de" in args
    assert "--accept-lang=fr" in args_for(tmp_path, lang="fr")
    # A malformed tag never reaches the command line: LaunchOptions rejects it up front.
    with pytest.raises(ValueError):
        args_for(tmp_path, lang="de DE; --no-sandbox")


def test_window_modes(tmp_path):
    normal = args_for(tmp_path, window="normal")
    assert not any(a.startswith(("--window-position", "--headless")) for a in normal)
    # Occlusion throttling is disabled in every mode: covered windows must keep rendering.
    assert "--disable-backgrounding-occluded-windows" in normal
    off = args_for(tmp_path, window="offscreen")
    assert "--window-position=-32000,-32000" in off and "--disable-backgrounding-occluded-windows" in off
    assert "--headless=new" in args_for(tmp_path, window="headless")


def test_session_restore_and_start_urls(tmp_path):
    first = args_for(tmp_path)
    assert "--restore-last-session" in first and first[-1] == "about:blank"
    restored = args_for(tmp_path, session_exists=True)
    assert "--restore-last-session" in restored and restored[-1] == "--restore-last-session"
    with_urls = args_for(tmp_path, start_urls=["https://example.com/", "about:blank"])
    assert with_urls[-2:] == ["https://example.com/", "about:blank"]
    plain = args_for(tmp_path, restore_session=False, session_exists=True)
    assert "--restore-last-session" not in plain and plain[-1] == "about:blank"
    with pytest.raises(ProfilePilotError):
        args_for(tmp_path, start_urls=["--user-data-dir=C:/elsewhere"])


@pytest.mark.parametrize("arg", [
    "--remote-debugging-port=1", "--remote-debugging-pipe", "--user-data-dir=x", "--proxy-server=http://a:1",
    "--headless", "--enable-automation", "--remote-allow-origins=*", "--disable-blink-features=AutomationControlled",
    "--no-sandbox", "--user-agent=x", "--test-type", "--profile-directory=Other", "--PROXY-SERVER=x",
])
def test_extra_args_refuses_managed_and_detectable_switches(arg):
    with pytest.raises(ProfilePilotError):
        validate_extra_args([arg])


def test_extra_args_validation(tmp_path):
    assert validate_extra_args(["--force-dark-mode", "--window-size=1200,800"]) == ["--force-dark-mode", "--window-size=1200,800"]
    for bad in ["no-dashes", "-x", "--", "---x", "--=x", "--a\nb"]:
        with pytest.raises(ProfilePilotError):
            validate_extra_args([bad])
    with pytest.raises(ProfilePilotError) as info:
        validate_extra_args(["http://user:s3cret@host:1"])
    assert "s3cret" not in str(info.value)  # values are never echoed
    args = args_for(tmp_path, extra_args=["--force-dark-mode"], start_urls=["https://example.com/"])
    assert args[-2:] == ["--force-dark-mode", "https://example.com/"]


def test_invalid_cdp_port_and_edge(tmp_path):
    with pytest.raises(ProfilePilotError):
        build_chrome_args(browser=CHROME, user_data_dir=tmp_path, cdp_port=0, launch=LaunchOptions(),
                          relay_port=None, start_urls=[])
    edge = BrowserInfo("edge", r"C:\msedge.exe")
    args = build_chrome_args(browser=edge, user_data_dir=tmp_path, cdp_port=9222, launch=LaunchOptions(),
                             relay_port=None, start_urls=[])
    assert "--edge-skip-compat-layer-relaunch" in args


# --------------------------------------------------------------------------- prefs


def test_prepare_user_data_dir_marks_clean_exit_and_keeps_settings(tmp_path):
    udd = tmp_path / "udd"
    prepare_user_data_dir(udd, LaunchOptions())
    prefs = json.loads((udd / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["profile"] == {"exit_type": "Normal", "exited_cleanly": True}

    (udd / "DevToolsActivePort").write_text("1234\n/devtools/browser/x")
    (udd / "Default" / "Preferences").write_text(json.dumps(
        {"profile": {"exit_type": "Crashed", "exited_cleanly": False, "name": "Shop"}, "intl": {"accept_languages": "de"}}
    ), encoding="utf-8")
    prepare_user_data_dir(udd, LaunchOptions())
    prefs = json.loads((udd / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["profile"] == {"exit_type": "Normal", "exited_cleanly": True, "name": "Shop"}
    assert prefs["intl"] == {"accept_languages": "de"}
    assert not (udd / "DevToolsActivePort").exists()


# Secure DNS when proxied (FIX-PLAN step 4, docs/FINGERPRINT-AUDIT.md F11)


def _local_state(udd: Path) -> dict:
    return json.loads((udd / "Local State").read_text(encoding="utf-8"))


def test_prepare_user_data_dir_turns_secure_dns_off_when_proxied(tmp_path):
    from profilepilot.browser.prefs import DOH_OFF_MARKER

    udd = tmp_path / "udd"
    prepare_user_data_dir(udd, LaunchOptions(), proxied=True)  # a fresh profile: Local State is created
    assert _local_state(udd) == {"dns_over_https": {"mode": "off"}}
    assert json.loads((udd / DOH_OFF_MARKER).read_text(encoding="utf-8")) == {"previous": None}

    # an existing Local State: merged, every other key (and the DoH templates) kept
    udd = tmp_path / "udd2"
    udd.mkdir()
    state = {"browser": {"enabled_labs_experiments": ["a@1"]}, "profile": {"info_cache": {"Default": {"name": "x"}}},
             "dns_over_https": {"mode": "secure", "templates": "https://doh.example/dns-query"}}
    (udd / "Local State").write_text(json.dumps(state), encoding="utf-8")
    prepare_user_data_dir(udd, LaunchOptions(), proxied=True)
    merged = _local_state(udd)
    assert merged["dns_over_https"] == {"mode": "off", "templates": "https://doh.example/dns-query"}
    assert {k: v for k, v in merged.items() if k != "dns_over_https"} == {
        k: v for k, v in state.items() if k != "dns_over_https"}
    assert json.loads((udd / DOH_OFF_MARKER).read_text(encoding="utf-8")) == {"previous": "secure"}
    # the next proxied launch keeps the value recorded first (not its own "off")
    prepare_user_data_dir(udd, LaunchOptions(), proxied=True)
    assert json.loads((udd / DOH_OFF_MARKER).read_text(encoding="utf-8")) == {"previous": "secure"}
    assert _local_state(udd)["dns_over_https"]["mode"] == "off"


def test_secure_dns_is_restored_when_the_proxy_is_removed(tmp_path):
    from profilepilot.browser.prefs import DOH_OFF_MARKER

    # Chrome's default ("automatic", no key) comes back, other keys stay
    udd = tmp_path / "udd"
    udd.mkdir()
    (udd / "Local State").write_text(json.dumps({"browser": {"x": 1}}), encoding="utf-8")
    prepare_user_data_dir(udd, LaunchOptions(), proxied=True)
    prepare_user_data_dir(udd, LaunchOptions())
    assert _local_state(udd) == {"browser": {"x": 1}}
    assert not (udd / DOH_OFF_MARKER).exists()

    # the user's own mode from before the proxy comes back
    udd = tmp_path / "udd2"
    udd.mkdir()
    (udd / "Local State").write_text(json.dumps({"dns_over_https": {"mode": "secure", "templates": "t"}}),
                                     encoding="utf-8")
    prepare_user_data_dir(udd, LaunchOptions(), proxied=True)
    prepare_user_data_dir(udd, LaunchOptions(), proxied=False)
    assert _local_state(udd) == {"dns_over_https": {"mode": "secure", "templates": "t"}}
    assert not (udd / DOH_OFF_MARKER).exists()

    # a mode the user chose while proxied is kept
    udd = tmp_path / "udd3"
    prepare_user_data_dir(udd, LaunchOptions(), proxied=True)
    (udd / "Local State").write_text(json.dumps({"dns_over_https": {"mode": "secure"}}), encoding="utf-8")
    prepare_user_data_dir(udd, LaunchOptions())
    assert _local_state(udd) == {"dns_over_https": {"mode": "secure"}}
    assert not (udd / DOH_OFF_MARKER).exists()


@pytest.mark.parametrize("mode", ["secure", "off", "automatic", None])
def test_secure_dns_untouched_without_marker(tmp_path, mode):
    """A user's own Secure DNS setting survives unproxied launches (only a proxied launch's marker is undone)."""
    udd = tmp_path / "udd"
    udd.mkdir()
    state = {"browser": {"x": 1}} if mode is None else {"dns_over_https": {"mode": mode}, "browser": {"x": 1}}
    raw = json.dumps(state, separators=(",", ":"))
    (udd / "Local State").write_text(raw, encoding="utf-8")
    for _ in range(2):
        prepare_user_data_dir(udd, LaunchOptions())
        assert (udd / "Local State").read_text(encoding="utf-8") == raw  # not even rewritten
    fresh = tmp_path / "fresh"
    prepare_user_data_dir(fresh, LaunchOptions())
    assert not (fresh / "Local State").exists()  # left to Chrome


def test_has_saved_session(tmp_path):
    udd = tmp_path / "udd"
    assert not has_saved_session(udd)
    sessions = udd / "Default" / "Sessions"
    sessions.mkdir(parents=True)
    (sessions / "Session_1").write_bytes(b"")
    assert not has_saved_session(udd)  # empty files do not count
    (sessions / "Session_1").write_bytes(b"SNSS")
    assert has_saved_session(udd)


def test_profile_in_use_lockfile(tmp_path):
    udd = tmp_path / "udd"
    udd.mkdir()
    assert not profile_in_use(udd)
    (udd / "lockfile").write_bytes(b"")
    assert not profile_in_use(udd)  # left-over file that nobody holds
    if sys.platform != "win32":
        pytest.skip("Windows lockfile semantics")
    import win32con
    import win32file

    # Chrome opens the lockfile with FILE_SHARE_READ only for its whole lifetime.
    handle = win32file.CreateFile(str(udd / "lockfile"), win32con.GENERIC_WRITE, win32con.FILE_SHARE_READ, None,
                                  win32con.OPEN_EXISTING, 0, None)
    try:
        assert profile_in_use(udd)
    finally:
        handle.Close()
    assert not profile_in_use(udd)


# --------------------------------------------------------------------------- control API


def _raw(port: int, request: bytes) -> tuple[int, dict]:
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body or b"{}")


@pytest.mark.asyncio
async def test_control_server_routes_and_token():
    calls: list = []

    async def status(body):
        return {"chrome_pid": 42}

    async def upstream(body):
        calls.append(body)
        if body.get("url") == "bad":
            raise ControlError(400, "Invalid proxy URL.")
        if body.get("url") == "conflict":
            raise ControlError(409, "no relay")
        return {"upstream": None}

    server = ControlServer("tok-123", {("GET", "/status"): status, ("POST", "/upstream"): upstream})
    port = await server.start()
    info = RuntimeInfo(profile_id="abcd1234", profile_name="p", host_pid=1, control_port=port, control_token="tok-123")
    try:
        assert await asyncio.to_thread(control_call, info, "GET", "/status") == {"chrome_pid": 42, "ok": True}
        assert (await asyncio.to_thread(control_call, info, "POST", "/upstream", {"url": None}))["ok"] is True
        assert calls == [{"url": None}]

        for body, status_code in ({"url": "bad"}, 400), ({"url": "conflict"}, 409):
            with pytest.raises(ControlCallError) as err:
                await asyncio.to_thread(control_call, info, "POST", "/upstream", body)
            assert err.value.status == status_code
        wrong = info.model_copy(update={"control_token": "tok-124"})
        with pytest.raises(ControlCallError) as err:
            await asyncio.to_thread(control_call, wrong, "GET", "/status")
        assert err.value.status == 401
        with pytest.raises(ControlCallError) as err:
            await asyncio.to_thread(control_call, info, "GET", "/nope")
        assert err.value.status == 404
        with pytest.raises(ControlCallError) as err:
            await asyncio.to_thread(control_call, info, "POST", "/status")
        assert err.value.status == 405

        auth = f"{TOKEN_HEADER}: tok-123\r\n".encode()
        code, body = await asyncio.to_thread(
            _raw, port, b"POST /upstream HTTP/1.1\r\n" + auth + b"Content-Length: 5\r\n\r\n{nope")
        assert code == 400 and "JSON" in body["error"]
        code, _ = await asyncio.to_thread(
            _raw, port, b"POST /upstream HTTP/1.1\r\n" + auth + b"Content-Length: 99999999\r\n\r\n")
        assert code == 413
        code, _ = await asyncio.to_thread(_raw, port, b"GET /status HTTP/1.1\r\nHost: x\r\n\r\n")
        assert code == 401  # no token at all
    finally:
        await server.stop()

    with pytest.raises(ControlCallError) as err:  # host gone
        await asyncio.to_thread(control_call, info, "GET", "/status", None, 1.0)
    assert err.value.status is None
    with pytest.raises(ProfileNotRunningError):
        control_call(RuntimeInfo(profile_id="abcd1234", profile_name="p", host_pid=1), "GET", "/status")


# --------------------------------------------------------------------------- host helpers


def _run_async(coro):
    with concurrent.futures.ThreadPoolExecutor(1) as pool:  # independent of any loop in this thread
        return pool.submit(asyncio.run, coro).result()


def test_wait_for_devtools_reports_handoff_and_early_exit():
    from profilepilot.browser.host import wait_for_devtools

    quick = subprocess.Popen([sys.executable, "-c", "pass"])
    with pytest.raises(LaunchError, match="handed the launch over"):
        _run_async(wait_for_devtools(quick, 9, timeout=10))
    failing = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    with pytest.raises(LaunchError, match="exit code 7"):
        _run_async(wait_for_devtools(failing, 9, timeout=10))


def test_host_main_exit_codes(store):
    from filelock import FileLock

    from profilepilot.browser.host import main

    assert main(["nosuchprofile", "--root", str(store.root)]) == 2
    assert main(["p", "--window", "sideways"]) == 2
    profile = store.create_profile("locked")
    lock = FileLock(str(store.profile_dir(profile.id) / "host.lock"))
    lock.acquire()
    try:
        assert main([profile.id, "--root", str(store.root)]) == 3  # another host owns the profile
    finally:
        lock.release()
    assert not store.runtime_file(profile.id).exists()


def test_host_command_bypasses_the_venv_redirector():
    from profilepilot.browser.runtime import host_command

    argv, env = host_command("abcd1234", Path("C:/pp"), "offscreen")
    assert argv[1:] == ["-m", "profilepilot.browser.host", "abcd1234", "--root", str(Path("C:/pp")), "--window", "offscreen"]
    if sys.platform == "win32" and sys.prefix != sys.base_prefix:
        assert env is not None and env["__PYVENV_LAUNCHER__"] == sys.executable
        assert Path(argv[0]) == Path(sys._base_executable)
    out = subprocess.run([argv[0], "-c", "import profilepilot, sys; print(sys.prefix)"], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and Path(out.stdout.strip()) == Path(sys.prefix)


# --------------------------------------------------------------------------- first navigation at launch (FIX-PLAN step 8)


def test_host_start_url_argument(store, tmp_path):
    from profilepilot.browser.host import launch_start_urls, main
    from profilepilot.browser.runtime import RuntimeManager, host_command

    url = "https://example.com/--not-an-option?a=-b"
    argv, _env = host_command("abcd1234", Path("C:/pp"), "offscreen", url)
    assert argv[-3:] == ["--window", "offscreen", f"--start-url={url}"]  # one argument: never read as options
    assert host_command("abcd1234", Path("C:/pp"))[0][-2:] == ["--root", str(Path("C:/pp"))]
    assert main(["nosuchprofile", "--root", str(store.root), f"--start-url={url}"]) == 2  # parsed; unknown profile

    # --start-url (a client's first destination) is opened even next to a restored session;
    # the profile's own launch.start_url only without one
    home = LaunchOptions(start_url="https://home.example/")
    assert launch_start_urls(home, url, session_exists=True) == [url]
    assert launch_start_urls(home, url, session_exists=False) == [url]
    assert launch_start_urls(home, None, session_exists=True) == []
    assert launch_start_urls(home, None, session_exists=False) == ["https://home.example/"]
    assert launch_start_urls(LaunchOptions(), None, session_exists=False) == []

    store.create_profile("p")
    for bad in ("file:///C:/Windows/win.ini", "javascript:alert(1)", "about:blank", "https://x.example/\r\n--x"):
        with pytest.raises(ProfilePilotError, match="http:// or https://"):
            RuntimeManager(store).start("p", start_url=bad)  # refused before any host is started
    assert not store.runtime_file(store.get_profile("p").id).exists()


def test_launch_log_line_shows_only_origins():
    from profilepilot.browser.host import redact_launch_args

    args = ["--user-data-dir=C:\\pp\\udd", "--window-position=-32000,-32000", "about:blank",
            "https://user:secret@shop.example:8443/checkout?token=abc#x", "http://[::1]/", "https://example.com"]
    assert redact_launch_args(args) == ["--user-data-dir=C:\\pp\\udd", "--window-position=-32000,-32000", "about:blank",
                                        "https://shop.example:8443/…", "http://[::1]/", "https://example.com/"]


def test_long_user_data_dirs_are_flagged(tmp_path):
    from profilepilot.browser.prefs import MAX_USER_DATA_DIR_CHARS, long_path_hint, user_data_dir_too_long

    short = tmp_path / "u"
    deep = Path("C:/") / ("x" * (MAX_USER_DATA_DIR_CHARS - 2))  # 176 characters: Chrome 154 crashes on some pages
    assert len(str(deep)) == MAX_USER_DATA_DIR_CHARS + 1
    assert not user_data_dir_too_long(short) and long_path_hint(short) == ""
    assert user_data_dir_too_long(deep) is (sys.platform == "win32")
    if sys.platform == "win32":
        assert not user_data_dir_too_long(Path("C:/") / ("x" * (MAX_USER_DATA_DIR_CHARS - 3)))  # 175: fine
        assert "176 characters" in long_path_hint(deep) and "PROFILEPILOT_HOME" in long_path_hint(deep)



def test_no_session_restore_right_after_a_crash():
    """FIX-PLAN step 3: --restore-last-session would reopen the tab that crashed the browser (and crash it
    again, in a loop); after a crash the next run starts without the old tabs, like Chrome itself."""
    from profilepilot.browser.host import launch_after

    on = LaunchOptions(restore_session=True)
    crashed = {"code": 3221225477, "crashed": True, "crash": "access violation (0xC0000005)"}
    assert launch_after(on, crashed).restore_session is False
    assert on.restore_session is True  # the profile's own setting is unchanged
    for normal in (None, {"code": 0, "crashed": False}, {"code": 3221225477, "crashed": False, "requested": True}):
        assert launch_after(on, normal) is on
    assert launch_after(LaunchOptions(restore_session=False), crashed).restore_session is False
    args = build_chrome_args(browser=CHROME, user_data_dir=Path("C:/pp/udd"), cdp_port=9333,
                             launch=launch_after(on, crashed), relay_port=None, start_urls=[])
    assert "--restore-last-session" not in args and args[-1] == "about:blank"



def test_host_open_route_only_hands_http_urls_to_a_running_browser(store):
    """The host's POST /open (FIX-PLAN step 8) refuses other schemes and a browser that is not running
    (or headless) before it runs anything."""
    from profilepilot.browser.host import ProfileHost

    async def scenario():
        host = ProfileHost(store, store.create_profile("opener"))
        for bad in (None, {}, {"url": 5}, {"url": "file:///C:/x"}, {"url": "javascript:alert(1)"},
                    {"url": "https://x.example/\r\n--y"}):
            with pytest.raises(ControlError) as refused:
                await host._route_open(bad)
            assert refused.value.status == 400
        with pytest.raises(ControlError) as refused:
            await host._route_open({"url": "https://example.com/"})  # no browser started
        assert refused.value.status == 409 and "not running" in str(refused.value)

    _run_async(scenario())
