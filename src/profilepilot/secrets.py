"""Secret storage for proxy passwords and API tokens.

Preferred backend: the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service).
Fallback: a file in the data root, encrypted with DPAPI on Windows (current-user scope) or
stored with 0600 permissions elsewhere. Secrets never appear in profile/proxy JSON files.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from pathlib import Path

from .jsonio import lock_for, read_json, write_json

log = logging.getLogger("profilepilot.secrets")

SERVICE = "ProfilePilot"
ENV_BACKEND = "PROFILEPILOT_SECRETS"  # "keyring" | "file"


class SecretStore:
    """Tiny key/value secret store. Keys look like ``proxy:<id>`` or ``shardx:token``."""

    def __init__(self, root: Path, backend: str | None = None) -> None:
        self.root = root
        self._file = root / "secrets.json"
        choice = (backend or os.environ.get(ENV_BACKEND) or "auto").lower()
        self._keyring = None
        if choice in ("auto", "keyring"):
            self._keyring = _load_keyring()
            if self._keyring is None and choice == "keyring":
                log.warning("keyring backend unavailable; falling back to the encrypted file store")
        # Namespace keyring entries per data root so test/alt roots never collide. Resolve first so
        # every spelling of the same directory (relative, 8.3 short name, ...) maps to one namespace.
        self._ns = f"{SERVICE}:{_short_hash(str(Path(root).resolve()))}"

    @property
    def backend(self) -> str:
        return "keyring" if self._keyring is not None else "file"

    def get(self, key: str) -> str | None:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(self._ns, key)
                if value is not None:
                    return value
            except Exception as exc:  # backend hiccup: fall through to file
                log.warning("keyring read failed (%s); trying file store", exc)
        blob = (read_json(self._file, {}) or {}).get(key)
        return _unprotect(blob) if blob else None

    def set(self, key: str, value: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.set_password(self._ns, key, value)
                self._file_delete(key)
                return
            except Exception as exc:
                log.warning("keyring write failed (%s); using encrypted file store", exc)
        with lock_for(self._file):
            data = read_json(self._file, {}) or {}
            data[key] = _protect(value)
            write_json(self._file, data)
        _restrict_permissions(self._file)

    def delete(self, key: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(self._ns, key)
            except Exception:
                pass
        self._file_delete(key)

    def _file_delete(self, key: str) -> None:
        if not self._file.exists():
            return
        with lock_for(self._file):
            data = read_json(self._file, {}) or {}
            if data.pop(key, None) is not None:
                write_json(self._file, data)


def _load_keyring():
    try:
        import keyring  # type: ignore[import-not-found]
        from keyring.backends import fail  # type: ignore[import-not-found]

        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring):
            return None
        if type(kr).__name__ == "ChainerBackend" and not getattr(kr, "backends", None):
            return None
        return keyring
    except Exception:
        return None


def _short_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.lower().encode()).hexdigest()[:8]


def _protect(value: str) -> str:
    raw = value.encode("utf-8")
    if sys.platform == "win32":
        try:
            import win32crypt  # type: ignore[import-not-found]

            enc = win32crypt.CryptProtectData(raw, "ProfilePilot", None, None, None, 0)
            return "dpapi:" + base64.b64encode(enc).decode()
        except Exception:
            pass
    return "b64:" + base64.b64encode(raw).decode()


def _unprotect(blob: str) -> str | None:
    try:
        kind, _, data = blob.partition(":")
        raw = base64.b64decode(data)
        if kind == "dpapi":
            import win32crypt  # type: ignore[import-not-found]

            return win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1].decode("utf-8")
        return raw.decode("utf-8")
    except Exception as exc:
        log.warning("could not decrypt a stored secret: %s", exc)
        return None


def _restrict_permissions(path: Path) -> None:
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
