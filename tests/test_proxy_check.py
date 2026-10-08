import asyncio
import json
import time

import pytest

from profilepilot.models import ProxyCheck
from profilepilot.proxy.check import (
    DEFAULT_PROVIDERS,
    GeoProvider,
    ProviderError,
    check_proxy,
    check_saved_proxy,
    check_via_relay,
    parse_provider_response,
    scrub_secrets,
)
from profilepilot.proxy.url import ProxyEndpoint

from .fakes import FakeHttpConnectProxy, FakeSocks5Server, OriginServer

IPWHO_OK = {
    "ip": "203.0.113.7", "success": True, "type": "IPv4", "country": "Germany", "country_code": "DE",
    "region": "Hesse", "city": "Frankfurt am Main",
    "connection": {"asn": 64500, "org": "Example Hosting", "isp": "Example ISP"},
    "timezone": {"id": "Europe/Berlin", "utc": "+02:00"},
}
IPAPI_CO_OK = {
    "ip": "198.51.100.9", "city": "Paris", "region": "Ile-de-France", "country_code": "FR",
    "country_name": "France", "timezone": "Europe/Paris", "org": "Example SAS",
}
IP_API_OK = {
    "status": "success", "query": "192.0.2.44", "country": "Japan", "countryCode": "JP",
    "regionName": "Tokyo", "city": "Tokyo", "isp": "Example KK", "timezone": "Asia/Tokyo",
}


# ---------------------------------------------------------------------- parsing (offline)


def test_parse_each_provider_shape():
    a = parse_provider_response("ipwho.is", IPWHO_OK)
    assert (a.ok, a.ip, a.country_code, a.city, a.isp, a.timezone) == (
        True, "203.0.113.7", "DE", "Frankfurt am Main", "Example ISP", "Europe/Berlin")
    b = parse_provider_response("ipapi.co", IPAPI_CO_OK)
    assert (b.ip, b.country, b.country_code, b.isp, b.timezone) == ("198.51.100.9", "France", "FR", "Example SAS", "Europe/Paris")
    c = parse_provider_response("ip-api.com", IP_API_OK)
    assert (c.ip, c.country_code, c.region, c.isp, c.timezone) == ("192.0.2.44", "JP", "Tokyo", "Example KK", "Asia/Tokyo")
    generic = parse_provider_response("other", {"query": "192.0.2.1", "countryCode": "US"})
    assert generic.ip == "192.0.2.1" and generic.country_code == "US"


@pytest.mark.parametrize("kind,data,match", [
    ("ipwho.is", {"success": False, "message": "You've hit the monthly limit"}, "monthly limit"),
    ("ipapi.co", {"error": True, "reason": "RateLimited", "message": "Visit ipapi.co/ratelimited"}, "RateLimited"),
    ("ip-api.com", {"status": "fail", "message": "quota exceeded"}, "quota"),
    ("ipwho.is", {"success": True, "country": "DE"}, "without an IP"),
    ("ipapi.co", ["not", "an", "object"], "not a JSON object"),
])
def test_quota_errors_returned_with_http_200_are_failures(kind, data, match):
    with pytest.raises(ProviderError, match=match):
        parse_provider_response(kind, data)


def test_scrub_secrets_removes_plain_and_percent_encoded_credentials():
    ep = ProxyEndpoint("socks5", "proxy.example", 1080, "alice", "p@ss:word")
    text = "failed for alice:p@ss:word and alice:p%40ss%3Aword"
    scrubbed = scrub_secrets(text, ep)
    assert "p@ss" not in scrubbed and "p%40ss" not in scrubbed and "alice" not in scrubbed
    assert scrub_secrets("nothing secret", None) == "nothing secret"


def test_default_chain_matches_design():
    assert [p.url for p in DEFAULT_PROVIDERS] == [
        "https://ipwho.is/", "https://ipapi.co/json/",
        "http://ip-api.com/json/?fields=status,message,query,country,countryCode,regionName,city,isp,timezone",
    ]


# ---------------------------------------------------------------------- local fake providers


@pytest.fixture
def providers_server():
    pages = {
        "/ipwho-quota": json.dumps({"success": False, "message": "You've hit the monthly limit"}),
        "/ipapi-ok": json.dumps(IPAPI_CO_OK),
        "/ipapi-err": json.dumps({"error": True, "reason": "RateLimited"}),
        "/ipapi-noip": json.dumps({"country_code": "FR"}),
        "/ip-api-ok": json.dumps(IP_API_OK),
    }
    with OriginServer(pages) as server:
        yield server


def _chain(port: int, *paths_and_kinds: tuple[str, str], host: str = "localhost.test") -> list[GeoProvider]:
    return [GeoProvider(f"{kind}#{i}", f"http://{host}:{port}{path}", kind=kind)
            for i, (path, kind) in enumerate(paths_and_kinds)]


@pytest.mark.asyncio
async def test_check_proxy_through_authenticated_socks5_skips_quota_errors(providers_server):
    upstream = await FakeSocks5Server().start()
    endpoint = ProxyEndpoint("socks5", "127.0.0.1", upstream.port, upstream.username, upstream.password)
    try:
        chain = _chain(providers_server.port, ("/ipwho-quota", "ipwho.is"), ("/not-json", "ipapi.co"),
                       ("/ipapi-ok", "ipapi.co"), ("/ip-api-ok", "ip-api.com"))
        result = await check_proxy(endpoint, timeout=10, providers=chain)
        assert isinstance(result, ProxyCheck)
        assert result.ok and result.error is None
        assert (result.ip, result.country_code, result.provider) == ("198.51.100.9", "FR", "ipapi.co#2")
        assert result.latency_ms is not None and 0 <= result.latency_ms < 5000
        # every request left through the upstream proxy, hostname unresolved (remote DNS)
        assert upstream.targets and all(t == ("localhost.test", providers_server.port) for t in upstream.targets)
        assert [r["path"] for r in providers_server.requests] == ["/ipwho-quota", "/not-json", "/ipapi-ok"]
    finally:
        await upstream.stop()


