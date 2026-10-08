# ProfilePilot

**Many isolated browser profiles for your AI agent, running in your real, native Chrome.**

ProfilePilot is an MCP plugin for **Claude** (Desktop and Code) and **ChatGPT** (plus Codex and Cursor).
It lets the AI browse and scrape through any number of **profiles**. Each profile is a separate
Chrome identity with its own cookies, logins, history, local storage, cache, and **its own
proxy** (HTTP, HTTPS, SOCKS4 or SOCKS5, with or without username and password).

The idea comes from anti-detect browsers like ShardX/ShardBrowser and Linken Sphere, with one
big difference: **ProfilePilot does no fingerprint spoofing.** No fake user agents, no canvas
noise and no patched engine. Each profile is your machine's genuine Chrome, exactly like a person
using it (and like Claude's built-in browser). On Chrome 154 we checked that `navigator.webdriver`
is `false`, the user agent is the real one, and no automation banners or flags appear.

```
ChatGPT ─┐                                   ┌─ profile "shop-us"  → Chrome window → SOCKS5 proxy (US)
Claude  ─┼─ MCP ─► ProfilePilot ─ CDP ───────┼─ profile "shop-de"  → Chrome window → HTTP proxy (DE)
Codex   ─┘                                   └─ profile "research" → Chrome window → direct
```

## Features

- **Isolated profiles.** Each profile gets its own Chrome user-data directory, so nothing is shared between them: cookies, sessions, history, storage, cache and extensions.
- **Per-profile proxies, including authenticated SOCKS5.** Stock Chrome can't log in to SOCKS5 proxies, so every proxied profile gets a small local relay that handles the login for it.
  - DNS is resolved at the proxy, not on your machine.
  - WebRTC is restricted to the proxy, so your real IP can't leak.
  - If the relay stops, traffic stops; it never quietly falls back to a direct connection.
- **Live proxy switching.** Give a running profile a new proxy or rotate it without restarting Chrome.
- **Built for AI agents.**
  - Accessibility snapshots mark elements with refs such as `[ref=e12]`; click and type by ref.
  - Page reading outputs markdown, text or HTML, with hidden prompt-injection text removed.
  - Scrapling-powered CSS/XPath extraction.
  - Screenshots, tabs, cookies, and HTTP requests made with a profile's cookies and proxy.
- **Shared by all your AI clients.** Profiles keep running in the background, so Claude, ChatGPT, the CLI and your Python scripts can all attach to the same live profile.
- **Scrapling integration.** Point Scrapling's fetchers and spiders at a profile and they reuse its cookies, logins and proxy (see [docs/SCRAPLING.md](docs/SCRAPLING.md)).
- **Optional ShardX backend.** Drive profiles from ShardX/ShardBrowser through its local API with the same tools. These profiles are labelled as a spoofed engine.
- **Safe defaults.**
  - Proxy passwords are stored in the OS keyring (Windows Credential Manager) and never shown to the AI.
  - Remote (ChatGPT) mode blocks `file://` and private-network URLs.

## Install

Requirements: Python 3.10 or newer, and Google Chrome (or Edge, Brave or Chromium). Node.js is not
needed, and no extra browser download is needed.

```bash
git clone https://github.com/matyaskovecses/ProfilePilot && cd ProfilePilot
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[scrapling]"
```

Then register it with your AI clients. The command makes a backup of each config file before
changing it:

```bash
.venv\Scripts\profilepilot install claude-desktop
```

```bash
.venv\Scripts\profilepilot install claude-code
```

```bash
.venv\Scripts\profilepilot install codex
```

`profilepilot install print` shows the config snippets without changing anything. For ChatGPT,
which needs a remote connection, see [docs/CLIENTS.md](docs/CLIENTS.md). It covers OpenAI's Secure
MCP Tunnel and the `serve --http` mode. That file also has setup steps for each client.

## Quick start (talking to your AI)

> Create two profiles: "us-1" using proxy `socks5://user:pass@1.2.3.4:1080` and "de-1" using `http://5.6.7.8:3128`.
> In us-1, open example-shop.com, search for "usb-c hub" and extract the first 20 product names and prices.
> Then do the same in de-1 and compare the prices.

What the AI does:
1. `profile_create` creates the profiles.
2. `browser_navigate` opens the site.
3. `browser_snapshot` takes an accessibility snapshot.
4. `browser_type` types the search, and `browser_click` clicks results by ref.
5. `browser_extract` (CSS selectors) or `browser_read` (markdown) pulls out the data.

Each profile keeps its own cookies, so logins and carts stay separate.

## Command line

```bash
profilepilot profile create shop-us --proxy "socks5://user:pass@1.2.3.4:1080" --tag shop
```

```bash
profilepilot profile start shop-us
```

```bash
profilepilot status
```

```bash
profilepilot proxy import proxies.txt --scheme socks5
```

```bash
profilepilot proxy test shop-us
```

```bash
profilepilot profile stop shop-us
```

```bash
profilepilot doctor
```

`proxy import` takes one proxy per line, in any of these formats: `scheme://user:pass@host:port`,
`host:port:user:pass`, `user:pass@host:port` or `host:port`. Add `  # name` at the end of a line to
name that proxy.

## Python and Scrapling

```python
from profilepilot import ProfilePilot

pp = ProfilePilot()
info = pp.start("shop-us")          # launches (or reuses) the profile's real Chrome
print(info.cdp_http_url)            # CDP endpoint; attach Playwright, Puppeteer or Scrapling
print(info.http_proxy_url)          # the profile's proxy as a credential-free local URL

from profilepilot.integrations.scrapling import AsyncProfileSession
async with AsyncProfileSession("shop-us") as session:   # reuses the profile's cookies and logins
    page = await session.fetch("https://example.com/account")
    print(page.css("h1::text").get())
```

More recipes, including `FetcherSession` with profile cookies and spiders, are in
[docs/SCRAPLING.md](docs/SCRAPLING.md).

## MCP tools

| Group | Tools |
|---|---|
| Profiles | `profile_list`, `profile_create`, `profile_update`, `profile_clone`, `profile_delete`, `profile_start`, `profile_stop`, `profile_status`, `profile_set_proxy` |
| Proxies | `proxy_list`, `proxy_add` (accepts a whole list at once), `proxy_remove`, `proxy_test` |
| Browser | `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_press_key`, `browser_select_option`, `browser_hover`, `browser_scroll`, `browser_wait_for`, `browser_screenshot`, `browser_read`, `browser_extract`, `browser_evaluate`, `browser_tabs` |
| Data | `cookies_get`, `cookies_set`, `cookies_clear`, `cookies_export`, `cookies_import`, `http_fetch` |
| ShardX (optional) | `shardx_status`, `shardx_profiles`, `shardx_start`, `shardx_stop`. Every browser tool also accepts `profile="shardx:<name>"`. |

## How it works

Each running profile is a small **host process** that owns two things:
- a real `chrome.exe`, started with only the flags a normal user could pass (its own `--user-data-dir` and a fixed DevTools port);
- the local proxy relay for that profile.

The MCP server attaches to that Chrome over the Chrome DevTools Protocol (CDP), the same mechanism
Claude's built-in browser uses. Because the hosts are separate processes, a profile keeps running
when an AI client restarts, and several clients can use it at the same time. If a host process
dies, Windows closes its Chrome too (a job object), so there are no orphaned browsers on a dead
proxy. The full design and the facts it was checked against are in [docs/DESIGN.md](docs/DESIGN.md).

### Native by design: what ProfilePilot does and doesn't change

| ProfilePilot does | ProfilePilot never does |
|---|---|
| Separate user-data dir per profile | Spoof the user agent, client hints, screen, GPU, fonts or canvas |
| Proxy via a local relay, with DNS resolved at the proxy | Use `--enable-automation`, `--headless` (unless you ask for it), `--no-sandbox` or `--disable-blink-features` |
| WebRTC limited to the proxy when one is set | Inject stealth scripts |
| Language/timezone override, only if you turn it on | Solve CAPTCHAs |

**Know the limit:** every profile shares your machine's real hardware fingerprint (GPU, fonts,
screen). Sites see different cookies and different IPs, but an advanced fingerprinting system
could still tell that the profiles come from the same computer. That's the price of being native.
If you really need unlinkable identities, use the ShardX backend for those profiles.

## Using it responsibly

ProfilePilot is a tool for legitimate automation and scraping. Respect websites' terms of service
and robots.txt, rate-limit your requests, and only log in to accounts you're allowed to use.

## Development

```bash
.venv\Scripts\python -m pytest
```

```bash
.venv\Scripts\python -m pytest -m chrome
```

The first command runs the fast unit and integration tests. The second runs the real-Chrome
end-to-end tests: windows open off-screen, and only processes the tests started are closed.

License: MIT.
