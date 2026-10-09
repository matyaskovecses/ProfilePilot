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
from typing import Any, Callable, Iterator

import psutil
from filelock import FileLock, Timeout

from ..errors import (
    ConflictError,
    LaunchError,
    ProfileNotRunningError,
    ProfilePilotError,
    RestartRequiredError,
)
from ..jsonio import lock_for, read_json, write_json
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
EXIT_IN_CLIENT_JOB = 5
"""The host found itself in a client's kill-on-close job and was asked to say so (see below)."""
LEAVE_CLIENT_JOB_ENV = "PROFILEPILOT_HOST_LEAVE_CLIENT_JOB"
"""Set to "1" for a host spawned with ``subprocess`` *only when the user opted in*
(``AppConfig.escape_client_job`` or :data:`ESCAPE_CLIENT_JOB_ENV`): if it still landed in a
kill-on-close job (the official Python MCP SDK's stdio client puts servers in one without allowing
breakaway, and nested jobs can swallow ``CREATE_BREAKAWAY_FROM_JOB`` silently), it exits with
:data:`EXIT_IN_CLIENT_JOB` before doing anything, and the manager starts it again through WMI, outside
every job. By default the host respects the client's job: it records ``client_job`` and the tools tell
the user that the browser closes with the client."""
ESCAPE_CLIENT_JOB_ENV = "PROFILEPILOT_ESCAPE_CLIENT_JOB"

HOST_LOCK_NAME = "host.lock"
HOST_STDERR_NAME = "host-stderr.log"
LAST_EXIT_NAME = "last_exit.json"
"""``profiles/<id>/last_exit.json``: how the profile's browser last exited (written by the host, see
:func:`write_last_exit`). Clients read it to tell a crash from a normal close."""
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


