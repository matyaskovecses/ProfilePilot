"""Register (and unregister) the ProfilePilot MCP server with AI clients.

Supported clients and the files they read:

=================  =====================================================================
claude-desktop     ``%APPDATA%\\Claude\\claude_desktop_config.json`` plus, for the Microsoft
                   Store (MSIX) build, every existing
                   ``%LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude\\`` copy.
                   macOS: ``~/Library/Application Support/Claude/``; Linux: ``~/.config/Claude/``.
claude-code        ``claude mcp add --scope user ...`` (the CLI owns its config file). When the
                   ``claude`` CLI is not on PATH the command is returned for the user to run.
codex              ``~/.codex/config.toml`` (or ``$CODEX_HOME/config.toml``). Shared by the Codex
                   CLI, the IDE extension and the ChatGPT desktop app.
cursor             ``~/.cursor/mcp.json``.
=================  =====================================================================

Every edit is conservative:

* the original file is backed up to ``<name>.bak-<timestamp>`` before it is changed;
* unknown keys, other servers and (for TOML) every byte outside the ``profilepilot`` block are
  preserved; user additions inside our entry (extra ``env`` variables, other keys) are kept;
* files with a UTF-8 BOM or in UTF-16 (PowerShell's defaults) are read; JSON is written back as
  UTF-8 without BOM, which is what Electron/Node based clients can parse;
* a file that cannot be parsed is never overwritten (``InstallError``), and an edited TOML file
  is re-parsed with :mod:`tomllib` before it is written;
* running ``register`` twice is a no-op the second time (no write, no new backup).

All locations are injectable through :class:`Locations`, so tests never touch real configs.
"""

from __future__ import annotations

import json
import logging
import math
import ntpath
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, Literal, get_args

from .errors import ProfilePilotError

log = logging.getLogger("profilepilot.install")

Client = Literal["claude-desktop", "claude-code", "codex", "cursor"]
CLIENTS: tuple[str, ...] = get_args(Client)

SERVER_NAME = "profilepilot"
SERVER_ARGS: tuple[str, ...] = ("-m", "profilepilot", "serve")
DEFAULT_HTTP_PORT = 8931  # 8765 (the usual example port) is often taken; see docs/CLIENTS.md
CODEX_STARTUP_TIMEOUT_SEC = 60  # Codex default is 10 s; first Chrome start can take longer
CODEX_TOOL_TIMEOUT_SEC = 180  # Codex default is 60 s; navigation + waits can take longer
#: Environment variables copied from the installing shell into the client's server entry, so
#: every client shares the same data root / browser choice as the CLI that registered it.
PASSTHROUGH_ENV: tuple[str, ...] = ("PROFILEPILOT_HOME", "PROFILEPILOT_BROWSER")
REPO = "matyaskovecses/ProfilePilot"
REPO_URL = f"https://github.com/{REPO}"


class InstallError(ProfilePilotError):
    """A client configuration could not be read or updated safely."""


# --------------------------------------------------------------------------- locations


@dataclass(frozen=True)
class Locations:
    """Where client configuration lives. Build with :meth:`detect`, or explicitly in tests."""

    home: Path
    appdata: Path | None = None  # Windows %APPDATA% (Roaming)
    localappdata: Path | None = None  # Windows %LOCALAPPDATA%
    codex_home: Path | None = None  # $CODEX_HOME (default: ~/.codex)
    xdg_config_home: Path | None = None  # Linux $XDG_CONFIG_HOME (default: ~/.config)
    platform: str = sys.platform
    #: Command prefix that runs the Claude Code CLI, e.g. ``("C:/.../claude.exe",)``; None if absent.
    claude_cli: tuple[str, ...] | None = None

    @classmethod
    def detect(cls, environ: Mapping[str, str] | None = None, *, platform: str | None = None) -> Locations:
        """Resolve locations from ``environ`` (default: ``os.environ``) for ``platform``."""
        env = os.environ if environ is None else environ
        plat = platform or sys.platform

        def p(name: str) -> Path | None:
            value = env.get(name)
            return Path(value).expanduser() if value else None

        if plat == "win32":
            home = p("USERPROFILE") or p("HOME") or Path.home()
            appdata = p("APPDATA") or home / "AppData" / "Roaming"
            localappdata = p("LOCALAPPDATA") or home / "AppData" / "Local"
        else:
            home = p("HOME") or Path.home()
            appdata = localappdata = None
        found = shutil.which("claude", path=env.get("PATH"))
        return cls(
            home=home,
            appdata=appdata,
            localappdata=localappdata,
            codex_home=p("CODEX_HOME"),
            xdg_config_home=p("XDG_CONFIG_HOME"),
            platform=plat,
            claude_cli=(found,) if found else None,
        )

    # -- per-client files -------------------------------------------------------------

    def claude_desktop_configs(self) -> list[Path]:
        """Primary config path first, then any MSIX (Microsoft Store) copies that exist."""
        if self.platform == "win32":
            appdata = self.appdata or self.home / "AppData" / "Roaming"
            paths = [appdata / "Claude" / "claude_desktop_config.json"]
            paths.extend(d / "claude_desktop_config.json" for d in self._msix_claude_dirs())
            return paths
        if self.platform == "darwin":
            return [self.home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]
        base = self.xdg_config_home or self.home / ".config"
        return [base / "Claude" / "claude_desktop_config.json"]

    def _msix_claude_dirs(self) -> list[Path]:
        local = self.localappdata or self.home / "AppData" / "Local"
        packages = local / "Packages"
        try:
            candidates = sorted(d for d in packages.iterdir() if d.name.lower().startswith("claude_"))
        except OSError:
            return []
        dirs = [d / "LocalCache" / "Roaming" / "Claude" for d in candidates]
        return [d for d in dirs if d.is_dir()]

    def codex_config(self) -> Path:
        return (self.codex_home or self.home / ".codex") / "config.toml"

    def cursor_config(self) -> Path:
        return self.home / ".cursor" / "mcp.json"


