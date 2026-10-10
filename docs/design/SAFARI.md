# Safari engine (macOS): spec

The user asked for **WebKit compatibility**, set up on their Mac. Their Windows PC keeps developing
ProfilePilot; the agent there reaches the Mac through Claude Remote Control. Decisions the user made
(2026-10-09):

1. **Purpose: another real identity.** A Safari profile is meant to look like a genuine Mac Safari user
   with persistent logins, next to the Chrome profiles. It is not meant as a testing engine.
2. **Engine: real Safari through a Safari Web Extension plus a small companion app.** Playwright WebKit was
   rejected: it is a patched, non-native build ("Native first", CONTRIBUTING.md). `safaridriver` was rejected too,
   for the reasons below. AppleScript `do JavaScript` was rejected because it cannot pick a Safari profile.
3. **Reach: this Mac only.** The bridge listens on 127.0.0.1. The protocol stays network-ready, so remote use
   from the Windows PC can come later (§12).
4. **v1 scope:** the basic browsing tools, **cookies tools**, **Manager integration** and **identity autofill**.
   `http_fetch` with the profile's identity is out of scope.

The user is not a developer. Setup must be a short checklist they can follow, with the CLI or the Manager
verifying each step (§9).

**Why not safaridriver** (the README used to plan it):
- WebDriver sessions set `navigator.webdriver = true`.
- They start from an empty, throwaway website data store, so no login survives.
- They lock the window against human input ("glass pane"), which breaks *Take control*.
- Safari runs one automation session at a time.

Each of these contradicts the identity purpose.

## 0. Phases

| Phase | Content | Spec |
|---|---|---|
| **S0 spike** | A throwaway extension that answers the open facts in §1. Its results are written into §1 and `docs/DESIGN.md` §1 before any plan is written. | this file, §1 |
| **1 Foundation** | Extension and companion app, build script, bridge, pairing, the runtime and session for `browser="safari"`, every basic browsing tool, cookies tools, CLI `profilepilot safari …` | this file, §2–§11 |
| **2 Manager** | Safari profiles in ProfilePilot Manager (status, thumbnails, open/close, take control, help requests, pairing UI, setup checklist) | own spec after Phase 1 (outline §10) |
| **3 Autofill** | `form_autofill` / `form_autofill_sensitive` / humanized typing on Safari profiles | own spec after Phase 1 (outline §10) |

All work happens on the `safari-engine` branch, mostly in new files, so the Windows agent's work on `main`
merges cleanly. Existing modules get small, guarded branches only (§3.2).

## 1. Facts this design relies on: verify first (spike S0)

These are "Verified facts" in the sense of `docs/DESIGN.md` §1. None is verified yet. Each row names the
fallback the design takes if the fact turns out false.

