"""Text entry into page fields: programmatic fill, key-by-key typing, human-paced typing and
"type-paste" (a real paste from the system clipboard).

:func:`enter_text` is the single entry point; ``method`` picks how the text gets in:

* ``fill`` - ``locator.fill()``: programmatic, no key events (fast; what most automation does).
* ``type`` - ``press_sequentially`` with a fixed 40 ms delay: one key event per character.
* ``human`` - click, select-all + Backspace, then one key per character with human timing:
  log-normal inter-key intervals (about 140-260 characters per minute, or ``wpm``), longer pauses
  after spaces and punctuation, a rare 0.3-0.9 s "thinking" pause, a short wait before the first
  key, Shift held for shifted characters. No typos. All randomness comes from ``rng``.
* ``paste`` - click, select-all + Backspace, put the text on the system clipboard
  (:mod:`.clipboard`: locked, snapshotted, history-excluded, restored) and press Ctrl+Shift+V
  (Cmd+Shift+V on macOS), Chrome's "paste as plain text". The page receives a trusted ``paste``
  event and ``insertFromPaste`` input events, exactly like a person pasting. The text is on the
  clipboard only for the key press itself: the user's clipboard is back before the value is
  awaited. If the value does not land (clipboard unavailable, page blocks paste) it falls back to
  ``human`` and reports ``"human (paste failed: <reason>)"``.

Keys only ever go to the target: disabled and read-only fields are refused up front, focus is
verified after the click (a covered, hidden or disabled field never gets keys meant for it; they
would land in whatever had focus before), again inside the clipboard window right before each
paste chord, and before every human-timed keystroke. A value that did not arrive after typing is an
error, not a success.

The text is never logged (not even its length), and Playwright errors (whose call logs echo filled
values) are re-raised as :class:`TextEntryError` with the text scrubbed out.

Verified on Chrome 154.0.8037.98 / Windows 11 / Playwright 1.63 (CDP ``Input.dispatchKeyEvent``),
with an isolated off-screen profile window (``--window-position=-32000,-32000``):

* A CDP-dispatched ``Control+Shift+V`` pastes the system clipboard into the focused ``<input>`` /
  ``<textarea>`` also while the Chrome window is **not** the OS foreground window (another
  process owned the foreground window for the whole run; ``document.hasFocus()`` stayed true and
  no focus was taken from the user).
* The page sees ``keydown`` (Control, Shift, V), then a ``paste`` event with
  ``isTrusted == true`` whose ``clipboardData`` holds the text, then ``beforeinput`` and ``input``
  with ``inputType == "insertFromPaste"`` (all trusted), then the ``keyup`` events. The value is
  there when ``keyboard.press`` returns.
* ``Control+V`` behaves identically (minus the Shift events). Ctrl+Shift+V is used first anyway and
  Ctrl+V is only a fallback when nothing landed.
* Same result inside a cross-site iframe that Chrome runs out of process (OOPIF; served from
  ``localhost`` inside a ``127.0.0.1`` page): ``page.keyboard`` reaches the focused frame.
* The clipboard-history exclusion formats are present while the text is on the clipboard, and
  Windows honours them: text set with ``CanIncludeInClipboardHistory = 0`` never appeared in
  ``Clipboard.GetHistoryItemsAsync()`` (history enabled), while a control set without it did.
* Windows bumps the clipboard sequence number when the clipboard is *closed*, not when Chrome
  reads it (also not for synthesised formats such as CF_TEXT); :mod:`.clipboard` relies on that.
* Chrome has read the clipboard when ``keyboard.press`` returns: restoring the user's clipboard
  immediately afterwards never changed what the page received, also for ``paste`` handlers that
  call ``preventDefault`` and insert the text themselves, defer their work (``setTimeout``) or
  read ``DataTransferItem.getAsString``. So the text is taken off the clipboard right after the
  chord and the landing wait happens with the user's clipboard already restored.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Union

from ..errors import ProfilePilotError
from .clipboard import ClipboardUnavailable, async_clipboard_text, paste_shortcut, plain_paste_shortcut

if TYPE_CHECKING:
    from playwright.async_api import ElementHandle, Locator, Page

    Target = Union[Locator, ElementHandle]

log = logging.getLogger("profilepilot.typing")

TypeMethod = Literal["fill", "type", "human", "paste"]
TYPE_METHODS: tuple[str, ...] = ("fill", "type", "human", "paste")

ACTION_TIMEOUT_MS = 15_000
CLICK_PROBE_TIMEOUT_MS = 750
"""Actionability / hit-target probe before clicking a field: a field still covered after this
(floating label, cookie banner) is focused through its own label or with focus() instead of
waiting out :data:`CLICK_TIMEOUT_MS`."""
CLICK_TIMEOUT_MS = 5_000
"""A click that passed the probe but still cannot happen within this time falls back to focus()."""
PASTE_LAND_TIMEOUT = 1.5
"""How long to wait for a pasted value to appear in the field."""
TYPE_DELAY_MS = 40

DEFAULT_CPM = (140.0, 260.0)
"""Default human typing speed range, characters per minute (one speed is drawn per call)."""
INTERVAL_SIGMA = 0.35
"""Spread of the log-normal inter-key interval (the median is about 0.94 x the mean)."""
INTERVAL_CLAMP = (0.35, 3.5)
"""Inter-key intervals are kept within these multiples of the mean interval."""
HOLD_MEAN, HOLD_SIGMA, HOLD_CLAMP = 0.075, 0.3, (0.03, 0.16)
"""Key hold time (keydown -> keyup), seconds."""
SPACE_FACTOR = (1.15, 1.6)
PUNCT_FACTOR = (1.4, 2.2)
PUNCTUATION = frozenset(".,;:!?")
THINK_PROBABILITY = 0.02
THINK_PAUSE = (0.3, 0.9)
FIRST_KEY_WAIT = (0.15, 0.6)

US_SHIFTED = frozenset('~!@#$%^&*()_+{}|:"<>?ABCDEFGHIJKLMNOPQRSTUVWXYZ')
"""Characters that need Shift on a US keyboard: Shift is held while they are pressed, as a
person would (``event.shiftKey`` is then true)."""

NON_TEXT_INPUT_TYPES = frozenset({
    "checkbox", "radio", "file", "range", "color", "button", "submit", "reset", "image", "hidden",
})
DATE_LIKE_INPUT_TYPES = frozenset({"date", "month", "time", "week", "datetime-local"})
"""Inputs without a free-text editor: never pasted into or typed key by key; they get ``fill``
(the text must then be in the input's wire format, e.g. ``YYYY-MM-DD``)."""


class TextEntryError(ProfilePilotError):
    """Text could not be entered. The message never contains the text."""


class NotTypeableError(TextEntryError):
    """The target is not a text control (select, checkbox, radio, file, ...)."""


# ---------------------------------------------------------------------- human timing


@dataclass(frozen=True)
class Keystroke:
    """One planned key: wait ``wait`` seconds, then press ``char`` holding it ``hold`` seconds."""

    char: str
    wait: float
    hold: float


def typing_speed(rng: random.Random, wpm: int | None = None) -> float:
    """Characters per minute for one typing run (``wpm`` words = 5 characters each, +-8 %)."""
    if wpm is not None:
        if wpm <= 0:
            raise ValueError("wpm must be positive")
        return wpm * 5.0 * rng.uniform(0.92, 1.08)
    return rng.uniform(*DEFAULT_CPM)


def plan_keystrokes(text: str, rng: random.Random, *, wpm: int | None = None) -> list[Keystroke]:
    """The human typing schedule for ``text`` (deterministic for a seeded ``rng``).

    The keydown-to-keydown interval for character ``i > 0`` is ``hold[i-1] + wait[i]`` and follows
    a log-normal distribution with mean ``60 / cpm`` seconds (clamped to :data:`INTERVAL_CLAMP`
    multiples), stretched after spaces and punctuation, with a rare thinking pause added."""
    cpm = typing_speed(rng, wpm)
    mean = 60.0 / cpm
    mu = math.log(mean) - INTERVAL_SIGMA ** 2 / 2  # log-normal with this mean
    lo, hi = INTERVAL_CLAMP[0] * mean, INTERVAL_CLAMP[1] * mean
    plan: list[Keystroke] = []
    prev_hold = 0.0
    for i, ch in enumerate(text):
        hold = min(max(rng.lognormvariate(math.log(HOLD_MEAN) - HOLD_SIGMA ** 2 / 2, HOLD_SIGMA),
                       HOLD_CLAMP[0]), HOLD_CLAMP[1])
        if i == 0:
            wait = rng.uniform(*FIRST_KEY_WAIT)
        else:
            interval = min(max(rng.lognormvariate(mu, INTERVAL_SIGMA), lo), hi)
            prev = text[i - 1]
            if prev == " ":
                interval *= rng.uniform(*SPACE_FACTOR)
            elif prev in PUNCTUATION:
                interval *= rng.uniform(*PUNCT_FACTOR)
            if rng.random() < THINK_PROBABILITY:
                interval += rng.uniform(*THINK_PAUSE)
            wait = max(interval - prev_hold, 0.005)
        plan.append(Keystroke(ch, wait, hold))
        prev_hold = hold
    return plan


def is_key_char(ch: str) -> bool:
    """Printable ASCII: sent as a key press; anything else is inserted as text."""
    return " " <= ch <= "~"


async def type_like_human(page: "Page", text: str, rng: random.Random, *, wpm: int | None = None,
                          target: "Target | None" = None) -> None:
    """Type ``text`` into the focused element of ``page`` with :func:`plan_keystrokes` timing.

    With ``target``, every key is only sent while ``target`` still has the focus (a page that moves
    the focus mid-typing would otherwise receive the rest of the text in another field)."""
    keyboard = page.keyboard
    loop = asyncio.get_running_loop()
    for stroke in plan_keystrokes(text, rng, wpm=wpm):
        started = loop.time()
        if target is not None and not await is_focused(target):
            raise TextEntryError("The field lost the focus while typing; the rest was not typed.")
        # the check's round trip is part of the planned pause, so the timing stays as planned
        await asyncio.sleep(max(stroke.wait - (loop.time() - started), 0.0))
        ch = stroke.char
        if not is_key_char(ch):
            await keyboard.insert_text(ch)
            await asyncio.sleep(stroke.hold)
        elif ch in US_SHIFTED:
            await keyboard.down("Shift")
            try:
                await keyboard.press(ch, delay=stroke.hold * 1000)
            finally:
                await keyboard.up("Shift")
        else:
            await keyboard.press(ch, delay=stroke.hold * 1000)


# ---------------------------------------------------------------------- element helpers


_INFO_JS = """el => {
  const tag = el.tagName.toLowerCase();
  const type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : '';
  const native = tag === 'input' || tag === 'textarea' || tag === 'select';
  return {tag, type, editable: !native && el.isContentEditable,
          disabled: native && el.matches(':disabled'),
          readOnly: (tag === 'input' || tag === 'textarea') && el.readOnly,
          maxLength: (tag === 'input' || tag === 'textarea') && el.maxLength > 0 ? el.maxLength : null};
}"""
"""``:disabled`` also covers a ``<fieldset disabled>`` ancestor. ``aria-disabled`` is not refused:
typing really lands in such a field."""
_FOCUSED_JS = """el => {
  const a = el.getRootNode().activeElement;
  return !!a && (a === el || el.contains(a) || (el.isContentEditable && a.contains(el)));
}"""
_OWN_LABEL_AT_CENTRE_JS = """el => {
  const r = el.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return null;
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = el.ownerDocument.elementFromPoint(x, y);
  const label = hit && hit.closest ? hit.closest('label') : null;
  return label && label.control === el ? [x, y] : null;
}"""
"""The centre of the field when its own ``<label>`` covers it (floating labels), else null."""
_VALUE_JS = """el => {
  const tag = el.tagName.toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return el.value;
  return el.isContentEditable ? el.innerText : (el.textContent || '');
}"""


async def _evaluate(target: "Target", js: str) -> Any:
    if _is_locator(target):  # a Locator waits for its element: bound the wait
        return await target.evaluate(js, timeout=ACTION_TIMEOUT_MS)  # type: ignore[call-arg]
    return await target.evaluate(js)


async def _control_info(target: "Target") -> dict[str, Any]:
    return await _evaluate(target, _INFO_JS)


async def read_value(target: "Target") -> str:
    """The current value (``innerText`` for contenteditable)."""
    value = await _evaluate(target, _VALUE_JS)
    return value if isinstance(value, str) else ""


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def value_landed(value: str, before: str, text: str, *, max_length: int | None = None) -> bool:
    """Did ``text`` arrive in a field that held ``before`` and now holds ``value``?

    Tolerates what real forms do to input: formatting masks (``4242 4242 ...``, ``(555) 010-0000``),
    case changes, ``maxlength`` truncation and masks that drop characters (a ``+1`` prefix)."""
    if not text:
        return True
    if text in value:
        return True
    want, got = _norm(text), _norm(value)
    if not want:
        return value != before
    if want in got:
        return True
    if value == before:
        return False
    added = got.replace(_norm(before), "", 1) if before else got
    if not added or added not in want:
        return False
    if max_length and len(value) >= max_length:
        return True  # truncated by maxlength
    return len(added) >= 0.6 * len(want)  # a mask dropped a few characters


def _scrub(message: str, text: str) -> str:
    """``message`` (first line) with ``text`` and its compact forms removed."""
    line = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    for needle in {text, text.strip(), re.sub(r"[\s-]", "", text)}:
        if len(needle) >= 2:
            line = line.replace(needle, "•••")
    return line[:300]


def _entry_error(exc: BaseException, text: str, what: str) -> TextEntryError:
    return TextEntryError(f"Could not {what}: {_scrub(str(exc), text) or type(exc).__name__}")


def _is_locator(target: Any) -> bool:
    return hasattr(target, "press_sequentially")


async def is_focused(target: "Target") -> bool:
    """Does ``target`` (or something inside it) have the focus of its document?"""
    return bool(await _evaluate(target, _FOCUSED_JS))


async def _click_own_label(page: "Page", target: "Target") -> bool:
    """When the field's own ``<label>`` covers its centre (a floating label without
    ``pointer-events: none``), click there: the label activates its control like a person's click
    would (trusted click, focus). False when something else covers the field."""
    from playwright.async_api import Error as PlaywrightError

    try:
        if not await _evaluate(target, _OWN_LABEL_AT_CENTRE_JS):
            return False
        box = await target.bounding_box()  # main-frame coordinates (also for fields in iframes)
    except PlaywrightError:
        return False
    if not box:
        return False
    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    return True


async def _focus_by_click(page: "Page", target: "Target") -> None:
    """Click the field like a person. A covered field is found by a short probe (not the full click
    timeout): it is clicked through its own floating label, else focused with focus(). Raises
    :class:`TextEntryError` unless the field really has the focus afterwards, so no key meant for it
    can land in whatever had the focus before (a disabled, hidden or covered field)."""
    from playwright.async_api import Error as PlaywrightError

    try:
        await target.click(trial=True, timeout=CLICK_PROBE_TIMEOUT_MS)  # actionability checks only
    except PlaywrightError:  # covered (floating label, fixed banner / modal), hidden or detached
        if not await _click_own_label(page, target):
            with contextlib.suppress(PlaywrightError):
                await target.focus()
    else:
        try:
            await target.click(timeout=CLICK_TIMEOUT_MS)
        except PlaywrightError:
            with contextlib.suppress(PlaywrightError):
                await target.focus()
    if not await is_focused(target):
        with contextlib.suppress(PlaywrightError):
            await target.focus()  # e.g. a label click that a covered iframe swallowed
        if not await is_focused(target):
            raise TextEntryError("Could not focus the field (it is disabled, hidden or covered); nothing was typed.")


async def _clear_focused(page: "Page") -> None:
    await page.keyboard.press("ControlOrMeta+A")
    await page.keyboard.press("Backspace")


async def _caret_to_end(page: "Page", info: dict[str, Any]) -> None:
    await page.keyboard.press("End" if info.get("tag") == "input" else "ControlOrMeta+End")


# ---------------------------------------------------------------------- entry point


async def enter_text(
    page: "Page",
    locator: "Target",
    text: str,
    *,
    method: TypeMethod = "paste",
    clear: bool = True,
    sensitive: bool = False,
    clipboard_lock: Path,
    rng: random.Random | None = None,
    wpm: int | None = None,
) -> str:
    """Enter ``text`` into ``locator`` (a Locator or ElementHandle; it may live in any frame of
    ``page``) and return the method actually used: ``"fill"``, ``"type"``, ``"human"``,
    ``"paste"`` or ``"human (paste failed: <reason>)"``.

    ``clear`` replaces the current value (default) instead of appending. ``sensitive`` marks the
    clipboard content as concealed (paste). Date/time inputs always get ``fill``; selects,
    checkboxes, radios, files, ranges and colours raise :class:`NotTypeableError`.
    """
    from playwright.async_api import Error as PlaywrightError

    if method not in TYPE_METHODS:
        raise ValueError(f"method must be one of {', '.join(TYPE_METHODS)}")
    text = "" if text is None else str(text)
    rng = rng or random.Random()
    try:
        info = await _control_info(locator)
    except PlaywrightError as exc:
        raise _entry_error(exc, text, "find the field") from None
    tag, typ = info.get("tag"), info.get("type") or ""
    if tag == "select" or (tag == "input" and typ in NON_TEXT_INPUT_TYPES):
        what = "a <select>" if tag == "select" else f"an <input type={typ}>"
        raise NotTypeableError(f"Cannot type into {what}; select an option or click it instead.")
    if tag not in ("input", "textarea") and not info.get("editable"):
        raise NotTypeableError(f"Cannot type into a <{tag}> element (not an input, textarea or contenteditable).")
    if info.get("disabled") or info.get("readOnly"):
        raise TextEntryError(f"The field is {'disabled' if info.get('disabled') else 'read-only'}; nothing was typed.")
    if tag == "input" and typ in DATE_LIKE_INPUT_TYPES:
        method = "fill"
    log.debug("enter_text: %s into <%s%s>", method, tag, f" type={typ}" if typ else "")  # never the text or its length
    try:
        if method == "fill":
            await _fill(locator, text, clear)
            return "fill"
        if method == "type":
            if clear:
                await locator.fill("", timeout=ACTION_TIMEOUT_MS)
                if _is_locator(locator):
                    await locator.press_sequentially(text, delay=TYPE_DELAY_MS, timeout=ACTION_TIMEOUT_MS)
                else:
                    await locator.type(text, delay=TYPE_DELAY_MS, timeout=ACTION_TIMEOUT_MS)  # type: ignore[union-attr]
            else:
                # press_sequentially re-focuses the field and puts the caret at the *start* of its value:
                # click it, move the caret to the end and type there instead.
                await _prepare(page, locator, info, clear=False)
                await page.keyboard.type(text, delay=TYPE_DELAY_MS)
            return "type"
        if method == "human" or not text:  # nothing to paste: never touch the clipboard
            await _prepare(page, locator, info, clear)
            before = await read_value(locator)
            await type_like_human(page, text, rng, wpm=wpm, target=locator)
            await _check_typed(locator, before, text, info)
            return method
        return await _paste(page, locator, info, text, clear=clear, sensitive=sensitive,
                            clipboard_lock=clipboard_lock, rng=rng, wpm=wpm)
    except PlaywrightError as exc:
        raise _entry_error(exc, text, "enter the text") from None


async def _fill(target: "Target", text: str, clear: bool) -> None:
    if not clear:
        text = await read_value(target) + text
    await target.fill(text, timeout=ACTION_TIMEOUT_MS)


async def _prepare(page: "Page", target: "Target", info: dict[str, Any], clear: bool) -> None:
    await _focus_by_click(page, target)
    if clear:
        await _clear_focused(page)
    else:
        await _caret_to_end(page, info)


async def _check_typed(target: "Target", before: str, text: str, info: dict[str, Any]) -> None:
    """Raise unless the typed ``text`` arrived in ``target`` (defence in depth: the keys went
    somewhere else, or the page threw them away)."""
    if not value_landed(await read_value(target), before, text, max_length=info.get("maxLength")):
        raise TextEntryError("The typed text did not arrive in the field (the page rejected it or the focus moved).")


async def _wait_landed(target: "Target", before: str, text: str, max_length: int | None,
                       timeout: float = PASTE_LAND_TIMEOUT) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    value = before
    while True:
        value = await read_value(target)
        if value_landed(value, before, text, max_length=max_length):
            return True, value
        if time.monotonic() >= deadline:
            return False, value
        await asyncio.sleep(0.05)


async def _press_paste(page: "Page", target: "Target", shortcut: str, text: str, *, sensitive: bool,
                       clipboard_lock: Path) -> None:
    """Hold the clipboard only for the key press. Chrome reads it while handling the key event
    (verified: ``preventDefault``-ed, deferred and ``getAsString`` handlers all received the text
    after an immediate restore), so the user's clipboard is back before we wait for the value.

    The focus is checked once more inside the clipboard window, right before the chord: a page
    that moved the focus meanwhile must not receive the paste in another field."""
    async with async_clipboard_text(text, sensitive=sensitive, lock_path=clipboard_lock):
        if not await is_focused(target):
            raise TextEntryError("The field lost the focus before the paste; nothing was pasted.")
        await page.keyboard.press(shortcut)


async def _paste(page: "Page", target: "Target", info: dict[str, Any], text: str, *, clear: bool,
                 sensitive: bool, clipboard_lock: Path, rng: random.Random, wpm: int | None) -> str:
    await _prepare(page, target, info, clear)
    before = await read_value(target)
    max_length = info.get("maxLength")
    reason = "the text did not appear in the field"
    landed = False
    try:
        await _press_paste(page, target, paste_shortcut(), text, sensitive=sensitive, clipboard_lock=clipboard_lock)
        landed, value = await _wait_landed(target, before, text, max_length)
        if not landed and value == before:
            # Nothing at all arrived: try the ordinary paste chord before giving up.
            await _press_paste(page, target, plain_paste_shortcut(), text, sensitive=sensitive,
                               clipboard_lock=clipboard_lock)
            landed, value = await _wait_landed(target, before, text, max_length, timeout=0.5)
        if not landed and value == before:
            reason = "the page did not accept the paste"
    except ClipboardUnavailable as exc:
        reason = f"clipboard unavailable: {exc}"
    if landed:
        return "paste"
    log.info("paste did not land (%s); typing instead", reason)
    # Undo whatever partially arrived, then type the text key by key (only into the focused field).
    await _focus_by_click(page, target)
    if clear:
        await _clear_focused(page)
    elif await read_value(target) != before:
        await target.fill(before, timeout=ACTION_TIMEOUT_MS)
        await _caret_to_end(page, info)
    start = await read_value(target)
    await type_like_human(page, text, rng, wpm=wpm, target=target)
    await _check_typed(target, start, text, info)
    return f"human (paste failed: {reason})"