# --------------------------------------------------------------------------- server spec


@dataclass(frozen=True)
class ServerSpec:
    """How a client launches the stdio MCP server: ``<command> <args...>`` with ``env``."""

    command: str
    args: tuple[str, ...] = SERVER_ARGS
    env: dict[str, str] = field(default_factory=dict)

    @property
    def argv(self) -> list[str]:
        return [self.command, *self.args]

    def json_entry(self) -> dict[str, Any]:
        return {"command": self.command, "args": list(self.args), "env": dict(self.env)}


def server_spec(
    python: str | None = None, env: Mapping[str, str] | None = None, *, platform: str | None = None
) -> ServerSpec:
    """Build the server launch spec.

    ``python`` defaults to the running interpreter and is made absolute *without* resolving
    symlinks (a POSIX venv's ``python`` is a symlink; resolving it would escape the venv).
    ``env`` defaults to the :data:`PASSTHROUGH_ENV` variables that are set in this process.
    ``platform`` is the platform the config is for (default: this one); a path for another
    platform family is only normalised, never resolved against this machine's directories.
    """
    exe = python or sys.executable
    if not exe:
        raise InstallError("Cannot determine the Python interpreter; pass python=<path to python>.")
    if env is None:
        env = {k: os.environ[k] for k in PASSTHROUGH_ENV if os.environ.get(k)}
    return ServerSpec(command=_absolute(exe, platform or sys.platform), env=dict(env))


def _absolute(path: str, platform: str) -> str:
    if (platform == "win32") == (os.name == "nt"):
        return os.path.abspath(path)
    flavour = ntpath if platform == "win32" else posixpath
    return flavour.normpath(path) if flavour.isabs(path) else path


# --------------------------------------------------------------------------- public API


def register(
    client: Client,
    *,
    python: str | None = None,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    locations: Locations | None = None,
) -> str:
    """Register ProfilePilot (``<python> -m profilepilot serve``) with ``client``.

    Returns a human-readable report (paths changed, backups made, next steps). With
    ``dry_run`` nothing is written or executed; the report says what would happen.
    Raises :class:`InstallError` if a config file exists but cannot be parsed safely.
    """
    loc = locations or Locations.detect()
    spec = server_spec(python, env)
    if client == "claude-desktop":
        lines = [_register_json(p, spec, dry_run) for p in loc.claude_desktop_configs()]
        lines.append("Quit Claude Desktop completely (tray icon > Quit) and start it again.")
        return "\n".join(lines)
    if client == "cursor":
        return _register_json(loc.cursor_config(), spec, dry_run) + "\nRestart Cursor (or reload MCP servers)."
    if client == "codex":
        return _register_codex(loc.codex_config(), spec, dry_run) + (
            "\nRestart Codex / the ChatGPT desktop app to load it."
        )
    if client == "claude-code":
        return _register_claude_code(loc, spec, dry_run)
    raise InstallError(f"Unknown client {client!r}; choose one of: {', '.join(CLIENTS)}.")


