# Wire-in steps

Integration steps for the new modules of docs/design/MANAGER-AND-CHATGPT.md. Each workflow
appends its own section; the wire-in step applies them to the existing files.

## ChatGPT

Spec: docs/design/MANAGER-AND-CHATGPT.md section D. New modules (done and tested):
`src/profilepilot/server/oauth.py` (D1), `src/profilepilot/connect.py` (D2),
`src/profilepilot/server/apps_ui.py` (D3), `docs/CHATGPT.md` (D4); tests `tests/test_oauth.py`,
`tests/test_connect.py`, `tests/test_apps_ui.py` (the panel's browser test is `@pytest.mark.chrome`).
Two tests skip themselves until this wire-in is done and then check it end to end:
`test_oauth.py::test_wired_into_serve_http` and `test_apps_ui.py::test_registered_in_create_server`.
No new dependencies.

### 1. `server/app.py` `create_server`: OAuth provider + the Apps extension

Add a parameter and pass the provider and the extension to `MCPServer`:

```python
def create_server(..., token_verifier: "TokenVerifier | None" = None,
                  auth_server_provider: "OAuthAuthorizationServerProvider[Any, Any, Any] | None" = None,
                  auth: "AuthSettings | None" = None, ...):
    ...
    kwargs: dict[str, Any] = {}
    if auth_server_provider is not None:          # --auth oauth (the SDK serves /authorize, /token, ...)
        kwargs["auth_server_provider"] = auth_server_provider
        kwargs["auth"] = auth
    elif token_verifier is not None:
        kwargs["token_verifier"] = token_verifier
        kwargs["auth"] = auth
    from . import apps_ui                          # inside the function: apps_ui imports .app

    server = MCPServer(..., extensions=[apps_ui.build_apps()], **kwargs)
```

(`TYPE_CHECKING` import: `from mcp.server.auth.provider import OAuthAuthorizationServerProvider`.)
`build_apps()` registers the `ui://profilepilot/dashboard.html` resource (`text/html;profile=mcp-app`),
`profiles_dashboard()` (visibility `["model","app"]`, read-only) and the app-only `dashboard_action`
(`_meta.ui.visibility = ["app"]`; it needs the per-process token from the result `_meta`, so the model
cannot take or hand back control even in hosts that ignore visibility). Both tools are wrapped in
`tool_guard`, so pause enforcement and activity logging apply automatically; neither is a guarded
tool (the user may stop a paused profile from the panel). Optional INSTRUCTIONS line:
"profiles_dashboard shows the user a live panel of their profiles (ChatGPT/Claude)." (keep
INSTRUCTIONS under the 1800-character test limit).

### 2. `server/http.py`: `--auth oauth`

```python
AuthMode = Literal["secret-path", "token", "oauth", "none"]

@dataclass
class HttpPlan:
    ...
    oauth: Any = None          # OAuthSetup when auth == "oauth"

def build_http_app(...):
    if auth not in ("secret-path", "token", "oauth", "none"):
        raise ProfilePilotError("auth must be 'secret-path', 'token', 'oauth' or 'none'.")
    ...
    oauth_setup = None
    ...
    elif auth == "oauth":
        from .oauth import build_oauth, public_base_url
        # issuer = https://<first public host>; without one http://127.0.0.1:<port> (local clients only)
        oauth_setup = build_oauth(store, public_base_url(host, port, publics), mcp_path=base_path)
        auth_settings = oauth_setup.settings
    ...
    server = create_server(..., token_verifier=verifier,
                           auth_server_provider=oauth_setup.provider if oauth_setup else None,
                           auth=auth_settings, ...)
    app = server.streamable_http_app(...)          # unchanged arguments; endpoint = base_path ("/mcp")
    if oauth_setup is not None:
        app = oauth_setup.wrap(app)                # consent page, discovery documents, RFC 9207 iss
    ...
    return HttpPlan(..., oauth=oauth_setup)
```

* `describe_plan`: for `plan.auth == "oauth"` add `plan.oauth.describe()` (3 lines: "Auth: OAuth with a
  pairing code...", the MCP URL, the pairing code). Like the secret URL, the banner goes to stderr
  once and is never logged. Without `--public-host` add: "No --public-host: only local clients can
  sign in. For ChatGPT run `profilepilot connect chatgpt`."
* Tests that use `plan.app.router` need `getattr(plan.app, "app", plan.app)` for an oauth plan (the
  gateway wraps the Starlette app and passes the lifespan through, so uvicorn needs no change).

### 3. `cli.py`

* `serve --auth` choices: `["secret-path", "token", "oauth", "none"]`; help: "secret-path (default),
  token (bearer), oauth (sign-in with a pairing code; what `connect chatgpt` uses) or none".
* `build_parser()`, after the other commands:

  ```python
  from .connect import add_cli as add_connect_cli
  add_connect_cli(sub, common)          # connect chatgpt | status | stop
  ```

  The handlers use the usual `args.home` / `args.json` and raise only ProfilePilotError;
  `connect chatgpt` asks questions only on a TTY (`-y` for none).
* Module docstring: `connect chatgpt|status|stop    share ProfilePilot with ChatGPT (tunnel + sign-in)`.
* `tests/test_cli.py`: if it pins the command list, add `connect`.

### 4. Tests to update

* `tests/test_server.py::test_tool_catalogue_and_annotations` (exact tool set): add
  `APPS_TOOLS = {"profiles_dashboard", "dashboard_action"}` to the expected union (together with the
  control tools). Both tools pass the existing per-tool checks (four hints, invocation meta,
  `output_schema is None`, description longer than 20 characters).
* Optionally assert `by_name["dashboard_action"].meta["ui"]["visibility"] == ["app"]` and
  `by_name["profiles_dashboard"].meta["ui"]["resourceUri"] == "ui://profilepilot/dashboard.html"`.

### 5. `mcpb/manifest.json` and the DESIGN.md catalogue

* manifest `tools`: add
  `{"name": "profiles_dashboard", "description": "Show your profiles as an interactive panel (status, start/stop, take control)."}`
  and `{"name": "dashboard_action", "description": "The panel's buttons (used by the panel only)."}`.
* DESIGN.md section 5, new paragraph "Apps panel (`server/apps_ui.py`)": `profiles_dashboard()` renders
  `ui://profilepilot/dashboard.html` in MCP Apps hosts (ChatGPT, Claude) and returns a text overview
  elsewhere; `dashboard_action(action, profile, request_id?, token)` is app-only.
* DESIGN.md section 6: `--auth oauth` (OAuth 2.1 + PKCE S256, DCR and client ID metadata documents,
  refresh and revoke, RFC 8707 resource binding, RFC 9207 `iss`, consent with the pairing code; token
  hashes in `<root>/oauth.json`), and "Recommended for ChatGPT: `profilepilot connect chatgpt`".

### 6. ProfilePilot Manager (`ui/api.py`, Connections view)

* `GET /api/chatgpt` -> `profilepilot.connect.status_info(store)` (blocking: run it in a thread):
  `{running, url, mcp_url, tunnel, started_at, port, pairing_code, connections: [{grant_id, client_id,
  client_name, created_at, last_used_at}]}` (`created_at` / `last_used_at` are unix seconds,
  `started_at` ISO 8601). It removes a stale `chatgpt.json` itself.
* Optional `POST /api/chatgpt/stop` with `{"revoke": bool}` -> `connect.stop_sharing(store, revoke=...)`
  (it only stops processes recorded in `chatgpt.json` with a matching PID *and* creation time).
* In the card: `connect chatgpt` runs in a terminal (it shows the pairing code and stops with Ctrl+C);
  the Manager shows the URL and the pairing code while it runs. The "How plugins work" copy can reuse
  the first section of docs/CHATGPT.md.

### 7. README "Use with ChatGPT" (paste after the client list)

```markdown
## Use with ChatGPT

ChatGPT on the web can only use MCP servers on the internet, so ProfilePilot opens a secure tunnel
to your PC, protected by a sign-in that only you can approve:

    profilepilot connect chatgpt

It prints a URL and a pairing code. In ChatGPT open chatgpt.com/plugins (Settings > Apps &
Connectors), choose **+ > Add custom MCP server**, paste the URL, pick **OAuth**, and enter the
pairing code on the ProfilePilot sign-in page. Keep the window open while you use ChatGPT; Ctrl+C
(or `profilepilot connect stop`) ends sharing. In ChatGPT and Claude, "show my profiles" opens an
interactive panel with start/stop and **Take control**. Details, the no-public-URL option (OpenAI
Secure MCP Tunnel) and troubleshooting: [docs/CHATGPT.md](docs/CHATGPT.md).
```

### 8. Other docs

* `docs/CLIENTS.md` "ChatGPT (web)": link to docs/CHATGPT.md and make Option B
  `profilepilot connect chatgpt` (OAuth with a pairing code) instead of the secret path.
* `skills/profilepilot/SKILL.md`: "To show the user their profiles (ChatGPT/Claude), call
  `profiles_dashboard`; never call `dashboard_action` (it is the panel's own button tool)."

## Manager & control

Spec: docs/design/MANAGER-AND-CHATGPT.md sections A-C. New modules (done and tested):

* `src/profilepilot/control.py` (A): `ControlStore` (pause / help requests in `profiles/<id>/control.json`),
  `ActivityLog` (`<root>/activity.jsonl`, rotated at 5 MB, 2 backups), `ProfilePausedError`
  (a `ConflictError`), `refusal_message`, `control_status_lines`, `scrub_text`, `manager_info`.
* `src/profilepilot/server/tools_control.py` (B): the tool `profile_request_help` with `register(server)`,
  and the `tool_guard` hooks `enforce_pause`, `log_activity`, `control_lines`, `is_guarded_tool`.
* `src/profilepilot/ui/` (C): `launcher.py` (`main(argv)`, also `python -m profilepilot.ui`), `server.py`
  (`create_app`, token/Host/Origin/CSP layer), `api.py` (REST + SSE, `record_proxy_history`), `events.py`,
  `cdp.py` (raw-CDP thumbnails and window focus), `shortcut.py` (generated .ico, shortcuts), `static/`.
* Tests: `tests/test_control.py`, `tests/test_ui_api.py`, `tests/test_ui_smoke.py` (`@pytest.mark.chrome`;
  writes `docs/img/manager/*.png`, `PROFILEPILOT_UI_SHOTS=0` skips that). `test_control.py::
  test_wire_in_tool_guard_sketch` runs the exact `tool_guard` below against a stand-in tool.

No new dependencies at runtime: Starlette, uvicorn and sse-starlette come with `mcp`. Since `ui/` imports
them directly, list them explicitly in `pyproject.toml` `dependencies` (recommended):
`"starlette>=0.40"`, `"uvicorn>=0.30"`. Packaging: hatch ships every file under `src/profilepilot`, so
`ui/static/*` (html, js, css, svg) is in the wheel; check `.mcpbignore` does not exclude `*.js`/`*.css`/`*.svg`
under `src/profilepilot/ui/static/` when building the MCPB bundle.

### 1. `server/app.py` `tool_guard`: pause enforcement + activity log

Replace the wrapper body (keep `_crash_instead` and `to_tool_error` as they are):

```python
import time  # module imports

def tool_guard(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Wrap a tool: refuse it on a profile the user controls, log the call, and turn every failure
    into a clean ToolError."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        from .tools_control import enforce_pause, log_activity  # lazy: tools_control imports .app

        started = time.perf_counter()
        ctx = kwargs.get("ctx")
        try:
            await enforce_pause(ctx, fn.__name__, kwargs)
            result = await fn(*args, **kwargs)
        except Exception as exc:  # cancellation (a BaseException) passes through untouched
            crash = await _crash_instead(exc, kwargs)
            error = to_tool_error(crash or exc, fn.__name__)
            await log_activity(ctx, fn.__name__, kwargs, ok=False, result=error, started=started)
            raise error from None
        await log_activity(ctx, fn.__name__, kwargs, ok=True, result=result, started=started)
        return result

    return wrapper
```

* Guarded tools (`tools_control.is_guarded_tool`): every `browser_*`, `form_*`, `cookies_*` and `http_*` tool,
  plus `profile_stop` and `profile_set_proxy` (closing the window or switching the exit IP mid-login would
  break what the user is doing). `profile_status`, `profile_list`, profile management, `proxy_*`,
  `identity_*` and `profile_request_help` stay allowed. ShardX refs (`shardx:...`) are never paused.
* The refusal is the model-facing ToolError text, e.g. "The user has taken control of profile 'shop-us'
  (since 14:02: 'logging in'). Don't act on this profile now. Wait and check profile_status, or ask the user."
  For a help-request pause: "Profile 'shop-us' is waiting for the user: you asked for help at 14:02 (...)".
* `log_activity` writes one line per call: tool, profile id + name, source `mcp` / `mcp-http`
  (`state.remote`), the MCP client's `clientInfo.name`, the first line of the result or error, the
  duration. It applies `state.redact(profile_id, text)` (values `form_autofill_sensitive` typed) and
  `control.scrub_text` (proxy credentials, bearer tokens, JWTs, secret URL parameters, long opaque tokens,
  Luhn-valid card numbers, SSNs; 200 characters). It never raises.
* The Apps panel's `dashboard_action` (ChatGPT section) goes through `tool_guard` too; it is not guarded.
  Its take control / hand back buttons should call `ControlStore(store).pause(profile)` /
  `.resume(profile)` so the Manager, the panel and the AI tools share one state.

### 2. `server/app.py` `create_server`: register the tool, update INSTRUCTIONS

```python
from . import tools_browser, tools_control, tools_data, tools_identity, tools_profiles
...
tools_control.register(server)
```

INSTRUCTIONS: replace "Do not solve CAPTCHAs: ask the user to solve them in the profile's window." with

    Do not solve CAPTCHAs or enter 2FA codes yourself: call profile_request_help(profile, message, kind); \
    the user is asked in ProfilePilot Manager and the profile is paused until they hand it back (browser \
    tools are refused meanwhile). The same refusal appears when the user takes control of a profile.

(stay under the INSTRUCTIONS length limit of `tests/test_server.py`).

### 3. `server/tools_profiles.py`: `profile_status` (and `profile_list`) show the control state

In `profile_status`, single-profile branch, after `target, info, stats, record, live = await run_sync(collect)`:

```python
    from .tools_control import control_lines

    control = await run_sync(control_lines, state.store, target.id)
    lines = [f"Profile '{target.name}' (id {target.id}); saved proxy: {proxy_label(record)}."]
    lines += control                     # pause, open help requests, recently handled requests
    if info is None:
        ...
```

(`control_lines` returns `[]` when nothing is paused or pending, so the output is unchanged then.) In the
no-argument branch (all running profiles), append for each running profile `control_lines(store, i.profile_id)`
after its `runtime_text`. In `profile_line` (`profile_list`), add a part when paused:

```python
from ..control import ControlStore
pause = ControlStore(store).state_by_id(profile.id).effective   # pass store into profile_line or precompute
if pause is not None:
    parts.append("PAUSED: the user has control" if pause.by == "user" else "waiting for the user's help")
```

### 4. `cli.py`

```python
def cmd_ui(args: argparse.Namespace) -> int:
    from .ui.launcher import main as ui_main

    argv = []
    if args.home:
        argv += ["--home", str(args.home)]
    if args.port:
        argv += ["--port", str(args.port)]
    for flag in ("no_window", "keep_running", "install_shortcut", "remove_shortcut"):
        if getattr(args, flag):
            argv.append("--" + flag.replace("_", "-"))
    return ui_main(argv)


def cmd_profile_pause(args: argparse.Namespace) -> int:
    from .control import ActivityEvent, ActivityLog, ControlStore

    store = _store(args)
    profile = store.get_profile(args.profile)
    info = ControlStore(store).pause(profile.id, note=args.note or "")
    ActivityLog(store.root).append(ActivityEvent(profile_id=profile.id, profile_name=profile.name, source="cli",
                                                 tool="take control", summary=f"Paused '{profile.name}' for the AI."))
    emit(args, info.model_dump(mode="json"),
         f"The AI won't act on '{profile.name}' until you run: profilepilot profile resume \"{profile.name}\"")
    return 0


def cmd_profile_resume(args: argparse.Namespace) -> int:
    from .control import ActivityEvent, ActivityLog, ControlStore

    store = _store(args)
    profile = store.get_profile(args.profile)
    closed = ControlStore(store).resume(profile.id)
    ActivityLog(store.root).append(ActivityEvent(profile_id=profile.id, profile_name=profile.name, source="cli",
                                                 tool="hand back", summary=f"Handed '{profile.name}' back to the AI."))
    emit(args, {"resolved": [r.model_dump(mode="json") for r in closed]},
         f"Handed '{profile.name}' back to the AI" + (f" (closed {len(closed)} help request(s))." if closed else "."))
    return 0


def cmd_help_list(args: argparse.Namespace) -> int:
    from .control import KIND_LABELS, ControlStore, clock

    store = _store(args)
    names = {p.id: p.name for p in store.list_profiles()}
    reqs = ControlStore(store).help_requests(open_only=not args.all)
    rows = [(r.id, names.get(r.profile_id, r.profile_id), KIND_LABELS.get(r.kind, r.kind), r.status,
             clock(r.created_at), r.message) for r in reqs]
    emit(args, [r.model_dump(mode="json") for r in reqs],
         lambda: table(rows, ["ID", "PROFILE", "KIND", "STATUS", "ASKED", "MESSAGE"]) if rows
         else "No open help requests.")
    return 0


def cmd_help_resolve(args: argparse.Namespace) -> int:
    from .control import ControlStore

    store = _store(args)
    req = ControlStore(store).resolve_help(args.profile, args.request_id,
                                           status="dismissed" if args.dismiss else "done", note=args.note or "")
    emit(args, req.model_dump(mode="json"), f"Help request {req.id} is {req.status}.")
    return 0
```

Parser (in `build_parser`, next to the other commands):

```python
    p = add(sub, "ui", "open ProfilePilot Manager: manage profiles, proxies and identities, take over from the AI",
            cmd_ui)
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)")
    p.add_argument("--no-window", action="store_true", help="only run the server")
    p.add_argument("--keep-running", action="store_true", help="keep running after the window closes")
    p.add_argument("--install-shortcut", action="store_true", help="create Desktop and Start-menu shortcuts")
    p.add_argument("--remove-shortcut", action="store_true", help="remove those shortcuts")

    # under `profile`:
    p = add(psub, "pause", "take control: the AI won't act on the profile until 'resume'", cmd_profile_pause)
    p.add_argument("profile")
    p.add_argument("--note", help="shown to the AI, e.g. 'logging in'")
    p = add(psub, "resume", "hand the profile back to the AI (closes its open help requests)", cmd_profile_resume)
    p.add_argument("profile")

    hlp = sub.add_parser("help", help="requests from the AI for you (CAPTCHA, login, 2FA)", parents=[common])
    hsub = hlp.add_subparsers(dest="action", metavar="<action>", required=True)
    p = add(hsub, "list", "list open help requests", cmd_help_list)
    p.add_argument("--all", action="store_true", help="include handled and dismissed requests")
    p = add(hsub, "resolve", "mark a help request as done (or dismissed)", cmd_help_resolve)
    p.add_argument("profile")
    p.add_argument("request_id")
    p.add_argument("--dismiss", action="store_true")
    p.add_argument("--note")
```

Module docstring lines: `ui [--install-shortcut]  ProfilePilot Manager (local app window)`,
`profile pause|resume`, `help list|resolve`. `cmd_proxy_test`: after saving a check, also call
`profilepilot.ui.api.record_proxy_history(store, record.id, result)` (feeds the Manager's latency
sparklines; the same one-liner fits `proxy_test` in `server/tools_profiles.py`, `test_saved()`).
`tests/test_cli.py`: if it pins the command list, add `ui`, `help`, `profile pause`, `profile resume`.
`doctor`: optionally report whether ProfilePilot Manager is running (`control.manager_info(store.root)`).

### 5. Tests, catalogue, manifest

* `tests/test_server.py::test_tool_catalogue_and_annotations` (exact tool set): add `"profile_request_help"`
  (annotations: not read-only, not destructive, not idempotent, closed world; it passes the per-tool checks).
* DESIGN.md section 5, Profiles & proxies: `profile_request_help(profile, message, kind=captcha|login|
  verification|payment|other)` - asks the user in ProfilePilot Manager and pauses the profile; plus a
  paragraph "Human handoff": paused profiles refuse browser/form/cookie/http tools (and profile_stop /
  profile_set_proxy) with the refusal above; `profile_status` reports pause and help state; every tool call is
  logged to `<root>/activity.jsonl` (scrubbed) and shown live in the Manager. DESIGN.md section 3 module map:
  `control.py`, `server/tools_control.py`, `ui/` (owner: Manager).
* `mcpb/manifest.json` `tools`: `{"name": "profile_request_help", "description": "Ask the user to solve a
  CAPTCHA, log in or enter a code in a profile's window; pauses the profile until they hand it back."}`.

### 6. `skills/profilepilot/SKILL.md`

Add under the browsing workflow:

    When a page needs a human - a CAPTCHA, a 2FA / e-mail / SMS code, a login with the user's own password,
    a payment confirmation - do not try to solve it. Call profile_request_help(profile, message, kind) with
    one plain sentence ("Solve the CAPTCHA on the sign-in page, then click Done."). The profile is paused
    until the user hands it back in ProfilePilot Manager; check profile_status every minute or so (it says
    when the user handled or dismissed the request) and tell the user in chat what you are waiting for.
    If a tool answers that the user has taken control of a profile, stop using it and wait.

### 7. README: "ProfilePilot Manager" section (paste after the install / clients section)

```markdown
## ProfilePilot Manager: manage everything by hand

Not everything can be automated. `profilepilot ui` opens **ProfilePilot Manager**, a desktop app window
for your profiles, proxies and identities, and the place where you take over from the AI.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/manager/profiles-dark.png">
  <img alt="ProfilePilot Manager: profile cards with live thumbnails" src="docs/img/manager/profiles-light.png">
</picture>

- **Profiles**: live thumbnails of every running browser (with an "AI working" badge while the AI acts),
  start / stop, **Focus** (bring the window to the front), open a page in a profile, and **Take control**: the
  AI pauses on that profile until you click **Hand back to AI**. **Select** several profiles to start, stop,
  tag, delete or give each its own proxy in one go.
- **Help requests**: when the AI hits a CAPTCHA, a login or a 2FA code it asks you here (with a desktop
  notification); you solve it in the profile's window and click **Done**.
- **Proxies**: paste hundreds at once (`host:port:user:pass`, `socks5://...`), test exit IP, country and
  latency, see which profiles use them. Passwords go to the OS keychain and are never shown again.
- **Identities**: your details for form autofill. Card numbers, CVVs, SSNs and passwords are write-only.
- **Activity**: a live feed of every tool call the AI makes, and of what you did.
- **Connections**: one-click setup for Claude Desktop, Claude Code, Codex and Cursor, and ChatGPT's
  pairing code and URL while `profilepilot connect chatgpt` runs.

On first run the Manager walks you through the three steps: connect your AI app, add proxies (optional),
create a profile. `profilepilot ui --install-shortcut` adds "ProfilePilot Manager" to the Desktop and the Start menu
(macOS: a `.command` file; Linux: a `.desktop` entry). The Manager listens on 127.0.0.1 only, needs a
one-time code from the launcher, and never shows stored secrets.

| | |
|---|---|
| ![Proxies](docs/img/manager/proxies-light.png) | ![Profile details](docs/img/manager/drawer-profile-light.png) |
| ![Identities](docs/img/manager/identities-light.png) | ![Activity](docs/img/manager/activity-dark.png) |
| ![Add proxies](docs/img/manager/dialog-import-proxies-light.png) | ![Connections](docs/img/manager/connections-dark.png) |

All screenshots (light and dark) are in `docs/img/manager/`; `tests/test_ui_smoke.py` regenerates them from
obviously fake demo data (`pytest -m chrome tests/test_ui_smoke.py`).
```

Also add `profilepilot ui` to the README command list, and a line under "Use with ChatGPT": the
Connections view shows the URL and the pairing code while `connect chatgpt` runs.

### 8. ProfilePilot Manager x ChatGPT (already done on the Manager side)

`GET /api/chatgpt` calls `profilepilot.connect.status_info(store)` (falls back to `chatgpt.json` when the
module is missing) and `POST /api/chatgpt/stop {"revoke": bool}` calls `connect.stop_sharing`. The
Connections view shows the URL, the pairing code (only while the connection runs), approved apps, and
"Stop sharing" / "Sign out all apps". Nothing to wire.