| # | Assumption (Safari 26.3, macOS 26.3) | If false |
|---|---|---|
| F1 | An extension background page can open `ws://127.0.0.1:<port>` (host permission `http://127.0.0.1/*`). The handshake carries `Origin: safari-web-extension://<uuid>`. | Native messaging: `runtime.sendNativeMessage` → the app extension (`SafariWebExtensionHandler`) → a Unix socket in the app group container, which the bridge serves instead of TCP. The bridge interface is unchanged. |
| F2 | Manifest V2 with `"persistent": true` is accepted on macOS, so the background page and its socket stay alive while Safari runs. | MV3 non-persistent background plus reconnect-on-wake. The bridge then queues commands until the instance reconnects (≤ 2 s) and reports "Safari profile asleep" after 10 s. |
| F3 | Every Safari Profile in which the extension is enabled runs **its own** background instance with **its own** `storage.local`. `windows.getAll()` / `tabs.query()` in that instance see only that profile's windows. | Pair by window instead of by instance: the popup pairs the window it was opened from, and the bridge tracks windows. A design change, so stop and re-spec. |
| F4 | A profile's background instance runs while Safari runs, even when the profile has no open window. `windows.create()` from it opens a window **in that profile**. | `start` cannot open a window by itself. It asks the user (and, in Phase 2, the Manager) to "open a Safari window in profile X" and waits. |
| F5 | `tabs.executeScript(tabId, {code, frameId, runAt})` runs a code string in the content-script world of any frame, including cross-origin iframes when the host permission is `<all_urls>`. Content scripts can use `new Function` (needed for `evaluate`). | MV3 `scripting.executeScript({func, args, target: {frameIds}})` with a fixed evaluator. If string code cannot run at all, `browser_evaluate` and the JS-string helpers of `content.py` are ported to fixed functions shipped in the content script (larger change: re-spec). |
| F6 | Main-world execution exists (`scripting.executeScript({world: "MAIN"})`, Safari ≥ 17.x). Page CSP may still forbid `eval` there. | `browser_evaluate(world="main")` is refused on Safari profiles. |
| F7 | `document.execCommand("insertText")` in a focused field fires `beforeinput`/`input` events with `isTrusted === true` in Safari. | Text entry uses the native value setter plus synthetic `input`/`change` events, like Playwright's `fill`, and the "trusted text input" claim is dropped. |
| F8 | `cookies.getAll/set/remove` work, including `httpOnly` cookies, and are scoped to the instance's own profile. | Cookie tools are refused on Safari profiles. |
| F9 | `tabs.captureVisibleTab(windowId, {format: "jpeg", quality})` works for a non-minimized window that is not the frontmost app. | Screenshots need the window in front. The tool says so instead of stealing focus. |
| F10 | Playwright 1.63's injected script (the `InjectedScript` class in `coreBundle.js`) can be instantiated in a Safari content script with `browserName: "webkit"`. Its `ariaSnapshot(..., {mode: "ai"})` and selector engines (`css`, `xpath`, `text`, `internal:*`, `aria-ref`) work there. | An own snapshot/selector script that emits the same `[ref=eN]` format (larger, needs its own tests against Chrome's output). |
| F11 | `runtime.getFrameId(iframeElement)` works in Safari content scripts. It is used to place a child frame's snapshot under its `<iframe>` node. | Match child frames to `<iframe>` elements by document order + `src` (from `webNavigation.getAllFrames`). |
| F12 | An unsigned extension built with "Sign to Run Locally" runs after Safari ▸ Settings ▸ Developer ▸ *Allow unsigned extensions* (reset at every Safari launch). A free Apple ID "Personal Team" signature removes that step. | Document the per-launch toggle only. |
| F13 | Once the user chooses *Always Allow on Every Website* for the extension in each Safari profile, there are no more per-site prompts. | `profilepilot safari setup` checks the access level and explains where to grant it. |

S0 builds the smallest extension that probes F1–F13 against a local test page and prints a result table.
Spike code stays throwaway (outside the repo or deleted). Only its results are kept, in this table, and the
confirmed rows are copied into `docs/DESIGN.md` §1.

## 2. Process model

```
 Claude / Codex ──MCP──► profilepilot serve ─► BrowserManager.session("shop-us")
 Python client ───────►                              │ profile.browser == "safari"
                                                     ▼
                                  SafariSession  (duck-types ProfileSession)
                                  SafariPage / SafariFrame / SafariLocator /
                                  SafariKeyboard / SafariMouse / SafariContext
                                                     │ WebSocket /client  (token, no Origin)
                                                     ▼
                         profilepilot safari-bridge  (one per data root; started on demand, detached)
                         • <root>/safari/bridge.json (pid, create_time, port, token)
                         • <root>/safari/pairings.json (instance_id → profile_id, secret hash)
                         • writes / removes profiles/<id>/runtime.json for Safari profiles
                                                     ▲ WebSocket /ext  (Origin safari-web-extension://…, instance secret)
                         ┌───────────────────────────┴───────────────────────────┐
              Safari Profile "shop-us"                                 Safari Profile "research"
              ProfilePilot extension instance                          ProfilePilot extension instance
              background.js ── tabs / windows / cookies / capture / webNavigation
              content.js (all frames) ── refs, snapshots, actions, evaluate (Playwright injected script)
```

- **The bridge is the unit of "Safari is reachable".** It is one process for all Safari profiles: Safari profiles
  share one Safari process, so there is nothing to host per profile. It outlives MCP servers, for the same
  reason the Chromium hosts do (§2 of DESIGN.md).
- **Several ProfilePilot processes can share the bridge** (Claude Desktop, Claude Code, Codex, the CLI, scripts).
  Requests are multiplexed by id; tab events go to every client subscribed to that profile.