def unregister(client: Client, *, dry_run: bool = False, locations: Locations | None = None) -> str:
    """Remove the ``profilepilot`` server entry from ``client`` (backing files up first)."""
    loc = locations or Locations.detect()
    if client == "claude-desktop":
        return "\n".join(_unregister_json(p, dry_run) for p in loc.claude_desktop_configs())
    if client == "cursor":
        return _unregister_json(loc.cursor_config(), dry_run)
    if client == "codex":
        return _unregister_codex(loc.codex_config(), dry_run)
    if client == "claude-code":
        argv = ["mcp", "remove", "--scope", "user", SERVER_NAME]
        if loc.claude_cli is None or dry_run:
            prefix = "Would run" if loc.claude_cli else "The 'claude' CLI is not on PATH. Run"
            return f"{prefix}: {format_command(['claude', *argv], loc.platform)}"
        proc = _run_cli([*loc.claude_cli, *argv])
        if proc.returncode != 0:
            return f"Claude Code: no user-scope '{SERVER_NAME}' server to remove ({_first_line(proc)})."
        return f"Removed '{SERVER_NAME}' from Claude Code (user scope)."
    raise InstallError(f"Unknown client {client!r}; choose one of: {', '.join(CLIENTS)}.")


def config_paths(client: Client, locations: Locations | None = None) -> list[Path]:
    """Config files ProfilePilot edits for ``client`` (empty for claude-code, which uses its CLI)."""
    loc = locations or Locations.detect()
    if client == "claude-desktop":
        return loc.claude_desktop_configs()
    if client == "cursor":
        return [loc.cursor_config()]
    if client == "codex":
        return [loc.codex_config()]
    if client == "claude-code":
        return []
    raise InstallError(f"Unknown client {client!r}; choose one of: {', '.join(CLIENTS)}.")


def is_registered(client: Client, locations: Locations | None = None) -> bool | None:
    """Whether ``client`` has a ``profilepilot`` entry. ``None`` means "cannot tell"
    (claude-code without its CLI, or an unreadable config file)."""
    loc = locations or Locations.detect()
    try:
        if client in ("claude-desktop", "cursor"):
            for path in config_paths(client, loc):
                servers = _read_json_config(path)[0].get("mcpServers")
                if isinstance(servers, dict) and SERVER_NAME in servers:
                    return True
            return False
        if client == "codex":
            text, _ = _read_text(loc.codex_config())
            if text is None:
                return False
            doc = _parse_toml(text)
            return None if doc is None else _has_server(doc)
        if client == "claude-code":
            if loc.claude_cli is None:
                return None
            return _run_cli([*loc.claude_cli, "mcp", "get", SERVER_NAME]).returncode == 0
    except (InstallError, OSError, subprocess.SubprocessError):
        return None
    raise InstallError(f"Unknown client {client!r}; choose one of: {', '.join(CLIENTS)}.")


def snippets(
    python: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    http_port: int = DEFAULT_HTTP_PORT,
    platform: str | None = None,
) -> dict[str, str]:
    """Ready-to-paste configuration for every client (``profilepilot install print``).

    Keys: ``claude-desktop``, ``claude-code``, ``claude-code-plugin``, ``codex``, ``cursor``,
    ``chatgpt``, ``http``.
    """
    plat = platform or sys.platform
    spec = server_spec(python, env, platform=plat)
    entry = {"mcpServers": {SERVER_NAME: spec.json_entry()}}
    entry_json = json.dumps(entry, indent=2, ensure_ascii=False)
    url = f"http://127.0.0.1:{http_port}/mcp"
    return {
        "claude-desktop": entry_json,
        "claude-code": format_command(_claude_add_argv("claude", spec), plat),
        "claude-code-plugin": (
            f"/plugin marketplace add {REPO}\n"
            f"/plugin install {SERVER_NAME}@{SERVER_NAME}\n"
            "# The plugin runs the server with uvx: install uv first (https://docs.astral.sh/uv/)."
        ),
        "codex": render_codex_block(spec).rstrip("\n"),
        "cursor": entry_json,
        "chatgpt": _chatgpt_snippet(spec, plat, http_port),
        "http": "\n".join(
            [
                f"profilepilot serve --http --port {http_port} --auth token --token <TOKEN>",
                f"claude mcp add --transport http --scope user {SERVER_NAME} {url} "
                '--header "Authorization: Bearer <TOKEN>"',
            ]
        ),
    }


