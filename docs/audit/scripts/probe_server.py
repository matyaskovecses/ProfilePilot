"""Local fingerprint probe for the ProfilePilot audit (docs/plans/fingerprint-audit.md).

A tiny HTTP server on 127.0.0.1 that serves a probe page. Everything is collected by in-page
JavaScript and POSTed back to this server, so a plain ``chrome.exe`` (no debugging port, no CDP)
can be measured exactly like a ProfilePilot profile.

Flow for one configuration ``cfg`` (each step is a normal page navigation in the same tab):

1. ``/start?cfg=X``  - back/forward-cache test page A. On first show it navigates to ``/bfb``.
2. ``/bfb?cfg=X``    - calls ``history.back()``. Page A then reports whether it was restored from
   the bfcache (``pageshow.persisted``) and the ``notRestoredReasons`` of the navigation entry
   (phase ``bfcache``), then navigates to the probe.
3. ``/probe?cfg=X&late=N`` - the probe. An inline script in <head> runs the CDP
   ``Runtime.enable`` detectors before anything else (classic Error.stack getter - dead on Chrome
   154 -, Error.prepareStackTrace via console.debug / exceptionThrown, console.debug timing),
   installs main-world call traps, then collects
   every vector (phase ``main``), and for ``N`` more seconds re-runs the detectors once a second
   (phase ``late``; this is the window in which an attached client may read the page).

The HTTP request headers of every request are recorded too (User-Agent, client hints,
Accept-Language, Sec-Fetch-*), and ``/start`` asks for the high-entropy client hints with
``Accept-CH`` so ``/probe`` carries them.

Endpoints: ``GET /start|/bfb|/probe`` (pages), ``POST /result?cfg=&phase=`` (collector),
``GET /results?cfg=`` (everything recorded for a configuration, JSON), ``GET /results`` (all).

Results are kept in memory **unredacted** (they contain the machine's real IP). The audit harness
(``run_probe_matrix.py``) redacts before writing anything. Stand-alone use::

    python probe_server.py --port 8765        # then open http://127.0.0.1:8765/start?cfg=manual
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

ACCEPT_CH = ", ".join([
    "Sec-CH-UA", "Sec-CH-UA-Mobile", "Sec-CH-UA-Platform", "Sec-CH-UA-Arch", "Sec-CH-UA-Bitness",
    "Sec-CH-UA-Full-Version", "Sec-CH-UA-Full-Version-List", "Sec-CH-UA-Model", "Sec-CH-UA-Platform-Version",
    "Sec-CH-UA-WoW64", "Sec-CH-UA-Form-Factors", "Sec-CH-Prefers-Color-Scheme", "Sec-CH-Prefers-Reduced-Motion",
    "Sec-CH-Device-Memory", "Sec-CH-DPR", "Sec-CH-Viewport-Width", "Sec-CH-Viewport-Height",
    "Device-Memory", "DPR", "Viewport-Width", "ECT", "RTT", "Downlink",
])

_COMMON_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
 :root { color-scheme: light dark; }
 body { font: 15px/1.5 system-ui, sans-serif; margin: 32px; background: Canvas; color: CanvasText; }
 code { font-size: 13px; }
</style>
"""

START_PAGE = _COMMON_HEAD.replace("__TITLE__", "probe start") + r"""
<script>
(() => {
  'use strict';
  const Q = new URLSearchParams(location.search);
  const CFG = Q.get('cfg') || 'manual';
  const LATE = Q.get('late') || '12';
  const KEY = 'pp-bf-left-' + CFG;
  const next = '/probe?cfg=' + encodeURIComponent(CFG) + '&late=' + encodeURIComponent(LATE);
  const post = (data) => fetch('/result?cfg=' + encodeURIComponent(CFG) + '&phase=bfcache', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
  }).catch(() => {});
  const navInfo = () => {
    const nav = performance.getEntriesByType('navigation')[0];
    let reasons = null;
    try { reasons = nav && nav.notRestoredReasons ? JSON.parse(JSON.stringify(nav.notRestoredReasons)) : null; } catch (e) { reasons = 'error: ' + e; }
    return {navType: nav ? nav.type : null, notRestoredReasons: reasons, historyLength: history.length};
  };
  window.addEventListener('pageshow', async (ev) => {
    let state = null;
    try { state = sessionStorage.getItem(KEY); } catch (e) {}
    if (ev.persisted) {
      try { sessionStorage.removeItem(KEY); } catch (e) {}
      await post(Object.assign({persisted: true, restoredFromBfcache: true}, navInfo()));
      location.href = next;
      return;
    }
    if (state === '1') {
      try { sessionStorage.removeItem(KEY); } catch (e) {}
      await post(Object.assign({persisted: false, restoredFromBfcache: false}, navInfo()));
      location.href = next;
      return;
    }
    try { sessionStorage.setItem(KEY, '1'); } catch (e) {}
    document.getElementById('s').textContent = 'leaving for the bfcache test page...';
    setTimeout(() => { location.href = '/bfb?cfg=' + encodeURIComponent(CFG) + '&late=' + encodeURIComponent(LATE); }, 400);
  });
})();
</script>
</head><body><h1>ProfilePilot local probe</h1><p>Back/forward cache test: <span id="s">starting</span></p></body></html>
"""

