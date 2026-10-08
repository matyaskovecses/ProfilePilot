#!/usr/bin/env python3
"""Build ``dist/profilepilot.mcpb``, the Claude Desktop extension bundle, without Node.js.

An MCPB bundle is a zip archive with ``manifest.json`` at its root. ProfilePilot uses the
``manifest_version "0.4"`` *uv* server type: the bundle carries the Python sources plus
``pyproject.toml`` and Claude Desktop's bundled ``uv`` installs the dependencies on first start,
so the bundle stays small (no Playwright/pydantic wheels inside).

Bundle layout::

    manifest.json        <- mcpb/manifest.json
    pyproject.toml
    README.md
    LICENSE
    src/profilepilot/...

Files matching ``.mcpbignore`` (gitignore-style: ``#`` comments, ``*``/``**``/``?`` globs, a
trailing ``/`` for directories, a leading ``/`` or an inner ``/`` to anchor at the project root,
``!`` to re-include) or the built-in defaults (those of ``mcpb pack`` plus Python caches) are
left out.

Usage::

    python scripts/build_mcpb.py                 # validate + build dist/profilepilot.mcpb
    python scripts/build_mcpb.py --check         # validate only
    python scripts/build_mcpb.py --out other.mcpb --root path/to/checkout

Only the standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MANIFEST_SOURCE = Path("mcpb") / "manifest.json"
DEFAULT_OUT = Path("dist") / "profilepilot.mcpb"
IGNORE_FILE = ".mcpbignore"
#: Top-level project entries copied into the bundle (directories recursively).
INCLUDE: tuple[str, ...] = ("pyproject.toml", "README.md", "LICENSE", "src")

#: ``mcpb pack``'s default exclusions plus Python build/caches debris.
DEFAULT_IGNORES: tuple[str, ...] = (
    ".DS_Store",
    "Thumbs.db",
    ".gitignore",
    ".git/",
    "*.log",
    "npm-debug.log*",
    "yarn-debug.log*",
    "yarn-error.log*",
    ".npm/",
    ".npmrc",
    ".yarnrc",
    ".yarn/",
    ".pnp.*",
    "node_modules/.cache/",
    "node_modules/.bin/",
    "*.map",
    ".env.local",
    ".env.*.local",
    "package-lock.json",
    "yarn.lock",
    "__pycache__/",
    "*.py[cod]",
    "*.egg-info/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
)

# --------------------------------------------------------------------------- manifest schema
# Mirrors schemas/mcpb-manifest-v0.4.schema.json (github.com/modelcontextprotocol/mcpb):
# additionalProperties is false at every level checked here.

_TOP_KEYS = {
    "$schema", "dxt_version", "manifest_version", "name", "display_name", "version", "description",
    "long_description", "author", "repository", "homepage", "documentation", "support", "icon",
    "icons", "screenshots", "server", "tools", "tools_generated", "prompts", "prompts_generated",
    "keywords", "license", "privacy_policies", "compatibility", "user_config", "localization", "_meta",
}  # fmt: skip
_REQUIRED = ("name", "version", "description", "author", "server")
_SERVER_TYPES = {"python", "node", "binary", "uv"}
_PLATFORMS = {"darwin", "win32", "linux"}
_USER_CONFIG_TYPES = {"string", "number", "boolean", "directory", "file"}
_USER_CONFIG_KEYS = {"type", "title", "description", "required", "default", "multiple", "sensitive", "min", "max"}
_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:\S+$")


class BuildError(Exception):
    """The manifest is invalid or the bundle could not be assembled."""


def validate_manifest(manifest: Any) -> list[str]:
    """Return a list of problems with ``manifest`` (empty when it is valid for v0.4)."""
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]
    problems: list[str] = []
    if manifest.get("manifest_version", manifest.get("dxt_version")) != "0.4":
        problems.append('manifest_version must be "0.4" (needed for the "uv" server type)')
    for key in sorted(set(manifest) - _TOP_KEYS):
        problems.append(f"unknown top-level field {key!r}")
    for key in _REQUIRED:
        if key not in manifest:
            problems.append(f"missing required field {key!r}")
    for key in ("name", "version", "description", "display_name", "long_description", "license"):
        if key in manifest and not (isinstance(manifest[key], str) and manifest[key].strip()):
            problems.append(f"{key} must be a non-empty string")
    for key in ("homepage", "documentation", "support"):
        if key in manifest and not (isinstance(manifest[key], str) and _URI.match(manifest[key])):
            problems.append(f"{key} must be a URI")

    author = manifest.get("author")
    if "author" in manifest:
        if not isinstance(author, dict) or not isinstance(author.get("name"), str) or not author["name"]:
            problems.append("author.name is required")
        elif set(author) - {"name", "email", "url"}:
            problems.append(f"unknown author fields: {sorted(set(author) - {'name', 'email', 'url'})}")

    repo = manifest.get("repository")
    if repo is not None and (
        not isinstance(repo, dict) or set(repo) != {"type", "url"} or not _URI.match(str(repo.get("url", "")))
    ):
        problems.append("repository must be {type, url}")

    problems += _validate_server(manifest.get("server"))
    problems += _validate_compatibility(manifest.get("compatibility"))
    problems += _validate_user_config(manifest.get("user_config"))

    tools = manifest.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            problems.append("tools must be a list")
        else:
            for i, tool in enumerate(tools):
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    problems.append(f"tools[{i}] needs a name")
                elif set(tool) - {"name", "description"}:
                    problems.append(f"tools[{i}] has unknown fields {sorted(set(tool) - {'name', 'description'})}")
    for key in ("keywords", "screenshots"):
        value = manifest.get(key)
        if value is not None and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            problems.append(f"{key} must be a list of strings")
    for key in ("tools_generated", "prompts_generated"):
        if key in manifest and not isinstance(manifest[key], bool):
            problems.append(f"{key} must be a boolean")
    return problems


def _validate_server(server: Any) -> list[str]:
    if server is None:
        return []
    if not isinstance(server, dict):
        return ["server must be an object"]
    problems = [f"unknown server field {k!r}" for k in sorted(set(server) - {"type", "entry_point", "mcp_config"})]
    if server.get("type") not in _SERVER_TYPES:
        problems.append(f"server.type must be one of {sorted(_SERVER_TYPES)}")
    if not isinstance(server.get("entry_point"), str) or not server.get("entry_point"):
        problems.append("server.entry_point is required")
    cfg = server.get("mcp_config")
    if not isinstance(cfg, dict) or not isinstance(cfg.get("command"), str) or not cfg.get("command"):
        problems.append("server.mcp_config.command is required")
        return problems
    problems += [f"unknown mcp_config field {k!r}" for k in sorted(set(cfg) - {"command", "args", "env", "platform_overrides"})]
    problems += _validate_command(cfg, "server.mcp_config")
    overrides = cfg.get("platform_overrides", {})
    if not isinstance(overrides, dict):
        problems.append("platform_overrides must be an object")
    else:
        for plat, override in overrides.items():
            if not isinstance(override, dict) or set(override) - {"command", "args", "env"}:
                problems.append(f"platform_overrides.{plat} may only set command, args, env")
            else:
                problems += _validate_command(override, f"platform_overrides.{plat}")
    return problems


def _validate_command(cfg: dict[str, Any], where: str) -> list[str]:
    problems = []
    if "command" in cfg and not isinstance(cfg["command"], str):
        problems.append(f"{where}.command must be a string")
    args = cfg.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        problems.append(f"{where}.args must be a list of strings")
    env = cfg.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        problems.append(f"{where}.env must map names to strings")
    return problems


def _validate_compatibility(compat: Any) -> list[str]:
    if compat is None:
        return []
    if not isinstance(compat, dict):
        return ["compatibility must be an object"]
    problems = [f"unknown compatibility field {k!r}" for k in sorted(set(compat) - {"claude_desktop", "platforms", "runtimes"})]
    platforms = compat.get("platforms", [])
    if not isinstance(platforms, list) or not set(platforms) <= _PLATFORMS:
        problems.append(f"compatibility.platforms must be a subset of {sorted(_PLATFORMS)}")
    runtimes = compat.get("runtimes", {})
    if not isinstance(runtimes, dict) or set(runtimes) - {"python", "node"}:
        problems.append("compatibility.runtimes may only set python and node")
    return problems


def _validate_user_config(user_config: Any) -> list[str]:
    if user_config is None:
        return []
    if not isinstance(user_config, dict):
        return ["user_config must be an object"]
    problems = []
    for key, item in user_config.items():
        if not isinstance(item, dict):
            problems.append(f"user_config.{key} must be an object")
            continue
        for req in ("type", "title", "description"):
            if req not in item:
                problems.append(f"user_config.{key}.{req} is required")
        if item.get("type") not in _USER_CONFIG_TYPES:
            problems.append(f"user_config.{key}.type must be one of {sorted(_USER_CONFIG_TYPES)}")
        for extra in sorted(set(item) - _USER_CONFIG_KEYS):
            problems.append(f"user_config.{key} has unknown field {extra!r}")
    return problems


def load_manifest(path: Path) -> dict[str, Any]:
    """Read and validate a manifest; raises :class:`BuildError` listing every problem."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise BuildError(f"manifest not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise BuildError(f"{path} is not valid JSON: {exc}") from None
    problems = validate_manifest(manifest)
    if problems:
        raise BuildError(f"{path} is invalid:\n  - " + "\n  - ".join(problems))
    return manifest


