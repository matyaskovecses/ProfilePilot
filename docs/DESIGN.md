# ProfilePilot — design & implementation contract

ProfilePilot lets an AI agent (Claude Desktop, Claude Code, ChatGPT, Codex) drive **many isolated
browser profiles** of the user's **real, native Chrome**. Each profile has its own cookies, history,
storage, cache and its own proxy (HTTP/HTTPS/SOCKS4/SOCKS5, with or without auth). It is
inspired by ShardX/ShardBrowser and Linken Sphere but deliberately does **no fingerprint
spoofing**: pages see the machine's genuine Chrome, exactly like a person using Chrome.

It ships as one Python package exposing an **MCP server** (stdio for Claude/Codex, Streamable
HTTP for ChatGPT), a **CLI**, and a **Python API** that integrates with **Scrapling**. ShardX can be
used as an optional backend.

## 1. Verified facts this design relies on (Chrome 154 on this Windows machine)

| Fact | Consequence |
|---|---|
| `--remote-debugging-port=0`, `--remote-debugging-pipe`, `--headless`, `--enable-automation` set `navigator.webdriver=true`. A **fixed non-zero port** keeps it `false` (tested). | Launch Chrome ourselves with a fixed, pre-allocated port. Never use Playwright's `launch_persistent_context`. |
| With a fixed port Chrome does **not** write `DevToolsActivePort`. | Poll `http://127.0.0.1:<port>/json/version` and verify the listening PID belongs to our Chrome (psutil). |
| Headless = `HeadlessChrome` UA + webdriver. Off-screen headed windows report `document.visibilityState == "hidden"`. | Default window mode `normal` (visible). `offscreen` and `headless` are opt-in. |
| Stock Chrome has no SOCKS5 auth; credentials in `--proxy-server` are rejected (fail closed). | Local credential-free relay per running profile (`proxy/relay.py`, done + tested). |
| Branded Chrome reads `--webrtc-ip-handling-policy` (NOT `--force-webrtc-ip-handling-policy`). | Use `--webrtc-ip-handling-policy=disable_non_proxied_udp` when proxied. |
| QUIC never goes through SOCKS/HTTP proxies. | `--disable-quic` when proxied (hygiene only). |
| `--load-extension` is ignored by branded Chrome 137+. | No extension-based features. |
| Chrome 136+ refuses remote debugging on the *default* user-data-dir. | Every profile has its own `--user-data-dir`. |
| A second Chrome on the same user-data-dir hands its command line to the running one and exits 0. | Per-profile lock + detect "exited quickly without a listener". |
| MCP hosts (Claude Desktop / Code, libuv) run servers in a kill-on-close job with silent breakaway. | A detached host process spawned by the MCP server survives server restarts. |
| The official Python MCP SDK's `stdio_client` runs the server in a kill-on-close job **without** breakaway; a host spawned with `CREATE_BREAKAWAY_FROM_JOB` still lands in it (no error) and dies within a second of the client closing. A process created through WMI `Win32_Process.Create` is in no job (parent `WmiPrvSE.exe`, same interactive session). | The host checks its own job before anything else; inside a foreign kill-on-close job it exits with code 5 and `RuntimeManager` starts it again through WMI. Without WMI it runs inside the job, records `client_job` and the tools say how to keep the browser running. |
| Playwright 1.63 Python: `connect_over_cdp(url, no_defaults=True)`, `page.aria_snapshot(mode="ai", depth=, boxes=)` emits `[ref=eN]`; `page.locator("aria-ref=eN")` resolves a ref; `browser.close()` on a CDP connection only disconnects. | Snapshot/ref-based tools; always use `browser.contexts[0]` (the persistent profile context), never `new_context()`. |
| patchright 1.63 (the default CDP driver since FIX-PLAN step 2; `automation/driver.py`) has Playwright 1.63's API but sends no `Runtime.enable` to pages or workers and evaluates in an isolated world by default. Over `connect_over_cdp(no_defaults=True)` it still sends `Emulation.setFocusEmulationEnabled` to every page (upstream skips it there): every attached tab, background tabs and minimized windows included, then reports `visibilityState "visible"` and `hasFocus() true` (verified). | All imports of the driver go through `automation/driver.py` (`PROFILEPILOT_DRIVER` / `automation.driver` select Playwright as a fallback). It starts patchright's node driver with `patchright_preload.js`, an in-memory, version-checked patch that restores upstream's `no_defaults` rule. `browser_evaluate(world="main")` is the only main-world evaluate. |
| Playwright and patchright send every evaluate (`Runtime.callFunctionOn`, also `page.title()`, aria snapshots and actionability checks) with `userGesture: true`: the page gets sticky user activation (`navigator.userActivation.hasBeenActive`, a running `AudioContext`) without any input. `Page.navigate` itself grants none (verified: a page polling its own state stays un-activated through a CDP navigation). | The second patch of `patchright_preload.js` (`evaluate-without-user-gesture`) sends `userGesture: false`: only real CDP `Input` clicks and keys activate a page, as for a person. `browser_evaluate` therefore runs without a user gesture (the popup blocker stops its `window.open`); the Playwright fallback still activates pages. |
| A URL on Chrome's command line opens like a link from another app: `Sec-Fetch-Site: none`, no user activation, the tab has the focus, `history.length` 1. Started on `about:blank`, focus stays in the omnibox and the first `Page.navigate` adds a history entry. | `browser_navigate` on a stopped profile starts it with the destination (`RuntimeManager.start(start_url=)` → host `--start-url`) and waits for that tab ("Opened at launch", no HTTP status). On a running profile whose only tab is its initial `about:blank`, the host's `POST /open` hands the URL to `chrome.exe --user-data-dir=<udd> --profile-directory=Default <url>` (new tab adopted, blank one closed; never for minimized or headless windows; measured: no change of the OS foreground window). Later navigations use `Page.navigate`. Out-of-process iframes that loaded before the attach have `frame.url == ''` in the driver: `content.frame_url` asks the document. |
| A user-data-dir longer than 175 characters (Windows): Chrome cannot create `GPUPersistentCache/DawnGraphiteCache/<32>/cache.*` (MAX_PATH), and some pages (iphey.com) then crash the browser process with 0xC0000005, with or without a DevTools client (19 of 19 runs at 176-197 characters crashed, none of about 30 at 175 or less). | `browser/prefs.py` `user_data_dir_too_long`; the host logs a warning and the crash message names the cause. The default data root gives ~58 characters. The host writes `last_exit.json`; the next tool call reports a crash once instead of silently restarting the profile, and the first start after a crash does not restore the session (verified: the restored crashing tab crashed it again, in a loop). |
| MCP SDK 2.3: `from mcp.server import MCPServer`; `run("streamable-http", host, port, streamable_http_path, json_response, stateless_http, transport_security)`; `Image` in `mcp.server.mcpserver`; `ToolAnnotations` snake_case in `mcp.types`. DNS-rebinding protection auto-on for loopback hosts. | See §6. |
| ChatGPT custom MCP: public HTTPS Streamable HTTP or OpenAI Secure MCP Tunnel; auth only none/OAuth (no static API keys); `readOnlyHint`/`destructiveHint`/`openWorldHint` should be set; images may not reach the model. | Text-first outputs; secret-path auth option; Secure Tunnel documented. |
| Scrapling 0.4.15 `cdp_url` attach always calls `browser.new_context()` (loses profile cookies, applies dark scheme/DPR2/UA override). | Our Scrapling integration subclasses the session to reuse `contexts[0]`. |
| A CDP `Control+Shift+V` (Playwright `keyboard.press`) pastes the system clipboard into the focused field, also inside out-of-process iframes and while the window is not the OS foreground window; the page gets a trusted `paste` event and `insertFromPaste` input events. Chrome has read the clipboard when `keyboard.press` returns. | "Type-paste" (`browser_paste`, autofill `method="paste"`) holds the text on the clipboard only for the key press: see `automation/typing.py` and `docs/design/AUTOFILL.md`. The same chord sent by `browser_press_key` would paste the *user's* clipboard, so that tool refuses paste chords. |

