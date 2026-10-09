"""Typing engine: human timing model, paste landing checks, and real-Chrome behaviour of every
method (fill / type / human / paste and its fallback).

Only ``test_paste_with_the_real_clipboard`` uses the user's real clipboard; it runs inside
:func:`tests.fakes.user_clipboard_guard`, which saves the user's clipboard first and always puts it
back. All values are obvious test data.
"""

from __future__ import annotations

import random
import statistics
import struct
import time
import types

import pytest
import pytest_asyncio

from profilepilot.automation import clipboard
from profilepilot.automation import typing as typing_mod
from profilepilot.automation.clipboard import ClipboardUnavailable, clipboard_text, paste_shortcut
from profilepilot.automation.typing import (
    INTERVAL_CLAMP,
    NotTypeableError,
    TextEntryError,
    enter_text,
    plan_keystrokes,
    typing_speed,
    value_landed,
)

from .fakes import TEST_DIB, OriginServer, clipboard_text_now, user_clipboard_guard

TEST_CARD = "4242424242424242"
TEST_SSN = "000-12-3456"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Typing</title></head><body>
<input id="a"> <input id="b"> <input id="blocked"> <input id="d" type="date"> <input id="off" disabled>
<select id="s"><option>x</option></select> <input id="cb" type="checkbox">
<div id="ce" contenteditable="true" style="width:200px;height:20px;border:1px solid #888"></div>
<textarea id="ta"></textarea>
<input id="ro" readonly> <input id="hid" style="display:none"> <input id="ad" aria-disabled="true">
<fieldset disabled><input id="fsd"></fieldset>
<div style="position:relative;display:inline-block;width:220px">
  <input id="fl" style="width:200px">
  <label for="fl" style="position:absolute;left:0;top:0;width:100%;height:100%;background:#fff">Floating label</label>
