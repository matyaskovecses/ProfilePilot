"""Profile host: the long-lived process that *is* a running profile.

Started detached by :class:`profilepilot.browser.runtime.RuntimeManager` as::

    python -m profilepilot.browser.host <profile_id> [--root PATH] [--window normal|offscreen|headless]
                                       [--start-url=URL]

It holds the profile lock, runs the credential-free proxy relay and the token-protected control
API, launches the user's real Chrome with a fixed DevTools port, publishes ``runtime.json`` and
exits when Chrome exits (window closed by the user, ``Browser.close``, or a stop request). On
Windows it first puts itself into a kill-on-close job object so Chrome can never outlive it.

This module must stay free of Playwright and MCP imports (it is a separate, lean process), and
must never print: its stdout is not a terminal and logging goes to ``profiles/<id>/host.log``.

Exit codes: 0 ok, 1 launch failure, 2 usage / unknown profile, 3 already running, 4 the profile's
data directory is in use by another browser process, 5 started inside a client's kill-on-close job
while the spawner asked to be told (``PROFILEPILOT_HOST_LEAVE_CLIENT_JOB=1``): it then starts the host
again outside that job.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psutil
from filelock import FileLock, Timeout

from ..errors import LaunchError, NotFoundError, ProfilePilotError
from ..jsonio import read_json, write_json
from ..models import LaunchOptions, Profile, RuntimeInfo, WindowMode
from ..paths import BrowserInfo, find_browser
from ..proxy.relay import LocalRelay
from ..proxy.url import ProxyEndpoint, ProxyParseError, parse_proxy
from ..store import Store
from .control import ControlError, ControlServer, async_cdp_browser_close, async_cdp_version
from .flags import build_chrome_args
from .prefs import (
    MAX_USER_DATA_DIR_CHARS,
    has_saved_session,
    long_path_hint,
    prepare_user_data_dir,
    profile_in_use,
    user_data_dir_too_long,
)
from .runtime import (
    EXIT_ALREADY_RUNNING,
    EXIT_FAILED,
    EXIT_IN_CLIENT_JOB,
    EXIT_IN_USE,
    EXIT_OK,
    EXIT_USAGE,
    HOST_LOCK_NAME,
    LAST_EXIT_NAME,
    LEAVE_CLIENT_JOB_ENV,
    crash_description,
    kill_tree,
    process_tree,
    read_last_exit,
    write_last_exit,
)
from .winjob import in_foreign_kill_on_close_job, process_in_job, put_self_in_kill_on_close_job

log = logging.getLogger("profilepilot.browser.host")

DEVTOOLS_TIMEOUT = 45.0
"""Seconds to wait for Chrome's /json/version after spawning it."""
HANDOFF_WINDOW = 5.0
"""Chrome exiting this quickly without a DevTools listener means it handed off to another instance."""
CLOSE_TIMEOUT = 10.0
WM_CLOSE_TIMEOUT = 5.0
LOCK_WAIT = 2.0
"""Short wait for host.lock: a stale-state cleanup in a client may hold it for a moment."""
LOG_MAX_BYTES = 1_000_000
_WINDOWS = sys.platform == "win32"
_CREATE_NO_WINDOW = 0x08000000


