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


class _SilentServer:
    """TCP server that accepts connections and never sends or closes anything."""

    def __init__(self) -> None:
        self._server: asyncio.base_events.Server | None = None
        self.writers: list[asyncio.StreamWriter] = []

    async def start(self) -> "_SilentServer":
        async def handle(reader, writer):
            self.writers.append(writer)
            await asyncio.Event().wait()

        self._server = await asyncio.start_server(handle, "127.0.0.1", 0)
        return self

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for w in self.writers:
            w.close()
        assert self._server is not None
        self._server.close()


async def _open_tunnel(relay_port: int, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
    writer.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    assert b" 200 " in await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    return reader, writer


async def test_half_closed_tunnel_is_torn_down_after_the_grace_period():
    silent = await _SilentServer().start()
    relay = LocalRelay(None, half_close_grace=0.3)
    await relay.start()
    try:
        _reader, writer = await _open_tunnel(relay.port, silent.port)
        assert relay.stats.connections_active == 1
        writer.close()  # the browser side goes away; the upstream never closes
        for _ in range(100):
            if relay.stats.connections_active == 0:
                break
            await asyncio.sleep(0.05)
        assert relay.stats.connections_active == 0 and not relay._tasks
    finally:
        await relay.stop()
        await silent.stop()


async def test_stop_does_not_wait_for_open_tunnels_like_python_3_12_wait_closed():
    silent = await _SilentServer().start()
    relay = LocalRelay(None)
    await relay.start()
    server = relay._server
    assert server is not None

    async def wait_closed_3_12():  # CPython >= 3.12.1 waits for every active connection
        while relay.stats.connections_active:
            await asyncio.sleep(0.05)

    server.wait_closed = wait_closed_3_12  # type: ignore[method-assign]
    try:
        _reader, writer = await _open_tunnel(relay.port, silent.port)
        writer.close()  # half-closed tunnel whose upstream never closes
        await asyncio.sleep(0.1)
        await asyncio.wait_for(relay.stop(), 3)
        assert relay.stats.connections_active == 0
    finally:
        await relay.stop()
        await silent.stop()