- **The extension always dials out** and reconnects with backoff (0.5 s → 5 s) while Safari runs. The bridge
  never needs to find the extension.

## 3. Files and ownership

### 3.1 New

| Path | Content |
|---|---|
| `safari-extension/ProfilePilot for Safari.xcodeproj` | Xcode project: a macOS app (a single window with setup instructions and a "Open Safari Settings" button) containing the Safari Web Extension app extension. Bundle ids `io.github.matyaskovecses.profilepilot.safari` and `….extension`. |
| `safari-extension/Shared (Extension)/Resources/manifest.json` | MV2 (F2): `background.persistent: true`, `content_scripts` with `all_frames: true`, `match_about_blank: true`, `run_at: document_start`. Permissions: `tabs`, `windows`, `cookies`, `webNavigation`, `storage`, `<all_urls>`, `http://127.0.0.1/*`. No `web_accessible_resources`, so pages cannot probe for the extension. |
| `…/Resources/background.js` | Connection, pairing, and the command dispatcher (§4). |
| `…/Resources/content.js` | Per-frame RPC: refs, snapshots, locators, actionability, input events, evaluate, element handles. |
| `…/Resources/popup.html`, `popup.js` | Shows the pairing state, the pairing code and the bridge connection. Offers *Unpair* and a field for the bridge port (default 47652, kept in `storage.local`). |
| `…/Resources/vendor/injected.js` | **Generated** by the build: Playwright's `InjectedScript` source extracted from the installed `playwright` 1.63 `coreBundle.js` (F10), with its Apache-2.0 notice. Never edited by hand. Listed in `ACKNOWLEDGEMENTS.md`. |
| `scripts/build_safari.py` | Extracts `injected.js`, runs `xcodebuild` (Release; ad-hoc signing, or `--team <id>` for an Apple ID signature) and installs the app to `~/Applications`. `--no-sign` builds only, for CI. |
| `src/profilepilot/safari/__init__.py` | Package. Nothing imports Xcode, and the package imports on every OS. |
| `src/profilepilot/safari/protocol.py` | Pydantic message models and command names (§4). Shared by the bridge, the client and the tests. |
| `src/profilepilot/safari/pairing.py` | `PairingStore` (file `<root>/safari/pairings.json`, atomic, locked; stores `sha256(secret)` only). |
| `src/profilepilot/safari/bridge.py` | The bridge process: `main(argv)` and `run_bridge(root, port)`. It never imports MCP or Playwright, like the host. |
| `src/profilepilot/safari/client.py` | `BridgeClient`: async WebSocket client, request/response by id, event subscription, `ensure_bridge(store)` (start detached, wait for `bridge.json`). |
| `src/profilepilot/safari/session.py` | `SafariSession` (the `ProfileSession` surface listed in DESIGN.md §3.6). |
| `src/profilepilot/safari/page.py` | `SafariPage`, `SafariFrame`, `SafariLocator`, `SafariElementHandle`, `SafariKeyboard`, `SafariMouse`, `SafariContext`: the Playwright API subset of §6. |
| `src/profilepilot/safari/setup.py` | Setup checklist and verification (§9). |
| `tests/test_safari_*.py` | §11. |

### 3.2 Changed (small, guarded)

| File | Change |
|---|---|
| `paths.py` | `SAFARI = "safari"`. `find_browser("safari")` returns `/Applications/Safari.app` on macOS and raises `BrowserNotFoundError` elsewhere. **Not** added to the `auto` order: `auto` never picks Safari. `BROWSER_LABELS["safari"] = "Safari"`. Update the "Not supported" comment. |
| `models.py` | `Profile.browser` docstring. `AppConfig.safari_bridge_port: int = 47652`. |
| `store.py` / profile tools / CLI | Creating or updating a `browser="safari"` profile refuses settings that cannot apply (§5), with a clear message. |
| `browser/runtime.py` | `RuntimeManager.start/stop/status` branch on `profile.browser == "safari"` and delegate to `safari.client` (§5). Everything else (`list_running`, `max_running`, `restart`) keeps working through `runtime.json`. `set_upstream` raises the "Safari profiles use the Mac's own network" error. |
| `automation/manager.py` | `BrowserManager.session()` returns a `SafariSession` for Safari profiles (cached per profile like CDP sessions). `disconnect()` drops it. |
| `server/tools_browser.py`, `server/tools_data.py`, … | Only where a Chrome-only feature is used: a single `require_chromium(session, "<feature>")` guard that raises `SafariUnsupportedError` (§7). |
| `errors.py` | `SafariUnsupportedError(ProfilePilotError)`, `SafariSetupError(ProfilePilotError)`. |
| `cli.py` | `profilepilot safari setup | status | pair <profile> <code> | unpair <profile>`, plus the hidden `safari-bridge` subcommand. |
| `README.md`, `docs/DESIGN.md`, `skills/profilepilot/SKILL.md` | Document Safari profiles. Replace the README's "planned as a safaridriver backend" line. |