def tunnel_command(spec: ServerSpec, platform: str | None = None) -> tuple[str, str | None]:
    """``spec`` as one command string, for tools that take the stdio command as a single value
    (OpenAI's ``tunnel-client --mcp-command``). Returns ``(command, warning)``.

    How tunnel-client splits that string is undocumented, so a space in the interpreter path is
    avoided: on Windows the 8.3 short path is used when the volume has one; otherwise the
    warning explains the problem.
    """
    plat = platform or sys.platform
    exe = spec.command
    warning = None
    if re.search(r"\s", exe):
        short = _windows_short_path(exe) if plat == "win32" else None
        if short and not re.search(r"\s", short):
            exe = short
        else:
            warning = (
                "The Python path contains spaces. If tunnel-client cannot start it, recreate the "
                "virtual environment in a folder without spaces and register that python instead."
            )
    return " ".join([_portable_path(exe, plat), *spec.args]), warning


def _chatgpt_snippet(spec: ServerSpec, plat: str, http_port: int) -> str:
    command, warning = tunnel_command(spec, plat)
    quote = "'" if "'" not in command else '"'
    lines = [
        "# ChatGPT web cannot start local programs, so it needs a remote connection.",
        "# Recommended: OpenAI Secure MCP Tunnel (outbound-only, no public URL). The API key's",
        "# user needs the Tunnels Read + Use permissions.",
        "# 1. Create a tunnel at https://platform.openai.com/settings/organization/tunnels",
        "# 2. Download tunnel-client from https://github.com/openai/tunnel-client/releases/latest",
        "# 3. In a terminal" + (" (PowerShell)" if plat == "win32" else "") + " with CONTROL_PLANE_API_KEY set to that key:",
    ]
    if warning:
        lines.append(f"#    {warning}")
    for key, value in spec.env.items():  # the stdio child inherits tunnel-client's environment
        lines.append(f"$env:{key} = {_ps_quote(value)}" if plat == "win32" else f"export {key}={shlex.quote(value)}")
    lines += [
        "tunnel-client init --sample sample_mcp_stdio_local --profile profilepilot "
        f"--tunnel-id <tunnel_id> --mcp-command {quote}{command}{quote}",
        "tunnel-client run --profile profilepilot",
        "# 4. chatgpt.com/plugins > + > Add custom MCP server > Connection: Tunnel > pick the tunnel",
        "#    > accept the risk warning > Create as a plugin. Keep `tunnel-client run` running.",
        "#",
        "# Alternative: a public HTTPS URL. Read the security notes in docs/CLIENTS.md first.",
        f"#   profilepilot serve --http --port {http_port} --auth secret-path --public-host <name>.trycloudflare.com",
        f"#   cloudflared tunnel --url http://127.0.0.1:{http_port}",
        "#   Add the https://<host>/mcp/<secret> URL that serve prints, with authentication 'No auth'.",
    ]
    return "\n".join(lines)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_short_path(path: str) -> str | None:
    """The 8.3 short form of an existing ``path`` (Windows only), or None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # private instance: no global argtypes
        get_short = kernel32.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD
        size = get_short(path, None, 0)
        if not size:
            return None
        buf = ctypes.create_unicode_buffer(size)
        return buf.value if get_short(path, buf, size) else None
    except (OSError, AttributeError, ValueError):
        return None


def render_codex_block(spec: ServerSpec, existing: Mapping[str, Any] | None = None) -> str:
    """The ``[mcp_servers.profilepilot]`` TOML table for ``spec``.

    Keys from an ``existing`` table that ProfilePilot does not manage are carried over; ``env``
    is merged (ours win); the timeouts are raised to at least our defaults, never lowered.
    """
    old = dict(existing or {})
    env = dict(old.pop("env", None) or {})
    env.update(spec.env)
    startup = _at_least(old.pop("startup_timeout_sec", None), CODEX_STARTUP_TIMEOUT_SEC)
    tool = _at_least(old.pop("tool_timeout_sec", None), CODEX_TOOL_TIMEOUT_SEC)
    old.pop("command", None)
    old.pop("args", None)
    lines = [
        f"[mcp_servers.{SERVER_NAME}]",
        f"command = {_toml_str(spec.command)}",
        f"args = {_toml_value(list(spec.args))}",
        f"startup_timeout_sec = {_toml_value(startup)}",
        f"tool_timeout_sec = {_toml_value(tool)}",
    ]
    if env:
        lines.append(f"env = {_toml_value(env)}")
    lines.extend(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in old.items())
    return "\n".join(lines) + "\n"


def format_command(argv: Sequence[str], platform: str | None = None) -> str:
    """Quote ``argv`` for display in the platform's usual shell."""
    if (platform or sys.platform) == "win32":
        return subprocess.list2cmdline(list(argv))
    return shlex.join(list(argv))


# --------------------------------------------------------------------------- JSON clients


