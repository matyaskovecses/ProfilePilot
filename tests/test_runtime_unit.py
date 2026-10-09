"""RuntimeManager logic that needs no real browser (fake runtime.json files, stubbed spawning)."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import pytest

from profilepilot.browser.runtime import RuntimeManager
from profilepilot.errors import LaunchError


def _write_runtime(store, profile, **fields) -> None:
    data = {"profile_id": profile.id, "profile_name": profile.name, "state": "starting", "host_pid": os.getpid()}
    data.update(fields)
    store.runtime_file(profile.id).write_text(json.dumps(data), encoding="utf-8")


def test_start_never_waits_for_another_hosts_readiness_inside_the_start_lock(store, monkeypatch):
    """Another client's host registers while we wait for the cross-profile start lock: the wait for
    its readiness must happen outside the lock, so other profiles can start meanwhile."""
    profile = store.create_profile("a")
    runtime = RuntimeManager(store)
    held = {"lock": False}
    ready_checks_inside_lock: list[bool] = []

    @contextlib.contextmanager
    def start_lock():
        # While we "waited" for the lock, another client's host wrote runtime.json (state starting).
        _write_runtime(store, profile)
        held["lock"] = True
        try:
            yield
        finally:
            held["lock"] = False

    def is_ready(info):
        ready_checks_inside_lock.append(held["lock"])
        return False

    monkeypatch.setattr(runtime, "_start_lock", start_lock)
    monkeypatch.setattr(runtime, "_is_ready", is_ready)
    monkeypatch.setattr(runtime, "_spawn_host", lambda *a, **k: pytest.fail("must not spawn a second host"))
    with pytest.raises(LaunchError, match="did not become ready in time"):
        runtime.start("a", timeout=0.5)
    assert ready_checks_inside_lock and not any(ready_checks_inside_lock)


def test_pyproject_dependency_floors_cover_the_apis_we_use():
    """psutil.Process.net_connections needs psutil 6; websockets connect(proxy=...) needs 15."""
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover
        pytest.skip("tomllib needs Python 3.11")
    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    deps = {d.split(">=")[0].split("[")[0].strip(): d for d in data["project"]["dependencies"]}

    def floor(name: str) -> tuple[int, ...]:
        spec = deps[name].split(">=", 1)[1].split(",")[0].split(";")[0].strip()
        return tuple(int(x) for x in spec.split("."))

    assert floor("psutil") >= (6, 0)
    assert floor("websockets") >= (15,)


def test_browser_close_helpers_never_raise(monkeypatch):
    """A broken websockets install must only cost the graceful close (fallback: WM_CLOSE / kill)."""
    import asyncio

    from profilepilot.browser import control

    real_import = __import__

    def broken_import(name, *args, **kwargs):
        if name.startswith("websockets"):
            raise ModuleNotFoundError("No module named 'websockets.asyncio'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", broken_import)
    assert control.cdp_browser_close("ws://127.0.0.1:9/devtools/browser/x", timeout=0.5) is False
    assert asyncio.run(control.async_cdp_browser_close("ws://127.0.0.1:9/devtools/browser/x", timeout=0.5)) is False
