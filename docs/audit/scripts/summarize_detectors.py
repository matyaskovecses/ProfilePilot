"""Extract the headline verdicts of every detector site per configuration (B1, P) from the raw
results written by run_detectors.py, and diff them.

    python docs/audit/scripts/summarize_detectors.py [--out docs/audit] [--write]

``--write`` saves ``<out>/raw/detector-summary.json``. Only already-redacted raw files are read.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

AUDIT = Path(__file__).resolve().parent.parent
CONFIGS = ("B1", "P", "PD", "PR")  # PD / PR = mitigation experiments, only for some sites


def stage_text(rec: dict, stage: str = "iso_final") -> str:
    st = rec.get(stage) or {}
    return (((st.get("main") or {}).get("value")) or {}).get("text") or ""


def stage_value(rec: dict, stage: str = "iso_final") -> dict:
    st = rec.get(stage) or {}
    return ((st.get("main") or {}).get("value")) or {}


def lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def after(text: str, label: str, n: int = 1, exact: bool = False) -> str | None:
    """The line(s) following the first line equal to (or, unless ``exact``, starting with) ``label``."""
    ls = lines(text)
    for i, ln in enumerate(ls):
        if ln == label or (not exact and ln.startswith(label)):
            rest = ln[len(label):].strip(" :")
            if rest:
                return rest
            return " ".join(ls[i + 1:i + 1 + n]) if i + 1 < len(ls) else None
    return None


def json_block(text: str, start_marker: str) -> Any:
    i = text.find(start_marker)
    if i < 0:
        return None
    j = text.find("{", i)
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1])
                except ValueError:
                    return text[j:k + 1][:2000]
    return None


# --------------------------------------------------------------------------- per site


def sannysoft(rec: dict) -> dict:
    v = stage_value(rec)
    bad, ok = [], 0
    for row in v.get("rows") or []:
        cells = row.get("cells") or []
        if len(cells) < 2:
            continue
        cls = " ".join(c.get("c", "") for c in cells)
        if "failed" in cls or "warn" in cls:
            bad.append(f"{cells[0]['t']}={cells[1]['t'][:60]} [{cls.strip()}]")
        elif "passed" in cls:
            ok += 1
    return {"passed": ok, "failed_or_warn": bad}


def rebrowser(rec: dict) -> dict:
    out = {}
    for stage in ("iso_before_read", "iso_after_read", "iso_final"):
        v = stage_value(rec, stage)
        if not v:
            continue
        rows = {}
        for row in (v.get("rows") or [])[1:]:
            cells = row.get("cells") or []
            if len(cells) >= 3:
                name = cells[0]["t"]
                emoji, _, test = name.partition(" ")
                rows[test.strip()] = f"{emoji.strip()} {cells[2]['t'].splitlines()[0][:160]}"
        out[stage] = rows
    return out


BS_BOT_KEYS = ["WebDriver", "WebDriver Advance", "Selenium", "NightmareJS", "PhantomJS", "Awesomium", "Cef",
               "CefSharp", "Coaches", "FMiner", "Born", "Phantomas", "Rhino", "Webdriverio", "Headless Chrome",
               "CDP", "Dev Tool"]


def browserscan_bot(rec: dict) -> dict:
    t = stage_text(rec)
    ls = lines(t)
    res = {}
    for i, ln in enumerate(ls[:-1]):
        if ln in BS_BOT_KEYS and ls[i + 1] not in BS_BOT_KEYS + ["Navigator", "User-Agent", "Webdriver"]:
            res[ln] = ls[i + 1]  # the detailed section comes after the summary header: last one wins
    res["test_results_header"] = after(t, "Test Results:", 6)
    return res


def browserscan(rec: dict) -> dict:
    t = stage_text(rec)
    return {"bot_detection": after(t, "Bot Detection:"), "authenticity": next(
        (ln for ln in lines(t) if ln.startswith("Browser fingerprint authenticity")), None),
        "proxy": after(t, "Proxy:"), "incognito": after(t, "Incognito mode"),
        "timezone_ip": after(t, "Time Zone Based on IP"), "timezone_js": after(t, "Time Zone", exact=True),
        "webrtc": after(t, "WebRTC", exact=True), "webrtc_stun": after(t, "WebRTC STUN", exact=True), "languages": after(t, "Languages"), "bot_detection_2": after(t, "Bot Detection")}


def deviceandbrowserinfo(rec: dict) -> dict:
    t = stage_text(rec)
    blk = json_block(t, "Raw detection details")
    head = next((ln for ln in lines(t) if "human" in ln.lower() or "bot!" in ln.lower()), None)
    if isinstance(blk, dict):
        flagged = [k for k, val in (blk.get("details") or {}).items() if val]
        return {"headline": head, "isBot": blk.get("isBot"), "true_signals": flagged}
    return {"headline": head, "raw": blk}


def incolumitas(rec: dict) -> dict:
    t = stage_text(rec)
    new = json_block(t, "New Detection Tests")
    old = json_block(t, "Old Bot Detection Tests")
    fails = {}
    if isinstance(new, dict):
        fails["new"] = {k: v for k, v in new.items() if v != "OK"}
    if isinstance(old, dict):
        for grp, vals in old.items():
            if isinstance(vals, dict):
                fails[grp] = {k: v for k, v in vals.items() if v != "OK"}
    score = next((ln for ln in lines(t) if ln.startswith("Your Behavioral Score")), None)
    fp = json_block(t, "Fp-collect info")
    extra = {}
    if isinstance(fp, dict):
        extra = {k: fp.get(k) for k in ("historyLength", "webDriver", "webDriverValue", "debugTool", "hasChrome")}
    return {"non_ok": fails, "behavioral": score, "fp_collect": extra}


def fingerprint(rec: dict) -> dict:
    """Smart-signal verdicts from the demo's SERVER API RESPONSE block (redaction breaks strict JSON,
    so scalar ``"key": value`` pairs are read with a regex)."""
    t = stage_text(rec)
    i = t.find("SERVER API RESPONSE")
    srv = t[i:] if i >= 0 else ""
    keys = ("bot", "developer_tools", "tampering", "tampering_confidence", "tampering_ml_score", "suspect_score",
            "incognito", "privacy_settings", "virtual_machine", "virtual_machine_ml_score", "proxy", "vpn",
            "high_activity_device", "rare_device", "anomaly_score", "anti_detect_browser", "visitor_found",
            "location_spoofing", "emulator", "frida")
    out: dict[str, Any] = {}
    for k in keys:
        m = re.search(r'"' + k + r'":\s*("[^"]*"|true|false|null|-?[\d.]+)', srv)
        if m:
            val = m.group(1)
            out[k] = val.strip('"') if val.startswith('"') else json.loads(val)
    agent = json_block(t, "JAVASCRIPT AGENT RESPONSE")
    if isinstance(agent, dict):
        out["agent_suspect_score"] = agent.get("suspect_score")
    return out


def creepjs(rec: dict) -> dict:
    t = stage_text(rec)
    ls = lines(t)
    pick = {}
    for ln in ls:
        low = ln.lower()
        if re.match(r"^\d+% (like headless|headless|stealth)", low) or low.startswith("chromium:"):
            pick[ln.split(":")[0]] = ln
        if "lies" in low and len(ln) < 60:
            pick.setdefault("lies", ln)
        if low.startswith("trust score") or low.startswith("trust"):
            pick.setdefault("trust", ln)
    for key in ("privacy:", "security:", "mode:", "extension:"):
        val = next((ln for ln in ls if ln.startswith(key)), None)
        if val:
            pick[key.rstrip(":")] = val
    pick["viewport_block"] = after(t, "viewport:", 6)
    return pick


def pixelscan(rec: dict) -> dict:
    t = stage_text(rec)
    ls = lines(t)
    keep = [ln for ln in ls if re.search(r"(consistent|masking|automated behavior|proxy detected|inconsistent|"
                                          r"spoof|detected)", ln, re.I) and len(ln) < 120]
    return {"verdict_lines": keep[:20]}


def pixelscan_bot(rec: dict) -> dict:
    t = stage_text(rec)
    ls = lines(t)
    detected = [ls[i - 1] for i, ln in enumerate(ls) if ln == "Detected" and i > 0]
    flagged = any("flagged by our bot detection" in ln for ln in ls)
    groups = {}
    for i, ln in enumerate(ls[:-2]):
        if ln in ("Navigator", "Webdriver", "CDP", "User Agent") and ls[i + 1] in ("Clear", "Detected") and ln not in groups:
            groups[ln] = f"{ls[i + 1]} ({ls[i + 2]})"
    return {"flagged_as_bot": flagged, "groups": groups, "detected_params": detected}


def iphey(rec: dict) -> dict:
    t = stage_text(rec)
    return {"identity": after(t, "Your Digital Identity Looks"), "hardware": after(t, "HARDWARE"),
            "software": after(t, "SOFTWARE"), "location": after(t, "LOCATION"),
            "mx_score": next((ls for ls in [lines(t)[i - 1] for i, ln in enumerate(lines(t)) if ln == "MX SCORE"]), None)}


def whoer(rec: dict) -> dict:
    t = stage_text(rec)
    disguise = next((ln for ln in lines(t) if ln.startswith("Your disguise")), None)
    return {"disguise": disguise, "proxy": after(t, "Proxy:"), "anonymizer": after(t, "Anonymizer:"),
            "blacklist": after(t, "Blacklist:"), "webrtc": after(t, "WEBRTC"), "os": after(t, "OS:"),
            "browser": after(t, "Browser:")}


def browserleaks(rec: dict) -> dict:
    v = stage_value(rec)
    rows = {}
    for row in v.get("rows") or []:
        cells = row.get("cells") or []
        if len(cells) >= 2 and cells[0]["t"]:
            key = cells[0]["t"][:80]
            while key in rows:
                key += "'"
            rows[key] = cells[1]["t"][:300]
    return {"rows": rows}


def antcpt(rec: dict) -> dict:
    t = stage_text(rec)
    return {"score": after(t, "Your score is:"), "suggestion": after(t, "Suggestion:")}


EXTRACTORS: dict[str, Callable[[dict], dict]] = {
    "sannysoft": sannysoft, "rebrowser": rebrowser, "browserscan-bot": browserscan_bot,
    "browserscan": browserscan, "deviceandbrowserinfo": deviceandbrowserinfo, "incolumitas": incolumitas,
    "fingerprint": fingerprint, "creepjs": creepjs, "pixelscan": pixelscan, "pixelscan-bot": pixelscan_bot,
    "iphey": iphey, "whoer": whoer, "browserleaks-javascript": browserleaks,
    "browserleaks-client-hints": browserleaks, "browserleaks-webgl": browserleaks,
    "browserleaks-canvas": browserleaks, "browserleaks-webgpu": browserleaks, "antcpt": antcpt,
}

VOLATILE_ROWS = re.compile(r"(time|date|downlink|rtt|hash|uptime|fps|ms\b|clock|system time|toLocale)", re.I)


def diff(a: Any, b: Any, path: str = "") -> list[dict]:
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in dict.fromkeys(list(a) + list(b)):
            out += diff(a.get(k), b.get(k), f"{path}.{k}" if path else str(k))
    elif a != b:
        out.append({"path": path, "B1": a, "P": b})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(AUDIT))
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    raw = Path(args.out) / "raw"
    summary: dict[str, Any] = {}
    for site, fn in EXTRACTORS.items():
        entry: dict[str, Any] = {}
        for cfg in CONFIGS:
            path = raw / f"detector-{site}-{cfg}.json"
            if not path.exists():
                if cfg in ("B1", "P"):
                    entry[cfg] = {"missing": True}
                continue
            rec = json.loads(path.read_text(encoding="utf-8"))
            try:
                got = fn(rec)
            except Exception as exc:  # keep going; report the parser problem
                got = {"parse_error": f"{type(exc).__name__}: {exc}"}
            got["_title"] = stage_value(rec).get("title")
            got["_error"] = rec.get("error")
            if rec.get("browser_crashed"):
                got["_browser_crashed"] = True
                got["_host_browser_exit"] = rec.get("host_browser_exit")
            if cfg == "P":
                got["_tool_screenshot"] = rec.get("tool_screenshot")
                got["_tool_read_chars"] = (rec.get("tool_read") or {}).get("chars")
                if rec.get("actions"):
                    got["_actions"] = [{k: a.get(k) for k in ("label", "error")} for a in rec["actions"]]
            entry[cfg] = got
        entry["diff"] = [d for d in diff(entry.get("B1"), entry.get("P"))
                         if not d["path"].startswith("_") and not VOLATILE_ROWS.search(d["path"])]
        for extra in ("PD", "PR"):
            if extra in entry:
                entry[f"diff_B1_vs_{extra}"] = [{"path": d["path"], "B1": d["B1"], extra: d["P"]}
                                                for d in diff(entry.get("B1"), entry.get(extra))
                                                if not d["path"].startswith("_") and not VOLATILE_ROWS.search(d["path"])]
        summary[site] = entry
        print(f"\n##### {site}")
        for cfg in CONFIGS:
            if cfg in entry:
                print(f"  {cfg}: {json.dumps(entry[cfg], ensure_ascii=False)[:1500]}")
        for d in entry["diff"][:40]:
            print(f"  DIFF {d['path']}: B1={json.dumps(d['B1'], ensure_ascii=False)[:200]} | P={json.dumps(d['P'], ensure_ascii=False)[:200]}")
        for extra in ("PD", "PR"):
            for d in entry.get(f"diff_B1_vs_{extra}", [])[:20]:
                print(f"  DIFF-{extra} {d['path']}: B1={json.dumps(d['B1'], ensure_ascii=False)[:160]} | {extra}={json.dumps(d[extra], ensure_ascii=False)[:160]}")
    if args.write:
        (raw / "detector-summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n",
                                                   encoding="utf-8")
        print(f"\nwrote {raw / 'detector-summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
