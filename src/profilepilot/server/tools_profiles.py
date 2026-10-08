"""``profile_*`` and ``proxy_*`` MCP tools.

Model-facing output never contains secrets: proxies are shown through
:meth:`ProxyRecord.summary` / :meth:`ProxyRecord.redacted_url` (password masked, never loaded),
running profiles through :meth:`RuntimeInfo.public` fields (no control token), and proxy parse
errors are replaced by a generic format hint because the parser may quote its input.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import Field

from ..automation.manager import SHARDX_PREFIX, is_shardx_ref
from ..errors import ConflictError, NotFoundError, ProfilePilotError, RestartRequiredError
from ..models import Profile, ProxyCheck, ProxyRecord, RuntimeInfo
from ..proxy.url import ProxyParseError, parse_proxy
from ..safety import normalize_url
from ..store import Store
from .app import PROXY_FORMAT_HELP, ProfileArg, add_tool, first_line, get_state, is_blank, run_sync

log = logging.getLogger("profilepilot.server")

WindowArg = Annotated[
    Literal["normal", "offscreen", "headless"] | None,
    Field(description="normal = visible window (most native), offscreen = real window placed off-screen, "
                      "headless = detectable (HeadlessChrome UA, webdriver=true); only when the user asks."),
]
SchemeArg = Annotated[
    Literal["http", "https", "socks4", "socks5"],
    Field(description="Scheme assumed for proxies written without one (host:port, host:port:user:pass)."),
]
TagsArg = Annotated[list[str] | None, Field(description="Tags, e.g. ['shop', 'us'].")]
BrowserArg = Annotated[
    Literal["auto", "chrome", "edge", "brave", "chromium"] | None,
    Field(description="Which installed browser to use (default auto: Chrome, then Edge, Brave, Chromium)."),
]
# Model-facing tools accept only browser *kinds*: a custom executable path or extra Chrome switches
# (e.g. --renderer-cmd-prefix) would let a model run arbitrary programs. Both stay available in
# the CLI (``profilepilot profile update --browser PATH --extra-arg ...``), typed by the user.


# ---------------------------------------------------------------------- formatting helpers


def proxy_label(record: ProxyRecord | None) -> str:
    if record is None:
        return "none (direct connection)"
    return f"{record.name} ({record.redacted_url()})"


def profile_line(profile: Profile, proxies: dict[str, ProxyRecord], info: RuntimeInfo | None) -> str:
    parts = [f"- {profile.name} (id {profile.id})"]
    parts.append(f"running, {info.window} window" if info else "stopped")
    if profile.proxy_id:
        record = proxies.get(profile.proxy_id)
        parts.append("proxy " + (proxy_label(record) if record else f"{profile.proxy_id} (missing)"))
    else:
        parts.append("no proxy")
    if profile.tags:
        parts.append("tags " + ", ".join(profile.tags))
    if profile.launch.lang:
        parts.append(f"lang {profile.launch.lang}")
    if profile.launch.timezone:
        parts.append(f"timezone {profile.launch.timezone}")
    if profile.browser != "auto":
        parts.append(f"browser {profile.browser}")
    if profile.notes:
        parts.append(f"notes: {first_line(profile.notes, 120)}")
    return " | ".join(parts)


def runtime_text(profile_name: str, info: RuntimeInfo) -> str:
    """Model-safe description of a running profile (built from public fields only)."""
    pub = info.public()
    version = f"{pub.get('browser_version')}" if pub.get("browser_version") else "browser"
    lines = [
        f"Profile '{profile_name}' is running ({version}, window {info.window}, chrome pid {info.chrome_pid}, "
        f"started {info.started_at.isoformat()})."
    ]
    if info.relay_port:
        upstream = info.upstream or "direct"
        lines.append(
            f"Proxy: {upstream} through the local relay {info.http_proxy_url} "
            "(credential-free; same exit IP as the browser)."
        )
    else:
        lines.append("Proxy: none (the browser connects directly).")
    if info.cdp_http_url:
        lines.append(f"DevTools endpoint: {info.cdp_http_url}")
    return "\n".join(lines)


def check_text(label: str, result: ProxyCheck) -> str:
    if not result.ok:
        return f"FAILED: {label}: {result.error or 'no provider answered'}"
    where = ", ".join(x for x in (result.country and f"{result.country}"
                                  + (f" ({result.country_code})" if result.country_code else ""),
                                  result.region, result.city) if x)
    details = [f"exit IP {result.ip}"]
    if where:
        details.append(where)
    if result.isp:
        details.append(f"ISP {result.isp}")
    if result.timezone:
        details.append(f"timezone {result.timezone}")
    timing = f"{result.latency_ms} ms" if result.latency_ms is not None else "?"
    via = f" via {result.provider}" if result.provider else ""
    return f"OK: {label} -> " + "; ".join(details) + f" ({timing}{via})"


# ---------------------------------------------------------------------- proxy resolution


def _looks_like_proxy_spec(value: str) -> bool:
    return "://" in value or "@" in value or ":" in value


def resolve_or_save_proxy(store: Store, proxy: str, *, name_hint: str | None, scheme: str) -> ProxyRecord:
    """A saved proxy (id, name or id prefix), or a proxy URL that is saved first.

    A new proxy is named after ``name_hint`` (the profile) when that name is free. Parse errors
    never echo the input (it contains the password).
    """
    ref = proxy.strip()
    if not ref:
        raise ProfilePilotError("Empty proxy.")
    for record in store.list_proxies():  # exact id / name first (names may contain ':')
        if record.id == ref.lower() or record.name.casefold() == ref.casefold():
            return record
    if not _looks_like_proxy_spec(ref):
        return store.get_proxy(ref)
    try:
        endpoint = parse_proxy(ref, scheme)
    except ProxyParseError:
        raise ProfilePilotError(PROXY_FORMAT_HELP) from None
    if name_hint:
        try:
            return store.add_proxy(endpoint, name_hint, default_scheme=scheme)
        except ConflictError:
            pass
    return store.add_proxy(endpoint, None, default_scheme=scheme)


def _clean_optional(value: str | None) -> str | None:
    """``""`` clears an optional setting (None); otherwise the stripped value."""
    if value is None:
        return None
    value = value.strip()
    return value or None


# ---------------------------------------------------------------------- profile tools


async def profile_list(
    ctx: Context,
    tag: Annotated[str | None, Field(description="Only profiles carrying this tag.")] = None,
) -> str:
    """List ProfilePilot profiles (isolated Chrome identities) with their proxy and running state.
    Reuse a fitting profile before creating a new one."""
    state = get_state(ctx)
    profiles = await run_sync(state.store.list_profiles, tag)
    if not profiles:
        if tag:
            return f"No profiles tagged '{tag}'."
        return "No profiles yet. Create one with profile_create(name, proxy?)."
    proxies = {p.id: p for p in await run_sync(state.store.list_proxies)}
    running = {i.profile_id: i for i in await run_sync(state.runtime.list_running)}
    lines = [f"{len(profiles)} profile(s), {sum(1 for p in profiles if p.id in running)} running:"]
    lines += [profile_line(p, proxies, running.get(p.id)) for p in profiles]
    return "\n".join(lines)


async def profile_create(
    ctx: Context,
    name: Annotated[str, Field(description="Unique profile name, e.g. 'shop-us'.")],
    proxy: Annotated[str | None, Field(
        description="Saved proxy name/id, or a proxy URL (scheme://user:pass@host:port, host:port:user:pass, ...) "
                    "which is saved under the profile's name.")] = None,
    tags: TagsArg = None,
    notes: Annotated[str | None, Field(description="Free-form notes.")] = None,
    window: WindowArg = None,
    browser: BrowserArg = None,
    lang: Annotated[str | None, Field(description="UI / Accept-Language override, e.g. 'de-DE' (opt-in).")] = None,
    timezone: Annotated[str | None, Field(description="IANA timezone override, e.g. 'Europe/Berlin' (opt-in).")] = None,
    start_url: Annotated[str | None, Field(description="Page opened when the profile starts.")] = None,
    proxy_scheme: SchemeArg = "http",
) -> str:
    """Create a profile: a new isolated Chrome identity with its own cookies, storage and optional proxy."""
    state = get_state(ctx)
    url = None
    if not is_blank(start_url):
        url = normalize_url(start_url or "")
        await state.policy.acheck(url)

    def create() -> tuple[Profile, ProxyRecord | None]:
        store = state.store
        clean = (name or "").strip()
        if clean and any(p.name.casefold() == clean.casefold() for p in store.list_profiles()):
            raise ConflictError(f"A profile named '{clean}' already exists. Pick another name or use it.")
        record = resolve_or_save_proxy(store, proxy, name_hint=clean, scheme=proxy_scheme) if not is_blank(proxy) else None
        launch: dict[str, Any] = {"window": window or store.load_config().default_window}
        if _clean_optional(lang):
            launch["lang"] = _clean_optional(lang)
        if _clean_optional(timezone):
            launch["timezone"] = _clean_optional(timezone)
        if url:
            launch["start_url"] = url
        created = store.create_profile(
            clean, notes=notes or "", tags=tags or (), proxy_id=record.id if record else None,
            browser=browser or "auto", launch=launch,
        )
        return created, record

    profile, record = await run_sync(create)
    lines = [
        f"Created profile '{profile.name}' (id {profile.id}).",
        f"Proxy: {proxy_label(record)}.",
        f"Window: {profile.launch.window}. Browser tools start it automatically "
        "(profile_start only to pick a different window mode).",
    ]
    if profile.launch.window == "headless":
        lines.append("Note: headless mode is detectable by websites (HeadlessChrome user agent).")
    return "\n".join(lines)


async def profile_update(
    ctx: Context,
    profile: ProfileArg,
    name: Annotated[str | None, Field(description="New name.")] = None,
    notes: Annotated[str | None, Field(description="New notes ('' clears them).")] = None,
    tags: TagsArg = None,
    window: WindowArg = None,
    browser: BrowserArg = None,
    lang: Annotated[str | None, Field(description="Language override; '' removes it.")] = None,
    timezone: Annotated[str | None, Field(description="Timezone override; '' removes it.")] = None,
    start_url: Annotated[str | None, Field(description="Start page; '' removes it.")] = None,
    restore_session: Annotated[bool | None, Field(description="Reopen tabs and keep session cookies across restarts.")] = None,
    webrtc: Annotated[Literal["auto", "proxy_only", "default"] | None, Field(
        description="WebRTC policy: auto (proxy_only when proxied), proxy_only, default.")] = None,
) -> str:
    """Change a profile's name, notes, tags or launch settings. Use profile_set_proxy for its proxy.
    Launch settings apply at the next start."""
    state = get_state(ctx)
    if is_shardx_ref(profile):
        raise ProfilePilotError("ShardX profiles are managed in the ShardX launcher.")
    launch: dict[str, Any] = {}
    if window is not None:
        launch["window"] = window
    if lang is not None:
        launch["lang"] = _clean_optional(lang)
    if timezone is not None:
        launch["timezone"] = _clean_optional(timezone)
    if start_url is not None:
        url = _clean_optional(start_url)
        if url:
            url = normalize_url(url)
            await state.policy.acheck(url)
        launch["start_url"] = url
    if restore_session is not None:
        launch["restore_session"] = restore_session
    if webrtc is not None:
        launch["webrtc"] = webrtc

    def update() -> tuple[Profile, bool]:
        current = state.store.get_profile(profile)
        updated = state.store.update_profile(
            current.id, name=name, notes=notes, tags=tags,
            browser=browser,
            launch=launch or None,
        )
        return updated, state.runtime.status(updated.id) is not None

    updated, running = await run_sync(update)
    changed = [k for k, v in {"name": name, "notes": notes, "tags": tags, "browser": browser}.items() if v is not None]
    changed += sorted(launch)
    lines = [f"Updated profile '{updated.name}' (id {updated.id}): {', '.join(changed) or 'nothing changed'}."]
    if running and (launch or browser is not None):
        lines.append("The profile is running: launch changes apply after profile_stop + profile_start.")
    return "\n".join(lines)


async def profile_delete(ctx: Context, profile: ProfileArg) -> str:
    """Move a stopped profile (with its cookies, logins and history) to the trash. Destructive:
    confirm with the user first. The trash can be restored with the CLI."""
    state = get_state(ctx)
    if is_shardx_ref(profile):
        raise ProfilePilotError("ShardX profiles are managed in the ShardX launcher.")
    target = await run_sync(state.store.get_profile, profile)
    await state.browsers.disconnect(target.id)
    entry = await run_sync(state.store.delete_profile, target.id)
    return (
        f"Moved profile '{entry.name}' to the trash (trash id {entry.trash_id}). "
        f"The user can restore it with: profilepilot profile restore {entry.trash_id}"
    )


async def profile_clone(
    ctx: Context,
    profile: ProfileArg,
    new_name: Annotated[str, Field(description="Name of the copy.")],
    copy_data: Annotated[bool, Field(
        description="Also copy browser data (cookies, logins, history). The source must be stopped.")] = False,
) -> str:
    """Copy a profile's settings (proxy, tags, launch options) into a new profile; optionally its data too."""
    state = get_state(ctx)
    if is_shardx_ref(profile):
        raise ProfilePilotError("ShardX profiles cannot be cloned here.")
    source = await run_sync(state.store.get_profile, profile)
    clone = await run_sync(partial(state.store.clone_profile, source.id, new_name, copy_data=copy_data))
    what = "settings and browser data (same logins)" if copy_data else "settings only (fresh cookies)"
    return f"Created profile '{clone.name}' (id {clone.id}) as a copy of '{source.name}': {what}."


