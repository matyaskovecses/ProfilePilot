"""CLI tests: ``python -m profilepilot ...`` in a subprocess with a temporary data root.

Client config locations (APPDATA, USERPROFILE, CODEX_HOME, ...) are redirected into the temp dir
and PATH holds no ``claude`` CLI, so the user's real configuration is never read or written.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from profilepilot.store import Store

SECRET = "Cl1-S3cret!pw"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzaGFyZHgtYXBpIn0.c2lnbmF0dXJl"


def make_cli(tmp_path: Path, *, isolate_clients: bool = True):
    """A runner for ``python -m profilepilot``. With ``isolate_clients`` the client config
    locations point into ``tmp_path`` (Chrome itself cannot start with a fake USERPROFILE, so the
    real-Chrome test keeps the user's profile variables and only isolates the data root)."""
    home = tmp_path / "home"
    users = tmp_path / "user"
    for sub in ("Roaming", "codex", "xdg"):
        (users / sub).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"PROFILEPILOT_HOME": str(home), "PROFILEPILOT_SECRETS": "file", "PYTHONIOENCODING": "utf-8"})
    if isolate_clients:
        env.update({
            "USERPROFILE": str(users),
            "HOME": str(users),
            "APPDATA": str(users / "Roaming"),
            "CODEX_HOME": str(users / "codex"),
            "XDG_CONFIG_HOME": str(users / "xdg"),
            "PATH": os.pathsep.join(p for p in (os.environ.get("SystemRoot", r"C:\Windows") + r"\System32",
                                                str(Path(sys.executable).parent)) if p),
        })
    env.pop("PROFILEPILOT_TOKEN", None)

    def run(*args: str, stdin: str | None = None, ok: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, "-m", "profilepilot", *args], input=stdin, capture_output=True, text=True,
            encoding="utf-8", env=env, timeout=timeout, cwd=str(tmp_path),
        )
        run.history.append(proc.stdout + proc.stderr)
        if ok:
            assert proc.returncode == 0, f"{args}: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
        return proc

    run.history = []  # type: ignore[attr-defined]
    run.home = home  # type: ignore[attr-defined]
    run.users = users  # type: ignore[attr-defined]
    run.env = env  # type: ignore[attr-defined]
    return run


@pytest.fixture
def cli(tmp_path):
    return make_cli(tmp_path)


def test_help_version_and_hidden_host(cli):
    out = cli("--help").stdout
    for command in ("serve", "profile", "proxy", "status", "install", "doctor", "shardx"):
        assert command in out
    assert "host" not in out.split("options:")[0].replace("localhost", "")
    assert "profilepilot 0.1.0" in cli("--version").stdout
    unknown = cli("host", "nope1234", "--root", str(cli.home), ok=False)
    assert unknown.returncode == 2  # usage / unknown profile (from the host entry point)


