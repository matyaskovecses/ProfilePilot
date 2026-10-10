"""OAuth 2.1 for ``serve --http --auth oauth`` (src/profilepilot/server/oauth.py).

The full flow runs against a real ASGI app (an ``MCPServer`` with the pairing-code provider, its
streamable HTTP app and the OAuth gateway) through httpx's ASGI transport: no sockets, no network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from profilepilot.server import oauth as oauth_mod
from profilepilot.server.oauth import (
    ACCESS_TTL,
    CHATGPT_REDIRECT_URI,
    REFRESH_GRACE,
    OAuthStore,
    build_oauth,
    build_oauth_settings,
    client_from_metadata_document,
    new_pairing_code,
    normalize_pairing_code,
    pairing_code,
    public_base_url,
    render_consent_page,
    validate_redirect_uri,
)
from profilepilot.store import Store

BASE = "https://tunnel.example"
MCP_URL = f"{BASE}/mcp"
ACCEPT = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


class FakeClock:
    def __init__(self, start: float = 1_900_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "home")


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


@asynccontextmanager
async def oauth_app(store: Store, clock: FakeClock, **kw: Any) -> AsyncIterator[tuple[Any, httpx2.AsyncClient]]:
    """A minimal MCP server protected by the pairing-code OAuth server, as an ASGI app."""
    setup = build_oauth(store, BASE, clock=clock, **kw)
    server = MCPServer("oauth-test", auth_server_provider=setup.provider, auth=setup.settings)

    @server.tool()
    def ping() -> str:
        """Answer pong."""
        return "pong"

    inner = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=["tunnel.example", "127.0.0.1:*"],
            allowed_origins=[BASE],
        ),
    )
    app = setup.wrap(inner)
    async with inner.router.lifespan_context(inner):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
            yield setup, http


async def register(http: httpx2.AsyncClient, **extra: Any) -> dict[str, Any]:
    body = {
        "redirect_uris": [CHATGPT_REDIRECT_URI], "client_name": "ChatGPT", "grant_types": ["authorization_code",
        "refresh_token"], "response_types": ["code"], "token_endpoint_auth_method": "client_secret_post", **extra,
    }
    response = await http.post("/register", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def consent_fields(page: str) -> dict[str, str]:
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', page))
    assert {"request", "csrf"} <= set(fields), page
    return fields


async def start_authorize(http: httpx2.AsyncClient, client: dict[str, Any], challenge: str, *,
                          state: str = "st-1", resource: str = MCP_URL) -> httpx2.Response:
    params = {
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": CHATGPT_REDIRECT_URI,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "resource": resource,
        "scope": "profilepilot",
    }
    return await http.get("/authorize?" + urlencode(params))


async def open_consent(http: httpx2.AsyncClient, client: dict[str, Any], challenge: str,
                       **kw: Any) -> tuple[str, dict[str, str]]:
    response = await start_authorize(http, client, challenge, **kw)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"{BASE}/oauth/consent?request=")
    page = await http.get(location)
    assert page.status_code == 200
    return page.text, consent_fields(page.text)


async def submit(http: httpx2.AsyncClient, fields: dict[str, str], code: str,
                 action: str = "approve") -> httpx2.Response:
    return await http.post("/oauth/consent", data={**fields, "code": code, "action": action})


async def authorize_code(http: httpx2.AsyncClient, store: Store, client: dict[str, Any], challenge: str,
                         state: str = "st-1") -> str:
    _, fields = await open_consent(http, client, challenge, state=state)
    response = await submit(http, fields, pairing_code(store))
    assert response.status_code == 303, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["state"] == [state] and query["iss"] == [BASE]
    return query["code"][0]


async def token_request(http: httpx2.AsyncClient, client: dict[str, Any], **form: str) -> httpx2.Response:
    data = {"client_id": client["client_id"], "client_secret": client.get("client_secret") or "", **form}
    if not data["client_secret"]:
        del data["client_secret"]
    return await http.post("/token", data=data)


async def exchange(http: httpx2.AsyncClient, client: dict[str, Any], code: str, verifier: str) -> httpx2.Response:
    return await token_request(http, client, grant_type="authorization_code", code=code,
                               redirect_uri=CHATGPT_REDIRECT_URI, code_verifier=verifier, resource=MCP_URL)


async def mcp_list(http: httpx2.AsyncClient, token: str) -> httpx2.Response:
    return await http.post("/mcp", json=LIST_TOOLS, headers={**ACCEPT, "Authorization": f"Bearer {token}"})


# ---------------------------------------------------------------------- unit helpers


def test_pairing_code_shape_and_rotation(store):
    alphabet = set(oauth_mod.PAIRING_ALPHABET)
    for _ in range(200):
        code = new_pairing_code()
        assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", code)
        assert set(code.replace("-", "")) <= alphabet
        assert not set(code) & set("01OIL")
    first = pairing_code(store)
    assert pairing_code(store) == first  # stable until used
    assert oauth_mod.rotate_pairing_code(store) != first
    assert normalize_pairing_code(" abcd 2345 ") == normalize_pairing_code("ABCD-2345") == "ABCD2345"
    # the longer code of `connect unlock` (about 59 bits) is kept like a normal one until it is used
    long_code = new_pairing_code(oauth_mod.UNLOCK_CODE_LENGTH)
    assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", long_code)
    assert set(long_code.replace("-", "")) <= alphabet
    store.secrets.set(oauth_mod.PAIRING_KEY, long_code)
    assert pairing_code(store) == long_code
    assert len(normalize_pairing_code(oauth_mod.rotate_pairing_code(store))) == oauth_mod.PAIRING_LENGTH
    store.secrets.set(oauth_mod.PAIRING_KEY, "ABC-123")  # anything else is replaced
    assert len(normalize_pairing_code(pairing_code(store))) == oauth_mod.PAIRING_LENGTH


def test_public_urls_and_redirect_validation(store):
    assert public_base_url("127.0.0.1", 8931, ["Abc.trycloudflare.com"]) == "https://abc.trycloudflare.com"
    assert public_base_url("127.0.0.1", 8931, ["https://x.example/mcp"]) == "https://x.example"
    assert public_base_url("0.0.0.0", 9000, []) == "http://127.0.0.1:9000"
    for good in (CHATGPT_REDIRECT_URI, "http://localhost:3000/callback", "http://127.0.0.1:5173/cb",
                 "cursor://anysphere.cursor-retrieval/oauth/callback", "https://claude.ai/api/mcp/auth_callback",
                 "vscode://vscode.github-authentication/did-authenticate", "claude://oauth/callback",
                 "com.example.app:/oauth2redirect"):
        assert validate_redirect_uri(good) == good
    # an allowlist: Windows protocol handlers and network shares are refused (they start programs / leak NTLM)
    for bad in ("javascript:alert(1)", "data:text/html,hi", "http://evil.example/cb", "https://a.example/cb#frag",
                "file:///C:/x", "https://user:pw@a.example/cb", "nonsense",
                r"search-ms:query=x&crumb=location:\\evil.example\share", "ms-officecmd:{}",
                "smb://evil.example/share", "ms-settings:privacy", "mailto:x@evil.example", "ftp://evil.example/x",
                "cursor://user:pw@anysphere.cursor-retrieval/cb"):
        with pytest.raises(ValueError):
            validate_redirect_uri(bad)
    with pytest.raises(Exception, match="https"):
        build_oauth_settings("http://public.example", store=store)  # https required off-localhost


def test_settings_for_the_sdk(store):
    provider, settings = build_oauth_settings("https://abc.trycloudflare.com/mcp", store=store)
    assert str(settings.issuer_url) == "https://abc.trycloudflare.com"
    assert str(settings.resource_server_url) == "https://abc.trycloudflare.com/mcp"
    assert settings.validate_token_resource is True
    assert settings.client_registration_options.enabled and settings.revocation_options.enabled
    assert provider.issuer == "https://abc.trycloudflare.com"


def test_consent_page_escapes_everything():
    pending = oauth_mod.PendingAuthorization(
        request_id='r"><script>x</script>', client_id="c", client_name='<img src=x onerror=alert(1)>',
        redirect_uri='https://evil.example/cb?a="><script>alert(1)</script>', redirect_uri_provided_explicitly=True,
        state=None, scopes=["profilepilot"], code_challenge="c", resource=MCP_URL, csrf="t", created_at=0,
    )
    page = render_consent_page(pending, nonce="n0nce", error="<b>bad</b>")
    assert "<script>" not in page and "<img" not in page and "<b>bad" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "not ChatGPT or Claude" in page  # unknown destination warning
    # the approve button comes first in the DOM, so Enter in the code field approves
    assert page.index('value="approve"') < page.index('value="deny"')
    # no external assets and no script at all
    assert not re.search(r"<(script|link|iframe|img)\b", page)
    assert not re.search(r'\s(src|href)="', page)


def test_cimd_document_validation():
    url = "https://chatgpt.example/oauth/client.json"
    doc = {"client_id": url, "client_name": "ChatGPT", "redirect_uris": [CHATGPT_REDIRECT_URI],
           "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
           "token_endpoint_auth_method": "private_key_jwt",
           "token_endpoint_auth_methods_supported": ["private_key_jwt", "none"]}
    client = client_from_metadata_document(url, doc)
    assert client.client_id == url and client.token_endpoint_auth_method == "none"
    assert [str(u) for u in client.redirect_uris] == [CHATGPT_REDIRECT_URI]
    with pytest.raises(ValueError, match="does not match"):
        client_from_metadata_document(url, {**doc, "client_id": "https://other.example/c.json"})
    with pytest.raises(ValueError, match="public clients"):
        client_from_metadata_document(url, {**doc, "token_endpoint_auth_methods_supported": ["private_key_jwt"]})
    with pytest.raises(ValueError):
        client_from_metadata_document(url, {**doc, "redirect_uris": ["javascript:alert(1)"]})
    for bad in ("http://chatgpt.example/c.json", "https://127.0.0.1/c.json", "https://localhost/c.json",
                "https://chatgpt.example/", "https://chatgpt.example:8443/c.json", "https://10.0.0.1/c.json",
                "https://intranet/c.json"):
        with pytest.raises(ValueError):
            oauth_mod.check_cimd_url(bad)


# ---------------------------------------------------------------------- full flow


@pytest.mark.asyncio
async def test_full_oauth_flow(store):
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        # discovery: 401 challenge -> protected resource metadata -> authorization server metadata
        challenge_response = await http.post("/mcp", json=LIST_TOOLS, headers=ACCEPT)
        assert challenge_response.status_code == 401
        www = challenge_response.headers["www-authenticate"]
        assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in www
        assert 'scope="profilepilot"' in www
        for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
            prm = (await http.get(path)).json()
            assert prm["resource"] == MCP_URL and prm["authorization_servers"] == [BASE]
            assert prm["scopes_supported"] == ["profilepilot"]
        meta_response = await http.get("/.well-known/oauth-authorization-server")
        meta = meta_response.json()
        assert meta_response.headers["access-control-allow-origin"] == "*"
        assert meta["issuer"] == BASE  # exactly the authorization_servers entry (RFC 9207)
        assert meta["authorization_response_iss_parameter_supported"] is True
        assert meta["code_challenge_methods_supported"] == ["S256"]
        assert meta["client_id_metadata_document_supported"] is True
        assert "none" in meta["token_endpoint_auth_methods_supported"]
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
            assert meta[key].startswith(BASE + "/")

        # dynamic client registration (and a refused javascript: redirect)
        bad = await http.post("/register", json={"redirect_uris": ["javascript:alert(1)"], "client_name": "x"})
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_redirect_uri"
        client = await register(http)
        assert client["client_secret"] and client["scope"] == "profilepilot"
        greedy = await register(http, scope="openid email profilepilot")  # extra scopes: accepted, never granted
        _, greedy_challenge = pkce()
        greedy_auth = await http.get("/authorize?" + urlencode({
            "response_type": "code", "client_id": greedy["client_id"], "redirect_uri": CHATGPT_REDIRECT_URI,
            "code_challenge": greedy_challenge, "code_challenge_method": "S256", "scope": "openid profilepilot"}))
        assert greedy_auth.status_code == 302 and "/oauth/consent?request=" in greedy_auth.headers["location"]

        # authorize -> consent page
        verifier, challenge = pkce()
        page, fields = await open_consent(http, client, challenge)
        assert "Allow ChatGPT to use ProfilePilot?" in page and "chatgpt.com" in page
        consent = await http.get(f"/oauth/consent?request={fields['request']}")
        csp = consent.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp
        assert "form-action 'self' https://chatgpt.com" in csp and "script-src" not in csp
        assert consent.headers["x-frame-options"] == "DENY"
        assert consent.headers["cache-control"] == "no-store"
        assert "httponly" in consent.headers["set-cookie"].lower()
        assert "samesite=strict" in consent.headers["set-cookie"].lower()
        assert "secure" in consent.headers["set-cookie"].lower()
        assert pairing_code(store) not in page

        # a wrong code is refused (and nothing is issued); CSRF is enforced
        old_code = pairing_code(store)
        wrong = await submit(http, fields, "ZZZZ-ZZZZ")
        assert wrong.status_code == 400 and "not right" in wrong.text and "4 attempt(s) left" in wrong.text
        async with httpx2.AsyncClient(transport=http._transport, base_url=BASE) as other_browser:
            no_cookie = await other_browser.post("/oauth/consent",
                                                 data={**fields, "code": old_code, "action": "approve"})
        assert no_cookie.status_code == 403
        forged = await submit(http, {**fields, "csrf": "forged"}, old_code)
        assert forged.status_code == 403

        # the right code (in any spelling) approves, redirects with code + state + iss and rotates the code
        approved = await submit(http, fields, old_code.lower().replace("-", " "))
        assert approved.status_code == 303
        location = approved.headers["location"]
        assert location.startswith(CHATGPT_REDIRECT_URI + "?")
        query = parse_qs(urlsplit(location).query)
        assert query["state"] == ["st-1"] and query["iss"] == [BASE]
        code = query["code"][0]
        assert pairing_code(store) != old_code
        assert (await submit(http, fields, pairing_code(store))).status_code == 400  # request already used

        # token exchange with PKCE
        response = await exchange(http, client, code, verifier)
        assert response.status_code == 200, response.text
        tokens = response.json()
        assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == ACCESS_TTL
        assert tokens["refresh_token"] and tokens["scope"] == "profilepilot"

        # the bearer token opens the MCP endpoint
        listed = await mcp_list(http, tokens["access_token"])
        assert listed.status_code == 200 and "ping" in listed.text
        assert (await mcp_list(http, "ppat_" + "x" * 43)).status_code == 401

        # only hashes are stored
        raw = (store.root / "oauth.json").read_text(encoding="utf-8")
        for secret_value in (tokens["access_token"], tokens["refresh_token"], code):
            assert secret_value not in raw
        grants = OAuthStore(store.root, clock=clock).grants()
        assert [g["client_name"] for g in grants] == ["ChatGPT"]

        # refresh rotates both tokens
        refreshed = await token_request(http, client, grant_type="refresh_token",
                                        refresh_token=tokens["refresh_token"], resource=MCP_URL)
        assert refreshed.status_code == 200, refreshed.text
        new = refreshed.json()
        assert new["access_token"] != tokens["access_token"] and new["refresh_token"] != tokens["refresh_token"]
        assert (await mcp_list(http, new["access_token"])).status_code == 200

        # revoke: the access token and the whole grant stop working
        revoked = await http.post("/revoke", data={"token": new["access_token"], "client_id": client["client_id"],
                                                   "client_secret": client["client_secret"]})
        assert revoked.status_code == 200
        assert (await mcp_list(http, new["access_token"])).status_code == 401
        dead = await token_request(http, client, grant_type="refresh_token", refresh_token=new["refresh_token"])
        assert dead.status_code == 400 and dead.json()["error"] == "invalid_grant"
        assert OAuthStore(store.root, clock=clock).grants() == []


@pytest.mark.asyncio
async def test_replayed_code_wrong_verifier_and_expiry(store):
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        client = await register(http)

        # replaying a used code fails and revokes the tokens issued from it
        verifier, challenge = pkce()
        code = await authorize_code(http, store, client, challenge)
        first = await exchange(http, client, code, verifier)
        assert first.status_code == 200
        access = first.json()["access_token"]
        assert (await mcp_list(http, access)).status_code == 200
        replay = await exchange(http, client, code, verifier)
        assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
        assert (await mcp_list(http, access)).status_code == 401

        # a wrong PKCE verifier is refused, and burns the code
        verifier, challenge = pkce()
        code = await authorize_code(http, store, client, challenge)
        wrong = await exchange(http, client, code, "w" * 50)
        assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_grant"
        assert (await exchange(http, client, code, verifier)).status_code == 400

        # an expired authorization code is refused
        verifier, challenge = pkce()
        code = await authorize_code(http, store, client, challenge)
        clock.advance(oauth_mod.CODE_TTL + 1)
        assert (await exchange(http, client, code, verifier)).status_code == 400

        # an expired access token is refused; the refresh token still works
        verifier, challenge = pkce()
        code = await authorize_code(http, store, client, challenge)
        tokens = (await exchange(http, client, code, verifier)).json()
        clock.advance(ACCESS_TTL + 1)
        assert (await mcp_list(http, tokens["access_token"])).status_code == 401
        refreshed = await token_request(http, client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        assert refreshed.status_code == 200
        new = refreshed.json()

        # a rotated refresh token works within the grace window (lost response): the same tokens again ...
        again = await token_request(http, client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        assert again.status_code == 200
        assert again.json()["refresh_token"] == new["refresh_token"]
        assert again.json()["access_token"] == new["access_token"]
        # ... but reusing it later revokes the whole connection
        clock.advance(REFRESH_GRACE + 1)
        reuse = await token_request(http, client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        assert reuse.status_code == 400
        assert (await mcp_list(http, new["access_token"])).status_code == 401
        assert (await mcp_list(http, again.json()["access_token"])).status_code == 401


@pytest.mark.asyncio
async def test_consent_rate_limit_deny_and_bad_requests(store):
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        client = await register(http)
        _, challenge = pkce()
        _, fields = await open_consent(http, client, challenge, state="deny-me")

        denied = await submit(http, fields, "", action="deny")
        assert denied.status_code == 303
        query = parse_qs(urlsplit(denied.headers["location"]).query)
        assert query["error"] == ["access_denied"] and query["state"] == ["deny-me"] and query["iss"] == [BASE]

        # 5 wrong codes close the sign-in request; 5 per 10 minutes overall, then even the right code waits
        _, fields = await open_consent(http, client, challenge)
        for _ in range(5):
            assert (await submit(http, fields, "AAAA-AAAA")).status_code == 400
        closed = await submit(http, fields, pairing_code(store))
        assert closed.status_code == 400 and "no longer valid" in closed.text
        _, fields = await open_consent(http, client, challenge)
        locked = await submit(http, fields, pairing_code(store))
        assert locked.status_code == 429 and "Too many" in locked.text and "connect unlock" in locked.text
        assert OAuthStore(store.root, clock=clock).sign_in_lock()["locked_until"] > clock.now
        clock.advance(oauth_mod.ATTEMPT_WINDOW + 1)
        _, fields = await open_consent(http, client, challenge)  # the pending request expired meanwhile
        assert (await submit(http, fields, pairing_code(store))).status_code == 303

        # a resource indicator for another server is refused with iss in the error redirect
        wrong_target = await start_authorize(http, client, challenge, resource="https://other.example/mcp")
        assert wrong_target.status_code == 302
        query = parse_qs(urlsplit(wrong_target.headers["location"]).query)
        assert query["error"] == ["invalid_target"] and query["iss"] == [BASE]

        # unknown / expired consent requests and foreign Host headers
        assert (await http.get("/oauth/consent?request=nope")).status_code == 400
        assert (await http.get("/oauth/consent?request=x", headers={"Host": "evil.example"})).status_code == 421
        big = await http.post("/oauth/consent", content=b"a=" + b"x" * 20000,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert big.status_code == 413


@pytest.mark.asyncio
async def test_client_id_metadata_document_flow(store):
    clock = FakeClock()
    url = "https://chatgpt.example/oauth/client.json"
    fetched: list[str] = []

    async def fetcher(target: str) -> dict[str, Any]:
        fetched.append(target)
        return {"client_id": url, "client_name": "ChatGPT", "redirect_uris": [CHATGPT_REDIRECT_URI],
                "token_endpoint_auth_method": "private_key_jwt",
                "token_endpoint_auth_methods_supported": ["private_key_jwt", "none"]}

    async with oauth_app(store, clock, cimd_fetcher=fetcher) as (setup, http):
        client = {"client_id": url}
        verifier, challenge = pkce()
        code = await authorize_code(http, store, client, challenge)
        tokens = await exchange(http, client, code, verifier)
        assert tokens.status_code == 200, tokens.text
        assert (await mcp_list(http, tokens.json()["access_token"])).status_code == 200
        assert fetched == [url]  # cached after the first fetch

    async with oauth_app(store, clock, cimd=False) as (setup, http):
        assert (await http.get("/.well-known/oauth-authorization-server")).json()[
            "client_id_metadata_document_supported"] is False
        _, challenge = pkce()
        refused = await start_authorize(http, {"client_id": url}, challenge)
        assert refused.status_code == 400  # unknown client: no redirect


@pytest.mark.asyncio
async def test_sdk_oauth_client_interoperates(store):
    """The MCP SDK's own OAuth client (discovery, DCR, PKCE, resource, RFC 9207 iss check)."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        transport = http._transport

        class Memory:
            def __init__(self) -> None:
                self.tokens = None
                self.client = None

            async def get_tokens(self):
                return self.tokens

            async def set_tokens(self, tokens):
                self.tokens = tokens

            async def get_client_info(self):
                return self.client

            async def set_client_info(self, info):
                self.client = info

        result: dict[str, AuthorizationCodeResult] = {}

        async def redirect_handler(url: str) -> None:  # the "browser": open the consent page, enter the code
            async with httpx2.AsyncClient(transport=transport) as browser:
                page = await browser.get(url)
                assert page.status_code == 302
                consent = await browser.get(page.headers["location"])
                fields = consent_fields(consent.text)
                done = await browser.post(f"{BASE}/oauth/consent",
                                          data={**fields, "code": pairing_code(store), "action": "approve"})
                assert done.status_code == 303
                q = parse_qs(urlsplit(done.headers["location"]).query)
                result["code"] = AuthorizationCodeResult(code=q["code"][0], state=q.get("state", [None])[0],
                                                         iss=q["iss"][0])

        async def callback_handler() -> AuthorizationCodeResult:
            return result["code"]

        memory = Memory()
        auth = OAuthClientProvider(
            server_url=MCP_URL,
            client_metadata=OAuthClientMetadata(client_name="SDK test", redirect_uris=[CHATGPT_REDIRECT_URI],
                                                grant_types=["authorization_code", "refresh_token"]),
            storage=memory, redirect_handler=redirect_handler, callback_handler=callback_handler,
        )
        client_http = httpx2.AsyncClient(transport=transport, auth=auth)
        async with Client(streamable_http_client(MCP_URL, http_client=client_http)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"ping"}
            assert "pong" in json.dumps((await client.call_tool("ping", {})).model_dump(mode="json"))
        assert memory.tokens is not None and memory.tokens.access_token.startswith("ppat_")