@dataclass
class _TextFormat:
    newline: str = "\n"
    bom: bool = False
    reencode: bool = False  # the file was UTF-16 / had a BOM we want to drop


def _read_text(path: Path) -> tuple[str | None, _TextFormat]:
    """Read a config file tolerating a UTF-8 BOM or UTF-16. ``None`` if it does not exist."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, _TextFormat()
    except OSError as exc:
        raise InstallError(f"Cannot read {path}: {exc.strerror or exc}") from None
    fmt = _TextFormat()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError:
            raise InstallError(f"{path} is not valid UTF-16 text; fix or remove it first.") from None
        fmt.reencode = True
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise InstallError(f"{path} is not UTF-8 text; fix or remove it first.") from None
        fmt.bom = raw.startswith(b"\xef\xbb\xbf")
    crlf = text.count("\r\n")
    if crlf and crlf >= text.count("\n") - crlf:
        fmt.newline = "\r\n"
    return text, fmt


def _read_json_config(path: Path) -> tuple[dict[str, Any], _TextFormat]:
    text, fmt = _read_text(path)
    return _parse_json_text(text, path), fmt


def _parse_json_text(text: str | None, path: Path) -> dict[str, Any]:
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # Only the position is reported: the file may hold other servers' tokens.
        raise InstallError(
            f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}); "
            "fix it by hand first - it was left unchanged."
        ) from None
    if not isinstance(data, dict):
        raise InstallError(f"{path} does not contain a JSON object; it was left unchanged.")
    return data


def _detect_indent(text: str | None) -> int | str:
    if text:
        m = re.search(r"^([ \t]+)\S", text, re.MULTILINE)
        if m:
            ws = m.group(1)
            return "\t" if "\t" in ws else max(1, min(len(ws), 8))
    return 2


def _servers_of(data: dict[str, Any], path: Path) -> dict[str, Any]:
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError(f'"mcpServers" in {path} is not an object; it was left unchanged.')
    return servers


def _merge_entry(existing: Any, spec: ServerSpec) -> dict[str, Any]:
    entry = dict(existing) if isinstance(existing, dict) else {}
    old_env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    entry["command"] = spec.command
    entry["args"] = list(spec.args)
    entry["env"] = {**old_env, **spec.env}
    return entry


def _register_json(path: Path, spec: ServerSpec, dry_run: bool) -> str:
    def mutate(data: dict[str, Any]) -> None:
        servers = _servers_of(data, path)
        servers[SERVER_NAME] = _merge_entry(servers.get(SERVER_NAME), spec)

    changed, backup = _edit_json(path, mutate, dry_run)
    return _report(path, changed, backup, dry_run, f"Registered '{SERVER_NAME}' in")


def _unregister_json(path: Path, dry_run: bool) -> str:
    if not path.exists():
        return f"Nothing to remove: {path} does not exist."
    removed = False

    def mutate(data: dict[str, Any]) -> None:
        nonlocal removed
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and SERVER_NAME in servers:
            del servers[SERVER_NAME]
            removed = True

    changed, backup = _edit_json(path, mutate, dry_run, only_if=lambda: removed)
    if not removed:
        return f"Nothing to remove: no '{SERVER_NAME}' entry in {path}."
    return _report(path, changed, backup, dry_run, f"Removed '{SERVER_NAME}' from")


def _edit_json(
    path: Path,
    mutate: Callable[[dict[str, Any]], None],
    dry_run: bool,
    only_if: Callable[[], bool] | None = None,
) -> tuple[bool, Path | None]:
    """Apply ``mutate`` to the JSON object in ``path``; returns (changed, backup path)."""
    text, fmt = _read_text(path)
    data = _parse_json_text(text, path)
    before = json.dumps(data, sort_keys=False)
    mutate(data)
    if only_if is not None and not only_if():
        return False, None
    after = json.dumps(data, sort_keys=False)
    if text is not None and before == after and not fmt.bom and not fmt.reencode:
        return False, None
    payload = json.dumps(data, indent=_detect_indent(text), ensure_ascii=False) + "\n"
    if fmt.newline != "\n":
        payload = payload.replace("\n", fmt.newline)
    if dry_run:
        return True, None
    backup = _backup(path) if text is not None else None
    _atomic_write(path, payload.encode("utf-8"))
    log.info("updated %s", path)
    return True, backup


# --------------------------------------------------------------------------- Codex (TOML)


def _register_codex(path: Path, spec: ServerSpec, dry_run: bool) -> str:
    text, fmt = _read_text(path)
    text = text or ""
    doc = _parse_toml_checked(text, path)
    lines = _split_lines(text)
    regions = _codex_regions(lines)
    servers = (doc or {}).get("mcp_servers")
    existing = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    if existing is not None and not regions:
        raise InstallError(
            f"{path} defines mcp_servers.{SERVER_NAME} inline or with dotted keys, which ProfilePilot "
            "cannot edit safely. Remove that definition by hand, then run the command again."
        )
    if existing is not None and not isinstance(existing, dict):
        raise InstallError(f"mcp_servers.{SERVER_NAME} in {path} is not a table; it was left unchanged.")
    block = render_codex_block(spec, existing).replace("\n", fmt.newline)
    if regions:
        new_lines = _replace_regions(lines, regions, block)
    else:
        new_lines = list(lines)
        if new_lines:
            if not new_lines[-1].endswith("\n"):
                new_lines[-1] += fmt.newline
            if new_lines[-1].strip():
                new_lines.append(fmt.newline)
        new_lines.append(block)
    new_text = "".join(new_lines)
    _validate_codex(new_text, path, spec)
    changed = new_text != text or fmt.reencode
    backup = _write_with_backup(path, new_text, fmt) if changed and not dry_run else None
    return _report(path, changed, backup, dry_run, f"Registered '{SERVER_NAME}' in")


def _unregister_codex(path: Path, dry_run: bool) -> str:
    text, fmt = _read_text(path)
    if text is None:
        return f"Nothing to remove: {path} does not exist."
    doc = _parse_toml_checked(text, path)
    lines = _split_lines(text)
    regions = _codex_regions(lines)
    if not regions:
        if doc is not None and _has_server(doc):
            raise InstallError(
                f"{path} defines mcp_servers.{SERVER_NAME} inline or with dotted keys; remove it by hand."
            )
        return f"Nothing to remove: no [mcp_servers.{SERVER_NAME}] table in {path}."
    new_text = "".join(_replace_regions(lines, regions, None))
    after = _parse_toml(new_text)
    if after is not None and _has_server(after):
        raise InstallError(f"Could not remove mcp_servers.{SERVER_NAME} from {path} safely; edit it by hand.")
    if doc is not None and after is None and _toml_available():
        raise InstallError(f"Removing the block would leave {path} invalid; it was left unchanged.")
    backup = None if dry_run else _write_with_backup(path, new_text, fmt)
    return _report(path, True, backup, dry_run, f"Removed '{SERVER_NAME}' from")


def _has_server(doc: Mapping[str, Any]) -> bool:
    servers = doc.get("mcp_servers")
    return isinstance(servers, dict) and SERVER_NAME in servers


def _write_with_backup(path: Path, text: str, fmt: _TextFormat) -> Path | None:
    """Back up ``path`` (if it exists), then write ``text``, keeping a UTF-8 BOM if it had one."""
    data = text.encode("utf-8")
    if fmt.bom and not fmt.reencode:
        data = b"\xef\xbb\xbf" + data
    backup = _backup(path) if path.exists() else None
    _atomic_write(path, data)
    log.info("updated %s", path)
    return backup


def _validate_codex(text: str, path: Path, spec: ServerSpec) -> None:
    doc = _parse_toml(text)
    if doc is None:
        if _toml_available():
            raise InstallError(f"Editing {path} would produce invalid TOML; it was left unchanged.")
        log.warning("tomllib/tomli unavailable: %s was edited without validation", path)
        return
    entry = (doc.get("mcp_servers") or {}).get(SERVER_NAME) or {}
    if entry.get("command") != spec.command or entry.get("args") != list(spec.args):
        raise InstallError(f"Validation of the edited {path} failed; it was left unchanged.")


def _parse_toml_checked(text: str, path: Path) -> dict[str, Any] | None:
    if not text.strip():
        return {}
    doc = _parse_toml(text)
    if doc is None and _toml_available():
        raise InstallError(f"{path} is not valid TOML; fix it by hand first - it was left unchanged.")
    return doc


def _toml_loads() -> Callable[[str], dict[str, Any]] | None:
    try:
        import tomllib  # Python 3.11+

        return tomllib.loads
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        try:
            import tomli  # type: ignore[import-not-found]

            return tomli.loads
        except ModuleNotFoundError:
            return None


def _toml_available() -> bool:
    return _toml_loads() is not None


def _parse_toml(text: str) -> dict[str, Any] | None:
    loads = _toml_loads()
    if loads is None:
        return None
    try:
        return loads(text)
    except Exception:  # tomllib.TOMLDecodeError (or tomli's); message may quote file content
        return None


def _split_lines(text: str) -> list[str]:
    """Split on ``\\n`` only, keeping line ends, so ``"".join(lines) == text`` exactly."""
    return re.findall(r"[^\n]*\n|[^\n]+$", text)


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _parse_header(line: str) -> tuple[tuple[str, ...], bool] | None:
    """Parse a TOML table header line into (key path, is_array_of_tables), else None."""
    s = line.strip()
    if not s.startswith("["):
        return None
    is_array = s.startswith("[[")
    pos = 2 if is_array else 1
    parts: list[str] = []
    try:
        while True:
            while s[pos] in " \t":
                pos += 1
            if s[pos] == '"':
                end = pos + 1
                while s[end] != '"':
                    end += 2 if s[end] == "\\" else 1
                parts.append(json.loads(s[pos : end + 1]))
                pos = end + 1
            elif s[pos] == "'":
                end = s.index("'", pos + 1)
                parts.append(s[pos + 1 : end])
                pos = end + 1
            else:
                m = _BARE_KEY.match(s, pos)
                if not m:
                    return None
                parts.append(m.group(0))
                pos = m.end()
            while pos < len(s) and s[pos] in " \t":
                pos += 1
            if pos < len(s) and s[pos] == ".":
                pos += 1
                continue
            break
    except (IndexError, ValueError):
        return None
    close = "]]" if is_array else "]"
    if not s.startswith(close, pos):
        return None
    rest = s[pos + len(close) :].strip()
    if rest and not rest.startswith("#"):
        return None
    return tuple(parts), is_array


def _scan_value_line(line: str, ml: str | None, depth: int) -> tuple[str | None, int]:
    """Advance the (multi-line string, bracket depth) state across one non-header line."""
    i, n = 0, len(line)
    while i < n:
        if ml is not None:
            if ml == '"""':
                if line[i] == "\\":
                    i += 2
                    continue
            if line.startswith(ml, i):
                i += 3
                while i < n and line[i] == ml[0]:  # up to two extra quotes may close the string
                    i += 1
                ml = None
                continue
            i += 1
            continue
        c = line[i]
        if c == "#":
            break
        if line.startswith('"""', i) or line.startswith("'''", i):
            ml = line[i : i + 3]
            i += 3
            continue
        if c == '"':
            i += 1
            while i < n and line[i] != '"':
                i += 2 if line[i] == "\\" else 1
            i += 1
            continue
        if c == "'":
            j = line.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth = max(0, depth - 1)
        i += 1
    return ml, depth