async def profile_start(ctx: Context, profile: ProfileArg, window: WindowArg = None) -> str:
    """Start a profile's Chrome (idempotent). Browser tools do this automatically; call it to pick a
    window mode for this run. The browser keeps running until profile_stop."""
    state = get_state(ctx)
    if is_shardx_ref(profile):
        session = await state.browsers.session(profile)
        return f"{session.label} is running and attached ({len(session.context.pages)} tab(s))."
    target = await run_sync(state.store.get_profile, profile)
    before = await run_sync(state.runtime.status, target.id)
    info = await run_sync(partial(state.runtime.start, target.id, window=window))
    text = runtime_text(target.name, info)
    if before is not None:
        text = "Already running. " + text
        if window and window != info.window:
            text += f"\nIt runs with window mode '{info.window}'; stop and start it to switch to '{window}'."
    return text


async def profile_stop(ctx: Context, profile: ProfileArg) -> str:
    """Close a profile's Chrome gracefully (tabs and session are kept for the next start)."""
    state = get_state(ctx)
    if is_shardx_ref(profile):
        if state.shardx is None:
            raise ProfilePilotError("The ShardX integration is not enabled.")
        resolved = await state.shardx.resolve(profile[len(SHARDX_PREFIX):].strip())
        await state.browsers.disconnect(profile)
        stopped = await state.shardx.stop(str(resolved.get("id")))
        name = resolved.get("name") or resolved.get("id")
        return f"Stopped ShardX profile '{name}'." if stopped else f"ShardX profile '{name}' was not running."
    target = await run_sync(state.store.get_profile, profile)
    await state.browsers.disconnect(target.id)
    stopped = await run_sync(state.runtime.stop, target.id)
    return f"Stopped profile '{target.name}'." if stopped else f"Profile '{target.name}' was not running."


