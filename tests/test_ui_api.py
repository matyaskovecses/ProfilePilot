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
    locations = Locations(home=tmp_path / "userhome", appdata=tmp_path / "appdata", localappdata=tmp_path / "local",
                          platform="win32", claude_cli=None)
    app = create_app(store, token=TOKEN, port=PORT, runtime=runtime, locations=locations,
                     checks=Checks(proxy=fake_check, relay=fake_check), focuser=lambda pid: focused.append(pid) or True,
                     opener=opened.append, poll_interval=0.1)
    return {"store": store, "app": app, "runtime": runtime, "focused": focused, "opened": opened, "tmp": tmp_path,
            "locations": locations}


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

        # Token exchange: a bad code gets the "expired" page; the token (or a launch code) a cookie.
        bad = await anon.get("/?t=nope")
        assert bad.status_code == 401 and "expired" in bad.text
        ok = await anon.get(f"/?t={TOKEN}")
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
        assert p["proxy"]["name"] == "shop-us" and p["proxy"]["host"] == "proxy.example.net"
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
        r = await c.post("/api/clients/claude-code/register")
        assert r.status_code == 200 and "claude mcp add" in r.json()["report"]
        r = await c.post("/api/clients/cursor/register")
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
