"""ProfilePilot Manager in a real browser: every view renders without console errors.

The test seeds a temporary data root with obviously fake profiles, proxies, identities, help requests
and activity, starts three of the profiles for real (headless, local test pages), serves the
Manager and opens it in a throwaway Chrome driven by Playwright. Screenshots of every view and of the
main dialogs, in the light and the dark theme, are saved to ``docs/img/manager/`` for the README
(``PROFILEPILOT_UI_SHOTS=0`` skips writing them).

Fake data only: example.net / example.com hosts, documentation IP ranges (RFC 5737), the 555-01xx
phone range, and Stripe's public test card number.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from profilepilot.control import ActivityEvent, ActivityLog, ControlStore
from profilepilot.identity import IdentityStore
from profilepilot.jsonio import read_json, write_json
from profilepilot.models import ProxyCheck
from profilepilot.store import Store

ROOT = Path(__file__).resolve().parents[1]
SHOTS_DIR = ROOT / "docs" / "img" / "manager"
MAX_SHOT_BYTES = 400 * 1024

# ---------------------------------------------------------------------- seed data

PROXIES = [
    # (name, url, check)  - hosts and IPs are reserved for documentation (RFC 2606 / RFC 5737)
    ("de-frankfurt", "socks5://pp-demo:not-a-real-pass@de1.proxy.example.net:1080",
     dict(ok=True, ip="203.0.113.24", country="Germany", country_code="DE", city="Frankfurt am Main", isp="Example Hosting", latency_ms=182)),
    ("us-residential", "http://alex-demo:not-a-real-pass@us-resi.example.net:8000",
     dict(ok=True, ip="198.51.100.17", country="United States", country_code="US", city="New York", isp="Example ISP", latency_ms=241)),
    ("uk-london", "socks5://uk.proxy.example.net:1080",
     dict(ok=True, ip="203.0.113.80", country="United Kingdom", country_code="GB", city="London", isp="Example Networks", latency_ms=98)),
    ("jp-tokyo", "http://jp.proxy.example.net:3128",
     dict(ok=False, error="upstream proxy jp.proxy.example.net:3128 refused the connection")),
    ("fr-paris", "https://demo:not-a-real-pass@fr.proxy.example.net:443", None),
    ("ca-toronto", "socks5://ca.proxy.example.net:1080",
     dict(ok=True, ip="198.51.100.140", country="Canada", country_code="CA", city="Toronto", isp="Example Fiber", latency_ms=156)),
]

PROFILES = [
    # (name, proxy, tags, identity, extra)
    ("shop-us", "us-residential", ["shop", "us"], "Alex Sample", {"notes": "Checkout flows on the demo store."}),
    ("mail-de", "de-frankfurt", ["mail", "de"], None, {}),
    ("research", None, ["research"], None, {"notes": "Reading and summarising articles."}),
    ("social-uk", "uk-london", ["social", "uk"], "Sam Placeholder", {}),
    ("jp-market", "jp-tokyo", ["shop", "jp"], None, {}),
    ("travel-fr", "fr-paris", ["travel"], "Alex Sample", {"window": "offscreen"}),
    ("news-ca", "ca-toronto", ["news"], None, {"browser": "edge"}),
]

IDENTITIES = {
    "Alex Sample": {
        "values": {"first_name": "Alex", "last_name": "Sample", "email": "alex.sample@example.com", "phone": "+1 555 0100",
                   "birth_date": "1990-01-01", "street": "1 Example Street", "city": "Springfield", "state": "IL",
                   "postal_code": "62701", "country": "United States", "country_code": "US"},
        "secrets": {"card_number": "4242424242424242", "card_exp_month": "12", "card_exp_year": "2030", "card_cvv": "123"},
        "origins": ["https://shop.example.com"],
    },
    "Sam Placeholder": {
        "values": {"first_name": "Sam", "last_name": "Placeholder", "email": "sam@example.org", "company": "Example Corp",
                   "phone": "+1 555 0123", "city": "London", "country": "United Kingdom", "country_code": "GB"},
        "secrets": {"password": "fake-demo-password"},
        "origins": [],
    },
}


def seed_store(store: Store) -> dict[str, str]:
    """Fill ``store`` with the demo data. Returns {profile name: id}."""
    ids: dict[str, str] = {}
    proxy_ids: dict[str, str] = {}
    history: dict[str, list[dict[str, Any]]] = {}
    now = datetime.now(timezone.utc)
    for i, (name, url, check) in enumerate(PROXIES):
        rec = store.add_proxy(url, name, tags=["residential"] if "resi" in url else [])
        proxy_ids[name] = rec.id
        if check:
            store.set_proxy_check(rec.id, ProxyCheck(provider="ipwho.is", checked_at=now - timedelta(minutes=7 + i * 11), **check))
            base = check.get("latency_ms") or 0
            history[rec.id] = [{"t": (now - timedelta(hours=12 - k)).isoformat(), "ok": check["ok"] or k % 3 != 0,
                                "ms": int(base * (0.8 + 0.4 * ((k * 37) % 10) / 10)) if check["ok"] else None} for k in range(12)]
    write_json(store.root / "proxy_history.json", {"proxies": history})

    idents = IdentityStore(store)
    ident_ids: dict[str, str] = {}
    for name, data in IDENTITIES.items():
        ident = idents.create(name, data["values"])
        for key, value in data["secrets"].items():
            idents.set_sensitive(ident.id, key, value)
        for origin in data["origins"]:
            idents.allow_origin(ident.id, origin)
        ident_ids[name] = ident.id

    for name, proxy, tags, identity, extra in PROFILES:
        launch = {"window": extra.get("window", "normal")}
        profile = store.create_profile(name, tags=tags, proxy_id=proxy_ids.get(proxy) if proxy else None,
                                       identity_id=ident_ids.get(identity) if identity else None,
                                       browser=extra.get("browser", "auto"), notes=extra.get("notes", ""), launch=launch)
        ids[name] = profile.id
    # jp-market crashed last time
    write_json(store.profile_dir(ids["jp-market"]) / "last_exit.json",
               {"profile_id": ids["jp-market"], "code": 3221225477, "crashed": True, "crash": "access violation (0xC0000005)",
                "requested": False, "chrome_pid": 1, "chrome_create_time": 1.0, "at": time.time() - 3600})

    control = ControlStore(store)
    control.request_help(ids["shop-us"], "Solve the CAPTCHA on the checkout page, then click Done.", "captcha",
                         requested_by="claude-ai")
    control.pause(ids["mail-de"], note="Signing in with my security key")

    log = ActivityLog(store.root)
    steps = [
        ("research", "browser_navigate", "Opened https://news.example.com/article/browser-profiles at launch", True, 1840, "claude-ai"),
        ("research", "browser_snapshot", "Page 'Why separate browser profiles matter' - 214 elements", True, 312, "claude-ai"),
        ("research", "browser_read", "Read 9,840 characters (markdown) from the main article", True, 488, "claude-ai"),
        ("shop-us", "browser_navigate", "Navigated to https://shop.example.com/products/trail-shoes (200)", True, 2210, "claude-ai"),
        ("shop-us", "browser_click", "Clicked button 'Add to cart' (e41)", True, 260, "claude-ai"),
        ("shop-us", "form_autofill", "Filled 9 fields from identity 'Alex Sample' (paste)", True, 3120, "claude-ai"),
        ("shop-us", "browser_click", "Clicked 'Continue to payment' (e88)", True, 410, "claude-ai"),
        ("shop-us", "profile_request_help", "The user has been asked in ProfilePilot Manager (CAPTCHA)", True, 35, "claude-ai"),
        ("mail-de", "take control", "You took control of 'mail-de': Signing in with my security key", True, 12, None),
        ("mail-de", "browser_snapshot", "The user has taken control of profile 'mail-de'. Don't act on this profile now.", False, 4, "openai-mcp"),
        (None, "proxy_test", "OK: proxy 'uk-london' -> exit IP 203.0.113.80; London, United Kingdom (98 ms)", True, 1180, "claude-ai"),
        ("jp-market", "browser_navigate", "Navigation timed out: the site or the profile's proxy did not respond", False, 30010, "claude-ai"),
    ]
    start = now - timedelta(minutes=len(steps) * 3)
    for k, (pname, tool, summary, ok, ms, client) in enumerate(steps):
        log.append(ActivityEvent(ts=start + timedelta(minutes=k * 3), profile_id=ids.get(pname) if pname else None,
                                 profile_name=pname, source="ui" if client is None else ("mcp-http" if client == "openai-mcp" else "mcp"),
                                 client=client, tool=tool, summary=summary, ok=ok, ms=ms))
    return ids


# ---------------------------------------------------------------------- demo pages served to the running profiles

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>
body{{margin:0;font:15px/1.5 Segoe UI,Arial,sans-serif;color:#1d1d24;background:{bg}}}
header{{display:flex;align-items:center;gap:14px;padding:14px 28px;background:{bar};color:#fff}}
header b{{font-size:19px}} nav span{{margin-left:18px;opacity:.85}} main{{padding:24px 28px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}
.card{{background:#fff;border-radius:12px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.img{{height:110px;border-radius:9px;background:linear-gradient(135deg,{a},{b});margin-bottom:10px}}
.btn{{display:inline-block;margin-top:8px;padding:7px 14px;border-radius:8px;background:{bar};color:#fff;font-weight:600}}
.row{{display:flex;gap:12px;padding:12px 4px;border-bottom:1px solid #e6e6ee}} .row b{{width:170px}}
.captcha{{margin-top:18px;border:2px solid #f2a10c;border-radius:12px;padding:16px;background:#fff8e8;width:330px}}
.box{{display:inline-block;width:22px;height:22px;border:2px solid #999;border-radius:4px;vertical-align:middle;margin-right:10px;background:#fff}}
h1{{margin:0 0 6px;font-size:26px}} p{{color:#555;max-width:720px}}
</style></head><body>{body}</body></html>"""

