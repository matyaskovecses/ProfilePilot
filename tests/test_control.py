"""Control layer (pause, help requests, activity log) and the AI-side ``profile_request_help`` tool."""

from __future__ import annotations

import functools
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver import Context

from profilepilot.control import (
    ActivityEvent,
    ActivityLog,
    ControlStore,
    PauseInfo,
    ProfilePausedError,
    control_status_lines,
    manager_info,
    refusal_message,
    scrub_text,
)
from profilepilot.errors import ConflictError, NotFoundError, ProfilePilotError
from profilepilot.store import Store

# ---------------------------------------------------------------------- pause / resume


def test_pause_resume_round_trip(store: Store) -> None:
    profile = store.create_profile("shop-us")
    control = ControlStore(store)
    assert control.paused("shop-us") is None

    info = control.pause("shop-us", note="logging in")
    assert info.paused and info.by == "user" and info.note == "logging in"
    assert control.paused(profile.id) == info
    again = control.pause("shop-us", note="still logging in")
    assert again.since == info.since and again.note == "still logging in"  # since is kept

    with pytest.raises(ProfilePausedError) as excinfo:
        control.check_not_paused("shop-us")
    message = str(excinfo.value)
    assert message.startswith("The user has taken control of profile 'shop-us' (since ")
    assert ": 'still logging in'). Don't act on this profile now. Wait and check profile_status, or ask the user." in message
    assert isinstance(excinfo.value, ConflictError)  # tool_guard maps it like any ProfilePilotError

    control.resume("shop-us")
    assert control.paused("shop-us") is None
    control.check_not_paused("shop-us")
    data = json.loads((store.profile_dir(profile.id) / "control.json").read_text("utf-8"))
    assert data["pause"] is None


def test_refusal_message_matches_the_spec_example() -> None:
    since = datetime.now().astimezone().replace(hour=14, minute=2, second=0, microsecond=0)
    text = refusal_message("shop-us", PauseInfo(paused=True, by="user", since=since, note="logging in"))
    assert text == ("The user has taken control of profile 'shop-us' (since 14:02: 'logging in'). Don't act on this "
                    "profile now. Wait and check profile_status, or ask the user.")
    help_text = refusal_message("shop-us", PauseInfo(paused=True, by="help", since=since, note="Solve the CAPTCHA"))
    assert "waiting for the user" in help_text and "'Solve the CAPTCHA'" in help_text and "profile_status" in help_text


def test_help_request_pauses_until_resolved(store: Store) -> None:
    store.create_profile("mail-de")
    control = ControlStore(store)
    req = control.request_help("mail-de", "Solve the CAPTCHA on the sign-in page.", "captcha", requested_by="claude-ai")
    assert req.status == "open" and req.kind == "captcha" and req.requested_by == "claude-ai"
    pause = control.paused("mail-de")
    assert pause is not None and pause.by == "help" and pause.request_id == req.id

    # Asking the same question again returns the open request instead of a duplicate.
    assert control.request_help("mail-de", "solve the captcha on the sign-in page.", "captcha").id == req.id
    second = control.request_help("mail-de", "Type the 6-digit code from the SMS.", "verification")
    assert [r.id for r in control.help_requests()] == [req.id, second.id]

    done = control.resolve_help("mail-de", req.id, status="done", note="solved")
    assert done.status == "done" and done.note == "solved" and done.resolved_at is not None
    assert control.paused("mail-de") is not None  # the second request is still open
    control.resolve_help("mail-de", second.id, status="dismissed")
    assert control.paused("mail-de") is None  # handed back: no open request remains
    assert control.resolve_help("mail-de", req.id).status == "done"  # idempotent
    assert control.help_requests() == []
    assert {r.id for r in control.help_requests(open_only=False)} == {req.id, second.id}
    with pytest.raises(NotFoundError):
        control.resolve_help("mail-de", "nope")
    with pytest.raises(ProfilePilotError):
        control.resolve_help("mail-de", req.id, status="open")  # type: ignore[arg-type]


def test_user_pause_survives_resolving_help(store: Store) -> None:
    store.create_profile("p1")
    control = ControlStore(store)
    control.pause("p1", note="checking the cart myself")
    req = control.request_help("p1", "Confirm the 3-D Secure prompt.", "payment")
    control.resolve_help("p1", req.id)
    pause = control.paused("p1")
    assert pause is not None and pause.by == "user"  # the user paused separately: still theirs