async def profile_status(
    ctx: Context,
    profile: Annotated[str | None, Field(description="A profile (default: list every running profile).")] = None,
) -> str:
    """Show whether a profile is running (window, proxy, relay traffic), or list all running profiles."""
    state = get_state(ctx)
    if is_blank(profile):
        running = await run_sync(state.runtime.list_running)
        if not running:
            return "No profiles are running."
        return "\n\n".join(runtime_text(i.profile_name, i) for i in running)
    assert profile is not None
    if is_shardx_ref(profile):
        if state.shardx is None:
            raise ProfilePilotError("The ShardX integration is not enabled.")
        resolved = await state.shardx.resolve(profile[len(SHARDX_PREFIX):].strip())
        cdp = await state.shardx.cdp(str(resolved.get("id")))
        name = resolved.get("name") or resolved.get("id")
        return f"ShardX profile '{name}' is {'running (attachable)' if cdp else 'not running (or not attachable)'}."

    def collect() -> tuple[Profile, RuntimeInfo | None, dict | None, ProxyRecord | None]:
        target = state.store.get_profile(profile)
        info = state.runtime.status(target.id)
        stats = None
        if info is not None and info.relay_port:
            try:
                stats = state.runtime.relay_stats(target.id)
            except ProfilePilotError as exc:
                log.debug("relay stats unavailable: %s", exc)
        record = None
        if target.proxy_id:
            try:
                record = state.store.get_proxy(target.proxy_id)
            except NotFoundError:
                record = None
        return target, info, stats, record

    target, info, stats, record = await run_sync(collect)
    lines = [f"Profile '{target.name}' (id {target.id}); saved proxy: {proxy_label(record)}."]
    if info is None:
        lines.append("Not running. Browser tools start it automatically.")
        return "\n".join(lines)
    lines.append(runtime_text(target.name, info))
    if stats:
        from .app import _scrub

        traffic = (
            f"Relay: {stats.get('connections_total', 0)} connections ({stats.get('connections_active', 0)} active, "
            f"{stats.get('connections_failed', 0)} failed), {stats.get('bytes_up', 0)} bytes up, "
            f"{stats.get('bytes_down', 0)} bytes down."
        )
        if stats.get("last_error"):
            traffic += f" Last error: {_scrub(first_line(str(stats['last_error']), 200))}"
        lines.append(traffic)
    if (record.id if record else None) != info.proxy_id:
        lines.append("Note: the saved proxy differs from the one the running browser uses; restart to apply it.")
    return "\n".join(lines)