PAGES = {
    "/shop": _PAGE.format(title="Trail Outfitters - Checkout (demo)", bg="#f4f5f8", bar="#16794c", a="#7bd389", b="#2a9d8f", body="""
<header><b>Trail Outfitters</b><nav><span>Shoes</span><span>Jackets</span><span>Packs</span></nav></header>
<main><h1>Checkout</h1><p>Almost there. Confirm you are human to continue to payment.</p>
<div class="grid"><div class="card"><div class="img"></div><b>Trail shoes</b><br>$89.00</div>
<div class="card"><div class="img"></div><b>Rain shell</b><br>$129.00</div><div class="card"><div class="img"></div><b>Day pack</b><br>$59.00</div></div>
<div class="captcha"><span class="box"></span><b>I'm not a robot</b><div style="color:#8a5a00;margin-top:8px">Verification required</div></div></main>"""),
    "/mail": _PAGE.format(title="Example Mail - Inbox (demo)", bg="#f3f6fb", bar="#2b59c3", a="#9ab8ff", b="#5b7cfa", body="""
<header><b>Example Mail</b><nav><span>Inbox</span><span>Sent</span><span>Drafts</span></nav></header>
<main><h1>Inbox</h1><div class="card">
<div class="row"><b>Example Bank</b>Your monthly statement is ready</div>
<div class="row"><b>Trail Outfitters</b>Your order has shipped</div>
<div class="row"><b>Team Calendar</b>Planning meeting moved to Thursday</div>
<div class="row"><b>Newsletter</b>Ten tips for a tidy inbox</div></div></main>"""),
    "/research": _PAGE.format(title="Example News - Why separate browser profiles matter", bg="#faf8f4", bar="#5d3fd3", a="#c9b8ff", b="#7c6cf2", body="""
<header><b>Example News</b><nav><span>Technology</span><span>Science</span><span>Culture</span></nav></header>
<main><h1>Why separate browser profiles matter</h1><p>Each profile keeps its own cookies, logins and history, so
accounts never mix. Combined with a dedicated network route per profile, every identity stays tidy and isolated.</p>
<div class="grid"><div class="card"><div class="img"></div><b>Cookies</b><p>Stay with their profile.</p></div>
<div class="card"><div class="img"></div><b>Logins</b><p>Never leak across accounts.</p></div>
<div class="card"><div class="img"></div><b>Routes</b><p>One exit per identity.</p></div></div></main>"""),
}