# ---------------------------------------------------------------------- after the wire-in


@pytest.mark.asyncio
async def test_wired_into_serve_http(store):
    """``serve --http --auth oauth`` end to end (skipped until docs/design/WIRE-IN.md "ChatGPT" is applied)."""
    import typing

    from profilepilot.server import http as http_mod

    if "oauth" not in typing.get_args(getattr(http_mod, "AuthMode", typing.Literal["x"])):
        pytest.skip("--auth oauth is not wired into server/http.py yet (docs/design/WIRE-IN.md, ChatGPT)")
    plan = http_mod.build_http_app(store=store, auth="oauth", public_hosts=["tunnel.example"], log_level="WARNING")
    assert f"{BASE}/mcp" in plan.urls
    banner = http_mod.describe_plan(plan)
    assert pairing_code(store) in banner and "OAuth" in banner
    inner = getattr(plan.app, "app", plan.app)
    async with inner.router.lifespan_context(inner):
        transport = httpx2.ASGITransport(app=plan.app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
            assert (await http.post("/mcp", json=LIST_TOOLS, headers=ACCEPT)).status_code == 401
            meta = (await http.get("/.well-known/oauth-authorization-server")).json()
            assert meta["issuer"] == BASE and meta["authorization_response_iss_parameter_supported"] is True
            client = await register(http)
            verifier, challenge = pkce()
            code = await authorize_code(http, store, client, challenge)
            tokens = (await exchange(http, client, code, verifier)).json()
            listed = await mcp_list(http, tokens["access_token"])
            assert listed.status_code == 200 and "profile_list" in listed.text


@pytest.mark.asyncio
async def test_claude_code_loopback_redirect_on_any_port(store):
    """Claude Code's client metadata document lists http://localhost/callback without a port and
    signs in on a random port (RFC 8252 7.3): any port matches, nothing else does."""
    clock = FakeClock()
    url = "https://claude.ai/oauth/claude-code-client-metadata"

    async def fetcher(target: str) -> dict[str, Any]:
        return {"client_id": url, "client_name": "Claude Code", "token_endpoint_auth_method": "none",
                "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}

    async with oauth_app(store, clock, cimd_fetcher=fetcher) as (setup, http):
        verifier, challenge = pkce()

        def authorize_url(redirect_uri: str) -> str:
            return "/authorize?" + urlencode({
                "response_type": "code", "client_id": url, "redirect_uri": redirect_uri, "state": "cc",
                "code_challenge": challenge, "code_challenge_method": "S256", "resource": MCP_URL})

        for bad in ("http://localhost:3118/other", "http://evil.example:3118/callback",
                    "https://localhost:3118/callback"):
            assert (await http.get(authorize_url(bad))).status_code == 400, bad
        redirect = "http://localhost:3118/callback"
        response = await http.get(authorize_url(redirect))
        assert response.status_code == 302
        page = await http.get(response.headers["location"])
        assert "a program on this computer" in page.text and "Claude Code" in page.text
        assert "form-action 'self' http://localhost:3118" in page.headers["content-security-policy"]
        approved = await submit(http, consent_fields(page.text), pairing_code(store))
        location = approved.headers["location"]
        assert location.startswith(redirect + "?")
        code = parse_qs(urlsplit(location).query)["code"][0]
        tokens = await http.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": url,
                                                 "redirect_uri": redirect, "code_verifier": verifier,
                                                 "resource": MCP_URL})
        assert tokens.status_code == 200, tokens.text
        assert (await mcp_list(http, tokens.json()["access_token"])).status_code == 200


