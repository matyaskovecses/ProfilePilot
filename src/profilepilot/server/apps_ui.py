"""MCP Apps panel for ChatGPT and Claude: ``profiles_dashboard`` + ``ui://profilepilot/dashboard.html``.

Hosts that support the MCP Apps extension (``io.modelcontextprotocol/ui``; ChatGPT and Claude do)
render the tool's result as a small interactive panel in the conversation: every profile with its
status and proxy, Start / Stop, **Take control** / **Hand back to AI**, and the AI's open help
requests with Done / Dismiss. The panel is one self-contained HTML document (no external URLs, no
inline event handlers, no ``eval``) that talks to the host over the MCP Apps JSON-RPC
``postMessage`` bridge (``ui/initialize`` ... ``tools/call``); inside ChatGPT it falls back to
``window.openai`` when the standard bridge is unavailable.

Clients without Apps support get the same overview as text, so the tool is useful everywhere.

The panel's buttons call ``dashboard_action``, a tool visible to the app only
(``_meta.ui.visibility = ["app"]``): taking control and handing back are the *user's* decisions, so
the model must not be able to make them. Hosts that ignore visibility still cannot misuse it: every
call needs the per-process action token that travels in the result's ``_meta`` (which hosts give to
the panel, not to the model).

Wire-in: ``MCPServer(..., extensions=[apps_ui.build_apps()])`` in ``create_server`` (see
docs/design/WIRE-IN.md, "ChatGPT").
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from functools import partial
from hmac import compare_digest
from typing import Annotated, Any, Callable, Literal

from mcp.server.apps import APP_MIME_TYPE, Apps, client_supports_apps
from mcp.server.extension import ToolBinding
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resources import TextResource
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from ..errors import NotFoundError, ProfilePilotError
from .app import NoneOK, annotations, get_state, invocation_meta, run_sync, tool_guard

log = logging.getLogger("profilepilot.server.apps_ui")

DASHBOARD_URI = "ui://profilepilot/dashboard.html"
META_KEY = "profilepilot/dashboard"
"""Result ``_meta`` key carrying the panel's action token (given to the panel, not the model)."""
MAX_PANEL_PROFILES = 60
ACTIONS = ("start", "stop", "take_control", "hand_back", "help_done", "help_dismiss")

PANEL_NOTE = "Taken over in the chat panel"

DashboardAction = Literal["start", "stop", "take_control", "hand_back", "help_done", "help_dismiss"]

WIDGET_DESCRIPTION = (
    "An interactive panel listing the user's ProfilePilot browser profiles with their status, proxy and "
    "open help requests. The user can start or stop profiles, take control of one (the AI must then not "
    "act on it) and hand it back. Do not repeat the list in your reply; summarise what matters."
)


# ---------------------------------------------------------------------- data


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value) if value else None


def _default_control_factory(store: Any) -> Any:
    from ..control import ControlStore

    return ControlStore(store)


def _kind_labels() -> dict[str, str]:
    try:
        from ..control import KIND_LABELS

        return dict(KIND_LABELS)
    except Exception:  # pragma: no cover - control layer missing
        return {}


def collect_dashboard(state: Any, control: Any | None, *, limit: int = MAX_PANEL_PROFILES) -> dict[str, Any]:
    """The panel's data (blocking: run it in a worker thread). Never contains secrets."""
    store = state.store
    profiles = store.list_profiles()
    running = {info.profile_id: info for info in state.runtime.list_running()}
    proxies = {p.id: p for p in store.list_proxies()}
    identities: dict[str, str] = {}
    if any(p.identity_id for p in profiles):
        try:
            from ..identity import IdentityStore

            identities = {i.id: i.name for i in IdentityStore(store).list()}
        except Exception as exc:  # optional decoration only
            log.debug("identity names unavailable: %s", exc)
    labels = _kind_labels()

    help_items: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    paused_count = 0
    for profile in profiles:
        info = running.get(profile.id)
        if info is not None:
            run_state = "running"
        elif store.is_running_on_disk(profile.id):
            run_state = "starting"
        else:
            run_state = "stopped"
        paused = False
        pause_note = pause_since = None
        open_requests: list[Any] = []
        if control is not None:
            try:
                cstate = control.state_by_id(profile.id)
                user_pause = getattr(cstate, "pause", None)
                paused = user_pause is not None
                if paused:
                    pause_note = getattr(user_pause, "note", "") or ""
                    pause_since = _iso(getattr(user_pause, "since", None))
                open_requests = list(getattr(cstate, "open", []) or [])
            except ProfilePilotError as exc:
                log.debug("control state of %s unavailable: %s", profile.id, exc)
        paused_count += int(paused)
        for req in open_requests:
            help_items.append({
                "id": req.id, "profile_id": profile.id, "profile_name": profile.name, "kind": req.kind,
                "kind_label": labels.get(req.kind, req.kind), "message": req.message,
                "created_at": _iso(req.created_at), "pauses": bool(getattr(req, "pauses", True)),
            })
        proxy = None
        if profile.proxy_id:
            record = proxies.get(profile.proxy_id)
            if record is not None:
                check = record.last_check
                proxy = {
                    "name": record.name,
                    "scheme": record.scheme,
                    "country_code": (check.country_code or None) if check else None,
                    "country": (check.country or None) if check else None,
                    "ip": (check.ip or None) if check and check.ok else None,
                    "ok": check.ok if check else None,
                    "checked_at": _iso(check.checked_at) if check else None,
                }
            else:
                proxy = {"name": "missing proxy", "ok": False}
        rows.append({
            "id": profile.id,
            "name": profile.name,
            "tags": list(profile.tags),
            "state": run_state,
            "running": run_state == "running",
            "window": info.window if info is not None else profile.launch.window,
            "paused": paused,
            "paused_note": pause_note,
            "paused_since": pause_since,
            "help_open": len(open_requests),
            "proxy": proxy,
            "identity": identities.get(profile.identity_id) if profile.identity_id else None,
        })

    order = {"running": 0, "starting": 1, "stopped": 2}
    rows.sort(key=lambda r: (-r["help_open"], not r["paused"], order.get(r["state"], 3), r["name"].lower()))
    help_items.sort(key=lambda r: r.get("created_at") or "")
    total = len(rows)
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "profiles": total,
            "running": sum(1 for r in rows if r["state"] == "running"),
            "paused": paused_count,
            "help": len(help_items),
        },
        "features": {"control": control is not None},
        "help": help_items,
        "profiles": rows[:limit],
        "truncated": total > limit,
    }