</div>
<script>
window.log = [];
for (const t of ['keydown', 'keyup', 'paste', 'beforeinput', 'input']) {
  document.addEventListener(t, e => log.push({t, key: e.key || null, trusted: e.isTrusted, shift: !!e.shiftKey,
    inputType: e.inputType || null, target: e.target.id, ts: performance.now()}), true);
}
document.getElementById('blocked').addEventListener('paste', e => e.preventDefault());
</script></body></html>"""


# ---------------------------------------------------------------------- unit: timing model


def _intervals(plan) -> list[float]:
    """keydown-to-keydown intervals implied by a plan."""
    return [plan[i - 1].hold + plan[i].wait for i in range(1, len(plan))]


def test_plan_is_deterministic_and_has_no_typos():
    a = plan_keystrokes("Testy McTestface", random.Random(7))
    b = plan_keystrokes("Testy McTestface", random.Random(7))
    assert a == b and "".join(k.char for k in a) == "Testy McTestface"
    assert plan_keystrokes("Testy", random.Random(8)) != plan_keystrokes("Testy", random.Random(7))[:5]


def test_intervals_follow_the_configured_distribution():
    text = "abcdefghij" * 300  # no spaces/punctuation: pure inter-key intervals
    plan = plan_keystrokes(text, random.Random(5))
    cpm = typing_speed(random.Random(5))  # the same first draw as the plan's
    mean = 60.0 / cpm
    assert 140 <= cpm <= 260
    intervals = _intervals(plan)
    # log-normal around the mean (+ ~2 % rare thinking pauses of 0.3-0.9 s)
    assert mean * 0.95 <= statistics.mean(intervals) <= mean * 1.15
    assert statistics.median(intervals) < statistics.mean(intervals)  # right-skewed
    thinking = [x for x in intervals if x > INTERVAL_CLAMP[1] * mean]
    assert 0 < len(thinking) < len(intervals) * 0.05
    assert all(x >= INTERVAL_CLAMP[0] * mean - 1e-9 for x in intervals)
    assert all(x <= INTERVAL_CLAMP[1] * mean + 0.9 + 1e-9 for x in intervals)
    assert 0.15 <= plan[0].wait <= 0.6
    assert all(0.03 <= k.hold <= 0.16 for k in plan)


def test_wpm_and_pauses_after_spaces_and_punctuation():
    plan = plan_keystrokes("ab" * 1500, random.Random(3), wpm=60)  # 60 wpm = 300 cpm = 0.2 s/char
    assert 0.18 <= statistics.mean(_intervals(plan)) <= 0.24
    text = "ab cd, ef. gh ij" * 150
    plan = plan_keystrokes(text, random.Random(4))
    after_space = [plan[i - 1].hold + plan[i].wait for i in range(1, len(text)) if text[i - 1] == " "]
    after_punct = [plan[i - 1].hold + plan[i].wait for i in range(1, len(text)) if text[i - 1] in ",."]
    plain = [plan[i - 1].hold + plan[i].wait for i in range(1, len(text)) if text[i - 1].isalpha()]
    assert statistics.median(after_space) > statistics.median(plain) * 1.15
    assert statistics.median(after_punct) > statistics.median(after_space)
    with pytest.raises(ValueError):
        plan_keystrokes("x", random.Random(1), wpm=0)


def test_value_landed_tolerates_real_world_inputs():
    assert value_landed("Testy", "", "Testy")
    assert value_landed("4242 4242 4242 4242", "", TEST_CARD)  # formatting mask
    assert value_landed("(555) 010-0000", "", "+1 555 010 0000")  # mask dropped the country code
    assert value_landed("TESTVILLE", "", "Testville")  # upper-casing
    assert value_landed("Testy McT", "", "Testy McTestface", max_length=9)  # maxlength truncation
    assert value_landed("Hello Testy", "Hello ", "Testy")  # appended
    assert not value_landed("", "", "Testy")  # nothing arrived
    assert not value_landed("4", "", TEST_CARD)  # a fraction is not a paste
    assert not value_landed("Hello", "Hello", "Testy")


def test_errors_are_scrubbed_of_the_text():
    err = typing_mod._entry_error(RuntimeError(f"Locator.fill: Timeout\n  - fill(\"{TEST_CARD}\")"), TEST_CARD, "x")
    assert TEST_CARD not in str(err)
    err = typing_mod._entry_error(RuntimeError(f"bad value {TEST_SSN} here"), TEST_SSN, "x")
    assert TEST_SSN not in str(err) and "•••" in str(err)


# ---------------------------------------------------------------------- real Chrome


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    from .chrome_helper import launch_chrome

    with launch_chrome(tmp_path_factory.mktemp("typing") / "udd") as launched:
        yield launched


@pytest.fixture(scope="module")
def origin():
    with OriginServer({"/": PAGE}) as server:
        yield server


@pytest_asyncio.fixture
async def page(chrome, origin):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(chrome.http_url, no_defaults=True)
        tab = await browser.contexts[0].new_page()
        await tab.goto(origin.url + "/")
        try:
            yield tab
        finally:
            await tab.close()
            await browser.close()


class NoClipboard:
    """Backend that is never usable (and so never touches the real clipboard)."""

    name = "none"
    calls = 0

    def snapshot_and_set(self, text, *, sensitive):
        NoClipboard.calls += 1
        raise ClipboardUnavailable("The clipboard is held open by another program.")

    def restore(self, snapshot, token):  # pragma: no cover - never reached
        return True


@pytest.fixture
def no_clipboard():
    NoClipboard.calls = 0
    clipboard.set_backend(NoClipboard())
    try:
        yield NoClipboard
    finally:
        clipboard.set_backend(None)


async def _log(page, target: str | None = None, types: tuple[str, ...] | None = None) -> list[dict]:
    entries = await page.evaluate("log")
    return [e for e in entries if (target is None or e["target"] == target) and (types is None or e["t"] in types)]


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_human_typing_sends_every_key_with_the_planned_timing(page, tmp_path, no_clipboard):
    text = "Hi Testy, ok!é"
    used = await enter_text(page, page.locator("#a"), text, method="human", clipboard_lock=tmp_path / "cb.lock",
                            rng=random.Random(11))
    assert used == "human" and await page.input_value("#a") == text
    plan = plan_keystrokes(text, random.Random(11))
    keys = await _log(page, "a", ("keydown", "keyup"))
    cleared = max(i for i, e in enumerate(keys) if e["key"] == "Backspace")  # Ctrl+A, Backspace come first
    downs = [e for e in keys[cleared + 1:] if e["t"] == "keydown" and e["key"] != "Shift"]
    ups = [e for e in keys[cleared + 1:] if e["t"] == "keyup" and e["key"] != "Shift"]
    ascii_chars = [c for c in text if " " <= c <= "~"]
    assert [e["key"] for e in downs] == ascii_chars and [e["key"] for e in ups] == ascii_chars
    assert all(e["trusted"] for e in downs + ups)
    for e in downs:  # Shift is held for shifted characters, like a person would
        assert e["shift"] == (e["key"] in "HT!")
    # observed keydown-to-keydown intervals follow the plan (seeded rng), within timer jitter
    for i in range(1, len(ascii_chars)):
        expected = plan[i - 1].hold + plan[i].wait
        observed = (downs[i]["ts"] - downs[i - 1]["ts"]) / 1000
        assert abs(observed - expected) <= max(0.06, 0.3 * expected), (i, observed, expected)
    assert no_clipboard.calls == 0


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_fill_type_append_and_non_text_controls(page, tmp_path, no_clipboard, monkeypatch):
    lock = tmp_path / "cb.lock"
    assert await enter_text(page, page.locator("#a"), "Testy", method="fill", clipboard_lock=lock) == "fill"
    assert await page.input_value("#a") == "Testy" and not await _log(page, "a", ("keydown",))
    assert await enter_text(page, page.locator("#a"), " Mc", method="fill", clear=False, clipboard_lock=lock) == "fill"
    assert await page.input_value("#a") == "Testy Mc"
    assert await enter_text(page, page.locator("#a"), "Testface", method="human", clear=False,
                            clipboard_lock=lock, rng=random.Random(2), wpm=300) == "human"
    assert await page.input_value("#a") == "Testy McTestface"

    assert await enter_text(page, page.locator("#b"), "McTestface", method="type", clipboard_lock=lock) == "type"
    assert await page.input_value("#b") == "McTestface"
    typed = [e["key"] for e in await _log(page, "b", ("keydown",)) if e["key"] != "Delete"]  # fill("") clears
    assert typed == list("McTestface")

    # date inputs always get fill (and never the clipboard), selects/checkboxes are refused
    assert await enter_text(page, page.locator("#d"), "1990-03-14", method="paste", clipboard_lock=lock) == "fill"
    assert await page.input_value("#d") == "1990-03-14"
    for selector in ("#s", "#cb"):
        with pytest.raises(NotTypeableError):
            await enter_text(page, page.locator(selector), "x", method="paste", clipboard_lock=lock)

    # contenteditable and a textarea with a newline (inserted, never Enter)
    assert await enter_text(page, page.locator("#ce"), "Example Test Co", method="type", clipboard_lock=lock) == "type"
    assert await page.inner_text("#ce") == "Example Test Co"
    handle = await page.query_selector("#ta")  # ElementHandles work as well as locators
    assert await enter_text(page, handle, "123 Test Street\nApt 4", method="human", clipboard_lock=lock,
                            rng=random.Random(3), wpm=400) == "human"
    assert await page.input_value("#ta") == "123 Test Street\nApt 4"

    # Playwright errors echo the value in their call log: it must not reach our message
    monkeypatch.setattr(typing_mod, "ACTION_TIMEOUT_MS", 500)
    with pytest.raises(TextEntryError) as exc:
        await enter_text(page, page.locator("#off"), TEST_CARD, method="fill", clipboard_lock=lock)
    assert TEST_CARD not in str(exc.value)
    assert no_clipboard.calls == 0


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_paste_falls_back_to_human_typing_when_the_clipboard_is_unavailable(page, tmp_path, no_clipboard):
    used = await enter_text(page, page.locator("#a"), "Testy McTestface", method="paste",
                            clipboard_lock=tmp_path / "cb.lock", rng=random.Random(4), wpm=400)
    assert used.startswith("human (paste failed: clipboard unavailable")
    assert await page.input_value("#a") == "Testy McTestface"
    assert not await _log(page, "a", ("paste",)) and no_clipboard.calls == 1


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_paste_with_the_real_clipboard(page, tmp_path):
    """Trusted paste events, exclusion formats while sensitive text is on the clipboard, restore
    of the user's text and image, fallback when the page blocks paste, and a newer copy made by
    the user during a paste is kept. The user's own clipboard is restored by the guard."""
    lock = tmp_path / "cb.lock"
    sentinel = "PP-TEST-SENTINEL-CLIPBOARD"
    await page.click("#b")  # warm up before taking the clipboard (keeps the window short)
    with user_clipboard_guard(sentinel) as backend:
        # 1. paste: a real, trusted paste with insertFromPaste input events
        used = await enter_text(page, page.locator("#a"), "Testy McTestface", method="paste", clipboard_lock=lock,
                                rng=random.Random(5))
        assert used == "paste" and await page.input_value("#a") == "Testy McTestface"
        events = await _log(page, "a", ("paste", "beforeinput", "input"))
        events = events[next(i for i, e in enumerate(events) if e["t"] == "paste"):]  # after the clearing keys
        assert [e["t"] for e in events] == ["paste", "beforeinput", "input"]
        assert all(e["trusted"] for e in events)
        assert [e["inputType"] for e in events[1:]] == ["insertFromPaste", "insertFromPaste"]
        assert clipboard_text_now(backend) == sentinel
        assert (backend.current_formats().get("8") or b"")[:len(TEST_DIB)] == TEST_DIB  # image restored too

        # 2. sensitive text carries the history / cloud / monitor exclusion formats
        with clipboard_text(TEST_SSN, sensitive=True, lock_path=lock):
            formats = backend.current_formats()
            assert clipboard_text_now(backend) == TEST_SSN
            for name in ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard",
                         "ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore"):
                assert formats.get(name) == struct.pack("<I", 0), name
            await page.click("#b")
            await page.keyboard.press(paste_shortcut())
            assert await page.input_value("#b") == TEST_SSN
        assert clipboard_text_now(backend) == sentinel

        # 3. a page that blocks paste gets the text typed instead
        used = await enter_text(page, page.locator("#blocked"), "Testville", method="paste", clipboard_lock=lock,
                                rng=random.Random(6), wpm=400)
        assert used == "human (paste failed: the page did not accept the paste)"
        assert await page.input_value("#blocked") == "Testville"
        assert clipboard_text_now(backend) == sentinel

        # 4. the user copies something while a paste holds the clipboard: their copy wins
        with clipboard_text("PP-TEST-OWN-TEXT", sensitive=False, lock_path=lock):
            backend.set_formats({13: "PP-TEST-NEWER-COPY\0".encode("utf-16-le"),
                                 backend._fmt("CanIncludeInClipboardHistory"): b"\0\0\0\0"})
        assert clipboard_text_now(backend) == "PP-TEST-NEWER-COPY"
    after = clipboard_text_now(clipboard.WindowsClipboard())
    assert after is None or not after.startswith("PP-TEST")


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_keys_never_go_to_another_field(page, tmp_path, no_clipboard, monkeypatch):
    """A disabled, read-only or hidden target must not get the keys: they would land in whatever
    field had the focus before (cleared first by select-all + Backspace)."""
    monkeypatch.setattr(typing_mod, "ACTION_TIMEOUT_MS", 1_000)  # fill() waits for a hidden field to show up
    lock = tmp_path / "cb.lock"
    await enter_text(page, page.locator("#a"), "Testy", method="fill", clipboard_lock=lock)
    for selector in ("#off", "#fsd", "#ro", "#hid"):
        for method, clear in (("human", True), ("paste", True), ("type", False), ("fill", False)):
            await page.click("#a")  # the previously focused field
            with pytest.raises(TextEntryError) as exc:
                await enter_text(page, page.locator(selector), TEST_SSN, method=method, clear=clear,
                                 clipboard_lock=lock, rng=random.Random(1), wpm=900)
            assert TEST_SSN not in str(exc.value)
            assert await page.input_value("#a") == "Testy", (selector, method)
    assert await page.input_value("#ro") == "" and no_clipboard.calls == 0
    # aria-disabled is only a hint: typing really lands there, so it is not refused
    assert await enter_text(page, page.locator("#ad"), "Testy", method="human", clipboard_lock=lock,
                            rng=random.Random(2), wpm=900) == "human"
    assert await page.input_value("#ad") == "Testy"


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_type_without_clear_appends_at_the_end(page, tmp_path, no_clipboard):
    lock = tmp_path / "cb.lock"
    await enter_text(page, page.locator("#b"), "Testy", method="fill", clipboard_lock=lock)
    assert await enter_text(page, page.locator("#b"), " Mc", method="type", clear=False, clipboard_lock=lock) == "type"
    assert await page.input_value("#b") == "Testy Mc"


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_a_floating_label_over_the_field_costs_no_click_timeout(page, tmp_path, no_clipboard):
    started = time.monotonic()
    used = await enter_text(page, page.locator("#fl"), "Testy", method="human", clipboard_lock=tmp_path / "cb.lock",
                            rng=random.Random(3), wpm=900)
    assert used == "human" and await page.input_value("#fl") == "Testy"
    assert time.monotonic() - started < 2.5  # the full click timeout alone was 5 s