BFB_PAGE = _COMMON_HEAD.replace("__TITLE__", "probe bfcache B") + r"""
<script>
(() => {
  'use strict';
  const Q = new URLSearchParams(location.search);
  const CFG = Q.get('cfg') || 'manual';
  const LATE = Q.get('late') || '12';
  window.addEventListener('load', () => setTimeout(() => history.back(), 400));
  // Fallback: history.back() did not leave this page.
  setTimeout(() => {
    fetch('/result?cfg=' + encodeURIComponent(CFG) + '&phase=bfcache', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({persisted: null, error: 'history.back() did not leave /bfb within 5 s'}),
    }).catch(() => {}).finally(() => {
      location.href = '/probe?cfg=' + encodeURIComponent(CFG) + '&late=' + encodeURIComponent(LATE);
    });
  }, 5000);
})();
</script>
</head><body><h1>ProfilePilot local probe</h1><p>Page B of the back/forward cache test; going back...</p></body></html>
"""

# The probe. One IIFE in <head>: no globals are added to the page.
PROBE_PAGE = _COMMON_HEAD.replace("__TITLE__", "probe") + r"""
<script>
(() => {
'use strict';
const T0 = performance.now();
const Q = new URLSearchParams(location.search);
const CFG = Q.get('cfg') || 'manual';
const LATE_S = Math.max(0, Math.min(120, parseInt(Q.get('late') || '12', 10) || 0));
const SELF = location.origin + location.pathname;
const R = {cfg: CFG, errors: {}};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const status = (text) => { const el = document.getElementById('status'); if (el) el.textContent = text; };

// ------------------------------------------------------------------ CDP / Runtime.enable detectors
// When a CDP client has the Runtime domain enabled (Playwright always does), V8's inspector turns
// every console argument into a RemoteObject (Runtime.consoleAPICalled). Plain Chrome with DevTools
// closed never does. Ways to see that from the page:
//  * stackGetter - bot-detector.rebrowser.net "runtimeEnableLeak": an Error whose own `stack`
//    accessor fires when the inspector describes it. (Dead on Chrome 154: V8 no longer calls the
//    accessor. Kept for reference.)
//  * prepareStackTrace - Error.prepareStackTrace is called when the inspector formats the stack of
//    a fresh Error passed to console.debug (works on Chrome 154).
//  * consoleTiming - console.debug(<object with 3000 properties>) x100: ~1 ms without a client,
//    tens of milliseconds while the inspector builds previews.
//  * asyncProbe - uncaught exception / unhandled rejection while prepareStackTrace is set: the
//    inspector formats the stack for Runtime.exceptionThrown only when a client listens.
function stackGetterProbe(method) {
  let touched = false;
  const err = new Error('probe');
  Object.defineProperty(err, 'stack', {configurable: false, enumerable: false, get() { touched = true; return ''; }});
  try { console[method](err); } catch (e) { /* ignore */ }
  return touched;
}
function prepareStackTraceProbe() {
  let called = false;
  const old = Error.prepareStackTrace;
  Error.prepareStackTrace = (e, frames) => { called = true; return 'x'; };
  try { console.debug(new Error('probe')); } catch (e) { /* ignore */ }
  if (old === undefined) delete Error.prepareStackTrace; else Error.prepareStackTrace = old;
  return called;
}
const BIG = {};
for (let i = 0; i < 3000; i++) BIG['k' + i] = {v: i};
function consoleTimingProbe() {
  const t = performance.now();
  for (let i = 0; i < 100; i++) console.debug(BIG);
  return Math.round((performance.now() - t) * 100) / 100;
}
function cdpProbes() {
  return {stackGetter: stackGetterProbe('debug'), prepareStackTrace: prepareStackTraceProbe(), consoleTimingMs: consoleTimingProbe()};
}
function asyncProbes() {
  return new Promise((resolve) => {
    const res = {uncaughtPrepare: false, rejectionPrepare: false};
    // No preventDefault(): a cancelled error/rejection is not reported to the inspector at all.
    const onErr = () => {};
    const onRej = () => {};
    window.addEventListener('error', onErr);
    window.addEventListener('unhandledrejection', onRej);
    const old = Error.prepareStackTrace;
    const restore = () => { if (old === undefined) delete Error.prepareStackTrace; else Error.prepareStackTrace = old; };
    Error.prepareStackTrace = () => { res.uncaughtPrepare = true; return 'x'; };
    setTimeout(() => { throw new Error('probe-uncaught'); }, 0);
    setTimeout(() => {
      Error.prepareStackTrace = () => { res.rejectionPrepare = true; return 'x'; };
      Promise.reject(new Error('probe-rejection'));
      setTimeout(() => {
        restore();
        window.removeEventListener('error', onErr);
        window.removeEventListener('unhandledrejection', onRej);
        resolve(res);
      }, 300);
    }, 300);
  });
}
const early = Object.assign({t_ms: Math.round(performance.now() - T0)}, cdpProbes(), {
  visibilityState: document.visibilityState,
  hasFocus: document.hasFocus(),
});

// ------------------------------------------------------------------ main-world call traps
// Count calls of common DOM APIs whose *caller* is not this page's own script (e.g. code that a
// CDP client evaluates in the main world: Runtime.evaluate / callFunctionOn / page.evaluate).
// Isolated worlds (Playwright's utility world) have their own prototypes and never hit these.
const ext = {count: 0, by: {}, samples: []};
function rawStack() {
  // A prepareStackTrace probe may be installed at this moment: format with V8's default instead.
  const p = Error.prepareStackTrace;
  if (p !== undefined) delete Error.prepareStackTrace;
  const s = String((new Error()).stack || '');
  if (p !== undefined) Error.prepareStackTrace = p;
  return s;
}
function note(label) {
  const lines = rawStack().split('\n');
  // [0] "Error" [1] rawStack [2] note [3] trap wrapper [4] caller
  const caller = lines[4] || '';
  if (caller.includes(SELF)) return;
  ext.count++;
  ext.by[label] = (ext.by[label] || 0) + 1;
  if (ext.samples.length < 10) {
    ext.samples.push({label, t_ms: Math.round(performance.now() - T0), frames: lines.slice(4, 8).map((s) => s.trim().slice(0, 200))});
  }
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
  [Document.prototype, 'getElementsByTagName', 'm'], [Document.prototype, 'createTreeWalker', 'm'],
  [Document.prototype, 'evaluate', 'm'], [Document.prototype, 'elementFromPoint', 'm'],
  [Element.prototype, 'querySelector', 'm'], [Element.prototype, 'querySelectorAll', 'm'],
  [Element.prototype, 'getBoundingClientRect', 'm'], [Element.prototype, 'getClientRects', 'm'],
  [Element.prototype, 'getAttribute', 'm'], [Element.prototype, 'closest', 'm'], [Element.prototype, 'matches', 'm'],
  [window, 'getComputedStyle', 'm'],
  [Document.prototype, 'visibilityState', 'g'], [Document.prototype, 'hidden', 'g'], [Document.prototype, 'body', 'g'],
  [Document.prototype, 'title', 'g'], [Document.prototype, 'readyState', 'g'], [Document.prototype, 'documentElement', 'g'],
  [HTMLElement.prototype, 'innerText', 'g'], [Node.prototype, 'textContent', 'g'], [Node.prototype, 'childNodes', 'g'],
  [Element.prototype, 'shadowRoot', 'g'],
].forEach(([obj, name, kind]) => {
  const owner = obj === window ? 'window' : (obj.constructor && obj.constructor.name) || '?';
  const label = owner + '.' + name;
  if ((kind === 'm' ? trapMethod : trapGetter)(obj, name, label)) trapsInstalled.push(label);
});

// ------------------------------------------------------------------ helpers
async function sha(data) {
  const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data;
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest)).slice(0, 12).map((b) => b.toString(16).padStart(2, '0')).join('');
}
function withTimeout(promise, ms, label) {
  return Promise.race([promise, new Promise((_, rej) => setTimeout(() => rej(new Error((label || 'step') + ' timed out')), ms))]);
}
async function step(name, fn, ms) {
  try { R[name] = await withTimeout(Promise.resolve().then(fn), ms || 10000, name); }
  catch (e) { R.errors[name] = String((e && e.message) || e).slice(0, 300); }
}
const plain = (v) => JSON.parse(JSON.stringify(v === undefined ? null : v));

// ------------------------------------------------------------------ collectors
function collectNavigator() {
  const n = navigator;
  const conn = n.connection;
  return {
    webdriver: n.webdriver,
    webdriverOnPrototype: !!Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver'),
    navigatorOwnProps: Object.getOwnPropertyNames(n),
    userAgent: n.userAgent, appVersion: n.appVersion, platform: n.platform, vendor: n.vendor,
    product: n.product, productSub: n.productSub, oscpu: n.oscpu === undefined ? null : n.oscpu,
    language: n.language, languages: Array.from(n.languages || []),
    hardwareConcurrency: n.hardwareConcurrency, deviceMemory: n.deviceMemory === undefined ? null : n.deviceMemory,
    maxTouchPoints: n.maxTouchPoints, pdfViewerEnabled: n.pdfViewerEnabled, cookieEnabled: n.cookieEnabled,
    doNotTrack: n.doNotTrack, globalPrivacyControl: n.globalPrivacyControl === undefined ? null : n.globalPrivacyControl,
    onLine: n.onLine,
    plugins: Array.from(n.plugins || []).map((p) => ({name: p.name, filename: p.filename, description: p.description,
      mimeTypes: Array.from(p).map((m) => m.type)})),
    mimeTypes: Array.from(n.mimeTypes || []).map((m) => m.type),
    connection: conn ? {effectiveType: conn.effectiveType, rtt: conn.rtt, downlink: conn.downlink, saveData: conn.saveData,
      type: conn.type === undefined ? null : conn.type} : null,
    userActivation: n.userActivation ? {hasBeenActive: n.userActivation.hasBeenActive, isActive: n.userActivation.isActive} : null,
    webdriverInWindowProps: Object.getOwnPropertyNames(window).includes('webdriver'),
  };
}

async function collectUaData() {
  const ua = navigator.userAgentData;
  if (!ua) return null;
  const high = await ua.getHighEntropyValues(['architecture', 'bitness', 'brands', 'formFactors', 'fullVersionList',
    'mobile', 'model', 'platform', 'platformVersion', 'uaFullVersion', 'wow64']);
  return {brands: plain(ua.brands), mobile: ua.mobile, platform: ua.platform, high: plain(high), toJSON: plain(ua.toJSON ? ua.toJSON() : null)};
}

async function collectPermissions() {
  const names = ['notifications', 'geolocation', 'camera', 'microphone', 'clipboard-read', 'clipboard-write',
    'persistent-storage', 'midi', 'background-sync', 'accelerometer', 'gyroscope', 'magnetometer', 'local-fonts',
    'storage-access', 'window-management', 'payment-handler', 'idle-detection', 'screen-wake-lock'];
  const out = {};
  for (const name of names) {
    try { out[name] = (await navigator.permissions.query({name})).state; }
    catch (e) { out[name] = 'error:' + (e && e.name); }
  }
  out['Notification.permission'] = typeof Notification !== 'undefined' ? Notification.permission : null;
  return out;
}

function collectWindow() {
  const s = screen;
  const nav = performance.getEntriesByType('navigation')[0];
  return {
    innerWidth, innerHeight, outerWidth, outerHeight, screenX, screenY, screenLeft, screenTop,
    devicePixelRatio,
    chromeWidth: outerWidth - innerWidth, chromeHeight: outerHeight - innerHeight,
    visualViewport: window.visualViewport ? {width: visualViewport.width, height: visualViewport.height, scale: visualViewport.scale} : null,
    screen: {width: s.width, height: s.height, availWidth: s.availWidth, availHeight: s.availHeight,
      availLeft: s.availLeft, availTop: s.availTop, colorDepth: s.colorDepth, pixelDepth: s.pixelDepth,
      orientation: s.orientation ? s.orientation.type : null, orientationAngle: s.orientation ? s.orientation.angle : null,
      isExtended: s.isExtended === undefined ? null : s.isExtended},
    windowOnScreen: (screenX + outerWidth > (s.availLeft || 0)) && (screenX < (s.availLeft || 0) + s.width * 4)
      && (screenY + outerHeight > (s.availTop || 0)) && (screenY < (s.availTop || 0) + s.height * 4),
    document: {visibilityState: document.visibilityState, hidden: document.hidden, hasFocus: document.hasFocus(),
      wasDiscarded: document.wasDiscarded === undefined ? null : document.wasDiscarded, referrer: document.referrer,
      prerendering: document.prerendering === undefined ? null : document.prerendering},
    historyLength: history.length,
    windowName: window.name,
    opener: window.opener !== null,
    isSecureContext: window.isSecureContext,
    crossOriginIsolated: window.crossOriginIsolated,
    navigation: nav ? {type: nav.type, nextHopProtocol: nav.nextHopProtocol,
      notRestoredReasons: nav.notRestoredReasons ? plain(nav.notRestoredReasons) : null} : null,
    framesLength: window.frames.length,
    isTop: window.top === window,
  };
}

function collectIntl() {
  const dtf = Intl.DateTimeFormat().resolvedOptions();
  const jan = new Date(2026, 0, 15).getTimezoneOffset();
  const jul = new Date(2026, 6, 15).getTimezoneOffset();
  const m = String(new Date(2026, 0, 15, 12)).match(/\(([^)]+)\)/);
  return {
    timeZone: dtf.timeZone, locale: dtf.locale, calendar: dtf.calendar, numberingSystem: dtf.numberingSystem,
    hourCycle: Intl.DateTimeFormat(undefined, {hour: 'numeric'}).resolvedOptions().hourCycle || null,
    numberFormatLocale: Intl.NumberFormat().resolvedOptions().locale,
    collatorLocale: Intl.Collator().resolvedOptions().locale,
    tzOffsetNow: new Date().getTimezoneOffset(), tzOffsetJan: jan, tzOffsetJul: jul,
    tzName: m ? m[1] : null,
    sampleDate: new Date(Date.UTC(2026, 0, 15, 12)).toLocaleString(),
    sampleNumber: (1234567.891).toLocaleString(),
  };
}

function collectMedia() {
  const mq = (q) => matchMedia(q).matches;
  const pick = (feature, values) => values.find((v) => mq('(' + feature + ': ' + v + ')')) || null;
  return {
    'prefers-color-scheme': pick('prefers-color-scheme', ['dark', 'light']),
    'prefers-reduced-motion': pick('prefers-reduced-motion', ['reduce', 'no-preference']),
    'prefers-reduced-transparency': pick('prefers-reduced-transparency', ['reduce', 'no-preference']),
    'prefers-contrast': pick('prefers-contrast', ['more', 'less', 'custom', 'no-preference']),
    'forced-colors': pick('forced-colors', ['active', 'none']),
    'inverted-colors': pick('inverted-colors', ['inverted', 'none']),
    pointer: pick('pointer', ['fine', 'coarse', 'none']),
    'any-pointer': pick('any-pointer', ['fine', 'coarse', 'none']),
    hover: pick('hover', ['hover', 'none']),
    'any-hover': pick('any-hover', ['hover', 'none']),
    'color-gamut': pick('color-gamut', ['rec2020', 'p3', 'srgb']),
    'dynamic-range': pick('dynamic-range', ['high', 'standard']),
    'display-mode': pick('display-mode', ['fullscreen', 'standalone', 'minimal-ui', 'window-controls-overlay', 'browser']),
    monochrome: mq('(monochrome)'),
    scripting: pick('scripting', ['enabled', 'initial-only', 'none']),
    update: pick('update', ['fast', 'slow', 'none']),
  };
}

async function collectWebgl(kind) {
  const c = document.createElement('canvas');
  c.width = 64; c.height = 64;
  const g = c.getContext(kind, {preserveDrawingBuffer: true});
  if (!g) return null;
  const P = (p) => { try { const v = g.getParameter(p); return (v && typeof v === 'object' && 'length' in v) ? Array.from(v) : v; } catch (e) { return null; } };
  const dbg = g.getExtension('WEBGL_debug_renderer_info');
  const exts = g.getSupportedExtensions() || [];
  const vs = g.createShader(g.VERTEX_SHADER);
  g.shaderSource(vs, 'attribute vec2 p;varying vec2 v;void main(){v=p;gl_Position=vec4(p,0.0,1.0);}');
  g.compileShader(vs);
  const fs = g.createShader(g.FRAGMENT_SHADER);
  g.shaderSource(fs, 'precision mediump float;varying vec2 v;void main(){gl_FragColor=vec4(sin(v.x*7.3)*0.5+0.5,cos(v.y*5.1)*0.5+0.5,fract(v.x*v.y*13.7),1.0);}');
  g.compileShader(fs);
  const pr = g.createProgram();
  g.attachShader(pr, vs); g.attachShader(pr, fs); g.linkProgram(pr); g.useProgram(pr);
  const b = g.createBuffer();
  g.bindBuffer(g.ARRAY_BUFFER, b);
  g.bufferData(g.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, 0, 1, -0.7, 0.9]), g.STATIC_DRAW);
  const loc = g.getAttribLocation(pr, 'p');
  g.enableVertexAttribArray(loc);
  g.vertexAttribPointer(loc, 2, g.FLOAT, false, 0, 0);
  g.clearColor(0.1, 0.2, 0.3, 1); g.clear(g.COLOR_BUFFER_BIT); g.drawArrays(g.TRIANGLE_STRIP, 0, 4);
  const px = new Uint8Array(64 * 64 * 4);
  g.readPixels(0, 0, 64, 64, g.RGBA, g.UNSIGNED_BYTE, px);
  const params = [g.MAX_TEXTURE_SIZE, g.MAX_RENDERBUFFER_SIZE, g.MAX_VIEWPORT_DIMS, g.MAX_VERTEX_ATTRIBS,
    g.MAX_VERTEX_UNIFORM_VECTORS, g.MAX_FRAGMENT_UNIFORM_VECTORS, g.MAX_VARYING_VECTORS, g.MAX_COMBINED_TEXTURE_IMAGE_UNITS,
    g.ALIASED_LINE_WIDTH_RANGE, g.ALIASED_POINT_SIZE_RANGE, g.MAX_CUBE_MAP_TEXTURE_SIZE].map(P);
  return {
    vendor: P(g.VENDOR), renderer: P(g.RENDERER),
    unmaskedVendor: dbg ? P(dbg.UNMASKED_VENDOR_WEBGL) : null, unmaskedRenderer: dbg ? P(dbg.UNMASKED_RENDERER_WEBGL) : null,
    version: P(g.VERSION), shadingLanguage: P(g.SHADING_LANGUAGE_VERSION),
    maxTextureSize: P(g.MAX_TEXTURE_SIZE), maxViewportDims: P(g.MAX_VIEWPORT_DIMS),
    paramsHash: await sha(JSON.stringify(params)),
    extensionCount: exts.length, extensionsHash: await sha(exts.join(',')),
    contextAttributes: plain(g.getContextAttributes()),
    pixelsHash: await sha(px),
  };
}

async function collectCanvas() {
  const c = document.createElement('canvas');
  c.width = 300; c.height = 70;
  const x = c.getContext('2d');
  x.textBaseline = 'top';
  x.font = '16px Arial';
  x.fillStyle = '#f60'; x.fillRect(110, 1, 62, 20);
  x.fillStyle = '#069'; x.fillText('ProfilePilot probe \u{1F603} Ω éß', 2, 15);
  x.fillStyle = 'rgba(102, 204, 0, 0.7)'; x.font = '18px "Times New Roman"'; x.fillText('Cwm fjordbank glyphs vext quiz', 4, 38);
  x.globalCompositeOperation = 'multiply';
  x.fillStyle = 'rgb(255,0,255)'; x.beginPath(); x.arc(250, 35, 25, 0, Math.PI * 2, true); x.closePath(); x.fill();
  x.fillStyle = 'rgb(0,255,255)'; x.beginPath(); x.arc(270, 35, 25, 0, Math.PI * 2, true); x.closePath(); x.fill();
  const tm = x.measureText('Cwm fjordbank glyphs vext quiz');
  return {
    dataUrlHash: await sha(c.toDataURL()),
    textWidth: tm.width,
    winding: (() => { x.rect(0, 0, 10, 10); x.rect(2, 2, 6, 6); return x.isPointInPath(5, 5, 'evenodd') === false; })(),
    offscreenCanvas: typeof OffscreenCanvas === 'function',
  };
}

async function collectAudio() {
  const out = {};
  const AC = window.AudioContext || window.webkitAudioContext;
  if (AC) {
    const ac = new AC();
    out.sampleRate = ac.sampleRate; out.baseLatency = ac.baseLatency; out.outputLatency = ac.outputLatency;
    out.state = ac.state; out.maxChannelCount = ac.destination.maxChannelCount;
    out.sinkId = typeof ac.sinkId === 'string' ? ac.sinkId : typeof ac.sinkId;
    try { await ac.close(); } catch (e) { /* ignore */ }
  }
  const oc = new OfflineAudioContext(1, 5000, 44100);
  const osc = oc.createOscillator();
  osc.type = 'triangle'; osc.frequency.value = 10000;
  const comp = oc.createDynamicsCompressor();
  comp.threshold.value = -50; comp.knee.value = 40; comp.ratio.value = 12; comp.attack.value = 0; comp.release.value = 0.25;
  osc.connect(comp); comp.connect(oc.destination); osc.start(0);
  const buf = await oc.startRendering();
  const data = buf.getChannelData(0);
  let sum = 0;
  for (let i = 4500; i < 5000; i++) sum += Math.abs(data[i]);
  out.offlineSum = sum;
  return out;
}

const FONTS = ['Arial', 'Arial Black', 'Bahnschrift', 'Calibri', 'Cambria', 'Candara', 'Cascadia Code', 'Comic Sans MS',
  'Consolas', 'Constantia', 'Corbel', 'Courier New', 'Ebrima', 'Franklin Gothic Medium', 'Gabriola', 'Georgia', 'Impact',
  'Lucida Console', 'Lucida Sans Unicode', 'Malgun Gothic', 'Microsoft Sans Serif', 'MS Gothic', 'Palatino Linotype',
  'Segoe Print', 'Segoe Script', 'Segoe UI', 'Segoe UI Emoji', 'SimSun', 'Sylfaen', 'Tahoma', 'Times New Roman',
  'Trebuchet MS', 'Verdana', 'Webdings', 'Wingdings', 'Yu Gothic', 'Helvetica', 'Roboto', 'Menlo', 'Ubuntu',
  'ZzNoSuchFont Probe'];

function collectFonts() {
  const span = document.createElement('span');
  span.style.cssText = 'position:absolute;left:-9999px;top:0;font-size:72px;white-space:nowrap;';
  span.textContent = 'mmmmmmmmmmlli1WwQq@#é中';
  document.body.appendChild(span);
  const bases = ['monospace', 'serif', 'sans-serif'];
  const base = {};
  for (const b of bases) { span.style.fontFamily = b; base[b] = [span.offsetWidth, span.offsetHeight]; }
  const measured = [];
  const check = [];
  for (const f of FONTS) {
    const present = bases.some((b) => {
      span.style.fontFamily = '"' + f + '",' + b;
      return span.offsetWidth !== base[b][0] || span.offsetHeight !== base[b][1];
    });
    if (present) measured.push(f);
    let ok = null;
    try { ok = document.fonts.check('16px "' + f + '"'); } catch (e) { ok = null; }
    if (ok) check.push(f);
  }
  span.remove();
  return {tested: FONTS.length, measured, measuredCount: measured.length, fontsCheck: check, fontsCheckCount: check.length};
}

async function collectSpeech() {
  if (!('speechSynthesis' in window)) return null;
  const voices = await new Promise((res) => {
    const v = speechSynthesis.getVoices();
    if (v.length) return res(v);
    const t = setTimeout(() => res(speechSynthesis.getVoices()), 3000);
    speechSynthesis.addEventListener('voiceschanged', () => { clearTimeout(t); res(speechSynthesis.getVoices()); }, {once: true});
  });
  return {count: voices.length, localCount: voices.filter((v) => v.localService).length,
    default: (voices.find((v) => v.default) || {}).name || null, names: voices.map((v) => v.name)};
}

async function collectMediaDevices() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return null;
  const devs = await navigator.mediaDevices.enumerateDevices();
  const byKind = {};
  for (const d of devs) byKind[d.kind] = (byKind[d.kind] || 0) + 1;
  return {count: devs.length, byKind, withLabel: devs.filter((d) => d.label).length, withDeviceId: devs.filter((d) => d.deviceId).length};
}

function collectChromeObject() {
  const ch = window.chrome;
  if (!ch) return null;
  return {
    keys: Object.keys(ch).sort(), ownNames: Object.getOwnPropertyNames(ch).sort(),
    runtime: typeof ch.runtime, runtimeKeys: ch.runtime ? Object.keys(ch.runtime).sort() : null,
    app: ch.app ? {isInstalled: ch.app.isInstalled, keys: Object.keys(ch.app).sort()} : null,
    loadTimes: typeof ch.loadTimes, csi: typeof ch.csi,
    webstore: typeof ch.webstore,
  };
}

async function collectGlobals() {
  const pat = /^(\$?cdc_|\$wdc_|__playwright|__pw|_pw|__driver|__webdriver|__selenium|__fxdriver|__nightmare|_phantom|callPhantom|domAutomation|__lastWatirAlert|__puppeteer|puppeteer|_Selenium_IDE|webdriver|_WEBDRIVER|__\$webdriver|__REBROWSER|__stealth)/i;
  const win = Object.getOwnPropertyNames(window).sort();
  const docOwn = Object.getOwnPropertyNames(document);
  return {
    windowPropCount: win.length,
    windowPropsHash: await sha(win.join(',')),
    windowProps: win,
    suspiciousWindow: win.filter((p) => pat.test(p)),
    documentOwnProps: docOwn,
    suspiciousDocument: docOwn.filter((p) => pat.test(p) || p.startsWith('$')),
    pwInitScripts: typeof window.__pwInitScripts,
    playwrightBinding: typeof window.__playwright__binding__,
    errorApi: {prepareStackTrace: typeof Error.prepareStackTrace, captureStackTrace: typeof Error.captureStackTrace,
      stackTraceLimit: Error.stackTraceLimit},
    functionToStringNative: Function.prototype.toString.call(navigator.permissions.query).includes('[native code]'),
  };
}

async function collectTiming() {
  const raf = await new Promise((res) => {
    let n = 0; let first = null; const t0 = performance.now(); let done = false;
    const f = () => {
      if (done) return;
      if (first === null) first = performance.now() - t0;
      n++;
      if (performance.now() - t0 < 1000) requestAnimationFrame(f);
      else { done = true; res({framesIn1s: n, firstFrameMs: Math.round(first)}); }
    };
    requestAnimationFrame(f);
    setTimeout(() => { if (!done) { done = true; res({framesIn1s: n, firstFrameMs: first === null ? null : Math.round(first), timedOut: true}); } }, 2500);
  });
  const nested = await new Promise((res) => {
    const t0 = performance.now(); let k = 0;
    (function tick() { if (++k >= 20) return res(Math.round((performance.now() - t0) * 10) / 10); setTimeout(tick, 0); })();
  });
  const t100 = await new Promise((res) => { const t0 = performance.now(); setTimeout(() => res(Math.round(performance.now() - t0)), 100); });
  const mem = performance.memory;
  return {raf, nested20SetTimeout0Ms: nested, setTimeout100ActualMs: t100,
    performanceMemory: mem ? {present: true, jsHeapSizeLimit: mem.jsHeapSizeLimit} : {present: false}};
}

async function collectWebrtc() {
  if (!window.RTCPeerConnection) return {supported: false};
  const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
  const cands = [];
  const t0 = performance.now();
  const end = new Promise((res) => {
    pc.addEventListener('icecandidate', (e) => {
      if (!e.candidate) return res('complete');
      const c = e.candidate;
      cands.push({type: c.type, protocol: c.protocol, address: c.address, relatedAddress: c.relatedAddress,
        tcpType: c.tcpType, t_ms: Math.round(performance.now() - t0), candidate: c.candidate});
    });
    setTimeout(() => res('timeout'), 7000);
  });
  pc.createDataChannel('probe');
  await pc.setLocalDescription(await pc.createOffer());
  const how = await end;
  const gatheringState = pc.iceGatheringState;
  pc.close();
  const byType = {};
  for (const c of cands) byType[c.type] = (byType[c.type] || 0) + 1;
  return {supported: true, end: how, gatheringState, count: cands.length, byType, candidates: cands};
}

async function fetchIp(url) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 9000);
  try {
    const r = await fetch(url, {signal: ctl.signal, cache: 'no-store', credentials: 'omit'});
    const j = await r.json();
    return j.ip || null;
  } catch (e) {
    return 'error:' + (e && e.name);
  } finally { clearTimeout(t); }
}

async function collectNetwork() {
  const [ipv4, ip64] = await Promise.all([fetchIp('https://api.ipify.org?format=json'), fetchIp('https://api64.ipify.org?format=json')]);
  return {ipify_v4: ipv4, ipify_64: ip64};
}

async function collectStorage() {
  const out = {};
  if (navigator.storage && navigator.storage.estimate) {
    const e = await navigator.storage.estimate();
    out.quotaGB = Math.round((e.quota || 0) / 1e8) / 10;
  }
  out.persisted = navigator.storage && navigator.storage.persisted ? await navigator.storage.persisted() : null;
  out.indexedDB = typeof indexedDB !== 'undefined';
  out.serviceWorker = 'serviceWorker' in navigator;
  return out;
}

async function collectWebgpu() {
  if (!navigator.gpu) return {present: false};
  const adapter = await navigator.gpu.requestAdapter();
  if (!adapter) return {present: true, adapter: null};
  const info = adapter.info || {};
  return {present: true, adapter: {vendor: info.vendor, architecture: info.architecture, device: info.device, description: info.description},
    featureCount: adapter.features ? adapter.features.size : null};
}

async function collectWorker() {
  const src = "self.onmessage=()=>{postMessage({userAgent:navigator.userAgent,webdriver:navigator.webdriver," +
    "hardwareConcurrency:navigator.hardwareConcurrency,deviceMemory:navigator.deviceMemory,languages:navigator.languages," +
    "platform:navigator.platform,timeZone:Intl.DateTimeFormat().resolvedOptions().timeZone," +
    "uaDataPlatform:navigator.userAgentData?navigator.userAgentData.platform:null})}";
  const url = URL.createObjectURL(new Blob([src], {type: 'text/javascript'}));
  const w = new Worker(url);
  const data = await new Promise((res, rej) => { w.onmessage = (e) => res(e.data); w.onerror = (e) => rej(new Error('worker error')); w.postMessage(1); });
  w.terminate();
  URL.revokeObjectURL(url);
  return plain(data);
}

async function post(phase, data) {
  await fetch('/result?cfg=' + encodeURIComponent(CFG) + '&phase=' + phase, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
  });
}

async function main() {
  status('collecting');
  R.early = early;
  R.trapsInstalled = trapsInstalled;
  await step('navigator', collectNavigator);
  await step('uaData', collectUaData);
  await step('permissions', collectPermissions);
  await step('window', collectWindow);
  await step('intl', collectIntl);
  await step('media', collectMedia);
  await step('webgl', () => collectWebgl('webgl'));
  await step('webgl2', () => collectWebgl('webgl2'));
  await step('canvas', collectCanvas);
  await step('fonts', collectFonts);
  await step('chromeObject', collectChromeObject);
  await step('globals', collectGlobals);
  await step('storage', collectStorage);
  await step('worker', collectWorker, 5000);
  // Slow / network steps run concurrently.
  await Promise.all([
    step('audio', collectAudio, 8000),
    step('speech', collectSpeech, 5000),
    step('mediaDevices', collectMediaDevices, 5000),
    step('webgpu', collectWebgpu, 5000),
    step('webrtc', collectWebrtc, 9000),
    step('network', collectNetwork, 12000),
    step('timing', collectTiming, 6000),
  ]);
  R.cdpAfterCollect = cdpProbes();
  R.cdpAsync = await asyncProbes();
  R.externalCallsDuringMain = JSON.parse(JSON.stringify(ext));
  R.collectMs = Math.round(performance.now() - T0);
  await post('main', R);
  status('main results sent; watching for ' + LATE_S + ' s');

  const late = {seconds: LATE_S, samples: []};
  for (let i = 0; i < LATE_S; i++) {
    await sleep(1000);
    late.samples.push(Object.assign({t: i + 1}, cdpProbes(), {visibilityState: document.visibilityState, hasFocus: document.hasFocus()}));
  }
  late.stackGetterAny = late.samples.some((s) => s.stackGetter);
  late.prepareStackTraceCount = late.samples.filter((s) => s.prepareStackTrace).length;
  late.cdpDetectedAny = late.samples.some((s) => s.prepareStackTrace || s.stackGetter);
  late.consoleTimingMsMax = late.samples.reduce((m, s) => Math.max(m, s.consoleTimingMs), 0);
  late.consoleTimingMsMedian = (() => { const v = late.samples.map((s) => s.consoleTimingMs).sort((a, b) => a - b); return v.length ? v[Math.floor(v.length / 2)] : null; })();
  late.externalCalls = JSON.parse(JSON.stringify(ext));
  late.window = {innerWidth, innerHeight, outerWidth, outerHeight, screenX, screenY};
  await post('late', late);
  status('done');
  document.title = 'probe done';
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => main().catch((e) => status('failed: ' + e)));
else main().catch((e) => status('failed: ' + e));
})();
</script>
</head><body>
<h1>ProfilePilot local probe</h1>
<p>Configuration <code id="cfg"></code>: <span id="status">loading</span></p>
<p>Results are sent to the local probe server only. Nothing is shown here.</p>
<script>document.getElementById('cfg').textContent = new URLSearchParams(location.search).get('cfg') || 'manual';</script>
</body></html>
"""


