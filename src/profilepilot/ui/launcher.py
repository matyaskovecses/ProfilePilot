"""Start ProfilePilot Manager: one server per data root, shown in its own app window.

``profilepilot ui`` / ``python -m profilepilot.ui``:

1. Takes the single-instance lock ``<root>/ui.lock``. If another Manager holds it, this process
   asks that one for a single-use launch code (with the token from the secret store) and opens a
   window on it, or brings its open window to the front, then exits.
2. Otherwise mints a fresh 32-byte token (kept in the secret store as ``ui:token`` for step 1),
   binds ``127.0.0.1:<port>`` (random unless ``--port``), writes ``<root>/ui.json`` ``{pid, port,
   started_at}`` and serves the app with uvicorn.
3. Opens an **app window**: ``<chrome> --app=<url> --user-data-dir=<root>/ui-window
   --window-size=1320,880 --no-first-run --no-default-browser-check``. That small user-data-dir is
   not a ProfilePilot profile and is never driven over CDP. Without a Chromium browser the default
   browser is used (``webbrowser``).
4. Exits when the app window closes, unless ``--keep-running`` or ``--no-window``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Sequence

from filelock import FileLock, Timeout

from .. import __version__
from ..control import UI_STATE_FILE, manager_info
from ..jsonio import read_json, write_json
from ..store import Store

log = logging.getLogger("profilepilot.ui.launcher")

TOKEN_KEY = "ui:token"
"""Secret-store key of the running Manager's token (rotated on every start)."""
LOCK_NAME = "ui.lock"
WINDOW_DIR = "ui-window"
WINDOW_SIZE = "1320,880"
ATTACH_WAIT = 15.0


_GUI_MODE = False
"""True when started without a console (pythonw.exe, the shortcut): problems are shown in a message box."""


def _say(text: str) -> None:
    """Print to the console when there is one (pythonw.exe has no stdout)."""
    stream = sys.stdout
    if stream is not None:
        with contextlib.suppress(Exception):
            stream.write(text + "\n")
            stream.flush()


def _problem(text: str) -> None:
    """Tell the user about a problem: on the console, or in a message box without one (Windows)."""
    _say(text)
    if _GUI_MODE and sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "ProfilePilot Manager", 0x10 | 0x40000)  # error icon, topmost


_LOGGERS = ("profilepilot", "uvicorn", "uvicorn.error")