# ---------------------------------------------------------------------- unit: fakes (no Chrome)


class FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str, **kwargs) -> None:
        self.pressed.append(key)

    async def insert_text(self, text: str) -> None:
        self.pressed.append(text)

    async def down(self, key: str) -> None:
        pass

    async def up(self, key: str) -> None:
        pass


class FakeTarget:
    """An input that is focusable but whose value never changes (the page swallows the paste)."""

    def __init__(self, *, coverable: bool = False) -> None:
        self.coverable = coverable
        self.focused = False
        self.clicks: list[dict] = []

    async def evaluate(self, js: str, *args):
        if js == typing_mod._INFO_JS:
            return {"tag": "input", "type": "text", "editable": False, "disabled": False, "readOnly": False,
                    "maxLength": None}
        if js == typing_mod._FOCUSED_JS:
            return self.focused
        if js == typing_mod._VALUE_JS:
            return ""
        if js == typing_mod._OWN_LABEL_AT_CENTRE_JS:
            return None
        raise AssertionError("unexpected script")

    async def click(self, **kwargs) -> None:
        from playwright.async_api import Error as PlaywrightError

        self.clicks.append(kwargs)
        if self.coverable:
            raise PlaywrightError("element is covered by another element")
        if not kwargs.get("trial"):
            self.focused = True

    async def focus(self) -> None:
        self.focused = True

    async def bounding_box(self):
        return {"x": 0, "y": 0, "width": 100, "height": 20}


