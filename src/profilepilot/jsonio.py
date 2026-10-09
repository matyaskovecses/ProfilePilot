"""Robust JSON file helpers (atomic writes, tolerant reads, cross-process locks)."""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any

from filelock import FileLock

from .errors import ProfilePilotError

_READ_ATTEMPTS = 10
_WRITE_ATTEMPTS = 25


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, tolerating a UTF-8 BOM or UTF-16 (files edited with PowerShell).

    Returns ``default`` if the file does not exist. A corrupt file is renamed to ``*.bad``
    (so it is never silently overwritten) and ``default`` is returned. On Windows a read can race
    an atomic replace by another process (sharing violation): it is retried briefly.
    """
    for attempt in range(_READ_ATTEMPTS):
        try:
            raw = path.read_bytes()
            break
        except FileNotFoundError:
            return default
        except PermissionError:
            if attempt == _READ_ATTEMPTS - 1:
                raise ProfilePilotError(f"{path.name} is locked by another process; try again in a moment.") from None
            time.sleep(0.02 + random.random() * 0.05)
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    bad = path.with_suffix(path.suffix + ".bad")
    try:
        os.replace(path, bad)
    except OSError:
        pass
    return default


def write_json(path: Path, data: Any) -> None:
    """Atomically write JSON (temp file + fsync + replace, retried for Windows scanners)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    last_exc: OSError | None = None
    for _ in range(_WRITE_ATTEMPTS):  # up to ~2 s, jittered so concurrent writers do not collide
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # antivirus / indexer / a reader briefly holding the target
            last_exc = exc
            time.sleep(0.03 + random.random() * 0.07)
    try:
        tmp.unlink()
    except OSError:
        pass
    assert last_exc is not None
    raise last_exc


def lock_for(path: Path, timeout: float = 15.0) -> FileLock:
    """Cross-process lock guarding read-modify-write cycles on ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path.with_name(f".{path.name}.lock")), timeout=timeout)
