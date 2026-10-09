"""Launch a throwaway real Chrome for tests (isolated user-data-dir, fixed CDP port, off-screen).

Only ever kills the processes it started itself - never the user's own Chrome.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest

from profilepilot.automation.driver import DRIVER, DRIVERS, ENV_DRIVER, installed
from profilepilot.errors import BrowserNotFoundError
from profilepilot.paths import BrowserInfo, find_browser


DRIVER_PARAMS = [pytest.param(name, marks=pytest.mark.skipif(not installed(name), reason=f"{name} is not installed"))
                 for name in DRIVERS]


@pytest.fixture(params=DRIVER_PARAMS)
def cdp_driver(request, monkeypatch) -> str:
    """Each CDP driver in turn (patchright, playwright): ``PROFILEPILOT_DRIVER`` is set for the test,
    so BrowserManager / the MCP server use it. Import this fixture into a test module to use it."""
    monkeypatch.setenv(ENV_DRIVER, request.param)
    return request.param


def default_driver_only(driver: str, why: str) -> None:
    """Skip the other drivers' run of a test that should run once (e.g. it uses the user's clipboard)."""
    if driver != DRIVER:
        pytest.skip(f"{why}: run with the default driver ({DRIVER}) only")


def find_test_browser() -> BrowserInfo:
    try:
        return find_browser("auto")
    except BrowserNotFoundError:
        pytest.skip("no Chromium-family browser installed")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class LaunchedChrome:
    proc: subprocess.Popen
    port: int
    user_data_dir: Path
    browser: BrowserInfo

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def version_info(self) -> dict:
        with urllib.request.urlopen(f"{self.http_url}/json/version", timeout=2) as r:
            return json.loads(r.read())

    def kill(self) -> None:
        kill_tree(self.proc.pid)


def kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.kill()
    psutil.wait_procs(procs, timeout=10)


@contextlib.contextmanager
def launch_chrome(user_data_dir: Path, *extra_args: str, url: str = "about:blank", timeout: float = 45.0):
    """Context manager yielding a :class:`LaunchedChrome`; always cleans up."""
    browser = find_test_browser()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    args = [
        browser.path,
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-search-engine-choice-screen",
        "--window-position=-32000,-32000",
        "--disable-backgrounding-occluded-windows",
        *extra_args,
        url,
    ]
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    launched = LaunchedChrome(proc, port, user_data_dir, browser)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                launched.version_info()
                break
            except Exception:
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("test Chrome did not expose its DevTools endpoint")
                time.sleep(0.1)
        yield launched
    finally:
        launched.kill()
