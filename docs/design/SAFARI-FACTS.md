# WebKit on macOS: measured facts (spike, 2026-10-09)

These are measurements, not assumptions. They were taken on the owner's Mac to decide how ProfilePilot gets a
WebKit/"Safari" engine. The design built on them is `docs/design/SAFARI.md`.

**Harness:** `docs/design/webkit-probe/` (`run.sh`, `probe.swift`, `servers.py`, `features.swift`). Throwaway spike
code, kept so the results can be reproduced. It is not product code.

## Environment

| Item | Value |
|---|---|
| Mac | Apple M1, macOS 26.3 (25D125) |
| Safari | 26.3 (`/Applications/Safari.app`); `safaridriver` "Included with Safari 26.3" (not enabled); no Safari Technology Preview |
| Toolchain | Xcode 26.6 (`/Applications/Xcode.app/Contents/Developer`, SDK MacOSX26.5), git 2.50.1 (Apple), Homebrew 6.0.21, Python 3.12.13 + 3.14.3 (Homebrew) |
| Browsers | Google Chrome (no Edge, Brave or Chromium) |
| Code signing | No identities (`security find-identity -v -p codesigning` → 0). Ad-hoc signing (`codesign -s -`) is enough for everything below. |
| ProfilePilot fast suite | `PROFILEPILOT_SECRETS=file pytest -m "not chrome and not network"`: **681 passed, 2 skipped**. `tests/test_proxy_check.py::test_unreachable_upstream_and_overall_timeout` **hangs** on this Mac (>60 s), so it was deselected for the count. |
| Gotcha | `~/Documents` is iCloud-synced. iCloud sets `UF_HIDDEN` on files in `.venv`, and Python ≥ 3.12 skips hidden `.pth` files, so `import profilepilot` fails. Fix: the venv lives in `.venv.nosync` (iCloud ignores it), with `.venv` as a symlink; both are in `.git/info/exclude`. |

## Method

`servers.py` runs:
- an HTTP server with test pages on 127.0.0.1:47801;
- a WebSocket echo server on :47802;
- a SOCKS5 proxy on :47803 and a SOCKS5 proxy with authentication (`u`/`p`) on :47804. Both log every
  CONNECT with its address type and forward everything to 127.0.0.1;
- a UDP sink on :47805 that stands in for a STUN server.

`probe.swift` is an ad-hoc signed `.app` (bundle id `dev.profilepilot.wkprobe`, `LSUIElement`). It drives WKWebView
through AppKit only, with no Playwright, and reports page-side results to the server log.

## Results

✅ verified · ⚠️ verified with a caveat · ❌ fails as stated

