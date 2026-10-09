"""Public bot/automation detectors and fingerprint sites: baseline B1 vs ProfilePilot P (no proxy).

Configurations (every site gets a FRESH user-data-dir / profile in each configuration):

  B1  chrome.exe with EXACTLY ProfilePilot's switches (``prepare_user_data_dir`` + ``build_chrome_args``
      like the host does, window=offscreen, no proxy) and the site as the start URL. No CDP client is
      attached while the site runs. After the wait a RAW CDP connection (websockets) attaches to the
      tab with ``Target.attachToTarget(flatten)`` and reads the page in an ISOLATED world
      (``Page.createIsolatedWorld`` + ``Runtime.evaluate(contextId)``) WITHOUT ``Runtime.enable``;
      the screenshot is ``Page.captureScreenshot``.
  P   a ProfilePilot profile (window=offscreen, no proxy) driven like the MCP tools drive it:
      ``RuntimeManager`` + ``BrowserManager`` (Playwright ``connect_over_cdp(no_defaults=True)``,
      ``contexts[0]``), the site opened with the MCP tool function ``browser_navigate`` itself,
      read with ``content.read_page`` (what ``browser_read`` runs) and ``browser_screenshot``.
      For a like-for-like comparison the same isolated-world extraction as B1 also runs (once
      before the main-world read, once after it) over a separate CDP session.
  PR  mitigation experiment: B1's launch on about:blank, ONE raw CDP Page.navigate to the site (no
      Runtime.enable, no auto-attach), detached at once; read like B1 (isolates Runtime.enable).
  PD  mitigation experiment (not part of the B1/P comparison): as P, but the Playwright connection
      is dropped right after ``browser_navigate`` returns, so no CDP client is attached while the
      site runs; the page is read afterwards like B1 (raw CDP, isolated world).

Site-specific actions (rebrowser's bot-detector needs the automation side to call page functions)
run in P only, through ``browser_evaluate`` / ``browser_click`` - the API an AI agent has.

Privacy: the real public IPv4/IPv6 (and the machine's other global IPv6 addresses, the reverse-DNS
host name, GeoIP ISP / city / region / postal code / ASN / coordinates from three lookup services)
are detected at runtime and replaced by labels (REAL_IP, REAL_IP6, REAL_HOSTNAME, REAL_ISP,
REAL_CITY, REAL_REGION, REAL_POSTAL, REAL_ASN, REAL_COORD, REAL_NET) in everything written, other
public IP literals by OTHER_PUBLIC_IP_n. ``--extra-terms <file outside the repo>`` adds more
(value, label) pairs (e.g. a city name another GeoIP database reports) and remembers the GeoIP
values found. Before each screenshot the IP / host name / ISP / city / postal code are replaced in
the page's DOM text. Screenshots stay local (docs/audit/.gitignore). No proxy is used by this script.

Usage (repo root, project venv)::

    python docs/audit/scripts/run_detectors.py --scratch <scratch dir> [--configs B1,P,PD] [--sites a,b]
                                               [--out <dir>] [--wait-scale 1.0] [--extra-terms <json>]
    python docs/audit/scripts/run_detectors.py --scratch <scratch dir> --redact-only --extra-terms <json>

Writes ``<out>/raw/detector-<site>-<cfg>.json`` and ``<out>/img/detector-<site>-<cfg>.jpg``
(default ``<out>`` = docs/audit).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent
REPO = AUDIT.parent.parent
sys.path.insert(0, str(REPO / "src"))

CREATE_NO_WINDOW = 0x08000000
MAX_IMG = 300 * 1024


# --------------------------------------------------------------------------- sites


@dataclass
class Site:
    key: str
    url: str
    wait: float
    note: str = ""
    actions: str = ""  # name of a P-only action routine (see SITE_ACTIONS)


SITES = [
    Site("sannysoft", "https://bot.sannysoft.com/", 12),
    Site("rebrowser", "https://bot-detector.rebrowser.net/", 10, actions="rebrowser"),
    Site("browserscan-bot", "https://www.browserscan.net/bot-detection", 18),
    Site("browserscan", "https://www.browserscan.net/", 22),
    Site("deviceandbrowserinfo", "https://deviceandbrowserinfo.com/are_you_a_bot", 16),
    Site("incolumitas", "https://bot.incolumitas.com/", 32),
    Site("fingerprint", "https://fingerprint.com/demo/", 16),
    Site("creepjs", "https://abrahamjuliot.github.io/creepjs/", 28),
    Site("pixelscan", "https://pixelscan.net/fingerprint-check", 28, note="the home page only links to the scan"),
    Site("pixelscan-bot", "https://pixelscan.net/bot-check", 22),
    Site("iphey", "https://iphey.com/", 22),
    Site("whoer", "https://whoer.net/", 18),
    Site("browserleaks-javascript", "https://browserleaks.com/javascript", 12),
    Site("browserleaks-client-hints", "https://browserleaks.com/client-hints", 10),
    Site("browserleaks-webgl", "https://browserleaks.com/webgl", 12),
    Site("browserleaks-canvas", "https://browserleaks.com/canvas", 12),
    Site("browserleaks-webgpu", "https://browserleaks.com/webgpu", 12, note="added: browserscan WebGPU Report hash differed"),
    Site("antcpt", "https://www.antcpt.com/score_detector/", 18),
]


# --------------------------------------------------------------------------- redaction

_V4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_CIDR4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}(?!\d)")
_V6 = re.compile(r"(?<![0-9A-Fa-f:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")


def _v6_plausible(literal: str) -> bool:
    """At least three non-empty hextets ("PDF::" or "a:b" are not addresses worth redacting)."""
    return sum(1 for h in literal.split(":") if h) >= 3


def _v4_version_like(m: re.Match) -> bool:
    """``Chrome/154.0.0.0``-style version numbers are not IP addresses."""
    before = m.string[max(0, m.start() - 1):m.start()]
    return before == "/" or before.isalpha() or m.group(0).endswith(".0.0")


class Redactor:
    """Replaces the user's network identity with labels; ``check()`` asserts nothing slipped through."""

    DOM_LABELS = ("REAL_IP", "REAL_IP6", "REAL_HOSTNAME", "REAL_ISP", "REAL_CITY", "REAL_POSTAL", "REAL_ASN")

    def __init__(self) -> None:
        self.patterns: list[tuple[re.Pattern, str, str]] = []  # (regex, label, raw value)
        self.repl: dict[str, str] = {}  # raw -> replacement template (default: the label)
        self.nets6: list[tuple[ipaddress.IPv6Network, str]] = []
        self.known_ips: dict[str, str] = {}
        self.other: dict[str, str] = {}

    def _add(self, regex: str, label: str, raw: str, repl: str | None = None) -> None:
        if all(r != raw or l != label for _, l, r in self.patterns):
            self.patterns.append((re.compile(regex, re.I), label, raw))
            if repl:
                self.repl[raw] = repl

    def add_ip(self, ip: str | None, label: str) -> None:
        if not ip:
            return
        try:
            addr = ipaddress.ip_address(ip.split("%", 1)[0])
        except ValueError:
            return
        self.known_ips.setdefault(str(addr), label)
        if addr.version == 4:
            self._add(r"(?<![\d.])" + re.escape(str(addr)) + r"(?!\d|\.\d)", label, str(addr))
            a, b, c, d = str(addr).split(".")
            for variant in (f"{a}-{b}-{c}-{d}", f"{a}_{b}_{c}_{d}", f"{d}.{c}.{b}.{a}", f"{d}-{c}-{b}-{a}"):
                self._add(r"(?<!\d)" + re.escape(variant) + r"(?!\d)", label, variant)
        else:
            for form in (str(addr), addr.exploded):
                self._add(r"(?<![0-9a-f])" + re.escape(form) + r"(?![0-9a-f])", label, form)
            if label.startswith("REAL"):
                net = ipaddress.IPv6Network(f"{addr}/64", strict=False)
                self.nets6.append((net, label))
                hextets = net.network_address.exploded.split(":")[:4]
                prefix = ":".join("0*" + h.lstrip("0") for h in hextets)
                self._add(r"(?<![0-9a-f])" + prefix + r":[0-9a-f:]*[0-9a-f]", label, prefix)

    def add_text(self, value: str | None, label: str, *, min_len: int = 4) -> None:
        if value and len(value.strip()) >= min_len:
            v = value.strip()
            if label != "REAL_IP" and [v, label] not in GEO_TERMS:
                GEO_TERMS.append([v, label])
            if label in ("REAL_CITY", "REAL_REGION", "REAL_ISP", "REAL_HOSTNAME") and len(v) >= 5:
                # Page text glues labels and values ("CityOakland", "californiaPrivacy"): no word
                # boundaries, only "not part of a tz id" (Europe/...).
                self._add(r"(?<!/)" + re.escape(v), label, v)
            else:
                self._add(r"(?<![\w/])" + re.escape(v) + r"(?!\w)", label, v)

    def _label_ip(self, literal: str) -> str | None:
        try:
            addr = ipaddress.ip_address(literal)
        except ValueError:
            return None
        if str(addr) in self.known_ips:
            return self.known_ips[str(addr)]
        if addr.version == 6:
            for net, label in self.nets6:
                if addr in net:
                    return label
        if addr.is_loopback or addr.is_unspecified or addr.is_multicast:
            return None
        if addr.is_private or addr.is_link_local:
            return "LAN_IP6" if addr.version == 6 else "LAN_IP"
        if addr.is_global:
            if literal not in self.other:
                self.other[literal] = f"OTHER_PUBLIC_IP_{len(self.other) + 1}"
            return self.other[literal]
        return None

    def text(self, s: str) -> str:
        if not s:
            return s
        for _ in range(3):  # a replacement can expose the next match (e.g. "lat,lon" after the first number)
            before = s
            for rx, label, raw in sorted(self.patterns, key=lambda t: -len(t[2])):
                s = rx.sub(self.repl.get(raw, label), s)
            if s == before:
                break
        s = _CIDR4.sub(self._cidr, s)
        s = _V6.sub(lambda m: (self._label_ip(m.group(0)) if _v6_plausible(m.group(0)) else None) or m.group(0), s)
        s = _V4.sub(lambda m: m.group(0) if _v4_version_like(m) else (self._label_ip(m.group(0)) or m.group(0)), s)
        return s

    def _cidr(self, m: re.Match) -> str:
        """``a.b.c.d/nn`` networks: REAL_NET when they contain a real IP, else left to the IP scan."""
        try:
            net = ipaddress.ip_network(m.group(0), strict=False)
        except ValueError:
            return m.group(0)
        for ip, label in self.known_ips.items():
            if label.startswith("REAL") and ":" not in ip and ipaddress.ip_address(ip) in net:
                return "REAL_NET"
        return m.group(0)

    def obj(self, value: Any) -> Any:
        """Redact every string (keys too) of a JSON-like value. Works on the decoded strings, so word
        boundaries are real ("City\\nOakland" in encoded JSON would hide the boundary)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value

    def strings(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [str(k) for k in value] + [x for v in value.values() for x in self.strings(v)]
        if isinstance(value, (list, tuple)):
            return [x for v in value for x in self.strings(v)]
        return []

    def check_obj(self, value: Any) -> list[str]:
        return sorted({label for s in self.strings(value) for label in self.check(s)})

    def check(self, s: str) -> list[str]:
        return sorted({label for rx, label, _ in self.patterns if label.startswith("REAL") and rx.search(s)})

    def labels(self) -> list[str]:
        return sorted({label for _, label, _ in self.patterns})

    def dom_pairs(self) -> list[list[str]]:
        """(needle, label) pairs for redacting the page's DOM text before a screenshot."""
        pairs = [[raw, label] for _, label, raw in self.patterns
                 if label in self.DOM_LABELS and "*" not in raw and not raw.startswith("AS")]
        return sorted(pairs, key=lambda p: -len(p[0]))


