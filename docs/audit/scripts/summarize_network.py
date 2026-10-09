"""Summarise docs/audit/raw/network-*.json (written by run_network_audit.py) into
raw/network-summary.json: exit IPs vs proxy_test, identity labels that leak through, DNS resolvers,
WebRTC, IPv6, TLS / HTTP2 fingerprints across configurations, timezone / language, http_fetch,
non-proxied sockets and the net-log diagnosis.

Works on the already-redacted files only (labels such as REAL_IP / PROXY_EXIT_IP), so it never
needs the real values. Usage: python docs/audit/scripts/summarize_network.py
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
from pathlib import Path
from typing import Any

AUDIT = Path(__file__).resolve().parent.parent
RAW = AUDIT / "raw"
CONFIGS = ["P", "PX", "PXH", "PXT", "PXN", "PXND", "PXHN", "B0D", "B0DX"]
PROXIED = {"PX", "PXH", "PXT", "PXN", "PXND", "PXHN", "B0DX"}
# Labels that would mean the real network identity reached a site. REAL_CITY is reported separately:
# the proxy exit's city has the same name as the real city (different state), see notes.
LEAK_LABELS = ("REAL_IP", "REAL_IP6", "REAL_ISP", "REAL_HOSTNAME", "REAL_NET", "REAL_REGION", "REAL_ASN",
               "REAL_POSTAL", "REAL_COORD", "LAN_IP", "LAN_IP6")


def load(name: str) -> dict[str, Any] | None:
    p = RAW / f"network-{name}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def grab(pattern: str, text: str, flags: int = 0) -> str | None:
    m = re.search(pattern, text or "", flags)
    return m.group(1).strip() if m else None


def labels_in(obj: Any) -> dict[str, int]:
    text = json.dumps(obj, ensure_ascii=False)
    out = {}
    for lbl in LEAK_LABELS + ("REAL_CITY",):
        n = len(re.findall(r"(?<![A-Z_])" + lbl + r"(?![A-Z0-9_])", text))
        if n:
            out[lbl] = n
    return out


def site(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    return (cfg.get("sites") or {}).get(key) or {}


def peet_summary(j: dict[str, Any] | None) -> dict[str, Any] | None:
    if not j or "tls" not in j:
        return None
    tls = j["tls"]
    exts = sorted(re.sub(r"\(0x[0-9a-f]+\)", "", e.get("name", "")).strip() for e in tls.get("extensions") or []
                  if "GREASE" not in e.get("name", ""))
    ciphers = [c for c in tls.get("ciphers") or [] if "GREASE" not in c]
    h2 = j.get("http2") or {}
    headers = []
    for f in h2.get("sent_frames") or []:
        if f.get("frame_type") == "HEADERS":
            headers = f.get("headers") or []
            break
    if not headers:
        headers = (j.get("http1") or {}).get("headers") or []
    hmap = {h.split(":", 1)[0].lower().strip() if not h.startswith(":") else h.split(": ", 1)[0]: h.split(": ", 1)[-1]
            for h in headers}
    syn = (j.get("tcpip") or {}).get("tcp_syn") or {}
    return {
        "ip": j.get("ip", "").rsplit(":", 1)[0], "http_version": j.get("http_version"), "user_agent": j.get("user_agent"),
        "ja4": tls.get("ja4"), "ja4_r": tls.get("ja4_r"), "ja3_hash": tls.get("ja3_hash"),
        "peetprint_hash": tls.get("peetprint_hash"),
        "sorted_extensions_sha1": hashlib.sha1("|".join(exts).encode()).hexdigest()[:16],
        "ciphers_sha1": hashlib.sha1("|".join(ciphers).encode()).hexdigest()[:16],
        "n_extensions": len(exts), "akamai": h2.get("akamai_fingerprint"), "akamai_hash": h2.get("akamai_fingerprint_hash"),
        "header_order": [h.split(": ", 1)[0] if h.startswith(":") else h.split(":", 1)[0] for h in headers],
        "accept_language": hmap.get("accept-language"), "sec_ch_ua": hmap.get("sec-ch-ua"),
        "tcp_syn": {k: syn.get(k) for k in ("ttl", "window", "mss", "window_scale", "option_order")} if syn else None,
        "p0f": (j.get("tcpip") or {}).get("p0f"),
    }


def bl_tls(text: str) -> dict[str, Any]:
    return {"ja4": grab(r"JA4\s+(t\d\d[a-z]\d{4}\w\d_[0-9a-f]{12}_[0-9a-f]{12})", text),
            "ja4_o": grab(r"JA4_o\s+(\S+)", text), "ja3": grab(r"JA3\s+([0-9a-f]{32})", text),
            "ja3_n": grab(r"JA3_n\s+([0-9a-f]{32})", text), "key_exchange": grab(r"Key Exchange\s+0x[0-9A-F]+\s+(\S+)", text),
            "ech_success": grab(r"ECH Success\s+\S+\s+(\w+)", text)}


def bl_ip(text: str) -> dict[str, Any]:
    keys = {"ip": r"IP Address\t([^\n]+)", "hostname": r"Hostname\t([^\n]+)", "country": r"Country\t([^\n]+)",
            "region": r"State/Region\t([^\n]+)", "city": r"City\t([^\n]+)", "isp": r"ISP\t([^\n]+)",
            "usage_type": r"Usage Type\t([^\n]+)", "timezone": r"Timezone\t([^\n]+)", "ipv6": r"IPv6 Address\t([^\n]+)",
            "webrtc_local": r"Local IP Address\t([^\n]+)", "webrtc_public": r"Public IP Address\t([^\n]+)",
            "tcpip_os": r"\nOS\t([^\n]+)", "mtu": r"MTU\t([^\n]+)", "link_type": r"Link Type\t([^\n]+)",
            "distance": r"Distance\t([^\n]+)", "ja4t": r"JA4T\t([^\n]+)", "ja4": r"\nJA4\t([^\n]+)",
            "akamai_hash": r"Akamai Hash\t([^\n]+)", "accept_language": r"Accept-Language\t([^\n]+)"}
    return {k: grab(v, text) for k, v in keys.items()}


def bl_dns(text: str) -> dict[str, Any]:
    rows = re.findall(r"\n([A-Z0-9_.:]+)\t([^\t\n]+)\t([^\n]+)", text or "")
    rows = [r for r in rows if r[0] not in ("IP Address", "ISP", "Location")]
    return {"result": grab(r"Test Results\t([^\n]+)", text),
            "resolver_isps": dict(collections.Counter(r[1].strip() for r in rows)),
            "resolver_locations": dict(collections.Counter(r[2].strip() for r in rows)), "rows": len(rows)}


def ipleak(text: str) -> dict[str, Any]:
    text = text or ""
    m = re.search(r"(DNS Address(?:es)? - [^\n]+)\n((?:.*\n)*?)If you are now connected to a VPN and between", text)
    entries = []
    if m:
        lines = [ln.strip() for ln in m.group(2).splitlines() if ln.strip()]
        i = 0
        while i + 2 < len(lines):
            entries.append({"ip": lines[i], "location": lines[i + 1], "isp": lines[i + 2]})
            i += 4 if i + 3 < len(lines) and "hit" in lines[i + 3] else 3
    w = re.search(r"WebRTC detection\n((?:(?!If you are now|DNS Address)[^\n]*\n)*)", text)
    webrtc = [ln.strip() for ln in (w.group(1).splitlines() if w else []) if ln.strip()]
    return {"ip": grab(r"Your IP addresses\n([^\n]+)", text), "ip_location": grab(r"Your IP addresses\n[^\n]+\n([^\n]+)", text),
            "ipv6": grab(r"\n(IPv6 test[^\n]*)", text), "family": grab(r"\n(Browser default:[^\n]*)", text),
            "webrtc_detected": webrtc, "dns_summary": m.group(1) if m else None, "dns_servers": entries,
            "accept_language": grab(r"What language you can accept:\t([^\n]+)", text),
            "region": grab(r"Region:\t([^\n]+)", text), "time_zone": grab(r"Time Zone:\t([^\n]+)", text)}


def testipv6(text: str) -> dict[str, Any]:
    return {"ipv4": grab(r"IPv4 address on the public Internet appears to be ([^\n]+)", text),
            "ipv6": grab(r"\n\s*(No IPv6 address detected|Your IPv6 address on the public Internet appears to be [^\n]+)", text),
            "isp": grab(r"Internet Service Provider \(ISP\) appears to be ([^\n]+)", text),
            "dns": grab(r"\n\s*(Your DNS server[^\n]+)", text), "score": grab(r"\n(\d+/10)\s", text)}


def webrtc_page(text: str) -> dict[str, Any]:
    return {"remote_ipv4": grab(r"IPv4 Address\t([^\n]+)", text), "leak_test": grab(r"Your WebRTC IP\nWebRTC Leak Test\s*\n[^\n]*\n([^\n]+)", text),
            "local_ip": grab(r"Local IP Address\t([^\n]+)", text), "public_ip": grab(r"Public IP Address\t([^\n]+)", text)}


def collect(d: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(d, dict):
        return None
    cands = (d.get("webrtc") or {}).get("candidates") or []
    types = collections.Counter(grab(r" typ (\w+)", c) for c in cands)
    addrs = sorted({c.split()[4] for c in cands if len(c.split()) > 4})

    def tz(x: Any) -> Any:
        return {k: x.get(k) for k in ("tz", "offset", "languages")} if isinstance(x, dict) else x

    return {"main": {k: (d.get("main") or {}).get(k) for k in ("tz", "offset", "language", "languages")},
            "cross_site_iframe": tz(d.get("crossSiteFrame")), "dedicated_worker": tz(d.get("dedicatedWorker")),
            "shared_worker": tz(d.get("sharedWorker")), "webrtc_candidates": dict(types), "webrtc_addresses": addrs,
            "fetch_ipify": d.get("fetchIpify"), "fetch_ipify64": d.get("fetchIpify64")}


def main() -> int:
    ptest = json.loads((RAW / "network-proxy-test.json").read_text(encoding="utf-8"))
    pt = {k: {kk: (v.get("result") or {}).get(kk) for kk in ("ok", "ip", "country_code", "region", "city", "isp", "timezone", "provider")}
          for k, v in ptest["before"].items()}
    pt_after = {k: (v.get("result") or {}).get("ip") for k, v in ptest["after"].items()}
    summary: dict[str, Any] = {"proxy_test_before": pt, "proxy_test_after_ip": pt_after, "configs": {}}
    for name in CONFIGS:
        cfg = load(name)
        if not cfg:
            continue
        s: dict[str, Any] = {"description": cfg.get("profile") or cfg.get("argv"), "error": cfg.get("error"),
                             "browser_cmdline": cfg.get("browser_cmdline") or cfg.get("argv")}
        s["identity_labels_found"] = labels_in({k: v for k, v in cfg.items() if k not in ("dns_policy_test",)})
        ipj = (site(cfg, "ipify").get("json") or {})
        s["exit_ip"] = {
            "ipify_navigation": ipj.get("ip"), "peet": (peet_summary(site(cfg, "peet").get("json")) or {}).get("ip"),
            "collector_fetch": (collect(site(cfg, "collect").get("data")) or {}).get("fetch_ipify"),
            "collector_fetch64": (collect(site(cfg, "collect").get("data")) or {}).get("fetch_ipify64"),
            "browserleaks_ip": bl_ip(site(cfg, "bl-ip").get("text", "")).get("ip"),
            "ipleak": ipleak(site(cfg, "ipleak").get("text", "")).get("ip"),
            "test_ipv6": testipv6(site(cfg, "testipv6").get("text", "")).get("ipv4"),
            "stability_browser": [r.get("browser_ip") for r in cfg.get("stability") or []],
            "stability_http_fetch": [r.get("http_fetch_ip") for r in cfg.get("stability") or []],
            "stability_times_s": [r.get("t_s") for r in cfg.get("stability") or []],
            "http_fetch_httpx": ((cfg.get("http_fetch") or {}).get("httpx:ipify") or {}).get("json", {}).get("ip"),
            "http_fetch_scrapling": ((cfg.get("http_fetch") or {}).get("scrapling:ipify") or {}).get("json", {}).get("ip"),
        }
        s["browserleaks_ip"] = bl_ip(site(cfg, "bl-ip").get("text", "")) if site(cfg, "bl-ip") else None
        s["dns_browserleaks"] = bl_dns(site(cfg, "bl-dns").get("text", "")) if site(cfg, "bl-dns") else None
        s["dns_ipleak"] = ipleak(site(cfg, "ipleak").get("text", "")) if site(cfg, "ipleak") else None
        s["webrtc_browserleaks"] = webrtc_page(site(cfg, "bl-webrtc").get("text", "")) if site(cfg, "bl-webrtc") else None
        s["ipv6_test"] = testipv6(site(cfg, "testipv6").get("text", "")) if site(cfg, "testipv6") else None
        s["tls_peet"] = peet_summary(site(cfg, "peet").get("json"))
        s["tls_browserleaks"] = bl_tls(site(cfg, "bl-tls").get("text", "")) if site(cfg, "bl-tls") else None
        s["collector"] = collect(site(cfg, "collect").get("data"))
        if cfg.get("after_disconnect"):
            s["collector_after_cdp_disconnect"] = collect((cfg["after_disconnect"] or {}).get("data"))
        if site(cfg, "collect").get("targets"):
            s["collector_targets"] = site(cfg, "collect").get("targets")
        hf = {}
        for k, v in (cfg.get("http_fetch") or {}).items():
            if k.endswith(":peet"):
                ps = peet_summary(v.get("json")) or {"error": v.get("error")}
                hf[k] = {kk: ps.get(kk) for kk in ("ip", "http_version", "user_agent", "ja4", "akamai_hash", "header_order",
                                                    "accept_language", "sec_ch_ua", "error")}
                hf[k]["route_line"] = (v.get("head") or "").splitlines()[-1] if v.get("head") else None
        s["http_fetch"] = hf or None
        conns = cfg.get("connections") or {}
        s["sockets"] = {"chrome_direct_tcp": conns.get("chrome_direct_tcp"),
                        "chrome_udp_bound_nonloopback": conns.get("chrome_udp_bound_nonloopback"),
                        "host_tcp_peers": sorted((conns.get("host_tcp_peers") or {}).keys()) if conns.get("host_tcp_peers") else None,
                        "samples": conns.get("samples")}
        if cfg.get("netlog"):
            s["netlog"] = cfg["netlog"]
        if cfg.get("dns_policy_test"):
            s["dns_policy_test"] = {"steps": cfg["dns_policy_test"].get("steps"),
                                    "resolved_by_os_resolver": cfg["dns_policy_test"].get("resolved_locally")}
        if cfg.get("relay_stats"):
            s["relay_stats"] = {k: v for k, v in cfg["relay_stats"].items() if k in ("connections_total", "connections_failed", "upstream")}
        summary["configs"][name] = s

    # Cross-configuration comparisons -------------------------------------------------------------
    cmp: dict[str, Any] = {}
    tl = {n: c["tls_peet"] for n, c in summary["configs"].items() if c.get("tls_peet")}
    for key in ("ja4", "ja4_r", "peetprint_hash", "sorted_extensions_sha1", "ciphers_sha1", "akamai_hash", "akamai",
                "http_version", "user_agent", "header_order", "sec_ch_ua", "ja3_hash"):
        vals = {n: (json.dumps(v.get(key)) if isinstance(v.get(key), list) else v.get(key)) for n, v in tl.items()}
        cmp[f"peet.{key}"] = {"identical": len(set(vals.values())) == 1, "values": vals}
    bt = {n: c["tls_browserleaks"] for n, c in summary["configs"].items() if c.get("tls_browserleaks")}
    for key in ("ja4", "ja4_o", "ja3_n", "ja3"):
        vals = {n: v.get(key) for n, v in bt.items()}
        cmp[f"browserleaks_tls.{key}"] = {"identical": len(set(vals.values())) == 1, "values": vals}
    summary["tls_comparison"] = cmp

    exits = {}
    for n, c in summary["configs"].items():
        seen = [v for k, v in c["exit_ip"].items() if isinstance(v, str) and v]
        seen += [x for k in ("stability_browser", "stability_http_fetch") for x in c["exit_ip"][k] if x]
        exits[n] = {"distinct": sorted(set(seen)), "expected": "PROXY_EXIT_IP" if n in PROXIED else "REAL_IP",
                    "all_equal_expected": bool(seen) and set(seen) == {"PROXY_EXIT_IP" if n in PROXIED else "REAL_IP"}}
    summary["exit_ip_check"] = exits
    summary["leak_check"] = {n: {"proxied": n in PROXIED,
                                 "leak_labels": {k: v for k, v in c["identity_labels_found"].items() if k in LEAK_LABELS},
                                 "real_city_name_hits": c["identity_labels_found"].get("REAL_CITY", 0)}
                             for n, c in summary["configs"].items()}
    out = RAW / "network-summary.json"
    out.write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(AUDIT.parent.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
