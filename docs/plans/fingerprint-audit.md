# Plan: fingerprint & leak audit (runs after the build is green)

**Goal:** a ProfilePilot profile is indistinguishable from the same machine's normal Chrome, both with
and without a proxy. Any difference we find is either fixed or documented as expected
(for example, timezone versus proxy country).

**Status:** planned. It starts once the build workflow and its tests pass.

## Method: compare against a baseline, not a score

For every site we capture the same pages in three configurations, each in a fresh profile:

| Config | How it's launched |
|---|---|
| **B: baseline** | Plain `chrome.exe --user-data-dir=<fresh>`. No debug port and no CDP. This is "a normal browser". |
| **P: ProfilePilot** | A ProfilePilot profile, driven over MCP the way an AI uses it (Playwright attached). |
| **PX: ProfilePilot + proxy** | P plus a real authenticated SOCKS5 proxy, then repeated with an HTTP proxy. |

B can't be driven over CDP without changing it. So B is captured by loading the same pages with a
small local "collector" page that `fetch()`es the results to a local server. Where a site has no
export, we use manual screenshots of B. For P and PX we collect results with `browser_read`,
`browser_extract` and screenshots.

Every finding is a **diff from B**. A red flag that B also shows (for example, "your GPU is common")
is not our bug.

## Sites (grouped by what they test)

| Area | Sites |
|---|---|
| Automation / bot flags | bot.sannysoft.com · deviceandbrowserinfo.com/are_you_a_bot · bot.incolumitas.com · bot-detector.rebrowser.net · fingerprint.com/products/bot-detection (BotD demo) |
| Full fingerprint consistency | abrahamjuliot.github.io/creepjs · pixelscan.net · browserscan.net · iphey.com · whoer.net · amiunique.org · fingerprint-scan.com |
| Individual vectors | browserleaks.com: /javascript /canvas /webgl /webgpu /fonts /client-hints /tls /features |
| Network leaks (PX only) | browserleaks.com/ip /webrtc /dns · ipleak.net · dnsleaktest.com (extended) · test-ipv6.com |
| Real-world score | antcpt.com/score_detector (reCAPTCHA v3 score) · Cloudflare-protected test pages (observe only, never solve) |

## Checks and expected results

1. `navigator.webdriver === false`. The UA, `userAgentData` brands and client hints must match the installed Chrome 154 exactly, and equal B.
2. **CDP detection.**
   - What to test: attaching Playwright sends `Runtime.enable`. That is detectable, for example by the "Error.stack getter fires on console.debug" trick that rebrowser, browserscan and deviceandbrowserinfo use.
   - **Expect this to be our main finding.** Mitigations to evaluate, in this order:
     - (a) Keep CDP detached while the AI isn't acting.
     - (b) Avoid `Runtime.enable`: use patchright (already installed via Scrapling) as the CDP driver, or keep Playwright and apply the rebrowser-patches approach.
     - (c) Run evaluation in isolated worlds only.
   - Pick the option that makes P equal B on all four bot-detector sites.
3. **Window and visibility.** `outerWidth/innerWidth`, `screen.*` and `devicePixelRatio` equal B in `normal` mode. Document what `offscreen` and `headless` change.
4. **WebRTC (PX).** No host or srflx candidate reveals the real IP. Test with the UDP-capable proxy and the UDP-less proxy. Confirm `--webrtc-ip-handling-policy=disable_non_proxied_udp` behaves as designed.
5. **DNS (PX).** Every resolver seen belongs to the proxy side, never the local ISP. With the SOCKS5 relay, hostnames must reach the proxy unresolved.
6. **IP / geo (PX).** The exit IP equals what `proxy_test` reports.
   - Timezone and language **will** mismatch the proxy country by design: this is native mode.
   - Record the mismatch, and verify that the opt-in `lang`/`timezone` profile options make it consistent when they're enabled.
7. **TLS / HTTP2.** The JA3/JA4 and Akamai h2 fingerprints on browserleaks.com/tls equal B. The relay is a TCP pipe, so this proves it doesn't alter TLS.
8. **No visible automation surfaces.** No "controlled by automated software" bar and no "unsupported command-line flag" infobar. Check with screenshots of the real window, not just the page.
9. **Cross-profile isolation.** Visit the same tracker sites in two profiles. Cookie, localStorage and IndexedDB identifiers must differ; the hardware fingerprint will match. That shared hardware fingerprint is the documented native limitation.

## Deliverables

- `docs/FINGERPRINT-AUDIT.md`: a per-site table of B, P and PX results with screenshots in `docs/audit/`. Every difference is marked fixed or expected.
- Code fixes for anything that isn't expected, plus regression tests: `tests/test_native_fingerprint.py`, marked `chrome`, which checks webdriver, the UA/client-hints match and the CDP-detection probe against a local page.
- An updated "Native by design" section in the README.

## Inputs needed from the user

- One authenticated SOCKS5 proxy and one authenticated HTTP proxy, ideally in different countries and residential. Alternatively, permission to import the two SOCKS5 proxies already saved in ShardX.
- Nothing else. All sites are visited read-only, and nothing is solved, submitted or logged into.