def test_resume_hands_back_and_closes_open_requests(store: Store) -> None:
    store.create_profile("p2")
    control = ControlStore(store)
    control.request_help("p2", "Log in with your account.", "login")
    closed = control.resume("p2")
    assert len(closed) == 1 and closed[0].status == "done" and "handed back" in closed[0].note
    assert control.paused("p2") is None and control.help_requests() == []


def test_request_limits_and_validation(store: Store) -> None:
    store.create_profile("p3")
    control = ControlStore(store)
    for i in range(5):
        control.request_help("p3", f"Step {i}: do the thing", "other", pause=False)
    with pytest.raises(ConflictError, match="already has 5 open help requests"):
        control.request_help("p3", "one more", "other")
    assert control.request_help("p3", "Step 2: do the thing", "other").message == "Step 2: do the thing"  # repeat is fine
    with pytest.raises(ProfilePilotError, match="empty"):
        control.request_help("p3", "   ", "other")
    with pytest.raises(ProfilePilotError, match="Unknown help kind"):
        control.request_help("p3", "x y z", "bogus")  # type: ignore[arg-type]
    assert control.paused("p3") is None  # pause=False requests do not pause
    long = control.state("p3").open[0]
    assert len(long.message) <= 500


def test_message_is_cleaned(store: Store) -> None:
    store.create_profile("p4")
    req = ControlStore(store).request_help("p4", "Solve\x00 the\n\n CAPTCHA " + "x" * 600, "captcha")
    assert "\x00" not in req.message and "\n" not in req.message
    assert req.message.startswith("Solve the CAPTCHA") and len(req.message) == 500


def test_deleted_profile_is_not_resurrected(store: Store) -> None:
    profile = store.create_profile("gone")
    control = ControlStore(store)
    control.pause("gone")
    store.delete_profile("gone")
    assert not store.profile_dir(profile.id).exists()
    with pytest.raises(NotFoundError):
        control.pause("gone")
    assert not store.profile_dir(profile.id).exists()
    assert control.help_requests() == []


def test_control_status_lines(store: Store) -> None:
    profile = store.create_profile("shop")
    control = ControlStore(store)
    assert control_status_lines(control, profile.id) == []
    control.pause("shop", note="paying")
    req = control.request_help("shop", "Enter the SMS code.", "verification")
    lines = control_status_lines(control, profile.id)
    assert lines[0].startswith("Paused: the user has taken control since") and "('paying')" in lines[0]
    assert f"Open help request {req.id} (verification code" in lines[1] and "'Enter the SMS code.'" in lines[1]
    control.resume("shop", note="code entered")
    lines = control_status_lines(control, profile.id)
    assert any(ln.startswith("The user handled your help request 'Enter the SMS code.' at ") and ln.endswith("('code entered').")
               for ln in lines)
    assert lines[-1] == "The profile is not paused: you may act on it."
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert control_status_lines(control, profile.id, now=later) == []


def test_manager_info(tmp_path: Path) -> None:
    assert manager_info(tmp_path) is None
    (tmp_path / "ui.json").write_text(json.dumps({"pid": 999_999_9, "port": 1, "started_at": time.time()}))
    assert manager_info(tmp_path) is None
    (tmp_path / "ui.json").write_text(json.dumps({"pid": __import__("os").getpid(), "port": 1,
                                                  "started_at": time.time()}))
    assert manager_info(tmp_path)["port"] == 1


# ---------------------------------------------------------------------- activity log


