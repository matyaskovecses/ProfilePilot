# Safari-engine profiles ("WebKit" browser kind, macOS): spec

The owner asked for **WebKit compatibility**, set up on their Mac. Decisions made with the owner on 2026-10-09:

1. **Purpose: another real identity.** A WebKit profile is used next to the Chrome profiles, as a Mac Safari user
   with persistent logins. It is not a testing engine.
2. **Engine: our own small macOS browser app on Apple's WebKit (`WKWebView`)**, called **ProfilePilot WebKit**.
   - Driving real Safari (an extension or `safaridriver`) can't give a profile its own proxy and only offers
     synthetic input.
   - Playwright WebKit is a patched, non-native build.
   - The model is ShardX: it ships its own browser, a Chromium fork, which is how it gets per-profile proxies.
     We do the same on WebKit, but without spoofing.
3. **It introduces itself as Safari.** `applicationNameForUserAgent = "Version/<installed Safari version>
   Safari/605.1.15"`, so the user agent is byte-identical to the Safari installed on the same Mac (SAFARI-FACTS W16).
   This is a **conscious, owner-approved exception** to the "no fake user agents" rule. Nothing else is spoofed:
   no script injection into pages, no fake objects.
4. **This Mac only.** Every endpoint listens on 127.0.0.1.
5. **v1 scope:** the basic browsing tools, **cookies**, **Manager integration** and **identity autofill**. `http_fetch` is out of scope.
6. **Ownership:** the Mac session leads this design and implementation on the `safari-engine` branch. The Windows
   session reviews it and owns `main`.

**Every behaviour below that is marked W# was measured** (`docs/design/SAFARI-FACTS.md`, harness in
`docs/design/webkit-probe/`). Items marked **V#** must be verified while Phase 1 is being built (§12).

## 0. Phases

| Phase | Content |
|---|---|
| **1a Engine core** | The Swift app, the host branch, the automation protocol, the Python facade, the fake-app tests, the facade-drift test, the SPI test and the macOS `webkit` CI job. Tools: `browser_navigate`, `browser_tabs`, `browser_snapshot`, `browser_read`, `browser_extract`, `browser_click`, `browser_hover`, `browser_type` (fill/type/human), `browser_press_key`, `browser_wait_for`, `browser_screenshot`, `browser_evaluate` (both worlds: `main` is the same call with the page world). CLI `profilepilot webkit install|status`, plus `doctor` checks. |
| **1b Engine complete** | `browser_select_option`, `browser_scroll`, cookies (`WebKitTransport` over the shared `CookieTransport` from `main`), downloads, `restore_session`, `lang` (V3), the leak and fingerprint verifications V9–V12, the quota (V4), and the docs (README, DESIGN.md, SKILL.md, SECURITY.md). |
| **2 Manager** | WebKit profiles in ProfilePilot Manager: status, thumbnails, open/close, *Take control*, help requests, WebRTC leak warning. Built together with the Windows session, which owns `ui/*`. |
| **3 Autofill** | `form_autofill`, `form_autofill_sensitive`, humanized typing and type-paste on WebKit profiles. |

Phases 2 and 3 get their own short specs once Phase 1 has landed (outline in §11).

## 1. Process model

It is the same as Chromium (DESIGN.md §2). Only the browser process differs.

```
 Claude / Codex ─MCP─► profilepilot serve ─► BrowserManager.session("shop-us")   (profile.browser == "webkit")
                                                   │  WebKitSession: Playwright-shaped facade (§7)
                                                   │  WebSocket ws://127.0.0.1:<automation_port>  (token header)
                                                   ▼
 profilepilot host <id>  ──spawns──►  ProfilePilot WebKit.app/Contents/MacOS/ppwebkit   (one process per profile)
   • host.lock, runtime.json            • WKWebsiteDataStore(forIdentifier: <profile store UUID>)   (W1)
   • LocalRelay 127.0.0.1:<relay> ◄──── • proxyConfigurations = [SOCKS5 127.0.0.1:<relay>]          (W4, no credentials)
   • control API (token)                • automation server on 127.0.0.1:<automation_port>
   • config to the app on stdin ───────►• windows = tabs (native macOS window tabs), minimal toolbar for the human
```

- **The host stays the unit of "a running profile".** It keeps the lock, the relay, the control API, `runtime.json`,
  `last_exit.json`, the stop sequence and `/upstream` live proxy switching unchanged. A `WebKitHost` subclass
  replaces only the browser part (§5).
- **One app process per profile.** Each process gets its own WebKit networking and content processes, crash
  isolation, a clean kill, and its own proxy.
- **The app exits when the host goes away.** The host keeps the app's stdin pipe open, and EOF on stdin means the
  host died. This is the POSIX equivalent of the Windows kill-on-close job.

