"""Filesystem locations and browser executable discovery."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import BrowserNotFoundError

ENV_HOME = "PROFILEPILOT_HOME"
ENV_BROWSER = "PROFILEPILOT_BROWSER"

_ID_RE = re.compile(r"^[a-z0-9]{4,32}$")


def data_root() -> Path:
    """Root data directory. Override with ``PROFILEPILOT_HOME``.

    Windows: ``%LOCALAPPDATA%\\ProfilePilot`` (local, not roaming: profiles can be large).
    macOS: ``~/Library/Application Support/ProfilePilot``. Linux: ``$XDG_DATA_HOME/profilepilot``.
    """
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ProfilePilot"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ProfilePilot"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "profilepilot"


def new_id() -> str:
    """Short random id (8 lowercase hex chars) - keeps Windows paths short."""
    return secrets.token_hex(4)


def is_valid_id(value: str) -> bool:
    return bool(_ID_RE.match(value))


# --------------------------------------------------------------------------- browsers


@dataclass(frozen=True)
class BrowserInfo:
    kind: str
    path: str
    version: str | None = None

    @property
    def major(self) -> int | None:
        if not self.version:
            return None
        try:
            return int(self.version.split(".")[0])
        except ValueError:
            return None


def _windows_candidates() -> dict[str, list[Path]]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    return {
        "chrome": [
            local / "Google/Chrome/Application/chrome.exe",
            pf / "Google/Chrome/Application/chrome.exe",
            pf86 / "Google/Chrome/Application/chrome.exe",
        ],
        "edge": [
            pf86 / "Microsoft/Edge/Application/msedge.exe",
            pf / "Microsoft/Edge/Application/msedge.exe",
            local / "Microsoft/Edge/Application/msedge.exe",
        ],
        "brave": [
            local / "BraveSoftware/Brave-Browser/Application/brave.exe",
            pf / "BraveSoftware/Brave-Browser/Application/brave.exe",
        ],
        "chromium": [local / "Chromium/Application/chrome.exe", pf / "Chromium/Application/chrome.exe"],
    }


def _mac_candidates() -> dict[str, list[Path]]:
    apps = [Path("/Applications"), Path.home() / "Applications"]
    names = {
        "chrome": "Google Chrome.app/Contents/MacOS/Google Chrome",
        "edge": "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "brave": "Brave Browser.app/Contents/MacOS/Brave Browser",
        "chromium": "Chromium.app/Contents/MacOS/Chromium",
    }
    return {k: [a / v for a in apps] for k, v in names.items()}


def _linux_candidates() -> dict[str, list[Path]]:
    def which(*names: str) -> list[Path]:
        return [Path(p) for n in names if (p := shutil.which(n))]

    return {
        "chrome": which("google-chrome", "google-chrome-stable"),
        "edge": which("microsoft-edge", "microsoft-edge-stable"),
        "brave": which("brave-browser", "brave"),
        "chromium": which("chromium", "chromium-browser"),
    }


def _candidates() -> dict[str, list[Path]]:
    if sys.platform == "win32":
        return _windows_candidates()
    if sys.platform == "darwin":
        return _mac_candidates()
    return _linux_candidates()


def browser_version(path: str | Path) -> str | None:
    """Best-effort version lookup without launching the browser on Windows."""
    p = Path(path)
    if sys.platform == "win32":
        try:
            import win32api  # type: ignore[import-not-found]

            info = win32api.GetFileVersionInfo(str(p), "\\")
            ms, ls = info["FileVersionMS"], info["FileVersionLS"]
            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        except Exception:
            pass
        versions = sorted(
            (d.name for d in p.parent.iterdir() if d.is_dir() and re.match(r"^\d+\.\d+\.\d+\.\d+$", d.name)),
            key=lambda v: [int(x) for x in v.split(".")],
        ) if p.parent.exists() else []
        return versions[-1] if versions else None
    try:
        out = subprocess.run([str(p), "--version"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def find_browser(preference: str = "auto", override: str | None = None) -> BrowserInfo:
    """Locate a Chromium-family browser.

    ``preference`` is ``auto`` (Chrome, then Edge, Brave, Chromium), a kind name, or an absolute
    path. ``override`` (from config or ``PROFILEPILOT_BROWSER``) wins when preference is ``auto``.
    """
    pref = (preference or "auto").strip()
    env_override = override or os.environ.get(ENV_BROWSER)
    if pref == "auto" and env_override:
        pref = env_override

    if pref not in ("auto", "chrome", "edge", "brave", "chromium"):
        path = Path(pref).expanduser()
        if path.is_file():
            kind = _kind_from_path(path)
            return BrowserInfo(kind, str(path), browser_version(path))
        raise BrowserNotFoundError(f"Browser executable not found: {pref}")

    cands = _candidates()
    order = ["chrome", "edge", "brave", "chromium"] if pref == "auto" else [pref]
    for kind in order:
        for path in cands.get(kind, []):
            if path.is_file():
                return BrowserInfo(kind, str(path), browser_version(path))
    wanted = "a Chromium-family browser (Chrome, Edge, Brave or Chromium)" if pref == "auto" else pref
    raise BrowserNotFoundError(
        f"Could not find {wanted}. Install Google Chrome, or set {ENV_BROWSER} / the 'browser' "
        "field of the profile to the full path of chrome.exe."
    )


def list_browsers() -> list[BrowserInfo]:
    found: list[BrowserInfo] = []
    for kind, paths in _candidates().items():
        for path in paths:
            if path.is_file():
                found.append(BrowserInfo(kind, str(path), browser_version(path)))
                break
    return found


def _kind_from_path(path: Path) -> str:
    name = path.name.lower()
    if "msedge" in name or "edge" in name:
        return "edge"
    if "brave" in name:
        return "brave"
    if "chromium" in str(path).lower():
        return "chromium"
    return "chrome"