def test_scrub_text_masks_secrets() -> None:
    assert scrub_text("Navigated to https://user:hunter2@example.com/x") == "Navigated to https://***:***@example.com/x"
    assert "hunter2" not in scrub_text("proxy 1.2.3.4:8080:alice:hunter2 failed")
    assert scrub_text("Authorization: Bearer abc.def.ghi") == "Authorization: Bearer ***"
    assert scrub_text("filled 4242 4242 4242 4242 into #card") == "filled [card] into #card"
    assert scrub_text("order 1234567890123") == "order 1234567890123"  # not Luhn-valid: kept
    assert scrub_text("ssn 123-45-6789 typed") == "ssn [redacted] typed"
    assert scrub_text("GET https://x.test/cb?code=SECRET&state=1") == "GET https://x.test/cb?code=***&state=1"
    fake_key = "sk_" + "live_" + "51HxQ2a8b9c0d1e2f3g4h5i6j7k8l9m0n"  # built at runtime: not a real key
    assert scrub_text(f"key {fake_key}") == "key ***"
    assert scrub_text("https://blog.test/how-to-bake-the-best-chocolate-cake-ever") .endswith("cake-ever")
    assert scrub_text("first line\nsecond line") == "first line"
    assert scrub_text("typed s3cr3t-value", extra=["s3cr3t-value"]) == "typed [redacted]"
    assert len(scrub_text("x" * 1000)) == 200
    assert scrub_text(None) == "" and scrub_text("\n\n") == ""


def test_scrub_text_masks_secrets_in_urls_json_and_key_shapes() -> None:
    # Secret-looking URL parameters: any name that contains token, secret, code, key, auth, session ...
    assert scrub_text("Opened https://x.test/reset?reset_token=abc123&lang=en") == \
        "Opened https://x.test/reset?reset_token=***&lang=en"
    assert scrub_text("https://x.test/__/auth/action?mode=verifyEmail&oobCode=Zq9-k&apiKey=AIzaXYZ") == \
        "https://x.test/__/auth/action?mode=verifyEmail&oobCode=***&apiKey=***"
    assert "s3cr3tval" not in scrub_text("POST https://x.test/token?client_id=app&client_secret=s3cr3tval")
    assert "pw1" not in scrub_text("https://x.test/login?password=pw1#x") and "x.test/login" in scrub_text(
        "https://x.test/login?password=pw1#x")
    # JSON fields with secret names, including access_token / client_secret.
    text = scrub_text('Response {"access_token": "at-1234", "expires_in": 3600, "client_secret":"cs-99"}')
    assert "at-1234" not in text and "cs-99" not in text and '"expires_in": 3600' in text
    # Magic-link style path tokens shorter than 32 characters; slugs and dates stay readable.
    assert scrub_text("Opened https://x.test/magic/Xk9fQ2pLm8RtZ1vB/confirm") == "Opened https://x.test/magic/***/confirm"
    assert scrub_text("https://blog.test/2024-10-09-why-profiles-matter").endswith("2024-10-09-why-profiles-matter")
    assert scrub_text("https://shop.test/products/trail-shoes-size-42").endswith("trail-shoes-size-42")
    # Well-known key shapes, built at runtime so no real-looking key sits in the source.
    aws = "AKIA" + "Q3EXAMPLE7KEY4AB"
    github = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
    assert scrub_text(f"env AWS_ACCESS_KEY_ID {aws} set") == "env AWS_ACCESS_KEY_ID *** set"
    assert scrub_text(f"token {github}") == "token ***"


def test_activity_event_blocked_flag(tmp_path: Path) -> None:
    log = ActivityLog(tmp_path)
    log.append(ActivityEvent(tool="browser_click", ok=False, blocked=True, summary="The user has taken control"))
    log.append(ActivityEvent(tool="browser_click"))
    first, second = log.tail()
    assert first.blocked and not first.ok and not second.blocked
    # Older log lines (written before the field existed) still parse.
    assert ActivityEvent.model_validate_json('{"tool": "x", "ts": "2026-01-01T00:00:00Z"}').blocked is False


def test_corrupt_control_file_fails_closed(store: Store) -> None:
    from profilepilot.control import UNREADABLE_NOTE

    profile = store.create_profile("shop-us")
    control = ControlStore(store)
    path = store.profile_dir(profile.id) / "control.json"
    path.write_text('{"pause": {"paused": true, "by": "user"', encoding="utf-8")  # truncated: corrupt
    pause = control.paused("shop-us")
    assert pause is not None and pause.by == "user" and pause.note == UNREADABLE_NOTE
    with pytest.raises(ProfilePausedError, match="could not read this profile's control state"):
        control.check_not_paused("shop-us")
    assert path.exists() and path.with_suffix(".json.bad").exists()  # kept (never silently "not paused")
    # The Manager lists it as paused by the user; handing it back writes a clean file.
    control.resume("shop-us")
    assert control.paused("shop-us") is None
    assert json.loads(path.read_text("utf-8"))["pause"] is None


