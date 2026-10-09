"""``profilepilot connect chatgpt | status | stop``: share ProfilePilot with ChatGPT, step by step.

ChatGPT (on the web) cannot start programs on this computer: it only talks to MCP servers on the
internet. The wizard explains that in plain language and offers three ways:

A. **OpenAI Secure MCP Tunnel** (``tunnel-client``): outbound only, no public address and no
   sign-in needed. Needs a ChatGPT workspace with tunnels and an OpenAI Platform API key, so the
   wizard prints the exact steps (with this machine's Python path) instead of running it.
B. **Cloudflare quick tunnel** (``cloudflared``, no account): the wizard starts
   ``cloudflared tunnel --url http://127.0.0.1:<port>``, reads the ``https://*.trycloudflare.com``
   address it prints, then starts ``profilepilot serve --http --auth oauth --public-host <host>``.
C. **ngrok** (free account): the same with ``ngrok http``; ``--ngrok-domain`` keeps a fixed address.

(``--via url --public-url https://...`` uses a tunnel or reverse proxy you run yourself.)

With B and C the server is protected by OAuth with a **pairing code** (see
:mod:`profilepilot.server.oauth`): ChatGPT's sign-in page asks for the code shown here, so only
someone who can see this screen can connect. The wizard keeps running until Ctrl+C (or
``profilepilot connect stop`` from another terminal), then stops both processes. While it runs,
``<data root>/chatgpt.json`` (``url``, ``mcp_url``, ``started_at``, pids) lets ProfilePilot Manager
and ``connect status`` show the connection.

Nothing is installed unless ``--install`` is given (then ``winget`` / ``brew`` install cloudflared).
Child output is kept in memory only (it is shown when something fails), never written to disk.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import psutil

from .errors import ProfilePilotError
from .jsonio import read_json, write_json
from .procs import process_alive

log = logging.getLogger("profilepilot.connect")

CHATGPT_FILE = "chatgpt.json"
DEFAULT_PORT = 8931
MCP_PATH = "/mcp"
VIA_CHOICES = ("auto", "cloudflared", "ngrok", "tunnel-client", "url")

INTRO = (
    "ChatGPT can only reach MCP servers on the internet. This starts a secure tunnel to ProfilePilot on this "
    "PC, protected by a sign-in that only you can approve."
)

CLOUDFLARE_URL_RE = re.compile(r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)*\.trycloudflare\.com\b", re.I)
_NGROK_URL_RE = re.compile(r"\burl=(https://[^\s\"']+)")
NGROK_AUTH_HINTS = ("ERR_NGROK_4018", "ERR_NGROK_105", "authtoken", "authentication failed")
TUNNELS_SETTINGS_URL = "https://platform.openai.com/settings/organization/tunnels"
TUNNEL_CLIENT_RELEASES = "https://github.com/openai/tunnel-client/releases/latest"
CLOUDFLARED_DOCS = "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
NGROK_SIGNUP = "https://dashboard.ngrok.com/get-started/your-authtoken"

Out = Callable[[str], None]


def _print(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


Ask = Callable[[str], str]
Locate = Callable[[str], "list[str] | None"]


# ---------------------------------------------------------------------- finding the tunnel programs


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def common_dirs(name: str) -> list[Path]:
    """Where installers usually put ``name`` (besides PATH): winget, Program Files, Homebrew."""
    dirs: list[Path] = []
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        pf = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
        pf86 = Path(os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
        dirs += [local / "Microsoft" / "WinGet" / "Links", pf86 / name, pf / name, local / name,
                 local / "Programs" / name]
        packages = local / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            with contextlib.suppress(OSError):
                dirs += sorted(p for p in packages.iterdir() if p.is_dir() and name.lower() in p.name.lower())
    else:
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path.home() / ".local" / "bin", Path("/usr/bin")]
    return dirs


def find_executable(name: str, *, search_path: str | None = None,
                    extra_dirs: Iterable[Path] | None = None) -> str | None:
    """``name`` on PATH (or ``search_path``), else in the usual install folders."""
    found = shutil.which(name, path=search_path)
    if found:
        return found
    for folder in common_dirs(name) if extra_dirs is None else extra_dirs:
        candidate = Path(folder) / _exe(name)
        if candidate.is_file():
            return str(candidate)
    return None


def default_locate(name: str) -> list[str] | None:
    path = find_executable(name)
    return [path] if path else None


# ---------------------------------------------------------------------- parsing tunnel output


def parse_cloudflared_url(line: str) -> str | None:
    """The ``https://<words>.trycloudflare.com`` address from a cloudflared log line."""
    if "api.trycloudflare.com" in line:  # the quick-tunnel API host is not the tunnel address
        line = line.replace("api.trycloudflare.com", "")
    match = CLOUDFLARE_URL_RE.search(line)
    return match.group(0).lower() if match else None


