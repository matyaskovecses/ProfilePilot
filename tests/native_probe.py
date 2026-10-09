"""Local probe for the native-fingerprint regression tests (``tests/test_native_fingerprint.py``).

A trimmed copy of ``docs/audit/scripts/probe_server.py`` (the fingerprint audit's probe): only the
checks that tell an attached automation client apart from a person's Chrome. Everything is
collected by the page's own script and POSTed back, so a browser without any CDP client is
measured exactly like a ProfilePilot profile driven over MCP.

Flow of one configuration ``cfg`` (each step a normal navigation in the same tab):

1. ``/start?cfg=X`` - back/forward-cache page A; on its first show it navigates to ``/bfb``.
2. ``/bfb?cfg=X`` - ``history.back()``; page A reports whether it came from the bfcache
   (phase ``bfcache``) and opens the probe.
3. ``/probe?cfg=X`` - an inline ``<head>`` script that, before anything else, runs the CDP
   ``Runtime.enable`` detectors (``Error.prepareStackTrace`` called by ``console.debug(new
   Error())``, and ``console.debug(<3000-key object>)`` x100 timing) and installs main-world DOM
   call traps. It then collects ``navigator`` / ``userAgentData`` / ``window.chrome`` / the
   global-property hash, user activation, the AudioContext state, ``document.hasFocus()``, the same
   CDP detectors inside a dedicated worker and the asynchronous variants (uncaught exception,
   unhandled rejection), and posts phase ``main``. Afterwards it re-runs the detectors (and reads
   ``document.hasFocus()`` and the sticky user activation) every 500 ms until the test calls :meth:`ProbeServer.finish` (or ``max`` seconds pass) and posts
   phase ``final`` with those samples and every trapped main-world call seen so far.

Not used: the classic ``Error.stack`` getter detector (rebrowser's ``runtimeEnableLeak``). It is
dead on Chrome 154 (V8 no longer calls the accessor) and stays green while Playwright is attached.

The request headers of every request are recorded too; ``/start`` asks for the high-entropy client
hints with ``Accept-CH`` so ``/probe`` carries them. Only 127.0.0.1 is contacted.

Timezone (``/tz?cfg=X``, FIX-PLAN step 6): the page reads its time zone in its first script, starts a
dedicated, a shared and a service worker and a cross-site iframe from ``localhost`` (the page is on
``127.0.0.1``: another site, so Chrome runs it out of process), which starts its own dedicated worker.
It posts phase ``ready`` (first readings), then waits for :meth:`ProbeServer.finish` and posts phase
``late`` with a fresh reading from every context.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

ACCEPT_CH = ", ".join([
    "Sec-CH-UA", "Sec-CH-UA-Mobile", "Sec-CH-UA-Platform", "Sec-CH-UA-Arch", "Sec-CH-UA-Bitness",
    "Sec-CH-UA-Full-Version", "Sec-CH-UA-Full-Version-List", "Sec-CH-UA-Model", "Sec-CH-UA-Platform-Version",
    "Sec-CH-UA-WoW64",
])

#: A page global, so a main-world evaluate has something only the main world can see.
PAGE_GLOBAL = "probeVersion"
PAGE_GLOBAL_VALUE = "native-probe-1"

_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>__TITLE__</title>
"""

START_PAGE = _HEAD.replace("__TITLE__", "probe start") + r"""
<script>
(() => {
  'use strict';
  const Q = new URLSearchParams(location.search);
  const CFG = Q.get('cfg') || 'manual';
  const MAX = Q.get('max') || '60';
  const KEY = 'pp-bf-left-' + CFG;
  const next = '/probe?cfg=' + encodeURIComponent(CFG) + '&max=' + encodeURIComponent(MAX);
  const post = (data) => fetch('/result?cfg=' + encodeURIComponent(CFG) + '&phase=bfcache', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
  }).catch(() => {});
  window.addEventListener('pageshow', async (ev) => {
    let state = null;
    try { state = sessionStorage.getItem(KEY); } catch (e) {}
    if (ev.persisted || state === '1') {
      try { sessionStorage.removeItem(KEY); } catch (e) {}
      await post({persisted: ev.persisted, historyLength: history.length});
      location.href = next;
      return;
    }
    try { sessionStorage.setItem(KEY, '1'); } catch (e) {}
    setTimeout(() => { location.href = '/bfb?cfg=' + encodeURIComponent(CFG) + '&max=' + encodeURIComponent(MAX); }, 200);
  });
})();
</script>
</head><body><h1>Probe start</h1><p>Back/forward cache test.</p></body></html>
"""