def test_locked_control_file_raises_instead_of_reporting_not_paused(store: Store, monkeypatch) -> None:
    from profilepilot import control as control_mod
    from profilepilot.control import ControlStateError

    profile = store.create_profile("shop-us")
    control = ControlStore(store)
    control.pause("shop-us")
    real = Path.read_bytes

    def locked(self: Path) -> bytes:
        if self.name == "control.json":
            raise PermissionError(13, "The process cannot access the file")
        return real(self)

    monkeypatch.setattr(control_mod, "READ_ATTEMPTS", 2)
    monkeypatch.setattr(Path, "read_bytes", locked)
    with pytest.raises(ControlStateError, match="cannot be read right now"):
        control.check_not_paused("shop-us")
    # Display paths do not crash the Manager.
    assert control.state_by_id(profile.id, strict=False).effective is None
    assert control.help_requests() == []


def test_activity_append_tail_and_filters(tmp_path: Path) -> None:
    log = ActivityLog(tmp_path)
    assert log.tail() == []
    for i in range(30):
        log.append(ActivityEvent(profile_id="aaaa1111" if i % 2 else "bbbb2222", profile_name="p", tool=f"browser_{i}",
                                 summary=f"step {i}", ok=i % 5 != 0, ms=i))
    events = log.tail(10)
    assert [e.tool for e in events] == [f"browser_{i}" for i in range(20, 30)]  # newest 10, oldest first
    assert all(e.profile_id == "aaaa1111" for e in log.tail(100, profile_id="aaaa1111"))
    assert len(log.tail(100, profile_id="aaaa1111")) == 15
    assert [e.tool for e in log.tail(100, ok=False)] == [f"browser_{i}" for i in range(0, 30, 5)]
    assert [e.tool for e in log.tail(100, tool="browser_2")] == ["browser_2"] + [f"browser_{i}" for i in range(20, 30)]
    since = events[4].ts
    assert all(e.ts > since for e in log.tail(100, since=since))


def test_activity_summary_is_scrubbed_on_write(tmp_path: Path) -> None:
    log = ActivityLog(tmp_path)
    log.append(ActivityEvent(tool="proxy_add", summary="Saved socks5://bob:pa55word@proxy.example.net:1080\nmore"))
    raw = (tmp_path / "activity.jsonl").read_text("utf-8")
    assert "pa55word" not in raw and "more" not in raw
    assert json.loads(raw)["summary"] == "Saved socks5://***:***@proxy.example.net:1080"


def test_activity_rotation_and_cursor(tmp_path: Path) -> None:
    log = ActivityLog(tmp_path, max_bytes=2_000, keep=2)
    cursor = log.cursor()
    for i in range(60):
        log.append(ActivityEvent(tool="t", summary=f"event number {i:03d} " + "y" * 40))
    files = log.files()
    assert files[0].name == "activity.jsonl" and {f.name for f in files} <= {"activity.jsonl", "activity.jsonl.1",
                                                                         "activity.jsonl.2"}
    assert len(files) == 3
    assert not (tmp_path / "activity.jsonl.3").exists()
    tail = log.tail(1000)
    assert tail[-1].summary.startswith("event number 059")
    assert [e.summary[:16] for e in tail] == sorted(e.summary[:16] for e in tail)  # chronological across files

    cursor = log.cursor()
    assert log.read_new(cursor)[0] == []
    log.append(ActivityEvent(tool="after", summary="new one"))
    new, cursor = log.read_new(cursor)
    assert [e.tool for e in new] == ["after"]
    assert log.read_new(cursor)[0] == []


def test_activity_partial_line_is_not_read(tmp_path: Path) -> None:
    log = ActivityLog(tmp_path)
    log.append(ActivityEvent(tool="a"))
    cursor = (log.cursor()[0], 0)
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write('{"tool": "half')
    events, cursor = log.read_new(cursor)
    assert [e.tool for e in events] == ["a"]
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write('-written"}\n')
    events, _ = log.read_new(cursor)
    assert [e.tool for e in events] == ["half-written"]


_APPENDER = """
import sys
from profilepilot.control import ActivityEvent, ActivityLog
log = ActivityLog(sys.argv[1], max_bytes=20_000)
for i in range(int(sys.argv[2])):
    log.append(ActivityEvent(tool="worker", summary=f"{sys.argv[3]}-{i}"))
"""


