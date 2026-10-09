"""Run the local fingerprint probe (probe_server.py) in every configuration and save redacted results.

Configurations (each in a FRESH user-data-dir, nothing external except ipify + one STUN server):

  B0   plain ``chrome.exe --user-data-dir=<fresh> <probe url>``: no debugging port, no CDP, no flags.
       The fresh dir gets an empty ``First Run`` sentinel, otherwise Chrome 154 shows the modal
       "Sign in to Chrome" first-run window and never opens the URL (= a returning user's Chrome).
  B1   chrome.exe with EXACTLY ProfilePilot's switches (``build_chrome_args`` + ``prepare_user_data_dir``
       like the host does), window=offscreen like P, debugging port open but no CDP client.
  B1N  as B1 with window=normal.
  P    ProfilePilot profile (window=offscreen) started by RuntimeManager and attached by BrowserManager
       (Playwright connect_over_cdp no_defaults, contexts[0]); the probe is opened with the MCP tool
       function ``browser_navigate`` itself (server/tools_browser.py, fake MCP context).
  P2   as P, but ``browser_read`` (a main-world ``page.evaluate``) runs on about:blank BEFORE the probe loads.
  P3   as P, plus ``browser_snapshot`` + ``browser_read`` on the probe page WHILE it watches (late phase).
  PN   as P with window=normal.
  PX   P + saved proxy "audit-socks5" (ProfilePilot relay -> authenticated SOCKS5 upstream).
  PXH  P + saved proxy "audit-http"   (ProfilePilot relay -> authenticated HTTP upstream).
  P0   isolation: host-launched profile with the probe as start URL and NO CDP client at all.
  PL   isolation: as P, but the probe is opened by a renderer-initiated navigation
       (page.evaluate("location.href = url")) instead of browser_navigate (CDP Page.navigate).

A plain Chrome window that is covered by other windows reports visibilityState "hidden" (native
occlusion tracking), so when B0 comes back hidden it is kept as ``B0-occluded`` and B0 is re-run
(up to 2 times) to get the visible reference.

``--ablate`` instead runs B0 plus ONE ProfilePilot switch at a time (``A-<switch>``) to attribute
each difference to a single flag.

Usage (from the repo root, with the project venv)::

    python docs/audit/scripts/run_probe_matrix.py --scratch <scratch dir> [--configs B0,B1,P] [--late 12]

Environment: ``PROFILEPILOT_HOME`` is forced to ``<scratch>/audit-home/localprobe`` and
``PROFILEPILOT_SECRETS=file`` for every ProfilePilot call; the two audit proxies are copied
in-process from the user's default store (saved proxies "test-proxy-socks5"/"test-proxy-http") and
are never printed. Everything written under docs/audit/ is redacted: the real IP becomes
``REAL_IP``, the proxy exit IP ``PROXY_EXIT_IP``; proxy host/user/password never appear.

Writes ``docs/audit/raw/probe-<CFG>.json`` and ``docs/audit/img/probe-<CFG>.jpg``. Run
``diff_probe.py`` afterwards for the comparison against B0.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
AUDIT = HERE.parent
REPO = AUDIT.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "src"))

from probe_server import ProbeServer  # noqa: E402

CREATE_NO_WINDOW = 0x08000000


@dataclass
class Spec:
    cfg: str
    kind: str  # "plain" | "flags" | "pp" | "pp0"
    window: str = "offscreen"
    proxy: str | None = None
    pre_read: bool = False
    post_read: bool = False
    nav: str = "tool"  # "tool" = browser_navigate; "location" = renderer-initiated location.href via page.evaluate
    extra: list[str] = field(default_factory=list)  # plain: extra switches on top of B0 (ablation)
    description: str = ""


SPECS = [
    Spec("B0", "plain", window="(chrome default)", description="plain chrome.exe --user-data-dir=<fresh> <url>; no debugging port; 'First Run' sentinel pre-created"),
    Spec("B1", "flags", window="offscreen", description="chrome.exe with exactly build_chrome_args(window=offscreen) + prepare_user_data_dir; port open, no CDP client"),
    Spec("B1N", "flags", window="normal", description="as B1 with window=normal"),
    Spec("P", "pp", window="offscreen", description="RuntimeManager + BrowserManager (connect_over_cdp no_defaults, contexts[0]); probe opened with tools_browser.browser_navigate"),
    Spec("P2", "pp", window="offscreen", pre_read=True, description="as P + browser_read (main-world page.evaluate) on about:blank before the probe loads"),
    Spec("P3", "pp", window="offscreen", post_read=True, description="as P + browser_snapshot and browser_read on the probe page during its late watch window"),
    Spec("PN", "pp", window="normal", description="as P with window=normal"),
    Spec("PX", "pp", window="offscreen", proxy="audit-socks5", description="as P + saved proxy audit-socks5 (relay -> authenticated SOCKS5)"),
    Spec("PXH", "pp", window="offscreen", proxy="audit-http", description="as P + saved proxy audit-http (relay -> authenticated HTTP CONNECT)"),
    # Isolation configs (not in the plan's list): separate the host launch from the CDP client.
    Spec("P0", "pp0", window="offscreen", description="ProfilePilot profile started by RuntimeManager (host process) with launch.start_url = probe; NO CDP client ever attached"),
    Spec("PL", "pp", window="offscreen", nav="location", description="as P, but the probe is opened by a renderer-initiated navigation (page.evaluate: location.href = url) instead of browser_navigate/Page.navigate"),
]

#: Ablation: B0 plus ONE of ProfilePilot's switches (window=normal, no proxy) - run with --ablate.
ABLATION_SWITCHES = [
    "--profile-directory=Default", "--remote-debugging-port={port}", "--no-first-run", "--no-default-browser-check",
    "--disable-search-engine-choice-screen", "--hide-crash-restore-bubble", "--disable-back-forward-cache",
    "--disable-backgrounding-occluded-windows", "--restore-last-session", "--window-position=-32000,-32000",
]


def ablation_specs() -> list[Spec]:
    out = []
    for sw in ABLATION_SWITCHES:
        name = sw[2:].split("=", 1)[0]
        out.append(Spec(f"A-{name}", "plain", window="(chrome default)", extra=[sw],
                        description=f"B0 + {sw.replace('{port}', '<free port>')} only"))
    out.append(Spec("A-none", "plain", window="(chrome default)", description="B0 again (variance control for the ablation)"))
    return out


# --------------------------------------------------------------------------- redaction


class Redactor:
    """Replaces every sensitive value with a label. ``check()`` asserts nothing slipped through."""

    def __init__(self) -> None:
        self.ips: dict[str, str] = {}          # ip literal -> label
        self.secrets: dict[str, str] = {}      # other strings -> label

    def add_ip(self, ip: str | None, label: str) -> None:
        if not ip:
            return
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return
        self.ips.setdefault(ip, label)

    def add_secret(self, value: str | None, label: str) -> None:
        if value and len(value) >= 3:
            self.secrets.setdefault(value, label)

    def text(self, s: str) -> str:
        for value, label in sorted(self.secrets.items(), key=lambda kv: -len(kv[0])):
            s = s.replace(value, label)
        for ip, label in sorted(self.ips.items(), key=lambda kv: -len(kv[0])):
            if ":" in ip:
                s = re.sub(r"(?<![0-9A-Fa-f:])" + re.escape(ip) + r"(?![0-9A-Fa-f:])", label, s, flags=re.I)
            else:
                s = re.sub(r"(?<![\d.])" + re.escape(ip) + r"(?![\d.])", label, s)
        return s

    def obj(self, value: Any) -> Any:
        return json.loads(self.text(json.dumps(value)))

    def check(self, s: str) -> list[str]:
        leaks = [label for value, label in self.secrets.items() if value in s]
        for ip, label in self.ips.items():
            if re.search(r"(?<![\d.:A-Fa-f])" + re.escape(ip) + r"(?![\d.:A-Fa-f])", s, flags=re.I):
                leaks.append(label)
        return leaks


RED = Redactor()


def say(msg: str) -> None:
    print(RED.text(msg), flush=True)


def classify_webrtc_ips(main: dict[str, Any]) -> None:
    """Register every IP literal seen in WebRTC candidates with the redactor (LAN / other public)."""
    rtc = (main or {}).get("webrtc") or {}
    for cand in rtc.get("candidates") or []:
        for key in ("address", "relatedAddress"):
            addr = cand.get(key)
            if not addr or addr.endswith(".local"):
                continue
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if addr in RED.ips or ip.is_unspecified or ip.is_loopback:
                continue
            if ip.is_private or ip.is_link_local:
                RED.add_ip(addr, "LAN_IP6" if ip.version == 6 else "LAN_IP")
            else:
                RED.add_ip(addr, "UNKNOWN_PUBLIC_IP6" if ip.version == 6 else "UNKNOWN_PUBLIC_IP")


# --------------------------------------------------------------------------- helpers


def import_profilepilot(retries: int = 5):
    """Import the modules the MCP server uses; retry while another workflow edits src/."""
    last = None
    for attempt in range(retries):
        try:
            from profilepilot.automation.manager import BrowserManager
            from profilepilot.browser.flags import build_chrome_args
            from profilepilot.browser.host import free_port
            from profilepilot.browser.prefs import prepare_user_data_dir
            from profilepilot.browser.runtime import RuntimeManager
            from profilepilot.models import LaunchOptions
            from profilepilot.paths import data_root, find_browser
            from profilepilot.proxy.check import check_proxy, check_saved_proxy
            from profilepilot.proxy.url import ProxyEndpoint
            from profilepilot.safety import UrlPolicy
            from profilepilot.secrets import SecretStore
            from profilepilot.server import tools_browser
            from profilepilot.server.app import AppState
            from profilepilot.store import Store
            return SimpleNamespace(**locals())
        except Exception as exc:  # concurrent edit in src/
            last = exc
            say(f"import failed ({type(exc).__name__}: {str(exc)[:200]}); retrying in 60 s")
            time.sleep(60)
    raise RuntimeError(f"could not import profilepilot: {last}")


def winshot(pid: int, out: Path) -> dict[str, Any]:
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "winshot.ps1"),
           "-ProcessId", str(pid), "-Out", str(out)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        line = (res.stdout or "").strip().splitlines()
        data = json.loads(line[-1]) if line else {"error": (res.stderr or "")[:500]}
        if data.get("captured"):
            data["captured"]["file"] = f"img/{out.name}"
        return data
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def browser_cmdline(pid: int | None) -> list[str] | None:
    import psutil
    try:
        return psutil.Process(int(pid)).cmdline()[1:] if pid else None
    except Exception:
        return None


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


def close_plain(proc: subprocess.Popen, udd: Path) -> str:
    import psutil
    try:
        tree = [proc.pid] + [c.pid for c in psutil.Process(proc.pid).children(recursive=True)]
    except psutil.Error:
        tree = [proc.pid]
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T"], capture_output=True)
    try:
        proc.wait(10)
        how = "graceful (WM_CLOSE)"
    except subprocess.TimeoutExpired:
        how = "killed"
        for pid in tree:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            pass
    n = kill_leftovers(str(udd))
    return how + (f" (+{n} leftovers killed)" if n else "")


def headers_of(requests: list[dict[str, Any]], path: str, method: str = "GET") -> dict[str, str] | None:
    for r in requests:
        if r.get("path") == path and r.get("method") == method:
            return {k.lower(): v for k, v in r.get("headers") or []}
    return None


# --------------------------------------------------------------------------- runners


def run_plain_or_flags(spec: Spec, pp: Any, srv: ProbeServer, base: Path, late: int, run_id: str) -> dict[str, Any]:
    browser = pp.find_browser()
    udd = base / "plain" / f"{spec.cfg}-{run_id}"
    udd.mkdir(parents=True, exist_ok=True)
    url = srv.url("start", spec.cfg, late)
    if spec.kind == "plain":
        (udd / "First Run").write_bytes(b"")
        extra = [sw.replace("{port}", str(pp.free_port())) for sw in spec.extra]
        argv = [f"--user-data-dir={udd}", *extra, url]
        proc = subprocess.Popen([browser.path, *argv])
    else:
        launch = pp.LaunchOptions(window=spec.window)
        pp.prepare_user_data_dir(udd, launch)  # exactly what the host does before launching
        argv = pp.build_chrome_args(browser=browser, user_data_dir=udd, cdp_port=pp.free_port(), launch=launch,
                                    relay_port=None, start_urls=[url])
        proc = subprocess.Popen([browser.path, *argv], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, close_fds=True)
    started = time.time()
    got_main = srv.wait_phase(spec.cfg, "main", 60)
    got_late = srv.wait_phase(spec.cfg, "late", late + 30) if got_main else False
    shot = winshot(proc.pid, AUDIT / "img" / f"probe-{spec.cfg}.jpg")
    cmdline = browser_cmdline(proc.pid)
    closed = close_plain(proc, udd)
    return {"browser": {"path": browser.path, "version": browser.version}, "argv": argv, "browser_cmdline": cmdline,
            "window_capture": shot, "closed": closed, "complete": bool(got_main and got_late),
            "elapsed_s": round(time.time() - started, 1), "actions": []}


async def run_pp_async(spec: Spec, pp: Any, srv: ProbeServer, store: Any, rt: Any, late: int, run_id: str) -> dict[str, Any]:
    name = f"probe-{spec.cfg}-{run_id}"
    proxy_id = store.get_proxy(spec.proxy).id if spec.proxy else None
    profile = store.create_profile(name, proxy_id=proxy_id, launch=pp.LaunchOptions(window=spec.window), tags=["audit"])
    url = srv.url("start", spec.cfg, late)
    actions: list[dict[str, Any]] = []
    result: dict[str, Any] = {"profile": {"name": name, "id": profile.id, "window": spec.window, "proxy": spec.proxy}}

    async def wait_phase(phase: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if srv.store.has_phase(spec.cfg, phase):
                return True
            await asyncio.sleep(0.25)
        return srv.store.has_phase(spec.cfg, phase)

    def first_line(text: str) -> str:
        return (text or "").strip().splitlines()[0][:300] if (text or "").strip() else ""

    started = time.time()
    async with pp.BrowserManager(store, rt) as bm:
        state = pp.AppState(store=store, runtime=rt, browsers=bm, policy=pp.UrlPolicy())
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
        if spec.pre_read:
            out = await pp.tools_browser.browser_read(ctx, name)
            actions.append({"tool": "browser_read", "on": "about:blank (before the probe)", "t": round(time.time() - started, 2),
                            "result_head": first_line(out)})
        if spec.nav == "location":
            session = await bm.session(name)
            page = await session.page(None, interactive=False)
            await page.evaluate("u => { location.href = u; }", url)
            actions.append({"tool": "page.evaluate(location.href = url)", "url": url, "t": round(time.time() - started, 2)})
        else:
            out = await pp.tools_browser.browser_navigate(ctx, name, url)
            actions.append({"tool": "browser_navigate", "url": url, "t": round(time.time() - started, 2), "result_head": first_line(out)})
        got_main = await wait_phase("main", 60)
        if spec.post_read and got_main:
            snap = await pp.tools_browser.browser_snapshot(ctx, name)
            actions.append({"tool": "browser_snapshot", "t": round(time.time() - started, 2), "chars": len(snap)})
            read = await pp.tools_browser.browser_read(ctx, name)
            actions.append({"tool": "browser_read", "t": round(time.time() - started, 2), "chars": len(read)})
        got_late = await wait_phase("late", late + 30) if got_main else False
        info = rt.status(profile.id)
    # BrowserManager closed: CDP connection dropped; the browser keeps running until stopped.
    chrome_pid = info.chrome_pid if info else None
    result.update({
        "runtime": {"browser_version": info.browser_version if info else None, "window": info.window if info else None,
                    "relay": bool(info and info.relay_port), "upstream": info.upstream if info else None},
        "browser_cmdline": browser_cmdline(chrome_pid),
        "window_capture": winshot(chrome_pid, AUDIT / "img" / f"probe-{spec.cfg}.jpg") if chrome_pid else {"error": "not running"},
        "actions": actions,
        "complete": bool(got_main and got_late),
    })
    stopped = rt.stop(profile.id)
    result["closed"] = "RuntimeManager.stop -> " + str(stopped)
    result["elapsed_s"] = round(time.time() - started, 1)
    log = store.host_log(profile.id)
    try:
        launching = [ln for ln in log.read_text("utf-8", "replace").splitlines() if " launching " in ln]
        result["host_log_launch_line"] = launching[-1].split(" launching ", 1)[1] if launching else None
    except OSError:
        result["host_log_launch_line"] = None
    return result


def run_pp0(spec: Spec, pp: Any, srv: ProbeServer, store: Any, rt: Any, late: int, run_id: str) -> dict[str, Any]:
    """Host-launched profile with the probe as start URL; no CDP client attaches at any time."""
    name = f"probe-{spec.cfg}-{run_id}"
    url = srv.url("start", spec.cfg, late)
    profile = store.create_profile(name, launch=pp.LaunchOptions(window=spec.window, start_url=url), tags=["audit"])
    started = time.time()
    info = rt.start(profile.id)
    got_main = srv.wait_phase(spec.cfg, "main", 60)
    got_late = srv.wait_phase(spec.cfg, "late", late + 30) if got_main else False
    result: dict[str, Any] = {
        "profile": {"name": name, "id": profile.id, "window": spec.window, "proxy": None, "start_url": "probe /start"},
        "runtime": {"browser_version": info.browser_version, "window": info.window, "relay": bool(info.relay_port)},
        "browser_cmdline": browser_cmdline(info.chrome_pid),
        "window_capture": winshot(info.chrome_pid, AUDIT / "img" / f"probe-{spec.cfg}.jpg"),
        "actions": [], "complete": bool(got_main and got_late),
    }
    result["closed"] = "RuntimeManager.stop -> " + str(rt.stop(profile.id))
    result["elapsed_s"] = round(time.time() - started, 1)
    return result


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the local probe matrix (see module docstring).")
    ap.add_argument("--scratch", required=True, help="scratch directory (tmp data root goes to <scratch>/audit-home/localprobe)")
    ap.add_argument("--configs", default=",".join(s.cfg for s in SPECS))
    ap.add_argument("--late", type=int, default=12, help="seconds the probe keeps watching after the main collection")
    ap.add_argument("--ablate", action="store_true", help="run only the flag ablation (B0 + one ProfilePilot switch each)")
    args = ap.parse_args()
    if args.ablate:
        specs = ablation_specs()
    else:
        wanted = [c.strip().upper() for c in args.configs.split(",") if c.strip()]
        specs = [s for s in SPECS if s.cfg in wanted]

    # The user's default store must be resolved BEFORE PROFILEPILOT_HOME / _SECRETS are forced.
    saved_env = {k: os.environ.pop(k, None) for k in ("PROFILEPILOT_HOME", "PROFILEPILOT_SECRETS")}
    pp = import_profilepilot()
    default_root = pp.data_root()
    base = Path(args.scratch).resolve() / "audit-home" / "localprobe"
    base.mkdir(parents=True, exist_ok=True)
    src_store = pp.Store(default_root, secrets=pp.SecretStore(default_root))  # keyring (auto)
    socks = src_store.proxy_endpoint("test-proxy-socks5")
    for k, v in saved_env.items():
        if v is not None:
            os.environ[k] = v
    os.environ["PROFILEPILOT_HOME"] = str(base)
    os.environ["PROFILEPILOT_SECRETS"] = "file"
    RED.add_secret(socks.host, "PROXY_HOST")
    RED.add_secret(socks.username, "PROXY_USER")
    RED.add_secret(socks.password, "PROXY_PASS")
    RED.add_ip(socks.host, "PROXY_HOST")

    store = pp.Store(base)  # file secrets (env)
    assert store.secrets.backend == "file", store.secrets.backend
    have = {p.name for p in store.list_proxies()}
    if "audit-socks5" not in have:
        store.add_proxy(socks, "audit-socks5", tags=["audit"])
    if "audit-http" not in have:
        store.add_proxy(pp.ProxyEndpoint("http", socks.host, socks.port, socks.username, socks.password), "audit-http", tags=["audit"])
    proxy_host = socks.host
    del socks
    rt = pp.RuntimeManager(store)

    # Real IP (direct) and proxy exit IPs - only ever used for redaction / classification.
    direct = asyncio.run(pp.check_proxy(None, timeout=15))
    RED.add_ip(direct.ip, "REAL_IP6" if direct.ip and ":" in direct.ip else "REAL_IP")
    try:
        import httpx
        with httpx.Client(trust_env=False, timeout=8) as client:
            for u in ("https://api.ipify.org?format=json", "https://api64.ipify.org?format=json"):
                try:
                    ip = client.get(u).json().get("ip")
                    RED.add_ip(ip, "REAL_IP6" if ip and ":" in ip else "REAL_IP")
                except Exception:
                    pass
    except ImportError:
        pass
    exits: dict[str, Any] = {}
    for name in ("audit-socks5", "audit-http"):
        chk = asyncio.run(pp.check_saved_proxy(store, name, timeout=20, save=False))
        RED.add_ip(chk.ip, "PROXY_EXIT_IP6" if chk.ip and ":" in chk.ip else "PROXY_EXIT_IP")
        if chk.ip and chk.ip == proxy_host:  # the exit IP is the proxy host itself: label it as the exit IP
            RED.secrets[proxy_host] = "PROXY_EXIT_IP"
            RED.ips[proxy_host] = "PROXY_EXIT_IP"
        exits[name] = {"ok": chk.ok, "ip": chk.ip, "country_code": chk.country_code, "timezone": chk.timezone,
                       "latency_ms": chk.latency_ms, "error": chk.error}
    if not any(lbl.startswith("REAL_IP") for lbl in RED.ips.values()):
        say("WARNING: could not determine the real IP; refusing to write unredacted results")
        return 2
    say(f"real IP known: yes; proxy checks: {json.dumps(RED.obj(exits))}")

    run_id = time.strftime("%H%M%S")
    srv = ProbeServer().start()
    outputs: dict[str, dict[str, Any]] = {}
    try:
        for spec in specs:
            say(f"=== {spec.cfg}: {spec.description}")
            meta: dict[str, Any]
            try:
                if spec.cfg == "B0":
                    for attempt in range(3):
                        meta = run_plain_or_flags(spec, pp, srv, base, args.late, f"{run_id}-{attempt}")
                        rec0 = srv.store.get("B0")
                        vis = (((rec0.get("phases") or {}).get("main") or {}).get("data") or {}).get("window", {}).get("document", {}).get("visibilityState")
                        if vis != "hidden" or attempt == 2:
                            break
                        say("    B0 window was occluded (visibilityState hidden): kept as B0-occluded, re-running B0")
                        occluded = {k: v.get("data") for k, v in (rec0.get("phases") or {}).items()}
                        outputs["B0-occluded"] = {
                            "cfg": "B0-occluded", "description": spec.description + " (window covered by other windows)",
                            "kind": spec.kind, "window": spec.window, "proxy": None, "run_id": run_id, "meta": meta,
                            "phases": occluded, "extra_switches": [], "nav": spec.nav,
                            "http": {"start": headers_of(rec0.get("requests") or [], "/start"),
                                     "probe": headers_of(rec0.get("requests") or [], "/probe"),
                                     "result_post": headers_of(rec0.get("requests") or [], "/result", "POST")},
                            "requests": [{"method": r["method"], "path": r["path"], "t": round(r["t"], 2)} for r in rec0.get("requests") or []],
                        }
                        classify_webrtc_ips(occluded.get("main") or {})
                        img = AUDIT / "img" / "probe-B0.jpg"
                        if img.exists():
                            img.replace(AUDIT / "img" / "probe-B0-occluded.jpg")
                            cap = (meta.get("window_capture") or {}).get("captured")
                            if cap:
                                cap["file"] = "img/probe-B0-occluded.jpg"
                        srv.store.reset("B0")
                elif spec.kind in ("plain", "flags"):
                    meta = run_plain_or_flags(spec, pp, srv, base, args.late, run_id)
                elif spec.kind == "pp0":
                    meta = run_pp0(spec, pp, srv, store, rt, args.late, run_id)
                else:
                    meta = asyncio.run(run_pp_async(spec, pp, srv, store, rt, args.late, run_id))
            except Exception as exc:
                meta = {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()[-2000:], "complete": False}
            rec = srv.store.get(spec.cfg)
            phases = {k: v.get("data") for k, v in (rec.get("phases") or {}).items()}
            classify_webrtc_ips(phases.get("main") or {})
            for ph in ("main",):
                net = (phases.get(ph) or {}).get("network") or {}
                for key in ("ipify_v4", "ipify_64"):
                    ip = net.get(key)
                    if ip and not str(ip).startswith("error") and ip not in RED.ips:
                        # An address seen through the proxy that the proxy check did not report (rotating exit).
                        label = "PROXY_EXIT_IP" if spec.proxy else "REAL_IP"
                        RED.add_ip(ip, label + ("6" if ":" in ip else ""))
            outputs[spec.cfg] = {
                "cfg": spec.cfg, "description": spec.description, "kind": spec.kind, "window": spec.window,
                "extra_switches": spec.extra, "nav": spec.nav,
                "proxy": spec.proxy, "run_id": run_id, "meta": meta, "phases": phases,
                "http": {"start": headers_of(rec.get("requests") or [], "/start"),
                         "probe": headers_of(rec.get("requests") or [], "/probe"),
                         "result_post": headers_of(rec.get("requests") or [], "/result", "POST")},
                "requests": [{"method": r["method"], "path": r["path"], "t": round(r["t"], 2)} for r in rec.get("requests") or []],
            }
            main_ok = "main" in phases
            say(f"    complete={meta.get('complete')} phases={sorted(phases)} closed={meta.get('closed')} err={meta.get('error')}")
            if not main_ok:
                say("    (no main result)")
    finally:
        srv.stop()
        try:
            stopped = rt.stop_all()
            if stopped:
                say(f"stop_all stopped: {stopped}")
        except Exception as exc:
            say(f"stop_all failed: {exc}")
        n = kill_leftovers(str(base))
        say(f"leftover chrome.exe processes for {base.name}: {n} killed")

    # Redact and write.
    raw_dir = AUDIT / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    context = {"proxy_checks": RED.obj(exits), "redaction_labels": sorted(set(RED.ips.values()) | set(RED.secrets.values()))}
    for cfg, data in outputs.items():
        data["context"] = context
        text = RED.text(json.dumps(data, indent=1, ensure_ascii=False))
        leaks = RED.check(text)
        if leaks:
            say(f"REFUSING to write {cfg}: unredacted values remain ({leaks})")
            continue
        (raw_dir / f"probe-{cfg}.json").write_text(text + "\n", encoding="utf-8")
        say(f"wrote raw/probe-{cfg}.json ({len(text) // 1024} KB)")
    for cfg in outputs:
        img = AUDIT / "img" / f"probe-{cfg}.jpg"
        if img.exists():
            say(f"img/probe-{cfg}.jpg {img.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