RED = Redactor()


def say(msg: str) -> None:
    print(RED.text(str(msg)), flush=True)


# --------------------------------------------------------------------------- page scripts

EXTRACT_JS = r"""
(() => {
  const clean = s => (s || '').replace(/[ \t\r\f\v]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim();
  const cls = el => (el && typeof el.className === 'string') ? el.className : '';
  const out = {url: location.href, title: document.title, readyState: document.readyState};
  out.text = clean(document.body ? document.body.innerText : '').slice(0, 400000);
  const rows = [];
  for (const tr of document.querySelectorAll('tr')) {
    const cells = [...tr.children].map(td => ({t: clean(td.innerText).slice(0, 600), c: cls(td)}));
    if (cells.length) rows.push({c: cls(tr), id: tr.id || '', cells});
    if (rows.length >= 800) break;
  }
  out.rows = rows;
  const colored = [];
  for (const el of document.querySelectorAll('[class*=fail],[class*=pass],[class*=warn],[class*=success],[class*=danger],[class*=error],[class*=red],[class*=green]')) {
    const t = clean(el.innerText).slice(0, 200);
    if (t && colored.length < 400) colored.push({c: cls(el).slice(0, 80), t});
  }
  out.colored = colored;
  out.iframes = [...document.querySelectorAll('iframe')].map(f => (f.src || '').slice(0, 200)).slice(0, 30);
  out.visibilityState = document.visibilityState;
  out.pre = [...document.querySelectorAll('pre, textarea, script[type="application/json"]')]
    .map(e => (e.value || e.textContent || '').slice(0, 30000)).filter(Boolean).slice(0, 20);
  return out;
})()
"""

