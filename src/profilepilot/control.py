"""Human handoff: pausing profiles, help requests from the AI, and the activity log.

This is the shared control layer of ProfilePilot Manager (the UI), the AI-side tools and the CLI.

* **Pause** - the user takes control of a profile ("Take control" in the Manager). While a profile
  is paused, the AI tools refuse to act on it (see :class:`ProfilePausedError`) until the user hands
  it back ("Hand back to AI").
* **Help requests** - the AI asks the user for something it must not or cannot do itself: solve a
  CAPTCHA, type a 2FA code, log in, confirm a payment step. A request pauses the profile; it is
  handed back when no open request remains (unless the user also paused it separately).
* **Activity** - every tool call (and every Manager action) is appended to a rotating JSONL log so
  the user can see what the AI is doing, and when.

Storage (all atomic, guarded by cross-process locks, so several MCP servers, the CLI and the
Manager can share them)::

    profiles/<id>/control.json   {"pause": PauseInfo | null, "requests": [HelpRequest, ...]}
    activity.jsonl               one ActivityEvent per line (rotated at 5 MB: .1, .2)

Messages produced here are written for the model to act on, e.g. the refusal of a paused profile:
"The user has taken control of profile 'shop-us' (since 14:02: 'logging in'). Don't act on this
profile now. Wait and check profile_status, or ask the user."
"""

from __future__ import annotations

import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import ConflictError, NotFoundError, ProfilePilotError
from .jsonio import lock_for, read_json, write_json
from .models import Profile
from .paths import is_valid_id

HelpKind = Literal["captcha", "login", "verification", "payment", "other"]
HelpStatus = Literal["open", "done", "dismissed"]

CONTROL_FILE = "control.json"
ACTIVITY_FILE = "activity.jsonl"
MAX_MESSAGE = 500
"""Longest help request message kept (characters)."""
MAX_NOTE = 200
MAX_OPEN_PER_PROFILE = 5
"""More open help requests than this for one profile are refused (the user has not answered yet)."""
KEEP_RESOLVED = 20
"""Resolved requests kept per profile (newest first) so profile_status can report the outcome."""
RECENT_RESOLVED = timedelta(hours=1)
"""How long a resolved request is reported back to the AI by :func:`control_status_lines`."""
SUMMARY_MAX = 200
ACTIVITY_MAX_BYTES = 5 * 1024 * 1024
ACTIVITY_KEEP = 2

KIND_LABELS: dict[str, str] = {
    "captcha": "CAPTCHA",
    "login": "login",
    "verification": "verification code",
    "payment": "payment step",
    "other": "help",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now_ms() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _clean_text(value: Any, limit: int) -> str:
    """Single-spaced printable text, at most ``limit`` characters (control characters removed)."""
    text = "" if value is None else str(value)
    text = "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clock(dt: datetime | None) -> str:
    """Local wall-clock time for messages: ``14:02`` today, ``Mar 03 14:02`` otherwise."""
    if dt is None:
        return "?"
    local = dt.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return local.strftime("%H:%M")
    return local.strftime("%b %d %H:%M")


# --------------------------------------------------------------------------- models


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PauseInfo(_Model):
    """Why a profile is paused. ``by="user"``: the user took control; ``by="help"``: the AI asked the
    user for help and waits (``request_id`` names the oldest open request)."""

    paused: bool = True
    by: Literal["user", "help"] = "user"
    since: datetime = Field(default_factory=_now)
    note: str = ""
    request_id: str | None = None

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, value: Any) -> str:
        return _clean_text(value, MAX_MESSAGE)


