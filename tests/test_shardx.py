"""ShardXClient against an in-process fake of the ShardX launcher API (no real ShardX needed)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from profilepilot.errors import AmbiguousError, ConflictError, LaunchError, NotFoundError
from profilepilot.integrations.shardx import (
    TOKEN_KEY,
    AsyncShardXClient,
    SettingsTokenMinter,
    ShardXAuthError,
    ShardXClient,
    ShardXConflictError,
    ShardXError,
    ShardXLaunchError,
    ShardXNotFoundError,
    ShardXUnavailableError,
    base_url_from_settings,
    mint_token,
    normalize_token,
    redact_secrets,
    save_token,
)

from .fakes import OriginServer

SECRET = "0123456789abcdef" * 4  # 64 hex chars, like ShardX's two simple() UUIDs


# --------------------------------------------------------------------------- fake launcher


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def verify_jwt(token: str, secret: str) -> dict | None:
    """What ShardX's jsonwebtoken Validation::new(HS256) checks: signature + exp."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    if header.get("alg") != "HS256":
        return None
    expected = hmac.new(secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
        return None
    if int(claims.get("exp", 0)) <= time.time():
        return None
    return claims


@dataclass
class FakeShardX:
    """Mimics the routes, shapes and errors of ShardX launcher 2.0.3 (src-tauri/src/api.rs)."""

    secret: str = SECRET
    profiles: dict[str, dict] = field(default_factory=dict)
    running: dict[str, dict] = field(default_factory=dict)
    proxies: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    unauthorized: int = 0
    behaviours: dict[str, str] = field(default_factory=dict)

    def add_profile(self, name: str, *, behaviour: str | None = None, pid: str | None = None) -> str:
        pid = pid or str(uuid.uuid4())
        self.profiles[pid] = {
            "id": pid, "name": name, "notes": "", "proxy_id": None, "last_launched_at": None,
            "created_at": 1_700_000_000, "pinned": False, "folder": None, "color": None, "extensions": [],
        }
        if behaviour:
            self.behaviours[pid] = behaviour
        return pid

    # ---------------------------------------------------------------- server plumbing

    def __enter__(self) -> "FakeShardX":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, body: Any = None) -> None:
                payload = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                token = header[7:] if header.startswith(("Bearer ", "bearer ")) else None
                return bool(token) and verify_jwt(token.strip(), outer.secret) is not None

            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                outer.requests.append({"method": method, "path": self.path, "auth": "Authorization" in self.headers})
                if method == "GET" and self.path == "/health":
                    return self._send(200, {"ok": True, "name": "shardx-launcher", "version": "2.0.3"})
                route = outer.route(method, self.path)
                if route is None:
                    return self._send(404)
                if not self._authorized():
                    outer.unauthorized += 1
                    return self._send(401)
                status, data = route(body)
                return self._send(status, data)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def token(self, ttl: int = 3600) -> str:
        return mint_token(self.secret, ttl=ttl)

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if r["method"] == method and r["path"] == path)

    # ---------------------------------------------------------------- routes

    def route(self, method: str, path: str):
        if method == "GET" and path == "/profiles":
            return lambda _b: (200, self._list_profiles())
        if method == "GET" and path == "/running":
            return lambda _b: (200, self._list_running())
        if method == "GET" and path == "/proxies":
            return lambda _b: (200, self.proxies)
        if method == "POST" and path == "/proxies":
            return self._add_proxy
        m = re.fullmatch(r"/profiles/([^/]+)/(start|stop)", path)
        if method == "POST" and m:
            pid, action = m.groups()
            return (lambda b: self._start(pid, b)) if action == "start" else (lambda _b: self._stop(pid))
        return None

    def _list_profiles(self) -> list[dict]:
        out = []
        for pid, meta in self.profiles.items():
            run = self.running.get(pid)
            out.append({**meta, "running": run is not None, "pid": run and run["pid"], "cdp": run and run.get("cdp")})
        return out

    def _list_running(self) -> list[dict]:
        out = []
        for pid, run in self.running.items():
            entry = {"profile_id": pid, "pid": run["pid"], "uptime_ms": 1234}
            if run.get("cdp") is not None:  # serde skip_serializing_if = "Option::is_none"
                entry["cdp"] = run["cdp"]
            out.append(entry)
        return out

    def _start(self, pid: str, body: dict | None) -> tuple[int, Any]:
        if pid not in self.profiles:
            return 500, {"error": f"profile {pid} not found"}
        if pid in self.running:
            return 500, {"error": f"profile {pid} is already running"}
        behaviour = self.behaviours.get(pid)
        if behaviour == "proxyfail":
            return 500, {"error": "proxy socks5://alice:hunter2@10.0.0.1:1080 unreachable (also 10.0.0.2:8000:bob:s3cret)"}
        port = 9222 + len(self.running)
        cdp = None
        if behaviour != "nocdp":
            cdp = {"port": port, "http_url": f"http://127.0.0.1:{port}",
                   "web_socket_debugger_url": f"ws://127.0.0.1:{port}/devtools/browser/{uuid.uuid4()}"}
        self.running[pid] = {"pid": 4000 + len(self.running), "cdp": cdp}
        resp: dict[str, Any] = {"profile_id": pid, "pid": self.running[pid]["pid"],
                                "headless": bool((body or {}).get("headless")), "cdp": cdp, "cdp_error": None}
        if cdp is None:
            resp["cdp_error"] = "the browser did not report a debugging port within 30s; read DevToolsActivePort"
        return 200, resp

    def _stop(self, pid: str) -> tuple[int, Any]:
        return 200, {"profile_id": pid, "stopped": self.running.pop(pid, None) is not None}

    def _add_proxy(self, body: dict | None) -> tuple[int, Any]:
        raw = (body or {}).get("proxy", "")
        m = re.fullmatch(r"(?:(socks5|http|https)://)?(?:([^:@]+):([^@]+)@)?([\w.]+):(\d+)", raw)
        if not m:
            return 400, {"error": f"unparseable proxy: {raw}"}
        entry = {"id": str(uuid.uuid4()), "name": (body or {}).get("name") or f"{m.group(4)}:{m.group(5)}",
                 "kind": m.group(1) or "socks5", "host": m.group(4), "port": int(m.group(5)), "country": ""}
        self.proxies.append(entry)
        return 200, entry


