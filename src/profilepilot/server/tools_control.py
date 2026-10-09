"""Human handoff for the AI: ``profile_request_help`` plus the pause / activity hooks of ``tool_guard``.

``register(server)`` adds the tool. The hooks are used by :func:`profilepilot.server.app.tool_guard`
(see docs/design/WIRE-IN.md, "Manager & control"):

* :func:`enforce_pause` refuses every browser, form, cookie and http tool (and the profile tools that
  would pull the window, its settings, its data or its exit IP out from under the user: stop, start,
  update, set_proxy, clone, delete, and ``proxy_remove(force=True)`` of a proxy such a profile uses) on
  a profile the user controls or that waits for the user's help. It fails closed: when the pause
  state cannot be read, the tool is refused too;
* :func:`run_unless_paused` cancels a running page tool as soon as its profile gets paused (the user
  took control mid-call), so e.g. humanized typing never continues into the user's window;
* :func:`log_activity` appends one scrubbed :class:`~profilepilot.control.ActivityEvent` per tool call,
  which ProfilePilot Manager shows live;
* :func:`control_lines` gives ``profile_status`` the pause and help-request state.

None of these hooks may break a tool call: they log and swallow their own failures, except the
deliberate refusals of :func:`enforce_pause` (:class:`~profilepilot.control.ProfilePausedError` and
:class:`~profilepilot.control.ControlStateError`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any, Awaitable, Callable, Literal, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import Field

from ..control import (
    CONTROL_FILE,
    KIND_LABELS,
    ActivityEvent,
    ActivityLog,
    ControlStateError,
    ControlStore,
    ProfilePausedError,
    clock,
    control_status_lines,
    manager_info,
    refusal_message,
)
from ..errors import AmbiguousError, NotFoundError, ProfilePilotError
from .app import ProfileArg, add_tool, get_state, run_sync

log = logging.getLogger("profilepilot.server.control")

T = TypeVar("T")

SHARDX_PREFIX = "shardx:"

GUARDED_PREFIXES: tuple[str, ...] = ("browser_", "form_", "cookies_", "http_")
"""Tools refused on a paused profile: everything that touches its pages, cookies or network."""
GUARDED_TOOLS: frozenset[str] = frozenset({
    "profile_stop", "profile_start", "profile_set_proxy", "profile_update", "profile_clone", "profile_delete",
})
"""Profile management tools that would disrupt the user's work in the window: closing or (re)starting
it, switching its exit IP or settings mid-login, copying its logged-in data, or deleting it.
``proxy_remove(force=True)`` is checked separately (:func:`enforce_pause`). Everything else
(profile_status, profile_list, proxy_list/add/test, identity_*, profile_request_help) stays allowed."""
PAUSE_POLL_S = 0.4
"""While a page tool (:data:`GUARDED_PREFIXES`) runs, how often its profile's pause is re-checked: a pause
that starts mid-call stops the call (see :func:`run_unless_paused`)."""

HelpKindArg = Annotated[
    Literal["captcha", "login", "verification", "payment", "other"],
    Field(description="What kind of help: captcha, login, verification (2FA / e-mail / SMS code), payment, or other."),
]


# ---------------------------------------------------------------------- the tool


async def profile_request_help(
    ctx: Context,
    profile: ProfileArg,
    message: Annotated[str, Field(
        description="What the user should do, in one or two plain sentences, e.g. 'Solve the CAPTCHA on the "
                    "sign-in page, then click Done.' The user sees it in ProfilePilot Manager.",
        min_length=3, max_length=500)],
    kind: HelpKindArg = "other",
) -> str:
    """Ask the user to do something in a profile's browser window that you must not or cannot do yourself:
    solve a CAPTCHA, enter a 2FA / verification code, log in, or confirm a payment step. The request shows up
    in ProfilePilot Manager (with a desktop notification) and the profile is paused: browser tools are refused
    until the user hands it back. Then call profile_status to see the outcome."""
    state = get_state(ctx)
    if profile.strip().lower().startswith(SHARDX_PREFIX):
        raise ProfilePilotError("Help requests work for ProfilePilot profiles only, not ShardX profiles.")
    control = ControlStore(state.store)
    client = client_name(ctx)

    def ask() -> tuple[Any, Any, dict | None]:
        target = state.store.get_profile(profile)
        req = control.request_help(target.id, message, kind, pause=True, requested_by=client)
        return target, req, manager_info(state.store.root)

    target, req, manager = await run_sync(ask)
    what = KIND_LABELS.get(req.kind, "help")
    lines = [
        f"The user has been asked in ProfilePilot Manager ({what}): '{req.message}'.",
        f"Profile '{target.name}' is paused until they hand it back (request id {req.id}). "
        "Check profile_status in a while; don't use browser tools on it meanwhile.",
    ]
    if manager is None:
        lines.append(
            "Note: ProfilePilot Manager is not open, so the user may not see the request yet. Tell them in your "
            "reply as well: they can open it with the 'ProfilePilot Manager' shortcut or `profilepilot ui`, and "
            "the profile's browser window is where they act."
        )
    return "\n".join(lines)


def register(server: MCPServer, state: Any = None) -> None:
    """Register the control tools. ``state`` is accepted for symmetry and unused: tools read the
    :class:`~profilepilot.server.app.AppState` from their context at call time."""
    add_tool(server, profile_request_help, title="Ask the user for help", read_only=False, destructive=False,
             idempotent=False, open_world=False, invoking="Asking the user…", invoked="The user was asked")


# ---------------------------------------------------------------------- tool_guard hooks


def is_guarded_tool(tool: str) -> bool:
    """Is ``tool`` refused on a paused profile?"""
    return tool.startswith(GUARDED_PREFIXES) or tool in GUARDED_TOOLS


def _profile_ref(kwargs: dict[str, Any]) -> str | None:
    ref = kwargs.get("profile")
    if isinstance(ref, str) and ref.strip() and not ref.strip().lower().startswith(SHARDX_PREFIX):
        return ref.strip()
    return None


def _state_of(ctx: Any) -> Any:
    if ctx is None:
        return None
    try:
        return get_state(ctx)
    except Exception:
        return None


async def enforce_pause(ctx: Any, tool: str, kwargs: dict[str, Any]) -> None:
    """Raise :class:`ProfilePausedError` when ``tool`` would act on a paused profile.

    Unknown or ambiguous profiles pass (the tool reports them itself). Everything else fails closed:
    when the profile's pause state cannot be read, :class:`ControlStateError` refuses the tool."""
    guarded = is_guarded_tool(tool)
    if not guarded and not (tool == "proxy_remove" and kwargs.get("force") is True):
        return
    state = _state_of(ctx if ctx is not None else kwargs.get("ctx"))
    if state is None:
        return
    if not guarded:
        await _enforce_proxy_users(state, kwargs.get("proxy"))
        return
    ref = _profile_ref(kwargs)
    if ref is None:
        return
    control = ControlStore(state.store)
    try:
        await run_sync(control.check_not_paused, ref)
    except (ProfilePausedError, ControlStateError):
        raise
    except (NotFoundError, AmbiguousError):
        return
    except Exception as exc:
        log.warning("pause check failed for %s: %s", tool, exc)
        raise ControlStateError(
            f"Could not check whether the user controls profile '{ref}' right now, so {tool} was not run. "
            "Try again in a moment."
        ) from None