FRAME_TEXT_JS = r"""
(() => ({url: location.href, title: document.title,
  text: (document.body ? document.body.innerText : '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim().slice(0, 60000)}))()
"""

REDACT_DOM_JS = r"""
((pairs) => {
  let n = 0;
  const fix = s => { for (const [a, b] of pairs) { if (s && s.toLowerCase().includes(a.toLowerCase())) {
      s = s.replace(new RegExp(a.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), b); } } return s; };
  const walk = root => {
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = tw.nextNode())) { const v = fix(node.nodeValue); if (v !== node.nodeValue) { node.nodeValue = v; n++; } }
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) walk(el.shadowRoot);
      if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.value) {
        const v = fix(el.value); if (v !== el.value) { el.value = v; n++; } }
    }
  };
  walk(document);
  return n;
})(%s)
"""

Send = Callable[[str, dict | None], Awaitable[dict]]


async def iso_read(send: Send, *, children: bool = True) -> dict[str, Any]:
    """Read the tab (and its same-process child frames) in a fresh isolated world - no Runtime.enable."""
    tree = (await send("Page.getFrameTree", {}))["frameTree"]
    out: dict[str, Any] = {"frames": []}

    def walk(node: dict, depth: int = 0):
        yield node["frame"], depth
        for child in node.get("childFrames") or []:
            yield from walk(child, depth + 1)

    for frame, depth in walk(tree):
        if depth and not children:
            continue
        rec: dict[str, Any] = {"depth": depth, "url": (frame.get("url") or "")[:300]}
        try:
            world = await send("Page.createIsolatedWorld", {"frameId": frame["id"], "worldName": "pp-audit-reader"})
            res = await send("Runtime.evaluate", {
                "expression": EXTRACT_JS if depth == 0 else FRAME_TEXT_JS,
                "contextId": world["executionContextId"], "returnByValue": True, "awaitPromise": True,
            })
            if res.get("exceptionDetails"):
                rec["error"] = str(res["exceptionDetails"].get("text"))[:300]
            else:
                rec["value"] = (res.get("result") or {}).get("value")
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        if depth == 0:
            out["main"] = rec
        elif len(out["frames"]) < 12:
            out["frames"].append(rec)
    return out


async def redact_dom(send: Send) -> int:
    tree = (await send("Page.getFrameTree", {}))["frameTree"]
    world = await send("Page.createIsolatedWorld", {"frameId": tree["frame"]["id"], "worldName": "pp-audit-redact"})
    res = await send("Runtime.evaluate", {"expression": REDACT_DOM_JS % json.dumps(RED.dom_pairs()),
                                          "contextId": world["executionContextId"], "returnByValue": True})
    return int((res.get("result") or {}).get("value") or 0)


