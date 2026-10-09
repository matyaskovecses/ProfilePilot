# ProfilePilot Manager (UI), human handoff and ChatGPT connectivity: spec

The user asked for two things:
1. A way to **control the browser and manage profiles, proxies and identities by hand**, like ShardX's launcher, "because not everything can be automated". The UI must be clean and intuitive.
2. Solid **ChatGPT compatibility**.

The user doesn't know how plugins work in either client. That makes setup UX part of the job.

## A. Control layer: `src/profilepilot/control.py` (shared by the UI, the AI tools and the CLI)

```python
class PauseInfo(BaseModel): paused: bool; by: Literal["user"] = "user"; since: datetime; note: str = ""
class HelpRequest(BaseModel): id: str; profile_id: str; message: str; kind: Literal["captcha","login","verification","payment","other"]
                              created_at: datetime; status: Literal["open","done","dismissed"] = "open"; resolved_at: datetime | None; note: str = ""
class ControlStore:   # file: profiles/<id>/control.json, atomic + locked, multi-process safe
    def __init__(self, store: Store)
    def pause(self, ref, note="") -> PauseInfo           # the user takes control: AI tools refuse to act on the profile
    def resume(self, ref) -> None
    def paused(self, ref) -> PauseInfo | None
    def request_help(self, ref, message, kind="other", *, pause=True) -> HelpRequest   # AI asks the human; pauses the profile
    def resolve_help(self, ref, request_id, *, status="done", note="") -> HelpRequest  # resuming happens when no open request remains (unless the user paused separately)
    def help_requests(self, *, open_only=True) -> list[HelpRequest]                    # across all profiles
class ActivityEvent(BaseModel): ts; profile_id | None; profile_name | None; source: str ("mcp", "mcp-http", "ui", "cli"); tool: str; summary: str; ok: bool; ms: int
class ActivityLog:    # <root>/activity.jsonl, rotating (5 MB, keep 2), append-only, multi-process safe
    def append(self, event) -> None    # summary: one line, at most 200 chars. NEVER secrets: run it through the existing scrubbers and redaction.
    def tail(self, limit=200, *, profile_id=None, since=None) -> list[ActivityEvent]
```

Messages must be **written for the model to act on**. Example of the refusal a paused profile produces:
"The user has taken control of profile 'shop-us' (since 14:02: 'logging in'). Don't act on this profile now. Wait and check
profile_status, or ask the user."

## B. AI-side tools: `src/profilepilot/server/tools_control.py`

These are registered during the wire-in phase.

- `profile_request_help(profile, message, kind="other")` creates a help request and pauses the profile.
  - Use it for a CAPTCHA, a 2FA code, a login, an unclear payment step, and so on.
  - It returns: "The user has been asked in ProfilePilot Manager: …. The profile is paused until they hand it back. Check profile_status in a while."
  - Annotations: not read-only, not destructive.
- `profile_status` (existing) should show the pause and open help requests. This is a wire-in task.
- **Pause enforcement (wire-in):** `tool_guard` in `server/app.py` refuses every browser, form, cookie and http tool on a paused profile, using the message above. Profile management and `profile_status` stay allowed.
- **Activity (wire-in):** `tool_guard` appends an `ActivityEvent` for every tool call: tool name, profile, the first line of the result or error, scrubbed, and the duration.

## C. ProfilePilot Manager: `src/profilepilot/ui/` (local web app, opened as its own app window)

**Constraints:** no Node and no build step. Python serves it with Starlette and uvicorn, both already installed via `mcp`. The frontend is plain ES modules plus modern CSS, vendored, so it works offline. No CDN.

### Launch: `ui/launcher.py`, CLI `profilepilot ui` (the CLI command is added at wire-in)

- Start the server on 127.0.0.1 (a random port, or `--port`). It **must hold a single-instance lock**: `<root>/ui.json` stores `{pid, port}`, and the token lives in the secret store. A second `profilepilot ui` opens a window on the running instance instead of starting another.
- Open an **app window**:
  - Run `<chrome> --app=<url> --user-data-dir=<root>/ui-window --window-size=1320,880 --no-first-run --no-default-browser-check`. This is a dedicated small user-data-dir, **not** a ProfilePilot profile, and it is never driven over CDP.
  - Fall back to `webbrowser.open`.
  - The server exits when the app window closes, unless `--keep-running` or `--no-window`.