# ---------------------------------------------------------------------- security regressions


def browser(http: httpx2.AsyncClient) -> httpx2.AsyncClient:
    """Another browser (its own cookie jar) on the same server."""
    return httpx2.AsyncClient(transport=http._transport, base_url=BASE)


async def wrong_codes(http: httpx2.AsyncClient, client: dict[str, Any], count: int) -> list[int]:
    """``count`` wrong pairing codes, on as few sign-in requests as the per-request limit allows."""
    statuses: list[int] = []
    fields: dict[str, str] | None = None
    for i in range(count):
        if i % oauth_mod.REQUEST_ATTEMPT_LIMIT == 0:
            _, fields = await open_consent(http, client, pkce()[1])
        assert fields is not None
        statuses.append((await submit(http, fields, "ZZZZ-ZZZZ")).status_code)
    return statuses


@pytest.mark.asyncio
async def test_owner_unlocks_after_someone_used_up_the_attempts(store):
    """Finding 3: wrong codes from someone who knows the tunnel URL must not lock the owner out for
    good: `connect unlock` (local) makes a new code that is checked whatever the limiter says."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        attacker = await register(http, client_name="attacker")
        assert await wrong_codes(http, attacker, oauth_mod.ATTEMPT_LIMIT) == [400] * oauth_mod.ATTEMPT_LIMIT
        async with browser(http) as owner_browser:
            owner = await register(owner_browser)
            old_code = pairing_code(store)
            _, fields = await open_consent(owner_browser, owner, pkce()[1])
            locked = await submit(owner_browser, fields, old_code)
            assert locked.status_code == 429 and "profilepilot connect unlock" in locked.text
            status = oauth_mod.oauth_status(store)
            assert status["locked_until"] and status["unlock_until"] is None

            new_code = oauth_mod.unlock_sign_in(store, clock=clock)
            assert new_code != old_code and pairing_code(store) == new_code
            # wrong codes are not counted against everyone in the unlock window, so its code is long
            # enough that unlimited guessing for UNLOCK_TTL seconds is hopeless (~59 bits)
            assert len(normalize_pairing_code(new_code)) == oauth_mod.UNLOCK_CODE_LENGTH >= 12
            assert oauth_mod.oauth_status(store)["unlock_until"] and not oauth_mod.oauth_status(store)["locked_until"]
            # the attacker keeps guessing during the unlock window: the owner still gets through
            assert set(await wrong_codes(http, attacker, 12)) <= {400}
            assert (await submit(owner_browser, fields, old_code)).status_code == 400  # the old code is dead
            approved = await submit(owner_browser, fields, new_code.lower().replace("-", " "))
            assert approved.status_code == 303 and "code=" in approved.headers["location"]
        # the unlock ends with that sign-in (back to a normal code); the attempts are used up again
        assert oauth_mod.oauth_status(store)["unlock_until"] is None
        assert len(normalize_pairing_code(pairing_code(store))) == oauth_mod.PAIRING_LENGTH
        _, fields = await open_consent(http, attacker, pkce()[1])
        assert (await submit(http, fields, pairing_code(store))).status_code == 429


@pytest.mark.asyncio
async def test_unlock_only_skips_the_limit_for_its_own_long_code(store):
    """The unlock window lets wrong codes go uncounted only while its long code is in effect. When
    that code is replaced by a normal 8-character one (the server restarted and made a new code,
    `connect stop --revoke`, ...), the normal limit applies again, also to concurrent guesses."""
    import asyncio

    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        attacker = await register(http, client_name="attacker")
        oauth_mod.unlock_sign_in(store, clock=clock)
        oauth_mod.rotate_pairing_code(store)  # e.g. `serve --auth oauth` restarted
        assert oauth_mod.oauth_status(store)["unlock_until"]  # the window is still recorded ...
        browsers = [browser(http) for _ in range(5)]
        try:
            forms = [(b, (await open_consent(b, attacker, pkce()[1]))[1]) for b in browsers]
            responses = await asyncio.gather(*(submit(b, f, "ZZZZ-ZZZZ") for b, f in forms for _ in range(4)))
        finally:
            for b in browsers:
                await b.aclose()
        statuses = [r.status_code for r in responses]
        wrong_checked = sum(1 for r in responses if r.status_code == 400 and "not right" in r.text)
        assert wrong_checked == oauth_mod.ATTEMPT_LIMIT, statuses  # ... but it no longer lifts the limit
        assert statuses.count(429) == len(responses) - oauth_mod.ATTEMPT_LIMIT, statuses
        _, fields = await open_consent(http, attacker, pkce()[1])
        assert (await submit(http, fields, pairing_code(store))).status_code == 429  # not even checked
        assert oauth_mod.oauth_status(store)["locked_until"]
        # the owner's way out still works: a new unlock gives a new long code
        code = oauth_mod.unlock_sign_in(store, clock=clock)
        assert (await submit(http, fields, code)).status_code == 303


@pytest.mark.asyncio
async def test_concurrent_guesses_cannot_pass_the_limit(store):
    """Finding 4: the limit is checked and counted in one step, so a burst of concurrent guesses gets
    at most ATTEMPT_LIMIT codes checked (the right one among them is refused like the others)."""
    import asyncio

    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        client = await register(http)
        browsers = [browser(http) for _ in range(12)]  # one sign-in each (the CSRF cookie is per browser)
        forms = [(b, (await open_consent(b, client, pkce()[1]))[1]) for b in browsers]
        right = pairing_code(store)
        guesses = []
        for i in range(60):
            code = right if i == 30 else oauth_mod.new_pairing_code()
            if i != 30 and normalize_pairing_code(code) == normalize_pairing_code(right):
                code = "ZZZZ-ZZZZ"
            guesses.append((*forms[i % len(forms)], code))
        try:
            responses = await asyncio.gather(*(submit(b, f, c) for b, f, c in guesses))
        finally:
            for b in browsers:
                await b.aclose()
        statuses = [r.status_code for r in responses]
        wrong_checked = sum(1 for r in responses if r.status_code == 400 and "not right" in r.text)
        assert wrong_checked <= oauth_mod.ATTEMPT_LIMIT, statuses
        assert statuses.count(429) >= len(guesses) - oauth_mod.ATTEMPT_LIMIT - 1, statuses
        if statuses[30] != 303:  # the right code was not among the first five: it was not even checked
            assert statuses[30] == 429


@pytest.mark.asyncio
async def test_concurrent_guesses_on_one_request_respect_its_limit(store):
    """Finding 4 (per request): a sign-in request's own limit is also counted before the await, so a
    burst on one consent link gets at most REQUEST_ATTEMPT_LIMIT codes checked, even during an
    unlock window (when the global limit does not count)."""
    import asyncio

    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        client = await register(http)
        _, fields = await open_consent(http, client, pkce()[1])
        oauth_mod.unlock_sign_in(store, clock=clock)
        provider = setup.provider
        checked: list[float] = []
        original = provider._wrong_code

        async def counting(pending: Any, now: float, **kw: Any) -> None:
            checked.append(now)
            await original(pending, now, **kw)

        provider._wrong_code = counting  # type: ignore[method-assign]
        responses = await asyncio.gather(*(submit(http, fields, "ZZZZ-ZZZZ") for _ in range(20)))
        assert len(checked) == oauth_mod.REQUEST_ATTEMPT_LIMIT
        assert {r.status_code for r in responses} == {400}
        assert any("This sign-in request is closed" in r.text for r in responses)
        # the request is closed: the right code does nothing on it any more ...
        assert (await submit(http, fields, pairing_code(store))).status_code == 400
        # ... but a new sign-in works (the unlock is still on)
        _, fresh = await open_consent(http, client, pkce()[1])
        assert (await submit(http, fresh, pairing_code(store))).status_code == 303


@pytest.mark.asyncio
async def test_clients_registered_before_the_redirect_allowlist(store):
    """Finding 6 (stored clients): redirect URIs saved by an older version that the allowlist now
    refuses are ignored, both for sign-in and for error redirects."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        mixed = await register(http, redirect_uris=["https://ok.example/cb"], client_name="mixed")
        only_bad = await register(http, redirect_uris=["https://ok.example/cb2"], client_name="bad")
        bad_uri = r"search-ms:query=x&crumb=location:\\evil.example\share"

        def legacy(data: dict[str, Any]) -> None:
            data["clients"][mixed["client_id"]]["info"]["redirect_uris"] = ["https://ok.example/cb", bad_uri]
            data["clients"][only_bad["client_id"]]["info"]["redirect_uris"] = ["ms-officecmd:{}"]
            data["grants"]["g"] = {"client_id": mixed["client_id"], "created_at": clock.now}
            data["access"]["h"] = {"grant_id": "g", "expires_at": clock.now + 3600}

        OAuthStore(store.root, clock=clock).mutate(legacy)
        info = await setup.provider.get_client(mixed["client_id"])
        assert info is not None and [str(u) for u in info.redirect_uris or []] == ["https://ok.example/cb"]
        assert await setup.provider.get_client(only_bad["client_id"]) is None
        assert setup.provider.trusted_redirect("https://ok.example/cb?error=x")  # approved before
        assert not setup.provider.trusted_redirect(bad_uri + "&error=x")
        params = {"response_type": "code", "client_id": mixed["client_id"], "redirect_uri": bad_uri,
                  "code_challenge": pkce()[1], "code_challenge_method": "S256", "state": "s", "resource": MCP_URL}
        refused = await http.get("/authorize?" + urlencode(params))
        assert refused.status_code == 400 and "location" not in refused.headers


