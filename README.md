<div align="center">

# ProfilePilot

**Many isolated browser profiles for your AI agent, running in your real, native Chrome.**

[![CI](https://github.com/matyaskovecses/ProfilePilot/actions/workflows/ci.yml/badge.svg)](https://github.com/matyaskovecses/ProfilePilot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)
![MCP](https://img.shields.io/badge/MCP-Claude%20%C2%B7%20ChatGPT%20%C2%B7%20Codex%20%C2%B7%20Cursor-6E56CF)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/manager/profiles-dark.png">
  <img alt="ProfilePilot Manager: profile cards with live thumbnails, an AI help request and take-control buttons" src="docs/img/manager/profiles-light.png" width="860">
</picture>

</div>

ProfilePilot gives **Claude** (Desktop and Code), **ChatGPT**, Codex and Cursor a fleet of browser
**profiles**. Each profile is a separate identity of the real Chrome (or Edge or Brave) on your
computer, with its own:
- cookies, logins, history, storage and cache;
- **proxy**: HTTP, HTTPS, SOCKS4 or SOCKS5, with or without username and password.

The AI browses, scrapes and fills forms through the profiles. **ProfilePilot Manager** lets you see
everything and take over whenever a human is needed.

The idea comes from anti-detect browsers such as [ShardX/ShardBrowser](https://github.com/ProxyShard/ShardBrowser)
and Linken Sphere, with one deliberate difference: **ProfilePilot spoofs nothing.** There are no fake user
agents, no canvas noise and no patched engine. Every profile is your genuine browser, just as a
person uses it. A [fingerprint audit](docs/FINGERPRINT-AUDIT.md) against 18 public bot and fingerprint
detectors checks exactly that.

```
ChatGPT ─┐                                     ┌─ profile "shop-us"   → Chrome window → SOCKS5 proxy (US)
Claude  ─┼─ MCP ─► ProfilePilot ─ DevTools ────┼─ profile "shop-de"   → Edge window   → HTTP proxy (DE)
Codex   ─┘              ▲                      └─ profile "research"  → Chrome window → direct
                        └── ProfilePilot Manager (you: take control, solve CAPTCHAs, manage proxies)
```

## Highlights

- **Isolated native profiles.** Every profile has its own browser data directory, and nothing is shared between profiles. Each one can run in any installed Chromium-family browser: Chrome, Edge, Brave, Chromium, and the Beta, Dev and Canary channels. Each sends exactly that browser's genuine identity.
- **Real proxies, including authenticated SOCKS5.** Stock Chrome can't log in to SOCKS5 proxies, so a local relay does it for the browser.
  - **No leaks:** DNS is resolved at the proxy, WebRTC can't leak your IP, and if the relay stops, traffic stops; it never silently goes direct. You can also switch proxies live.
  - **Credentials** stay in the OS keyring and never reach the AI.
- **Built for AI agents.**
  - **Reading pages:** accessibility snapshots with element refs (`[ref=e12]`), and page reading in markdown, text or HTML with hidden prompt-injection text stripped.
  - **Doing things:** CSS/XPath extraction (via [Scrapling](https://github.com/D4Vinci/Scrapling)), screenshots, tabs and cookies, plus HTTP requests that carry the profile's own cookies and browser identity.
- **Human-like input and autofill.**
  - **Typing:** humanized typing, and type-paste (a real Ctrl/⌘+Shift+V paste).
  - **Autofill:** fills sign-up, address and checkout forms from details *you* entered. Card fields in Stripe-style iframes are included.
  - **Sensitive values:** card numbers, CVVs, SSNs and passwords stay in the OS keyring, are filled only on sites you allow, and only after you approve.
- **You stay in charge.** **ProfilePilot Manager** is a desktop window for your profiles, proxies and identities, with live thumbnails of what the AI is doing.
  - Click **Take control** to pause the AI on a profile.
  - When the AI hits a CAPTCHA, a login or a 2FA code, it asks you for help instead of guessing.
- **Works with every major AI client.**
  - **Claude Desktop and Claude Code:** one-command setup.
  - **Codex and the ChatGPT desktop app:** a `config.toml` entry.
  - **ChatGPT on the web:** a guided `connect chatgpt` that uses an OAuth sign-in you approve with a pairing code.
  - **In ChatGPT and Claude:** an interactive profiles panel right in the chat.
- **Shared and persistent.** Profiles keep running in the background. Claude, ChatGPT, the CLI and your Python and Scrapling scripts can all use the same live profile.

## Quick start

You need **Python 3.10+** and **Google Chrome** (or Edge, Brave or Chromium). No Node.js and no extra browser downloads.

```bash
git clone https://github.com/matyaskovecses/ProfilePilot && cd ProfilePilot
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[scrapling]"
```

On macOS and Linux use `.venv/bin/...` instead of `.venv/Scripts/...`.

Open the Manager. Its first-run guide connects your AI app in one click:

```bash
.venv/Scripts/profilepilot ui
```

Or register from the terminal. Each config file is backed up first:

```bash
.venv/Scripts/profilepilot install claude-desktop
```

```bash
.venv/Scripts/profilepilot install claude-code
```

```bash
.venv/Scripts/profilepilot install codex
```

Restart the AI app and ask it something like:

> Create a profile "us-1" with proxy `socks5://user:pass@1.2.3.4:1080`, open example-shop.com in it, search for
> "usb-c hub" and give me the first 20 product names and prices.

The AI does this:
1. Creates the profile.
2. Calls `browser_navigate`, then `browser_snapshot`.
3. Clicks and types by ref.
4. Calls `browser_extract`.

If a CAPTCHA appears, it calls `profile_request_help`. The Manager shows "Claude needs you in us-1"
with a **Focus window** button. You solve it, click **Done**, and the AI carries on.

## ProfilePilot Manager: manage everything by hand

Not everything can be automated. `profilepilot ui` opens **ProfilePilot Manager** in its own app window.
`profilepilot ui --install-shortcut` adds it to the Desktop and the Start menu.

- **Profiles:**
  - live thumbnails of every running browser, with an "AI working" badge while the AI acts;
  - start/stop, and **Focus**, which brings the window to the front;
  - open a page in a profile;
  - **Take control / Hand back to AI**;
  - bulk start, stop, tag and proxy assignment.
- **Cookies:** every cookie of a profile, grouped by site. Search, reveal and copy values, and edit every attribute
  (domain or host-only, path, expiry, Secure, HttpOnly, SameSite, partitioned). Import Cookie-Editor / EditThisCookie
  JSON or `cookies.txt` (merge, replace those sites, or replace everything, with a preview first) and export all,
  a filter or a selection. A stopped profile can be started in the background for it.
- **Help requests:** when the AI needs a human (CAPTCHA, login, 2FA, payment confirmation), a banner and a desktop notification appear. You act in the profile's window and click **Done**.
- **Proxies:** paste hundreds at once (`host:port:user:pass`, `socks5://…`). Test the exit IP, country and latency, with a history sparkline, and see which profiles use each proxy. Passwords go into the OS keyring and are never shown again.
- **Identities:** your details for autofill. Card numbers, CVVs, SSNs and passwords are write-only, and you choose which sites may receive them.
- **Activity:** a live feed of every tool call the AI makes and of everything you did.
- **Connections:** one-click setup for Claude Desktop, Claude Code, Codex and Cursor, and the ChatGPT connection status with its pairing code.

| | |
|---|---|
| ![Proxies](docs/img/manager/proxies-light.png) | ![Profile details with a help request](docs/img/manager/drawer-profile-light.png) |
| ![Identities](docs/img/manager/identities-light.png) | ![Activity](docs/img/manager/activity-dark.png) |
| ![Add proxies](docs/img/manager/dialog-import-proxies-light.png) | ![Connections](docs/img/manager/connections-dark.png) |
| ![Cookies](docs/img/manager/drawer-cookies-light.png) | ![Edit a cookie](docs/img/manager/dialog-cookie-edit-light.png) |

The Manager listens on 127.0.0.1 only, requires a one-time code from its launcher, and never shows stored
secrets. The same controls are on the command line: `profilepilot profile pause|resume`, `profilepilot help list|resolve`.
A paused profile is off-limits everywhere, not just to the AI's tools: the Python client, Scrapling sessions
and the CLI's `start`, `stop`, `delete`, proxy changes and `stop-all` leave it alone until you hand it back
(your own scripts can pass `ignore_pause=True`, the CLI `--ignore-pause`).

## Use with ChatGPT

ChatGPT on the web can only use MCP servers on the internet. ProfilePilot opens a secure tunnel to
your PC, protected by a sign-in that only you can approve:

```bash
profilepilot connect chatgpt
```

It prints a URL and a **pairing code**. In ChatGPT:
1. Open chatgpt.com/plugins (Settings → Apps & Connectors).
2. Choose **+ → Add custom MCP server**.
3. Paste the URL and pick **OAuth**.
4. Enter the pairing code on the ProfilePilot sign-in page.

Keep the window open while you use ChatGPT. Ctrl+C, or `profilepilot connect stop`, ends sharing. The
Manager's **Connections** view shows the URL and code while the connection runs.

There's also an option with no public URL, OpenAI's Secure MCP Tunnel. That option, plan availability
and troubleshooting are covered in [docs/CHATGPT.md](docs/CHATGPT.md). Setup for every other client is in
[docs/CLIENTS.md](docs/CLIENTS.md).

In ChatGPT and Claude, *"show my profiles"* opens an interactive panel with start/stop and **Take control**,
right in the chat.

## Typing, paste & autofill

`browser_type` takes a `method`:

| Method | What the page sees | Use it for |
|---|---|---|
| `fill` (default) | the value appears at once, with an `input` event but no key events | most fields |
| `type` | one key event per character, at a fixed pace | fields that react to each key (search suggestions, masks) |
| `human` | key by key with human timing: varied intervals, pauses after spaces and punctuation, Shift held for capitals | sites that look at how people type |
| `paste` | a real paste from the system clipboard with Ctrl+Shift+V (⌘⇧V on macOS): a trusted `paste` event and `insertFromPaste` input | long text, and fields that expect pasting |

Pasting with `browser_paste` or `method="paste"`:
- holds a lock, so two profiles never paste at the same time;
- keeps the text out of Windows clipboard history and cloud sync;
- puts your own clipboard back afterwards.

**Identities** are named sets of your details: name, email, phone, address, date of birth, company.
Nothing is ever generated. Card number, expiry, CVV, SSN and password are *sensitive*:
- you enter them yourself, in a terminal or the Manager;
- they're stored only in the OS keyring;
- the AI only ever sees them masked (`visa •••• 4242`), and they're redacted from everything it reads back.

```bash
profilepilot identity create "Jane" --set first_name=Jane --set last_name=Doe --set email=jane@example.com --set zip=94105
```

```bash
profilepilot identity secret "Jane" card_number
```

```bash
profilepilot identity allow "Jane" https://shop.example.com
```

**Autofill tools:**
- `form_detect` lists a page's fields, including those in cross-origin iframes such as Stripe's card fields.
- `form_autofill` fills text fields, selects, dates, radios, and split phone, SSN and card fields.
- `form_autofill_sensitive` fills card, SSN and password fields. It needs your approval for every call and only works on allow-listed sites. Card fields can also go into known payment processors' iframes.
- `autofill_sources` lists where details can come from, including the addresses you already saved in Chrome, Edge or Brave.

**Use what your browser already knows.** `form_autofill` can fill from the addresses saved in your own
browser ("Addresses and more"): `identity="chrome"` takes the active Chrome profile, and
`chrome:edge` or `chrome:chrome/Profile 1` pick another one. A profile with no linked identity uses
them by itself (turn that off in Settings or with `autofill_from_browser` in the config). Only
names, email, phone, company and address are read, live and read-only; saved cards, passwords and
form history are never opened, and the AI sees each address only as a name and a city.

```bash
profilepilot identity sources
```

```bash
profilepilot identity connect-chrome "Jane" --source chrome --address 1
```

An identity linked this way takes its details from the browser at fill time; values you set on the
identity itself win.

Hidden or covered fields are never filled, nothing is submitted for you, and tool output never
contains the values.

## Command line

| Command | What it does |
|---|---|
| `profilepilot ui` | ProfilePilot Manager |
| `profilepilot profile create shop-us --proxy "socks5://u:p@1.2.3.4:1080" --browser edge` | new profile (any installed browser: `profilepilot browsers`) |
| `profilepilot profile start shop-us` / `stop` / `pause` / `resume` | run it, or take control of it |
| `profilepilot proxy import proxies.txt --scheme socks5` | bulk import: `scheme://user:pass@host:port`, `host:port:user:pass`, `user:pass@host:port`, `host:port` (append `  # name`) |
| `profilepilot proxy test shop-us` | exit IP, country and latency of a proxy or a profile's route |
| `profilepilot cookies list shop-us` / `export` / `import` / `set` / `delete` | the profile's cookies; a stopped profile is started off-screen for the command and stopped again |
| `profilepilot status` | running profiles |
| `profilepilot help list` | open help requests from the AI |
| `profilepilot connect chatgpt` | share ProfilePilot with ChatGPT |
| `profilepilot doctor` | check the installation |

Add `--json` to any command for machine-readable output. Secrets are never printed. Proxy specs and
tokens can be read from stdin (`-`), so they never appear in the process list.

## Python and Scrapling

```python
from profilepilot import ProfilePilot

pp = ProfilePilot()
info = pp.start("shop-us")          # launches (or reuses) the profile's real browser
print(info.cdp_http_url)            # DevTools endpoint: attach Playwright, Puppeteer or Scrapling
print(info.http_proxy_url)          # the profile's proxy as a credential-free local URL

from profilepilot.integrations.scrapling import AsyncProfileSession, fetcher_session
async with AsyncProfileSession("shop-us") as session:   # Scrapling inside the profile: its cookies, logins, proxy
    page = await session.fetch("https://example.com/account")
    print(page.css("h1::text").get())

with fetcher_session("shop-us") as s:                   # fast HTTP with the profile's identity and cookies
    print(s.get("https://example.com/api/items").json())
```

More recipes (spiders, cookie write-back) are in [docs/SCRAPLING.md](docs/SCRAPLING.md).

## MCP tools

| Group | Tools |
|---|---|
| Profiles | `profile_list`, `profile_create`, `profile_update`, `profile_clone`, `profile_delete`, `profile_start`, `profile_stop`, `profile_status`, `profile_set_proxy`, `browser_list` |
| Human handoff | `profile_request_help` (asks you in the Manager and pauses the profile), `profiles_dashboard` (interactive panel in ChatGPT/Claude) |
| Proxies | `proxy_list`, `proxy_add` (many at once), `proxy_remove`, `proxy_test` |
| Browser | `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_paste`, `browser_press_key`, `browser_select_option`, `browser_hover`, `browser_scroll`, `browser_wait_for`, `browser_screenshot`, `browser_read`, `browser_extract`, `browser_evaluate`, `browser_tabs` |
| Identities & forms | `identity_list`, `identity_show`, `identity_create`, `identity_update`, `form_detect`, `form_autofill`, `form_autofill_sensitive`, `autofill_sources` |
| Data | `cookies_get`, `cookies_set`, `cookies_clear`, `cookies_export`, `cookies_import`, `http_fetch` |
| ShardX (optional) | `shardx_status`, `shardx_profiles`, `shardx_start`, `shardx_stop`; any browser tool also takes `profile="shardx:<name>"` |

## How it works

Each running profile is a small **host process** that owns:
- the real browser, started with the flags a normal user could pass: its own `--user-data-dir` and a fixed DevTools port;
- the profile's local proxy relay.

Tools attach to it over the Chrome DevTools Protocol using [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python).
That driver never enables the `Runtime` domain, and it evaluates in an isolated world. ProfilePilot
also patches out two automation artifacts patchright adds: forced focus emulation and synthetic user
activation.

A profile keeps running when an AI client restarts, and several clients can share it. If a host dies,
its browser goes with it, so no browser is left running on a dead proxy. The design and the facts it
was checked against are in [docs/DESIGN.md](docs/DESIGN.md).

### Native by design: what the fingerprint audit found

[docs/FINGERPRINT-AUDIT.md](docs/FINGERPRINT-AUDIT.md) compares three setups: plain Chrome, a ProfilePilot
profile, and a ProfilePilot profile behind a real residential proxy. It uses a local probe of about 40
signals and 18 public detectors.

| Check | Result |
|---|---|
| Static fingerprint (UA, client hints, screen, WebGL/WebGPU, canvas, audio, fonts, globals) | identical to plain Chrome |
| `navigator.webdriver`, automation infobars, CDP side channels (`prepareStackTrace`, console timing, workers) | identical to plain Chrome |
| deviceandbrowserinfo, fingerprint.com, pixelscan bot check, rebrowser bot detector | same verdict as plain Chrome; these flagged earlier Playwright-based builds |
| sannysoft, CreepJS, BrowserScan, incolumitas, whoer, browserleaks, reCAPTCHA v3 score | same as plain Chrome |
| Through SOCKS5 and HTTP proxies: IP, DNS, WebRTC, IPv6 | no leak; plain Chrome on the same proxy leaks your IP over WebRTC |
| TLS (JA4) and HTTP/2 fingerprint | byte-identical to plain Chrome |

**Known limits.**
- Every profile shares your machine's hardware fingerprint (GPU, fonts, screen), so a fingerprinting vendor can tell that profiles come from the same computer.
- The timezone follows your OS. Aligning it with the proxy is opt-in.
- The back/forward cache is off.
- `offscreen` windows are detectable; `normal` windows aren't.

That's the price of being native. If you need unlinkable identities, use the ShardX backend for those profiles.

**Browsers.** Chrome, Edge, Brave, Chromium and the Chrome/Edge Beta, Dev and Canary channels.
- Opera and Vivaldi aren't supported.
- Safari/WebKit is planned as a macOS `safaridriver` backend. On Windows there's no native Safari, and a WebKit build would not be native.

## Security & privacy

- **Prompt injection:** ProfilePilot assumes the AI can be prompt-injected by the pages it reads.
- **Secrets:** proxy passwords, tokens and identity secrets live in the OS keyring and are never shown to the AI.
- **Remote mode:** blocks local and private-network targets.
- **Sensitive autofill:** needs your approval and an allow-listed site.

The full model and private vulnerability reporting are in [SECURITY.md](SECURITY.md).

## Using it responsibly

ProfilePilot is for legitimate automation and scraping:
- Respect websites' terms and robots.txt.
- Rate-limit your requests.
- Only use accounts and details you're entitled to.

It does not solve CAPTCHAs or bypass access controls; those go to you.

## Development

```bash
.venv/Scripts/python -m pip install -e ".[test,scrapling]"
```

```bash
.venv/Scripts/python -m pytest -m "not chrome"
```

```bash
.venv/Scripts/python -m pytest
```

The second command runs the fast tests. The third runs everything, including the real-browser tests,
whose windows open off-screen. See [CONTRIBUTING.md](CONTRIBUTING.md).

Releases: push a tag like `v0.1.0` (matching `pyproject.toml` and a `CHANGELOG.md` section). The release
workflow runs the tests, builds the wheel, the sdist and the Claude Desktop extension (`profilepilot.mcpb`)
and creates a draft GitHub release with them and their checksums.

## Credits

ProfilePilot stands on the shoulders of two open-source projects:

- **[ShardBrowser / ShardX](https://github.com/ProxyShard/ShardBrowser)** by the **ProxyShard** team (MIT). Its profile and proxy management, launch handling and local API design were the blueprint, and ProfilePilot can drive ShardX profiles directly.
- **[Scrapling](https://github.com/D4Vinci/Scrapling)** by **Karim Shoair** ([@D4Vinci](https://github.com/D4Vinci)) (BSD-3-Clause). It powers extraction, Scrapling sessions inside profiles, curl_cffi fetching, and the patterns of ProfilePilot's MCP server.

Also built with Playwright, patchright, the MCP Python SDK, python-socks, curl_cffi and more; see [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).

## License

[MIT](LICENSE) © 2026 Matyas Kovecses
