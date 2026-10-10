"""Window icons: the Manager's logo and a per-profile icon on the browser windows (Windows).

Each profile's browser windows get the ProfilePilot tile in the profile's own colour (the hue of its
avatar in the Manager) with the first characters of its name, and their own taskbar group: a
``System.AppUserModel.ID`` per profile with that icon as ``RelaunchIconResource``, the same per-window
properties Chrome sets for its own profile-badged icons. The Manager's app window gets the plain logo
the same way. Tiles are drawn with :mod:`profilepilot.ui.shortcut`'s rasteriser, the letters with GDI
(Windows only, where the icons are applied; elsewhere the tile has no letters).
"""

from __future__ import annotations

import colorsys
import hashlib
import logging
import re
import struct
import sys
from pathlib import Path
from typing import Sequence

from .ui import shortcut

log = logging.getLogger("profilepilot.winicon")

WINDOW_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96)
MANAGER_APP_ID = shortcut.APP_ID
_ICON_VERSION = "1"  # bump when the drawing changes, so cached icons are redrawn


def name_hue(seed: str) -> int:
    """The Manager's avatar hue for ``seed`` (``dom.js`` ``hue()``): profiles keep their colour."""
    value = 0
    for ch in str(seed or ""):
        value = (value * 31 + ord(ch)) & 0xFFFFFFFF
    return value % 360


def badge_text(name: str, count: int) -> str:
    """The first ``count`` letters or digits of ``name``, upper-cased (``shop-us`` -> ``SHO``)."""
    letters = re.sub(r"[\W_]+", "", str(name or ""))
    return (letters or str(name or "").strip() or "?")[:count].upper()