@pytest.mark.asyncio
async def test_authorize_flood_cannot_evict_the_owners_sign_in(store):
    """Finding 3: consent links are signed, not stored, so no number of /authorize calls pushes the
    owner's sign-in out; tampered, foreign and expired links are refused."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        owner = await register(http)
        _, fields = await open_consent(http, owner, pkce()[1])
        attacker = await register(http, client_name="x")
        for _ in range(300):
            assert (await start_authorize(http, attacker, pkce()[1])).status_code == 302
        page = await http.get(f"/oauth/consent?request={fields['request']}")
        assert page.status_code == 200 and "Allow ChatGPT" in page.text

        blob, signature = fields["request"].rsplit(".", 1)
        forged_body = json.loads(oauth_mod._unb64(blob))
        forged_body["r"] = "https://evil.example/cb"
        forged = oauth_mod._b64(json.dumps(forged_body).encode()) + "." + signature
        for bad in (forged, blob + ".x", "x" * 9000, "nonsense"):
            assert (await http.get("/oauth/consent?" + urlencode({"request": bad}))).status_code == 400
        other = build_oauth(store, BASE, clock=clock).provider  # another server process: another key
        assert other.pending(fields["request"]) is None

        clock.advance(oauth_mod.PENDING_TTL + 1)
        assert (await http.get(f"/oauth/consent?request={fields['request']}")).status_code == 400


@pytest.mark.asyncio
async def test_metadata_fetch_budgets(store):
    """Finding 3: junk client_id URLs cannot use up ChatGPT's client metadata fetches."""
    clock = FakeClock()
    fetched: list[str] = []

    async def fetcher(url: str) -> dict[str, Any]:
        fetched.append(url)
        if "junk" in url:
            raise ValueError("HTTP 404")
        return {"client_id": url, "client_name": "ChatGPT", "redirect_uris": [CHATGPT_REDIRECT_URI],
                "token_endpoint_auth_method": "none"}

    async with oauth_app(store, clock, cimd_fetcher=fetcher) as (setup, http):
        # malformed URLs are refused before any budget is spent (and never fetched)
        for i in range(50):
            await start_authorize(http, {"client_id": f"https://nodots{i}/c"}, pkce()[1])
        assert fetched == []
        # junk on many other hosts: they share one budget, which ChatGPT's host does not use
        for i in range(80):
            await start_authorize(http, {"client_id": f"https://junk{i}.example/c.json"}, pkce()[1])
        assert len(fetched) == oauth_mod.CIMD_GLOBAL_FETCHES_PER_MINUTE
        legit = "https://chatgpt.com/oauth/profilepilot/client.json"
        ok = await start_authorize(http, {"client_id": legit}, pkce()[1])
        assert ok.status_code == 302 and "/oauth/consent?request=" in ok.headers["location"]
        assert fetched[-1] == legit
        # one other host cannot even use all of the shared budget
        fetched.clear()
        clock.advance(61)
        for i in range(30):
            await start_authorize(http, {"client_id": f"https://junk.example/c{i}.json"}, pkce()[1])
        assert len(fetched) == oauth_mod.CIMD_FETCHES_PER_MINUTE
        # a document that worked before is refreshed even when its host's budget is used up
        clock.advance(oauth_mod.CIMD_CACHE_TTL + 1)
        for i in range(40):
            await start_authorize(http, {"client_id": f"https://chatgpt.com/junk{i}"}, pkce()[1])
        fetched.clear()
        assert (await start_authorize(http, {"client_id": legit}, pkce()[1])).status_code == 302
        assert fetched == [legit]


