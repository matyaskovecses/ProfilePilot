"""Temporary use of the system clipboard for "type-paste" (paste text into a page as a real paste).

:func:`clipboard_text` / :func:`async_clipboard_text` put ``text`` on the clipboard for the
duration of a ``with`` block and then give the user their clipboard back:

1. a cross-process :class:`~filelock.FileLock` serialises every user of the clipboard (several
   profiles, several MCP servers), so two pastes never interleave;
2. the user's current clipboard is snapshotted (every format that round-trips as bytes);
3. ``text`` is set as Unicode text. Transient automation text is always marked
   ``CanIncludeInClipboardHistory = 0`` and ``CanUploadToCloudClipboard = 0`` so it never shows up
   in Windows clipboard history (Win+V) or syncs to other devices; ``sensitive=True`` adds
   ``ExcludeClipboardContentFromMonitorProcessing`` and ``Clipboard Viewer Ignore`` (third-party
   clipboard managers skip the content) and, on macOS, the ``org.nspasteboard.ConcealedType``
   marker;
4. the caller presses the paste shortcut (:func:`paste_shortcut`) inside the block;
5. the snapshot is restored (or the clipboard cleared if it was empty), also on errors. If another
   application wrote to the clipboard meanwhile (the user copied something), their newer content is
   kept instead. If restoring sensitive text fails, a background thread retries for about 30 s
   (holding the lock) and finally empties the clipboard if our text is still on it.

The text is never logged. Backends: Windows (Win32 clipboard API through ``ctypes``, see
:class:`WindowsClipboard` for why not pywin32's ``win32clipboard``), macOS (AppKit ``NSPasteboard`` when pyobjc is installed, else ``pbcopy``/``pbpaste``
for text only), Linux (``wl-copy``/``wl-paste`` on Wayland, else ``xclip``; text plus one image
type). Tests replace the backend with :func:`set_backend`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Protocol

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from ..errors import ProfilePilotError

log = logging.getLogger("profilepilot.clipboard")

OPEN_RETRY_SECONDS = 1.0
"""How long to retry opening the clipboard when another application holds it."""
RESTORE_RETRY_SECONDS = 5.0
"""Restoring matters more (sensitive text must not stay behind), so it retries longer."""
SENSITIVE_RESTORE_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)
"""After a failed restore of *sensitive* text, a background thread retries at these delays (about
30 s in all) while still holding the clipboard lock, then, as a last resort, empties the clipboard
if it still holds our text: a card number or SSN never outlives the paste on the clipboard."""

HISTORY_FORMAT = "CanIncludeInClipboardHistory"
CLOUD_FORMAT = "CanUploadToCloudClipboard"
EXCLUDE_MONITOR_FORMAT = "ExcludeClipboardContentFromMonitorProcessing"
VIEWER_IGNORE_FORMAT = "Clipboard Viewer Ignore"
MAC_CONCEALED_TYPE = "org.nspasteboard.ConcealedType"
MAC_TRANSIENT_TYPE = "org.nspasteboard.TransientType"


class ClipboardUnavailable(ProfilePilotError):
    """The system clipboard cannot be used (no backend, held by another program, lock timeout)."""


def paste_shortcut() -> str:
    """Chrome's "paste as plain text" chord for this platform (Playwright key syntax)."""
    return "Meta+Shift+V" if sys.platform == "darwin" else "Control+Shift+V"


def plain_paste_shortcut() -> str:
    """The ordinary paste chord (fallback when the plain-text chord does not paste)."""
    return "Meta+V" if sys.platform == "darwin" else "Control+V"


def default_lock_path() -> Path:
    """A machine-wide lock file (the clipboard is shared by every data root of this OS user)."""
    return Path(tempfile.gettempdir()) / "profilepilot-clipboard.lock"


# ---------------------------------------------------------------------- backend protocol


class Snapshot(Protocol):
    """Opaque saved clipboard state of a backend."""

    @property
    def empty(self) -> bool: ...


