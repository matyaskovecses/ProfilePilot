# Using ProfilePilot profiles from Scrapling

ProfilePilot profiles are real, native Chrome instances. Each one has its own user-data-dir
(cookies, storage, history) and its own proxy. [Scrapling](https://github.com/D4Vinci/Scrapling)
is a scraping framework that can drive a browser (Playwright) or send fast HTTP requests
(curl_cffi). This page shows how to point Scrapling at a profile so that it uses the profile's
**logged-in session, proxy and genuine browser fingerprint**.

```bash
pip install "profilepilot[scrapling]"     # Scrapling + its fetchers (curl_cffi, Playwright)
```

You don't need `scrapling install`, and nothing downloads a browser. ProfilePilot uses the Chrome
that is already installed.

## Why not just pass `cdp_url=` to Scrapling?

If you give Scrapling 0.4.15 a CDP URL, it attaches and then creates a **new, empty browser
context**. Inside that context it forces a dark color scheme, `devicePixelRatio = 2`, a synthetic
user agent in headless mode, and a fake `Referer: https://www.google.com/`. That means the
profile's cookies and logins are missing and the fingerprint no longer matches the real browser.
This was verified against Chrome 154; `tests/test_scrapling_integration.py` contains the control
experiment.

The classes below avoid all of that. They attach to the profile's own persistent context
(`browser.contexts[0]`), apply **no** context overrides, and when they close they only close the
tabs they opened themselves. The user's window, tabs and cookies are left as they were.

| You want | Use |
|---|---|
| A browser session inside the profile (async) | `AsyncProfileSession(ref, **scrapling_options)` |
| The same, synchronous | `ProfileSession(ref, **scrapling_options)` |
| One page, one call | `await fetch(ref, url, **options)` |
| Fast HTTP with the profile's exit IP and cookies | `fetcher_session(ref, **fetcher_options)` |
| The raw endpoints for your own code | `ProfilePilot().cdp_url(ref)`, `ProfilePilot().proxy_url(ref)` |

`ref` can be a profile name (case-insensitive), its id, or a unique id prefix. Whenever a profile
is needed and is not running yet, it is started. Pass `autostart=False` to get an error instead.
Sessions never stop the profile: call `ProfilePilot().stop(ref)` yourself when you are done.

While you have taken control of a profile in ProfilePilot Manager (or it waits for your help after the
AI asked for it), sessions and `ProfilePilot()` refuse to drive it, start, stop or re-route it: they
raise `ProfilePausedError` until you hand it back. Your own scripts can opt out with
`ProfilePilot(ignore_pause=True)` (pass it as `pilot=`); never give that to an agent.

## 0. Create a profile and start it

```python
from profilepilot import ProfilePilot

pp = ProfilePilot()  # data dir: %LOCALAPPDATA%\ProfilePilot (override with PROFILEPILOT_HOME)
pp.create("shop-de", proxy="socks5://user:pass@de.example.net:1080", lang="de-DE")
info = pp.start("shop-de")              # native Chrome window; window="offscreen" hides it
print(pp.cdp_url("shop-de"))            # http://127.0.0.1:<cdp-port>
print(pp.proxy_url("shop-de"))          # http://127.0.0.1:<relay-port>   (no credentials)
print(pp.proxy_url("shop-de", "socks5"))
```

Chrome can't authenticate to a SOCKS5 proxy. To work around that, every proxied profile runs a
**local relay** on `127.0.0.1` that needs no credentials, and the relay forwards connections
through the real upstream proxy using its credentials. `proxy_url()` is the address of that relay.
It speaks HTTP (CONNECT and plain) as well as SOCKS4/5. Anything you send through it leaves with
**the same exit IP as the browser**, and the password never appears in a URL, the process list
or a log. `proxy_url()` returns `None` when the profile has no proxy.

## 1. The raw endpoints (plain Playwright / plain Scrapling `Fetcher`)

ProfilePilot itself drives profiles with patchright, Playwright's API without the page-visible
`Runtime.enable` (see `profilepilot.automation.driver`). Attach with patchright too; upstream
Playwright works the same way but a page can detect it while it is attached.

```python
from patchright.sync_api import sync_playwright   # same API as playwright.sync_api
from scrapling.fetchers import Fetcher
from profilepilot import ProfilePilot

pp = ProfilePilot()

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(pp.cdp_url("shop-de"), no_defaults=True)
    context = browser.contexts[0]        # the profile itself; never browser.new_context()
    page = context.new_page()
    page.goto("https://example.com/account")
    print(page.title())
    page.close()
    browser.close()                      # over CDP this only disconnects; Chrome keeps running

# One-off HTTP request through the profile's proxy (no cookies, see fetcher_session below)
page = Fetcher.get("https://example.com/", proxy=pp.proxy_url("shop-de"), stealthy_headers=False)
```

## 2. `AsyncProfileSession`: Scrapling browser fetching inside the profile

```python
import asyncio
from profilepilot.integrations.scrapling import AsyncProfileSession

async def main():
    async with AsyncProfileSession("shop-de", max_pages=3) as session:
        page = await session.fetch("https://example.com/account", network_idle=True)
        print(page.status, page.css("h1::text").get())

        # Several tabs at once (up to max_pages), all in the logged-in profile:
        urls = [f"https://example.com/orders?page={n}" for n in range(1, 4)]
        pages = await asyncio.gather(*(session.fetch(u) for u in urls))
        for p in pages:
            print(p.url, len(p.css(".order")))

asyncio.run(main())
```

`AsyncProfileSession` is a subclass of Scrapling's patchright-based `AsyncStealthySession` (or of
`AsyncDynamicSession` when ProfilePilot's CDP driver is set to Playwright with `PROFILEPILOT_DRIVER` /
config `automation.driver`), so everything Scrapling offers on a page still works: `page_action`, `page_setup`, `wait_selector`, `network_idle`,
`disable_resources`, `blocked_domains`, `block_ads`, `capture_xhr`, `extra_headers`, `retries`,
`timeout`, the parser (`css`, `xpath`, `find_all`, ...), and so on.