class HelpRequest(_Model):
    """A request from the AI for the user to do something in a profile's browser window."""

    id: str
    profile_id: str
    message: str
    kind: HelpKind = "other"
    created_at: datetime = Field(default_factory=_now)
    status: HelpStatus = "open"
    resolved_at: datetime | None = None
    note: str = ""
    pauses: bool = True
    """The request pauses the profile while it is open (the default)."""
    requested_by: str = ""
    """The MCP client that asked (``clientInfo.name``, e.g. ``claude-ai``); may be empty."""

    @field_validator("message", mode="before")
    @classmethod
    def _message(cls, value: Any) -> str:
        return _clean_text(value, MAX_MESSAGE)

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, value: Any) -> str:
        return _clean_text(value, MAX_NOTE)

    @field_validator("requested_by", mode="before")
    @classmethod
    def _client(cls, value: Any) -> str:
        return _clean_text(value, 64)

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class ControlState(_Model):
    """Everything the Manager and ``profile_status`` show about one profile's control state."""

    profile_id: str
    pause: PauseInfo | None = None
    """The user's own pause (Take control), if any."""
    open: list[HelpRequest] = Field(default_factory=list)
    resolved: list[HelpRequest] = Field(default_factory=list)
    """Resolved requests, newest first."""

    @property
    def effective(self) -> PauseInfo | None:
        """The pause that applies to the AI: the user's own, else the oldest pausing open request."""
        if self.pause is not None and self.pause.paused:
            return self.pause
        pausing = [r for r in self.open if r.pauses]
        if not pausing:
            return None
        oldest = min(pausing, key=lambda r: r.created_at)
        return PauseInfo(paused=True, by="help", since=oldest.created_at, note=oldest.message, request_id=oldest.id)

    def as_dict(self) -> dict[str, Any]:
        eff = self.effective
        return {
            "paused": eff is not None,
            "pause": eff.model_dump(mode="json") if eff else None,
            "user_pause": self.pause.model_dump(mode="json") if self.pause else None,
            "help": [r.model_dump(mode="json") for r in self.open],
            "recent": [r.model_dump(mode="json") for r in self.resolved[:5]],
        }


class ProfilePausedError(ConflictError):
    """An AI tool tried to act on a profile the user controls (or that waits for the user's help)."""

    def __init__(self, message: str, *, profile_id: str, pause: PauseInfo) -> None:
        super().__init__(message)
        self.profile_id = profile_id
        self.pause = pause


def refusal_message(profile_name: str, pause: PauseInfo) -> str:
    """The refusal an AI tool returns for a paused profile (actionable, model-facing)."""
    if pause.by == "help":
        what = f" ('{pause.note}')" if pause.note else ""
        return (
            f"Profile '{profile_name}' is waiting for the user: you asked for help at {clock(pause.since)}{what}. "
            "Don't act on this profile until the user hands it back in ProfilePilot Manager. "
            "Check profile_status in a while, or ask the user."
        )
    note = f": '{pause.note}'" if pause.note else ""
    return (
        f"The user has taken control of profile '{profile_name}' (since {clock(pause.since)}{note}). "
        "Don't act on this profile now. Wait and check profile_status, or ask the user."
    )


# --------------------------------------------------------------------------- control store