## 2. The app (`src/profilepilot/webkit/app/`, Swift)

The Swift sources ship inside the Python package, so a pip install can build them. `profilepilot webkit install`
compiles them with `swiftc` into `<data root>/apps/ProfilePilot WebKit.app`. The bundle id is
`dev.profilepilot.webkit`, the app is ad-hoc signed (`codesign -s -`), and no binary is ever committed (§10).

| File | Responsibility |
|---|---|
| `main.swift` | Reads one JSON config line from stdin (§2.1). Exits on stdin EOF. Activation policy `.accessory` until the user takes control (Phase 2). `--spi-report` prints the SPI table (§8) and exits. `--remove-store <uuid>` deletes a data store and exits. |
| `Profile.swift` | Builds the profile's `WKWebViewConfiguration`: the data store by UUID (W1), the proxy (W4), `applicationNameForUserAgent` (W16), the feature switches (§8), the WebRTC policy (§8.2), and the utility-world scripts (§2.2). |
| `Tabs.swift` | One `NSWindow` + `WKWebView` per tab. Windows of one profile are grouped as native window tabs (`tabbingIdentifier = "pp-<profile id>"`). It handles popups and `target=_blank` (`createWebViewWith` → a new tab built from the given configuration), dialogs (§2.3), downloads (`WKDownload` → the profile's downloads folder), permission requests (deny), the window-frame callback (§8) and HTTP status capture (`decidePolicyFor navigationResponse`). |
| `Window.swift` | Window modes (§6). `OffscreenWindow` overrides `constrainFrameRect` (W15). Occlusion detection is turned off in offscreen mode (W15). A minimal toolbar (back, forward, reload, address field) for the human when they take control. |
| `Input.swift` | Trusted input (W9): it builds `NSEvent`s and calls the web view's `mouseMoved/mouseDown/mouseUp/scrollWheel/keyDown/keyUp/flagsChanged` **directly**. `NSWindow.sendEvent` does not deliver clicks (W10). Playwright key names map to macOS virtual key codes. |
| `Automation.swift` | A WebSocket server (Network.framework `NWListener`, 127.0.0.1 only) and the JSON-RPC dispatcher (§3). The token is checked in the upgrade request's `X-ProfilePilot-Token`; any upgrade request carrying `Origin` is rejected. |
| `Frames.swift` | The frame registry: the utility-world document-start script registers every frame (W13). The frame tree comes from `_WKFrameTreeNode` (SPI, §8) when available; otherwise children are matched to `<iframe>` elements by `src`. |
| `Capture.swift` | `takeSnapshot` for the viewport, clips and full page (W14), encoded as JPEG/PNG in the app. |
| `Cookies.swift` | `httpCookieStore`: get, set, delete and clear. The store is warmed before the first call (W2 caveat). |
| `Session.swift` | `restore_session`: on a clean exit, saves the open tabs' URLs and the session cookies to `<profile dir>/webkit-session.json`, and restores them on the next start (V5). The file holds session cookies in plaintext (often the login), so it is written **0600 and atomically** (temp file + rename) and **deleted after a successful restore**. SECURITY.md says so. Persistent cookies stay in the WebKit store, protected like Safari's own. |

### 2.1 Config (stdin, one JSON line, never argv or environment)

```json
{"profile_id": "…", "name": "…", "color": "#…", "store_uuid": "…", "safari_version": "26.3",
 "proxy": {"type": "socks5", "host": "127.0.0.1", "port": 50123} | null, "webrtc": true | false,
 "window": "normal" | "offscreen", "start_urls": ["…"], "restore_session": true,
 "automation_port": 50124, "token": "…", "downloads_dir": "…", "session_file": "…", "log_file": "…",
 "language": "de-DE" | null}
```

- The app answers `browser.version` (§3) once its server is up. The host polls that call, the way it polls
  `/json/version` for Chrome.
- `language` (V3) is applied by relaunch arguments (`-AppleLanguages (de-DE)`). If V3 fails, `lang` is refused for
  WebKit profiles (§6).

### 2.2 Utility world

- **Every** script runs in `WKContentWorld.world(name: "profilepilot")` (W12), never in the page world, except
  `browser_evaluate(world="main")`.
- A tiny document-start script (all frames) registers the frame with a message handler that exists **only in that
  world**, so `window.webkit` stays undefined for the page (W12).
- The large scripts are injected **lazily, once per document**, the first time a snapshot or locator needs them:
  - ProfilePilot's runtime: the handle table, actionability and geometry helpers;
  - Playwright's `InjectedScript` (V1), extracted at build time from the installed `playwright` 1.63
    `coreBundle.js`, Apache-2.0 (listed in `ACKNOWLEDGEMENTS.md`).
- No user script ever runs in the page world.

### 2.3 Dialogs, permissions and popups (same semantics as the Chromium path)

- `alert` → OK. `confirm` and `prompt` → dismissed (`false` / `null`). `beforeunload` → accepted. That's what
  `ProfileSession` does on Chrome (W11 shows the delegate answers without blocking).
- Every dialog is reported as an event, so `drain_dialogs()` works.
- Camera, microphone, geolocation and notification requests → denied, plus an event.
- `window.open` and `target=_blank` open a new tab in the same profile and window group, and raise `tab.created`
  with the opener.

## 3. Automation protocol (app ↔ Python)

- **Transport:** JSON text frames on `ws://127.0.0.1:<automation_port>/`.
- **Requests:** `{"id": n, "method": "…", "params": {…}}` → `{"id": n, "result": …}` |
  `{"id": n, "error": {"type": "…", "message": "…"}}`.
- **Events:** `{"event": "…", "params": {…}}`.

| Method | Params → result |
|---|---|
| `browser.version` | → `{product, app_version, safari_version, ua, spi: {features, occlusion, window_frame, frame_tree}}` |
| `browser.close` | Closes every window and exits 0 |
| `browser.focus` / `browser.unfocus` | Phase 2: activate the app and bring the active tab on screen / return to the window mode |
| `tabs.list` | → `[{tab_id, url, title, loading, active, minimized}]`, in window-tab order |
| `tabs.create` / `tabs.activate` / `tabs.close` | `{url?, active?}` → `{tab_id}` / `{tab_id}` / `{tab_id}` |
| `page.navigate` | `{tab_id, url, wait_until, timeout_ms}` → `{url, status}` (`status` from the navigation response) |
| `page.reload` / `page.back` / `page.forward` | `{tab_id, wait_until, timeout_ms}` |
| `page.wait_for_load_state` | `{tab_id, state, timeout_ms}` |
| `page.screenshot` | `{tab_id, clip?, full_page?, format, quality}` → base64 (W14) |
| `frames.list` | `{tab_id}` → `[{frame_id, parent_id, url, origin, is_main}]` |
| `js.evaluate` | `{tab_id, frame_id?, world: "utility" \| "main", body, args, timeout_ms}` → `{value}` (`callAsyncJavaScript`, so promises are awaited) |
| `input.mouse` | `{tab_id, action: move \| down \| up \| click \| wheel, x, y, button, click_count, modifiers, delta_x, delta_y}`, with main-frame viewport CSS px (W9) |
| `input.key` | `{tab_id, action: down \| up \| press, key, modifiers}` (Playwright key names) |
| `input.insert_text` | `{tab_id, text}`: `insertText:` on the web view (NSTextInputClient), the same as Playwright's `keyboard.insert_text` (V2). Per-character typing is a sequence of `input.key` calls made by the Python side. |
| `cookies.get` / `cookies.set` / `cookies.delete` / `cookies.clear` | Cookie dicts in the cookiejar shape of `main` (§7.4) |

**Events:** `tab.created` (with `opener_tab_id`), `tab.closed`, `tab.updated` (url/title/loading),
`page.load` (`commit`/`domcontentloaded`/`load`), `dialog`, `download`, `frame.attached`, `frame.detached`.

**Error types:** `timeout`, `no_tab`, `no_frame`, `navigation` (the context was destroyed; callers retry, as in
`content.py`), `js` (message only), `unsupported`, `bad_request`.

**Wait mapping** (`wait_until`):
- `commit` → `didCommit`;
- `domcontentloaded` → the utility-world `DOMContentLoaded` report;
- `load` → `didFinish`;
- `networkidle` → `didFinish` plus 500 ms with no new resource entries (a documented approximation).

**Rules:**
- **No payload logging.** `<profile dir>/webkit.log` gets method names, ids, durations and error types only.
  Cookies, typed text and evaluated source may contain secrets (CONTRIBUTING rule 2).
- A command whose tab disappears fails at once with `no_tab`; it never hangs.
- Default timeout: 30 s.

## 4. Data, profiles and deletion

- **Browser kind** `webkit`, label "Safari (WebKit)". `find_browser("webkit")` returns the built app (§10) on macOS
  ≥ 14 when `/Applications/Safari.app` exists (it supplies the version for the user agent). Otherwise it raises
  `BrowserNotFoundError` with the fix ("run `profilepilot webkit install`"). `auto` **never** picks it.
- **Store identity.** Each WebKit profile gets a store UUID at creation, saved in `profiles/<id>/webkit.json`. WebKit
  keeps the data in `~/Library/WebKit/dev.profilepilot.webkit/WebsiteDataStore/<uuid>/`, outside the data root.
- **Deletion:**
  - Moving the profile to the trash keeps the store, so a restore just works.
  - Purging the trash runs `ppwebkit --remove-store <uuid>` (`WKWebsiteDataStore.remove(forIdentifier:)`).
  - If the app isn't built, or that call fails (for example after `webkit uninstall`), the purge deletes
    `~/Library/WebKit/dev.profilepilot.webkit/WebsiteDataStore/<uuid>` directly, so a purged profile never leaves
    data behind.
  - `TrashEntry.size_bytes` measures that store directory.
- **Cloning or exporting a profile** is out of scope for v1, and refused for WebKit profiles with a clear message.

## 5. Host and runtime integration (Python)

**`browser/host.py`.** `ProfileHost._launch` is split into a common part plus a `_spawn_browser()` hook. Its
Chromium code moves unchanged into that hook. The new `browser/webkit_host.py` has `WebKitHost(ProfileHost)`, which
overrides:

| Hook | WebKit behaviour |
|---|---|
| `_spawn_browser` | Exec `<app>/Contents/MacOS/ppwebkit` **directly** (never through `open`/LaunchServices, so the pid, stdin and lifetime belong to the host) with stdin = PIPE. Write the config (§2.1) and keep the pipe open. Poll `browser.version` (≤ 45 s). Record `chrome_pid`/`chrome_create_time` (the browser process, as the field docs will say), `automation_url` and `automation_token`. |
| `_close_browser` | `browser.close` → wait 10 s → SIGTERM → wait 5 s → kill. |
| `_route_open` | `tabs.create {url, active: true}`. The app loads it as a browser-initiated navigation, with no command-line hand-off needed. |
| `_route_status` | Adds `{"engine": "webkit", "spi": {…}}` |

**Everything else is unchanged:** the lock, the relay, `/upstream`, `/stop`, `last_exit.json`, the logging and
`runtime.json`. `run_host` picks the class from `profile.browser`.

**`models.py`:**
- `RuntimeInfo` gains `engine: Literal["chromium", "webkit"] = "chromium"`, `automation_url: str | None`,
  `automation_token: str | None`. The token is excluded from `public()`, like `control_token`.

**`browser/runtime.py`:**
- `status()` validates a WebKit runtime by the host and app pids plus create_time, and by the host's `/status`.
  There is no `/json/version`.
- `start` and `stop` are unchanged: the host does the rest.

## 6. Profile settings for WebKit profiles

| Setting | WebKit behaviour |
|---|---|
| `proxy_id` | ✅ Through the LocalRelay (W4/W5), with the same leak rules as Chrome. Live switching via `/upstream` works because it happens at the relay. |
| `launch.webrtc` | §8.2 |
| `launch.window` | `normal`: on-screen windows, opened without activating the app. `offscreen`: the window sits at −32000, −32000 with occlusion detection off, so the page stays `visible` and animation frames run (W15). `headless` set **on the profile**: **refused** ("use offscreen: it keeps the page visible and native"). A **global** `AppConfig.default_window = "headless"`, or a `window="headless"` override at start, quietly means `offscreen` for WebKit profiles, so one global setting never breaks them. |
| `launch.restore_session` | ✅ Tabs and session cookies (V5) |
| `launch.lang` | ✅ If V3 holds, otherwise refused |
| `launch.timezone` | **Refused:** WebKit has no per-view timezone override |
| `launch.extra_args` | **Refused:** there are no browser switches |
| `launch.start_url` | ✅ |
| `identity_id` | ✅ (Phase 3) |

Refusals are one sentence written for the model to act on. They live in one function,
`webkit/profiles.py: check_webkit_profile(profile)`, which `Store.create_profile`/`update_profile` call for
`browser == "webkit"`. The profile tools, the CLI and the Manager API therefore refuse at create and update time,
not at launch. The same module creates the store UUID (`webkit.json`) on create.

**Minimized windows:** same policy as Chrome (DESIGN.md §3.6). Read-only tools work. Interactive tools report that
the window is minimized, because a minimized page is `hidden` and its animation frames stop (W15).

## 7. Python facade (`src/profilepilot/webkit/`)

| File | Content |
|---|---|
| `client.py` | `AutomationClient`: async WebSocket client (the `websockets` dependency already exists) with the token header, request ids, event subscription and the error mapping (§3) to `automation.driver.Error`/`TimeoutError` subclasses. |
| `session.py` | `WebKitSession`: the `ProfileSession` surface tools use (`key`, `label`, `profile`, `runtime`, `context`, `downloads_dir`, `is_connected`, `page()`, `tabs()`, `new_tab()`, `select_tab()`, `close_tab()`, `locate()`, `index_of()`, `is_active()`, `drain_new_tabs()`, `drain_dialogs()`, `take_launch_url()`, `adopt()`, `adopt_url()`, `initial_blank_tab()`, `window_minimized()`, `setup()`, `close()`). |
| `page.py` | `WebKitPage`, `WebKitFrame`, `WebKitLocator`, `WebKitElementHandle`, `WebKitKeyboard`, `WebKitMouse`, `WebKitContext`: **exactly** the Playwright calls made by `server/tools_browser.py`, `automation/content.py` and `automation/cookies.py`. The plan lists them, from a grep. Any other attribute raises `EngineUnsupportedError` naming the call. |
| `injected.py` | Extracts Playwright's `InjectedScript` source from the installed `playwright` package (the version is pinned, and the extraction is checked against a known marker) and builds the ProfilePilot utility runtime. |
| `cookies.py` | The cookiejar-shaped functions for WebKit (§7.4) |
| `build.py` | `profilepilot webkit install`/`status` (§10) |
| `cli.py` | The `webkit` subcommand group. `profilepilot/cli.py` only registers it (one small hunk). |
| `profiles.py` | `check_webkit_profile()` (the §6 refusals), store UUID creation (`webkit.json`), store purge (`ppwebkit --remove-store`) |

### 7.1 Locators, refs and snapshots

- Locators are `{frame_id, selector, nth}`, resolved by Playwright's own selector engines in the utility world
  (V1), so CSS, `xpath=`, `text=`, `internal:*` and `aria-ref=` behave as in Chrome. Strictness (several matches →
  error) is kept.
- Snapshots use Playwright's AI-mode aria snapshot per frame (V1). Child-frame snapshots are stitched under their
  iframe node, with refs prefixed `f<n>` as in Playwright, so `content.snapshot()`, `mask_sensitive_values()` and
  `subtree()` work unchanged.
- If V1 fails, an own walker emits the same format. That is the Windows session's suggestion, and is larger.

### 7.2 Actions (trusted, §3 `input.*`)

1. Actionability, in Playwright's order (utility-world helpers): attached → visible → stable (two animation
   frames; the page is never hidden in normal or offscreen mode) → enabled → scrolled into view → hit target at
   the click point (`elementFromPoint`, covered → retry until timeout, then name the covering element).