async def profile_set_proxy(
    ctx: Context,
    profile: ProfileArg,
    proxy: Annotated[str | None, Field(
        description="Saved proxy name/id or a proxy URL; null, '' or 'none' for a direct connection.")] = None,
    proxy_scheme: SchemeArg = "http",
) -> str:
    """Bind a proxy to a profile (or remove it). On a running profile the switch is live for new
    connections."""
    state = get_state(ctx)
    if is_shardx_ref(profile):
        raise ProfilePilotError("ShardX profile proxies are managed in the ShardX launcher.")
    direct = is_blank(proxy) or str(proxy).strip().lower() in ("none", "direct", "null")

    def apply() -> str:
        store, runtime = state.store, state.runtime
        target = store.get_profile(profile)
        record = None if direct else resolve_or_save_proxy(store, str(proxy), name_hint=target.name, scheme=proxy_scheme)
        updated = store.update_profile(target.id, proxy_id=record.id if record else None)
        saved = f"Profile '{updated.name}' now uses proxy: {proxy_label(record)}."
        if runtime.status(updated.id) is None:
            return saved + " It applies when the profile starts."
        try:
            runtime.set_upstream(updated.id, record.id if record else None)
        except RestartRequiredError:
            return (
                saved + " The profile is running without a proxy relay, so the change applies after "
                "profile_stop + profile_start."
            )
        return saved + " Switched live: new connections use it (already open connections finish on the old route)."

    return await run_sync(apply)