async def capture(send: Send, out: Path) -> dict[str, Any]:
    """Redact the DOM, then a JPEG of the top of the page (<= 300 KB)."""
    info: dict[str, Any] = {}
    try:
        info["dom_redactions"] = await redact_dom(send)
    except Exception as exc:
        info["dom_redaction_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    await asyncio.sleep(0.3)
    metrics = await send("Page.getLayoutMetrics", {})
    vv = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
    size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
    width = max(320, min(int(vv.get("clientWidth") or 1280), 1600))
    height = max(400, min(int(size.get("height") or 1000), 2600))
    for scale, quality in ((0.6, 60), (0.5, 45), (0.4, 35), (0.33, 30)):
        params = {"format": "jpeg", "quality": quality, "captureBeyondViewport": True,
                  "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": scale}}
        data = base64.b64decode((await send("Page.captureScreenshot", params))["data"])
        if len(data) <= MAX_IMG:
            break
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    info.update({"file": f"img/{out.name}", "bytes": len(data), "css_clip": [width, height], "scale": scale,
                 "quality": quality})
    return info


# --------------------------------------------------------------------------- raw CDP


class RawCDP:
    """Minimal flat-session CDP client over the browser websocket (sends nothing on its own)."""

    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def __aenter__(self) -> "RawCDP":
        import websockets
        self.ws = await websockets.connect(self.ws_url, max_size=512 * 1024 * 1024, open_timeout=10,
                                           ping_interval=None)
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._reader.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass

    async def _read(self) -> None:
        try:
            async for msg in self.ws:
                data = json.loads(msg)
                fut = self._pending.pop(data["id"], None) if "id" in data else None
                if fut is not None and not fut.done():
                    fut.set_result(data)
        except Exception as exc:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError(str(exc)))

    async def send(self, method: str, params: dict | None = None, session_id: str | None = None,
                   timeout: float = 40.0) -> dict:
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        msg: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await self.ws.send(json.dumps(msg))
        data = await asyncio.wait_for(fut, timeout)
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error'].get('message')}")
        return data.get("result") or {}


# --------------------------------------------------------------------------- helpers


def import_profilepilot(retries: int = 5) -> SimpleNamespace:
    last = None
    for _ in range(retries):
        try:
            from profilepilot.automation import content
            from profilepilot.automation.manager import BrowserManager
            from profilepilot.browser.flags import build_chrome_args
            from profilepilot.browser.host import free_port
            from profilepilot.browser.prefs import prepare_user_data_dir
            from profilepilot.browser.runtime import RuntimeManager, kill_tree
            from profilepilot.models import LaunchOptions
            from profilepilot.paths import find_browser
            from profilepilot.proxy.check import check_proxy
            from profilepilot.safety import UrlPolicy
            from profilepilot.server import tools_browser
            from profilepilot.server.app import AppState
            from profilepilot.store import Store
            return SimpleNamespace(**locals())
        except Exception as exc:  # another workflow is editing src/
            last = exc
            say(f"import failed ({type(exc).__name__}: {str(exc)[:200]}); retrying in 60 s")
            time.sleep(60)
    raise RuntimeError(f"could not import profilepilot: {last}")


def kill_leftovers(marker: str) -> int:
    """Kill chrome.exe processes whose command line contains ``marker`` (our own dirs only)."""
    import psutil
    killed = 0
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "chrome.exe":
                continue
            if marker.lower() in " ".join(p.info["cmdline"] or []).lower():
                p.kill()
                killed += 1
        except psutil.Error:
            pass
    return killed


def leftovers(marker: str) -> list[str]:
    import psutil
    out = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if marker.lower() in " ".join(p.info["cmdline"] or []).lower():
                out.append(f"{p.info['name']}:{p.pid}")
        except psutil.Error:
            pass
    return out


GEO_URLS = (
    "https://ipwho.is/",
    "https://ipapi.co/json/",
    "http://ip-api.com/json/?fields=status,query,regionName,city,zip,lat,lon,isp,org,as,asname,reverse",
)
GEO_TERMS: list[list[str]] = []  # (value, label) found by collect_geo; persisted to --extra-terms (scratch)
_ISP_SUFFIX = re.compile(r"[ ,]+(llc|inc\.?|ltd\.?|gmbh|s\.a\.|corp\.?|co\.?)$", re.I)