def test_proxy_and_profile_commands_never_print_secrets(cli, tmp_path):
    out = cli("proxy", "add", "--name", "de-1", "--tag", "de", stdin=f"socks5://alice:{SECRET}@10.1.2.3:1080\n").stdout
    assert "de-1" in out and "alice:***@10.1.2.3:1080" in out
    proxies = tmp_path / "proxies.txt"
    proxies.write_text(f"10.9.9.9:8000:bob:{SECRET}  # us-1\n# comment\nhttp://carol:{SECRET}@bad\n", encoding="utf-8")
    imported = cli("proxy", "import", str(proxies), "--scheme", "socks5")
    assert "Saved 1 proxy(ies)" in imported.stdout and "line 3: could not parse" in imported.stdout
    bad = cli("proxy", "add", f"socks5://x:{SECRET}@nohost", ok=False)
    assert bad.returncode == 1 and "Could not parse that proxy" in bad.stderr

    listing = json.loads(cli("proxy", "list", "--json").stdout)
    assert {p["name"] for p in listing} == {"de-1", "us-1"}
    assert all("***" in p["url"] for p in listing)

    created = cli("profile", "create", "shop", "--proxy", "-", "--tag", "shop", "--window", "offscreen",
                  "--lang", "de-DE", stdin=f"http://frank:{SECRET}@10.4.4.4:3128")
    assert "Created profile 'shop'" in created.stdout
    cli("profile", "create", "plain", "--proxy", "de-1", "--window", "offscreen")
    dup = cli("profile", "create", "plain", ok=False)
    assert dup.returncode == 1 and "already exists" in dup.stderr

    profiles = json.loads(cli("profile", "list", "--json").stdout)
    by_name = {p["name"]: p for p in profiles}
    assert by_name["shop"]["proxy"]["name"] == "shop"  # an inline proxy is saved under the profile name
    assert by_name["plain"]["proxy"]["name"] == "de-1" and not by_name["plain"]["running"]
    table = cli("profile", "list").stdout
    assert "NAME" in table and "shop" in table and "stopped" in table

    shown = cli("profile", "show", "shop").stdout
    assert "frank:***@10.4.4.4:3128" in shown and "state:     stopped" in shown
    shown_json = json.loads(cli("profile", "show", "shop", "--json").stdout)
    assert shown_json["profile"]["launch"]["lang"] == "de-DE" and shown_json["runtime"] is None

    cli("profile", "update", "plain", "--name", "plain-2", "--tag", "a", "--tag", "b", "--no-proxy",
        "--timezone", "Europe/Berlin")
    store = Store(cli.home)
    updated = store.get_profile("plain-2")
    assert updated.tags == ["a", "b"] and updated.proxy_id is None and updated.launch.timezone == "Europe/Berlin"
    refused = cli("profile", "update", "plain-2", "--extra-arg=--remote-debugging-port=1", ok=False)
    assert refused.returncode == 1 and "managed by ProfilePilot" in refused.stderr

    assert "Cloned 'shop' to 'shop-2'" in cli("profile", "clone", "shop", "shop-2").stdout
    deleted = cli("profile", "delete", "shop-2").stdout
    assert "trash" in deleted
    trash = json.loads(cli("profile", "restore", "--json").stdout)
    assert len(trash) == 1 and trash[0]["name"] == "shop-2"
    assert "Restored profile 'shop-2'" in cli("profile", "restore", trash[0]["trash_id"]).stdout

    in_use = cli("proxy", "remove", "shop", ok=False)
    assert in_use.returncode == 1 and "force" in in_use.stderr
    assert "Unbound from" in cli("proxy", "remove", "shop", "--force").stdout

    assert "No profiles are running" in cli("status").stdout
    assert json.loads(cli("status", "--json").stdout) == []
    assert "Stopped 0 profile(s)" in cli("stop-all").stdout

    for output in cli.history:
        assert SECRET not in output and "Cl1-S3cret%21pw" not in output
    on_disk = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in cli.home.rglob("*.json")
                        if p.name != "secrets.json")
    assert SECRET not in on_disk