@pytest.fixture
def fake():
    with FakeShardX() as server:
        yield server


@pytest.fixture
def client(fake):
    with ShardXClient(fake.url, fake.token(), cdp_wait=0.3) as c:
        yield c


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- tests


def test_health_needs_no_token_and_reports_version(fake):
    with ShardXClient(fake.url) as c:  # no token at all
        assert c.health()["version"] == "2.0.3"
        assert c.is_available()
    assert fake.requests and all(r == {"method": "GET", "path": "/health", "auth": False} for r in fake.requests)


def test_launcher_not_running_gives_clear_error():
    with ShardXClient(f"http://127.0.0.1:{_closed_port()}", timeout=1) as c:
        with pytest.raises(ShardXUnavailableError, match="not reachable.*Automation API"):
            c.health()
        assert c.is_available() is False
        status = c.status()
        assert status["reachable"] is False and "not reachable" in status["error"]


def test_something_else_on_the_port_is_not_mistaken_for_shardx():
    with OriginServer() as origin, ShardXClient(origin.url) as c:
        with pytest.raises(ShardXUnavailableError, match="not the ShardX launcher"):
            c.health()


def test_listing_profiles_running_and_proxies(fake, client):
    a = fake.add_profile("Shop A")
    fake.add_profile("Shop B")
    fake.proxies.append({"id": "p1", "name": "res", "kind": "socks5", "host": "1.2.3.4", "port": 1080, "country": "US"})
    names = sorted(p["name"] for p in client.list_profiles())
    assert names == ["Shop A", "Shop B"]
    assert client.running() == []
    assert client.list_proxies()[0]["host"] == "1.2.3.4"
    client.start(a)
    profiles = {p["id"]: p for p in client.list_profiles()}
    assert profiles[a]["running"] is True and profiles[a]["cdp"]["port"]
    assert [r["profile_id"] for r in client.running()] == [a]