@pytest.mark.asyncio
async def test_check_proxy_through_http_connect_upstream(providers_server):
    upstream = await FakeHttpConnectProxy().start()
    endpoint = ProxyEndpoint("http", "127.0.0.1", upstream.port, upstream.username, upstream.password)
    try:
        result = await check_proxy(endpoint, timeout=10, providers=_chain(providers_server.port, ("/ip-api-ok", "ip-api.com")))
        assert result.ok and result.ip == "192.0.2.44" and result.timezone == "Asia/Tokyo"
        assert upstream.targets == [f"localhost.test:{providers_server.port}"]
    finally:
        await upstream.stop()


@pytest.mark.asyncio
async def test_bad_credentials_fail_without_leaking_them(providers_server):
    upstream = await FakeSocks5Server().start()
    endpoint = ProxyEndpoint("socks5", "127.0.0.1", upstream.port, "alice", "Sup3r-Secret!")
    try:
        result = await check_proxy(endpoint, timeout=10, providers=_chain(providers_server.port, ("/ipapi-ok", "ipapi.co")))
        assert not result.ok and result.ip is None
        assert "upstream proxy" in result.error and endpoint.redacted() in result.error
        assert "Sup3r-Secret!" not in result.error and "Sup3r" not in result.model_dump_json()
        assert upstream.auth_failures >= 1
    finally:
        await upstream.stop()


@pytest.mark.asyncio
async def test_all_providers_failing_lists_each_reason(providers_server):
    chain = _chain(providers_server.port, ("/ipapi-err", "ipapi.co"), ("/ipapi-noip", "ipapi.co"), host="127.0.0.1")
    result = await check_via_relay(None, timeout=10, providers=chain)
    assert not result.ok
    assert "ipapi.co#0: refused: RateLimited" in result.error
    assert "ipapi.co#1: answered without an IP address" in result.error


@pytest.mark.asyncio
async def test_check_via_existing_relay_url(providers_server):
    from profilepilot.proxy.relay import LocalRelay

    relay = LocalRelay(None)
    await relay.start()
    try:
        chain = _chain(providers_server.port, ("/ip-api-ok", "ip-api.com"), host="127.0.0.1")
        result = await check_via_relay(relay.http_url, timeout=10, providers=chain)
        assert result.ok and result.ip == "192.0.2.44"
        assert relay.stats.connections_total >= 1
    finally:
        await relay.stop()


@pytest.mark.asyncio
async def test_unreachable_upstream_and_overall_timeout():
    # a "provider" that accepts connections and never answers
    async def hang(reader, writer):
        await asyncio.sleep(30)

    server = await asyncio.start_server(hang, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        chain = [GeoProvider("slow", f"http://127.0.0.1:{port}/", kind="ipapi.co"),
                 GeoProvider("never-tried", f"http://127.0.0.1:{port}/x", kind="ipapi.co")]
        started = time.monotonic()
        result = await check_via_relay(None, timeout=1.5, providers=chain)
        assert time.monotonic() - started < 5
        assert not result.ok and "timed out" in result.error
        # nothing listens on this port: the relay reports the upstream failure
        dead = ProxyEndpoint("socks5", "127.0.0.1", 9, "u", "p")
        result = await check_proxy(dead, timeout=3, providers=[GeoProvider("x", "http://example.invalid/", kind="ipapi.co")])
        assert not result.ok and "upstream proxy socks5://u***:***@127.0.0.1:9 failed" in result.error
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_check_saved_proxy_records_the_result(store, providers_server):
    upstream = await FakeSocks5Server().start()
    try:
        record = store.add_proxy(f"socks5://user:p%40ss%3Aword@127.0.0.1:{upstream.port}", "fake")
        chain = _chain(providers_server.port, ("/ipapi-ok", "ipapi.co"))
        result = await check_saved_proxy(store, "fake", timeout=10, providers=chain)
        assert result.ok and result.ip == "198.51.100.9"
        saved = store.get_proxy(record.id).last_check
        assert saved is not None and saved.ok and saved.ip == "198.51.100.9"
        assert store.get_proxy(record.id).summary()["last_check"]["country"] == "FR"
    finally:
        await upstream.stop()


@pytest.mark.network
@pytest.mark.asyncio
async def test_real_direct_check_against_public_providers():
    result = await check_proxy(None, timeout=15)
    assert result.ok, result.error
    assert result.ip and result.provider in {p.name for p in DEFAULT_PROVIDERS}


@pytest.mark.network
@pytest.mark.asyncio
async def test_real_check_through_local_relay_with_https_providers():
    # direct relay (no upstream): exercises the CONNECT path for the HTTPS providers
    from profilepilot.proxy.relay import LocalRelay

    relay = LocalRelay(None)
    await relay.start()
    try:
        result = await check_via_relay(relay.http_url, timeout=15)
        assert result.ok, result.error
    finally:
        await relay.stop()