# ---------------------------------------------------------------------- proxy tools


async def proxy_list(
    ctx: Context,
    tag: Annotated[str | None, Field(description="Only proxies carrying this tag.")] = None,
) -> str:
    """List saved proxies (passwords are never shown) with their last test result."""
    state = get_state(ctx)
    proxies = await run_sync(state.store.list_proxies, tag)
    if not proxies:
        return "No saved proxies. Add some with proxy_add(url)."
    profiles = await run_sync(state.store.list_profiles)
    users: dict[str, list[str]] = {}
    for p in profiles:
        if p.proxy_id:
            users.setdefault(p.proxy_id, []).append(p.name)
    lines = [f"{len(proxies)} proxy(ies):"]
    for record in proxies:
        parts = [f"- {record.name} (id {record.id}) {record.redacted_url()}"]
        if record.tags:
            parts.append("tags " + ", ".join(record.tags))
        if users.get(record.id):
            parts.append("used by " + ", ".join(users[record.id]))
        check = record.summary().get("last_check")
        if check:
            parts.append(
                ("last test OK " + " ".join(str(check[k]) for k in ("ip", "country", "city") if check.get(k)))
                if check.get("ok") else f"last test FAILED ({first_line(str(check.get('error', '')), 120)})"
            )
        lines.append(" | ".join(parts))
    return "\n".join(lines)


