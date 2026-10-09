"""Process liveness helpers shared by the store and the browser runtime (PID-reuse safe).

Kept free of other ProfilePilot imports (except the models) so that :mod:`profilepilot.store`
and :mod:`profilepilot.browser.runtime` can both use them without an import cycle.
"""

from __future__ import annotations

from datetime import datetime

import psutil

from .models import RuntimeInfo


def process_alive(pid: int | None, create_time: float | None = None, *, tolerance: float = 1.0) -> bool:
    """True if ``pid`` runs and (when given) was created at ``create_time`` (guards PID reuse)."""
    if not pid:
        return False
    try:
        proc = psutil.Process(int(pid))
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return create_time is None or abs(proc.create_time() - float(create_time)) <= tolerance
    except (psutil.Error, ValueError, OSError):
        return False


def pid_started_before(pid: int | None, timestamp: float | None, *, slack: float = 2.0) -> bool:
    """True if ``pid`` runs and was created no later than ``timestamp`` (+ ``slack`` seconds).

    A process that wrote a file at ``timestamp`` must have existed by then; a live PID created
    afterwards is a different process that reused the number.
    """
    if not pid or int(pid) <= 0:
        return False
    try:
        proc = psutil.Process(int(pid))
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return timestamp is None or proc.create_time() <= float(timestamp) + slack
    except (psutil.Error, ValueError, OSError):
        return False


def host_alive(info: RuntimeInfo) -> bool:
    """Host PID alive and created no later than the runtime.json it wrote (PID-reuse guard)."""
    started = info.started_at.timestamp() if isinstance(info.started_at, datetime) else None
    return pid_started_before(info.host_pid, started)


__all__ = ["host_alive", "pid_started_before", "process_alive"]
