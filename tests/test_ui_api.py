"""ProfilePilot Manager API: security layer, CRUD, secrets, control round trips, events, clients, launcher.

Everything runs in-process through ``httpx.ASGITransport`` against a temporary store. No browser is
started (a fake runtime writes ``runtime.json`` for the test process itself), no network is used
(proxy checks are faked) and client configs live in temporary folders.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from profilepilot.control import ActivityEvent, ActivityLog, ControlStore
from profilepilot.identity import IdentityStore
from profilepilot.install import Locations
from profilepilot.jsonio import read_json, write_json
from profilepilot.models import ProxyCheck, RuntimeInfo, utcnow
from profilepilot.store import Store
from profilepilot.ui.api import Checks
from profilepilot.ui.server import TOKEN_HEADER, Auth, create_app

PORT = 47123
TOKEN = "t" * 20 + "-test-token-0123456789abcdef"
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = {"Origin": BASE}
PROXY_PASSWORD = "Pr0xy-S3cret-pw"
CARD = "4242424242424242"
SSN = "123-45-6789"


class FakeRuntime:
    """Writes a runtime.json for this test process (alive), so the API sees the profile running."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.started: list[tuple[str, Any]] = []
        self.stopped: list[str] = []
        self.upstreams: list[tuple[str, str | None]] = []
        self.opened: list[tuple[str, str]] = []

    def start(self, ref: str, *, timeout: float = 60.0, window: Any = None, start_url: Any = None) -> RuntimeInfo:
        p = self.store.get_profile(ref)
        me = psutil.Process()
        info = RuntimeInfo(profile_id=p.id, profile_name=p.name, state="running", host_pid=os.getpid(),
                           chrome_pid=os.getpid(), chrome_create_time=me.create_time(), cdp_port=9,
                           cdp_http_url="http://127.0.0.1:9", cdp_ws_url="ws://127.0.0.1:9/devtools/browser/x",
                           window=window or p.launch.window, proxy_id=p.proxy_id,
                           relay_port=40000 if p.proxy_id else None, started_at=utcnow())
        write_json(self.store.runtime_file(p.id), info.model_dump(mode="json"))
        self.started.append((p.id, window))
        return info

    def stop(self, ref: str, *, timeout: float = 20.0) -> bool:
        p = self.store.get_profile(ref)
        self.stopped.append(p.id)
        path = self.store.runtime_file(p.id)
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    def set_upstream(self, ref: str, proxy_id: str | None) -> None:
        self.upstreams.append((self.store.get_profile(ref).id, proxy_id))

    def open_url(self, ref: str, url: str) -> None:
        self.opened.append((self.store.get_profile(ref).id, url))

    def stop_all(self, timeout: float = 20.0) -> list[str]:
        names = []
        for p in self.store.list_profiles():
            if self.store.runtime_file(p.id).exists():
                self.stop(p.id)
                names.append(p.name)
        return names


async def fake_check(_endpoint: Any) -> ProxyCheck:
    return ProxyCheck(ok=True, ip="203.0.113.9", country="Germany", country_code="DE", city="Berlin", latency_ms=123,
                      provider="fake")


@pytest.fixture
def env(tmp_path: Path):
    store = Store(tmp_path / "pp-home")
    runtime = FakeRuntime(store)
    focused: list[Any] = []
    opened: list[Path] = []
    terminals: list[tuple[list[str], dict[str, str]]] = []
    locations = Locations(home=tmp_path / "userhome", appdata=tmp_path / "appdata", localappdata=tmp_path / "local",
                          platform="win32", claude_cli=None)
    app = create_app(store, token=TOKEN, port=PORT, runtime=runtime, locations=locations,
                     checks=Checks(proxy=fake_check, relay=fake_check), focuser=lambda pid: focused.append(pid) or True,
                     opener=opened.append, terminal=lambda argv, extra: terminals.append((argv, extra)),
                     poll_interval=0.1)
    return {"store": store, "app": app, "runtime": runtime, "focused": focused, "opened": opened, "tmp": tmp_path,
            "locations": locations, "terminals": terminals}


def client(app: Any, *, token: bool = True, **kw: Any) -> httpx.AsyncClient:
    headers = {TOKEN_HEADER: TOKEN} if token else {}
    headers.update(kw.pop("headers", {}))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, headers=headers,
                             follow_redirects=False, **kw)


class Recorder:
    """Records every response body so the test can assert that no secret was ever returned."""

    def __init__(self, c: httpx.AsyncClient) -> None:
        self.c = c
        self.bodies: list[str] = []

    async def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        resp = await self.c.request(method, url, **kw)
        self.bodies.append(resp.text)
        return resp

    def assert_never(self, *secrets: str) -> None:
        for body in self.bodies:
            for secret in secrets:
                assert secret not in body, f"secret leaked in a response: {body[:200]}"


# ---------------------------------------------------------------------- security


