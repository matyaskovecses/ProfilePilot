"""``shardx_*`` MCP tools (registered only when ``config.shardx.enabled``).

ShardX is an optional launcher that runs its own (fingerprint-spoofing) Chromium fork. Its
profiles can be driven by every ``browser_*`` tool as ``profile="shardx:<name>"``. Any text that
comes back from the launcher is passed through :func:`redact_secrets` (it may echo proxy URLs).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import Field

from ..automation.manager import SHARDX_PREFIX
from ..errors import ProfilePilotError
from .app import DEFAULT_MAX_CHARS, AppState, MaxCharsArg, OffsetArg, add_tool, get_state, paginate_text

log = logging.getLogger("profilepilot.server")

ShardXProfileArg = Annotated[str, Field(description="ShardX profile name, id or id prefix (with or without 'shardx:').")]


def _client(state: AppState) -> Any:
    if state.shardx is None:
        raise ProfilePilotError("The ShardX integration is not enabled (config shardx.enabled).")
    return state.shardx


def _redact(text: str) -> str:
    from ..integrations.shardx import redact_secrets

    return redact_secrets(text)


def _bare(ref: str) -> str:
    ref = (ref or "").strip()
    return ref[len(SHARDX_PREFIX):].strip() if ref.lower().startswith(SHARDX_PREFIX) else ref


async def shardx_status(ctx: Context) -> str:
    """Is the ShardX launcher reachable and authorised? (ShardX profiles spoof their fingerprint.)"""
    client = _client(get_state(ctx))
    status = await client.status()
    lines = [f"ShardX launcher at {status.get('base_url')}: "
             + ("reachable" if status.get("reachable") else "NOT reachable")
             + (f", version {status['version']}" if status.get("version") else "")
             + (", authorised" if status.get("authenticated") else ", not authorised")]
    if "running" in status:
        lines.append(f"Running ShardX profiles: {status['running']}.")
    if status.get("error"):
        lines.append(f"Error: {_redact(str(status['error']))}")
    if status.get("note"):
        lines.append(str(status["note"]))
    return "\n".join(lines)


async def shardx_profiles(ctx: Context, max_chars: MaxCharsArg = DEFAULT_MAX_CHARS, offset: OffsetArg = 0) -> str:
    """List ShardX profiles. Use them with any browser tool as profile='shardx:<name>'."""
    client = _client(get_state(ctx))
    profiles = await client.list_profiles()
    if not profiles:
        return "ShardX has no profiles."
    lines = [f"{len(profiles)} ShardX profile(s):"]
    for p in profiles:
        parts = [f"- shardx:{p.get('name')} (id {p.get('id')})", "running" if p.get("running") else "stopped"]
        if p.get("notes"):
            parts.append(f"notes: {str(p['notes'])[:120]}")
        lines.append(_redact(" | ".join(parts)))
    return paginate_text("\n".join(lines), offset, max_chars)


async def shardx_start(ctx: Context, profile: ShardXProfileArg) -> str:
    """Launch a ShardX profile with remote debugging and attach to it."""
    state = get_state(ctx)
    _client(state)
    session = await state.browsers.session(SHARDX_PREFIX + _bare(profile))
    return f"{session.label} is running and attached ({len(session.context.pages)} tab(s))."


async def shardx_stop(ctx: Context, profile: ShardXProfileArg) -> str:
    """Stop a ShardX profile's browser."""
    state = get_state(ctx)
    client = _client(state)
    resolved = await client.resolve(_bare(profile))
    await state.browsers.disconnect(SHARDX_PREFIX + _bare(profile))
    stopped = await client.stop(str(resolved.get("id")))
    name = resolved.get("name") or resolved.get("id")
    return f"Stopped ShardX profile '{name}'." if stopped else f"ShardX profile '{name}' was not running."


def register(server: MCPServer) -> None:
    add_tool(server, shardx_status, title="ShardX status", read_only=True, destructive=False, idempotent=True,
             open_world=False, invoking="Checking ShardX…", invoked="ShardX checked")
    add_tool(server, shardx_profiles, title="ShardX profiles", read_only=True, destructive=False, idempotent=True,
             open_world=False, invoking="Listing ShardX profiles…", invoked="ShardX profiles listed")
    add_tool(server, shardx_start, title="Start ShardX profile", read_only=False, destructive=False,
             idempotent=True, open_world=False, invoking="Starting the ShardX profile…",
             invoked="ShardX profile running")
    add_tool(server, shardx_stop, title="Stop ShardX profile", read_only=False, destructive=False,
             idempotent=True, open_world=False, invoking="Stopping the ShardX profile…",
             invoked="ShardX profile stopped")


__all__ = ["register"]