## 4. Bridge protocol

All messages are JSON text frames, one object per frame. They are size-limited: 32 MB for screenshots,
2 MB for everything else.

**`/ext` (extension instances)**
- **Handshake.** The `Origin` must start with `safari-web-extension://`; anything else gets HTTP 403. A browser
  page cannot forge `Origin`, so web pages are locked out.
- **First message** within 5 s: `{"type": "hello", "instance": "<uuid4>", "secret": "<base64url 32 bytes>",
  "ext_version": "…", "ua": navigator.userAgent}`.
- **Reply:** `{"type": "welcome", "paired": true, "profile_id": "…"}` for a known instance whose secret hash
  matches. Otherwise `{"type": "unpaired", "code": "<6 chars>"}`, with `code = base32(sha256("pp-pair" ‖ secret))[:6]`.
- **A paired instance whose secret does not match** is closed (4401) and logged with its instance id only, never
  the secret.
- **One live connection per instance.** A newer connection replaces the older one.
- **Commands** to the extension: `{"id": n, "cmd": "<name>", "args": {…}}`.
- **Replies:** `{"id": n, "ok": true, "result": …}` or `{"id": n, "ok": false, "error": {"type": "<kind>", "message": "…"}}`.
- **Events** from the extension: `{"type": "event", "event": "<name>", "data": {…}}`.
  Names: `window_created`, `window_removed`, `tab_created`, `tab_removed`, `tab_updated` (url/title/status),
  `tab_activated`, `nav_committed`, `nav_completed`.

**`/client` (ProfilePilot processes)**
- **Header** `X-ProfilePilot-Token: <bridge.json token>` (constant-time compare). A present `Origin` header gets 403.
- **Requests:** `{"id": n, "profile": "<profile id>", "cmd": "<name>", "args": {…}, "timeout": s}`, answered in
  the same shape as on `/ext`.
- **Bridge-level commands** (no `profile`): `status`, `pair`, `unpair`, `subscribe`, `unsubscribe`.

**Commands forwarded to the profile's instance (Phase 1):**

| Command | Args → result |
|---|---|
| `windows.open` | `{url?}` → `{window_id, tab_id}` |
| `windows.close_all` | → `{closed}` |
| `tabs.list` | → `[{tab_id, window_id, index, url, title, active, status}]` (profile-wide order: window, then index) |
| `tabs.create` / `tabs.activate` / `tabs.close` | `{url?}` / `{tab_id}` / `{tab_id}` |
| `tabs.navigate` | `{tab_id, url, wait_until, timeout}` → `{url, status?}` (`status` only if `webRequest` reports it) |
| `tabs.reload` / `tabs.back` / `tabs.forward` | `{tab_id, wait_until, timeout}` |
| `tabs.capture` | `{tab_id, clip?, quality}` → base64 JPEG. Activates the tab inside its window but never focuses the window (F9). Cropping happens in the background page's canvas, so Python needs no image library. |
| `frames.list` | `{tab_id}` → `[{frame_id, parent_frame_id, url}]` |
| `dom` | `{tab_id, frame_id, method, params}` → the content script's result (methods below) |
| `eval.main` | `{tab_id, frame_id, code, arg}` (F6) |
| `cookies.get` / `cookies.set` / `cookies.remove` / `cookies.clear` | Playwright cookie dicts in and out (F8) |

**Content-script methods (`dom`):** `snapshot`, `count`, `query`, `bounding_box`, `action`, `evaluate`,
`handle.evaluate`, `handle.dispose`, `wait_for_selector`, `wait_for_function`, `document_state`.