def setup_logging(root: Path, level: str = "INFO") -> logging.Handler:
    """Log to ``<root>/ui.log`` (rotated at 1 MB). Returns the handler (see :func:`teardown_logging`)."""
    handler = logging.handlers.RotatingFileHandler(root / "ui.log", maxBytes=1_000_000, backupCount=1,
                                                   encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("profilepilot").setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in _LOGGERS:
        logging.getLogger(name).addHandler(handler)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True  # URLs carry launch codes
    for name in ("httpx", "httpcore", "websockets"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def teardown_logging(handler: logging.Handler) -> None:
    for name in _LOGGERS:
        logging.getLogger(name).removeHandler(handler)
    with contextlib.suppress(Exception):
        handler.close()


# --------------------------------------------------------------------------- the app window


def window_dir(store: Store) -> Path:
    return store.root / WINDOW_DIR


def window_pids(udd: Path) -> list[int]:
    """Browser processes (not renderers/helpers) running the Manager window's user-data-dir."""
    import psutil

    needle = f"--user-data-dir={udd}".casefold()
    found: list[int] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if not name.startswith(("chrome", "msedge", "brave", "chromium", "google chrome", "microsoft edge")):
                continue
            cmdline = proc.info.get("cmdline") or []
            if any(a.casefold() == needle for a in cmdline) and not any(a.startswith("--type=") for a in cmdline):
                found.append(proc.info["pid"])
        except (psutil.Error, OSError):
            continue
    return found


def window_command(browser_path: str, url: str, udd: Path) -> list[str]:
    return [
        browser_path, f"--app={url}", f"--user-data-dir={udd}", f"--window-size={WINDOW_SIZE}",
        "--no-first-run", "--no-default-browser-check", "--disable-search-engine-choice-screen",
        "--class=ProfilePilotManager",
    ]


def open_window(store: Store, url: str) -> subprocess.Popen | None:
    """Open the Manager in its own app window. Returns the browser process, or None when the
    default browser was used instead (no Chromium-family browser found)."""
    from ..errors import BrowserNotFoundError
    from ..paths import find_browser

    try:
        browser = find_browser("auto", store.load_config().browser_path)
    except BrowserNotFoundError:
        log.warning("no Chromium-family browser found; opening the default browser")
        webbrowser.open(url)
        return None
    udd = window_dir(store)
    udd.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW (for the launcher stub)
    try:
        return subprocess.Popen(window_command(browser.path, url, udd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                creationflags=flags)
    except OSError as exc:
        log.warning("could not start %s (%s); opening the default browser", browser.path, exc)
        webbrowser.open(url)
        return None


def wait_for_window_close(store: Store, proc: subprocess.Popen | None, stop: threading.Event,
                          poll: float = 1.0) -> None:
    """Block until the Manager window is gone (or ``stop`` is set).

    A browser started on a user-data-dir that is already open hands its URL to the running
    instance and exits at once, so the window processes are found by their command line."""
    udd = window_dir(store)
    deadline = time.monotonic() + 20.0
    while not stop.is_set():  # wait for the window to appear (or the hand-off to finish)
        pids = window_pids(udd)
        if pids:
            break
        if proc is not None and proc.poll() is None:
            pids = [proc.pid]
            break
        if time.monotonic() > deadline:
            return
        stop.wait(0.5)
    import psutil

    while not stop.is_set():
        alive = [p for p in pids if psutil.pid_exists(p)]
        if not alive:
            pids = window_pids(udd)  # a new window process (e.g. after a browser restart)?
            if not pids:
                return
            continue
        stop.wait(poll)


def focus_existing_window(store: Store) -> bool:
    from .cdp import focus_native_window

    for pid in window_pids(window_dir(store)):
        if focus_native_window(pid):
            return True
    return False


# --------------------------------------------------------------------------- attach to a running Manager


def attach(store: Store, *, open_new_window: bool = True) -> int:
    """Another Manager runs for this data root: show its window."""
    import httpx

    from .server import TOKEN_HEADER

    deadline = time.monotonic() + ATTACH_WAIT
    info = token = None
    while time.monotonic() < deadline:
        info = manager_info(store.root)
        token = store.secrets.get(TOKEN_KEY)
        if info and token:
            break
        time.sleep(0.25)
    if not info or not token:
        _problem("ProfilePilot Manager is already starting for this data folder. Try again in a moment.")
        return 1
    if focus_existing_window(store):
        _say("ProfilePilot Manager is already open.")
        return 0
    if not open_new_window:
        _say(f"ProfilePilot Manager is running on port {info['port']}. Run 'profilepilot ui' to open its window.")
        return 0
    try:
        resp = httpx.post(f"http://127.0.0.1:{int(info['port'])}/api/launch-code", headers={TOKEN_HEADER: token},
                          timeout=5.0, trust_env=False)
        code = resp.json().get("code") if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        code = None
    if not code:
        _problem("ProfilePilot Manager is running but did not answer. Close it and try again.")
        return 1
    open_window(store, f"http://127.0.0.1:{int(info['port'])}/?t={code}")
    _say("Opened ProfilePilot Manager.")
    return 0


# --------------------------------------------------------------------------- serve


def bind_socket(port: int = 0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 0xFFFB), 1)
    try:
        sock.bind(("127.0.0.1", int(port)))
    except OSError:
        sock.close()
        raise
    sock.listen(128)
    sock.set_inheritable(False)
    return sock


def run_manager(store: Store, *, port: int = 0, open_app_window: bool = True, keep_running: bool = False,
                log_level: str = "INFO") -> int:
    """Run the Manager (blocking). Returns the exit code."""
    lock = FileLock(str(store.root / LOCK_NAME))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return attach(store, open_new_window=open_app_window)
    try:
        return _serve(store, port=port, open_app_window=open_app_window, keep_running=keep_running,
                      log_level=log_level)
    finally:
        with contextlib.suppress(Exception):
            lock.release()


def _serve(store: Store, *, port: int, open_app_window: bool, keep_running: bool, log_level: str) -> int:
    handler = setup_logging(store.root, log_level)
    try:
        return _serve_logged(store, port=port, open_app_window=open_app_window, keep_running=keep_running)
    finally:
        teardown_logging(handler)


def _serve_logged(store: Store, *, port: int, open_app_window: bool, keep_running: bool) -> int:
    import uvicorn

    from .server import create_app

    try:
        sock = bind_socket(port)
    except OSError as exc:
        _problem(f"ProfilePilot Manager cannot listen on 127.0.0.1:{port}: {exc}")
        return 1
    actual_port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    store.secrets.set(TOKEN_KEY, token)
    app = create_app(store, token=token, port=actual_port)
    state_file = store.root / UI_STATE_FILE
    write_json(state_file, {"pid": os.getpid(), "port": actual_port, "started_at": time.time(), "version": __version__})
    log.info("ProfilePilot Manager %s listening on 127.0.0.1:%s (data root %s)", __version__, actual_port, store.root)

    config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on", proxy_headers=False,
                            server_header=False, date_header=False, timeout_graceful_shutdown=2,
                            ws="none", log_level="warning")
    server = uvicorn.Server(config)
    stop = threading.Event()
    watcher: threading.Thread | None = None
    if open_app_window:
        url = f"http://127.0.0.1:{actual_port}/?t={app.auth.new_code()}"
        proc = open_window(store, url)

        def watch() -> None:
            if proc is None:  # default browser: we cannot see it close
                return
            wait_for_window_close(store, proc, stop)
            if not stop.is_set() and not keep_running:
                log.info("the Manager window was closed; shutting down")
                server.should_exit = True

        watcher = threading.Thread(target=watch, name="ui-window-watch", daemon=True)
        watcher.start()
        if proc is None or keep_running:
            _say(f"ProfilePilot Manager is running at http://127.0.0.1:{actual_port}/ (press Ctrl+C to stop).")
        else:
            _say("ProfilePilot Manager is open. Close its window to stop it.")
    else:
        _say(f"ProfilePilot Manager is running on port {actual_port}. Run 'profilepilot ui' to open a window "
             "(press Ctrl+C to stop).")
    try:
        asyncio.run(server.serve(sockets=[sock]))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            sock.close()
        current = read_json(state_file)
        if isinstance(current, dict) and current.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                state_file.unlink()
        with contextlib.suppress(Exception):
            if store.secrets.get(TOKEN_KEY) == token:
                store.secrets.delete(TOKEN_KEY)
        log.info("ProfilePilot Manager stopped")
    return 0


# --------------------------------------------------------------------------- command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="profilepilot ui", description=(
        "Open ProfilePilot Manager: manage profiles, proxies and identities by hand, watch what the AI does, "
        "and take over a browser whenever a human is needed."))
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)")
    parser.add_argument("--no-window", action="store_true", help="only run the server; 'profilepilot ui' opens a window")
    parser.add_argument("--keep-running", action="store_true", help="keep the server running after the window closes")
    parser.add_argument("--install-shortcut", action="store_true",
                        help="create Desktop and Start-menu shortcuts (macOS: a .command file, Linux: a .desktop file)")
    parser.add_argument("--remove-shortcut", action="store_true", help="remove the shortcuts again")
    parser.add_argument("--home", metavar="PATH", help="data folder (default: PROFILEPILOT_HOME or the platform default)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--version", action="version", version=f"profilepilot {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    global _GUI_MODE
    if sys.stdout is None or sys.stderr is None:  # pythonw.exe (the shortcut): no console at all
        _GUI_MODE = True
        devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives for the process
        sys.stdout = sys.stdout or devnull
        sys.stderr = sys.stderr or devnull
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    store = Store(Path(args.home).expanduser().resolve()) if args.home else Store()
    if args.install_shortcut or args.remove_shortcut:
        from .shortcut import install_shortcuts, remove_shortcuts

        if args.remove_shortcut:
            removed = remove_shortcuts()
            _say("Removed: " + ", ".join(str(p) for p in removed) if removed else "No shortcuts to remove.")
            return 0
        created = install_shortcuts(store.root)
        _say("Created:\n" + "\n".join(f"  {p}" for p in created))
        return 0
    try:
        return run_manager(store, port=args.port, open_app_window=not args.no_window, keep_running=args.keep_running,
                           log_level=args.log_level)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # never die silently when started from the shortcut
        log.exception("ProfilePilot Manager failed")
        _problem(f"ProfilePilot Manager could not start: {type(exc).__name__}: {exc}\n\nDetails: {store.root / 'ui.log'}")
        return 1


__all__ = ["attach", "main", "open_window", "run_manager", "window_pids"]