BFB_PAGE = _HEAD.replace("__TITLE__", "probe bfcache B") + r"""
<script>
(() => {
  'use strict';
  const Q = new URLSearchParams(location.search);
  window.addEventListener('load', () => setTimeout(() => history.back(), 200));
  setTimeout(() => {  // history.back() did not leave this page
    location.href = '/probe?cfg=' + encodeURIComponent(Q.get('cfg') || 'manual') + '&max=' + encodeURIComponent(Q.get('max') || '60');
  }, 5000);
})();
</script>
</head><body><h1>Probe page B</h1><p>Going back.</p></body></html>
"""

PROBE_PAGE = _HEAD.replace("__TITLE__", "probe") + r"""
<script>
(() => {
'use strict';
const T0 = performance.now();
const Q = new URLSearchParams(location.search);
const CFG = Q.get('cfg') || 'manual';
const MAX_S = Math.max(1, Math.min(300, parseInt(Q.get('max') || '60', 10) || 60));
const SELF = location.origin + location.pathname;
const R = {cfg: CFG, errors: {}};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ms = (t) => Math.round((performance.now() - t) * 100) / 100;

// ---------------------------------------------------------------- CDP (Runtime.enable) detectors
// With the Runtime domain enabled by a client, V8's inspector turns every console argument into a
// RemoteObject: it formats the stack of an Error (calling Error.prepareStackTrace) and builds
// previews of objects (slow for big ones). Plain Chrome with DevTools closed does neither.
function prepareStackTraceProbe() {
  let called = false;
  const old = Error.prepareStackTrace;
  Error.prepareStackTrace = () => { called = true; return 'x'; };
  try { console.debug(new Error('probe')); } catch (e) { /* ignore */ }
  if (old === undefined) delete Error.prepareStackTrace; else Error.prepareStackTrace = old;
  return called;
}
const BIG = {};
for (let i = 0; i < 3000; i++) BIG['k' + i] = {v: i};
function consoleTimingProbe() {
  const t = performance.now();
  for (let i = 0; i < 100; i++) console.debug(BIG);
  return ms(t);
}
const cdpProbes = () => ({prepareStackTrace: prepareStackTraceProbe(), consoleTimingMs: consoleTimingProbe()});
// Uncaught exception / unhandled rejection while prepareStackTrace is set: the inspector formats the
// stack for Runtime.exceptionThrown only when a client listens. No preventDefault (a cancelled
// error is not reported at all).
function asyncProbes() {
  return new Promise((resolve) => {
    const res = {uncaughtPrepare: false, rejectionPrepare: false};
    const old = Error.prepareStackTrace;
    const restore = () => { if (old === undefined) delete Error.prepareStackTrace; else Error.prepareStackTrace = old; };
    Error.prepareStackTrace = () => { res.uncaughtPrepare = true; return 'x'; };
    setTimeout(() => { throw new Error('probe-uncaught'); }, 0);
    setTimeout(() => {
      Error.prepareStackTrace = () => { res.rejectionPrepare = true; return 'x'; };
      Promise.reject(new Error('probe-rejection'));
      setTimeout(() => { restore(); resolve(res); }, 250);
    }, 250);
  });
}
// The same console detectors inside a dedicated worker (clients auto-attach to workers).
const WORKER_SRC = [
  "const BIG = {}; for (let i = 0; i < 3000; i++) BIG['k' + i] = {v: i};",
  "function pst() { let called = false; const old = Error.prepareStackTrace;",
  "  Error.prepareStackTrace = () => { called = true; return 'x'; };",
  "  try { console.debug(new Error('probe')); } catch (e) {}",
  "  if (old === undefined) delete Error.prepareStackTrace; else Error.prepareStackTrace = old; return called; }",
  "function timing() { const t = performance.now(); for (let i = 0; i < 100; i++) console.debug(BIG);",
  "  return Math.round((performance.now() - t) * 100) / 100; }",
  "self.onmessage = () => { const a = pst(); const t = timing(); const b = pst();",
  "  postMessage({prepareStackTrace: a || b, consoleTimingMs: t, userAgent: navigator.userAgent}); };",
].join('\n');
function workerProbe() {
  const url = URL.createObjectURL(new Blob([WORKER_SRC], {type: 'text/javascript'}));
  const w = new Worker(url);
  return new Promise((resolve) => {
    const done = (v) => { w.terminate(); URL.revokeObjectURL(url); resolve(v); };
    w.onmessage = (e) => done(e.data);
    w.onerror = () => done({error: 'worker error'});
    setTimeout(() => done({error: 'worker timed out'}), 5000);
    w.postMessage(1);
  });
}
const early = Object.assign({t_ms: ms(T0)}, cdpProbes(), {hasFocus: document.hasFocus()});

// ---------------------------------------------------------------- main-world call traps
// Count calls of common DOM APIs whose caller is not this page's own script, e.g. code a CDP client
// evaluates in the main world (Runtime.evaluate / callFunctionOn). Isolated worlds have their own
// prototypes and never reach these.
const ext = {count: 0, by: {}, samples: []};
function rawStack() {
  const p = Error.prepareStackTrace;  // a detector may be installed right now: use V8's format
  if (p !== undefined) delete Error.prepareStackTrace;
  const s = String((new Error()).stack || '');
  if (p !== undefined) Error.prepareStackTrace = p;
  return s;
}
function note(label) {
  const lines = rawStack().split('\n');
  const caller = lines[4] || '';  // [0] Error [1] rawStack [2] note [3] trap wrapper [4] caller
  if (caller.includes(SELF)) return;
  ext.count++;
  ext.by[label] = (ext.by[label] || 0) + 1;
  if (ext.samples.length < 10) ext.samples.push({label, frames: lines.slice(4, 8).map((s) => s.trim().slice(0, 200))});
}
function trapMethod(obj, name, label) {
  const d = Object.getOwnPropertyDescriptor(obj, name);
  if (!d || typeof d.value !== 'function') return false;
  const orig = d.value;
  const wrapped = {[name](...args) { note(label); return orig.apply(this, args); }}[name];
  Object.defineProperty(obj, name, Object.assign({}, d, {value: wrapped}));
  return true;
}
function trapGetter(obj, name, label) {
  const d = Object.getOwnPropertyDescriptor(obj, name);
  if (!d || typeof d.get !== 'function') return false;
  const g = d.get;
  Object.defineProperty(obj, name, Object.assign({}, d, {get: function () { note(label); return g.call(this); }}));
  return true;
}
const trapsInstalled = [];
[
  [Document.prototype, 'querySelector', 'm'], [Document.prototype, 'querySelectorAll', 'm'],
  [Document.prototype, 'getElementsByTagName', 'm'], [Document.prototype, 'getElementById', 'm'],
  [Document.prototype, 'getElementsByClassName', 'm'], [Document.prototype, 'createTreeWalker', 'm'],
  [Document.prototype, 'evaluate', 'm'], [Document.prototype, 'elementFromPoint', 'm'],
  [Element.prototype, 'querySelector', 'm'], [Element.prototype, 'querySelectorAll', 'm'],
  [Element.prototype, 'getBoundingClientRect', 'm'], [Element.prototype, 'getClientRects', 'm'],
  [Element.prototype, 'getAttribute', 'm'], [Element.prototype, 'closest', 'm'], [Element.prototype, 'matches', 'm'],
  [window, 'getComputedStyle', 'm'], [window, 'scrollTo', 'm'],
  [Document.prototype, 'visibilityState', 'g'], [Document.prototype, 'hidden', 'g'], [Document.prototype, 'body', 'g'],
  [Document.prototype, 'title', 'g'], [Document.prototype, 'readyState', 'g'], [Document.prototype, 'documentElement', 'g'],
  [Document.prototype, 'contentType', 'g'], [Document.prototype, 'scrollingElement', 'g'],
  [HTMLElement.prototype, 'innerText', 'g'], [Node.prototype, 'textContent', 'g'], [Node.prototype, 'childNodes', 'g'],
  [Element.prototype, 'shadowRoot', 'g'], [Element.prototype, 'scrollHeight', 'g'], [Element.prototype, 'scrollWidth', 'g'],
].forEach(([obj, name, kind]) => {
  const owner = obj === window ? 'window' : (obj.constructor && obj.constructor.name) || '?';
  const label = owner + '.' + name;
  if ((kind === 'm' ? trapMethod : trapGetter)(obj, name, label)) trapsInstalled.push(label);
});

// ---------------------------------------------------------------- collectors
async function sha(text) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest)).slice(0, 12).map((b) => b.toString(16).padStart(2, '0')).join('');
}
async function step(name, fn) {
  try { R[name] = await fn(); } catch (e) { R.errors[name] = String((e && e.message) || e).slice(0, 300); }
}
const plain = (v) => JSON.parse(JSON.stringify(v === undefined ? null : v));

async function collectIdentity() {
  const n = navigator;
  const ua = n.userAgentData;
  const high = ua ? await ua.getHighEntropyValues(['architecture', 'bitness', 'brands', 'fullVersionList', 'mobile',
    'model', 'platform', 'platformVersion', 'uaFullVersion', 'wow64']) : null;
  const names = Object.getOwnPropertyNames(window).sort();
  const ch = window.chrome;
  return {
    webdriver: n.webdriver,
    webdriverOwnProperty: Object.getOwnPropertyNames(n).includes('webdriver'),
    userAgent: n.userAgent, platform: n.platform, languages: Array.from(n.languages || []),
    uaData: ua ? {brands: plain(ua.brands), mobile: ua.mobile, platform: ua.platform, high: plain(high)} : null,
    globals: {count: names.length, hash: await sha(names.join(',')),
              suspicious: names.filter((p) => /^(\$?cdc_|__playwright|__pw|_pw|__driver|__webdriver|__puppeteer)/i.test(p))},
    chromeKeys: ch ? Object.keys(ch).sort() : null,
  };
}
async function collectFirstUse() {
  const act = navigator.userActivation;
  const out = {hasBeenActive: act ? act.hasBeenActive : null, isActive: act ? act.isActive : null,
               hasFocus: document.hasFocus(), visibilityState: document.visibilityState, historyLength: history.length};
  const AC = window.AudioContext || window.webkitAudioContext;
  if (AC) {
    const ac = new AC();
    out.audioState = ac.state;  // "running" only when autoplay is allowed (sticky user activation)
    try { await ac.close(); } catch (e) { /* ignore */ }
  }
  return out;
}

async function post(phase, data) {
  await fetch('/result?cfg=' + encodeURIComponent(CFG) + '&phase=' + phase, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
  });
}
async function finishRequested() {
  try {
    const r = await fetch('/control?cfg=' + encodeURIComponent(CFG), {cache: 'no-store'});
    return !!(await r.json()).finish;
  } catch (e) { return false; }
}

async function main() {
  R.early = early;
  R.trapsInstalled = trapsInstalled;
  await step('firstUse', collectFirstUse);
  await step('identity', collectIdentity);
  await step('worker', workerProbe);
  await step('async', asyncProbes);
  R.cdpAfter = cdpProbes();
  R.externalCalls = plain(ext);
  await post('main', R);
  document.title = 'probe ready';
  const samples = [];
  const until = performance.now() + MAX_S * 1000;
  while (performance.now() < until) {
    await sleep(500);
    samples.push(Object.assign({t_ms: ms(T0)}, cdpProbes(), {
      hasFocus: document.hasFocus(),
      hasBeenActive: navigator.userActivation ? navigator.userActivation.hasBeenActive : null,
    }));
    if (await finishRequested()) break;
  }
  const worker = await workerProbe();
  await post('final', {samples, worker, externalCalls: plain(ext)});
  document.title = 'probe done';
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => main());
else main();
})();
</script>
<script>window.__PAGE_GLOBAL__ = '__PAGE_GLOBAL_VALUE__';</script>
</head><body>
<h1>ProfilePilot native probe</h1>
<p id="status">Results are sent to the local probe server only.</p>
<ul><li class="item">One</li><li class="item">Two</li></ul>
<div style="height:3000px">tall</div>
</body></html>
""".replace("__PAGE_GLOBAL__", PAGE_GLOBAL).replace("__PAGE_GLOBAL_VALUE__", PAGE_GLOBAL_VALUE)