- `action.kind`: `click`, `dblclick`, `hover`, `focus`, `fill`, `type`, `press`, `select_option`, `check`,
  `scroll_into_view`, `scroll`, `set_checked`.

**Error types:** `timeout`, `not_found` (selector), `stale_ref`, `strict` (several matches), `not_actionable`
(hidden, disabled, covered: with the reason), `no_tab`, `dialog` (the tab is blocked by an `alert`/`confirm`/
`prompt`: §6.6), `navigation` (an in-flight navigation destroyed the context: callers retry like `content.py`),
`unsupported`, `js` (page script threw: message only), `asleep` (F2 fallback).

**Rules:**
- **Nothing is logged with payload content.** The bridge logs command names, profile ids, durations and error
  types only. `cookies.*`, `dom.action` (`fill`/`type` text) and `eval*` may carry secrets (CONTRIBUTING.md rule 2).
- **Default per-command timeout:** 30 s, or the caller's own. A command whose instance disconnects fails at once
  with `no_tab`/`asleep`, never hangs.

## 5. Profiles, pairing and lifecycle

### 5.1 A Safari profile
- `Profile.browser == "safari"`, created with `profile_create(browser="safari")` or `profilepilot profile create --browser safari`.
- Refused on a Safari profile, each with a one-line reason:
  - `proxy_id` ("Safari uses this Mac's own network settings; Safari profiles cannot have their own proxy");
  - `launch.timezone`, `launch.lang`, `launch.extra_args`;
  - `launch.window` other than `normal`;
  - `restore_session=False` (Safari owns its session restore).
- `identity_id` works (Phase 3).
- Each ProfilePilot Safari profile maps to **one Safari Profile** that the user creates in Safari ▸ Settings ▸
  Profiles and enables the extension in. ProfilePilot cannot create Safari profiles; setup explains how (§9).

### 5.2 Pairing
1. The extension instance creates `{instance, secret}` once (`crypto.getRandomValues`) and keeps it in its own
   `storage.local` (F3).
2. Unpaired, it only answers `hello`. Its popup shows the code and "Run: `profilepilot safari pair <profile> <code>`".
3. `pair` binds the **live** unpaired instance whose code matches to the profile. It stores `{instance_id,
   profile_id, secret_sha256, paired_at, ua}`, and sends `welcome` to the instance. One instance per profile and
   one profile per instance; pairing a second time replaces the old binding after confirmation (CLI `--replace`).
4. `unpair` removes the binding and tells the instance, if connected, to forget nothing but show "unpaired".
   Deleting a profile unpairs it.

### 5.3 Running, start and stop
- **Running** means the paired instance is connected **and** reports at least one window. The bridge writes
  `profiles/<id>/runtime.json` (`RuntimeInfo` with `browser_kind="safari"`, `browser_path`, `browser_version` from the
  UA, `host_pid` = bridge pid, `state="running"`). It removes the file when the last window closes or the instance
  disconnects.
- `RuntimeManager.status()` validates a Safari `runtime.json` by bridge pid + create_time alive and the bridge
  answering `status` for that profile. Anything else is stale and removed (same rule as Chromium).
- **`start(profile, start_url=)`:**
  1. `ensure_bridge()`.
  2. If Safari is not running: `open -g -a Safari` (no activation).
  3. Wait ≤ 30 s for the paired instance to connect (F4).
  4. If it has no window: `windows.open(start_url)`.
  5. If it has windows and `start_url` is given: open it in a new tab.

  Not connected after 30 s: `ProfileNotRunningError` with "Open a window of the Safari profile '<Safari profile
  name, if known>' (File ▸ New Window), then try again". In Phase 2 this also raises a Manager help request.
- **`stop(profile)`:** `windows.close_all` for that instance only. Safari itself is never quit, so the user's own
  browsing stays untouched.
- **`window`, `offscreen`, `headless`:** refused (§5.1). The user's Safari windows stay where they are; nothing is
  moved off-screen.
- **Never steal focus:** no command activates Safari or raises a window. `tabs.activate` changes the active tab
  inside its window only. This mirrors the Chromium rule about minimized windows (DESIGN.md §3.6).