def test_missing_and_rotated_tokens(fake, caplog):
    caplog.set_level(logging.DEBUG)
    with ShardXClient(fake.url) as c:
        with pytest.raises(ShardXAuthError, match="No ShardX API token configured"):
            c.list_profiles()
    stale = mint_token("f" * 64, ttl=3600)  # signed with an old (rotated) secret
    with ShardXClient(fake.url, stale) as c:
        with pytest.raises(ShardXAuthError, match="401.*regenerated") as info:
            c.list_profiles()
        assert stale not in str(info.value)
        status = c.status()
        assert status["reachable"] is True and status["authenticated"] is False
    assert fake.unauthorized >= 1
    assert stale not in caplog.text and SECRET not in caplog.text


def test_start_returns_cdp_and_is_idempotent(fake, client):
    pid = fake.add_profile("Main")
    first = client.start(pid)
    assert set(first) >= {"port", "http_url", "web_socket_debugger_url"}
    assert first["http_url"] == f"http://127.0.0.1:{first['port']}"
    assert first["web_socket_debugger_url"].startswith(f"ws://127.0.0.1:{first['port']}/devtools/browser/")
    again = client.start(f"shardx:{pid}")  # prefix form accepted, no second launch
    assert again["http_url"] == first["http_url"]
    assert fake.count("POST", f"/profiles/{pid}/start") == 1
    assert client.cdp(pid)["port"] == first["port"]
    assert client.stop(pid) is True
    assert client.stop(pid) is False
    assert client.cdp(pid) is None


def test_start_without_cdp_reports_cdp_error(fake, client):
    pid = fake.add_profile("Slow", behaviour="nocdp")
    t0 = time.monotonic()
    with pytest.raises(ShardXLaunchError, match="not attachable: the browser did not report a debugging port") as info:
        client.start(pid)
    assert isinstance(info.value, LaunchError)
    assert time.monotonic() - t0 < 5  # bounded by cdp_wait


def test_profile_opened_from_launcher_ui_is_a_conflict(fake, client):
    pid = fake.add_profile("Opened in UI")
    fake.running[pid] = {"pid": 777, "cdp": None}  # UI launches have no CDP
    with pytest.raises(ShardXConflictError, match="already running without remote debugging") as info:
        client.start(pid)
    assert isinstance(info.value, ConflictError)


def test_start_unknown_profile(fake, client):
    with pytest.raises(ShardXError, match="500.*not found"):
        client.start("does-not-exist")


def test_proxy_credentials_are_redacted_from_errors(fake, client, caplog):
    caplog.set_level(logging.DEBUG)
    pid = fake.add_profile("Proxied", behaviour="proxyfail")
    with pytest.raises(ShardXError) as info:
        client.start(pid)
    text = str(info.value)
    assert "hunter2" not in text and "alice" not in text and "s3cret" not in text and "bob" not in text
    assert "socks5://***:***@10.0.0.1:1080" in text and "10.0.0.2:8000:***:***" in text
    with pytest.raises(ShardXError, match="400") as info:
        client.add_proxy("socks5://carol:pa55word@no-port-here")
    assert "pa55word" not in str(info.value) and "carol" not in str(info.value)
    assert "pa55word" not in caplog.text and "hunter2" not in caplog.text
    created = client.add_proxy("http://dave:pw@5.6.7.8:3128", name="dc")
    assert created == {**created, "name": "dc", "kind": "http", "host": "5.6.7.8", "port": 3128}