def test_activity_is_multi_process_safe(tmp_path: Path) -> None:
    procs = [subprocess.Popen([sys.executable, "-c", _APPENDER, str(tmp_path), "60", f"w{n}"]) for n in range(4)]
    for proc in procs:
        assert proc.wait(60) == 0
    events = ActivityLog(tmp_path, max_bytes=20_000).tail(5000)
    lines = sum(len([ln for ln in p.read_text("utf-8").splitlines() if ln.strip()])
                for p in ActivityLog(tmp_path).files())
    assert lines == len(events)  # every line parses: no interleaved writes
    assert len({e.summary for e in events}) == len(events)


# ---------------------------------------------------------------------- the MCP tool and the hooks


async def browser_navigate(ctx: Context, profile: str, url: str) -> str:
    """A stand-in browser tool for the tool_guard sketch."""
    return f"Navigated {profile} to {url}"


def _fake_ctx(store: Store, *, remote: bool = False, client: str = "claude-ai") -> Any:
    from profilepilot.safety import UrlPolicy
    from profilepilot.server.app import AppState

    state = AppState(store=store, runtime=None, browsers=None, policy=UrlPolicy(remote=remote, allow_private=False),
                     remote=remote)
    session = SimpleNamespace(client_params=SimpleNamespace(client_info=SimpleNamespace(name=client)))
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state, session=session), session=session)


@pytest.mark.asyncio
async def test_profile_request_help_tool(store: Store) -> None:
    from mcp import Client
    from mcp.types import TextContent

    from profilepilot.server import tools_control
    from profilepilot.server.app import create_server

    store.create_profile("shop-us")
    server = create_server(store=store)
    tools_control.register(server)
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        tool = tools["profile_request_help"]
        assert tool.annotations.read_only_hint is False and tool.annotations.destructive_hint is False
        assert set(tool.input_schema["properties"]) == {"profile", "message", "kind"}
        result = await client.call_tool("profile_request_help", {"profile": "shop-us", "message": "Solve the CAPTCHA",
                                                                 "kind": "captcha"})
        text = "\n".join(c.text for c in result.content if isinstance(c, TextContent))
        assert not result.is_error, text
        assert text.startswith("The user has been asked in ProfilePilot Manager (CAPTCHA): 'Solve the CAPTCHA'.")
        assert "is paused until they hand it back" in text and "Check profile_status" in text
        assert "ProfilePilot Manager is not open" in text  # no ui.json in this store

        bad = await client.call_tool("profile_request_help", {"profile": "nope", "message": "do x"})
        assert bad.is_error
    reqs = ControlStore(store).help_requests()
    assert len(reqs) == 1 and reqs[0].kind == "captcha"
    assert ControlStore(store).paused("shop-us").by == "help"


@pytest.mark.asyncio
async def test_enforce_pause_hook(store: Store) -> None:
    from profilepilot.server.tools_control import enforce_pause, is_guarded_tool

    store.create_profile("shop-us")
    ctx = _fake_ctx(store)
    assert is_guarded_tool("browser_navigate") and is_guarded_tool("form_autofill") and is_guarded_tool("cookies_get")
    assert is_guarded_tool("http_fetch") and is_guarded_tool("profile_stop") and is_guarded_tool("profile_set_proxy")
    assert not is_guarded_tool("profile_status") and not is_guarded_tool("profile_list")
    assert not is_guarded_tool("profile_request_help") and not is_guarded_tool("proxy_test")

    await enforce_pause(ctx, "browser_navigate", {"profile": "shop-us", "ctx": ctx})
    ControlStore(store).pause("shop-us", note="logging in")
    with pytest.raises(ProfilePausedError, match="The user has taken control of profile 'shop-us'"):
        await enforce_pause(ctx, "browser_navigate", {"profile": "shop-us"})
    await enforce_pause(ctx, "profile_status", {"profile": "shop-us"})  # allowed
    await enforce_pause(ctx, "browser_navigate", {"profile": "unknown"})  # the tool reports it
    await enforce_pause(ctx, "browser_navigate", {"profile": "shardx:abc"})  # not ours
    await enforce_pause(None, "browser_navigate", {"profile": "shop-us"})  # no context: no check


