"""Tests for client registration (install.py), the plugin/MCPB packaging files and the bundle
build script. Every path is redirected into tmp_path: real client configs are never touched."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import textwrap
try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

import profilepilot
from profilepilot import install
from profilepilot.install import InstallError, Locations

ROOT = Path(__file__).resolve().parent.parent
# A native interpreter path for this OS (spaces on purpose; backslashes on Windows). The installer
# resolves paths in the running OS's own form, so a Windows path is not "absolute" on Linux/macOS.
WIN_PY = r"C:\Users\Test User\pp\.venv\Scripts\python.exe"
PY = WIN_PY if sys.platform == "win32" else "/opt/Test User/pp/.venv/bin/python"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Point every location variable at tmp_path so even default detection stays sandboxed."""
    home = tmp_path / "home"
    env = {
        "USERPROFILE": home,
        "HOME": home,
        "APPDATA": home / "AppData" / "Roaming",
        "LOCALAPPDATA": home / "AppData" / "Local",
        "CODEX_HOME": home / ".codex",
        "XDG_CONFIG_HOME": home / ".config",
        "PATH": tmp_path / "empty-bin",  # no real `claude` CLI is ever found
    }
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    for key in install.PASSTHROUGH_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def loc(tmp_path) -> Locations:
    found = Locations.detect(platform="win32")
    assert str(found.home).startswith(str(tmp_path))  # sanity: never the real profile
    return found


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".bak-*"))


# --------------------------------------------------------------------------- locations


