# Fingerprint and leak audit: ProfilePilot vs. plain Chrome

Chrome 154.0.8037.98 (branded), Windows 11, one machine, October 2026. The plan was
[docs/plans/fingerprint-audit.md](plans/fingerprint-audit.md), and the ordered fix list is
[docs/audit/FIX-PLAN.md](audit/FIX-PLAN.md).

Raw results are in [docs/audit/raw/](audit/raw/) and the harness scripts in [docs/audit/scripts/](audit/scripts/).
Screenshots are in `docs/audit/img/` and are local only: they are git-ignored because they can show the machine's region.

**Machine-specific values** (GPU model, hardware counts, screen size, the OS timezone and fingerprint hashes) are replaced by placeholders (`the machine's GPU`, `OS_TIMEZONE`, `HASH_A` …) in the published report; only *equality* between configurations matters for the verdicts.

**Redaction:** the machine's real public IP appears as `REAL_IP` everywhere in this document and the raw files.
The proxy exit IP appears as `PROXY_EXIT_IP`. The proxy host, user name and password appear nowhere. Proxies are referred to by
their audit names, `audit-socks5` and `audit-http`. Both are the same upstream endpoint, used as SOCKS5 and as HTTP.

---

## 1. Summary verdict

| Question | Answer |
|---|---|
| Does an **idle** ProfilePilot profile look like this machine's normal Chrome? This means started by the host, debugging port open, no client attached. | **Yes, apart from two flag effects.** B1, B1N and P0 match plain Chrome (B0) on every fingerprint vector the audit measured. The open fixed debugging port and the native switches are invisible, and `navigator.webdriver` is `false`. The two differences are the back/forward cache, which is off because of `--disable-back-forward-cache`, and in the opt-in `offscreen` mode the window position of -32000. |
| Does a profile look normal **while the agent drives it** over MCP (Playwright attached)? | **No.** The static fingerprint is still identical. But the attached Playwright client is detectable. See the next row. |
| What detects the attached client? | (1) Playwright sends `Runtime.enable` (plus auto-attach to workers). Three of 18 public detector sites flag the profile as automated (deviceandbrowserinfo, the fingerprint.com Pro demo and pixelscan's bot check), and the local probe catches it from the first line of the page. (2) One site (iphey.com) **crashes the whole browser process** while the client is attached. (3) `browser_evaluate` is flagged as main-world Playwright code (rebrowser). `browser_read`, `browser_snapshot` and every tool's visibility check also run in the main world, which a page that hooks DOM APIs can catch. (4) Weaker tells come from how ProfilePilot navigates: user activation without any input, `document.hasFocus()` false all session, and one extra `history.length` entry. |
| Same answer **with a proxy**? | **Yes, with a clean network layer.** For SOCKS5 and HTTP upstreams no page saw the real IP: IPv4, IPv6, WebRTC, DNS resolvers and headers were all clean. TLS (JA4), the HTTP/2 Akamai fingerprint and header order are byte-identical to plain Chrome. ProfilePilot is *better* than stock Chrome with the same proxy, which leaks `REAL_IP` over WebRTC. Expected differences: the timezone doesn't match the exit country, the TCP/IP OS fingerprint is the exit device's, and WebRTC gathers zero ICE candidates. Bugs: the opt-in timezone alignment misses cross-site iframes, remote (HTTP-server) mode resolves every URL through the ISP's DNS, Chrome's Secure-DNS probes go around the proxy (native behaviour, one-line fix), and `http_fetch(engine="scrapling")` claims to be macOS Chrome 150. |

**Update after FIX-PLAN steps 3 and 8 (raw: [detector-iphey-crash-isolation-step3.json](audit/raw/detector-iphey-crash-isolation-step3.json),
[detector-iphey-P-step3.json](audit/raw/detector-iphey-P-step3.json), [first-navigation-step8.json](audit/raw/first-navigation-step8.json)).**
Two root causes in the table above were wrong. (2) The iphey.com crash is not caused by the attached client: Chrome 154 crashes there
whenever its user-data-dir path is longer than 175 characters, also with no client at all, and the audit's P profiles happened to
sit 6 characters deeper than B1 (F2). With a short data root, P with patchright reads "Trustworthy". (4) The user activation does not come
from `Page.navigate` but from the driver's own evaluates, which are sent as user gestures (F4). Both are fixed or handled;
`hasFocus()` and `history.length` now match P0 for the first navigation (F5).

**Update after FIX-PLAN steps 4, 5, 6 and 9 (raw: [network-doh-step4.json](audit/raw/network-doh-step4.json),
[timezone-step6.json](audit/raw/timezone-step6.json)).** The proxied network layer has no egress around the proxy left:
Chrome opens 0 non-loopback connections (F11) and remote mode no longer resolves a proxied profile's URLs locally (F8).
The opt-in timezone now reaches cross-site iframes and every worker type, except an out-of-process iframe's first
script (F7). Model-facing text names a proxy by its saved name and scheme (F10).

**Bottom line.** ProfilePilot already achieves "a genuine Chrome" at the fingerprint level. Every remaining difference
that a site can grade comes from *how the automation client talks to Chrome*, not from the launch: the CDP
domains it enables, the JavaScript world it evaluates in and the way it navigates. Experiment PR shows the way to fix it.
In PR the same launch, plus one raw `Page.navigate` without `Runtime.enable`, passes every detector that P fails.
The fix plan's first step swaps the CDP driver to patchright 1.63.0, which is already installed. A static check of its driver bundle
shows it removes `Runtime.enable` from page and worker sessions and evaluates in an isolated world by default.

### Scorecard

| Harness | Same as baseline | Differs: bug | Differs: expected | Inconclusive |
|---|---|---|---|---|
| Local probe: 35 check groups × 11 configurations ([probe-diff.json](audit/raw/probe-diff.json)) | P 26/35 (every static fingerprint vector); PX 23/35 | P 5: prepareStackTrace, console timing, user activation, hasFocus, bfcache. P3 adds main-world traps (6). | P 3: offscreen geometry, history.length, netinfo noise. PX 6: those 3 plus WebRTC, exit IP, timezone vs geo. | 1 (rAF rate) |
| 18 public detector sites, B1 vs P, no proxy ([detector-summary.json](audit/raw/detector-summary.json)) | 13 sites | 3 sites flag "bot", 1 site crashes the browser, 1 site flags `browser_evaluate` | n/a | WebGPU report hash |
| Network and leaks, 9 proxy configurations plus 2 stock-Chrome baselines ([network-summary.json](audit/raw/network-summary.json)) | IP, DNS (local mode), IPv6, TLS/H2, QUIC, exit stability | OOPIF timezone, remote-mode DNS, scrapling identity, proxy details shown to the model | timezone vs geo, TCP/IP OS, WebRTC with zero candidates, httpx stack | n/a |

### Findings at a glance

| ID | Finding | Verdict | Severity (scraping) | Fix (see FIX-PLAN) |
|---|---|---|---|---|
| F1 | `Runtime.enable` and worker auto-attach from the attached Playwright client are detectable (prepareStackTrace and console-timing side channels; 3 detector sites) | differs-bug | **High** | Step 2: patchright driver |
| F2 | A page can crash the whole Chrome browser process (iphey.com, 5/5). **Re-diagnosed in step 3:** not DevTools; Chrome 154 crashes when the user-data-dir path is longer than 175 characters, with or without a client (the audit's P paths were 178, B1's 172) | differs-bug → native Chrome bug, path-dependent | Low with the default data root (~58 characters); High for long data roots | Step 3: **done** (warning, crash reported once, no silent restart, no restore after a crash) |
| F3 | Main-world evaluation: `browser_evaluate` flagged (UtilityScript stack, main-world DOM calls), and `read_page`, the snapshot masking and `_ensure_foreground` call DOM APIs in the main world | differs-bug | Medium | Step 2 (isolated world by default) |
| F4 | The page gets sticky user activation with no input event. **Re-diagnosed in step 8:** not `Page.navigate`; every driver evaluate (`Runtime.callFunctionOn(userGesture: true)`), already `browser_navigate`'s own title read, activates the page | differs-bug | Low | Step 8: **fixed** with patchright (patch `evaluate-without-user-gesture`); the Playwright fallback still activates |
| F5 | `document.hasFocus()` stays `false`: keyboard focus stays in the omnibox of the initial `about:blank` tab | differs-bug | Low (stays low: a CDP click gives the page focus) | Step 8: **fixed** (first URL opened by Chrome itself: at launch, or handed to the running browser) |
| F6 | Back/forward cache disabled (`--disable-back-forward-cache`) | differs-bug (deliberate) | Low | Step 10 (keep, document) |
| F7 | Opt-in timezone alignment does not reach out-of-process (cross-site) iframes, and it disappears when CDP detaches | differs-bug | Medium when the option is used | Step 6: **fixed** with a per-iframe override (residual: such an iframe's first script still sees the OS zone; overrides end when CDP detaches) |
| F8 | Remote mode (`serve --http`) resolves every URL hostname through the OS resolver (the ISP's DNS), even for proxied profiles | differs-bug | Medium (privacy) | Step 5: **fixed** (static checks only for proxied profiles) |
| F9 | `http_fetch(engine="scrapling")` sends a macOS / Chrome 150 identity on the profile's IP and cookies | differs-bug | Medium | Step 7 |
| F10 | Model-facing text shows the proxy host, port and the first 3 characters of the proxy user name | differs-bug | Low (privacy) | Step 9: **fixed** (the proxy's saved name and scheme) |
| F11 | Chrome's Secure DNS "automatic" probes go direct from `REAL_IP` around the proxy. This is native behaviour; stock Chrome with the same proxy does the same. | same as native, fix anyway | Low–medium (privacy, not site-visible) | Step 4: **fixed** (0 non-loopback connections, both proxy types) |

---

## 2. Method

Every result is a **difference from a baseline** measured on the same machine in the same session, never an absolute score.
Every configuration ran in a fresh user-data-dir with a dedicated temporary `PROFILEPILOT_HOME` and `PROFILEPILOT_SECRETS=file`.
The window was off-screen unless the name ends in N. Sites were visited read-only: nothing was clicked, submitted, solved or logged into.

| Config | What it is | Used by |
|---|---|---|
| **B0** | Plain `chrome.exe --user-data-dir=<fresh> <url>`, with no debugging port and no CDP. A `First Run` sentinel is pre-created, because a fresh dir shows Chrome 154's modal sign-in window and never opens the URL. This is a returning user's Chrome. | probe |
| B0-occluded | B0 when other windows covered it on the busy desktop: native `visibilityState: hidden`. Kept for reference. | probe |
| B0D / B0DX | Plain chrome.exe with only a debugging port (results read through an isolated world, no `Runtime.enable`) and an off-screen position. B0DX adds `--proxy-server` to an in-process LocalRelay on the same upstream: **stock Chrome plus the same proxy**. | network |
| **B1** / B1N | ProfilePilot's exact argv (`prepare_user_data_dir` + `build_chrome_args`), launched like the host does. The port is open but nothing connects while the page runs. B1N uses window=normal. | probe, detectors |
| P0 | A profile started by `RuntimeManager` with `launch.start_url`; no CDP client ever attaches | probe |
| **P** | `RuntimeManager` + `BrowserManager` (Playwright `connect_over_cdp(no_defaults=True)`, `contexts[0]`). Pages are opened with the MCP tool function `browser_navigate`. This is the way an agent uses ProfilePilot. | all |
| P2 / P3 | P2 adds a `browser_read` on about:blank before navigating. P3 adds `browser_snapshot` and `browser_read` on the probe page. | probe |
| PL | P, but navigation by a main-world `location.href=` (mitigation experiment) | probe |
| PN | P with window=normal | probe |
| PD | P, with Playwright disconnected right after `browser_navigate` returns | detectors |
| **PR** | The B1 launch on about:blank, plus **one raw CDP `Page.navigate`** (no `Runtime.enable`, no auto-attach), then detached at once | detectors |
| **PX** / **PXH** | P with `audit-socks5` / `audit-http` | probe, network |
| PXT | PX plus `lang="en-US"` and `timezone="America/New_York"` (opt-in alignment to the exit) | network |
| PXN / PXHN / PXND | PX / PXH with Chrome `--log-net-log` (diagnosis only). PXND adds Local State `dns_over_https.mode=off` (candidate fix). | network |

Three harnesses:

1. **Local probe** ([run_probe_matrix.py](audit/scripts/run_probe_matrix.py), [probe_server.py](audit/scripts/probe_server.py), [diff_probe.py](audit/scripts/diff_probe.py)).
   A 127.0.0.1 server serves a bfcache test (`/start` → `/bfb` → `history.back()`) and then `/probe`.
   The probe's inline `<head>` script runs the CDP detectors at T0 and installs main-world DOM call traps. It then collects every vector and re-runs the detectors once per second for 12 s.
   All data is POSTed back by the page itself, so the baseline needs no CDP. Request headers are recorded server-side, and `Accept-CH` asks for the high-entropy client hints.
   A single-flag ablation (B0 plus one ProfilePilot switch at a time, `raw/probe-A-*.json`) attributes each switch's effect.
2. **Public detector sites** ([run_detectors.py](audit/scripts/run_detectors.py), [summarize_detectors.py](audit/scripts/summarize_detectors.py)): 18 sites, B1 vs P, plus PD and PR where P differed.
   B1 and P are read out by the **same** isolated-world CDP extractor, so the verdicts compare like for like.
   B1 serves as the site baseline because the probe proved B1 ≡ B0 except for bfcache and the offscreen geometry.
   Root-cause scripts: [crash_isolate.py](audit/scripts/crash_isolate.py), [debug_iphey_p.py](audit/scripts/debug_iphey_p.py), [webgpu_probe.py](audit/scripts/webgpu_probe.py) and [webgpu_isolate.py](audit/scripts/webgpu_isolate.py).
3. **Network** ([run_network_audit.py](audit/scripts/run_network_audit.py), [summarize_network.py](audit/scripts/summarize_network.py)):
   - exit IP from five sources plus `proxy_test`; DNS-leak and WebRTC pages; IPv6; TLS/H2 (tls.peet.ws, browserleaks /tls);
   - a 1 s socket monitor of the profile's chrome.exe tree and the host;
   - a Chrome net-log, to diagnose the socket monitor's findings;
   - `http_fetch` with both engines;
   - unique NXDOMAIN names checked against the Windows DNS client cache, to test the URL policy's DNS.

Caveats that apply throughout:

- **The visible B0 reference.** It is the stand-alone B0 run 184000 from the same session and probe code. All six later B0 attempts were covered by other windows (see `raw/probe-B0.json`).
- **`document.hasFocus()` and the rAF rate depend on the live desktop.**
- **`src/` changed during the runs.** Another workflow was editing it, so the detector P runs reflect `src/` as of roughly 19:26–19:42 local time on the audit day.
- **One upstream proxy.** It is a sticky US residential exit in America/New_York, used as SOCKS5 and as HTTP. The planned second proxy in another country, and a UDP-capable vs UDP-less comparison, were not available.

---

## 3. Per-check results

Verdicts:
- **same**: equal to the baseline.
- **differs-bug**: a ProfilePilot difference that should be fixed.
- **differs-expected**: by design or inherent; documented.
- **inconclusive**: environment-dependent; not attributable to ProfilePilot.

### 3.1 Static fingerprint (local probe; baseline B0)

| Check | Baseline (plain Chrome) | ProfilePilot (P, P2, P3, PN, PL) | ProfilePilot + proxy (PX, PXH) | Verdict |
|---|---|---|---|---|
| `navigator.webdriver` (value, own-property, prototype) | `false` | `false`; also `false` with only the port open (B1, ablation `A-remote-debugging-port`) | `false` | same |
| UA + `navigator.*` (platform, languages, hardwareConcurrency, deviceMemory, plugins, mimeTypes, pdfViewerEnabled, cookieEnabled, DNT) | Win64 Chrome/154.0.0.0, `Win32`, `en-US,en`, hwc N, N GB, 5 PDF plugins | identical | identical | same |
| `userAgentData` brands + `getHighEntropyValues` | Chromium / Google Chrome 154.0.8037.98, "Not A(Brand" 99; Windows 19.0.0; x86/64 | identical | identical | same |
| Navigation request headers (UA, `Sec-CH-UA*`, Accept-Language, Accept, Accept-Encoding, `Sec-Fetch-*`) | `accept-language en-US,en;q=0.9`; `sec-fetch-site none`, `sec-fetch-user ?1` | identical: `Page.navigate` is sent like a typed URL. Only the PL experiment (`location.href`) sends `Sec-Fetch-Site: cross-site`. | identical | same |
| `permissions.query` (18 names), `Notification.permission` | prompt / default | identical | identical | same |
| `screen.*`, `devicePixelRatio` | W×H, avail W×H @0,0, depth 24, DPR 1, isExtended | identical | identical | same |
| Window geometry, window=normal (B1N, PN) | outer 1265×1420 @10,10; inner 1249×1325 | identical, so no infobar (innerHeight equal) | n/a (PX ran offscreen) | same |
| Window geometry, window=offscreen | screenX/Y 10,10; inner 1249×1325 | screenX/Y/Left/Top = **-32000**; inner 1265×1333 = outer width (the 8 px borders are gone); `Viewport-Width` hint 1265 | same as P | differs-expected (opt-in; see §5) |
| Intl timezone/locale, Date offsets | OS_TIMEZONE, en-US | identical | identical (no spoof; see the network table for the exit mismatch) | same |
| `matchMedia` (scheme, motion, contrast, pointer, hover, gamut, display-mode) | dark, no-preference, fine/hover, srgb, browser | identical | identical | same |
| WebGL/WebGL2 vendor, unmasked renderer, params, extensions, pixel hash | ANGLE (the machine's GPU, D3D11) | identical incl. pixel hashes | identical | same |
| WebGPU adapter info | <vendor> / <architecture> | identical | identical | same |
| Canvas 2D hash | h | h | h | same |
| AudioContext sampleRate/baseLatency + OfflineAudioContext sum | 48000, 0.01, 124.04347776696522 | identical | identical | same |
| Fonts (41; width probe) | 37/41 present | identical | identical | same |
| `speechSynthesis` voices | 19 | identical list | identical | same |
| `mediaDevices.enumerateDevices` | 1 audioinput + 1 audiooutput, no labels | identical | identical | same |
| `window.chrome` | `app`, `csi`, `loadTimes`; no `runtime` | identical | identical | same |
| Globals: the full window property list, `cdc_*`, `__playwright*`, `__pw*`, `__pwInitScripts`, `__driver*`, `Error.prepareStackTrace` | 1234 props (hash h), none present | identical hash; none present (`no_defaults=True` keeps the context clean) | identical | same |
| Dedicated worker `navigator` / Intl | same as the page | identical (Playwright's empty worker UA override has no visible effect) | identical | same |
| Storage quota, `performance.memory` | jsHeapSizeLimit 4395630592 | identical (quota follows free disk) | identical | same |
| Network Information API, RTT/Downlink/ECT hints | rtt 100, downlink 1.55–1.65 | rtt 50–100, downlink 1.5–1.75 | same range | differs-expected (live estimate; plain runs vary the same way) |

### 3.2 Automation and CDP signals (local probe; baseline B0)

| Check | Baseline | ProfilePilot | ProfilePilot + proxy | Verdict |
|---|---|---|---|---|
| Debugging port + all ProfilePilot switches, **no client** (B1, B1N, P0) | reference | identical to B0 except bfcache and the offscreen geometry; no CDP detector fires | n/a | differs-expected (only the two flag effects) |
| CDP: classic `Error.stack` getter on `console.debug` (rebrowser `runtimeEnableLeak`) | not fired | **not fired**, although the protocol trace shows `Runtime.enable` and delivered `Runtime.consoleAPICalled` | not fired | same. **This detector is dead on Chrome 154:** V8 reports `stack` as an accessor without calling it. |
| CDP: `Error.prepareStackTrace` called by `console.debug(new Error())`, by an uncaught exception and by an unhandled rejection | false / false / false (also B1, B1N, P0) | **true** at T0 and in 12/12 late samples; both async probes true | true | **differs-bug (F1)** |
| CDP: `console.debug(<3000-key object>)` ×100 timing | 0.4–1.5 ms (B0, B1, B1N, P0) | **26–35 ms** | 26–33 ms | **differs-bug (F1)** |
| Main-world DOM call traps (hooked getters/methods called by non-page code) | 0 | P/P2/PN: 0. **P3 (`browser_snapshot` + `browser_read`): 46 calls** (visibilityState ×2, body ×7, documentElement ×6, getComputedStyle ×6, getAttribute ×8, shadowRoot ×6, childNodes ×6, getBoundingClientRect ×5) whose stacks contain `UtilityScript.evaluate (<anonymous>:311:30)` / `eval at evaluate` | 0 (no reads done) | **differs-bug (F3)** |
| One main-world evaluate on about:blank before the page loads (P2 vs P) | n/a | no trace on the next document | n/a | same |
| User activation without input (`navigator.userActivation.hasBeenActive`, `new AudioContext().state`) | false / suspended | **true / running** in every `browser_navigate` config; PL and P0 false / suspended | true / running | **differs-bug (F4)**; step 8: false / suspended in P (the driver's evaluates no longer carry a user gesture) |
| `document.hasFocus()` | true in all late samples (B0, B1, B1N, P0) | **false for the whole session** in every CDP-navigated config (P, P2, P3, PL, PN). Captures show the omnibox focused with the URL selected. | false | **differs-bug (F5)**; step 8: true in every late sample, like P0 |
| Back/forward cache (`pageshow.persisted` after `history.back()`) | true (restored) | false: the page reloads (navigation type back_forward, `notRestoredReasons` null); same in B1, B1N, P0 | false | **differs-bug (F6)**. The ablation shows `--disable-back-forward-cache` is the only switch with this effect. |
| `history.length` | 2 | 3: the extra entry is the initial about:blank, which `browser_navigate` reuses | 3 | differs-expected (like a new tab plus a typed URL); step 8: equal to P0 (no about:blank entry) |
| Browser UI: automation infobar, unsupported-flag infobar, first-run / search-engine dialogs | none (with sentinel) | none in any window capture; innerHeight equals B for the same window mode | none | same |

### 3.3 Window and rendering

| Check | Baseline | ProfilePilot | ProfilePilot + proxy | Verdict |
|---|---|---|---|---|
| `document.visibilityState` | `visible` in front. A covered plain window is `hidden`, with outer size 0 and rAF stopped (B0-occluded, 6 of 7 B0 attempts, 6 of 11 ablation runs). | always `visible`, even off-screen or covered (`--disable-backgrounding-occluded-windows`) | visible | same when in front; see §5 for covered windows |
| `requestAnimationFrame` rate / `setTimeout(100)` | 242 fps (visible B0); hidden B0 stops | 137–140 fps in every config, including B1/B1N/P0 without CDP; timers 105–112 ms | 137–139 fps | inconclusive: plain-Chrome ablation runs ranged 141–242 fps with no switch correlation. It depends on monitors, VRR and z-order. |

### 3.4 Public detector sites (no proxy; baseline B1, same extractor)

| Site | Baseline (B1) | ProfilePilot (P) | Diagnostics | Verdict |
|---|---|---|---|---|
| bot.sannysoft.com (31 graded rows) | 31/31 pass | 31/31 pass; `browser_read` and `browser_screenshot` OK | `PHANTOM_WINDOW_HEIGHT` cHeight 1579 vs 1561 (stable, ungraded) | same |
| bot-detector.rebrowser.net, passive rows (`runtimeEnableLeak`, webdriver, viewport, `pwInitScripts`, bypassCsp, useragent, exposeFunctionLeak) | all green / untriggered | identical before and after `browser_read` | `runtimeEnableLeak` is dead on Chrome 154 (see 3.2) | same |
| bot-detector.rebrowser.net, page-requested calls through `browser_evaluate` (`dummyFn`, `sourceUrlLeak`, `mainWorldExecution`) | untriggered | **sourceUrlLeak RED** ("Error stack contains UtilityScript"), **mainWorldExecution RED**, dummyFn reachable | only when the agent uses `browser_evaluate` | **differs-bug (F3)** |
| browserscan.net/bot-detection (WebDriver, Selenium, headless, **CDP**, Dev Tool, …) | all Normal | all Normal | n/a | same |
| browserscan.net home (bot, authenticity, proxy, incognito, tz vs IP, WebRTC) | No bot, 100 %, tz = IP tz, WebRTC `REAL_IP` | identical | WebGPU report hash differs (next row) | same |
| WebGPU report hash (browserscan, browserleaks/webgpu) | browserleaks HASH_A… in 3 of 4 B1 runs | HASH_B… (P ×2, PD) | adapter, features, limits and WGSL identical; a later plain B1 also gave 3DC396DE | inconclusive |
| deviceandbrowserinfo.com/are_you_a_bot | "You are human!", 0 signals (2 runs) | **"You are a bot!"**: `isAutomatedWithCDP`, `isAutomatedWithCDPInWebWorker` (2/2) | PD still a bot (checks during load); **PR human** | **differs-bug (F1)** |
| bot.incolumitas.com (new tests, intoli, fpscanner, worker consistency) | all OK; fpscanner WEBDRIVER FAIL (property exists in every modern Chrome) | same verdicts | `historyLength` 1 vs 2 (expected) | same |
| fingerprint.com demo (Pro smart signals) | bot not_detected, developer_tools false, suspect score 0 | bot not_detected, **developer_tools TRUE, suspect score 8** (2/2) | PD and PR: false / 0. The visitor ID is the same across fresh profiles, B1 included (shared hardware). | **differs-bug (F1)** |
| CreepJS | 0 % headless, 0 % stealth, no lies | identical section hashes; PD identical | n/a | same |
| pixelscan.net/fingerprint-check | consistent; no masking, proxy or automation | identical | n/a | same |
| pixelscan.net/bot-check (Navigator, Webdriver, CDP, UA) | all Clear (2 runs) | **"Bot Behavior Detected"**: CDP group, `IsDevtoolOpen` (2/2) | PD and PR Clear (the check runs after load) | **differs-bug (F1)** |
| iphey.com | Trustworthy, MX 100 | **chrome.exe exits 0xC0000005** about 1 s after the page starts a dedicated worker; nothing to read (5/5) | PD crashes too; **PR Trustworthy**; raw isolation below | **differs-bug (F2)**; step 3: P (patchright) **Trustworthy** with a short data root; the crash follows the user-data-dir length |
| whoer.net | 100 %, no proxy/anonymizer | identical | n/a | same |
| browserleaks /javascript | webdriver false; inner 1265×1333, outer 1265×1420 | identical except downlink | n/a | same |
| browserleaks /client-hints | brands, platform, viewport, DPR, device-memory | identical except Downlink | n/a | same |
| browserleaks /webgl | report HASH_C…, image HASH_D… | identical | n/a | same |
| browserleaks /canvas | signature HASH_E… | identical | n/a | same |
| antcpt.com reCAPTCHA v3 score (observe only) | 0.9 | 0.9 | n/a | same |

Thirteen sites were the same, three flagged P as automated, one crashed the P browser, and one flagged `browser_evaluate`. All four
automation findings disappear in **PR**.

### 3.5 Network and leaks (baseline B0D, plus B0DX = stock Chrome with the same proxy)

| Check | Baseline | ProfilePilot (P, no proxy) | ProfilePilot + proxy (PX, PXH, …) | Verdict |
|---|---|---|---|---|
| Exit IP: ipify navigation, `fetch()` to api/api64.ipify, tls.peet.ws, browserleaks /ip, ipleak, test-ipv6 | B0D `REAL_IP`; B0DX `PROXY_EXIT_IP` everywhere | `REAL_IP` everywhere (= `proxy test --direct`) | `PROXY_EXIT_IP` everywhere, equal to `proxy_test` for both proxies; all 9 configs `all_equal_expected=true` | same |
| Real identity in anything a site showed while proxied (`REAL_IP`, IPv6, ISP, rDNS, net, region, ASN, LAN IP) | **B0DX: `REAL_IP` 3×** (WebRTC srflx; browserleaks /webrtc) | n/a (no proxy) | **0 occurrences** in PX, PXH, PXT, PXN, PXND, PXHN. A city-name label collision was verified as the exit city. | same (better than stock) |
| DNS leak, browserleaks /dns | B0DX: 83 Google resolvers (proxy side); B0D: user ISP | user ISP (12 servers) | 73–84 servers, all Google LLC (proxy side), no ISP row | same |
| DNS leak, ipleak.net | n/a | user ISP | Google LLC only | same |
| Hostname resolution path | n/a | n/a | SOCKS5 relay uses remote DNS (`rdns`); the net-log shows no DNS transaction for any visited host, for both the SOCKS5 and HTTP upstreams | same |
| WebRTC, browserleaks /webrtc | **B0DX public IP = `REAL_IP`** (leak) | public `REAL_IP` (normal, no proxy) | "No Leak", local -, public - | differs-expected (by design) |
| WebRTC, local STUN gather | host mDNS + srflx `REAL_IP` (B0D and B0DX) | host mDNS + srflx `REAL_IP`, no LAN IP | **0 candidates**, gathering complete | differs-expected (`disable_non_proxied_udp`) |
| IPv6 (test-ipv6, ipleak) | n/a | none (this machine has no global IPv6) | IPv4 exit only, 0/10 IPv6 | same (no local IPv6, so a v6 leak could not be exercised) |
| TLS/H2, tls.peet.ws: JA4, ja4_r, peetprint, extension and cipher lists, Akamai h2, header order | JA4 `t13d1517h2_8daaf6152771_cb7bf5808d99`, Akamai `52d84b11…` | identical on every field | identical (PX, PXH, PXT) | same. JA3 varies per connection in every config: Chrome permutes extensions. |
| TLS, browserleaks /tls | same JA4 / JA3_n | same; X25519MLKEM768, ECH GREASE | same | same |
| QUIC / HTTP version | h2 | h2 | h2; `--disable-quic` when proxied | same |
| TCP/IP (p0f / JA4T) OS fingerprint | B0D Windows, TTL 115; B0DX like PX | Windows, TTL 115 | browserleaks says **"Android"**, TTL 49–51, MSS 1400: the exit device's TCP stack, while UA and client hints say Windows | differs-expected (inherent to the proxy; same in B0DX) |
| Timezone / language vs exit (native mode) | n/a | OS_TIMEZONE, en-US | page **OS_TIMEZONE** (main frame, workers, OOPIF) vs exit **America/New_York**; language en-US is consistent with the US exit | differs-expected (native mode) |
| Opt-in alignment (PXT: `lang=en-US`, `timezone=America/New_York`) | n/a | n/a | main frame, dedicated worker and shared worker all NY; whoer 100 %. **A cross-site (out-of-process) iframe reports OS_TIMEZONE.** Everything reverts to LA after CDP disconnects. | **differs-bug (F7)** |
| Exit stability (5 browser + 5 `http_fetch` over ~1 min; `proxy_test` 30 min apart) | n/a | n/a | always `PROXY_EXIT_IP` (sticky) | same |
| `http_fetch` exit IP (httpx and scrapling) | n/a | `REAL_IP` (no proxy) | `PROXY_EXIT_IP` for both engines | same |
| `http_fetch` engine=httpx (default/auto) fingerprint | browser: JA4 `t13d1517h2…`, h2, client hints | Chrome 154 UA on **Python TLS** (JA4 `t13d1812h1…`), HTTP/1.1, no client hints | same through the exit | differs-expected (documented plain client; risky, see §5) |
| `http_fetch` engine=scrapling fingerprint | browser: Windows Chrome 154 | **macOS Chrome/150** UA, Chrome 150 `sec-ch-ua`, JA4 `t13d1516h2_8daaf6152771_806a8c22fdea` | same mismatch through the exit | **differs-bug (F9)** |
| Traffic outside the relay (socket monitor + net-log) | **B0DX: the same direct TCP 443 to the system DoH server** | many direct connections (no proxy) | Chrome's network service opens **direct TCP 443 to `SYSTEM_DOH_SERVER`** about 7 s after launch and keeps it alive, plus UDP DNS for the DoH host, all from `REAL_IP`. Secure DNS "automatic" mode probes are sent with LOAD_BYPASS_PROXY. **PXND (`dns_over_https.mode=off`): 0 non-loopback TCP, 0 DNS transactions**, and leak results unchanged. The host process talks only to the proxy and loopback. | same as native; fix anyway (F11) |
| URL-policy DNS lookups, local (stdio) vs remote (`serve --http`) mode | n/a | n/a | local mode: test names **not** in the OS DNS cache. **Remote mode: `browser_navigate` and `http_fetch` names ARE in the OS cache**, i.e. sent to the ISP resolver, although the profile is proxied. | **differs-bug (F8)** |
| Proxy details visible to the model | n/a | n/a | `http_fetch` route line, `RuntimeInfo.upstream`, relay hints: `socks5://<3 chars of user>***:***@<proxy host>:<port>` | **differs-bug (F10)** (privacy) |

---

## 4. Bugs: root cause, severity, fix

The severity scale for scraping:
- **High**: graded today by public or commercial detectors, or it breaks the session.
- **Medium**: a targeted check that some sites do, or a real privacy leak.
- **Low**: unusual but rarely graded.

### F1. The attached Playwright client is detectable through `Runtime.enable` (High)

**Evidence.**

*Local probe.* In every configuration with a Playwright client (P, P2, P3, PL, PN, PX, PXH), the probe catches it from the
page's first script (T0) and in 12/12 late samples:
- `Error.prepareStackTrace` is invoked by `console.debug(new Error())`, by an uncaught exception and by an unhandled rejection.
- `console.debug(<3000-key object>)` ×100 takes 26–35 ms instead of 0.4–1.5 ms.

B0, B1 (port open, no client), B1N and P0 are clean.

*Sites.* Each of these is flagged in P and clean in B1 (`raw/detector-*-P.json`, plus repeat runs):
- deviceandbrowserinfo: `isAutomatedWithCDP`, `isAutomatedWithCDPInWebWorker`
- the fingerprint.com Pro demo: `developer_tools=true`, suspect score 0 → 8
- pixelscan bot-check: `IsDevtoolOpen`

*Isolation.*
- **PR**: the same launch plus one raw `Page.navigate`, no `Runtime.enable`, no auto-attach. It equals B1 on all three sites, which rules out the flags, the host, the debugging port and `Page.navigate` itself.
- **PD**: Playwright dropped right after `browser_navigate`. It fixes the two sites that check after load (fingerprint.com, pixelscan) but not deviceandbrowserinfo, which checks during load.

**Root cause.** `automation/manager.py` `BrowserManager._attach` calls `playwright.chromium.connect_over_cdp`.
A `DEBUG=pw:protocol` trace shows the following on every page session:
- `Runtime.enable`, `Log.enable`, `Network.enable` and `Page.enable`;
- `Target.setAutoAttach({autoAttach: true, waitForDebuggerOnStart: true, flatten: true})`;
- `Runtime.enable` on each auto-attached dedicated worker.

With `Runtime` enabled, V8's inspector builds RemoteObjects and stack traces for every console call and every exception. That work runs page-observable code (`prepareStackTrace`) and costs measurable time.

The same applies to every other Playwright attach in the codebase:
- `client.py` `_with_profile_context` (cookie operations) briefly attaches to *all* open tabs;
- the Scrapling browser session (`integrations/scrapling.py`, a `DynamicSession` subclass) attaches too.

**Note on the plan's assumption.** The plan expected the "`Error.stack` getter fires on `console.debug`" trick to be the detector.
On Chrome 154 that trick is dead: V8 sends `stack` as an accessor without invoking it, which the trace verifies. So
bot-detector.rebrowser.net's `runtimeEnableLeak` row shows **green while Playwright is attached**. Regression tests must use the
`prepareStackTrace` and timing variants in [probe_server.py](audit/scripts/probe_server.py) instead.

**Severity: High.** A commercial vendor (fingerprint.com Pro) exposes this signal to its customers, and two public
detectors grade it. The side channel needs no special access, works from workers too, and fires from the first script of
every document while ProfilePilot is attached, which in practice is the whole session.

**Fix options and trade-offs.**

| Option | What it fixes | Cost / risk |
|---|---|---|
| **A. Switch the CDP driver to patchright** (1.63.0 is already installed with Scrapling) | A static check of its driver bundle (`patchright/driver/package/lib/coreBundle.js`) shows three changes. Frame sessions no longer send `Runtime.enable`. Dedicated workers get their context id from `Runtime.evaluate("globalThis")` instead of `Runtime.enable`. `page.evaluate`, `locator.evaluate` and `evaluate_all` default to `isolated_context=True`. `connect_over_cdp(no_defaults=...)` and `aria_snapshot(mode="ai")` exist with the same signatures. Expected to fix the probe detectors, deviceandbrowserinfo (page and worker), fingerprint.com, pixelscan, and F3. | A third-party fork that follows Playwright with some lag. It still sends `Target.setAutoAttach(waitForDebuggerOnStart)` and `Network.enable` to workers, **so it is not expected to fix F2**. It disables console events; ProfilePilot uses none (grep: no `on("console")`). `browser_evaluate` semantics change: an isolated world cannot see page JS globals, so add an explicit `world="main"` opt-in. aria-ref resolution, dialogs, downloads and the timezone session must be re-verified. |
| B. Keep Playwright and apply rebrowser-patches | Same idea as A | Patches Playwright's JS on disk and is tied to specific versions; it breaks on every upgrade and is harder to ship in a wheel or `.mcpb`. |
| C. Keep CDP detached between tool calls | Post-load checks only (PD: fingerprint.com, pixelscan) | Does not fix load-time checks (deviceandbrowserinfo) or F2, and the profile is still exposed during every tool call. Each call pays the attach cost. While detached, the per-session state is lost: the timezone override, download routing, dialog auto-answer, new-tab tracking and the active tab. |
| D. ProfilePilot's own raw-CDP driver (PR-style: `Page.navigate`, isolated-world `Runtime.evaluate`, no `Runtime.enable`, no auto-attach) | Everything PR fixes, F2 included | A large rewrite: aria snapshots, refs, actionability checks and input would have to be reimplemented. Long-term option only. |

**Recommendation.** Do A now, behind a single driver module with a config switch so Playwright stays available as a fallback.
Gate it on the new local regression test, then re-run the four flagged sites. Consider C only as an optional "detach when idle" setting
for long idle periods, and D only if A regresses.

### F2. A page crashes the Chrome browser process while DevTools instrumentation is attached (High, availability; re-diagnosed in step 3: user-data-dir path length)

**Evidence.** On iphey.com, `chrome.exe` (the browser process) exits with 0xC0000005 (3221225477) about 1 s after the page starts a
dedicated worker. This happened in 5/5 P attempts. The host logs `browser exited (code 3221225477)` and exits, every tab dies, and the next tool call autostarts a fresh browser.
PD crashes too, while B1 (2 runs) and PR are "Trustworthy / MX 100".

The raw isolation in [detector-iphey-crash-isolation.json](audit/raw/detector-iphey-crash-isolation.json):

| Experiment | Result |
|---|---|
| attach only + `Page.navigate` | alive after 20 s |
| `Runtime.enable` on the tab alone | **crash** |
| `Target.setAutoAttach(waitForDebuggerOnStart, flatten)` alone | **crash**, about 1 s after "child worker / resumed worker" |
| Playwright | **crash** |

**Root cause.** Most likely a Chrome 154 browser-process bug triggered when DevTools instruments a page or its worker. The site may
trigger it on purpose as an anti-DevTools trap. The ProfilePilot switches are not involved (B1 is fine).
Any client that enables `Runtime` or auto-attaches workers during load triggers it.

**Severity: High for availability.** A single site takes down the whole profile, every tab included. This is also a
detection outcome: the site prevents automated sessions. A crash loop is a further risk: with `restore_session` the crashing tab is
restored on the next start and the client attaches again. This was not tested.

**Fix.**
1. Patchright (F1-A) removes `Runtime.enable` but keeps worker auto-attach, so **verify iphey after F1-A**. The static check predicts it still crashes.
2. If it does, the only way to avoid the crash is to not auto-attach workers during load: a `Target.setAutoAttach` `filter` that excludes `worker`. That needs a driver patch, because Playwright has no option for it, and ProfilePilot never uses worker handles. The other route is option D.
3. Independently, make the crash legible. When the host reports exit code 0xC0000005 while a tool call is in flight, the tool error should say "the page crashed Chrome while it was being automated". It should also stop the next autostart from silently reopening the same tab.
4. Report the browser-process crash upstream with `crash_isolate.py` as a reproduction. Don't include the site's code.

**Update (FIX-PLAN step 3): re-diagnosed, the cause is the user-data-dir path length.** Raw:
[detector-iphey-crash-isolation-step3.json](audit/raw/detector-iphey-crash-isolation-step3.json) (`crash_isolate.py` now records `udd_len`
and takes `--udd-len`).

| Experiment (Chrome 154, ProfilePilot's switches, iphey.com) | user-data-dir ≥ 176 characters | ≤ 175 characters (84–99 in the matrix) |
|---|---|---|
| `startup`: the URL on Chrome's command line, **no CDP client at all** | **crash** (0xC0000005, ~6 s) | alive |
| `none`, `runtime`, `autoattach_ev` (each child resumed through its own session), `autoattach_filtered` (no worker auto-attached) | **crash** | alive |
| `playwright`, `patchright` (ProfilePilot's driver) | **crash** | alive |

- Every one of 19 runs with a user-data-dir of 176–197 characters crashed; none of about 30 runs at 175 or less did (bisection
  with no client: 172, 173, 174 and 175 alive, 176, 179 and 181 crashed). At 175 Chrome creates
  `GPUPersistentCache/DawnGraphiteCache/<32 chars>/cache.db`, `cache.db-wal` and `cache.journal` (83 characters below the
  user-data-dir); at 176 the folder stays empty: the GPU cache database cannot be created under the Windows path limit (260), and
  when iphey.com's page then uses the GPU, the browser process apparently dereferences the missing database (an inference from
  the files; Chrome's crash dump was not analysed). A site-independent reproduction page has not been found yet, so the upstream
  report still needs the site or more work.
- The audit's own paths explain every original result. It ran under the same 126-character scratch root
  (`<scratch>/audit-home/detectors-noproxy` = 156): B1 and PR used `b1|pr/iphey-<time>` (172, Trustworthy), P and PD
  `profiles/<id>/udd` (178, crash), and the isolation folders `crash/<exp>-<time>` were 174 for `none` (alive) and 177–183 for
  `runtime`, `autoattach`, `autoattach_rt` and `playwright` (crash). The DevTools attribution followed the experiment-name length.
- With a short data root, P (patchright, `browser_navigate` + `browser_read` + `browser_screenshot`) reads **Trustworthy** like
  B1 ([detector-iphey-P-step3.json](audit/raw/detector-iphey-P-step3.json), [detector-iphey-B1-step3.json](audit/raw/detector-iphey-B1-step3.json)).
- **Decision on worker auto-attach:** no driver patch (option a is not needed): auto-attach is not the cause, and the filter
  alone still crashes with a long path. ProfilePilot's default data root (`%LOCALAPPDATA%\ProfilePilot\profiles\<id>\udd`) is
  about 58 characters plus the Windows user name, far below the limit.
- **What ProfilePilot does now:** `browser/prefs.py` `MAX_USER_DATA_DIR_CHARS = 175`; the host logs a warning at launch when the
  user-data-dir is longer. When Chrome exits with an NTSTATUS crash code that no stop asked for, the host writes
  `profiles/<id>/last_exit.json` and logs `it crashed (access violation (0xC0000005))`. The next tool call (or the call that was
  running) reports "Chrome crashed while this page was open ... The profile was not restarted" once, with the path hint when it
  applies, instead of "the tab was closed" and a silent restart.
- **Crash loop (verified):** with `restore_session` the restarted browser restored the crashing tab next to the agent's new URL and
  crashed again 6 s later, in 3 of 3 rounds. The first start after a crash therefore runs without `--restore-last-session` (like
  Chrome, which does not restore by itself after a crash): 0 further crashes ([first-navigation-step8.json](audit/raw/first-navigation-step8.json)).
  The tabs (and session cookies) of the crashed run are not restored.

### F3. Main-world evaluation (Medium)

**Evidence.**
- *rebrowser*: `browser_evaluate` → `sourceUrlLeak` RED ("Error stack contains UtilityScript") and `mainWorldExecution` RED.
- *Local probe*: in P3, `browser_snapshot` + `browser_read` produced 46 calls to hooked main-world DOM APIs, with stack frames
  `UtilityScript.evaluate (<anonymous>:311:30)` / `eval at evaluate`.
  This is a recognizable Playwright signature. Playwright 1.63 no longer uses the old `__playwright_evaluation_script__` sourceURL; its bundle has no such string. The `UtilityScript` frame is what leaks now.
- Plain navigation (P) left 0 traces: `browser_navigate`'s own evaluate runs on about:blank before the target loads, and `page.title()` runs in Playwright's utility world.

**Root cause.** All of these run in the page's main world:
- `server/tools_browser.py` `browser_evaluate`: `page.evaluate(expression)`.
- `automation/manager.py` `ProfileSession._ensure_foreground` and `_guess_foreground`: `page.evaluate("document.visibilityState")`. **Every tool** runs this through `session.page()`.
- `automation/content.py`:
  - `read_page`: `page.evaluate(_READ_JS)` and `locator.evaluate_all`;
  - `visible_html` / `full_html`: `_evaluate`;
  - `mask_sensitive_values`: `locator.evaluate(SENSITIVE_FIELD_JS)`, run during every snapshot.
- `tools_browser.py`: `pdf_hint`, the scroll-position and screenshot-size evaluates, and `browser_select_option`.
- The `automation/typing.py` and `autofill.py` locator evaluates.

The aria snapshot itself and locator actions run in Playwright's utility world and were not trapped.

**Severity: Medium.** It only matters on pages that hook DOM prototypes, but anti-bot scripts do exactly that, and the
`visibilityState` call happens on **every** tool call. `browser_evaluate` with page-requested calls is flagged today.

**Fix.**
- With F1-A (patchright), every `evaluate` defaults to an isolated world. Add `world: "isolated" | "main" = "isolated"` to `browser_evaluate`, and document `main` as detectable: it is only for reading page JS state such as `window.__NEXT_DATA__`.
- Without patchright, the fallback is an isolated-world helper: `Page.createIsolatedWorld` on the frame, then `Runtime.evaluate(contextId=…, returnByValue=True)`. This needs no `Runtime.enable`.
- Without patchright, also replace element evaluates with utility-world locator methods, for example `locator.get_attribute("type")` instead of `e => e.type === 'password'`.

### F4. `browser_navigate` grants user activation without input (Low; re-diagnosed and fixed in step 8: the driver's user-gesture evaluates)

**Evidence.**
- In every `browser_navigate` configuration, `navigator.userActivation.hasBeenActive === true` and `new AudioContext().state === "running"` (autoplay allowed) before any input.
- B0, B1, B1N and P0 (command-line URL) and PL (renderer-initiated `location.href` from the same attached session) all show `false` / `suspended`.
- The activation is sticky across the probe's later same-origin navigations.

**Root cause.** `page.goto` → CDP `Page.navigate` is a browser-initiated navigation that Chrome treats as user-gesture
navigation, so the new document starts with sticky activation. Attaching alone does not cause it: PL shows no activation.
It is **not verified** whether a URL a human types into the omnibox gets the same activation. If it does, this is natural. Either way, a page can pair "activated" with
"zero trusted input events so far".

**Severity: Low.** Few scripts check it, and the signal becomes natural once the agent has clicked or typed on the page.

**Fix.** See F5: the first navigation can avoid `Page.navigate` entirely. Do **not** switch to `location.href`. PL shows
it sends `Sec-Fetch-Site: cross-site` instead of `none`, which is a worse tell for the first request than the activation.

**Update (FIX-PLAN step 8): re-diagnosed and fixed.** A page that reports its own state every 50 ms
([first-navigation-step8.json](audit/raw/first-navigation-step8.json)) shows that `Page.navigate` grants no activation: a document
opened with it starts un-activated. The activation arrives tens of milliseconds later with the driver's next evaluate, because
Playwright and patchright send every `Runtime.callFunctionOn` (`evaluate`, `page.title()`, aria snapshots, actionability checks)
with `userGesture: true`. `browser_navigate` itself reads the title, so every config activated the page within the first tool call.
ProfilePilot's patchright driver now carries a second in-memory patch, `evaluate-without-user-gesture`, which sends
`userGesture: false` on the default context of its `no_defaults` connections (checked against the installed bundle before the
driver starts; `tests/test_driver.py`). Measured: the page stays un-activated through `browser_navigate`, `browser_snapshot`,
`browser_read`, `browser_screenshot`, `browser_scroll`, `browser_evaluate` and a second `Page.navigate`, and becomes activated
by `browser_click`, as for a person. `tests/test_native_fingerprint.py::test_reading_tools_never_activate_the_page` guards it (a
strict xfail with the Playwright fallback, which keeps its gestures). Side effect: `browser_evaluate` code has no user gesture, so
the popup blocker stops its `window.open` (documented; open popups with `browser_click`). The "human-typed URL" question below is moot.

### F5. `document.hasFocus()` is false for the whole session (Low; fixed in step 8)

**Evidence.**
- Every CDP-navigated configuration had `hasFocus()` false in 12/12 late samples. B0, B1, B1N and P0 were true.
- The window captures of P and PN show the omnibox focused with the URL selected.
- A bare CDP attach did not flip focus in a scratch attach-while-watching test.
- PL (script navigation) is false too, so the cause is the about:blank start, not `Page.navigate`.

**Root cause.**
- With no start URL, `browser/flags.py` appends `about:blank`, and Chrome puts keyboard focus in the omnibox on a blank tab.
- Neither `Page.navigate` nor a script navigation moves focus into the web contents.
- P0, which has the probe as its command-line start URL, has page focus.

**Severity: Low as measured.** Not tested: CDP input events go straight to the renderer and do not move browser-level focus. Clicks and keys could therefore arrive while `hasFocus()` is still `false`, which a human cannot produce. If that holds, the severity is medium.

**Fix (with F4).** Open the *first* URL the way a person or another app opens one, not with `Page.navigate`:
1. If `browser_navigate` autostarts a stopped profile, pass the destination as the launch start URL. This is exactly P0: no activation, page focus, `Sec-Fetch-Site: none`, `history.length` like B0.
2. If the profile is running and the active tab is still the untouched initial blank tab, hand the URL to Chrome's command line: `chrome.exe --user-data-dir=<udd> --profile-directory=Default <url>`. The running browser opens it in a new tab, as for a link clicked in another app. Then close the blank tab. This must be verified with the probe first; the expectation is P0-like.
3. Later navigations keep `Page.navigate`.

Trade-offs:
- The start URL is visible in the local process command line.
- A saved session plus a start URL opens an extra tab (already documented in `flags.py`).
- The handoff spawns a short-lived chrome.exe.

**Update (FIX-PLAN step 8): fixed for the first navigation.** Both routes are implemented:
`browser_navigate` on a stopped profile starts it with the destination (`RuntimeManager.start(start_url=)` → host `--start-url`,
"Opened at launch"); on a running profile whose only tab is still its initial `about:blank`, the host's `POST /open` runs
`chrome.exe --user-data-dir=<udd> --profile-directory=Default <url>` (with the relay's proxy switches, so a browser that died meanwhile
could never start unproxied; such a process is killed), the new tab is adopted and the blank one closed. Measured with the probe
against P0 in the same run, for both routes: `Sec-Fetch-Site: none`, no user activation, `AudioContext` suspended, `history.length`
equal and `hasFocus()` true in every late sample (`test_first_navigation_is_like_a_typed_url`). The hand-off did not change the OS
foreground window (`GetForegroundWindow` polled for 4 s; [first-navigation-step8.json](audit/raw/first-navigation-step8.json)); it is
never used for a minimized window (Chrome would restore it) or a headless one. `hasFocus()` turned out to be measurable off-screen:
Chrome reports focus for its active window while another application has the OS foreground (B1 and P0 were true), so the test
compares with P0 instead of opening a visible window. Open question 4 of the plan is answered: a CDP click gives the page focus
(from about:blank + `Page.navigate`, `hasFocus()` became true with the first `browser_scroll` or `browser_click`), so clicks never
arrive while it is false and F5 stays low. Later navigations keep `Page.navigate`.

Do not use `Emulation.setFocusEmulationEnabled`. It fakes focus even when the window really is in the background, which is not native.

### F6. Back/forward cache disabled (Low, deliberate)

**Evidence.** After `history.back()`, B0 restores the page (`pageshow.persisted` true). Every ProfilePilot configuration, B1 and P0 included, reloads it instead.
The single-flag ablation of 10 switches shows `--disable-back-forward-cache` is the only one with this effect.

**Root cause.** `browser/flags.py` passes it on purpose. A page restored from the bfcache yields aria refs that Playwright 1.63 cannot
resolve ("Invalid frame in aria-ref selector"), and `go_back(wait_until="domcontentloaded")` would time out.

**Severity: Low.** Pages and RUM scripts can count bfcache restores, but few, if any, anti-bot products grade it.

**Fix.** Keep the flag and document it as a known difference. Revisit after the driver swap. If a fresh snapshot on a
bfcache-restored page then works under patchright, drop the flag: `browser_navigate` back/forward already waits only for
`commit`. Then map the "Invalid frame in aria-ref selector" error to the stale-ref message.

### F7. Opt-in timezone alignment misses cross-site iframes (Medium when used)

**Evidence.** PXT's collector (`raw/network-PXT.json`) shows America/New_York in the main frame, the dedicated worker and the shared worker.
The cross-site iframe, a separate `iframe` target (an out-of-process iframe, OOPIF), shows **OS_TIMEZONE**. After disconnecting, every context reverts to Los Angeles.
Service workers were not tested.

**Root cause.** `ProfileSession._apply_timezone` sends `Emulation.setTimezoneOverride` on `context.new_cdp_session(page)`, which covers only
the page target's renderer. Under site isolation, third-party captcha, anti-bot and ad iframes run in other renderers and keep the real
timezone. The override also lives only while that CDP session is attached.

**Severity: Medium when `launch.timezone` is used.** Anti-bot iframes are exactly the OOPIFs that report the time zone, and
they can compare it with the top frame.

**Fix.**
1. Try a launch-level mechanism first, because it covers every renderer and survives detaching. Candidate: Chromium's `--time-zone-for-testing=<IANA>` switch. Verify on Chrome 154 that it reaches the main frame, OOPIFs, dedicated, shared and service workers, shows no infobar, and is not page-visible. Being a "for testing" switch, it may disappear in a later Chrome.
2. If it does not work, apply the override per OOPIF. On `page.on("frameattached")` / `framenavigated`, try `context.new_cdp_session(frame)`, which only succeeds for out-of-process frames, and keep those sessions. There is a race: the iframe's first scripts can run before the override lands. Document it.

**Update (FIX-PLAN step 6; raw: [timezone-step6.json](audit/raw/timezone-step6.json)).** Option 1 does not exist on
branded Chrome 154: `--time-zone-for-testing` is in none of its binaries, and with it every context kept the OS zone.
Option 2 is implemented (`framenavigated`, plus the frames present at attach), and a profile with `launch.timezone`
now gets its first URL navigated in the already overridden tab instead of opened at launch (step 8), so the page's own
first script has the zone too. Measured 4/4 with `Asia/Tokyo`: the main frame from its first script, dedicated, shared
and **service** workers, the cross-site iframe (after its first script) and the iframe's worker all report it; without
the per-frame override the iframe and its worker report `OS_TIMEZONE`, as in PXT. **Residual:** the
out-of-process iframe's first script still reads the OS zone (4/4; the earliest hook is its commit), new tabs and popups
have the same race, and every override still ends when CDP detaches. Severity: Low-Medium when the option is used.

### F8. Remote mode resolves URLs through the OS resolver even for proxied profiles (Medium, privacy)

**Evidence.**
- `raw/network-PX.json` `dns_policy_test`: with `UrlPolicy(remote=True)`, the unique names opened by `browser_navigate` and `http_fetch` appear in the Windows DNS client cache. In local mode they do not.
- A direct `getaddrinfo` control name does appear, so the method is valid.

**Root cause.** `safety.UrlPolicy.acheck` → `loop.getaddrinfo` is called whenever `restricts_private` is true (remote mode without `--allow-private-network`). Its callers are:
- `tools_browser.check_url` (`browser_navigate`, `browser_tabs` new)
- `enforce_final_url` (after every action)
- `http_fetch`'s per-redirect `on_request` hook and final-URL check

Every hostname an agent opens over the HTTP server therefore goes to the local ISP's resolver, which defeats the proxy's privacy.

**Severity: Medium.** Sites cannot see it, but the ISP sees every hostname. That is a real DNS leak for remote users.

**Fix.** For proxied profiles, skip the DNS step and keep the static checks. Literal IPs, every numeric spelling, `localhost` and
`*.localhost` stay blocked, and so do link-local and private literals.
A hostname is then resolved at the proxy and connected from the proxy's network, so it cannot reach the user's LAN.
The static checks are still needed because Chrome's implicit proxy bypass sends `localhost` and loopback literals direct.

Trade-off: a hostname that resolves to a private address *on the proxy's side* becomes reachable through the proxy. That network is not the user's; document it.
The alternative, resolving over DoH through the relay, is more code.

**Update (FIX-PLAN step 5): fixed.** `UrlPolicy.check/acheck(resolve=False)` runs the static checks only. Remote-mode
tools use it when the profile's traffic leaves through an upstream proxy: a running profile is judged by its live relay
(it must have an upstream), a stopped one by its saved proxy. A profile that runs without a proxy, ShardX profiles, and
the settings-time check of a profile's `launch.start_url` keep resolving.
`tests/test_tools_unit.py::test_proxied_profile_remote_mode_does_not_resolve_locally` fails every local lookup and drives `browser_navigate`
(at launch and later) and `http_fetch` (through a real relay) on a proxied profile: 0 local lookups, while local names
and private literals are still refused.

### F9. `http_fetch(engine="scrapling")` impersonates a different browser and OS (Medium)

**Evidence.** In `raw/network-PX.json` (`http_fetch scrapling:peet`):

| | `http_fetch` (scrapling) | The profile's browser |
|---|---|---|
| UA | `Macintosh; Intel Mac OS X 10_15_7 … Chrome/150.0.0.0` | Windows Chrome 154 |
| `sec-ch-ua` | Chrome 150 brands | Chrome 154 brands |
| JA4 | `t13d1516h2_8daaf6152771_806a8c22fdea` | `t13d1517h2_8daaf6152771_cb7bf5808d99` |

Both share the same exit IP and cookies.

**Root cause.** `integrations/scrapling.ProfileFetcherSession` (used by `tools_data._fetch_scrapling`) uses curl_cffi's default
impersonation profile and its macOS header set. Unlike `_fetch_httpx`, it does not take the browser's UA, platform or version.

**Severity: Medium.** Sites that correlate TLS and header identity across requests on one IP or cookie jar see two different
devices alternating within one session.

**Fix.** Build the fetcher's default headers from the profile's browser:
- `User-Agent` from `Browser.getVersion`;
- `sec-ch-ua`, `sec-ch-ua-mobile` and `sec-ch-ua-platform` from `navigator.userAgentData`, read once in an isolated world and cached per session;
- `Accept-Language` as in httpx.

Pick curl_cffi's newest Chrome target as `impersonate`.

Trade-off: the TLS ClientHello stays curl_cffi's Chrome 14x (JA4 `…1516…` vs `…1517…`), close to but not identical with Chrome 154.
User headers still override.

### F10. Proxy details in model-facing output (Low, privacy)

**Evidence.**
- `ProxyEndpoint.redacted()` (`proxy/url.py`) returns `scheme://<first 3 chars of user>***:***@<host>:<port>`.
- That string reaches the model through `RuntimeInfo.upstream` (`browser/host.py`), `http_fetch`'s route line (`tools_data.py`), `relay_stats` and the relay hints.
- It also ended up in two raw files of this audit before redaction.

**Root cause.** `redacted()` was designed for logs and is reused as model-facing text.

**Fix.** Model-facing text names the proxy by its saved name and scheme, for example `via the profile's proxy 'audit-socks5' (socks5)`. `redacted()` keeps no part of the user name.

**Update (FIX-PLAN step 9): fixed.** `redacted()` gives `scheme://***:***@host:port`. The `http_fetch` route line,
`profile_start` / `profile_status`, `proxy_test(profile=)` and the profile tools' saved-proxy label name the proxy by
its saved name and scheme (the profile label used to show `ProxyRecord.redacted_url()`, i.e. the *whole* user name), and
the relay's error text, which reaches the model in hints, has the upstream's address replaced. `RuntimeInfo.upstream`
stays for logs, the CLI and the local UI; the proxy tools themselves (`proxy_list`, `proxy_add`, `proxy_test(proxy=)`)
still show the saved records with host, port and user name.

### F11. Secure DNS "automatic" probes bypass the proxy (native; Low–medium privacy)

**Evidence.**
- In PX, PXH, PXT, PXN and PXHN, Chrome's network service opens direct TCP 443 to `SYSTEM_DOH_SERVER` about 7 s after launch, before any navigation, and keeps it alive for minutes. It also sends plain UDP DNS for the DoH host to `SYSTEM_DNS_SERVER:53`. All of this comes from `REAL_IP`.
- The net-log attributes the traffic to `DNS_OVER_HTTPS` probes (secure_dns_mode 1, the auto-upgraded template of the system resolver). Chromium sends DoH requests with `LOAD_BYPASS_PROXY`.
- Stock Chrome with the same proxy (B0DX) does exactly the same, so this is native behaviour.
- No visited hostname went this way, and pages cannot see it.

**Verified fix (PXND).** When the profile is proxied, seed Local State `{"dns_over_https": {"mode": "off"}}` before launch.
The result was 0 non-loopback TCP, 0 DNS transactions and secure_dns_mode 0, with the DNS-leak, exit-IP and WebRTC results unchanged.
The pref is not MAC-protected and pages cannot observe it.

Trade-off: no Secure DNS for proxied profiles. Hostnames resolve at the proxy anyway, so nothing is lost.

**Update (FIX-PLAN step 4): fixed** ([network-doh-step4.json](audit/raw/network-doh-step4.json)). `prepare_user_data_dir(proxied=True)`
merges the pref before every proxied launch and records the previous mode in a marker; the first launch
without a proxy restores it (a mode the user picked meanwhile is kept, and without the marker nothing is touched).
Re-measured through the real proxy with the socket monitor (30 s from launch): **0** non-loopback connections from
Chrome's process tree with `audit-socks5` and with `audit-http`, while the pre-fix control on the same machine and proxy
opened 2-3 direct TCP 443 connections about 5 s after launch.

---

## 5. Expected differences (documented, not bugs)

| Difference | Why | Note |
|---|---|---|
| **Shared hardware fingerprint across profiles** | Native mode never spoofs. WebGL, WebGPU, canvas, audio, fonts, voices, screen and CPU/RAM are the machine's own. | fingerprint.com returned the *same visitor ID* for fresh profiles, B1 included (`visitor_found=true`). Cookies and storage are per profile, but a hardware-fingerprinting vendor can link profiles on one machine. This is the documented native limitation. Plan item 9, cross-profile cookie/storage isolation, was not re-measured in this audit. |
| **Timezone vs proxy country** | Native mode keeps the OS timezone, OS_TIMEZONE, while the exit geolocates to America/New_York. | The opt-in `launch.timezone` / `launch.lang` align it (PXT), apart from F7. |
| **TCP/IP OS fingerprint is the exit's** | The proxy terminates TCP. browserleaks reads "Android" (TTL 49–51, MSS 1400) while the UA says Windows. | Identical with stock Chrome and the same proxy. Only fixable by choosing a Windows-based exit. |
| **WebRTC gathers zero ICE candidates when proxied** | `--webrtc-ip-handling-policy=disable_non_proxied_udp` (`launch.webrtc=auto`) | No real or LAN IP leaks, where stock Chrome with the same proxy leaks `REAL_IP`. A page can see that gathering yields nothing, which looks like a WebRTC-protection setting and is unusual for desktop Chrome. Accepted trade-off. |
| **`history.length` +1** | The profile starts on about:blank and `browser_navigate` reuses that tab. | Like a person who opened a new tab and typed a URL. Gone for the first navigation since FIX-PLAN step 8 (the URL is opened by Chrome itself). |
| **`window="offscreen"` geometry** | `--window-position=-32000,-32000` gives screenX/Y = -32000 (Windows' minimized-window coordinate) and a window on no screen. The invisible 8 px borders vanish (innerWidth = outerWidth, innerHeight +8), which also changes `Sec-CH-Viewport-*`. | **Trivially detectable**, although none of the 18 sites graded it. Opt-in only; `normal` (the default) equals B0 on every geometry field. Document "offscreen = detectable; use normal for stealth". |
| **Window never reports `hidden`** | `--disable-backgrounding-occluded-windows` (all modes). A covered native Chrome reports `hidden`, with outer size 0 and rAF stopped. | Required: otherwise Playwright actions hang on covered windows. No infobar, and the single-flag ablation shows no other effect. A page cannot verify that it "should" be hidden. |
| **`http_fetch` engine=httpx is a plain HTTP client** | Documented. It borrows the browser's UA, but TLS is Python's (JA4 `t13d1812h1…`), over HTTP/1.1 with no client hints. | Risky. A Chrome UA on a non-Chrome TLS stack, sharing an IP with the real browser, is a classic bot signal. Consider making `auto` prefer the aligned curl_cffi engine (after F9), or not borrowing the Chrome UA (optional FIX-PLAN step 11). |
| **Network Information / storage quota** | Live estimates and free disk space | The same noise appears between plain-Chrome runs. |
| **First-run handling** | ProfilePilot uses `--no-first-run`; the B0 harness used a `First Run` sentinel. | Probe fields are identical between the two. |

Inconclusive (not attributable to ProfilePilot):

- **rAF rate.** 242 fps in the visible B0 reference vs 137–140 fps in every other configuration, including B1, B1N and P0 without CDP. Plain-Chrome ablation runs spanned 141–242 fps with no switch correlation. A controlled re-run on an idle single-monitor desktop is needed.
- **WebGPU report hash.**
  - browserleaks: B1 gave B56DFF93 in 3 of 4 runs; P and PD gave 3DC396DE.
  - browserscan: 059EBDF3 vs 6153FC44.
  - Every displayed value and an isolated-world adapter dump are identical.
  - The hash correlated with launch path (start-URL vs about:blank + navigation) and possibly with focus, but a later plain B1 also gave 3DC396DE ([detector-webgpu-isolation.json](audit/raw/detector-webgpu-isolation.json)).
- **sannysoft `PHANTOM_WINDOW_HEIGHT` cHeight.** 1579 (B1) vs 1561 (P), stable across two runs of each. It is likely load or layout timing of the about:blank → `Page.navigate` path. No verdict impact.

---

## 6. Leak checks

| Vector | No proxy (P) | SOCKS5 (PX) | HTTP CONNECT (PXH) | Stock Chrome + same proxy (B0DX) |
|---|---|---|---|---|
| IPv4 exit (6 sources + `fetch()`) | `REAL_IP` | `PROXY_EXIT_IP` = `proxy_test` | `PROXY_EXIT_IP` = `proxy_test` | `PROXY_EXIT_IP` |
| IPv6 | none (the machine has no IPv6) | none; api64 returns the IPv4 exit | none | n/a |
| DNS resolvers seen by sites | user ISP | Google LLC (proxy side) only | Google LLC (proxy side) only | Google LLC |
| Hostnames resolved locally while browsing | yes (no proxy) | **none** (relay `rdns`, net-log) | **none** | none |
| URL-policy lookups, local mode | n/a | none | none | n/a |
| URL-policy lookups, **remote mode** | n/a | **ISP resolver (F8)** | **ISP resolver (F8)** | n/a |
| WebRTC public / local IP | `REAL_IP` / mDNS only | none / none (0 candidates) | none / none | **`REAL_IP` leaked** / none |
| Real-identity labels in page outputs | expected | **0** | **0** | 3 |
| TLS JA4 / Akamai h2 / header order | = B0D | = B0D | = B0D | = B0D |
| QUIC | default | disabled | disabled | n/a |
| Direct (non-proxy) sockets from Chrome | all (no proxy) | **DoH probe to `SYSTEM_DOH_SERVER` (F11)**; mDNS 5353 multicast (LAN, also stock); one IPv6 reachability UDP connect that sends 0 datagrams | same | same DoH probe |
| Host process sockets | loopback | the proxy endpoint + loopback only | same | n/a |
| `http_fetch` exit | `REAL_IP` | `PROXY_EXIT_IP` (both engines) | `PROXY_EXIT_IP` (both engines) | n/a |
| Exit stability | n/a | sticky (5/5 over 1 min; same 30 min later) | sticky | n/a |

Verdict: **no real-IP or DNS leak is visible to websites through either proxy type.** The two non-site-visible egress paths
are the Secure DNS probe (F11, native) and remote-mode URL checks (F8). Both have small, targeted fixes.

---

## 7. Coverage gaps

Not covered by this audit, and worth a follow-up run:

- **Plan sites not run:**
  - amiunique.org and fingerprint-scan.com
  - browserleaks /fonts and /features
  - dnsleaktest.com (extended)
  - the fingerprint.com BotD page; the Pro demo was used instead
  - Cloudflare-protected pages (observe only)
- **Behaviour-based checks.** incolumitas' behavioral score and deviceandbrowserinfo's behavioural page need real mouse movement.
- **Proxies.** A second proxy in another country, a UDP-capable vs UDP-less upstream, and a machine with global IPv6.
- **Untested launch options.** `headless` mode, and a non-default `launch.lang`; PXT's `en-US` equals the OS language.
- ~~**Service workers** for the timezone override.~~ Answered in step 6: they report the configured zone.
- ~~**`hasFocus()` during CDP clicks and typing** (F5 severity).~~ Answered in step 8: a CDP click gives the page focus.
- ~~**Human-typed URL baseline.**~~ Moot since step 8: the activation came from the driver's evaluates, not from the navigation.

---

## 8. Reproduce

```
.venv/Scripts/python.exe docs/audit/scripts/run_probe_matrix.py --scratch <dir> [--configs B0,B1,P,...|--ablate]
.venv/Scripts/python.exe docs/audit/scripts/diff_probe.py --fields
.venv/Scripts/python.exe docs/audit/scripts/run_detectors.py --scratch <dir> --configs B1,P --extra-terms <redaction-terms.json>
.venv/Scripts/python.exe docs/audit/scripts/summarize_detectors.py --write
.venv/Scripts/python.exe docs/audit/scripts/run_network_audit.py ...   then   summarize_network.py
```

Every harness uses its own `PROFILEPILOT_HOME` under a scratch directory and `PROFILEPILOT_SECRETS=file`.
They only stop or kill processes whose command line contains that scratch root, and they refuse to write any file that still
contains the real IP, the exit IP or the proxy credentials.
