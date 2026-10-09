"""Clipboard context manager: locking, snapshot/restore, cancellation and error paths.

Everything here uses in-memory fake backends - the user's real clipboard is never touched. The few
real-clipboard checks (exclusion formats, image restore, "newer copy wins") live in the
chrome-marked tests of ``test_typing.py`` and always restore the user's clipboard.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from dataclasses import dataclass, field

import pytest
from filelock import FileLock

from profilepilot.automation import clipboard
from profilepilot.automation.clipboard import (
    ClipboardUnavailable,
    async_clipboard_text,
    clipboard_text,
    paste_shortcut,
    plain_paste_shortcut,
)

TEST_TEXT = "Testy McTestface"
TEST_SSN = "000-12-3456"


@dataclass
class FakeSnap:
    value: str
    empty: bool = False


@dataclass
class FakeBackend:
    """Records calls and how many pastes hold the clipboard at the same time."""

    name: str = "fake"
    content: str = "the user's own clipboard"
    calls: list[tuple] = field(default_factory=list)
    active: int = 0
    max_active: int = 0
    fail_set: bool = False
    fail_restore: Exception | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot_and_set(self, text: str, *, sensitive: bool):
        if self.fail_set:
            raise ClipboardUnavailable("The clipboard is held open by another program.")
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.calls.append(("set", text, sensitive))
        snap = FakeSnap(self.content)
        self.content = text
        return snap, "token"

    def restore(self, snapshot: FakeSnap, token) -> bool:
        with self._lock:
            self.active -= 1
        if self.fail_restore is not None:
            raise self.fail_restore
        self.calls.append(("restore", snapshot.value, token))
        self.content = snapshot.value
        return True


@pytest.fixture
def fake():
    backend = FakeBackend()
    clipboard.set_backend(backend)
    try:
        yield backend
    finally:
        clipboard.set_backend(None)


def test_shortcuts_per_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert (paste_shortcut(), plain_paste_shortcut()) == ("Control+Shift+V", "Control+V")
    monkeypatch.setattr(sys, "platform", "linux")
    assert paste_shortcut() == "Control+Shift+V"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert (paste_shortcut(), plain_paste_shortcut()) == ("Meta+Shift+V", "Meta+V")


def test_sets_text_and_restores_even_when_the_block_fails(fake, tmp_path):
    lock = tmp_path / "cb.lock"
    with clipboard_text(TEST_TEXT, sensitive=False, lock_path=lock):
        assert fake.content == TEST_TEXT
    assert fake.content == "the user's own clipboard"
    with pytest.raises(RuntimeError):
        with clipboard_text(TEST_SSN, sensitive=True, lock_path=lock):
            assert fake.content == TEST_SSN
            raise RuntimeError("page crashed")
    assert fake.content == "the user's own clipboard"
    assert [c[0] for c in fake.calls] == ["set", "restore", "set", "restore"]
    assert fake.calls[2] == ("set", TEST_SSN, True)
    # the lock is free again
    assert FileLock(str(lock), timeout=0).acquire()


def test_threads_are_serialised_by_the_lock(fake, tmp_path):
    lock = tmp_path / "cb.lock"
    errors: list[BaseException] = []

    def paste(text: str) -> None:
        try:
            with clipboard_text(text, sensitive=False, lock_path=lock):
                time.sleep(0.15)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=paste, args=(f"profile-{i}",)) for i in range(3)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    assert fake.max_active == 1
    assert time.monotonic() - started >= 0.4
    assert fake.content == "the user's own clipboard"


@pytest.mark.asyncio
async def test_async_pastes_queue_up_without_blocking_the_event_loop(fake, tmp_path):
    """Two profiles pasting at the same time in one server process: the second waits for the lock
    in a worker thread while the event loop keeps running."""
    lock = tmp_path / "cb.lock"
    order: list[str] = []
    ticks = 0

    async def paste(name: str) -> None:
        async with async_clipboard_text(name, sensitive=False, lock_path=lock):
            order.append(f"{name}:in")
            await asyncio.sleep(0.2)
            order.append(f"{name}:out")

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    tick_task = asyncio.create_task(ticker())
    try:
        await asyncio.gather(paste("A"), paste("B"))
    finally:
        tick_task.cancel()
    assert fake.max_active == 1
    assert order in (["A:in", "A:out", "B:in", "B:out"], ["B:in", "B:out", "A:in", "A:out"])
    assert ticks >= 15  # ~0.4 s of waiting did not freeze the loop
    assert fake.content == "the user's own clipboard"


@pytest.mark.asyncio
async def test_cancellation_still_restores_and_unlocks(fake, tmp_path):
    lock = tmp_path / "cb.lock"
    entered = asyncio.Event()

    async def stuck() -> None:
        async with async_clipboard_text(TEST_SSN, sensitive=True, lock_path=lock):
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(stuck())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.content == "the user's own clipboard"
    async with async_clipboard_text("next", sensitive=False, lock_path=lock):
        assert fake.content == "next"


def test_lock_timeout_and_backend_failures(fake, tmp_path):
    lock = tmp_path / "cb.lock"
    holder = FileLock(str(lock), thread_local=False)
    holder.acquire()
    try:
        with pytest.raises(ClipboardUnavailable, match="in use by another ProfilePilot paste"):
            with clipboard_text(TEST_TEXT, sensitive=False, lock_path=lock, timeout=0.2):
                pass  # pragma: no cover
    finally:
        holder.release()
    assert fake.calls == []

    fake.fail_set = True
    with pytest.raises(ClipboardUnavailable, match="held open"):
        with clipboard_text(TEST_TEXT, sensitive=False, lock_path=lock):
            pass  # pragma: no cover
    assert FileLock(str(lock), timeout=0).acquire()  # released after the failure


def test_restore_failure_is_logged_without_the_text(fake, tmp_path, caplog):
    fake.fail_restore = RuntimeError(f"boom {TEST_SSN}")
    with caplog.at_level(logging.DEBUG, logger="profilepilot.clipboard"):
        with clipboard_text(TEST_TEXT, sensitive=False, lock_path=tmp_path / "cb.lock"):
            pass
    assert "could not restore the clipboard (RuntimeError)" in caplog.text
    assert TEST_SSN not in caplog.text and TEST_TEXT not in caplog.text
    assert FileLock(str(tmp_path / "cb.lock"), timeout=0).acquire()  # not sensitive: no retries, lock free


class FlakyRestore(FakeBackend):
    """restore() fails ``failures`` times (like a clipboard held open by another program)."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.attempts = 0
        self.discarded: list[object] = []

    def restore(self, snapshot, token) -> bool:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise ClipboardUnavailable("The clipboard is held open by another program.")
        self.content = snapshot.value
        return True

    def discard(self, token) -> bool:
        self.discarded.append(token)
        self.content = ""
        return True


