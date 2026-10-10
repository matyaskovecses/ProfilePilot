"""Throwaway probe servers for the WKWebView spike (not part of ProfilePilot).

HTTP pages   127.0.0.1:47801  (also reachable through the SOCKS servers under any host name)
WebSocket    127.0.0.1:47802  (echo)
SOCKS5       127.0.0.1:47803  (no auth)          logs every CONNECT / UDP ASSOCIATE with its address type
SOCKS5+auth  127.0.0.1:47804  (user "u", pass "p")
UDP sink     0.0.0.0:47805    logs every datagram (stands in for a STUN server)
Every event goes to events.jsonl next to this file.
"""

import asyncio
import json
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import websockets

HERE = Path(__file__).resolve().parent
LOG = HERE / "events.jsonl"
HTTP_PORT, WS_PORT, SOCKS_PORT, SOCKS_AUTH_PORT, UDP_PORT = 47801, 47802, 47803, 47804, 47805
_lock = threading.Lock()


def emit(kind: str, **data) -> None:
    with _lock, LOG.open("a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **data}) + "\n")


POST = """
<script>
window.__post = (obj) => fetch('/log', {method: 'POST', body: JSON.stringify(obj)}).catch(() => {});
</script>
"""

PAGES = {
    "/set": POST + """<body>set<script>
const v = new URLSearchParams(location.search).get('v');
document.cookie = 'pp_js=' + v + '; path=/; max-age=86400';
localStorage.setItem('pp', v);
__post({test: 'set', v, cookie: document.cookie, ls: localStorage.getItem('pp')});
</script></body>""",
    "/get": POST + """<body>get<script>
const tag = new URLSearchParams(location.search).get('tag');
__post({test: 'get', tag, cookie: document.cookie, ls: localStorage.getItem('pp')});
</script></body>""",
    "/proxy-page": POST + """<body>proxy<script>
const tag = new URLSearchParams(location.search).get('tag');
__post({test: 'proxy-fetch', tag, href: location.href});
try {
  const ws = new WebSocket('ws://' + location.hostname + ':47802/');
  ws.onopen = () => ws.send(tag);
  ws.onmessage = (e) => __post({test: 'proxy-ws', tag, echo: e.data});
  ws.onerror = () => __post({test: 'proxy-ws', tag, error: true});
} catch (e) { __post({test: 'proxy-ws', tag, error: String(e)}); }
</script></body>""",
    "/rtc": POST + """<body>rtc<script>
const q = new URLSearchParams(location.search);
const tag = q.get('tag'), stun = q.get('stun');
(async () => {
  const cands = [];
  let err = null;
  try {
    const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:' + stun}]});
    pc.createDataChannel('x');
    pc.onicecandidate = (e) => { if (e.candidate) cands.push(e.candidate.candidate); };
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise(r => setTimeout(r, 5000));
    pc.close();
  } catch (e) { err = String(e); }
  __post({test: 'rtc', tag, has_rtc: typeof RTCPeerConnection, cands, err});
})();
</script></body>""",
    "/input": POST + """<body style="margin:0">
<input id="t" style="position:absolute;left:100px;top:100px;width:300px;height:40px">
<button id="b" style="position:absolute;left:100px;top:200px;width:120px;height:40px">btn</button>
<script>
window.__log = [];
for (const type of ['pointerdown','mousedown','mouseup','click','focus','keydown','keypress','beforeinput','input','keyup']) {
  document.addEventListener(type, (e) => {
    window.__log.push({type, trusted: e.isTrusted, target: (e.target && e.target.id) || e.target.nodeName, value: document.getElementById('t').value});
  }, true);
}
</script></body>""",
    "/dialog": POST + """<body>dialog<script>
setTimeout(() => {
  const t0 = Date.now();
  alert('probe alert');
  const c = confirm('probe confirm');
  const p = prompt('probe prompt', 'default');
  __post({test: 'dialog', confirm: c, prompt: p, ms: Date.now() - t0});
}, 300);
</script></body>""",
    "/vis": POST + """<body>vis<script>
const tag = new URLSearchParams(location.search).get('tag');
(async () => {
  let raf = 0; const start = performance.now();
  const loop = () => { raf++; if (performance.now() - start < 2000) requestAnimationFrame(loop); };
  requestAnimationFrame(loop);
  const drifts = [];
  for (let i = 0; i < 5; i++) { const s = performance.now(); await new Promise(r => setTimeout(r, 100)); drifts.push(Math.round(performance.now() - s)); }
  await new Promise(r => setTimeout(r, 1600));
  __post({test: 'vis', tag, vis: document.visibilityState, hidden: document.hidden, focus: document.hasFocus(), raf, drifts});
})();
</script></body>""",
    "/frames": POST + """<body>frames
<iframe id="f" src="http://localhost:47801/child" width="300" height="100"></iframe>
</body>""",
    "/child": "<body>child frame</body>",
    "/fp": POST + """<body>fp<script>
(async () => {
  const src = new URLSearchParams(location.search).get('src');
  const o = {};
  const g = (k, f) => { try { o[k] = f(); } catch (e) { o[k] = 'ERR:' + e; } };
  g('ua', () => navigator.userAgent);
  g('appVersion', () => navigator.appVersion);
  g('vendor', () => navigator.vendor);
  g('platform', () => navigator.platform);
  g('languages', () => navigator.languages);
  g('hardwareConcurrency', () => navigator.hardwareConcurrency);
  g('deviceMemory', () => navigator.deviceMemory);
  g('maxTouchPoints', () => navigator.maxTouchPoints);
  g('webdriver', () => navigator.webdriver);
  g('pdfViewerEnabled', () => navigator.pdfViewerEnabled);
  g('plugins', () => Array.from(navigator.plugins).map(p => p.name));
  g('mimeTypes', () => navigator.mimeTypes.length);
  g('cookieEnabled', () => navigator.cookieEnabled);
  g('doNotTrack', () => navigator.doNotTrack);
  g('userAgentData', () => typeof navigator.userAgentData);
  g('window.safari', () => typeof window.safari);
  g('safari.pushNotification', () => window.safari ? typeof window.safari.pushNotification : 'n/a');
  g('ApplePaySession', () => typeof window.ApplePaySession);
  g('window.webkit', () => typeof window.webkit);
  g('window.chrome', () => typeof window.chrome);
  g('PublicKeyCredential', () => typeof window.PublicKeyCredential);
  g('Notification', () => typeof window.Notification);
  g('Notification.permission', () => window.Notification ? Notification.permission : 'n/a');
  g('PushManager', () => typeof window.PushManager);
  g('serviceWorker', () => typeof navigator.serviceWorker);
  g('share', () => typeof navigator.share);
  g('clipboard', () => typeof navigator.clipboard);
  g('credentials', () => typeof navigator.credentials);
  g('mediaDevices', () => typeof navigator.mediaDevices);
  g('getBattery', () => typeof navigator.getBattery);
  g('storage', () => typeof navigator.storage);
  g('webkitTemporaryStorage', () => typeof navigator.webkitTemporaryStorage);
  g('screen', () => [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth, devicePixelRatio]);
  g('tz', () => Intl.DateTimeFormat().resolvedOptions().timeZone);
  g('locale', () => Intl.DateTimeFormat().resolvedOptions().locale);
  g('webgl', () => { const c = document.createElement('canvas').getContext('webgl'); const d = c.getExtension('WEBGL_debug_renderer_info'); return [c.getParameter(c.VENDOR), c.getParameter(c.RENDERER), d && c.getParameter(d.UNMASKED_VENDOR_WEBGL), d && c.getParameter(d.UNMASKED_RENDERER_WEBGL)]; });
  g('webgl.ext', () => document.createElement('canvas').getContext('webgl').getSupportedExtensions().length);
  g('gpu', () => typeof navigator.gpu);
  g('audioRate', () => new (window.AudioContext || window.webkitAudioContext)().sampleRate);
  g('voices', () => speechSynthesis.getVoices().length);
  g('matchMedia', () => ['(prefers-color-scheme: dark)', '(dynamic-range: high)', '(color-gamut: p3)', '(pointer: fine)', '(hover: hover)', '(display-mode: browser)', '(prefers-reduced-motion: reduce)'].map(q => matchMedia(q).matches));
  g('css', () => ['-webkit-touch-callout: none', 'backdrop-filter: blur(1px)', '-apple-pay-button-style: black', 'text-wrap: balance'].map(s => CSS.supports(s)));
  g('history', () => history.length);
  g('outer-inner', () => [outerWidth, outerHeight, innerWidth, innerHeight]);
  g('visibility', () => document.visibilityState);
  g('webkitEnums', () => Object.getOwnPropertyNames(window).filter(n => /^(webkit|Webkit|WebKit|apple|Apple|safari|Safari)/.test(n)).sort());
  try { o['enumerateDevices'] = (await navigator.mediaDevices.enumerateDevices()).map(d => d.kind); } catch (e) { o['enumerateDevices'] = 'ERR:' + e; }
  try { o['storageEstimate'] = (await navigator.storage.estimate()).quota; } catch (e) { o['storageEstimate'] = 'ERR:' + e; }
  try { o['permissions.notifications'] = (await navigator.permissions.query({name: 'notifications'})).state; } catch (e) { o['permissions.notifications'] = 'ERR:' + e; }
  __post({test: 'fp', src, fp: o});
})();
</script></body>""",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: bytes, ctype="text/html", headers=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        emit("http", path=u.path, query=q, host=self.headers.get("Host"), cookie=self.headers.get("Cookie"),
             ua=self.headers.get("User-Agent"), accept_language=self.headers.get("Accept-Language"))
        page = PAGES.get(u.path)
        if page is None:
            return self._send(b"ok", "text/plain")
        headers = {}
        if u.path == "/set":
            headers["Set-Cookie"] = f"pp_http={q.get('v', '')}; Path=/; HttpOnly; Max-Age=86400"
        self._send(page.encode(), headers=headers)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw.decode(errors="replace")}
        emit("page", host=self.headers.get("Host"), **(data if isinstance(data, dict) else {"data": data}))
        self._send(b"ok", "text/plain")


async def ws_echo(conn):
    async for msg in conn:
        emit("ws", msg=msg)
        await conn.send(msg)


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


def make_socks(port: int, auth: bool):
    async def handle(reader, writer):
        try:
            ver, n = await reader.readexactly(2)
            methods = await reader.readexactly(n)
            if auth:
                if 2 not in methods:
                    writer.write(b"\x05\xff"); await writer.drain(); writer.close(); return
                writer.write(b"\x05\x02"); await writer.drain()
                _v, ulen = await reader.readexactly(2)
                user = (await reader.readexactly(ulen)).decode()
                (plen,) = await reader.readexactly(1)
                pw = (await reader.readexactly(plen)).decode()
                ok = user == "u" and pw == "p"
                emit("socks-auth", port=port, ok=ok, user=user)
                writer.write(b"\x01\x00" if ok else b"\x01\x01"); await writer.drain()
                if not ok:
                    writer.close(); return
            else:
                writer.write(b"\x05\x00"); await writer.drain()
            ver, cmd, _rsv, atyp = await reader.readexactly(4)
            if atyp == 1:
                host = socket.inet_ntoa(await reader.readexactly(4)); kind = "ipv4"
            elif atyp == 3:
                (ln,) = await reader.readexactly(1); host = (await reader.readexactly(ln)).decode(); kind = "domain"
            else:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16)); kind = "ipv6"
            (dport,) = struct.unpack("!H", await reader.readexactly(2))
            emit("socks", port=port, cmd={1: "CONNECT", 2: "BIND", 3: "UDP_ASSOCIATE"}.get(cmd, cmd), atyp=kind,
                 host=host, dport=dport)
            if cmd != 1:
                writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6); await writer.drain(); writer.close(); return
            try:
                ur, uw = await asyncio.open_connection("127.0.0.1", dport)  # fake upstream: everything is local
            except Exception:
                writer.write(b"\x05\x05\x00\x01" + b"\x00" * 6); await writer.drain(); writer.close(); return
            writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", dport))
            await writer.drain()
            await asyncio.gather(pipe(reader, uw), pipe(ur, writer))
        except Exception as exc:
            emit("socks-error", port=port, error=repr(exc))

    return handle


class UdpSink(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        emit("udp", src=f"{addr[0]}:{addr[1]}", size=len(data), stun=data[:2].hex())


async def main():
    LOG.write_text("")
    httpd = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    await websockets.serve(ws_echo, "127.0.0.1", WS_PORT)
    await asyncio.start_server(make_socks(SOCKS_PORT, False), "127.0.0.1", SOCKS_PORT)
    await asyncio.start_server(make_socks(SOCKS_AUTH_PORT, True), "127.0.0.1", SOCKS_AUTH_PORT)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(UdpSink, local_addr=("0.0.0.0", UDP_PORT))
    emit("ready")
    print("ready", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