async def _enforce_proxy_users(state: Any, proxy_ref: Any) -> None:
    """``proxy_remove(force=True)``: refuse while a profile that uses the proxy is paused (removing it
    would switch that profile to a direct connection under the user)."""
    if not isinstance(proxy_ref, str) or not proxy_ref.strip():
        return
    store = state.store

    def check() -> None:
        try:
            record = store.get_proxy(proxy_ref.strip())
        except (NotFoundError, AmbiguousError):
            return  # proxy_remove reports it
        control = ControlStore(store)
        for profile in store.list_profiles():
            if profile.proxy_id != record.id:
                continue
            pause = control.state_by_id(profile.id).effective
            if pause is not None:
                who = "waits for the user's help" if pause.by == "help" else "is controlled by the user"
                raise ProfilePausedError(
                    f"Proxy '{record.name}' is used by profile '{profile.name}', which {who} right now "
                    f"(since {clock(pause.since)}). Don't remove it now: wait and check profile_status, or ask the user.",
                    profile_id=profile.id, pause=pause,
                )

    try:
        await run_sync(check)
    except (ProfilePausedError, ControlStateError):
        raise
    except Exception as exc:
        log.warning("pause check for proxy_remove failed: %s", exc)
        raise ControlStateError(
            "Could not check whether the user controls a profile that uses this proxy, so proxy_remove was not "
            "run. Try again in a moment."
        ) from None


def stopped_message(profile_name: str, tool: str, pause: Any) -> str:
    """The error of a page tool that was cancelled because a pause started while it ran."""
    return f"{tool} was stopped before it finished. " + refusal_message(profile_name, pause)


