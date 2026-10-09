"""``profilepilot`` command line (also ``python -m profilepilot``).

Commands::

    serve [--http ...]                       MCP server (stdio by default; --http for remote clients)
    profile list|create|show|start|stop|delete|restore|clone|update
    identity list|show|create|set|secret|clear|allow|disallow|delete|fields
    proxy list|add|import|remove|test
    status | stop-all
    install <client> | install print | uninstall <client>
    doctor
    shardx status|login|logout|profiles

Output is a human-friendly table; ``--json`` prints machine-readable JSON instead. The CLI never
prints secrets: proxies are shown redacted, running profiles without their control token, and
proxy specs / tokens can be read from stdin (``-``) so they never appear in the process list.
Sensitive identity values (card, SSN, password) are only read from a hidden prompt or stdin
(``identity secret``), never from argv, and only shown masked.

Heavy modules (Playwright, the MCP SDK) are only imported by the commands that need them.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import platform
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from pydantic import ValidationError

from . import __version__
from .errors import ConflictError, ProfilePilotError, RestartRequiredError
from .models import Profile, ProxyCheck, ProxyRecord, RuntimeInfo
from .proxy.url import ProxyParseError
from .store import Store

log = logging.getLogger("profilepilot.cli")

PROXY_FORMAT_HELP = (
    "Could not parse that proxy. Use scheme://user:pass@host:port, user:pass@host:port, host:port or "
    "host:port:user:pass (schemes: http, https, socks4, socks5)."
)
WINDOW_MODES = ("normal", "offscreen", "headless")
SCHEMES = ("http", "https", "socks4", "socks5")


class CliError(Exception):
    """A user-facing CLI error (printed without a traceback, exit code 1)."""


# ---------------------------------------------------------------------- output helpers


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _json(data: Any) -> None:
    _out(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    """Plain fixed-width table."""
    cells = [[str(h) for h in headers]] + [["" if v is None else str(v) for v in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def emit(args: argparse.Namespace, data: Any, text: str | Callable[[], str]) -> None:
    if getattr(args, "json", False):
        _json(data)
    else:
        _out(text() if callable(text) else text)


def _read_secret_input(prompt: str) -> str:
    """Read a secret (proxy spec, token) from stdin without echoing it on a terminal."""
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.read() if sys.stdin is not None else ""


# ---------------------------------------------------------------------- store / runtime


def _store(args: argparse.Namespace) -> Store:
    return Store(args.home) if getattr(args, "home", None) else Store()


def _runtime(store: Store) -> Any:
    from .browser.runtime import RuntimeManager

    return RuntimeManager(store)


def _proxy_map(store: Store) -> dict[str, ProxyRecord]:
    return {p.id: p for p in store.list_proxies()}


def _proxy_text(record: ProxyRecord | None) -> str:
    return f"{record.name} ({record.redacted_url()})" if record else "-"


def _runtime_lines(name: str, info: RuntimeInfo) -> str:
    lines = [
        f"Profile '{name}' is running: {info.browser_version or 'browser'}, window {info.window}, "
        f"chrome pid {info.chrome_pid}, started {info.started_at.isoformat()}.",
        f"  proxy:    {info.upstream or 'direct'}" + (f" via relay {info.http_proxy_url}" if info.relay_port else ""),
        f"  devtools: {info.cdp_http_url or '-'}",
    ]
    return "\n".join(lines)


def _import_lines(store: Store, text: str, *, scheme: str, tags: Iterable[str] = ()) -> tuple[list[ProxyRecord], list[str]]:
    """Bulk proxy import; errors never echo the line (it may contain a password)."""
    added: list[ProxyRecord] = []
    errors: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        spec, label = line, None
        if " #" in line:
            spec, _, label = line.partition(" #")
            spec, label = spec.strip(), label.strip() or None
        try:
            added.append(store.add_proxy(spec, label, default_scheme=scheme, tags=tags))
        except ProxyParseError:
            errors.append(f"line {lineno}: could not parse the proxy")
        except ProfilePilotError as exc:
            errors.append(f"line {lineno}: {exc}")
    return added, errors


def _resolve_proxy(store: Store, proxy: str, *, name_hint: str | None, scheme: str) -> ProxyRecord:
    from .server.tools_profiles import resolve_or_save_proxy

    return resolve_or_save_proxy(store, proxy, name_hint=name_hint, scheme=scheme)


def _proxy_arg(args: argparse.Namespace) -> str | None:
    """--proxy value; '-' reads it from stdin (keeps the password out of the process list)."""
    value = getattr(args, "proxy", None)
    if value == "-":
        value = _read_secret_input("Proxy: ").strip()
        if not value:
            raise CliError("No proxy given on stdin.")
    return value


# ---------------------------------------------------------------------- serve / host


def cmd_serve(args: argparse.Namespace) -> int:
    if args.home:
        os.environ["PROFILEPILOT_HOME"] = str(Path(args.home).expanduser().resolve())
    if not args.http:
        if args.public_host or args.auth or args.token or args.allow_private_network:
            raise CliError("--public-host/--auth/--token/--allow-private-network need --http.")
        if args.allow_sensitive_autofill:
            raise CliError("--allow-sensitive-autofill needs --http (the local stdio server always offers "
                           "form_autofill_sensitive; each call still needs the user's approval).")
        from .server.app import serve_stdio

        serve_stdio(args.home, log_level=args.log_level, files_anywhere=args.files_anywhere)
        return 0
    if args.files_anywhere:
        raise CliError("--files-anywhere is only available for the stdio server (remote clients never get it).")
    from .server.http import serve_http

    token = args.token
    if token == "-":
        token = _read_secret_input("Bearer token: ").strip()
    serve_http(
        host=args.host, port=args.port, path=args.path, public_hosts=args.public_host or [],
        auth=args.auth or "secret-path", token=token, allow_private=args.allow_private_network,
        i_understand=args.i_understand, new_secret=args.new_secret, root=args.home, log_level=args.log_level,
        announce=lambda banner: _err(banner), allow_sensitive_autofill=args.allow_sensitive_autofill,
    )
    return 0


def _host_main(argv: list[str]) -> int:
    from .browser.host import main as host_main

    return host_main(argv)


# ---------------------------------------------------------------------- profile


def cmd_profile_list(args: argparse.Namespace) -> int:
    store = _store(args)
    profiles = store.list_profiles(args.tag)
    proxies = _proxy_map(store)
    running = {i.profile_id: i for i in _runtime(store).list_running()} if profiles else {}
    identities = _identity_names(store) if any(p.identity_id for p in profiles) else {}

    def text() -> str:
        if not profiles:
            return "No profiles. Create one with: profilepilot profile create <name>"
        rows = [
            (p.name, p.id, "running" if p.id in running else "stopped",
             running[p.id].window if p.id in running else p.launch.window,
             _proxy_text(proxies.get(p.proxy_id)) if p.proxy_id else "-", ", ".join(p.tags) or "-")
            + ((identities.get(p.identity_id, "(missing)") if p.identity_id else "-",) if identities else ())
            for p in profiles
        ]
        return table(rows, ["NAME", "ID", "STATE", "WINDOW", "PROXY", "TAGS"] + (["IDENTITY"] if identities else []))

    data = [{**p.summary(), "running": p.id in running,
             "proxy": proxies[p.proxy_id].summary() if p.proxy_id in proxies else None} for p in profiles]
    emit(args, data, text)
    return 0


def cmd_profile_create(args: argparse.Namespace) -> int:
    store = _store(args)
    name = args.name.strip()
    if any(p.name.casefold() == name.casefold() for p in store.list_profiles()):
        raise ConflictError(f"A profile named '{name}' already exists.")
    proxy = _proxy_arg(args)
    record = _resolve_proxy(store, proxy, name_hint=name, scheme=args.proxy_scheme) if proxy else None
    launch: dict[str, Any] = {"window": args.window or store.load_config().default_window}
    for key in ("lang", "timezone", "start_url"):
        value = getattr(args, key)
        if value:
            launch[key] = value
    if args.extra_arg:
        from .browser.flags import validate_extra_args

        launch["extra_args"] = validate_extra_args(args.extra_arg)
    profile = store.create_profile(
        name, notes=args.notes or "", tags=args.tag or (), proxy_id=record.id if record else None,
        browser=args.browser or "auto", launch=launch, identity_id=args.identity or None,
    )
    identity = _identity_names(store).get(profile.identity_id or "")
    emit(args, {**profile.summary(), "proxy": record.summary() if record else None},
         f"Created profile '{profile.name}' (id {profile.id}), proxy {_proxy_text(record)}, "
         f"window {profile.launch.window}" + (f", identity {identity}." if identity else "."))
    return 0


def cmd_profile_show(args: argparse.Namespace) -> int:
    store = _store(args)
    profile = store.get_profile(args.profile)
    info = _runtime(store).status(profile.id)
    record = store.get_proxy(profile.proxy_id) if profile.proxy_id else None
    identity = _identity_names(store).get(profile.identity_id, "(missing)") if profile.identity_id else None
    data = {"profile": profile.model_dump(mode="json"), "proxy": record.summary() if record else None,
            "identity": identity, "runtime": info.public() if info else None}

    def text() -> str:
        launch = profile.launch
        lines = [
            f"{profile.name} (id {profile.id})",
            f"  proxy:     {_proxy_text(record)}",
            f"  identity:  {identity or '-'}",
            f"  tags:      {', '.join(profile.tags) or '-'}",
            f"  browser:   {profile.browser}",
            f"  window:    {launch.window}; webrtc {launch.webrtc}; restore session {launch.restore_session}",
            f"  lang:      {launch.lang or '-'}; timezone {launch.timezone or '-'}",
            f"  start url: {launch.start_url or '-'}",
            f"  data:      {store.user_data_dir(profile.id)}",
            f"  created:   {profile.created_at.isoformat()}; last started "
            f"{profile.last_started_at.isoformat() if profile.last_started_at else '-'}; "
            f"total runtime {profile.total_runtime_s} s",
        ]
        if launch.extra_args:
            lines.append(f"  extra:     {' '.join(launch.extra_args)}")
        if profile.notes:
            lines.append(f"  notes:     {profile.notes}")
        lines.append(_runtime_lines(profile.name, info) if info else "  state:     stopped")
        return "\n".join(lines)

    emit(args, data, text)
    return 0


def cmd_profile_start(args: argparse.Namespace) -> int:
    store = _store(args)
    profile = store.get_profile(args.profile)
    info = _runtime(store).start(profile.id, timeout=args.timeout, window=args.window)
    emit(args, info.public(), _runtime_lines(profile.name, info))
    return 0


def cmd_profile_stop(args: argparse.Namespace) -> int:
    store = _store(args)
    profile = store.get_profile(args.profile)
    stopped = _runtime(store).stop(profile.id, timeout=args.timeout)
    emit(args, {"profile": profile.name, "stopped": stopped},
         f"Stopped '{profile.name}'." if stopped else f"'{profile.name}' was not running.")
    return 0


def cmd_profile_delete(args: argparse.Namespace) -> int:
    store = _store(args)
    entry = store.delete_profile(args.profile)
    emit(args, entry.model_dump(mode="json"),
         f"Moved '{entry.name}' to the trash ({entry.size_bytes // 1024} KB). "
         f"Restore it with: profilepilot profile restore {entry.trash_id}")
    return 0


def cmd_profile_restore(args: argparse.Namespace) -> int:
    store = _store(args)
    if not args.trash_id:
        entries = store.list_trash()
        emit(args, [e.model_dump(mode="json") for e in entries],
             lambda: table([(e.trash_id, e.name, e.deleted_at.isoformat(), f"{e.size_bytes // 1024} KB") for e in entries],
                           ["TRASH ID", "NAME", "DELETED", "SIZE"]) if entries else "The trash is empty.")
        return 0
    profile = store.restore_profile(args.trash_id)
    emit(args, profile.summary(), f"Restored profile '{profile.name}' (id {profile.id}).")
    return 0


def cmd_profile_clone(args: argparse.Namespace) -> int:
    store = _store(args)
    source = store.get_profile(args.profile)
    clone = store.clone_profile(source.id, args.new_name, copy_data=args.copy_data)
    emit(args, clone.summary(), f"Cloned '{source.name}' to '{clone.name}' (id {clone.id})"
                                + (" including browser data." if args.copy_data else " (settings only)."))
    return 0


def cmd_profile_update(args: argparse.Namespace) -> int:
    store = _store(args)
    profile = store.get_profile(args.profile)
    launch: dict[str, Any] = {}
    if args.window:
        launch["window"] = args.window
    for key in ("lang", "timezone", "start_url"):
        value = getattr(args, key)
        if value is not None:
            launch[key] = value.strip() or None
    if args.restore_session is not None:
        launch["restore_session"] = args.restore_session
    if args.webrtc:
        launch["webrtc"] = args.webrtc
    if args.extra_arg is not None:
        from .browser.flags import validate_extra_args

        launch["extra_args"] = validate_extra_args([a for a in args.extra_arg if a])
    tags = [] if args.clear_tags else (args.tag or None)
    changes: dict[str, Any] = {"name": args.name, "notes": args.notes, "tags": tags, "browser": args.browser,
                               "launch": launch or None}
    proxy = _proxy_arg(args)
    record: ProxyRecord | None = None
    proxy_changed = proxy is not None or args.no_proxy
    if proxy:
        record = _resolve_proxy(store, proxy, name_hint=profile.name, scheme=args.proxy_scheme)
    if proxy_changed:
        changes["proxy_id"] = record.id if record else None
    if args.identity is not None:
        changes["identity_id"] = args.identity.strip() or None  # '' unlinks
    updated = store.update_profile(profile.id, **changes)
    notes = []
    runtime = _runtime(store)
    if runtime.status(updated.id) is not None:
        if proxy_changed:
            try:
                runtime.set_upstream(updated.id, record.id if record else None)
                notes.append("proxy switched live")
            except RestartRequiredError:
                notes.append("restart the profile to apply the proxy")
        if launch or args.browser:
            notes.append("launch changes apply after a restart")
    emit(args, updated.summary(), f"Updated '{updated.name}'." + (f" ({'; '.join(notes)})" if notes else ""))
    return 0


# ---------------------------------------------------------------------- identity

CONFIRM_TWICE = ("card_number", "ssn", "password")
"""Sensitive fields that are typed twice at the hidden prompt (a typo would go unnoticed)."""


def _identities(store: Store) -> Any:
    from .identity import IdentityStore

    return IdentityStore(store)


def _identity_names(store: Store) -> dict[str, str]:
    return {i.id: i.name for i in _identities(store).list()}


_SAFE_NAME = re.compile(r"[A-Za-z0-9_.@+-]+")
_QUOTABLE_NAME = re.compile(r"[^\"`$%!^&|<>\\]+")


def shell_name(name: str, fallback: str = "<identity>") -> str:
    """How to write a name in a command the user pastes into a terminal: bare, double-quoted, or
    ``fallback`` (e.g. the id) when quoting would not be safe in every shell."""
    name = (name or "").strip()
    if _SAFE_NAME.fullmatch(name):
        return name
    if name and _QUOTABLE_NAME.fullmatch(name):
        return f'"{name}"'
    return fallback


def _field(name: str) -> str:
    """Canonical field key; an unknown name is only echoed when it looks like a name (not a value)."""
    from .identity import FIELDS, field_key

    try:
        return field_key(name)
    except ProfilePilotError:
        shown = f"'{name}'" if re.fullmatch(r"[A-Za-z][A-Za-z _-]{0,39}", name or "") else "given"
        raise CliError(f"Unknown identity field {shown}. Known fields: {', '.join(FIELDS)} "
                       "(see: profilepilot identity fields).") from None


def _assignments(items: Sequence[str] | None, identity: str) -> dict[str, str | None]:
    """``KEY=VALUE`` arguments -> {key: value or None}. Sensitive keys are refused (never stored from argv)."""
    from .identity import FIELDS

    out: dict[str, str | None] = {}
    for item in items or ():
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise CliError("Values are given as KEY=VALUE, e.g. email=jane@example.com (see: profilepilot identity "
                           "fields).")
        canonical = _field(key.strip())
        if FIELDS[canonical].sensitive:
            raise CliError(
                f"'{canonical}' is sensitive and is never taken from the command line; nothing was stored. Use: "
                f"profilepilot identity secret {identity} {canonical}  (it asks for the value without showing it). "
                "The value you typed may be in your shell history: consider removing it."
            )
        out[canonical] = value if value.strip() else None
    return out


def _identity_data(store: Store, ident: Any) -> dict[str, Any]:
    return {"id": ident.id, "name": ident.name, "fields": sorted(ident.values),
            "sensitive_set": sorted(ident.sensitive_set), "allowed_origins": ident.allowed_origins,
            "profiles": [p.name for p in store.profiles_using_identity(ident.id)]}


def cmd_identity_list(args: argparse.Namespace) -> int:
    store = _store(args)
    items = [_identity_data(store, i) for i in _identities(store).list()]
    emit(args, items, lambda: table(
        [(i["name"], i["id"], len(i["fields"]), ", ".join(i["sensitive_set"]) or "-",
          ", ".join(i["allowed_origins"]) or "-", ", ".join(i["profiles"]) or "-") for i in items],
        ["NAME", "ID", "FIELDS", "SENSITIVE", "SENSITIVE AUTOFILL ON", "PROFILES"],
    ) if items else "No identities. Create one with: profilepilot identity create <name> --set first_name=...")
    return 0


def cmd_identity_show(args: argparse.Namespace) -> int:
    from .identity import FIELDS

    store = _store(args)
    ids = _identities(store)
    ident = ids.get(args.identity)
    view = ids.masked(ident.id)
    view["profiles"] = [p.name for p in store.profiles_using_identity(ident.id)]

    def text() -> str:
        width = max(len(k) for k in FIELDS)
        lines = [f"{ident.name} (id {ident.id})"]
        if ident.notes:
            lines.append(f"  notes: {ident.notes}")
        fields = view["fields"]
        lines += [f"  {k.ljust(width)}  {fields[k]}" for k in FIELDS if k in fields] or ["  (no values)"]
        lines.append(f"  sensitive autofill on: {', '.join(ident.allowed_origins) or '- (none)'}")
        lines.append(f"  profiles: {', '.join(view['profiles']) or '-'}")
        return "\n".join(lines)

    emit(args, view, text)
    return 0


def cmd_identity_create(args: argparse.Namespace) -> int:
    store = _store(args)
    values = _assignments(args.assignments, shell_name(args.name, "<identity>"))
    ident = _identities(store).create(args.name, {k: v for k, v in values.items() if v is not None},
                                      notes=args.notes or "")
    emit(args, _identity_data(store, ident),
         f"Created identity '{ident.name}' (id {ident.id}) with {len(ident.values)} value(s). Add card, SSN or "
         f"password values with: profilepilot identity secret {shell_name(ident.name, ident.id)} <field>")
    return 0


def cmd_identity_set(args: argparse.Namespace) -> int:
    store = _store(args)
    ids = _identities(store)
    ident = ids.get(args.identity)
    values = _assignments(args.assignments, shell_name(ident.name, ident.id))
    if not values and args.name is None and args.notes is None:
        raise CliError("Nothing to change: give KEY=VALUE pairs, --name or --notes.")
    updated = ids.update(ident.id, values, name=args.name, notes=args.notes)
    removed = sorted(k for k, v in values.items() if v is None)
    emit(args, _identity_data(store, updated),
         f"Updated identity '{updated.name}'" + (f": set {', '.join(k for k in values if k not in removed)}"
                                                if len(values) > len(removed) else "")
         + (f"; removed {', '.join(removed)}" if removed else "") + ".")
    return 0


def cmd_identity_secret(args: argparse.Namespace) -> int:
    from .identity import FIELDS

    if args.value:
        raise CliError("Never put the value on the command line (it ends up in your shell history and the process "
                       "list); nothing was stored. Run the command without it and type the value at the prompt, or "
                       "pipe it in with --stdin.")
    store = _store(args)
    ids = _identities(store)
    key = _field(args.field)
    ident = ids.get(args.identity)
    if not FIELDS[key].sensitive:
        raise CliError(f"'{key}' is not a sensitive field; set it with: profilepilot identity set "
                       f"{shell_name(ident.name, ident.id)} {key}=VALUE")
    label = FIELDS[key].label
    if args.stdin:
        value = (sys.stdin.readline() if sys.stdin is not None else "").rstrip("\r\n")
    else:
        if sys.stdin is None or not sys.stdin.isatty():
            raise CliError("No terminal to ask in: pipe the value in with --stdin.")
        try:
            value = getpass.getpass(f"{label} for '{ident.name}' (input is hidden): ")
            if key in CONFIRM_TWICE and value.strip():
                if getpass.getpass(f"Repeat the {label.lower()}: ") != value:
                    raise CliError("The two entries differ; nothing was stored.")
        except EOFError:
            raise CliError("No value was given; nothing was stored.") from None
    if not value.strip():
        raise CliError(f"No value given; nothing was stored. To remove a stored value use: profilepilot identity "
                       f"clear {shell_name(ident.name, ident.id)} {key}")
    ident = ids.set_sensitive(ident.id, key, value)
    value = ""
    masked = ids.masked(ident.id)["fields"].get(key, "set")
    hint = "" if ident.allowed_origins else (
        f" Sensitive autofill works only on sites you allow: profilepilot identity allow "
        f"{shell_name(ident.name, ident.id)} https://shop.example.com")
    emit(args, {"identity": ident.name, "field": key, "stored": True, "masked": masked},
         f"Stored {key} for identity '{ident.name}' ({masked}) in the {store.secrets.backend} secret store.{hint}")
    return 0


def cmd_identity_clear(args: argparse.Namespace) -> int:
    from .identity import FIELDS

    store = _store(args)
    ids = _identities(store)
    key = _field(args.field)
    ident = ids.get(args.identity)
    if FIELDS[key].sensitive:
        ident = ids.set_sensitive(ident.id, key, None)
    else:
        ident = ids.update(ident.id, {key: None})
    emit(args, _identity_data(store, ident), f"Removed {key} from identity '{ident.name}'.")
    return 0


def cmd_identity_allow(args: argparse.Namespace) -> int:
    from urllib.parse import urlsplit

    from .identity import normalize_origin

    store = _store(args)
    ids = _identities(store)
    origin = normalize_origin(args.origin)
    ident = ids.allow_origin(args.identity, origin)
    note = ""
    if origin.startswith("http://") and urlsplit(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
        note = " Note: sensitive autofill also needs HTTPS, so it will not run on this http:// origin."
    emit(args, _identity_data(store, ident),
         f"Sensitive autofill (card, SSN, password) of '{ident.name}' is now allowed on {origin}.{note}")
    return 0


def cmd_identity_disallow(args: argparse.Namespace) -> int:
    from .identity import normalize_origin

    store = _store(args)
    origin = normalize_origin(args.origin)
    ident = _identities(store).disallow_origin(args.identity, origin)
    emit(args, _identity_data(store, ident), f"Sensitive autofill of '{ident.name}' is no longer allowed on {origin}.")
    return 0


def cmd_identity_delete(args: argparse.Namespace) -> int:
    store = _store(args)
    ids = _identities(store)
    ident = ids.get(args.identity)
    linked = store.profiles_using_identity(ident.id)
    if not args.yes:
        if sys.stdin is None or not sys.stdin.isatty():
            raise CliError("Add --yes to delete without the confirmation prompt.")
        extra = f" It is linked to {len(linked)} profile(s), which will be unlinked." if linked else ""
        try:
            answer = input(f"Delete identity '{ident.name}' and its stored card/SSN/password values?{extra} [y/N] ")
        except EOFError:  # e.g. stdin is the NUL device, which Windows reports as a terminal
            raise CliError("No answer was given; add --yes to delete without the confirmation prompt.") from None
        if answer.strip().lower() not in ("y", "yes"):
            _out("Cancelled; nothing was deleted.")
            return 1
    name = ids.delete(ident.id)
    for profile in linked:
        store.update_profile(profile.id, identity_id=None)
    emit(args, {"deleted": name, "unlinked_profiles": [p.name for p in linked]},
         f"Deleted identity '{name}' and its stored secrets."
         + (f" Unlinked from: {', '.join(p.name for p in linked)}." if linked else ""))
    return 0


def cmd_identity_fields(args: argparse.Namespace) -> int:
    from .identity import ALIASES, FIELDS

    aliases: dict[str, list[str]] = {}
    for alias, key in ALIASES.items():
        aliases.setdefault(key, []).append(alias)
    data = [{"key": s.key, "label": s.label, "sensitive": s.sensitive, "group": s.group,
             "aliases": sorted(aliases.get(s.key, [])), "help": s.help} for s in FIELDS.values()]
    emit(args, data, lambda: table(
        [(d["key"], d["label"], "yes (identity secret)" if d["sensitive"] else "-", ", ".join(d["aliases"]) or "-",
          d["help"] or "") for d in data],
        ["KEY", "LABEL", "SENSITIVE", "ALIASES", "NOTES"],
    ))
    return 0


# ---------------------------------------------------------------------- proxy


def cmd_proxy_list(args: argparse.Namespace) -> int:
    store = _store(args)
    proxies = store.list_proxies(args.tag)
    users: dict[str, list[str]] = {}
    for p in store.list_profiles():
        if p.proxy_id:
            users.setdefault(p.proxy_id, []).append(p.name)

    def last(record: ProxyRecord) -> str:
        c = record.last_check
        if c is None:
            return "-"
        return f"ok {c.ip} {c.country_code or c.country or ''}".strip() if c.ok else "failed"

    emit(args, [{**r.summary(), "used_by": users.get(r.id, [])} for r in proxies],
         lambda: table([(r.name, r.id, r.redacted_url(), ", ".join(r.tags) or "-", ", ".join(users.get(r.id, [])) or "-",
                         last(r)) for r in proxies], ["NAME", "ID", "URL", "TAGS", "USED BY", "LAST TEST"])
         if proxies else "No saved proxies. Add one with: profilepilot proxy add")
    return 0


def cmd_proxy_add(args: argparse.Namespace) -> int:
    store = _store(args)
    spec = args.spec
    if spec in (None, "-"):
        spec = _read_secret_input("Proxy (scheme://user:pass@host:port): ")
    lines = [ln for ln in (spec or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise CliError("No proxy given.")
    if len(lines) == 1:
        try:
            records = [store.add_proxy(lines[0].strip(), args.name, default_scheme=args.scheme, tags=args.tag or ())]
        except ProxyParseError:
            raise CliError(PROXY_FORMAT_HELP) from None
        errors: list[str] = []
    else:
        records, errors = _import_lines(store, "\n".join(lines), scheme=args.scheme, tags=args.tag or ())
    return _report_added(args, records, errors)


def _report_added(args: argparse.Namespace, records: list[ProxyRecord], errors: list[str]) -> int:
    def text() -> str:
        lines = [f"Saved {len(records)} proxy(ies)."]
        lines += [f"  {r.name} ({r.id}) {r.redacted_url()}" for r in records]
        if errors:
            lines.append(f"{len(errors)} line(s) failed:")
            lines += [f"  {e}" for e in errors]
        return "\n".join(lines)

    emit(args, {"added": [r.summary() for r in records], "errors": errors}, text)
    return 1 if errors and not records else 0


def cmd_proxy_import(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.file == "-":
        text = sys.stdin.read()
    else:
        try:
            raw = Path(args.file).expanduser().read_bytes()
        except OSError as exc:
            raise CliError(f"Cannot read {args.file}: {exc.strerror or type(exc).__name__}") from None
        text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig", "replace")
    records, errors = _import_lines(store, text, scheme=args.scheme, tags=args.tag or ())
    return _report_added(args, records, errors)


def cmd_proxy_remove(args: argparse.Namespace) -> int:
    store = _store(args)
    record = store.get_proxy(args.proxy)
    unbound = store.remove_proxy(record.id, force=args.force)
    emit(args, {"removed": record.summary(), "unbound": unbound},
         f"Removed proxy '{record.name}'." + (f" Unbound from: {', '.join(unbound)}." if unbound else ""))
    return 0


def _check_text(label: str, result: ProxyCheck) -> str:
    if not result.ok:
        return f"FAILED  {label}: {result.error}"
    where = ", ".join(x for x in (result.country, result.region, result.city) if x)
    return (f"OK      {label}: exit IP {result.ip} ({where or 'unknown location'}"
            + (f", {result.isp}" if result.isp else "") + f"), {result.latency_ms} ms via {result.provider}")


def cmd_proxy_test(args: argparse.Namespace) -> int:
    import anyio

    from .proxy.check import check_proxy, check_via_relay

    store = _store(args)
    if args.direct or not args.ref:
        result = anyio.run(lambda: check_proxy(None, timeout=args.timeout))
        emit(args, result.model_dump(mode="json"), _check_text("direct connection", result))
        return 0 if result.ok else 1
    record: ProxyRecord | None = None
    label = ""
    relay_url: str | None = None
    try:
        record = store.get_proxy(args.ref)
        label = f"proxy '{record.name}' ({record.redacted_url()})"
    except ProfilePilotError:
        profile = store.get_profile(args.ref)  # raises a "not found" error naming profiles
        info = _runtime(store).status(profile.id)
        if info is not None and info.relay_port:
            relay_url = info.http_proxy_url
            label = f"profile '{profile.name}' (live relay, upstream {info.upstream or 'direct'})"
        elif profile.proxy_id:
            record = store.get_proxy(profile.proxy_id)
            label = f"profile '{profile.name}' proxy '{record.name}' ({record.redacted_url()})"
        else:
            label = f"profile '{profile.name}' (no proxy: direct connection)"
    if relay_url:
        result = anyio.run(lambda: check_via_relay(relay_url, timeout=args.timeout))
    elif record is not None:
        endpoint = store.proxy_endpoint(record.id)
        result = anyio.run(lambda: check_proxy(endpoint, timeout=args.timeout))
        store.set_proxy_check(record.id, result)
    else:
        result = anyio.run(lambda: check_proxy(None, timeout=args.timeout))
    emit(args, result.model_dump(mode="json"), _check_text(label, result))
    return 0 if result.ok else 1


# ---------------------------------------------------------------------- status / stop-all


def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    running = _runtime(store).list_running()
    emit(args, [i.public() for i in running],
         lambda: table([(i.profile_name, i.profile_id, i.window, i.chrome_pid, i.upstream or "direct",
                         i.http_proxy_url or "-", i.cdp_http_url or "-") for i in running],
                       ["NAME", "ID", "WINDOW", "CHROME PID", "PROXY", "RELAY", "DEVTOOLS"])
         if running else "No profiles are running.")
    return 0


def cmd_stop_all(args: argparse.Namespace) -> int:
    store = _store(args)
    stopped = _runtime(store).stop_all(timeout=args.timeout)
    emit(args, {"stopped": stopped}, f"Stopped {len(stopped)} profile(s)" + (f": {', '.join(stopped)}." if stopped else "."))
    return 0


# ---------------------------------------------------------------------- install


def cmd_install(args: argparse.Namespace) -> int:
    from . import install

    if args.client == "print":
        snippets = install.snippets(args.python, http_port=args.port)
        if args.only:
            if args.only not in snippets:
                raise CliError(f"Unknown client {args.only!r}; choose one of: {', '.join(snippets)}.")
            snippets = {args.only: snippets[args.only]}
        emit(args, snippets, lambda: "\n\n".join(f"## {k}\n{v}" for k, v in snippets.items()))
        return 0
    report = install.register(args.client, python=args.python, dry_run=args.dry_run)
    emit(args, {"client": args.client, "dry_run": args.dry_run, "report": report}, report)
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    from . import install

    report = install.unregister(args.client, dry_run=args.dry_run)
    emit(args, {"client": args.client, "dry_run": args.dry_run, "report": report}, report)
    return 0


# ---------------------------------------------------------------------- doctor


def _version_of(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def cmd_browsers(args: argparse.Namespace) -> int:
    """List the installed browsers a profile can run in (their genuine identities)."""
    from .paths import BROWSER_LABELS, list_browsers

    browsers = list_browsers()
    data = [{"kind": b.kind, "name": BROWSER_LABELS.get(b.kind, b.kind), "version": b.version, "path": b.path}
            for b in browsers]

    def text() -> str:
        if not browsers:
            return "No supported browser found. Install Google Chrome, Microsoft Edge, Brave or Chromium."
        rows = [(d["kind"], d["name"], d["version"] or "?", d["path"]) for d in data]
        hint = "Use a kind with: profilepilot profile create NAME --browser KIND"
        return table(rows, ["KIND", "BROWSER", "VERSION", "PATH"]) + "\n" + hint

    emit(args, data, text)
    return 0 if browsers else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    from .paths import data_root, list_browsers

    checks: list[tuple[str, bool | None, str]] = []  # (name, ok (None = info), detail)
    checks.append(("python", sys.version_info >= (3, 10), f"{platform.python_version()} ({sys.executable})"))
    checks.append(("profilepilot", True, __version__))
    for dist in ("playwright", "patchright", "mcp", "scrapling", "curl_cffi"):
        version = _version_of(dist)
        required = dist in ("playwright", "mcp")
        checks.append((dist, bool(version) if required else None, version or "not installed"))
    browsers = list_browsers()
    checks.append(("browsers", bool(browsers),
                   "; ".join(f"{b.kind} {b.version or '?'} ({b.path})" for b in browsers) or "none found"))
    root = Path(args.home).expanduser() if args.home else data_root()
    try:
        store = _store(args)
        probe = store.root / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(("data root", True, str(store.root)))
    except OSError as exc:
        checks.append(("data root", False, f"{root}: not writable ({exc.strerror or type(exc).__name__})"))
        store = None
    if store is not None:
        backend = store.secrets.backend
        detail = backend
        if backend == "keyring":
            try:
                import keyring

                detail = f"keyring ({type(keyring.get_keyring()).__name__})"
            except Exception:  # pragma: no cover
                pass
        else:
            detail = "encrypted file (DPAPI)" if sys.platform == "win32" else "file (0600)"
        checks.append(("secrets", True, detail))
        try:
            running = _runtime(store).list_running()
            checks.append(("running profiles", None, ", ".join(i.profile_name for i in running) or "none"))
        except Exception as exc:
            checks.append(("running profiles", False, f"{type(exc).__name__}: {exc}"))
        config = store.load_config()
        try:
            from .automation.driver import select_driver

            checks.append(("cdp driver", None, f"{select_driver(config)} (config automation.driver="
                                               f"{config.automation.driver}; env PROFILEPILOT_DRIVER overrides)"))
        except ProfilePilotError as exc:
            checks.append(("cdp driver", False, str(exc)))
        checks.append(("profiles", None, f"{len(store.list_profiles())} profile(s), {len(store.list_proxies())} proxy(ies), "
                                         f"max_running {config.max_running}"))
        if config.shardx.enabled:
            from .integrations.shardx import ShardXClient

            client = ShardXClient.from_store(store, settings_path=_shardx_settings_path(store))
            try:
                status = client.status()
            finally:
                client.close()
            ok = bool(status.get("reachable") and status.get("authenticated"))
            checks.append(("shardx", ok, f"{status.get('base_url')}: "
                                         + ("reachable" if status.get("reachable") else "not reachable")
                                         + (", authorised" if status.get("authenticated") else "")
                                         + (f" ({status['error']})" if status.get("error") else "")))
        else:
            checks.append(("shardx", None, "disabled"))
    try:
        from . import install

        loc = install.Locations.detect()
        registered = {c: install.is_registered(c, loc) for c in install.CLIENTS}  # type: ignore[arg-type]
        checks.append(("clients", None, ", ".join(
            f"{c}: {'yes' if v else 'no' if v is False else '?'}" for c, v in registered.items())))
    except Exception as exc:  # pragma: no cover - never fail the doctor on this
        checks.append(("clients", None, f"unknown ({type(exc).__name__})"))

    failed = [c for c in checks if c[1] is False]
    data = [{"check": name, "ok": ok, "detail": detail} for name, ok, detail in checks]
    emit(args, {"ok": not failed, "checks": data},
         lambda: "\n".join(f"[{'ok ' if ok else 'ERR' if ok is False else ' - '}] {name:<17} {detail}"
                           for name, ok, detail in checks))
    return 1 if failed else 0


# ---------------------------------------------------------------------- shardx


def _shardx_settings_path(store: Store) -> str | None:
    from .jsonio import read_json

    data = read_json(store.root / "shardx.json", {}) or {}
    value = data.get("settings_path") if isinstance(data, dict) else None
    return str(value) if value else None


def cmd_shardx_status(args: argparse.Namespace) -> int:
    from .integrations.shardx import ShardXClient

    store = _store(args)
    config = store.load_config().shardx
    client = ShardXClient.from_store(store, settings_path=_shardx_settings_path(store))
    try:
        status = client.status()
    finally:
        client.close()
    status = {"enabled": config.enabled, "token_source": config.token_source, **status}
    emit(args, status, lambda: "\n".join([
        f"ShardX integration: {'enabled' if config.enabled else 'disabled'} (token source: {config.token_source})",
        f"Launcher API: {status.get('base_url')} - " + ("reachable" if status.get("reachable") else "not reachable")
        + (f", version {status['version']}" if status.get("version") else "")
        + (", authorised" if status.get("authenticated") else ", not authorised"),
    ] + ([f"Error: {status['error']}"] if status.get("error") else [])))
    return 0 if status.get("reachable") else 1


def cmd_shardx_login(args: argparse.Namespace) -> int:
    from .integrations.shardx import SettingsTokenMinter, base_url_from_settings, normalize_token, save_token
    from .jsonio import write_json

    store = _store(args)
    config = store.load_config()
    if args.from_settings is not None:
        path = Path(args.from_settings).expanduser() if args.from_settings else None
        minter = SettingsTokenMinter(path)
        if not minter():  # validates the file has an api_secret; the token is not shown
            raise CliError("The ShardX settings file has no api_secret.")
        config.shardx.token_source = "settings"
        config.shardx.base_url = base_url_from_settings(path)
        if path is not None:
            write_json(store.root / "shardx.json", {"settings_path": str(path.resolve())})
        else:
            (store.root / "shardx.json").unlink(missing_ok=True)
        message = f"ShardX enabled: tokens are minted from {minter.settings_path} (API {config.shardx.base_url})."
    else:
        token = args.token
        if token in (None, "-"):
            token = _read_secret_input("ShardX API token: ")
        token = normalize_token((token or "").strip())
        save_token(store.secrets, token)
        config.shardx.token_source = "keyring"
        if args.base_url:
            config.shardx.base_url = args.base_url
        message = f"ShardX enabled: token saved to the {store.secrets.backend} secret store."
    config.shardx.enabled = True
    store.save_config(config)
    emit(args, {"enabled": True, "token_source": config.shardx.token_source, "base_url": config.shardx.base_url},
         message + " Restart your MCP clients to load the shardx_* tools.")
    return 0


def cmd_shardx_logout(args: argparse.Namespace) -> int:
    from .integrations.shardx import delete_token

    store = _store(args)
    delete_token(store.secrets)
    config = store.load_config()
    config.shardx.enabled = False
    store.save_config(config)
    (store.root / "shardx.json").unlink(missing_ok=True)
    emit(args, {"enabled": False}, "ShardX disabled and its token removed.")
    return 0


def cmd_shardx_profiles(args: argparse.Namespace) -> int:
    from .integrations.shardx import ShardXClient

    store = _store(args)
    client = ShardXClient.from_store(store, settings_path=_shardx_settings_path(store))
    try:
        profiles = client.list_profiles()
    finally:
        client.close()
    rows = [{"id": p.get("id"), "name": p.get("name"), "running": bool(p.get("running"))} for p in profiles]
    emit(args, rows, lambda: table([(r["name"], r["id"], "running" if r["running"] else "stopped") for r in rows],
                                   ["NAME", "ID", "STATE"]) if rows else "ShardX has no profiles.")
    return 0


# ---------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable JSON output")
    common.add_argument("--home", default=argparse.SUPPRESS, metavar="PATH",
                        help="data folder (default: PROFILEPILOT_HOME or the platform default)")

    parser = argparse.ArgumentParser(
        prog="profilepilot", parents=[common],
        description="Isolated native Chrome profiles (own cookies, history, proxy) for AI agents.",
    )
    parser.add_argument("--version", action="version", version=f"profilepilot {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(parent: Any, name: str, help_text: str, func: Callable[[argparse.Namespace], int]) -> argparse.ArgumentParser:
        p = parent.add_parser(name, help=help_text, description=help_text, parents=[common])
        p.set_defaults(func=func)
        return p

    # serve
    p = add(sub, "serve", "run the MCP server (stdio; --http for remote clients)", cmd_serve)
    p.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8931, help="port (default 8931)")
    p.add_argument("--path", default="/mcp", help="endpoint path (default /mcp)")
    p.add_argument("--public-host", action="append", metavar="HOST",
                   help="public host name of a tunnel (repeatable); allowed in Host/Origin headers")
    p.add_argument("--auth", choices=["secret-path", "token", "none"], default=None,
                   help="secret-path (default; for ChatGPT), token (bearer) or none (loopback only)")
    p.add_argument("--token", default=None, help="bearer token for --auth token ('-' = read from stdin; "
                                                 "default PROFILEPILOT_TOKEN or a generated one)")
    p.add_argument("--allow-private-network", action="store_true",
                   help="let remote clients open localhost / private-network URLs")
    p.add_argument("--files-anywhere", action="store_true",
                   help="stdio only: let cookies_export / cookies_import use any folder, not just the "
                        "profiles' exports folders")
    p.add_argument("--allow-sensitive-autofill", action="store_true",
                   help="--http only: also offer form_autofill_sensitive (card, SSN, password) to remote clients")
    p.add_argument("--i-understand", action="store_true", help="confirm --auth none on a loopback host")
    p.add_argument("--new-secret", action="store_true", help="rotate the secret path")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    # profile
    prof = sub.add_parser("profile", help="manage profiles", parents=[common])
    psub = prof.add_subparsers(dest="action", metavar="<action>", required=True)
    p = add(psub, "list", "list profiles", cmd_profile_list)
    p.add_argument("--tag")
    p = add(psub, "create", "create a profile", cmd_profile_create)
    p.add_argument("name")
    p.add_argument("--proxy", help="saved proxy name/id or proxy URL ('-' = read it from stdin)")
    p.add_argument("--proxy-scheme", choices=SCHEMES, default="http", help="scheme for proxies without one")
    p.add_argument("--tag", action="append", help="tag (repeatable)")
    p.add_argument("--notes")
    p.add_argument("--window", choices=WINDOW_MODES)
    p.add_argument("--browser", help="auto, chrome, edge, brave, chromium or a path")
    p.add_argument("--lang", help="language override, e.g. de-DE")
    p.add_argument("--timezone", help="IANA timezone override, e.g. Europe/Berlin")
    p.add_argument("--start-url")
    p.add_argument("--extra-arg", action="append", help="extra Chrome switch (repeatable)")
    p.add_argument("--identity", help="identity (name or id) that form autofill uses for this profile")
    p = add(psub, "show", "show a profile", cmd_profile_show)
    p.add_argument("profile")
    p = add(psub, "start", "start a profile's Chrome", cmd_profile_start)
    p.add_argument("profile")
    p.add_argument("--window", choices=WINDOW_MODES, help="window mode for this run")
    p.add_argument("--timeout", type=float, default=60.0)
    p = add(psub, "stop", "stop a profile's Chrome", cmd_profile_stop)
    p.add_argument("profile")
    p.add_argument("--timeout", type=float, default=20.0)
    p = add(psub, "delete", "move a stopped profile to the trash", cmd_profile_delete)
    p.add_argument("profile")
    p = add(psub, "restore", "restore a profile from the trash (no id: list the trash)", cmd_profile_restore)
    p.add_argument("trash_id", nargs="?")
    p = add(psub, "clone", "copy a profile", cmd_profile_clone)
    p.add_argument("profile")
    p.add_argument("new_name")
    p.add_argument("--copy-data", action="store_true", help="also copy cookies, logins and history")
    p = add(psub, "update", "change a profile", cmd_profile_update)
    p.add_argument("profile")
    p.add_argument("--name")
    p.add_argument("--notes")
    p.add_argument("--tag", action="append", help="replace the tags (repeatable)")
    p.add_argument("--clear-tags", action="store_true")
    proxy_group = p.add_mutually_exclusive_group()
    proxy_group.add_argument("--proxy", help="saved proxy name/id or proxy URL ('-' = stdin)")
    proxy_group.add_argument("--no-proxy", action="store_true", help="remove the proxy (direct connection)")
    p.add_argument("--proxy-scheme", choices=SCHEMES, default="http")
    p.add_argument("--window", choices=WINDOW_MODES)
    p.add_argument("--browser")
    p.add_argument("--lang", help="'' removes it")
    p.add_argument("--timezone", help="'' removes it")
    p.add_argument("--start-url", help="'' removes it")
    p.add_argument("--restore-session", dest="restore_session", action="store_true", default=None)
    p.add_argument("--no-restore-session", dest="restore_session", action="store_false")
    p.add_argument("--webrtc", choices=["auto", "proxy_only", "default"])
    p.add_argument("--extra-arg", action="append", help="replace the extra Chrome switches (repeatable; '' clears)")
    p.add_argument("--identity", help="identity (name or id) for form autofill; '' removes the link")

    # identity
    idn = sub.add_parser("identity", help="manage identities for form autofill", parents=[common],
                         description="Identities hold your details for form autofill. Card numbers, CVVs, SSNs and "
                                     "passwords are stored in the OS secret store with 'identity secret' and are "
                                     "only filled on sites you allow with 'identity allow'.")
    isub = idn.add_subparsers(dest="action", metavar="<action>", required=True)
    add(isub, "list", "list identities", cmd_identity_list)
    p = add(isub, "show", "show an identity (card, SSN and password masked)", cmd_identity_show)
    p.add_argument("identity")
    p = add(isub, "create", "create an identity", cmd_identity_create)
    p.add_argument("name")
    p.add_argument("--set", dest="assignments", action="append", metavar="KEY=VALUE",
                   help="a non-sensitive value, e.g. --set email=jane@example.com (repeatable)")
    p.add_argument("--notes")
    p = add(isub, "set", "set non-sensitive values (KEY= removes one)", cmd_identity_set)
    p.add_argument("identity")
    p.add_argument("assignments", nargs="*", metavar="KEY=VALUE")
    p.add_argument("--name", help="rename the identity")
    p.add_argument("--notes")
    p = add(isub, "secret", "store a card number, expiry, CVV, SSN or password (asked for without echo; "
                            "never on the command line)", cmd_identity_secret)
    p.add_argument("identity")
    p.add_argument("field", help="card_number, card_exp_month, card_exp_year, card_cvv, ssn or password")
    p.add_argument("value", nargs="*", help=argparse.SUPPRESS)  # refused: values never come from argv
    p.add_argument("--stdin", action="store_true", help="read the value from one line of stdin instead")
    p = add(isub, "clear", "remove one value (sensitive or not)", cmd_identity_clear)
    p.add_argument("identity")
    p.add_argument("field")
    p = add(isub, "allow", "allow sensitive autofill on a site, e.g. https://shop.example.com", cmd_identity_allow)
    p.add_argument("identity")
    p.add_argument("origin")
    p = add(isub, "disallow", "stop allowing sensitive autofill on a site", cmd_identity_disallow)
    p.add_argument("identity")
    p.add_argument("origin")
    p = add(isub, "delete", "delete an identity and its stored secrets", cmd_identity_delete)
    p.add_argument("identity")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    add(isub, "fields", "list the identity field keys", cmd_identity_fields)

    # proxy
    prx = sub.add_parser("proxy", help="manage proxies", parents=[common])
    xsub = prx.add_subparsers(dest="action", metavar="<action>", required=True)
    p = add(xsub, "list", "list saved proxies", cmd_proxy_list)
    p.add_argument("--tag")
    p = add(xsub, "add", "save a proxy (omit SPEC or use '-' to read it from stdin)", cmd_proxy_add)
    p.add_argument("spec", nargs="?", help="scheme://user:pass@host:port, host:port:user:pass, ...")
    p.add_argument("--name")
    p.add_argument("--tag", action="append")
    p.add_argument("--scheme", choices=SCHEMES, default="http", help="scheme for proxies without one")
    p = add(xsub, "import", "import proxies from a file, one per line ('-' = stdin)", cmd_proxy_import)
    p.add_argument("file")
    p.add_argument("--scheme", choices=SCHEMES, default="http")
    p.add_argument("--tag", action="append")
    p = add(xsub, "remove", "delete a saved proxy", cmd_proxy_remove)
    p.add_argument("proxy")
    p.add_argument("--force", action="store_true", help="also unbind it from profiles")
    p = add(xsub, "test", "check a proxy or a profile's route (exit IP, country, latency)", cmd_proxy_test)
    p.add_argument("ref", nargs="?", help="saved proxy or profile (default: direct connection)")
    p.add_argument("--direct", action="store_true", help="test this computer's direct connection")
    p.add_argument("--timeout", type=float, default=12.0)

    p = add(sub, "status", "list running profiles", cmd_status)
    p = add(sub, "stop-all", "stop every running profile", cmd_stop_all)
    p.add_argument("--timeout", type=float, default=20.0)

    from .install import CLIENTS

    p = add(sub, "install", "register the MCP server with a client, or 'print' the config snippets", cmd_install)
    p.add_argument("client", choices=[*CLIENTS, "print"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--python", help="interpreter to register (default: this one)")
    p.add_argument("--client", dest="only", help="with 'print': only this client's snippet")
    p.add_argument("--port", type=int, default=8931, help="with 'print': HTTP port for the remote snippets")
    p = add(sub, "uninstall", "remove the MCP server from a client's config", cmd_uninstall)
    p.add_argument("client", choices=list(CLIENTS))
    p.add_argument("--dry-run", action="store_true")

    add(sub, "browsers", "list the installed browsers a profile can run in", cmd_browsers)
    add(sub, "doctor", "check the installation", cmd_doctor)

    shx = sub.add_parser("shardx", help="optional ShardX launcher backend", parents=[common])
    ssub = shx.add_subparsers(dest="action", metavar="<action>", required=True)
    add(ssub, "status", "is the ShardX launcher reachable?", cmd_shardx_status)
    p = add(ssub, "login", "enable ShardX with an API token (stdin) or minted from its settings.json", cmd_shardx_login)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--token", nargs="?", const="-", default=None,
                       help="API token (omit the value or use '-' to read it from stdin)")
    group.add_argument("--from-settings", nargs="?", const="", default=None, metavar="PATH",
                       help="mint short-lived tokens from ShardX's settings.json (default location if PATH omitted)")
    p.add_argument("--base-url", help="launcher API URL (default http://127.0.0.1:40325)")
    add(ssub, "logout", "disable ShardX and forget its token", cmd_shardx_logout)
    add(ssub, "profiles", "list ShardX profiles", cmd_shardx_profiles)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the exit code (0 ok, 1 error, 2 usage)."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["host"]:  # hidden: the per-profile host process
        return _host_main(args_list[1:])
    parser = build_parser()
    args = parser.parse_args(args_list)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    args.json = getattr(args, "json", False)
    args.home = getattr(args, "home", None)
    if args.func is not cmd_serve:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except (AttributeError, ValueError):
                pass
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except CliError as exc:
        _err(f"error: {exc}")
        return 1
    except ProxyParseError:
        _err(f"error: {PROXY_FORMAT_HELP}")
        return 1
    except ProfilePilotError as exc:
        _err(f"error: {exc}")
        return 1
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', ())) or 'value'}: {e.get('msg')}"
                             for e in exc.errors())
        _err(f"error: invalid value(s): {problems}")
        return 1


__all__ = ["main", "build_parser", "shell_name"]
