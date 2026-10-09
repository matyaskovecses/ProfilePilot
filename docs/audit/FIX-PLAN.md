# Fix plan: from the fingerprint audit to parity with plain Chrome

This plan is based on [docs/FINGERPRINT-AUDIT.md](../FINGERPRINT-AUDIT.md), whose finding IDs F1–F11 it uses.
It lists the steps in order: each later step assumes the earlier ones are done.
Every step is the smallest change that closes its findings, and every step adds a test.
The audit itself changed nothing under `src/` or `tests/`.

**Ground rules (unchanged).** No fingerprint spoofing, and no stealth init scripts or `--disable-blink-features`-style switches.
Do not use `Emulation.setFocusEmulationEnabled`, which fakes focus. Do not replace `Page.navigate` with renderer-initiated
`location.href`: the PL experiment showed it sends `Sec-Fetch-Site: cross-site` instead of `none`.

| # | Step | Closes | Size |
|---|---|---|---|
| 1 | Regression probe test (`tests/test_native_fingerprint.py`) | guards F1, F3, F4, F5 | M |
| 2 | One driver module; switch to patchright; `browser_evaluate(world=)` | F1, F3 | M |
| 3 | iphey-type crash: verify, make it legible, decide on worker auto-attach | F2 | S–L |
| 4 | Secure DNS off when proxied | F11 | S |
| 5 | Remote mode: no local DNS for proxied profiles | F8 | S |
| 6 | Timezone alignment for out-of-process iframes | F7 | S–M |
| 7 | Align `http_fetch(engine="scrapling")` with the browser | F9 | S |
| 8 | First navigation without `Page.navigate` | F4, F5, history.length | M |
| 9 | Proxy details in model-facing text | F10 | S |
| 10 | Docs: native-by-design differences | F6, offscreen, expected | S |
| 11 | Optional follow-ups | httpx identity, bfcache, offscreen | n/a |

---

## 1. Regression probe test first

**Files.**
- New `tests/native_probe.py` (helper): a trimmed copy of `docs/audit/scripts/probe_server.py`, keeping only the T0/late CDP detectors, the main-world traps, `userActivation`, `hasFocus`, the request headers and the bfcache pages.
- New `tests/test_native_fingerprint.py`, marked `chrome` + `asyncio`. It uses the `tests/test_tools_chrome.py` pattern: a temporary `Store` and the MCP tool functions.

**Tests.**

| Test | Asserts |
|---|---|
| `test_idle_profile_is_native` | A profile started with `launch.start_url=<probe>` and no client (P0) has these fields equal to an unattached `chrome_helper` launch: `webdriver` false; UA, `userAgentData` and headers equal `Browser.getVersion` and each other; the globals hash and `window.chrome` keys; and all CDP detectors false. |
| `test_attached_session_is_not_cdp_detectable` | After `browser_navigate` to the probe, every check is false: `prepareStackTrace` (console, uncaught, rejection) and a dedicated worker running the same probe. The console timing stays ≤ 3× the idle profile's value + 5 ms. Use a ratio, not an absolute threshold, because the timing depends on the machine. |
| `test_reading_tools_leave_no_main_world_traces` | `browser_snapshot`, `browser_read`, `browser_screenshot`, `browser_scroll` and default `browser_evaluate("document.title")` cause 0 trapped main-world calls. `browser_evaluate(..., world="main")` can read a page global (documented as detectable). |
| `test_first_navigation_is_like_a_typed_url` | `hasBeenActive` false, `AudioContext` `suspended`, `Sec-Fetch-Site: none`. Check `hasFocus()` only in window=normal with `xfail(strict=False)`, because it depends on the desktop. |

Mark the assertions that fail today `xfail(strict=True, reason="F1"/"F3"/"F4")`. Each fix step then flips its marker, and a fix that starts passing forces the marker's removal.

Do **not** use the `Error.stack`-getter detector as the only CDP check. It is dead on Chrome 154 and stays green while Playwright is attached.

**Accept:** the suite runs in under 2 minutes, and `pytest -m chrome tests/test_native_fingerprint.py` reports exactly the expected xfails.