class ClipboardBackend(Protocol):
    """What :func:`clipboard_text` needs from a platform. All methods are blocking."""

    name: str

    def snapshot_and_set(self, text: str, *, sensitive: bool) -> tuple[Snapshot, Any]:
        """Save the current content, then put ``text`` on the clipboard. Returns the snapshot and a
        change token that :meth:`restore` uses to detect foreign writes (None if unsupported)."""
        ...

    def restore(self, snapshot: Snapshot, token: Any) -> bool:
        """Put the snapshot back unless someone else changed the clipboard since ``token``.
        Returns False when the foreign content was kept."""
        ...

    # Optional: ``discard(token) -> bool`` empties the clipboard if it still holds the text set with
    # ``token`` (last resort after failed restores of sensitive text).


_backend_override: ClipboardBackend | None = None
_backend_cache: ClipboardBackend | None = None
_backend_lock = threading.Lock()


def set_backend(backend: ClipboardBackend | None) -> None:
    """Replace the platform backend (tests); ``None`` goes back to auto-detection."""
    global _backend_override
    _backend_override = backend


def get_backend() -> ClipboardBackend:
    """The clipboard backend of this platform. Raises :class:`ClipboardUnavailable`."""
    global _backend_cache
    if _backend_override is not None:
        return _backend_override
    with _backend_lock:
        if _backend_cache is None:
            if sys.platform == "win32":
                _backend_cache = WindowsClipboard()
            elif sys.platform == "darwin":
                _backend_cache = MacClipboard()
            else:
                _backend_cache = LinuxClipboard.detect()
        return _backend_cache


@dataclass(frozen=True)
class _Token:
    """Clipboard state right after our text was set: change counter + digest of our text (the
    digest only lets :meth:`restore` recognise its own text; the text itself is not kept)."""

    seq: int
    digest: bytes


def _digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16,
                           key=_DIGEST_KEY).digest()


_DIGEST_KEY = os.urandom(32)
"""Per-process key: the digests of sensitive values are not comparable outside this process."""


# ---------------------------------------------------------------------- public API


@dataclass
class _Paste:
    """One use of the clipboard: lock -> snapshot + set -> (paste) -> restore -> unlock."""

    text: str
    sensitive: bool
    lock_path: Path
    timeout: float
    _lock: FileLock | None = None
    _backend: ClipboardBackend | None = None
    _snapshot: Snapshot | None = None
    _token: Any = None
    _entered: bool = field(default=False)

    def enter(self) -> None:
        backend = get_backend()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # thread_local=False: the async variant acquires and releases in different worker threads.
        lock = FileLock(str(self.lock_path), timeout=self.timeout, thread_local=False)
        try:
            lock.acquire()
        except FileLockTimeout:
            raise ClipboardUnavailable(
                f"The clipboard is in use by another ProfilePilot paste (waited {self.timeout:.0f} s)."
            ) from None
        try:
            self._snapshot, self._token = backend.snapshot_and_set(self.text, sensitive=self.sensitive)
        except BaseException:
            lock.release()
            raise
        finally:
            self.text = ""  # do not keep the value around longer than needed
        self._lock, self._backend, self._entered = lock, backend, True

    def exit(self) -> None:
        """Restore the user's clipboard and release the lock. Never raises (it runs in ``finally``
        blocks and must not mask the caller's exception).

        If restoring *sensitive* text fails, a background thread keeps retrying (and keeps the lock,
        so no other paste snapshots our text as "the user's clipboard"), see
        :data:`SENSITIVE_RESTORE_DELAYS`."""
        if not self._entered:
            return
        self._entered = False
        backend, snapshot, token, lock = self._backend, self._snapshot, self._token, self._lock
        self._snapshot = self._token = self._lock = None
        handed_off = False
        try:
            assert backend is not None and snapshot is not None
            if not backend.restore(snapshot, token):
                log.info("clipboard changed by another program during the paste; kept the newer content")
        except Exception as exc:
            if isinstance(exc, ClipboardUnavailable):
                log.error("could not restore the clipboard: %s", exc)
            else:
                log.error("could not restore the clipboard (%s)", type(exc).__name__)
            if self.sensitive and backend is not None and snapshot is not None:
                worker = threading.Thread(target=_retry_restore, args=(backend, snapshot, token, lock),
                                          name="profilepilot-clipboard-restore", daemon=True)
                worker.start()
                handed_off = True
        finally:
            if lock is not None and not handed_off:
                with contextlib.suppress(Exception):
                    lock.release()