@pytest.mark.asyncio
async def test_registration_budgets(store):
    """Finding 3: junk dynamic registrations cannot block Claude's (or ChatGPT's) registration."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        codes = []
        for i in range(oauth_mod.MAX_REGISTRATIONS_PER_HOUR + 5):
            response = await http.post("/register", json={"redirect_uris": [f"https://junk{i}.example/cb"],
                                                          "client_name": f"junk{i}"})
            codes.append(response.status_code)
        assert codes.count(201) == oauth_mod.MAX_REGISTRATIONS_PER_HOUR and codes[-1] == 400
        claude = await http.post("/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                                                     "client_name": "Claude"})
        assert claude.status_code == 201, claude.text
        assert (await http.post("/register", json={"redirect_uris": [CHATGPT_REDIRECT_URI],
                                                   "client_name": "ChatGPT"})).status_code == 201
        # one host cannot use up the shared budget alone either
        clock.advance(3601)
        same = [(await http.post("/register", json={"redirect_uris": ["https://one.example/cb"]})).status_code
                for _ in range(oauth_mod.REGISTRATIONS_PER_HOST_PER_HOUR + 1)]
        assert same[-1] == 400 and same.count(201) == oauth_mod.REGISTRATIONS_PER_HOST_PER_HOUR
        assert (await http.post("/register", json={"redirect_uris": ["https://two.example/cb"]})).status_code == 201


@pytest.mark.asyncio
async def test_refresh_replay_gets_the_same_tokens(store):
    """Finding 5: replaying a just-rotated refresh token never mints an independent token family."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        client = await register(http)
        verifier, challenge = pkce()
        code = await authorize_code(http, store, client, challenge)
        first = (await exchange(http, client, code, verifier)).json()

        def refresh(token: str):
            return token_request(http, client, grant_type="refresh_token", refresh_token=token)

        legit = (await refresh(first["refresh_token"])).json()
        clock.advance(10)
        stolen = [(await refresh(first["refresh_token"])).json() for _ in range(3)]
        assert {s["refresh_token"] for s in stolen} == {legit["refresh_token"]}
        assert {s["access_token"] for s in stolen} == {legit["access_token"]}
        assert all(s["expires_in"] <= ACCESS_TTL - 10 for s in stolen)
        assert len(OAuthStore(store.root, clock=clock).read()["refresh"]) == 1  # one live family
        raw = (store.root / "oauth.json").read_text(encoding="utf-8")
        assert legit["refresh_token"] not in raw and first["refresh_token"] not in raw

        # after the grace window the salt is gone and the first token is spent for good
        clock.advance(oauth_mod.REFRESH_GRACE + 1)
        stale_used = await refresh(first["refresh_token"])
        assert stale_used.status_code == 400
        assert all("salt" not in r for r in OAuthStore(store.root, clock=clock).read()["used_refresh"].values())
        # ... and that reuse signed the whole connection out (both copies of the successor are dead)
        assert (await refresh(legit["refresh_token"])).status_code == 400
        assert (await mcp_list(http, legit["access_token"])).status_code == 401