@pytest.mark.asyncio
async def test_enforce_pause_guards_profile_management_and_forced_proxy_removal(store: Store) -> None:
    from profilepilot.server.tools_control import enforce_pause, is_guarded_tool

    proxy = store.add_proxy("socks5://proxy.example.net:1080", "de-1")
    store.create_profile("bank", proxy_id=proxy.id)
    store.create_profile("other")
    ctx = _fake_ctx(store)
    for tool in ("profile_clone", "profile_start", "profile_update", "profile_delete", "profile_stop",
                 "profile_set_proxy"):
        assert is_guarded_tool(tool)
    ControlStore(store).pause("bank")
    for tool in ("profile_clone", "profile_start", "profile_update", "profile_delete"):
        with pytest.raises(ProfilePausedError):
            await enforce_pause(ctx, tool, {"profile": "bank"})
        await enforce_pause(ctx, tool, {"profile": "other"})  # other profiles are not affected
    # Removing the paused profile's proxy would switch it to a direct connection under the user.
    with pytest.raises(ProfilePausedError, match="Proxy 'de-1' is used by profile 'bank'"):
        await enforce_pause(ctx, "proxy_remove", {"proxy": "de-1", "force": True})
    await enforce_pause(ctx, "proxy_remove", {"proxy": "de-1", "force": False})  # refused by the tool itself
    await enforce_pause(ctx, "proxy_remove", {"proxy": "nope", "force": True})  # the tool reports it
    ControlStore(store).resume("bank")
    await enforce_pause(ctx, "proxy_remove", {"proxy": "de-1", "force": True})
    # A help request pauses the same way.
    ControlStore(store).request_help("bank", "Solve the CAPTCHA", "captcha")
    with pytest.raises(ProfilePausedError, match="waits for the user's help"):
        await enforce_pause(ctx, "proxy_remove", {"proxy": "de-1", "force": True})


@pytest.mark.asyncio
async def test_enforce_pause_fails_closed(store: Store, monkeypatch) -> None:
    from profilepilot import control as control_mod
    from profilepilot.control import ControlStateError
    from profilepilot.server.tools_control import enforce_pause

    store.create_profile("shop-us")
    ControlStore(store).pause("shop-us")
    ctx = _fake_ctx(store)
    real = Path.read_bytes

    def locked(self: Path) -> bytes:
        if self.name == "control.json":
            raise PermissionError(13, "locked")
        return real(self)

    monkeypatch.setattr(control_mod, "READ_ATTEMPTS", 2)
    monkeypatch.setattr(Path, "read_bytes", locked)
    with pytest.raises(ControlStateError):
        await enforce_pause(ctx, "profile_stop", {"profile": "shop-us"})
    with pytest.raises(ControlStateError):
        await enforce_pause(ctx, "browser_click", {"profile": "shop-us"})
    await enforce_pause(ctx, "profile_status", {"profile": "shop-us"})  # not guarded: allowed

    def broken(self: ControlStore, ref: str) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(Path, "read_bytes", real)
    monkeypatch.setattr(ControlStore, "check_not_paused", broken)
    with pytest.raises(ControlStateError, match="Could not check whether the user controls profile 'shop-us'"):
        await enforce_pause(ctx, "browser_click", {"profile": "shop-us"})


@pytest.mark.asyncio
async def test_real_tool_guard_logs_pause_refusals_as_blocked(store: Store) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    from profilepilot.server.app import tool_guard

    store.create_profile("shop-us")
    ctx = _fake_ctx(store)
    guarded = tool_guard(browser_navigate)
    assert await guarded(ctx=ctx, profile="shop-us", url="https://example.com") == "Navigated shop-us to https://example.com"
    ControlStore(store).pause("shop-us")
    with pytest.raises(ToolError, match="The user has taken control of profile 'shop-us'"):
        await guarded(ctx=ctx, profile="shop-us", url="https://example.com")
    first, second = ActivityLog(store.root).tail()
    assert first.ok and not first.blocked
    assert not second.ok and second.blocked and second.tool == "browser_navigate"