def test_resolve_by_id_name_prefix(fake, client, monkeypatch):
    a = fake.add_profile("Alpha", pid="aaaa1111-0000-4000-8000-000000000001")
    fake.add_profile("Beta", pid="aaaa2222-0000-4000-8000-000000000002")
    fake.add_profile("twin", pid="bbbb0000-0000-4000-8000-000000000003")
    fake.add_profile("TWIN", pid="cccc0000-0000-4000-8000-000000000004")
    assert client.resolve(a)["name"] == "Alpha"
    assert client.resolve("shardx:alpha")["id"] == a
    assert client.resolve("aaaa1")["name"] == "Alpha"
    with pytest.raises(AmbiguousError):
        client.resolve("aaaa")
    with pytest.raises(AmbiguousError):
        client.resolve("twin")
    with pytest.raises(ShardXNotFoundError, match="Gamma.*Alpha") as info:
        client.resolve("Gamma")
    assert isinstance(info.value, NotFoundError)


def test_mint_token_is_a_valid_hs256_jwt():
    token = mint_token(SECRET, ttl=300, now=1_700_000_000)
    header_b64, payload_b64, sig_b64 = token.split(".")
    assert "=" not in token
    assert json.loads(_b64url_decode(header_b64)) == {"alg": "HS256", "typ": "JWT"}
    assert json.loads(_b64url_decode(payload_b64)) == {"sub": "shardx-api", "iat": 1_700_000_000, "exp": 1_700_000_300}
    expected = hmac.new(SECRET.encode("utf-8"), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256).digest()
    assert hmac.compare_digest(_b64url_decode(sig_b64), expected)
    fresh = mint_token(SECRET)
    claims = verify_jwt(fresh, SECRET)
    assert claims and claims["exp"] - claims["iat"] == 300
    assert verify_jwt(fresh, "other-secret") is None
    with pytest.raises(ShardXAuthError):
        mint_token("")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_settings_minter_reads_secret_and_follows_rotation(fake, tmp_path, encoding, caplog):
    caplog.set_level(logging.DEBUG)
    settings = tmp_path / "shardx-launcher" / "settings.json"
    settings.parent.mkdir()

    def write(secret: str) -> None:
        settings.write_bytes(json.dumps({"api_enabled": True, "api_port": 40999, "api_secret": secret}).encode(encoding))

    write(SECRET)
    assert base_url_from_settings(settings) == "http://127.0.0.1:40999"
    pid = fake.add_profile("Minted")
    minter = SettingsTokenMinter(settings)
    with ShardXClient(fake.url, token_provider=minter) as c:
        assert c.resolve("Minted")["id"] == pid
        first_token = minter()
        assert verify_jwt(first_token, SECRET)["sub"] == "shardx-api"
        # "Regenerate token" in the launcher: new secret on disk and in the server
        rotated = "9" * 64
        fake.secret = rotated
        write(rotated)
        before = fake.unauthorized
        assert c.list_profiles()[0]["id"] == pid  # 401 -> invalidate -> re-mint -> retry
        assert fake.unauthorized == before + 1
        assert verify_jwt(minter(), rotated)
    assert SECRET not in caplog.text and rotated not in caplog.text and first_token not in caplog.text


def test_settings_minter_errors_are_clear(tmp_path):
    with pytest.raises(ShardXAuthError, match="settings file not found"):
        SettingsTokenMinter(tmp_path / "missing.json")()
    empty = tmp_path / "settings.json"
    empty.write_text(json.dumps({"api_secret": ""}))
    with pytest.raises(ShardXAuthError, match="no api_secret"):
        SettingsTokenMinter(empty)()
    broken = tmp_path / "broken.json"
    broken.write_text('{"api_secret": "abc", oops')
    with pytest.raises(ShardXError, match="not valid JSON") as info:
        SettingsTokenMinter(broken)()
    assert "abc" not in str(info.value)


