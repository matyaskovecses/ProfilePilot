# Acknowledgements

ProfilePilot is built on top of two open-source projects, and it wouldn't exist without them.

## ShardBrowser / ShardX by the ProxyShard team

**[github.com/ProxyShard/ShardBrowser](https://github.com/ProxyShard/ShardBrowser)** · MIT License · © ShardBrowser

ShardX is a free, open-source anti-detect browser launcher for web scraping and multi-accounting. Its
design was the starting point for ProfilePilot. We studied it closely and adopted these ideas:

- **Profiles and proxies:** one Chromium user-data directory per profile, a proxy library, and bulk import of proxies in common provider formats.
- **Browser lifecycle:** close Chrome gracefully first and force it only if needed (`Browser.close`, then WM_CLOSE, then kill), and poll the DevTools endpoint with a timeout.
- **Data hygiene:** atomic JSON writes, tolerant reading of BOM and UTF-16 files, and a trash bin that can restore deleted profiles.
- **Local control API:** an authenticated API, plus the general shape of driving profiles from an MCP server.
- **Proxy checks:** exit-IP and geolocation lookups through the proxy itself.

ProfilePilot takes a deliberately **different** path on fingerprints. ShardX patches Chromium to spoof
a device's fingerprint. ProfilePilot runs your *native* Chrome unmodified, and it adds a local relay because
stock Chrome cannot log in to SOCKS5 proxies by itself. ProfilePilot can also drive ShardX profiles
directly through ShardX's local API: see `shardx_*` tools and `profile="shardx:<name>"`.

## Scrapling by Karim Shoair (D4Vinci)

**[github.com/D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling)** · BSD 3-Clause License · © 2024 Karim Shoair

Scrapling is an adaptive web-scraping framework. ProfilePilot uses it in three ways:

- **Extraction:** `browser_extract` uses Scrapling's `Selector` for CSS (including `::text` and `::attr()`) and XPath.
- **Browser fetching:** `AsyncProfileSession` / `ProfileSession` subclass Scrapling's dynamic sessions so Scrapling's fetchers and spiders run inside a ProfilePilot profile. They reuse the profile's cookies, logins and proxy.
- **HTTP fetching:** `http_fetch(engine="scrapling")` and `fetcher_session()` use Scrapling's curl_cffi-based `FetcherSession`.

Scrapling's own MCP server also showed us how to use the MCP Python SDK v2: static bearer auth,
transport security, and image results.

## Also built with

- [Playwright](https://github.com/microsoft/playwright-python) (Apache-2.0) and [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) (Apache-2.0) to talk to Chrome over the DevTools Protocol.
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (MIT).
- [python-socks](https://github.com/romis2012/python-socks) (Apache-2.0), [curl_cffi](https://github.com/lexiforest/curl_cffi) (MIT), [httpx](https://github.com/encode/httpx) (BSD-3-Clause), [psutil](https://github.com/giampaolo/psutil) (BSD-3-Clause), [filelock](https://github.com/tox-dev/filelock) (Unlicense), [keyring](https://github.com/jaraco/keyring) (MIT), [markdownify](https://github.com/matthewwithanm/python-markdownify) (MIT), [websockets](https://github.com/python-websockets/websockets) (BSD-3-Clause), [pydantic](https://github.com/pydantic/pydantic) (MIT) and [pywin32](https://github.com/mhammond/pywin32) (PSF).

The fingerprint audit used these public test pages: bot.sannysoft.com, bot-detector.rebrowser.net,
browserscan.net, deviceandbrowserinfo.com, bot.incolumitas.com, fingerprint.com, CreepJS, pixelscan.net,
iphey.com, whoer.net, browserleaks.com, ipleak.net, test-ipv6.com and tls.peet.ws. Thanks to their
authors for making detection transparent.