async def proxy_add(
    ctx: Context,
    url: Annotated[str, Field(
        description="One proxy, or many separated by newlines. Formats: scheme://user:pass@host:port, "
                    "user:pass@host:port, host:port, host:port:user:pass. In a list, '  # name' after a proxy names it.")],
    name: Annotated[str | None, Field(description="Name for a single proxy (default host:port).")] = None,
    tags: TagsArg = None,
    scheme: SchemeArg = "http",
) -> str:
    """Save one or more upstream proxies (HTTP, HTTPS, SOCKS4, SOCKS5, with or without auth).
    Passwords go to the OS keyring and are never shown again."""
    state = get_state(ctx)
    lines = [ln.strip() for ln in (url or "").splitlines()]
    entries = [ln for ln in lines if ln and not ln.startswith("#")]
    if not entries:
        raise ProfilePilotError("No proxy given.")

    def add_all() -> tuple[list[ProxyRecord], list[str]]:
        store = state.store
        if len(entries) == 1:
            spec, label = entries[0], name
            if " #" in spec and not name:
                spec, _, label = spec.partition(" #")
                spec, label = spec.strip(), label.strip() or None
            return [store.add_proxy(spec, label, default_scheme=scheme, tags=tags or ())], []
        added: list[ProxyRecord] = []
        errors: list[str] = []
        for lineno, line in enumerate(lines, 1):
            if not line or line.startswith("#"):
                continue
            spec, label = line, None
            if " #" in line:
                spec, _, label = line.partition(" #")
                spec, label = spec.strip(), label.strip() or None
            try:
                added.append(store.add_proxy(spec, label, default_scheme=scheme, tags=tags or ()))
            except ProxyParseError:
                errors.append(f"line {lineno}: could not parse the proxy")
            except ProfilePilotError as exc:
                errors.append(f"line {lineno}: {exc}")
        return added, errors

    added, errors = await run_sync(add_all)
    out = [f"Saved {len(added)} proxy(ies):"] + [f"- {r.name} (id {r.id}) {r.redacted_url()}" for r in added]
    if errors:
        out.append(f"{len(errors)} line(s) failed:")
        out += [f"- {e}" for e in errors]
        out.append(PROXY_FORMAT_HELP)
    out.append("Test them with proxy_test(proxy=...) and attach them with profile_set_proxy or profile_create.")
    return "\n".join(out)


async def proxy_remove(
    ctx: Context,
    proxy: Annotated[str, Field(description="Saved proxy name, id or id prefix.")],
    force: Annotated[bool, Field(description="Also remove it from profiles that use it.")] = False,
) -> str:
    """Delete a saved proxy and its stored password. Destructive: confirm with the user first."""
    state = get_state(ctx)
    record = await run_sync(state.store.get_proxy, proxy)
    unbound = await run_sync(partial(state.store.remove_proxy, record.id, force=force))
    text = f"Removed proxy '{record.name}'."
    if unbound:
        text += (
            f" Unbound from: {', '.join(unbound)} (they connect directly from their next start; running ones keep "
            "their current route until restarted)."
        )
    return text