- `profilepilot ui --install-shortcut`: Windows Desktop + Start-menu shortcuts ("ProfilePilot Manager") with a generated `.ico`. Build it with stdlib zlib+struct PNG-in-ICO: a simple, crisp logo mark. macOS: a `.command` file. Linux: a `.desktop` file.

### Security (it controls logged-in browsers and secrets)

- **Auth:** a random 32-byte token is required. The launcher mints a single-use, two-minute launch code with the token (`POST /api/launch-code`) and opens `/?t=<code>` once; the master token itself is never accepted in a URL. The server sets an `HttpOnly; SameSite=Strict` cookie and redirects to `/`. Every API call needs the cookie or the `X-ProfilePilot-Token` header.
- **Requests:**
  - The Host header must be `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding).
  - Unsafe methods need an `Origin` that matches (CSRF).
  - CSP is `default-src 'self'`, with no inline script.
- **Secrets:** never returned. Identity sensitive fields are **write-only**, through `PUT /api/identities/{id}/secret/{field}`. Responses are masked. Proxy passwords can be set but are never read back.

### REST API (`ui/api.py`)

JSON, consistent errors `{"error": "...", "code": ...}`. ProfilePilotError maps to 4xx with the error message. Blocking store and runtime calls run in threads.

- **Overview:** `GET /api/overview` returns version, data root, running count, profiles, proxies, identities, open help requests, installed browsers, and client registration status.
- **Profiles:**
  - `GET/POST /api/profiles`
  - `GET/PATCH/DELETE /api/profiles/{id}`; DELETE goes to the trash
  - `POST /api/profiles/{id}/clone|start|stop|focus|pause|resume`
    - `focus` brings the profile's window to the front: the user asked for it, so taking the foreground is fine here.
    - `start` takes `{"window": …}`.
  - `GET /api/profiles/{id}/screenshot`: a JPEG thumbnail of the active tab, via a **raw CDP** `Page.captureScreenshot` on a short-lived page session, **without** `Runtime.enable`. Throttle it: at most one capture per profile every 2 s.
  - `GET /api/profiles/{id}/tabs`
- **Proxies:**
  - `GET/POST /api/proxies`; POST takes `{"text": "<one or many lines>", "scheme": …}`, the bulk format the CLI uses
  - `PATCH/DELETE /api/proxies/{id}`; PATCH can replace the URL, and the password is write-only
  - `POST /api/proxies/{id}/test`, and `POST /api/proxies/test` to test them all, with limited concurrency
- **Identities:**
  - `GET/POST /api/identities`; GET is masked
  - `GET/PATCH/DELETE /api/identities/{id}`
  - `PUT/DELETE /api/identities/{id}/secret/{field}`
  - `POST/DELETE /api/identities/{id}/origins`
  - Linking happens on the profile, through PATCH.
- **Help:** `GET /api/help` (open requests); `POST /api/help/{profile_id}/{request_id}` with `{status, note}`.
- **Activity and events:** `GET /api/activity?limit=&profile=`; `GET /api/events` is **Server-Sent Events** carrying activity events, runtime changes (started, stopped, crashed), help requests and pause changes. It's file-polling based (1 s).
- **Trash:** `GET /api/trash`, `POST /api/trash/{id}/restore`, `DELETE /api/trash`.
- **Settings:** `GET/PATCH /api/settings` (AppConfig: default window, max_running, browser, ShardX base URL and enable, escape_client_job); `PUT /api/settings/shardx-token` is write-only; `GET /api/browsers`.
- **Clients:**
  - `GET /api/clients` returns the registration status for Claude Desktop, Claude Code, Codex and Cursor (`install.is_registered` / `config_paths`).
  - `POST /api/clients/{client}/register|unregister`: the user clicks it in the UI.
  - `GET /api/chatgpt` returns the connection status, the pairing code and the URL, from §D.

### Frontend (`ui/static/`: `index.html`, `app.js` + modules, `styles.css`, `icons.svg`)

The design bar is a polished desktop app, not an admin template: think Linear, Raycast or Arc settings.

- **Layout:**
  - A left sidebar: Profiles, Proxies, Identities, Activity, Connections, Settings, with live counts and a running indicator.
  - The main content on the right. Detail views open as **side drawers**.
- **Profiles view:**
  - A responsive card grid. Each card shows:
    - a live thumbnail (running) or a placeholder (stopped);
    - name, tags, a status dot (running, starting, paused, needs help, stopped, crashed);
    - proxy chip (name + country flag + last check), browser chip, identity chip;
    - quick actions: Start / Stop / **Focus** / **Take control** ↔ **Hand back to AI**.
  - Search and tag filter. A "New profile" dialog sets name, proxy (select or paste a new URL), identity, browser (installed kinds), window mode (normal is recommended, with an explanation), tags and notes.
- **Profile drawer:** tabs for Overview (runtime info, DevTools endpoint copy button, exit IP test), Tabs (list of open tabs), Activity (filtered feed), Settings (edit, clone, delete).
- **Help banner:** pinned at the top whenever the AI requests help. "🤖 Claude needs you in **shop-us**: 'Solve the CAPTCHA'", with [Focus window] [Done] [Dismiss]. It also shows a native desktop notification through the Notification API, when the user allows it.
- **Proxies view:**
  - A table: name, scheme, host:port (username masked), country flag, last exit IP, latency with a sparkline of history if available, and which profiles use it.
  - Bulk import dialog: a textarea with a format hint and a live parse preview. Test and test-all with progress.
- **Identities view:** cards with masked fields grouped (Personal, Address, Card, Sensitive). The edit form uses proper input types. Sensitive fields:
  - Each sensitive field is a write-only input: "Set" / "Replace" / "Clear". The current value is never shown, only `visa •••• 4242`.
  - Allowed origins sit in a chip list with an explanation of why they're needed.
- **Activity view:** a live, filterable feed (profile, tool, ok/error, time), showing what the AI is doing and when.
- **Connections view:**
  - Claude Desktop, Claude Code, Codex, Cursor: registered or not, with a one-click Register/Unregister and copy-paste snippets.
  - ChatGPT: a step-by-step card using §D's status. It shows the server and tunnel state, the URL to paste, the pairing code, and a "How plugins work" explainer in plain language.
- **Settings:** data folder, default window mode, max running, installed browsers, ShardX integration (base URL, token entry), trash.
- **Quality bar:**
  - Light and dark themes (prefers-color-scheme plus a toggle), design tokens in CSS variables, a consistent 4/8 px spacing scale and typography scale.
  - Empty states with a call to action, loading skeletons, toasts for results and errors.
  - Keyboard shortcuts (`/` search, `n` new, `Esc` close).
  - Accessible: labels, focus rings, `aria-live` for toasts, contrast AA.
  - No layout shift when thumbnails load.
- **No secrets in the DOM:** sensitive inputs are cleared after submit.

### Tests (`tests/test_ui_api.py`, `tests/test_ui_smoke.py`)

- **API:** use httpx `ASGITransport` and a tmp store. Cover:
  - auth (missing or wrong token → 401, wrong Host → 400/421, cross-origin POST → 403);
  - profile, proxy and identity CRUD;
  - no secrets in any response (proxy password, identity secret);
  - pause/resume and help request round trips through ControlStore;
  - SSE emits an event after an activity append;
  - the clients endpoint with injected config locations (never the real ones).
- **Smoke (chrome-marked):** start the UI server against a tmp store with seeded data, load it in a throwaway Chrome via `tests/chrome_helper` and Playwright, and check that each view renders with no console errors. Take screenshots of each view into the scratchpad for the UX review.

## D. ChatGPT connectivity

### D1. OAuth 2.1 for `serve --http --auth oauth`: `src/profilepilot/server/oauth.py`

- Implement the SDK's `OAuthAuthorizationServerProvider` (inspect `mcp/server/auth/*` in the venv) as a **single-user** authorization server:
  - dynamic client registration; client-ID metadata documents too, if the SDK supports them;
  - authorization code + PKCE S256 only;
  - refresh tokens and revocation;
  - access tokens that live 1 h and refresh tokens that live 30 days.
- **Storage:** `<root>/oauth.json`, holding **hashed** tokens and the clients. Tokens are random, 32 bytes.
- **Consent page `/oauth/consent`:** server-rendered HTML with no external assets.
  - It shows the client name and redirect URI and asks for the **pairing code**: 8 characters, in groups like `ABCD-2345`, with no ambiguous characters.
  - The pairing code appears in the `serve`/`connect` terminal, in the Manager's Connections view, and in `profilepilot connect status`.
  - Rate-limit to 5 attempts per 10 minutes. The code rotates after use.
  - On approval, redirect with the code.
  - This is what makes a public tunnel URL safe: only someone who can see the user's screen can approve.
- **Redirect URIs:** allow ChatGPT's (`https://chatgpt.com/connector_platform_oauth_redirect`) plus any URI the client registered. Validate exact matches.
- `build_oauth_settings(public_url) -> (provider, AuthSettings)` is the helper for the wire-in step. Also serve `/.well-known/oauth-protected-resource` as the SDK expects.
- **Tests:** a full flow against the ASGI app with httpx. Steps: discovery, then register, then authorize, then consent with a wrong code (refused) and the right code, then token exchange with PKCE, then `/mcp` `tools/list` with the bearer token, then refresh, then revoke. Also: replayed code refused, wrong verifier refused, expired token refused.

### D2. `profilepilot connect chatgpt` wizard: `src/profilepilot/connect.py`

**Interactive and plain-language.** It explains what will happen first:
"ChatGPT can only reach MCP servers on the internet. This starts a secure tunnel to ProfilePilot on this PC, protected by a sign-in that only you can approve."

1. **Option A, OpenAI Secure MCP Tunnel (recommended when available).** Detect `tunnel-client` on PATH. If it's missing, print the official setup steps: an API key with Tunnels permissions, `tunnel-client init … --mcp-command "<python> -m profilepilot serve"`, then `tunnel-client run`. There's no public URL and no OAuth needed.
2. **Option B, cloudflared quick tunnel (no account).**
   - Detect `cloudflared` on PATH or in the common install paths. If it's missing, show `winget install --id Cloudflare.cloudflared`, and run it **only with `--install`**.
   - Start `serve --http --auth oauth --public-host <host>` and `cloudflared tunnel --url http://127.0.0.1:<port>`. Parse the `https://*.trycloudflare.com` URL from cloudflared's output.
   - Print a boxed card:

     ```
     URL to paste: https://<host>/mcp
     ChatGPT → Settings → Apps & Connectors (or chatgpt.com/plugins) → Add custom MCP server
       → Authentication: OAuth
     Pairing code when asked: ABCD-2345
     ```
   - Keep running until Ctrl+C, then stop both. Write `<root>/chatgpt.json` (`{url, started_at, pid}`) so the Manager can show the status.
3. **Option C, ngrok.** Same as B, if `ngrok` is installed and authenticated.

Also add `profilepilot connect status` and `profilepilot connect stop`.

**Tests:** fake `cloudflared`/`ngrok` executables (small Python scripts) that print a realistic URL banner, and a check that the wizard parses them, starts the server with the right flags, and cleans up. No real tunnels and no internet.

### D3. MCP Apps panel inside ChatGPT and Claude: `src/profilepilot/server/apps_ui.py`

- Use `mcp.server.apps.Apps`. Register `ui://profilepilot/dashboard.html`, a self-contained compact profiles panel: status, start/stop, take control/hand back, open help requests.
- Bind it to a tool `profiles_dashboard()` (visibility `["model","app"]`). It must also return a meaningful **text** summary for clients without Apps support.
- Inside the panel, buttons call the server's tools through the MCP Apps host bridge. Implement the ext-apps postMessage JSON-RPC protocol per the spec. Research it: modelcontextprotocol.io/specification/draft/extensions/apps, github.com/modelcontextprotocol/ext-apps, and the OpenAI Apps SDK docs (developers.openai.com/apps-sdk).
- Hosts that don't support it must keep working.
- **Tests:** the resource is listed with MIME `text/html;profile=mcp-app`; the tool carries `_meta.ui.resourceUri`; the text fallback works; the HTML has no external URLs and a CSP-friendly script.

### D4. Docs: `docs/CHATGPT.md`

- What ChatGPT needs (a remote MCP server, plan availability).
- The three connection options with screenshots-as-text steps, the security model (OAuth pairing), and troubleshooting.
- How to stop sharing.

## E. Wire-in, done later by a separate step

- `server/app.py` `tool_guard`: pause enforcement plus activity logging.
- Register `tools_control` and the Apps extension.
- `profile_status`: pause and help state.
- `server/http.py`: `--auth oauth`.
- `cli.py`: `ui`, `connect`, `profile pause/resume`, `help list/resolve`.
- README: Manager and ChatGPT sections. SKILL.md: when to call `profile_request_help`.
- The DESIGN.md tool catalogue, `mcpb/manifest.json` tools, and the exact-tool-set test.
