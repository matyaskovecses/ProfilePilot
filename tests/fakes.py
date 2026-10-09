"""In-process fake servers used by the tests (no external network needed)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import socket
import struct
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class FakeSocks5Server:
    """SOCKS5 server that requires username/password auth (RFC 1929) and records targets."""

    username: str = "user"
    password: str = "p@ss:word"
    targets: list[tuple[str, int]] = field(default_factory=list)
    auth_failures: int = 0
    _server: asyncio.base_events.Server | None = None
    _tasks: set = field(default_factory=set)

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> "FakeSocks5Server":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            ver, n = await reader.readexactly(2)
            methods = await reader.readexactly(n)
            if 0x02 not in methods:
                writer.write(b"\x05\xff")
                return
            writer.write(b"\x05\x02")
            await writer.drain()
            await reader.readexactly(1)  # auth version
            ulen = (await reader.readexactly(1))[0]
            user = (await reader.readexactly(ulen)).decode()
            plen = (await reader.readexactly(1))[0]
            pwd = (await reader.readexactly(plen)).decode()
            if (user, pwd) != (self.username, self.password):
                self.auth_failures += 1
                writer.write(b"\x01\x01")
                return
            writer.write(b"\x01\x00")
            await writer.drain()
            _ver, _cmd, _rsv, atyp = await reader.readexactly(4)
            if atyp == 3:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            elif atyp == 1:
                host = socket.inet_ntoa(await reader.readexactly(4))
            else:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            (port,) = struct.unpack("!H", await reader.readexactly(2))
            self.targets.append((host, port))
            # "localhost.test" lets tests prove DNS was resolved here (remotely), not by the client
            real_host = "127.0.0.1" if host in ("localhost.test", "localhost") else host
            up_r, up_w = await asyncio.open_connection(real_host, port)
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await _pipe(reader, writer, up_r, up_w)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            self._tasks.discard(asyncio.current_task())


@dataclass
class FakeHttpConnectProxy:
    """HTTP proxy supporting CONNECT with Basic auth; records targets."""

    username: str = "huser"
    password: str = "hpass"
    targets: list[str] = field(default_factory=list)
    _server: asyncio.base_events.Server | None = None
    _tasks: set = field(default_factory=set)

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> "FakeHttpConnectProxy":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            expected = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            auth = next((l.split(":", 1)[1].strip() for l in lines[1:] if l.lower().startswith("proxy-authorization:")), None)
            if auth != expected:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            if method != "CONNECT":
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                return
            self.targets.append(target)
            host, port = target.rsplit(":", 1)
            real_host = "127.0.0.1" if host in ("localhost.test", "localhost") else host
            up_r, up_w = await asyncio.open_connection(real_host, int(port))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await _pipe(reader, writer, up_r, up_w)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
            self._tasks.discard(asyncio.current_task())


async def _pipe(r1, w1, r2, w2) -> None:
    async def copy(src, dst):
        try:
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                dst.write_eof()

    await asyncio.gather(copy(r1, w2), copy(r2, w1))
    with contextlib.suppress(Exception):
        w2.close()


class OriginServer:
    """Threaded HTTP origin that serves small pages and echoes request info."""

    def __init__(self, pages: dict[str, str] | None = None,
                 files: dict[str, tuple[str, bytes, dict[str, str]]] | None = None) -> None:
        """``pages``: path -> HTML. ``files``: path -> (content type, body, extra headers)."""
        self.pages = pages or {}
        self.files = files or {}
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(length) if length else b""
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": data})
                body = b"post " + self.path.encode() + b" body=" + data
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                if self.path in outer.files:
                    ctype, body, extra = outer.files[self.path]
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    for name, value in extra.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/set-session-cookie"):
                    name, _, value = self.path.partition("?")[2].partition("=")
                    body = b"session cookie set"
                    self.send_response(200)
                    self.send_header("Set-Cookie", f"{name}={value}; Path=/")
                elif self.path.startswith("/set-cookie"):
                    name, _, value = self.path.partition("?")[2].partition("=")
                    body = b"cookie set"
                    self.send_response(200)
                    self.send_header("Set-Cookie", f"{name}={value}; Path=/; Max-Age=86400")
                elif self.path in outer.pages:
                    body = outer.pages[self.path].encode()
                    self.send_response(200)
                else:
                    body = f"echo {self.path} cookie={self.headers.get('Cookie', '')}".encode()
                    self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "OriginServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