def test_from_store_uses_keyring_token(fake, store):
    cfg = store.load_config()
    cfg.shardx.enabled = True
    cfg.shardx.base_url = fake.url
    store.save_config(cfg)
    token = fake.token()
    save_token(store.secrets, f"Bearer {token}\n")
    assert store.secrets.get(TOKEN_KEY) == token
    assert token not in (store.root / "config.json").read_text()
    fake.add_profile("FromStore")
    with ShardXClient.from_store(store) as c:
        assert c.base_url == fake.url
        assert [p["name"] for p in c.list_profiles()] == ["FromStore"]
        assert token not in repr(c)


def test_from_store_settings_source(fake, store, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"api_port": 40325, "api_secret": SECRET}))
    cfg = store.load_config()
    cfg.shardx.base_url = fake.url
    cfg.shardx.token_source = "settings"
    store.save_config(cfg)
    fake.add_profile("Opt-in")
    with ShardXClient.from_store(store, settings_path=settings) as c:
        assert c.list_profiles()[0]["name"] == "Opt-in"


def test_normalize_token_rejects_garbage():
    assert normalize_token("  bearer a.b.c ") == "a.b.c"
    with pytest.raises(Exception, match="does not look like a ShardX API token"):
        normalize_token("not a token")


@pytest.mark.parametrize(
    ("text", "leaked", "expected"),
    [
        ("dial socks5://user:pa:ss@1.2.3.4:1080 failed", "pa:ss", "socks5://***:***@1.2.3.4:1080"),
        ("bad http://u%40x:p%23w@host.example:80/path", "p%23w", "http://***:***@host.example:80/path"),
        ("socks5h://only-user@h:1", "only-user", "socks5h://***:***@h:1"),
        ("unparseable proxy: alice:secret@10.1.1.1:3128", "secret", "***:***@10.1.1.1:3128"),
        ("unparseable proxy: 10.1.1.1:3128:alice:secret", "secret", "10.1.1.1:3128:***:***"),
        ("proxy.example.com:8000:alice:secret", "alice", "proxy.example.com:8000:***:***"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi", "Bearer ***"),
        ('{"username": "u", "password": "hunter2"}', "hunter2", '"password": "***"'),
        ("token eyJhbGciOi.eyJzdWIi.c2lnbmF0dXJl leaked", "c2lnbmF0dXJl", "token *** leaked"),
    ],
)
def test_redact_secrets(text, leaked, expected):
    out = redact_secrets(text)
    assert leaked not in out
    assert expected in out


def test_redact_keeps_harmless_text():
    for text in ("profile 1234 is already running", "mail admin@example.com", "http://127.0.0.1:9222/json",
                 "at 12:30:45 the launcher started"):
        assert redact_secrets(text) == text


@pytest.mark.asyncio
async def test_async_client(fake):
    pid = fake.add_profile("Async")
    async with AsyncShardXClient(base_url=fake.url, token=fake.token()) as c:
        assert (await c.health())["ok"] is True
        assert (await c.resolve("async"))["id"] == pid
        cdp = await c.start(pid)
        assert cdp["http_url"].startswith("http://127.0.0.1:")
        assert [r["profile_id"] for r in await c.running()] == [pid]
        assert await c.stop(pid) is True


def test_redact_secrets_stays_fast_on_long_page_controlled_text():
    from profilepilot.integrations.shardx import REDACT_MAX

    # each of these made a pattern backtrack quadratically (minutes at 200 KB)
    for unit in ("!", "ab.", "eyJ-", "a:", "x@"):
        text = unit * (200_000 // len(unit))
        started = time.perf_counter()
        out = redact_secrets(text)
        assert time.perf_counter() - started < 1.0, unit
        assert len(out) <= REDACT_MAX + 20
    # a secret that the cut falls into is dropped whole, never half-kept
    secret = "proxyuser:" + "S3cretPassw0rd" * 2 + "@gate.example.test"
    text = "x " * ((REDACT_MAX - 10) // 2) + secret + " tail"
    out = redact_secrets(text)
    assert out.endswith("[truncated]") and "S3cret" not in out and "proxyuser" not in out
    assert redact_secrets("short socks5://u:p@h:1080 text") == "short socks5://***:***@h:1080 text"