## 6. The facade (Phase 1 surface)

`SafariSession` implements what `ProfileSession` offers to tools: `key`, `label`, `profile`, `runtime`,
`context`, `downloads_dir` (= None), `is_connected`, `page()`, `tabs()`, `new_tab()`, `select_tab()`,
`close_tab()`, `locate()`, `index_of()`, `is_active()`, `drain_new_tabs()`, `drain_dialogs()` (always empty:
§6.6), `take_launch_url()`, `adopt()`, `adopt_url()`, `initial_blank_tab()`, `window_minimized()`, `setup()`,
`close()`.

`SafariPage` and friends implement **exactly** the Playwright calls that `server/tools_browser.py`,
`automation/content.py` and `automation/cookies.py` make. The implementation plan lists them, from a grep of
those modules. Any other attribute raises
`SafariUnsupportedError` naming the call. The facade raises `automation.driver.Error` / `TimeoutError` subclasses,
so `to_tool_error` and `navigation_error` keep working unchanged.

### 6.1 Locators and refs
- A locator is `{frame_id, selector, nth}`, resolved in that frame's content script by Playwright's own
  `parseSelector` + `querySelectorAll` (F10). `css`, `xpath=`, `text=`, `aria-ref=` and Playwright's `internal:*`
  selectors therefore behave as in Chrome.
- **Strictness:** like Playwright, an action on a locator that matches several elements fails with `strict`,
  listing the first matches.

### 6.2 Snapshots
- `page.aria_snapshot(mode="ai", depth, boxes)` is Playwright's own AI snapshot (F10), run per frame.
- Child frames are stitched under their `<iframe>` node (F11), with refs prefixed `f<n>` in the same way as
  Playwright, so `aria-ref=f1e3` routes to frame 1.
- `content.snapshot()`, `mask_sensitive_values()` and `subtree()` run unchanged on the result.

### 6.3 Actions (synthetic input, emulated defaults)
- **Actionability**, checked in the content script before each action (Playwright's order): attached → visible →
  stable (same box over two animation frames) → enabled → scrolled into view (`scrollIntoView({block: "center",
  inline: "center"})` only if out of view) → hit-test at the click point with `elementFromPoint`. If another
  element covers the target, the action is retried until the timeout, then fails with `not_actionable` naming the
  covering element.
- **click / dblclick / hover:** the full pointer and mouse sequence at the element's center with real
  `clientX/Y`, `buttons` and `detail`, then `focus()` where a person's click would focus. Untrusted `click`
  events still run activation behaviour (links navigate, checkboxes toggle, submit buttons submit). Popups opened
  from such clicks may be blocked (no user activation): a documented limit.
- **fill / type:**
  1. Focus the field.
  2. Select its contents (fill only).
  3. `document.execCommand("insertText", false, text)`. The resulting `beforeinput`/`input` events are trusted
     (F7).

  If the command is refused (some custom editors), fall back to Playwright's `fill` approach: the native value
  setter plus `input` and `change`. `type` inserts one character at a time with `keydown`/`keypress`/`keyup`
  around each insert, so `typing.py`'s humanized delays still apply.
- **press:** synthetic `keydown`/`keyup` with the correct `key`/`code`/`keyCode`, plus **emulated default actions**,
  because synthetic keys have none:

  | Key | Default action |
  |---|---|
  | `Enter` | In a text input: implicit submission following HTML's rules (click the form's default button if it has one, else `form.requestSubmit()` when the form allows implicit submission). In a textarea or contenteditable: a line break. On a focused link or button: activation. |
  | `Tab` / `Shift+Tab` | Sequential focus navigation |
  | `Backspace` / `Delete` | `execCommand("delete")` / `execCommand("forwardDelete")` |
  | Arrows, `Home`, `End` | Caret moves in text fields; option change in `<select>` |
  | `Space` | Activates the focused button, checkbox or radio |
  | `Meta+A` / `Control+A` | Select all |

  Paste chords are refused, as on Chrome (`browser_press_key`).
- **select_option / check:** Playwright's semantics: select by value or label, set the state, dispatch `input` +
  `change`. Playwright does the same on Chrome.
- **scroll:** `scrollBy` on the page or on the element under the given point. `mouse.wheel` maps to the same.

