# Cookie manager — implementation spec

The user can see, edit, import and export every cookie of a profile, like the cookie editors in
ShardX / Linken Sphere or the Cookie-Editor extension, from three places: the **Manager** (a Cookies
tab in the profile drawer), the **CLI** (`profilepilot cookies ...`) and the existing MCP tools
(`cookies_get` / `cookies_set` / `cookies_clear` / `cookies_export` / `cookies_import`, unchanged;
the AI still never sees cookie values).

## 1. Ground rules

- **The live browser is the source of truth.** Chrome encrypts cookies on disk (DPAPI / Keychain /
  libsecret, plus app-bound encryption on recent Chrome), so cookies are only read and written
  through the profile's running browser over CDP. A stopped profile is started **in the
  background** (window `offscreen`, or the profile's own mode if it is `headless`) on an explicit
  user action ("Start in background"); the CLI does that by itself and stops the browser again
  afterwards unless `--keep-running`.
- **Native and invisible to pages.** Only browser-level `Storage.*` commands (and, if needed for
  one deletion case, a short flat session on a page target): never `Runtime.enable`,
  `Network.enable` or any other domain enable, never script evaluation. Same rule as
  `ui/cdp.py`; tests assert `CdpConnection.sent` contains no `*.enable`.
- **Exact round trips.** A cookie keeps every attribute through list → edit → save and through
  export → import: `name, value, domain` (leading dot = domain cookie; no dot = host-only), `path`,
  `expires` (unix seconds; session cookies have none), `httpOnly`, `secure`, `sameSite`
  (`Strict` / `Lax` / `None` / unset), `partitionKey` (CHIPS partitioned cookies: keep the CDP
  object as is), `priority`, `sourceScheme`, `sourcePort` when Chrome reports them.
- **Identity of a cookie** = `(name, domain, path, partitionKey)`. Editing any of these is "delete
  the old one, set the new one" (in that order only after the new one was accepted; if setting
  fails the old one stays).
- **Formats** (reuse `profilepilot.automation.cookies`): JSON (our portable list, `{cookies: [...]}`,
  Cookie-Editor / EditThisCookie exports, Playwright `storage_state` cookies) and Netscape
  `cookies.txt`. Extend that module where needed (e.g. `partitionKey`, `priority` passthrough,
  `storage_state` input); keep its existing behaviour and tests green.
- **Who may act:** the Manager is the user, so it may manage cookies of a paused profile. The CLI
  follows the pause rule (`ignore_pause` / `--ignore-pause`, see `cli._check_not_paused`). The
  Python client already refuses paused profiles.

## 2. Shared core: `src/profilepilot/browser/cookiejar.py` (new)

Async functions over a browser websocket URL (`RuntimeInfo.cdp_ws_url`), reusing
`ui.cdp.browser_connection` / `CdpConnection` (move them to a neutral module if importing `ui`
from `browser` would be wrong; keep `ui.cdp` re-exporting them):

```python
async def list_cookies(ws_url) -> list[dict]          # CDP Storage.getCookies, normalised (see 1)
async def set_cookies(ws_url, cookies) -> int          # Storage.setCookies; validates first
async def delete_cookies(ws_url, keys) -> int          # by identity; returns how many existed
async def clear_cookies(ws_url, *, domain=None) -> int # all, or one domain incl. subdomains
def cookie_key(cookie) -> str                          # stable opaque id for the UI (url-safe)
```

Deleting one cookie must not touch any other (verify against real Chrome whether
`Storage.setCookies` with a past `expires`, or `Network.deleteCookies` in a flat page session,
deletes exactly that cookie, including host-only vs domain cookies, different paths and
partitioned cookies; pick what works and test it with `@pytest.mark.chrome`). Validation errors
(empty name, bad domain, `SameSite=None` without `secure`, `__Secure-` / `__Host-` prefix rules,
value with control characters, absurd sizes) are raised as `ProfilePilotError` with a plain
message before anything is sent. Chrome's own refusal is reported as such.

## 3. Manager API (`src/profilepilot/ui/api.py`)

All under the existing auth / origin / CSRF handling and the `endpoint` error mapping. Profiles
that are not running answer 409 `not_running` (the UI then offers "Start in background").