def parse_ngrok_url(line: str) -> str | None:
    """The public ``https://`` address from an ngrok log line (``--log-format json`` or logfmt)."""
    text = line.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            url = data.get("url")
            if isinstance(url, str) and url.startswith("https://") and (
                data.get("msg") in (None, "started tunnel") or data.get("obj") == "tunnels"
            ):
                return url.rstrip("/")
            return None
    match = _NGROK_URL_RE.search(text)
    return match.group(1).rstrip("/") if match else None


def host_of(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0].lower()


# ---------------------------------------------------------------------- child processes


class ManagedProcess:
    """A child process whose output is read by a thread and kept in memory (never on disk)."""

    def __init__(self, argv: Sequence[str], *, name: str, env: dict[str, str] | None = None) -> None:
        self.name = name
        self.argv = list(argv)
        self.lines: deque[str] = deque(maxlen=200)
        self._new: deque[str] = deque()
        self._cond = threading.Condition()
        flags = 0
        if os.name == "nt":  # no console window of its own; Ctrl+C is handled by the wizard
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        self.proc = subprocess.Popen(
            self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, creationflags=flags, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        try:
            self.create_time: float | None = psutil.Process(self.proc.pid).create_time()
        except psutil.Error:
            self.create_time = None
        self._reader = threading.Thread(target=self._read, name=f"{name}-output", daemon=True)
        self._reader.start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _read(self) -> None:
        stream = self.proc.stdout
        if stream is None:
            return
        for raw in stream:
            line = raw.rstrip("\r\n")
            with self._cond:
                self.lines.append(line)
                self._new.append(line)
                self._cond.notify_all()
        with self._cond:
            self._cond.notify_all()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def wait_for(self, parse: Callable[[str], str | None], timeout: float) -> str | None:
        """The first value ``parse`` finds in the output, or None (timeout / the process ended)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                while self._new:
                    found = parse(self._new.popleft())
                    if found:
                        return found
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (not self.alive() and not self._reader.is_alive()):
                    return None
                self._cond.wait(min(remaining, 0.25))

    def output_contains(self, needles: Iterable[str]) -> bool:
        text = "\n".join(self.lines).lower()
        return any(n.lower() in text for n in needles)

    def tail(self, count: int = 12) -> list[str]:
        return [line for line in list(self.lines)[-count:] if line.strip()]

    def stop(self, timeout: float = 8.0) -> None:
        if self.proc.poll() is None:
            kill_tree(self.proc.pid, self.create_time, timeout=timeout)
        with contextlib.suppress(Exception):
            self.proc.wait(timeout=timeout)
        with contextlib.suppress(Exception):
            if self.proc.stdout:
                self.proc.stdout.close()


def kill_tree(pid: int | None, create_time: float | None, *, timeout: float = 8.0) -> bool:
    """Terminate ``pid`` and its children, but only if it is still the process created at
    ``create_time`` (never a recycled PID). Returns True if something was stopped."""
    if not pid or not process_alive(pid, create_time):
        return False
    try:
        parent = psutil.Process(int(pid))
        procs = parent.children(recursive=True) + [parent]
    except psutil.Error:
        return False
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.terminate()
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(alive, timeout=timeout)
    return True


# ---------------------------------------------------------------------- commands


def server_command(port: int, public_host: str, *, root: Path | str, prefix: Sequence[str] | None = None,
                   log_level: str = "WARNING") -> list[str]:
    """``profilepilot serve`` for the tunnel: Streamable HTTP on loopback, OAuth for ``public_host``."""
    base = list(prefix) if prefix else [sys.executable, "-m", "profilepilot"]
    return [*base, "serve", "--http", "--auth", "oauth", "--host", "127.0.0.1", "--port", str(int(port)),
            "--public-host", public_host, "--log-level", log_level, "--home", str(root)]


def tunnel_argv(kind: str, prefix: Sequence[str], port: int, *, ngrok_domain: str | None = None) -> list[str]:
    target = f"http://127.0.0.1:{int(port)}"
    if kind == "cloudflared":
        return [*prefix, "tunnel", "--no-autoupdate", "--url", target]
    if kind == "ngrok":
        argv = [*prefix, "http", target, "--log", "stdout", "--log-format", "json"]
        if ngrok_domain:
            argv += ["--url", ngrok_domain if "://" in ngrok_domain else f"https://{ngrok_domain}"]
        return argv
    raise ProfilePilotError(f"Unknown tunnel {kind!r}.")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", int(port)))
        except OSError:
            return False
    return True


def pick_port(preferred: int | None) -> int:
    if preferred:
        if not port_free(preferred):
            raise ProfilePilotError(f"Port {preferred} is already in use. Choose another one with --port.")
        return int(preferred)
    if port_free(DEFAULT_PORT):
        return DEFAULT_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_server_ready(port: int, proc: ManagedProcess, timeout: float) -> bool:
    """Poll the server's protected-resource metadata until it answers (or the process ends)."""
    import httpx

    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{int(port)}/.well-known/oauth-protected-resource"
    while time.monotonic() < deadline:
        if not proc.alive():
            return False
        with contextlib.suppress(httpx.HTTPError):
            response = httpx.get(url, timeout=2.0, trust_env=False)
            if response.status_code < 500:
                return True
        time.sleep(0.25)
    return False


def check_reachable(url: str, timeout: float = 30.0, stop: threading.Event | None = None) -> bool:
    """Best effort: does ``url``'s metadata answer through the tunnel yet? (DNS for a fresh quick
    tunnel can take a little while.)"""
    import httpx

    deadline = time.monotonic() + timeout
    target = url.rstrip("/") + "/.well-known/oauth-protected-resource"
    while time.monotonic() < deadline and not (stop and stop.is_set()):
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(target, timeout=5.0, trust_env=False).status_code == 200:
                return True
        time.sleep(3.0)
    return False


# ---------------------------------------------------------------------- state file


def state_path(store: Any) -> Path:
    return Path(store.root) / CHATGPT_FILE


def read_state(store: Any) -> dict[str, Any] | None:
    data = read_json(state_path(store), None)
    return data if isinstance(data, dict) else None


def _state_alive(state: dict[str, Any]) -> bool:
    return process_alive(state.get("server_pid"), state.get("server_create_time")) or process_alive(
        state.get("pid"), state.get("pid_create_time"))


def status_info(store: Any) -> dict[str, Any]:
    """Connection status for ``connect status``, ProfilePilot Manager (``GET /api/chatgpt``) and tests.

    ``running`` / ``url`` / ``mcp_url`` / ``tunnel`` / ``started_at`` / ``port`` describe the shared
    server (when ``connect chatgpt`` runs); ``pairing_code`` and ``connections`` (approved apps)
    come from the OAuth store. A stale state file (its processes are gone) is removed."""
    from .server.oauth import oauth_status

    state = read_state(store)
    running = bool(state) and _state_alive(state or {})
    if state and not running:
        with contextlib.suppress(OSError):
            state_path(store).unlink()
        state = None
    info: dict[str, Any] = {
        "running": running,
        "url": state.get("url") if state else None,
        "mcp_url": state.get("mcp_url") if state else None,
        "tunnel": state.get("tunnel") if state else None,
        "started_at": state.get("started_at") if state else None,
        "port": state.get("port") if state else None,
    }
    info.update(oauth_status(store))
    return info


TUNNEL_LABELS = {
    "cloudflared": "Cloudflare quick tunnel",
    "ngrok": "ngrok",
    "url": "your own public URL",
    "tunnel-client": "OpenAI Secure MCP Tunnel",
}


def _clock(value: Any) -> str:
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(float(value))
        else:
            moment = datetime.fromisoformat(str(value)).astimezone()
        return moment.strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def status_text(info: dict[str, Any]) -> str:
    lines: list[str] = []
    if info.get("running"):
        lines.append(f"ChatGPT connection: ON ({TUNNEL_LABELS.get(info.get('tunnel') or '', 'tunnel')}, "
                     f"since {_clock(info.get('started_at'))}).")
        lines.append(f"  URL to paste:  {info.get('mcp_url')}")
    else:
        lines.append("ChatGPT connection: off. Start it with: profilepilot connect chatgpt")
    lines.append(f"  Pairing code:  {info.get('pairing_code')}   (asked on the sign-in page; changes after each use)")
    connections = info.get("connections") or []
    if connections:
        lines.append("  Connected apps:")
        for grant in connections:
            name = grant.get("client_name") or grant.get("client_id") or "app"
            lines.append(f"    - {name} (approved {_clock(grant.get('created_at'))}, last used "
                         f"{_clock(grant.get('last_used_at'))})")
    else:
        lines.append("  Connected apps: none")
    return "\n".join(lines)


def stop_sharing(store: Any, *, revoke: bool = False, timeout: float = 8.0) -> str:
    """Stop a running ``connect chatgpt`` and optionally sign out every connected app.

    The wizard is asked first (``"stopping": true`` in the state file): it stops its tunnel and
    server itself and removes the file. Whatever is still running after a few seconds is stopped
    here, but only processes recorded in the state file with the same PID *and* creation time."""
    from .server.oauth import OAuthStore, rotate_pairing_code

    state = read_state(store)
    stopped = False
    if state:
        alive = _state_alive(state)
        if alive:
            with contextlib.suppress(OSError, ProfilePilotError):
                write_json(state_path(store), {**state, "stopping": True})
            wizard_pid, wizard_created = state.get("pid"), state.get("pid_create_time")
            other = bool(wizard_pid) and int(wizard_pid) != os.getpid()
            deadline = time.monotonic() + max(3.0, timeout)
            while time.monotonic() < deadline and (
                state_path(store).exists() or (other and process_alive(wizard_pid, wizard_created))
            ):
                time.sleep(0.1)
            stopped = True
        for key in ("tunnel", "server"):
            stopped |= kill_tree(state.get(f"{key}_pid"), state.get(f"{key}_create_time"), timeout=timeout)
        wizard_pid = state.get("pid")
        if wizard_pid and int(wizard_pid) != os.getpid():
            stopped |= kill_tree(wizard_pid, state.get("pid_create_time"), timeout=timeout)
        with contextlib.suppress(OSError):
            state_path(store).unlink()
    parts = ["Stopped sharing ProfilePilot: ChatGPT cannot reach this computer any more." if stopped
             else "ProfilePilot was not being shared."]
    if revoke:
        count = OAuthStore(store.root).revoke_all()
        rotate_pairing_code(store)
        parts.append(f"Signed out {count} connected app(s); the next connection needs the new pairing code.")
    return " ".join(parts)


# ---------------------------------------------------------------------- the wizard


@dataclass
class Option:
    kind: str
    title: str
    blurb: str
    available: bool
    argv: list[str] | None = None


@dataclass
class Wizard:
    """``profilepilot connect chatgpt``. Every outside effect is injectable for tests."""

    store: Any
    via: str = "auto"
    port: int | None = None
    install: bool = False
    public_url: str | None = None
    ngrok_domain: str | None = None
    yes: bool = False
    out: Out = _print
    ask: Ask | None = None
    locate: Locate = default_locate
    server_prefix: Sequence[str] | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    check_public: bool = True
    tunnel_timeout: float = 60.0
    server_timeout: float = 45.0
    poll_interval: float = 1.0
    runner: Callable[[list[str]], int] = field(default=lambda argv: subprocess.call(argv))
    _children: list[ManagedProcess] = field(default_factory=list, init=False)

    # -- entry point

    def run(self) -> int:
        if self.via not in VIA_CHOICES:
            raise ProfilePilotError(f"--via must be one of: {', '.join(VIA_CHOICES)}.")
        existing = read_state(self.store)
        if existing and _state_alive(existing):
            self.out(f"ProfilePilot is already shared with ChatGPT at {existing.get('mcp_url')}.")
            self.out("Run `profilepilot connect status` for the pairing code, or `profilepilot connect stop` first.")
            return 1
        self._header()
        option = self._choose()
        if option is None:
            return 1
        if option.kind == "tunnel-client":
            self._secure_tunnel_steps(option)
            return 0
        if option.kind != "url" and not option.available:
            if not self._install_missing(option):
                return 1
            option.argv = self.locate(option.kind)
            if not option.argv:
                self.out(f"{option.kind} is still not found. Open a new terminal (so PATH is refreshed) and run "
                         "`profilepilot connect chatgpt` again.")
                return 1
        return self._share(option)

    # -- steps

    def _header(self) -> None:
        self.out("")
        self.out("ProfilePilot -> ChatGPT")
        self.out("=" * 23)
        for line in textwrap.wrap(INTRO, width=88):
            self.out(line)
        self.out("")

    def options(self) -> list[Option]:
        cloudflared = self.locate("cloudflared")
        ngrok = self.locate("ngrok")
        tunnel_client = self.locate("tunnel-client")
        opts = [
            Option("cloudflared", "Cloudflare quick tunnel", "no account needed; a new address each time",
                   bool(cloudflared), cloudflared),
            Option("ngrok", "ngrok", "free ngrok account; can keep one fixed address", bool(ngrok), ngrok),
            Option("tunnel-client", "OpenAI Secure MCP Tunnel",
                   "no public address; needs ChatGPT Business/Enterprise/Edu + an OpenAI API key",
                   bool(tunnel_client), tunnel_client),
        ]
        if tunnel_client:  # recommended when available
            opts.insert(0, opts.pop(2))
        elif not cloudflared and ngrok:
            opts.insert(0, opts.pop(1))
        return opts

    def _choose(self) -> Option | None:
        if self.via == "url" or self.public_url:
            if not self.public_url:
                raise ProfilePilotError("--via url needs --public-url https://<your public host>.")
            return Option("url", "Your own public URL", "", True, None)
        opts = self.options()
        if self.via != "auto":
            return next(o for o in opts if o.kind == self.via)
        interactive = self.ask is not None and not self.yes
        if not interactive:
            choice = opts[0]
            self.out(f"Using: {choice.title} ({choice.blurb}).")
            return choice
        self.out("How should ChatGPT reach this PC?")
        for index, opt in enumerate(opts, 1):
            mark = "found" if opt.available else "not installed"
            self.out(f"  {index}) {opt.title:<26} {opt.blurb}  [{mark}]")
        assert self.ask is not None
        answer = (self.ask(f"Choose 1-{len(opts)} [1]: ") or "1").strip()
        if not answer.isdigit() or not 1 <= int(answer) <= len(opts):
            self.out("Cancelled.")
            return None
        return opts[int(answer) - 1]

    def _install_missing(self, option: Option) -> bool:
        if option.kind == "cloudflared":
            if os.name == "nt":
                command = ["winget", "install", "--id", "Cloudflare.cloudflared", "-e"]
            elif sys.platform == "darwin":
                command = ["brew", "install", "cloudflared"]
            else:
                command = []
            hint = " ".join(command) if command else f"see {CLOUDFLARED_DOCS}"
        else:
            command = ["winget", "install", "--id", "Ngrok.Ngrok", "-e"] if os.name == "nt" else (
                ["brew", "install", "ngrok"] if sys.platform == "darwin" else [])
            hint = " ".join(command) if command else "see https://ngrok.com/download"
        if not self.install or not command:
            self.out(f"{option.kind} is not installed. Install it with:")
            self.out(f"    {hint}")
            if option.kind == "ngrok":
                self.out(f"then sign in once: ngrok config add-authtoken <token from {NGROK_SIGNUP}>")
            self.out("Then run `profilepilot connect chatgpt` again" + (", or add --install to do it now." if command
                                                                       else "."))
            return False
        self.out(f"Installing {option.kind}: {' '.join(command)}")
        code = self.runner(command)
        if code != 0:
            self.out(f"The installer exited with code {code}. Install {option.kind} yourself ({hint}) and try again.")
            return False
        return True

    def _secure_tunnel_steps(self, option: Option) -> None:
        from .install import server_spec, tunnel_command

        command, warning = tunnel_command(server_spec())
        quote = "'" if "'" not in command else '"'
        self.out("OpenAI Secure MCP Tunnel: ChatGPT reaches ProfilePilot through an outbound-only tunnel.")
        self.out("No public address and no sign-in page are involved.")
        self.out("")
        self.out(f"1. Create a tunnel at {TUNNELS_SETTINGS_URL}")
        self.out("   (needs the Tunnels Read + Manage permissions) and copy its id (tunnel_...).")
        where = f" (found: {option.argv[-1]})" if option.argv else f": {TUNNEL_CLIENT_RELEASES}"
        self.out(f"2. Install tunnel-client{where}.")
        self.out("3. In a terminal where CONTROL_PLANE_API_KEY holds an OpenAI API key with Tunnels Read + Use:")
        if warning:
            self.out(f"   Note: {warning}")
        self.out("     tunnel-client init --sample sample_mcp_stdio_local --profile profilepilot "
                 f"--tunnel-id <tunnel_id> --mcp-command {quote}{command}{quote}")
        self.out("     tunnel-client run --profile profilepilot")
        self.out("4. In ChatGPT: chatgpt.com/plugins > + > Add custom MCP server > Connection: Tunnel > pick your")
        self.out("   tunnel > Authentication: No auth > accept the warning > Create as a plugin.")
        self.out("Keep `tunnel-client run` running while you use ChatGPT.")

    def _share(self, option: Option) -> int:
        from .server.oauth import OAuthStore, pairing_code, rotate_pairing_code

        port = pick_port(self.port)
        code = rotate_pairing_code(self.store)  # a fresh code for every sharing session
        try:
            if option.kind == "url":
                assert self.public_url
                public = self.public_url.rstrip("/")
                if public.endswith(MCP_PATH):
                    public = public[: -len(MCP_PATH)]
                if not public.startswith("https://"):
                    raise ProfilePilotError("--public-url must start with https://")
                tunnel = None
                self.out(f"Using your public URL {public} (it must forward to http://127.0.0.1:{port}).")
            else:
                tunnel, public = self._start_tunnel(option, port)
                if tunnel is None or public is None:
                    return 1
            host = host_of(public)
            server = ManagedProcess(server_command(port, host, root=self.store.root, prefix=self.server_prefix),
                                    name="server", env=self._child_env())
            self._children.append(server)
            self.out("Starting the ProfilePilot server...")
            if not wait_server_ready(port, server, self.server_timeout):
                self.out("The ProfilePilot server did not start:")
                for line in server.tail():
                    self.out(f"    {line}")
                return 1
            mcp_url = public + MCP_PATH
            self._write_state(public, mcp_url, option.kind, port, server, tunnel)
            self._card(mcp_url, code, option.kind)
            return self._watch(public, server, tunnel, code, OAuthStore(self.store.root), pairing_code)
        except KeyboardInterrupt:
            self.out("")
            return 0
        finally:
            self._cleanup()

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PROFILEPILOT_HOME"] = str(Path(self.store.root).resolve())
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _start_tunnel(self, option: Option, port: int) -> tuple[ManagedProcess | None, str | None]:
        assert option.argv
        argv = tunnel_argv(option.kind, option.argv, port, ngrok_domain=self.ngrok_domain)
        self.out(f"Starting the {option.title}...")
        tunnel = ManagedProcess(argv, name=option.kind)
        self._children.append(tunnel)
        parse = parse_cloudflared_url if option.kind == "cloudflared" else parse_ngrok_url
        public = tunnel.wait_for(parse, self.tunnel_timeout)
        if public:
            return tunnel, public.rstrip("/")
        if option.kind == "ngrok" and tunnel.output_contains(NGROK_AUTH_HINTS):
            self.out("ngrok needs a (free) account. Copy your authtoken from")
            self.out(f"    {NGROK_SIGNUP}")
            self.out("and run once:  ngrok config add-authtoken <token>   then try again.")
        else:
            self.out(f"The {option.title} did not report a public address"
                     + (" (it exited)." if not tunnel.alive() else f" within {int(self.tunnel_timeout)} s."))
            for line in tunnel.tail():
                self.out(f"    {line}")
            self.out("Check your internet connection (and any firewall or VPN) and try again.")
        return None, None

    def _write_state(self, public: str, mcp_url: str, kind: str, port: int, server: ManagedProcess,
                     tunnel: ManagedProcess | None) -> None:
        me = psutil.Process(os.getpid())
        write_json(state_path(self.store), {
            "url": public, "mcp_url": mcp_url, "tunnel": kind, "port": port,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(), "pid_create_time": me.create_time(),
            "server_pid": server.pid, "server_create_time": server.create_time,
            "tunnel_pid": tunnel.pid if tunnel else None, "tunnel_create_time": tunnel.create_time if tunnel else None,
        })

    def _card(self, mcp_url: str, code: str, kind: str) -> None:
        rows = [f"URL to paste:   {mcp_url}", f"Pairing code:   {code}"]
        width = max(len(r) for r in rows) + 4
        self.out("")
        self.out("+" + "-" * width + "+")
        for row in rows:
            self.out("|  " + row.ljust(width - 2) + "|")
        self.out("+" + "-" * width + "+")
        self.out("In ChatGPT:")
        self.out("  1. Open chatgpt.com/plugins (or Settings > Apps & Connectors).")
        self.out("  2. Click + > Add custom MCP server. Name: ProfilePilot.")
        self.out("  3. Connection: paste the URL above.  Authentication: OAuth.")
        self.out("  4. Accept the warning and create it. A ProfilePilot sign-in page opens:")
        self.out("     enter the pairing code and click Approve.")
        self.out("Claude (claude.ai) works too: Settings > Connectors > Add custom connector > same URL.")
        if kind == "cloudflared":
            self.out("This address changes every time you run this. Next time, update the URL in ChatGPT")
            self.out("(or use --via ngrok --ngrok-domain <your free static domain> for a fixed one).")
        self.out("Keep this window open while you use ChatGPT. Press Ctrl+C to stop sharing.")
        self.out("")

    def _watch(self, public: str, server: ManagedProcess, tunnel: ManagedProcess | None, code: str,
               db: Any, current_code: Callable[[Any], str]) -> int:
        if self.check_public and tunnel is not None:
            threading.Thread(target=self._report_reachability, args=(public,), daemon=True).start()
        known = {g["grant_id"] for g in db.grants()}
        last_code = code
        while not self.stop_event.wait(self.poll_interval):
            state = read_state(self.store)
            if not state or state.get("pid") != os.getpid() or state.get("stopping"):
                self.out("Stopping: `profilepilot connect stop` was run.")
                return 0
            for proc in (server, tunnel):
                if proc is not None and not proc.alive():
                    label = "ProfilePilot server" if proc is server else "tunnel"
                    self.out(f"The {label} stopped unexpectedly:")
                    for line in proc.tail():
                        self.out(f"    {line}")
                    return 1
            with contextlib.suppress(Exception):
                for grant in db.grants():
                    if grant["grant_id"] not in known:
                        known.add(grant["grant_id"])
                        name = grant.get("client_name") or "An app"
                        self.out(f"Connected: {name} (approved at {_clock(grant.get('created_at'))}).")
            with contextlib.suppress(Exception):
                fresh = current_code(self.store)
                if fresh != last_code:
                    last_code = fresh
                    self.out(f"Pairing code for the next sign-in: {fresh}")
        return 0

    def _report_reachability(self, public: str) -> None:
        if check_reachable(public, stop=self.stop_event):
            self.out("Reachable from the internet: OK.")
        elif not self.stop_event.is_set():
            self.out(f"Note: {public} does not answer from here yet. A new quick tunnel can take a minute; if "
                     "ChatGPT cannot connect, check firewall/VPN settings.")

    def _cleanup(self) -> None:
        self.stop_event.set()
        for proc in reversed(self._children):
            with contextlib.suppress(Exception):
                proc.stop()
        state = read_state(self.store)
        if state and state.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                state_path(self.store).unlink()
        if self._children:
            self.out("Stopped sharing: ChatGPT can no longer reach ProfilePilot on this computer.")
        self._children.clear()


# ---------------------------------------------------------------------- CLI


def _store(args: argparse.Namespace) -> Any:
    from .store import Store

    home = getattr(args, "home", None)
    return Store(home) if home else Store()


def cmd_connect_chatgpt(args: argparse.Namespace) -> int:
    interactive = sys.stdin is not None and sys.stdin.isatty() and not args.yes
    wizard = Wizard(
        store=_store(args), via=args.via, port=args.port, install=args.install, public_url=args.public_url,
        ngrok_domain=args.ngrok_domain, yes=args.yes, out=_print, ask=input if interactive else None,
        check_public=not args.no_check,
    )
    return wizard.run()


def cmd_connect_status(args: argparse.Namespace) -> int:
    info = status_info(_store(args))
    if getattr(args, "json", False):
        _print(json.dumps(info, indent=2, default=str))
    else:
        _print(status_text(info))
    return 0


def cmd_connect_stop(args: argparse.Namespace) -> int:
    _print(stop_sharing(_store(args), revoke=args.revoke))
    return 0


def add_cli(subparsers: Any, common: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add ``connect chatgpt|status|stop`` to the ``profilepilot`` parser (see WIRE-IN.md)."""
    parser = subparsers.add_parser("connect", help="share ProfilePilot with ChatGPT (tunnel + sign-in)",
                                   parents=[common])
    actions = parser.add_subparsers(dest="action", metavar="<action>", required=True)

    def add(name: str, text: str, func: Callable[[argparse.Namespace], int]) -> argparse.ArgumentParser:
        p = actions.add_parser(name, help=text, description=text, parents=[common])
        p.set_defaults(func=func)
        return p

    chat = add("chatgpt", "start a secure tunnel so ChatGPT can use ProfilePilot (Ctrl+C stops it)",
               cmd_connect_chatgpt)
    chat.add_argument("--via", choices=VIA_CHOICES, default="auto",
                      help="cloudflared (no account), ngrok, tunnel-client (OpenAI Secure MCP Tunnel) or url "
                           "(your own public URL); default: ask / the best available")
    chat.add_argument("--port", type=int, default=None, help=f"local port (default {DEFAULT_PORT} or a free one)")
    chat.add_argument("--install", action="store_true", help="install the missing tunnel program (winget / brew)")
    chat.add_argument("--public-url", default=None, metavar="URL",
                      help="with --via url: the https:// address that forwards to the local port")
    chat.add_argument("--ngrok-domain", default=None, metavar="DOMAIN", help="your fixed ngrok domain")
    chat.add_argument("-y", "--yes", action="store_true", help="no questions: use the best available option")
    chat.add_argument("--no-check", action="store_true", help="skip the internet reachability check")
    add("status", "show whether ProfilePilot is shared, the URL, the pairing code and connected apps",
        cmd_connect_status)
    stop = add("stop", "stop sharing ProfilePilot with ChatGPT", cmd_connect_stop)
    stop.add_argument("--revoke", action="store_true", help="also sign out every connected app")
    return parser


__all__ = [
    "CHATGPT_FILE", "INTRO", "ManagedProcess", "Wizard", "add_cli", "find_executable", "parse_cloudflared_url",
    "parse_ngrok_url", "read_state", "server_command", "status_info", "status_text", "stop_sharing", "tunnel_argv",
]
