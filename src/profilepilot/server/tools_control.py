"""Human handoff for the AI: ``profile_request_help`` plus the pause / activity hooks of ``tool_guard``.

``register(server)`` adds the tool. The hooks are used by :func:`profilepilot.server.app.tool_guard`
(see docs/design/WIRE-IN.md, "Manager & control"):

* :func:`enforce_pause` refuses every browser, form, cookie and http tool (and ``profile_stop`` /
  ``profile_set_proxy``, which would pull the window or the exit IP out from under the user) on a
  profile the user controls or that waits for the user's help;
* :func:`log_activity` appends one scrubbed :class:`~profilepilot.control.ActivityEvent` per tool call,
  which ProfilePilot Manager shows live;
* :func:`control_lines` gives ``profile_status`` the pause and help-request state.

None of these hooks may break a tool call: they log and swallow their own failures (except the
deliberate :class:`~profilepilot.control.ProfilePausedError`).
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import Field

from ..control import (
    KIND_LABELS,
    ActivityEvent,
    ActivityLog,
    ControlStore,
    ProfilePausedError,
    control_status_lines,
    manager_info,
)
from ..errors import NotFoundError, ProfilePilotError
from .app import ProfileArg, add_tool, get_state, run_sync

log = logging.getLogger("profilepilot.server.control")

SHARDX_PREFIX = "shardx:"

GUARDED_PREFIXES: tuple[str, ...] = ("browser_", "form_", "cookies_", "http_")
"""Tools refused on a paused profile: everything that touches its pages, cookies or network."""
GUARDED_TOOLS: frozenset[str] = frozenset({"profile_stop", "profile_set_proxy"})
"""Profile management tools that would disrupt the user's work in the window (closing it, or
switching the exit IP mid-login). Everything else (profile_status, profile_list, profile_update,
proxy_*, identity_*, profile_request_help) stays allowed."""

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

    Unknown profiles pass (the tool reports them itself); any other failure here is logged and
    ignored so that the pause check can never break a tool."""
    if not is_guarded_tool(tool):
        return
    ref = _profile_ref(kwargs)
    state = _state_of(ctx if ctx is not None else kwargs.get("ctx"))
    if ref is None or state is None:
        return
    control = ControlStore(state.store)
    try:
        await run_sync(control.check_not_paused, ref)
    except ProfilePausedError:
        raise
    except (NotFoundError, ProfilePilotError):
        return
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("pause check failed for %s: %s", tool, exc)


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
                       started: float | None = None, source: str | None = None) -> None:
    """Append an :class:`ActivityEvent` for one tool call (never raises).

    ``result`` is the tool's return value, or the error (message) for a failed call; only its first
    line is kept, scrubbed of credentials and of values ``form_autofill_sensitive`` filled."""
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
                                  client=client or None, tool=tool, summary=summary, ok=ok, ms=ms)
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


__all__ = [
    "GUARDED_PREFIXES",
    "GUARDED_TOOLS",
    "client_name",
    "control_lines",
    "enforce_pause",
    "is_guarded_tool",
    "log_activity",
    "profile_request_help",
    "register",
    "result_text",
]