def _hsl(hue: float, sat: float, light: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb((hue % 360) / 360.0, light, sat)
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def _tile_rgba(size: int, top_left: tuple[int, int, int], bottom_right: tuple[int, int, int], *,
               arrow_scale: float = 1.0, arrow_dy: float = 0.0, arrow: bool = True) -> bytearray:
    """The logo tile (``shortcut.logo_rgba`` geometry) in other colours, the arrow scaled and moved."""
    pts = [(0.5 + (x - 0.5) * arrow_scale, 0.5 + (y - 0.5) * arrow_scale + arrow_dy) for x, y in shortcut.ARROW]
    px_unit = 1.0 / size
    out = bytearray(size * size * 4)
    for y in range(size):
        fy = (y + 0.5) * px_unit
        for x in range(size):
            fx = (x + 0.5) * px_unit
            tile_cov = min(1.0, max(0.0, 0.5 - shortcut._sd_round_box(fx, fy, 0.5, shortcut.TILE_RADIUS) / px_unit))
            if tile_cov <= 0.0:
                continue
            t = min(1.0, max(0.0, (fx + fy) / 2.0))
            rgb = [a + (b - a) * t for a, b in zip(top_left, bottom_right)]
            hl = max(0.0, 0.35 - fy) * 0.25
            rgb = [c + (255 - c) * hl for c in rgb]
            if arrow:
                cov = min(1.0, max(0.0, 0.5 - (shortcut._sd_polygon(fx, fy, pts) - shortcut.ARROW_ROUND * arrow_scale)
                                   / px_unit))
                rgb = [c + (255 - c) * cov for c in rgb]
            i = (y * size + x) * 4
            out[i:i + 4] = bytes((*(int(round(c)) for c in rgb), int(round(255 * tile_cov))))
    return out


def _text_coverage(text: str, size: int, box: tuple[int, int, int, int], height: int) -> list[float] | None:
    """Anti-aliased coverage (0..1 per pixel of a ``size`` square) of ``text`` in bold Segoe UI,
    centred in ``box`` (left, top, right, bottom) and shrunk until it fits. None off Windows."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    gdi32, user32 = ctypes.WinDLL("gdi32"), ctypes.WinDLL("user32")
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.CreateFontW.restype = wintypes.HFONT
    gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    gdi32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.SIZE)]
    user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.RECT), wintypes.UINT]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    dc = gdi32.CreateCompatibleDC(None)
    bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    if not dc or not bitmap or not bits.value:
        if dc:
            gdi32.DeleteDC(dc)
        return None
    old_bitmap = gdi32.SelectObject(dc, bitmap)
    font = None
    try:
        left, top, right, bottom = box
        extent = wintypes.SIZE()
        for px in range(height, 5, -1):  # the largest size whose text fits the box
            if font:
                gdi32.DeleteObject(font)
            font = gdi32.CreateFontW(-px, 0, 0, 0, 800, 0, 0, 0, 1, 0, 0, 4, 0, "Segoe UI")  # ANTIALIASED_QUALITY
            gdi32.SelectObject(dc, font)
            gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(extent))
            if extent.cx <= right - left:
                break
        gdi32.SetBkMode(dc, 1)  # TRANSPARENT
        gdi32.SetTextColor(dc, 0x00FFFFFF)
        rect = wintypes.RECT(left, top, right, bottom)
        user32.DrawTextW(dc, text, -1, ctypes.byref(rect), 0x1 | 0x4 | 0x20 | 0x800)  # CENTER|VCENTER|SINGLELINE|NOPREFIX
        gdi32.GdiFlush()
        raw = ctypes.string_at(bits.value, size * size * 4)
        return [max(raw[i], raw[i + 1], raw[i + 2]) / 255.0 for i in range(0, len(raw), 4)]
    finally:
        gdi32.SelectObject(dc, old_bitmap)
        if font:
            gdi32.DeleteObject(font)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(dc)


def profile_icon_rgba(size: int, name: str, seed: str) -> bytes:
    """A profile's icon: the tile in its avatar colour, the arrow and up to three letters of its
    name (from 32 px; smaller icons show two big letters only, the arrow would be unreadable)."""
    hue = name_hue(seed)
    top_left, bottom_right = _hsl(hue, 0.55, 0.40), _hsl(hue + 35, 0.60, 0.32)
    small = size < 32
    pixels = _tile_rgba(size, top_left, bottom_right, arrow=not small, arrow_scale=0.62, arrow_dy=-0.17)
    if small:
        coverage = _text_coverage(badge_text(name, 2), size, (0, 0, size, size), int(size * 0.66))
    else:
        coverage = _text_coverage(badge_text(name, 3), size,
                                  (int(size * 0.08), int(size * 0.56), size - int(size * 0.08), int(size * 0.94)),
                                  int(size * 0.36))
    if coverage:
        for p, cov in enumerate(coverage):
            if cov > 0.0 and pixels[p * 4 + 3]:
                i = p * 4
                for c in range(3):
                    pixels[i + c] = int(round(pixels[i + c] + (255 - pixels[i + c]) * cov))
    return bytes(pixels)


def ico_from(images: dict[int, bytes]) -> bytes:
    """A PNG-in-ICO file from ``{size: RGBA pixels}``."""
    sizes = sorted(images)
    pngs = [shortcut.png_bytes(s, images[s]) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    for size, data in zip(sizes, pngs):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return head + entries + b"".join(pngs)


def profile_icon_file(folder: Path, name: str, seed: str, sizes: Sequence[int] = WINDOW_ICON_SIZES) -> Path:
    """``<folder>/window-icon.ico`` for a profile, redrawn only when its letters or colour change."""
    key = hashlib.sha256(f"{_ICON_VERSION}|{badge_text(name, 3)}|{name_hue(seed)}|{sys.platform == 'win32'}|"
                         f"{','.join(map(str, sizes))}".encode()).hexdigest()[:16]
    path, stamp = folder / "window-icon.ico", folder / "window-icon.key"
    try:
        if path.is_file() and stamp.read_text(encoding="utf-8").strip() == key:
            return path
    except OSError:
        pass
    folder.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ico_from({s: profile_icon_rgba(s, name, seed) for s in sizes}))
    stamp.write_text(key, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- applying (Windows)


def _browser_windows(pid: int) -> list[int]:
    """Top-level browser windows (``Chrome_WidgetWin_*`` with a title) of process ``pid``."""
    import win32gui  # type: ignore[import-not-found]
    import win32process  # type: ignore[import-not-found]

    found: list[int] = []

    def visit(hwnd: int, _extra: object) -> bool:
        try:
            if (win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd)) \
                    and win32process.GetWindowThreadProcessId(hwnd)[1] == pid \
                    and win32gui.GetClassName(hwnd).startswith("Chrome_WidgetWin") and win32gui.GetWindowText(hwnd) \
                    and not win32gui.GetWindow(hwnd, 4):  # GW_OWNER: no owned popups/bubbles
                found.append(hwnd)
        except Exception:  # a window that went away mid-enumeration
            pass
        return True

    win32gui.EnumWindows(visit, None)
    return found


class WindowIcons:
    """Puts one icon and one taskbar identity on every browser window of a process. Call
    :meth:`apply` periodically: new windows are picked up, and a window whose icon the browser put
    back (an app window's favicon changing) gets ours again. Never raises."""

    def __init__(self, icon: Path, app_id: str, display_name: str, relaunch_command: str | None = None) -> None:
        self.icon = Path(icon)
        self.app_id = app_id
        self.display_name = display_name
        self.relaunch_command = relaunch_command
        self._small = self._big = None
        self._disabled = sys.platform != "win32"

    def _load(self) -> bool:
        if self._big is None:
            import win32api  # type: ignore[import-not-found]
            import win32con  # type: ignore[import-not-found]
            import win32gui  # type: ignore[import-not-found]

            def load(metric_x: int, metric_y: int) -> int:
                return win32gui.LoadImage(0, str(self.icon), win32con.IMAGE_ICON, win32api.GetSystemMetrics(metric_x),
                                          win32api.GetSystemMetrics(metric_y), win32con.LR_LOADFROMFILE)

            self._small = load(win32con.SM_CXSMICON, win32con.SM_CYSMICON)
            self._big = load(win32con.SM_CXICON, win32con.SM_CYICON)
        return bool(self._big)

    def _set_properties(self, hwnd: int) -> None:
        import pythoncom  # type: ignore[import-not-found]
        from win32com.propsys import propsys, pscon  # type: ignore[import-not-found]

        try:
            pythoncom.CoInitialize()
        except pythoncom.com_error:
            pass
        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        if self.relaunch_command:  # relaunch details first: Windows reads them when the ID is set
            store.SetValue(pscon.PKEY_AppUserModel_RelaunchCommand, propsys.PROPVARIANTType(self.relaunch_command))
            store.SetValue(pscon.PKEY_AppUserModel_RelaunchDisplayNameResource,
                           propsys.PROPVARIANTType(self.display_name))
        store.SetValue(pscon.PKEY_AppUserModel_RelaunchIconResource, propsys.PROPVARIANTType(f"{self.icon},0"))
        store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(self.app_id))
        store.Commit()

    def apply(self, pids: int | Sequence[int]) -> int:
        """Give every browser window of ``pids`` the icon; returns how many windows were changed."""
        if self._disabled:
            return 0
        changed = 0
        try:
            import win32con  # type: ignore[import-not-found]
            import win32gui  # type: ignore[import-not-found]

            if not self._load():
                return 0
            for pid in [pids] if isinstance(pids, int) else list(pids):
                for hwnd in _browser_windows(pid):
                    try:
                        _ok, current = win32gui.SendMessageTimeout(hwnd, win32con.WM_GETICON, win32con.ICON_BIG, 0,
                                                                   win32con.SMTO_ABORTIFHUNG, 500)
                        if current == self._big:
                            continue
                        for kind, handle in ((win32con.ICON_SMALL, self._small), (win32con.ICON_BIG, self._big)):
                            win32gui.SendMessageTimeout(hwnd, win32con.WM_SETICON, kind, handle,
                                                        win32con.SMTO_ABORTIFHUNG, 500)
                        self._set_properties(hwnd)
                        changed += 1
                    except Exception as exc:  # a window that closed, or a hung browser: next time
                        log.debug("window icon for %s not set: %s", hwnd, exc)
        except Exception as exc:  # pywin32 missing or broken: icons are a nicety, never an error
            log.debug("window icons disabled: %s", exc)
            self._disabled = True
        return changed


def profile_window_icons(store: object, profile: object) -> WindowIcons | None:
    """The :class:`WindowIcons` for a profile's browser (Windows), its icon file cached in the profile folder."""
    if sys.platform != "win32":
        return None
    try:
        icon = profile_icon_file(store.profile_dir(profile.id), profile.name, profile.id)  # type: ignore[attr-defined]
    except Exception as exc:
        log.debug("profile icon not drawn: %s", exc)
        return None
    python = shortcut._gui_python()
    return WindowIcons(icon, f"ProfilePilot.Profile.{profile.id}", profile.name,  # type: ignore[attr-defined]
                       f'"{python}" -m profilepilot profile start {profile.id}')  # type: ignore[attr-defined]


__all__ = ["MANAGER_APP_ID", "WindowIcons", "badge_text", "ico_from", "name_hue", "profile_icon_file",
           "profile_icon_rgba", "profile_window_icons"]