## 2. Process model

```
 Claude Desktop ──stdio──► profilepilot serve ─┐
 Claude Code    ──stdio──► profilepilot serve ─┤  (each server: Store + RuntimeManager + BrowserManager)
 ChatGPT ─HTTPS tunnel──► profilepilot serve --http ─┤
 Python/Scrapling script ──► profilepilot.client ────┘
                                   │ start(profile) spawns (detached)            attach via CDP (Playwright)
                                   ▼                                                  │
                     profilepilot host <profile_id>   ──spawns──► chrome.exe --user-data-dir=… --remote-debugging-port=N
                     (one per running profile)                    --proxy-server=socks5://127.0.0.1:<relay>
                     • holds profile lock                          ▲
                     • LocalRelay 127.0.0.1:<relay> ───────────────┘ (credential-free; forwards via upstream proxy w/ auth)
                     • control API 127.0.0.1:<ctl> (token)
                     • writes profiles/<id>/runtime.json
                     • Windows job object: Chrome dies if host dies
```

* The **host** is the unit of "a running profile". It exits when Chrome exits (user closed the
  window, or `Browser.close`). Browsers therefore outlive MCP servers; any client can attach. This
  holds also for clients that kill their server's process tree through a job object without
  breakaway (the Python SDK's stdio client): the host leaves such a job by being restarted through
  WMI (see §1). Only where that fails does the browser close with the client, and `profile_start` /
  `profile_status` then say so.
* `runtime.json` (`RuntimeInfo`) is the discovery mechanism. A profile is *running* iff
  runtime.json exists, `state == "running"`, host PID alive, Chrome PID alive with matching
  `create_time`, and `/json/version` answers on `cdp_port`. Anything else is stale and is cleaned.

## 3. Module map & ownership

Done (do not rewrite; extend only if a bug is found, and say so):
`errors.py`, `models.py`, `paths.py`, `jsonio.py`, `secrets.py`, `store.py`, `proxy/url.py`, `proxy/relay.py`.

To implement (one owner each — agents must only create/modify the files they own, plus their tests):

| Owner | Files |
|---|---|
| **A: browser runtime** | `browser/__init__.py`, `browser/flags.py`, `browser/prefs.py`, `browser/control.py`, `browser/host.py`, `browser/runtime.py`, `browser/winjob.py`, tests `tests/test_flags.py`, `tests/test_runtime_chrome.py` |
| **B: automation & content** | `automation/__init__.py`, `automation/manager.py`, `automation/content.py`, `automation/cookies.py`, `safety.py`, `proxy/check.py`, tests `tests/test_content.py`, `tests/test_cookies.py`, `tests/test_safety.py`, `tests/test_proxy_check.py` |
| **C: integrations** | `integrations/__init__.py`, `integrations/shardx.py`, `integrations/scrapling.py`, `client.py`, tests `tests/test_shardx.py`, `tests/test_scrapling_integration.py` |
| **D: packaging & install** | `install.py`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `skills/profilepilot/SKILL.md`, `mcpb/manifest.json`, `scripts/build_mcpb.py`, `.mcpbignore`, `LICENSE`, tests `tests/test_install.py` |
| **E: MCP server & CLI** (after A–C) | `server/__init__.py`, `server/app.py`, `server/tools_profiles.py`, `server/tools_browser.py`, `server/tools_data.py`, `server/tools_shardx.py`, `server/tools_identity.py`, `server/http.py`, `cli.py`, `__main__.py`, tests `tests/test_server.py`, `tests/test_cli.py`, `tests/test_identity_tools.py` |
| **F: typing & autofill** (spec: `docs/design/AUTOFILL.md`) | `identity.py`, `automation/clipboard.py`, `automation/typing.py`, `automation/autofill.py`, tests `tests/test_identity.py`, `tests/test_clipboard.py`, `tests/test_typing.py`, `tests/test_autofill.py` |

### 3.1 `browser/flags.py` (A)

```python
def build_chrome_args(*, browser: BrowserInfo, user_data_dir: Path, cdp_port: int,
                      launch: LaunchOptions, relay_port: int | None, start_urls: list[str]) -> list[str]
```
Native base (always): `--user-data-dir=<abs>`, `--profile-directory=Default`,
`--remote-debugging-port=<cdp_port>`, `--no-first-run`, `--no-default-browser-check`,
`--disable-search-engine-choice-screen`, `--hide-crash-restore-bubble`.
Proxied (`relay_port` set): `--proxy-server=socks5://127.0.0.1:<relay_port>`, and per options:
webrtc auto/proxy_only → `--webrtc-ip-handling-policy=disable_non_proxied_udp`;
`disable_quic` None/True → `--disable-quic`. Not proxied: webrtc `proxy_only` still adds the policy.
`launch.lang` → `--lang=<lang>` and `--accept-lang=<lang>,<primary>` (e.g. `de-DE,de`).
`window == "offscreen"` → `--window-position=-32000,-32000` and `--disable-backgrounding-occluded-windows`
(verify it keeps `visibilityState` "visible"; drop the extra flag if it doesn't help).
`window == "headless"` → `--headless=new` (documented as non-native).
Then `launch.extra_args` (each validated: must start with `--`; **refuse** `--remote-debugging-*`,
`--user-data-dir`, `--proxy-server`, `--headless`, `--enable-automation` — those are managed).
Finally the start URLs (or `about:blank`). **Never** emit `--remote-allow-origins`,
`--enable-automation`, `--disable-blink-features`, `--no-sandbox`, `--user-agent`, `--test-type`.

### 3.2 `browser/prefs.py` (A)
* `prepare_user_data_dir(udd: Path, launch: LaunchOptions, *, proxied: bool = False) -> None` — before each launch:
  delete stale `DevToolsActivePort`; `proxied` (the host has a relay): merge `dns_over_https.mode = "off"`
  into `Local State` and record the previous mode in `<udd>/.profilepilot-doh-off` (Secure DNS probes bypass the
  proxy, docs/FINGERPRINT-AUDIT.md F11); unproxied with that marker: restore the recorded mode unless the user
  changed it, remove the marker; never touch Secure DNS without the marker; ensure `Default/Preferences` exists; set
  `profile.exit_type = "Normal"` and `profile.exited_cleanly = true` (avoid "restore?" bubbles);
  if `launch.restore_session`, make session cookies + tabs survive restarts. **Verify empirically**
  which mechanism works on Chrome 154 (seeding `session.restore_on_startup = 1` in Preferences vs.
  passing `--restore-last-session`) using a session cookie set by a local test server; implement the
  one that works and document the result in a comment.
* `profile_in_use(udd: Path) -> bool` — Windows: try opening `udd/lockfile` for delete/rename
  (PermissionError ⇒ in use); POSIX: `SingletonLock` symlink target pid alive.

### 3.3 `browser/control.py` (A)
Minimal HTTP/1.1 JSON control server run inside the host on `127.0.0.1:0`, every request must
carry header `X-ProfilePilot-Token: <token>` (constant-time compare). Endpoints:
`GET /status` → `{"ok": true, "relay": RelayStats.as_dict() | null, "upstream": redacted|null, "chrome_pid": int}`;
`POST /stop` → triggers graceful stop, returns `{"ok": true}`;
`POST /upstream` body `{"url": "<proxy url with creds>"|null}` or `{"proxy_id": "<id>"|null}` →
swaps the relay upstream live (new connections only). Rejected (409) if the profile was launched
without a relay. `POST /open` body `{"url": "http(s)://..."}` → hands the URL to the running Chrome's
command line (new active tab, like a link from another app; 400 for other schemes, 409 when not
running, headless, or when the extra chrome.exe did not hand off within 5 s: it is then killed; it
carries the relay's proxy switches so it could never start unproxied). Client helper: `control_call(info: RuntimeInfo, method, path, body=None, timeout=5.0) -> dict` (sync, httpx).

### 3.4 `browser/host.py` (A) — `run_host(profile_id: str, root: Path | None) -> int`
Entry: `python -m profilepilot.browser.host <profile_id> [--root PATH] [--window MODE] [--start-url=URL]` (module has `main(argv)`; the CLI also exposes it as the hidden `profilepilot host` subcommand). The host must not
import MCP/Playwright). Logging to `profiles/<id>/host.log` (rotate at ~1 MB). Steps:
1. `Store(root)`; load profile; acquire **non-blocking** `FileLock(profile_dir/"host.lock")` held for
   life (if taken → exit code 3, "already running").
2. Windows: put the host process itself in a new job object with
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (`browser/winjob.py`, pywin32 `win32job`), so Chrome (child)
   dies with the host. Failure is logged, not fatal.
3. Resolve browser (`find_browser(profile.browser, config.browser_path)`), refuse if
   `profile_in_use(udd)` (exit code 4 with message in runtime.json `error`).
4. If profile has a proxy: `LocalRelay(store.profile_proxy_endpoint(profile))`, start on port 0.
5. Start control server; allocate a free CDP port (bind 127.0.0.1:0, read, close; avoid ports
   in use); write runtime.json with `state="starting"`.
6. `prepare_user_data_dir(proxied=relay is not None)`, build args (start URL: `--start-url`, opened even next to a restored session,
   else `launch.start_url` when no session is restored; the log line shows URLs as origins only; a
   user-data-dir longer than 175 characters is logged as a warning, see §1), spawn Chrome (`subprocess.Popen`, stdin/stdout/stderr
   DEVNULL; Windows `creationflags=CREATE_NO_WINDOW`). Poll `/json/version` every 100 ms up to
   45 s; verify via psutil that a process in Chrome's tree (root pid or its children) LISTENs on
   the port. If Chrome exits within ~5 s with no listener → "handed off to an existing Chrome on this
   user-data-dir" error. On failure write runtime.json `error` and exit non-zero.
7. Write runtime.json `state="running"` with `cdp_http_url`, `cdp_ws_url` (from /json/version
   `webSocketDebuggerUrl`), pids, `chrome_create_time`, relay/control ports, token, version.
   `store.touch_started(id)`.
8. Wait for Chrome exit **or** a stop request. Stop = CDP `Browser.close` via websocket (use the
   `websockets` package or raw HTTP upgrade… simplest: Playwright is NOT allowed in the host; use
   `websockets` (add dependency) to send `{"id":1,"method":"Browser.close"}`), wait up to 10 s,
   then `taskkill /PID` (Windows, no /F), wait 5 s, then kill the process tree (psutil).
   When Chrome has exited, write `profiles/<id>/last_exit.json` (`code`, `crashed`/`crash` for an NTSTATUS
   crash code such as 0xC0000005 that no stop asked for, `chrome_pid` + `chrome_create_time`).
9. Cleanup: stop relay & control server, delete runtime.json, `store.add_runtime(...)`, release lock,
   exit 0.

### 3.5 `browser/runtime.py` (A) — `RuntimeManager` (sync API; async callers use `anyio.to_thread.run_sync`)
```python
class RuntimeManager:
    def __init__(self, store: Store): ...
    def status(self, ref: str) -> RuntimeInfo | None          # validated; stale runtime.json removed
    def list_running(self) -> list[RuntimeInfo]
    def start(self, ref: str, *, timeout: float = 60.0, window: WindowMode | None = None,
              start_url: str | None = None) -> RuntimeInfo
        # idempotent (returns current info if running). Enforces config.max_running.
        # start_url (http/https only): passed as `--start-url=<url>`; Chrome opens it at launch
        # (RuntimeInfo.start_url records it; not part of public()).
        # Spawns `sys.executable -m profilepilot.browser.host <id> --root <root>` detached:
        #   Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW (+ CREATE_BREAKAWAY_FROM_JOB, retry without on failure); POSIX: start_new_session=True
        # Waits for runtime.json state=running (or error / host exit) → raises LaunchError with the
        # host's error + last lines of host.log.
        # `window` overrides launch.window for this run only (passed as `--window` to host).
    def stop(self, ref: str, *, timeout: float = 20.0) -> bool  # control /stop → wait; fallback CDP Browser.close; fallback kill pids
    def stop_all(self, timeout: float = 20.0) -> list[str]
    def restart(self, ref: str, **kw) -> RuntimeInfo
    def set_upstream(self, ref: str, proxy_id: str | None) -> None  # live switch via control API; RestartRequiredError if not relayed
    def relay_stats(self, ref: str) -> dict | None
```

### 3.6 `automation/manager.py` (B) — Playwright side (async), used by the MCP server
```python
class BrowserManager:
    def __init__(self, store: Store, runtime: RuntimeManager, shardx: "ShardXClient | None" = None): ...
    async def __aenter__/__aexit__  # starts/stops async_playwright; never closes browsers
    async def session(self, ref: str, *, autostart: bool = True, window=None, start_url=None) -> ProfileSession
        # ref may be a ProfilePilot profile ref or "shardx:<id-or-name>" (ShardX backend)
        # caches one CDP connection per profile; reconnects if disconnected/stale (runtime changed)
        # start_url: used only when this call starts the profile (session.take_launch_url() returns it once)
        # after a crash (last_exit.json of the browser the cached session was attached to): raises
        # ProfileNotRunningError with the crash once instead of autostarting; the next call starts it
    async def crash_error(self, ref: str) -> ProfilePilotError | None   # tool_guard: crash instead of "closed"
    async def disconnect(self, ref: str) -> None   # drop cached connection (call after stop)

class ProfileSession:
    key: str; label: str; runtime: RuntimeInfo | None; browser: Browser; context: BrowserContext
    async def page(self, tab: int | None = None) -> Page    # active page; creates one if none
    async def tabs(self) -> list[dict]                     # [{index, url, title, active}]
    async def new_tab(self, url: str | None) -> Page
    async def select_tab(self, index: int) -> Page
    async def close_tab(self, index: int) -> None          # never close the last tab: navigate it to about:blank instead
    async def locate(self, page, ref: str | None, selector: str | None) -> Locator
        # ref → page.locator(f"aria-ref={ref}"); selector → page.locator(selector) (CSS, or "text=…");
        # neither → PolicyError-style ValueError
```
On attach: `connect_over_cdp(cdp_http_url, no_defaults=True)`, use `browser.contexts[0]`; apply
`Browser.setDownloadBehavior(behavior="allow", downloadPath=<profile downloads>, eventsEnabled=True)`;
if `launch.timezone` set, `Emulation.setTimezoneOverride` on each page (+ `context.on("page")`) and on each
out-of-process iframe (`framenavigated` → `context.new_cdp_session(frame)`, which only succeeds for those; sessions
kept attached). Residual races (an OOPIF's first script, new tabs) and the revert on disconnect: see
`automation/manager.py`; `--time-zone-for-testing` does not exist in branded Chrome 154. `browser_navigate` does not
open a timezone profile's first URL at launch (the page would load before the override).
Track the active page per profile (new popups become active; tools report a tab switch).
A **minimized** window is never restored or brought to the front: `bringToFront`,
`Browser.setWindowBounds` and even `ShowWindow(SW_SHOWNOACTIVATE)` give Chrome the keyboard focus
(verified on Windows 11 / Chrome 154). Read-only tools work on the hidden page
(`page(interactive=False)`); actions report that the window is minimized.

### 3.7 `automation/content.py` (B)
```python
async def snapshot(page, *, depth: int | None, ref: str | None, boxes: bool) -> str   # aria_snapshot(mode="ai"); scoped to ref when given
async def read_page(page, *, fmt: Literal["markdown","text","html"], selector: str | None, main_only: bool) -> str
    # Hidden content (display:none, visibility:hidden, aria-hidden, zero-size, <template>, offscreen
    # tricks) is removed IN THE BROWSER using computed styles before serialising — prompt-injection
    # hygiene. markdown via `markdownify`; text via innerText of the cleaned clone. The selector is
    # resolved by Playwright's engine (pierces open shadow roots, like the action tools). Fixed notes
    # (never page text) report skipped visible iframes and opacity:0 / visibility:hidden text blocks
    # below the viewport that a page reveals on scroll (they stay excluded: browser_scroll, then read).
async def visible_html(page_or_frame) -> str   # for browser_extract: same visibility rules, all
    # attributes, <head> kept, open shadow roots inlined as <template shadowrootmode="open">;
    # browser_extract(include_hidden=true) uses full_html() instead. Both also run in visible iframes.
def extract(html: str, url: str, *, css: str | None, xpath: str | None, attr: str | None, limit: int) -> list[str]
    # Uses scrapling.parser.Selector (supports ::text and ::attr(x)); falls back to lxml if missing.
def paginate(text: str, *, offset: int, max_chars: int) -> tuple[str, int | None]   # returns (chunk, next_offset)
```

### 3.8 `automation/cookies.py` (B)
Conversions between Playwright cookies and: the ShardX/JSON shape
`{domain,name,value,path,expires(unix|null),secure,httpOnly,sameSite}`, Netscape cookies.txt,
and `http.cookiejar.CookieJar`. `export_cookies(cookies, path, fmt)` / `load_cookie_file(path) -> list[dict]`
(auto-detect format). Values are written to files, never returned to the model by default.

### 3.9 `proxy/check.py` (B)
`async def check_proxy(endpoint: ProxyEndpoint | None, *, timeout=12.0) -> ProxyCheck` — starts a
temporary `LocalRelay(endpoint)`, then `httpx.AsyncClient(proxy=relay.http_url)` against the provider
chain (`https://ipwho.is/`, `https://ipapi.co/json/`, `http://ip-api.com/json/?fields=...`),
treating quota errors returned as 200 (`success:false` / `error:true`) as failures. Measures latency.
Also `async def check_via_relay(http_proxy_url)` for running profiles (uses the profile's live relay).

### 3.10 `safety.py` (B)
`class UrlPolicy(remote: bool, allow_private: bool)`, `check(url) -> None` raising `PolicyError`.
Always block: `file:`, `chrome:`, `chrome-extension:`, `devtools:`, `view-source:`, `javascript:`.
In remote mode (HTTP server) unless `allow_private`: block hostnames resolving to loopback/private/
link-local/reserved addresses and `localhost`. Local stdio mode allows localhost (dev servers).
`\` counts as `/` before the query of http(s) URLs (WHATWG, as in Chrome), both when checking and in
`normalize_url`, so the checked host is the one Chrome contacts. `check_host(host, port)` applies the
same rules to upstream proxy hosts (proxy_add / profile_create / profile_set_proxy / proxy_test).
`check(url, resolve=False)` / `acheck(..., resolve=False)` run the static checks only (every local name and private
literal stays blocked): the tools use it for profiles whose traffic leaves through an upstream proxy, so their
host names never reach this machine's resolver (docs/FINGERPRINT-AUDIT.md F8).

### 3.11 `integrations/shardx.py` (C)
`ShardXClient(base_url="http://127.0.0.1:40325", token=None, *, token_provider=None)` using httpx:
`health()`, `list_profiles()`, `running()`, `start(id, headless=False) -> {"port","http_url","web_socket_debugger_url"}`,
`stop(id)`, `list_proxies()`, `resolve(ref)` (id/name/prefix). Errors mapped to `ProfilePilotError`
subclasses with clear messages (launcher not running / 401 token rotated / "already running" opened
from UI). Redact any proxy credentials in error text. Token sources: keyring key `shardx:token`
(set via CLI `profilepilot shardx login --token`), or opt-in mint from ShardX `settings.json`
(`api_secret`, HS256 JWT `{sub:"shardx-api", iat, exp: now+300}` implemented with `hmac`, no PyJWT).
Tested against an in-process fake ShardX API.

### 3.12 `integrations/scrapling.py` + `client.py` (C)
`client.ProfilePilot(root=None)` sync facade: `profiles()`, `create(name, proxy=None, **kw)`, `start(ref, window=None) -> RuntimeInfo`,
`stop(ref)`, `info(ref)`, `cdp_url(ref)`, `proxy_url(ref, kind="http"|"socks5")` (starts if needed),
`cookies(ref, url=None)` (via CDP), `add_proxy(...)`.
Scrapling helpers (import scrapling lazily; clear error if `profilepilot[scrapling]` not installed):
`fetcher_session(ref, **kw) -> FetcherSession` (proxy = profile relay HTTP URL, cookies preloaded),
`AsyncProfileSession(ref, **kw)` — subclass of Scrapling `AsyncDynamicSession` that attaches over CDP
and **reuses `contexts[0]`** (profile cookies), never closes the user's context/browser, never
applies UA/dark-mode/DPR overrides. `async def fetch(ref, url, **kw) -> scrapling Response` convenience.

### 3.13 `install.py` (D)
`register(client: Literal["claude-desktop","claude-code","codex","cursor"], *, python: str = sys.executable, dry_run=False) -> str`
Merges an `mcpServers.profilepilot = {command: <abs python>, args: ["-m","profilepilot","serve"], env:{}}`
entry (backup `*.bak-<timestamp>` first, preserve other keys, utf-8-sig tolerant). Claude Desktop:
write to `%APPDATA%\Claude\claude_desktop_config.json` and, if present, the MSIX copy
`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude_desktop_config.json`. Claude Code:
run `claude mcp add --scope user profilepilot -- <python> -m profilepilot serve` if `claude` is on PATH,
else return the command to run. Codex: add/replace `[mcp_servers.profilepilot]` in `~/.codex/config.toml`
(TOML literal strings for Windows paths, `startup_timeout_sec = 60`, `tool_timeout_sec = 180`).
`snippets() -> dict[str,str]` for `profilepilot install print`.

### 3.14 Server & CLI (E) — see §5 for the tool catalogue.

## 4. Conventions
* Python ≥ 3.10, `from __future__ import annotations`, type hints, small docstrings, `logging`
  (never `print` in server/host code — stdout is the MCP wire).
* Never put secrets (proxy passwords, tokens, cookie values) in model-facing output, logs or argv.
* Windows first, but keep macOS/Linux working (guard Windows-only imports).
* Tests: pytest + pytest-asyncio (strict; mark async tests). `conftest.py` sets
  `PROFILEPILOT_SECRETS=file`; use `tmp_path` stores. Real-Chrome tests are marked
  `@pytest.mark.chrome` and must clean up every process they start. No external network in tests
  (use `tests/fakes.py`: `FakeSocks5Server`, `FakeHttpConnectProxy`, `OriginServer`); anything that
  needs the internet is marked `@pytest.mark.network` and skipped by default.
* Run tests with `.venv/Scripts/python.exe -m pytest`.

## 5. MCP tool catalogue (E implements; names are final)

All browser tools take `profile` (name/id, or `shardx:<name>`), auto-start the profile if needed,
and act on the active tab unless `tab` is given. Outputs are text-first and paginated
(`max_chars` default 12000, `offset`, returning `next_offset`). Annotations: set
`read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint` on every tool.

Profiles & proxies: `profile_list`, `browser_list`, `profile_create(name, proxy?, tags?, notes?, window?, browser?, lang?, timezone?)`,
`profile_update(profile, …)`, `profile_delete(profile)` (to trash; destructive), `profile_clone(profile, new_name, copy_data?)`,
`profile_start(profile, window?)`, `profile_stop(profile)`, `profile_status(profile?)`,
`proxy_list`, `proxy_add(url, name?, tags?)` (+ bulk: newline-separated), `proxy_remove(proxy, force?)`,
`proxy_test(proxy? | profile?)`, `profile_set_proxy(profile, proxy|null)` (live switch when running).

Browser: `browser_navigate(profile, url | "back" | "forward" | "reload", wait_until?)`,
`browser_snapshot(profile, ref?, depth?, max_chars?, offset?)`,
`browser_click(profile, ref|selector, button?, double?)`, `browser_type(profile, ref|selector, text, submit?, clear?, method?)`
(`method`: `fill` (default) | `type` (= the old `slowly`) | `human` (key by key with human timing) | `paste` (system
clipboard + Ctrl/⌘+Shift+V, a real trusted paste; the clipboard is restored)), `browser_paste(profile, ref|selector, text, clear?, submit?)`,
`browser_press_key(profile, key)` (paste chords such as Ctrl+V / Shift+Insert are refused: they would paste the user's own clipboard), `browser_select_option(profile, ref|selector, values)`, `browser_hover`,
`browser_scroll(profile, direction|ref, amount?)`, `browser_wait_for(profile, text?|selector?|seconds?)`,
`browser_screenshot(profile, full_page?, ref?)` → Image + caption, `browser_read(profile, format, selector?, main_only?, max_chars?, offset?)`,
`browser_extract(profile, css?|xpath?, attr?, limit?)`, `browser_evaluate(profile, expression)`,
`browser_tabs(profile, action: list|new|select|close, index?, url?)`.

Data: `cookies_get(profile, url?, names_only?)`, `cookies_set(profile, cookies)`, `cookies_clear(profile, domain?)` (destructive),
`cookies_export(profile, path?, format?, overwrite?)`, `cookies_import(profile, path)` — files live in the
exports folders of the data root (local mode: `serve --files-anywhere` allows other folders; the
store's own files are never written; only cookie files are replaced, and only with `overwrite`),
`http_fetch(profile, url, method?, headers?, body?, format?, engine: auto|httpx|scrapling)` — HTTP request
through the profile's proxy with its cookies (and writes Set-Cookie back to the browser; deletions
only for the responding host's domain, like a browser). Binary bodies are saved to the profile's
downloads folder (PDF text via the optional `profilepilot[pdf]` extra).

Optional free-text parameters are annotated as plain `str` (default None): the MCP SDK `json.loads`
every other string argument, which turned `'{"a": 1}'` into a dict and `'null'` into None.

Identities & forms (`server/tools_identity.py`; spec `docs/design/AUTOFILL.md`): `identity_list`, `identity_show(identity)`
(card / SSN / password masked), `identity_create(name, fields, notes?)`, `identity_update(identity, fields?, name?, notes?)`.
These handle non-sensitive fields only: sensitive keys are refused with the exact `profilepilot identity secret NAME FIELD`
command the user must run (deleting an identity is CLI-only too). `form_detect(profile, scope_ref?, scope_selector?)`
(read-only: kind, label, control, iframe origin, split part), `form_autofill(profile, identity?, fields?, method="paste",
overwrite?, scope_ref?, scope_selector?)` (non-sensitive kinds; the identity defaults to the profile's linked one),
`form_autofill_sensitive(profile, identity?, fields?, method="paste", overwrite?, scope_ref?, scope_selector?)` (card,
expiry, CVV, SSN, password; `destructive_hint`, `_meta["anthropic/requiresUserInteraction"] = true`). The sensitive tool
checks `IdentityStore.check_sensitive_origin` on the top-level URL *before* any secret is read and stops the fill if the
page leaves that origin; child frames get sensitive values only when they are same-origin, allow-listed, or (card fields only)
a known payment processor's https frame; remote (HTTP) servers register it only with `--allow-sensitive-autofill`. Snapshots
mask card / CVV / SSN / password values in every frame, and every page-reading tool output of a profile has the values that
`form_autofill_sensitive` filled replaced by `[redacted]`.
`profile_create` / `profile_update` take `identity` (name or id; `""` unlinks). Type-paste is serialised across
processes by `<data root>/clipboard.lock`. Tool output never contains sensitive values.

ShardX (only registered when enabled): `shardx_status`, `shardx_profiles`, `shardx_start(profile)`, `shardx_stop(profile)`.

## 6. Remote (HTTP) mode
`profilepilot serve --http --host 127.0.0.1 --port 8931 --path /mcp [--public-host H]... [--auth secret-path|token|none] [--token T] [--allow-private-network] [--allow-sensitive-autofill]`.
* `json_response=True`, `stateless_http=True` (cloudflared quick tunnels have no SSE).
* `TransportSecuritySettings(allowed_hosts=[*loopback, H, H:*], allowed_origins=[https://H])`.
* `--auth token`: static bearer via `TokenVerifier` (Claude Code / Codex `--header`). `--auth secret-path`
  (default for ChatGPT): path becomes `/mcp/<32-byte urlsafe token>`; print the full URL once.
  Refuse `--auth none` unless `--host` is loopback **and** `--i-understand` is passed.
* `UrlPolicy(remote=True)` blocks local/private targets unless `--allow-private-network`: before
  navigation, before any browser tool acts on a tab, and again before its output is returned (a page
  can move itself to a blocked address with scripts, timers, meta refresh or popups); blocked tabs
  are navigated to about:blank (`browser_tabs` blanks them all). Proxy hosts are checked too.
* Cookie files: writes only to the session's own exports folder, reads from any exports folder.
* `form_autofill_sensitive` is not registered unless `--allow-sensitive-autofill` (remote clients cannot be relied on to
  ask the user before each call); the allow-listed-origin check applies on top.
* Recommended for ChatGPT: OpenAI Secure MCP Tunnel launching `profilepilot serve` over stdio (no public URL).