def _table_headers(lines: Sequence[str]) -> list[tuple[int, tuple[str, ...]]]:
    """(line index, key path) of every real table header, skipping look-alikes inside
    multi-line strings and multi-line arrays."""
    headers: list[tuple[int, tuple[str, ...]]] = []
    ml: str | None = None
    depth = 0
    for idx, line in enumerate(lines):
        if ml is None and depth == 0:
            parsed = _parse_header(line)
            if parsed is not None:
                headers.append((idx, parsed[0]))
                continue
        ml, depth = _scan_value_line(line, ml, depth)
    return headers


def _codex_regions(lines: Sequence[str]) -> list[tuple[int, int]]:
    """Line ranges [start, end) of ``[mcp_servers.profilepilot]`` and its sub-tables.

    Trailing blank and comment lines are left out: they usually introduce the next table.
    """
    headers = _table_headers(lines)
    regions: list[tuple[int, int]] = []
    for k, (start, key) in enumerate(headers):
        if key[:2] != ("mcp_servers", SERVER_NAME):
            continue
        stop = headers[k + 1][0] if k + 1 < len(headers) else len(lines)
        end = stop
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        regions.append((start, end))
    return regions


def _replace_regions(lines: Sequence[str], regions: Sequence[tuple[int, int]], block: str | None) -> list[str]:
    """Drop every region; put ``block`` (if any) where the first one was."""
    out: list[str] = []
    pos = 0
    removed_last = False
    for n, (start, end) in enumerate(regions):
        out.extend(lines[pos:start])
        if n == 0 and block is not None:
            out.append(block)
            removed_last = False
        else:
            # Removed outright: don't leave a doubled blank line behind.
            if out and not out[-1].strip() and end < len(lines) and not lines[end].strip():
                end += 1
            removed_last = True
        pos = end
    rest = list(lines[pos:])
    if removed_last and not rest and out and not out[-1].strip():
        out.pop()  # the block was appended after a separating blank line: drop that too
    out.extend(rest)
    return out