| # | Fact | Result | Evidence |
|---|---|---|---|
| W1 | `WKWebsiteDataStore(forIdentifier: UUID)` is a **persistent, isolated** store per profile. | ✅ | Run 1 set an HttpOnly cookie, a JS cookie and `localStorage` in stores A and B. Run 2 (new process): A's `/get` request carried `Cookie: pp_http=A; pp_js=A` and the page read `localStorage.pp == "A"`; B saw only B; a new store C saw nothing. |
| W2 | `httpCookieStore` reads (incl. HttpOnly), sets and deletes cookies. | ⚠️ | `allCookies()` → `pp_http=A(HttpOnly)@127.0.0.1, pp_js=A@127.0.0.1`. `setCookie` and `deleteCookie` round-trip. **Caveat:** in a fresh process, `allCookies()` returned `[]` until a web view had used the store. The engine must "warm" a store (create its web view) before cookie calls. |
| W3 | `WKWebsiteDataStore.allDataStoreIdentifiers` lists the stores. `removeDataStore(forIdentifier:)` exists (SDK). | ✅ | `[A…, B…, C…]` |
| W4 | `WKWebsiteDataStore.proxyConfigurations` with `ProxyConfiguration(socksv5Proxy:)` routes **page loads, fetch/XHR and WebSocket** through that store's proxy. **DNS is resolved by the proxy.** | ✅ | The SOCKS log shows `CONNECT atyp=domain host=probe.invalid` for :47801 (page + fetch) and :47802 (WebSocket). `probe.invalid` can't resolve locally, so DNS happened at the proxy. |
| W5 | SOCKS5 with username and password via `applyCredential(username:password:)`. | ✅ | `socks-auth ok=true user=u` before every CONNECT on :47804. ProfilePilot will still point WebKit at its credential-free LocalRelay; credentials never enter WebKit. |
| W6 | Requests to `127.0.0.1` bypass the proxy. | ✅ (expected) | `P-loopback` loaded directly, with no SOCKS event. Same as Chrome's implicit loopback bypass. |
| W7 | **WebRTC bypasses the proxy.** | ❌ leak | With the proxy set, STUN binding requests went straight from the real interface (`udp src=10.0.0.10:53318` ×5). Host candidates are mDNS-obfuscated (`….local`), but server-reflexive candidates would reveal the public IP. None of WebKit's 579 features offers "proxy-only UDP". |
| W8 | Fix for W7: the `PeerConnectionEnabled` feature off. | ✅ | `RTCPeerConnection` is undefined and **0 UDP packets** are sent. Cost: a proxied WebKit profile has no WebRTC, which a page can notice. |
| W9 | **Trusted input without focus:** `NSEvent`s passed straight to the view (`wv.mouseDown/mouseUp/keyDown/keyUp`) produce `isTrusted: true` events and real text entry. | ✅ | `pointerdown:T mousedown:T focus:T mouseup:T click:T keydown:T keypress:T beforeinput:T input:T keyup:T`, and the field value is `ab`. It worked with the window behind other windows, fully off-screen, and with a view in no window at all. `NSApp.isActive` stayed false and the frontmost app didn't change. |
| W10 | Delivering the same events through `NSWindow.sendEvent` is **not** enough. | ❌ | Mouse clicks never reached the page; keys went to `BODY`. Use direct view calls. |
| W11 | `WKUIDelegate` answers `alert`, `confirm` and `prompt`. | ✅ | The page got `confirm=true`, `prompt="probe-answer"` and did not block. |
| W12 | An **isolated world** (`WKContentWorld.world(name:)`) is invisible to the page. | ✅ | Set `window.__ppSecret` in the world; the page world sees `["undefined","undefined"]` for `__ppSecret` and `window.webkit`. `callAsyncJavaScript` awaits promises (`7`). |
| W13 | Frames: a user script in the isolated world (`forMainFrameOnly: false`) plus a message handler **in that world** yield a `WKFrameInfo` per frame, including cross-origin frames. Evaluating in a child frame works. | ✅ | `main 127.0.0.1:47801`, `child localhost:47801`; child eval → `http://localhost:47801/child \| child frame` |
| W14 | `takeSnapshot` works off-screen, including a rect taller than the viewport (**full-page screenshots**). | ✅ | Viewport 1000×700; `rect` 1000×3000 → 2000×6000 px at 2× |
| W15 | Page visibility by window placement. | ⚠️ | Behind other windows: `visible`, rAF ≈60/s, timers exact. Minimized, or not in a window: `hidden`, rAF 0. Truly off-screen (-32000; needs an `NSWindow` subclass whose `constrainFrameRect` returns the rect unchanged, or AppKit moves titled windows back on screen): `hidden` by default, but **`visible` with rAF ≈58/s after `_setWindowOcclusionDetectionEnabled:NO`** (private API). Input and snapshots work in every case. |
| W16 | `applicationNameForUserAgent = "Version/26.3 Safari/605.1.15"` gives **exactly** real Safari 26.3's user agent, in JS and in the HTTP header. | ✅ | Both: `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.3 Safari/605.1.15`. `Accept-Language: en-US,en;q=0.9`. |
| W17 | `navigator.webdriver` is `false`. | ✅ | Same in real Safari. |
| W18 | Fingerprint compared with real Safari 26.3 on the same Mac. | ⚠️ | See below. |

### W18: WKWebView with the Safari UA compared with real Safari