@pytest.mark.asyncio
async def test_refresh_after_both_parties_used_the_successor(store):
    """Finding 5: whoever uses the shared successor second (after its grace window) revokes it all."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        client = await register(http)
        verifier, challenge = pkce()
        first = (await exchange(http, client, await authorize_code(http, store, client, challenge), verifier)).json()

        def refresh(token: str):
            return token_request(http, client, grant_type="refresh_token", refresh_token=token)

        legit = (await refresh(first["refresh_token"])).json()
        stolen = (await refresh(first["refresh_token"])).json()  # replay within the grace window
        attacker_next = (await refresh(stolen["refresh_token"])).json()  # the attacker moves first
        clock.advance(oauth_mod.REFRESH_GRACE + 1)
        assert (await refresh(legit["refresh_token"])).status_code == 400  # the owner's app comes back later
        assert (await mcp_list(http, attacker_next["access_token"])).status_code == 401
        assert (await refresh(attacker_next["refresh_token"])).status_code == 400


@pytest.mark.asyncio
async def test_refresh_tokens_are_bound_to_the_server_url(store):
    """Finding 9: a refresh token approved for one tunnel address does not work at another."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        client = await register(http)
        verifier, challenge = pkce()
        tokens = (await exchange(http, client, await authorize_code(http, store, client, challenge), verifier)).json()
    moved = build_oauth(store, "https://new-tunnel.example", clock=clock).provider
    info = await moved.get_client(client["client_id"])
    assert info is not None
    assert await moved.load_refresh_token(info, tokens["refresh_token"]) is None
    assert await moved.load_access_token(tokens["access_token"]) is None
    same = build_oauth(store, BASE, clock=clock).provider
    assert await same.load_refresh_token(info, tokens["refresh_token"]) is not None
    assert OAuthStore(store.root, clock=clock).grants()[0]["resource"] == MCP_URL