def host_command(profile_id: str, root: Path | str, window: WindowMode | None = None,
                 start_url: str | None = None) -> tuple[list[str], dict[str, str] | None]:
    """argv (and env, if it must differ from ours) that starts a host for ``profile_id``.

    ``start_url`` is passed as ``--start-url=<url>`` (one argument, so a URL can never be read as an
    option); the host opens it at launch.

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
    if start_url:
        argv.append(f"--start-url={start_url}")
    return argv, env


#: Names of the Windows NTSTATUS codes a crashing browser process exits with.
_NTSTATUS_NAMES = {
    0xC0000005: "access violation",
    0xC00000FD: "stack overflow",
    0xC0000374: "heap corruption",
    0xC0000409: "fail-fast / stack buffer overrun",
    0xC000001D: "illegal instruction",
}
_CRASH_SIGNALS = {"SIGSEGV", "SIGBUS", "SIGILL", "SIGABRT", "SIGFPE"}


def crash_description(code: int | None) -> str | None:
    """A readable description if ``code`` (a browser process exit code) means it *crashed*, else None.

    Windows: an NTSTATUS error code (0xC0000000 and up, also read as a signed 32-bit number), e.g.
    ``access violation (0xC0000005)``. POSIX: death by SIGSEGV, SIGBUS, SIGILL, SIGABRT or SIGFPE (a
    small negative ``Popen`` code)."""
    if code is None:
        return None
    if -256 < code < 0:
        import signal

        try:
            name = signal.Signals(-code).name
        except ValueError:
            return None
        return name if name in _CRASH_SIGNALS else None
    value = code & 0xFFFFFFFF
    if value >= 0xC0000000:
        return f"{_NTSTATUS_NAMES.get(value, 'crash')} (0x{value:08X})"
    return None


def write_last_exit(store: Store, profile_id: str, *, code: int | None, requested: bool,
                    chrome_pid: int | None, chrome_create_time: float | None) -> dict[str, Any]:
    """Record how the profile's browser exited in ``last_exit.json`` (the host calls this when Chrome
    exits). ``requested``: a stop was asked for (profile_stop), so the exit is no crash."""
    crash = None if requested else crash_description(code)
    record = {
        "profile_id": profile_id, "code": code, "crashed": crash is not None, "crash": crash,
        "requested": requested, "chrome_pid": chrome_pid, "chrome_create_time": chrome_create_time,
        "at": time.time(),
    }
    folder = store.profile_dir(profile_id)
    if folder.is_dir():  # never resurrect the folder of a profile deleted meanwhile
        write_json(folder / LAST_EXIT_NAME, record)
    return record


def read_last_exit(store: Store, profile_id: str) -> dict[str, Any] | None:
    """The ``last_exit.json`` record of the profile (None if there is none or it is unreadable)."""
    data = read_json(store.profile_dir(profile_id) / LAST_EXIT_NAME)
    return data if isinstance(data, dict) else None


def exit_of(record: dict[str, Any] | None, info: RuntimeInfo | None) -> bool:
    """Does the ``last_exit.json`` ``record`` describe the browser of ``info`` (same pid and start)?"""
    if not record or info is None or not info.chrome_pid or record.get("chrome_pid") != info.chrome_pid:
        return False
    recorded, started = record.get("chrome_create_time"), info.chrome_create_time
    return recorded is None or started is None or abs(float(recorded) - float(started)) <= 1.0


def first_error_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:200] if text else type(exc).__name__


_WMI_DETACHED_PROCESS = 0x8
_WMI_CREATE_NEW_PROCESS_GROUP = 0x200
_SW_HIDE = 0


def spawn_outside_job(argv: list[str], env: dict[str, str], cwd: str | None) -> int:
    """Windows: start ``argv`` through WMI ``Win32_Process.Create``. The process is created by the
    WMI provider host, so it is in none of our jobs (verified: no job, same interactive session, parent
    WmiPrvSE.exe), unlike anything ``CreateProcess`` starts from inside a kill-on-close job that does
    not allow breakaway. Returns the new PID. Raises on any failure (no WMI, refused, ...)."""
    if sys.platform != "win32":
        raise ProfilePilotError("WMI process creation is Windows-only.")
    import pythoncom  # type: ignore[import-not-found]
    import win32com.client  # type: ignore[import-not-found]

    pythoncom.CoInitialize()  # this runs in worker threads
    try:
        wmi = win32com.client.GetObject("winmgmts:")
        startup = wmi.Get("Win32_ProcessStartup").SpawnInstance_()
        startup.ShowWindow = _SW_HIDE
        startup.CreateFlags = _WMI_DETACHED_PROCESS | _WMI_CREATE_NEW_PROCESS_GROUP
        startup.EnvironmentVariables = [f"{k}={v}" for k, v in env.items() if k and not k.startswith("=")]
        process = wmi.Get("Win32_Process")
        params = process.Methods_("Create").InParameters.SpawnInstance_()
        params.CommandLine = subprocess.list2cmdline(argv)
        if cwd:
            params.CurrentDirectory = cwd
        params.ProcessStartupInformation = startup
        result = process.ExecMethod_("Create", params)
        if int(result.ReturnValue) != 0:
            raise LaunchError(f"Win32_Process.Create failed with code {int(result.ReturnValue)}.")
        return int(result.ProcessId)
    finally:
        pythoncom.CoUninitialize()


class _HostProcess:
    """``Popen``-like ``pid`` / ``poll()`` / ``wait()`` for a host we did not start ourselves (WMI).
    A process handle is opened right away, so the exit code stays readable after the process exits."""

    _SYNCHRONIZE = 0x00100000
    _QUERY_LIMITED = 0x1000

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._handle: Any = None
        try:
            import win32api  # type: ignore[import-not-found]

            self._handle = win32api.OpenProcess(self._SYNCHRONIZE | self._QUERY_LIMITED, False, pid)
        except Exception:  # already gone (or no pywin32): fall back to psutil
            if not psutil.pid_exists(pid):
                self.returncode = EXIT_FAILED

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if self._handle is None:
            if not psutil.pid_exists(self.pid):
                self.returncode = EXIT_FAILED
            return self.returncode
        import win32event  # type: ignore[import-not-found]
        import win32process  # type: ignore[import-not-found]

        if win32event.WaitForSingleObject(self._handle, 0) == win32event.WAIT_OBJECT_0:
            self.returncode = int(win32process.GetExitCodeProcess(self._handle))
            self._close()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("profilepilot host", timeout or 0)
            time.sleep(_POLL / 2)
        assert self.returncode is not None
        return self.returncode

    def _close(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._handle.Close()
            self._handle = None

    def __del__(self) -> None:
        self._close()


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

    def start(self, ref: str, *, timeout: float = 60.0, window: WindowMode | None = None,
              start_url: str | None = None) -> RuntimeInfo:
        """Start the profile's browser (idempotent: returns the current info if it is running).

        ``window`` overrides ``launch.window`` for this run only. ``start_url`` (http/https) is
        opened by Chrome itself at launch, from its command line like a link from another app,
        instead of the profile's ``launch.start_url`` and also when a saved session is restored (in a
        new, active tab). Its first request then looks like a typed URL, which a CDP navigation does
        not quite (docs/FINGERPRINT-AUDIT.md F4/F5). The returned info's ``start_url`` tells whether
        the browser was launched with it (an already running profile is returned unchanged). Raises
        :class:`LaunchError` (with the host's error and the last lines of ``host.log``) or
        :class:`ConflictError` when ``config.max_running`` profiles are already running.
        """
        profile = self.store.get_profile(ref)
        if window is not None and window not in ("normal", "offscreen", "headless"):
            raise ProfilePilotError(f"Invalid window mode {window!r}; use normal, offscreen or headless.")
        if start_url is not None and (not str(start_url).lower().startswith(("http://", "https://"))
                                      or any(ch in start_url for ch in "\x00\r\n")):
            raise ProfilePilotError("A start URL must be an http:// or https:// address.")
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
                escape = config.escape_client_job or os.environ.get(ESCAPE_CLIENT_JOB_ENV) == "1"
                proc = self._spawn_host(profile, window, leave_client_job=escape, start_url=start_url)
                self._wait_registered(profile.id, proc, min(deadline, time.monotonic() + _REGISTER_WAIT))
                if proc.poll() == EXIT_IN_CLIENT_JOB:
                    proc = self._respawn_outside_job(profile, window, start_url=start_url)
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

    def _respawn_outside_job(self, profile: Profile, window: WindowMode | None, *,
                             start_url: str | None = None) -> "subprocess.Popen | _HostProcess":
        """The first host landed in the client's kill-on-close job: start it through WMI (its parent is
        then the WMI provider host, in no job). If that fails, start it inside the job after all; it
        then records ``client_job`` and the tools tell the user how to keep the browser running."""
        argv, env = host_command(profile.id, self.store.root, window, start_url)
        stderr_path = self.store.profile_dir(profile.id) / HOST_STDERR_NAME
        root = Path(self.store.root)
        child_env = dict(env if env is not None else os.environ)
        child_env.pop(LEAVE_CLIENT_JOB_ENV, None)
        try:
            pid = spawn_outside_job([*argv, "--stderr", str(stderr_path)], child_env,
                                    str(root) if root.is_absolute() else None)
        except Exception as exc:  # pywin32/WMI missing or refused
            log.warning("the host is inside this client's kill-on-close job and could not leave it (%s); the "
                        "browser will close when the client disconnects", first_error_line(exc))
            return self._spawn_host(profile, window, leave_client_job=False, start_url=start_url)
        log.info("started the host for profile %s through WMI, outside the client's job (pid %s)", profile.name, pid)
        return _HostProcess(pid)

    def _spawn_host(self, profile: Profile, window: WindowMode | None, *,
                    leave_client_job: bool = False, start_url: str | None = None) -> subprocess.Popen:
        argv, env = host_command(profile.id, self.store.root, window, start_url)
        if sys.platform == "win32":
            env = dict(env if env is not None else os.environ)
            if leave_client_job:
                env[LEAVE_CLIENT_JOB_ENV] = "1"
            else:
                env.pop(LEAVE_CLIENT_JOB_ENV, None)
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

    def stop_all(self, timeout: float = 20.0, *, keep: Callable[[RuntimeInfo], bool] | None = None) -> list[str]:
        """Stop every profile with a live host (except those ``keep`` returns True for). Returns the
        names of the stopped profiles."""
        stopped: list[str] = []
        for info in self._live_hosts():
            if keep is not None and keep(info):
                continue
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

    def open_url(self, ref: str, url: str) -> None:
        """Have the running profile's Chrome open an http(s) ``url`` from its command line, in a new
        active tab, like a link opened from another app (the host's ``/open``). Raises
        :class:`ProfileNotRunningError` or :class:`ControlCallError` (e.g. 409 for a headless browser)."""
        _profile, info = self._require_running(ref)
        control_call(info, "POST", "/open", {"url": url}, timeout=15.0)

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