**Identical out of the box:**
- **Identity:** `userAgent`, `appVersion`, `vendor` ("Apple Computer, Inc."), `platform` ("MacIntel"), `userAgentData` (undefined), `window.chrome` and `window.webkit` (undefined).
- **Plugins:** `plugins` (the five PDF viewer names), `mimeTypes`, `pdfViewerEnabled`.
- **Hardware:** `hardwareConcurrency`, `maxTouchPoints`, `screen` and `devicePixelRatio`, WebGL vendor/renderer (masked "WebKit"/"WebKit WebGL", unmasked "Apple Inc."/"Apple GPU") with 39 extensions, `navigator.gpu`, `getBattery`, audio sample rate.
- **Locale:** languages, locale, timezone.
- **Web APIs:** `matchMedia` set, `CSS.supports` set, `Notification` + permission, `PublicKeyCredential`, `credentials`, `clipboard`, `share`, `serviceWorker`, `storage`, speech voices, `webdriver`.

**Different by default, and fixed with WebKit's own switches** (no script injection):

| Signal | Default WKWebView | Real Safari | Fix (verified) |
|---|---|---|---|
| `ApplePaySession`, `ApplePaySetup*` | undefined | function | Feature `ApplePayEnabled` on |
| `PushManager` | undefined | function | Feature `PushAPIEnabled` on |
| `navigator.mediaDevices`, `enumerateDevices()` | undefined | `["audioinput","videoinput"]` | Feature `MediaDevicesEnabled` on |
| `outerWidth`/`outerHeight` | **0 / 0** (a classic bot tell) | window size | Private UI-delegate callback `_webView:getWindowFrameWithCompletionHandler:` returning the real window frame → 1000 × 732 |

Features are toggled with the private `-[WKPreferences _setEnabled:forFeature:]`, using the `_WKFeature` list from
`+[WKPreferences _features]`.

**Still different:**

| Signal | WKWebView | Real Safari | Status |
|---|---|---|---|
| `window.safari` (`.pushNotification`) | undefined | object | Added by Safari.app itself, with no WebKit switch. Faking it would be page-world injection, which conflicts with the "native first" rule. Recommendation: leave it absent. |
| `navigator.storage.estimate().quota` | 20.6 GB | 82.5 GB | A different quota policy. Possibly adjustable with `_WKWebsiteDataStoreConfiguration` quota ratios; **untested**. |
| `RTCPeerConnection` in **proxied** profiles | undefined | function | Deliberate (W7/W8): the price of no IP leak. Unproxied profiles keep WebRTC with mDNS host candidates, like Safari. |

## Not measured (and why)

- **safaridriver** (needs `sudo safaridriver --enable`). The owner rejected it for the identity use case:
  `navigator.webdriver = true`, a throwaway store per session, a glass pane over the window, one session at a time.
- **Playwright WebKit.** The owner rejected it as a patched, non-native build.
- **To measure during the Phase 1 build:**
  - HTTPS through the proxy (same CONNECT path; expected to work);
  - downloads (`WKDownload`);
  - popups (`createWebViewWith`);
  - permission prompts;
  - a per-process language (`-AppleLanguages`);
  - quota ratios;
  - several app processes in parallel.

## Private API used (pin and guard)

| API | Purpose |
|---|---|
| `-[WKPreferences _setEnabled:forFeature:]`, `+[WKPreferences _features]` | `ApplePayEnabled`, `PushAPIEnabled`, `MediaDevicesEnabled` on; `PeerConnectionEnabled` off when proxied |
| `-[WKWebView _setWindowOcclusionDetectionEnabled:]` | Off-screen windows stay `visible` |
| `WKUIDelegate` `_webView:getWindowFrameWithCompletionHandler:` | Real `outerWidth`/`outerHeight`/`screenX`/`screenY` |

Every call is guarded with `responds(to:)`. The app reports which of them took effect, and ProfilePilot refuses to
claim parity when one is missing. This mirrors the version-checked patch rule of `patchright_preload.js`.
