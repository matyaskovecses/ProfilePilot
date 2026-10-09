"""Start, stop and discover running profiles (sync API).

A running profile is a detached **host process** (:mod:`profilepilot.browser.host`) that owns the
profile lock, the proxy relay, the control API and Chrome itself, and publishes its state in
``profiles/<id>/runtime.json``. Browsers therefore outlive the MCP server or script that started
them, and any number of clients can discover and attach to them.

A profile counts as *running* iff ``runtime.json`` exists, ``state == "running"``, the host PID is
alive (and not a reused PID), Chrome's PID is alive with the recorded ``create_time`` and
``/json/version`` answers on the CDP port. A ``runtime.json`` whose host is gone is stale and is
removed (any orphaned Chrome with the recorded identity is killed with it).

Async callers use ``anyio.to_thread.run_sync`` / ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import psutil
from filelock import FileLock, Timeout

from ..errors import (
    ConflictError,
    LaunchError,
    ProfileNotRunningError,
    ProfilePilotError,
    RestartRequiredError,
)
from ..jsonio import lock_for, read_json
from ..models import Profile, RuntimeInfo, WindowMode
from ..procs import host_alive as _host_alive
from ..procs import process_alive
from ..store import Store
from .control import ControlCallError, cdp_browser_close, cdp_version, control_call

log = logging.getLogger("profilepilot.browser.runtime")

#: Host exit codes (see :mod:`profilepilot.browser.host`).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_ALREADY_RUNNING = 3
EXIT_IN_USE = 4

HOST_LOCK_NAME = "host.lock"
HOST_STDERR_NAME = "host-stderr.log"
_STDERR_MAX = 256 * 1024
_POLL = 0.1
_REGISTER_WAIT = 15.0
"""How long ``start`` holds the cross-profile start lock waiting for the new host to register."""


# --------------------------------------------------------------------------- process helpers


def kill_tree(pid: int, *, create_time: float | None = None, timeout: float = 10.0) -> None:
    """Kill ``pid`` and all its descendants (children first). Never touches a reused PID."""
    try:
        root = psutil.Process(int(pid))
        if create_time is not None and abs(root.create_time() - create_time) > 1.0:
            return
        procs = root.children(recursive=True) + [root]
    except psutil.Error:
        return
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(procs, timeout=timeout)


def process_tree(pid: int) -> list[psutil.Process]:
    """``pid`` and its live descendants as psutil handles (empty if it is gone)."""
    try:
        root = psutil.Process(int(pid))
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return []


def _wait_pid_exit(pid: int, timeout: float) -> bool:
    """Wait for ``pid`` to exit; True if it did (or was already gone)."""
    try:
        psutil.Process(int(pid)).wait(timeout=max(0.0, timeout))
        return True
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        return False
    except psutil.Error:
        return not psutil.pid_exists(int(pid))


def host_command(profile_id: str, root: Path | str, window: WindowMode | None = None) -> tuple[list[str], dict[str, str] | None]:
    """argv (and env, if it must differ from ours) that starts a host for ``profile_id``.

    On Windows a venv's ``python.exe`` is only a redirector that runs the base interpreter as a
    child; like :mod:`multiprocessing` (bpo-35797) we start the base interpreter directly with
    ``__PYVENV_LAUNCHER__`` so the recorded host PID is the process that really holds the
    profile (and owns Chrome's job object).
    """
    exe, env = sys.executable, None
    base = getattr(sys, "_base_executable", None)
    if (
        sys.platform == "win32" and base and sys.prefix != sys.base_prefix
        and os.path.normcase(os.path.abspath(base)) != os.path.normcase(os.path.abspath(exe))
        and os.path.isfile(base)
    ):
        env = dict(os.environ)
        env["__PYVENV_LAUNCHER__"] = exe
        exe = base
    argv = [exe, "-m", "profilepilot.browser.host", profile_id, "--root", str(root)]
    if window:
        argv += ["--window", window]
    return argv, env


def _tail(path: Path, lines: int = 8, max_bytes: int = 16 * 1024) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [ln for ln in data.splitlines() if ln.strip()][-lines:]


# --------------------------------------------------------------------------- manager


class RuntimeManager:
    """Starts/stops profile hosts and answers "is this profile running?" from ``runtime.json``."""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------ discovery

    def _read(self, profile_id: str) -> RuntimeInfo | None:
        data = read_json(self.store.runtime_file(profile_id))
        if not data:
            return None
        try:
            return RuntimeInfo.model_validate(data)
        except Exception:
            log.warning("ignoring malformed runtime.json of profile %s", profile_id)
            return RuntimeInfo(profile_id=profile_id, profile_name="?", host_pid=0)

    def _is_ready(self, info: RuntimeInfo) -> bool:
        """Running state + Chrome identity + DevTools answering (host liveness checked by caller)."""
        if info.state != "running" or not info.cdp_port:
            return False
        if not process_alive(info.chrome_pid, info.chrome_create_time):
            return False
        return cdp_version(info.cdp_port, timeout=2.0) is not None

    def _clean_stale(self, profile_id: str, info: RuntimeInfo | None = None) -> None:
        """Remove a runtime.json whose host is dead (and kill an orphaned Chrome it recorded).

        Done under the profile's host lock: if a live host holds it, nothing is touched.
        """
        lock = FileLock(str(self.store.profile_dir(profile_id) / HOST_LOCK_NAME))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            return
        except OSError:
            return
        try:
            current = self._read(profile_id)
            if current is None or (info is not None and current.host_pid != info.host_pid):
                return
            if _host_alive(current):
                return
            if current.chrome_pid and current.chrome_create_time and process_alive(
                current.chrome_pid, current.chrome_create_time
            ):
                log.warning("killing orphaned browser pid %s of profile %s", current.chrome_pid, profile_id)
                kill_tree(current.chrome_pid, create_time=current.chrome_create_time)
            with contextlib.suppress(OSError):
                self.store.runtime_file(profile_id).unlink()
            log.info("removed stale runtime.json of profile %s", profile_id)
        finally:
            lock.release()

    def status(self, ref: str) -> RuntimeInfo | None:
        """Validated runtime info of a running profile, or None (stale state is cleaned up)."""
        profile = self.store.get_profile(ref)
        info = self._read(profile.id)
        if info is None:
            return None
        if not _host_alive(info):
            self._clean_stale(profile.id, info)
            return None
        return info if self._is_ready(info) else None

    def list_running(self) -> list[RuntimeInfo]:
        running: list[RuntimeInfo] = []
        for profile in self.store.list_profiles():
            if not self.store.runtime_file(profile.id).exists():
                continue
            with contextlib.suppress(ProfilePilotError):
                info = self.status(profile.id)
                if info is not None:
                    running.append(info)
        return running

    def _live_hosts(self, exclude: str | None = None) -> list[RuntimeInfo]:
        """Runtime infos whose host process is alive (running, starting or stopping)."""
        live: list[RuntimeInfo] = []
        for profile in self.store.list_profiles():
            if profile.id == exclude or not self.store.runtime_file(profile.id).exists():
                continue
            info = self._read(profile.id)
            if info is None:
                continue
            if _host_alive(info):
                live.append(info)
            else:
                self._clean_stale(profile.id, info)
        return live

    # ------------------------------------------------------------------ start

    def start(self, ref: str, *, timeout: float = 60.0, window: WindowMode | None = None) -> RuntimeInfo:
        """Start the profile's browser (idempotent: returns the current info if it is running).

        ``window`` overrides ``launch.window`` for this run only. Raises :class:`LaunchError`
        (with the host's error and the last lines of ``host.log``) or :class:`ConflictError`
        when ``config.max_running`` profiles are already running.
        """
        profile = self.store.get_profile(ref)
        if window is not None and window not in ("normal", "offscreen", "headless"):
            raise ProfilePilotError(f"Invalid window mode {window!r}; use normal, offscreen or headless.")
        deadline = time.monotonic() + timeout
        while True:
            # Never wait for another host's readiness while holding the cross-profile start lock:
            # that would block every other profile's start behind one slow launch.
            current = self._settle(profile, deadline)  # raises at the deadline
            if current is not None:
                return current
            with self._start_lock():
                info = self._read(profile.id)
                if info is not None:
                    if _host_alive(info):
                        continue  # another client's host registered meanwhile: settle outside the lock
                    self._clean_stale(profile.id, info)
                config = self.store.load_config()
                others = self._live_hosts(exclude=profile.id)
                if config.max_running > 0 and len(others) >= config.max_running:
                    names = ", ".join(sorted(i.profile_name for i in others))
                    raise ConflictError(
                        f"Cannot start '{profile.name}': {len(others)} profiles are already running "
                        f"(max_running = {config.max_running}: {names}). Stop one first or raise max_running."
                    )
                proc = self._spawn_host(profile, window)
                self._wait_registered(profile.id, proc, min(deadline, time.monotonic() + _REGISTER_WAIT))
            break
        return self._wait_running(profile, proc, deadline, timeout)

    @contextlib.contextmanager
    def _start_lock(self) -> Iterator[None]:
        """Cross-process lock serialising the max_running check and host spawning."""
        lock = lock_for(self.store.profiles_dir / "start", timeout=_REGISTER_WAIT + 15.0)
        try:
            lock.acquire()
        except Timeout as exc:
            raise LaunchError("Another client is busy starting a profile; try again in a moment.") from exc
        try:
            yield
        finally:
            lock.release()

    def _settle(self, profile: Profile, deadline: float) -> RuntimeInfo | None:
        """Wait out a host that is starting/stopping. Returns running info, or None if no live host."""
        while True:
            info = self._read(profile.id)
            if info is None:
                return None
            if not _host_alive(info):
                self._clean_stale(profile.id, info)
                return None
            if self._is_ready(info):
                return info
            if time.monotonic() > deadline:
                raise LaunchError(
                    f"Profile '{profile.name}' has a host process (pid {info.host_pid}, state {info.state}) "
                    "that did not become ready in time. Stop the profile and try again."
                )
            time.sleep(_POLL)

    def _spawn_host(self, profile: Profile, window: WindowMode | None) -> subprocess.Popen:
        argv, env = host_command(profile.id, self.store.root, window)
        profile_dir = self.store.profile_dir(profile.id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        stderr_path = profile_dir / HOST_STDERR_NAME
        with contextlib.suppress(OSError):
            if stderr_path.stat().st_size > _STDERR_MAX:
                stderr_path.unlink()
        # The host is long-lived: run it in the data root so it never pins the caller's working
        # directory (Windows cannot delete/rename a directory that is some process's cwd). A
        # relative root keeps the inherited cwd so it resolves the same way in the host.
        root = Path(self.store.root)
        common: dict[str, Any] = dict(
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, close_fds=True, env=env,
            cwd=str(root) if root.is_absolute() else None,
        )
        log.info("starting host for profile %s (%s)", profile.name, profile.id)
        with open(stderr_path, "ab") as stderr:
            if sys.platform == "win32":
                base_flags = (
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                )
                try:
                    return subprocess.Popen(
                        argv, stderr=stderr, creationflags=base_flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common
                    )
                except OSError as exc:  # our job forbids breakaway: start inside it instead
                    log.debug("breakaway from job refused (%s); starting the host inside the job", exc)
                try:
                    return subprocess.Popen(argv, stderr=stderr, creationflags=base_flags, **common)
                except OSError as exc:
                    raise LaunchError(f"Could not start the host process for '{profile.name}': {exc}") from exc
            try:
                return subprocess.Popen(argv, stderr=stderr, start_new_session=True, **common)
            except OSError as exc:
                raise LaunchError(f"Could not start the host process for '{profile.name}': {exc}") from exc

    def _wait_registered(self, profile_id: str, proc: subprocess.Popen, until: float) -> None:
        """Wait until the new host wrote runtime.json (so concurrent starts count it) or exited."""
        path = self.store.runtime_file(profile_id)
        while time.monotonic() < until and proc.poll() is None and not path.exists():
            time.sleep(_POLL / 2)

    def _wait_running(self, profile: Profile, proc: subprocess.Popen, deadline: float, timeout: float) -> RuntimeInfo:
        while True:
            code = proc.poll()
            if code is not None:
                if code == EXIT_ALREADY_RUNNING:
                    # Another client started this profile concurrently; its host owns it.
                    current = self._settle(profile, deadline)
                    if current is not None:
                        return current
                    raise LaunchError(f"Profile '{profile.name}' was being started by another client, which failed.")
                info = self._read(profile.id)
                error = info.error if info is not None and info.error else None
                self._clean_stale(profile.id, info)
                raise LaunchError(self._failure_message(profile, error or f"The host process exited with code {code}."))
            info = self._read(profile.id)
            if info is not None:
                if info.error:
                    with contextlib.suppress(Exception):
                        proc.wait(5)
                    self._clean_stale(profile.id, info)
                    raise LaunchError(self._failure_message(profile, info.error))
                if info.state == "running" and _host_alive(info) and self._is_ready(info):
                    return info
            if time.monotonic() > deadline:
                log.warning("host of profile %s did not become ready in %.0fs; killing it", profile.id, timeout)
                info = self._read(profile.id)
                if info is not None and _host_alive(info):
                    kill_tree(info.host_pid)
                kill_tree(proc.pid)
                with contextlib.suppress(Exception):
                    proc.wait(5)
                self._clean_stale(profile.id)
                raise LaunchError(self._failure_message(profile, f"The browser did not start within {timeout:.0f} s."))
            time.sleep(_POLL)

    def _failure_message(self, profile: Profile, error: str) -> str:
        message = f"Could not start profile '{profile.name}': {error}"
        tail = _tail(self.store.host_log(profile.id)) or _tail(self.store.profile_dir(profile.id) / HOST_STDERR_NAME)
        if tail:
            message += "\nLast host log lines:\n" + "\n".join(tail)
        return message

    # ------------------------------------------------------------------ stop

    def stop(self, ref: str, *, timeout: float = 20.0) -> bool:
        """Close the profile's browser gracefully. Returns False if it was not running.

        Order: control API ``/stop`` (the host closes Chrome via CDP ``Browser.close``, then
        WM_CLOSE, then a tree kill) -> CDP ``Browser.close`` directly -> kill Chrome and host.
        """
        profile = self.store.get_profile(ref)
        info = self._read(profile.id)
        if info is None:
            return False
        if not _host_alive(info):
            self._clean_stale(profile.id, info)
            return False
        deadline = time.monotonic() + max(1.0, timeout)
        try:
            control_call(info, "POST", "/stop", timeout=min(5.0, timeout))
        except ProfilePilotError as exc:
            log.warning("control /stop failed for %s: %s", profile.id, exc)
        if _wait_pid_exit(info.host_pid, deadline - time.monotonic()):
            self._clean_stale(profile.id, info)
            return True

        log.warning("host of profile %s did not stop in time; closing the browser directly", profile.id)
        if info.cdp_ws_url and process_alive(info.chrome_pid, info.chrome_create_time):
            cdp_browser_close(info.cdp_ws_url, timeout=3.0)
            if _wait_pid_exit(info.host_pid, 10.0):
                self._clean_stale(profile.id, info)
                return True
        if info.chrome_pid and info.chrome_create_time:
            kill_tree(info.chrome_pid, create_time=info.chrome_create_time)
        if _host_alive(info):
            kill_tree(info.host_pid)
        self._clean_stale(profile.id, info)
        return True

    def stop_all(self, timeout: float = 20.0) -> list[str]:
        """Stop every profile with a live host. Returns the names of the stopped profiles."""
        stopped: list[str] = []
        for info in self._live_hosts():
            try:
                if self.stop(info.profile_id, timeout=timeout):
                    stopped.append(info.profile_name)
            except ProfilePilotError as exc:
                log.warning("could not stop profile %s: %s", info.profile_id, exc)
        return stopped

    def restart(self, ref: str, **kw: Any) -> RuntimeInfo:
        """Stop (if running) and start again. Keyword arguments are passed to :meth:`start`."""
        self.stop(ref, timeout=kw.pop("stop_timeout", 20.0))
        return self.start(ref, **kw)

    # ------------------------------------------------------------------ live control

    def _require_running(self, ref: str) -> tuple[Profile, RuntimeInfo]:
        profile = self.store.get_profile(ref)
        info = self.status(profile.id)
        if info is None:
            raise ProfileNotRunningError(f"Profile '{profile.name}' is not running.")
        return profile, info

    def set_upstream(self, ref: str, proxy_id: str | None) -> None:
        """Switch the running profile's upstream proxy live (new connections only).

        ``proxy_id`` is a saved proxy reference (id/name) or None for a direct connection. Only
        the live relay is changed; persisting ``profile.proxy_id`` is the caller's job. Raises
        :class:`RestartRequiredError` if the profile was started without a relay (no proxy).
        """
        profile, info = self._require_running(ref)
        if not info.relay_port:
            raise RestartRequiredError(
                f"Profile '{profile.name}' was started without a proxy, so its browser connects directly. "
                "Restart the profile to apply a proxy."
            )
        target = self.store.get_proxy(proxy_id).id if proxy_id else None
        try:
            control_call(info, "POST", "/upstream", {"proxy_id": target}, timeout=15.0)
        except ControlCallError as exc:
            if exc.status == 409:
                raise RestartRequiredError(str(exc)) from exc
            raise

    def relay_stats(self, ref: str) -> dict | None:
        """Relay counters (plus the redacted ``upstream``) of a running profile.

        None if the profile is not running or was started without a relay (no proxy).
        """
        profile = self.store.get_profile(ref)
        info = self.status(profile.id)
        if info is None or not info.relay_port:
            return None
        data = control_call(info, "GET", "/status")
        relay = data.get("relay")
        if not isinstance(relay, dict):
            return None
        return {**relay, "upstream": data.get("upstream"), "proxy_id": data.get("proxy_id")}