---

## 2. One driver module; switch the CDP driver to patchright (F1, F3)

**Why patchright.** Version 1.63.0 is already installed in the venv as a Scrapling dependency, with the same API as Playwright 1.63.
`connect_over_cdp(no_defaults=...)` and `aria_snapshot(mode="ai")` have the same signatures.
A static check of `patchright/driver/package/lib/coreBundle.js` shows three differences:
- page/frame sessions no longer send `Runtime.enable`;
- dedicated workers get their context via `Runtime.evaluate("globalThis")` instead of `Runtime.enable`;
- `Page.evaluate`, `Locator.evaluate` and `evaluate_all` default to `isolated_context=True`.

It still sends `Target.setAutoAttach(waitForDebuggerOnStart)` and `Network.enable` to workers (see step 3).

**Files and changes.**

1. **New `src/profilepilot/automation/driver.py`.** The single import point. It exports `async_playwright`, `sync_playwright`, `Browser`, `BrowserContext`, `CDPSession`, `Dialog`, `ElementHandle`, `Frame`, `Locator`, `Page`, `Playwright`, `Error`, `TimeoutError` and `DRIVER`.
   - Selection: config `automation.driver` / env `PROFILEPILOT_DRIVER` ∈ {`patchright`, `playwright`}. The default is `patchright` when importable, otherwise `playwright`.
   - Exception classes differ between the two packages, so **every** `except PlaywrightError` must import from this module.
2. **Replace `from playwright.async_api|sync_api import …`** with `from ..automation.driver import …` in:
   - `automation/manager.py`, `automation/content.py`, `automation/typing.py` and `automation/autofill.py`;
   - `server/tools_browser.py`, `server/tools_identity.py` and `server/app.py` (error mapping);
   - `client.py` (`_with_profile_context`);
   - `integrations/scrapling.py`. Its `DynamicSession` subclass stays on Playwright, because Scrapling's base class owns that import. Document that the Scrapling *browser* integration is still CDP-detectable, or move it to Scrapling's patchright-based session if 0.4.15 provides one (verify).
3. **`server/tools_browser.py` `browser_evaluate`.** Add `world: Literal["isolated", "main"] = "isolated"`. `main` passes `isolated_context=False` and its docstring says it is detectable (main-world `UtilityScript` stacks). Mirror the parameter in `skills/profilepilot/SKILL.md`.
4. **No other call-site changes.** `_ensure_foreground` / `_guess_foreground`, `content.read_page`, `visible_html`, `full_html`, `mask_sensitive_values`, `pdf_hint`, the scroll and screenshot evaluates, typing and autofill all become isolated-world evaluates through the new default.
5. **`pyproject.toml`.** Add `patchright==1.63.*` to the core dependencies, pinned to the Playwright minor version.

**Fallback.** If patchright is rejected, keep Playwright and add `automation/isolated.py`: `isolated_evaluate(page|frame, js, arg)` using
`context.new_cdp_session(page)`, then `Page.getFrameTree`, then `Page.createIsolatedWorld(frameId, worldName, grantUniveralAccess=False)`,
then `Runtime.evaluate(contextId=…, returnByValue=True, awaitPromise=True)`. None of these needs `Runtime.enable`.
Then route every call site in item 4 through it, and use `locator.get_attribute("type")` instead of `e => e.type === 'password'`.
This closes F3 only, not F1.

**Tests.**
- Parametrize `tests/test_manager_chrome.py` and `tests/test_tools_chrome.py` over `DRIVER ∈ {patchright, playwright}` with a fixture that sets `PROFILEPILOT_DRIVER`. Both must pass. Pay particular attention to aria-ref resolution, popups, dialogs, downloads, the timezone session and the minimized-window rule.
- Re-run the chrome parts of `tests/test_typing.py` and `tests/test_autofill.py`. Values set and events dispatched from an isolated world must still reach React/Vue handlers.
- Flip the step 1 xfails for F1 and F3.
- New unit test `tests/test_tools_unit.py::test_browser_evaluate_world_param`.