```python
async def log_in(page):                     # page is a Playwright Page in the profile's context
    if await page.locator("text=Sign in").count():
        await page.click("text=Sign in")
        await page.wait_for_load_state("networkidle")

async with AsyncProfileSession("shop-de") as session:
    page = await session.fetch("https://example.com/", page_action=log_in, wait_selector="#account")
    print(session.profile.id, session.context)    # session.context is browser.contexts[0]
```

Some options are **refused** because the profile already owns them, or because they would change
the fingerprint:

| Option | Do this instead |
|---|---|
| `proxy`, `proxy_rotator`, `fetch(..., proxy=)` | Set the proxy on the profile (`pp.create(..., proxy=...)` / `pp.set_proxy(ref, ...)`). A profile has one sticky exit IP. |
| `useragent`, `locale`, `timezone_id`, `additional_args`, `init_script` | Keep the genuine browser; use the profile's `lang=` / `timezone=` launch options when you really need them. |
| `cookies` | Use `pp.set_cookies(ref, cookies)`. Cookies live in the profile. |
| `cdp_url`, `user_data_dir`, `real_chrome`, `executable_path`, `extra_flags`, `dns_over_https` | The browser is launched by ProfilePilot (profile `browser=` and `extra_args=` options). |
| `hide_canvas`, `allow_webgl`, `block_webrtc` | Stealth launch flags; the genuine browser stays as it is (the WebRTC policy is the profile's `webrtc=` launch option). |
| `headless` | Accepted and ignored; the window mode is a profile option (`window="normal"/"offscreen"/"headless"`). |

`google_search` (the fake Google referer) defaults to **False** here. Pass `google_search=True` to
turn it back on.

## 3. `ProfileSession`: the synchronous twin

```python
from profilepilot.integrations.scrapling import ProfileSession

with ProfileSession("shop-de") as session:
    page = session.fetch("https://example.com/account")
    print(page.css("title::text").get())
```

It has the same rules and the same refused options. Like all of Playwright's sync API, it must
not be used on a thread that runs an asyncio event loop. In async code, use `AsyncProfileSession`.

## 4. `fetch()`: one page, one call

```python
from profilepilot.integrations.scrapling import fetch

page = await fetch("shop-de", "https://example.com/account", network_idle=True, retries=1)
```

You can mix session options (`max_pages`, `retries`, ...) and fetch options (`network_idle`,
`wait_selector`, `page_action`, `extra_headers`, ...) in one call. The page opens in a new tab,
and that tab is closed afterwards.

## 5. `fetcher_session()`: fast HTTP with the profile's exit IP and cookies

`fetcher_session(ref)` returns a Scrapling `FetcherSession` (curl_cffi) that:

* routes every request through the profile's relay, so it uses the browser's exit IP and the
  upstream credentials never appear in a URL;
* loads the browser's current cookies into the curl session each time a `with` / `async with`
  block starts.

```python
from profilepilot.integrations.scrapling import fetcher_session

# sync
with fetcher_session("shop-de") as client:
    r = client.get("https://example.com/api/orders")
    print(r.status, r.json())

# async
async with fetcher_session("shop-de", impersonate="chrome", timeout=20) as client:
    r = await client.post("https://example.com/api/cart", json={"sku": "123"})
```

Options:

* `write_back=True` copies cookies that were **added or changed** during the block back into the
  browser when the block exits, for example a session cookie refreshed by an API call.
  Deletions are not propagated.
* `cookie_urls=["https://example.com/"]` loads only the cookies that would be sent to those
  URLs. By default every cookie is loaded.
* Any other `FetcherSession` option works (`impersonate`, `headers`, `timeout`, `retries`,
  `follow_redirects`, `verify`, ...), **except** `proxy` / `proxies` / `proxy_auth` /
  `proxy_rotator`, which are refused.
* `stealthy_headers` defaults to **False** (no fake Google referer).
* `session.proxy_url` is the relay URL being used.

Note that curl_cffi impersonates a Chrome TLS/HTTP2 fingerprint from its own list, and that
version may differ from the profile's real Chrome (154 here). For requests that have to look
exactly like the browser, use `AsyncProfileSession`.

On Windows, curl_cffi's async client prints a one-time `CurlCffiWarning` about the Proactor event
loop and adds a selector thread for itself. The warning is harmless.

## 6. Spiders: register profile sessions in `configure_sessions`

Scrapling spiders route each request to a session in their `SessionManager`, chosen by
`Request(..., sid=...)`. Profile sessions plug straight in:

```python
from scrapling.spiders import Request, Spider
from profilepilot.integrations.scrapling import AsyncProfileSession, fetcher_session

class OrdersSpider(Spider):
    name = "orders"
    start_urls = ["https://example.com/orders"]
    concurrent_requests = 4

    def configure_sessions(self, manager):
        # The first session added is the default. Each profile is its own identity.
        manager.add("de", AsyncProfileSession("shop-de", max_pages=4), default=True)
        manager.add("de-http", fetcher_session("shop-de"), lazy=True)
        manager.add("us", AsyncProfileSession("shop-us", max_pages=2), lazy=True)

    async def parse(self, response):
        for href in response.css("a.order::attr(href)").getall():
            yield Request(response.urljoin(href), sid="de-http", callback=self.parse_order)
        yield Request("https://example.com/us/orders", sid="us", callback=self.parse_us)

    async def parse_order(self, response):
        yield {"id": response.css("#id::text").get(), "total": response.css(".total::text").get()}

    async def parse_us(self, response):
        yield {"us_orders": len(response.css(".order"))}

result = OrdersSpider().start()
print(len(result.items), result.stats)
```

When the crawl ends, the spider closes its sessions. Only the tabs the sessions opened are
closed; the profiles keep running. Stop them with `ProfilePilot().stop("shop-de")` if you want to.
`fetcher_session(...)` starts its profile as soon as it is created, even when it is registered
with `lazy=True`.

## 7. Cookies between the browser and your code

```python
pp = ProfilePilot()
cookies = pp.cookies("shop-de", "https://example.com/")   # Playwright cookie dicts
pp.set_cookies("shop-de", [{"name": "consent", "value": "1", "url": "https://example.com/"}])
```

Cookie values are secrets (sessions, logins), so never log them. For export and import in JSON,
Netscape `cookies.txt` or `http.cookiejar`, see `profilepilot.automation.cookies`.

## 8. Using a non-default data directory or an existing `ProfilePilot`

Every helper accepts `root=` (the data directory) or `pilot=` (an existing `ProfilePilot`):

```python
pp = ProfilePilot(r"D:\pp-data")
async with AsyncProfileSession("shop-de", pilot=pp) as session: ...
with fetcher_session("shop-de", pilot=pp) as client: ...
```

## Troubleshooting

* **`Profile 'x' is not running`**: you passed `autostart=False`. Start the profile with
  `pp.start("x")` first.
* **`Could not attach to profile ... over CDP`**: the browser was closed while you were attaching.
  Check `pp.info("x")` and start the profile again.
* **Requests do not use the proxy you just set**: a profile that was started *without* a proxy
  has no relay. Restart it (`pp.stop` then `pp.start`). A profile that already has a proxy
  switches to a new one live with `pp.set_proxy(ref, proxy)`.
* **A login that works in the browser fails over HTTP**: the site probably binds the session to
  the browser's TLS fingerprint or to client-side tokens. Use `AsyncProfileSession` for those
  requests.