def _retry_restore(backend: ClipboardBackend, snapshot: Snapshot, token: Any, lock: FileLock | None) -> None:
    """Background retries after a failed restore of sensitive text (holds ``lock`` throughout)."""
    try:
        for attempt, delay in enumerate(SENSITIVE_RESTORE_DELAYS, 1):
            time.sleep(delay)
            try:
                if backend.restore(snapshot, token):
                    log.info("clipboard restored on retry %d", attempt)
                else:
                    log.info("clipboard changed by another program meanwhile; kept the newer content")
                return
            except Exception as exc:
                log.debug("clipboard restore retry %d failed (%s)", attempt, type(exc).__name__)
        discard = getattr(backend, "discard", None)
        try:
            if discard is not None and discard(token):
                log.error("could not restore the user's clipboard; removed the sensitive text from it instead")
            else:
                log.error("could not restore the user's clipboard; the sensitive text may still be on it")
        except Exception as exc:
            log.error("could not clear the sensitive text from the clipboard (%s)", type(exc).__name__)
    finally:
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()


@contextmanager
def clipboard_text(text: str, *, sensitive: bool, lock_path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Put ``text`` on the system clipboard for the ``with`` block, then restore the user's
    clipboard. Raises :class:`ClipboardUnavailable` when the clipboard cannot be used."""
    paste = _Paste(text, sensitive, Path(lock_path), timeout)
    paste.enter()
    try:
        yield
    finally:
        paste.exit()


@asynccontextmanager
async def async_clipboard_text(text: str, *, sensitive: bool, lock_path: Path,
                               timeout: float = 10.0) -> AsyncIterator[None]:
    """:func:`clipboard_text` for async callers. The blocking steps (waiting for the cross-process
    lock, opening the clipboard) run in worker threads so the event loop keeps serving, and two
    pastes of the same process queue up on the lock instead of deadlocking the loop.

    Cancellation-safe: if the caller is cancelled while the clipboard is being taken, the step is
    finished, the user's clipboard is restored and only then the cancellation propagates."""
    loop = asyncio.get_running_loop()
    paste = _Paste(text, sensitive, Path(lock_path), timeout)
    entering = loop.run_in_executor(None, paste.enter)
    if await _wait_through_cancel(entering):
        await _wait_through_cancel(loop.run_in_executor(None, paste.exit))
        raise asyncio.CancelledError
    entering.result()  # ClipboardUnavailable
    try:
        yield
    finally:
        if await _wait_through_cancel(loop.run_in_executor(None, paste.exit)):
            raise asyncio.CancelledError


async def _wait_through_cancel(fut: "asyncio.Future[Any]") -> bool:
    """Wait until ``fut`` (a worker-thread job) is done, even if this task gets cancelled meanwhile.
    Returns True when a cancellation arrived (the caller re-raises it after cleaning up)."""
    cancelled = False
    while not fut.done():
        try:
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break  # the job failed; the caller reads fut.result()
    return cancelled


# ---------------------------------------------------------------------- Windows


# Formats whose data is a GDI/metafile handle rather than global memory (never byte-copied).
# CF_BITMAP is synthesised from CF_DIB again by Windows; CF_ENHMETAFILE round-trips through its bits.
_CF_TEXT, _CF_BITMAP, _CF_METAFILEPICT, _CF_OEMTEXT, _CF_DIB = 1, 2, 3, 7, 8
_CF_PALETTE, _CF_UNICODETEXT, _CF_ENHMETAFILE, _CF_LOCALE, _CF_DIBV5 = 9, 13, 14, 16, 17
_HANDLE_FORMATS = frozenset({_CF_BITMAP, _CF_METAFILEPICT, _CF_PALETTE, 0x80, 0x82, 0x83, 0x8E})
_SYNTHESISED_FROM = {_CF_UNICODETEXT: (_CF_TEXT, _CF_OEMTEXT, _CF_LOCALE), _CF_DIB: (_CF_DIBV5,)}
"""Formats Windows synthesises from another one: never read them (that runs the conversion in our
process) when their source is present - Windows synthesises them again after a restore."""
_HWND_MESSAGE = -3
_GMEM_MOVEABLE = 0x0002


def _is_handle_format(fmt: int) -> bool:
    return fmt in _HANDLE_FORMATS or 0x300 <= fmt <= 0x3FF  # CF_GDIOBJFIRST..CF_GDIOBJLAST


def _terminated(fmt: int, data: bytes) -> bytes:
    """Text formats cut after their terminator (a block may be longer than the text)."""
    if fmt == _CF_UNICODETEXT:
        for i in range(0, len(data) - 1, 2):
            if data[i] == 0 and data[i + 1] == 0:
                return data[:i + 2]
        return data[: len(data) // 2 * 2] + b"\0\0"
    if fmt in (_CF_TEXT, _CF_OEMTEXT):
        end = data.find(b"\0")
        return data[:end + 1] if end >= 0 else data + b"\0"
    return data


@dataclass
class WindowsSnapshot:
    items: list[tuple[int, bytes]]
    empty: bool


class WindowsClipboard:
    """Win32 clipboard backend on plain ``ctypes`` (user32/kernel32/gdi32).

    Every block is written with an exact-size ``GlobalAlloc``. pywin32's ``SetClipboardData`` (312)
    is not used: for ``bytes`` in CF_UNICODETEXT it allocates ``len + 1`` bytes and writes a two-byte
    terminator at offset ``len`` - a one-byte heap overflow for even lengths (an odd-length block
    even had its last character overwritten). Round-tripping text that way grew it by a byte per
    paste, and a test run crashed with STATUS_HEAP_CORRUPTION (0xC0000374) inside that call.

    Reads open the clipboard without an owner. Writes use an owner window: after
    ``OpenClipboard(NULL)`` + ``EmptyClipboard`` the owner is NULL and ``SetClipboardData`` can fail
    (documented; seen intermittently from worker threads). Each write creates a hidden
    message-only window in the calling thread and destroys it right after ``CloseClipboard``: the
    data stays, and no clipboard message can ever be sent to a window of ours that nobody pumps.
    """

    name = "windows"

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as wt

        try:
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        except OSError as exc:  # pragma: no cover - not Windows
            raise ClipboardUnavailable(f"The Windows clipboard API is not available ({exc}).") from None
        h, uint = ctypes.c_void_p, wt.UINT
        for fn, args, res in (
            (u32.OpenClipboard, [wt.HWND], wt.BOOL), (u32.CloseClipboard, [], wt.BOOL),
            (u32.EmptyClipboard, [], wt.BOOL), (u32.EnumClipboardFormats, [uint], uint),
            (u32.GetClipboardData, [uint], h), (u32.SetClipboardData, [uint, h], h),
            (u32.RegisterClipboardFormatW, [wt.LPCWSTR], uint),
            (u32.GetClipboardFormatNameW, [uint, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
            (u32.GetClipboardSequenceNumber, [], wt.DWORD),
            (u32.CreateWindowExW, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID], wt.HWND),
            (u32.DestroyWindow, [wt.HWND], wt.BOOL),
            (k32.GlobalAlloc, [uint, ctypes.c_size_t], h), (k32.GlobalLock, [h], h),
            (k32.GlobalUnlock, [h], wt.BOOL), (k32.GlobalSize, [h], ctypes.c_size_t), (k32.GlobalFree, [h], h),
            (gdi.GetEnhMetaFileBits, [h, uint, h], uint), (gdi.SetEnhMetaFileBits, [uint, ctypes.c_char_p], h),
            (gdi.DeleteEnhMetaFile, [h], wt.BOOL),
        ):
            fn.argtypes, fn.restype = args, res
        self._ct, self._u32, self._k32, self._gdi = ctypes, u32, k32, gdi
        self._formats: dict[str, int] = {}

    # -- low level

    def _fmt(self, name: str) -> int:
        fmt = self._formats.get(name)
        if fmt is None:
            fmt = self._u32.RegisterClipboardFormatW(name)
            if not fmt:
                raise ClipboardUnavailable(f"Could not register the clipboard format {name!r}.")
            self._formats[name] = fmt
        return fmt

    def _owner_window(self) -> int:
        """A hidden message-only window (HWND_MESSAGE parent) to own the clipboard while writing."""
        hwnd = self._u32.CreateWindowExW(0, "STATIC", "ProfilePilot clipboard", 0, 0, 0, 0, 0,
                                         _HWND_MESSAGE, None, None, None)
        if not hwnd:
            log.debug("no clipboard owner window (error %s); writing without an owner", self._ct.get_last_error())
        return hwnd or 0

    @contextmanager
    def _opened(self, retry: float, *, write: bool = False) -> Iterator[None]:
        owner = self._owner_window() if write else 0
        try:
            deadline = time.monotonic() + retry
            while not self._u32.OpenClipboard(owner or None):
                if time.monotonic() >= deadline:
                    raise ClipboardUnavailable("The clipboard is held open by another program.")
                time.sleep(0.02)
            try:
                yield
            finally:
                self._u32.CloseClipboard()
        finally:
            if owner:
                self._u32.DestroyWindow(owner)

    def _formats_now(self) -> list[int]:
        out, fmt = [], 0
        while True:
            fmt = self._u32.EnumClipboardFormats(fmt)
            if not fmt:
                return out
            out.append(fmt)

    def _read_raw(self, fmt: int) -> bytes | None:
        handle = self._u32.GetClipboardData(fmt)
        if not handle:
            return None
        if fmt == _CF_ENHMETAFILE:
            size = self._gdi.GetEnhMetaFileBits(handle, 0, None)
            if not size:
                return None
            buf = self._ct.create_string_buffer(size)
            return buf.raw if self._gdi.GetEnhMetaFileBits(handle, size, buf) == size else None
        size = self._k32.GlobalSize(handle)
        if not size:
            return None
        ptr = self._k32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return _terminated(fmt, self._ct.string_at(ptr, size))
        finally:
            self._k32.GlobalUnlock(handle)

    def _write_raw(self, fmt: int, data: bytes) -> None:
        """Put ``data`` on the open clipboard as ``fmt`` in a block of exactly ``len(data)`` bytes."""
        if fmt == _CF_ENHMETAFILE:
            meta = self._gdi.SetEnhMetaFileBits(len(data), data)
            if meta and not self._u32.SetClipboardData(fmt, meta):
                self._gdi.DeleteEnhMetaFile(meta)
            return
        block = self._k32.GlobalAlloc(_GMEM_MOVEABLE, max(len(data), 1))
        if not block:
            raise ClipboardUnavailable("Out of memory for the clipboard.")
        ptr = self._k32.GlobalLock(block)
        if not ptr:
            self._k32.GlobalFree(block)
            raise ClipboardUnavailable("Could not lock clipboard memory.")
        try:
            self._ct.memmove(ptr, data, len(data))
        finally:
            self._k32.GlobalUnlock(block)
        if not self._u32.SetClipboardData(fmt, block):
            error = self._ct.get_last_error()
            self._k32.GlobalFree(block)  # ownership only passes to the system on success
            raise ClipboardUnavailable(f"SetClipboardData failed (Windows error {error}).")

    def _seq(self) -> int:
        return int(self._u32.GetClipboardSequenceNumber())

    def _snapshot(self) -> WindowsSnapshot:
        formats = self._formats_now()
        present = set(formats)
        skip = {derived for source, derived_formats in _SYNTHESISED_FROM.items() if source in present
                for derived in derived_formats}
        items: list[tuple[int, bytes]] = []
        for fmt in formats:
            if fmt in skip or _is_handle_format(fmt):
                continue
            data = self._read_raw(fmt)
            if data is not None:
                items.append((fmt, data))
        return WindowsSnapshot(items, empty=not formats)

    # -- backend API

    def snapshot_and_set(self, text: str, *, sensitive: bool) -> tuple[WindowsSnapshot, _Token]:
        dword0 = struct.pack("<I", 0)
        with self._opened(OPEN_RETRY_SECONDS, write=True):
            snap = self._snapshot()
            if not self._u32.EmptyClipboard():
                raise ClipboardUnavailable("Could not take over the clipboard.")
            try:
                self._write_raw(_CF_UNICODETEXT, (text + "\0").encode("utf-16-le", "surrogatepass"))
                self._write_raw(self._fmt(HISTORY_FORMAT), dword0)
                self._write_raw(self._fmt(CLOUD_FORMAT), dword0)
                if sensitive:
                    self._write_raw(self._fmt(EXCLUDE_MONITOR_FORMAT), dword0)
                    self._write_raw(self._fmt(VIEWER_IGNORE_FORMAT), dword0)
            except Exception as exc:
                # Never leave the user with an empty (or half-written) clipboard.
                with contextlib.suppress(Exception):
                    self._u32.EmptyClipboard()
                    self._put(snap)
                if isinstance(exc, ClipboardUnavailable):
                    raise
                raise ClipboardUnavailable(f"Could not write to the clipboard ({type(exc).__name__}).") from None
        # Read after CloseClipboard: Windows bumps the sequence number when the clipboard is closed
        # (verified), not when other programs read it (also for synthesised formats such as CF_TEXT).
        return snap, _Token(self._seq(), _digest(text))

    def _holds(self, token: _Token) -> bool:
        """Is our own text still on the (open) clipboard?"""
        data = self._read_raw(_CF_UNICODETEXT)
        if not data:
            return False
        text = data.decode("utf-16-le", "surrogatepass").split("\0", 1)[0]
        return _digest(text) == token.digest

    def _put(self, snapshot: WindowsSnapshot) -> None:
        """Write the snapshot's formats onto the (open, emptied) clipboard."""
        dword0 = struct.pack("<I", 0)
        restored = set()
        for fmt, data in snapshot.items:
            try:
                self._write_raw(fmt, data)
                restored.add(fmt)
            except Exception as exc:  # one odd format must not lose the others
                log.debug("clipboard format %s not restored: %s", fmt, type(exc).__name__)
        if not snapshot.empty:
            # The user's content is already in their clipboard history: do not add a duplicate
            # entry or upload it again.
            for name in (HISTORY_FORMAT, CLOUD_FORMAT):
                if self._fmt(name) not in restored:
                    with contextlib.suppress(Exception):
                        self._write_raw(self._fmt(name), dword0)

    def restore(self, snapshot: WindowsSnapshot, token: _Token | None) -> bool:
        with self._opened(RESTORE_RETRY_SECONDS, write=True):
            if token is not None and self._seq() != token.seq and not self._holds(token):
                return False  # the user (or another program) copied something newer: keep it
            if not self._u32.EmptyClipboard():
                raise ClipboardUnavailable("Could not take over the clipboard to restore it.")
            self._put(snapshot)
            return True

    def discard(self, token: _Token | None) -> bool:
        """Last resort when a restore keeps failing: empty the clipboard if it still holds our text."""
        if token is None:
            return False
        with self._opened(RESTORE_RETRY_SECONDS, write=True):
            return bool(self._holds(token) and self._u32.EmptyClipboard())

    # -- inspection helpers (tests, diagnostics)

    def format_name(self, fmt: int) -> str:
        if fmt < 0xC000:
            return str(fmt)
        buf = self._ct.create_unicode_buffer(256)
        n = self._u32.GetClipboardFormatNameW(fmt, buf, 256)
        return buf.value[:n] if n else str(fmt)

    def current_formats(self) -> dict[str, bytes | None]:
        """``{format name (registered) or number: raw bytes}`` of the current clipboard (tests,
        diagnostics). Synthesised formats are listed without data."""
        out: dict[str, bytes | None] = {}
        with self._opened(OPEN_RETRY_SECONDS):
            formats = self._formats_now()
            present = set(formats)
            skip = {d for s, ds in _SYNTHESISED_FROM.items() if s in present for d in ds}
            for fmt in formats:
                out[self.format_name(fmt)] = (None if fmt in skip or _is_handle_format(fmt)
                                              else self._read_raw(fmt))
        return out

    def set_formats(self, items: dict[int, bytes]) -> None:
        """Replace the clipboard with raw ``{format: bytes}`` (tests: simulate another program)."""
        with self._opened(OPEN_RETRY_SECONDS, write=True):
            self._u32.EmptyClipboard()
            for fmt, data in items.items():
                self._write_raw(fmt, data)


# ---------------------------------------------------------------------- macOS


@dataclass
class MacSnapshot:
    items: list[dict[str, bytes]] | None  # AppKit: every type of every pasteboard item
    text: str | None  # pbpaste fallback
    empty: bool


class MacClipboard:
    """``NSPasteboard`` via pyobjc when available (all types, concealed marker), else
    ``pbcopy``/``pbpaste`` (text only)."""

    name = "macos"

    def __init__(self) -> None:
        try:
            import AppKit  # type: ignore[import-not-found]

            self._appkit: Any = AppKit
        except ImportError:
            self._appkit = None
            if not (shutil.which("pbcopy") and shutil.which("pbpaste")):
                raise ClipboardUnavailable("pbcopy/pbpaste not found.") from None

    def snapshot_and_set(self, text: str, *, sensitive: bool) -> tuple[MacSnapshot, Any]:
        if self._appkit is not None:
            return self._appkit_snapshot_and_set(text, sensitive)
        if sensitive:
            log.warning("pyobjc (AppKit) is not installed: the clipboard text is not marked as concealed, "
                        "so clipboard managers may record it")
        old = _run(["pbpaste"], timeout=5).decode("utf-8", "replace")
        _run(["pbcopy"], data=text.encode("utf-8"), timeout=5)
        return MacSnapshot(None, old, empty=not old), None

    def _appkit_snapshot_and_set(self, text: str, sensitive: bool) -> tuple[MacSnapshot, Any]:
        ak = self._appkit
        pb = ak.NSPasteboard.generalPasteboard()
        items: list[dict[str, bytes]] = []
        for item in pb.pasteboardItems() or []:
            saved: dict[str, bytes] = {}
            for typ in item.types() or []:
                data = item.dataForType_(typ)
                if data is not None:
                    saved[str(typ)] = bytes(data)
            if saved:
                items.append(saved)
        pb.clearContents()
        types = [ak.NSPasteboardTypeString, MAC_TRANSIENT_TYPE]
        if sensitive:
            types.append(MAC_CONCEALED_TYPE)
        pb.declareTypes_owner_(types, None)
        pb.setString_forType_(text, ak.NSPasteboardTypeString)
        marker = ak.NSData.data()
        pb.setData_forType_(marker, MAC_TRANSIENT_TYPE)
        if sensitive:
            pb.setData_forType_(marker, MAC_CONCEALED_TYPE)
        return MacSnapshot(items, None, empty=not items), _Token(int(pb.changeCount()), _digest(text))

    def restore(self, snapshot: MacSnapshot, token: Any) -> bool:
        if self._appkit is None:
            _run(["pbcopy"], data=(snapshot.text or "").encode("utf-8"), timeout=5)
            return True
        ak = self._appkit
        pb = ak.NSPasteboard.generalPasteboard()
        if token is not None and int(pb.changeCount()) != token.seq:
            current = pb.stringForType_(ak.NSPasteboardTypeString)
            if current is None or _digest(str(current)) != token.digest:
                return False
        pb.clearContents()
        objects = []
        for saved in snapshot.items or []:
            item = ak.NSPasteboardItem.alloc().init()
            for typ, data in saved.items():
                item.setData_forType_(ak.NSData.dataWithBytes_length_(data, len(data)), typ)
            objects.append(item)
        if objects:
            pb.writeObjects_(objects)
        return True

    def discard(self, token: Any) -> bool:
        """Last resort when a restore keeps failing: clear the pasteboard if it still holds our text."""
        if self._appkit is None or token is None:
            return False
        ak = self._appkit
        pb = ak.NSPasteboard.generalPasteboard()
        current = pb.stringForType_(ak.NSPasteboardTypeString)
        if current is None or _digest(str(current)) != token.digest:
            return False
        pb.clearContents()
        return True


# ---------------------------------------------------------------------- Linux


_IMAGE_TYPES = ("image/png", "image/jpeg", "image/bmp")


@dataclass
class LinuxSnapshot:
    text: str | None
    mime: str | None
    data: bytes | None
    empty: bool


class LinuxClipboard:
    """``wl-copy``/``wl-paste`` (Wayland) or ``xclip`` (X11). Keeps text, or one image type."""

    name = "linux"

    def __init__(self, tool: str) -> None:
        self.tool = tool

    @classmethod
    def detect(cls) -> "LinuxClipboard":
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy") and shutil.which("wl-paste"):
            return cls("wl")
        if os.environ.get("DISPLAY") and shutil.which("xclip"):
            return cls("xclip")
        raise ClipboardUnavailable(
            "No clipboard tool found: install wl-clipboard (Wayland) or xclip (X11) to use paste typing."
        )

    def _types(self) -> list[str]:
        cmd = ["wl-paste", "--list-types"] if self.tool == "wl" else ["xclip", "-selection", "clipboard", "-t",
                                                                      "TARGETS", "-o"]
        try:
            return _run(cmd, timeout=5).decode("utf-8", "replace").split()
        except ClipboardUnavailable:
            return []  # empty clipboard

    def _get(self, mime: str) -> bytes:
        cmd = (["wl-paste", "--no-newline", "--type", mime] if self.tool == "wl"
               else ["xclip", "-selection", "clipboard", "-t", mime, "-o"])
        return _run(cmd, timeout=5)

    def _set(self, data: bytes, mime: str, *, sensitive: bool = False) -> None:
        if self.tool == "wl":
            cmd = ["wl-copy", "--type", mime]
        else:
            cmd = ["xclip", "-selection", "clipboard", "-t", mime, "-i"]
        if sensitive:
            log.debug("Linux clipboard tools have no standard 'concealed' marker; text is transient only")
        # The tools fork a process that serves the selection: never wait on inherited pipes.
        _run(cmd, data=data, timeout=5, detach=True)

    def _clear(self) -> None:
        if self.tool == "wl":
            _run(["wl-copy", "--clear"], timeout=5, detach=True)
        else:
            _run(["xclip", "-selection", "clipboard", "-i", "/dev/null"], timeout=5, detach=True)

    def snapshot_and_set(self, text: str, *, sensitive: bool) -> tuple[LinuxSnapshot, Any]:
        types = self._types()
        snap = LinuxSnapshot(None, None, None, empty=not types)
        text_types = [t for t in types if t in ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "STRING")]
        if text_types:
            with contextlib.suppress(ClipboardUnavailable):
                snap.text = self._get(text_types[0]).decode("utf-8", "replace")
        else:
            image = next((t for t in _IMAGE_TYPES if t in types), None)
            if image:
                with contextlib.suppress(ClipboardUnavailable):
                    snap.mime, snap.data = image, self._get(image)
        self._set(text.encode("utf-8"), "text/plain;charset=utf-8", sensitive=sensitive)
        return snap, None

    def restore(self, snapshot: LinuxSnapshot, token: Any) -> bool:
        if snapshot.text is not None:
            self._set(snapshot.text.encode("utf-8"), "text/plain;charset=utf-8")
        elif snapshot.data is not None and snapshot.mime:
            self._set(snapshot.data, snapshot.mime)
        else:
            self._clear()
        return True


def _run(cmd: list[str], *, data: bytes | None = None, timeout: float, detach: bool = False) -> bytes:
    """Run a clipboard tool; ``detach`` for setters that leave a selection-serving child behind."""
    try:
        if detach:
            subprocess.run(cmd, input=data if data is not None else b"", stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout, check=True)
            return b""
        result = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout, check=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClipboardUnavailable(f"{cmd[0]} failed ({type(exc).__name__}).") from None
