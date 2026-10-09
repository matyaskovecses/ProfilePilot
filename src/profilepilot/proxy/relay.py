"""Local, credential-free proxy relay.

Chromium cannot authenticate to SOCKS5 proxies, and HTTP proxy auth needs an interactive
challenge handler. ProfilePilot therefore points the browser at a relay listening on
``127.0.0.1:<port>`` that needs no credentials, and the relay opens every connection through
the real upstream proxy (with credentials) on the browser's behalf.

Inbound, the relay speaks SOCKS5 (no-auth, CONNECT), SOCKS4/4a and HTTP (CONNECT tunnels and
absolute-URI plain HTTP requests), auto-detected from the first byte. Hostnames are passed to
the upstream unresolved, so DNS resolution happens at the proxy and does not leak locally.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import ssl
import struct
from dataclasses import dataclass, field

from python_socks import ProxyType
from python_socks.async_.asyncio.v2 import Proxy

from .url import ProxyEndpoint

log = logging.getLogger("profilepilot.relay")

_BUF = 64 * 1024
_MAX_HTTP_HEAD = 64 * 1024
_PROXY_TYPES = {
    "http": ProxyType.HTTP,
    "https": ProxyType.HTTP,
    "socks4": ProxyType.SOCKS4,
    "socks5": ProxyType.SOCKS5,
}


class UpstreamError(Exception):
    """The upstream proxy refused or failed the connection."""


def upstream_failure(exc: BaseException, upstream: ProxyEndpoint | None) -> str:
    """``Type: message`` of a failed upstream connection, with the upstream proxy's address replaced by
    ``<upstream proxy>`` (python-socks names it: "Couldn't connect to proxy HOST:PORT"). The text is the
    relay's ``last_error``, which tools show to the model (docs/FINGERPRINT-AUDIT.md F10)."""
    text = f"{type(exc).__name__}: {exc}"
    if upstream is not None:
        for host in dict.fromkeys((f"[{upstream.host}]", upstream.host)):
            text = text.replace(f"{host}:{upstream.port}", "<upstream proxy>").replace(host, "<upstream proxy>")
    return text