@pytest.mark.asyncio
async def test_no_open_redirect(store):
    """Finding 6: error and deny redirects only go to approved / first-party / loopback clients."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        evil = await register(http, redirect_uris=["https://evil.example/landing"], client_name="x")
        for scheme_uri in ("search-ms:query=x", "ms-officecmd:{}", "smb://evil.example/share"):
            bad = await http.post("/register", json={"redirect_uris": [scheme_uri], "client_name": "x"})
            assert bad.status_code == 400 and bad.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")
        params = {"response_type": "code", "client_id": evil["client_id"],
                  "redirect_uri": "https://evil.example/landing", "code_challenge": pkce()[1],
                  "code_challenge_method": "S256", "state": "s", "resource": "https://other.example/"}
        refused = await http.get("/authorize?" + urlencode(params))
        assert refused.status_code == 400 and "location" not in refused.headers
        assert "invalid_target" in refused.text and "evil.example" not in refused.headers.get("location", "")
        assert "frame-ancestors 'none'" in refused.headers["content-security-policy"]
        bad_scope = await http.get("/authorize?" + urlencode({**params, "resource": MCP_URL, "scope": "admin"}))
        assert bad_scope.status_code == 400 and "location" not in bad_scope.headers
        assert "invalid_scope" in bad_scope.text

        # "Deny" for an app the user never approved: a page, not a redirect
        params["resource"] = MCP_URL
        response = await http.get("/authorize?" + urlencode(params))
        page = await http.get(response.headers["location"])
        denied = await submit(http, consent_fields(page.text), "", action="deny")
        assert denied.status_code == 200 and "You denied the request" in denied.text
        assert "location" not in denied.headers

        # once the user approved the client (it has a connection), its errors go back to it
        verifier, challenge = pkce()
        response = await http.get("/authorize?" + urlencode({**params, "code_challenge": challenge}))
        page = await http.get(response.headers["location"])
        approved = await submit(http, consent_fields(page.text), pairing_code(store))
        assert approved.status_code == 303
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        assert (await token_request(http, evil, grant_type="authorization_code", code=code,
                                    redirect_uri="https://evil.example/landing",
                                    code_verifier=verifier)).status_code == 200
        again = await http.get("/authorize?" + urlencode({**params, "resource": "https://other.example/"}))
        assert again.status_code == 302 and again.headers["location"].startswith("https://evil.example/landing?")

        # ChatGPT (first party) always gets its errors and denials back
        chatgpt = await register(http)
        _, fields = await open_consent(http, chatgpt, pkce()[1])
        assert (await submit(http, fields, "", action="deny")).status_code == 303


def test_consent_trust_signals():
    """Finding 11: the product badge only for exact known redirect URIs; the "did you start this"
    warning and the request's age always; the code is only ever asked for on this page."""
    def pending(uri: str, created: float = 1000.0) -> Any:
        return oauth_mod.PendingAuthorization(
            request_id="r", client_id="c", client_name="ChatGPT", redirect_uri=uri,
            redirect_uri_provided_explicitly=True, state=None, scopes=["profilepilot"], code_challenge="c",
            resource=MCP_URL, csrf="t", created_at=created)

    exact = render_consent_page(pending(CHATGPT_REDIRECT_URI), nonce="n", now=1000.0)
    assert '<span class="badge">ChatGPT</span>' in exact
    assert "Only approve if you started connecting ChatGPT" in exact and "just now" in exact
    assert "never asks for it anywhere else" in exact
    lookalike = render_consent_page(pending("https://chatgpt.com/share/attacker"), nonce="n", now=1000.0 + 420)
    assert 'class="badge"' not in lookalike and "not ChatGPT or Claude" in lookalike
    assert "7 minutes ago" in lookalike
    with_query = render_consent_page(pending(CHATGPT_REDIRECT_URI + "?next=x"), nonce="n", now=1000.0)
    assert 'class="badge"' not in with_query
    claude = render_consent_page(pending("https://claude.ai/api/mcp/auth_callback"), nonce="n", now=1000.0)
    assert '<span class="badge">Claude</span>' in claude