def test_detect_locations_from_environ(tmp_path):
    env = {
        "USERPROFILE": str(tmp_path / "u"),
        "APPDATA": str(tmp_path / "roam"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "PATH": str(tmp_path),
    }
    win = Locations.detect(env, platform="win32")
    assert win.claude_desktop_configs() == [tmp_path / "roam" / "Claude" / "claude_desktop_config.json"]
    assert win.codex_config() == tmp_path / "u" / ".codex" / "config.toml"
    assert win.cursor_config() == tmp_path / "u" / ".cursor" / "mcp.json"
    assert win.claude_cli is None

    msix = tmp_path / "local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    msix.mkdir(parents=True)
    (tmp_path / "local" / "Packages" / "Claude_nocache").mkdir()  # no LocalCache: ignored
    assert win.claude_desktop_configs()[1:] == [msix / "claude_desktop_config.json"]

    mac = Locations.detect({"HOME": str(tmp_path / "m")}, platform="darwin")
    assert mac.claude_desktop_configs() == [
        tmp_path / "m" / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    ]
    linux = Locations.detect({"HOME": str(tmp_path / "l"), "CODEX_HOME": str(tmp_path / "cx")}, platform="linux")
    assert linux.claude_desktop_configs() == [tmp_path / "l" / ".config" / "Claude" / "claude_desktop_config.json"]
    assert linux.codex_config() == tmp_path / "cx" / "config.toml"


def test_server_spec_passthrough_env(monkeypatch):
    monkeypatch.setenv("PROFILEPILOT_HOME", r"D:\pp-data")
    monkeypatch.setenv("PROFILEPILOT_SECRETS", "file")  # test-only setting must not leak into configs
    spec = install.server_spec(PY)
    assert spec.env == {"PROFILEPILOT_HOME": r"D:\pp-data"}
    assert spec.argv == [os.path.abspath(PY), "-m", "profilepilot", "serve"]
    assert install.server_spec(PY, env={}).env == {}
    assert install.server_spec().command == os.path.abspath(sys.executable)


# --------------------------------------------------------------------------- JSON clients


def test_claude_desktop_fresh_install_creates_config(loc):
    report = install.register("claude-desktop", python=PY, locations=loc)
    cfg = loc.claude_desktop_configs()[0]
    assert str(cfg) in report and "Quit Claude Desktop" in report
    assert _read(cfg) == {"mcpServers": {"profilepilot": {"command": PY, "args": ["-m", "profilepilot", "serve"], "env": {}}}}
    assert _backups(cfg) == []  # nothing existed, nothing to back up
    assert not cfg.read_bytes().startswith(b"\xef\xbb\xbf")


def test_claude_desktop_merge_preserves_keys_backs_up_and_is_idempotent(loc):
    cfg = loc.claude_desktop_configs()[0]
    cfg.parent.mkdir(parents=True)
    original = {
        "mcpServers": {"other": {"command": "npx", "args": ["-y", "x"], "env": {"TOKEN": "keep-me"}}},
        "preferences": {"menuBarEnabled": False},
        "isUsingBuiltInNodeForMcp": True,
    }
    raw = ("\ufeff" + json.dumps(original, indent=4)).encode("utf-8")  # PowerShell-style BOM
    cfg.write_bytes(raw)

    report = install.register("claude-desktop", python=PY, locations=loc)
    (backup,) = _backups(cfg)
    assert backup.read_bytes() == raw and backup.name in report
    data = _read(cfg)
    assert list(data) == ["mcpServers", "preferences", "isUsingBuiltInNodeForMcp"]
    assert data["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert data["mcpServers"]["profilepilot"]["command"] == PY
    text = cfg.read_text(encoding="utf-8")
    assert not text.startswith("\ufeff") and '\n    "mcpServers"' in text  # BOM dropped, 4-space indent kept

    before = cfg.read_bytes()
    again = install.register("claude-desktop", python=PY, locations=loc)
    assert "Already up to date" in again
    assert cfg.read_bytes() == before and len(_backups(cfg)) == 1


def test_claude_desktop_writes_msix_copy_too(loc):
    msix_dir = loc.localappdata / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    msix_dir.mkdir(parents=True)
    report = install.register("claude-desktop", python=PY, locations=loc)
    for cfg in (loc.claude_desktop_configs()[0], msix_dir / "claude_desktop_config.json"):
        assert str(cfg) in report
        assert _read(cfg)["mcpServers"]["profilepilot"]["args"] == ["-m", "profilepilot", "serve"]


def test_utf16_config_is_read_and_rewritten_as_utf8(loc):
    cfg = loc.cursor_config()
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(json.dumps({"mcpServers": {"a": {"command": "a"}}}).encode("utf-16"))
    install.register("cursor", python=PY, locations=loc)
    data = json.loads(cfg.read_bytes().decode("utf-8"))
    assert set(data["mcpServers"]) == {"a", "profilepilot"}
    assert len(_backups(cfg)) == 1


def test_existing_entry_keeps_user_additions(loc):
    cfg = loc.cursor_config()
    cfg.parent.mkdir(parents=True)
    old = {"command": "old-python", "args": ["x"], "env": {"FOO": "1"}, "type": "stdio"}
    cfg.write_text(json.dumps({"mcpServers": {"profilepilot": old}}), encoding="utf-8")
    install.register("cursor", python=PY, env={"PROFILEPILOT_HOME": "D:/pp"}, locations=loc)
    entry = _read(cfg)["mcpServers"]["profilepilot"]
    assert entry == {
        "command": PY,
        "args": ["-m", "profilepilot", "serve"],
        "env": {"FOO": "1", "PROFILEPILOT_HOME": "D:/pp"},
        "type": "stdio",
    }


@pytest.mark.parametrize("content", ['{"mcpServers": {', "[1, 2]", '{"mcpServers": []}'])
def test_unparseable_json_is_never_overwritten(loc, content):
    cfg = loc.cursor_config()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(content, encoding="utf-8")
    with pytest.raises(InstallError) as err:
        install.register("cursor", python=PY, locations=loc)
    assert "left unchanged" in str(err.value) or "fix it" in str(err.value)
    assert cfg.read_text(encoding="utf-8") == content
    assert not list(cfg.parent.glob("*.bad")) and _backups(cfg) == []


def test_unregister_json(loc):
    cfg = loc.claude_desktop_configs()[0]
    assert "does not exist" in install.unregister("claude-desktop", locations=loc)
    install.register("claude-desktop", python=PY, locations=loc)
    data = _read(cfg)
    data["mcpServers"]["other"] = {"command": "x"}
    cfg.write_text(json.dumps(data), encoding="utf-8")

    report = install.unregister("claude-desktop", locations=loc)
    assert "Removed" in report
    assert _read(cfg) == {"mcpServers": {"other": {"command": "x"}}}
    assert len(_backups(cfg)) == 1
    assert "Nothing to remove" in install.unregister("claude-desktop", locations=loc)
    assert install.is_registered("claude-desktop", loc) is False


def test_dry_run_writes_nothing(loc, tmp_path):
    def snapshot() -> set[Path]:
        return {p for p in tmp_path.rglob("*") if p.is_file()}

    before = snapshot()
    for client in ("claude-desktop", "cursor", "codex"):
        report = install.register(client, python=PY, dry_run=True, locations=loc)
        assert "Would update" in report
    assert "Would run" in install.register(
        "claude-code", python=PY, dry_run=True, locations=replace(loc, claude_cli=("claude",))
    )
    assert snapshot() == before


def test_is_registered_and_config_paths(loc):
    assert install.is_registered("cursor", loc) is False
    assert install.is_registered("codex", loc) is False
    assert install.is_registered("claude-code", loc) is None  # no CLI: cannot tell
    install.register("cursor", python=PY, locations=loc)
    install.register("codex", python=PY, locations=loc)
    assert install.is_registered("cursor", loc) is True
    assert install.is_registered("codex", loc) is True
    assert install.config_paths("codex", loc) == [loc.codex_config()]
    assert install.config_paths("claude-code", loc) == []
    with pytest.raises(InstallError):
        install.register("vscode", locations=loc)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Codex TOML

CODEX_ORIGINAL = textwrap.dedent(
    '''\
    # Codex settings - this comment must survive
    model = "gpt-5"   # trailing comment
    approval_policy = 'on-request'
    notes = """
    [mcp_servers.profilepilot]
    command = 'not a real table - inside a multi-line string'
    """

    [profiles.fast]
    matrix = [
      ["looks", "like"],
      ["a", "header"],
    ]

    [mcp_servers.other]
    command = "npx"
    args = ["-y", "other-mcp"]

    [mcp_servers.other.env]
    TOKEN = "keep-me"
    '''
)


def test_codex_append_preserves_every_byte(loc):
    cfg = loc.codex_config()
    cfg.parent.mkdir(parents=True)
    cfg.write_text(CODEX_ORIGINAL, encoding="utf-8", newline="\n")

    report = install.register("codex", python=PY, locations=loc)
    new = cfg.read_text(encoding="utf-8")
    assert new.startswith(CODEX_ORIGINAL + "\n[mcp_servers.profilepilot]\n")
    # a literal string for Windows paths (backslashes need no escaping), a basic string otherwise
    assert f"command = {install._toml_str(PY)}" in new
    doc = tomllib.loads(new)
    entry = doc["mcp_servers"]["profilepilot"]
    assert entry == {
        "command": PY,
        "args": ["-m", "profilepilot", "serve"],
        "startup_timeout_sec": 60,
        "tool_timeout_sec": 180,
    }
    assert doc["mcp_servers"]["other"]["env"] == {"TOKEN": "keep-me"}
    assert "[mcp_servers.profilepilot]" in doc["notes"]  # the string look-alike was untouched
    assert (_backups(cfg)[0]).read_text(encoding="utf-8") == CODEX_ORIGINAL and "backup" in report

    # idempotent: second run changes nothing and makes no new backup
    assert "Already up to date" in install.register("codex", python=PY, locations=loc)
    assert cfg.read_text(encoding="utf-8") == new and len(_backups(cfg)) == 1

    # unregister restores the original byte-for-byte
    assert "Removed" in install.unregister("codex", locations=loc)
    assert cfg.read_text(encoding="utf-8") == CODEX_ORIGINAL


def test_codex_replace_in_place_keeps_user_settings(loc):
    cfg = loc.codex_config()
    cfg.parent.mkdir(parents=True)
    head = 'model = "o3"\n\n'
    tail = '# the next server\n[mcp_servers.zeta]\ncommand = "z"\n'
    original = (
        head
        + "[mcp_servers.profilepilot]\n"
        + 'command = "old-python"\n'
        + 'args = ["-m", "profilepilot", "serve"]\n'
        + "startup_timeout_sec = 120\n"
        + "tool_timeout_sec = 30\n"
        + "enabled = true  # keep\n"
        + "\n"
        + "[mcp_servers.profilepilot.env]\n"
        + "PROFILEPILOT_HOME = 'D:\\pp'\n"
        + "\n"
        + tail
    )
    cfg.write_text(original, encoding="utf-8", newline="\n")

    install.register("codex", python=PY, locations=loc)
    new = cfg.read_text(encoding="utf-8")
    expected_block = (
        "[mcp_servers.profilepilot]\n"
        f"command = {install._toml_str(PY)}\n"
        'args = ["-m", "profilepilot", "serve"]\n'
        "startup_timeout_sec = 120\n"  # user's larger value kept
        "tool_timeout_sec = 180\n"  # raised to our minimum
        "env = { PROFILEPILOT_HOME = 'D:\\pp' }\n"
        "enabled = true\n"
    )
    assert new == head + expected_block + "\n" + tail
    assert tomllib.loads(new)["mcp_servers"]["zeta"] == {"command": "z"}

    install.unregister("codex", locations=loc)
    assert cfg.read_text(encoding="utf-8") == head + tail


def test_codex_crlf_bom_and_quote_in_path(loc):
    cfg = loc.codex_config()
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(b"\xef\xbb\xbfmodel = \"x\"\r\n")
    weird = r"C:\Users\O'Brien\py\python.exe" if sys.platform == "win32" else "/Users/O'Brien/py/python"
    install.register("codex", python=weird, locations=loc)
    raw = cfg.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbfmodel = \"x\"\r\n\r\n[mcp_servers.profilepilot]\r\n")
    assert b"\n" not in raw.replace(b"\r\n", b"")  # every line ending stays CRLF
    entry = tomllib.loads(raw.decode("utf-8-sig"))["mcp_servers"]["profilepilot"]
    assert entry["command"] == weird  # basic string with escaped backslashes round-trips


def test_codex_refuses_invalid_or_inline_definitions(loc):
    cfg = loc.codex_config()
    cfg.parent.mkdir(parents=True)
    for content in ("model = \n", 'mcp_servers = { profilepilot = { command = "x" } }\n',
                    '[mcp_servers]\nprofilepilot.command = "x"\n'):  # fmt: skip
        cfg.write_text(content, encoding="utf-8")
        with pytest.raises(InstallError):
            install.register("codex", python=PY, locations=loc)
        assert cfg.read_text(encoding="utf-8") == content
    assert _backups(cfg) == []


def test_codex_value_rendering():
    spec = install.ServerSpec(command="/usr/bin/python3", env={"A B": 'q"uote', "PLAIN": "C:\\x"})
    block = install.render_codex_block(
        spec, {"enabled_tools": ["a", "b"], "nested": {"k": 1.5, "on": False}, "env": {"OLD": "1"}}
    )
    doc = tomllib.loads(block)["mcp_servers"]["profilepilot"]
    assert doc["command"] == "/usr/bin/python3"
    assert doc["env"] == {"OLD": "1", "A B": 'q"uote', "PLAIN": "C:\\x"}
    assert doc["enabled_tools"] == ["a", "b"] and doc["nested"] == {"k": 1.5, "on": False}


# --------------------------------------------------------------------------- Claude Code CLI


def test_claude_code_without_cli_returns_command(loc):
    report = install.register("claude-code", python=PY, locations=loc)
    assert "not on PATH" in report
    assert f'claude mcp add --scope user profilepilot -- "{PY}" -m profilepilot serve' in report
    assert "claude mcp remove --scope user profilepilot" in install.unregister("claude-code", locations=loc)


def test_claude_code_with_cli(loc, tmp_path):
    calls = tmp_path / "calls.jsonl"
    fake = tmp_path / "fake_claude.py"
    fake.write_text(
        textwrap.dedent(
            f"""\
            import json, sys
            with open({str(calls)!r}, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(sys.argv[1:]) + "\\n")
            if sys.argv[1:3] == ["mcp", "remove"]:
                print("No MCP server named profilepilot", file=sys.stderr)
                sys.exit(1)
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    cli_loc = replace(loc, claude_cli=(sys.executable, str(fake)))
    report = install.register("claude-code", python=PY, env={"PROFILEPILOT_HOME": "D:/pp"}, locations=cli_loc)
    assert "Registered" in report
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert recorded == [
        ["mcp", "remove", "--scope", "user", "profilepilot"],
        ["mcp", "add", "--env", "PROFILEPILOT_HOME=D:/pp", "--scope", "user", "profilepilot", "--",
         os.path.abspath(PY), "-m", "profilepilot", "serve"],
    ]  # fmt: skip
    assert "no user-scope" in install.unregister("claude-code", locations=cli_loc)


def test_claude_code_batch_cli_is_not_run_with_unsafe_args(loc, tmp_path):
    marker = tmp_path / "ran.txt"
    cmd = tmp_path / "claude.cmd"
    cmd.write_text(f'@echo off\r\necho ran> "{marker}"\r\n', encoding="ascii")
    cli_loc = replace(loc, claude_cli=(str(cmd),))
    report = install.register("claude-code", python=PY, env={"PROFILEPILOT_HOME": "D:/100%"}, locations=cli_loc)
    assert "batch file" in report and "claude mcp add" in report
    assert not marker.exists()
    assert install._batch_safe((str(cmd),), ["mcp", "add", "--", PY, "-m", "profilepilot", "serve"])
    assert install._batch_safe(("C:/bin/claude.exe",), ["100%", "a&b"])


def test_cross_platform_spec_is_not_resolved_against_this_machine():
    assert install.server_spec("/opt/x/../pp/bin/python", env={}, platform="linux").command == "/opt/pp/bin/python"
    assert install.server_spec(r"C:\a\..\pp\python.exe", env={}, platform="win32").command == r"C:\pp\python.exe"


def test_claude_code_cli_failure_raises(loc, tmp_path):
    fake = tmp_path / "failing_claude.py"
    fake.write_text("import sys\nprint('boom', file=sys.stderr)\nsys.exit(2)\n", encoding="utf-8")
    with pytest.raises(InstallError, match="boom"):
        install.register("claude-code", python=PY, locations=replace(loc, claude_cli=(sys.executable, str(fake))))


# --------------------------------------------------------------------------- snippets


def test_snippets_for_every_client():
    PY = WIN_PY  # this test renders the *Windows* snippets, whatever OS runs it
    snips = install.snippets(PY, env={}, platform="win32")
    assert set(snips) >= {"claude-desktop", "claude-code", "claude-code-plugin", "codex", "cursor", "chatgpt"}
    assert json.loads(snips["claude-desktop"])["mcpServers"]["profilepilot"]["command"] == PY
    assert json.loads(snips["cursor"]) == json.loads(snips["claude-desktop"])
    assert tomllib.loads(snips["codex"])["mcp_servers"]["profilepilot"]["tool_timeout_sec"] == 180
    assert snips["claude-code"] == f'claude mcp add --scope user profilepilot -- "{PY}" -m profilepilot serve'
    assert "/plugin install profilepilot@profilepilot" in snips["claude-code-plugin"]
    chatgpt = snips["chatgpt"]
    assert "tunnel-client init" in chatgpt and "Add custom MCP server" in chatgpt
    # PY does not exist, so there is no 8.3 short name: the command is kept and a warning added
    assert "--mcp-command 'C:/Users/Test User/pp/.venv/Scripts/python.exe -m profilepilot serve'" in chatgpt
    assert "contains spaces" in chatgpt
    assert "8931" in chatgpt and "8765" not in chatgpt
    assert "$env:" not in chatgpt  # no env to pass through
    posix = install.snippets("/opt/pp/bin/python", env={"PROFILEPILOT_HOME": "/data/it's"}, platform="linux")
    assert posix["claude-code"] == (
        "claude mcp add --env 'PROFILEPILOT_HOME=/data/it'\"'\"'s' --scope user profilepilot -- "
        "/opt/pp/bin/python -m profilepilot serve"
    )
    assert "--mcp-command '/opt/pp/bin/python -m profilepilot serve'" in posix["chatgpt"]
    assert "export PROFILEPILOT_HOME='/data/it'\"'\"'s'" in posix["chatgpt"]
    assert "contains spaces" not in posix["chatgpt"]
    win_env = install.snippets(PY, env={"PROFILEPILOT_HOME": r"D:\O'Neil"}, platform="win32")["chatgpt"]
    assert r"$env:PROFILEPILOT_HOME = 'D:\O''Neil'" in win_env


def test_tunnel_command_avoids_spaces(tmp_path):
    exe = tmp_path / "dir with space" / "python.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    command, warning = install.tunnel_command(install.ServerSpec(command=str(exe)), "win32")
    assert command.endswith(" -m profilepilot serve") and "\\" not in command
    exe_part = command[: -len(" -m profilepilot serve")]
    if warning is None:  # the volume has 8.3 names: a space-free alias of the same file
        assert " " not in exe_part and os.path.samefile(exe_part, exe)
    else:
        assert exe_part == exe.as_posix() and "spaces" in warning
    plain = install.tunnel_command(install.ServerSpec(command=r"C:\py\python.exe"), "win32")
    assert plain == ("C:/py/python.exe -m profilepilot serve", None)


# --------------------------------------------------------------------------- packaging files


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_versions_are_in_sync():
    version = _pyproject_version()
    assert profilepilot.__version__ == version
    assert json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"] == version
    assert json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))["version"] == version


def test_claude_plugin_and_marketplace():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", plugin["name"]) and not plugin["name"].startswith("claude")
    server = plugin["mcpServers"]["profilepilot"]
    assert server["command"] == "uvx"
    assert server["args"] == ["--from", "${CLAUDE_PLUGIN_ROOT}", "profilepilot", "serve"]
    assert "${user_config." not in json.dumps(plugin)  # no undeclared userConfig references
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"]["profilepilot"]  # what `uvx ... profilepilot` runs
    assert not (ROOT / "bin").exists()  # a top-level bin/ blocks claude.ai / Cowork installs

    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert market["name"] == "profilepilot" and market["owner"]["name"]
    (entry,) = market["plugins"]
    assert entry["name"] == plugin["name"] and entry["source"].startswith("./")
    assert "version" not in entry  # plugin.json is the single source of the version


def test_skill_frontmatter_and_tool_names():
    text = (ROOT / "skills" / "profilepilot" / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md needs YAML frontmatter"
    front = dict(line.split(": ", 1) for line in m.group(1).splitlines())
    assert front["name"] == "profilepilot" and 50 < len(front["description"]) <= 1024

    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    catalogue = design[design.index("## 5. MCP tool catalogue") : design.index("## 6.")]
    known = set(re.findall(r"`((?:profile|proxy|browser|cookies|shardx)_[a-z_]+|http_fetch)", catalogue))
    mentioned = set(re.findall(r"\b((?:profile|proxy|browser|cookies)_[a-z_]+|http_fetch)\b", text))
    assert mentioned - known == set(), "SKILL.md mentions tools that are not in the catalogue"
    for core in ("profile_list", "profile_create", "profile_start", "browser_navigate", "browser_snapshot",
                 "browser_click", "browser_type", "browser_read", "browser_extract", "http_fetch",
                 "cookies_get", "proxy_add", "proxy_test"):  # fmt: skip
        assert core in mentioned
    assert "CAPTCHA" in text and "robots.txt" in text


def test_license_is_mit():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License") and "Copyright (c) 2026 Matyas Kovecses" in text


# --------------------------------------------------------------------------- MCPB bundle


@pytest.fixture(scope="module")
def bm():
    spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "scripts" / "build_mcpb.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_mcpb"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("build_mcpb", None)


def test_repo_manifest_is_valid(bm):
    manifest = bm.load_manifest(ROOT / "mcpb" / "manifest.json")
    assert manifest["manifest_version"] == "0.4" and manifest["server"]["type"] == "uv"
    args = manifest["server"]["mcp_config"]["args"]
    assert args[:3] == ["run", "--directory", "${__dirname}"] and args[-3:] == ["-m", "profilepilot", "serve"]
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    for tool in manifest["tools"]:
        assert f"`{tool['name']}" in design


def test_manifest_validation_reports_problems(bm):
    good = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    assert bm.validate_manifest(good) == []
    broken = dict(good, manifest_version="0.3", surprise=1)
    del broken["author"]
    broken["server"] = {"type": "ruby", "mcp_config": {"args": "x"}}
    broken["user_config"] = {"dir": {"type": "folder", "title": "t"}}
    problems = "\n".join(bm.validate_manifest(broken))
    for fragment in ("manifest_version", "surprise", "'author'", "server.type", "entry_point",
                     "mcp_config.command", "user_config.dir.description", "user_config.dir.type"):  # fmt: skip
        assert fragment in problems


def test_ignore_rules(bm):
    rules = bm.IgnoreRules(["# comment", "", "__pycache__/", "*.py[cod]", "/build/", "src/**/*.tmp", "*.log", "!keep.log"])
    assert rules.ignored("src/pkg/__pycache__", is_dir=True)
    assert not rules.ignored("src/pkg/__pycache__")  # dir-only pattern does not match a file
    assert rules.ignored("src/pkg/mod.pyc") and not rules.ignored("src/pkg/mod.py")
    assert rules.ignored("build", is_dir=True) and not rules.ignored("src/build", is_dir=True)
    assert rules.ignored("src/a/b/c.tmp") and rules.ignored("src/c.tmp") and not rules.ignored("other/c.tmp")
    assert rules.ignored("x/debug.log") and not rules.ignored("x/keep.log")


def _fake_project(root: Path, version: str = "1.2.3") -> Path:
    (root / "mcpb").mkdir(parents=True)
    manifest = {
        "manifest_version": "0.4",
        "name": "demo",
        "version": version,
        "description": "demo",
        "author": {"name": "me"},
        "server": {"type": "uv", "entry_point": "src/demo/__main__.py",
                   "mcp_config": {"command": "uv", "args": ["run", "--directory", "${__dirname}", "demo"]}},
    }  # fmt: skip
    (root / "mcpb" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.2.3"\n', encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    pkg = root / "src" / "demo"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "__main__.py").write_text("print('hi')\n", encoding="utf-8")
    (pkg / "core.py").write_text("X = 1\n", encoding="utf-8")
    (pkg / "__pycache__" / "core.cpython-311.pyc").write_bytes(b"\0")
    (pkg / "debug.log").write_text("noise\n", encoding="utf-8")
    (pkg / "secret.local").write_text("nope\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("", encoding="utf-8")
    (root / ".mcpbignore").write_text("# extra\n*.local\n", encoding="utf-8")
    return root


def test_build_bundle(bm, tmp_path):
    root = _fake_project(tmp_path / "proj")
    out = bm.build(root, tmp_path / "out" / "demo.mcpb")
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert names[0] == "manifest.json"
        assert sorted(names) == sorted(["manifest.json", "LICENSE", "README.md", "pyproject.toml",
                                        "src/demo/__main__.py", "src/demo/core.py"])  # fmt: skip
        assert json.loads(zf.read("manifest.json"))["name"] == "demo"
        assert zf.getinfo("src/demo/core.py").compress_type == zipfile.ZIP_DEFLATED
    assert not list((tmp_path / "out").glob(".*.tmp"))
    assert bm.main(["--root", str(root), "--check"]) == 0


def test_build_rejects_bad_projects(bm, tmp_path):
    root = _fake_project(tmp_path / "missing-entry")
    (root / "src" / "demo" / "__main__.py").unlink()
    with pytest.raises(bm.BuildError, match="entry_point"):
        bm.build(root, tmp_path / "a.mcpb")

    root = _fake_project(tmp_path / "version-skew", version="9.9.9")
    with pytest.raises(bm.BuildError, match="version mismatch"):
        bm.build(root, tmp_path / "b.mcpb")

    root = _fake_project(tmp_path / "bad-manifest")
    (root / "mcpb" / "manifest.json").write_text('{"name": "x"}', encoding="utf-8")
    with pytest.raises(bm.BuildError, match="missing required field"):
        bm.build(root, tmp_path / "c.mcpb")
    assert bm.main(["--root", str(root), "--out", str(tmp_path / "c.mcpb")]) == 1
    assert not (tmp_path / "a.mcpb").exists() and not (tmp_path / "b.mcpb").exists()


def test_real_repo_bundle_contents(bm):
    files = bm.collect_files(ROOT, bm.IgnoreRules.from_file(ROOT / ".mcpbignore"))
    names = {arc for _, arc in files}
    assert {"pyproject.toml", "README.md", "LICENSE", "src/profilepilot/install.py"} <= names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert all(n in ("pyproject.toml", "README.md", "LICENSE") or n.startswith("src/") for n in names)


def test_build_real_repo_bundle(bm, tmp_path):
    """Build from a copy of the real project files (other agents may be editing the checkout)."""
    root = tmp_path / "checkout"
    root.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", ".mcpbignore"):
        shutil.copy2(ROOT / name, root / name)
    shutil.copytree(ROOT / "mcpb", root / "mcpb")
    shutil.copytree(ROOT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    main = root / "src" / "profilepilot" / "__main__.py"
    if not main.exists():  # the entry point belongs to the CLI owner; stand in for it
        main.write_text("from profilepilot.cli import main\nmain()\n", encoding="utf-8")
    (root / "src" / "profilepilot" / "__pycache__").mkdir(exist_ok=True)
    (root / "src" / "profilepilot" / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\0")
    out = bm.build(root, tmp_path / "profilepilot.mcpb")
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert {"manifest.json", "pyproject.toml", "README.md", "LICENSE", "src/profilepilot/install.py"} <= names
    assert not any("__pycache__" in n or n.startswith(("tests/", "docs/", ".venv/")) for n in names)


# --------------------------------------------------------------------------- client docs


def test_clients_doc_matches_the_installer():
    doc = (ROOT / "docs" / "CLIENTS.md").read_text(encoding="utf-8")
    for needle in ("mcp-server-*.log", "Secure MCP Tunnel", "tunnel-client init --sample sample_mcp_stdio_local",
                   "/plugin install profilepilot@profilepilot", "--auth secret-path", "uv",
                   "claude mcp add --scope user profilepilot -- ", "[mcp_servers.profilepilot]",
                   "startup_timeout_sec = 60", "tool_timeout_sec = 180", "MCP_TIMEOUT"):  # fmt: skip
        assert needle in doc, needle
    assert "8931" in doc and "8765" not in doc
    toml_block = re.search(r"```toml\n(.*?)```", doc, re.DOTALL).group(1)
    assert tomllib.loads(toml_block)["mcp_servers"]["profilepilot"]["args"] == ["-m", "profilepilot", "serve"]
    json_block = re.search(r"```json\n(.*?)```", doc, re.DOTALL).group(1)
    assert json.loads(json_block)["mcpServers"]["profilepilot"]["args"] == ["-m", "profilepilot", "serve"]
