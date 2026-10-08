"""Robust JSON file helpers (atomic writes, tolerant reads, cross-process locks)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from filelock import FileLock


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON, tolerating a UTF-8 BOM or UTF-16 (files edited with PowerShell).

    Returns ``default`` if the file does not exist. A corrupt file is renamed to ``*.bad``
    (so it is never silently overwritten) and ``default`` is returned.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default
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
    for _ in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # antivirus / indexer briefly holding the target
            last_exc = exc
            time.sleep(0.05)
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