def project_version(pyproject: Path) -> str | None:
    """``[project].version`` from ``pyproject.toml`` (None if absent or dynamic)."""
    text = pyproject.read_text(encoding="utf-8-sig")
    try:
        import tomllib

        return tomllib.loads(text).get("project", {}).get("version")
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        section = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
        m = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', section.group(1), re.MULTILINE) if section else None
        return m.group(1) if m else None


# --------------------------------------------------------------------------- ignore rules


class IgnoreRules:
    """Gitignore-style matcher for ``.mcpbignore`` patterns (last matching pattern wins)."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self._rules: list[tuple[re.Pattern[str], bool, bool]] = []  # (regex, dir_only, negated)
        for raw in patterns:
            line = raw.rstrip("\r\n").rstrip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            if not line:
                continue
            anchored = "/" in line
            line = line.lstrip("/")
            body = _glob_to_regex(line)
            regex = re.compile(("^" if anchored else "^(?:.*/)?") + body + "$")
            self._rules.append((regex, dir_only, negated))

    @classmethod
    def from_file(cls, path: Path, defaults: Iterable[str] = DEFAULT_IGNORES) -> IgnoreRules:
        lines = list(defaults)
        if path.is_file():
            lines += path.read_text(encoding="utf-8-sig").splitlines()
        return cls(lines)

    def ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        """Whether the project-relative POSIX path ``rel_path`` is excluded."""
        result = False
        for regex, dir_only, negated in self._rules:
            if dir_only and not is_dir:
                continue
            if regex.match(rel_path):
                result = not negated
        return result


def _glob_to_regex(pattern: str) -> str:
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------- build


def collect_files(root: Path, rules: IgnoreRules, include: Iterable[str] = INCLUDE) -> list[tuple[Path, str]]:
    """(absolute path, archive name) for every bundled file, sorted by archive name."""
    files: list[tuple[Path, str]] = []
    for name in include:
        top = root / name
        if top.is_file():
            if not rules.ignored(name):
                files.append((top, name))
            continue
        if not top.is_dir():
            raise BuildError(f"required project file is missing: {top}")
        if rules.ignored(name, is_dir=True):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            base = Path(dirpath)
            rel_dir = base.relative_to(root).as_posix()
            dirnames[:] = sorted(
                d for d in dirnames
                if not (base / d).is_symlink() and not rules.ignored(f"{rel_dir}/{d}", is_dir=True)
            )  # fmt: skip
            for fname in filenames:
                path = base / fname
                rel = f"{rel_dir}/{fname}"
                if path.is_symlink() or not path.is_file() or rules.ignored(rel):
                    continue
                files.append((path, rel))
    return sorted(files, key=lambda item: item[1])


def _zip_time(path: Path) -> tuple[int, int, int, int, int, int]:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    stamp = int(epoch) if epoch and epoch.isdigit() else int(path.stat().st_mtime)
    return time.gmtime(max(stamp, 315532800))[:6]  # zip cannot store dates before 1980


def _add(zf: zipfile.ZipFile, arcname: str, data: bytes, date_time: tuple[int, int, int, int, int, int]) -> None:
    info = zipfile.ZipInfo(arcname, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100644 & 0xFFFF) << 16
    zf.writestr(info, data, compresslevel=9)


def build(root: Path, out: Path | None = None, *, manifest_path: Path | None = None) -> Path:
    """Validate the manifest and write the bundle. Returns the path of the ``.mcpb`` file."""
    root = root.resolve()
    manifest_file = manifest_path or root / MANIFEST_SOURCE
    manifest = load_manifest(manifest_file)

    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise BuildError(f"pyproject.toml not found in {root} (required by the uv server type)")
    version = project_version(pyproject)
    if version is not None and version != manifest["version"]:
        raise BuildError(
            f"version mismatch: {manifest_file.name} says {manifest['version']!r} but pyproject.toml "
            f"says {version!r}; bump them together"
        )

    rules = IgnoreRules.from_file(root / IGNORE_FILE)
    files = collect_files(root, rules)
    names = {arc for _, arc in files}
    entry = manifest["server"]["entry_point"]
    if entry not in names:
        raise BuildError(f"server.entry_point {entry!r} is not in the bundle (missing or ignored)")
    if "manifest.json" in names:
        raise BuildError("a project file would shadow the bundle's manifest.json")

    out = (out or root / DEFAULT_OUT).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(tmp, "w") as zf:
            _add(zf, "manifest.json", manifest_file.read_bytes(), _zip_time(manifest_file))
            for path, arcname in files:
                _add(zf, arcname, path.read_bytes(), _zip_time(path))
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the ProfilePilot Claude Desktop extension (.mcpb).")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="project checkout")
    parser.add_argument("--out", type=Path, default=None, help=f"output file (default: <root>/{DEFAULT_OUT.as_posix()})")
    parser.add_argument("--check", action="store_true", help="validate the manifest only")
    args = parser.parse_args(argv)
    try:
        if args.check:
            manifest = load_manifest(args.root / MANIFEST_SOURCE)
            print(f"{MANIFEST_SOURCE.as_posix()} is valid ({manifest['name']} {manifest['version']})")
            return 0
        out = build(args.root, args.out)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(out) as zf:
        count = len(zf.namelist())
    print(f"wrote {out} ({count} files, {out.stat().st_size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