_TZ_JS = """const tzNow = () => { const ro = Intl.DateTimeFormat().resolvedOptions();
  return {tz: ro.timeZone, offset: new Date().getTimezoneOffset()}; };
const tzEarly = tzNow();
"""

TZ_WORKER = _TZ_JS + "onmessage = () => postMessage({early: tzEarly, now: tzNow()});\n"
TZ_SHARED = _TZ_JS + ("onconnect = e => { const port = e.ports[0];\n"
                      "  port.onmessage = () => port.postMessage({early: tzEarly, now: tzNow()}); };\n")
TZ_SERVICE = _TZ_JS + """self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('message', e => e.ports[0].postMessage({early: tzEarly, now: tzNow()}));
"""

TZ_FRAME = _HEAD.replace("__TITLE__", "tz frame") + "<script>\n" + _TZ_JS + r"""
const worker = new Worker('/tz-worker.js');
const askWorker = () => Promise.race([new Promise(res => { worker.onmessage = e => res(e.data); worker.postMessage('m'); }),
                                      new Promise(res => setTimeout(() => res('timeout'), 5000))]);
addEventListener('message', async e => {
  if (e.data !== 'measure') return;
  parent.postMessage({kind: 'tz-frame', origin: location.origin, early: tzEarly, now: tzNow(),
                      worker: await askWorker()}, '*');
});
parent.postMessage({kind: 'tz-frame-ready', origin: location.origin, early: tzEarly}, '*');
</script></head><body>cross-site frame</body></html>
"""

