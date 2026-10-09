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


def test_public_urls_and_redirect_validation(store):
    assert public_base_url("127.0.0.1", 8931, ["Abc.trycloudflare.com"]) == "https://abc.trycloudflare.com"
    assert public_base_url("127.0.0.1", 8931, ["https://x.example/mcp"]) == "https://x.example"
    assert public_base_url("0.0.0.0", 9000, []) == "http://127.0.0.1:9000"
    for good in (CHATGPT_REDIRECT_URI, "http://localhost:3000/callback", "http://127.0.0.1:5173/cb",
                 "cursor://anysphere.cursor-retrieval/oauth/callback", "https://claude.ai/api/mcp/auth_callback"):
        assert validate_redirect_uri(good) == good
    for bad in ("javascript:alert(1)", "data:text/html,hi", "http://evil.example/cb", "https://a.example/cb#frag",
                "file:///C:/x", "https://user:pw@a.example/cb", "nonsense"):
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

        # a rotated refresh token works within the grace window (lost response) ...
        again = await token_request(http, client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
        assert again.status_code == 200
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

        # 5 wrong codes per 10 minutes, then even the right code waits
        _, fields = await open_consent(http, client, challenge)
        for _ in range(5):
            assert (await submit(http, fields, "AAAA-AAAA")).status_code == 400
        locked = await submit(http, fields, pairing_code(store))
        assert locked.status_code == 429 and "Too many" in locked.text
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