class ControlStore:
    """Pause state and help requests, one ``control.json`` per profile.

    ``ref`` arguments accept a profile id, name or unique id prefix (like :meth:`Store.get_profile`).
    """

    def __init__(self, store: Any) -> None:  # store: profilepilot.store.Store (no import cycle)
        self.store = store

    # ------------------------------------------------------------------ io

    def _path(self, profile_id: str) -> Path:
        return self.store.profile_dir(profile_id) / CONTROL_FILE

    def _read(self, profile_id: str) -> dict[str, Any]:
        data = read_json(self._path(profile_id), {}) or {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _parse(profile_id: str, data: dict[str, Any]) -> ControlState:
        pause = None
        raw = data.get("pause")
        if isinstance(raw, dict):
            try:
                pause = PauseInfo.model_validate(raw)
            except Exception:
                pause = None
        requests: list[HelpRequest] = []
        for item in data.get("requests") or []:
            try:
                requests.append(HelpRequest.model_validate(item))
            except Exception:
                continue
        open_ = sorted((r for r in requests if r.is_open), key=lambda r: r.created_at)
        resolved = sorted((r for r in requests if not r.is_open),
                          key=lambda r: r.resolved_at or r.created_at, reverse=True)
        return ControlState(profile_id=profile_id, pause=pause if pause and pause.paused else None,
                            open=open_, resolved=resolved)

    def _mutate(self, profile: Profile, fn: Callable[[ControlState], Any]) -> Any:
        folder = self.store.profile_dir(profile.id)
        if not folder.is_dir():  # never resurrect the folder of a profile deleted meanwhile
            raise NotFoundError(f"Profile '{profile.name}' no longer exists.")
        path = self._path(profile.id)
        with lock_for(path):
            state = self._parse(profile.id, self._read(profile.id))
            result = fn(state)
            requests = [*state.open, *state.resolved[:KEEP_RESOLVED]]
            write_json(path, {
                "pause": state.pause.model_dump(mode="json") if state.pause else None,
                "requests": [r.model_dump(mode="json") for r in requests],
            })
        return result

    # ------------------------------------------------------------------ queries

    def state(self, ref: str) -> ControlState:
        profile = self.store.get_profile(ref)
        return self._parse(profile.id, self._read(profile.id))

    def state_by_id(self, profile_id: str) -> ControlState:
        """Like :meth:`state` for a known id (no profile lookup; empty state for a missing file)."""
        if not is_valid_id(profile_id):
            raise ProfilePilotError(f"Invalid profile id: {profile_id!r}")
        return self._parse(profile_id, self._read(profile_id))

    def paused(self, ref: str) -> PauseInfo | None:
        """The pause that applies to the AI (the user's own, or an open help request), else None."""
        return self.state(ref).effective

    def check_not_paused(self, ref: str) -> None:
        """Raise :class:`ProfilePausedError` (with the model-facing refusal) if ``ref`` is paused."""
        profile = self.store.get_profile(ref)
        pause = self._parse(profile.id, self._read(profile.id)).effective
        if pause is not None:
            raise ProfilePausedError(refusal_message(profile.name, pause), profile_id=profile.id, pause=pause)

    def help_requests(self, *, open_only: bool = True) -> list[HelpRequest]:
        """Help requests across all profiles (oldest first)."""
        out: list[HelpRequest] = []
        root = self.store.profiles_dir
        if not root.exists():
            return out
        for entry in root.iterdir():
            if not entry.is_dir() or not is_valid_id(entry.name) or not (entry / CONTROL_FILE).exists():
                continue
            state = self._parse(entry.name, self._read(entry.name))
            out.extend(state.open)
            if not open_only:
                out.extend(state.resolved)
        out.sort(key=lambda r: r.created_at)
        return out

    def get_request(self, ref: str, request_id: str) -> HelpRequest:
        state = self.state(ref)
        for req in [*state.open, *state.resolved]:
            if req.id == request_id:
                return req
        raise NotFoundError(f"Help request '{request_id}' not found.")

    # ------------------------------------------------------------------ mutations

    def pause(self, ref: str, note: str = "") -> PauseInfo:
        """The user takes control: AI tools refuse to act on the profile until :meth:`resume`.
        Pausing an already paused profile keeps its original ``since`` (the note is updated)."""
        profile = self.store.get_profile(ref)

        def apply(state: ControlState) -> PauseInfo:
            if state.pause is not None:
                if note:
                    state.pause.note = _clean_text(note, MAX_MESSAGE)
                return state.pause
            state.pause = PauseInfo(paused=True, by="user", since=_now(), note=note)
            return state.pause

        return self._mutate(profile, apply)

    def resume(self, ref: str, *, resolve_help: bool = True, note: str = "") -> list[HelpRequest]:
        """The user hands the profile back to the AI. Also marks every open help request as done
        (``resolve_help``): handing back means "I'm done here". Returns the requests it resolved."""
        profile = self.store.get_profile(ref)

        def apply(state: ControlState) -> list[HelpRequest]:
            state.pause = None
            closed: list[HelpRequest] = []
            if resolve_help:
                now = _now()
                for req in state.open:
                    req.status, req.resolved_at = "done", now
                    req.note = _clean_text(note, MAX_NOTE) if note else "handed back to the AI"
                    closed.append(req)
                state.resolved = [*closed, *state.resolved]
                state.open = []
            return closed

        return self._mutate(profile, apply)

    def request_help(self, ref: str, message: str, kind: HelpKind = "other", *, pause: bool = True,
                     requested_by: str = "") -> HelpRequest:
        """The AI asks the user for help; by default this pauses the profile until it is resolved."""
        profile = self.store.get_profile(ref)
        text = _clean_text(message, MAX_MESSAGE)
        if not text:
            raise ProfilePilotError("Describe what the user should do (message is empty).")
        if kind not in KIND_LABELS:
            raise ProfilePilotError(f"Unknown help kind {kind!r}; use one of: {', '.join(KIND_LABELS)}.")

        def apply(state: ControlState) -> HelpRequest:
            for req in state.open:  # the same question twice: return the open one
                if req.message.casefold() == text.casefold() and req.kind == kind:
                    return req
            if len(state.open) >= MAX_OPEN_PER_PROFILE:
                raise ConflictError(
                    f"Profile '{profile.name}' already has {len(state.open)} open help requests. Wait for the user "
                    "to answer them (check profile_status) instead of asking again."
                )
            req = HelpRequest(id=secrets.token_hex(4), profile_id=profile.id, message=text, kind=kind,
                              created_at=_now(), pauses=pause, requested_by=requested_by)
            state.open.append(req)
            return req

        return self._mutate(profile, apply)

    def resolve_help(self, ref: str, request_id: str, *, status: HelpStatus = "done", note: str = "") -> HelpRequest:
        """Close a help request (``done`` or ``dismissed``). The profile is handed back to the AI when
        no open request remains, unless the user paused it separately (Take control)."""
        if status not in ("done", "dismissed"):
            raise ProfilePilotError("status must be 'done' or 'dismissed'.")
        profile = self.store.get_profile(ref)

        def apply(state: ControlState) -> HelpRequest:
            for req in state.open:
                if req.id == request_id:
                    req.status, req.resolved_at, req.note = status, _now(), _clean_text(note, MAX_NOTE)
                    state.open = [r for r in state.open if r.id != request_id]
                    state.resolved = [req, *state.resolved]
                    return req
            for req in state.resolved:
                if req.id == request_id:
                    return req  # already resolved: idempotent
            raise NotFoundError(f"Help request '{request_id}' not found for profile '{profile.name}'.")

        return self._mutate(profile, apply)

    def clear(self, ref: str) -> None:
        """Forget the profile's pause and every request (e.g. after restoring it from the trash)."""
        profile = self.store.get_profile(ref)
        path = self._path(profile.id)
        if path.exists():
            with lock_for(path):
                path.unlink(missing_ok=True)


def control_status_lines(control: ControlStore, profile_id: str, *, now: datetime | None = None) -> list[str]:
    """Model-facing lines about the profile's control state (for ``profile_status``)."""
    state = control.state_by_id(profile_id)
    now = now or _now()
    lines: list[str] = []
    if state.pause is not None:
        note = f" ('{state.pause.note}')" if state.pause.note else ""
        lines.append(
            f"Paused: the user has taken control since {clock(state.pause.since)}{note}. Browser, form, cookie and "
            "http tools are refused until they hand it back in ProfilePilot Manager."
        )
    for req in state.open:
        lines.append(
            f"Open help request {req.id} ({KIND_LABELS.get(req.kind, req.kind)}, asked {clock(req.created_at)}): "
            f"'{req.message}' - waiting for the user"
            + ("; the profile is paused until they answer." if req.pauses else ".")
        )
    for req in state.resolved:
        when = req.resolved_at or req.created_at
        if now - when > RECENT_RESOLVED:
            break
        verb = "handled" if req.status == "done" else "dismissed"
        note = f" ('{req.note.rstrip('.')}')" if req.note else ""
        lines.append(f"The user {verb} your help request '{req.message}' at {clock(when)}{note}.")
    if not lines:
        return []
    if state.effective is None:
        lines.append("The profile is not paused: you may act on it.")
    return lines


# --------------------------------------------------------------------------- activity log

_CARDISH = re.compile(r"(?<![\d.])(?:\d[ -]?){12,18}\d(?![\d.])")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_SECRET_QUERY = re.compile(
    r"(?i)([?&#](?:access_token|refresh_token|id_token|token|api[_-]?key|apikey|key|secret|password|passwd|pwd|pass|"
    r"auth|code|session|sessionid|sid|sig|signature|otp|t)=)[^&#\s'\"]+"
)
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])")


