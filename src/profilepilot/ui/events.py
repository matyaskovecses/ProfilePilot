"""Live events for ProfilePilot Manager (Server-Sent Events).

Several processes change the data root at the same time (MCP servers for Claude / ChatGPT, profile
hosts, the CLI), so the hub *polls the files* once a second and turns changes into events:

=================  ===========================================================================
``activity``       a new line in ``activity.jsonl`` (an AI tool call or a Manager action)
``profile``        a profile changed (settings, running state, pause, help requests); full view
``profile-removed``  a profile disappeared (deleted / moved to the trash)
``help``           a new open help request (the UI shows a banner and a desktop notification)
``proxies``        ``proxies.json`` changed
``identities``     ``identities.json`` changed
``settings``       ``config.json`` changed
``trash``          the trash changed
``chatgpt``        ``chatgpt.json`` (the ChatGPT tunnel state) changed
``proxy-test``     progress of a proxy test run started from the Manager (in-process): ``started``
                   with the ``ids`` being tested, one event per proxy, then ``finished``
``clients``        the AI apps detected on this computer (in-process, after the first overview)
=================  ===========================================================================

In-process changes (Manager actions) are also published directly, so the UI does not wait for
the next poll. Events are idempotent updates: a duplicate is harmless.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import anyio.to_thread

from ..control import CONTROL_FILE, ActivityLog
from ..paths import is_valid_id

log = logging.getLogger("profilepilot.ui.events")

POLL_INTERVAL = 1.0
LIVENESS_EVERY = 3
"""Every N polls the hosts of running profiles are checked for liveness (a killed host leaves its
runtime.json behind)."""
IDLE_STOP = 30.0
"""Stop polling after this many seconds without subscribers."""
QUEUE_MAX = 1000

_PROFILE_FILES = ("profile.json", "runtime.json", "last_exit.json", CONTROL_FILE)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


class EventHub:
    """Fan-out of events to SSE subscribers, fed by in-process publishers and a file poller.

    ``profile_view`` builds the JSON view of one profile (``None`` if it no longer exists);
    ``liveness`` returns a hashable "is it really running" signature for one profile id; both run
    in a worker thread.
    """

    def __init__(self, root: Path, activity: ActivityLog, *,
                 profile_view: Callable[[str], dict[str, Any] | None],
                 open_help: Callable[[], list[dict[str, Any]]],
                 liveness: Callable[[str], Any] | None = None,
                 interval: float = POLL_INTERVAL) -> None:
        self.root = Path(root)
        self.activity = activity
        self.profile_view = profile_view
        self.open_help = open_help
        self.liveness = liveness
        self.interval = interval
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._cursor: tuple[int, int] | None = None
        self._sigs: dict[str, Any] | None = None
        self._files: dict[str, Any] = {}
        self._help_seen: set[str] | None = None
        self._ticks = 0
        self._idle_since: float | None = None
        self._closed = False

    # ------------------------------------------------------------------ subscribers

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._subscribers.add(queue)
        self._idle_since = None
        if self._cursor is None:
            self._cursor = self.activity.cursor()  # baseline now: later appends become events
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="profilepilot-ui-poller")
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
        if not self._subscribers:
            self._idle_since = time.monotonic()

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: Any) -> None:
        """Send ``event`` to every subscriber (call from the event loop thread)."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:  # a stuck client: drop it, EventSource reconnects
                self._subscribers.discard(queue)
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(None)

    async def close(self) -> None:
        self._closed = True
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        self._subscribers.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    # ------------------------------------------------------------------ polling

    async def _run(self) -> None:
        try:
            while not self._closed:
                if not self._subscribers and self._idle_since is not None \
                        and time.monotonic() - self._idle_since > IDLE_STOP:
                    self._cursor = None
                    self._sigs = None
                    self._help_seen = None
                    self._files = {}
                    return
                try:
                    events = await anyio.to_thread.run_sync(self._scan)
                except Exception as exc:  # never let one bad poll kill the stream
                    log.debug("event poll failed: %s", exc)
                    events = []
                for name, data in events:
                    self.publish(name, data)
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            pass

    def _scan(self) -> list[tuple[str, Any]]:
        """One poll (worker thread): the events since the previous one."""
        out: list[tuple[str, Any]] = []
        self._ticks += 1
        # -- activity
        if self._cursor is None:
            self._cursor = self.activity.cursor()
        else:
            new, self._cursor = self.activity.read_new(self._cursor)
            out.extend(("activity", e.model_dump(mode="json")) for e in new[-200:])
        # -- shared files
        for name, event in (("proxies.json", "proxies"), ("identities.json", "identities"),
                            ("config.json", "settings"), ("chatgpt.json", "chatgpt")):
            stamp = _mtime(self.root / name)
            if name in self._files and self._files[name] != stamp:
                out.append((event, {}))
            self._files[name] = stamp
        trash = self.root / "trash"
        trash_sig = None
        if trash.is_dir():
            with contextlib.suppress(OSError):
                trash_sig = tuple(sorted(p.name for p in trash.iterdir()))
        if "trash" in self._files and self._files["trash"] != trash_sig:
            out.append(("trash", {}))
        self._files["trash"] = trash_sig
        # -- profiles
        check_live = self.liveness is not None and self._ticks % LIVENESS_EVERY == 0
        sigs: dict[str, Any] = {}
        profiles_dir = self.root / "profiles"
        if profiles_dir.is_dir():
            for entry in profiles_dir.iterdir():
                if not entry.is_dir() or not is_valid_id(entry.name) or not (entry / "profile.json").exists():
                    continue
                stamps = tuple(_mtime(entry / f) for f in _PROFILE_FILES)
                live = None
                if stamps[1] is not None and self.liveness is not None:  # runtime.json exists
                    previous = (self._sigs or {}).get(entry.name)
                    if check_live or previous is None or previous[0][1] != stamps[1]:
                        try:
                            live = self.liveness(entry.name)
                        except Exception:
                            live = None
                    else:
                        live = previous[1]
                sigs[entry.name] = (stamps, live)
        if self._sigs is not None:
            changed = [pid for pid, sig in sigs.items() if self._sigs.get(pid) != sig]
            removed = [pid for pid in self._sigs if pid not in sigs]
            for pid in changed:
                view = self.profile_view(pid)
                if view is not None:
                    out.append(("profile", view))
            for pid in removed:
                out.append(("profile-removed", {"id": pid}))
            if changed or removed:
                out.extend(self._new_help())
        else:
            self._help_seen = {r["id"] for r in self.open_help()}
        self._sigs = sigs
        return out

    def _new_help(self) -> list[tuple[str, Any]]:
        current = self.open_help()
        seen = self._help_seen or set()
        fresh = [("help", r) for r in current if r["id"] not in seen]
        self._help_seen = {r["id"] for r in current}
        return fresh

    def mark_help_seen(self, request_id: str) -> None:
        """A request the Manager itself published (do not announce it twice)."""
        if self._help_seen is not None:
            self._help_seen.add(request_id)


def sse_format(event: str, data: Any, event_id: int | None = None) -> str:
    """One Server-Sent Events message."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\n" + "".join(f"data: {line}\n" for line in payload.splitlines() or [""]) + "\n"


__all__ = ["EventHub", "sse_format"]
