"""``profilepilot connect chatgpt|status|stop`` (src/profilepilot/connect.py).

No real tunnel and no internet: ``cloudflared`` / ``ngrok`` / the server are small Python scripts
that print realistic banners (and record how they were started). Every process a test starts is a
child of this test run and is stopped by the wizard or by ``stop_sharing``.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from profilepilot import connect
from profilepilot.connect import (
    Wizard,
    add_cli,
    find_executable,
    parse_cloudflared_url,
    parse_ngrok_url,
    read_state,
    server_command,
    status_info,
    status_text,
    stop_sharing,
    tunnel_argv,
)
from profilepilot.server.oauth import OAuthStore, pairing_code, rotate_pairing_code
from profilepilot.store import Store

QUICK_HOST = "sunny-river-velvet-orbit.trycloudflare.com"
NGROK_URL = "https://3f2a-203-0-113-7.ngrok-free.app"

CLOUDFLARED_BANNER = [
    "2026-10-08T21:14:03Z INF Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a "
    "quick way to experiment and try it out. However, be aware that these account-less Tunnels have no uptime "
    "guarantee, are subject to the Cloudflare Online Services Terms of Use (https://www.cloudflare.com/website-terms/), "
    "and Cloudflare reserves the right to investigate your use of Tunnels for violations of such terms. If you intend "
    "to use Tunnels in production you should use a pre-created named tunnel by following: "
    "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks",
    "2026-10-08T21:14:03Z INF Requesting new quick Tunnel on trycloudflare.com...",
    "2026-10-08T21:14:05Z INF +--------------------------------------------------------------------------------------------+",
    "2026-10-08T21:14:05Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |",
    f"2026-10-08T21:14:05Z INF |  https://{QUICK_HOST}                                        |",
    "2026-10-08T21:14:05Z INF +--------------------------------------------------------------------------------------------+",
    "2026-10-08T21:14:05Z INF Cannot determine default configuration path. No file [config.yml config.yaml] in "
    "[~/.cloudflared ~/.cloudflare-warp ~/cloudflare-warp /etc/cloudflared /usr/local/etc/cloudflared]",
    "2026-10-08T21:14:05Z INF Version 2026.9.1",
    "2026-10-08T21:14:05Z INF Starting metrics server on 127.0.0.1:20241/metrics",
    "2026-10-08T21:14:06Z INF Registered tunnel connection connIndex=0 connection=8a1f event=0 ip=198.41.200.13 "
    "location=fra08 protocol=quic",
]

NGROK_LINES = [
    '{"addr":"127.0.0.1:4040","lvl":"info","msg":"starting web service","obj":"web","t":"2026-10-08T21:14:03Z"}',
    '{"lvl":"info","msg":"client session established","obj":"tunnels.session","t":"2026-10-08T21:14:04Z"}',
    '{"addr":"http://127.0.0.1:PORT","lvl":"info","msg":"started tunnel","name":"command_line","obj":"tunnels",'
    f'"t":"2026-10-08T21:14:04Z","url":"{NGROK_URL}"}}',
]
NGROK_AUTH_ERROR = (
    '{"err":"authentication failed: Usage of ngrok requires a verified account and authtoken.\\n\\nSign up for an '
    'account: https://dashboard.ngrok.com/signup\\nInstall your authtoken: '
    'https://dashboard.ngrok.com/get-started/your-authtoken\\r\\n\\r\\nERR_NGROK_4018\\r\\n","lvl":"eror",'
    '"msg":"failed to reconnect session","obj":"tunnels.session","t":"2026-10-08T21:14:04Z"}'
)

FAKE_TUNNEL = r'''
import json, os, sys, time
record, mode, lines = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
args = sys.argv[4:]
with open(record, "w", encoding="utf-8") as fh:
    json.dump({"argv": args, "pid": os.getpid()}, fh)
port = next((a.rsplit(":", 1)[1] for a in args if a.startswith("http://127.0.0.1:")), "0")
stream = sys.stderr if mode == "stderr" else sys.stdout
for line in lines:
    stream.write(line.replace("PORT", port) + "\n")
    stream.flush()
    time.sleep(0.02)
if mode == "fail":
    sys.exit(1)
while True:
    time.sleep(0.2)
'''

FAKE_SERVER = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
record, mode = sys.argv[1], sys.argv[2]
args = sys.argv[3:]
with open(record, "w", encoding="utf-8") as fh:
    json.dump({"argv": args, "home": os.environ.get("PROFILEPILOT_HOME"), "pid": os.getpid()}, fh)
if mode == "crash":
    print("Traceback (most recent call last):", flush=True)
    print("ProfilePilotError: something is wrong with the data folder", flush=True)
    sys.exit(3)
port = int(args[args.index("--port") + 1])
host = args[args.index("--public-host") + 1]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"resource": "https://%s/mcp" % host}).encode()
        self.send_response(200 if self.path.startswith("/.well-known/") else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
print("fake ProfilePilot server ready", flush=True)
HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "home")


class Fakes:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.tunnel_script = root / "fake_tunnel.py"
        self.tunnel_script.write_text(FAKE_TUNNEL, encoding="utf-8")
        self.server_script = root / "fake_server.py"
        self.server_script.write_text(FAKE_SERVER, encoding="utf-8")
        self.tunnel_record = root / "tunnel.json"
        self.server_record = root / "server.json"

    def tunnel(self, mode: str, lines: list[str]) -> list[str]:
        return [sys.executable, str(self.tunnel_script), str(self.tunnel_record), mode, json.dumps(lines)]

    def server(self, mode: str = "ok") -> list[str]:
        return [sys.executable, str(self.server_script), str(self.server_record), mode]

    def record(self, which: str) -> dict[str, Any]:
        path = self.tunnel_record if which == "tunnel" else self.server_record
        return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def fakes(tmp_path) -> Fakes:
    return Fakes(tmp_path / "fakes")


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class Run:
    """Runs a wizard in a thread and collects its output."""

    def __init__(self, wizard: Wizard) -> None:
        self.lines: list[str] = []
        wizard.out = self.lines.append
        self.wizard = wizard
        self.code: int | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        self.code = self.wizard.run()

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def join(self, timeout: float = 30.0) -> int | None:
        self.thread.join(timeout)
        return self.code


def make_wizard(store: Store, fakes: Fakes, *, tools: dict[str, list[str]], server_mode: str = "ok", **kw) -> Wizard:
    return Wizard(store=store, locate=lambda name: tools.get(name), server_prefix=fakes.server(server_mode),
                  check_public=False, poll_interval=0.05, tunnel_timeout=20, server_timeout=20, **kw)


# ---------------------------------------------------------------------- unit


def test_parse_tunnel_output():
    found = [parse_cloudflared_url(line) for line in CLOUDFLARED_BANNER]
    assert [u for u in found if u] == [f"https://{QUICK_HOST}"]
    assert parse_cloudflared_url("INF Requesting new quick Tunnel on https://api.trycloudflare.com ...") is None
    assert parse_ngrok_url(NGROK_LINES[2]) == NGROK_URL
    assert parse_ngrok_url(NGROK_LINES[0]) is None and parse_ngrok_url(NGROK_AUTH_ERROR) is None
    logfmt = 't=2026 lvl=info msg="started tunnel" obj=tunnels name=command_line addr=http://localhost:8931 ' \
             f'url={NGROK_URL}'
    assert parse_ngrok_url(logfmt) == NGROK_URL


def test_commands():
    argv = server_command(8931, QUICK_HOST, root="C:/data")
    assert argv[:3] == [sys.executable, "-m", "profilepilot"]
    assert argv[3:] == ["serve", "--http", "--auth", "oauth", "--host", "127.0.0.1", "--port", "8931",
                        "--public-host", QUICK_HOST, "--log-level", "WARNING", "--home", "C:/data"]
    assert tunnel_argv("cloudflared", ["cf"], 9000) == ["cf", "tunnel", "--no-autoupdate", "--url",
                                                        "http://127.0.0.1:9000"]
    assert tunnel_argv("ngrok", ["ng"], 9000, ngrok_domain="me.ngrok-free.app")[-2:] == [
        "--url", "https://me.ngrok-free.app"]


def test_find_executable(tmp_path):
    empty, tools = tmp_path / "empty", tmp_path / "tools"
    empty.mkdir()
    tools.mkdir()
    name = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    assert find_executable("cloudflared", search_path=str(empty), extra_dirs=[tools]) is None
    (tools / name).write_bytes(b"")
    assert find_executable("cloudflared", search_path=str(empty), extra_dirs=[tools]) == str(tools / name)


def test_cli_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--home", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(parents=[common])
    sub = parser.add_subparsers(dest="command")
    add_cli(sub, common)
    args = parser.parse_args(["connect", "chatgpt", "--via", "ngrok", "--port", "9100",
                              "--ngrok-domain", "x.app", "-y"])
    assert args.func is connect.cmd_connect_chatgpt and args.via == "ngrok" and args.port == 9100 and args.yes
    args = parser.parse_args(["connect", "stop", "--revoke"])
    assert args.func is connect.cmd_connect_stop and args.revoke
    assert parser.parse_args(["connect", "status", "--json"]).func is connect.cmd_connect_status
    with pytest.raises(SystemExit):
        parser.parse_args(["connect", "chatgpt", "--via", "carrier-pigeon"])


# ---------------------------------------------------------------------- the wizard


def test_cloudflared_session_end_to_end(store, fakes):
    port = free_port()
    wizard = make_wizard(store, fakes, tools={"cloudflared": fakes.tunnel("stderr", CLOUDFLARED_BANNER)},
                         via="cloudflared", port=port)
    run = Run(wizard)
    try:
        assert wait_until(lambda: read_state(store) is not None and "Ctrl+C" in run.text), run.text
        info = status_info(store)
        assert info["running"] and info["tunnel"] == "cloudflared" and info["port"] == port
        assert info["mcp_url"] == f"https://{QUICK_HOST}/mcp"
        code = info["pairing_code"]
        assert f"URL to paste:   https://{QUICK_HOST}/mcp" in run.text and f"Pairing code:   {code}" in run.text
        assert "Authentication: OAuth" in run.text and "Keep this window open" in run.text
        assert " ".join(connect.INTRO.split()) in " ".join(run.text.split())

        tunnel = fakes.record("tunnel")
        assert tunnel["argv"] == ["tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
        server = fakes.record("server")
        assert server["argv"][:11] == ["serve", "--http", "--auth", "oauth", "--host", "127.0.0.1", "--port",
                                       str(port), "--public-host", QUICK_HOST, "--log-level"]
        assert Path(server["home"]) == Path(store.root).resolve()
        assert server["argv"][server["argv"].index("--home") + 1] == str(store.root)

        # the wizard reports a new connection and the rotated pairing code
        def add_grant(data):
            data["grants"]["g1"] = {"client_id": "c1", "client_name": "ChatGPT", "created_at": time.time(),
                                    "last_used_at": time.time()}
            data["refresh"]["h1"] = {"grant_id": "g1", "client_id": "c1", "expires_at": time.time() + 3600}

        OAuthStore(store.root).mutate(add_grant)
        assert wait_until(lambda: "Connected: ChatGPT" in run.text), run.text
        fresh = rotate_pairing_code(store)
        assert wait_until(lambda: f"Pairing code for the next sign-in: {fresh}" in run.text), run.text
        assert "ChatGPT" in status_text(status_info(store))

        # `profilepilot connect stop` from "another terminal"
        message = stop_sharing(store)
        assert message.startswith("Stopped sharing")
        assert run.join() == 0
    finally:
        wizard.stop_event.set()
        run.join()
    assert not psutil.pid_exists(tunnel["pid"]) or not _running(tunnel["pid"])
    assert not psutil.pid_exists(server["pid"]) or not _running(server["pid"])
    assert read_state(store) is None and status_info(store)["running"] is False
    assert "Stopped" in run.text


def _running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def test_ngrok_session_and_stop_event(store, fakes):
    wizard = make_wizard(store, fakes, tools={"ngrok": fakes.tunnel("stdout", NGROK_LINES)}, via="ngrok",
                         port=free_port(), ngrok_domain=None)
    run = Run(wizard)
    try:
        assert wait_until(lambda: read_state(store) is not None and "Ctrl+C" in run.text), run.text
        assert status_info(store)["mcp_url"] == NGROK_URL + "/mcp"
        assert fakes.record("tunnel")["argv"][:2] == ["http", f"http://127.0.0.1:{wizard.port}"]
        assert fakes.record("server")["argv"][9] == "3f2a-203-0-113-7.ngrok-free.app"
        assert "This address changes every time" not in run.text  # only said for quick tunnels
    finally:
        wizard.stop_event.set()  # like Ctrl+C
    assert run.join() == 0
    assert read_state(store) is None
    assert not _running(fakes.record("tunnel")["pid"]) and not _running(fakes.record("server")["pid"])


def test_ngrok_without_account(store, fakes):
    wizard = make_wizard(store, fakes, tools={"ngrok": fakes.tunnel("fail", [NGROK_AUTH_ERROR])}, via="ngrok")
    run = Run(wizard)
    assert run.join() == 1
    assert "ngrok config add-authtoken" in run.text
    assert not fakes.server_record.exists()  # the server is never started without a tunnel
    assert read_state(store) is None


def test_server_that_fails_to_start(store, fakes):
    wizard = make_wizard(store, fakes, tools={"cloudflared": fakes.tunnel("stderr", CLOUDFLARED_BANNER)},
                         via="cloudflared", server_mode="crash")
    run = Run(wizard)
    assert run.join() == 1
    assert "did not start" in run.text and "something is wrong with the data folder" in run.text
    assert not _running(fakes.record("tunnel")["pid"])  # the tunnel was stopped again
    assert read_state(store) is None


def test_missing_tool_is_only_installed_with_flag(store, fakes):
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> int:
        calls.append(argv)
        return 1

    run = Run(make_wizard(store, fakes, tools={}, via="cloudflared", runner=runner))
    assert run.join() == 1
    assert calls == []  # never runs an installer without --install
    assert "cloudflared is not installed" in run.text
    if os.name == "nt":
        assert "winget install --id Cloudflare.cloudflared" in run.text and "--install" in run.text

    if os.name == "nt" or sys.platform == "darwin":
        run = Run(make_wizard(store, fakes, tools={}, via="cloudflared", runner=runner, install=True))
        assert run.join() == 1
        assert calls and calls[0][0] in ("winget", "brew") and "installer exited with code 1" in run.text


def test_secure_tunnel_option_prints_steps(store, fakes):
    run = Run(make_wizard(store, fakes, tools={"tunnel-client": ["C:/tools/tunnel-client.exe"],
                                               "cloudflared": ["cf"]}))
    assert run.join() == 0
    text = run.text
    assert "Using: OpenAI Secure MCP Tunnel" in text  # recommended when available
    assert "tunnel-client init --sample sample_mcp_stdio_local --profile profilepilot" in text
    assert "--mcp-command" in text and "-m profilepilot serve" in text
    assert "Authentication: No auth" in text and "CONTROL_PLANE_API_KEY" in text
    assert read_state(store) is None and not fakes.server_record.exists()


def test_interactive_choice(store, fakes):
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return "9"

    run = Run(make_wizard(store, fakes, tools={"cloudflared": ["cf"]}, ask=ask))
    assert run.join() == 1 and "Cancelled." in run.text
    assert asked and "How should ChatGPT reach this PC?" in run.text
    assert "Cloudflare quick tunnel" in run.text and "[found]" in run.text and "[not installed]" in run.text


def test_status_stop_and_revoke(store):
    info = status_info(store)
    assert info["running"] is False and info["pairing_code"] == pairing_code(store)
    assert "connect chatgpt" in status_text(info) and info["pairing_code"] in status_text(info)
    assert stop_sharing(store) == "ProfilePilot was not being shared."

    def add_grant(data):
        data["grants"]["g1"] = {"client_id": "c1", "client_name": "ChatGPT", "created_at": time.time()}
        data["access"]["h1"] = {"grant_id": "g1", "client_id": "c1", "expires_at": time.time() + 3600}

    OAuthStore(store.root).mutate(add_grant)
    old = pairing_code(store)
    message = stop_sharing(store, revoke=True)
    assert "Signed out 1 connected app(s)" in message
    assert OAuthStore(store.root).grants() == [] and pairing_code(store) != old

    # a state file whose processes are gone is stale and is cleaned up
    connect.state_path(store).write_text(json.dumps({"url": "https://x.trycloudflare.com", "pid": 999999,
                                                     "pid_create_time": 1.0, "server_pid": 999998}),
                                         encoding="utf-8")
    assert status_info(store)["running"] is False and read_state(store) is None


def test_refuses_a_second_session(store, fakes):
    connect.state_path(store).write_text(json.dumps({"mcp_url": "https://busy.trycloudflare.com/mcp",
                                                     "pid": os.getpid(),
                                                     "pid_create_time": psutil.Process().create_time()}),
                                         encoding="utf-8")
    run = Run(make_wizard(store, fakes, tools={"cloudflared": ["cf"]}, via="cloudflared"))
    assert run.join() == 1 and "already shared" in run.text
    connect.state_path(store).unlink()


DRIVER = r'''
import json, sys
from profilepilot.connect import Wizard
from profilepilot.store import Store
tunnel, server = json.loads(sys.argv[2]), json.loads(sys.argv[3])
wizard = Wizard(store=Store(sys.argv[1]), via="cloudflared", locate=lambda name: tunnel if name == "cloudflared" else None,
                server_prefix=server, check_public=False, poll_interval=0.1)
sys.exit(wizard.run())
'''


def test_stop_from_another_process(store, fakes, tmp_path):
    """`profilepilot connect stop` in a second terminal: the wizard (another process) shuts down cleanly."""
    import subprocess

    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    env = {**os.environ, "PROFILEPILOT_SECRETS": "file", "PYTHONUNBUFFERED": "1"}
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen([sys.executable, str(driver), str(store.root), json.dumps(fakes.tunnel(
        "stderr", CLOUDFLARED_BANNER)), json.dumps(fakes.server())], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=env, creationflags=flags, text=True, encoding="utf-8", errors="replace")
    try:
        assert wait_until(lambda: (read_state(store) or {}).get("server_pid") is not None or proc.poll() is not None)
        assert proc.poll() is None, proc.stdout.read() if proc.stdout else ""
        assert status_info(store)["running"] is True
        message = stop_sharing(store)
        assert message.startswith("Stopped sharing")
        assert proc.wait(timeout=30) == 0
        output = proc.stdout.read() if proc.stdout else ""
        assert "connect stop` was run" in output and "Stopped sharing" in output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert not _running(fakes.record("tunnel")["pid"]) and not _running(fakes.record("server")["pid"])
    assert read_state(store) is None


REAL_SERVER = r'''
import sys
import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from profilepilot.server.oauth import build_oauth, public_base_url
from profilepilot.store import Store
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
host = args[args.index("--public-host") + 1]
store = Store(args[args.index("--home") + 1])
setup = build_oauth(store, public_base_url("127.0.0.1", port, [host]))
server = MCPServer("pp-connect-test", auth_server_provider=setup.provider, auth=setup.settings)

@server.tool()
def ping() -> str:
    """Answer pong."""
    return "pong"

inner = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                 allowed_hosts=[host, "127.0.0.1:*"], allowed_origins=["https://" + host]))
uvicorn.run(setup.wrap(inner), host="127.0.0.1", port=port, log_level="warning", lifespan="on")
'''


def test_card_code_signs_in_to_a_real_oauth_server(store, fakes, tmp_path):
    """The wizard + a real OAuth-protected MCP server process: the pairing code on the card is the one
    the server's sign-in page accepts, and the issued token opens /mcp."""
    import base64
    import hashlib
    import re
    import secrets as pysecrets
    from urllib.parse import parse_qs, urlencode, urlsplit

    import httpx

    script = tmp_path / "real_server.py"
    script.write_text(REAL_SERVER, encoding="utf-8")
    port = free_port()
    wizard = Wizard(store=store, via="cloudflared", port=port, check_public=False, poll_interval=0.05,
                    locate=lambda name: fakes.tunnel("stderr", CLOUDFLARED_BANNER) if name == "cloudflared" else None,
                    server_prefix=[sys.executable, str(script)], server_timeout=60)
    run = Run(wizard)
    try:
        assert wait_until(lambda: "Pairing code:" in run.text or run.code is not None, timeout=60), run.text
        code = re.search(r"Pairing code:\s+([A-Z0-9]{4}-[A-Z0-9]{4})", run.text).group(1)
        host = {"Host": QUICK_HOST}
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=host, trust_env=False) as http:
            reg = http.post("/register", json={"redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
                                               "client_name": "ChatGPT", "token_endpoint_auth_method": "none"})
            client_id = reg.json()["client_id"]
            verifier = pysecrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            auth = http.get("/authorize?" + urlencode({
                "response_type": "code", "client_id": client_id, "code_challenge": challenge,
                "code_challenge_method": "S256", "state": "s",
                "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect"}))
            consent_path = urlsplit(auth.headers["location"]).path + "?" + urlsplit(auth.headers["location"]).query
            page = http.get(consent_path)
            fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', page.text))
            # the CSRF cookie is Secure (https issuer): send it like the browser does over the https tunnel
            cookie = page.cookies.get("pp_consent")
            assert cookie and "secure" in page.headers["set-cookie"].lower()
            done = http.post("/oauth/consent", data={**fields, "code": code, "action": "approve"},
                             headers={"Cookie": f"pp_consent={cookie}"})
            assert done.status_code == 303, done.text
            auth_code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
            token = http.post("/token", data={"grant_type": "authorization_code", "code": auth_code,
                                              "client_id": client_id, "code_verifier": verifier,
                                              "redirect_uri": "https://chatgpt.com/connector_platform_oauth_redirect"})
            assert token.status_code == 200, token.text
            listed = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                               headers={"Accept": "application/json, text/event-stream",
                                        "Authorization": "Bearer " + token.json()["access_token"]})
            assert listed.status_code == 200 and "ping" in listed.text
        assert wait_until(lambda: "Connected: ChatGPT" in run.text), run.text
        assert wait_until(lambda: "Pairing code for the next sign-in:" in run.text), run.text
    finally:
        wizard.stop_event.set()
        run.join(60)
    assert read_state(store) is None