TZ_PAGE = _HEAD.replace("__TITLE__", "tz probe") + "<script>\n" + _TZ_JS + r"""
const CFG = new URLSearchParams(location.search).get('cfg') || 'manual';
const post = (phase, data) => fetch('/result?cfg=' + encodeURIComponent(CFG) + '&phase=' + phase,
                                    {method: 'POST', body: JSON.stringify(data)});
const within = (promise, ms) => Promise.race([promise, new Promise(res => setTimeout(() => res('timeout'), ms))]);
const ask = target => within(new Promise(res => { target.onmessage = e => res(e.data); target.postMessage('m'); }), 5000);
const fromFrame = kind => new Promise(res => addEventListener('message', e => {
  if (e.data && e.data.kind === kind) res(e.data); }));
(async () => {
  const dedicated = new Worker('/tz-worker.js');
  const shared = new SharedWorker('/tz-shared.js');
  shared.port.start();
  let registration = null;
  try {
    registration = await navigator.serviceWorker.register('/tz-sw.js', {scope: '/'});
    await within(navigator.serviceWorker.ready, 8000);
  } catch (e) { registration = String(e); }
  const ready = fromFrame('tz-frame-ready');
  const frame = document.createElement('iframe');
  frame.src = 'http://localhost:' + location.port + '/tz-frame?cfg=' + encodeURIComponent(CFG);
  document.body.appendChild(frame);
  await post('ready', {early: tzEarly, frame: await within(ready, 15000)});
  for (;;) {
    try { if ((await (await fetch('/control?cfg=' + encodeURIComponent(CFG), {cache: 'no-store'})).json()).finish) break; }
    catch (e) {}
    await new Promise(res => setTimeout(res, 200));
  }
  const askService = () => {
    const active = registration && registration.active;
    if (!active) return Promise.resolve('no service worker: ' + registration);
    const channel = new MessageChannel();
    return within(new Promise(res => { channel.port1.onmessage = e => res(e.data); active.postMessage('m', [channel.port2]); }), 5000);
  };
  const late = fromFrame('tz-frame');
  frame.contentWindow.postMessage('measure', '*');
  const [d, s, sw, f] = await Promise.all([ask(dedicated), ask(shared.port), askService(), within(late, 8000)]);
  await post('late', {main: tzNow(), early: tzEarly, dedicated: d, shared: s, service: sw, frame: f});
  document.title = 'tz done';
})();
</script></head><body><h1>timezone probe</h1></body></html>
"""