async def proxy_test(
    ctx: Context,
    proxy: Annotated[str | None, Field(description="Saved proxy name/id to test.")] = None,
    profile: Annotated[str | None, Field(
        description="Test a profile's route instead (its live relay when running, else its saved proxy).")] = None,
    timeout_s: Annotated[float, Field(description="Timeout in seconds.", ge=2, le=60)] = 12.0,
) -> str:
    """Check a proxy (or a profile's route) end to end: exit IP, country, city, ISP and latency.
    Without arguments it checks this computer's direct connection."""
    from ..proxy.check import check_proxy, check_via_relay

    state = get_state(ctx)
    store = state.store
    if not is_blank(proxy) and not is_blank(profile):
        raise ProfilePilotError("Give either 'proxy' or 'profile', not both.")

    async def test_saved(record: ProxyRecord, label: str) -> str:
        endpoint = await run_sync(store.proxy_endpoint, record.id)
        result = await check_proxy(endpoint, timeout=timeout_s)
        await run_sync(store.set_proxy_check, record.id, result)
        return check_text(label, result)

    if not is_blank(proxy):
        record = await run_sync(store.get_proxy, str(proxy))
        return await test_saved(record, f"proxy '{record.name}' ({record.redacted_url()})")
    if not is_blank(profile):
        if is_shardx_ref(str(profile)):
            raise ProfilePilotError("proxy_test cannot test ShardX profiles; check them in the ShardX launcher.")
        target = await run_sync(store.get_profile, str(profile))
        info = await run_sync(state.runtime.status, target.id)
        if info is not None and info.relay_port:
            result = await check_via_relay(info.http_proxy_url, timeout=timeout_s)
            return check_text(f"profile '{target.name}' (live relay, upstream {info.upstream or 'direct'})", result)
        note = ""
        if info is not None and target.proxy_id:
            note = "\nNote: the profile is running WITHOUT its proxy (started before it was set); restart it to apply."
        if target.proxy_id:
            record = await run_sync(store.get_proxy, target.proxy_id)
            return await test_saved(record, f"profile '{target.name}' saved proxy '{record.name}' "
                                            f"({record.redacted_url()})") + note
        result = await check_proxy(None, timeout=timeout_s)
        return check_text(f"profile '{target.name}' has no proxy: direct connection", result)
    result = await check_proxy(None, timeout=timeout_s)
    return check_text("direct connection of this computer (no proxy)", result)


# ---------------------------------------------------------------------- registration


def register(server: MCPServer) -> None:
    add_tool(server, profile_list, title="List profiles", read_only=True, destructive=False, idempotent=True,
             open_world=False, invoking="Listing profiles…", invoked="Profiles listed")
    add_tool(server, profile_create, title="Create profile", read_only=False, destructive=False, idempotent=False,
             open_world=False, invoking="Creating profile…", invoked="Profile created")
    add_tool(server, profile_update, title="Update profile", read_only=False, destructive=False, idempotent=True,
             open_world=False, invoking="Updating profile…", invoked="Profile updated")
    add_tool(server, profile_delete, title="Delete profile (to trash)", read_only=False, destructive=True,
             idempotent=False, open_world=False, invoking="Moving profile to the trash…",
             invoked="Profile moved to the trash")
    add_tool(server, profile_clone, title="Clone profile", read_only=False, destructive=False, idempotent=False,
             open_world=False, invoking="Cloning profile…", invoked="Profile cloned")
    add_tool(server, profile_start, title="Start profile", read_only=False, destructive=False, idempotent=True,
             open_world=False, invoking="Starting Chrome…", invoked="Chrome is running")
    add_tool(server, profile_stop, title="Stop profile", read_only=False, destructive=False, idempotent=True,
             open_world=False, invoking="Closing Chrome…", invoked="Chrome closed")
    add_tool(server, profile_status, title="Profile status", read_only=True, destructive=False, idempotent=True,
             open_world=False, invoking="Checking profiles…", invoked="Status ready")
    add_tool(server, profile_set_proxy, title="Set profile proxy", read_only=False, destructive=False,
             idempotent=True, open_world=False, invoking="Switching proxy…", invoked="Proxy set")
    add_tool(server, proxy_list, title="List proxies", read_only=True, destructive=False, idempotent=True,
             open_world=False, invoking="Listing proxies…", invoked="Proxies listed")
    add_tool(server, proxy_add, title="Add proxies", read_only=False, destructive=False, idempotent=True,
             open_world=False, invoking="Saving proxies…", invoked="Proxies saved")
    add_tool(server, proxy_remove, title="Remove proxy", read_only=False, destructive=True, idempotent=False,
             open_world=False, invoking="Removing proxy…", invoked="Proxy removed")
    add_tool(server, proxy_test, title="Test proxy", read_only=True, destructive=False, idempotent=True,
             open_world=True, invoking="Testing the route…", invoked="Route tested")


__all__ = ["register", "resolve_or_save_proxy", "runtime_text", "check_text", "proxy_label"]
