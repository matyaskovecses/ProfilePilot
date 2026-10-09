# Connecting ProfilePilot to your AI clients

ProfilePilot is an MCP server. Every client starts it the same way:

```
<absolute path to python> -m profilepilot serve
```

That runs the server over **stdio**, which is what Claude Desktop, Claude Code, Codex, the ChatGPT
desktop app and Cursor use. ChatGPT on the web cannot start programs on your computer, so it needs
a remote connection instead (see [ChatGPT (web)](#chatgpt-web)).

| Client | Recommended route | Other routes |
|---|---|---|
| Claude Desktop | `profilepilot install claude-desktop` | Desktop extension (`.mcpb`) |
| Claude Code | `profilepilot install claude-code` | Plugin from this repo's marketplace (needs uv); shared HTTP server |
| Codex CLI / IDE extension / ChatGPT desktop app | `profilepilot install codex` | `codex mcp add` |
| ChatGPT (web) | OpenAI Secure MCP Tunnel | Public HTTPS URL (`serve --http --auth secret-path`) |
| Cursor | `profilepilot install cursor` | Edit `~/.cursor/mcp.json` |

All clients share the same profiles. Running browsers live in their own background processes, so
Claude Desktop, Claude Code, ChatGPT and your Python scripts can all use the same running profile
at the same time, and a profile keeps running when a client restarts.

## Before you start

1. Install ProfilePilot into a virtual environment (Python 3.10 or newer):

   ```powershell
   git clone https://github.com/matyaskovecses/ProfilePilot
   cd ProfilePilot
   python -m venv .venv
   .venv\Scripts\python -m pip install -e ".[scrapling]"
   ```

2. Make sure Google Chrome (or Edge, Brave or Chromium) is installed. `profilepilot doctor` checks
   the browser, the keyring and the data folder.

3. Register the server with your client, as described below. The `install` commands:
   - register the Python interpreter that runs them, so run them with the venv's `profilepilot`
     (or `.venv\Scripts\python -m profilepilot install ...`);
   - back up every file before they change it, as `<file>.bak-<date>-<time>`;
   - keep everything else in the file: other servers, settings, comments and formatting;
   - never overwrite a file they cannot parse, and change nothing when run a second time;
   - copy `PROFILEPILOT_HOME` and `PROFILEPILOT_BROWSER` into the client's config when they are set
     in your shell, so every client uses the same data folder and browser.

   `profilepilot install print` shows the snippets for every client without changing anything,
   and `--dry-run` shows what an install would change.

The data folder is `%LOCALAPPDATA%\ProfilePilot` on Windows, `~/Library/Application Support/ProfilePilot`
on macOS and `~/.local/share/profilepilot` on Linux. To use another folder with every route
(including the extension and the plugin, which do not read your shell), set `PROFILEPILOT_HOME` as a
user environment variable and restart the clients.

## Claude Desktop

### Option A: config file (recommended)

```powershell
.venv\Scripts\profilepilot install claude-desktop
```

This adds a `profilepilot` entry to `%APPDATA%\Claude\claude_desktop_config.json`. If you
installed Claude from the Microsoft Store, the app reads a private copy of that file in
`%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\`; the installer updates every copy
it finds. On macOS the file is `~/Library/Application Support/Claude/claude_desktop_config.json`.

To do it by hand, open **Settings > Developer > Edit Config** and merge this in (use your own path;
in JSON every backslash is doubled):

```json
{
  "mcpServers": {
    "profilepilot": {
      "command": "C:\\Users\\you\\ProfilePilot\\.venv\\Scripts\\python.exe",
      "args": ["-m", "profilepilot", "serve"],
      "env": {}
    }
  }
}
```

Then **quit Claude Desktop completely** (tray icon > Quit, not just closing the window) and start it
again. The ProfilePilot tools appear under the "+" (connectors / tools) menu of a chat.

### Option B: desktop extension (`.mcpb`)

The extension needs no virtual environment of your own: Claude Desktop installs the Python
dependencies with the `uv` it ships with.

1. Build the bundle (only the Python standard library is needed, no Node.js):

   ```powershell
   python scripts/build_mcpb.py
   ```

   This validates `mcpb/manifest.json` and writes `dist/profilepilot.mcpb`.
2. Double-click `dist/profilepilot.mcpb`, or open **Settings > Extensions > Advanced settings >
   Install Extension...** and choose the file.
3. The first start downloads and installs the dependencies (Playwright, Scrapling and others), which
   can take a few minutes and needs internet access. If the first connection times out, wait a
   minute and restart Claude Desktop.

The `uv` extension type is still marked experimental by the MCPB project. Use either the extension
or the config entry, not both, or every tool appears twice.

## Claude Code

### Option A: `claude mcp add` (recommended)

```powershell
.venv\Scripts\profilepilot install claude-code
```

This runs the following command for you, or prints it if the `claude` CLI is not on PATH:

```powershell
claude mcp add --scope user profilepilot -- "C:\Users\you\ProfilePilot\.venv\Scripts\python.exe" -m profilepilot serve
```

`--scope user` makes the server available in every project. Check it with `claude mcp list`, or
with `/mcp` inside a session.

### Option B: plugin (server + skill)

The repository is also a Claude Code plugin marketplace. The plugin adds the MCP server and the
`profilepilot` skill, which teaches Claude the snapshot/ref workflow and polite scraping:

```
/plugin marketplace add matyaskovecses/ProfilePilot
/plugin install profilepilot@profilepilot
```

From a shell: `claude plugin marketplace add matyaskovecses/ProfilePilot`, then
`claude plugin install profilepilot@profilepilot`.

**The plugin needs [uv](https://docs.astral.sh/uv/)** on PATH, because it runs the server with
`uvx --from <plugin folder> profilepilot serve`. Install uv with
`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`,
`winget install astral-sh.uv` or `pip install uv`, then restart Claude Code. The first start builds
an environment with all dependencies, which can take longer than Claude Code's startup timeout:
start Claude Code with a longer timeout once (`$env:MCP_TIMEOUT = "180000"; claude`), or run
`/mcp reconnect all` after a minute.

Plugin tools are named `mcp__plugin_profilepilot_profilepilot__<tool>`. Third-party marketplaces
do not update automatically; run `/plugin marketplace update profilepilot` to get new versions.
Don't combine the plugin with Option A, or every tool appears twice.

To use only the skill with Option A, copy `skills/profilepilot/` to `~/.claude/skills/profilepilot/`.

### Option C: one shared HTTP server

If you prefer one long-running server for several clients:

```powershell
$env:PROFILEPILOT_TOKEN = "<TOKEN>"
profilepilot serve --http --port 8931 --auth token
claude mcp add --transport http --scope user profilepilot http://127.0.0.1:8931/mcp --header "Authorization: Bearer <TOKEN>"
```

Use a long random token. Read it from `PROFILEPILOT_TOKEN` or pipe it in with `--token -` rather
than typing `--token <TOKEN>`, because other local programs can read a process's command line.
If you don't give a token at all, the server generates one and prints it once. Stdio (Option A)
is simpler and needs no token.

## Codex CLI, Codex IDE extension and the ChatGPT desktop app

These share one configuration file: `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`).

```powershell
.venv\Scripts\profilepilot install codex
```

This adds (or replaces) only the `[mcp_servers.profilepilot]` table; the rest of the file stays
byte-for-byte the same. The result looks like this:

```toml
[mcp_servers.profilepilot]
command = 'C:\Users\you\ProfilePilot\.venv\Scripts\python.exe'
args = ["-m", "profilepilot", "serve"]
startup_timeout_sec = 60
tool_timeout_sec = 180
```

Single-quoted TOML strings keep Windows backslashes literal. The timeouts are raised from Codex's
defaults (10 s to start, 60 s per tool call), because starting Chrome and waiting for slow pages can
take longer. Check it with `codex mcp list`. In the ChatGPT desktop app the server appears under
**Settings > MCP servers**.

`codex mcp add profilepilot -- "C:\...\python.exe" -m profilepilot serve` also works, but does not
set the timeouts.

## ChatGPT (web)

ChatGPT on the web does not read local config files and cannot start programs on your computer. It
connects to MCP servers in two ways: through **OpenAI Secure MCP Tunnel**, or through a **public
HTTPS URL**. It cannot send API keys or bearer tokens: its only authentication options are "No
auth" and OAuth.

**Easiest:** run `profilepilot connect chatgpt`. It starts a tunnel and the server with an OAuth
sign-in that you approve with a pairing code, and prints exactly what to paste into ChatGPT. The full
walkthrough is in [CHATGPT.md](CHATGPT.md). The manual options are below.

**Plans and workspaces.** Custom MCP servers are not available on every ChatGPT plan, and a
workspace admin may have to allow them. OpenAI describes full MCP support for Business, Enterprise
and Edu workspaces; check what your own account offers under chatgpt.com/plugins before you set
anything up.

### Option A: Secure MCP Tunnel (recommended)

The tunnel client makes only outbound connections to OpenAI and starts ProfilePilot over stdio on
your machine. Nothing is exposed to the internet.

1. Open the [tunnel settings](https://platform.openai.com/settings/organization/tunnels) of your
   OpenAI Platform organization and create a tunnel. Copy its id (`tunnel_...`). Creating a tunnel
   needs the Tunnels Read + Manage permissions; running one needs Tunnels Read + Use.
2. Download `tunnel-client` from
   [github.com/openai/tunnel-client/releases/latest](https://github.com/openai/tunnel-client/releases/latest).
3. In PowerShell, with an API key for that organization:

   ```powershell
   $env:CONTROL_PLANE_API_KEY = "sk-..."
   tunnel-client init --sample sample_mcp_stdio_local --profile profilepilot --tunnel-id tunnel_... --mcp-command 'C:/Users/you/ProfilePilot/.venv/Scripts/python.exe -m profilepilot serve'
   tunnel-client doctor --profile profilepilot --explain
   tunnel-client run --profile profilepilot
   ```

   `profilepilot install print` prints this command with your own interpreter path. Use a path
   without spaces (the printed command uses the Windows short path when needed).
4. On [chatgpt.com/plugins](https://chatgpt.com/plugins), select **+ > Add custom MCP server**.
   Choose **Connection: Tunnel**, pick your tunnel, set authentication to **No auth**, accept the
   risk warning and select **Create as a plugin**.

Keep `tunnel-client run` running while you use ChatGPT. Run only one tunnel client per tunnel id.

### Option B: public HTTPS URL

Prefer `profilepilot connect chatgpt`, which uses `--auth oauth`: ChatGPT signs in with OAuth, and you
approve it with a rotating pairing code that only you can see. The secret-path setup described below
still works for clients that can't do OAuth.

> **Security warning.** Anyone who knows the URL can drive your browser profiles: use their logins
> and cookies, spend your proxy traffic and read pages as you. ChatGPT cannot send a password, so
> the only protection is a long secret in the URL path. Keep the URL private, run the tunnel only
> while you need it, and prefer profiles without important logins. Remote mode blocks `file://`,
> `localhost` and private-network addresses; do not pass `--allow-private-network`. It also does
> not offer `form_autofill_sensitive` (card, SSN and password autofill) unless you start the server
> with `--allow-sensitive-autofill`; even then it only fills sites you allowed with
> `profilepilot identity allow`.

1. Start a tunnel to local port 8931 and note its public host name. With a Cloudflare quick tunnel
   (no account needed; the name changes on every run):

   ```powershell
   cloudflared tunnel --url http://127.0.0.1:8931
   ```

   With ngrok: `ngrok http 8931` (the free plan keeps one fixed `*.ngrok-free.app` name).
2. Start the server with that host name:

   ```powershell
   profilepilot serve --http --port 8931 --auth secret-path --public-host <name>.trycloudflare.com
   ```

   It prints the full URL once: `https://<name>.trycloudflare.com/mcp/<secret>`. The server binds
   to 127.0.0.1 only, answers with plain JSON (quick tunnels do not support streaming responses)
   and rejects requests for any other host name.
3. On chatgpt.com/plugins, select **+ > Add custom MCP server**, paste the URL, choose **No auth**,
   accept the warning and create the plugin.

Port 8931 is ProfilePilot's default; use `--port` and the same port in the tunnel command if it is
taken. A Cloudflare quick tunnel gets a new name each time, so restart `serve` with the new
`--public-host` and update the URL in ChatGPT.

ChatGPT may not pass screenshots to the model. ProfilePilot's tools are text-first, so ask for
`browser_snapshot` or `browser_read` rather than screenshots. Tools that delete data are marked as
destructive, so ChatGPT asks before running them.

## Cursor

```powershell
.venv\Scripts\profilepilot install cursor
```

This adds the same `mcpServers.profilepilot` entry as for Claude Desktop to `~/.cursor/mcp.json`.
Restart Cursor or reload the server under **Cursor Settings > MCP**.

## Any other MCP client

- **stdio:** command `<absolute path to python>`, arguments `-m profilepilot serve`. Some clients
  (for example the official Python MCP SDK's `stdio_client`, used by many Python agent frameworks)
  run the server in a Windows job that kills the server's whole process tree when the client
  disconnects. ProfilePilot detects that and starts the profile's host outside the job (through
  WMI), so browsers keep running. If that is not possible (no WMI), `profile_start` and
  `profile_status` say so; start the profile from a terminal (`profilepilot profile start NAME`)
  or connect over `serve --http` to keep it running.
- **Streamable HTTP:** `profilepilot serve --http --port 8931 --auth token`, with the token in
  `PROFILEPILOT_TOKEN`. URL `http://127.0.0.1:8931/mcp`, header `Authorization: Bearer <TOKEN>`.

## Removing ProfilePilot from a client

| Client | How |
|---|---|
| Claude Desktop, Cursor, Codex | `profilepilot uninstall codex` (or `claude-desktop`, `cursor`). It backs the file up and removes only the `profilepilot` entry |
| Claude Code | `claude mcp remove --scope user profilepilot`, or `/plugin uninstall profilepilot@profilepilot` for the plugin |
| Claude Desktop extension | **Settings > Extensions**, then remove ProfilePilot |
| ChatGPT | Delete the plugin on chatgpt.com/plugins and stop `tunnel-client` or the public tunnel |

Removing the server leaves your profiles and saved proxies in the data folder.

## Troubleshooting

**Where are the logs?**

- Claude Desktop: `%APPDATA%\Claude\logs\mcp-server-*.log` (one file per server; `mcp.log` covers
  all servers). For the Microsoft Store build look in
  `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\logs\`. macOS: `~/Library/Logs/Claude/`.
- Claude Code: `claude mcp get profilepilot` shows the failure on its `Issue:` line; `/mcp` shows the
  status in a session.
- Codex: `codex mcp list`, and the Codex log in `~/.codex/log/`.
- ProfilePilot itself logs to stderr, which the clients write to the files above. Each running
  profile also has `profiles/<id>/host.log` in the data folder.

**The server does not start, or the client says it disconnected.**

1. Run the exact command from the config by hand:
   `"C:\...\.venv\Scripts\python.exe" -m profilepilot serve`. A working server prints nothing and
   waits for input; stop it with Ctrl+C. An error here is the same one the client sees.
2. Run `profilepilot doctor`.
3. If you moved or recreated the virtual environment, run `profilepilot install <client>` again: the
   config still points at the old interpreter.

**Timeouts.**

- The first start of the extension or the plugin installs all dependencies. Restart the client
  once it is done (Claude Code: `/mcp reconnect all`).
- Codex: `startup_timeout_sec` and `tool_timeout_sec` in `config.toml` (the installer sets 60 and 180).
- Claude Code: `MCP_TIMEOUT` (milliseconds) for startup and `MCP_TOOL_TIMEOUT` for tool calls.
  Long pages are returned in chunks; if Claude Code reports output above its token limit, ask for
  smaller chunks (`max_chars`) or raise `MAX_MCP_OUTPUT_TOKENS`.
- A profile's first start can take several seconds while Chrome creates the profile folder.

**Config changes have no effect.** Claude Desktop reads its config only at startup: quit it from
the tray icon and start it again. The Microsoft Store build reads its own copy of the file (see
above), which `profilepilot install claude-desktop` also updates.

**"is not valid JSON" / "is not valid TOML".** The installer never rewrites a file it cannot parse.
Fix the reported line by hand (or restore a `.bak-*` copy) and run the command again.

**HTTP mode: `421` or "Invalid Host header".** The tunnel's host name must be passed with
`--public-host`. Port already in use: pick another `--port` and use it in the tunnel command too.

**ChatGPT does not list the tunnel.** The tunnel must belong to the ChatGPT workspace you use, and
your user needs the Tunnels Use permission. Role changes can take up to 30 minutes.

**A profile will not start.** Another Chrome may be using that profile's folder, or the browser was
not found. `profile_status` and the profile's `host.log` show the reason. To choose the browser
explicitly, set `PROFILEPILOT_BROWSER` (or the profile's `browser` setting) to `chrome`, `edge`,
`brave`, `chromium` or the full path of the browser executable.