def test_install_print_dry_run_and_uninstall(cli):
    snippets = json.loads(cli("install", "print", "--json").stdout)
    assert {"claude-desktop", "claude-code", "codex", "cursor", "chatgpt"} <= set(snippets)
    assert "mcpServers" in snippets["claude-desktop"]
    only = cli("install", "print", "--client", "codex", "--port", "9000").stdout
    assert "[mcp_servers.profilepilot]" in only and "## codex" in only and "## cursor" not in only

    if sys.platform == "win32":
        config = cli.users / "Roaming" / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        config = cli.users / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        config = cli.users / "xdg" / "Claude" / "claude_desktop_config.json"
    cli("install", "claude-desktop", "--dry-run")
    assert not config.exists()
    report = cli("install", "claude-desktop").stdout
    assert str(config) in report or "claude_desktop_config.json" in report
    data = json.loads(config.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["profilepilot"]
    assert entry["args"] == ["-m", "profilepilot", "serve"] and Path(entry["command"]).is_absolute()
    cli("uninstall", "claude-desktop")
    assert "profilepilot" not in json.loads(config.read_text(encoding="utf-8")).get("mcpServers", {})
    bad = cli("install", "netscape", ok=False)
    assert bad.returncode == 2


def test_doctor_reports_environment(cli):
    proc = cli("doctor", "--json", ok=False)
    assert proc.returncode in (0, 1)
    report = json.loads(proc.stdout)
    checks = {c["check"]: c for c in report["checks"]}
    for name in ("python", "playwright", "mcp", "browsers", "data root", "secrets", "running profiles", "shardx"):
        assert name in checks, name
    assert checks["data root"]["ok"] is True and str(cli.home) in checks["data root"]["detail"]
    assert checks["shardx"]["detail"] == "disabled"
    assert "[ok ] python" in cli("doctor", ok=False).stdout


def test_shardx_login_from_stdin_and_logout(cli):
    out = cli("shardx", "login", "--token", stdin=FAKE_JWT + "\n").stdout
    assert "ShardX enabled" in out and FAKE_JWT not in out
    store = Store(cli.home)
    assert store.load_config().shardx.enabled and store.secrets.get("shardx:token") == FAKE_JWT
    bad = cli("shardx", "login", "--token", "not-a-jwt", ok=False)
    assert bad.returncode == 1 and "not-a-jwt" not in bad.stderr
    assert "disabled" in cli("shardx", "logout").stdout
    store = Store(cli.home)
    assert not store.load_config().shardx.enabled and store.secrets.get("shardx:token") is None


def test_serve_argument_validation(cli):
    proc = cli("serve", "--auth", "token", ok=False)
    assert proc.returncode == 1 and "need --http" in proc.stderr
    refused = cli("serve", "--http", "--auth", "none", "--port", "1", ok=False)
    assert refused.returncode == 1 and "Refusing" in refused.stderr


TEST_CARD = "4242424242424242"  # the public Stripe test card number
TEST_SSN = "000-12-3456"


def test_identity_commands_never_take_or_print_sensitive_values(cli):
    ident = "Testy McTestface"
    out = cli("identity", "create", ident, "--set", "first_name=Testy", "--set", "last_name=McTestface",
              "--set", "email=testy@example.test", "--set", "zip=12345").stdout
    assert f"Created identity '{ident}'" in out and f'identity secret "{ident}" <field>' in out
    refused = cli("identity", "create", "Other", "--set", f"ssn={TEST_SSN}", ok=False)
    assert refused.returncode == 1 and "never taken from the command line" in refused.stderr
    assert "profilepilot identity secret Other ssn" in refused.stderr
    assert [i["name"] for i in json.loads(cli("identity", "list", "--json").stdout)] == [ident]

    stored = cli("identity", "secret", ident, "card_number", "--stdin", stdin="4242 4242 4242 4242\n").stdout
    assert "Stored card_number" in stored and "visa •••• 4242" in stored and "identity allow" in stored
    cli("identity", "secret", ident, "ssn", "--stdin", stdin=TEST_SSN + "\r\n")
    cli("identity", "secret", ident, "cvv", "--stdin", stdin="123\n")
    argv = cli("identity", "secret", ident, "ssn", TEST_SSN, ok=False)
    assert argv.returncode == 1 and "Never put the value on the command line" in argv.stderr
    luhn = cli("identity", "secret", ident, "card_number", "--stdin", stdin="4242 4242 4242 4241\n", ok=False)
    assert luhn.returncode == 1 and "Luhn" in luhn.stderr and "4241" not in luhn.stderr
    no_tty = cli("identity", "secret", ident, "password", stdin="", ok=False)
    assert no_tty.returncode == 1 and "--stdin" in no_tty.stderr
    plain = cli("identity", "secret", ident, "email", "--stdin", stdin="x@example.test\n", ok=False)
    assert "not a sensitive field" in plain.stderr
    missing = cli("identity", "secret", ident, TEST_CARD, "--stdin", stdin="1\n", ok=False)
    assert "Unknown identity field given" in missing.stderr

    store = Store(cli.home)
    from profilepilot.identity import IdentityStore

    record = IdentityStore(store).get(ident)
    assert store.secrets.get(f"identity:{record.id}:card_number") == TEST_CARD
    assert store.secrets.get(f"identity:{record.id}:ssn") == TEST_SSN
    assert store.secrets.get(f"identity:{record.id}:card_cvv") == "123"
    on_disk = (cli.home / "identities.json").read_text(encoding="utf-8")
    assert TEST_CARD not in on_disk and TEST_SSN not in on_disk and '"123"' not in on_disk

    shown = cli("identity", "show", ident).stdout
    assert "visa •••• 4242" in shown and "•••-••-3456" in shown and "testy@example.test" in shown
    view = json.loads(cli("identity", "show", ident, "--json").stdout)
    assert view["fields"]["card_cvv"] == "set" and view["fields"]["postal_code"] == "12345"

    assert "https://shop.example.test" in cli("identity", "allow", ident, "https://Shop.Example.test/checkout").stdout
    assert "needs HTTPS" in cli("identity", "allow", ident, "http://plain.example.test").stdout
    cli("identity", "disallow", ident, "http://plain.example.test")
    assert json.loads(cli("identity", "list", "--json").stdout)[0]["allowed_origins"] == ["https://shop.example.test"]

    assert f"identity {ident}" in cli("profile", "create", "shop", "--identity", ident, "--window", "offscreen").stdout
    listing = cli("profile", "list").stdout
    assert "IDENTITY" in listing and ident in listing
    cli("profile", "update", "shop", "--identity", "")
    assert Store(cli.home).get_profile("shop").identity_id is None
    cli("profile", "update", "shop", "--identity", ident)
    assert "identity:  Testy McTestface" in cli("profile", "show", "shop").stdout

    out = cli("identity", "set", ident, "city=Testville", "zip=").stdout
    assert "set city" in out and "removed postal_code" in out
    cli("identity", "clear", ident, "ssn")
    assert Store(cli.home).secrets.get(f"identity:{record.id}:ssn") is None
    fields = {f["key"]: f for f in json.loads(cli("identity", "fields", "--json").stdout)}
    assert fields["card_number"]["sensitive"] and "cc" in fields["card_number"]["aliases"]
    assert not fields["email"]["sensitive"]

    unconfirmed = cli("identity", "delete", ident, stdin="", ok=False)
    assert unconfirmed.returncode == 1 and "--yes" in unconfirmed.stderr
    out = cli("identity", "delete", ident, "--yes").stdout
    assert f"Deleted identity '{ident}'" in out and "Unlinked from: shop" in out
    assert Store(cli.home).get_profile("shop").identity_id is None
    assert Store(cli.home).secrets.get(f"identity:{record.id}:card_number") is None

    serve = cli("serve", "--allow-sensitive-autofill", ok=False)
    assert serve.returncode == 1 and "needs --http" in serve.stderr

    for output in cli.history:
        for secret in (TEST_CARD, "4242 4242 4242 4242", TEST_SSN, "000123456"):
            assert secret not in output


def _kill_leftovers(marker: Path) -> None:
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


@pytest.mark.chrome
def test_profile_start_status_stop(tmp_path):
    from tests.chrome_helper import find_test_browser

    find_test_browser()
    cli = make_cli(tmp_path, isolate_clients=False)
    try:
        cli("profile", "create", "c1", "--window", "offscreen")
        started = json.loads(cli("profile", "start", "c1", "--json").stdout)
        assert started["state"] == "running" and started["window"] == "offscreen"
        assert "control_token" not in started and "control_port" not in started
        status = cli("status").stdout
        assert "c1" in status and "offscreen" in status
        running = json.loads(cli("status", "--json").stdout)
        assert [r["profile_name"] for r in running] == ["c1"]
        assert psutil.pid_exists(started["chrome_pid"])
        assert "Stopped 'c1'" in cli("profile", "stop", "c1").stdout
        assert "No profiles are running" in cli("status").stdout
        assert "was not running" in cli("profile", "stop", "c1").stdout
    finally:
        with contextlib.suppress(Exception):
            from profilepilot.browser.runtime import RuntimeManager

            RuntimeManager(Store(cli.home)).stop_all(timeout=15)
        _kill_leftovers(tmp_path)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_serve_http_subprocess_with_bearer_token(tmp_path):
    import asyncio
    import socket

    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from profilepilot.browser.runtime import kill_tree

    cli = make_cli(tmp_path)
    port = _free_port()
    token = "tok-" + "x" * 28
    env = dict(cli.env, PROFILEPILOT_TOKEN=token)
    log = open(tmp_path / "serve.log", "w+", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "profilepilot", "serve", "--http", "--port", str(port), "--auth", "token"],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, cwd=str(tmp_path),
    )
    try:
        for _ in range(150):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            assert proc.poll() is None, (tmp_path / "serve.log").read_text(encoding="utf-8")
            await asyncio.sleep(0.1)
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx2.AsyncClient() as anonymous:
            denied = await anonymous.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                          headers={"Accept": "application/json, text/event-stream"})
            assert denied.status_code == 401
        http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        async with Client(streamable_http_client(url, http_client=http_client)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            result = await client.call_tool("browser_navigate", {"profile": "x", "url": "http://127.0.0.1:1/"})
        assert {"profile_list", "browser_navigate", "http_fetch"} <= names
        assert result.is_error and "Blocked" in result.content[0].text
    finally:
        kill_tree(proc.pid)
        log.close()
    banner = (tmp_path / "serve.log").read_text(encoding="utf-8")
    assert f"127.0.0.1:{port}/mcp" in banner and token not in banner  # a supplied token is never echoed


def test_commands_that_drive_a_profile_respect_the_pause(cli):
    cli("profile", "create", "p")
    cli("profile", "pause", "p", "--note", "logging in")
    for args in (("profile", "start", "p"), ("profile", "stop", "p"), ("profile", "delete", "p"),
                 ("profile", "update", "p", "--no-proxy"), ("profile", "clone", "p", "q", "--copy-data")):
        refused = cli(*args, ok=False)
        assert refused.returncode == 1, args
        assert "'p' is paused for the AI" in refused.stderr and 'profile resume "p"' in refused.stderr, args
        assert "--ignore-pause" in refused.stderr
    cli("profile", "update", "p", "--notes", "harmless")  # no live effect: allowed
    cli("profile", "clone", "p", "q")  # settings only
    assert "'p' was not running" in cli("profile", "stop", "p", "--ignore-pause").stdout
    assert json.loads(cli("stop-all", "--json").stdout) == {"stopped": [], "kept_paused": []}
    cli("profile", "resume", "p")
    assert "Moved 'p' to the trash" in cli("profile", "delete", "p").stdout
