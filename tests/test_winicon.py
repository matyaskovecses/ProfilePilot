"""Window icons: per-profile icons (colour + letters), the cache, and applying them to real browser
windows (Windows; a throwaway off-screen Chrome, never the user's own windows)."""

from __future__ import annotations

import struct
import sys
import time

import pytest

from profilepilot import winicon


def test_hue_matches_the_managers_avatar_colour():
    # dom.js hue(): hash = hash * 31 + codePoint (uint32), % 360
    assert winicon.name_hue("abc") == ((97 * 31 + 98) * 31 + 99) % 360 == 234
    assert winicon.name_hue("") == 0


def test_badge_text_takes_the_first_letters_of_the_name():
    assert winicon.badge_text("shop-us", 3) == "SHO"
    assert winicon.badge_text("jp market", 2) == "JP"
    assert winicon.badge_text("--x", 3) == "X"
    assert winicon.badge_text("日本の店", 2) == "日本"
    assert winicon.badge_text("", 2) == "?"


@pytest.mark.parametrize("size", [16, 48])
def test_icons_are_tiles_with_letters(size):
    rgba = winicon.profile_icon_rgba(size, "shop-us", "p1")
    assert len(rgba) == size * size * 4
    assert rgba[3] == 0  # rounded corner: transparent
    middle = (size // 2 * size + size // 2) * 4
    assert rgba[middle + 3] == 255
    if sys.platform == "win32":  # the letters are drawn with GDI: some near-white pixels in the lower part
        start = size * (size // 2) * 4 if size >= 32 else 0
        white = sum(1 for i in range(start, len(rgba), 4) if min(rgba[i:i + 3]) > 230 and rgba[i + 3] == 255)
        assert white >= (4 if size < 32 else 20)
    other = winicon.profile_icon_rgba(size, "shop-us", "another-profile-id")
    assert other[middle:middle + 3] != rgba[middle:middle + 3] or size < 32  # another profile, another colour


def test_icon_file_is_cached_and_redrawn_when_the_name_changes(tmp_path):
    first = winicon.profile_icon_file(tmp_path, "shop-us", "p1", sizes=(16, 32))
    data = first.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 2)
    stamp = first.stat().st_mtime_ns
    time.sleep(0.05)
    assert winicon.profile_icon_file(tmp_path, "shop-us", "p1", sizes=(16, 32)).stat().st_mtime_ns == stamp
    winicon.profile_icon_file(tmp_path, "travel-fr", "p1", sizes=(16, 32))
    assert first.read_bytes() != data


@pytest.mark.chrome
@pytest.mark.skipif(sys.platform != "win32", reason="window icons are applied on Windows only")
def test_a_browser_window_gets_the_icon_and_its_own_taskbar_group(tmp_path):
    import win32con
    import win32gui
    from win32com.propsys import propsys, pscon

    from .chrome_helper import launch_chrome

    ico = winicon.profile_icon_file(tmp_path / "icon", "test", "test-profile")
    icons = winicon.WindowIcons(ico, "ProfilePilot.Test.Window", "test")
    with launch_chrome(tmp_path / "udd") as chrome:  # off-screen window
        deadline = time.monotonic() + 15
        changed = 0
        while not changed and time.monotonic() < deadline:
            changed = icons.apply(chrome.proc.pid)
            time.sleep(0.3)
        assert changed >= 1
        hwnd = winicon._browser_windows(chrome.proc.pid)[0]
        _ok, current = win32gui.SendMessageTimeout(hwnd, win32con.WM_GETICON, win32con.ICON_BIG, 0,
                                                   win32con.SMTO_ABORTIFHUNG, 500)
        assert current == icons._big
        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        assert store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue() == "ProfilePilot.Test.Window"
        assert store.GetValue(pscon.PKEY_AppUserModel_RelaunchIconResource).GetValue() == f"{ico},0"
        assert icons.apply(chrome.proc.pid) == 0  # already ours: nothing to do
