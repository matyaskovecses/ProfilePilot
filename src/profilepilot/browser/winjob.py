"""Windows job objects: tie Chrome's lifetime to the host process.

The host puts *itself* into a fresh job object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``.
Chrome (spawned afterwards) inherits the job, and Chrome's own sandbox jobs nest inside it
(Windows 8+). When the host dies for any reason - including a hard kill - the kernel closes the
last job handle and terminates every process in the job, so no orphaned Chrome keeps a profile
locked with a dead proxy relay.

Everything here is best effort: on other platforms, or when the job cannot be created (for
example because an enclosing job forbids nesting), the functions log and return ``None``/False.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger("profilepilot.browser.winjob")

# The handle must stay referenced for the life of the process: closing the last handle of a
# KILL_ON_JOB_CLOSE job kills every process in it - including this one.
_JOB_HANDLE: Any = None


def kill_on_close_job_supported() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32job  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        return False
    return True


def put_self_in_kill_on_close_job() -> Any | None:
    """Assign the current process to a new kill-on-close job. Returns the job handle or None.

    Idempotent: a second call returns the handle created by the first.
    """
    global _JOB_HANDLE
    if _JOB_HANDLE is not None:
        return _JOB_HANDLE
    if sys.platform != "win32":
        return None
    try:
        import win32api  # type: ignore[import-not-found]
        import win32job  # type: ignore[import-not-found]
    except ImportError:
        log.warning("pywin32 is not installed; Chrome will not be tied to the host's lifetime")
        return None
    try:
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    except Exception as exc:  # pywintypes.error: e.g. access denied by an enclosing job
        log.warning("could not create the kill-on-close job object: %s", exc)
        return None
    _JOB_HANDLE = job
    log.info("host process assigned to a kill-on-close job object")
    return job


def process_in_job(pid: int, job: Any | None = None) -> bool:
    """True if ``pid`` belongs to ``job`` (default: the job created by this module)."""
    job = job if job is not None else _JOB_HANDLE
    if sys.platform != "win32" or job is None:
        return False
    try:
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32job  # type: ignore[import-not-found]

        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        try:
            return bool(win32job.IsProcessInJob(handle, job))
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return False
