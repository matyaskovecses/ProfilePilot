"""Desktop / Start-menu shortcuts for ProfilePilot Manager, with a generated icon.

The logo mark (a white navigation arrow on an indigo-violet rounded tile, the same geometry as
``static/logo.svg``) is rasterised here with plain Python: signed distance fields give crisp,
anti-aliased edges at every size, PNGs are written with ``zlib`` + ``struct`` and packed into a
PNG-in-ICO file (Windows Vista and later read PNG entries at every size).

* Windows: ``ProfilePilot Manager.lnk`` on the Desktop and in the Start menu (``WScript.Shell``),
  running ``pythonw.exe -m profilepilot.ui`` (no console window).
* macOS: ``ProfilePilot Manager.command`` on the Desktop.
* Linux: ``profilepilot-manager.desktop`` in ``~/.local/share/applications`` (and on the Desktop).
"""

from __future__ import annotations

import math
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Sequence

APP_NAME = "ProfilePilot Manager"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# Geometry in unit coordinates (0..1, y down), shared with static/logo.svg (viewBox 0 0 256 256).
TILE_INSET = 0.0
TILE_RADIUS = 0.225
ARROW = ((0.73, 0.27), (0.27, 0.47), (0.48, 0.52), (0.53, 0.73))
ARROW_ROUND = 0.025
GRADIENT_FROM = (124, 108, 242)  # top-left  #7c6cf2
GRADIENT_TO = (70, 70, 200)  # bottom-right #4646c8


# --------------------------------------------------------------------------- rasteriser