def dashboard_text(data: dict[str, Any], *, panel: bool) -> str:
    """Model-facing overview (the text fallback for clients without MCP Apps)."""
    counts = data.get("counts", {})
    head = [f"{counts.get('profiles', 0)} profile(s)", f"{counts.get('running', 0)} running"]
    if counts.get("paused"):
        head.append(f"{counts['paused']} controlled by the user")
    if counts.get("help"):
        head.append(f"{counts['help']} open help request(s)")
    lines = ["ProfilePilot: " + ", ".join(head) + "."]
    if panel:
        lines.append("The user sees these in the ProfilePilot panel above (start/stop, take control, help requests).")
    for req in data.get("help", []):
        lines.append(f"Waiting for the user in '{req['profile_name']}' ({req.get('kind_label') or req['kind']}): "
                     f"'{req['message']}'.")
    if not data.get("profiles"):
        lines.append("No profiles yet. Create one with profile_create.")
    for row in data.get("profiles", []):
        parts = [row["state"]]
        if row["state"] == "running" and row.get("window") and row["window"] != "normal":
            parts[0] += f" ({row['window']} window)"
        if row.get("paused"):
            note = f": '{row['paused_note']}'" if row.get("paused_note") else ""
            parts.append(f"the user has taken control{note} - don't act on it")
        proxy = row.get("proxy")
        if proxy:
            where = " ".join(x for x in (proxy.get("country_code"), proxy.get("ip")) if x)
            parts.append(f"proxy {proxy['name']}" + (f" ({where})" if where else ""))
        else:
            parts.append("no proxy")
        if row.get("tags"):
            parts.append("tags " + ", ".join(row["tags"]))
        lines.append(f"- {row['name']} (id {row['id']}): " + "; ".join(parts))
    if data.get("truncated"):
        shown = len(data["profiles"])
        lines.append(f"(Showing {shown} of {counts.get('profiles')} profiles; use profile_list for all.)")
    return "\n".join(lines)


# ---------------------------------------------------------------------- the extension


