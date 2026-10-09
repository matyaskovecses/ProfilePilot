"""Network identity and leak audit: no-proxy profile vs. the real proxy (SOCKS5 and HTTP upstream).

Configurations (each in a FRESH profile / user-data-dir under ``<scratch>/audit-home/network``):

  P     ProfilePilot profile, no proxy (window=offscreen). RuntimeManager + BrowserManager
        (Playwright connect_over_cdp(no_defaults=True), contexts[0]); pages are opened with the MCP
        tool function ``browser_navigate`` itself and read with ``page.evaluate``.
  PX    P + saved proxy "audit-socks5" (host relay -> authenticated SOCKS5 upstream).
  PXH   P + saved proxy "audit-http"   (host relay -> authenticated HTTP CONNECT upstream).
  PXT   PX + opt-in alignment: launch lang="en-US", timezone = the proxy's timezone from proxy_test.
  B0D   plain chrome.exe, fresh dir ('First Run' sentinel), ONLY --remote-debugging-port (to read the
        result) + --window-position off-screen (not to disturb the desktop). No ProfilePilot code.
  B0DX  B0D + --proxy-server=socks5://127.0.0.1:<port> of a temporary LocalRelay started in THIS
        process for the same upstream (what a person would do to use the proxy in stock Chrome).

Checks per configuration: exit IP (ipify JSON, browserleaks /ip), DNS resolvers (browserleaks /dns,
ipleak.net), WebRTC (browserleaks /webrtc + a local STUN gather), IPv6 (test-ipv6.com), TLS / HTTP2
fingerprint (tls.peet.ws/api/all, browserleaks /tls), timezone / language (local page: main frame,
dedicated + shared worker, cross-site OOPIF), exit-IP stability (5 loads over ~1 min, browser and
http_fetch), http_fetch exit IP / fingerprint (server/tools_data.http_fetch, engines httpx and
scrapling), a socket monitor of every chrome.exe process of the profile (TCP peers / UDP sockets) and
of the ProfilePilot host process, and (PX) whether URL-policy DNS lookups reach the OS resolver in
local vs remote (HTTP server) mode.

Privacy: the real public IPv4/IPv6, rDNS, ISP, city, region... are detected at runtime and replaced
by REAL_* labels (run_detectors.Redactor + the scratch extra-terms file), the proxy exit IP by
PROXY_EXIT_IP, the proxy host / user / password by PROXY_HOST / PROXY_USER / PROXY_PASS, in
everything written; a file that still contains one of those values is not written. Screenshots are
only taken for proxied configurations, after the same values are replaced in the page DOM; they stay
local (docs/audit/.gitignore). Nothing raw is printed.

Usage (repo root, project venv)::

    python docs/audit/scripts/run_network_audit.py --scratch <scratch dir> [--configs P,PX,PXH,PXT,B0D,B0DX]

Writes docs/audit/raw/network-<CFG>.json, raw/network-proxy-test.json, img/network-<site>-<CFG>.jpg.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import secrets as pysecrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent
REPO = AUDIT.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "src"))

import run_detectors as rd  # noqa: E402  (Redactor, RawCDP, discover_identity, REDACT_DOM_JS)

MAX_IMG = 300 * 1024


class NetRedactor(rd.Redactor):
    """run_detectors' redactor + proxy secrets / exit IP; ``leaks`` covers REAL_* and PROXY_*."""

    # only inside names / whois handles ("NET-24-1-2-0-1", "c-73-1-2-3"), never in JA3 / JA4T strings
    _DASHED4 = re.compile(r"(?<=[A-Za-z]-)(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})(?![\d.])")

    def secret(self, value: str | None, label: str) -> None:
        if value and len(value) >= 3:
            self._add(re.escape(value), label, value)

    def add_ip(self, ip: str | None, label: str) -> None:
        super().add_ip(ip, label)
        try:
            addr = ipaddress.ip_address((ip or "").split("%", 1)[0])
        except ValueError:
            return
        if addr.version == 4:  # rDNS names such as "host-c0000201.dyn.example.net" (192.0.2.1) carry the IP in hex
            hx = "".join(f"{int(o):02x}" for o in str(addr).split("."))
            self._add(r"(?<![0-9a-f])" + hx + r"(?![0-9a-f])", label, hx)

    def text(self, s: str) -> str:
        s = super().text(s)
        if not s:
            return s
        # whois handles / host names with a dashed public IPv4 ("NET-24-1-2-0-1")
        s = self._DASHED4.sub(lambda m: self._label_ip(".".join(m.groups())) or m.group(0)
                              if all(int(g) < 256 for g in m.groups()) else m.group(0), s)
        return re.sub(r"PROXY_HOST:\d{2,5}", "PROXY_HOST:PROXY_PORT", s)

    def _cidr(self, m: re.Match) -> str:
        try:
            net = ipaddress.ip_network(m.group(0), strict=False)
        except ValueError:
            return m.group(0)
        for ip, label in self.known_ips.items():
            if ":" not in ip and ipaddress.ip_address(ip) in net:
                if label.startswith("REAL"):
                    return "REAL_NET"
                if label.startswith("PROXY_EXIT"):
                    return "PROXY_EXIT_NET"
        return m.group(0)

    def dom_pairs(self) -> list[list[str]]:
        pairs = super().dom_pairs()
        pairs += [[raw, label] for _, label, raw in self.patterns
                  if label.startswith(("PROXY_EXIT_IP", "PROXY_HOST")) and not any(c in raw for c in "*\\(")]
        return sorted(pairs, key=lambda p: -len(p[0]))

    def leaks(self, s: str) -> list[str]:
        return sorted({label for rx, label, _ in self.patterns
                       if label.startswith(("REAL", "PROXY_")) and rx.search(s)})


RED = NetRedactor()
rd.RED = RED  # rd.say / rd.capture / rd.collect_geo use the module global


def say(msg: Any) -> None:
    print(RED.text(str(msg)), flush=True)


# --------------------------------------------------------------------------- local pages

COLLECT_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>pp-net-collector</title></head>
<body><h3>ProfilePilot network audit collector</h3><pre id="out">running</pre>
<script>
const CFG = %(cfg)s, FRAME = %(frame)s;
const ro = Intl.DateTimeFormat().resolvedOptions();
const out = {cfg: CFG, href: location.href,
  main: {tz: ro.timeZone, locale: ro.locale, offset: new Date().getTimezoneOffset(),
         jan: new Date(2026, 0, 15, 12).toString(), language: navigator.language,
         languages: navigator.languages, ua: navigator.userAgent}};