@pytest.mark.asyncio
async def test_tool_guard_stops_a_page_tool_when_the_user_takes_control_mid_call(store: Store, monkeypatch) -> None:
    import asyncio

    from mcp.server.mcpserver.exceptions import ToolError

    from profilepilot.server import tools_control
    from profilepilot.server.app import tool_guard

    monkeypatch.setattr(tools_control, "PAUSE_POLL_S", 0.05)
    store.create_profile("shop-us")
    store.create_profile("other")
    ctx = _fake_ctx(store)
    typed: list[str] = []
    cancelled = asyncio.Event()

    async def browser_type(ctx: Context, profile: str, text: str) -> str:
        """A stand-in for humanized typing: one character every 30 ms."""
        try:
            for ch in text:
                typed.append(ch)
                await asyncio.sleep(0.03)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return f"Typed {len(text)} characters"

    guarded = tool_guard(browser_type)

    async def later(seconds: float, fn: Any, *args: Any) -> None:
        await asyncio.sleep(seconds)
        await asyncio.to_thread(fn, *args)

    started = time.perf_counter()
    pauser = asyncio.create_task(later(0.2, ControlStore(store).pause, "shop-us"))
    with pytest.raises(ToolError, match="browser_type was stopped before it finished. The user has taken control "
                                       "of profile 'shop-us'"):
        await guarded(ctx=ctx, profile="shop-us", text="x" * 200)  # about 6 s if it were not stopped
    await pauser
    assert cancelled.is_set() and len(typed) < 60 and time.perf_counter() - started < 3
    count = len(typed)
    await asyncio.sleep(0.2)
    assert len(typed) == count  # nothing is typed after the stop
    last = ActivityLog(store.root).tail(1)[0]
    assert last.tool == "browser_type" and last.blocked and not last.ok

    # The AI's own help request on another profile, or a pause of another profile, lets the call finish.
    ControlStore(store).resume("shop-us")
    other = asyncio.create_task(later(0.1, ControlStore(store).request_help, "other", "Solve the CAPTCHA", "captcha"))
    assert await guarded(ctx=ctx, profile="shop-us", text="abcdefghij") == "Typed 10 characters"
    await other
    # A help request on the same profile stops it too.
    helper = asyncio.create_task(later(0.1, ControlStore(store).request_help, "shop-us", "Enter the 2FA code", "verification"))
    with pytest.raises(ToolError, match="browser_type was stopped before it finished. Profile 'shop-us' is waiting"):
        await guarded(ctx=ctx, profile="shop-us", text="y" * 200)
    await helper


def test_python_client_respects_the_pause(store: Store) -> None:
    """Scripts (and the Scrapling integration, which drives profiles through the client) cannot pull a
    profile out from under the user either; the user's own scripts can opt out explicitly."""
    from profilepilot.client import ProfilePilot

    class FakeRuntime:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def status(self, profile_id: str) -> Any:
            return SimpleNamespace(state="running", cdp_http_url="http://127.0.0.1:9", profile_name="bank",
                                   http_proxy_url=None, proxy_url=None)

        def start(self, profile_id: str, **kw: Any) -> Any:
            self.calls.append("start")
            return self.status(profile_id)

        def stop(self, profile_id: str, **kw: Any) -> bool:
            self.calls.append("stop")
            return True

        def set_upstream(self, profile_id: str, proxy_id: Any) -> None:
            self.calls.append("set_upstream")

    store.create_profile("bank")
    runtime = FakeRuntime()
    pp = ProfilePilot(store=store, runtime=runtime)
    assert pp.cdp_url("bank", start=False) == "http://127.0.0.1:9"
    ControlStore(store).pause("bank", note="paying a bill")
    calls = {
        "cdp_url": lambda: pp.cdp_url("bank", start=False), "proxy_url": lambda: pp.proxy_url("bank"),
        "ensure_running": lambda: pp.ensure_running("bank"), "cookies": lambda: pp.cookies("bank"),
        "set_cookies": lambda: pp.set_cookies("bank", [{"name": "a", "value": "b", "url": "https://example.com"}]),
        "http_identity": lambda: pp.http_identity("bank"), "start": lambda: pp.start("bank"),
        "stop": lambda: pp.stop("bank"), "set_proxy": lambda: pp.set_proxy("bank", None), "delete": lambda: pp.delete("bank"),
    }
    for name, call in calls.items():
        with pytest.raises(ProfilePausedError, match="The user has taken control of profile 'bank'"):
            call()
    assert runtime.calls == [] and store.get_profile("bank")  # nothing was touched
    assert pp.info("bank") is not None and [p.name for p in pp.profiles()] == ["bank"]  # reading is fine
    # A help request pauses it the same way.
    ControlStore(store).resume("bank")
    ControlStore(store).request_help("bank", "Approve the payment", "payment")
    with pytest.raises(ProfilePausedError, match="waiting for the user"):
        pp.stop("bank")
    # The user's own script may opt out.
    assert ProfilePilot(store=store, runtime=runtime, ignore_pause=True).stop("bank") is True
    assert runtime.calls == ["stop"]