class DashboardApps(Apps):
    """The MCP Apps extension with ProfilePilot's panel, its model-visible tool and the app-only
    action tool."""

    def __init__(self, *, control_factory: Callable[[Any], Any] | None = None) -> None:
        super().__init__()
        self.action_token = secrets.token_urlsafe(24)
        self._control_factory = control_factory or _default_control_factory
        self._extra_tools: list[ToolBinding] = []
        self.add_resource(TextResource(
            uri=DASHBOARD_URI,
            name="profilepilot-dashboard",
            title="ProfilePilot profiles",
            description="Interactive panel of the user's browser profiles (status, start/stop, take control).",
            mime_type=APP_MIME_TYPE,
            meta={"ui": {"prefersBorder": True}, "openai/widgetDescription": WIDGET_DESCRIPTION,
                  "openai/widgetPrefersBorder": True},
            text=DASHBOARD_HTML,
        ))
        self.tool(
            resource_uri=DASHBOARD_URI,
            visibility=["model", "app"],
            meta={**invocation_meta("Loading your profiles…", "Profiles ready"), "openai/widgetAccessible": True},
            name="profiles_dashboard",
            title="Profiles panel",
            description=_profiles_dashboard_doc(),
            annotations=annotations(read_only=True, destructive=False, idempotent=True, open_world=False,
                                    title="Profiles panel"),
            structured_output=False,
        )(tool_guard(self._make_dashboard_tool()))
        self._extra_tools.append(ToolBinding(
            fn=tool_guard(self._make_action_tool()),
            meta={"ui": {"visibility": ["app"]}, "openai/visibility": "private", "openai/widgetAccessible": True,
                  **invocation_meta("Updating the profile…", "Profile updated")},
            kwargs={
                "name": "dashboard_action",
                "title": "Panel action",
                "description": _dashboard_action_doc(),
                "annotations": annotations(read_only=False, destructive=False, idempotent=False, open_world=False,
                                           title="Panel action"),
                "structured_output": False,
            },
        ))

    def tools(self) -> list[ToolBinding]:  # type: ignore[override]
        return [*super().tools(), *self._extra_tools]

    # -- helpers

    def _control(self, store: Any) -> Any | None:
        try:
            return self._control_factory(store)
        except Exception as exc:  # the control layer is optional for the panel
            log.debug("control store unavailable: %s", exc)
            return None

    def _meta(self) -> dict[str, Any]:
        return {META_KEY: {"token": self.action_token}}

    async def _result(self, ctx: Context, message: str | None = None) -> CallToolResult:
        state = get_state(ctx)
        control = self._control(state.store)
        data = await run_sync(collect_dashboard, state, control)
        if message:
            data["message"] = message
        text = dashboard_text(data, panel=_supports_apps(ctx))
        if message:
            text = message + "\n" + text
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data,
                              _meta=self._meta())

    # -- tools

    def _make_dashboard_tool(self) -> Callable[..., Any]:
        apps = self

        async def profiles_dashboard(ctx: Context) -> CallToolResult:
            return await apps._result(ctx)

        profiles_dashboard.__doc__ = _profiles_dashboard_doc()
        return profiles_dashboard

    def _make_action_tool(self) -> Callable[..., Any]:
        apps = self

        async def dashboard_action(
            ctx: Context,
            action: Annotated[DashboardAction, Field(description="The button the user clicked.")],
            profile: Annotated[str, NoneOK, Field(description="Profile id.")] = None,
            request_id: Annotated[str, NoneOK, Field(description="Help request id (help_done / help_dismiss).")] = None,
            token: Annotated[str, NoneOK, Field(description="The panel's action token.")] = None,
        ) -> CallToolResult:
            if not token or not compare_digest(str(token).encode(), apps.action_token.encode()):
                raise ToolError(
                    "dashboard_action only works from the buttons of the ProfilePilot panel (if you are the panel: "
                    "it is out of date, refresh it). The model must use profile_start / profile_stop; taking "
                    "control and handing back are the user's decisions."
                )
            message = await apps._perform(ctx, action, profile, request_id)
            return await apps._result(ctx, message)

        dashboard_action.__doc__ = _dashboard_action_doc()
        return dashboard_action

    async def _perform(self, ctx: Context, action: str, profile: str | None, request_id: str | None) -> str:
        state = get_state(ctx)
        if not profile:
            raise ToolError("Choose a profile.")
        target = await run_sync(state.store.get_profile, profile)
        if action == "start":
            await run_sync(partial(state.runtime.start, target.id))
            return f"Started '{target.name}'."
        if action == "stop":
            await state.browsers.disconnect(target.id)
            stopped = await run_sync(state.runtime.stop, target.id)
            return f"Stopped '{target.name}'." if stopped else f"'{target.name}' was not running."
        control = self._control(state.store)
        if control is None:
            raise ToolError("Taking control needs ProfilePilot's control layer, which is not available here.")
        if action == "take_control":
            await run_sync(partial(control.pause, target.id, PANEL_NOTE))
            return f"You have control of '{target.name}': the AI will not act on it until you hand it back."
        if action == "hand_back":
            await run_sync(partial(control.resume, target.id))
            return f"Handed '{target.name}' back to the AI."
        if action in ("help_done", "help_dismiss"):
            if not request_id:
                raise ToolError("Which help request? (request_id is missing)")
            status = "done" if action == "help_done" else "dismissed"
            try:
                await run_sync(partial(control.resolve_help, target.id, request_id, status=status))
            except NotFoundError:
                raise ToolError("That help request no longer exists; refresh the panel.") from None
            return "Marked the request as done." if status == "done" else "Dismissed the request."
        raise ToolError(f"Unknown action {action!r}; use one of: {', '.join(ACTIONS)}.")


def _supports_apps(ctx: Context) -> bool:
    try:
        return client_supports_apps(ctx)
    except Exception:
        return False


def _profiles_dashboard_doc() -> str:
    return (
        "Show the user's ProfilePilot browser profiles as an interactive panel (in ChatGPT and Claude): status, "
        "proxy, Start/Stop, Take control / Hand back, and your open help requests. Other clients get the same "
        "overview as text. Use it when the user wants to see or manage their profiles; for one profile's details "
        "use profile_status."
    )


def _dashboard_action_doc() -> str:
    return (
        "Only for the buttons of the ProfilePilot panel (the user's own clicks): start/stop a profile, take control "
        "/ hand back, resolve a help request. Needs the panel's token; the model cannot use it."
    )


