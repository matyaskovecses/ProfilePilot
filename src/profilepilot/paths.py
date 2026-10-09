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
#
# Every profile runs a genuine browser installed on this computer: the Chromium family, which the
# DevTools protocol drives natively (Chrome, Edge, Brave, Chromium, and the pre-release channels of
# Chrome and Edge). Pages - and http_fetch, which takes its identity from the running browser - see
# that browser exactly as a person using it would. Not supported, on purpose: Opera and Vivaldi;
# Firefox and Safari (other engines without native CDP; a Playwright Firefox or WebKit build would
# be a non-native browser). See docs/DESIGN.md, "Supported browsers".

BROWSER_KINDS: tuple[str, ...] = (
    "chrome", "edge", "brave", "chromium",
    "chrome-beta", "chrome-dev", "chrome-canary", "edge-beta", "edge-dev", "edge-canary",
)
"""Supported browser kinds, in the order ``auto`` tries them: the stable browsers first, then the
pre-release channels of Chrome and Edge."""

BROWSER_LABELS: dict[str, str] = {
    "chrome": "Google Chrome", "edge": "Microsoft Edge", "brave": "Brave", "chromium": "Chromium",
    "chrome-beta": "Google Chrome Beta", "chrome-dev": "Google Chrome Dev", "chrome-canary": "Google Chrome Canary",
    "edge-beta": "Microsoft Edge Beta", "edge-dev": "Microsoft Edge Dev", "edge-canary": "Microsoft Edge Canary",
}


def browser_family(kind: str) -> str:
    """``chrome-canary`` -> ``chrome``, ``edge-beta`` -> ``edge``; the other kinds are their own family."""
    return (kind or "").split("-", 1)[0]


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

    @property
    def family(self) -> str:
        """``chrome``, ``edge``, ``brave`` or ``chromium``, whatever the release channel."""
        return browser_family(self.kind)

    @property
    def label(self) -> str:
        return BROWSER_LABELS.get(self.kind, self.kind)


def _windows_candidates() -> dict[str, list[Path]]:
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))

    def chrome(folder: str) -> list[Path]:
        return [base / f"Google/{folder}/Application/chrome.exe" for base in (local, pf, pf86)]

    def edge(folder: str) -> list[Path]:
        return [base / f"Microsoft/{folder}/Application/msedge.exe" for base in (pf86, pf, local)]

    return {
        "chrome": chrome("Chrome"),
        "edge": edge("Edge"),
        "brave": [base / "BraveSoftware/Brave-Browser/Application/brave.exe" for base in (local, pf, pf86)],
        "chromium": [local / "Chromium/Application/chrome.exe", pf / "Chromium/Application/chrome.exe"],
        "chrome-beta": chrome("Chrome Beta"),
        "chrome-dev": chrome("Chrome Dev"),
        "chrome-canary": [local / "Google/Chrome SxS/Application/chrome.exe"],  # Canary installs per user only
        "edge-beta": edge("Edge Beta"),
        "edge-dev": edge("Edge Dev"),
        "edge-canary": [local / "Microsoft/Edge SxS/Application/msedge.exe"],
    }


def _mac_candidates() -> dict[str, list[Path]]:
    apps = [Path("/Applications"), Path.home() / "Applications"]
    names = {
        "chrome": "Google Chrome",
        "edge": "Microsoft Edge",
        "brave": "Brave Browser",
        "chromium": "Chromium",
        "chrome-beta": "Google Chrome Beta",
        "chrome-dev": "Google Chrome Dev",
        "chrome-canary": "Google Chrome Canary",
        "edge-beta": "Microsoft Edge Beta",
        "edge-dev": "Microsoft Edge Dev",
        "edge-canary": "Microsoft Edge Canary",
    }
    return {k: [a / f"{v}.app/Contents/MacOS/{v}" for a in apps] for k, v in names.items()}


def _linux_candidates() -> dict[str, list[Path]]:
    def which(*names: str) -> list[Path]:
        return [Path(p) for n in names if (p := shutil.which(n))]

    return {
        "chrome": which("google-chrome", "google-chrome-stable"),
        "edge": which("microsoft-edge", "microsoft-edge-stable"),
        "brave": which("brave-browser", "brave"),
        "chromium": which("chromium", "chromium-browser"),
        "chrome-beta": which("google-chrome-beta"),
        "chrome-dev": which("google-chrome-unstable"),  # Linux calls Chrome's Dev channel "unstable"
        "chrome-canary": which("google-chrome-canary"),
        "edge-beta": which("microsoft-edge-beta"),
        "edge-dev": which("microsoft-edge-dev"),
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

    ``preference`` is ``auto`` (the first installed of :data:`BROWSER_KINDS`: Chrome, Edge, Brave,
    Chromium, then the pre-release channels), a kind name, or an absolute path. ``override`` (from
    config or ``PROFILEPILOT_BROWSER``) wins when preference is ``auto``.
    """
    pref = (preference or "auto").strip()
    env_override = override or os.environ.get(ENV_BROWSER)
    if pref == "auto" and env_override:
        pref = env_override

    if pref != "auto" and pref not in BROWSER_KINDS:
        path = Path(pref).expanduser()
        if path.is_file():
            kind = _kind_from_path(path)
            return BrowserInfo(kind, str(path), browser_version(path))
        raise BrowserNotFoundError(f"Browser executable not found: {pref}")

    cands = _candidates()
    order = list(BROWSER_KINDS) if pref == "auto" else [pref]
    for kind in order:
        for path in cands.get(kind, []):
            if path.is_file():
                return BrowserInfo(kind, str(path), browser_version(path))
    if pref == "auto":
        raise BrowserNotFoundError(
            "Could not find a Chromium-family browser (Chrome, Edge, Brave or Chromium). Install Google Chrome, "
            f"or set {ENV_BROWSER} / the 'browser' field of the profile to the full path of chrome.exe."
        )
    installed = ", ".join(b.kind for b in list_browsers()) or "none"
    raise BrowserNotFoundError(
        f"Could not find {BROWSER_LABELS.get(pref, pref)} ('{pref}') on this computer (installed: {installed}). "
        "Install it or choose an installed browser for the profile."
    )


def list_browsers() -> list[BrowserInfo]:
    """The installed browsers ProfilePilot can run, one per kind, in :data:`BROWSER_KINDS` order."""
    found: list[BrowserInfo] = []
    cands = _candidates()
    for kind in BROWSER_KINDS:
        for path in cands.get(kind, []):
            if path.is_file():
                found.append(BrowserInfo(kind, str(path), browser_version(path)))
                break
    return found


def _channel(parts: tuple[str, ...]) -> str | None:
    """The pre-release channel named by an install folder, app bundle or binary (``Chrome SxS``,
    ``Google Chrome Beta.app``, ``google-chrome-unstable``, ``msedge-dev`` ...), if any."""
    for part in parts:
        stem = part.lower().removesuffix(".app").removesuffix(".exe")
        if stem in ("chrome sxs", "edge sxs") or stem.endswith((" canary", "-canary")):
            return "canary"
        if stem.endswith((" dev", "-dev", "-unstable")):
            return "dev"
        if stem.endswith((" beta", "-beta")):
            return "beta"
    return None


def _kind_from_path(path: Path) -> str:
    name = path.name.lower()
    parts = path.parts[-4:]  # the executable and the folders that name its channel
    if "msedge" in name or "edge" in name:
        family = "edge"
    elif "brave" in name:
        return "brave"
    elif any("chromium" in part.lower() for part in parts):
        return "chromium"
    else:
        family = "chrome"
    channel = _channel(parts)
    return f"{family}-{channel}" if channel else family