| Method | Path | Body / query | Answer |
|---|---|---|---|
| GET | `/api/profiles/{pid}/cookies` | `?domain=` `&q=` (name/domain/value substring) | `{cookies: [view], domains: [{domain, count}], total}` |
| POST | `/api/profiles/{pid}/cookies` | `{cookie: {...}, replace?: key}` | `{cookie: view}` (create or edit) |
| DELETE | `/api/profiles/{pid}/cookies` | `{keys: [...]}` or `{domain}` or `{all: true}` | `{deleted: n}` |
| POST | `/api/cookies/parse` | `{text, format?}` | `{format, count, domains: [{domain, count}], problems: [...]}` (preview, no profile needed) |
| POST | `/api/profiles/{pid}/cookies/import` | `{text, format?, mode: "merge"\|"replace", domain?}` | `{imported, skipped, problems, domains}` |
| GET | `/api/profiles/{pid}/cookies/export` | `?format=json\|netscape&domain=&keys=` | the file (`Content-Disposition: attachment`) |
| POST | `/api/profiles/{pid}/start` | existing; add `{background: true}` = off-screen for this run | existing |

`view` = `{key, name, value, domain, host_only, path, expires (ISO or null), session, http_only,
secure, same_site, partitioned, partition_site, size, priority}`. Values are part of the view (this
is the user's own local UI behind its token), but the UI masks them until revealed. Import bodies
use the larger `MAX_IMPORT_BODY`. Every change appends an Activity entry (source `manager`,
"Edited 3 cookies on example.com" style, never values) and republishes the profile.

## 4. Manager UI (`src/profilepilot/ui/static/`)

A **Cookies** tab in the profile drawer (between Tabs and Activity), plus "Cookies…" in the
profile card's menu that opens the drawer on that tab.

- Not running: an empty state with **Start in background** (and Start).
- Toolbar: search, domain filter, counts, **Add**, **Import**, **Export** (JSON / cookies.txt, all
  or the current filter or the selection), **Delete selected**, **Clear…** (domain or all; confirm
  with what will happen: "logs this profile out of these sites").
- List grouped by domain (collapsible, counts), each row: name, masked value (click / eye to
  reveal, copy button), path, expiry ("Session", "in 3 days", "expired"), badges for
  HttpOnly / Secure / SameSite / Partitioned. Multi-select with checkboxes. Large jars (thousands
  of cookies) stay responsive (render lazily per domain group).
- Edit dialog (also for Add): all attributes with plain-language hints (host-only vs "this domain
  and its subdomains", session vs date-time picker, SameSite explanation), client-side validation
  mirroring the server's, dirty-guard on close.
- Import dialog: paste text or pick / drop a file; live preview from `/api/cookies/parse` (format,
  count, domains, problems); merge vs replace (replace = clear the listed domains first, or the
  whole jar when "replace everything" is chosen, with a confirm); then the result toast.
- Changes made by the AI or the page show up on refresh (manual refresh button; auto-refresh when
  the tab is opened).
- Light and dark themes, keyboard accessible, no console errors; README screenshots regenerated
  (`drawer-cookies-light.png` / `-dark.png`, edit dialog) with sample data only.

## 5. CLI (`src/profilepilot/cli.py`)

```
profilepilot cookies list PROFILE [--domain D] [--values] [--json]
profilepilot cookies export PROFILE [FILE] [--format json|netscape] [--domain D]   (FILE '-' = stdout)
profilepilot cookies import PROFILE FILE [--format ...] [--replace] [--domain D]   (FILE '-' = stdin)
profilepilot cookies set PROFILE NAME VALUE --domain D [--path P] [--expires ISO|unix|session]
                         [--secure] [--http-only] [--same-site Strict|Lax|None] [--host-only]
profilepilot cookies delete PROFILE [--domain D] [--name N] [--path P] [--all]
```

Values are hidden in `list` unless `--values`. A stopped profile is started off-screen for the
command and stopped again (`--keep-running` keeps it). All of them follow the pause rule
(`--ignore-pause`). Export files are written with owner-only permissions where the OS supports it;
existing files are only replaced with `--force`.

## 6. Tests

- `tests/test_cookiejar.py`: normalisation, keys, validation (no browser); `@pytest.mark.chrome`
  round trips against a real throwaway Chrome (`tests/chrome_helper.launch_chrome`): set / list /
  edit (rename = delete+set) / delete one of several same-name cookies (host-only vs domain,
  different paths, partitioned) / clear by domain / no `*.enable` sent.
- `tests/test_ui_api.py`: endpoints with a fake cookie backend (monkeypatched `cookiejar`
  functions), auth/origin checks, import preview and modes, export headers and formats, 409 when
  stopped, activity entries without values.
- `tests/test_cli.py` (or a new file): commands against a fake, pause refusal, stdout/stdin, file
  permissions/force.
- Smoke test: the Cookies tab renders and the edit/import dialogs open without console errors.