def build_apps(*, control_factory: Callable[[Any], Any] | None = None) -> DashboardApps:
    """The extension to pass to ``MCPServer(extensions=[...])``."""
    return DashboardApps(control_factory=control_factory)


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>ProfilePilot</title>
<style>
:root {
  --pp-bg: var(--color-background-primary, #ffffff);
  --pp-bg-2: var(--color-background-secondary, #f6f7f9);
  --pp-bg-3: var(--color-background-tertiary, #eef0f3);
  --pp-text: var(--color-text-primary, #15171c);
  --pp-text-2: var(--color-text-secondary, #5b6270);
  --pp-text-3: var(--color-text-tertiary, #8a909c);
  --pp-border: var(--color-border-primary, #e2e5ea);
  --pp-border-2: var(--color-border-secondary, #eceef2);
  --pp-accent: #3b5bfd;
  --pp-accent-text: #ffffff;
  --pp-accent-soft: #3b5bfd14;
  --pp-ok: var(--color-text-success, #198a4a);
  --pp-ok-soft: #198a4a1a;
  --pp-warn: var(--color-text-warning, #a35a00);
  --pp-warn-bg: var(--color-background-warning, #fff5e6);
  --pp-warn-border: var(--color-border-warning, #f3cf8f);
  --pp-danger: var(--color-text-danger, #b42323);
  --pp-danger-soft: #b423231a;
  --pp-info: var(--color-text-info, #2c4fe0);
  --pp-ring: var(--color-ring-primary, #3b5bfd66);
  --pp-radius: var(--border-radius-lg, 12px);
  --pp-radius-sm: var(--border-radius-md, 8px);
  --pp-font: var(--font-sans, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif);
  --pp-mono: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace);
  --pp-shadow: var(--shadow-sm, 0 1px 2px #0000000f);
  color-scheme: light;
}
:root[data-theme="dark"] {
  --pp-bg: var(--color-background-primary, #17191e);
  --pp-bg-2: var(--color-background-secondary, #1e2128);
  --pp-bg-3: var(--color-background-tertiary, #262a32);
  --pp-text: var(--color-text-primary, #eceef2);
  --pp-text-2: var(--color-text-secondary, #a3a9b6);
  --pp-text-3: var(--color-text-tertiary, #7b8290);
  --pp-border: var(--color-border-primary, #2c3039);
  --pp-border-2: var(--color-border-secondary, #252831);
  --pp-accent: #7088ff;
  --pp-accent-text: #0c0e13;
  --pp-accent-soft: #7088ff1f;
  --pp-ok: var(--color-text-success, #4cc483);
  --pp-ok-soft: #4cc4831f;
  --pp-warn: var(--color-text-warning, #f0b35a);
  --pp-warn-bg: var(--color-background-warning, #2a2112);
  --pp-warn-border: var(--color-border-warning, #6b5121);
  --pp-danger: var(--color-text-danger, #ff8a8a);
  --pp-danger-soft: #ff8a8a1f;
  --pp-info: var(--color-text-info, #93a6ff);
  --pp-ring: var(--color-ring-primary, #7088ff77);
  color-scheme: dark;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) {
    --pp-bg: #17191e; --pp-bg-2: #1e2128; --pp-bg-3: #262a32; --pp-text: #eceef2; --pp-text-2: #a3a9b6;
    --pp-text-3: #7b8290; --pp-border: #2c3039; --pp-border-2: #252831; --pp-accent: #7088ff; --pp-accent-text: #0c0e13;
    --pp-accent-soft: #7088ff1f; --pp-ok: #4cc483; --pp-ok-soft: #4cc4831f; --pp-warn: #f0b35a; --pp-warn-bg: #2a2112;
    --pp-warn-border: #6b5121; --pp-danger: #ff8a8a; --pp-danger-soft: #ff8a8a1f; --pp-info: #93a6ff; --pp-ring: #7088ff77;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: transparent; }
body {
  color: var(--pp-text); font-family: var(--pp-font); font-size: var(--font-text-sm-size, 13.5px);
  line-height: var(--font-text-sm-line-height, 1.45); -webkit-font-smoothing: antialiased;
}
button { font: inherit; color: inherit; }
.app { background: var(--pp-bg); border-radius: var(--pp-radius); padding: 12px; }
.top { display: flex; align-items: center; gap: 10px; min-height: 32px; }
.brand { display: flex; align-items: center; gap: 9px; min-width: 0; flex: 1; }
.mark {
  width: 22px; height: 22px; border-radius: 7px; flex: none; position: relative;
  background: linear-gradient(135deg, #3b5bfd, #9b6bff);
}
.mark::after { content: ""; position: absolute; inset: 6px; border-radius: 3px; border: 2px solid #fff; }
.title { font-weight: 650; font-size: 14.5px; letter-spacing: -0.01em; white-space: nowrap; }
.summary { color: var(--pp-text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.icon-btn {
  width: 30px; height: 30px; display: inline-grid; place-items: center; border-radius: 8px; flex: none;
  border: 1px solid transparent; background: transparent; color: var(--pp-text-2); cursor: pointer; font-size: 15px;
}
.icon-btn:hover { background: var(--pp-bg-2); color: var(--pp-text); }
.icon-btn[aria-busy="true"] .glyph { animation: spin 0.9s linear infinite; }
.glyph { display: inline-block; line-height: 1; }
:focus-visible { outline: 2px solid var(--pp-ring); outline-offset: 2px; }

.help { display: grid; gap: 8px; margin-top: 10px; }
.help-card {
  display: flex; gap: 12px; align-items: flex-start; padding: 10px 12px; border-radius: var(--pp-radius-sm);
  background: var(--pp-warn-bg); border: 1px solid var(--pp-warn-border);
}
.help-icon {
  width: 26px; height: 26px; border-radius: 50%; flex: none; display: grid; place-items: center; margin-top: 1px;
  background: var(--pp-warn); color: var(--pp-bg); font-weight: 700; font-size: 14px;
}
.help-body { flex: 1; min-width: 0; }
.help-title { font-weight: 600; }
.help-msg { margin: 2px 0 0; overflow-wrap: anywhere; }
.help-when { color: var(--pp-text-2); font-size: 12px; margin-top: 2px; }
.kind {
  display: inline-block; margin-left: 6px; padding: 0 7px; border-radius: 99px; font-size: 11.5px; font-weight: 600;
  color: var(--pp-warn); border: 1px solid var(--pp-warn-border); vertical-align: 1px;
}

.filter { margin-top: 10px; }
.filter input {
  width: 100%; height: 32px; padding: 0 10px; border-radius: var(--pp-radius-sm); border: 1px solid var(--pp-border);
  background: var(--pp-bg-2); color: var(--pp-text); font: inherit;
}
.filter input::placeholder { color: var(--pp-text-3); }

.list { list-style: none; margin: 10px 0 0; padding: 0; border: 1px solid var(--pp-border); border-radius: var(--pp-radius-sm); overflow: auto; }
.inline .list { max-height: 440px; }
.row { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-top: 1px solid var(--pp-border-2); min-height: 56px; }
.row:first-child { border-top: 0; }
.row:hover { background: var(--pp-bg-2); }
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; background: var(--pp-text-3); box-shadow: 0 0 0 3px var(--pp-bg-3); }
.row[data-state="running"] .dot { background: var(--pp-ok); box-shadow: 0 0 0 3px var(--pp-ok-soft); }
.row[data-state="starting"] .dot { background: var(--pp-warn); animation: pulse 1.2s ease-in-out infinite; }
.main { flex: 1; min-width: 0; }
.name-line { display: flex; align-items: center; gap: 6px; min-width: 0; flex-wrap: wrap; }
.name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.badge { font-size: 11.5px; font-weight: 600; padding: 1px 7px; border-radius: 99px; white-space: nowrap; }
.badge.you { color: var(--pp-info); background: var(--pp-accent-soft); }
.badge.need { color: var(--pp-warn); background: var(--pp-warn-bg); border: 1px solid var(--pp-warn-border); }
.meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; color: var(--pp-text-2); font-size: 12.5px; margin-top: 2px; }
.meta .sep { color: var(--pp-text-3); }
.cc {
  font: 600 10.5px/1 var(--pp-mono); padding: 2px 4px; border-radius: 4px; border: 1px solid var(--pp-border);
  color: var(--pp-text-2); background: var(--pp-bg-2); letter-spacing: 0.02em;
}
.bad { color: var(--pp-danger); }
.tag { font-size: 11.5px; padding: 0 6px; border-radius: 5px; background: var(--pp-bg-3); color: var(--pp-text-2); }
.actions { display: flex; gap: 6px; flex: none; }
.btn {
  height: 30px; padding: 0 11px; border-radius: 8px; border: 1px solid var(--pp-border); background: var(--pp-bg);
  font-weight: 600; font-size: 12.5px; cursor: pointer; white-space: nowrap; display: inline-flex; align-items: center; gap: 6px;
}
.btn:hover { background: var(--pp-bg-2); }
.btn.primary { background: var(--pp-accent); border-color: var(--pp-accent); color: var(--pp-accent-text); }
.btn.primary:hover { filter: brightness(1.06); }
.btn.quiet { border-color: transparent; background: transparent; color: var(--pp-text-2); }
.btn.quiet:hover { background: var(--pp-bg-3); color: var(--pp-text); }
.btn[disabled] { opacity: 0.55; cursor: default; filter: none; }
.btn .spin { width: 12px; height: 12px; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin 0.8s linear infinite; }

.empty { text-align: center; padding: 26px 16px; color: var(--pp-text-2); border: 1px dashed var(--pp-border); border-radius: var(--pp-radius-sm); margin-top: 10px; }
.empty strong { display: block; color: var(--pp-text); font-size: 14px; margin-bottom: 4px; }
.skeleton .row .bar { height: 10px; border-radius: 5px; background: var(--pp-bg-3); }
.bar.w1 { width: 38%; } .bar.w2 { width: 52%; }
.foot { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; color: var(--pp-text-3); font-size: 12px; margin-top: 9px; }
.toast {
  position: sticky; bottom: 0; margin-top: 8px; padding: 8px 11px; border-radius: var(--pp-radius-sm); font-size: 12.5px;
  background: var(--pp-text); color: var(--pp-bg); box-shadow: var(--pp-shadow);
}
.toast.error { background: var(--pp-danger); color: #fff; }
.toast[hidden] { display: none; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes pulse { 50% { opacity: 0.35; } }
@media (max-width: 460px) {
  .row { flex-wrap: wrap; }
  .actions { width: 100%; padding-left: 21px; }
  .summary { display: none; }
}
@media (prefers-reduced-motion: reduce) { * { animation: none !important; } }
</style>
</head>
<body>
<div class="app inline" id="app">
  <header class="top">
    <div class="brand">
      <span class="mark" aria-hidden="true"></span>
      <span class="title">ProfilePilot</span>
      <span class="summary" id="summary">Loading profiles…</span>
    </div>
    <button type="button" class="icon-btn" id="expand" title="Expand" aria-label="Expand" hidden><span class="glyph">&#x2922;</span></button>
    <button type="button" class="icon-btn" id="refresh" title="Refresh" aria-label="Refresh"><span class="glyph">&#x21bb;</span></button>
  </header>
  <section class="help" id="help" aria-label="Requests from the AI"></section>
  <div class="filter" id="filter-box" hidden>
    <label class="sr" for="filter">Filter profiles</label>
    <input id="filter" type="search" placeholder="Filter by name or tag" autocomplete="off" spellcheck="false">
  </div>
  <ul class="list skeleton" id="list" aria-label="Profiles">
    <li class="row"><span class="dot"></span><div class="main"><div class="bar w1"></div></div></li>
    <li class="row"><span class="dot"></span><div class="main"><div class="bar w2"></div></div></li>
  </ul>
  <div class="empty" id="empty" hidden></div>
  <footer class="foot">
    <span id="updated"></span>
    <span>Take control pauses the AI on that profile until you hand it back.</span>
  </footer>
  <div class="toast" id="toast" role="status" aria-live="polite" hidden></div>
</div>
<script>
(() => {
  "use strict";
  const PROTOCOL_VERSION = "2026-01-26";
  const META_KEY = "profilepilot/dashboard";
  const S = {
    data: null, token: "", busy: new Set(), filter: "", hostOrigin: "", hostCaps: {}, connected: false,
    displayMode: "inline", modes: [], openai: null, toastTimer: 0,
  };
  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------------ DOM helper (text only, never HTML)
  function h(tag, props, children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = String(value);
      else if (key === "click") el.addEventListener("click", value);
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of [].concat(children === undefined ? [] : children)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  // ------------------------------------------------------------------ MCP Apps bridge (JSON-RPC over postMessage)
  let nextId = 1;
  const pending = new Map();

  function post(message) {
    try { window.parent.postMessage(message, S.hostOrigin || "*"); } catch (err) { /* detached */ }
  }
  function request(method, params, timeoutMs) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error("The app did not answer in time."));
      }, timeoutMs || 90000);
      pending.set(id, { resolve, reject, timer });
      post({ jsonrpc: "2.0", id, method, params: params || {} });
    });
  }
  function notify(method, params) { post({ jsonrpc: "2.0", method, params: params || {} }); }
  function reply(id, result) { post({ jsonrpc: "2.0", id, result: result || {} }); }

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    if (S.hostOrigin && event.origin !== S.hostOrigin) return;
    const msg = event.data;
    if (!msg || typeof msg !== "object" || msg.jsonrpc !== "2.0") return;
    if (typeof msg.method !== "string") {
      const waiter = pending.get(msg.id);
      if (!waiter) return;
      if (!S.hostOrigin && event.origin && event.origin !== "null") S.hostOrigin = event.origin;
      pending.delete(msg.id);
      clearTimeout(waiter.timer);
      if (msg.error) waiter.reject(new Error(msg.error.message || "Request failed."));
      else waiter.resolve(msg.result);
      return;
    }
    const params = msg.params || {};
    switch (msg.method) {
      case "ui/notifications/tool-result": applyResult(params); break;
      case "ui/notifications/tool-input":
      case "ui/notifications/tool-input-partial": break;
      case "ui/notifications/tool-cancelled": toast("The request was cancelled."); break;
      case "ui/notifications/host-context-changed": applyContext(params); break;
      case "ui/resource-teardown": reply(msg.id, {}); break;
      case "ping": reply(msg.id, {}); break;
      default:
        if (msg.id !== undefined) post({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: "Method not found" } });
    }
  });

  async function callTool(name, args) {
    if (S.connected) return request("tools/call", { name, arguments: args || {} });
    if (S.openai && typeof S.openai.callTool === "function") {
      const res = await S.openai.callTool(name, args || {});
      if (res && typeof res.result === "string" && !res.structuredContent) {
        try { return { structuredContent: JSON.parse(res.result) }; } catch (err) { return { content: [{ type: "text", text: res.result }] }; }
      }
      return res || {};
    }
    throw new Error("Open this panel from ChatGPT or Claude to use the buttons.");
  }

  // ------------------------------------------------------------------ host context (theme, fonts, display mode)
  function applyContext(ctx) {
    if (!ctx || typeof ctx !== "object") return;
    if (ctx.theme === "dark" || ctx.theme === "light") document.documentElement.setAttribute("data-theme", ctx.theme);
    const vars = ctx.styles && ctx.styles.variables;
    if (vars && typeof vars === "object") {
      for (const [name, value] of Object.entries(vars)) {
        if (typeof value === "string" && /^--[\w-]+$/.test(name)) document.documentElement.style.setProperty(name, value);
      }
    }
    if (Array.isArray(ctx.availableDisplayModes)) S.modes = ctx.availableDisplayModes;
    if (typeof ctx.displayMode === "string") S.displayMode = ctx.displayMode;
    $("app").classList.toggle("inline", S.displayMode === "inline");
    const expand = $("expand");
    expand.hidden = !S.modes.includes("fullscreen");
    const full = S.displayMode === "fullscreen";
    expand.title = full ? "Collapse" : "Expand";
    expand.setAttribute("aria-label", expand.title);
    expand.firstElementChild.textContent = full ? "⤡" : "⤢";
  }

  // ------------------------------------------------------------------ data
  function textOf(result) {
    const items = (result && result.content) || [];
    return items.filter((c) => c && c.type === "text").map((c) => c.text).join("\n").trim();
  }

  function applyResult(result) {
    if (!result || typeof result !== "object") return;
    const meta = result._meta || result.meta || {};
    if (meta[META_KEY] && meta[META_KEY].token) S.token = String(meta[META_KEY].token);
    if (result.isError) { toast(textOf(result) || "Something went wrong.", true); return; }
    const data = result.structuredContent || result.structured_content;
    if (data && Array.isArray(data.profiles)) {
      S.data = data;
      render();
      if (data.message) toast(data.message);
      shareWithModel(result);
    }
  }

  function shareWithModel(result) {
    if (!S.connected || !S.hostCaps.updateModelContext) return;
    const text = textOf(result);
    if (text) request("ui/update-model-context", { content: [{ type: "text", text }] }, 15000).catch(() => {});
  }

  async function refresh() {
    if (S.busy.has("refresh")) return;
    S.busy.add("refresh");
    $("refresh").setAttribute("aria-busy", "true");
    try { applyResult(await callTool("profiles_dashboard", {})); }
    catch (err) { toast(err.message || String(err), true); }
    finally { S.busy.delete("refresh"); $("refresh").removeAttribute("aria-busy"); }
  }

  async function act(action, profile, extra) {
    const key = [action, profile && profile.id, extra && extra.request_id].join(":");
    if (S.busy.has(key)) return;
    S.busy.add(key);
    render();
    try {
      const args = { action, profile: profile ? profile.id : "", token: S.token };
      if (extra && extra.request_id) args.request_id = extra.request_id;
      const result = await callTool("dashboard_action", args);
      if (result && result.isError) {
        const text = textOf(result) || "That did not work.";
        toast(text, true);
        if (/out of date/i.test(text)) refresh();
      } else {
        applyResult(result);
        if (extra && extra.tellAi) tellAi(extra.tellAi);
      }
    } catch (err) {
      toast(err.message || String(err), true);
    } finally {
      S.busy.delete(key);
      render();
    }
  }

  function tellAi(text) {
    if (!S.connected || !S.hostCaps.message) return;
    request("ui/message", { role: "user", content: [{ type: "text", text }] }, 15000).catch(() => {});
  }

  // ------------------------------------------------------------------ rendering
  function clock(iso) {
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function ago(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return d.toLocaleDateString();
  }
  function busyBtn(label, key, props) {
    const busy = S.busy.has(key);
    return h("button", Object.assign({ type: "button", disabled: busy, "aria-busy": busy ? "true" : null }, props),
      busy ? [h("span", { class: "spin", "aria-hidden": "true" }), label] : label);
  }

  function renderHelp(data) {
    const box = $("help");
    box.replaceChildren();
    const byId = new Map(data.profiles.map((p) => [p.id, p]));
    for (const req of data.help || []) {
      const profile = byId.get(req.profile_id) || { id: req.profile_id, name: req.profile_name || req.profile_id };
      const doneKey = ["help_done", profile.id, req.id].join(":");
      const dismissKey = ["help_dismiss", profile.id, req.id].join(":");
      const canTell = S.connected && S.hostCaps.message;
      box.append(h("div", { class: "help-card", role: "group", "aria-label": "Help request" }, [
        h("span", { class: "help-icon", "aria-hidden": "true", text: "!" }),
        h("div", { class: "help-body" }, [
          h("div", { class: "help-title" }, ["The AI needs you in ", h("strong", { text: profile.name }),
            h("span", { class: "kind", text: req.kind_label || req.kind || "help" })]),
          h("p", { class: "help-msg", text: "“" + req.message + "”" }),
          h("div", { class: "help-when", text: "Asked " + ago(req.created_at) +
            (req.pauses ? " · the AI waits until you finish" : "") }),
        ]),
        h("div", { class: "actions" }, [
          busyBtn(canTell ? "Done, continue" : "Done", doneKey, { class: "btn primary",
            title: "Mark it done" + (canTell ? " and tell the AI to continue" : ""),
            click: () => act("help_done", profile, { request_id: req.id,
              tellAi: "I took care of “" + req.message + "” in the ProfilePilot profile “" + profile.name + "”. Please continue." }) }),
          busyBtn("Dismiss", dismissKey, { class: "btn quiet", click: () => act("help_dismiss", profile, { request_id: req.id }) }),
        ]),
      ]));
    }
  }

  function profileRow(p) {
    const state = p.state || (p.running ? "running" : "stopped");
    const stateLabel = { running: "Running", starting: "Starting…", stopped: "Stopped" }[state] || state;
    const meta = [h("span", { text: stateLabel })];
    if (p.running && p.window && p.window !== "normal") meta.push(h("span", { class: "sep", text: "·" }), h("span", { text: p.window + " window" }));
    meta.push(h("span", { class: "sep", text: "·" }));
    if (p.proxy) {
      const parts = [];
      if (p.proxy.country_code) parts.push(h("span", { class: "cc", title: p.proxy.country || p.proxy.country_code, text: p.proxy.country_code }));
      parts.push(" " + p.proxy.name);
      if (p.proxy.ip) parts.push(h("span", { class: "sep", text: " · " }), p.proxy.ip);
      if (p.proxy.ok === false) parts.push(h("span", { class: "bad", text: " (last check failed)" }));
      meta.push(h("span", { title: "Proxy" }, parts));
    } else {
      meta.push(h("span", { text: "Direct connection" }));
    }
    if (p.identity) meta.push(h("span", { class: "sep", text: "·" }), h("span", { title: "Identity for forms", text: p.identity }));
    for (const tag of (p.tags || []).slice(0, 4)) meta.push(h("span", { class: "tag", text: tag }));

    const startKey = ["start", p.id].join(":");
    const stopKey = ["stop", p.id].join(":");
    const ctlKey = [p.paused ? "hand_back" : "take_control", p.id].join(":");
    const actions = [];
    if (state === "stopped") actions.push(busyBtn("Start", startKey, { class: "btn", click: () => act("start", p) }));
    else actions.push(busyBtn("Stop", stopKey, { class: "btn quiet", click: () => act("stop", p) }));
    if (S.data && S.data.features && S.data.features.control) {
      actions.push(p.paused
        ? busyBtn("Hand back to AI", ctlKey, { class: "btn primary", title: "Let the AI use this profile again", click: () => act("hand_back", p) })
        : busyBtn("Take control", ctlKey, { class: "btn", title: "Pause the AI on this profile while you use it", click: () => act("take_control", p) }));
    }
    return h("li", { class: "row", "data-state": state, "aria-label": p.name + ", " + stateLabel }, [
      h("span", { class: "dot", "aria-hidden": "true" }),
      h("div", { class: "main" }, [
        h("div", { class: "name-line" }, [
          h("span", { class: "name", title: p.name, text: p.name }),
          p.paused ? h("span", { class: "badge you", text: "You’re in control" }) : null,
          p.help_open ? h("span", { class: "badge need", text: "Needs you" }) : null,
        ]),
        h("div", { class: "meta" }, meta),
      ]),
      h("div", { class: "actions" }, actions),
    ]);
  }

  function render() {
    const data = S.data;
    const list = $("list");
    const empty = $("empty");
    if (!data) return;
    list.classList.remove("skeleton");
    const c = data.counts || {};
    const bits = [c.profiles + (c.profiles === 1 ? " profile" : " profiles")];
    if (c.running) bits.push(c.running + " running");
    if (c.paused) bits.push(c.paused + " with you");
    if (c.help) bits.push(c.help + (c.help === 1 ? " request" : " requests"));
    $("summary").textContent = bits.join(" · ");
    renderHelp(data);
    $("filter-box").hidden = data.profiles.length < 7;
    const q = S.filter.trim().toLowerCase();
    const shown = data.profiles.filter((p) => !q || p.name.toLowerCase().includes(q) ||
      (p.tags || []).some((t) => t.toLowerCase().includes(q)));
    list.replaceChildren(...shown.map(profileRow));
    list.hidden = shown.length === 0;
    empty.hidden = shown.length !== 0;
    if (!shown.length) {
      empty.replaceChildren(
        h("strong", { text: data.profiles.length ? "No profile matches “" + S.filter + "”" : "No profiles yet" }),
        data.profiles.length ? "Try another name or tag." :
          "Ask the AI to create one (for example “create a profile called shopping”), or use ProfilePilot Manager on your computer.");
    }
    const more = data.truncated ? " · showing " + data.profiles.length + " of " + c.profiles : "";
    $("updated").textContent = (data.generated_at ? "Updated " + clock(data.generated_at) : "") + more;
  }

  function toast(text, isError) {
    const el = $("toast");
    el.textContent = text;
    el.classList.toggle("error", !!isError);
    el.hidden = false;
    clearTimeout(S.toastTimer);
    S.toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 9000 : 4000);
  }

  function showEmpty(title, text) {
    $("list").hidden = true;
    const empty = $("empty");
    empty.replaceChildren(h("strong", { text: title }), text);
    empty.hidden = false;
    $("summary").textContent = "";
  }

  // ------------------------------------------------------------------ sizing
  function observeSize() {
    let last = "";
    const send = () => {
      const height = Math.ceil(document.documentElement.getBoundingClientRect().height);
      const width = Math.ceil(document.documentElement.scrollWidth);
      const key = width + "x" + height;
      if (key === last || !S.connected) return;
      last = key;
      notify("ui/notifications/size-changed", { width, height });
    };
    if (typeof ResizeObserver === "function") new ResizeObserver(send).observe(document.body);
    send();
  }

  // ------------------------------------------------------------------ start
  $("refresh").addEventListener("click", refresh);
  $("expand").addEventListener("click", async () => {
    const mode = S.displayMode === "fullscreen" ? "inline" : "fullscreen";
    try {
      const res = await request("ui/request-display-mode", { mode }, 15000);
      applyContext({ displayMode: (res && res.mode) || mode });
    } catch (err) { toast(err.message || String(err), true); }
  });
  $("filter").addEventListener("input", (event) => { S.filter = event.target.value; render(); });

  async function init() {
    const prefill = window.openai;
    if (prefill && prefill.toolOutput) applyResult({ structuredContent: prefill.toolOutput, _meta: prefill.toolResponseMetadata });
    try {
      const result = await request("ui/initialize", {
        appInfo: { name: "ProfilePilot", version: "1.0.0" },
        appCapabilities: { availableDisplayModes: ["inline", "fullscreen"] },
        protocolVersion: PROTOCOL_VERSION,
      }, 6000);
      S.connected = true;
      S.hostCaps = (result && result.hostCapabilities) || {};
      applyContext(result && result.hostContext);
      notify("ui/notifications/initialized", {});
    } catch (err) {
      if (window.openai) {
        S.openai = window.openai;
        const sync = () => applyResult({ structuredContent: S.openai.toolOutput, _meta: S.openai.toolResponseMetadata });
        window.addEventListener("openai:set_globals", sync);
        if (S.openai.theme) applyContext({ theme: S.openai.theme });
        sync();
      } else if (!S.data) {
        showEmpty("ProfilePilot panel", "This panel works inside ChatGPT or Claude. Ask the AI to show your profiles.");
      }
    }
    observeSize();
  }
  init();
})();
</script>
</body>
</html>
"""

__all__ = ["DASHBOARD_HTML", "DASHBOARD_URI", "DashboardApps", "META_KEY", "build_apps", "collect_dashboard",
           "dashboard_text"]
