"""Compare detector results of two configurations (default B1 vs P) per site.

Reads ``<out>/raw/detector-<site>-<cfg>.json`` written by run_detectors.py and prints, per site:
table rows whose cells differ, and text lines that only one configuration shows (after masking
obviously volatile numbers when ``--mask`` is given). Optionally writes a JSON summary.

    python docs/audit/scripts/diff_detectors.py [--out docs/audit] [--a B1] [--b P] [--sites x,y]
           [--stage iso_final|iso_before_read] [--mask] [--json <file>]
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

AUDIT = Path(__file__).resolve().parent.parent


def load(out: Path, site: str, cfg: str) -> dict | None:
    path = out / "raw" / f"detector-{site}-{cfg}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main_value(rec: dict | None, stage: str) -> dict:
    if not rec:
        return {}
    st = rec.get(stage) or rec.get("iso_final") or {}
    return ((st.get("main") or {}).get("value")) or {}


MASKS = [
    (re.compile(r"\b\d+(\.\d+)?\s*(ms|s|sec|fps|Hz)\b", re.I), "<dur>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"), "<time>"),
]


def mask(line: str) -> str:
    for rx, rep in MASKS:
        line = rx.sub(rep, line)
    return line


def rows_by_key(value: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for i, row in enumerate(value.get("rows") or []):
        cells = [c.get("t", "") + (f" [{c['c']}]" if c.get("c") else "") for c in row.get("cells") or []]
        key = (row.get("cells") or [{}])[0].get("t", "")[:80] or f"#{i}"
        while key in out:
            key += "'"
        out[key] = cells
    return out


def compare(site: str, a: dict | None, b: dict | None, la: str, lb: str, stage: str, do_mask: bool) -> dict:
    va, vb = main_value(a, "iso_final"), main_value(b, stage)
    res: dict = {"site": site, "a_error": (a or {}).get("error"), "b_error": (b or {}).get("error"),
                 "a_title": va.get("title"), "b_title": vb.get("title"), "row_diffs": [], "only_a": [], "only_b": []}
    ra, rb = rows_by_key(va), rows_by_key(vb)
    for key in list(dict.fromkeys(list(ra) + list(rb))):
        if ra.get(key) != rb.get(key):
            res["row_diffs"].append({"row": key, la: ra.get(key), lb: rb.get(key)})
    ta = [ln.strip() for ln in (va.get("text") or "").splitlines() if ln.strip()]
    tb = [ln.strip() for ln in (vb.get("text") or "").splitlines() if ln.strip()]
    if do_mask:
        ta, tb = [mask(x) for x in ta], [mask(x) for x in tb]
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            res["only_a"] += ta[i1:i2]
        if op in ("replace", "insert"):
            res["only_b"] += tb[j1:j2]
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(AUDIT))
    ap.add_argument("--a", default="B1")
    ap.add_argument("--b", default="P")
    ap.add_argument("--sites", default="")
    ap.add_argument("--stage", default="iso_final")
    ap.add_argument("--mask", action="store_true")
    ap.add_argument("--json", default="")
    ap.add_argument("--max", type=int, default=60)
    args = ap.parse_args()
    out = Path(args.out)
    sites = [s for s in args.sites.split(",") if s] or sorted(
        {p.name[len("detector-"):].rsplit("-", 1)[0] for p in (out / "raw").glob("detector-*-*.json")})
    summary = []
    for site in sites:
        a, b = load(out, site, args.a), load(out, site, args.b)
        r = compare(site, a, b, args.a, args.b, args.stage, args.mask)
        summary.append(r)
        print(f"\n##### {site}: {args.a} title={r['a_title']!r} err={r['a_error']} | {args.b} title={r['b_title']!r} err={r['b_error']}")
        for d in r["row_diffs"][: args.max]:
            print(f"  ROW {d['row']!r}\n     {args.a}: {d[args.a]}\n     {args.b}: {d[args.b]}")
        if r["only_a"] or r["only_b"]:
            print(f"  TEXT only {args.a} ({len(r['only_a'])}): {r['only_a'][: args.max]}")
            print(f"  TEXT only {args.b} ({len(r['only_b'])}): {r['only_b'][: args.max]}")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