@dataclass
class RelayStats:
    connections_total: int = 0
    connections_active: int = 0
    connections_failed: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class LocalRelay:
    """Credential-free local proxy that forwards through ``upstream``.

    ``upstream=None`` connects directly (useful for tests and for "no proxy" profiles that
    still want a stable local endpoint).
    """

    upstream: ProxyEndpoint | None
    host: str = "127.0.0.1"
    connect_timeout: float = 20.0
    half_close_grace: float = 60.0
    """Seconds a tunnel stays open after one direction ended (a half-close) before it is torn down."""
    stats: RelayStats = field(default_factory=RelayStats)
    _server: asyncio.base_events.Server | None = field(default=None, init=False, repr=False)
    _tasks: set[asyncio.Task] = field(default_factory=set, init=False, repr=False)

    @property
    def port(self) -> int:
        if not self._server or not self._server.sockets:
            raise RuntimeError("relay is not running")
        return self._server.sockets[0].getsockname()[1]

    @property
    def socks_url(self) -> str:
        return f"socks5://{self.host}:{self.port}"

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, self.host, port, limit=_BUF)
        log.info("relay listening on %s:%s -> %s", self.host, self.port,
                 self.upstream.redacted() if self.upstream else "direct")
        return self.port

    async def stop(self) -> None:
        """Stop listening and tear down every open tunnel.

        Handler tasks are cancelled *before* waiting for the server: since CPython 3.12.1
        ``Server.wait_closed()`` waits for every active connection, so a tunnel whose peer never
        closes would otherwise block the stop forever.
        """
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for task in list(self._tasks):
            task.cancel()  # _handle's finally closes the client writer, detaching its transport
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if server is not None:
            with contextlib.suppress(Exception):
                # bounded: covers connections accepted but not yet registered in _tasks
                await asyncio.wait_for(server.wait_closed(), 2.0)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    # ------------------------------------------------------------------ upstream

    async def open_upstream(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a TCP stream to ``host:port`` through the upstream proxy (or directly)."""
        try:
            if self.upstream is None:
                return await asyncio.wait_for(asyncio.open_connection(host, port, limit=_BUF), self.connect_timeout)
            up = self.upstream
            proxy = Proxy(
                proxy_type=_PROXY_TYPES[up.scheme],
                host=up.host,
                port=up.port,
                username=up.username,
                password=up.password,
                rdns=True,
                proxy_ssl=ssl.create_default_context() if up.scheme == "https" else None,
            )
            stream = await proxy.connect(dest_host=host, dest_port=port, timeout=self.connect_timeout)
            return stream.reader, stream.writer
        except (asyncio.TimeoutError, OSError, Exception) as exc:  # python-socks raises its own types
            raise UpstreamError(upstream_failure(exc, self.upstream)) from exc

    # ------------------------------------------------------------------ inbound

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self.stats.connections_total += 1
        self.stats.connections_active += 1
        try:
            first = await reader.read(1)
            if not first:
                return
            if first == b"\x05":
                await self._socks5(reader, writer)
            elif first == b"\x04":
                await self._socks4(reader, writer)
            else:
                await self._http(first, reader, writer)
        except UpstreamError as exc:
            self.stats.connections_failed += 1
            self.stats.last_error = str(exc)
            log.debug("upstream failure: %s", exc)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            log.debug("client connection error: %s", exc)
        except asyncio.CancelledError:
            pass
        except Exception:  # never let one bad connection kill the relay
            log.exception("relay connection crashed")
        finally:
            self.stats.connections_active -= 1
            _close(writer)
            if task is not None:
                self._tasks.discard(task)

    async def _socks5(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nmethods = (await reader.readexactly(1))[0]
        methods = await reader.readexactly(nmethods)
        if 0x00 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return
        writer.write(b"\x05\x00")
        await writer.drain()

        ver, cmd, _rsv, atyp = await reader.readexactly(4)
        if ver != 5:
            return
        if atyp == 1:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif atyp == 3:
            length = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(length)).decode("idna")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            writer.write(_socks5_reply(0x08))
            await writer.drain()
            return
        (port,) = struct.unpack("!H", await reader.readexactly(2))

        if cmd != 1:  # only CONNECT; UDP ASSOCIATE/BIND are not relayed
            writer.write(_socks5_reply(0x07))
            await writer.drain()
            return
        try:
            up_reader, up_writer = await self.open_upstream(host, port)
        except UpstreamError:
            writer.write(_socks5_reply(0x05))
            await writer.drain()
            raise
        writer.write(_socks5_reply(0x00))
        await writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _socks4(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        cmd = (await reader.readexactly(1))[0]
        (port,) = struct.unpack("!H", await reader.readexactly(2))
        ip = await reader.readexactly(4)
        await reader.readuntil(b"\x00")  # user id, ignored
        if ip[:3] == b"\x00\x00\x00" and ip[3] != 0:  # SOCKS4a: hostname follows
            host = (await reader.readuntil(b"\x00"))[:-1].decode("idna")
        else:
            host = socket.inet_ntop(socket.AF_INET, ip)
        if cmd != 1:
            writer.write(b"\x00\x5b" + b"\x00" * 6)
            await writer.drain()
            return
        try:
            up_reader, up_writer = await self.open_upstream(host, port)
        except UpstreamError:
            writer.write(b"\x00\x5b" + b"\x00" * 6)
            await writer.drain()
            raise
        writer.write(b"\x00\x5a" + b"\x00" * 6)
        await writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _http(self, first: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = first
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(_BUF)
            if not chunk:
                return
            head += chunk
            if len(head) > _MAX_HTTP_HEAD:
                writer.write(b"HTTP/1.1 431 Request Header Fields Too Large\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
        head, _, rest = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        if method.upper() == "CONNECT":
            host, port = _split_authority(target, 443)
            try:
                up_reader, up_writer = await self.open_upstream(host, port)
            except UpstreamError as exc:
                writer.write(_http_error(502, str(exc)))
                await writer.drain()
                raise
            writer.write(f"{version} 200 Connection Established\r\n\r\n".encode("latin-1"))
            await writer.drain()
            if rest:
                up_writer.write(rest)
                await up_writer.drain()
            await self._pipe(reader, writer, up_reader, up_writer)
            return

        # Plain HTTP with an absolute URI: tunnel to the origin and send it in origin-form.
        if not target.lower().startswith("http://"):
            writer.write(_http_error(400, "relay only accepts CONNECT or absolute http:// URIs"))
            await writer.drain()
            return
        authority, _, path = target[len("http://"):].partition("/")
        host, port = _split_authority(authority, 80)
        headers = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
        headers.append("Connection: close")
        request = f"{method} /{path} {version}\r\n" + "\r\n".join(headers) + "\r\n\r\n"
        try:
            up_reader, up_writer = await self.open_upstream(host, port)
        except UpstreamError as exc:
            writer.write(_http_error(502, str(exc)))
            await writer.drain()
            raise
        up_writer.write(request.encode("latin-1") + rest)
        await up_writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _pipe(self, c_reader, c_writer, u_reader, u_writer) -> None:
        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter, up: bool) -> None:
            try:
                while True:
                    data = await src.read(_BUF)
                    if not data:
                        break
                    if up:
                        self.stats.bytes_up += len(data)
                    else:
                        self.stats.bytes_down += len(data)
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    if dst.can_write_eof():
                        dst.write_eof()

        up = asyncio.ensure_future(copy(c_reader, u_writer, True))
        down = asyncio.ensure_future(copy(u_reader, c_writer, False))
        try:
            _done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            if pending:
                # One side finished and half-closed the other. Legitimate half-close clients get a
                # bounded grace period; a peer that never closes must not keep the tunnel (and its
                # sockets) alive for the relay's whole life.
                await asyncio.wait(pending, timeout=self.half_close_grace)
        finally:
            for task in (up, down):
                task.cancel()
            await asyncio.gather(up, down, return_exceptions=True)
            _close(u_writer)


def _socks5_reply(code: int) -> bytes:
    return bytes([0x05, code, 0x00, 0x01, 0, 0, 0, 0, 0, 0])


def _http_error(status: int, message: str) -> bytes:
    reason = {400: "Bad Request", 502: "Bad Gateway"}.get(status, "Error")
    body = message.encode("utf-8", "replace")
    return (f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("latin-1") + body


def _split_authority(authority: str, default_port: int) -> tuple[str, int]:
    authority = authority.strip()
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if authority.count(":") > 1:  # bare IPv6 literal without brackets carries no port
        return authority, default_port
    host, sep, port = authority.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return authority, default_port


def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