def _sd_round_box(px: float, py: float, half: float, radius: float) -> float:
    qx = abs(px - 0.5) - (half - radius)
    qy = abs(py - 0.5) - (half - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - radius


def _sd_polygon(px: float, py: float, pts: Sequence[tuple[float, float]]) -> float:
    d = (px - pts[0][0]) ** 2 + (py - pts[0][1]) ** 2
    sign = 1.0
    n = len(pts)
    j = n - 1
    for i in range(n):
        vix, viy = pts[i]
        vjx, vjy = pts[j]
        ex, ey = vjx - vix, vjy - viy
        wx, wy = px - vix, py - viy
        t = max(0.0, min(1.0, (wx * ex + wy * ey) / (ex * ex + ey * ey)))
        bx, by = wx - ex * t, wy - ey * t
        d = min(d, bx * bx + by * by)
        c1, c2, c3 = py >= viy, py < vjy, ex * wy > ey * wx
        if (c1 and c2 and c3) or (not c1 and not c2 and not c3):
            sign = -sign
        j = i
    return sign * math.sqrt(d)


def logo_rgba(size: int) -> bytes:
    """The logo as ``size`` x ``size`` straight-alpha RGBA pixels."""
    if size < 8 or size > 1024:
        raise ValueError("icon size must be 8..1024")
    px_unit = 1.0 / size
    half = 0.5 - TILE_INSET
    out = bytearray(size * size * 4)
    for y in range(size):
        fy = (y + 0.5) * px_unit
        for x in range(size):
            fx = (x + 0.5) * px_unit
            tile = _sd_round_box(fx, fy, half, TILE_RADIUS) / px_unit
            tile_cov = min(1.0, max(0.0, 0.5 - tile))
            if tile_cov <= 0.0:
                continue
            t = min(1.0, max(0.0, (fx + fy) / 2.0))
            r = GRADIENT_FROM[0] + (GRADIENT_TO[0] - GRADIENT_FROM[0]) * t
            g = GRADIENT_FROM[1] + (GRADIENT_TO[1] - GRADIENT_FROM[1]) * t
            b = GRADIENT_FROM[2] + (GRADIENT_TO[2] - GRADIENT_FROM[2]) * t
            # soft top highlight
            hl = max(0.0, 0.35 - fy) * 0.25
            r, g, b = r + (255 - r) * hl, g + (255 - g) * hl, b + (255 - b) * hl
            arrow = (_sd_polygon(fx, fy, ARROW) - ARROW_ROUND) / px_unit
            arrow_cov = min(1.0, max(0.0, 0.5 - arrow))
            r = r + (255 - r) * arrow_cov
            g = g + (255 - g) * arrow_cov
            b = b + (255 - b) * arrow_cov
            i = (y * size + x) * 4
            out[i] = int(round(r))
            out[i + 1] = int(round(g))
            out[i + 2] = int(round(b))
            out[i + 3] = int(round(255 * tile_cov))
    return bytes(out)


def png_bytes(size: int, rgba: bytes | None = None) -> bytes:
    """Encode RGBA pixels (default: the logo) as a PNG."""
    pixels = rgba if rgba is not None else logo_rgba(size)
    stride = size * 4
    raw = b"".join(b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico_bytes(sizes: Sequence[int] = ICON_SIZES) -> bytes:
    """A PNG-in-ICO file with one entry per size."""
    images = [png_bytes(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for size, data in zip(sizes, images):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return head + entries + b"".join(images)


def write_icons(folder: Path) -> dict[str, Path]:
    """Write ``ProfilePilot.ico`` and ``ProfilePilot.png`` (256 px) into ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    ico, png = folder / "ProfilePilot.ico", folder / "ProfilePilot.png"
    ico.write_bytes(ico_bytes())
    png.write_bytes(png_bytes(256))
    return {"ico": ico, "png": png}


# --------------------------------------------------------------------------- shortcuts


def _gui_python(python: str | None = None) -> str:
    exe = Path(python or sys.executable)
    if sys.platform == "win32":
        windowed = exe.with_name("pythonw.exe")
        if windowed.exists():
            return str(windowed)
    return str(exe)


def _launch_args(root: Path) -> list[str]:
    from ..paths import ENV_HOME, data_root

    args = ["-m", "profilepilot.ui"]
    saved = os.environ.pop(ENV_HOME, None)
    try:
        default = data_root()
    finally:
        if saved is not None:
            os.environ[ENV_HOME] = saved
    if Path(root).resolve() != Path(default).resolve():
        args += ["--home", str(Path(root).resolve())]
    return args


def _windows_special_folder(name: str) -> Path | None:
    try:
        import win32com.client  # type: ignore[import-not-found]

        return Path(win32com.client.Dispatch("WScript.Shell").SpecialFolders(name))
    except Exception:
        return None


def default_locations() -> list[Path]:
    """Folders that receive a shortcut on this platform."""
    home = Path.home()
    if sys.platform == "win32":
        desktop = _windows_special_folder("Desktop") or home / "Desktop"
        programs = _windows_special_folder("Programs") or Path(os.environ.get("APPDATA", home)) / \
            "Microsoft" / "Windows" / "Start Menu" / "Programs"
        return [desktop, programs]
    if sys.platform == "darwin":
        return [home / "Desktop"]
    apps = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "applications"
    return [apps] + ([home / "Desktop"] if (home / "Desktop").is_dir() else [])


def _create_lnk(path: Path, target: str, args: list[str], icon: Path, workdir: Path) -> None:
    try:
        import win32com.client  # type: ignore[import-not-found]

        shell = win32com.client.Dispatch("WScript.Shell")
        link = shell.CreateShortCut(str(path))
        link.Targetpath = target
        link.Arguments = subprocess.list2cmdline(args)
        link.WorkingDirectory = str(workdir)
        link.IconLocation = f"{icon},0"
        link.Description = "Manage ProfilePilot browser profiles, proxies and identities"
        link.save()
        return
    except ImportError:
        pass
    # Fallback without pywin32: PowerShell's WScript.Shell COM object (no user input reaches the script).
    def q(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$l = $s.CreateShortcut({q(str(path))}); $l.TargetPath = {q(target)}; "
        f"$l.Arguments = {q(subprocess.list2cmdline(args))}; $l.WorkingDirectory = {q(str(workdir))}; "
        f"$l.IconLocation = {q(str(icon) + ',0')}; $l.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                   capture_output=True, timeout=30)


def install_shortcuts(root: Path, *, folders: Sequence[Path] | None = None, python: str | None = None,
                      platform: str | None = None) -> list[Path]:
    """Create the Manager shortcuts. ``folders`` defaults to :func:`default_locations` (tests pass
    temporary folders). Returns the files written (icons included)."""
    plat = platform or sys.platform
    icons = write_icons(Path(root) / "ui")
    targets = list(folders) if folders is not None else default_locations()
    args = _launch_args(Path(root))
    created: list[Path] = [icons["ico"], icons["png"]]
    if plat == "win32":
        exe = _gui_python(python)
        for folder in targets:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{APP_NAME}.lnk"
            _create_lnk(path, exe, args, icons["ico"], Path.home())
            created.append(path)
    elif plat == "darwin":
        exe = python or sys.executable
        for folder in targets:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{APP_NAME}.command"
            path.write_text("#!/bin/sh\nexec " + " ".join(_sh_quote(a) for a in [exe, *args]) + ' "$@"\n',
                            encoding="utf-8")
            path.chmod(0o755)
            created.append(path)
    else:
        exe = python or sys.executable
        exec_line = " ".join(_desktop_quote(a) for a in [exe, *args])
        content = (
            "[Desktop Entry]\nType=Application\nVersion=1.0\n"
            f"Name={APP_NAME}\nComment=Manage ProfilePilot browser profiles, proxies and identities\n"
            f"Exec={exec_line}\nIcon={icons['png']}\nTerminal=false\nCategories=Network;WebBrowser;Utility;\n"
            "StartupWMClass=ProfilePilotManager\n"
        )
        for folder in targets:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "profilepilot-manager.desktop"
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)
            created.append(path)
    return created


def remove_shortcuts(folders: Sequence[Path] | None = None) -> list[Path]:
    removed: list[Path] = []
    for folder in folders if folders is not None else default_locations():
        for name in (f"{APP_NAME}.lnk", f"{APP_NAME}.command", "profilepilot-manager.desktop"):
            path = folder / name
            if path.exists():
                path.unlink()
                removed.append(path)
    return removed


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _desktop_quote(value: str) -> str:
    if not any(ch in value for ch in ' \t\n"\'\\$`'):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


__all__ = ["ico_bytes", "install_shortcuts", "logo_rgba", "png_bytes", "remove_shortcuts", "write_icons"]
