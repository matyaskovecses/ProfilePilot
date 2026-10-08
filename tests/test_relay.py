import asyncio

import httpx
import pytest
from python_socks import ProxyType
from python_socks.async_.asyncio.v2 import Proxy

from profilepilot.proxy.relay import LocalRelay
from profilepilot.proxy.url import ProxyEndpoint

from .fakes import FakeHttpConnectProxy, FakeSocks5Server, OriginServer

pytestmark = pytest.mark.asyncio


async def _get_via_socks(relay_port: int, host: str, port: int, path: str = "/hello") -> bytes:
    client = Proxy(ProxyType.SOCKS5, "127.0.0.1", relay_port, rdns=True)
    stream = await client.connect(dest_host=host, dest_port=port, timeout=5)
    stream.writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    await stream.writer.drain()
    data = await asyncio.wait_for(stream.reader.read(-1), 5)
    stream.writer.close()
    return data


async def test_socks5_inbound_to_authenticated_socks5_upstream_uses_remote_dns():
    upstream = await FakeSocks5Server().start()
    relay = LocalRelay(ProxyEndpoint("socks5", "127.0.0.1", upstream.port, upstream.username, upstream.password))
    await relay.start()
    try:
        with OriginServer() as origin:
            # .test never resolves locally, so success proves the hostname reached the upstream unresolved
            data = await _get_via_socks(relay.port, "localhost.test", origin.port)
        assert b"200 OK" in data and b"echo /hello" in data
        assert upstream.targets == [("localhost.test", origin.port)]
        assert relay.stats.bytes_down > 0 and relay.stats.connections_failed == 0
    finally:
        await relay.stop()
        await upstream.stop()


async def test_http_connect_inbound_to_authenticated_http_upstream():
    upstream = await FakeHttpConnectProxy().start()
    relay = LocalRelay(ProxyEndpoint("http", "127.0.0.1", upstream.port, upstream.username, upstream.password))
    await relay.start()
    try:
        with OriginServer() as origin:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
            writer.write(f"CONNECT localhost.test:{origin.port} HTTP/1.1\r\nHost: localhost.test\r\n\r\n".encode())
            await writer.drain()
            status = await reader.readuntil(b"\r\n\r\n")
            assert b" 200 " in status
            writer.write(b"GET /tunnel HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            await writer.drain()
            body = await asyncio.wait_for(reader.read(-1), 5)
            writer.close()
        assert b"echo /tunnel" in body
        assert upstream.targets == [f"localhost.test:{origin.port}"]
    finally:
        await relay.stop()
        await upstream.stop()


async def test_plain_http_absolute_uri_through_relay_with_httpx():
    upstream = await FakeSocks5Server().start()
    relay = LocalRelay(ProxyEndpoint("socks5", "127.0.0.1", upstream.port, upstream.username, upstream.password))
    await relay.start()
    try:
        with OriginServer() as origin:
            async with httpx.AsyncClient(proxy=relay.http_url, timeout=5) as client:
                resp = await client.get(f"http://127.0.0.1:{origin.port}/plain", headers={"Cookie": "a=1"})
        assert resp.status_code == 200
        assert resp.text == "echo /plain cookie=a=1"
        assert upstream.targets == [("127.0.0.1", origin.port)]
    finally:
        await relay.stop()
        await upstream.stop()


async def test_bad_upstream_credentials_fail_cleanly_and_relay_survives():
    upstream = await FakeSocks5Server().start()
    relay = LocalRelay(ProxyEndpoint("socks5", "127.0.0.1", upstream.port, "user", "wrong"))
    await relay.start()
    try:
        with pytest.raises(Exception):
            await _get_via_socks(relay.port, "127.0.0.1", 9)
        await asyncio.sleep(0.05)
        assert upstream.auth_failures == 1
        assert relay.stats.connections_failed == 1
        assert "rror" in (relay.stats.last_error or "")
        # the relay keeps serving after a failure
        relay.upstream = ProxyEndpoint("socks5", "127.0.0.1", upstream.port, upstream.username, upstream.password)
        with OriginServer() as origin:
            assert b"echo /hello" in await _get_via_socks(relay.port, "127.0.0.1", origin.port)
    finally:
        await relay.stop()
        await upstream.stop()


async def test_direct_mode_without_upstream():
    relay = LocalRelay(None)
    await relay.start()
    try:
        with OriginServer() as origin:
            assert b"echo /hello" in await _get_via_socks(relay.port, "127.0.0.1", origin.port)
    finally:
        await relay.stop()