def _at_least(value: Any, minimum: int) -> int | float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= minimum:
        return value
    return minimum


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_basic(key)


def _toml_basic(value: str) -> str:
    # JSON string syntax is valid TOML basic-string syntax; TOML additionally forbids raw DEL.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_str(value: str) -> str:
    """A literal string for values with backslashes (Windows paths need no escaping), when it
    can represent them; a basic (double-quoted) string otherwise."""
    if "\\" in value and "'" not in value and not re.search(r"[\x00-\x08\x0a-\x1f\x7f]", value):
        return f"'{value}'"
    return _toml_basic(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise InstallError(f"Cannot write a value of type {type(value).__name__} to TOML.")


# --------------------------------------------------------------------------- Claude Code CLI


def _claude_add_argv(cli: str, spec: ServerSpec) -> list[str]:
    # `--env` is variadic: a server name right after it would be read as another KEY=value
    # pair and rejected, so the env pairs go first and `--scope user` separates them.
    argv = [cli, "mcp", "add"]
    for key, value in spec.env.items():
        argv += ["--env", f"{key}={value}"]
    return [*argv, "--scope", "user", SERVER_NAME, "--", *spec.argv]


def _register_claude_code(loc: Locations, spec: ServerSpec, dry_run: bool) -> str:
    display = format_command(_claude_add_argv("claude", spec), loc.platform)
    if loc.claude_cli is None:
        return (
            "The 'claude' CLI is not on PATH. Run this in a terminal (or use the plugin, see "
            f"docs/CLIENTS.md):\n{display}"
        )
    argv = _claude_add_argv("claude", spec)
    if not _batch_safe(loc.claude_cli, argv[1:]):
        return (
            "The 'claude' CLI on PATH is a batch file and the command line contains characters that "
            f"cmd.exe would reinterpret, so it was not run. Run this yourself:\n{display}"
        )
    if dry_run:
        remove = format_command(["claude", "mcp", "remove", "--scope", "user", SERVER_NAME], loc.platform)
        return f"Would run: {remove}\nWould run: {display}"
    # `claude mcp add` refuses an existing name, so drop a previous user-scope entry first.
    _run_cli([*loc.claude_cli, "mcp", "remove", "--scope", "user", SERVER_NAME])
    proc = _run_cli([*loc.claude_cli, *argv[1:]])
    if proc.returncode != 0:
        raise InstallError(
            f"'claude mcp add' failed (exit {proc.returncode}): {_first_line(proc)}\nRun it yourself:\n{display}"
        )
    return f"Registered '{SERVER_NAME}' with Claude Code (user scope). Check with: claude mcp list"


#: Characters cmd.exe re-parses even inside quotes when it runs a ``.cmd``/``.bat`` file (an npm
#: install of Claude Code puts ``claude.cmd`` on PATH); Python cannot escape them reliably.
_BATCH_UNSAFE = re.compile(r'[%!^&|<>"\r\n]')


def _batch_safe(cli: Sequence[str], args: Sequence[str]) -> bool:
    """False if ``cli`` is a Windows batch file and an argument could be mangled by cmd.exe."""
    if not cli or not str(cli[0]).lower().endswith((".cmd", ".bat")):
        return True
    return not any(_BATCH_UNSAFE.search(a) for a in args)


def _run_cli(argv: Sequence[str], timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(f"Could not run the Claude Code CLI: {exc}") from None


def _first_line(proc: subprocess.CompletedProcess[str]) -> str:
    text = (proc.stderr or proc.stdout or "").strip()
    return text.splitlines()[0][:300] if text else "no output"


# --------------------------------------------------------------------------- file helpers


def _report(path: Path, changed: bool, backup: Path | None, dry_run: bool, verb: str) -> str:
    if not changed:
        return f"Already up to date: {path}"
    if dry_run:
        return f"Would update {path}"
    suffix = f" (backup: {backup.name})" if backup else ""
    return f"{verb} {path}{suffix}"


def _backup(path: Path) -> Path:
    """Copy ``path`` to ``<name>.bak-<YYYYmmdd-HHMMSS>[-n]`` next to it (metadata preserved)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    shutil.copy2(path, candidate)
    return candidate


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via temp file + fsync + replace, keeping the original file's permission bits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        if path.exists() and os.name != "nt":
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        last_exc: OSError | None = None
        for _ in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:  # antivirus / the client briefly holding the file
                last_exc = exc
                time.sleep(0.05)
        assert last_exc is not None
        raise InstallError(f"Cannot write {path}: {last_exc.strerror or last_exc}") from None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _portable_path(path: str, platform: str) -> str:
    """Forward slashes on Windows: survives shell-style splitting in other tools' flags."""
    return path.replace("\\", "/") if platform == "win32" else path
