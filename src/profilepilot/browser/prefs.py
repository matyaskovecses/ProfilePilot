"""User-data-dir preparation and "is this profile's Chrome alive?" probing.

Session restore (verified on Chrome 154): seeding ``{"session": {"restore_on_startup": 1}}`` in
``Default/Preferences`` before the first launch does **not** work - the value stays in the file
but Chrome ignores it (``session.restore_on_startup`` is a MAC-tracked pref and the seeded value
has no valid MAC in ``Secure Preferences``), so session cookies are dropped and tabs are not
restored. ``--restore-last-session`` (added by :func:`profilepilot.browser.flags.build_chrome_args`
when ``launch.restore_session``) restores both tabs and session cookies after a graceful
``Browser.close``; nothing needs to be written here for it.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import sys
from pathlib import Path
from typing import Any

import psutil

from ..jsonio import read_json, write_json
from ..models import LaunchOptions

log = logging.getLogger("profilepilot.browser.prefs")

PROFILE_DIRECTORY = "Default"


def preferences_path(udd: Path) -> Path:
    return Path(udd) / PROFILE_DIRECTORY / "Preferences"


def prepare_user_data_dir(udd: Path, launch: LaunchOptions) -> None:
    """Make ``udd`` ready for a launch. Must only be called while Chrome is not running on it.

    * removes a stale ``DevToolsActivePort`` (never written with a fixed port, but an old one
      would mislead other tools);
    * ensures ``Default/Preferences`` exists and marks the last exit as clean
      (``profile.exit_type = "Normal"``, ``profile.exited_cleanly = true``) so Chrome shows no
      "restore pages?" bubble after a forced stop.

    ``launch.restore_session`` needs no preference changes: it is implemented with the
    ``--restore-last-session`` switch (see the module docstring for why).
    """
    udd = Path(udd)
    udd.mkdir(parents=True, exist_ok=True)
    (udd / "DevToolsActivePort").unlink(missing_ok=True)

    path = preferences_path(udd)
    data: Any = read_json(path, {}) if path.exists() else {}
    if not isinstance(data, dict):
        data = {}
    profile = data.get("profile")
    if not isinstance(profile, dict):
        profile = data["profile"] = {}
    if profile.get("exit_type") == "Normal" and profile.get("exited_cleanly") is True and path.exists():
        return
    profile["exit_type"] = "Normal"
    profile["exited_cleanly"] = True
    write_json(path, data)
    log.debug("prepared %s (restore_session=%s)", path, launch.restore_session)


def has_saved_session(udd: Path) -> bool:
    """True if Chrome has written session files (tabs) that ``--restore-last-session`` can restore."""
    sessions = Path(udd) / PROFILE_DIRECTORY / "Sessions"
    try:
        return any(p.name.startswith(("Session_", "Tabs_")) and p.stat().st_size > 0 for p in sessions.iterdir())
    except OSError:
        return False


def profile_in_use(udd: Path) -> bool:
    """Is a browser process currently running on ``udd``?

    Windows: Chrome holds ``<udd>/lockfile`` open (share-read only, delete-on-close) for its whole
    life, so opening it for writing fails with a sharing violation. POSIX: the
    ``SingletonLock`` symlink points at ``<hostname>-<pid>`` of the owning process.
    """
    udd = Path(udd)
    if sys.platform == "win32":
        lockfile = udd / "lockfile"
        try:
            fd = os.open(str(lockfile), os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            return False
        except PermissionError:
            return True
        except OSError as exc:
            return exc.errno in (errno.EACCES, errno.EBUSY)
        os.close(fd)
        return False

    return _singleton_lock_in_use(udd / "SingletonLock")


def _singleton_lock_in_use(singleton: Path) -> bool:
    """POSIX: does the ``<hostname>-<pid>`` SingletonLock symlink belong to a live process?

    A PID that was created after the lock was written is a different process that reused the
    number (the lock is stale), so it does not count."""
    try:
        target = os.readlink(singleton)
        written = os.lstat(singleton).st_mtime
    except OSError:
        return False
    host, _, pid_text = target.rpartition("-")
    if host and host != socket.gethostname():
        return True  # locked by another machine (shared home directory): treat as in use
    try:
        proc = psutil.Process(int(pid_text))
        return proc.is_running() and proc.create_time() <= written + 2.0
    except (ValueError, psutil.Error):
        return False
