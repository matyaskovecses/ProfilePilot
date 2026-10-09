# Changelog

## 0.1.0 — first public release

### Profiles and browsers
- **Isolated native profiles.** Every profile is a separate user-data directory for your **real, installed** Chromium-family browser: Chrome, Edge, Brave, Chromium, and the Chrome/Edge Beta, Dev and Canary channels. Nothing is spoofed: no fake user agents and no injected stealth scripts.
- **Per-profile host process** that owns the browser and its proxy relay. Browsers survive AI-client restarts, several clients can share one profile, and if a host dies its browser closes with it, so there are no orphans on a dead proxy.
- Session restore keeps tabs and session cookies across restarts. Profiles go to a trash bin before deletion and can be restored, cloned (with or without browser data), tagged and annotated.

### Proxies
- **HTTP, HTTPS, SOCKS4 and SOCKS5, with or without username and password.** Stock Chrome can't log in to SOCKS5 proxies, so a local relay does it for the browser.
  - **No leaks:** DNS is resolved at the proxy, WebRTC is limited to the proxy, and if the relay stops, traffic stops.
  - **Credentials** are stored in the OS keyring and never shown to the AI or put on a command line.
- **Proxy tools:** live proxy switching without restarting the browser; bulk import in common provider formats; exit-IP, geo and latency checks.

### AI tools (MCP)
- **Snapshot and act by ref:** accessibility snapshots with `[ref=…]` element references; click, type, select, hover, scroll and wait by ref or selector.
- **Reading pages:** markdown, text or HTML, with hidden prompt-injection text removed; CSS/XPath extraction via Scrapling; screenshots; tabs.
- **Cookies and HTTP:** cookie get, set, clear, import and export; `http_fetch` sends requests with the profile's own proxy, cookies and browser identity.
- **Typing:** humanized typing, and **type-paste** (Ctrl/⌘+Shift+V, a real trusted paste event).
- **Identity autofill:** fills forms from identities you enter yourself, across selects, split fields, dates and card iframes.
  - **Sensitive fields** (SSN, card, password) can only be set by you in a terminal.
  - They're filled only on sites you allow-list, after your approval.
  - They're redacted from everything the AI reads back.
- **ShardX backend (optional):** drive ShardX/ShardBrowser profiles with the same tools.

### Clients
- **Claude Desktop:** an `.mcpb` bundle or one-command config.
- **Claude Code:** a plugin and marketplace, or `claude mcp add`.
- **Codex and the ChatGPT desktop app:** `config.toml`.
- **Cursor.**
- **ChatGPT:** a secured HTTP mode (OAuth with a pairing code, or a secret path) plus a connect wizard for OpenAI Secure MCP Tunnel, cloudflared or ngrok.

### Fingerprint audit
- An audit against 18 public detector sites and a local probe harness compared plain Chrome, ProfilePilot, and ProfilePilot behind a real residential proxy. See [docs/FINGERPRINT-AUDIT.md](docs/FINGERPRINT-AUDIT.md).
- The CDP driver uses **patchright** with two in-memory driver patches. As a result:
  - `Runtime.enable` is never sent, and evaluations run in an isolated world;
  - no focus emulation and no synthetic user activation.

### Credits
Built on ideas from [ShardBrowser](https://github.com/ProxyShard/ShardBrowser) by the ProxyShard team, and on
[Scrapling](https://github.com/D4Vinci/Scrapling) by Karim Shoair. See [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).