const frameP = new Promise(res => {
  addEventListener('message', e => { if (e.data && e.data.kind === 'tzframe') res(e.data); });
  setTimeout(() => res('timeout'), 10000);
});
const fr = document.createElement('iframe'); fr.src = FRAME; fr.width = 400; fr.height = 60;
document.body.appendChild(fr);
const WSRC = "const ro = Intl.DateTimeFormat().resolvedOptions(); const v = {tz: ro.timeZone, locale: ro.locale, offset: new Date().getTimezoneOffset(), languages: navigator.languages};";
function dedicated() { return new Promise(res => { try {
  const w = new Worker(URL.createObjectURL(new Blob([WSRC + "postMessage(v);"], {type: 'text/javascript'})));
  w.onmessage = e => res(e.data); w.onerror = e => res('error: ' + (e.message || 'worker error'));
  setTimeout(() => res('timeout'), 6000); } catch (e) { res('error: ' + e); } }); }
function shared() { return new Promise(res => { try {
  const w = new SharedWorker(URL.createObjectURL(new Blob([WSRC + "onconnect = e => e.ports[0].postMessage(v);"], {type: 'text/javascript'})));
  w.port.onmessage = e => res(e.data); w.onerror = e => res('error: ' + (e.message || 'shared worker error'));
  w.port.start(); setTimeout(() => res('timeout'), 6000); } catch (e) { res('error: ' + e); } }); }
async function rtc() {
  const r = {candidates: [], state: null};
  try {
    const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
    pc.createDataChannel('x');
    pc.onicecandidate = e => { if (e.candidate && e.candidate.candidate) r.candidates.push(e.candidate.candidate); };
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise(done => { const t = setTimeout(done, 9000);
      pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === 'complete') { clearTimeout(t); done(); } }; });
    r.state = pc.iceGatheringState; pc.close();
  } catch (e) { r.error = String(e); }
  return r;
}
async function ip(u) { try { return (await (await fetch(u, {cache: 'no-store'})).json()).ip; } catch (e) { return 'error: ' + e; } }
(async () => {
  const [f, d, s, r, v4, v64] = await Promise.all([frameP, dedicated(), shared(), rtc(),
    ip('https://api.ipify.org?format=json'), ip('https://api64.ipify.org?format=json')]);
  Object.assign(out, {crossSiteFrame: f, dedicatedWorker: d, sharedWorker: s, webrtc: r, fetchIpify: v4, fetchIpify64: v64});
  document.getElementById('out').textContent = JSON.stringify(out);
  document.title = 'done';
  try { await fetch('/result?cfg=' + encodeURIComponent(CFG), {method: 'POST', body: JSON.stringify(out)}); } catch (e) {}
})();
</script></body></html>
"""

FRAME_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>tzframe</title></head><body>
<span id="t"></span><script>
const ro = Intl.DateTimeFormat().resolvedOptions();
const v = {kind: 'tzframe', origin: location.origin, tz: ro.timeZone, locale: ro.locale,
           offset: new Date().getTimezoneOffset(), languages: navigator.languages};
document.getElementById('t').textContent = 'frame ' + v.origin + ' tz=' + v.tz;
parent.postMessage(v, '*');
</script></body></html>
"""


class CollectServer:
    """Tiny local HTTP server: /collect (main page), /tzframe (cross-site iframe), POST /result."""

    def __init__(self) -> None:
        self.results: dict[str, Any] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # quiet
                pass

            def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                q = parse_qs(parts.query)
                if parts.path == "/collect":
                    cfg = (q.get("cfg") or ["?"])[0]
                    html = COLLECT_HTML % {"cfg": json.dumps(cfg),
                                           "frame": json.dumps(f"http://localhost:{outer.port}/tzframe")}
                    self._send(200, html.encode("utf-8"))
                elif parts.path == "/tzframe":
                    self._send(200, FRAME_HTML.encode("utf-8"))
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                cfg = (parse_qs(parts.query).get("cfg") or ["?"])[0]
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    outer.results[cfg] = json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    pass
                self._send(204, b"")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "CollectServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()

    def url(self, cfg: str) -> str:
        return f"http://127.0.0.1:{self.port}/collect?cfg={cfg}"


# --------------------------------------------------------------------------- socket monitor


class ConnMonitor:
    """Samples the sockets of a chrome.exe process tree (and optionally the host process) every second."""

    def __init__(self) -> None:
        self.chrome_pid: int | None = None
        self.host_pid: int | None = None
        self.samples = 0
        self.chrome_tcp: dict[str, dict[str, Any]] = {}
        self.chrome_udp: dict[str, dict[str, Any]] = {}
        self.host_tcp: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.t0 = time.time()

    def start(self, chrome_pid: int | None, host_pid: int | None = None) -> "ConnMonitor":
        self.chrome_pid, self.host_pid = chrome_pid, host_pid
        self._thread.start()
        return self

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(5)
        return self.report()

    @staticmethod
    def _conns(proc: Any) -> list[Any]:
        fn = getattr(proc, "net_connections", None) or proc.connections
        return fn(kind="inet")

    def _note(self, bucket: dict[str, dict[str, Any]], key: str, **info: Any) -> None:
        rec = bucket.get(key)
        if rec is None:
            bucket[key] = {"first_s": round(time.time() - self.t0, 1), "samples": 1, **info}
        else:
            rec["samples"] += 1

    def _run(self) -> None:
        import psutil
        while not self._stop.is_set():
            procs = []
            try:
                if self.chrome_pid:
                    root = psutil.Process(self.chrome_pid)
                    procs = [root] + root.children(recursive=True)
            except psutil.Error:
                procs = []
            for p in procs:
                try:
                    conns = self._conns(p)
                    ptype = next((a.split("=", 1)[1] for a in p.cmdline() if a.startswith("--type=")), "browser")
                except psutil.Error:
                    continue
                for c in conns:
                    proto = "tcp" if c.type == socket.SOCK_STREAM else "udp"
                    if proto == "tcp" and c.raddr:
                        rip = c.raddr.ip
                        loop = ipaddress.ip_address(rip.split("%", 1)[0]).is_loopback
                        key = f"loopback:{c.raddr.port}" if loop else f"DIRECT {rip}:{c.raddr.port}"
                        self._note(self.chrome_tcp, key, process=ptype, status=c.status)
                    elif proto == "udp" and c.laddr:
                        lip = c.laddr.ip
                        loop = lip not in ("0.0.0.0", "::") and ipaddress.ip_address(lip.split("%", 1)[0]).is_loopback
                        key = f"{'loopback' if loop else 'bound'} {lip}:{c.laddr.port}"
                        self._note(self.chrome_udp, key, process=ptype)
            if self.host_pid:
                try:
                    for c in self._conns(psutil.Process(self.host_pid)):
                        if c.type == socket.SOCK_STREAM and c.raddr:
                            loop = ipaddress.ip_address(c.raddr.ip.split("%", 1)[0]).is_loopback
                            key = "loopback" if loop else f"{c.raddr.ip}:{c.raddr.port}"
                            self._note(self.host_tcp, key, status=c.status)
                except psutil.Error:
                    pass
            self.samples += 1
            self._stop.wait(1.0)

    def report(self) -> dict[str, Any]:
        direct = sorted(k for k in self.chrome_tcp if k.startswith("DIRECT"))
        udp_bound = sorted(k for k in self.chrome_udp if k.startswith("bound"))
        return {"samples": self.samples, "chrome_tcp_peers": self.chrome_tcp, "chrome_udp_sockets": self.chrome_udp,
                "chrome_direct_tcp": direct, "chrome_udp_bound_nonloopback": udp_bound,
                "host_tcp_peers": self.host_tcp if self.host_pid else None}