### 6.4 Navigation and waiting
- `goto`/`reload`/`back`/`forward` go through the background. `wait_until` maps to `webNavigation` events:
  - `commit` → `onCommitted`;
  - `domcontentloaded` → `onDOMContentLoaded`;
  - `load` → `onCompleted`;
  - `networkidle` → `onCompleted` followed by 500 ms with no new resource entries (`PerformanceObserver` in
    the content script).

  These are documented as approximations.
- `page.url` and `title()` come from `tabs.get`.
- `wait_for_load_state`, `wait_for_url`, `wait_for_selector` and `wait_for_function` poll in the content script,
  with the timeout enforced on the Python side as well.

### 6.5 Evaluate, screenshots, cookies
- **`page.evaluate(js, arg)` / `frame.evaluate` / `locator.evaluate`** run in the content-script world (F5), like
  patchright's isolated-world default. Functions get `arg` and are awaited. Results are JSON (DOM nodes come back as
  `SafariElementHandle` ids that live in the content script). `browser_evaluate(world="main")` uses `eval.main` (F6).
- **Screenshots:** viewport and element screenshots via `tabs.capture`, cropped in the background canvas.
  `full_page=True` returns the viewport plus a note: "full-page capture isn't available on Safari profiles; scroll
  and capture, or use browser_read".
- **Cookies:** `SafariContext.cookies(urls)`, `add_cookies`, `clear_cookies(name/domain/path)` map to `cookies.*`
  (F8), so the cookies tools work unchanged, including import/export files.

### 6.6 What a Safari profile cannot do in Phase 1
Each refusal is one `SafariUnsupportedError` sentence written for the model to act on:

| Feature | Message (short form) |
|---|---|
| Proxy, relay stats, live proxy switch | Safari uses the Mac's own network. |
| `browser_paste` (type-paste) | Use browser_type. Type-paste needs Chrome's trusted paste. |
| `http_fetch`, Scrapling, `cdp_url` hand-out | Chrome only. |
| `full_page` screenshots | Viewport plus note (§6.5). |
| `world="main"` evaluate when F6 is false | Refused. |
| Timezone or language override | Refused at profile level (§5.1). |
| Downloads into the profile's folder | Safari saves to the user's Downloads folder; the tool reports the file name only. |

**JS dialogs.** Safari gives extensions no way to see or answer `alert`/`confirm`/`prompt`/`beforeunload`, and
patching `window.alert` would be page-visible (rule 1: native first). A command that times out on a tab whose
document stopped responding is reported as `dialog`: "The page in tab N may be showing a dialog. Ask the user to
answer it (profile_request_help), then retry." `drain_dialogs()` returns nothing.

## 7. Guard for Chrome-only code paths

`require_chromium(session, feature)` lives in `server/app.py`. It is called at the top of every tool or code path
that needs CDP or the relay. The implementation plan enumerates them, from a grep for `new_cdp_session`, `relay`,
`http_identity` and `clipboard`. It never hides a feature silently: the model always learns why.

## 8. Security and privacy

- **Pages cannot reach the bridge.**
  - `/ext` requires an extension `Origin` and an instance secret.
  - `/client` requires the bridge token and the absence of `Origin`.
  - The bridge binds 127.0.0.1 only. Remote use would need its own auth design (§12).
- **The extension acts only when paired,** and only on commands from the bridge.
  - It never executes anything a page sends.
  - It adds no DOM nodes, attributes, globals or `postMessage` traffic to pages.
  - Its world is isolated.
  - It declares no web-accessible resources (rule 1: native first; nothing for a page to detect beyond what
    synthetic events reveal).
- **Secrets:**
  - The instance secret never leaves the extension except in `hello`, and only its hash is stored.
  - The bridge token sits in `bridge.json` (0600), like host control tokens.
  - Payloads are never logged (§4).
  - The existing redaction (`AppState.redact`) applies to every Safari tool output unchanged, because `respond()`
    is shared.
- **Prompt injection:** the existing URL policy (`check_url`, `enforce_final_url`) and secret-page refusals run on
  the facade unchanged.

## 9. Setup UX (non-developer user)

`profilepilot safari setup` prints a checklist. It re-checks every item on each run and stops at the first open one:

1. *Build and install the app* (`scripts/build_safari.py`, done for the user by this command when Xcode is
   present). It checks that `~/Applications/ProfilePilot for Safari.app` exists.
2. *Show developer features* (Safari ▸ Settings ▸ Advanced) and *Allow unsigned extensions* (Settings ▸
   Developer). This is needed after every Safari launch until the app is signed (F12). There is no automatic
   check here; step 4's check confirms it.
3. *Create a Safari profile* for each ProfilePilot Safari profile (Settings ▸ Profiles ▸ New Profile).
   **Don't use your personal profile:** the AI acts with that profile's logins.
4. *Enable "ProfilePilot" in that profile* (Settings ▸ Profiles ▸ <profile> ▸ Extensions), then *Always Allow on
   Every Website* (F13). It checks that an unpaired instance is connected to the bridge.
5. *Pair*: open the extension's toolbar popup in a window of that profile, then run the `pair` command shown.
   It checks that the profile is paired and its instance connected.

Phase 2 shows the same checklist in the Manager, with buttons instead of commands.

## 10. Later phases (outline only)

- **Phase 2, Manager:**
  - Safari profile cards using `runtime.json`.
  - Thumbnails via `tabs.capture` (only when the window is visible; otherwise the last one).
  - *Open* / *Close* (= start/stop).
  - *Take control* and help requests, unchanged (`control.py` is engine-independent).
  - A pairing panel listing live unpaired instances with their codes and a *Pair with…* button, plus the setup
    checklist.
  - `ui/cdp.py` gets a Safari branch for thumbnails and the live view.
- **Phase 3, Autofill:**
  - Extend the facade with what `automation/autofill.py` and `automation/typing.py` use: frame tree,
    `frame_element()`, element handles, `press_sequentially`, `bounding_box` per frame.
  - Card iframes (Stripe) work through `all_frames` content scripts.
  - Sensitive values are sent only to the one target frame, never logged, and never kept in the content script
    after the fill.
  - The type-paste method is unavailable; `method="type"` is used.
  - The `form_autofill_sensitive` approval flow is unchanged.

## 11. Testing

- **Fast tests, every OS, CI** (pure Python, fake extension and fake clients over real WebSockets):
  - `test_safari_protocol.py`: models and the size limits.
  - `test_safari_bridge.py`:
    - `Origin` rules on both endpoints;
    - token check;
    - hello within 5 s;
    - pairing by code;
    - wrong secret → 4401;
    - one connection per instance;
    - routing with several clients;
    - event fan-out;
    - an instance disconnect fails in-flight commands at once;
    - `runtime.json` written and removed;
    - **no payload or secret in the logs** (caplog).
  - `test_safari_runtime.py`: start, stop and status branches with a fake bridge; the refusals of §5.1; `auto`
    never picks Safari; `find_browser("safari")` off macOS.
  - `test_safari_facade.py`: facade calls → exact protocol messages; error mapping to driver errors; strictness;
    unsupported attributes raise `SafariUnsupportedError`.
- **`chrome` tests:** `test_safari_content_js.py` loads `content.js` + `vendor/injected.js` into Chromium pages
  through the existing fixtures. The fixture generates `injected.js` with the build script's extraction function,
  which is plain Python. It checks actionability, the event sequences, the emulated default actions of
  §6.3, `execCommand` text entry and snapshot stitching. These are standard DOM; WebKit differences are caught by
  the next group.
- **`safari` tests:** a new marker; opt-in `-m safari`, macOS only, needs a paired Safari profile named
  `ProfilePilot Test`.
  - They run every Phase 1 tool against the local test server used by the Chrome tests, and check F1–F13 do not
    regress.
  - They never touch other Safari profiles (rule 4: leave the user's machine alone): every test asserts it acts
    on the test profile's instance only.
- **CI:** a `macos-latest` job runs `scripts/build_safari.py --no-sign` so the Xcode project keeps building.

## 12. Out of scope (later)

- Remote use from the Windows PC: authenticated bridge access over Tailscale, and a `safari:` profile ref in
  another machine's ProfilePilot.
- Trusted OS-level input (CGEvent) when the window may come to the front.
- Developer ID signing and notarization.
- Full-page screenshots.
- `http_fetch` with the Safari identity.
- Per-profile proxies, which Safari cannot do.