@pytest.mark.asyncio
async def test_auth_host_and_origin_checks(env) -> None:
    app = env["app"]
    async with client(app, token=False) as anon:
        assert (await anon.get("/api/overview")).status_code == 401
        assert (await anon.get("/api/overview", headers={TOKEN_HEADER: "wrong"})).status_code == 401
        assert (await anon.get("/api/events")).status_code == 401
        r = await anon.get("/api/profiles", headers={TOKEN_HEADER: TOKEN, "Host": "evil.example:47123"})
        assert r.status_code == 421 and r.json()["code"] == "bad_host"
        r = await anon.get("/api/profiles", headers={TOKEN_HEADER: TOKEN, "Host": f"localhost:{PORT}"})
        assert r.status_code == 200
        assert (await anon.get("/", headers={"Host": "127.0.0.1"})).status_code == 421  # wrong port

        # Page + static files load without a session; the page needs the API to be useful.
        index = await anon.get("/")
        assert index.status_code == 200 and "app.js" in index.text
        assert "default-src 'self'" in index.headers["content-security-policy"]
        assert "'unsafe-inline'" not in index.headers["content-security-policy"]
        assert index.headers["x-frame-options"] == "DENY"
        js = await anon.get("/static/app.js")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
        assert (await anon.get("/favicon.ico")).status_code == 200
        icon = await anon.get("/icon-32.png")
        assert icon.status_code == 200 and icon.content[1:4] == b"PNG" and icon.headers["content-type"] == "image/png"
        assert (await anon.get("/icon-33.png")).status_code == 404

        # Token exchange: a bad code gets the "expired" page; so does the master token itself (it never
        # travels in a URL); a launch code minted with the token header gets a cookie.
        bad = await anon.get("/?t=nope")
        assert bad.status_code == 401 and "expired" in bad.text
        assert (await anon.get(f"/?t={TOKEN}")).status_code == 401
        code = (await anon.post("/api/launch-code", headers={TOKEN_HEADER: TOKEN})).json()["code"]
        ok = await anon.get(f"/?t={code}")
        assert ok.status_code == 303 and ok.headers["location"] == "/"
        cookie = ok.headers["set-cookie"]
        assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower() and f"pp_session_{PORT}" in cookie
        assert TOKEN not in cookie
        sid = cookie.split(";")[0].split("=", 1)[1]
    async with client(app, token=False, cookies={f"pp_session_{PORT}": sid}) as browser:
        r = await browser.get("/api/overview")
        assert r.status_code == 200
        # Cookie sessions: unsafe methods need a matching Origin; cross-site fetches are refused.
        assert (await browser.post("/api/profiles", json={"name": "x"})).status_code == 403
        r = await browser.post("/api/profiles", json={"name": "x"}, headers={"Origin": "http://evil.test"})
        assert r.status_code == 403
        r = await browser.post("/api/profiles", json={"name": "x"}, headers={"Origin": f"http://127.0.0.1:{PORT + 1}"})
        assert r.status_code == 403
        r = await browser.get("/api/profiles", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        r = await browser.post("/api/profiles", json={"name": "from-ui"}, headers={**ORIGIN, "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 201
        # Launch codes need the token header, work once and give a session.
        assert (await browser.post("/api/launch-code", headers=ORIGIN)).status_code == 403
    async with client(app) as c:
        code = (await c.post("/api/launch-code")).json()["code"]
        async with client(app, token=False) as anon:
            assert (await anon.get(f"/?t={code}")).status_code == 303
            assert (await anon.get(f"/?t={code}")).status_code == 401  # single use
        # The header token alone works for scripts (no Origin needed).
        assert (await c.post("/api/profiles", json={"name": "from-script"})).status_code == 201
        r = await c.get("/api/does-not-exist")
        assert r.status_code == 404 and r.json()["code"] == "not_found"
        r = await c.put("/api/profiles")
        assert r.status_code == 405 and "error" in r.json()


def test_auth_class() -> None:
    auth = Auth("x" * 40)
    code = auth.new_code(ttl=0.05)
    time.sleep(0.1)
    assert auth.redeem(code) is None  # expired
    sid = auth.redeem(auth.new_code())
    assert sid and auth.check_session(sid) and not auth.check_session(sid + "x") and not auth.check_session(None)
    assert auth.check_token("x" * 40) and not auth.check_token("y" * 40) and not auth.check_token(None)
    assert auth.redeem("x" * 40) is None  # the master token is not a launch code
    with pytest.raises(ValueError):
        Auth("short")


# ---------------------------------------------------------------------- profiles, proxies, identities


@pytest.mark.asyncio
async def test_profile_crud_and_trash(env) -> None:
    store: Store = env["store"]
    async with client(env["app"]) as c:
        call = Recorder(c)
        r = await call("POST", "/api/profiles", json={
            "name": "shop-us", "proxy_url": f"socks5://alice:{PROXY_PASSWORD}@proxy.example.net:1080",
            "tags": ["shop", "us"], "notes": "demo", "window": "normal", "lang": "en-US",
            "start_url": "example.com/start", "browser": "auto"})
        assert r.status_code == 201, r.text
        p = r.json()
        assert p["name"] == "shop-us" and p["state"] == "stopped" and p["tags"] == ["shop", "us"]
        # A pasted proxy is named after its address (not after the profile), or as the user names it.
        assert p["proxy"]["name"] == "proxy.example.net:1080" and p["proxy"]["host"] == "proxy.example.net"
        assert p["launch"]["start_url"] == "https://example.com/start" and p["launch"]["lang"] == "en-US"
        assert store.proxy_endpoint(p["proxy_id"]).password == PROXY_PASSWORD

        assert (await call("POST", "/api/profiles", json={"name": "SHOP-US"})).status_code == 409
        assert (await call("POST", "/api/profiles", json={"name": ""})).status_code == 400
        r = await call("POST", "/api/profiles", json={"name": "bad", "browser": "C:/Windows/notepad.exe"})
        assert r.status_code == 400 and "CLI" in r.json()["error"]
        r = await call("POST", "/api/profiles", json={"name": "bad2", "proxy_url": "nonsense:pass:word@@"})
        assert r.status_code == 400
        r = await call("POST", "/api/profiles", json={"name": "bad3", "start_url": "file:///C:/x"})
        assert r.status_code == 400
        r = await call("POST", "/api/profiles", json={"name": "bad4", "window": "fullscreen"})
        assert r.status_code == 400
        r = await call("POST", "/api/profiles", json={"name": "bad5", "timezone": "Mars/Base"})
        assert r.status_code in (400, 422)

        listing = (await call("GET", "/api/profiles")).json()["profiles"]
        assert [x["name"] for x in listing] == ["shop-us"]
        assert (await call("GET", f"/api/profiles/{p['id']}")).json()["id"] == p["id"]
        assert (await call("GET", "/api/profiles/shop-us")).json()["id"] == p["id"]  # names resolve too
        assert (await call("GET", "/api/profiles/nope")).status_code == 404

        r = await call("PATCH", f"/api/profiles/{p['id']}", json={
            "name": "shop-usa", "tags": "a, b", "launch": {"window": "offscreen", "lang": "", "restore_session": False},
            "proxy_id": ""})
        assert r.status_code == 200, r.text
        updated = r.json()["profile"]
        assert updated["name"] == "shop-usa" and updated["tags"] == ["a", "b"] and updated["proxy_id"] is None
        assert updated["launch"]["window"] == "offscreen" and updated["launch"]["lang"] is None
        assert updated["launch"]["restore_session"] is False
        r = await call("PATCH", f"/api/profiles/{p['id']}", json={"extra_args": ["--renderer-cmd-prefix=calc"]})
        assert r.status_code == 200 and store.get_profile(p["id"]).launch.extra_args == []  # ignored, never applied

        clone = await call("POST", f"/api/profiles/{p['id']}/clone", json={"name": "shop-copy"})
        assert clone.status_code == 201 and clone.json()["name"] == "shop-copy"

        # Running profiles cannot be deleted; stopped ones go to the trash and come back.
        await call("POST", f"/api/profiles/{p['id']}/start", json={"window": "headless"})
        assert env["runtime"].started[-1] == (p["id"], "headless")
        r = await call("DELETE", f"/api/profiles/{p['id']}")
        assert r.status_code == 409
        await call("POST", f"/api/profiles/{p['id']}/stop")
        r = await call("DELETE", f"/api/profiles/{p['id']}")
        assert r.status_code == 200
        trash = (await call("GET", "/api/trash")).json()["trash"]
        assert [t["name"] for t in trash] == ["shop-usa"]
        r = await call("POST", f"/api/trash/{trash[0]['trash_id']}/restore")
        assert r.status_code == 200 and r.json()["name"] == "shop-usa"
        await call("DELETE", f"/api/profiles/{p['id']}")
        confirmed = [t["trash_id"] for t in (await call("GET", "/api/trash")).json()["trash"]]
        await call("DELETE", f"/api/profiles/{clone.json()['id']}")  # trashed after the user confirmed
        r = await call("DELETE", "/api/trash", json={"ids": confirmed + ["not-in-trash"]})
        assert r.json()["removed"] == 1  # only what the confirmation showed
        assert [t["name"] for t in (await call("GET", "/api/trash")).json()["trash"]] == ["shop-copy"]
        assert (await call("DELETE", "/api/trash", json={"ids": "all"})).status_code == 400
        assert (await call("DELETE", "/api/trash")).json()["removed"] == 1
        assert (await call("GET", "/api/trash")).json()["trash"] == []
        assert (await call("POST", "/api/trash/..%2F..%2Fx/restore")).status_code in (400, 404)
        call.assert_never(PROXY_PASSWORD)
    events = ActivityLog(store.root).tail()
    assert {"create profile", "update profile", "start", "stop", "delete profile", "restore profile"} <= {e.tool for e in events}
    assert all(e.source == "ui" for e in events)


@pytest.mark.asyncio
async def test_start_stop_focus_tabs_screenshot(env) -> None:
    store: Store = env["store"]
    p = store.create_profile("live")
    async with client(env["app"]) as c:
        assert (await c.post(f"/api/profiles/{p.id}/focus")).status_code == 409  # not running
        r = await c.post(f"/api/profiles/{p.id}/start", json={})
        assert r.status_code == 200 and r.json()["state"] == "running"
        assert r.json()["runtime"]["window"] == "normal" and r.json()["runtime"]["cdp_http_url"]
        assert "control_token" not in r.text
        r = await c.post(f"/api/profiles/{p.id}/focus")
        assert r.status_code == 200 and env["focused"] == [os.getpid()]
        # No DevTools behind the fake runtime: a clean 204 / 502, never a 500.
        r = await c.get(f"/api/profiles/{p.id}/screenshot")
        assert r.status_code == 204 and r.headers["x-thumb-state"] == "error"
        assert (await c.get(f"/api/profiles/{p.id}/screenshot")).status_code == 204  # throttled (cached)
        r = await c.get(f"/api/profiles/{p.id}/tabs")
        assert r.status_code == 502 and r.json()["code"] == "devtools"
        await c.post(f"/api/profiles/{p.id}/stop")
        assert (await c.get(f"/api/profiles/{p.id}")).json()["state"] == "stopped"
        r = await c.get(f"/api/profiles/{p.id}/screenshot")
        assert r.status_code == 204 and r.headers["x-thumb-state"] == "stopped"
        await c.post(f"/api/profiles/{p.id}/start", json={"window": "headless"})
        r = await c.post(f"/api/profiles/{p.id}/focus")
        assert r.status_code == 409 and r.json()["code"] == "headless"
        # Open a page: a running profile gets a new tab, a stopped one starts on it.
        await c.post(f"/api/profiles/{p.id}/stop")
        r = await c.post(f"/api/profiles/{p.id}/open", json={"url": "example.com/x"})
        assert r.status_code == 200 and r.json()["started"] is True
        r = await c.post(f"/api/profiles/{p.id}/open", json={"url": "https://example.org/"})
        assert r.json()["started"] is False and env["runtime"].opened == [(p.id, "https://example.org/")]
        assert (await c.post(f"/api/profiles/{p.id}/open", json={"url": "javascript:alert(1)"})).status_code == 400
        assert (await c.post(f"/api/profiles/{p.id}/open", json={"url": "file:///C:/secret.txt"})).status_code == 400
        assert (await c.post(f"/api/profiles/{p.id}/open", json={})).status_code == 400
        r = await c.post("/api/stop-all")
        assert r.json()["stopped"] == ["live"]
        # A crash is reported until the next start.
        await c.post(f"/api/profiles/{p.id}/stop")
        write_json(store.profile_dir(p.id) / "last_exit.json", {"crashed": True, "crash": "access violation (0xC0000005)"})
        view = (await c.get(f"/api/profiles/{p.id}")).json()
        assert view["state"] == "crashed" and "0xC0000005" in view["crash"]
        assert (await c.post("/api/reveal", json={"what": "profile", "id": p.id})).status_code == 200
        assert env["opened"] == [store.profile_dir(p.id)]
        assert (await c.post("/api/reveal", json={"what": "C:/Windows"})).status_code == 400


@pytest.mark.asyncio
async def test_proxies_import_edit_test_and_no_password_leaks(env) -> None:
    store: Store = env["store"]
    async with client(env["app"]) as c:
        call = Recorder(c)
        text = (f"socks5://bob:{PROXY_PASSWORD}@de.proxy.example.net:1080  # Berlin\n"
                f"us.proxy.example.net:8000:carol:{PROXY_PASSWORD}2\n"
                f"# a comment\nthis-is-not:valid:{PROXY_PASSWORD}3:at all:extra:bits@@\n203.0.113.5:3128")
        preview = (await call("POST", "/api/proxies/parse", json={"text": text, "scheme": "http"})).json()
        assert preview["valid"] == 3 and preview["invalid"] == 1
        assert preview["lines"][0]["name"] == "Berlin" and preview["lines"][0]["username"] == "b•••"
        assert preview["lines"][1]["has_password"] is True
        r = await call("POST", "/api/proxies", json={"text": text, "scheme": "http", "tags": ["de"]})
        assert r.status_code == 201, r.text
        result = r.json()
        assert result["created"] == 3 and len(result["errors"]) == 1 and result["errors"][0].startswith("line 4")
        again = (await call("POST", "/api/proxies", json={"text": text})).json()
        assert again["created"] == 0 and again["existing"] == 3
        nothing = await call("POST", "/api/proxies", json={"text": "garbage line"})
        assert nothing.status_code == 400 and nothing.json()["code"] == "proxy_format"

        proxies = (await call("GET", "/api/proxies")).json()["proxies"]
        berlin = next(p for p in proxies if p["name"] == "Berlin")
        assert berlin["has_password"] and berlin["username"] == "b•••" and berlin["tags"] == ["de"]
        assert "bob" not in berlin["url"] and berlin["url"].startswith("socks5://b•••:***@")

        # Password is write-only: replace it, then change the host and keep it, then clear it.
        r = await call("PATCH", f"/api/proxies/{berlin['id']}", json={"password": "N3w-pass-123"})
        assert r.status_code == 200 and store.proxy_endpoint(berlin["id"]).password == "N3w-pass-123"
        r = await call("PATCH", f"/api/proxies/{berlin['id']}", json={"host": "de2.proxy.example.net", "port": 1081})
        ep = store.proxy_endpoint(berlin["id"])
        assert (ep.host, ep.port, ep.username, ep.password) == ("de2.proxy.example.net", 1081, "bob", "N3w-pass-123")
        r = await call("PATCH", f"/api/proxies/{berlin['id']}", json={"clear_password": True, "name": "Berlin 2",
                                                                       "notes": "n"})
        assert store.proxy_endpoint(berlin["id"]).password is None and r.json()["proxy"]["name"] == "Berlin 2"
        r = await call("PATCH", f"/api/proxies/{berlin['id']}", json={"username": "", "password": "x"})
        assert r.status_code == 400  # a password needs a username
        r = await call("PATCH", f"/api/proxies/{berlin['id']}", json={"scheme": "ftp"})
        assert r.status_code == 400

        # A running profile that uses an edited proxy is switched live.
        user = store.create_profile("uses-it", proxy_id=berlin["id"])
        env["runtime"].start(user.id)
        await call("PATCH", f"/api/proxies/{berlin['id']}", json={"port": 1082})
        assert env["runtime"].upstreams[-1] == (user.id, berlin["id"])

        r = await call("POST", f"/api/proxies/{berlin['id']}/test")
        assert r.json()["proxy"]["last_check"]["ip"] == "203.0.113.9"
        assert r.json()["proxy"]["history"][-1]["ms"] == 123
        r = await call("POST", "/api/proxies/test", json={})
        assert r.status_code == 202 and r.json()["total"] == 3
        for _ in range(50):
            await asyncio.sleep(0.05)
            if all((p["last_check"] or {}).get("ok") for p in (await c.get("/api/proxies")).json()["proxies"]):
                break
        assert all(p["last_check"]["ok"] for p in (await c.get("/api/proxies")).json()["proxies"])

        r = await call("DELETE", f"/api/proxies/{berlin['id']}")
        assert r.status_code == 409  # used by a profile
        r = await call("DELETE", f"/api/proxies/{berlin['id']}?force=1")
        assert r.status_code == 200 and r.json()["unbound"] == ["uses-it"]
        assert berlin["id"] not in (read_json(store.root / "proxy_history.json") or {}).get("proxies", {})
        call.assert_never(PROXY_PASSWORD, "N3w-pass-123", "carol:")


@pytest.mark.asyncio
async def test_identities_secrets_are_write_only(env) -> None:
    store: Store = env["store"]
    async with client(env["app"]) as c:
        call = Recorder(c)
        r = await call("POST", "/api/identities", json={"name": "Alex", "values": {"first_name": "Alex", "zip": "62701",
                                                                                   "email": "alex@example.com"}})
        assert r.status_code == 201, r.text
        ident = r.json()
        assert ident["values"] == {"email": "alex@example.com", "first_name": "Alex", "postal_code": "62701"}
        r = await call("POST", "/api/identities", json={"name": "Bad", "values": {"card_number": CARD}})
        assert r.status_code == 400 and r.json()["code"] == "sensitive"
        r = await call("POST", "/api/identities", json={"name": "Bad2", "values": {"company": CARD}})
        assert r.status_code == 400  # looks like a card number: refused, never echoed
        r = await call("PATCH", f"/api/identities/{ident['id']}", json={"values": {"ssn": SSN}})
        assert r.status_code == 400

        r = await call("PUT", f"/api/identities/{ident['id']}/secret/card_number", json={"value": CARD})
        assert r.status_code == 200 and r.json()["sensitive"]["card_number"] == "visa •••• 4242"
        assert r.json()["card"] == {"brand": "visa", "last4": "4242"}
        r = await call("PUT", f"/api/identities/{ident['id']}/secret/ssn", json={"value": SSN})
        assert r.json()["sensitive"]["ssn"] == "•••-••-6789"
        r = await call("PUT", f"/api/identities/{ident['id']}/secret/password", json={"value": "hunter2-Secret!"})
        assert r.json()["sensitive"]["password"] == "set"
        r = await call("PUT", f"/api/identities/{ident['id']}/secret/card_number", json={"value": "4242424242424241"})
        assert r.status_code == 400 and "Luhn" in r.json()["error"]
        r = await call("PUT", f"/api/identities/{ident['id']}/secret/email", json={"value": "x@example.com"})
        assert r.status_code == 400
        assert IdentityStore(store).fill_values(ident["id"], include_sensitive=True)["card_number"] == CARD

        r = await call("POST", f"/api/identities/{ident['id']}/origins", json={"origin": "Shop.Example.com/checkout"})
        assert r.json()["allowed_origins"] == ["https://shop.example.com"]
        r = await call("DELETE", f"/api/identities/{ident['id']}/origins", json={"origin": "https://shop.example.com"})
        assert r.json()["allowed_origins"] == []

        r = await call("PATCH", f"/api/identities/{ident['id']}", json={"name": "Alex S", "values": {"postal_code": None,
                                                                                                    "city": "Springfield"}})
        assert r.json()["values"] == {"city": "Springfield", "email": "alex@example.com", "first_name": "Alex"}
        await call("DELETE", f"/api/identities/{ident['id']}/secret/ssn")
        assert "ssn" not in (await call("GET", f"/api/identities/{ident['id']}")).json()["sensitive"]

        p = store.create_profile("linked", identity_id=ident["id"])
        listing = (await call("GET", "/api/identities")).json()["identities"]
        assert listing[0]["used_by"] == [{"id": p.id, "name": "linked"}]
        r = await call("DELETE", f"/api/identities/{ident['id']}")
        assert r.json()["unlinked"] == [p.id] and store.get_profile(p.id).identity_id is None
        await call("GET", "/api/overview")
        call.assert_never(CARD, "6789-", "123-45", "hunter2-Secret!")


# ---------------------------------------------------------------------- control, help, activity, events


@pytest.mark.asyncio
async def test_pause_resume_and_help_round_trip(env) -> None:
    store: Store = env["store"]
    p = store.create_profile("shop")
    control = ControlStore(store)
    async with client(env["app"]) as c:
        r = await c.post(f"/api/profiles/{p.id}/pause", json={"note": "logging in"})
        assert r.status_code == 200 and r.json()["profile"]["control"]["paused"] is True
        assert control.paused(p.id).note == "logging in"
        r = await c.post(f"/api/profiles/{p.id}/resume")
        assert r.json()["profile"]["control"]["paused"] is False and control.paused(p.id) is None

        req = control.request_help(p.id, "Solve the CAPTCHA", "captcha", requested_by="claude-ai")
        help_ = (await c.get("/api/help")).json()["help"]
        assert [h["id"] for h in help_] == [req.id] and help_[0]["profile_name"] == "shop"
        view = (await c.get(f"/api/profiles/{p.id}")).json()
        assert view["control"]["help"][0]["message"] == "Solve the CAPTCHA" and view["control"]["pause"]["by"] == "help"
        r = await c.post(f"/api/help/{p.id}/{req.id}", json={"status": "bogus"})
        assert r.status_code == 400
        r = await c.post(f"/api/help/{p.id}/{req.id}", json={"status": "done", "note": "solved"})
        assert r.status_code == 200 and r.json()["request"]["status"] == "done"
        assert control.paused(p.id) is None and (await c.get("/api/help")).json()["help"] == []
        assert (await c.post(f"/api/help/{p.id}/nope", json={})).status_code == 404

        # Hand back closes open requests too.
        control.request_help(p.id, "Type the SMS code", "verification")
        r = await c.post(f"/api/profiles/{p.id}/resume")
        assert len(r.json()["resolved"]) == 1 and control.help_requests() == []


@pytest.mark.asyncio
async def test_activity_endpoint_filters(env) -> None:
    store: Store = env["store"]
    a, b = store.create_profile("a"), store.create_profile("b")
    log = ActivityLog(store.root)
    for i in range(6):
        log.append(ActivityEvent(profile_id=(a if i % 2 else b).id, profile_name="x", tool=f"browser_{i}", ok=i != 3,
                                 summary=f"step {i}"))
    async with client(env["app"]) as c:
        events = (await c.get("/api/activity?limit=4")).json()["events"]
        assert [e["tool"] for e in events] == ["browser_5", "browser_4", "browser_3", "browser_2"]  # newest first
        assert {e["profile_id"] for e in (await c.get("/api/activity?profile=a")).json()["events"]} == {a.id}
        assert [e["tool"] for e in (await c.get("/api/activity?status=errors")).json()["events"]] == ["browser_3"]
        assert (await c.get("/api/activity?limit=x")).status_code == 400


@pytest.mark.asyncio
async def test_sse_emits_activity_and_profile_events(env) -> None:
    store: Store = env["store"]
    p = store.create_profile("watched")
    async with client(env["app"]) as c:
        async def append_later() -> None:
            await asyncio.sleep(0.5)
            ActivityLog(store.root).append(ActivityEvent(profile_id=p.id, profile_name="watched", tool="browser_click",
                                                         summary="Clicked 'Buy' (e3)"))
            await asyncio.sleep(0.4)
            ControlStore(store).request_help(p.id, "Log in please", "login")

        task = asyncio.create_task(append_later())
        r = await c.get("/api/events?max_events=4&timeout=8")
        await task
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = r.text
        assert "event: ready" in body
        assert "event: activity" in body and "Clicked 'Buy' (e3)" in body
        assert "event: help" in body and "Log in please" in body
        assert "event: profile" in body
        # Every data line is valid JSON.
        for line in body.splitlines():
            if line.startswith("data: "):
                json.loads(line[6:])
        r = await c.get("/api/events?max_events=x")
        assert r.status_code == 400
    await env["app"].api.aclose()


# ---------------------------------------------------------------------- settings, clients, chatgpt, overview


@pytest.mark.asyncio
async def test_settings_and_shardx_token(env) -> None:
    store: Store = env["store"]
    async with client(env["app"]) as c:
        call = Recorder(c)
        s = (await call("GET", "/api/settings")).json()
        assert s["default_window"] == "normal" and s["shardx"]["token_set"] is False
        r = await call("PATCH", "/api/settings", json={"default_window": "offscreen", "max_running": 5,
                                                       "escape_client_job": True, "shardx": {"enabled": True}})
        assert r.status_code == 200 and r.json()["max_running"] == 5 and r.json()["shardx"]["enabled"] is True
        cfg = store.load_config()
        assert cfg.default_window == "offscreen" and cfg.escape_client_job is True
        assert (await call("PATCH", "/api/settings", json={"max_running": -1})).status_code == 400
        assert (await call("PATCH", "/api/settings", json={"max_running": True})).status_code == 400
        assert (await call("PATCH", "/api/settings", json={"browser_path": "C:/Windows/System32/calc.exe"})).status_code == 400
        r = await call("PATCH", "/api/settings", json={"shardx": {"base_url": "http://evil.example:40325"}})
        assert r.status_code == 400
        r = await call("PATCH", "/api/settings", json={"shardx": {"base_url": "http://127.0.0.1:40999/"}})
        assert r.json()["shardx"]["base_url"] == "http://127.0.0.1:40999"
        assert (await call("PUT", "/api/settings/shardx-token", json={"token": "not a jwt"})).status_code == 400
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzaGFyZHgtYXBpIn0.c2lnbmF0dXJlLXZhbHVl"
        r = await call("PUT", "/api/settings/shardx-token", json={"token": jwt})
        assert r.status_code == 200 and r.json()["shardx"]["token_set"] is True
        r = await call("DELETE", "/api/settings/shardx-token")
        assert r.json()["shardx"]["token_set"] is False
        browsers = (await call("GET", "/api/browsers")).json()["browsers"]
        assert isinstance(browsers, list)
        call.assert_never(jwt)


@pytest.mark.asyncio
async def test_clients_use_injected_locations(env) -> None:
    tmp: Path = env["tmp"]
    async with client(env["app"]) as c:
        clients = {x["client"]: x for x in (await c.get("/api/clients")).json()["clients"]}
        assert set(clients) == {"claude-desktop", "claude-code", "codex", "cursor"}
        assert clients["claude-desktop"]["registered"] is False
        assert clients["claude-code"]["registered"] is None  # no CLI in the injected locations
        assert str(tmp / "appdata") in clients["claude-desktop"]["config_paths"][0]
        assert "mcpServers" in clients["claude-desktop"]["snippet"]
        r = await c.post("/api/clients/claude-desktop/register")
        assert r.status_code == 200, r.text
        config = json.loads((tmp / "appdata" / "Claude" / "claude_desktop_config.json").read_text("utf-8"))
        assert config["mcpServers"]["profilepilot"]["args"] == ["-m", "profilepilot", "serve"]
        assert not config["mcpServers"]["profilepilot"]["command"].lower().endswith("pythonw.exe")
        assert {x["client"]: x for x in r.json()["clients"]}["claude-desktop"]["registered"] is True
        assert r.json()["ok"] is True and r.json()["summary"].startswith("Added to Claude Desktop.")
        # Without the claude CLI nothing is registered: never a success, but the command to run.
        r = await c.post("/api/clients/claude-code/register")
        body = r.json()
        assert r.status_code == 200 and body["ok"] is False and body["manual"] is True
        assert "claude mcp add" in body["command"] and "profilepilot" in body["command"] and "report" not in body
        assert {x["client"]: x for x in body["clients"]}["claude-code"]["registered"] is None
        last = ActivityLog(env["store"].root).tail(1)[0]
        assert last.summary == "Claude Code: setup command shown (CLI not found)." and not last.ok
        r = await c.post("/api/clients/cursor/register")
        assert r.json()["summary"] == "Added to Cursor. Restart Cursor to use it." and "mcp.json" in r.json()["report"]
        assert (tmp / "userhome" / ".cursor" / "mcp.json").exists()
        r = await c.post("/api/clients/claude-desktop/unregister")
        assert {x["client"]: x for x in r.json()["clients"]}["claude-desktop"]["registered"] is False
        assert (await c.post("/api/clients/notepad/register")).status_code == 404
        assert (await c.post("/api/clients/cursor/explode")).status_code == 404


@pytest.mark.asyncio
async def test_chatgpt_status_and_overview(env) -> None:
    store: Store = env["store"]
    async with client(env["app"]) as c:
        s = (await c.get("/api/chatgpt")).json()
        assert s["running"] is False and s["commands"]["connect"] == "profilepilot connect chatgpt"
        assert s["pairing_code"] is None and s["connections"] == []
        write_json(store.root / "chatgpt.json", {"url": "https://demo.trycloudflare.com", "tunnel": "cloudflared",
                                                 "pid": os.getpid(), "started_at": "2026-10-09T00:00:00+00:00"})
        s = (await c.get("/api/chatgpt")).json()
        assert s["running"] is True and s["mcp_url"] == "https://demo.trycloudflare.com/mcp"
        assert s["method"] == "Cloudflare quick tunnel"
        assert s["pairing_code"] and len(s["pairing_code"].replace("-", "")) == 8
        write_json(store.root / "chatgpt.json", {"url": "https://demo.trycloudflare.com/mcp", "pid": 99999999})
        assert (await c.get("/api/chatgpt")).json()["running"] is False
        r = await c.post("/api/chatgpt/stop", json={"revoke": True})
        assert r.status_code == 200 and "Signed out 0" in r.json()["report"]

        store.create_profile("one")
        o = (await c.get("/api/overview")).json()
        assert {"version", "data_root", "profiles", "proxies", "identities", "help", "browsers", "settings",
                "chatgpt", "running", "trash_count"} <= set(o)
        assert o["profiles"][0]["name"] == "one" and o["running"] == 0
        meta = (await c.get("/api/meta")).json()
        keys = {f["key"]: f for f in meta["fields"]}
        assert keys["card_number"]["sensitive"] and keys["card_number"]["group"] == "card"
        assert keys["ssn"]["group"] == "sensitive" and keys["email"]["group"] == "personal"


# ---------------------------------------------------------------------- UX review regressions


def _drain(queue: asyncio.Queue) -> list[tuple[str, Any]]:
    out = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            out.append(item)
    return out


@pytest.mark.parametrize(("error", "reason"), [
    ("upstream proxy <upstream proxy> failed: ProxyTimeoutError: Proxy connection timed out: 8.0. Details: every "
     "IP-check service failed - ipwho.is: timed out after 8s", "The proxy didn't answer within 8 s."),
    ("upstream proxy http://jp.proxy.example.net:3128 failed: ProxyConnectionError: Couldn't connect to proxy "
     "<upstream proxy> [Errno 11001] getaddrinfo failed. Details: ipwho.is: proxy error (502 Bad Gateway)",
     "Can't find the server jp.proxy.example.net – check the address."),
    ("ipwho.is: proxy error (407 Proxy Authentication Required)", "Wrong proxy username or password."),
    ("upstream proxy jp.proxy.example.net:3128 refused the connection", "The proxy refused the connection."),
    ("every IP-check service failed - ipwho.is: HTTP 503; ipapi.co: HTTP 429",
     "Couldn't reach the internet through this proxy."),
])
def test_friendly_proxy_errors(error: str, reason: str) -> None:
    from profilepilot.ui.api import check_view, friendly_proxy_error

    assert friendly_proxy_error(error, "jp.proxy.example.net") == reason
    view = check_view(ProxyCheck(ok=False, error=error), host="jp.proxy.example.net")
    assert view["reason"] == reason and view["error"].startswith(error[:20])  # the raw text stays as details
    assert check_view(ProxyCheck(ok=True, ip="203.0.113.1"))["reason"] is None


@pytest.mark.asyncio
async def test_proxy_test_jobs_announce_their_rows_and_can_be_cancelled(env) -> None:
    store: Store = env["store"]
    for i in range(3):
        store.add_proxy(f"203.0.113.{i + 1}:8080")
    release = asyncio.Event()

    async def slow_check(_endpoint: Any) -> ProxyCheck:
        await release.wait()
        return ProxyCheck(ok=False, error="upstream proxy <upstream proxy> failed: ProxyTimeoutError: Proxy connection "
                                          "timed out: 8.0")

    api = env["app"].api
    api.checks = Checks(proxy=slow_check, relay=fake_check)
    queue = api.hub.subscribe()
    try:
        async with client(env["app"]) as c:
            r = await c.post("/api/proxies/test", json={})
            job = r.json()
            assert r.status_code == 202 and sorted(job["ids"]) == sorted(p.id for p in store.list_proxies())
            started = [d for n, d in _drain(queue) if n == "proxy-test"]
            assert started and started[0]["started"] is True and started[0]["ids"] == job["ids"]
            again = (await c.post("/api/proxies/test", json={})).json()
            assert again["already_running"] is True and again["ids"] == job["ids"]
            # Cancel: the run stops and every window is told (so the "Testing" rows clear).
            r = await c.delete("/api/proxies/test")
            assert r.status_code == 200 and r.json()["cancelled"] is True
            finished = [d for n, d in _drain(queue) if n == "proxy-test" and d.get("finished")]
            assert finished and finished[-1]["cancelled"] is True
            assert (await c.delete("/api/proxies/test")).json()["cancelled"] is False
            assert ActivityLog(store.root).tail(1)[0].summary.startswith("Stopped the proxy test after 0 of 3 proxies")
            # A finished run reports per proxy, with a plain-language reason and the raw details.
            release.set()
            r = await c.post("/api/proxies/test", json={"ids": [job["ids"][0]]})
            assert r.json()["ids"] == [job["ids"][0]]
            for _ in range(100):
                if any(n == "proxy-test" and d.get("finished") for n, d in _drain(queue)):
                    break
                await asyncio.sleep(0.05)
            tested = next(p for p in (await c.get("/api/proxies")).json()["proxies"] if p["id"] == job["ids"][0])
            assert tested["last_check"]["reason"] == "The proxy didn't answer within 8 s."
            assert "ProxyTimeoutError" in tested["last_check"]["error"]
            assert ActivityLog(store.root).tail(1)[0].summary == "Tested 1 proxy: 0 working, 1 failed."
    finally:
        api.hub.unsubscribe(queue)
        await api.aclose()


@pytest.mark.asyncio
async def test_sensitive_autofill_sites_must_be_https(env) -> None:
    store: Store = env["store"]
    ident = IdentityStore(store).create("Alex", {"first_name": "Alex"})
    IdentityStore(store).allow_origin(ident.id, "http://legacy.example.com")  # e.g. added with an older version
    async with client(env["app"]) as c:
        r = await c.post(f"/api/identities/{ident.id}/origins", json={"origin": "http://shop.example.com"})
        assert r.status_code == 400 and r.json()["code"] == "insecure_origin"
        assert "https://" in r.json()["error"]
        r = await c.post(f"/api/identities/{ident.id}/origins", json={"origin": "http://localhost:8000"})
        assert r.status_code == 200
        r = await c.post(f"/api/identities/{ident.id}/origins", json={"origin": "shop.example.com"})
        view = r.json()
        assert "https://shop.example.com" in view["allowed_origins"]
        assert view["insecure_origins"] == ["http://legacy.example.com"]  # shown as "Not secure"
        r = await c.request("DELETE", f"/api/identities/{ident.id}/origins", json={"origin": "http://legacy.example.com"})
        assert r.status_code == 200 and r.json()["insecure_origins"] == []


@pytest.mark.asyncio
async def test_overview_detects_clients_in_the_background(env) -> None:
    api = env["app"].api
    queue = api.hub.subscribe()
    try:
        async with client(env["app"]) as c:
            first = (await c.get("/api/overview")).json()
            assert first["clients"] is None  # never blocks the first paint
            for _ in range(100):
                events = [d for n, d in _drain(queue) if n == "clients"]
                if events:
                    break
                await asyncio.sleep(0.05)
            assert {x["client"] for x in events[0]["clients"]} == {"claude-desktop", "claude-code", "codex", "cursor"}
            assert (await c.get("/api/overview")).json()["clients"] is not None
    finally:
        api.hub.unsubscribe(queue)
        await api.aclose()


@pytest.mark.asyncio
async def test_start_connection_opens_a_terminal_and_reveal_log(env) -> None:
    store: Store = env["store"]
    p = store.create_profile("logged")
    (store.profile_dir(p.id) / "host.log").write_text("host log", encoding="utf-8")
    async with client(env["app"]) as c:
        r = await c.post("/api/chatgpt/start")
        assert r.status_code == 200 and r.json()["started"] is True
        argv, extra = env["terminals"][-1]
        assert argv[1:] == ["-m", "profilepilot", "connect", "chatgpt"] and extra == {"PROFILEPILOT_HOME": str(store.root)}
        r = await c.post("/api/reveal", json={"what": "log", "id": p.id})
        assert r.status_code == 200 and env["opened"][-1] == store.profile_dir(p.id) / "host.log"

    def no_terminal(argv: list[str], extra: dict[str, str]) -> None:
        raise OSError("none")

    env["app"].api.terminal = no_terminal
    async with client(env["app"]) as c:
        r = await c.post("/api/chatgpt/start")
        assert r.status_code == 501 and "profilepilot connect chatgpt" in r.json()["error"]


@pytest.mark.asyncio
async def test_starting_on_an_untested_proxy_checks_it_and_error_pages_are_named(env, monkeypatch) -> None:
    from profilepilot.ui import cdp

    store: Store = env["store"]
    rec = store.add_proxy("socks5://de.proxy.example.net:1080")
    p = store.create_profile("fresh", proxy_id=rec.id)
    async with client(env["app"]) as c:
        assert (await c.post(f"/api/profiles/{p.id}/start", json={})).status_code == 200
        for _ in range(100):
            if store.get_proxy(rec.id).last_check is not None:
                break
            await asyncio.sleep(0.05)
        assert store.get_proxy(rec.id).last_check.ok is True  # tested in the background (fake check)

        async def error_page(_port: int) -> list[dict[str, Any]]:
            return [{"id": "T1", "url": "chrome-error://chromewebdata/", "title": "de.proxy.example.net"}]

        monkeypatch.setattr(cdp, "page_targets", error_page)
        env["app"].api._thumbs.clear()
        r = await c.get(f"/api/profiles/{p.id}/screenshot")
        assert r.status_code == 204 and r.headers["x-thumb-state"] == "page-error"
    await env["app"].api.aclose()


# ---------------------------------------------------------------------- cookies

COOKIE_SECRET = "c00kie-S3cret-value"
SOON = int(time.time()) + 86400 * 10
CHIPS_KEY = {"topLevelSite": "https://top.example", "hasCrossSiteAncestor": False}


def jar_cookie(name: str, domain: str = "example.com", path: str = "/", **kw: Any) -> dict[str, Any]:
    return {"name": name, "value": kw.pop("value", f"{name}-{COOKIE_SECRET}"), "domain": domain, "path": path,
            "expires": kw.pop("expires", SOON), "secure": kw.pop("secure", True), "httpOnly": kw.pop("httpOnly", False),
            **kw}


class FakeJar:
    """The live jar behind the cookie routes, in memory: replaces the browser-facing functions of
    :mod:`profilepilot.browser.cookiejar` (the routes' parsing, validation, keys and views stay real).
    Chrome's own refusal is played by domain cookies for the public suffix ``.co.uk``."""

    def __init__(self, monkeypatch, cookies: list[dict[str, Any]] = ()) -> None:  # type: ignore[assignment]
        from profilepilot.browser import cookiejar

        self.cj = cookiejar
        self.jar: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        for c in cookies:
            self._put(cookiejar.validate_cookie(c))
        for name in ("list_cookies", "save_cookie", "delete_cookies", "clear_cookies", "import_cookies"):
            monkeypatch.setattr(cookiejar, name, getattr(self, name))

    def _put(self, valid: dict[str, Any]) -> dict[str, Any]:
        c = self.cj.normalize_cookie(valid)
        self.jar[self.cj.cookie_key(c)] = c
        return c

    @staticmethod
    def _refused(valid: dict[str, Any]) -> bool:
        return valid["domain"] == ".co.uk"

    def values(self) -> set[tuple[str, str, str]]:
        return {(c["name"], c["domain"], c["value"]) for c in self.jar.values()}

    async def list_cookies(self, ws_url: str, *, cdp: Any = None) -> list[dict[str, Any]]:
        assert ws_url.startswith("ws://127.0.0.1:9/devtools/browser/")
        self.calls.append("list")
        return sorted(self.jar.values(), key=lambda c: (c["domain"].lstrip("."), c["name"], c["path"]))

    async def save_cookie(self, ws_url: str, cookie: dict[str, Any], *, replace: str | None = None,
                          overwrite: bool = False, cdp: Any = None) -> dict[str, Any]:
        self.calls.append("save")
        base = None
        if replace is not None:
            base = self.jar.get(replace)
            if base is None:
                from profilepilot.errors import NotFoundError

                raise NotFoundError("That cookie no longer exists (it expired or was deleted). Refresh the list.")
        valid = self.cj.validate_cookie(self.cj.merge_cookie(base, cookie))
        if replace is not None and not overwrite and self.cj.cookie_key(valid) != replace                 and self.cj.cookie_key(valid) in self.jar:
            label = self.cj.cookie_label(valid)
            raise self.cj.CookieExistsError(f"There already is a cookie {label}.", existing=label)
        if self._refused(valid):
            label = self.cj.cookie_label(valid)
            raise self.cj.CookieRefusedError(f"Chrome did not store this cookie: {label}.", refused=[label])
        saved = self._put(valid)
        if replace is not None and self.cj.cookie_key(saved) != replace:
            del self.jar[replace]
        return saved

    async def delete_cookies(self, ws_url: str, keys: Any, *, cdp: Any = None) -> int:
        self.calls.append("delete")
        return sum(self.jar.pop(k, None) is not None for k in set(keys))

    async def clear_cookies(self, ws_url: str, *, domain: str | None = None, cdp: Any = None) -> int:
        from profilepilot.automation.cookies import domain_matches

        self.calls.append("clear")
        site = self.cj.filter_domain(domain) if domain is not None else None
        gone = [k for k, c in self.jar.items() if site is None or domain_matches(c["domain"], site)]
        for k in gone:
            del self.jar[k]
        return len(gone)

    async def import_cookies(self, ws_url: str, cookies: Any, *, mode: str = "merge", cdp: Any = None) -> Any:
        from profilepilot.automation.cookies import domain_matches

        self.calls.append(f"import:{mode}")
        valid = [self.cj.validate_cookie(c) for c in cookies]
        refused = [c for c in valid if self._refused(c)]
        stored = [self._put(c) for c in valid if not self._refused(c)]
        removed = 0
        if mode != "merge" and stored:
            keep = {self.cj.cookie_key(c) for c in stored}
            sites = {c["domain"].lstrip(".") for c in valid}
            stale = [k for k, c in self.jar.items() if k not in keep
                     and (mode == "replace_all" or any(domain_matches(c["domain"], s) for s in sites))]
            for k in stale:
                del self.jar[k]
            removed = len(stale)
        return self.cj.ImportResult(imported=len(stored), refused=[self.cj.cookie_label(c) for c in refused],
                                    removed=removed)


async def cookie_activity(c: httpx.AsyncClient, profile_id: str) -> list[dict[str, Any]]:
    events = (await c.get(f"/api/activity?profile={profile_id}")).json()["events"]
    return [e for e in events if "cookie" in e["tool"]]


@pytest.mark.asyncio
async def test_cookie_routes_need_the_token_and_a_running_profile(env, monkeypatch) -> None:
    jar = FakeJar(monkeypatch, [jar_cookie("sid")])
    p = env["store"].create_profile("jar")
    base = f"/api/profiles/{p.id}/cookies"
    async with client(env["app"], token=False) as anon:
        assert (await anon.get(base)).status_code == 401
        assert (await anon.post("/api/cookies/parse", json={"text": "[]"})).status_code == 401
        assert (await anon.get(f"{base}/export")).status_code == 401
    async with client(env["app"]) as c:
        code = (await c.post("/api/launch-code")).json()["code"]
        async with client(env["app"], token=False) as anon:
            sid = (await anon.get(f"/?t={code}")).headers["set-cookie"].split(";")[0].split("=", 1)[1]
        # Not running: 409 not_running (the UI offers "Start in background"); the browser is never asked.
        for method, url, body in [("GET", base, None), ("POST", base, {"cookie": jar_cookie("x")}),
                                  ("DELETE", base, {"all": True}), ("POST", f"{base}/import", {"text": "[]"}),
                                  ("GET", f"{base}/export", None), ("POST", f"{base}/export", {"format": "json"})]:
            r = await c.request(method, url, json=body)
            assert r.status_code == 409 and r.json()["code"] == "not_running", (method, url, r.text)
        assert jar.calls == []
        assert (await c.post("/api/cookies/parse", json={"text": "[]"})).status_code == 200  # no profile needed
        assert (await c.post(f"/api/profiles/{p.id}/start", json={})).status_code == 200
        assert (await c.get(base)).status_code == 200
        assert (await c.get("/api/profiles/nope/cookies")).status_code == 404
    async with client(env["app"], token=False, cookies={f"pp_session_{PORT}": sid}) as browser:
        assert (await browser.get(base)).json()["total"] == 1  # the Manager page's own session
        # Changes need the Manager's Origin; cross-site requests are refused even for reads and downloads.
        assert (await browser.request("DELETE", base, json={"all": True})).status_code == 403
        assert (await browser.request("DELETE", base, json={"all": True}, headers={"Origin": "http://evil.test"})).status_code == 403
        assert (await browser.post(f"{base}/import", json={"text": "[]"})).status_code == 403
        assert (await browser.get(f"{base}/export", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        assert (await browser.get(base, headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        assert len(jar.jar) == 1
        r = await browser.request("DELETE", base, json={"all": True}, headers={**ORIGIN, "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200 and r.json() == {"deleted": 1}


@pytest.mark.asyncio
async def test_an_edit_that_lands_on_another_cookie_asks_first(env, monkeypatch) -> None:
    jar = FakeJar(monkeypatch, [jar_cookie("sid", value="host"), jar_cookie("sid", ".example.com", value="wide")])
    p = env["store"].create_profile("shop")
    base = f"/api/profiles/{p.id}/cookies"
    async with client(env["app"]) as c:
        await c.post(f"/api/profiles/{p.id}/start", json={})
        host_only = next(v for v in (await c.get(base)).json()["cookies"] if v["host_only"])
        r = await c.post(base, json={"cookie": {"host_only": False}, "replace": host_only["key"]})
        assert r.status_code == 409 and r.json()["code"] == "cookie_exists" and "wide" not in r.text
        assert {v["value"] for v in (await c.get(base)).json()["cookies"]} == {"host", "wide"}  # nothing changed
        r = await c.post(base, json={"cookie": {"host_only": False}, "replace": host_only["key"], "overwrite": True})
        assert r.status_code == 200 and r.json()["cookie"]["value"] == "host"
        assert [v["value"] for v in (await c.get(base)).json()["cookies"]] == ["host"]
    assert jar.calls.count("save") == 2


@pytest.mark.asyncio
async def test_cookie_list_add_edit_and_delete(env, monkeypatch) -> None:
    jar = FakeJar(monkeypatch, [jar_cookie("sid", httpOnly=True, sameSite="Lax"), jar_cookie("sid", ".example.com"),
                                jar_cookie("pref", "shop.example.com", "/app", expires=None, secure=False),
                                jar_cookie("chips", "widget.example", partitionKey=CHIPS_KEY),
                                jar_cookie("other", "other.test", value="needle-in-value")])
    p = env["store"].create_profile("shop")
    base = f"/api/profiles/{p.id}/cookies"
    async with client(env["app"]) as c:
        await c.post(f"/api/profiles/{p.id}/start", json={})
        listing = (await c.get(base)).json()
        assert listing["total"] == 5 and len(listing["cookies"]) == 5
        assert listing["domains"] == [{"domain": "example.com", "count": 2}, {"domain": "other.test", "count": 1},
                                      {"domain": "shop.example.com", "count": 1}, {"domain": "widget.example", "count": 1}]
        sid = next(v for v in listing["cookies"] if v["name"] == "sid" and v["host_only"])
        assert sid["value"] == f"sid-{COOKIE_SECRET}" and sid["http_only"] and sid["same_site"] == "Lax"
        assert sid["expires"].endswith("Z") and sid["session"] is False and sid["priority"] == "Medium"
        chips = next(v for v in listing["cookies"] if v["name"] == "chips")
        assert chips["partitioned"] and chips["partition_site"] == "https://top.example"
        # Filters: a domain with its subdomains, a substring of name / domain / value; totals stay the jar's.
        shown = (await c.get(f"{base}?domain=Example.com")).json()
        assert {(v["name"], v["domain"]) for v in shown["cookies"]} == {("sid", "example.com"), ("sid", ".example.com"),
                                                                       ("pref", "shop.example.com")}
        assert shown["total"] == 5 and len(shown["domains"]) == 4
        assert [v["name"] for v in (await c.get(f"{base}?q=NEEDLE")).json()["cookies"]] == ["other"]
        assert [v["name"] for v in (await c.get(f"{base}?q=widget")).json()["cookies"]] == ["chips"]
        assert (await c.get(f"{base}?domain=.")).status_code == 400

        # Add (Add dialog field names), edit (rename keeps everything else), and the errors.
        r = await c.post(base, json={"cookie": {"name": "new", "value": COOKIE_SECRET, "domain": "example.com",
                                                "host_only": False, "session": True, "same_site": "Strict",
                                                "http_only": True, "secure": True}})
        assert r.status_code == 201, r.text
        new = r.json()["cookie"]
        assert (new["domain"], new["session"], new["same_site"], new["http_only"]) == (".example.com", True, "Strict", True)
        r = await c.post(base, json={"cookie": {"name": "renamed"}, "replace": sid["key"]})
        assert r.status_code == 200, r.text
        renamed = r.json()["cookie"]
        assert renamed["value"] == sid["value"] and renamed["http_only"] and renamed["expires"] == sid["expires"]
        assert sid["key"] not in jar.jar and renamed["key"] in jar.jar
        r = await c.post(base, json={"cookie": {"value": "x"}, "replace": sid["key"]})
        assert r.status_code == 404 and r.json()["code"] == "not_found"
        r = await c.post(base, json={"cookie": {"name": "bad", "value": COOKIE_SECRET + ";", "domain": "example.com"}})
        assert r.status_code == 400 and r.json()["code"] == "invalid" and COOKIE_SECRET not in r.text
        r = await c.post(base, json={"cookie": {"name": "n", "value": "v", "domain": "example.com", "same_site": "None"}})
        assert r.status_code == 400 and "Secure" in r.json()["error"]
        r = await c.post(base, json={"cookie": {"name": "n", "value": COOKIE_SECRET, "domain": ".co.uk", "secure": True}})
        assert r.status_code == 422 and r.json()["code"] == "refused" and COOKIE_SECRET not in r.text
        assert (await c.post(base, json={"cookie": "sid=1"})).status_code == 400
        assert (await c.post(base, json={"cookie": {"name": "n"}, "replace": 5})).status_code in (400, 404)

        # Delete: by keys, by domain (with subdomains), everything; exactly one way at a time.
        r = await c.request("DELETE", base, json={"keys": [renamed["key"], chips["key"], renamed["key"]]})
        assert r.json() == {"deleted": 2}
        for body in ({}, {"keys": [renamed["key"]], "all": True}, {"keys": "not-a-list!"}, {"keys": ["%%%"]},
                     {"all": "yes"}, {"domain": 3.5, "all": True}):
            r = await c.request("DELETE", base, json=body)
            assert r.status_code == 400, body
        r = await c.request("DELETE", base, json={"domain": "example.com"})
        assert r.json() == {"deleted": 3}  # sid on .example.com, new, pref on shop.example.com
        assert {name for name, _d, _v in jar.values()} == {"other"}
        assert (await c.request("DELETE", base, json={"all": True})).json() == {"deleted": 1}
        assert (await c.request("DELETE", base, json={"all": True})).json() == {"deleted": 0}

        events = await cookie_activity(c, p.id)
        assert [e["tool"] for e in reversed(events)] == ["add cookie", "edit cookie", "delete cookies", "clear cookies",
                                                        "clear cookies"]
        assert all(e["source"] == "ui" and e["ok"] for e in events)
        assert [e["summary"] for e in reversed(events)] == [
            "Added cookie 'new' on example.com.", "Edited cookie 'renamed' on example.com.",
            "Deleted 2 cookies on 2 sites.", "Cleared 3 cookies on example.com.", "Cleared all cookies (1)."]
        assert not any(COOKIE_SECRET in json.dumps(e) or "needle" in json.dumps(e) for e in events)


@pytest.mark.asyncio
async def test_cookie_import_preview_and_modes(env, monkeypatch) -> None:
    jar = FakeJar(monkeypatch, [jar_cookie("old", "example.com"), jar_cookie("old", "sub.example.com"),
                                jar_cookie("keep", "other.test")])
    p = env["store"].create_profile("importer")
    base = f"/api/profiles/{p.id}/cookies"
    text = json.dumps([jar_cookie("new", ".example.com"), {"name": "nodomain", "value": COOKIE_SECRET},
                       jar_cookie("gone", expires=int(time.time()) - 60), jar_cookie("p", ".co.uk"),
                       jar_cookie("chips", "widget.example", partitionKey=CHIPS_KEY)])
    async with client(env["app"]) as c:
        # Preview: no profile involved, nothing written, problems by position / name - never values.
        r = await c.post("/api/cookies/parse", json={"text": text})
        assert r.status_code == 200
        preview = r.json()
        assert (preview["format"], preview["count"], preview["skipped"]) == ("json", 3, 2)
        assert preview["domains"] == [{"domain": "co.uk", "count": 1}, {"domain": "example.com", "count": 1},
                                      {"domain": "widget.example", "count": 1}]
        assert len(preview["problems"]) == 2 and preview["problems"][0].startswith("Cookie #2")
        assert "already expired" in preview["problems"][1] and COOKIE_SECRET not in r.text
        netscape = (await c.post("/api/cookies/parse", json={"text": ".example.com\tTRUE\t/\tTRUE\t0\ta\tb\n"})).json()
        assert (netscape["format"], netscape["count"]) == ("netscape", 1)
        assert (await c.post("/api/cookies/parse", json={"text": text, "format": "json"})).json()["count"] == 3
        assert (await c.post("/api/cookies/parse", json={"text": text, "format": "xml"})).status_code == 400
        assert (await c.post("/api/cookies/parse", json={"text": 5})).status_code == 200  # numbers are text, nothing found
        assert (await c.post("/api/cookies/parse", content=b"x" * (9 * 1024 * 1024))).status_code == 413
        assert jar.calls == []

        await c.post(f"/api/profiles/{p.id}/start", json={})
        # merge: everything else stays; Chrome's refusal is reported per cookie.
        r = await c.post(f"{base}/import", json={"text": text, "mode": "merge"})
        assert r.status_code == 200, r.text
        result = r.json()
        assert (result["imported"], result["skipped"], result["removed"]) == (2, 3, 0)
        assert result["problems"][-1] == "Chrome did not accept 'p' on co.uk."
        assert result["domains"] == [{"domain": "example.com", "count": 1}, {"domain": "widget.example", "count": 1}]
        assert len(jar.jar) == 5
        # replace: the other cookies of the imported sites go (with their subdomains); other sites stay.
        r = await c.post(f"{base}/import", json={"text": text, "mode": "replace"})
        assert r.json()["removed"] == 2
        assert {(n, d) for n, d, _v in jar.values()} == {("new", ".example.com"), ("chips", "widget.example"),
                                                         ("keep", "other.test")}
        # domain: only that domain's cookies of the text; replace_all clears everything else.
        r = await c.post(f"{base}/import", json={"text": text, "mode": "replace_all", "domain": "widget.example"})
        assert (r.json()["imported"], r.json()["removed"]) == (1, 2)
        assert {(n, d) for n, d, _v in jar.values()} == {("chips", "widget.example")}
        r = await c.post(f"{base}/import", json={"text": text, "domain": "nothing.test"})
        assert r.status_code == 400 and r.json()["code"] == "nothing_to_import" and "nothing.test" in r.json()["error"]
        r = await c.post(f"{base}/import", json={"text": "[{oops"})
        assert r.status_code == 400 and "Invalid JSON" in r.json()["error"]
        assert (await c.post(f"{base}/import", json={"text": text, "mode": "wipe"})).status_code == 400

        events = await cookie_activity(c, p.id)
        assert [e["summary"] for e in reversed(events)] == [
            "Imported 2 cookies on 2 sites.",
            "Imported 2 cookies on 2 sites, replacing 2 older cookies of those sites.",
            "Imported 1 cookie on widget.example, replacing all other cookies (2)."]
        assert not any(COOKIE_SECRET in json.dumps(e) for e in events)


@pytest.mark.asyncio
async def test_cookie_export_headers_and_formats(env, monkeypatch) -> None:
    FakeJar(monkeypatch, [jar_cookie("sid", httpOnly=True, expires=SOON + 0.704219),
                          jar_cookie("pref", ".shop.example.com", "/app", expires=None, secure=False),
                          jar_cookie("chips", "widget.example", partitionKey=CHIPS_KEY, priority="High")])
    p = env["store"].create_profile("My Shop/1")
    base = f"/api/profiles/{p.id}/cookies/export"
    async with client(env["app"]) as c:
        await c.post(f"/api/profiles/{p.id}/start", json={})
        r = await c.get(base)
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
        disposition = r.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="cookies-My-Shop-1-') and disposition.endswith('.json"')
        assert r.headers["cache-control"] == "no-store" and r.headers["x-cookie-count"] == "3"
        assert r.headers["x-cookies-omitted"] == "0"
        exported = {x["name"]: x for x in r.json()}
        assert exported["sid"]["value"] == f"sid-{COOKIE_SECRET}" and exported["sid"]["expires"] == SOON + 0.704219
        assert exported["chips"]["partitionKey"] == CHIPS_KEY and exported["chips"]["priority"] == "High"
        assert exported["pref"]["expires"] is None and exported["pref"]["domain"] == ".shop.example.com"
        # The export imports back as the same cookies.
        preview = (await c.post("/api/cookies/parse", json={"text": r.text})).json()
        assert (preview["count"], preview["problems"]) == (3, [])

        r = await c.get(f"{base}?format=netscape")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
        assert r.headers["content-disposition"].endswith('.txt"')
        assert r.text.startswith("# Netscape HTTP Cookie File") and "Left out 1 partitioned" in r.text
        assert "\tchips\t" not in r.text and f"#HttpOnly_example.com\tFALSE\t/\tTRUE\t{SOON}\tsid\t" in r.text
        assert (r.headers["x-cookie-count"], r.headers["x-cookies-omitted"]) == ("2", "1")

        # The current filter or a selection: ?domain=, ?keys=k1,k2 (GET) or a keys list (POST).
        r = await c.get(f"{base}?domain=shop.example.com")
        assert [x["name"] for x in r.json()] == ["pref"]
        keys = [v["key"] for v in (await c.get(f"/api/profiles/{p.id}/cookies")).json()["cookies"] if v["name"] != "pref"]
        r = await c.get(f"{base}?keys={','.join(keys)}")
        assert sorted(x["name"] for x in r.json()) == ["chips", "sid"]
        r = await c.post(base, json={"format": "netscape", "keys": keys[:1]})  # sid (sorted by site)
        assert r.status_code == 200 and r.headers["x-cookie-count"] == "1" and "\tsid\t" in r.text
        assert (await c.get(f"{base}?format=xml")).status_code == 400
        assert (await c.get(f"{base}?keys=a%2C%2C")).status_code == 200  # empty ids are ignored

        events = await cookie_activity(c, p.id)
        assert events[-1]["summary"] == "Exported 3 cookies on 3 sites as JSON."
        assert events[-2]["summary"] == "Exported 2 cookies on 2 sites as cookies.txt."
        assert not any(COOKIE_SECRET in json.dumps(e) for e in events)


@pytest.mark.asyncio
async def test_start_in_the_background(env) -> None:
    store: Store = env["store"]
    async with client(env["app"]) as c:
        normal = (await c.post("/api/profiles", json={"name": "visible", "window": "normal"})).json()
        headless = (await c.post("/api/profiles", json={"name": "quiet", "window": "headless"})).json()
        assert (await c.post(f"/api/profiles/{normal['id']}/start", json={"background": "yes"})).status_code == 400
        r = await c.post(f"/api/profiles/{normal['id']}/start", json={"background": True})
        assert r.status_code == 200 and r.json()["runtime"]["window"] == "offscreen"
        await c.post(f"/api/profiles/{headless['id']}/start", json={"background": True, "window": "normal"})
        await c.post(f"/api/profiles/{headless['id']}/stop")
        await c.post(f"/api/profiles/{headless['id']}/start", json={"background": True})
        assert env["runtime"].started == [(normal["id"], "offscreen"), (headless["id"], "offscreen"),
                                          (headless["id"], "headless")]
        assert store.get_profile(normal["id"]).launch.window == "normal"  # for this run only
        events = (await c.get(f"/api/activity?profile={normal['id']}")).json()["events"]
        assert events[0]["summary"] == "Started 'visible' in the background (off-screen window)."
        events = (await c.get(f"/api/activity?profile={headless['id']}")).json()["events"]
        assert events[0]["summary"] == "Started 'quiet' in the background (headless)."


# ---------------------------------------------------------------------- identities linked to browser-saved addresses

# Chromium FieldType numbers (see profilepilot.chrome_autofill.FIELD_TYPES).
NAME_FIRST, NAME_LAST, EMAIL, PHONE, STREET, CITY, STATE, ZIP, COUNTRY = 3, 5, 9, 14, 77, 33, 34, 35, 36
GUID_HOME = "0a1b2c3d-0000-4000-8000-000000000001"
GUID_WORK = "0a1b2c3d-0000-4000-8000-000000000002"
HOME_STREET, HOME_EMAIL, HOME_PHONE = "1 Example Street", "alex.home@example.com", "+1 555 0100"
WORK_STREET, WORK_EMAIL = "200 Office Park", "alex.work@example.com"


def make_web_data(root: Path) -> Path:
    """A fake Chrome "User Data" folder: profile Default ("Me", last used) with two saved addresses and a
    credit_cards table that must never be read. Returns the User Data folder."""
    import sqlite3

    udd = root / "User Data"
    folder = udd / "Default"
    folder.mkdir(parents=True)
    con = sqlite3.connect(folder / "Web Data")
    con.execute("CREATE TABLE addresses(guid TEXT PRIMARY KEY, use_count INTEGER, use_date INTEGER, "
                "date_modified INTEGER, language_code TEXT, label TEXT, initial_creator_id INTEGER, record_type INTEGER)")
    con.execute("CREATE TABLE address_type_tokens(guid TEXT, type INTEGER, value TEXT, verification_status INTEGER, "
                "observations BLOB)")
    con.execute("CREATE TABLE credit_cards(guid TEXT, name_on_card TEXT, card_number_encrypted BLOB)")
    con.execute("INSERT INTO credit_cards VALUES ('c1', 'Alex Sample', ?)", (CARD.encode(),))
    rows = {
        GUID_HOME: (9, {NAME_FIRST: "Alex", NAME_LAST: "Sample", EMAIL: HOME_EMAIL, PHONE: HOME_PHONE,
                        STREET: HOME_STREET, CITY: "Springfield", STATE: "IL", ZIP: "62701", COUNTRY: "US"}),
        GUID_WORK: (2, {NAME_FIRST: "Alex", NAME_LAST: "Sample", EMAIL: WORK_EMAIL, STREET: WORK_STREET,
                        CITY: "Chicago", STATE: "IL", COUNTRY: "US"}),
    }
    for guid, (uses, fields) in rows.items():
        con.execute("INSERT INTO addresses VALUES (?, ?, ?, 0, 'en', '', 0, 0)", (guid, uses, 1_700_000_000 + uses))
        for kind, value in fields.items():
            con.execute("INSERT INTO address_type_tokens VALUES (?, ?, ?, 0, NULL)", (guid, kind, value))
    con.commit()
    con.close()
    (udd / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Default": {"name": "Me"}},
                                                             "last_used": "Default"}}), encoding="utf-8")
    return udd


@pytest.fixture
def browser_data(tmp_path: Path, monkeypatch) -> Path:
    """Point browser discovery at the fake User Data folder (never at the user's real browsers)."""
    from profilepilot import chrome_autofill

    udd = make_web_data(tmp_path / "browser")
    monkeypatch.setattr(chrome_autofill, "_user_data_dirs", lambda: {"chrome": udd})
    return udd


@pytest.mark.asyncio
async def test_autofill_sources_list_summaries_only(env, browser_data) -> None:
    async with client(env["app"]) as c:
        call = Recorder(c)
        r = await call("GET", "/api/autofill/sources")
        assert r.status_code == 200
        (source,) = r.json()["sources"]
        assert source["ref"] == "chrome:chrome/Default" and source["active"] is True and source["profile_name"] == "Me"
        assert source["label"] == "Google Chrome profile 'Me' (active)" and source["error"] is None
        assert [(a["number"], a["id"], a["summary"]) for a in source["addresses"]] == [
            (1, GUID_HOME, "Alex Sample - Springfield, IL, US"), (2, GUID_WORK, "Alex Sample - Chicago, IL, US")]
        # Name and city only: no street, email, phone - and never a card.
        call.assert_never(HOME_STREET, HOME_EMAIL, HOME_PHONE, WORK_STREET, WORK_EMAIL, CARD)

        # An unreadable profile is listed with its reason instead of breaking the list.
        (browser_data / "Default" / "Web Data").write_bytes(b"this is not a database" * 64)
        (source,) = (await c.get("/api/autofill/sources")).json()["sources"]
        assert source["addresses"] == [] and "Could not read the saved addresses" in source["error"]


@pytest.mark.asyncio
async def test_identity_connect_to_browser_and_disconnect(env, browser_data) -> None:
    store: Store = env["store"]
    ids = IdentityStore(store)
    ident = ids.create("Personal", {})
    async with client(env["app"]) as c:
        call = Recorder(c)
        # Default: the user's active Chrome profile, its most used address; kept symbolic ("chrome").
        r = await call("POST", f"/api/identities/{ident.id}/chrome", json={"source": "chrome"})
        assert r.status_code == 200, r.text
        link = r.json()["chrome"]
        assert link["source"] == "chrome" and link["ok"] is True and link["pinned"] is False
        assert link["label"] == "Google Chrome profile 'Me' (active)"
        assert link["address"] == "Alex Sample - Springfield, IL, US"
        assert {"first_name", "last_name", "email", "phone", "street", "city", "state", "postal_code",
                "country_code"} <= set(link["fields_from_chrome"])
        assert r.json()["values"] == {}  # the browser's values are read at fill time, never copied in
        assert ids.get(ident.id).chrome_source == "chrome"
        # The identity's own values win (an own e-mail: the browser's is not used).
        r = await call("PATCH", f"/api/identities/{ident.id}", json={"values": {"email": "me@example.org"}})
        assert "email" not in r.json()["chrome"]["fields_from_chrome"]
        # A specific address: remembered by its id in that one browser profile.
        r = await call("POST", f"/api/identities/{ident.id}/chrome", json={"source": "chrome", "address": GUID_WORK})
        link = r.json()["chrome"]
        assert link["pinned"] is True and link["source"] == "chrome:chrome/Default"
        assert link["address"] == "Alex Sample - Chicago, IL, US"
        assert ids.get(ident.id).chrome_address == GUID_WORK
        r = await call("POST", f"/api/identities/{ident.id}/chrome", json={"source": "chrome:chrome/Default", "address": 1})
        assert r.json()["chrome"]["address"] == "Alex Sample - Springfield, IL, US"
        # The list carries the link too.
        listed = {i["id"]: i for i in (await call("GET", "/api/identities")).json()["identities"]}
        assert listed[ident.id]["chrome"]["ok"] is True
        # Bad requests.
        assert (await call("POST", f"/api/identities/{ident.id}/chrome", json={"address": 7})).status_code == 404
        assert (await call("POST", f"/api/identities/{ident.id}/chrome", json={"source": "profile"})).status_code == 400
        assert (await call("POST", f"/api/identities/{ident.id}/chrome", json={"source": "C:/Users"})).status_code == 400
        assert (await call("POST", f"/api/identities/{ident.id}/chrome", json={"address": True})).status_code == 400
        r = await call("POST", f"/api/identities/{ident.id}/chrome", json={"source": "chrome:edge"})
        assert r.status_code == 404 and "edge" in r.json()["error"]
        assert (await call("POST", "/api/identities/nope/chrome", json={})).status_code == 404
        # A browser profile that went away: the identity says so instead of failing the list.
        (browser_data / "Default" / "Web Data").unlink()
        link = (await call("GET", f"/api/identities/{ident.id}")).json()["chrome"]
        assert link["ok"] is False and link["error"]
        # Disconnect.
        r = await call("DELETE", f"/api/identities/{ident.id}/chrome")
        assert r.status_code == 200 and r.json()["chrome"] is None
        assert ids.get(ident.id).chrome_source is None and ids.get(ident.id).chrome_address is None
        call.assert_never(HOME_STREET, HOME_EMAIL, HOME_PHONE, WORK_STREET, WORK_EMAIL, CARD)
    summaries = [e.summary for e in ActivityLog(store.root).tail(10) if e.tool == "identity browser link"]
    assert summaries[0] == ("Identity 'Personal' now takes its details from Google Chrome profile 'Me' (active) "
                            "(the most used saved address).")
    assert summaries[-1] == "Identity 'Personal' no longer takes details from a browser."


@pytest.mark.asyncio
async def test_browser_link_endpoints_keep_the_csrf_checks(env, browser_data) -> None:
    ident = IdentityStore(env["store"]).create("Personal", {})
    code = env["app"].auth.new_code()
    async with client(env["app"], token=False) as anon:
        assert (await anon.get("/api/autofill/sources")).status_code == 401
        assert (await anon.post(f"/api/identities/{ident.id}/chrome", json={})).status_code == 401
        sid = (await anon.get(f"/?t={code}")).headers["set-cookie"].split(";")[0].split("=", 1)[1]
    async with client(env["app"], token=False, cookies={f"pp_session_{PORT}": sid}) as browser:
        assert (await browser.get("/api/autofill/sources")).status_code == 200
        assert (await browser.get("/api/autofill/sources", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        url = f"/api/identities/{ident.id}/chrome"
        assert (await browser.post(url, json={})).status_code == 403  # no Origin
        assert (await browser.post(url, json={}, headers={"Origin": "http://evil.test"})).status_code == 403
        assert (await browser.delete(url, headers={"Origin": "http://evil.test"})).status_code == 403
        assert IdentityStore(env["store"]).get(ident.id).chrome_source is None
        r = await browser.post(url, json={}, headers=ORIGIN)
        assert r.status_code == 200 and r.json()["chrome"]["ok"] is True
        assert (await browser.delete(url, headers=ORIGIN)).status_code == 200


@pytest.mark.asyncio
async def test_settings_toggle_autofill_from_browser(env) -> None:
    store: Store = env["store"]
    async with client(env["app"]) as c:
        assert (await c.get("/api/settings")).json()["autofill_from_browser"] is True
        r = await c.patch("/api/settings", json={"autofill_from_browser": False})
        assert r.status_code == 200 and r.json()["autofill_from_browser"] is False
        assert store.load_config().autofill_from_browser is False
        assert (await c.patch("/api/settings", json={"autofill_from_browser": "yes"})).status_code == 400
        assert (await c.patch("/api/settings", json={"autofill_from_browser": None})).status_code == 400
        r = await c.patch("/api/settings", json={"autofill_from_browser": True})
        assert store.load_config().autofill_from_browser is True
        assert (await c.get("/api/overview")).json()["settings"]["autofill_from_browser"] is True


# ---------------------------------------------------------------------- shortcut icon, launcher


def test_icon_files_are_valid(tmp_path: Path) -> None:
    from profilepilot.ui.shortcut import ICON_SIZES, ico_bytes, logo_rgba, png_bytes, write_icons

    ico = ico_bytes()
    reserved, kind, count = struct.unpack("<HHH", ico[:6])
    assert (reserved, kind, count) == (0, 1, len(ICON_SIZES))
    for i, size in enumerate(ICON_SIZES):
        w, h, _c, _r, planes, bpp, length, offset = struct.unpack("<BBBBHHII", ico[6 + 16 * i: 22 + 16 * i])
        assert (w or 256) == size and planes == 1 and bpp == 32
        assert ico[offset:offset + 8] == b"\x89PNG\r\n\x1a\n" and offset + length <= len(ico)
    png = png_bytes(32)
    assert png[12:16] == b"IHDR" and struct.unpack(">II", png[16:24]) == (32, 32)
    px = logo_rgba(64)
    assert px[3] == 0  # transparent corner
    centre = (32 * 64 + 32) * 4
    assert px[centre + 3] == 255 and px[centre] > 200  # white arrow in the middle
    files = write_icons(tmp_path / "ui")
    assert files["ico"].stat().st_size == len(ico) and files["png"].exists()


def test_install_shortcuts_into_given_folders(tmp_path: Path) -> None:
    from profilepilot.ui.shortcut import install_shortcuts, remove_shortcuts

    folders = [tmp_path / "Desktop", tmp_path / "Programs"]
    if sys.platform == "win32":
        pytest.importorskip("win32com.client")
    created = install_shortcuts(tmp_path / "root", folders=folders)
    links = [p for p in created if p.parent in folders]
    assert len(links) == 2 and all(p.exists() for p in links)
    for plat in ("darwin", "linux"):
        other = install_shortcuts(tmp_path / "root", folders=[tmp_path / plat], platform=plat)
        text = other[-1].read_text("utf-8")
        assert "profilepilot.ui" in text and "--home" in text
    assert len(remove_shortcuts(folders)) == 2


def test_window_command_is_a_plain_app_window(tmp_path: Path) -> None:
    from profilepilot.ui.launcher import window_command

    argv = window_command("C:/chrome.exe", "http://127.0.0.1:1/?t=x", tmp_path / "ui-window")
    assert argv[1] == "--app=http://127.0.0.1:1/?t=x" and f"--user-data-dir={tmp_path / 'ui-window'}" in argv
    assert not any(a.startswith(("--remote-debugging", "--enable-automation", "--headless")) for a in argv)


def test_server_exits_when_the_window_closes(tmp_path: Path, monkeypatch) -> None:
    """The app window's lifetime drives the server; no real window is opened (a stand-in process
    carries the window's --user-data-dir)."""
    import threading

    from profilepilot.ui import launcher

    home = tmp_path / "pp-home"
    store = Store(home)
    procs: list[subprocess.Popen] = []
    urls: list[str] = []

    def fake_open_window(store_: Store, url: str) -> subprocess.Popen:
        urls.append(url)
        udd = launcher.window_dir(store_)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"--user-data-dir={udd}"])
        procs.append(proc)
        return proc

    monkeypatch.setattr(launcher, "open_window", fake_open_window)
    result: dict[str, int] = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", launcher.run_manager(store, log_level="WARNING")),
                              daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not (read_json(home / "ui.json") and urls) and time.monotonic() < deadline:
            time.sleep(0.1)
        info = read_json(home / "ui.json")
        assert info and info["pid"] == os.getpid() and urls[0].startswith(f"http://127.0.0.1:{info['port']}/?t=")
        # The launch code in the window's URL opens a session once.
        r = httpx.get(urls[0], trust_env=False, follow_redirects=False)
        assert r.status_code == 303 and "pp_session_" in r.headers["set-cookie"]
        assert httpx.get(urls[0], trust_env=False).status_code == 401
        time.sleep(1.5)
        assert thread.is_alive()  # the "window" is still open
    finally:
        for proc in procs:
            for child in psutil.Process(proc.pid).children(recursive=True) if proc.poll() is None else []:
                child.kill()
            proc.kill()
            proc.wait(10)
    thread.join(30)
    assert not thread.is_alive() and result["code"] == 0
    assert not (home / "ui.json").exists() and store.secrets.get("ui:token") is None


def test_launcher_module_runs_and_is_single_instance(tmp_path: Path) -> None:
    """``python -m profilepilot.ui --no-window`` serves the API; a second launcher attaches to it."""
    home = tmp_path / "pp-home"
    env = {**os.environ, "PROFILEPILOT_SECRETS": "file", "PYTHONIOENCODING": "utf-8"}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    first = subprocess.Popen([sys.executable, "-m", "profilepilot.ui", "--no-window", "--home", str(home)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, creationflags=flags)
    try:
        deadline = time.monotonic() + 30
        info = None
        while time.monotonic() < deadline:
            info = read_json(home / "ui.json")
            if info:
                break
            assert first.poll() is None, first.stdout.read().decode("utf-8", "replace") if first.stdout else ""
            time.sleep(0.2)
        # A venv's python.exe may be a launcher that runs the real interpreter as its child.
        tree = {first.pid, *(c.pid for c in psutil.Process(first.pid).children(recursive=True))}
        assert info and info["pid"] in tree
        token = Store(home).secrets.get("ui:token")
        assert token and len(token) >= 40
        r = httpx.get(f"http://127.0.0.1:{info['port']}/api/ping", headers={TOKEN_HEADER: token}, trust_env=False)
        assert r.status_code == 200 and r.json()["pid"] == info["pid"]
        assert httpx.get(f"http://127.0.0.1:{info['port']}/api/ping", trust_env=False).status_code == 401
        second = subprocess.run([sys.executable, "-m", "profilepilot.ui", "--no-window", "--home", str(home)],
                                capture_output=True, text=True, env=env, timeout=60)
        assert second.returncode == 0 and "running on port" in second.stdout
    finally:
        with_children = [first.pid]
        try:
            with_children += [c.pid for c in psutil.Process(first.pid).children(recursive=True)]
        except psutil.Error:
            pass
        for pid in reversed(with_children):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        first.wait(10)
        if first.stdout:
            first.stdout.close()