def _opaque(word: str) -> bool:
    """A long run of letters and digits that looks like a key or token (not a URL slug or a word)."""
    digits = sum(ch.isdigit() for ch in word)
    letters = sum(ch.isalpha() for ch in word)
    return digits >= 6 and letters >= 6 and word.count("-") < 4


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scrub_text(text: Any, *, extra: Iterable[str] = (), redact: Callable[[str], str] | None = None,
               limit: int = SUMMARY_MAX) -> str:
    """One line of ``text``, safe to store and show: proxy credentials, bearer tokens, JWTs, secret
    URL parameters, long opaque tokens, card numbers and SSNs are masked, as are the ``extra``
    strings (e.g. values a sensitive autofill typed) and whatever ``redact`` replaces."""
    raw = "" if text is None else str(text)
    line = ""
    for candidate in raw.splitlines():
        if candidate.strip():
            line = candidate.strip()
            break
    if not line:
        return ""
    if redact is not None:
        try:
            line = redact(line)
        except Exception:
            pass
    for secret in extra:
        if secret and len(secret) >= 4:
            line = line.replace(secret, "[redacted]")
    try:
        from .integrations.shardx import redact_secrets

        line = redact_secrets(line)
    except Exception:  # pragma: no cover - keep a minimal fallback
        line = re.sub(r"://[^\s/@]*@", "://***@", line)
    line = _SECRET_QUERY.sub(r"\1***", line)

    def card(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[card]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    line = _CARDISH.sub(card, line)
    line = _SSN.sub("[redacted]", line)
    line = _LONG_TOKEN.sub(lambda m: "***" if _opaque(m.group(0)) else m.group(0), line)
    line = "".join(ch if ch.isprintable() else " " for ch in line)
    line = " ".join(line.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


class ActivityEvent(_Model):
    """One tool call or Manager action. ``summary`` is scrubbed and at most 200 characters."""

    ts: datetime = Field(default_factory=_now_ms)
    profile_id: str | None = None
    profile_name: str | None = None
    source: str = "mcp"
    """``mcp`` (stdio clients), ``mcp-http`` (remote clients such as ChatGPT), ``ui`` or ``cli``."""
    client: str | None = None
    """The MCP client's name (``clientInfo.name``), when known."""
    tool: str
    summary: str = ""
    ok: bool = True
    ms: int = 0

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: Any) -> str:
        return scrub_text(value)

    @field_validator("tool", "source", mode="before")
    @classmethod
    def _short(cls, value: Any) -> str:
        return _clean_text(value, 64)

    @field_validator("profile_name", "client", mode="before")
    @classmethod
    def _name(cls, value: Any) -> str | None:
        return None if value is None else _clean_text(value, 64)

    @field_validator("ms", mode="before")
    @classmethod
    def _ms(cls, value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0


class ActivityLog:
    """Append-only, rotating JSONL log of :class:`ActivityEvent` (multi-process safe)."""

    def __init__(self, root: Path | str, *, max_bytes: int = ACTIVITY_MAX_BYTES, keep: int = ACTIVITY_KEEP) -> None:
        self.root = Path(root)
        self.path = self.root / ACTIVITY_FILE
        self.max_bytes = max_bytes
        self.keep = keep

    def _rotated(self, n: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{n}")

    def append(self, event: ActivityEvent) -> None:
        """Append ``event`` (one line). Rotates the file first when it exceeds ``max_bytes``."""
        line = event.model_dump_json() + "\n"
        self.root.mkdir(parents=True, exist_ok=True)
        with lock_for(self.path):
            try:
                if self.path.stat().st_size >= self.max_bytes:
                    self._rotate()
            except FileNotFoundError:
                pass
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)

    def _rotate(self) -> None:
        for attempt in range(5):
            try:
                oldest = self._rotated(self.keep)
                oldest.unlink(missing_ok=True)
                for n in range(self.keep - 1, 0, -1):
                    src = self._rotated(n)
                    if src.exists():
                        os.replace(src, self._rotated(n + 1))
                os.replace(self.path, self._rotated(1))
                return
            except PermissionError:  # a reader holds a file for a moment (Windows)
                time.sleep(0.05 * (attempt + 1))
        # Could not rotate: keep appending to the current file rather than losing events.

    def files(self) -> list[Path]:
        """Log files, newest first (the current file, then .1, .2, ...)."""
        return [p for p in [self.path, *(self._rotated(n) for n in range(1, self.keep + 1))] if p.exists()]

    def tail(self, limit: int = 200, *, profile_id: str | None = None, since: datetime | None = None,
             tool: str | None = None, ok: bool | None = None) -> list[ActivityEvent]:
        """The newest ``limit`` matching events, oldest first."""
        limit = max(1, min(int(limit), 5000))
        found: list[ActivityEvent] = []
        for path in self.files():
            for raw in _reverse_lines(path):
                event = _parse_event(raw)
                if event is None:
                    continue
                if since is not None and event.ts <= since:
                    found.reverse()
                    return found
                if profile_id and event.profile_id != profile_id:
                    continue
                if tool and tool.lower() not in event.tool.lower():
                    continue
                if ok is not None and event.ok != ok:
                    continue
                found.append(event)
                if len(found) >= limit:
                    found.reverse()
                    return found
        found.reverse()
        return found

    # ---------------------------------------------------------------- incremental reading (SSE)

    def cursor(self) -> tuple[int, int]:
        """A cursor at the current end of the log (``(file id, offset)``)."""
        try:
            st = self.path.stat()
            return (_file_id(st), st.st_size)
        except FileNotFoundError:
            return (0, 0)

    def read_new(self, cursor: tuple[int, int]) -> tuple[list[ActivityEvent], tuple[int, int]]:
        """Events appended since ``cursor``, and the new cursor. A rotation restarts at the top of
        the new file (events written to the old file after the cursor are skipped)."""
        file_id, offset = cursor
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return [], (0, 0)
        current = _file_id(st)
        if current != file_id or st.st_size < offset:
            offset = 0
        if st.st_size == offset:
            return [], (current, offset)
        try:
            with open(self.path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(4 * 1024 * 1024)
        except OSError:
            return [], (file_id, cursor[1])
        end = data.rfind(b"\n")
        if end < 0:
            return [], (current, offset)
        events = [e for e in (_parse_event(line) for line in data[: end + 1].splitlines()) if e is not None]
        return events, (current, offset + end + 1)


def _file_id(st: os.stat_result) -> int:
    ino = getattr(st, "st_ino", 0) or 0
    return int(ino) or int(getattr(st, "st_ctime_ns", 0) or 0)


def _parse_event(raw: bytes | str) -> ActivityEvent | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = raw.strip()
    if not raw:
        return None
    try:
        return ActivityEvent.model_validate_json(raw)
    except Exception:
        return None


def _reverse_lines(path: Path, block: int = 64 * 1024) -> Iterator[bytes]:
    """Lines of ``path`` from the last to the first."""
    try:
        fh = open(path, "rb")
    except OSError:
        return
    with fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        rest = b""
        while pos > 0:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            chunk = fh.read(step) + rest
            lines = chunk.split(b"\n")
            rest = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        if rest.strip():
            yield rest


def activity_for(store: Any) -> ActivityLog:
    """The activity log of ``store``'s data root."""
    return ActivityLog(store.root)


# --------------------------------------------------------------------------- manager presence

UI_STATE_FILE = "ui.json"
"""``<root>/ui.json``: ``{"pid", "port", "started_at"}`` of the running ProfilePilot Manager
(written by :mod:`profilepilot.ui.launcher`; its access token lives in the secret store)."""


def manager_info(root: Path | str) -> dict[str, Any] | None:
    """``ui.json`` of a ProfilePilot Manager that is running for this data root, else None."""
    data = read_json(Path(root) / UI_STATE_FILE)
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data.get("pid") or 0)
        started = float(data.get("started_at") or 0) or None
    except (TypeError, ValueError):
        return None
    from .procs import pid_started_before

    return data if pid > 0 and pid_started_before(pid, started) else None


__all__ = [
    "ActivityEvent",
    "ActivityLog",
    "ControlState",
    "ControlStore",
    "HelpKind",
    "HelpRequest",
    "KIND_LABELS",
    "PauseInfo",
    "ProfilePausedError",
    "activity_for",
    "clock",
    "control_status_lines",
    "manager_info",
    "refusal_message",
    "scrub_text",
]