**Accept:**
- The step 1 probe shows no CDP detector and 0 main-world traps.
- `run_detectors.py --configs B1,P` for deviceandbrowserinfo, fingerprint, pixelscan-bot and rebrowser equals B1. For rebrowser this means `sourceUrlLeak` and `mainWorldExecution` stay untriggered with the default world.

---

## 3. Browser-process crash under DevTools instrumentation (F2)

1. **Verify.** After step 2, re-run `docs/audit/scripts/crash_isolate.py` and the iphey P configuration.
   The static check predicts it **still crashes**: patchright keeps worker auto-attach, and in isolation `Target.setAutoAttach` alone crashed it.
2. **Make it legible** (small, always do this).
   - **`browser/host.py`.** When Chrome exits with 0xC0000005 / 3221225477, write the exit code to `profiles/<id>/last_exit.json`. It already logs `browser exited (code …)`.
   - **`automation/manager.py` `ProfileSession._on_disconnected` and `server/app.py` error mapping.** When the connection drops during a tool call and `last_exit.json` shows an access violation, raise `ProfileNotRunningError` with this message: "Chrome crashed while this page was open (some sites crash automated browsers). The profile was not restarted; reopening the same page may crash it again."
   - **`BrowserManager.session`.** Do not autostart within the same tool call after a crash.
   - Test: `tests/test_tools_unit.py::test_crash_exit_is_reported` with a fake runtime.
3. **Decide on worker auto-attach** if the crash persists. ProfilePilot never uses worker handles. Two options:
   - **(a)** Carry a minimal driver patch that adds `filter: [{type: "worker", exclude: true}, {type: "shared_worker", exclude: true}, {type: "service_worker", exclude: true}, {}]` to the page-session `Target.setAutoAttach`. Version-pinned, applied and verified at import time by `driver.py`, with a test that it applied.
   - **(b)** Accept the crash and document it.

   Also verify whether `restore_session` causes a crash loop: the crashing tab is restored and attached again.
4. **Report upstream.** Send a Chromium bug with the raw CDP reproduction from `crash_isolate.py`, without the site's code.

**Accept:** iphey either reads "Trustworthy" (option a) or fails with the explicit crash message and no silent restart.

**Status: done, with a different root cause.** The crash is not DevTools-related: Chrome 154 crashes on iphey.com whenever the
user-data-dir path is longer than 175 characters, also with no client (audit F2 update). With a short data root iphey reads
"Trustworthy" under patchright without any patch, so option (a) was not needed. Done as planned: `last_exit.json`, the crash
message once instead of a silent autostart (the next call starts the profile), `tests/test_tools_unit.py::test_crash_exit_is_reported`.
Added: a launch warning for long user-data-dirs (`prefs.MAX_USER_DATA_DIR_CHARS`), and no session restore on the first start after a
crash, because the restore loop was verified (`host.launch_after`, `tests/test_flags.py::test_no_session_restore_right_after_a_crash`).
The upstream report is still to be sent: `crash_isolate.py --exp startup --udd-len 176` reproduces it with iphey.com and no client;
a minimal page that triggers it was not found yet.

---

## 4. Secure DNS off when proxied (F11; verified in PXND)