# --------------------------------------------------------------------------- helpers


def import_profilepilot(retries: int = 6) -> SimpleNamespace:
    last = None
    for _ in range(retries):
        try:
            from profilepilot.automation.manager import BrowserManager
            from profilepilot.browser.host import free_port
            from profilepilot.browser.runtime import RuntimeManager, kill_tree
            from profilepilot.models import LaunchOptions
            from profilepilot.paths import data_root, find_browser
            from profilepilot.proxy.check import check_proxy, check_saved_proxy
            from profilepilot.proxy.relay import LocalRelay
            from profilepilot.proxy.url import ProxyEndpoint
            from profilepilot.safety import UrlPolicy
            from profilepilot.secrets import SecretStore
            from profilepilot.server import tools_browser, tools_data
            from profilepilot.server.app import AppState
            from profilepilot.store import Store
            return SimpleNamespace(**locals())
        except Exception as exc:  # another workflow is editing src/
            last = exc
            say(f"import failed ({type(exc).__name__}: {str(exc)[:200]}); retrying in 60 s")
            time.sleep(60)
    raise RuntimeError(f"could not import profilepilot: {last}")


def kill_leftovers(marker: str) -> int:
    return rd.kill_leftovers(marker)


def first_ip(text: str) -> str | None:
    m = re.search(r'"ip"\s*:\s*"([^"]+)"', text or "")
    return m.group(1) if m else None


def note_exit_ip(ip: str | None, proxied: bool) -> None:
    """Register an address seen through a proxy that proxy_test did not report (rotating exit)."""
    if not ip or ip.startswith("error"):
        return
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return
    if ip in RED.known_ips:
        return
    if proxied:
        n = sum(1 for lbl in set(RED.known_ips.values()) if lbl.startswith("PROXY_EXIT_IP")) + 1
        RED.add_ip(ip, f"PROXY_EXIT_IP_{n}")
    else:
        RED.add_ip(ip, "REAL_IP6" if ":" in ip else "REAL_IP")


def register_resolver_cities(text: str) -> None:
    """browserleaks /dns rows of the user's own ISP name the resolver's city (near the user): redact."""
    for _ip, isp, loc in re.findall(r"\n([0-9a-fA-F.:]+)\t([^\t\n]+)\t([^\n]+)", text or ""):
        if "REAL_ISP" in RED.text(isp) and "," in loc:
            RED.add_text(loc.split(",", 1)[1].strip(), "ISP_RESOLVER_CITY", min_len=3)


BODY_JS = "() => { const pre = document.querySelector('body > pre'); return {title: document.title, url: location.href, " \
          "pre: pre ? pre.textContent : null, text: document.body ? document.body.innerText : ''}; }"