def collect_geo(found: dict[str, Any]) -> None:
    """GeoIP details of the real IP from three lookup services -> REAL_* redaction patterns."""
    import httpx
    lats: set[int] = set()
    lons: set[int] = set()
    with httpx.Client(trust_env=False, timeout=10) as client:
        for url in GEO_URLS:
            try:
                d = client.get(url).json()
            except Exception:
                continue
            conn = d.get("connection") if isinstance(d.get("connection"), dict) else {}
            for key in ("city",):
                RED.add_text(str(d.get(key) or ""), "REAL_CITY")
            for key in ("region", "regionName"):
                RED.add_text(str(d.get(key) or ""), "REAL_REGION")
            for key in ("postal", "zip"):
                v = str(d.get(key) or "").strip()
                if len(v) >= 3:
                    RED._add(r"(?<![\w.])" + re.escape(v) + r"(?![\w.])", "REAL_POSTAL", v)
            names = [d.get("isp"), d.get("org"), d.get("asname"), conn.get("isp"), conn.get("org"), conn.get("domain")]
            asn = d.get("asn") or conn.get("asn") or d.get("as")
            for name in names:
                name = str(name or "").strip()
                if len(name) < 4:
                    continue
                RED.add_text(name, "REAL_ISP")
                RED.add_text(_ISP_SUFFIX.sub("", name), "REAL_ISP")
                first = re.split(r"[\s,\-]+", name)[0]
                if len(first) >= 5 and first.isalpha():
                    RED.add_text(first, "REAL_ISP")
            m = re.search(r"(\d{3,7})", str(asn or ""))
            if m:
                RED._add(r"AS\s?" + m.group(1) + r"", "REAL_ASN", "AS" + m.group(1))
            if d.get("reverse"):
                RED.add_text(str(d["reverse"]), "REAL_HOSTNAME")
            for key, bucket in (("latitude", lats), ("lat", lats), ("longitude", lons), ("lon", lons)):
                try:
                    bucket.add(int(float(d.get(key))))
                except (TypeError, ValueError):
                    pass
    RED._add(r"[\w-]*REAL_IP[\w-]*(?:\.[\w-]+)+\.[a-z]{2,}(?![\w.])", "REAL_HOSTNAME", "host-with-ip")
    RED._add(r"(zip|postal[\w ]*)([^\d]{0,20})(?<!\d)\d{5}(?:-\d{4})?(?!\d)", "REAL_POSTAL", "near-label-zip",
             repl=r"\1\2REAL_POSTAL")
    # Coordinates: degrees/minutes/seconds anywhere, decimals near the GeoIP position.
    RED._add(r"\d{1,3}\s?°\s?\d{1,2}\s?[′']\s?[\d.]*\s?[″\"]?\s?[NSEW]?", "REAL_COORD", "DMS")
    degs = sorted({abs(d + k) for d in lats | lons for k in (-1, 0, 1)})
    if degs:
        num = r"-?(?:" + "|".join(str(d) for d in degs) + r")\.\d{2,}"
        # a decimal near a lat/long label, and "lat, lon" pairs
        RED._add(r"(lat\w*|lon\w*|lng|coord\w*|geoloc\w*)([^\d-]{0,40})(?<![\d.])" + num, "REAL_COORD",
                 "near-label-coord", repl=r"\1\2REAL_COORD")
        RED._add(r"(?<![\d.])" + num + r"\s*[,/;]\s*" + num, "REAL_COORD", "coord-pair")
    found["geo_patterns"] = True


def load_extra_terms(path: Path) -> int:
    """Extra (value, label) pairs kept OUTSIDE the repo (scratch): GeoIP names other sites show."""
    if not path.exists():
        return 0
    n = 0
    for value, label in json.loads(path.read_text(encoding="utf-8")):
        if label.startswith("REAL_IP") or label.startswith("OTHER"):
            RED.add_ip(value, label) if not label.startswith("OTHER") else RED._add(
                r"(?<![\d.])" + re.escape(value), label, value)
        else:
            RED.add_text(value, label, min_len=3)
        n += 1
    return n


def redact_existing(out_dir: Path) -> None:
    """Re-apply the (possibly extended) redaction to already written detector-*.json files."""
    for path in sorted((out_dir / "raw").glob("detector-*.json")):
        text = path.read_text(encoding="utf-8")
        data = RED.obj(json.loads(text))
        new = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
        leaks = sorted(set(RED.check_obj(data)) | set(RED.check(new)))
        if leaks:
            say(f"{path.name}: still has {leaks}; deleting it")
            path.unlink()
            continue
        if new != text:
            path.write_text(new, encoding="utf-8")
            say(f"re-redacted {path.name}")


def discover_identity(pp: SimpleNamespace) -> dict[str, Any]:
    """Real public IPs (+ machine IPv6, rDNS, ISP, city) -> redaction labels. Never printed raw."""
    import httpx
    import psutil
    found: dict[str, Any] = {"v4": False, "v6": False}
    with httpx.Client(trust_env=False, timeout=10) as client:
        for u in ("https://api.ipify.org?format=json", "https://api64.ipify.org?format=json",
                  "https://api6.ipify.org?format=json"):
            try:
                ip = client.get(u).json().get("ip")
            except Exception:
                continue
            if ip:
                v6 = ":" in ip
                RED.add_ip(ip, "REAL_IP6" if v6 else "REAL_IP")
                found["v6" if v6 else "v4"] = True
    chk = asyncio.run(pp.check_proxy(None, timeout=15))
    if chk.ip:
        RED.add_ip(chk.ip, "REAL_IP6" if ":" in chk.ip else "REAL_IP")
        found["v6" if ":" in chk.ip else "v4"] = True
    RED.add_text(chk.isp, "REAL_ISP")
    RED.add_text(chk.city, "REAL_CITY")
    RED.add_text(chk.region, "REAL_REGION")
    found["country_code"] = chk.country_code
    found["timezone"] = chk.timezone
    collect_geo(found)
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            try:
                addr = ipaddress.ip_address(a.address.split("%", 1)[0])
            except ValueError:
                continue
            if addr.version == 6 and addr.is_global:
                RED.add_ip(str(addr), "REAL_IP6")
                found["v6"] = True
            elif addr.is_private and not addr.is_loopback:
                RED.add_ip(str(addr), "LAN_IP6" if addr.version == 6 else "LAN_IP")
    for ip, label in list(RED.known_ips.items()):
        if label.startswith("REAL"):
            with concurrent.futures.ThreadPoolExecutor(1) as ex:
                try:
                    host = ex.submit(socket.gethostbyaddr, ip).result(timeout=6)[0]
                except Exception:
                    host = None
            if host and host != ip:
                RED.add_text(host, "REAL_HOSTNAME")
                found["rdns"] = True
    return found