async def run_unless_paused(ctx: Any, tool: str, kwargs: dict[str, Any], call: Callable[[], Awaitable[T]]) -> T:
    """Run a tool body (``call()``), cancelling it the moment its profile gets paused.

    :func:`enforce_pause` only checks *before* a call. A page tool can run for seconds (humanized
    typing, waits, long navigations); if the user takes control meanwhile (or a help request pauses the
    profile), the call is cancelled at once - so typing never continues into whatever field the user
    has focused - and :class:`ProfilePausedError` reports it. Only page tools (:data:`GUARDED_PREFIXES`)
    are watched; the check is a ``stat`` of ``control.json`` every :data:`PAUSE_POLL_S` seconds, and the
    file is only read when it changed."""
    if not tool.startswith(GUARDED_PREFIXES):
        return await call()
    state = _state_of(ctx if ctx is not None else kwargs.get("ctx"))
    ref = _profile_ref(kwargs)
    if state is None or ref is None:
        return await call()
    try:
        profile = await run_sync(state.store.get_profile, ref)
    except Exception:  # unknown / ambiguous: the tool reports it
        return await call()
    control = ControlStore(state.store)
    path = state.store.profile_dir(profile.id) / CONTROL_FILE

    def stamp() -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    seen = stamp()
    task = asyncio.ensure_future(call())
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=PAUSE_POLL_S)
            if done:
                return task.result()
            now = stamp()
            if now == seen:
                continue
            seen = now
            try:
                pause = await run_sync(lambda: control.state_by_id(profile.id, strict=False).effective)
            except Exception as exc:  # pragma: no cover - a display-grade read; enforce_pause guards the next call
                log.debug("in-flight pause check failed for %s: %s", tool, exc)
                continue
            if pause is None:
                continue
            task.cancel()
            await asyncio.wait({task}, timeout=5)
            log.info("%s on profile %s was stopped: the profile was paused while it ran", tool, profile.id)
            raise ProfilePausedError(stopped_message(profile.name, tool, pause), profile_id=profile.id, pause=pause)
    finally:
        if not task.done():
            task.cancel()


def client_name(ctx: Any) -> str:
    """The MCP client's ``clientInfo.name`` (e.g. ``claude-ai``, ``openai-mcp``), or ''."""
    try:
        params = ctx.session.client_params
        info = getattr(params, "client_info", None) if params is not None else None
        return str(getattr(info, "name", "") or "")[:64]
    except Exception:
        return ""


def result_text(result: Any) -> str:
    """The first text of a tool result (str, content list or result object)."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        for item in result:
            text = item if isinstance(item, str) else getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                return text
        return "[image]" if result else ""
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(result, "content", None)
    if isinstance(content, (list, tuple)):
        return result_text(content)
    return ""


async def log_activity(ctx: Any, tool: str, kwargs: dict[str, Any], *, ok: bool, result: Any = None,
                       started: float | None = None, source: str | None = None, blocked: bool = False) -> None:
    """Append an :class:`ActivityEvent` for one tool call (never raises).

    ``result`` is the tool's return value, or the error (message) for a failed call; only its first
    line is kept, scrubbed of credentials and of values ``form_autofill_sensitive`` filled.
    ``blocked`` marks a call refused because the user controls the profile (the Manager shows it as
    "Blocked - you were in control", not as an error)."""
    try:
        state = _state_of(ctx if ctx is not None else kwargs.get("ctx"))
        if state is None:
            return
        ms = int((time.perf_counter() - started) * 1000) if started is not None else 0
        text = str(result) if isinstance(result, BaseException) else result_text(result)
        ref = _profile_ref(kwargs)
        if ref is None and tool == "profile_create" and isinstance(kwargs.get("name"), str):
            ref = kwargs["name"]
        raw_ref = kwargs.get("profile") if isinstance(kwargs.get("profile"), str) else None
        client = client_name(ctx if ctx is not None else kwargs.get("ctx"))
        src = source or ("mcp-http" if getattr(state, "remote", False) else "mcp")

        def write() -> None:
            profile_id = profile_name = None
            if ref is not None:
                try:
                    target = state.store.get_profile(ref)
                    profile_id, profile_name = target.id, target.name
                except Exception:
                    profile_name = ref[:64]
            elif raw_ref:
                profile_name = raw_ref[:64]
            redact = getattr(state, "redact", None)
            summary = text
            if callable(redact) and profile_id:
                try:
                    summary = redact(profile_id, summary)
                except Exception:
                    pass
            event = ActivityEvent(profile_id=profile_id, profile_name=profile_name, source=src,
                                  client=client or None, tool=tool, summary=summary, ok=ok, ms=ms,
                                  blocked=bool(blocked))
            ActivityLog(state.store.root).append(event)

        await run_sync(write)
    except Exception as exc:  # never let logging break a tool
        log.debug("activity logging failed for %s: %s", tool, exc)


def control_lines(store: Any, profile_id: str) -> list[str]:
    """``profile_status`` lines about the pause and help requests of ``profile_id`` (sync)."""
    try:
        return control_status_lines(ControlStore(store), profile_id)
    except Exception as exc:  # pragma: no cover - status must still work
        log.debug("control status unavailable for %s: %s", profile_id, exc)
        return []


def is_pause_refusal(exc: BaseException) -> bool:
    """Was ``exc`` raised because the user controls the profile (``enforce_pause``'s refusal)?"""
    return isinstance(exc, ProfilePausedError)


__all__ = [
    "GUARDED_PREFIXES",
    "GUARDED_TOOLS",
    "PAUSE_POLL_S",
    "client_name",
    "is_pause_refusal",
    "run_unless_paused",
    "stopped_message",
    "control_lines",
    "enforce_pause",
    "is_guarded_tool",
    "log_activity",
    "profile_request_help",
    "register",
    "result_text",
]
