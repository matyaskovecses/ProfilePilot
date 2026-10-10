# WebKit engine, Phase 1a: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A ProfilePilot profile with `browser="webkit"` runs in ProfilePilot's own WebKit app (one process per profile, its own store and proxy, introducing itself as the installed Safari), and the Phase 1a tools work on it: navigate, tabs, snapshot, read, extract, click, hover, type (fill/type/human), press_key, wait_for, screenshot and evaluate.

**Architecture:**
- **App:** a Swift `ppwebkit` app (sources in the Python package, built on the user's Mac by `profilepilot webkit install`) hosts WKWebViews. It answers a token-protected JSON-RPC WebSocket on 127.0.0.1.
- **Host:** `WebKitHost` reuses the existing host (lock, relay, control API, `runtime.json`) and replaces only the browser process.
- **Facade:** a Playwright-shaped facade (`WebKitSession`/`WebKitPage`/…) lets the existing tools, `content.py` and `typing.py` run unchanged.

**Tech Stack:** Python 3.10+ (pydantic, websockets ≥ 15, psutil, already dependencies), Swift 5 mode via `swiftc` (WebKit, AppKit, Network frameworks), Playwright 1.63's injected script (Apache-2.0), pytest + pytest-asyncio.

**Spec:** `docs/design/SAFARI.md` (Phase 1a row of §0). Measured facts: `docs/design/SAFARI-FACTS.md` (W#). Open verifications: SAFARI.md §12 (V#).

**Before Task 1:** rebase `safari-engine` onto `origin/main` once the Windows session has pushed its in-flight work (cookie manager, `cli.py`, test fixes). Then run the fast suite once to know the baseline.

## Global Constraints

- **Platform:**
  - The engine runs on macOS ≥ 14 only.
  - Every Python module imports cleanly on Windows and Linux. No macOS-only import at module level; Swift is never needed off macOS.
- **Dependencies:** no new Python dependencies. The app is built with `swiftc -swift-version 5`, ad-hoc signed (`codesign -s - --force`). **No binary is ever committed.**
- **Names:**

  | Item | Value |
  |---|---|
  | kind | `webkit` |
  | label | `Safari (WebKit)` |
  | bundle id | `dev.profilepilot.webkit` |
  | app | `ProfilePilot WebKit.app` |
  | executable | `ppwebkit` |
  | installed to | `<data root>/apps/` |
  | test seam | env `PROFILEPILOT_WEBKIT_APP` (a JSON argv list or a path) |

- **`auto`** never picks `webkit`. `webkit` is not added to `paths.BROWSER_KINDS`.
- **User agent:** `applicationNameForUserAgent = "Version/<Safari CFBundleShortVersionString> Safari/605.1.15"`, only when the loaded WebKit's `CFBundleVersion` equals Safari.app's (V11). Otherwise there is no Safari claim, and the mismatch is reported.
- **Secrets:**
  - The token and the launch config go to the app **only via stdin** (one JSON line), never argv or environment.
  - Logs carry method names, ids, durations and error types, never payloads (cookies, typed text, evaluated source, results).
- **Isolation:**
  - Every ProfilePilot script runs in `WKContentWorld.world(name: "profilepilot")`; there is no page-world user script or handler.
  - The page world is used only for `evaluate(…, isolated_context=False)`.
  - Every evaluation runs without a user gesture (SPI `forceUserGesture: NO`).
- **Automation server:** binds 127.0.0.1 only, requires header `X-ProfilePilot-Token`, and rejects any upgrade request carrying `Origin`.
- **Timeouts:** 30 s per command by default; 45 s host readiness wait.
- **Errors:**
  - Facade errors subclass `automation.driver.Error[0]` / `TimeoutError[0]`.
  - Refusals are `EngineUnsupportedError` with one sentence written for the model.
- **WebRTC:** `launch.webrtc` `auto`/`proxy_only` → `PeerConnectionEnabled` off when proxied; `default` → always on.
- **Window:**
  - `headless` set on a WebKit profile is refused.
  - A global or per-start `headless` runs as `offscreen`.
  - `offscreen` = `OffscreenWindow` at (−32000, −32000) with occlusion detection off.
- **Test marker:** `webkit` (macOS, opt-in). It is excluded from the default and the CI fast suite.
- **Do not touch** (the Windows session's files): `ui/*`, `control.py`, `server/app.py`, `server/tools_control.py`, `server/oauth.py`, `server/http.py`, `connect.py`, `automation/cookies.py`, `browser/cookiejar.py`, `browser/devtools.py`. `cli.py`: only the two small hunks named below.
- **Commits** end with the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A page that calls `alert()`/`confirm()` while loading:** the navigation and the next tool call finish; nothing waits for a human. Test: Task 7 `test_dialog_at_load_does_not_block`.
2. **A URL that downloads instead of rendering:** `browser_navigate` fails fast with a message containing "Download is starting" (so `navigation_error` explains it), not a timeout. Test: Task 7 `test_download_navigation_fails_fast`.
3. **A proxied profile whose upstream is dead:** the navigation error contains one of `ERR_PROXY`/`ERR_SOCKS`/`ERR_TUNNEL`/`ERR_TIMED_OUT`/`ERR_EMPTY_RESPONSE`, so `navigation_error` adds the relay hint. Test: Task 7 `test_dead_upstream_names_a_proxy_error`.
4. **The app dies mid-command (crash, kill, sleep/wake):** in-flight calls fail within 2 s, and the profile's status turns "not running"; nothing hangs. Tests: Task 4 `test_inflight_calls_fail_when_app_dies`, Task 6 `test_killed_app_clears_runtime`.
5. **Non-ASCII/emoji text, and elements inside cross-origin iframes:** typed text lands exactly, and clicks hit the element inside the right iframe. Tests: Task 8 `test_insert_text_unicode`, `test_cross_origin_frame_click`.

---

### Task 1: The `webkit` kind, runtime fields and errors

**Files:**
- Create: `src/profilepilot/webkit/__init__.py`
- Modify: `src/profilepilot/models.py` (`RuntimeInfo`, `Profile.browser` docstring), `src/profilepilot/errors.py`, `src/profilepilot/paths.py` (`BROWSER_LABELS`, `find_browser`, `list_browsers`, the "Not supported" comment)
- Test: `tests/test_webkit_kind.py`

**Interfaces:**
- Produces, in `profilepilot.webkit`:
  - constants `WEBKIT_KIND = "webkit"`, `WEBKIT_LABEL = "Safari (WebKit)"`, `BUNDLE_ID`, `APP_NAME`, `EXECUTABLE`, `ENV_APP`;
  - `store_root() -> Path` (`~/Library/WebKit/dev.profilepilot.webkit/WebsiteDataStore`);
  - `app_bundle(root: Path) -> Path`;
  - `app_argv(root: Path) -> list[str]`: the `ENV_APP` JSON list, the `ENV_APP` path as `[path]`, else `[<bundle>/Contents/MacOS/ppwebkit]`;
  - `safari_version() -> str | None` and `safari_build() -> str | None`, from `/Applications/Safari.app/Contents/Info.plist` via `plistlib`;
  - `macos_version() -> tuple[int, int] | None`;
  - `webkit_browser_info(root: Path | None = None) -> BrowserInfo`.
- Produces in `models.py`: `RuntimeInfo.engine: Literal["chromium", "webkit"] = "chromium"`, `RuntimeInfo.automation_url: str | None = None`, `RuntimeInfo.automation_token: str | None = None`. `public()` excludes `automation_token`.
- Produces in `errors.py`: `class EngineUnsupportedError(ProfilePilotError)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_runtime_info_public_hides_automation_token():
    info = RuntimeInfo(profile_id="abcd1234", profile_name="x", host_pid=1, engine="webkit",
                       automation_url="ws://127.0.0.1:5/", automation_token="s3cret")
    pub = info.public()
    assert pub["engine"] == "webkit" and "automation_token" not in pub and "s3cret" not in json.dumps(pub)

def test_auto_never_picks_webkit(monkeypatch):
    monkeypatch.setattr(paths, "_candidates", lambda: {})
    monkeypatch.setenv("PROFILEPILOT_WEBKIT_APP", json.dumps([sys.executable, "x.py"]))
    assert "webkit" not in paths.BROWSER_KINDS
    with pytest.raises(BrowserNotFoundError):
        paths.find_browser("auto")

def test_webkit_needs_macos(monkeypatch):
    monkeypatch.delenv("PROFILEPILOT_WEBKIT_APP", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(BrowserNotFoundError, match="WebKit profiles need macOS 14 or later"):
        paths.find_browser("webkit")

def test_webkit_not_built(monkeypatch, tmp_path):
    monkeypatch.delenv("PROFILEPILOT_WEBKIT_APP", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(webkit, "macos_version", lambda: (26, 3))
    monkeypatch.setattr(webkit, "safari_version", lambda: "26.3")
    with pytest.raises(BrowserNotFoundError, match="profilepilot webkit install"):
        webkit.webkit_browser_info(tmp_path)

def test_env_override_is_a_test_seam(monkeypatch, tmp_path):
    monkeypatch.setenv("PROFILEPILOT_WEBKIT_APP", json.dumps([sys.executable, "fake.py"]))
    info = paths.find_browser("webkit")
    assert info.kind == "webkit" and info.label == "Safari (WebKit)"
    assert webkit.app_argv(tmp_path) == [sys.executable, "fake.py"]
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_webkit_kind.py -q`
Expected: FAIL (`ModuleNotFoundError: profilepilot.webkit`)

- [ ] **Step 3: Implement**
  - **`webkit/__init__.py`:** the constants and functions above.
  - **`webkit_browser_info` error sentences, checked in this order:**
    1. "WebKit profiles need macOS 14 or later (they run Apple's WebKit, the engine of Safari)."
    2. "Safari is not installed in /Applications; WebKit profiles take Safari's version for their identity."
    3. "The ProfilePilot WebKit app is not built yet. Run: profilepilot webkit install"

    With `ENV_APP` set, all three checks are skipped. The version is `safari_version() or "26.0"`.
  - **`find_browser`:** `pref == "webkit"` returns `webkit_browser_info()`, with a lazy import.
  - **`list_browsers`:** appends that info only on darwin, when it doesn't raise.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_webkit_kind.py -q` → PASS. Then run the fast suite (`-m "not chrome and not network"`) and confirm it still passes.

- [ ] **Step 5: Commit**: `git add src/profilepilot/webkit/__init__.py src/profilepilot/models.py src/profilepilot/errors.py src/profilepilot/paths.py tests/test_webkit_kind.py && git commit -m "WebKit: browser kind, runtime fields and EngineUnsupportedError"`

---

### Task 2: WebKit profiles in the store and the profile tools

**Files:**
- Create: `src/profilepilot/webkit/profiles.py`
- Modify:
  - `src/profilepilot/store.py`:
    - `create_profile`, `update_profile`: call `check_webkit_profile`, and `ensure_store_uuid` after the write;
    - `clone_profile`: refuse `copy_data`;
    - `purge_trash`: remove WebKit stores, including orphaned dirs.
  - `src/profilepilot/server/tools_profiles.py`:
    - add `"webkit"` to `BrowserArg`'s `Literal` and description;
    - `browser_list` adds the WebKit line.
- Test: `tests/test_webkit_profiles.py`

**Interfaces:**
- Consumes: `WEBKIT_KIND`, `store_root()`, `app_argv()` (Task 1).
- Produces:
  - `check_webkit_profile(profile: Profile, *, window_explicit: bool) -> Profile`;
  - `WEBKIT_FILE = "webkit.json"`;
  - `ensure_store_uuid(profile_dir: Path) -> str`;
  - `read_store_uuid(profile_dir: Path) -> str | None`;
  - `store_dir(uuid: str) -> Path`;
  - `remove_store(uuid: str, argv: list[str] | None) -> bool`;
  - `UUID_RE` (lowercase canonical UUID).

**Refusal sentences** (exact; `ProfilePilotError`):

| Setting | Message |
|---|---|
| timezone | "WebKit profiles cannot override the timezone (WebKit has no per-page timezone setting). Remove launch.timezone." |
| extra_args | "WebKit profiles take no browser switches; launch.extra_args only applies to Chrome-family browsers." |
| lang (until 1b) | "Language overrides for WebKit profiles are not supported yet. Remove launch.lang." |
| headless, explicit | "WebKit profiles have no headless mode. Use window='offscreen': the window stays off-screen but the page keeps running like a visible one." |
| clone with data | "Copying the browser data of a WebKit profile is not supported; clone it without data (fresh cookies and logins)." |

A non-explicit `headless` (the global default, or a stored value from before the switch to webkit) becomes
`offscreen` silently.

- [ ] **Step 1: Write the failing tests**

```python
def test_create_writes_store_uuid(store):
    p = store.create_profile("wk", browser="webkit")
    assert UUID_RE.fullmatch(read_store_uuid(store.profile_dir(p.id)))

@pytest.mark.parametrize("launch, words", [({"timezone": "Europe/Berlin"}, "timezone"),
    ({"extra_args": ["--x"]}, "browser switches"), ({"lang": "de-DE"}, "Language overrides"),
    ({"window": "headless"}, "no headless mode")])
def test_refusals(store, launch, words):
    with pytest.raises(ProfilePilotError, match=words):
        store.create_profile("wk", browser="webkit", launch=launch)

def test_global_headless_means_offscreen(store):
    cfg = store.load_config(); cfg.default_window = "headless"; store.save_config(cfg)
    assert store.create_profile("wk", browser="webkit").launch.window == "offscreen"

def test_switch_to_webkit_is_checked(store):
    p = store.create_profile("c", launch={"timezone": "Europe/Berlin"})
    with pytest.raises(ProfilePilotError, match="timezone"):
        store.update_profile(p.id, browser="webkit")
    store.update_profile(p.id, launch={"timezone": None})
    assert store.update_profile(p.id, browser="webkit").browser == "webkit"
    assert read_store_uuid(store.profile_dir(p.id))

def test_clone(store):
    p = store.create_profile("wk", browser="webkit")
    with pytest.raises(ProfilePilotError, match="not supported"):
        store.clone_profile(p.id, "wk2", copy_data=True)
    c = store.clone_profile(p.id, "wk3")
    assert read_store_uuid(store.profile_dir(c.id)) != read_store_uuid(store.profile_dir(p.id))

def test_purge_removes_store_without_app(store, tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "store_root", lambda: tmp_path / "stores")
    p = store.create_profile("wk", browser="webkit")
    d = profiles.store_dir(read_store_uuid(store.profile_dir(p.id))); d.mkdir(parents=True); (d / "x").write_text("1")
    entry = store.delete_profile(p.id)
    assert d.exists()                                   # trash keeps the store (restorable)
    store.purge_trash(older_than_days=0)
    assert not d.exists() and not (store.trash_dir / entry.trash_id).exists()

def test_restore_keeps_uuid(store):
    p = store.create_profile("wk", browser="webkit")
    before = read_store_uuid(store.profile_dir(p.id))
    restored = store.restore_profile(store.delete_profile(p.id).trash_id)
    assert read_store_uuid(store.profile_dir(restored.id)) == before
def test_remove_store_rejects_bad_uuid(tmp_path):
    with pytest.raises(ValueError):
        profiles.remove_store("../../etc", None)

def test_browser_arg_lists_webkit():
    assert "webkit" in typing.get_args(typing.get_args(BrowserArg)[0].__args__[0])
```

(The last assertion unwraps `Annotated[Literal[...] | None, ...]`; adjust the unwrapping to the real typing structure and
keep the assertion.)

- [ ] **Step 2: Run them and confirm they fail.** Run: `.venv/bin/python -m pytest tests/test_webkit_profiles.py -q` → FAIL

- [ ] **Step 3: Implement**
  - **`profiles.py`:**
    - `check_webkit_profile` returns a copy with `launch.window` coerced as above.
    - `ensure_store_uuid` writes `{"store_uuid": str(uuid4())}` with `write_json`.
    - `remove_store` validates `UUID_RE`. If `argv` is given, it runs `[*argv, "--remove-store", uuid]` (timeout 30 s), then `shutil.rmtree(store_dir(uuid), ignore_errors=True)`. It returns `not store_dir(uuid).exists()`.
  - **`store.py`:** `window_explicit` is True when `launch` was passed with a `window` key (or a `LaunchOptions` whose `model_fields_set` contains `window`), or when an update's `launch` patch has `window`. `purge_trash` reads `webkit.json` from each trash dir it removes, and calls `remove_store(uuid, app_argv(self.root) if built else None)`.
  - **`browser_list`:** adds the line `- webkit: Safari (WebKit) <version>: ProfilePilot's own WebKit app (Safari's engine); it introduces itself to sites as Safari <version>`.

- [ ] **Step 4: Run them and confirm they pass**, then the fast suite.

- [ ] **Step 5: Commit**: `git commit -m "WebKit: profile checks, store UUIDs and purge"` (the files above)

---

### Task 3: Wire format: launch config, wire errors, macOS key strokes

**Files:**
- Create: `src/profilepilot/webkit/protocol.py`, `src/profilepilot/webkit/keys.py`
- Test: `tests/test_webkit_protocol.py`, `tests/test_webkit_keys.py`

**Interfaces:**
- Produces in `protocol.py`:
  - `class ProxyConf(BaseModel)`: `type: Literal["socks5"] = "socks5"`, `host: str`, `port: int`.
  - `class LaunchConfig(BaseModel)`: the exact fields of SAFARI.md §2.1, plus `def line(self) -> bytes` (compact JSON + `b"\n"`).
  - `class WebKitError(driver.Error[0])` and `class WebKitTimeoutError(driver.TimeoutError[0])`.
  - `ERROR_TYPES = frozenset({"timeout", "no_tab", "no_frame", "navigation", "js", "unsupported", "bad_request", "not_actionable", "strict", "stale_ref", "dialog"})`.
  - `def wire_error(error: dict, method: str) -> Exception`: `timeout` → `WebKitTimeoutError`, `unsupported` → `EngineUnsupportedError`, anything else → `WebKitError`. The message is the wire message.
- Produces in `keys.py`:
  - `MOD_SHIFT = 1 << 17`, `MOD_CONTROL = 1 << 18`, `MOD_ALT = 1 << 19`, `MOD_META = 1 << 20` (NSEvent.ModifierFlags raw values);
  - `class KeyStroke(NamedTuple)`: `key_code: int`, `chars: str`, `chars_unmodified: str`, `modifiers: int`, `modifier_codes: tuple[int, ...]`;
  - `def parse_chord(key: str) -> tuple[frozenset[str], str]`;
  - `def stroke(key: str) -> KeyStroke`;
  - `def char_stroke(ch: str) -> KeyStroke | None`.

**Key data** (macOS `kVK_*` virtual key codes, US layout):

- **Named keys** (code, chars):

  | Key | Code | Chars |
  |---|---|---|
  | Enter | 36 | `\r` |
  | Tab | 48 | `\t` |
  | Backspace | 51 | `\x7f` |
  | Escape | 53 | `\x1b` |
  | Space (and `" "`) | 49 | `" "` |
  | Delete | 117 | `` |
  | Home | 115 | `` |
  | End | 119 | `` |
  | PageUp | 116 | `` |
  | PageDown | 121 | `` |
  | ArrowUp | 126 | `` |
  | ArrowDown | 125 | `` |
  | ArrowLeft | 123 | `` |
  | ArrowRight | 124 | `` |
  | F1–F12 | 122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111 | ``+n−1 |

- **Characters:**
  - **Letters:** a0 s1 d2 f3 h4 g5 z6 x7 c8 v9 b11 q12 w13 e14 r15 y16 t17 o31 u32 i34 p35 l37 j38 k40 n45 m46.
  - **Digits:** 1→18 2→19 3→20 4→21 6→22 5→23 9→25 7→26 8→28 0→29.
  - **Punctuation:** `=` 24, `-` 27, `]` 30, `[` 33, `'` 39, `;` 41, `\` 42, `,` 43, `/` 44, `.` 47, `` ` `` 50.
  - **Shifted forms** use the same codes: `!@#$%^&*()` on 1–0, `_+{}|:"<>?~` on their base keys.
- **Modifier key codes:** Shift 56, Control 59, Alt 58, Meta 55.
- **Chord rules:**
  - Modifier names are case-insensitive. `ControlOrMeta` → Meta, `Option` → Alt, `Cmd`/`Command` → Meta.
  - `"Control++"` means the `+` key.
  - With Control and no Meta, `chars` for a letter is the control character (`chr(ord(upper) - 64)`). Otherwise it's the printed character.
  - `chars_unmodified` is always the unshifted character.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_chord():
    assert parse_chord("Control+Shift+A") == (frozenset({"Control", "Shift"}), "A")
    assert parse_chord("Control++") == (frozenset({"Control"}), "+")
    assert parse_chord("ControlOrMeta+a") == (frozenset({"Meta"}), "a")
    assert parse_chord("shift+Tab") == (frozenset({"Shift"}), "Tab")

def test_named_and_chords():
    assert stroke("Enter") == KeyStroke(36, "\r", "\r", 0, ())
    assert stroke("ArrowDown").chars == ""
    assert stroke("Meta+a") == KeyStroke(0, "a", "a", MOD_META, (55,))
    assert stroke("Control+a").chars == "\x01"
    with pytest.raises(ValueError, match="Unknown key"):
        stroke("Hyper+Q")

def test_char_stroke():
    assert char_stroke("A") == KeyStroke(0, "A", "a", MOD_SHIFT, (56,))
    assert char_stroke("!") == KeyStroke(18, "!", "1", MOD_SHIFT, (56,))
    assert char_stroke("\n") == stroke("Enter") and char_stroke("é") is None

def test_launch_config_line():
    line = LaunchConfig(**SAMPLE).line()
    assert line.endswith(b"\n") and json.loads(line) == LaunchConfig(**SAMPLE).model_dump()

def test_wire_errors():
    assert isinstance(wire_error({"type": "timeout", "message": "m"}, "x"), driver.TimeoutError)
    assert isinstance(wire_error({"type": "js", "message": "m"}, "x"), driver.Error)
    assert isinstance(wire_error({"type": "unsupported", "message": "m"}, "x"), EngineUnsupportedError)
```

- [ ] **Step 2: Run and confirm they fail.** `.venv/bin/python -m pytest tests/test_webkit_protocol.py tests/test_webkit_keys.py -q`
- [ ] **Step 3: Implement** the two modules (pure Python; tables as module constants).
- [ ] **Step 4: Run and confirm they pass.**
- [ ] **Step 5: Commit**: `git commit -m "WebKit: launch config, wire errors and macOS key strokes"`

---

### Task 4: Automation client and the fake app

**Files:**
- Create: `src/profilepilot/webkit/client.py`, `tests/fake_webkit_app.py`, `tests/webkit_helper.py` (a fixture that starts the fake or the real app with a `LaunchConfig`)
- Test: `tests/test_webkit_client.py`

**Interfaces:**
- Consumes: `LaunchConfig`, `wire_error`, `WebKitError`, `WebKitTimeoutError` (Task 3).
- Produces:
  - `class AutomationClient`:
    - `@classmethod async def open(cls, url: str, token: str, *, timeout: float = 10.0) -> AutomationClient`;
    - `async def call(self, method: str, params: dict | None = None, *, timeout: float = 30.0) -> Any`;
    - `def on_event(self, callback: Callable[[str, dict], None]) -> None`;
    - `connected: bool` (property);
    - `async def close(self) -> None`.
  - `def call_sync(url: str, token: str, method: str, params: dict | None = None, *, timeout: float = 5.0) -> Any`.
- Produces (tests): `tests/fake_webkit_app.py`.
  - **Startup:** reads one `LaunchConfig` line from stdin, then serves `ws://127.0.0.1:<automation_port>/`. Startup itself is logged (argv, env keys, config), so the "nothing in argv" assertions in Task 6 can be made.
  - **Security:** checks `X-ProfilePilot-Token` and rejects `Origin` with HTTP 403.
  - **Methods:**
    - `browser.version`: `{"product": "FakeWebKit/1", "safari_version": cfg.safari_version, "spi": {five keys: true}, "ua_claim": true}`;
    - `browser.close`: exits 0;
    - `tabs.list/create/activate/close` (in memory, ids `t1`, `t2`…; `create` emits `tab.created`);
    - `page.navigate`: `{"url", "status": 200}`;
    - `frames.list`: main frame only, unless `FAKE_WEBKIT_FRAMES` is set;
    - `js.evaluate`: answers from the JSON file in `FAKE_WEBKIT_EVAL` (a list of `[substring of body, value]` rules; first match wins);
    - `input.*`;
    - `page.screenshot`: base64 of `b"\xff\xd8fake"`;
    - `hang`: never answers.
  - **Logging:** every request goes to the JSONL file in `FAKE_WEBKIT_LOG`.
  - **Exit:** on stdin EOF. `FAKE_WEBKIT_EXIT_EARLY=<code>` exits with that code before serving.

- [ ] **Step 1: Write the failing tests**:
  - `test_roundtrip`;
  - `test_wrong_token_rejected` (`open` raises `WebKitError`);
  - `test_origin_rejected` (`websockets` connect with `origin="https://evil.example"` raises);
  - `test_call_timeout` (`call("hang", timeout=0.3)` raises `WebKitTimeoutError`);
  - `test_inflight_calls_fail_when_app_dies` (start `hang`, `terminate()` the fake; the call raises `WebKitError` within 2 s);
  - `test_events_delivered` (`tabs.create` → callback gets `("tab.created", {...})`);
  - `test_call_sync`;
  - `test_fake_exits_on_stdin_eof` (closing stdin → exit within 3 s).
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement**
  - **`client.py`:**
    - Uses `websockets.asyncio.client.connect(url, additional_headers={"X-ProfilePilot-Token": token}, origin=None, max_size=64 * 2**20)`.
    - A reader task resolves futures by `id` and dispatches events.
    - On close, it fails every pending future with `WebKitError("The WebKit app closed the connection.")`.
    - `call_sync` uses `websockets.sync.client.connect` with the same header.
  - **Fake app:** asyncio + `websockets.asyncio.server.serve` with `process_request` for the header checks.
- [ ] **Step 4: Run and confirm they pass**, on this Mac, then the fast suite.
- [ ] **Step 5: Commit**: `git commit -m "WebKit: automation client and fake app for tests"`

---

### Task 5: The Swift app core, the build and `profilepilot webkit`

**Files:**
- Create Swift sources in `src/profilepilot/webkit/app/`:
  - `main.swift`: argv modes, stdin config, EOF exit, `.accessory` policy;
  - `Config.swift`: `LaunchConfig: Decodable`, snake_case keys;
  - `Automation.swift`: `AutomationServer` + `Dispatcher`;
  - `SPI.swift`: guarded SPI helpers + report;
  - `Log.swift`: payload-free log to `config.log_file`.
- Create Python:
  - `src/profilepilot/webkit/build.py`: `SWIFT_SOURCES`, `info_plist`, `build_app`, `needs_build`, `webkit_status`, `WebKitBuildError`;
  - `src/profilepilot/webkit/cli.py`: `register(sub, common)`, `webkit install [--force]`, `webkit status [--json]`.
- Modify:
  - `src/profilepilot/cli.py`: **one hunk**, `from .webkit.cli import register as _register_webkit; _register_webkit(sub, common)` next to the other groups.
  - `pyproject.toml`: marker `"webkit: drives the ProfilePilot WebKit app on macOS (slow; run with -m webkit)"`, `addopts = "-m 'not network and not webkit'"`.
  - `.github/workflows/ci.yml`: the fast step uses `-m "not chrome and not network and not webkit"`.
- Test: `tests/test_webkit_build.py` (fast), `tests/test_webkit_app.py` (`webkit` marker; module fixture builds the app once into a temp root)

**Interfaces:**
- Consumes: `LaunchConfig` (Task 3), `call_sync` (Task 4), `safari_version`/`safari_build`/`macos_version` (Task 1).
- Produces (Python):
  - `build_app(root: Path, *, force: bool = False) -> Path` returns the bundle path. It builds into `<root>/apps/.build-<pid>` and replaces `<root>/apps/ProfilePilot WebKit.app` atomically, then writes `<root>/apps/webkit-build.json` = `{"package_version", "playwright_version", "built_at"}`.
  - `needs_build(root: Path) -> bool` (missing app, or a different `package_version`).
  - `webkit_status(root: Path) -> dict` with keys `built`, `app_version`, `package_version`, `safari_version`, `macos`, `spi`, `stores`.
  - `RESOURCE_FILES: list[tuple[str, Callable[[], bytes]]]`, which Task 9 extends.
- Produces (app):
  - `ppwebkit --spi-report` prints `{"features", "occlusion", "window_frame", "frame_tree", "eval_no_gesture", "webkit_build", "safari_build", "safari_version"}` and exits 0.
  - `ppwebkit --remove-store <uuid>` → `WKWebsiteDataStore.remove(forIdentifier:)`; exit 0 on success.
  - The normal mode serves `browser.version` (`{product, app_version, safari_version, ua_claim, spi}`) and `browser.close`.
- `Dispatcher` registration API for later Swift files: `dispatcher.register("tabs.list") { params in ... }` with `async throws -> Any` handlers that run on the main actor.
- Wire errors are thrown as `RPCError(type:message:)`.

**Pinned details:**
- **`Info.plist`:**
  - `CFBundleIdentifier` `dev.profilepilot.webkit`, `CFBundleExecutable` `ppwebkit`, `CFBundleName` `ProfilePilot WebKit`, `CFBundlePackageType` `APPL`;
  - `CFBundleShortVersionString` = package version, `LSMinimumSystemVersion` `14.0`, `LSUIElement` true;
  - `NSAppTransportSecurity.NSAllowsArbitraryLoads` true;
  - `NSCameraUsageDescription` / `NSMicrophoneUsageDescription` = "A website in a ProfilePilot WebKit profile asked for the camera/microphone; ProfilePilot denies it."
- **Compile command:** `swiftc -swift-version 5 -O -framework WebKit -framework AppKit -framework Network -o <MacOS>/ppwebkit <all SWIFT_SOURCES>`. Find `swiftc` with `xcrun --find swiftc`, falling back to `shutil.which("swiftc")`. When it's missing: `WebKitBuildError("Building the WebKit app needs Apple's Swift compiler: install Xcode or run 'xcode-select --install'.")`.
- **Signing:** `codesign -s - --force <bundle>`.
- **Server:**
  - `NWListener` with `requiredLocalEndpoint` 127.0.0.1:port and `NWProtocolWebSocket.Options`: `autoReplyPing = true`, `maximumMessageSize = 64 MiB`.
  - `setClientRequestHandler` accepts only when the header token matches (constant-time compare) **and** there's no `Origin` header.
  - Messages are decoded with `JSONSerialization`; replies and events are broadcast as in SAFARI.md §3.
- **Stdin:** after the config line, a background thread blocks in `readDataToEndOfFile()`; on return → `DispatchQueue.main.async { NSApp.terminate(nil) }`.
- **SPI detection:**
  - `features`: `WKPreferences` responds to `_features` and `_setEnabled:forFeature:`;
  - `occlusion`: `WKWebView` instances respond to `_setWindowOcclusionDetectionEnabled:`;
  - `frame_tree`: responds to `_frames:`, and `WKFrameInfo` to `_handle`;
  - `eval_no_gesture`: responds to `_evaluateJavaScript:asAsyncFunction:withSourceURL:withArguments:forceUserGesture:inFrame:inWorld:completionHandler:`;
  - `window_frame`: `protocol_getMethodDescription(objc_getProtocol("WKUIDelegatePrivate"), sel("_webView:getWindowFrameWithCompletionHandler:"), false, true)` has a name.
- **`ua_claim`:** `webkit_build == safari_build`, comparing `Bundle(for: WKWebView.self)`'s `CFBundleVersion` with Safari.app's.

- [ ] **Step 1: Write the failing fast tests**:
  - `test_info_plist` (`plistlib.loads(info_plist("1.2.3"))`, every pinned key/value above);
  - `test_swift_sources_exist`;
  - `test_build_refuses_off_macos` (match "macOS 14");
  - `test_build_commands` (monkeypatch `subprocess.run` to record; darwin; macOS (26, 3); assert the swiftc argv contains every framework flag and every source, then a `codesign -s - --force` call);
  - `test_needs_build` (missing → True; other `package_version` → True; same → False);
  - `test_cli_registers_webkit_group` (`profilepilot webkit status --json` on a temp home prints JSON with `"built": false`).
- [ ] **Step 2: Write the failing `webkit` tests** (`pytest.mark.webkit`, skipped unless darwin):
  - `test_version_and_spi` (`browser.version.safari_version` equals `safari_version()`; every `spi` value True; `ua_claim` True on this Mac);
  - `test_token_and_origin_enforced`;
  - `test_exits_on_stdin_eof` (≤ 5 s);
  - `test_spi_report_cli`;
  - `test_remove_store_cli` (a random uuid → exit 0).
- [ ] **Step 3: Run and confirm they fail.** Fast: `.venv/bin/python -m pytest tests/test_webkit_build.py -q`. App: `.venv/bin/python -m pytest -m webkit tests/test_webkit_app.py -q`.
- [ ] **Step 4: Implement** the Swift files, `build.py`, `cli.py`, the `cli.py` hunk, `pyproject.toml` and `ci.yml`.
- [ ] **Step 5: Run both groups and confirm they pass**, then the fast suite.
- [ ] **Step 6: Commit**: `git commit -m "WebKit: Swift app core, build-from-source and the webkit CLI"`

---

### Task 6: Host and runtime integration

**Files:**
- Create: `src/profilepilot/browser/webkit_host.py`
- Modify:
  - `src/profilepilot/browser/host.py`:
    - split `ProfileHost._launch` so that everything from the browser launch down to "browser running" moves, **unchanged**, into `async def _spawn_browser(self, launch: LaunchOptions, endpoint: ProxyEndpoint | None) -> None`;
    - add a `_check_in_use()` hook (the `profile_in_use` check, Chromium only);
    - `run_host` picks `WebKitHost` when `profile.browser == "webkit"`.
  - `src/profilepilot/browser/runtime.py`: `_is_ready` branches on `info.engine == "webkit"`.
- Test: `tests/test_webkit_host.py` (all OS; uses the fake app via `PROFILEPILOT_WEBKIT_APP='[sys.executable, "tests/fake_webkit_app.py"]'`)

**Interfaces:**
- Consumes: `LaunchConfig`, `ProxyConf` (Task 3); `call_sync` (Task 4); `ensure_store_uuid` (Task 2); `app_argv` (Task 1).
- Produces:
  - `class WebKitHost(ProfileHost)` overriding `_check_in_use`, `_spawn_browser`, `_close_browser`, `_route_open`, `_route_status`;
  - `def webrtc_on(mode: WebRTCMode, proxied: bool) -> bool`;
  - `RuntimeInfo` for WebKit: `engine="webkit"`, `browser_kind="webkit"`, `chrome_pid`/`chrome_create_time` of the app process, `automation_url="ws://127.0.0.1:<port>/"`, `automation_token`, `browser_version` = `browser.version.safari_version`.

**`_spawn_browser` (WebKit):**
1. `argv = app_argv(store.root)`.
2. Allocate the port with `free_port(avoid=…)` and the token with `secrets.token_urlsafe(32)`.
3. `window = "offscreen" if launch.window == "headless" else launch.window`.
4. Build `LaunchConfig`:
   - `proxy=ProxyConf(host="127.0.0.1", port=relay.port)` iff relay;
   - `webrtc=webrtc_on(launch.webrtc, relay is not None)`;
   - `start_urls=launch_start_urls(launch, start_url, session_exists=False)`;
   - `downloads_dir=store.downloads_dir(id)`;
   - `session_file=<profile dir>/webkit-session.json`, `log_file=<profile dir>/webkit.log`;
   - `language=None`.
5. `Popen(argv, stdin=PIPE, stdout=DEVNULL, stderr=<profile dir>/webkit.stderr.log, close_fds=True)`. Write `config.line()` and flush; **keep stdin open** (`self._stdin`).
6. Poll `call_sync(url, token, "browser.version", timeout=2.0)` every 0.1 s for ≤ 45 s.
   - If the process exits first: `HostError(f"The WebKit app exited during startup (code {code}); see webkit.log.")`.
   - On timeout: `HostError("The WebKit app did not answer within 45 s; see webkit.log.")`.

**The other hooks:**
- `_close_browser`: `call_sync("browser.close", timeout=5)` → wait 10 s → `terminate()` → wait 5 s → `kill_tree`.
- `_route_open`: the same URL validation as Chromium → `call_sync("tabs.create", {"url": u, "active": True}, timeout=10)` → `{"opened": True}`.
- `_route_status`: `super()` + `{"engine": "webkit", "spi": <cached browser.version.spi>}`.

**Readiness:** `_is_ready` (WebKit) = `state == "running"` and `automation_url` and `process_alive(chrome_pid, chrome_create_time)` and `call_sync(..., "browser.version", timeout=2.0)` succeeds.

- [ ] **Step 1: Write the failing tests**:
  - `test_start_status_stop` (engine, URL, status not None; stop → True, status None, fake log has `browser.close`);
  - `test_config_only_on_stdin` (the fake logs its argv and environment: the token appears in neither; the config's `store_uuid` equals `read_store_uuid`);
  - `test_proxied_profile_uses_relay_and_no_webrtc` (a SOCKS5 proxy with credentials: config `proxy.port == info.relay_port`, `webrtc is False`, the credentials appear nowhere in the fake log);
  - `test_webrtc_default_stays_on`;
  - `test_headless_override_runs_offscreen` (`start(id, window="headless")` → config `window == "offscreen"`);
  - `test_open_url_creates_tab`;
  - `test_exit_during_startup` (`FAKE_WEBKIT_EXIT_EARLY=3` → `LaunchError` containing "exited during startup");
  - `test_killed_app_clears_runtime` (SIGKILL the fake → within 5 s `status()` is None and `runtime.json` is gone);
  - `test_host_main_exit_codes_unchanged` (re-run the existing Chromium exit-code expectations).
- [ ] **Step 2: Run and confirm they fail.** `.venv/bin/python -m pytest tests/test_webkit_host.py -q`
- [ ] **Step 3: Implement** the split (Chromium behaviour must stay byte-for-byte; `tests/test_flags.py` and `tests/test_runtime_unit.py` must stay green), then `webkit_host.py` and the `_is_ready` branch.
- [ ] **Step 4: Run** `tests/test_webkit_host.py`, `tests/test_flags.py`, `tests/test_runtime_unit.py`, then the fast suite, and confirm all pass. On this Mac, also run `.venv/bin/python -m pytest -m chrome tests/test_runtime_chrome.py -q` and confirm Chromium still passes.
- [ ] **Step 5: Commit**: `git commit -m "WebKit: host branch and runtime readiness"`

---

### Task 7: Tabs, windows, navigation, dialogs, popups and identity in the app

**Files:**
- Create Swift sources in `src/profilepilot/webkit/app/`:
  - `Profile.swift`: the configuration builder;
  - `Tabs.swift`: `TabController`, the tabs/page handlers, the UI and navigation delegates;
  - `Window.swift`: `OffscreenWindow`, window modes, native tab group, toolbar;
  - `NetErrors.swift`: NSError → `net::ERR_*`.
- Test: `tests/test_webkit_app_pages.py` (`webkit` marker; a local test server via `tests/webkit_helper.py` with `/ok`, `/missing` (404), `/dialog-at-load`, `/download` (`Content-Disposition: attachment`), `/vis`, `/ua`, plus a SOCKS5 fake that logs the address type (reuse `tests/fakes.FakeSocks5Server` if it logs the host; otherwise add the spike's logger to the helper))

**Interfaces:**
- Consumes: `Dispatcher`, `SPI`, `LaunchConfig` (Task 5).
- Produces these wire methods (SAFARI.md §3):
  - `tabs.list` → `[{tab_id, url, title, loading, active, minimized}]`;
  - `tabs.create {url?, active?}` → `{tab_id}`;
  - `tabs.activate`, `tabs.close`;
  - `page.navigate {tab_id, url, wait_until, timeout_ms}` → `{url, status, status_text: ""}`;
  - `page.reload`, `page.back`, `page.forward` (→ `{url, status}` or `null` when there is no history entry);
  - `page.wait_for_load_state {tab_id, state, timeout_ms}`.
- Events: `tab.created {tab_id, opener_tab_id, url}`, `tab.closed`, `tab.updated {url, title, loading}`, `page.load {tab_id, state}`, `dialog {tab_id, type, message}`, `permission {tab_id, kind}`.
- Tab ids are `t1`, `t2`… in creation order.

**Pinned behaviour:**
- **Configuration:**
  - `WKWebsiteDataStore(forIdentifier:)` from `store_uuid`.
  - Proxy: `[ProxyConfiguration(socksv5Proxy: .hostPort(host: "127.0.0.1", port: relay))]`.
  - `applicationNameForUserAgent` per the Global Constraints.
  - `ApplePayEnabled`, `PushAPIEnabled`, `MediaDevicesEnabled` on; `PeerConnectionEnabled = config.webrtc` (W8, W18).
- **Windows:**
  - Native tabs: `tabbingIdentifier = "pp-<profile_id>"`, `tabbingMode = .preferred`. The title is `<profile name> — <page title>`.
  - `normal`: `orderFront(nil)`, with no `activate`.
  - `offscreen`: `OffscreenWindow` (`constrainFrameRect` returns the rect unchanged), `orderBack(nil)`, then `setFrameOrigin(-32000, -32000)`, then SPI occlusion off (W15).
- **The UI delegate:**
  - `_webView:getWindowFrameWithCompletionHandler:` → `window.frame` (W18).
  - alert → OK; confirm → `false`; prompt → `nil`; `_webView:runBeforeUnloadConfirmPanelWithMessage:initiatedByFrame:completionHandler:` → `true`. Each emits `dialog`.
  - `createWebViewWith` → a new `TabController` built from the given configuration, emitting `tab.created` with the opener.
  - `webViewDidClose` → close the tab.
  - Media capture → `.deny` plus a `permission` event. Notifications (`_webView:requestNotificationPermissionForSecurityOrigin:decisionHandler:`) → `false`.
- **Navigation:**
  - `decidePolicyFor navigationResponse` records the HTTP status. If `!canShowMIMEType` or `Content-Disposition` starts with `attachment`, it answers `.cancel` and fails the pending navigation with message `"Download is starting"` (downloads land in 1b).
  - `didFailProvisionalNavigation`/`didFail` → error type `navigation`, message `"net::<NAME> at <url>"`.
- **`NetErrors.swift` mapping** (`NSURLErrorDomain` unless noted):

  | Code | Name |
  |---|---|
  | −1001 | `ERR_TIMED_OUT` |
  | −1003 | `ERR_NAME_NOT_RESOLVED` |
  | −1004 | `ERR_CONNECTION_REFUSED` |
  | −1005 | `ERR_CONNECTION_RESET` |
  | −1009 | `ERR_INTERNET_DISCONNECTED` |
  | −1011 | `ERR_EMPTY_RESPONSE` |
  | −1200 | `ERR_SSL_PROTOCOL_ERROR` |
  | −1202 | `ERR_CERT_AUTHORITY_INVALID` |
  | −999 | `ERR_ABORTED` |
  | any `kCFErrorDomainCFNetwork` code 100–199 (SOCKS) | `ERR_SOCKS_CONNECTION_FAILED` |
  | any `kCFErrorDomainCFNetwork` code 300–399 (proxy) | `ERR_PROXY_CONNECTION_FAILED` |
  | anything else | `ERR_FAILED (<domain> <code>)` |

  **When the profile is proxied**, −1004/−1005 become `ERR_PROXY_CONNECTION_FAILED`: the only TCP peer is the relay.
- **Wait states:**
  - `commit` = `didCommit`;
  - `load` = `didFinish`;
  - `domcontentloaded` = a document-start utility-world user script that posts `{"event": "dcl"}` through the `ppFrame` handler (Task 8 extends the same script);
  - `networkidle` = `load` + 500 ms with an unchanged `performance.getEntriesByType('resource').length`, evaluated in the utility world.

- [ ] **Step 1: Write the failing tests**:
  - `test_navigate_status` (200 and 404);
  - `test_dns_failure_is_named` ("net::ERR_NAME_NOT_RESOLVED" for `http://nonexistent.invalid/`);
  - `test_dead_upstream_names_a_proxy_error` (`proxy` → a closed local port; the message contains `ERR_PROXY` or `ERR_SOCKS`);
  - `test_dialog_at_load_does_not_block` (`/dialog-at-load` reaches `load` in ≤ 5 s; three `dialog` events; the server receives `confirm=false`, `prompt=null`);
  - `test_download_navigation_fails_fast` (≤ 5 s, "Download is starting");
  - `test_user_agent_is_safaris` (the server's User-Agent equals `f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{safari_version()} Safari/605.1.15"`);
  - `test_offscreen_page_stays_visible` (`/vis` posts `visibilityState == "visible"` and `raf > 30`);
  - `test_proxied_dns_at_proxy` (the SOCKS log shows `atyp == "domain"`, `host == "probe.invalid"`);
  - `test_tabs_create_list_close`;
  - `test_normal_window_does_not_take_focus` (V8): `NSWorkspace.shared.frontmostApplication` is unchanged after starting with `window: "normal"` and after `tabs.create`. Check it with `osascript -e 'tell application "System Events" to get name of first process whose frontmost is true'` before and after.
- [ ] **Step 2: Run and confirm they fail.** `.venv/bin/python -m pytest -m webkit tests/test_webkit_app_pages.py -q`
- [ ] **Step 3: Implement** the four Swift files and register the handlers.
- [ ] **Step 4: Run and confirm they pass**, then the `webkit` tests of Task 5 again.
- [ ] **Step 5: Commit**: `git commit -m "WebKit app: tabs, windows, navigation, dialogs, popups and identity"`

---

### Task 8: Frames, evaluation, trusted input and capture in the app

**Files:**
- Create Swift sources in `src/profilepilot/webkit/app/`:
  - `Frames.swift`: the registry and `frames.list`;
  - `Eval.swift`: `js.evaluate`;
  - `Input.swift`: `input.mouse`, `input.key`, `input.insert_text`;
  - `Capture.swift`: `page.screenshot`.
- Test: `tests/test_webkit_app_input.py` (`webkit` marker; pages `/input`, `/activation`, `/frames` (a cross-origin iframe on `localhost` with a button and a text field), `/popup-button`, `/tall`)

**Interfaces:**
- Produces these wire methods:
  - `frames.list {tab_id}` → `[{frame_id, parent_id, url, origin, is_main}]` in frame-tree order (depth-first, children in document order). `frame_id = "f" + _WKFrameHandle.frameID`.
  - `js.evaluate {tab_id, frame_id?, world: "utility" | "main", body, args: dict, timeout_ms, runtime?: bool, frame_seq?: int}` → `{"value": <JSON>}`. `runtime` and `frame_seq` are used from Task 9 on: the utility runtime is installed in that frame with that sequence number before the body runs.
  - `input.mouse {tab_id, action: "move" | "down" | "up" | "click" | "wheel", x, y, button: "left" | "right" | "middle", click_count, modifiers, delta_x, delta_y}`.
  - `input.key {tab_id, action: "down" | "up" | "press", key_code, chars, chars_unmodified, modifiers, modifier_codes}`.
  - `input.insert_text {tab_id, text}`.
  - `page.screenshot {tab_id, clip?: {x, y, width, height}, full_page: bool, format: "jpeg" | "png", quality}` → `{"data": <base64>}`.

**Pinned behaviour:**
- **Eval:** SPI `_evaluateJavaScript:asAsyncFunction:YES withSourceURL:nil withArguments:args forceUserGesture:NO inFrame:inWorld:` (fallback `callAsyncJavaScript`, with `spi.eval_no_gesture = false` reported).
  - Results are converted to JSON (`NSNull`, numbers, strings, arrays, dicts; anything else → `null`).
  - A JS exception → `js` with its first line. A destroyed context → `navigation`.
- **Input:**
  - Coordinates are CSS px in the main-frame viewport. View point = `(x, webView.bounds.height − y)` at page zoom 1.
  - Make the web view first responder, then call `mouseMoved/mouseDown/mouseUp` (`right…`/`other…` for the other buttons) **directly on the web view** (W9/W10).
  - Wheel: a `CGEvent(scrollWheelEvent2Source:units:.pixel,…)` turned into an `NSEvent` → `scrollWheel`.
  - Keys: `flagsChanged` for each `modifier_codes` entry going down (cumulative flags), then `keyDown`/`keyUp`, then `flagsChanged` in reverse.
  - `insert_text` → `insertText(text, replacementRange: NSRange(location: NSNotFound, length: 0))`.
- **Capture:** `WKSnapshotConfiguration.rect` in view coordinates = page clip − (scrollX, scrollY).
  - `full_page` = the rect `(0 − scrollX, 0 − scrollY, scrollWidth, scrollHeight)`. If WebKit refuses negative origins, scroll to (0,0), capture and restore the scroll; whichever works must satisfy the test.
  - Output is encoded at CSS-pixel size (Playwright `scale: "css"`): JPEG quality `quality/100`.

- [ ] **Step 1: Write the failing tests**:
  - `test_trusted_click_and_keys` (click the field, type `ab` via `keys.char_stroke`; every logged event `isTrusted`; value `ab`);
  - `test_insert_text_unicode` (`"é漢😀"` lands exactly; `beforeinput`/`input` are trusted);
  - `test_utility_world_is_invisible` (a global set in `utility` is `undefined` in `main`; `typeof window.webkit` is `"undefined"` in `main`);
  - `test_evaluate_grants_no_user_activation` (after evaluates in both worlds, `navigator.userActivation.hasBeenActive` is false; after a real click it's true);
  - `test_cross_origin_frame_click` (`frames.list` has the child with `parent_id` = main; evaluate in the child works; clicking at iframe offset + element centre triggers the child button's trusted click);
  - `test_popup_from_click_becomes_tab` (`tab.created` with `opener_tab_id`);
  - `test_full_page_screenshot` (on `/tall` scrolled to 1000: decoded JPEG height = `scrollHeight` CSS px and top pixel red; viewport capture height = `innerHeight`);
  - `test_wheel_scrolls` (`delta_y` 500 → `scrollY` > 0).
- [ ] **Step 2: Run and confirm they fail.** `.venv/bin/python -m pytest -m webkit tests/test_webkit_app_input.py -q`
- [ ] **Step 3: Implement** the four Swift files; extend the document-start utility script with frame registration (`{"event": "init", "url"}`).
- [ ] **Step 4: Run and confirm they pass**, plus Task 7's `webkit` tests.
- [ ] **Step 5: Commit**: `git commit -m "WebKit app: frames, gesture-free evaluation, trusted input and capture"`

---

### Task 9: Utility runtime and Playwright's injected script (V1)

**Files:**
- Create:
  - `src/profilepilot/webkit/injected.py`;
  - `src/profilepilot/webkit/js/runtime.js`;
  - `src/profilepilot/webkit/app/Scripts.swift`: loads `Resources/install-template.js` and evaluates it lazily per document when `js.evaluate.runtime` is true. The build has already filled in everything except the frame number. Swift only replaces every `__PP_FRAME_SEQ__` with the request's `frame_seq` (0 when absent).
- Modify: `src/profilepilot/webkit/build.py` (`RESOURCE_FILES` += `install-template.js`), `ACKNOWLEDGEMENTS.md` (Playwright injected script, Apache-2.0)
- Test: `tests/test_webkit_injected.py` (fast), `tests/test_webkit_runtime_js.py` (`chrome` marker), additions to `tests/test_webkit_app_input.py` (`webkit`)

**Interfaces:**
- Produces in `injected.py`:
  - `CORE_BUNDLE_MARKER = "packages/playwright-core/src/generated/injectedScriptSource.ts"`;
  - `def core_bundle() -> Path` (`playwright/driver/package/lib/coreBundle.js` of the installed `playwright`);
  - `def extract_injected(bundle: Path) -> str`: decodes the single-quoted JS literal after the marker (`\n`, `\'`, `\"`, `\\`, `\xHH`, `\uHHHH`, `\u{…}`); raises `WebKitBuildError` without the marker;
  - `def injected_options(frame_seq: int) -> dict` = `{"isUnderTest": False, "sdkLanguage": "python", "frameSeq": frame_seq, "testIdAttributeName": "data-testid", "stableRafCount": 1, "browserName": "webkit", "shouldPrependErrorPrefix": False, "isUtilityWorld": True, "customEngines": []}`;
  - `def install_template() -> str`: the text below after the build replaces `/*INJECTED*/` with `extract_injected(core_bundle())`, `/*RUNTIME*/` with `runtime.js`, and `__PP_OPTIONS__` with `json.dumps(injected_options(0))` whose `0` frame number is rewritten to the bare token `__PP_FRAME_SEQ__`. Only `__PP_FRAME_SEQ__` is left for Swift to fill per frame.

    ```js
    (() => { if (globalThis.__pp) return;
      const module = {};
      /*INJECTED*/
      const injected = new (module.exports.InjectedScript())(globalThis, __PP_OPTIONS__);
      /*RUNTIME*/
      globalThis.__pp = __ppRuntime(injected, __PP_FRAME_SEQ__);
    })();
    ```

- Produces in `runtime.js`: `function __ppRuntime(injected, frameSeq)`, returning:

  | Member | Meaning |
  |---|---|
  | `snapshot({depth, boxes})` | Playwright AI aria snapshot of `document` |
  | `query(selector)` | Handle ids of the matches (`injected.parseSelector` + `querySelectorAll`) |
  | `resolve(id)` | The element, or throws `stale_ref` |
  | `wrap(v)` | Elements → `{__pp_handle: id}` |
  | `actionable(id, {trial, force})` | `{ok, x, y, reason}`: Playwright's order (attached, visible, stable over 2 rAF, enabled, scroll into view when needed, hit target via `elementFromPoint`) |
  | `bbox(id)` | `{x, y, width, height}` in this frame's viewport |
  | `iframeOffset(childIndex)` | Content-box offset of the parent's n-th `iframe, frame` element in document order |
  | `focus(id)`, `selectAll(id)` | |
  | `isVisible(id)` | |
  | `count(selector)` | |

- **V1 check, first step of the task:** list the `InjectedScript` prototype names in the extracted source (`ariaSnapshot`, `parseSelector`, `querySelectorAll`, `elementState`, …). Write the ones used at the top of `runtime.js`. If AI-mode `ariaSnapshot` is missing, stop and report: that is the fallback branch of SAFARI.md §7.1.

- [ ] **Step 1: Write the failing fast tests**:
  - `test_extract_from_installed_playwright` (contains `InjectedScript` and `ariaSnapshot`; starts with `"\nvar __commonJS"`);
  - `test_extract_requires_marker` (a temp file → `WebKitBuildError`);
  - `test_options_exact`;
  - `test_install_template_shape`: contains `"frameSeq":__PP_FRAME_SEQ__`, `"browserName":"webkit"`, `__ppRuntime(injected, __PP_FRAME_SEQ__)`, and the `if (globalThis.__pp) return;` guard. Contains none of `/*INJECTED*/`, `/*RUNTIME*/`, `__PP_OPTIONS__`.
- [ ] **Step 2: Write the failing `chrome` test** (`test_runtime_js_in_chromium`, through the existing Chrome fixtures):
  - inject the template with options for seq 0 into a page that has a form;
  - `__pp.snapshot({})` contains `[ref=e`;
  - `__pp.query('role=button[name="Send"]')` returns 1 handle;
  - `__pp.actionable(h, {})` → `ok` with a point inside the button's rect.
- [ ] **Step 3: Write the failing `webkit` test** (`test_snapshot_format_in_webkit`):
  - `js.evaluate(runtime=true, body="return __pp.snapshot({})")` on the form page contains `- button "Send" [ref=e`;
  - the same call on the child frame with seq 1 contains `[ref=f1e`.
- [ ] **Step 4: Run all three and confirm they fail.**
- [ ] **Step 5: Implement** `injected.py`, `runtime.js`, `Scripts.swift`, the build resource and `ACKNOWLEDGEMENTS.md`.
- [ ] **Step 6: Run all three and confirm they pass.**
- [ ] **Step 7: Commit**: `git commit -m "WebKit: utility runtime on Playwright's injected script"`

---

### Task 10: The Python facade and the session

**Files:**
- Create: `src/profilepilot/webkit/page.py`, `src/profilepilot/webkit/session.py`
- Modify:
  - `src/profilepilot/automation/manager.py`:
    - `_attach_profile`: `info.engine == "webkit"` → `await attach_webkit(profile, info, downloads)`;
    - `_same_runtime`: add `automation_url` to the compared tuple;
    - `open_in_blank_tab`: for a `WebKitSession`, return `await session.open_in_blank_tab(url)`.
  - `src/profilepilot/automation/driver.py`: `world_kwargs` returns `{"isolated_context": world == "isolated"}` when `getattr(target, "supports_isolated_context", False)`.
- Test: `tests/test_webkit_facade.py` (fake app with `FAKE_WEBKIT_EVAL` rules), `tests/test_webkit_facade_drift.py` (fast, all OS)

**Interfaces:**
- Consumes: `AutomationClient` (Task 4), `stroke`/`char_stroke`/`KeyStroke` (Task 3), `RuntimeInfo` fields (Task 1), the wire methods of Tasks 7–9.
- Produces in `session.py`:
  - `async def attach_webkit(profile: Profile, info: RuntimeInfo, downloads_dir: Path | None) -> WebKitSession`;
  - `class WebKitSession` with exactly the `ProfileSession` surface listed in SAFARI.md §7, plus `engine = "webkit"` and `async def open_in_blank_tab(self, url: str) -> WebKitPage | None`.
  - It caches **one `WebKitPage` object per `tab_id`** (the tools compare pages with `is`).
- Produces in `page.py`, with `supports_isolated_context = True` on page, frame, locator and handle:

  | Class | Members |
  |---|---|
  | `WebKitPage` | `url`, `is_closed`, `title`, `goto`, `reload`, `go_back`, `go_forward`, `wait_for_load_state`, `wait_for_url`, `evaluate`, `locator`, `get_by_text`, `frames`, `main_frame`, `keyboard`, `mouse`, `screenshot`, `aria_snapshot`, `content`, `bring_to_front`, `close` |
  | `WebKitFrame` | `url`, `is_detached`, `evaluate`, `frame_element`, `content`, `locator`, `wait_for_load_state` |
  | `WebKitLocator` | `first`, `last`, `nth`, `filter(visible=)`, `count`, `wait_for`, `click(button, click_count, trial, timeout)`, `dblclick`, `hover`, `fill`, `press`, `press_sequentially`, `focus`, `evaluate`, `evaluate_all`, `bounding_box`, `screenshot`, `scroll_into_view_if_needed`, `is_visible` |
  | `WebKitElementHandle` | `is_visible`, `evaluate`, `bounding_box`, `click`, `type`, `fill`, `focus`, `dispose` |
  | `WebKitKeyboard` | `press(key, delay=0)`, `down`, `up`, `type(text, delay=0)`, `insert_text` |
  | `WebKitMouse` | `move`, `click(x, y, button="left", click_count=1)`, `down`, `up`, `wheel(dx, dy)` |
  | `WebKitContext` | `pages`, `new_page`. `cookies`/`add_cookies`/`clear_cookies` raise `EngineUnsupportedError("Cookies of WebKit (Safari) profiles arrive in the next update; use a Chrome-family profile for cookie tools for now.")` |
  | `WebKitResponse` | `status: int`, `status_text: str = ""` |

**Pinned algorithms:**
- **Evaluate body** (sent as `js.evaluate.body`, with `args = {"__arg": arg}`):
  `const __v = (\n<js>\n); return (typeof __v === "function") ? await __v(__arg) : __v;`
  - Locator/handle form: `const __el = __pp.resolve(__h); const __v = (\n<js>\n); return await __v(__el, __arg);`
  - `isolated_context=False` → `world: "main"` (and no `__pp`).
  - Results equal to `{"__pp_handle": id}` become `WebKitElementHandle`.
- **Selectors:**
  - `first` → `>> nth=0`, `last` → `>> nth=-1`, `nth(i)` → `>> nth=i`, `filter(visible=True)` → `>> visible=true`, `get_by_text(t)` → `internal:text=<json.dumps(t)>i`.
  - Actions on a locator that isn't narrowed by `nth` fail with `WebKitError(f"strict mode violation: {selector} resolved to {n} elements")` when n > 1.
- **Refs:** `aria-ref=e<n>` → main frame; `aria-ref=f<k>e<n>` → the frame whose `frame_seq == k`. The selector stays the full ref.
- **Frame seq:** main = 0; others = their 1-based position in `frames.list` order, stable while the frame lives. Every `js.evaluate` that needs `__pp` sends `runtime: true` and that frame's `frame_seq`.
- **Snapshot stitching:** take each frame's `__pp.snapshot`. Insert a child frame's lines after its `- iframe [ref=…]` line in the parent, indented 2 spaces deeper. Parent iframe lines and children are matched by document order (`frames.list` children order).
- **Click point:** `actionable` in the element's frame gives `(x, y)`. For each ancestor, add `iframeOffset(index of child among the parent's children)`. Send `input.mouse click` with `click_count` (2 for `dblclick`). `trial=True` stops after `actionable`.
- **Keys:**
  - `keyboard.press(k)` → `input.key press` with `stroke(k)`.
  - `keyboard.type(t, delay)` → for each char, `char_stroke(c)` → `input.key press` (sleeping `delay` ms), else `input.insert_text(c)`.
  - `fill(v)` → focus → `selectAll` → `input.insert_text(v)` (an empty `v` → `input.key press` Backspace).
- **Waits:** `wait_for_url(pred, wait_until, timeout)` polls `tabs.list` every 0.1 s, then `page.wait_for_load_state`.

**The drift test** (`test_webkit_facade_drift.py`):
1. Parse with `ast` the modules `server/tools_browser.py`, `server/tools_data.py`, `automation/content.py`, `automation/cookies.py`, `automation/autofill.py`, `automation/typing.py`.
2. Collect `ast.Attribute` names whose value is a `Name` in `{page, frame, f, locator, loc, target, el, element, handle, context, keyboard, mouse, first, owner, child}` or an `Attribute` ending in `.keyboard`/`.mouse`/`.first`/`.main_frame`/`.context`.
3. Every collected name must be an attribute of one of the facade classes, or a key of `UNSUPPORTED: dict[str, str]` (name → which phase brings it, e.g. `"select_option": "1b"`, `"cookies": "1b"`).
4. Python-object attributes reached through those names (`session.context.pages` …) are allowed only via an explicit `NOT_PLAYWRIGHT` set in the test.

- [ ] **Step 1: Write the failing tests**:
  - `test_goto_status_and_params`;
  - `test_evaluate_wrapper_and_worlds` (body contains `typeof __v === "function"`; `world` `utility` vs `main`);
  - `test_world_kwargs_for_facade` (`driver.world_kwargs(page, "main") == {"isolated_context": False}`);
  - `test_click_sends_trusted_mouse_at_point` (fake rule: `actionable` → `{"ok": true, "x": 10, "y": 20}` → the log has `input.mouse` click at (10, 20), `click_count` 1);
  - `test_strict_mode_violation`;
  - `test_aria_ref_routes_to_frame`;
  - `test_keyboard_maps` (`press("Meta+a")` → `key_code` 0, `modifiers` `MOD_META`, `modifier_codes` [55]);
  - `test_type_inserts_unmapped_chars` (`"aé"` → `input.key` then `input.insert_text`);
  - `test_snapshot_stitching`;
  - `test_popup_becomes_active` (`drain_new_tabs`, `is_active`);
  - `test_dialogs_drained`;
  - `test_cookies_unsupported`;
  - `test_same_page_object_per_tab`;
  - **the drift test**.
- [ ] **Step 2: Run and confirm they fail.** `.venv/bin/python -m pytest tests/test_webkit_facade.py tests/test_webkit_facade_drift.py -q`
- [ ] **Step 3: Implement** `page.py`, `session.py`, the manager branch and `world_kwargs`. Grow the facade until the drift test passes; anything left must sit in `UNSUPPORTED` with its phase.
- [ ] **Step 4: Run and confirm they pass**, then the fast suite on this Mac. On Windows CI the fast suite runs the drift test too.
- [ ] **Step 5: Commit**: `git commit -m "WebKit: Playwright-shaped facade and session"`

---

### Task 11: The tools on WebKit profiles

**Files:**
- Create: `src/profilepilot/server/engine.py`
- Modify:
  - `src/profilepilot/server/tools_browser.py`: `_enter_text` with `method == "paste"` on a WebKit session raises before touching the clipboard. Nothing else changes; the facade carries the rest.
  - `src/profilepilot/server/tools_data.py`: `http_fetch` → `require_chromium(session, "http_fetch")` before anything is fetched.
- Test: `tests/test_webkit_tools.py` (fast, fake app, following the tool-calling pattern of `tests/test_tools_unit.py`), `tests/test_webkit_e2e_tools.py` (`webkit` marker, real app, local server)

**Interfaces:**
- Produces: `def require_chromium(session: Any, feature: str) -> None`. It raises `EngineUnsupportedError` when `getattr(session, "engine", "chromium") == "webkit"`.
- Exact messages:
  - **Generic:** `f"{feature} needs a Chrome-family profile; '{session.label}' is a WebKit (Safari) profile."`
  - **http_fetch:** the generic text plus " Use browser_navigate and browser_read instead."
  - **paste:** "Pasting through the system clipboard isn't available on WebKit (Safari) profiles yet. Use browser_type with method 'type' or 'human'."

- [ ] **Step 1: Write the failing fast tests**:
  - `test_paste_refused_on_webkit` (`ToolError` contains "WebKit (Safari)"; the fake log has no `input.*`);
  - `test_http_fetch_refused_on_webkit`;
  - `test_navigate_on_webkit_reports_status` ("Navigated: HTTP 200.");
  - `test_press_enter_on_webkit` (`input.key` `key_code` 36).
- [ ] **Step 2: Write the failing e2e test** (`webkit`):
  1. `browser_navigate` to the form page → `browser_snapshot` has refs.
  2. `browser_click` by ref, then `browser_type` with method `fill`, then `human`.
  3. `browser_press_key("Enter")` submits; the server receives the typed value.
  4. `browser_screenshot`: JPEG, plus `full_page`.
  5. `browser_evaluate` in both worlds; `browser_read` has the page text; `browser_extract` css works.
  6. `browser_wait_for(text=…)`.
  7. `browser_tabs` new, select and close (the last tab is never closed).
  8. `browser_hover` opens a `:hover` menu (the snapshot shows its item).
  9. A cross-origin iframe page: the snapshot contains `f1e` refs, and clicking one works.
- [ ] **Step 3: Run and confirm they fail.**
- [ ] **Step 4: Implement** `engine.py` and the two guarded call sites.
- [ ] **Step 5: Run and confirm they pass**, plus the fast suite and `-m chrome tests/test_tools_chrome.py` (Chromium is unchanged).
- [ ] **Step 6: Commit**: `git commit -m "WebKit: browsing tools on WebKit profiles; Chrome-only tools refuse clearly"`

---

### Task 12: Doctor, SPI and fingerprint guards, CI job

**Files:**
- Modify:
  - `src/profilepilot/webkit/build.py`: `doctor_checks(root: Path) -> list[tuple[str, bool | None, str]]`.
  - `src/profilepilot/cli.py`: **second small hunk**, `cmd_doctor` extends `checks` with `doctor_checks(store.root)`.
  - `src/profilepilot/server/tools_profiles.py`: `profile_status` appends warnings for WebKit runtimes. These are a missing SPI from the host `/status` → `spi`, and the WebRTC leak for `webrtc == "default"` with a proxy: "Warning: WebRTC can reveal this Mac's real IP address past the proxy (launch.webrtc is 'default')."
  - `.github/workflows/ci.yml`: a new `webkit` job.
- Test: `tests/test_webkit_doctor.py` (fast), `tests/test_webkit_spi.py`, `tests/test_webkit_fingerprint.py`, `tests/test_webkit_proxy.py` (all three `webkit`)

**Pinned checks** (`doctor_checks`):
- Off macOS: `("webkit", None, "not available on this OS (WebKit profiles need macOS 14+)")`.
- On macOS:
  - `("webkit app", built, "<path> <version>" | "not built: run profilepilot webkit install")`;
  - for each SPI that is False: `(f"webkit spi {name}", False, "missing private WebKit API; the Safari fingerprint or off-screen behaviour may differ")`;
  - `ua_claim` False: `("webkit version", False, "Safari's WebKit build differs from the system WebKit; WebKit profiles make no Safari claim")`;
  - a data root inside `~/Documents`, `~/Desktop` or `~/Downloads`: `("data root location", False, "macOS asks the WebKit app for permission to use this folder, which can stall a profile start; use the default data root")`.

**CI job:**

```yaml
  webkit:
    name: webkit (macos-latest)
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.13", cache: pip}
      - run: python -m pip install -e ".[test]"
      - run: python -m profilepilot webkit install
      - env: {PROFILEPILOT_SECRETS: file}
        run: python -m pytest -m webkit -q
```

- [ ] **Step 1: Write the failing tests**:
  - **fast:** `test_doctor_off_macos`, `test_doctor_reports_missing_spi` (monkeypatched status), `test_doctor_warns_documents_root`.
  - **webkit:**
    - `test_all_spi_present` (`--spi-report`: every value true);
    - `test_fingerprint_matches_safari` on the spike's `/fp` page (move it to `tests/fixtures/webkit_fp.html`):
      - `ua` equals Safari's string;
      - `vendor == "Apple Computer, Inc."`;
      - `ApplePaySession`, `PushManager` → `"function"`; `mediaDevices` → `"object"`;
      - `outerWidth > 0`; `webdriver is False`;
      - `RTCPeerConnection` `"function"` unproxied, `"undefined"` proxied with `auto`;
    - `test_proxied_profile_sends_no_udp` (the spike's UDP sink receives 0 STUN packets proxied, more than 0 unproxied as a control).
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement** `doctor_checks`, the `cli.py` hunk, the `profile_status` warnings and the CI job.
- [ ] **Step 4: Run** the fast suite, then `-m webkit` (everything), then `-m chrome` on this Mac, and confirm all pass.
- [ ] **Step 5: Commit and push**: `git commit -m "WebKit: doctor, SPI/fingerprint/leak guards and the macOS CI job" && git push`. Then tell the Windows session that Phase 1a is ready for review, with the test counts.