async def page_shot(page: Any, out: Path) -> dict[str, Any]:
    info: dict[str, Any] = {}
    try:
        info["dom_redactions"] = await page.evaluate(rd.REDACT_DOM_JS % json.dumps(RED.dom_pairs()))
    except Exception as exc:
        info["dom_redaction_error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
    data = b""
    for q in (60, 45, 35, 25):
        data = await page.screenshot(type="jpeg", quality=q, timeout=20000)
        if len(data) <= MAX_IMG:
            break
    if len(data) > MAX_IMG:
        return {**info, "error": "screenshot larger than 300 KB; not saved"}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return {**info, "file": f"img/{out.name}", "bytes": len(data)}


SITES = [
    # key, url, wait seconds after DOMContentLoaded, screenshot?
    ("ipify", "https://api.ipify.org?format=json", 2, False),
    ("peet", "https://tls.peet.ws/api/all", 2, False),
    ("collect", None, 0, False),
    ("bl-ip", "https://browserleaks.com/ip", 10, True),
    ("bl-dns", "https://browserleaks.com/dns", 22, True),
    ("bl-webrtc", "https://browserleaks.com/webrtc", 12, True),
    ("bl-tls", "https://browserleaks.com/tls", 10, True),
    ("ipleak", "https://ipleak.net/", 32, True),
    ("testipv6", "https://test-ipv6.com/", 40, True),
]
SITES_PXT = ["ipify", "peet", "collect", "bl-ip", "whoer"]
EXTRA_SITES = {"whoer": ("whoer", "https://whoer.net/", 20, True)}


async def wait_title(get_title, want: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if await get_title() == want:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


# --------------------------------------------------------------------------- ProfilePilot configs


async def run_pp(cfg: str, pp: SimpleNamespace, store: Any, rt: Any, srv: CollectServer, *, proxy: str | None,
                 lang: str | None = None, timezone: str | None = None, sites: list[str] | None = None,
                 stability: bool = False, dns_policy_test: bool = False, netlog: Path | None = None,
                 local_state: dict[str, Any] | None = None) -> dict[str, Any]:
    run_id = time.strftime("%H%M%S")
    name = f"net-{cfg}-{run_id}"
    extra = [f"--log-net-log={netlog}", "--net-log-capture-mode=Default"] if netlog else []
    launch = pp.LaunchOptions(window="offscreen", lang=lang, timezone=timezone, extra_args=extra)
    profile = store.create_profile(name, proxy_id=proxy, launch=launch, tags=["audit"])
    if local_state:  # experiment: seed Chrome's Local State before the first launch
        udd = store.user_data_dir(profile.id)
        udd.mkdir(parents=True, exist_ok=True)
        (udd / "Local State").write_text(json.dumps(local_state), encoding="utf-8")
    proxied = proxy is not None
    launch_dump = launch.model_dump(mode="json")
    launch_dump["extra_args"] = ["--log-net-log=<scratch netlog file>" if a.startswith("--log-net-log=") else a
                                 for a in launch_dump.get("extra_args") or []]
    res: dict[str, Any] = {"cfg": cfg, "profile": {"name": name, "proxy": proxy, "launch": launch_dump,
                                                   "seeded_local_state": local_state},
                           "sites": {}, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    wanted = sites or [s[0] for s in SITES]
    site_defs = {s[0]: s for s in SITES} | EXTRA_SITES
    mon = ConnMonitor()
    t0 = time.time()
    async with pp.BrowserManager(store, rt) as bm:
        state = pp.AppState(store=store, runtime=rt, browsers=bm, policy=pp.UrlPolicy())
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
        session = await bm.session(name)
        info = session.runtime
        mon.start(info.chrome_pid, info.host_pid)
        res["runtime"] = {"browser_version": info.browser_version, "window": info.window, "relay": bool(info.relay_port),
                          "upstream": info.upstream}
        try:
            import psutil
            argv = psutil.Process(info.chrome_pid).cmdline()[1:]
            res["browser_cmdline"] = [re.sub(r"(--user-data-dir=).*", r"\1<fresh profile dir>",
                                             re.sub(r"(--log-net-log=).*", r"\1<scratch netlog file>", a)) for a in argv]
        except Exception as exc:
            res["browser_cmdline"] = f"error: {exc}"

        async def nav(url: str, timeout_s: float = 60.0) -> str:
            try:
                out = await pp.tools_browser.browser_navigate(ctx, name, url, timeout_s=timeout_s)
                return (out or "").strip().splitlines()[0][:300] if out else ""
            except Exception as exc:
                return f"ERROR {type(exc).__name__}: {str(exc)[:300]}"

        for key in wanted:
            k, url, wait, shot = site_defs[key]
            rec: dict[str, Any] = {"t_s": round(time.time() - t0, 1)}
            try:
                if key == "collect":
                    url = srv.url(cfg)
                    rec["url"] = url
                    rec["navigate"] = await nav(url)
                    page = await session.page(None, interactive=False)
                    rec["done"] = await wait_title(page.title, "done", 30)
                    body = await page.evaluate(BODY_JS)
                    raw = await page.evaluate("() => document.getElementById('out').textContent")
                    try:
                        rec["data"] = json.loads(raw)
                    except Exception:
                        rec["data_raw"] = (raw or "")[:2000]
                    cdp = await session.browser.new_browser_cdp_session()
                    try:
                        tg = (await cdp.send("Target.getTargets"))["targetInfos"]
                    finally:
                        await cdp.detach()
                    rec["targets"] = [{"type": t["type"], "url": (t.get("url") or "")[:120]} for t in tg
                                      if t.get("type") in ("iframe", "worker", "shared_worker", "service_worker")]
                    d = rec.get("data") or {}
                    note_exit_ip(d.get("fetchIpify"), proxied)
                    note_exit_ip(d.get("fetchIpify64"), proxied)
                else:
                    rec["url"] = url
                    rec["navigate"] = await nav(url)
                    await asyncio.sleep(wait)
                    page = await session.page(None, interactive=False)
                    body = await page.evaluate(BODY_JS)
                    rec["title"] = body.get("title")
                    rec["final_url"] = body.get("url")
                    if body.get("pre"):
                        try:
                            rec["json"] = json.loads(body["pre"])
                        except Exception:
                            rec["pre"] = body["pre"][:20000]
                    rec["text"] = (body.get("text") or "")[:60000]
                    if key == "ipify":
                        note_exit_ip((rec.get("json") or {}).get("ip"), proxied)
                    if key == "bl-dns" and not proxied:
                        register_resolver_cities(rec["text"])
                    if key == "peet":
                        note_exit_ip((rec.get("json") or {}).get("ip", "").rsplit(":", 1)[0] or None, proxied)
                    if shot and proxied:
                        rec["screenshot"] = await page_shot(page, AUDIT / "img" / f"network-{key}-{cfg}.jpg")
            except Exception as exc:
                rec["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
                rec["traceback"] = traceback.format_exc()[-1500:]
            res["sites"][key] = rec
            say(f"  [{cfg}] {key}: {rec.get('navigate', '')[:90]} {('ERR ' + rec['error'][:120]) if rec.get('error') else ''}")

        # http_fetch (the MCP tool function) - exit IP and what the server sees of it.
        hf: dict[str, Any] = {}
        for engine in ("httpx", "scrapling"):
            for key, url in (("ipify", "https://api.ipify.org?format=json"), ("peet", "https://tls.peet.ws/api/all")):
                try:
                    out = await pp.tools_data.http_fetch(ctx, name, url, engine=engine, format="raw", timeout_s=40)
                    head, _, bodytxt = out.partition("\n\n")
                    item: dict[str, Any] = {"head": head}
                    try:
                        item["json"] = json.loads(bodytxt)
                    except Exception:
                        item["body"] = bodytxt[:3000]
                    if key == "ipify":
                        note_exit_ip((item.get("json") or {}).get("ip"), proxied)
                    hf[f"{engine}:{key}"] = item
                except Exception as exc:
                    hf[f"{engine}:{key}"] = {"error": f"{type(exc).__name__}: {str(exc)[:400]}"}
        res["http_fetch"] = hf
        say(f"  [{cfg}] http_fetch: " + ", ".join(f"{k}={'ERR' if 'error' in v else 'ok'}" for k, v in hf.items()))

        if stability:
            rows = []
            for i in range(5):
                t_iter = time.time()
                row: dict[str, Any] = {"i": i, "t_s": round(t_iter - t0, 1)}
                row["navigate"] = await nav("https://api.ipify.org?format=json", 30)
                try:
                    page = await session.page(None, interactive=False)
                    row["browser_ip"] = (json.loads((await page.evaluate(BODY_JS)).get("pre") or "{}")).get("ip")
                except Exception as exc:
                    row["browser_ip"] = f"error: {type(exc).__name__}"
                try:
                    out = await pp.tools_data.http_fetch(ctx, name, "https://api.ipify.org?format=json", format="raw", timeout_s=30)
                    row["http_fetch_ip"] = first_ip(out)
                except Exception as exc:
                    row["http_fetch_ip"] = f"error: {type(exc).__name__}: {str(exc)[:200]}"
                note_exit_ip(row.get("browser_ip"), proxied)
                note_exit_ip(row.get("http_fetch_ip"), proxied)
                rows.append(row)
                say(f"  [{cfg}] stability {i}: browser={RED.text(str(row['browser_ip']))} http_fetch={RED.text(str(row['http_fetch_ip']))}")
                if i < 4:
                    await asyncio.sleep(max(0.0, 13.0 - (time.time() - t_iter)))
            res["stability"] = rows

        if dns_policy_test:
            res["dns_policy_test"] = await dns_policy_test_run(pp, state, ctx, name)

        res["relay_stats"] = None
        try:
            res["relay_stats"] = rt.relay_stats(profile.id) if hasattr(rt, "relay_stats") else None
        except Exception as exc:
            res["relay_stats"] = f"error: {type(exc).__name__}"

    # BrowserManager closed (CDP disconnected); for PXT check what the timezone is now.
    if timezone:
        res["after_disconnect"] = await after_disconnect_tz(pp, rt, profile.id, srv, cfg)
    res["connections"] = mon.stop()
    res["closed"] = "RuntimeManager.stop -> " + str(rt.stop(profile.id))
    if netlog:
        res["netlog"] = parse_netlog(netlog)
    res["elapsed_s"] = round(time.time() - t0, 1)
    return res


def parse_netlog(path: Path) -> dict[str, Any]:
    """Summarise a Chrome net-log: every TCP connect / UDP socket that is not loopback, the DNS
    transactions Chrome itself sent (host names), DoH requests and the secure-DNS mode."""
    import collections
    for _ in range(20):
        if path.exists():
            break
        time.sleep(0.5)
    try:
        txt = path.read_text(encoding="utf-8", errors="replace").rstrip()
        try:
            d = json.loads(txt)
        except json.JSONDecodeError:  # unterminated file (Chrome was killed)
            d = json.loads(txt.rstrip(", \n") + "]}")
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    c = d["constants"]
    ev = {v: k for k, v in c["logEventTypes"].items()}
    st = {v: k for k, v in c["logSourceType"].items()}
    by = collections.defaultdict(list)
    for e in d["events"]:
        by[e["source"]["id"]].append(e)

    def loop(addr: str) -> bool:
        return addr.startswith(("127.", "[::1]"))

    tcp = collections.Counter()
    for e in d["events"]:
        if ev.get(e["type"]) == "TCP_CONNECT_ATTEMPT" and (e.get("params") or {}).get("address"):
            a = e["params"]["address"]
            tcp["loopback" if loop(a) else a] += 1
    udp = collections.Counter()
    for evs in by.values():
        conns = [e["params"].get("address") for e in evs if ev.get(e["type"]) == "UDP_CONNECT" and e.get("params")]
        if conns and conns[0]:
            sent = sum(1 for e in evs if ev.get(e["type"]) == "UDP_BYTES_SENT")
            udp[f"{conns[0]} datagrams_sent={sent}"] += 1
    dns = collections.Counter(f"{e['params'].get('hostname')} qtype={e['params'].get('query_type')}" for e in d["events"]
                              if ev.get(e["type"]) == "DNS_TRANSACTION" and (e.get("params") or {}).get("hostname"))
    doh_urls = collections.Counter()
    for evs in by.values():
        if st.get(evs[0]["source"]["type"]) == "DNS_OVER_HTTPS":
            for e in evs:
                u = (e.get("params") or {}).get("url")
                if u:
                    doh_urls[u.split("?", 1)[0] + "?dns=<query>"] += 1
                    break
    modes = [{"secure_dns_mode": e["params"].get("secure_dns_mode"),
              "doh_templates": [x.get("template") for x in (e["params"].get("doh_config") or {}).get("servers") or []]}
             for e in d["events"] if ev.get(e["type"]) == "DNS_CONFIG_CHANGED" and e.get("params")]
    jobs = collections.Counter()
    for e in d["events"]:
        if ev.get(e["type"]) == "HOST_RESOLVER_MANAGER_JOB" and (e.get("params") or {}).get("host"):
            jobs[e["params"]["host"]] += 1
    # Attribute every non-loopback TCP socket: walk the source_dependency graph to the request that used it.
    links = collections.defaultdict(set)
    for e in d["events"]:
        dep = (e.get("params") or {}).get("source_dependency")
        if dep:
            links[dep["id"]].add(e["source"]["id"])
            links[e["source"]["id"]].add(dep["id"])
    attributed: dict[str, list[str]] = {}
    for sid, evs in by.items():
        addrs = [e["params"]["address"] for e in evs if ev.get(e["type"]) == "TCP_CONNECT_ATTEMPT"
                 and (e.get("params") or {}).get("address") and not loop(e["params"]["address"])]
        if not addrs:
            continue
        seen, todo = set(), [sid]
        while todo and len(seen) < 400:
            x = todo.pop()
            if x not in seen:
                seen.add(x)
                todo += list(links.get(x, ()))
        kinds = sorted({st.get(by[x][0]["source"]["type"], "?") for x in seen if by.get(x)})
        for a in addrs:
            attributed.setdefault(a, [])
            attributed[a] = sorted(set(attributed[a]) | set(kinds))
    for e in d["events"]:  # label well-known infrastructure for the redacted output
        pr = e.get("params") or {}
        if ev.get(e["type"]) == "DNS_CONFIG_CHANGED":
            for ns in pr.get("nameservers") or []:
                RED.add_ip(ns.rsplit(":", 1)[0].strip("[]"), "SYSTEM_DNS_SERVER")
    for a, kinds in attributed.items():
        if "DNS_OVER_HTTPS" in kinds:
            RED.add_ip(a.rsplit(":", 1)[0].strip("[]"), "SYSTEM_DOH_SERVER")
    for key in udp:
        addr = key.split(" ", 1)[0]
        if key.endswith("datagrams_sent=0") and addr.startswith("["):
            RED.add_ip(addr.rsplit(":", 1)[0].strip("[]"), "IPV6_REACHABILITY_PROBE_TARGET")
    return {"tcp_connect_attempts": dict(tcp), "tcp_direct_attributed_to": attributed, "udp_sockets": dict(udp),
            "dns_transactions": dict(dns), "doh_requests": dict(doh_urls), "resolver_jobs_that_reached_dns": dict(jobs),
            "dns_config": modes, "events": len(d["events"])}


async def dns_policy_test_run(pp: SimpleNamespace, state: Any, ctx: Any, name: str) -> dict[str, Any]:
    """Do URL-policy checks resolve hostnames through the OS resolver (= the ISP's DNS)?

    Unique NXDOMAIN names under example.com are opened (browser_navigate) / fetched (http_fetch) in
    local mode and in remote mode (UrlPolicy(remote=True), what ``profilepilot serve --http`` uses).
    Right after each step the Windows DNS client cache is queried for exactly that name (nothing
    else is read). A control name resolved with getaddrinfo proves the cache method works.
    """
    tag = pysecrets.token_hex(4)
    names = {k: f"pp-audit-{tag}-{k}.example.com" for k in ("control", "localnav", "localfetch", "remotenav", "remotefetch")}
    out: dict[str, Any] = {"names": names, "steps": {}, "in_os_dns_cache": {}}

    def cached(host: str) -> list[dict[str, Any]]:
        ps = (f"@(Get-DnsClientCache -ErrorAction SilentlyContinue | Where-Object {{ $_.Entry -eq '{host}' }} | "
              "Select-Object Entry, Status, Type) | ConvertTo-Json -Compress")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=90)
            raw = (r.stdout or "").strip()
            ents = json.loads(raw) if raw else []
            return [ents] if isinstance(ents, dict) else ents
        except Exception as exc:
            return [{"error": f"{type(exc).__name__}: {exc}"}]

    try:
        socket.getaddrinfo(names["control"], 443)
        out["steps"]["control"] = "resolved?!"
    except Exception as exc:
        out["steps"]["control"] = f"socket.getaddrinfo -> {type(exc).__name__}"
    out["in_os_dns_cache"]["control"] = cached(names["control"])
    for mode, policy in (("local", pp.UrlPolicy()), ("remote", pp.UrlPolicy(remote=True))):
        state.policy = policy
        for kind in ("nav", "fetch"):
            key = f"{mode}{kind}"
            host = names[key]
            try:
                if kind == "nav":
                    r = await pp.tools_browser.browser_navigate(ctx, name, f"https://{host}/", timeout_s=20)
                else:
                    r = await pp.tools_data.http_fetch(ctx, name, f"https://{host}/", timeout_s=20)
                out["steps"][key] = (r or "")[:200]
            except Exception as exc:
                out["steps"][key] = f"{type(exc).__name__}: {str(exc)[:200]}"
            out["in_os_dns_cache"][key] = cached(host)
    state.policy = pp.UrlPolicy()
    out["resolved_locally"] = {k: bool(v) and "error" not in v[0] for k, v in out["in_os_dns_cache"].items()}
    return out


async def after_disconnect_tz(pp: SimpleNamespace, rt: Any, profile_id: str, srv: CollectServer, cfg: str) -> dict[str, Any]:
    """After the Playwright connection is closed: open the collector with ONE raw CDP Page.navigate
    (no Runtime.enable, no emulation) and read it from an isolated world."""
    import httpx
    out: dict[str, Any] = {}
    try:
        info = rt.status(profile_id)
        ver = httpx.get(f"http://127.0.0.1:{info.cdp_port}/json/version", timeout=5, trust_env=False).json()
        async with rd.RawCDP(ver["webSocketDebuggerUrl"]) as cdp:
            tab = next(t for t in (await cdp.send("Target.getTargets"))["targetInfos"] if t["type"] == "page")
            sid = (await cdp.send("Target.attachToTarget", {"targetId": tab["targetId"], "flatten": True}))["sessionId"]
            await cdp.send("Page.navigate", {"url": srv.url(cfg + "-after-disconnect")}, sid)
            await asyncio.sleep(1.0)
            for _ in range(60):
                val = await iso_eval(cdp, sid, "document.title")
                if val == "done":
                    break
                await asyncio.sleep(0.5)
            raw = await iso_eval(cdp, sid, "document.getElementById('out') ? document.getElementById('out').textContent : null")
            out["data"] = json.loads(raw) if raw and raw.startswith("{") else raw
            await cdp.send("Target.detachFromTarget", {"sessionId": sid})
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return out


async def iso_eval(cdp: Any, sid: str, expr: str) -> Any:
    tree = (await cdp.send("Page.getFrameTree", {}, sid))["frameTree"]
    world = await cdp.send("Page.createIsolatedWorld", {"frameId": tree["frame"]["id"], "worldName": "pp-net-audit"}, sid)
    res = await cdp.send("Runtime.evaluate", {"expression": expr, "contextId": world["executionContextId"],
                                              "returnByValue": True, "awaitPromise": True}, sid)
    return (res.get("result") or {}).get("value")


# --------------------------------------------------------------------------- plain Chrome configs

B0_STEPS = [
    ("ipify", "https://api.ipify.org?format=json", 3),
    ("peet", "https://tls.peet.ws/api/all", 3),
    ("collect", None, 0),
    ("bl-tls", "https://browserleaks.com/tls", 10),
    ("bl-webrtc", "https://browserleaks.com/webrtc", 12),
    ("bl-dns", "https://browserleaks.com/dns", 22),
]


async def run_plain(cfg: str, pp: SimpleNamespace, base: Path, srv: CollectServer, upstream: Any | None) -> dict[str, Any]:
    import httpx
    run_id = time.strftime("%H%M%S")
    udd = base / "plain" / f"{cfg}-{run_id}"
    udd.mkdir(parents=True, exist_ok=True)
    (udd / "First Run").write_bytes(b"")
    browser = pp.find_browser()
    port = pp.free_port()
    relay = None
    res: dict[str, Any] = {"cfg": cfg, "sites": {}, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if upstream is not None:
        relay = pp.LocalRelay(upstream)
        await relay.start()
    argv = [f"--user-data-dir={udd}", f"--remote-debugging-port={port}", "--window-position=-32000,-32000"]
    if relay is not None:
        argv.append(f"--proxy-server=socks5://127.0.0.1:{relay.port}")
    argv.append("about:blank")
    res["argv"] = [re.sub(r"(--user-data-dir=).*", r"\1<fresh dir>", a) for a in argv]
    res["browser_version"] = browser.version
    proc = subprocess.Popen([browser.path, *argv])
    mon = ConnMonitor().start(proc.pid)
    t0 = time.time()
    try:
        ver = None
        for _ in range(100):
            try:
                ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=1, trust_env=False).json()
                break
            except Exception:
                await asyncio.sleep(0.3)
        if not ver:
            raise RuntimeError(f"DevTools endpoint did not answer (exit code {proc.poll()})")
        async with rd.RawCDP(ver["webSocketDebuggerUrl"]) as cdp:
            tab = next(t for t in (await cdp.send("Target.getTargets"))["targetInfos"] if t["type"] == "page")
            sid = (await cdp.send("Target.attachToTarget", {"targetId": tab["targetId"], "flatten": True}))["sessionId"]

            async def send(method: str, params: dict | None = None) -> dict:
                return await cdp.send(method, params, sid)

            for key, url, wait in B0_STEPS:
                rec: dict[str, Any] = {"t_s": round(time.time() - t0, 1)}
                try:
                    if key == "collect":
                        url = srv.url(cfg)
                    rec["url"] = url
                    rec["navigate"] = await cdp.send("Page.navigate", {"url": url}, sid, timeout=60)
                    await asyncio.sleep(1.5 + wait)
                    if key == "collect":
                        for _ in range(60):
                            if await iso_eval(cdp, sid, "document.title") == "done":
                                break
                            await asyncio.sleep(0.5)
                        raw = await iso_eval(cdp, sid, "document.getElementById('out').textContent")
                        rec["data"] = json.loads(raw) if raw and raw.startswith("{") else raw
                        d = rec["data"] if isinstance(rec["data"], dict) else {}
                        note_exit_ip(d.get("fetchIpify"), upstream is not None)
                        note_exit_ip(d.get("fetchIpify64"), upstream is not None)
                    else:
                        for _ in range(40):  # wait for the document to exist
                            if await iso_eval(cdp, sid, "document.readyState") in ("interactive", "complete"):
                                break
                            await asyncio.sleep(0.5)
                        body = await iso_eval(cdp, sid, "(" + BODY_JS + ")()")
                        rec["title"] = body.get("title")
                        rec["final_url"] = body.get("url")
                        if body.get("pre"):
                            try:
                                rec["json"] = json.loads(body["pre"])
                            except Exception:
                                rec["pre"] = body["pre"][:20000]
                        rec["text"] = (body.get("text") or "")[:60000]
                        if key == "ipify":
                            note_exit_ip((rec.get("json") or {}).get("ip"), upstream is not None)
                        if key == "bl-dns" and upstream is None:
                            register_resolver_cities(rec["text"])
                        if key == "peet":
                            note_exit_ip(((rec.get("json") or {}).get("ip") or "").rsplit(":", 1)[0] or None, upstream is not None)
                except Exception as exc:
                    rec["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
                res["sites"][key] = rec
                say(f"  [{cfg}] {key}: {'ERR ' + rec['error'][:150] if rec.get('error') else 'ok'}")
            try:
                await cdp.send("Browser.close", timeout=5)
            except Exception:
                pass
    finally:
        res["connections"] = mon.stop()
        try:
            proc.wait(15)
            res["closed"] = "Browser.close"
        except subprocess.TimeoutExpired:
            pp.kill_tree(proc.pid)
            res["closed"] = "killed"
        n = kill_leftovers(str(udd))
        if n:
            res["closed"] += f" (+{n} leftovers killed)"
        if relay is not None:
            res["relay_stats"] = {k: v for k, v in relay.stats.as_dict().items() if k != "last_error"}
            await relay.stop()
    res["elapsed_s"] = round(time.time() - t0, 1)
    return res


# --------------------------------------------------------------------------- proxy test (CLI)


def cli_proxy_test(base: Path, args: list[str]) -> dict[str, Any]:
    env = dict(os.environ, PROFILEPILOT_HOME=str(base), PROFILEPILOT_SECRETS="file",
               PYTHONPATH=str(REPO / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""))
    cmd = [sys.executable, "-m", "profilepilot", "proxy", "test", *args, "--json", "--timeout", "20"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120)
    try:
        data = json.loads(r.stdout)
    except Exception:
        data = {"stdout": (r.stdout or "")[:2000]}
    return {"command": "profilepilot proxy test " + " ".join(args) + " --json", "exit_code": r.returncode,
            "result": data, "stderr": (r.stderr or "")[-1500:]}


# --------------------------------------------------------------------------- main


def save(name: str, data: dict[str, Any]) -> bool:
    data = RED.obj(data)
    text = json.dumps(data, indent=1, ensure_ascii=False)
    leaks = sorted(set(RED.leaks(text)))
    if leaks:
        say(f"REFUSING to write {name}: unredacted values remain ({leaks})")
        return False
    path = AUDIT / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    say(f"wrote raw/{name} ({len(text) // 1024} KB)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--configs", default="P,PX,PXH,PXT,B0D,B0DX")
    args = ap.parse_args()
    wanted = [c.strip().upper() for c in args.configs.split(",") if c.strip()]

    saved_env = {k: os.environ.pop(k, None) for k in ("PROFILEPILOT_HOME", "PROFILEPILOT_SECRETS")}
    pp = import_profilepilot()
    default_root = pp.data_root()
    src_store = pp.Store(default_root, secrets=pp.SecretStore(default_root))  # the user's store (keyring)
    socks = src_store.proxy_endpoint("test-proxy-socks5")
    for k, v in saved_env.items():
        if v is not None:
            os.environ[k] = v
    base = Path(args.scratch).resolve() / "audit-home" / "network"
    base.mkdir(parents=True, exist_ok=True)
    os.environ["PROFILEPILOT_HOME"] = str(base)
    os.environ["PROFILEPILOT_SECRETS"] = "file"

    RED.secret(socks.username, "PROXY_USER")
    RED.secret(socks.password, "PROXY_PASS")
    if socks.username:  # ProxyEndpoint.redacted() keeps the first 3 characters of the user name
        RED.secret(socks.username[:3] + "***", "PROXY_USER")
    store = pp.Store(base)
    assert store.secrets.backend == "file", store.secrets.backend
    have = {p.name for p in store.list_proxies()}
    if "audit-socks5" not in have:
        store.add_proxy(socks, "audit-socks5", tags=["audit"])
    if "audit-http" not in have:
        store.add_proxy(pp.ProxyEndpoint("http", socks.host, socks.port, socks.username, socks.password), "audit-http", tags=["audit"])
    proxy_host = socks.host
    upstream_socks = socks
    del socks

    # Real identity -> REAL_* labels (never printed raw).
    found = rd.discover_identity(pp)
    # run_detectors' generic "5 digits near a zip label" and "any DMS coordinate" patterns would tag
    # the PROXY's postal code / coordinates as REAL_*: give them neutral labels (still redacted).
    neutral = {"near-label-zip": "POSTAL_CODE", "DMS": "COORD_DMS"}
    RED.patterns = [(rx, neutral.get(raw, label), raw) for rx, label, raw in RED.patterns]
    if "near-label-zip" in RED.repl:
        RED.repl["near-label-zip"] = r"\g<1>\g<2>POSTAL_CODE"
    extra = Path(args.scratch) / "det-redact-extra.json"
    n_extra = rd.load_extra_terms(extra) if extra.exists() else 0
    if not any(lbl == "REAL_IP" for lbl in RED.known_ips.values()):
        say("could not determine the real IP; refusing to continue")
        return 2
    say(f"identity: v4={found.get('v4')} v6={found.get('v6')} country={found.get('country_code')} "
        f"tz={found.get('timezone')} extra_terms={n_extra}")

    # proxy_test via the CLI (the user-facing path), before the browser runs.
    ptest: dict[str, Any] = {"before": {}, "after": {}}
    ptest["before"]["direct"] = cli_proxy_test(base, ["--direct"])
    note_exit_ip((ptest["before"]["direct"]["result"] or {}).get("ip"), False)
    for name in ("audit-socks5", "audit-http"):
        ptest["before"][name] = cli_proxy_test(base, [name])
    exit_ips = {(ptest["before"][n]["result"] or {}).get("ip") for n in ("audit-socks5", "audit-http")} - {None}
    for ip in sorted(exit_ips):
        RED.add_ip(ip, "PROXY_EXIT_IP" if len(exit_ips) == 1 else f"PROXY_EXIT_IP_{sorted(exit_ips).index(ip) + 1}")
    if proxy_host in exit_ips:
        ptest["exit_ip_equals_proxy_host"] = True
    else:
        ptest["exit_ip_equals_proxy_host"] = False
        RED.add_ip(proxy_host, "PROXY_HOST")
        RED.secret(proxy_host, "PROXY_HOST")
    proxy_tz = (ptest["before"]["audit-socks5"]["result"] or {}).get("timezone")
    say("proxy_test before: " + json.dumps(RED.obj({k: {kk: v["result"].get(kk) for kk in ("ok", "ip", "country_code", "timezone", "isp", "latency_ms")}
                                                    for k, v in ptest["before"].items() if isinstance(v.get("result"), dict)})))

    rt = pp.RuntimeManager(store)
    srv = CollectServer().start()
    outputs: dict[str, dict[str, Any]] = {}
    try:
        for cfg in wanted:
            say(f"=== {cfg}")
            try:
                if cfg == "P":
                    out = asyncio.run(run_pp(cfg, pp, store, rt, srv, proxy=None))
                elif cfg == "PX":
                    out = asyncio.run(run_pp(cfg, pp, store, rt, srv, proxy="audit-socks5", stability=True, dns_policy_test=True))
                elif cfg == "PXH":
                    out = asyncio.run(run_pp(cfg, pp, store, rt, srv, proxy="audit-http", stability=True))
                elif cfg == "PXT":
                    out = asyncio.run(run_pp(cfg, pp, store, rt, srv, proxy="audit-socks5", lang="en-US",
                                             timezone=proxy_tz or "America/New_York", sites=SITES_PXT))
                elif cfg == "PXW":  # tz alignment control without the override (same sites as PXT)
                    out = asyncio.run(run_pp(cfg, pp, store, rt, srv, proxy="audit-socks5", sites=SITES_PXT))
                elif cfg == "PXN":  # diagnosis: PX + Chrome net-log (which connections bypass the relay?)
                    out = asyncio.run(run_pp(cfg, pp, store, rt, srv, proxy="audit-socks5", sites=["ipify", "collect", "bl-dns"],
                                             netlog=Path(args.scratch) / f"netlog-PXN-{time.strftime('%H%M%S')}.json"))
                elif cfg == "PXND":  # candidate fix: PXN + Local State dns_over_https.mode=off seeded before launch
                    out = asyncio.run(run_pp(cfg, pp, store, rt, srv, proxy="audit-socks5", sites=["ipify", "collect", "bl-dns"],
                                             netlog=Path(args.scratch) / f"netlog-PXND-{time.strftime('%H%M%S')}.json",
                                             local_state={"dns_over_https": {"mode": "off"}}))
                elif cfg == "PXHN":  # PXN with the HTTP upstream
                    out = asyncio.run(run_pp(cfg, pp, store, rt, srv, proxy="audit-http", sites=["ipify", "collect"],
                                             netlog=Path(args.scratch) / f"netlog-PXHN-{time.strftime('%H%M%S')}.json"))
                elif cfg == "B0D":
                    out = asyncio.run(run_plain(cfg, pp, base, srv, None))
                elif cfg == "B0DX":
                    out = asyncio.run(run_plain(cfg, pp, base, srv, upstream_socks))
                else:
                    say(f"unknown config {cfg}")
                    continue
            except Exception as exc:
                out = {"cfg": cfg, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-3000:]}
                say(f"  {cfg} failed: {out['error'][:300]}")
            out["server_posted"] = srv.results.get(cfg)
            if cfg == "PXT":
                out["server_posted_after_disconnect"] = srv.results.get(cfg + "-after-disconnect")
            outputs[cfg] = out
            save(f"network-{cfg}.json", out)  # write as we go (re-written at the end with all labels)
    finally:
        srv.stop()
        try:
            stopped = rt.stop_all()
            if stopped:
                say(f"stop_all stopped: {stopped}")
        except Exception as exc:
            say(f"stop_all failed: {exc}")
        n = kill_leftovers(str(base))
        say(f"leftover chrome.exe processes under {base.name}: {n} killed; remaining: {rd.leftovers(str(base))}")

    ptest["after"]["direct"] = cli_proxy_test(base, ["--direct"])
    for name in ("audit-socks5", "audit-http"):
        ptest["after"][name] = cli_proxy_test(base, [name])
        note_exit_ip((ptest["after"][name]["result"] or {}).get("ip"), True)
    ptest["identity"] = {k: v for k, v in found.items() if k in ("v4", "v6", "country_code", "timezone")}
    ptest["redaction_labels"] = RED.labels()
    save("network-proxy-test.json", ptest)
    for cfg, out in outputs.items():  # re-redact with every label found during the run
        save(f"network-{cfg}.json", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