@pytest.mark.asyncio
async def test_log_activity_hook(store: Store) -> None:
    from mcp.types import TextContent

    from profilepilot.server.tools_control import control_lines, log_activity

    profile = store.create_profile("shop-us")
    ctx = _fake_ctx(store, remote=True, client="openai-mcp")
    ctx.request_context.lifespan_context.remember_secrets(profile.id, ["4000056655665556"])
    await log_activity(ctx, "browser_snapshot", {"profile": "shop-us"}, ok=True,
                       result=[TextContent(type="text", text="Page: card 4000056655665556 entered\n- more")],
                       started=time.perf_counter() - 0.25)
    await log_activity(ctx, "browser_click", {"profile": "shardx:remote"}, ok=False,
                       result=ProfilePilotError("Timed out: socks5://u:p4ss@h.test:1 refused"))
    await log_activity(ctx, "profile_create", {"name": "new-one"}, ok=True, result="Created profile 'new-one'.")
    await log_activity(None, "browser_click", {}, ok=True, result="x")  # no state: silently ignored
    events = ActivityLog(store.root).tail()
    assert len(events) == 3
    first, second, third = events
    assert first.profile_id == profile.id and first.profile_name == "shop-us" and first.source == "mcp-http"
    assert first.client == "openai-mcp" and first.ms >= 200 and first.ok
    assert first.summary == "Page: card [redacted] entered"
    assert second.profile_id is None and second.profile_name == "shardx:remote" and not second.ok
    assert "p4ss" not in second.summary
    assert third.profile_name == "new-one" and third.profile_id is None  # profile not created in this test
    assert control_lines(store, profile.id) == []


@pytest.mark.asyncio
async def test_wire_in_tool_guard_sketch(store: Store) -> None:
    """The tool_guard patch from docs/design/WIRE-IN.md: a paused profile's browser tool fails with the
    refusal as the model-facing error, and both calls are logged."""
    from mcp import Client
    from mcp.server import MCPServer
    from mcp.types import TextContent

    from profilepilot.safety import UrlPolicy
    from profilepilot.server.app import AppState, to_tool_error
    from profilepilot.server.tools_control import enforce_pause, log_activity

    store.create_profile("shop-us")

    def tool_guard(fn):  # mirrors the WIRE-IN.md snippet
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            ctx = kwargs.get("ctx")
            try:
                await enforce_pause(ctx, fn.__name__, kwargs)
                result = await fn(*args, **kwargs)
            except Exception as exc:
                error = to_tool_error(exc, fn.__name__)
                await log_activity(ctx, fn.__name__, kwargs, ok=False, result=error, started=started)
                raise error from None
            await log_activity(ctx, fn.__name__, kwargs, ok=True, result=result, started=started)
            return result

        return wrapper

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        yield AppState(store=store, runtime=None, browsers=None, policy=UrlPolicy(remote=False, allow_private=False))

    server = MCPServer("t", lifespan=lifespan)
    server.add_tool(tool_guard(browser_navigate), name="browser_navigate", structured_output=False)
    async with Client(server) as client:
        ok = await client.call_tool("browser_navigate", {"profile": "shop-us", "url": "https://example.com"})
        assert not ok.is_error
        ControlStore(store).pause("shop-us", note="logging in")
        refused = await client.call_tool("browser_navigate", {"profile": "shop-us", "url": "https://example.com"})
        text = "\n".join(c.text for c in refused.content if isinstance(c, TextContent))
        assert refused.is_error and "The user has taken control of profile 'shop-us'" in text
    events = ActivityLog(store.root).tail()
    assert [(e.tool, e.ok) for e in events] == [("browser_navigate", True), ("browser_navigate", False)]
    assert events[1].summary.startswith("The user has taken control")