class TimedBackend:
    """Fake clipboard that records when the text was set and when the user's content came back."""

    name = "timed"

    def __init__(self) -> None:
        self.events: list[tuple[str, float]] = []

    def snapshot_and_set(self, text, *, sensitive):
        self.events.append(("set", time.monotonic()))
        return types.SimpleNamespace(empty=False), None

    def restore(self, snapshot, token):
        self.events.append(("restore", time.monotonic()))
        return True


@pytest.mark.asyncio
async def test_the_clipboard_holds_the_text_only_for_the_key_press(tmp_path, monkeypatch):
    backend = TimedBackend()
    clipboard.set_backend(backend)

    async def no_typing(*args, **kwargs):
        pass

    monkeypatch.setattr(typing_mod, "type_like_human", no_typing)
    page = types.SimpleNamespace(keyboard=FakeKeyboard())
    try:
        with pytest.raises(TextEntryError, match="did not arrive"):  # nothing ever lands: an error, not success
            await enter_text(page, FakeTarget(), TEST_SSN, method="paste", sensitive=True,
                             clipboard_lock=tmp_path / "cb.lock", rng=random.Random(1))
    finally:
        clipboard.set_backend(None)
    assert [e for e, _ in backend.events] == ["set", "restore", "set", "restore"]
    (_, set1), (_, restore1), (_, set2), (_, restore2) = backend.events
    assert restore1 - set1 < 0.1 and restore2 - set2 < 0.1  # restored right after each chord
    assert set2 - restore1 >= typing_mod.PASTE_LAND_TIMEOUT - 0.1  # the landing wait ran on the user's clipboard
    assert page.keyboard.pressed.count("Control+Shift+V") + page.keyboard.pressed.count("Meta+Shift+V") == 1


@pytest.mark.asyncio
async def test_a_covered_field_is_probed_briefly_then_focused(monkeypatch):
    target = FakeTarget(coverable=True)
    page = types.SimpleNamespace(keyboard=FakeKeyboard())
    await typing_mod._focus_by_click(page, target)
    assert target.focused
    assert target.clicks == [{"trial": True, "timeout": typing_mod.CLICK_PROBE_TIMEOUT_MS}]  # no 5 s click wait

    never = FakeTarget(coverable=True)

    async def refuses_focus() -> None:  # disabled / hidden: focus() is a no-op
        pass

    never.focus = refuses_focus  # type: ignore[method-assign]
    with pytest.raises(TextEntryError, match="Could not focus"):
        await typing_mod._focus_by_click(page, never)