class _PageHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # quiet
        pass

    def do_GET(self) -> None:  # noqa: N802
        body = PAGES.get(self.path.split("?")[0], "<!doctype html><title>demo</title>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def demo_pages() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------- the Manager server in a thread


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch_url(port: int, token: str) -> str:
    """A one-time sign-in URL for the Manager on ``port`` (the master token never travels in a URL)."""
    import httpx

    from profilepilot.ui.server import TOKEN_HEADER

    resp = httpx.post(f"http://127.0.0.1:{port}/api/launch-code", headers={TOKEN_HEADER: token}, timeout=10,
                      trust_env=False)
    resp.raise_for_status()
    return f"http://127.0.0.1:{port}/?t={resp.json()['code']}"


@contextlib.contextmanager
def manager_server(store: Store, **kwargs: Any) -> Iterator[tuple[int, str]]:
    """Serve the Manager for ``store`` on a free port in a background thread. Yields (port, token)."""
    import uvicorn

    from profilepilot.ui.launcher import bind_socket
    from profilepilot.ui.server import create_app

    sock = bind_socket(0)
    port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    app = create_app(store, token=token, port=port, **kwargs)
    config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on", ws="none", log_level="warning",
                            timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield port, token
    finally:
        server.should_exit = True
        thread.join(10)
        with contextlib.suppress(Exception):
            sock.close()


# ---------------------------------------------------------------------- the smoke test

VIEWS = ["profiles", "proxies", "identities", "activity", "connections", "settings"]
DEMO_ROOT = r"C:\Users\you\AppData\Local\ProfilePilot"
SANITIZE_JS = """(pairs) => {
  const fix = (text) => pairs.reduce((t, [a, b]) => t.split(a).join(b), text);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const fixed = fix(n.nodeValue);
    if (fixed !== n.nodeValue) n.nodeValue = fixed;
  }
  for (const el of document.querySelectorAll("[title]")) el.title = fix(el.title);
}"""


def wait_until(predicate: Any, seconds: float) -> None:
    """Poll ``predicate`` (the page's CSP forbids Playwright's string-eval wait_for_function)."""
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {seconds} s")
        time.sleep(0.2)


def _kill_leftovers(marker: Path) -> None:
    """Kill only processes whose command line mentions our temporary folder."""
    import psutil

    needle = str(marker).lower()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if any(needle in str(a).lower() for a in proc.info.get("cmdline") or []):
                proc.kill()
        except (psutil.Error, OSError):
            continue


@pytest.mark.chrome
def test_manager_views_render_without_console_errors(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from profilepilot import chrome_autofill
    from profilepilot.browser.runtime import RuntimeManager

    from .chrome_helper import launch_chrome
    from .test_ui_api import HOME_EMAIL, WORK_STREET, make_web_data

    store = Store(tmp_path / "pp-home")
    ids = seed_store(store)
    # Addresses "saved in the browser" come from a fake User Data folder: the user's real Chrome data is
    # never read (and never ends up on a README screenshot). One identity takes its details from it.
    browser_data = make_web_data(tmp_path / "browser")
    monkeypatch.setattr(chrome_autofill, "_user_data_dirs", lambda: {"chrome": browser_data})
    idents = IdentityStore(store)
    idents.connect_chrome(idents.create("Personal").id, "chrome")
    runtime = RuntimeManager(store)
    shots = os.environ.get("PROFILEPILOT_UI_SHOTS", "1") != "0"
    if shots:
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with demo_pages() as pages:
            # Three real profiles, started headless (never on the user's screen, never taking the keyboard focus)
            # without their fake proxies.
            for name, path in (("shop-us", "/shop"), ("mail-de", "/mail"), ("research", "/research")):
                profile = store.get_profile(name)
                saved_proxy = profile.proxy_id
                store.update_profile(profile.id, proxy_id=None)
                runtime.start(profile.id, window="headless", start_url=f"{pages}{path}", timeout=60)
                if saved_proxy:
                    store.update_profile(profile.id, proxy_id=saved_proxy)  # shown on the card; not used by the run
                # Present them as normal windows on the screenshots (they are headless only for the test).
                runtime_file = store.runtime_file(profile.id)
                data = read_json(runtime_file)
                data["window"] = "normal"
                write_json(runtime_file, data)
            time.sleep(2.0)  # let the pages paint

            # Never let the test bring a window to the front or on-screen: no native focus, and the
            # page's Focus requests are answered by the test (see page.route below).
            # Client configs live in temporary folders: the user's real Claude/Codex/Cursor configs are never read.
            from profilepilot.install import Locations, register

            locations = Locations(home=tmp_path / "userhome", appdata=tmp_path / "appdata",
                                  localappdata=tmp_path / "local", platform=sys.platform, claude_cli=None)
            for client in ("claude-desktop", "codex"):
                register(client, locations=locations)  # type: ignore[arg-type]
            with manager_server(store, focuser=lambda _pid: False, locations=locations) as (port, token), \
                    launch_chrome(tmp_path / "ui-browser", "--force-device-scale-factor=1", "--headless=new") as chrome, \
                    sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(chrome.http_url)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.new_page()
                page.set_viewport_size({"width": 1320, "height": 860})
                errors: list[str] = []
                page.on("console", lambda msg: errors.append(f"{msg.type}: {msg.text}") if msg.type == "error" else None)
                page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
                page.route("**/api/profiles/*/focus", lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"ok": True, "focused": True})))
                base = f"http://127.0.0.1:{port}"

                # Unauthenticated: the API refuses, the page says "session ended". The master token is
                # not a sign-in code either; a one-time launch code is.
                page.goto(f"{base}/?t=wrong-code")
                assert "expired" in page.content().lower()
                page.goto(f"{base}/?t={token}")
                assert "expired" in page.content().lower()
                page.goto(launch_url(port, token))
                page.wait_for_selector(".profile-card", timeout=15000)
                errors.clear()  # the 401 page above logs its own status

                def shoot(name: str) -> None:
                    if not shots:
                        return
                    # README screenshots must not show this machine's user name or temp folders.
                    page.evaluate(SANITIZE_JS, [[str(store.root), DEMO_ROOT],
                                                [str(tmp_path / "appdata"), r"C:\Users\you\AppData\Roaming"],
                                                [str(tmp_path / "userhome"), r"C:\Users\you"],
                                                [str(tmp_path), DEMO_ROOT],
                                                [f"\\Users\\{Path.home().name}", "\\Users\\you"],
                                                [f"/Users/{Path.home().name}", "/Users/you"]])
                    path = SHOTS_DIR / f"{name}.png"
                    page.screenshot(path=str(path))
                    if path.stat().st_size > MAX_SHOT_BYTES:
                        page.screenshot(path=str(path), scale="css", clip={"x": 0, "y": 0, "width": 1320, "height": 760})
                    assert path.stat().st_size <= MAX_SHOT_BYTES, f"{path.name} is {path.stat().st_size} bytes"

                def alex_card() -> Any:
                    # (the "Personal" card mentions "Alex Sample" too: its browser address' summary)
                    return page.locator(".identity-card").filter(has=page.get_by_role("button", name="Alex Sample", exact=True))

                def discard_with_escape() -> None:
                    """Esc on a dialog with typed text asks before throwing it away (never silently)."""
                    page.keyboard.press("Escape")
                    page.locator("dialog.narrow[open]").get_by_role("button", name="Discard", exact=True).click()
                    page.wait_for_selector("dialog[open]", state="detached")

                for theme in ("light", "dark"):
                    page.emulate_media(color_scheme=theme)
                    for view in VIEWS:
                        page.evaluate(f"location.hash = '#/{view}'")
                        page.wait_for_selector(f"section.view[aria-label='{view.capitalize()}']", timeout=10000)
                        if view == "profiles":
                            # live thumbnails of the three running profiles
                            wait_until(lambda: page.locator(".thumb img.loaded").count() >= 3, 25)
                            assert page.locator(".help-banner").count() == 1
                            assert "Needs you" in page.locator(".profile-card", has_text="shop-us").inner_text()
                            assert "in control" in page.locator(".profile-card", has_text="mail-de").inner_text()
                            # A tool call arriving over the live event stream marks the card "AI working".
                            ActivityLog(store.root).append(ActivityEvent(
                                profile_id=ids["research"], profile_name="research", source="mcp", client="claude-ai",
                                tool="browser_snapshot", summary="Page 'Why separate browser profiles matter' - 214 elements"))
                            wait_until(lambda: page.locator(".profile-card .thumb-live.ai").count() == 1, 10)
                        if view == "proxies":
                            page.wait_for_selector("table.table tbody tr")
                            assert page.locator("table.table tbody tr").count() == len(PROXIES)
                            assert "not-a-real-pass" not in page.content()
                        if view == "identities":
                            page.wait_for_selector(".identity-card")
                            assert "4242424242424242" not in page.content() and "•••• 4242" in page.content()
                            linked = page.locator(".identity-card .ic-link").inner_text()  # "Personal" only
                            assert "from Google Chrome profile 'Me'" in linked and "Springfield" in linked
                            content = page.content()  # summaries only: never the browser's e-mail, phone or street
                            assert HOME_EMAIL not in content and WORK_STREET not in content
                        if view == "activity":
                            page.wait_for_selector(".feed-item")
                        if view == "connections":
                            page.wait_for_selector(".client-card", timeout=20000)
                        if view == "settings":
                            page.wait_for_selector(".setting")
                        page.wait_for_timeout(400)
                        shoot(f"{view}-{theme}")

                    # dialogs and drawers
                    page.evaluate("location.hash = '#/profiles'")
                    page.wait_for_selector(".profile-card")
                    page.keyboard.press("n")
                    page.wait_for_selector("dialog.dialog[open]")
                    page.locator("dialog.dialog[open] input").first.fill("new-profile")
                    page.wait_for_timeout(300)
                    shoot(f"dialog-new-profile-{theme}")
                    discard_with_escape()

                    page.locator(".profile-card", has_text="shop-us").locator(".pc-name").click()
                    page.wait_for_selector("dialog.drawer[open]")
                    page.wait_for_selector("dialog.drawer[open] .big-thumb img.loaded", timeout=15000)
                    page.wait_for_timeout(300)
                    shoot(f"drawer-profile-{theme}")
                    page.locator("dialog.drawer[open] .drawer-tabs button", has_text="Tabs").click()
                    page.wait_for_selector("dialog.drawer[open] .list-item")
                    page.keyboard.press("Escape")
                    page.wait_for_selector("dialog[open]", state="detached")

                    page.evaluate("location.hash = '#/proxies'")
                    page.wait_for_selector("table.table")
                    page.keyboard.press("n")
                    page.wait_for_selector("dialog.dialog[open] textarea")
                    page.locator("dialog.dialog[open] textarea").fill(
                        "socks5://demo:not-a-real-pass@nl.proxy.example.net:1080  # Amsterdam\n"
                        "203.0.113.50:8080:demo:not-a-real-pass\nthis is not a proxy")
                    page.wait_for_selector("dialog.dialog[open] .list-item .badge.red")
                    page.wait_for_timeout(300)
                    shoot(f"dialog-import-proxies-{theme}")
                    discard_with_escape()

                    page.evaluate("location.hash = '#/identities'")
                    alex_card().locator(".ic-name").click()
                    page.wait_for_selector("dialog.drawer[open] .secret-row")
                    assert "4242424242424242" not in page.content()
                    page.wait_for_timeout(300)
                    shoot(f"drawer-identity-{theme}")
                    page.keyboard.press("Escape")
                    page.wait_for_selector("dialog[open]", state="detached")

                # Take control / hand back round trip through the UI.
                page.emulate_media(color_scheme="light")
                page.evaluate("location.hash = '#/profiles'")
                card = page.locator(".profile-card", has_text="research")
                card.get_by_role("button", name="Take control").click()
                wait_until(lambda: page.locator(".profile-card.is-paused", has_text="research").count() == 1, 10)
                assert ControlStore(store).paused(ids["research"]) is not None
                page.locator(".profile-card", has_text="research").get_by_role("button", name="Hand back").click()
                wait_until(lambda: page.locator(".profile-card.is-paused", has_text="research").count() == 0, 10)
                assert ControlStore(store).paused(ids["research"]) is None
                # Connect an identity to the browser's saved addresses (the fake User Data folder).
                page.evaluate("location.hash = '#/identities'")
                alex_card().get_by_role("button", name="Connect to browser").click()
                page.wait_for_selector("dialog.dialog[open] .source-option")
                page.locator("dialog.dialog[open] .source-option", has_text="Chicago").click()
                page.locator("dialog.dialog[open]").get_by_role("button", name="Connect", exact=True).click()
                page.wait_for_selector("dialog.dialog[open]", state="detached")
                wait_until(lambda: "Chicago" in alex_card().inner_text(), 10)
                alex = next(i for i in IdentityStore(store).list() if i.name == "Alex Sample")
                assert alex.chrome_address and alex.chrome_source == "chrome:chrome/Default"
                alex_card().get_by_role("button", name="Disconnect").click()
                wait_until(lambda: IdentityStore(store).get(alex.id).chrome_source is None, 10)
                page.evaluate("location.hash = '#/profiles'")
                # The help banner's "I'm done" button resolves the request.
                page.locator(".help-banner").get_by_role("button", name="I'm done").click()
                page.wait_for_selector(".help-banner", state="detached", timeout=10000)
                assert ControlStore(store).help_requests() == []

                # First run: an empty data folder shows the "get started" steps.
                with manager_server(Store(tmp_path / "empty-home"), focuser=lambda _pid: False,
                                    locations=locations) as (port2, token2):
                    page.goto(launch_url(port2, token2))
                    page.wait_for_selector(".onboarding .onb-step")
                    assert page.locator(".onb-step").count() == 3
                    for theme in ("light", "dark"):
                        page.emulate_media(color_scheme=theme)
                        page.wait_for_timeout(300)
                        shoot(f"welcome-{theme}")

                assert not [e for e in errors if "favicon" not in e], "\n".join(errors)
                browser.close()
    finally:
        runtime.stop_all(timeout=20)
        _kill_leftovers(tmp_path)