class HostError(Exception):
    """A launch failure with the exit code to use; the message is shown to users (no secrets)."""

    def __init__(self, message: str, code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- helpers


def free_port(host: str = "127.0.0.1", avoid: set[int] | None = None) -> int:
    """A currently free TCP port on ``host`` (bind to port 0, read it, close)."""
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            port = sock.getsockname()[1]
        if not avoid or port not in avoid:
            return port
    raise HostError("Could not allocate a free local port.")


def _listener_pids(port: int, pids: set[int]) -> set[int]:
    """Which of ``pids`` LISTEN on TCP ``port``."""
    found: set[int] = set()
    for pid in pids:
        try:
            conns = psutil.Process(pid).net_connections(kind="tcp")
        except psutil.Error:
            continue
        if any(c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port for c in conns):
            found.add(pid)
    return found


def _port_owner(port: int) -> int | None:
    """PID of whatever process listens on 127.0.0.1:``port`` (best effort)."""
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                return conn.pid
    except (psutil.Error, OSError):
        pass
    return None


async def wait_for_devtools(
    proc: subprocess.Popen, port: int, *, timeout: float = DEVTOOLS_TIMEOUT, handoff_window: float = HANDOFF_WINDOW
) -> dict[str, Any]:
    """Wait until Chrome (``proc``) serves ``/json/version`` on ``port`` and owns the listener.

    Returns the ``/json/version`` payload. Raises :class:`LaunchError` when Chrome exits first
    (quickly with code 0 = singleton hand-off to a browser already running on the same
    user-data-dir), when another process owns the port, or on timeout.
    """
    spawned = time.monotonic()
    deadline = spawned + timeout
    while True:
        code = proc.poll()
        if code is not None:
            elapsed = time.monotonic() - spawned
            if code == 0 and elapsed <= handoff_window:
                raise LaunchError(
                    "Chrome handed the launch over to a browser that is already running on this profile's "
                    "data directory and exited. Close any window still using this profile and try again."
                )
            raise LaunchError(f"The browser exited during startup (exit code {code}, after {elapsed:.1f} s).")
        version = await async_cdp_version(port, timeout=1.0)
        if version is not None:
            tree = {p.pid for p in process_tree(proc.pid)}
            if await asyncio.to_thread(_listener_pids, port, tree):
                return version
            owner = await asyncio.to_thread(_port_owner, port)
            if owner is not None and owner not in tree:
                raise LaunchError(
                    f"DevTools port {port} is served by another process (pid {owner}), not by the browser we started."
                )
            # Listener not visible yet (racy table snapshot): poll again.
        if time.monotonic() > deadline:
            raise LaunchError(f"The browser did not open its DevTools endpoint on port {port} within {timeout:.0f} s.")
        await asyncio.sleep(0.1)


def launch_start_urls(launch: LaunchOptions, start_url: str | None, *, session_exists: bool) -> list[str]:
    """The URLs to put on Chrome's command line.

    A client's ``--start-url`` (its first destination) is always opened: next to a restored session it
    becomes a new, active tab. The profile's own ``launch.start_url`` is only opened when no saved
    session is restored (else a new tab would pile up on every restart)."""
    if start_url:
        return [start_url]
    return [] if session_exists else ([launch.start_url] if launch.start_url else [])


def launch_after(launch: LaunchOptions, last_exit: dict[str, Any] | None) -> LaunchOptions:
    """The launch options of this run: after a *crash* of the previous run (``last_exit.json``) the
    session is not restored (``--restore-last-session`` would reopen the tab that crashed the browser
    and crash it again, in a loop; Chrome itself does not restore by itself after a crash). The
    profile's own setting is unchanged and applies again from the next start."""
    if launch.restore_session and last_exit and last_exit.get("crashed"):
        return launch.model_copy(update={"restore_session": False})
    return launch


def redact_launch_args(args: list[str]) -> list[str]:
    """Chrome's argv for the log, with every URL cut down to its origin (start URLs can carry paths,
    queries and tokens; the log may be shared)."""
    out: list[str] = []
    for arg in args:
        if arg.startswith("-") or "://" not in arg:
            out.append(arg)
            continue
        try:
            parts = urlsplit(arg)
            host = parts.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            out.append("<url>")
            continue
        out.append(f"{parts.scheme}://{host}{port}/…" if (parts.path not in ("", "/") or parts.query
                                                          or parts.fragment) else f"{parts.scheme}://{host}{port}/")
    return out


def _quiet_connection_resets(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Peers (Chrome, proxies) resetting relay sockets are routine: log them at DEBUG, not as
    ERROR tracebacks from the proactor's ``_call_connection_lost``."""
    if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
        log.debug("connection reset by peer: %s", context.get("message"))
        return
    loop.default_exception_handler(context)


def _setup_logging(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s"))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return handler


# --------------------------------------------------------------------------- host


class ProfileHost:
    """Runs one profile: relay + control API + Chrome, until Chrome exits or a stop is requested."""

    def __init__(self, store: Store, profile: Profile, window: WindowMode | None = None, *,
                 client_job: bool = False, start_url: str | None = None) -> None:
        self.store = store
        self.profile = profile
        self.client_job = client_job
        self.start_url = start_url
        self.launch = profile.launch.model_copy(deep=True)
        if window:
            self.launch.window = window
        self.udd = store.user_data_dir(profile.id)
        self.runtime_file = store.runtime_file(profile.id)
        self.info: RuntimeInfo | None = None
        self.relay: LocalRelay | None = None
        self.control: ControlServer | None = None
        self.proc: subprocess.Popen | None = None
        self.browser: BrowserInfo | None = None
        self._stop = asyncio.Event()
        self._tree: list[psutil.Process] = []

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> int:
        started = time.monotonic()
        self._install_signal_handlers()
        asyncio.get_running_loop().set_exception_handler(_quiet_connection_resets)
        if self.client_job:
            log.warning("running inside the kill-on-close job of the client that started this host: the browser "
                        "will close when that client disconnects")
        if put_self_in_kill_on_close_job() is None and _WINDOWS:
            log.warning("running without a kill-on-close job: Chrome may outlive a crashed host")
        try:
            await self._launch()
        except HostError as exc:
            log.error("launch failed: %s", exc)
            await self._abort(str(exc))
            return exc.code
        except LaunchError as exc:
            log.error("launch failed: %s", exc)
            await self._abort(str(exc))
            return EXIT_FAILED
        except Exception as exc:  # unexpected: keep details in the log only
            log.exception("unexpected launch failure")
            await self._abort(f"Unexpected error in the profile host ({type(exc).__name__}); see host.log.")
            return EXIT_FAILED

        try:
            await self._supervise()
        finally:
            await self._shutdown()
            with contextlib.suppress(Exception):
                self.store.add_runtime(self.profile.id, time.monotonic() - started)
        log.info("host exiting")
        return EXIT_OK

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def request_stop(*_args: Any) -> None:
            loop.call_soon_threadsafe(self._stop.set)

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError, RuntimeError):
                    signal.signal(sig, request_stop)

    async def _launch(self) -> None:
        profile, launch = self.profile, self.launch
        self.udd.mkdir(parents=True, exist_ok=True)
        config = self.store.load_config()
        try:
            self.browser = find_browser(profile.browser, config.browser_path)
        except ProfilePilotError as exc:
            raise HostError(str(exc)) from exc
        if profile_in_use(self.udd):
            raise HostError(
                f"Profile '{profile.name}' is already open in another browser process (its data directory "
                f"{self.udd} is locked). Close that browser window and try again.",
                EXIT_IN_USE,
            )

        endpoint: ProxyEndpoint | None = None
        if profile.proxy_id:
            try:
                endpoint = self.store.profile_proxy_endpoint(profile)
            except NotFoundError as exc:
                raise HostError(
                    f"The proxy of profile '{profile.name}' no longer exists ({exc}). Set another proxy or none."
                ) from exc
            if endpoint.username and endpoint.password is None:
                log.warning("proxy %s has a username but no stored password", endpoint.redacted())
            self.relay = LocalRelay(endpoint)
            await self.relay.start(0)

        token = secrets.token_urlsafe(32)
        self.control = ControlServer(token, {
            ("GET", "/status"): self._route_status,
            ("POST", "/stop"): self._route_stop,
            ("POST", "/upstream"): self._route_upstream,
            ("POST", "/open"): self._route_open,
        })
        await self.control.start(0)

        avoid = {self.control.port} | ({self.relay.port} if self.relay else set())
        cdp_port = free_port(avoid=avoid)
        self.info = RuntimeInfo(
            profile_id=profile.id,
            profile_name=profile.name,
            state="starting",
            host_pid=os.getpid(),
            browser_path=self.browser.path,
            browser_version=self.browser.version,
            browser_kind=self.browser.kind,
            cdp_port=cdp_port,
            relay_port=self.relay.port if self.relay else None,
            proxy_id=profile.proxy_id,
            upstream=endpoint.redacted() if endpoint else None,
            control_port=self.control.port,
            control_token=token,
            window=launch.window,
            client_job=True if self.client_job else None,
            start_url=self.start_url,
        )
        self._write_info()

        last_exit = read_last_exit(self.store, profile.id)
        launch = launch_after(launch, last_exit)
        if launch.restore_session != self.launch.restore_session:
            log.warning("the previous run crashed (%s): its tabs are not restored this time, like Chrome after a "
                        "crash, so the page that crashed it does not reopen by itself", (last_exit or {}).get("crash"))
        prepare_user_data_dir(self.udd, launch, proxied=self.relay is not None)  # Secure DNS off when proxied
        if user_data_dir_too_long(self.udd):
            log.warning("the data directory path is %d characters long (more than %d): Chrome crashes on some pages "
                        "with it (see browser/prefs.py); use a shorter PROFILEPILOT_HOME",
                        len(str(self.udd.absolute())), MAX_USER_DATA_DIR_CHARS)
        session = launch.restore_session and has_saved_session(self.udd)
        start_urls = launch_start_urls(launch, self.start_url, session_exists=session)
        try:
            args = build_chrome_args(
                browser=self.browser, user_data_dir=self.udd, cdp_port=cdp_port, launch=launch,
                relay_port=self.relay.port if self.relay else None, start_urls=start_urls, session_exists=session,
            )
        except ProfilePilotError as exc:
            raise HostError(str(exc)) from exc
        log.info("launching %s %s", self.browser.path, " ".join(redact_launch_args(args)))
        try:
            self.proc = subprocess.Popen(
                [self.browser.path, *args],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW if _WINDOWS else 0, close_fds=True,
            )
        except OSError as exc:
            raise HostError(f"Could not start the browser {self.browser.path}: {exc}") from exc

        version = await wait_for_devtools(self.proc, cdp_port)
        chrome = psutil.Process(self.proc.pid)
        self._tree = process_tree(self.proc.pid)
        product = str(version.get("Browser", ""))
        self.info.state = "running"
        self.info.chrome_pid = self.proc.pid
        self.info.chrome_create_time = chrome.create_time()
        self.info.cdp_http_url = f"http://127.0.0.1:{cdp_port}"
        self.info.cdp_ws_url = version.get("webSocketDebuggerUrl")
        if "/" in product:
            self.info.browser_version = product.split("/", 1)[1]
        self._write_info()
        with contextlib.suppress(Exception):
            self.store.touch_started(profile.id)
        log.info(
            "browser running: pid=%s cdp=%s relay=%s upstream=%s window=%s in_job=%s",
            self.proc.pid, cdp_port, self.info.relay_port, self.info.upstream or "direct",
            launch.window, process_in_job(self.proc.pid) if _WINDOWS else "n/a",
        )

    async def _supervise(self) -> None:
        """Wait for Chrome to exit or for a stop request (then close Chrome gracefully)."""
        assert self.proc is not None
        last_snapshot = time.monotonic()
        while self.proc.poll() is None:
            try:
                await asyncio.wait_for(self._stop.wait(), 0.25)
            except asyncio.TimeoutError:
                if time.monotonic() - last_snapshot > 5.0:
                    # Remember Chrome's children: once the browser process exits they can no
                    # longer be found through it, and any straggler must be reaped.
                    self._tree = process_tree(self.proc.pid) or self._tree
                    last_snapshot = time.monotonic()
                continue
            log.info("stop requested")
            await self._close_browser()
            break
        self._record_exit(self.proc.poll(), requested=self._stop.is_set())

    def _record_exit(self, code: int | None, *, requested: bool) -> None:
        """Log how Chrome exited and write ``last_exit.json``, so clients can tell a crash (for example
        0xC0000005, which some pages cause: see browser/prefs.py) from a normal close."""
        crash = None if requested else crash_description(code)
        if crash:
            hint = long_path_hint(self.udd)
            log.error("browser exited (code %s): it crashed (%s)%s", code, crash, f". {hint}" if hint else "")
        else:
            log.info("browser exited (code %s)", code)
        try:
            write_last_exit(self.store, self.profile.id, code=code, requested=requested,
                            chrome_pid=self.proc.pid if self.proc else None,
                            chrome_create_time=self.info.chrome_create_time if self.info else None)
        except Exception as exc:  # never let the record keep the host from cleaning up
            log.warning("could not write %s: %s", LAST_EXIT_NAME, exc)

    async def _close_browser(self) -> None:
        """Browser.close via CDP -> WM_CLOSE (taskkill without /F) -> kill the process tree."""
        assert self.proc is not None and self.info is not None
        self.info.state = "stopping"
        self._write_info()
        self._tree = process_tree(self.proc.pid) or self._tree
        if self.info.cdp_ws_url and await async_cdp_browser_close(self.info.cdp_ws_url, timeout=5.0):
            if await self._wait_exit(CLOSE_TIMEOUT):
                return
            log.warning("browser did not exit within %.0f s after Browser.close", CLOSE_TIMEOUT)
        if _WINDOWS and self.proc.poll() is None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    subprocess.run, ["taskkill", "/PID", str(self.proc.pid)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=_CREATE_NO_WINDOW, timeout=10,
                )
            if await self._wait_exit(WM_CLOSE_TIMEOUT):
                return
        if self.proc.poll() is None:
            log.warning("killing the browser process tree")
            await asyncio.to_thread(kill_tree, self.proc.pid)

    async def _wait_exit(self, timeout: float) -> bool:
        assert self.proc is not None
        deadline = time.monotonic() + timeout
        while self.proc.poll() is None:
            if time.monotonic() > deadline:
                return False
            await asyncio.sleep(0.1)
        return True

    async def _reap_tree(self) -> None:
        """Give Chrome's helper processes a moment to exit, then kill stragglers (PID-reuse safe)."""
        procs = [p for p in self._tree if self.proc is None or p.pid != self.proc.pid]
        if not procs:
            return
        _gone, alive = await asyncio.to_thread(psutil.wait_procs, procs, 3.0)
        for proc in alive:
            with contextlib.suppress(psutil.Error):
                proc.kill()

    async def _shutdown(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            await asyncio.to_thread(kill_tree, self.proc.pid)
        await self._reap_tree()
        if self.control is not None:
            await self.control.stop()
        if self.relay is not None:
            await self.relay.stop()
        self._remove_info()

    async def _abort(self, error: str) -> None:
        """Launch failed: kill what we started and leave the error in runtime.json for the client."""
        if self.proc is not None and self.proc.poll() is None:
            await asyncio.to_thread(kill_tree, self.proc.pid)
        if self.control is not None:
            await self.control.stop()
        if self.relay is not None:
            await self.relay.stop()
        info = self.info or RuntimeInfo(profile_id=self.profile.id, profile_name=self.profile.name, host_pid=os.getpid())
        info.state = "starting"
        info.error = error
        info.control_port = None
        info.control_token = None
        self.info = info
        self._write_info()

    # ------------------------------------------------------------------ runtime.json

    def _write_info(self) -> None:
        assert self.info is not None
        write_json(self.runtime_file, self.info.model_dump(mode="json"))

    def _remove_info(self) -> None:
        data = read_json(self.runtime_file)
        if data and data.get("host_pid") not in (None, os.getpid()):
            return  # not ours (should not happen while we hold the lock)
        with contextlib.suppress(OSError):
            self.runtime_file.unlink()

    # ------------------------------------------------------------------ control routes

    async def _route_status(self, _body: Any) -> dict[str, Any]:
        info = self.info
        return {
            "state": info.state if info else "starting",
            "relay": self.relay.stats.as_dict() if self.relay else None,
            "upstream": info.upstream if info else None,
            "proxy_id": info.proxy_id if info else None,
            "chrome_pid": self.proc.pid if self.proc else None,
        }

    async def _route_open(self, body: Any) -> dict[str, Any]:
        """Hand an http(s) URL to the running Chrome through its command line
        (``chrome.exe --user-data-dir=<udd> --profile-directory=Default <url>``): it opens in a new,
        active tab like a link opened from another app (no CDP navigation; FIX-PLAN step 8). The
        short-lived chrome.exe only passes the URL on and exits 0. Should the browser have died
        meanwhile, that process would become a new browser: it gets the relay's proxy switches (it
        could never connect directly) and is killed."""
        url = body.get("url") if isinstance(body, dict) else None
        if (not isinstance(url, str) or not url.lower().startswith(("http://", "https://"))
                or any(ch in url for ch in "\x00\r\n")):
            raise ControlError(400, 'Body must be {"url": "http(s)://..."}.')
        if self.proc is None or self.proc.poll() is not None or self.browser is None or self.info is None \
                or self.info.state != "running":
            raise ControlError(409, "The browser is not running.")
        if self.launch.window == "headless":
            raise ControlError(409, "A headless browser takes no URLs from its command line.")
        argv = [self.browser.path, f"--user-data-dir={self.udd.resolve()}", "--profile-directory=Default"]
        if self.relay is not None:
            argv += [f"--proxy-server=socks5://127.0.0.1:{self.relay.port}",
                     "--webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"]
        argv.append(url)
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW if _WINDOWS else 0,
                                    close_fds=True)
        except OSError as exc:
            raise ControlError(500, f"Could not run the browser: {exc}") from exc
        try:
            code = await asyncio.to_thread(proc.wait, HANDOFF_WINDOW)
        except subprocess.TimeoutExpired:
            await asyncio.to_thread(kill_tree, proc.pid)
            log.warning("the URL hand-off did not reach the running browser; the extra process was killed")
            raise ControlError(409, "The running browser did not take the URL over.") from None
        if code != 0:
            raise ControlError(502, f"The browser exited with code {code} instead of passing the URL on.")
        log.info("handed %s to the running browser", redact_launch_args([url])[0])
        return {"opened": True}

    async def _route_stop(self, _body: Any) -> dict[str, Any]:
        self._stop.set()
        return {}

    async def _route_upstream(self, body: Any) -> dict[str, Any]:
        if self.relay is None or self.info is None:
            raise ControlError(409, "This profile was started without a proxy relay; restart it to apply a proxy.")
        if not isinstance(body, dict) or not ({"proxy_id", "url"} & body.keys()):
            raise ControlError(400, 'Body must be {"proxy_id": id|null} or {"url": proxy-url|null}.')
        proxy_id: str | None = None
        endpoint: ProxyEndpoint | None = None
        if "proxy_id" in body:
            ref = body.get("proxy_id")
            if ref is not None and not isinstance(ref, str):
                raise ControlError(400, "proxy_id must be a string or null.")
            if ref:
                try:
                    record = await asyncio.to_thread(self.store.get_proxy, ref)
                    endpoint = await asyncio.to_thread(self.store.proxy_endpoint, record.id)
                except ProfilePilotError as exc:
                    raise ControlError(404, str(exc)) from exc
                proxy_id = record.id
        else:
            url = body.get("url")
            if url is not None and not isinstance(url, str):
                raise ControlError(400, "url must be a string or null.")
            if url:
                try:
                    endpoint = parse_proxy(url)
                except (ProxyParseError, ValueError):
                    # The parser's message may echo the URL (credentials): never return it.
                    raise ControlError(400, "Invalid proxy URL.") from None
        self.relay.upstream = endpoint
        self.relay.stats.last_error = None  # never blame the new upstream for the old one's failure
        self.info.proxy_id = proxy_id
        self.info.upstream = endpoint.redacted() if endpoint else None
        self._write_info()
        log.info("upstream switched to %s", self.info.upstream or "direct")
        return {"upstream": self.info.upstream, "proxy_id": proxy_id}


# --------------------------------------------------------------------------- entry points


def run_host(profile_id: str, root: Path | None = None, *, window: WindowMode | None = None,
             client_job: bool = False, start_url: str | None = None) -> int:
    """Run the host for ``profile_id`` until its browser exits. Returns the process exit code."""
    try:
        store = Store(root)
        profile = store.get_profile(profile_id)
    except ProfilePilotError as exc:
        log.error("cannot start host: %s", exc)
        return EXIT_USAGE

    lock = FileLock(str(store.profile_dir(profile.id) / HOST_LOCK_NAME))
    try:
        lock.acquire(timeout=LOCK_WAIT)
    except Timeout:
        log.warning("profile %s already has a host process", profile.id)
        return EXIT_ALREADY_RUNNING
    try:
        handler = _setup_logging(store.host_log(profile.id))
        log.info("host starting for profile %s (%s), pid %s, window=%s", profile.name, profile.id, os.getpid(),
                 window or profile.launch.window)
        try:
            return asyncio.run(ProfileHost(store, profile, window, client_job=client_job, start_url=start_url).run())
        finally:
            handler.flush()
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m profilepilot.browser.host <profile_id> [--root PATH] [--window MODE]
    [--start-url=URL]``."""
    parser = argparse.ArgumentParser(prog="profilepilot host", description="Run one ProfilePilot profile host.")
    parser.add_argument("profile_id")
    parser.add_argument("--root", type=Path, default=None, help="ProfilePilot data root")
    parser.add_argument("--window", choices=["normal", "offscreen", "headless"], default=None)
    parser.add_argument("--start-url", default=None,
                        help="open this URL at launch (also next to a restored session), instead of launch.start_url")
    parser.add_argument("--stderr", type=Path, default=None, help="append stderr to this file (no inherited handle)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else EXIT_USAGE
    if args.stderr is not None:
        _redirect_stderr(args.stderr)
    # Before anything else (lock, job object, Chrome): a host inside a client's kill-on-close job
    # dies when that client disconnects. If the spawner can start us elsewhere, let it.
    client_job = in_foreign_kill_on_close_job()
    if client_job and os.environ.get(LEAVE_CLIENT_JOB_ENV) == "1":
        return EXIT_IN_CLIENT_JOB
    return run_host(args.profile_id, args.root, window=args.window, client_job=client_job, start_url=args.start_url)


def _redirect_stderr(path: Path) -> None:
    """A host started through WMI has no inherited stderr: write it (Python tracebacks, fatal
    errors) to ``path`` instead."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 - process lifetime
    except OSError:
        return
    with contextlib.suppress(OSError, ValueError):
        os.dup2(stream.fileno(), 2)
    sys.stderr = stream


if __name__ == "__main__":
    sys.exit(main())
