"""User-data-dir preparation and "is this profile's Chrome alive?" probing.

Session restore (verified on Chrome 154): seeding ``{"session": {"restore_on_startup": 1}}`` in
``Default/Preferences`` before the first launch does **not** work - the value stays in the file
but Chrome ignores it (``session.restore_on_startup`` is a MAC-tracked pref and the seeded value
has no valid MAC in ``Secure Preferences``), so session cookies are dropped and tabs are not
restored. ``--restore-last-session`` (added by :func:`profilepilot.browser.flags.build_chrome_args`
when ``launch.restore_session``) restores both tabs and session cookies after a graceful
``Browser.close``; nothing needs to be written here for it.

User-data-dir path length (verified on Chrome 154, Windows 11; docs/FINGERPRINT-AUDIT.md F2): Chrome
keeps a GPU cache database at ``GPUPersistentCache/DawnGraphiteCache/<32 chars>/cache.db`` (with
``cache.journal``: 83 characters below the user-data-dir). When the user-data-dir path is longer than
:data:`MAX_USER_DATA_DIR_CHARS` (175) that file exceeds the Windows path limit (260) and cannot be
created, and some pages (iphey.com, 5/5) then crash the whole browser process with 0xC0000005 a few
seconds after they load - with or without a DevTools client (19 of 19 runs at 176-197 characters
crashed, none of about 30 at 175 or less). The default data root (``%LOCALAPPDATA%\\ProfilePilot``) gives ~58 characters.

Secure DNS for proxied profiles (verified on Chrome 154; docs/FINGERPRINT-AUDIT.md F11): in its default
"automatic" mode Chrome probes the system resolver's DNS-over-HTTPS server. Those probes are sent with
``LOAD_BYPASS_PROXY``: about 7 s after launch the network service opens a direct TCP 443 connection to
the DoH server (plus plain UDP DNS for its name) from the machine's real IP, around the proxy - stock
Chrome with the same proxy does the same. Pages cannot see it, and no visited host goes that way, but
the local network and ISP see a proxied profile's browser talk directly. With ``dns_over_https.mode =
"off"`` in ``Local State`` (not a MAC-protected pref) the audit measured 0 non-loopback connections and
0 DNS transactions, with unchanged DNS-leak, exit-IP and WebRTC results. Nothing is lost: a proxied
profile's host names are resolved at the proxy anyway. :func:`prepare_user_data_dir` therefore turns
Secure DNS off before a proxied launch, remembers that (and the user's own value) in
:data:`DOH_OFF_MARKER`, and restores the value at the first launch without a proxy.
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
MAX_USER_DATA_DIR_CHARS = 175
"""Longest user-data-dir path (Windows) with which Chrome 154 can create all of its files: see the
module docstring."""
LOCAL_STATE = "Local State"
DOH_OFF_MARKER = ".profilepilot-doh-off"
"""Written next to ``Local State`` while ProfilePilot keeps Secure DNS off for a proxied profile; holds
the ``dns_over_https.mode`` the profile had before (``{"previous": null}`` = Chrome's default)."""


def user_data_dir_too_long(udd: Path) -> bool:
    """Windows: is ``udd`` longer than :data:`MAX_USER_DATA_DIR_CHARS` (some pages then crash Chrome)?"""
    return sys.platform == "win32" and len(str(Path(udd).absolute())) > MAX_USER_DATA_DIR_CHARS


def long_path_hint(udd: Path) -> str:
    """Model- and user-facing advice when :func:`user_data_dir_too_long` (else '')."""
    if not user_data_dir_too_long(udd):
        return ""
    return (f"This profile's data folder path is {len(str(Path(udd).absolute()))} characters long; Chrome crashes like "
            f"this on some pages when it is longer than {MAX_USER_DATA_DIR_CHARS} (the Windows path limit). Move "
            "ProfilePilot's data to a shorter folder (PROFILEPILOT_HOME).")


def preferences_path(udd: Path) -> Path:
    return Path(udd) / PROFILE_DIRECTORY / "Preferences"


def prepare_user_data_dir(udd: Path, launch: LaunchOptions, *, proxied: bool = False) -> None:
    """Make ``udd`` ready for a launch. Must only be called while Chrome is not running on it.

    * removes a stale ``DevToolsActivePort`` (never written with a fixed port, but an old one
      would mislead other tools);
    * ``proxied`` (the browser will use the profile's proxy relay): turns Secure DNS off in
      ``Local State``, else restores what an earlier proxied launch changed (see
      :func:`set_secure_dns_for_proxy` and the module docstring);
    * ensures ``Default/Preferences`` exists and marks the last exit as clean
      (``profile.exit_type = "Normal"``, ``profile.exited_cleanly = true``) so Chrome shows no
      "restore pages?" bubble after a forced stop.

    ``launch.restore_session`` needs no preference changes: it is implemented with the
    ``--restore-last-session`` switch (see the module docstring for why).
    """
    udd = Path(udd)
    udd.mkdir(parents=True, exist_ok=True)
    (udd / "DevToolsActivePort").unlink(missing_ok=True)
    set_secure_dns_for_proxy(udd, proxied)

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


def set_secure_dns_for_proxy(udd: Path, proxied: bool) -> None:
    """Secure DNS of ``udd`` (``Local State`` ``dns_over_https.mode``) for a launch with or without a proxy.

    * ``proxied``: ``mode = "off"`` is merged into ``Local State`` (every other key stays) and
      :data:`DOH_OFF_MARKER` records the mode the profile had before (written once, before the change).
    * not ``proxied``, with the marker: the recorded mode comes back (none recorded: the key is removed,
      so Chrome's own default, "automatic", applies) - unless the mode is no longer ``"off"``: then the
      user chose another one while proxied, and it is kept. The marker is removed.
    * not ``proxied``, no marker: nothing is read or written; a user's own Secure DNS setting is never
      touched.
    """
    udd = Path(udd)
    marker = udd / DOH_OFF_MARKER
    if not proxied and not marker.exists():
        return
    path = udd / LOCAL_STATE
    data: Any = read_json(path, {}) if path.exists() else {}
    if not isinstance(data, dict):
        data = {}
    doh = data.get("dns_over_https")
    if not isinstance(doh, dict):
        doh = {}
    if proxied:
        if not marker.exists():
            write_json(marker, {"previous": doh.get("mode")})
        if doh.get("mode") != "off":
            doh["mode"] = "off"
            data["dns_over_https"] = doh
            write_json(path, data)
            log.info("Secure DNS turned off for a proxied launch (its probes would bypass the proxy)")
        return
    record = read_json(marker, {})
    previous = record.get("previous") if isinstance(record, dict) else None
    if doh.get("mode") == "off":
        if isinstance(previous, str) and previous:
            doh["mode"] = previous
        else:
            doh.pop("mode", None)
        if doh:
            data["dns_over_https"] = doh
        else:
            data.pop("dns_over_https", None)
        write_json(path, data)
        log.info("Secure DNS restored for a launch without a proxy")
    marker.unlink(missing_ok=True)


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