def _wait_for_lock(lock_path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            FileLock(str(lock_path), timeout=0).acquire()
            return
        except Exception:
            assert time.monotonic() < deadline, "the clipboard lock was never released"
            time.sleep(0.02)


def test_failed_restore_of_sensitive_text_is_retried_while_holding_the_lock(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(clipboard, "SENSITIVE_RESTORE_DELAYS", (0.15, 0.05, 0.05))
    lock = tmp_path / "cb.lock"
    backend = FlakyRestore(failures=2)
    clipboard.set_backend(backend)
    try:
        with caplog.at_level(logging.DEBUG, logger="profilepilot.clipboard"):
            with clipboard_text(TEST_SSN, sensitive=True, lock_path=lock):
                pass
            # the retries hold the lock: no other paste can snapshot the SSN as "the user's clipboard"
            with pytest.raises(Exception):
                FileLock(str(lock), timeout=0).acquire()
            _wait_for_lock(lock)
        assert backend.attempts == 3 and backend.content == "the user's own clipboard"
        assert backend.discarded == [] and TEST_SSN not in caplog.text

        backend = FlakyRestore(failures=99)  # never restorable: the SSN is removed instead
        clipboard.set_backend(backend)
        with caplog.at_level(logging.DEBUG, logger="profilepilot.clipboard"):
            with clipboard_text(TEST_SSN, sensitive=True, lock_path=lock):
                pass
            _wait_for_lock(lock)
        assert backend.attempts == 4 and backend.discarded == ["token"] and backend.content == ""
        assert "removed the sensitive text" in caplog.text and TEST_SSN not in caplog.text
    finally:
        clipboard.set_backend(None)


def test_linux_without_clipboard_tools_is_unavailable(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    with pytest.raises(ClipboardUnavailable, match="wl-clipboard"):
        clipboard.LinuxClipboard.detect()


def test_default_lock_path_is_machine_wide():
    path = clipboard.default_lock_path()
    assert path.name == "profilepilot-clipboard.lock" and path.parent.exists()


def test_windows_format_classification():
    assert clipboard._is_handle_format(2) and clipboard._is_handle_format(0x305)
    assert not clipboard._is_handle_format(13) and not clipboard._is_handle_format(8)
    assert not clipboard._is_handle_format(0xC123)