class ProbeStore:
    """Thread-safe in-memory store of everything recorded per configuration."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}

    def _cfg(self, cfg: str) -> dict[str, Any]:
        return self._data.setdefault(cfg, {"requests": [], "phases": {}})

    def add_request(self, cfg: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self._cfg(cfg)["requests"].append(entry)

    def add_phase(self, cfg: str, phase: str, payload: Any) -> None:
        with self._lock:
            self._cfg(cfg)["phases"][phase] = {"received_at": time.time(), "data": payload}

    def get(self, cfg: str | None = None) -> Any:
        with self._lock:
            if cfg is None:
                return json.loads(json.dumps(self._data))
            return json.loads(json.dumps(self._data.get(cfg) or {"requests": [], "phases": {}}))

    def reset(self, cfg: str) -> None:
        with self._lock:
            self._data.pop(cfg, None)

    def has_phase(self, cfg: str, phase: str) -> bool:
        with self._lock:
            return phase in (self._data.get(cfg) or {}).get("phases", {})


def make_handler(store: ProbeStore):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "probe/1"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            return

        def _cfg(self) -> tuple[str, dict[str, list[str]]]:
            q = parse_qs(urlsplit(self.path).query)
            return (q.get("cfg") or ["manual"])[0][:64], q

        def _record(self) -> None:
            cfg, _ = self._cfg()
            store.add_request(cfg, {
                "t": time.time(), "method": self.command, "path": urlsplit(self.path).path,
                "headers": [[k, v] for k, v in self.headers.items()],
            })

        def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path in ("/start", "/bfb", "/probe"):
                self._record()
                page = {"/start": START_PAGE, "/bfb": BFB_PAGE, "/probe": PROBE_PAGE}[path]
                # No Cache-Control: no-store (it would make the page ineligible for the bfcache).
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", {"Accept-CH": ACCEPT_CH})
                return
            if path == "/results":
                cfg = parse_qs(urlsplit(self.path).query).get("cfg", [None])[0]
                self._send(200, json.dumps(store.get(cfg)).encode(), "application/json", {"Cache-Control": "no-store"})
                return
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if path != "/result":
                self._send(404, b"not found", "text/plain")
                return
            cfg, q = self._cfg()
            phase = (q.get("phase") or ["main"])[0][:32]
            self._record()
            try:
                payload = json.loads(raw.decode("utf-8") or "null")
            except ValueError:
                payload = {"unparsable": raw[:2000].decode("utf-8", "replace")}
            store.add_phase(cfg, phase, payload)
            self._send(204, b"", "text/plain", {"Cache-Control": "no-store"})

    return Handler


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Browsers drop idle keep-alive sockets (WinError 10054) when a page navigates away.
        return


class ProbeServer:
    """``with ProbeServer() as srv: srv.url("start", "B0")``"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.store = ProbeStore()
        self.httpd = _QuietServer((host, port), make_handler(self.store))
        self.httpd.daemon_threads = True
        self.host, self.port = self.httpd.server_address[:2]
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, page: str, cfg: str, late: int = 12) -> str:
        return f"{self.origin}/{page}?cfg={cfg}&late={late}"

    def start(self) -> "ProbeServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="probe-server", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self) -> "ProbeServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def wait_phase(self, cfg: str, phase: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.store.has_phase(cfg, phase):
                return True
            time.sleep(0.25)
        return self.store.has_phase(cfg, phase)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ProbeServer(args.host, args.port).start()
    print(f"probe server on {srv.origin}  -> open {srv.url('start', 'manual')}  (results: {srv.origin}/results)")
    print("Results stay in memory and are UNREDACTED (they contain your real IP). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