2. The point is converted to main-frame viewport coordinates by adding iframe content-box offsets up the frame
   chain.
3. `input.mouse` then delivers real `NSEvent`s (W9).

| Action | How |
|---|---|
| `click` / `dblclick` / `hover` | Mouse events at the point |
| `fill` | Click to focus → `Meta+A` → insert the text. If V2 shows `insertText` on the web view gives trusted `beforeinput`/`input`, it's used; otherwise per-character key events. |
| `type` / `press_sequentially` | `input.text` with the delays of `typing.py` |
| `press` | `input.key` with real key codes, so Enter submits forms, Tab moves focus, Backspace deletes and arrows move the caret **natively**. Nothing is emulated (unlike the old extension design). Paste chords are refused, as on Chrome. |
| `select_option` / `check` | Playwright's semantics (the same in-page events Playwright uses on Chrome) |
| `scroll` / `mouse.wheel` | Real `scrollWheel` events |

### 7.3 Evaluate, screenshots and navigation

- `page.evaluate(js, arg)` turns Playwright's "expression or function source" into a `callAsyncJavaScript` body:
  `const f = (<js>); return typeof f === "function" ? await f(...args) : f;`. It runs in the utility world
  (the same default as patchright's isolated world); `world="main"` uses the page world.
- DOM nodes come back as handle ids that live in the utility world's handle table.
- Screenshots: viewport, element clip and **full page** (W14), capped at `MAX_SHOT_PX` as today.
- Navigation and waits: §3. The HTTP status is real (navigation response).

### 7.4 Cookies (a transport for the shared cookiejar, Phase 1b)

`browser/cookiejar.py` on `main` (the Windows session's Cookie Manager) is being refactored so the engine-neutral
logic is shared: store-and-verify readback, rename = delete then set, import merge/replace, validation, and the
`cookie_key`/`normalize_cookie`/`validate_cookie` helpers. Engines supply only a transport:

```python
class CookieTransport(Protocol):
    async def get(self) -> list[dict]                       # normalised cookiejar shape (normalize_cookie)
    async def write(self, cookies: list[dict]) -> list[int]  # validated cookies; returns the refused indexes
    async def remove(self, cookies: list[dict]) -> list[dict]  # returns those still present
```

The public cookiejar functions take `jar: str | CookieTransport` (a `str` is a CDP ws URL). `webkit/cookies.py` is
only `WebKitTransport(runtime: RuntimeInfo)` over `cookies.get/set/delete`. It never re-implements keys or
validation. `partitionKey` is always `None`: `HTTPCookie` doesn't expose partitions.
`WebKitContext.cookies/add_cookies/clear_cookies` go through the same transport, so the MCP cookie tools, the
Manager tab and the CLI all work unchanged.

### 7.5 Chrome-only code paths

`server/engine.py` (new, so it never conflicts with `server/app.py`) has `require_chromium(session, feature)`. It is
called where CDP or Chrome itself is needed, in `server/tools_browser.py` and in `server/tools_data.py` (where
`http_fetch` and the HTTP identity live):
- `http_fetch` and the HTTP identity;
- Scrapling;
- `cdp_url` hand-outs;
- the timezone override;
- ShardX.

The plan enumerates the call sites from a grep. Each refusal is one sentence for the model. Type-paste stays refused
on WebKit until Phase 3 proves a trusted paste (V6).

## 8. Fingerprint parity and private WebKit SPI

Defaults for every WebKit profile, all measured against real Safari 26.3 on the same Mac (W18):
- **Features on**, through WebKit's own switches: `ApplePayEnabled`, `PushAPIEnabled`, `MediaDevicesEnabled`.
- **The real window frame for `outerWidth/outerHeight/screenX/screenY`**, through the private UI-delegate callback.
  Default WKWebView reports 0 × 0, a classic bot tell.
- **The user agent** from the installed Safari's `CFBundleShortVersionString`, read at every launch (a Safari update
  is followed automatically), but only when Safari's WebKit build matches the WebKit loaded in the app (V11).
  Otherwise the mismatch is reported and the claim adjusted.
- **Known, documented differences:**
  - no `window.safari` (Safari.app adds it itself; we inject nothing);
  - a different storage quota (V4);
  - no WebRTC in proxied profiles (§8.2).

### 8.1 SPI rules

| SPI | Used for |
|---|---|
| `+[WKPreferences _features]`, `-[WKPreferences _setEnabled:forFeature:]` | The feature switches above, and `PeerConnectionEnabled` |
| `-[WKWebView _setWindowOcclusionDetectionEnabled:]` | Offscreen windows stay `visible` (W15) |
| `WKUIDelegate _webView:getWindowFrameWithCompletionHandler:` | Real outer window geometry |
| `-[WKWebView _frames:]` (`_WKFrameTreeNode`: `info`, `childFrames`); `-[WKFrameInfo _handle]` / `_parentFrameHandle` (`_WKFrameHandle.frameID`) | The frame tree and stable frame ids for snapshots and coordinates (fallback: `src` matching). Present on macOS 26.3. |
| `-[WKWebView _evaluateJavaScript:asAsyncFunction:withSourceURL:withArguments:forceUserGesture:inFrame:inWorld:completionHandler:]` with `forceUserGesture: NO` | Every evaluation runs **without a user gesture**. Public `evaluateJavaScript`/`callAsyncJavaScript` count as one, which would give pages sticky user activation (`navigator.userActivation.hasBeenActive`) with no input: the same problem `patchright_preload.js` fixes on Chrome. Present on macOS 26.3. |

- Every call is guarded with `respondsToSelector:`. The app reports the result in `browser.version.spi` and in
  `ppwebkit --spi-report`.
- `profilepilot doctor` and `profile_status` show a missing SPI as a **fingerprint/visibility warning**, never a
  crash.
- `tests/test_webkit_spi.py` (marker `webkit`, run by the macOS CI job) asserts every SPI still exists, so a macOS
  update that removes one fails CI loudly instead of silently changing the fingerprint.

### 8.2 WebRTC

| `launch.webrtc` | Not proxied | Proxied |
|---|---|---|
| `auto` (default) / `proxy_only` | WebRTC on (mDNS host candidates, like Safari) | **`PeerConnectionEnabled` off.** WebKit sends STUN straight from the real interface (W7) and has no "proxy-only UDP" policy. With it off: 0 packets, `RTCPeerConnection` undefined (W8). |
| `default` | On | **On, and it leaks.** This is the user's explicit choice. The Manager's profile settings and `profile_status` show a warning: "WebRTC can reveal this Mac's real IP address past the proxy." |

The README's fingerprint section documents the trade-off: a page can see that `RTCPeerConnection` is missing in a
proxied WebKit profile.

## 9. Security and privacy

- **Network exposure:**
  - The automation server binds 127.0.0.1 only.
  - It requires the per-launch token, passed via stdin and never argv or environment.
  - It rejects any upgrade request with an `Origin` header, so web pages can never connect.
- **Page-invisible automation:**
  - A utility world only (W12): no page-world script handlers, globals, DOM marks or `postMessage` traffic.
  - The only page-world execution is `browser_evaluate(world="main")`, as on Chrome.
- **Credentials:** proxy credentials never reach WebKit (the relay holds them).
- **Logs:** `webkit.log` never contains payloads (§3). The existing redaction (`AppState.redact`) applies unchanged,
  because `respond()` is shared. The URL policy (`check_url`, `enforce_final_url`) and secret-page refusals run on
  the facade unchanged.
- **Downloads** go to the profile's downloads folder only. Permission requests are denied.

## 10. Build, install and distribution

- **Build:** `profilepilot webkit install`.
  1. Requires macOS ≥ 14 and `swiftc` (Xcode or the Command Line Tools; it explains how to get them if missing).
  2. Compiles `webkit/app/*.swift`, writes `Info.plist` (`dev.profilepilot.webkit`, `LSUIElement`, ATS arbitrary
     loads for http test pages), extracts `injected.js`, and ad-hoc signs.
  3. Installs to `<data root>/apps/ProfilePilot WebKit.app` and records the package version.

  It is idempotent, and rebuilds when the package version changes. **No binary is committed.**
- **`profilepilot webkit status`:** whether the app is built, its version against the package, the Safari version,
  the SPI report and the store count.
- **`profilepilot doctor`:** adds the same as one line, plus warnings. It also warns when the data root lies in
  `~/Documents`, `~/Desktop` or `~/Downloads`: macOS privacy protection (TCC) would show `ppwebkit` a permission
  prompt on its first file access there, and that can block the host's 45 s readiness wait. The app's downloads
  folder and logs live under the data root (default `~/Library/Application Support/ProfilePilot`).
- **`profilepilot browsers`:** lists `webkit` when it is usable.
- **CI:** a new `webkit` job on `macos-latest` installs the package, runs `profilepilot webkit install`, then
  `pytest -m webkit` unattended. The spike ran windowless and off-screen, so the runner's GUI session is enough.
  The `webkit` marker is kept out of the fast suite.

## 11. Later phases (outline)

- **Phase 2, Manager.**
  - Profile cards from `runtime.json`.
  - Thumbnails via `page.screenshot`, which works off-screen (W14), unlike Chrome's window capture.
  - *Open*/*Close*.
  - *Take control* → the existing pause plus `browser.focus`: the app becomes `.regular`, activates, and moves an
    offscreen window on screen. *Hand back* → `browser.unfocus`.
  - Help requests unchanged.
  - The WebRTC leak warning (§8.2).
  - The UI files belong to the Windows session; this phase is done together.
- **Phase 3, Autofill.**
  - Fill in the facade gaps that `automation/autofill.py` and `typing.py` use: the frame tree, `frame_element()`,
    element handles, per-frame `bounding_box`.
  - Card iframes work through registered frames.
  - Type-paste via `NSPasteboard` plus a real Cmd+V (V6).
  - Sensitive values go only to the target frame and are never logged.
  - The `form_autofill_sensitive` approval flow is unchanged.

## 12. Verify during Phase 1 (V#)

| # | Question | If false |
|---|---|---|
| V1 | Does Playwright 1.63's `InjectedScript` instantiate in a `WKContentWorld` (`browserName: "webkit"`), with AI aria snapshots and selector engines working? | Own walker with the same format (§7.1) |
| V2 | Does `insertText` on the web view (NSTextInputClient) give trusted `beforeinput`/`input`, including non-ASCII and emoji? | Per-character key events; for characters with no key code, `insertText` |
| V3 | Does `-AppleLanguages (xx-YY)` per process set `navigator.language(s)` and `Accept-Language`? | `lang` refused for WebKit |
| V4 | Can `_WKWebsiteDataStoreConfiguration` quota ratios match Safari's storage estimate? | Documented difference |
| V5 | Can session cookies be saved and restored through `httpCookieStore` so they survive restarts? | Tabs only; documented |
| V6 | Is a real Cmd+V through `NSPasteboard` a trusted paste? | Type-paste stays refused on WebKit |
| V7 | Does HTTPS through the relay, downloads, popups and several app processes in parallel work as specified? | Fix before release |
| V8 | Does an `.accessory` app opening on-screen windows ever take focus on launch, and does `.regular` plus no `activate` stay in the background? | Adjust the window-mode details |
| V9 | **HTTP/3 and QUIC:** load a site that advertises h3 (Alt-Svc), such as `https://cloudflare-quic.com`, twice in a proxied profile. Do 0 UDP/443 packets leave the real interface? Network.framework may race QUIC outside a SOCKS proxy. | Find the switch (a WebKit feature, or disabling HTTP/3 for the store) before release, as with WebRTC |
| V10 | **DNS outside the proxy:** with the proxy set, does any DNS query leave (port 53, DoH, or the system resolver log) from `<link rel=dns-prefetch>`, `preconnect`, speculative loads or HTTPS/SVCB (type 65) lookups? | Turn the responsible feature off for proxied profiles before release |
| V11 (measured 2026-10-09: WebKit `CFBundleVersion` 21623.2.7.11.6 = Safari's, no staged framework on 26.3) | **Safari's staged WebKit:** on macOS 14/15 a newer Safari can ship its own WebKit in `/Library/Apple/System/Library/StagedFrameworks/Safari`, while WKWebView apps load the older system WebKit. The user agent would then claim a Safari version the engine doesn't match. Does the in-process `WebKit.framework` `CFBundleVersion` match Safari.app's? | `browser.version` reports a mismatch, `doctor` and `profile_status` warn, and the user agent uses the Safari version that matches the loaded WebKit (or makes no Safari claim). Never Safari.app's version blindly. On 26.x they match (W16). |
| V12 | **Live proxy switch:** after `/upstream` switches the relay, do WebKit's existing keep-alive connections stop using the old exit? The relay drops tunnels on a switch. Does WebKit reconnect cleanly, with the new exit IP on the next fetch and no restart? | Close idle connections on a switch (reload the store's network session) before release |

## 13. Testing

- **Fast, every OS** (pure Python; a fake app server speaks the §3 protocol over a real WebSocket):
  - `test_webkit_protocol.py`: models; the token and Origin rules on the fake; error mapping.
  - `test_webkit_host.py`: the `WebKitHost` hooks with a fake `ppwebkit` script. Config goes over stdin, never
    argv; `browser.version` readiness; the close sequence; stdin EOF.
  - `test_webkit_profiles.py`: the §6 refusals; `auto` never picks `webkit`; `find_browser("webkit")` off macOS;
    store UUID creation, trash and restore.
  - `test_webkit_facade.py`: facade calls → exact protocol messages; strictness; unsupported attributes raise.
  - `test_webkit_cookies.py` (1b): `WebKitTransport` round-trips against the fake.
  - **No secrets in logs or argv** (caplog, plus the fake process's argv).
  - **Facade drift:** `test_webkit_facade_drift.py` AST-scans `server/tools_browser.py`, `server/tools_data.py`,
    `automation/content.py`, `automation/cookies.py`, `automation/autofill.py` and `automation/typing.py`. It
    collects the Playwright attributes used on page, frame, locator, element-handle, context, keyboard and mouse
    objects, and asserts that each is either implemented by the facade or listed in an explicit `UNSUPPORTED` set.
    A new Playwright call on `main` then fails CI on every OS, instead of surfacing as `EngineUnsupportedError` on
    someone's Mac.
  - **Purge fallback:** a fake store directory is deleted when the app is missing.
- **`webkit` marker** (macOS, opt-in `-m webkit`, and the CI job):
  - `test_webkit_spi.py` (§8.1);
  - `test_webkit_fingerprint.py`: the W16/W18 expectations, `RTCPeerConnection` absent iff proxied with
    `auto`/`proxy_only`, `outerWidth > 0`;
  - `test_webkit_proxy.py`: DNS at the relay, 0 UDP packets when proxied (the spike's SOCKS and UDP fakes);
  - `test_webkit_tools.py`: every Phase 1 tool end to end against the local test server.

  All of them use temporary data roots and their own store UUIDs, and remove their stores afterwards (rule 4).
- The `chrome` tests stay as they are.

## 14. Files touched (conflict plan)

- **New:**
  - `src/profilepilot/webkit/**` (including `app/*.swift`);
  - `src/profilepilot/browser/webkit_host.py`;
  - `src/profilepilot/server/engine.py`;
  - `tests/test_webkit_*.py`;
  - the CI job.
- **Small, guarded edits** (the Windows session confirmed these are free):
  - `paths.py`, `models.py`;
  - `browser/host.py` (the `_spawn_browser` split);
  - `browser/runtime.py`;
  - `automation/manager.py` (`session()` returns a `WebKitSession`);
  - `server/tools_browser.py` (only `require_chromium` calls).
- **Small edits not on either list** (tell the Windows session before touching them):
  - `errors.py`: `EngineUnsupportedError`;
  - `store.py`: calls `check_webkit_profile` on create/update, and the trash-purge hook that removes a WebKit
    store.
- **Shared, rebased after the Windows session's push:**
  - `cli.py`: one hunk registering `webkit`, plus `doctor` lines;
  - `README.md`, `docs/DESIGN.md`, `skills/profilepilot/SKILL.md`.
- **Not touched:** `ui/*`, `control.py`, `server/app.py`, `server/tools_control.py`, `server/oauth.py`,
  `server/http.py`, `connect.py`, `automation/cookies.py`, `browser/cookiejar.py`, `browser/devtools.py`.

## 15. Out of scope

- Remote use from the Windows PC.
- Developer ID signing and notarization (build from source instead).
- `window.safari`.
- `http_fetch` with the WebKit identity.
- A timezone override.
- Profile clone/export for WebKit.
- iOS.