**Files.**
- **`browser/prefs.py`.** Change the signature to `prepare_user_data_dir(udd, launch, *, proxied: bool = False)`.
  - When `proxied`: merge `{"dns_over_https": {"mode": "off"}}` into `<udd>/Local State`, keeping every other key, and create the marker `<udd>/.profilepilot-doh-off`.
  - When not proxied and the marker exists: remove `dns_over_https.mode` (Chrome's native "automatic" returns) and delete the marker.
  - Document the PXND evidence in the module docstring.
- **`browser/host.py`.** Call `prepare_user_data_dir(self.udd, launch, proxied=self.relay is not None)`.

**Tests (`tests/test_flags.py`).**
- `test_prepare_user_data_dir_turns_secure_dns_off_when_proxied` covers the merge and the preserved keys.
- `test_secure_dns_is_restored_when_the_proxy_is_removed`.
- `test_secure_dns_untouched_without_marker`: a user's own setting survives unproxied launches.

**Accept:** the network audit's PXN socket monitor shows 0 non-loopback connections from Chrome.

**Status: done.** As planned, with one refinement to "preserve user settings": the marker records the mode the profile
had before (`{"previous": ...}`), and an unproxied launch restores that value (none recorded: the key is removed, so
Chrome's "automatic" returns) - unless the mode is no longer `"off"`, i.e. the user chose another one while proxied,
which is kept. Without the marker nothing is read or written. Tests: the three above (the third parametrized over the
user's modes). Re-measured through the real proxy (socket monitor of the Chrome tree, 30 s from launch, counts only:
[network-doh-step4.json](raw/network-doh-step4.json)): the product path opens **0** non-loopback connections with both
the SOCKS5 and the HTTP upstream; the pre-fix control on the same machine and proxy opens 2-3 direct TCP 443
connections about 5 s after launch.

---

## 5. Remote mode: no local DNS for proxied profiles (F8)

**Files.**
- **`safety.py`.** Add `check(url, *, resolve=True)` / `acheck(url, *, resolve=True)`. With `resolve=False`, run `_check_static` only. It already blocks `localhost`, `*.localhost`, local suffixes and every private, loopback or link-local literal, including numeric spellings.
- **`server/tools_browser.py`.**
  - `check_url(state, url, *, proxied)` and `enforce_final_url(state, page, *, proxied)`.
  - `proxied` comes from the stored profile: `proxy_id` is set. `check_url` runs before the profile starts, so it reads `state.store.get_profile(ref)` in a worker thread.
  - ShardX refs and unknown refs keep `resolve=True`.
- **`server/tools_data.py`.** `http_fetch`'s `on_request` hook and the final-URL check pass `resolve=not proxy`.

**Tests.**
- `tests/test_safety.py::test_remote_mode_without_resolution_blocks_literals_and_local_names`: the `fake_dns` fixture asserts `getaddrinfo` is never called.
- `tests/test_tools_unit.py::test_proxied_profile_remote_mode_does_not_resolve_locally`: a monkeypatched loop `getaddrinfo` raises. `browser_navigate` and `http_fetch` on a proxied fake profile still succeed, and an unproxied one still resolves.

**Trade-off to document in `safety.py`.** For a proxied profile, a hostname that resolves to a private address on the *proxy's* side is reachable through the proxy. That is not the user's network.

**Status: done, with one deviation.** "Proxied" is not read from the stored `proxy_id` alone: a *running* profile
answers with its live relay (`RuntimeInfo.relay_port` and an upstream), a stopped one with its saved proxy
(`tools_browser.profile_proxied` before the start, `session_proxied` after it). The stored value alone would skip the
DNS check for a profile that was started without a proxy and got one saved later (its browser still connects
directly, so a name resolving to the LAN would be reached from this machine), and the same for a relay switched to a
direct connection. `check_url` / `enforce_final_url` take a required `proxied` keyword; `http_fetch` passes
`resolve=not proxied` to its per-hop hook and its final-URL check. ShardX and unknown refs keep `resolve=True`, and so do
the settings-time checks of `launch.start_url` in `profile_create` / `profile_update` (that URL is later opened by
the host without any check, so it must stay safe without a proxy). Tests: the two above (the unit test runs a real relay
through a fake SOCKS5 upstream and fails every local lookup).

---

## 6. Timezone alignment for out-of-process iframes (F7)

1. **Spike (½ day).**
   - Launch with `--time-zone-for-testing=America/New_York` through `launch.extra_args`, then run the network collector (main frame, dedicated worker, shared worker, cross-site OOPIF, and a service worker).
   - Check three things: every context reports the zone; there is no infobar (window capture); and it survives CDP detach.
2. **If the spike passes.**
   - **`browser/flags.py`**: `launch.timezone` → `--time-zone-for-testing=<tz>`. Add the switch to `MANAGED_SWITCHES`.
   - **`automation/manager.py`**: drop `_apply_timezone` and `_tz_sessions`.
   - Test: `tests/test_flags.py::test_timezone_flag`.
   - Chrome test: `tests/test_native_fingerprint.py::test_timezone_reaches_oopif_and_workers`, with a cross-site iframe served from a second local origin, `localhost` vs `127.0.0.1`.
3. **Otherwise.** In `ProfileSession._on_page`, register `page.on("frameattached")` / `page.on("framenavigated")`. These call `_apply_timezone_frame(frame)`, which tries `context.new_cdp_session(frame)` (only out-of-process frames succeed), sends `Emulation.setTimezoneOverride` and keeps the session.
   - Same chrome test.
   - Document the first-script race and the revert on detach.

**Status: done (path 3).** The spike failed: `--time-zone-for-testing` is not compiled into branded Chrome 154 (the
string is in none of its binaries; with the switch every context kept the OS zone, before and after a CDP
attach/detach; no infobar) - [timezone-step6.json](raw/timezone-step6.json). Implemented as planned with
`framenavigated` only (at `frameattached` an iframe is still in its parent's process, so `new_cdp_session` cannot
succeed yet) plus `framedetached` cleanup and the child frames that exist at attach; a kept session is reused on the
frame's later navigations (the override follows the target into new processes) and replaced when its target is gone.
Added: `browser_navigate` no longer opens the first URL at launch (step 8) for a profile with `launch.timezone`. Chrome
would load it before the override can be attached, so the page's first scripts saw the OS zone; it is navigated in the
existing, already overridden tab instead (`test_tools_unit.py::test_navigate_with_a_timezone_uses_the_overridden_tab`).
Measured 4/4 (`tests/test_native_fingerprint.py::test_timezone_reaches_oopif_and_workers`): the main frame from its
first script, dedicated, shared and service workers, the cross-site iframe (late) and its worker all report the
configured zone; without the frame override the iframe and its worker report the OS zone. Residuals (documented in
`automation/manager.py`): the out-of-process iframe's *first* script still sees the OS zone (4/4: the earliest hook
is its commit); tabs and popups opened while attached have the same race; every override reverts when ProfilePilot
disconnects. A race-free option would be a third patchright patch that sets the default context's `timezoneId`, so the
driver applies it to every page and iframe target before `Runtime.runIfWaitingForDebugger` (not done).

---

## 7. Align `http_fetch(engine="scrapling")` with the browser (F9)

**Files.**
- **`server/tools_data.py`.** Add `browser_client_hints(state, session)`, which reads `navigator.userAgentData.getHighEntropyValues(["fullVersionList", "platformVersion"])` once in an isolated world and caches it per session, like `browser_user_agent`.
  - `_fetch_scrapling` passes default headers built from it: `User-Agent` from `Browser.getVersion`, `sec-ch-ua`, `sec-ch-ua-mobile: ?0`, `sec-ch-ua-platform: "Windows"` (whatever the browser reports), and `Accept-Language` as in httpx.
  - Headers the user passes still win.
- **`integrations/scrapling.py` `ProfileFetcherSession`.** Default `impersonate` to curl_cffi's newest Chrome target, unless the caller sets one, and accept the browser headers above. The same applies to the Python-API `fetcher_session`.

**Tests.**
- `tests/test_scrapling_integration.py::test_fetcher_session_uses_the_browser_identity`: with a fake UA and hints, there is no `Macintosh` and the platform is `"Windows"`.
- Chrome test: a local echo server compares `User-Agent` and `sec-ch-ua-platform` between `browser_navigate` and `http_fetch(engine="scrapling")`.

**Known residual.** The curl_cffi ClientHello is Chrome 14x: JA4 `t13d1516h2…` vs the browser's `t13d1517h2…`.

---

## 8. First navigation without `Page.navigate` (F4, F5, history.length)

The P0 configuration (probe as the command-line start URL, no CDP) was clean: no user activation, page focus, `Sec-Fetch-Site: none`, `history.length` as B0.

1. **Autostart path** (the common case: an agent's first call is `browser_navigate` on a stopped profile).
   - **`browser/runtime.py`.** Add `RuntimeManager.start(ref, *, timeout, window, start_url: str | None = None)`, passed to the host as `--start-url` (like `--window`).
   - **`browser/host.py`.** Use `--start-url`, else `launch.start_url`, as the start URL.
   - **`automation/manager.py`.** Add a `start_url` argument to `BrowserManager.session(..., start_url=)`.
   - **`server/tools_browser.py` `browser_navigate`.** If the profile is not running and the target is http(s), start it with the destination (already policy-checked by `check_url`). Then wait on the active tab with `page.wait_for_load_state(wait_until)` instead of calling `page.goto`. Report "Opened at launch" (no HTTP status is available).
   - **Trade-offs.**
     - The URL appears in Chrome's command line and the host log. Log a redacted form: the origin only.
     - With `restore_session` and a saved session, the URL opens in an extra tab. That is already the documented behaviour, and the URL tab becomes active.
2. **Running profile whose active tab is still the untouched initial `about:blank`.**
   - **Spike first:** a host control endpoint `POST /open {"url"}` that runs `chrome.exe --user-data-dir=<udd> --profile-directory=Default <url>`. Chrome hands it to the running instance as "a link from another app".
   - Measure with the step 1 probe: activation, focus and Sec-Fetch-Site. Also check that it **does not take the OS foreground** from the user's current app. If it does, drop this sub-step: ProfilePilot never steals focus.
   - If it passes: open there, adopt the new tab, and close the blank one.
3. **Later navigations keep `Page.navigate`.** Activation is natural once the agent has clicked or typed.
4. **Also measure whether `hasFocus()` stays `false` while CDP clicks and keys arrive.** If it does, raise F5 to medium in the audit doc.

**Tests.**
- Flip `test_first_navigation_is_like_a_typed_url` (autostart path).
- `tests/test_flags.py` for the host's `--start-url` argument.
- `tests/test_tools_unit.py::test_navigate_autostarts_with_the_destination` with a fake runtime.

**Status: done (1, 2, 3, 4).** Both routes are implemented and measured like P0 (audit F4/F5 updates). The hand-off did not change the
OS foreground window. Found on the way: the user activation came from the driver's evaluates (`userGesture: true`), not from
`Page.navigate`; fixed with a second patchright patch, `evaluate-without-user-gesture` (so `browser_evaluate` has no user gesture
any more). `hasFocus()` is compared with P0 off-screen instead of in a visible window. Pages opened before the attach report their
out-of-process iframes with `frame.url == ''` in the driver; `content.frame_url` falls back to the document's location (form_detect
labels, browser_extract base URLs). Tests: the three above, plus `test_reading_tools_never_activate_the_page`, the `running` path of
`test_first_navigation_is_like_a_typed_url`, `tests/test_runtime_chrome.py::test_urls_open_like_links_from_another_app` and the
driver patch tests.

---

## 9. Proxy details in model-facing text (F10)

**Files.**
- **`proxy/url.py` `ProxyEndpoint.redacted()`.** Return `scheme://***:***@host:port` when there is a user name, keeping no part of it.
- **`server/tools_data.py` route line.** `via the profile's proxy '<proxy name>' (<scheme>)`, taken from the store record (`profile.proxy_id`) instead of `info.upstream`.
- **`browser/host.py` / `models.RuntimeInfo.upstream`.** Keep it for logs. Model-facing summaries in `tools_profiles.py` use the proxy name.

**Tests.**
- `tests/test_proxy_url.py::test_redacted_hides_the_whole_user`.
- `tests/test_tools_unit.py::test_http_fetch_route_names_the_proxy_not_its_host`.

**Status: done.** `redacted()` returns `scheme://***:***@host:port`. `tools_profiles.live_proxy_label` names the proxy
the *live* relay uses (`RuntimeInfo.proxy_id`, which follows live switches; `'name' (scheme)`, or "an unsaved
<scheme> proxy" after a switch to a bare URL), used by the `http_fetch` route line, `runtime_text` (`profile_start`,
`profile_status`) and `proxy_test(profile=)`. The profile tools' saved-proxy label (`proxy_label`: `profile_list`,
`profile_create`, `profile_set_proxy`, `profile_status`, `proxy_test(profile=)` of a stopped profile) is now
`'name' (scheme)` too; it showed `ProxyRecord.redacted_url()`, i.e. host, port and the *whole* user name. Added: the
relay's `last_error`, which reaches the model through the relay hints and `profile_status`, no longer contains the
upstream's address (python-socks writes "Couldn't connect to proxy HOST:PORT"; `relay.upstream_failure` replaces it with
`<upstream proxy>`; test `test_proxy_url.py::test_relay_errors_do_not_name_the_upstream_proxy`). Unchanged on purpose:
the CLI and the local UI (the user's own views) keep showing `RuntimeInfo.upstream`, and the proxy tools themselves
(`proxy_list`, `proxy_add`, `proxy_test(proxy=)`) still show a record's `redacted_url()` (host, port, user name) - the
records the user or the model manages there. Tests changed because the output changed on purpose:
`tests/test_proxy_check.py::test_unreachable_upstream_and_overall_timeout` (the old `u***` form) and two
`profile_create` assertions in `tests/test_server.py` (they expected `user:***@host:port`; they now assert the name
and scheme and that host and user are absent).

---

## 10. Documentation (F6, offscreen, expected differences)

- **README "Native by design":**
  - the shared hardware fingerprint across profiles (a vendor can link profiles on one machine);
  - the timezone vs exit country, and the opt-in alignment;
  - the TCP/IP OS of the exit;
  - zero ICE candidates when proxied;
  - bfcache off (F6: kept for aria-ref stability);
  - windows never report `hidden`;
  - **`offscreen` is trivially detectable** (`screenX = -32000`, no window borders), so use `normal` for stealth;
  - `http_fetch` engines and their identities.
- **`docs/DESIGN.md` §1 facts table:**
  - Runtime.enable detection on Chrome 154: the `Error.stack` getter is dead, while prepareStackTrace and timing work;
  - patchright removes `Runtime.enable` but keeps worker auto-attach;
  - `Page.navigate` gives sticky user activation;
  - an about:blank start leaves focus in the omnibox;
  - Secure DNS "automatic" bypasses the proxy;
  - the iphey browser-process crash.
- **`browser/flags.py` docstring.** Note that `--disable-back-forward-cache` is page-visible, with the ablation evidence.
- **`docs/FINGERPRINT-AUDIT.md`.** After each step, re-run the affected harness and mark the finding *fixed* with the new raw file.

---

## 11. Optional follow-ups (not needed for parity on graded checks)

- **httpx identity.** Make `engine="auto"` prefer the aligned curl_cffi engine when Scrapling is installed (after step 7), or stop borrowing the Chrome UA for httpx. A Chrome UA on a Python TLS stack is a strong mismatch signal.
- **bfcache.** Under patchright, test whether a fresh `browser_snapshot` on a bfcache-restored page resolves its refs. If it does, drop `--disable-back-forward-cache` and map "Invalid frame in aria-ref selector" to `stale_ref_error`. Back/forward already waits only for `commit`.
- **A less detectable background mode.** For example, a normal on-screen position with a non-activating start, kept behind other windows. `--disable-backgrounding-occluded-windows` already keeps it rendering. Only worth it if `offscreen` users need stealth.
- **Detach when idle.** An optional setting to disconnect CDP after N idle seconds, against periodic post-load checks. It is only useful if step 2 leaves residual signals; the costs are listed in the audit under F1, option C.
- **Re-measure on an idle single-monitor desktop:** the rAF rate, the WebGPU report hash and the sannysoft cHeight (all currently inconclusive).
- **Coverage gaps from audit §7:** a second proxy country, UDP-capable vs UDP-less proxies, IPv6, headless mode, a non-default `lang`, cross-profile cookie isolation, amiunique, fingerprint-scan, browserleaks /fonts /features, dnsleaktest and Cloudflare pages.