@pytest.mark.asyncio
async def test_metadata_fetch_connects_to_the_checked_address(monkeypatch):
    """Minor note: the client metadata fetch connects to the address it checked (no DNS rebinding
    between check and connect); TLS still verifies the real host name."""
    import httpx

    seen: list[httpx.Request] = []

    async def resolve(host: str, port: int) -> list[str]:
        assert host == "client.example" and port == 443
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"client_id": "https://client.example/c.json", "redirect_uris": []})

    monkeypatch.setattr(oauth_mod, "_resolve_public", resolve)
    document = await oauth_mod.fetch_client_metadata_document("https://client.example/c.json",
                                                              transport=httpx.MockTransport(handler))
    assert document["client_id"] == "https://client.example/c.json"
    request = seen[0]
    assert request.url.host == "93.184.216.34" and request.url.path == "/c.json"
    assert request.headers["host"] == "client.example"
    assert request.extensions["sni_hostname"] == "client.example"

    def private(*_a: Any, **_k: Any) -> list[Any]:
        return [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.5", 443))]

    monkeypatch.undo()
    monkeypatch.setattr(oauth_mod.socket, "getaddrinfo", private)
    with pytest.raises(ValueError, match="private"):
        await oauth_mod._resolve_public("client.example", 443)


def test_serve_rotates_the_code_and_hides_it_from_logs(store):
    """Finding 12: `serve --auth oauth` makes a new pairing code at start, and the banner only shows
    it on an interactive terminal."""
    import io

    from profilepilot.server import http as http_mod

    before = pairing_code(store)
    plan = http_mod.build_http_app(store=store, auth="oauth", public_hosts=["tunnel.example"], log_level="WARNING")
    now = pairing_code(store)
    assert now != before
    assert now in http_mod.describe_plan(plan)
    hidden = http_mod.describe_plan(plan, show_pairing_code=False)
    assert now not in hidden and "connect status" in hidden
    assert http_mod._is_terminal(io.StringIO()) is False
    assert http_mod._is_terminal(None) is False


# ---------------------------------------------------------------------- security review fixes


@pytest.mark.asyncio
async def test_iss_in_a_registered_redirect_uri_does_not_skip_the_redirect_guard(store):
    """Fix 1: a redirect URI that already has an ``iss`` parameter gets the open-redirect guard like
    any other, and a client only ever sees this server's ``iss``."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        landing = "https://evil.example/landing?iss=attacker"
        evil = await register(http, redirect_uris=[landing], client_name="x")
        params = {"response_type": "code", "client_id": evil["client_id"], "redirect_uri": landing,
                  "code_challenge": pkce()[1], "code_challenge_method": "S256", "state": "s",
                  "resource": "https://other.example/"}
        refused = await http.get("/authorize?" + urlencode(params))
        assert refused.status_code == 400 and "location" not in refused.headers
        assert "invalid_target" in refused.text

        # approved: codes and errors go back to it, each with exactly one iss (this server's)
        verifier, challenge = pkce()
        started = await http.get("/authorize?" + urlencode({**params, "resource": MCP_URL,
                                                             "code_challenge": challenge}))
        page = await http.get(started.headers["location"])
        approved = await submit(http, consent_fields(page.text), pairing_code(store))
        assert approved.status_code == 303
        query = parse_qs(urlsplit(approved.headers["location"]).query)
        assert query["iss"] == [BASE]
        assert (await token_request(http, evil, grant_type="authorization_code", code=query["code"][0],
                                    redirect_uri=landing, code_verifier=verifier)).status_code == 200
        again = await http.get("/authorize?" + urlencode(params))
        assert again.status_code == 302 and again.headers["location"].startswith("https://evil.example/landing?")
        query = parse_qs(urlsplit(again.headers["location"]).query)
        assert query["iss"] == [BASE] and query["error"] == ["invalid_target"]


@pytest.mark.asyncio
async def test_first_party_subdomains_do_not_get_their_own_registration_budget(store):
    """Fix 2: only ChatGPT's and Claude's own hosts have a first-party budget; their subdomains share
    the budget of every other host, so registrations stay limited (and Claude's own is untouched)."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        codes = [(await http.post("/register", json={"redirect_uris": [f"https://x{i}.claude.ai/cb"]})).status_code
                 for i in range(2 * oauth_mod.FIRST_PARTY_REGISTRATIONS_PER_HOUR)]
        assert codes.count(201) == oauth_mod.MAX_REGISTRATIONS_PER_HOUR
        assert (await http.post("/register", json={"redirect_uris": ["https://x.chatgpt.com/cb"]})).status_code == 400
        claude = await http.post("/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                                                     "client_name": "Claude"})
        assert claude.status_code == 201, claude.text
    # with every budget used up, fewer clients register within a sign-in than the table holds
    per_hour = (len(oauth_mod.FIRST_PARTY_HOSTS) * oauth_mod.FIRST_PARTY_REGISTRATIONS_PER_HOUR
                + oauth_mod.MAX_REGISTRATIONS_PER_HOUR)
    assert per_hour * oauth_mod.SIGN_IN_WINDOW / 3600 < oauth_mod.MAX_CLIENTS / 2


@pytest.mark.asyncio
async def test_registration_flood_cannot_evict_a_client_that_is_signing_in(store, monkeypatch):
    """Fix 2: making room for new registrations never evicts a client registered moments ago (it is
    about to sign in), and evicts the clients idle longest first, not one that just started signing
    in again; when every client is new or connected, a registration is refused instead."""
    monkeypatch.setattr(oauth_mod, "MAX_CLIENTS", 10)
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        early = await register(http, client_name="Claude Code")  # registered long ago, signs in again now
        for i in range(6):
            old = await http.post("/register", json={"redirect_uris": [f"https://old{i}.example/cb"]})
            assert old.status_code == 201
        clock.advance(oauth_mod.SIGN_IN_WINDOW + 3600)
        early_verifier, early_challenge = pkce()
        _, early_fields = await open_consent(http, early, early_challenge)
        fresh = await register(http)  # registers and signs in right away
        fresh_verifier, fresh_challenge = pkce()
        _, fresh_fields = await open_consent(http, fresh, fresh_challenge)

        # 2 free places, then the 6 old idle clients make room
        for i in range(8):
            junk = await http.post("/register", json={"redirect_uris": [f"https://junk{i}.example/cb"]})
            assert junk.status_code == 201
        clients = OAuthStore(store.root, clock=clock).read()["clients"]
        assert early["client_id"] in clients and fresh["client_id"] in clients and len(clients) == 10

        async def finish(fields: dict[str, str], client: dict[str, Any], verifier: str) -> httpx2.Response:
            page = await http.get(f"/oauth/consent?request={fields['request']}")  # its CSRF cookie
            response = await submit(http, consent_fields(page.text), pairing_code(store))
            assert response.status_code == 303, response.text
            code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
            return await exchange(http, client, code, verifier)

        assert (await finish(early_fields, early, early_verifier)).status_code == 200
        assert (await finish(fresh_fields, fresh, fresh_verifier)).status_code == 200
        # now every client is either connected or new: nobody is evicted, the registration is refused
        full = await http.post("/register", json={"redirect_uris": ["https://late.example/cb"]})
        assert full.status_code == 400 and "Too many" in full.text
        # once the junk is no longer new it makes room again; connected clients stay
        clock.advance(oauth_mod.SIGN_IN_WINDOW + 1)
        assert (await http.post("/register", json={"redirect_uris": [CHATGPT_REDIRECT_URI]})).status_code == 201
        clients = OAuthStore(store.root, clock=clock).read()["clients"]
        assert early["client_id"] in clients and fresh["client_id"] in clients


@pytest.mark.asyncio
async def test_metadata_fetch_budget_is_per_first_party_site(store):
    """Fix 2 (same cause): subdomains of a first-party host share that site's metadata fetch budget."""
    clock = FakeClock()
    fetched: list[str] = []

    async def fetcher(url: str) -> dict[str, Any]:
        fetched.append(url)
        raise ValueError("HTTP 404")

    async with oauth_app(store, clock, cimd_fetcher=fetcher) as (setup, http):
        for i in range(3 * oauth_mod.CIMD_FIRST_PARTY_FETCHES_PER_MINUTE):
            await start_authorize(http, {"client_id": f"https://x{i}.openai.com/c.json"}, pkce()[1])
        assert len(fetched) == oauth_mod.CIMD_FIRST_PARTY_FETCHES_PER_MINUTE


def test_serve_http_hides_the_pairing_code_from_a_redirected_stderr(store, monkeypatch):
    """Fix 3: `serve --auth oauth` (which announces through the CLI) only shows the pairing code when
    stderr is an interactive terminal; otherwise it says where to find it."""
    import io
    import sys

    import anyio

    from profilepilot.server import http as http_mod

    class Stderr(io.StringIO):
        def __init__(self, terminal: bool) -> None:
            super().__init__()
            self.terminal = terminal

        def isatty(self) -> bool:
            return self.terminal

    monkeypatch.setattr(anyio, "run", lambda *a, **k: None)  # build and announce, but do not serve
    for terminal in (False, True):
        stderr = Stderr(terminal)
        monkeypatch.setattr(sys, "stderr", stderr)
        banners: list[str] = []
        http_mod.serve_http(auth="oauth", public_hosts=["tunnel.example"], root=store.root, log_level="WARNING",
                            announce=banners.append)
        code = pairing_code(Store(store.root))
        if terminal:
            assert code in banners[0]
        else:
            assert code not in banners[0] and "profilepilot connect status" in banners[0]
            assert code not in stderr.getvalue()


@pytest.mark.asyncio
async def test_metadata_fetch_tries_each_checked_address(monkeypatch):
    """Fix 4: the client metadata fetch falls back to the next checked address (IPv6 and IPv4
    alternating) when one cannot be reached, never to an address that was not checked; an address
    that answers (even with an error) ends the search."""
    import httpx

    checked = ["2001:db8::1", "2001:db8::2", "93.184.216.34", "93.184.216.35"]
    tried: list[str] = []
    answer: dict[str, int] = {}

    async def resolve(host: str, port: int) -> list[str]:
        return list(checked)

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        assert request.headers["host"] == "client.example"
        if request.url.host not in answer:
            raise httpx.ConnectError("unreachable", request=request)
        return httpx.Response(answer[request.url.host], json={"client_id": "https://client.example/c.json"})

    monkeypatch.setattr(oauth_mod, "_resolve_public", resolve)
    url = "https://client.example/c.json"
    answer = {"2001:db8::2": 200}
    document = await oauth_mod.fetch_client_metadata_document(url, transport=httpx.MockTransport(handler))
    assert document["client_id"] == url
    assert tried == ["2001:db8::1", "93.184.216.34", "2001:db8::2"]

    tried.clear()
    answer = {"93.184.216.34": 404, "2001:db8::2": 200}
    with pytest.raises(ValueError, match="404"):
        await oauth_mod.fetch_client_metadata_document(url, transport=httpx.MockTransport(handler))
    assert tried == ["2001:db8::1", "93.184.216.34"]

    tried.clear()
    answer = {}
    with pytest.raises(httpx.ConnectError):
        await oauth_mod.fetch_client_metadata_document(url, transport=httpx.MockTransport(handler))
    assert sorted(tried) == sorted(checked)


@pytest.mark.asyncio
async def test_registration_metadata_is_limited(store):
    """Fix 5: registration is unauthenticated and oauth.json is re-read on every OAuth write, so
    oversized client metadata (long fields, long lists, big documents) is refused, not stored."""
    clock = FakeClock()
    async with oauth_app(store, clock) as (setup, http):
        base = {"redirect_uris": ["https://app.example/cb"], "client_name": "app"}
        oversized = [
            {"contacts": [f"c{i}@example.com" for i in range(50)]},
            {"contacts": ["a" * 5000 + "@example.com"]},
            {"software_id": "x" * 5000},
            {"scope": "profilepilot " + "s" * 5000},
            {"grant_types": ["authorization_code", *(f"urn:x:{i}" for i in range(50))]},
            {"jwks": {"keys": [{"kty": "oct", "k": "A" * 1000, "kid": str(i)} for i in range(10)]}},
            {"client_uri": "https://app.example/" + "p" * 3000},
        ]
        for extra in oversized:
            response = await http.post("/register", json={**base, **extra})
            assert response.status_code == 400, (list(extra), response.text)
            assert response.json()["error"] == "invalid_client_metadata"
        huge = await http.post("/register", content=json.dumps({**base, "padding": "x" * 200_000}),
                               headers={"Content-Type": "application/json"})
        assert huge.status_code == 413
        assert OAuthStore(store.root, clock=clock).read()["clients"] == {}
        ok = await http.post("/register", json={**base, "contacts": ["me@example.com"], "scope": "profilepilot",
                                                "software_id": "app", "software_version": "1.2.3"})
        assert ok.status_code == 201, ok.text
        raw = (store.root / "oauth.json").read_bytes()
        assert len(raw) < 4 * 1024