class _Records:
    """Thread-safe store of everything recorded per configuration."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._finish: set[str] = set()

    def _cfg(self, cfg: str) -> dict[str, Any]:
        return self._data.setdefault(cfg, {"requests": [], "phases": {}})

    def add_request(self, cfg: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self._cfg(cfg)["requests"].append(entry)

    def add_phase(self, cfg: str, phase: str, payload: Any) -> None:
        with self._lock:
            self._cfg(cfg)["phases"][phase] = payload

    def has_phase(self, cfg: str, phase: str) -> bool:
        with self._lock:
            return phase in (self._data.get(cfg) or {}).get("phases", {})

    def get(self, cfg: str) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data.get(cfg) or {"requests": [], "phases": {}}))

    def finish(self, cfg: str) -> None:
        with self._lock:
            self._finish.add(cfg)

    def finish_requested(self, cfg: str) -> bool:
        with self._lock:
            return cfg in self._finish


def _handler(records: _Records) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            return

        def _cfg(self) -> tuple[str, dict[str, list[str]]]:
            query = parse_qs(urlsplit(self.path).query)
            return (query.get("cfg") or ["manual"])[0][:64], query

        def _record(self) -> None:
            records.add_request(self._cfg()[0], {
                "method": self.command, "path": urlsplit(self.path).path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            })

        def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            pages = {"/start": START_PAGE, "/bfb": BFB_PAGE, "/probe": PROBE_PAGE}
            scripts = {"/tz-worker.js": TZ_WORKER, "/tz-shared.js": TZ_SHARED, "/tz-sw.js": TZ_SERVICE}
            if path in pages:
                self._record()
                # no Cache-Control: no-store (it would make the page ineligible for the bfcache)
                self._send(200, pages[path].encode(), "text/html; charset=utf-8", {"Accept-CH": ACCEPT_CH})
            elif path in ("/tz", "/tz-frame"):
                self._record()
                body = (TZ_PAGE if path == "/tz" else TZ_FRAME).encode()
                self._send(200, body, "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            elif path in scripts:
                self._send(200, scripts[path].encode(), "text/javascript; charset=utf-8", {"Cache-Control": "no-store"})
            elif path == "/control":
                body = json.dumps({"finish": records.finish_requested(self._cfg()[0])}).encode()
                self._send(200, body, "application/json", {"Cache-Control": "no-store"})
            else:
                self._send(404 if path != "/favicon.ico" else 204, b"", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if urlsplit(self.path).path != "/result":
                self._send(404, b"", "text/plain")
                return
            cfg, query = self._cfg()
            self._record()
            try:
                payload = json.loads(raw.decode("utf-8") or "null")
            except ValueError:
                payload = {"unparsable": True}
            records.add_phase(cfg, (query.get("phase") or ["main"])[0][:32], payload)
            self._send(204, b"", "text/plain", {"Cache-Control": "no-store"})

    return Handler


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        return  # browsers drop idle keep-alive sockets when a page navigates away


class ProbeServer:
    """``with ProbeServer() as probe: url = probe.url("cfg")`` - then wait for its phases."""

    def __init__(self) -> None:
        self.records = _Records()
        self._httpd = _QuietServer(("127.0.0.1", 0), _handler(self.records))
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="native-probe", daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, cfg: str, *, max_s: int = 60) -> str:
        """The first URL of configuration ``cfg`` (the bfcache start page, then the probe)."""
        return f"{self.origin}/start?cfg={cfg}&max={max_s}"

    def tz_url(self, cfg: str) -> str:
        """The timezone page of configuration ``cfg`` (its iframe comes from ``localhost``, another site)."""
        return f"{self.origin}/tz?cfg={cfg}"

    def __enter__(self) -> "ProbeServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def finish(self, cfg: str) -> None:
        """Ask the probe page of ``cfg`` to post its ``final`` phase."""
        self.records.finish(cfg)

    def wait_phase(self, cfg: str, phase: str, timeout: float = 45.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while not self.records.has_phase(cfg, phase):
            if time.monotonic() > deadline:
                raise TimeoutError(f"probe {cfg!r}: no {phase!r} result within {timeout:g} s")
            time.sleep(0.1)
        return self.records.get(cfg)["phases"][phase]

    async def await_phase(self, cfg: str, phase: str, timeout: float = 45.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while not self.records.has_phase(cfg, phase):
            if time.monotonic() > deadline:
                raise TimeoutError(f"probe {cfg!r}: no {phase!r} result within {timeout:g} s")
            await asyncio.sleep(0.1)
        return self.records.get(cfg)["phases"][phase]

    def headers(self, cfg: str, path: str) -> dict[str, str]:
        """Headers of the first GET of ``path`` (``/start`` or ``/probe``) in ``cfg``."""
        for request in self.records.get(cfg)["requests"]:
            if request["path"] == path and request["method"] == "GET":
                return request["headers"]
        raise AssertionError(f"probe {cfg!r}: no GET {path}")

    def result(self, cfg: str) -> dict[str, Any]:
        """Everything recorded for ``cfg``: ``{"requests": [...], "phases": {...}}``."""
        return self.records.get(cfg)