def save_json(out_dir: Path, name: str, data: dict[str, Any]) -> bool:
    data = RED.obj(data)
    text = json.dumps(data, indent=1, ensure_ascii=False)
    leaks = sorted(set(RED.check_obj(data)) | set(RED.check(text)))
    if leaks:
        say(f"REFUSING to write {name}: unredacted values remain ({leaks})")
        for rx, label, raw in RED.patterns:
            if label in leaks:
                for m in list(rx.finditer(text))[:3]:
                    ctx = text[max(0, m.start() - 40):m.end() + 20]
                    say(f"    {label} via {raw[:3]}...: " + re.sub(r"[0-9]", "#", re.sub(r"[A-Za-z]", "x", m.group(0)))
                        + " | context: " + re.sub(r"[0-9]", "#", ctx).replace(chr(10), " "))
        return False
    path = out_dir / "raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    say(f"    wrote raw/{name} ({len(text) // 1024} KB)")
    return True


def pick_page_target(targets: list[dict], site: Site) -> dict | None:
    pages = [t for t in targets if t.get("type") == "page"]
    host = site.url.split("/")[2].replace("www.", "")
    for t in pages:
        if host in (t.get("url") or ""):
            return t
    web = [t for t in pages if (t.get("url") or "").startswith("http")]
    return (web or pages or [None])[0]


# --------------------------------------------------------------------------- B1


async def run_b1(site: Site, pp: SimpleNamespace, base: Path, out_dir: Path, run_id: str, wait: float,
                 cfg: str = "B1") -> dict[str, Any]:
    """B1, or with ``cfg="PR"``: same launch on about:blank, then ONE raw CDP ``Page.navigate`` (no
    Runtime.enable, no auto-attach), detach at once; read like B1 afterwards."""
    browser = pp.find_browser()
    udd = base / ("b1" if cfg == "B1" else "pr") / f"{site.key}-{run_id}"
    launch = pp.LaunchOptions(window="offscreen")
    pp.prepare_user_data_dir(udd, launch)  # exactly what the host does before a launch
    port = pp.free_port()
    argv = pp.build_chrome_args(browser=browser, user_data_dir=udd, cdp_port=port, launch=launch,
                                relay_port=None, start_urls=[site.url] if cfg == "B1" else [])
    proc = subprocess.Popen([browser.path, *argv], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, close_fds=True)
    started = time.time()
    result: dict[str, Any] = {"config": cfg, "browser_version": browser.version,
                              "argv": [a if not a.startswith("--user-data-dir=") else "--user-data-dir=<fresh tmp dir>"
                                       for a in argv]}
    import httpx
    if cfg == "PR":
        for _ in range(100):
            try:
                ver0 = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=1, trust_env=False).json()
                break
            except Exception:
                await asyncio.sleep(0.2)
        async with RawCDP(ver0["webSocketDebuggerUrl"]) as cdp0:
            tab = next(t for t in (await cdp0.send("Target.getTargets"))["targetInfos"] if t["type"] == "page")
            sid0 = (await cdp0.send("Target.attachToTarget", {"targetId": tab["targetId"], "flatten": True}))["sessionId"]
            result["raw_navigate"] = await cdp0.send("Page.navigate", {"url": site.url}, sid0)
            await cdp0.send("Target.detachFromTarget", {"sessionId": sid0})
        result["detached_at_s"] = round(time.time() - started, 1)
    await asyncio.sleep(wait + 3)  # no CDP client during this time
    try:
        ver = None
        for _ in range(50):
            try:
                ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2, trust_env=False).json()
                break
            except Exception:
                await asyncio.sleep(0.3)
        if not ver:
            raise RuntimeError(f"DevTools endpoint did not answer (chrome exit code: {proc.poll()})")
        async with RawCDP(ver["webSocketDebuggerUrl"]) as cdp:
            targets = (await cdp.send("Target.getTargets"))["targetInfos"]
            result["targets"] = [{"type": t.get("type"), "url": (t.get("url") or "")[:200]} for t in targets
                                 if t.get("type") in ("page", "iframe")]
            target = pick_page_target(targets, site)
            if not target:
                raise RuntimeError("no page target")
            sid = (await cdp.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["sessionId"]
            attached = time.time()

            async def send(method: str, params: dict | None = None) -> dict:
                return await cdp.send(method, params, sid)

            result["read_at_s"] = round(attached - started, 1)
            result["iso_final"] = await iso_read(send)
            result["screenshot"] = await capture(send, out_dir / "img" / f"detector-{site.key}-{cfg}.jpg")
            try:
                await cdp.send("Target.detachFromTarget", {"sessionId": sid})
            except Exception:
                pass
            try:
                await cdp.send("Browser.close", timeout=5)
            except Exception:
                pass
    finally:
        try:
            proc.wait(15)
            result["closed"] = "Browser.close"
        except subprocess.TimeoutExpired:
            pp.kill_tree(proc.pid)
            result["closed"] = "killed"
        n = kill_leftovers(str(udd))
        if n:
            result["closed"] += f" (+{n} leftovers killed)"
    result["elapsed_s"] = round(time.time() - started, 1)
    return result


# --------------------------------------------------------------------------- P


async def rebrowser_actions(ctx: Any, name: str, pp: SimpleNamespace, page: Any) -> list[dict[str, Any]]:
    """bot-detector.rebrowser.net asks the automation side to call page functions; do it with the
    MCP tool browser_evaluate (Playwright page.evaluate in the main world), as an agent would."""
    actions = []
    exprs = [
        ("dummyFn", "() => window.dummyFn()"),
        ("sourceUrlLeak / getElementById", "() => document.getElementById('detections-json')"),
        ("mainWorldExecution / getElementsByClassName", "() => document.getElementsByClassName('div')"),
    ]
    for label, expr in exprs:
        t = time.time()
        try:
            out = await pp.tools_browser.browser_evaluate(ctx, name, expr)
            actions.append({"tool": "browser_evaluate", "label": label, "expression": expr,
                            "result_head": "\n".join(out.splitlines()[:3])[:300]})
        except Exception as exc:
            actions.append({"tool": "browser_evaluate", "label": label, "expression": expr,
                            "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        actions[-1]["s"] = round(time.time() - t, 2)
        await asyncio.sleep(0.5)
    return actions


SITE_ACTIONS = {"rebrowser": rebrowser_actions}


async def run_pd(site: Site, pp: SimpleNamespace, store: Any, rt: Any, bm: Any, out_dir: Path, run_id: str,
                 wait: float) -> dict[str, Any]:
    """Mitigation experiment "PD": like P, but the Playwright connection is dropped right after
    browser_navigate returns (CDP detached while the site runs its tests); the page is read
    afterwards like B1 (raw CDP, isolated world, no Runtime.enable)."""
    name = f"detpd-{site.key}-{run_id}"
    profile = store.create_profile(name, launch=pp.LaunchOptions(window="offscreen"), tags=["audit"])
    state = pp.AppState(store=store, runtime=rt, browsers=bm, policy=pp.UrlPolicy())
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
    result: dict[str, Any] = {"config": "PD", "profile": {"name": name, "window": "offscreen", "proxy": None}}
    started = time.time()
    try:
        nav = await pp.tools_browser.browser_navigate(ctx, name, site.url)
        result["navigate"] = {"tool": "browser_navigate", "result_head": "\n".join(nav.splitlines()[:2])[:400],
                              "s": round(time.time() - started, 1)}
        await bm.disconnect(name)
        result["detached_at_s"] = round(time.time() - started, 1)
        await asyncio.sleep(wait)
        info = rt.status(profile.id)
        if info is None:
            result["browser_crashed"] = True
            raise RuntimeError("the browser exited while the site was running")
        async with RawCDP(info.cdp_ws_url) as cdp:
            targets = (await cdp.send("Target.getTargets"))["targetInfos"]
            target = pick_page_target(targets, site)
            sid = (await cdp.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["sessionId"]

            async def send(method: str, params: dict | None = None) -> dict:
                return await cdp.send(method, params, sid)

            result["read_at_s"] = round(time.time() - started, 1)
            result["iso_final"] = await iso_read(send)
            result["screenshot"] = await capture(send, out_dir / "img" / f"detector-{site.key}-PD.jpg")
            try:
                await cdp.send("Target.detachFromTarget", {"sessionId": sid})
            except Exception:
                pass
    finally:
        try:
            result["closed"] = "RuntimeManager.stop -> " + str(rt.stop(profile.id))
        except Exception as exc:
            result["closed"] = f"stop failed: {exc}"
    result["elapsed_s"] = round(time.time() - started, 1)
    return result


async def run_p(site: Site, pp: SimpleNamespace, store: Any, rt: Any, bm: Any, out_dir: Path, run_id: str,
                wait: float) -> dict[str, Any]:
    name = f"det-{site.key}-{run_id}"
    profile = store.create_profile(name, launch=pp.LaunchOptions(window="offscreen"), tags=["audit"])
    state = pp.AppState(store=store, runtime=rt, browsers=bm, policy=pp.UrlPolicy())
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
    result: dict[str, Any] = {"config": "P", "profile": {"name": name, "window": "offscreen", "proxy": None}}
    started = time.time()
    try:
        nav = await pp.tools_browser.browser_navigate(ctx, name, site.url)
        result["navigate"] = {"tool": "browser_navigate", "result_head": "\n".join(nav.splitlines()[:2])[:400],
                              "s": round(time.time() - started, 1)}
        await asyncio.sleep(wait)
        if rt.status(profile.id) is None:  # do not let bm.session() autostart a crashed browser again
            result["browser_crashed"] = True
            raise RuntimeError("the browser exited while the site was running")
        session = await bm.session(name)
        pages = [p for p in session.context.pages if not p.is_closed()]
        page = session._current(pages)  # the active tab, without a main-world evaluate
        result["tabs"] = [p.url[:200] for p in pages]
        holder = {"cdp": await session.context.new_cdp_session(page), "reattached": 0}

        async def send(method: str, params: dict | None = None) -> dict:
            try:
                return await holder["cdp"].send(method, params or {})
            except Exception as exc:  # the tab swapped its renderer (cross-process navigation): re-attach
                if "closed" not in str(exc).lower() or holder["reattached"] >= 3:
                    raise
                holder["reattached"] += 1
                result["cdp_reattached"] = holder["reattached"]
                live = [p for p in session.context.pages if not p.is_closed()]
                holder["cdp"] = await session.context.new_cdp_session(session._current(live))
                return await holder["cdp"].send(method, params or {})

        result["iso_before_read"] = await iso_read(send)
        # What browser_read does: open_page (session.page -> main-world visibilityState probe) + read_page.
        t = time.time()
        try:
            page = await session.page(None, interactive=False)
            md = await pp.content.read_page(page, fmt="markdown")
            result["tool_read"] = {"fn": "content.read_page(fmt=markdown)", "chars": len(md), "s": round(time.time() - t, 2),
                                   "markdown": md[:300000]}
        except Exception as exc:
            result["tool_read"] = {"error": f"{type(exc).__name__}: {str(exc)[:400]}"}
        if site.actions:
            await asyncio.sleep(1)
            result["iso_after_read"] = await iso_read(send)  # did the main-world read alone trigger anything?
            result["actions"] = await SITE_ACTIONS[site.actions](ctx, name, pp, page)
            await asyncio.sleep(3)
        await asyncio.sleep(1)
        result["iso_final"] = await iso_read(send)
        t = time.time()
        try:
            shot = await pp.tools_browser.browser_screenshot(ctx, name)
            img = shot[0]
            result["tool_screenshot"] = {"ok": True, "bytes": len(img.data), "s": round(time.time() - t, 2),
                                         "caption_head": str(shot[1]).splitlines()[-1][:200]}
        except Exception as exc:
            result["tool_screenshot"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                                         "s": round(time.time() - t, 2)}
        result["screenshot"] = await capture(send, out_dir / "img" / f"detector-{site.key}-P.jpg")
        try:
            await holder["cdp"].detach()
        except Exception:
            pass
        info = rt.status(profile.id)
        result["runtime"] = {"browser_version": info.browser_version if info else None,
                             "window": info.window if info else None}
    except Exception as exc:  # keep the partial result
        result["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        result["traceback"] = traceback.format_exc()[-2500:]
    finally:
        try:
            await bm.disconnect(name)
        except Exception:
            pass
        try:
            result["closed"] = "RuntimeManager.stop -> " + str(rt.stop(profile.id))
        except Exception as exc:
            result["closed"] = f"stop failed: {exc}"
        log = store.host_log(profile.id)
        try:
            launching = [ln for ln in log.read_text("utf-8", "replace").splitlines() if " launching " in ln]
            line = launching[-1].split(" launching ", 1)[1] if launching else None
            result["host_launch_line"] = re.sub(r"--user-data-dir=\S+", "--user-data-dir=<profile dir>", line or "")
            exits = [ln.split(" profilepilot.browser.host: ", 1)[-1] for ln in log.read_text("utf-8", "replace").splitlines()
                     if "browser exited" in ln]
            result["host_browser_exit"] = exits[-3:]
        except OSError:
            pass
    result["elapsed_s"] = round(time.time() - started, 1)
    return result


# --------------------------------------------------------------------------- main


async def main_async(args: argparse.Namespace, pp: SimpleNamespace, base: Path, out_dir: Path) -> None:
    sites = [s for s in SITES if not args.sites or s.key in args.sites]
    configs = args.configs
    store = pp.Store(base)
    assert store.secrets.backend == "file", store.secrets.backend
    rt = pp.RuntimeManager(store)
    run_id = time.strftime("%H%M%S")
    async with pp.BrowserManager(store, rt) as bm:
        for site in sites:
            for cfg in configs:
                wait = site.wait * args.wait_scale
                say(f"=== {site.key} [{cfg}] {site.url} (wait {wait:.0f}s)")
                meta: dict[str, Any] = {"site": site.key, "url": site.url, "config": cfg, "wait_s": wait,
                                        "run_id": run_id, "when": time.strftime("%Y-%m-%d %H:%M:%S")}
                try:
                    if cfg in ("B1", "PR"):
                        meta.update(await run_b1(site, pp, base, out_dir, run_id, wait, cfg))
                    elif cfg == "PD":
                        meta.update(await run_pd(site, pp, store, rt, bm, out_dir, run_id, wait))
                    else:
                        meta.update(await run_p(site, pp, store, rt, bm, out_dir, run_id, wait))
                except Exception as exc:
                    meta["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                    meta["traceback"] = traceback.format_exc()[-2500:]
                    say(f"    ERROR {meta['error']}")
                main = ((meta.get("iso_final") or {}).get("main") or {}).get("value") or {}
                say(f"    title={main.get('title')!r} chars={len(main.get('text') or '')} rows={len(main.get('rows') or [])}"
                    f" shot={(meta.get('screenshot') or {}).get('bytes')} closed={meta.get('closed')}")
                save_json(out_dir, f"detector-{site.key}-{cfg}.json", meta)
                await asyncio.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--configs", default="B1,P")
    ap.add_argument("--sites", default="")
    ap.add_argument("--out", default=str(AUDIT))
    ap.add_argument("--wait-scale", type=float, default=1.0)
    ap.add_argument("--extra-terms", default="", help="JSON [[value, label], ...] kept outside the repo")
    ap.add_argument("--redact-only", action="store_true", help="only re-redact existing raw files")
    args = ap.parse_args()
    args.configs = [c.strip().upper() for c in args.configs.split(",") if c.strip()]
    args.sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    base = Path(args.scratch).resolve() / "audit-home" / "detectors-noproxy"
    base.mkdir(parents=True, exist_ok=True)
    os.environ["PROFILEPILOT_HOME"] = str(base)
    os.environ["PROFILEPILOT_SECRETS"] = "file"
    out_dir = Path(args.out).resolve()
    pp = import_profilepilot()
    found = discover_identity(pp)
    say(f"identity: v4={found.get('v4')} v6={found.get('v6')} rdns={found.get('rdns', False)} "
        f"labels={RED.labels()}")
    if not found.get("v4") and not found.get("v6"):
        say("could not determine the real IP; refusing to run")
        return 2
    if args.extra_terms:
        extra = Path(args.extra_terms)
        say(f"extra redaction terms: {load_extra_terms(extra)}")
        old = json.loads(extra.read_text(encoding="utf-8")) if extra.exists() else []
        extra.write_text(json.dumps(old + [t for t in GEO_TERMS if t not in old], indent=0), encoding="utf-8")
    if args.redact_only:
        redact_existing(out_dir)
        return 0
    try:
        asyncio.run(main_async(args, pp, base, out_dir))
    finally:
        try:
            rt = pp.RuntimeManager(pp.Store(base))
            stopped = rt.stop_all()
            if stopped:
                say(f"stop_all stopped: {stopped}")
        except Exception as exc:
            say(f"stop_all failed: {exc}")
        n = kill_leftovers(str(base))
        say(f"leftover chrome.exe for {base.name}: {n} killed; remaining processes: {leftovers(str(base))}")
        if RED.other:
            say(f"other public IP literals redacted: {len(RED.other)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
