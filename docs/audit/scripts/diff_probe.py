"""Diff every probe configuration against the B0 baseline and classify each difference.

Reads ``docs/audit/raw/probe-<CFG>.json`` (written by run_probe_matrix.py, already redacted) and
writes ``docs/audit/raw/probe-diff.json``:

* ``configs.<CFG>.differences`` - every field that differs from B0, with the verdict
  (``same`` / ``differs-expected`` / ``differs-bug`` / ``inconclusive``) and the responsible
  flag or mechanism;
* ``groups`` - one row per check (navigator, client hints, WebGL, CDP detection, ...) with the
  worst verdict per configuration - the table used in the audit report.

Usage::

    python docs/audit/scripts/diff_probe.py            # prints the group table
    python docs/audit/scripts/diff_probe.py --fields   # also prints every differing field
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

RAW = Path(__file__).resolve().parent.parent / "raw"
REFERENCE = "B0"
CONFIGS = ["B1", "B1N", "P0", "P", "P2", "P3", "PL", "PN", "PX", "PXH", "B0-occluded"]
#: Configurations with a Playwright (CDP) client attached while the probe runs.
CDP = {"P", "P2", "P3", "PL", "PN", "PX", "PXH"}
#: Configurations whose tab started on about:blank and was navigated by the client.
ABOUT_BLANK = {"P", "P2", "P3", "PL", "PN", "PX", "PXH"}
PROXIED = {"PX", "PXH"}
SEVERITY = {"same": 0, "inconclusive": 1, "differs-expected": 2, "differs-bug": 3}

MECHANISMS = {
    "cdp_runtime": (
        "Playwright connect_over_cdp (automation/manager.py BrowserManager._attach) sends Runtime.enable "
        "(plus Log/Network/Page.enable) on every page and worker session. V8's inspector then serialises every "
        "console argument: Error.prepareStackTrace runs for console.debug(new Error()), uncaught exceptions / "
        "unhandled rejections get their stacks formatted for Runtime.exceptionThrown, and console.debug(<3000-key "
        "object>) x100 costs ~30 ms instead of ~1 ms. (The classic Error.stack-getter trick of "
        "bot-detector.rebrowser.net no longer fires on Chrome 154 - V8 stopped calling the accessor.)"),
    "main_world": (
        "browser_read (automation/content.py read_page -> page.evaluate(_READ_JS)) and "
        "ProfileSession._ensure_foreground (page.evaluate('document.visibilityState'), run by every tool via "
        "session.page()) execute in the page's MAIN world. Hooked DOM APIs see callers whose stack frames are "
        "Playwright's 'UtilityScript.evaluate' / 'eval at evaluate (<anonymous>)'."),
    "page_navigate_activation": (
        "browser_navigate -> page.goto -> CDP Page.navigate is a browser-initiated navigation with a user gesture: "
        "the document gets sticky user activation (kept across the following same-origin navigations), so "
        "navigator.userActivation.hasBeenActive is true and a new AudioContext starts 'running' without any input "
        "event. PL (renderer-initiated location.href) and P0/B0 (command-line URL) have no activation."),
    "bfcache_flag": (
        "--disable-back-forward-cache (browser/flags.py, added for Playwright aria-ref stability): Back reloads "
        "the page instead of restoring it (pageshow.persisted false, navigation type back_forward). The ablation "
        "shows it is the only ProfilePilot switch with this effect."),
    "about_blank_history": (
        "The profile starts on about:blank (flags.py appends it when there is no start URL) and browser_navigate "
        "reuses that tab, so history.length is one higher - like a person who opened a new tab and typed a URL."),
    "omnibox_focus": (
        "Chrome gives keyboard focus to the omnibox on the initial about:blank tab; a CDP Page.navigate (or a "
        "script navigation) does not move it to the page, so document.hasFocus() stays false for the whole session "
        "even when the window is the foreground window (screenshots show the URL selected in the omnibox). P0 "
        "(probe as start URL, no CDP) and B0 have page focus."),
    "offscreen": (
        "window=offscreen -> --window-position=-32000,-32000 (+ --disable-backgrounding-occluded-windows keeps it "
        "'visible'): screenX/screenY/screenLeft/screenTop = -32000 (the coordinate Windows uses for minimized "
        "windows), the window is outside every screen, and the 8 px invisible resize borders disappear "
        "(innerWidth == outerWidth, innerHeight +8), which also changes the Viewport-Width/Height client hints."),
    "webrtc_policy": (
        "--webrtc-ip-handling-policy=disable_non_proxied_udp (launch.webrtc=auto when proxied): no host and no "
        "srflx candidate is gathered at all, so the real IP cannot leak; a page sees zero ICE candidates, unlike "
        "a default Chrome (mDNS host + srflx)."),
    "proxy_exit": "Traffic goes through the ProfilePilot relay to the upstream proxy: ipify sees the proxy exit IP.",
    "netinfo_noise": "Network Information API / RTT-Downlink-ECT client hints are coarse live estimates that vary run to run.",
    "frame_rate_env": (
        "requestAnimationFrame/timer rates depend on the desktop (multi-monitor, VRR, which window is in front): "
        "plain-Chrome runs measured 141-242 fps; ProfilePilot runs ~138-147 fps. Not attributable to a switch."),
    "storage_noise": "navigator.storage quota follows free disk space.",
    "pl_sec_fetch": (
        "Mitigation experiment only: a renderer-initiated navigation from about:blank is sent with "
        "Sec-Fetch-Site: cross-site instead of 'none' (a typed URL), so it is not a clean replacement for Page.navigate."),
    "hidden_window": (
        "Native behaviour of a covered (occluded) normal Chrome window: visibilityState 'hidden', outerWidth/Height and "
        "screenX/Y read 0, requestAnimationFrame stops. ProfilePilot windows never do this "
        "(--disable-backgrounding-occluded-windows)."),
    "focus_env": "Which window has the keyboard focus depends on the desktop at that moment (user activity, Windows foreground rules).",
    "unknown": "Unclassified difference - investigate.",
}

GROUPS: list[tuple[str, list[str]]] = [
    ("navigator.webdriver", [r"^main\.navigator\.webdriver"]),
    ("User-Agent + navigator.* (platform, languages, hardwareConcurrency, deviceMemory, plugins, mimeTypes, pdfViewerEnabled, DNT, cookieEnabled)",
     [r"^main\.navigator\.(?!connection|userActivation|webdriver)"]),
    ("userAgentData brands + getHighEntropyValues", [r"^main\.uaData\."]),
    ("HTTP request headers (UA, Sec-CH-UA*, Accept-Language, Accept, Sec-Fetch-*)",
     [r"^http\.(probe|start)\.(?!rtt|downlink|ect|viewport-width|sec-ch-viewport|dpr|sec-ch-dpr)"]),
    ("Network Information (navigator.connection, RTT/Downlink/ECT hints)", [r"connection\.", r"^http\.\w+\.(rtt|downlink|ect)$"]),
    ("permissions.query + Notification.permission", [r"^main\.permissions\."]),
    ("screen.* + devicePixelRatio", [r"^main\.window\.screen\.", r"^main\.window\.devicePixelRatio", r"dpr$"]),
    ("window geometry (inner/outer, screenX/Y, viewport hints)",
     [r"^main\.window\.(inner|outer|screen[XYLT]|chrome|visualViewport|windowOnScreen)", r"viewport", r"^capture\.(left|top|width|height)"]),
    ("document.visibilityState / hidden", [r"visibilityState", r"document\.hidden", r"late\.visibility"]),
    ("document.hasFocus()", [r"hasFocus", r"late\.focus"]),
    ("Intl timezone/locale + Date offset", [r"^main\.intl\."]),
    ("matchMedia (color scheme, motion, pointer, hover, ...)", [r"^main\.media\."]),
    ("WebGL / WebGL2 vendor, UNMASKED renderer, params, pixels", [r"^main\.webgl2?\."]),
    ("WebGPU adapter", [r"^main\.webgpu\."]),
    ("Canvas 2D hash", [r"^main\.canvas\."]),
    ("AudioContext sampleRate/baseLatency + OfflineAudio hash", [r"^main\.audio\.(?!state)"]),
    ("Fonts (document.fonts.check + width probe, 41 fonts)", [r"^main\.fonts\."]),
    ("speechSynthesis voices", [r"^main\.speech\."]),
    ("mediaDevices.enumerateDevices", [r"^main\.mediaDevices\."]),
    ("window.chrome object", [r"^main\.chromeObject\."]),
    ("Globals: window props, cdc_/__playwright/__pw/__driver, Error.prepareStackTrace",
     [r"^main\.globals\.", r"^main\.navigator\.navigatorOwnProps", r"webdriverInWindowProps"]),
    ("Dedicated worker navigator/Intl", [r"^main\.worker\."]),
    ("Storage / performance.memory", [r"^main\.storage\.", r"performanceMemory"]),
    ("CDP detection: classic Error.stack getter (rebrowser runtimeEnableLeak)", [r"stackGetter"]),
    ("CDP detection: Error.prepareStackTrace via console.debug / exceptionThrown",
     [r"prepareStackTrace(?!Count)$", r"\.prepareStackTrace$", r"cdpAsync\.", r"prepareStackTraceDetected", r"cdpDetectedAny"]),
    ("CDP detection: console.debug(big object) timing", [r"consoleTiming"]),
    ("Main-world execution traps (DOM getters called by non-page code)", [r"externalCalls"]),
    ("User activation without input (userActivation.hasBeenActive, AudioContext autoplay)",
     [r"userActivation", r"^main\.audio\.state"]),
    ("Back/forward cache (pageshow.persisted after Back)", [r"^bfcache\.(persisted|navType|restored)", r"notRestoredReasons"]),
    ("history.length", [r"historyLength"]),
    ("requestAnimationFrame / timer throttling", [r"^main\.timing\.(raf|nested|setTimeout)", r"errors\.timing"]),
    ("WebRTC ICE candidates (STUN)", [r"^main\.webrtc\."]),
    ("Exit IP seen by ipify (v4 + v64)", [r"^main\.network\."]),
    ("Timezone vs proxy exit geo", [r"^derived\.timezoneVsExit"]),
    ("Browser-UI surfaces (infobars) via window capture / innerHeight", [r"^derived\.infobar"]),
]

PORT = re.compile(r"127\.0\.0\.1:\d+")
EXCLUDE = re.compile(
    r"(^main\.cfg$|collectMs|\.t_ms$|received_at|windowProps$|candidates|samples|speech\.names|"
    r"^main\.early\.t_ms|trapsInstalled|^main\.window\.document\.referrer|^http\.start\.referer|^http\.probe\.referer|"
    r"documentOwnProps|navigatorOwnProps|^main\.errors\.(?!timing))")


def flat(x: Any, pre: str = "", out: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {} if out is None else out
    if isinstance(x, dict):
        for k, v in x.items():
            flat(v, f"{pre}.{k}" if pre else str(k), out)
    elif isinstance(x, list) and all(not isinstance(i, (dict, list)) for i in x):
        out[pre] = json.dumps(x, ensure_ascii=False)
    elif isinstance(x, list):
        out[pre] = json.dumps(x, ensure_ascii=False, sort_keys=True)
    else:
        out[pre] = x
    return out


def addr_class(addr: str | None) -> str:
    if not addr:
        return "none"
    if addr.endswith(".local"):
        return "mdns"
    if addr in ("0.0.0.0", "::"):
        return "unspecified"
    return addr  # already a redaction label (REAL_IP, PROXY_EXIT_IP, LAN_IP, ...)


def view(d: dict[str, Any]) -> dict[str, Any]:
    ph = d.get("phases") or {}
    main = ph.get("main") or {}
    late = ph.get("late") or {}
    v: dict[str, Any] = {}
    skip_headers = {"host", "connection", "content-length", "referer"}
    for which in ("probe", "start"):
        for k, val in ((d.get("http") or {}).get(which) or {}).items():
            if k not in skip_headers:
                v[f"http.{which}.{k}"] = val
    for k, val in flat(ph.get("bfcache") or {}).items():
        v[f"bfcache.{k}"] = val
    m = json.loads(json.dumps(main))
    rtc = m.get("webrtc") or {}
    rtc["signature"] = json.dumps(sorted(f"{c.get('type')}/{c.get('protocol')}/{addr_class(c.get('address'))}"
                                         for c in rtc.get("candidates") or []))
    if isinstance(m.get("speech"), dict):
        m["speech"]["namesHash"] = hashlib.sha256(json.dumps(m["speech"].get("names")).encode()).hexdigest()[:12]
    for key in ("early", "cdpAfterCollect"):
        if isinstance(m.get(key), dict) and "consoleTimingMs" in m[key]:
            m[key]["consoleTimingMs"] = bucket_ms(m[key]["consoleTimingMs"])
    for k, val in flat(m).items():
        v[f"main.{k}"] = val
    samples = late.get("samples") or []
    v["late.prepareStackTraceDetected"] = bool(late.get("prepareStackTraceCount"))
    v["late.stackGetterAny"] = late.get("stackGetterAny")
    v["late.cdpDetectedAny"] = late.get("cdpDetectedAny")
    v["late.consoleTimingMsMedian"] = bucket_ms(late.get("consoleTimingMsMedian"))
    v["late.externalCalls.count"] = (late.get("externalCalls") or {}).get("count")
    v["late.externalCalls.by"] = json.dumps(sorted(((late.get("externalCalls") or {}).get("by") or {}).keys()))
    v["late.focusAll"] = "".join("1" if s.get("hasFocus") else "0" for s in samples) or None
    v["late.visibilityAll"] = json.dumps(sorted({s.get("visibilityState") for s in samples}))
    win = ((d.get("meta") or {}).get("window_capture") or {}).get("windows") or []
    if win:
        for k in ("left", "top", "width", "height", "minimized"):
            v[f"capture.{k}"] = win[0].get(k)
    # Infobar check: an infobar ("controlled by automated test software", "unsupported command-line flag")
    # pushes the page down by ~40 px, i.e. a larger outerHeight - innerHeight for the same window mode.
    w = main.get("window") or {}
    v["derived.infobar.chromeHeightMinusFrame"] = (w.get("chromeHeight") or 0) - (8 if w.get("chromeWidth") else 0) if w.get("outerHeight") else None
    tz = (main.get("intl") or {}).get("timeZone")
    exit_tz = None
    if d.get("proxy"):
        exit_tz = ((d.get("context") or {}).get("proxy_checks") or {}).get(d["proxy"], {}).get("timezone")
    v["derived.timezoneVsExit"] = None if not exit_tz else ("match" if exit_tz == tz else f"mismatch (page {tz}, exit {exit_tz})")
    out = {}
    for k, val in v.items():
        if EXCLUDE.search(k):
            continue
        if isinstance(val, str):
            val = PORT.sub("127.0.0.1:PORT", val)
            val = re.sub(r"cfg=[A-Za-z0-9-]+", "cfg=X", val)
        out[k] = val
    return out


def bucket_ms(ms: Any) -> Any:
    if not isinstance(ms, (int, float)):
        return ms
    return "<5ms (no inspector)" if ms < 5 else ">=5ms (inspector previews)"


def window_props_diff(ref: dict[str, Any], other: dict[str, Any]) -> tuple[list[str], list[str]]:
    a = set(((ref.get("phases") or {}).get("main") or {}).get("globals", {}).get("windowProps") or [])
    b = set(((other.get("phases") or {}).get("main") or {}).get("globals", {}).get("windowProps") or [])
    return sorted(b - a), sorted(a - b)


def classify(cfg: str, spec: dict[str, Any], path: str, ref: Any, val: Any) -> tuple[str, str]:
    offscreen = spec.get("window") == "offscreen"
    if cfg == "B0-occluded":
        if re.search(r"focus|foreground", path, re.I):
            return "inconclusive", "focus_env"
        return "differs-expected", "hidden_window"
    if re.search(r"stackGetter", path):
        return ("differs-bug", "cdp_runtime") if cfg in CDP else ("inconclusive", "unknown")
    if re.search(r"prepareStackTrace|cdpAsync|cdpDetectedAny|consoleTiming", path):
        return ("differs-bug", "cdp_runtime") if cfg in CDP else ("differs-bug", "unknown")
    if path.startswith("late.externalCalls"):
        return "differs-bug", "main_world"
    if re.search(r"userActivation\.hasBeenActive|^main\.audio\.state", path):
        return ("differs-bug", "page_navigate_activation") if cfg in CDP else ("differs-bug", "unknown")
    if path.startswith("bfcache.") or path == "main.window.navigation.type":
        if path == "bfcache.historyLength":
            return "differs-expected", "about_blank_history"
        return "differs-bug", "bfcache_flag"
    if "historyLength" in path:
        return "differs-expected", "about_blank_history"
    if re.search(r"hasFocus|focusAll", path):
        return ("differs-bug", "omnibox_focus") if cfg in ABOUT_BLANK else ("inconclusive", "focus_env")
    if path == "capture.foreground":
        return "inconclusive", "focus_env"
    if re.search(r"screen[XYLT]|windowOnScreen|innerWidth|innerHeight|chromeWidth|chromeHeight|visualViewport|"
                 r"viewport-width|viewport-height|^capture\.(left|top)|derived\.infobar", path):
        return ("differs-expected", "offscreen") if offscreen else ("differs-bug", "unknown")
    if path.startswith("main.webrtc."):
        return ("differs-expected", "webrtc_policy") if cfg in PROXIED else ("inconclusive", "unknown")
    if path.startswith("main.network."):
        return ("differs-expected", "proxy_exit") if cfg in PROXIED else ("differs-bug", "unknown")
    if path.startswith("derived.timezoneVsExit"):
        return "differs-expected", "proxy_exit"
    if re.search(r"connection\.(rtt|downlink|effectiveType)|^http\.\w+\.(rtt|downlink|ect)$", path):
        return "differs-expected", "netinfo_noise"
    if re.search(r"^main\.timing\.(raf|nested|setTimeout)|errors\.timing", path):
        return "inconclusive", "frame_rate_env"
    if path.startswith("main.storage.quotaGB"):
        return "differs-expected", "storage_noise"
    if path == "http.start.sec-fetch-site" and cfg == "PL":
        return "differs-bug", "pl_sec_fetch"
    return "differs-bug", "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fields", action="store_true", help="print every differing field")
    args = ap.parse_args()
    raw = {p.stem[len("probe-"):]: json.loads(p.read_text("utf-8")) for p in RAW.glob("probe-*.json")
           if p.stem != "probe-diff"}
    ref_doc = raw[REFERENCE]
    ref = view(ref_doc)
    result: dict[str, Any] = {"reference": REFERENCE, "reference_description": ref_doc.get("description"),
                              "mechanisms": MECHANISMS, "configs": {}, "groups": []}
    per_cfg_diffs: dict[str, list[dict[str, Any]]] = {}
    for cfg in [c for c in CONFIGS if c in raw]:
        doc = raw[cfg]
        cur = view(doc)
        diffs = []
        for path in sorted(set(ref) | set(cur)):
            a, b = ref.get(path), cur.get(path)
            if a == b:
                continue
            verdict, mech = classify(cfg, doc, path, a, b)
            diffs.append({"path": path, "B0": a, cfg: b, "verdict": verdict, "mechanism": mech})
        added, removed = window_props_diff(ref_doc, doc)
        if added or removed:
            diffs.append({"path": "main.globals.windowProps(set)", "B0": f"-{removed}", cfg: f"+{added}",
                          "verdict": "differs-bug", "mechanism": "unknown"})
        per_cfg_diffs[cfg] = diffs
        counts: dict[str, int] = {}
        for d in diffs:
            counts[d["verdict"]] = counts.get(d["verdict"], 0) + 1
        result["configs"][cfg] = {"description": doc.get("description"), "window": doc.get("window"),
                                  "proxy": doc.get("proxy"), "fields_compared": len(set(ref) | set(cur)),
                                  "counts": counts, "differences": diffs}
    # Group table.
    for title, patterns in GROUPS:
        rx = [re.compile(p) for p in patterns]
        row: dict[str, Any] = {"check": title, "fields": sorted(k for k in ref if any(r.search(k) for r in rx))[:60]}
        for cfg, diffs in per_cfg_diffs.items():
            hits = [d for d in diffs if any(r.search(d["path"]) for r in rx)]
            if not hits:
                row[cfg] = {"verdict": "same"}
                continue
            worst = max(hits, key=lambda d: SEVERITY[d["verdict"]])
            row[cfg] = {"verdict": worst["verdict"], "mechanism": worst["mechanism"],
                        "examples": [{"path": h["path"], "B0": h["B0"], "value": h[cfg]} for h in hits[:4]]}
        result["groups"].append(row)
    (RAW / "probe-diff.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    cfgs = list(per_cfg_diffs)
    short = {"same": "=", "differs-expected": "exp", "differs-bug": "BUG", "inconclusive": "?"}
    print(f"{'check':78} " + " ".join(f"{c:>5}" for c in cfgs))
    for row in result["groups"]:
        print(f"{row['check'][:78]:78} " + " ".join(f"{short[row[c]['verdict']]:>5}" for c in cfgs))
    if args.fields:
        for cfg, diffs in per_cfg_diffs.items():
            print(f"\n== {cfg}")
            for d in diffs:
                print(f"  [{d['verdict']:16}] {d['path']}: {str(d['B0'])[:60]} -> {str(d[cfg])[:60]}  ({d['mechanism']})")
    unknown = [(c, d["path"]) for c, ds in per_cfg_diffs.items() for d in ds if d["mechanism"] == "unknown"]
    if unknown:
        print("\nUNCLASSIFIED:", unknown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
