"""Isolate the Chrome browser-process crash (exit 0xC0000005) seen on iphey.com in configuration P.

Each experiment launches chrome.exe with ProfilePilot's exact switches (window=offscreen, fresh
user-data-dir, no proxy) on about:blank, attaches a RAW CDP client (websockets) and sends a chosen
set of commands, then navigates with Page.navigate and watches the browser for ``--wait`` seconds.

  none       attach to the tab only (Target.attachToTarget flatten), Page.navigate
  runtime    + Runtime.enable on the tab
  autoattach + Target.setAutoAttach(autoAttach, waitForDebuggerOnStart=true, flatten) on the tab;
             every auto-attached child gets Runtime.runIfWaitingForDebugger (no Runtime.enable)
  autoattach_rt  as autoattach, and children also get Runtime.enable (what Playwright does for workers)
  playwright Playwright connect_over_cdp(no_defaults) + page.goto (= configuration P without the tools)

    python docs/audit/scripts/crash_isolate.py --scratch <scratch> [--url https://iphey.com/] [--exp a,b]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent.parent / "src"))

from run_detectors import RawCDP, kill_leftovers  # noqa: E402

CREATE_NO_WINDOW = 0x08000000


async def experiment(exp: str, url: str, base: Path, wait: float) -> dict:
    import httpx

    from profilepilot.browser.flags import build_chrome_args
    from profilepilot.browser.host import free_port
    from profilepilot.browser.prefs import prepare_user_data_dir
    from profilepilot.models import LaunchOptions
    from profilepilot.paths import find_browser

    browser = find_browser()
    udd = base / "crash" / f"{exp}-{time.strftime('%H%M%S')}"
    launch = LaunchOptions(window="offscreen")
    prepare_user_data_dir(udd, launch)
    port = free_port()
    argv = build_chrome_args(browser=browser, user_data_dir=udd, cdp_port=port, launch=launch, relay_port=None,
                             start_urls=[])
    proc = subprocess.Popen([browser.path, *argv], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, close_fds=True)
    events: list[str] = []
    t0 = time.time()
    res: dict = {"exp": exp}
    try:
        ver = None
        for _ in range(100):
            try:
                ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=1, trust_env=False).json()
                break
            except Exception:
                await asyncio.sleep(0.2)
        if exp == "playwright":
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                b = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", no_defaults=True)
                page = b.contexts[0].pages[0]
                await page.goto(url, wait_until="domcontentloaded")
                end = time.time() + wait
                while time.time() < end and proc.poll() is None:
                    await asyncio.sleep(0.5)
                try:
                    await b.close()
                except Exception:
                    pass
        else:
            async with RawCDP(ver["webSocketDebuggerUrl"]) as cdp:
                targets = (await cdp.send("Target.getTargets"))["targetInfos"]
                tab = next(t for t in targets if t["type"] == "page")
                sid = (await cdp.send("Target.attachToTarget", {"targetId": tab["targetId"], "flatten": True}))["sessionId"]
                if exp == "runtime":
                    await cdp.send("Runtime.enable", {}, sid)
                if exp.startswith("autoattach"):
                    # Children attach events arrive as events; poll Target.getTargets and handle new sessions.
                    await cdp.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True,
                                                            "flatten": True}, sid)
                await cdp.send("Page.navigate", {"url": url}, sid)
                seen: set[str] = set()
                end = time.time() + wait
                while time.time() < end and proc.poll() is None:
                    await asyncio.sleep(0.3)
                    if exp.startswith("autoattach"):
                        try:
                            infos = (await cdp.send("Target.getTargets", timeout=3))["targetInfos"]
                        except Exception as exc:
                            events.append(f"{time.time() - t0:.1f}s getTargets failed: {type(exc).__name__}")
                            break
                        for info in infos:
                            if info["type"] in ("worker", "iframe", "service_worker", "shared_worker") and info.get("attached") \
                                    and info["targetId"] not in seen:
                                seen.add(info["targetId"])
                                events.append(f"{time.time() - t0:.1f}s child {info['type']}")
                        # Sessions of auto-attached children are not known here (events are not read), so
                        # resume them by attaching again and running the debugger-wait release.
                        for info in infos:
                            if info["type"] == "worker" and info["targetId"] not in res.setdefault("_resumed", []):
                                res["_resumed"].append(info["targetId"])
                                try:
                                    csid = (await cdp.send("Target.attachToTarget", {"targetId": info["targetId"],
                                                                                     "flatten": True}, timeout=3))["sessionId"]
                                    if exp == "autoattach_rt":
                                        await cdp.send("Runtime.enable", {}, csid, timeout=3)
                                    await cdp.send("Runtime.runIfWaitingForDebugger", {}, csid, timeout=3)
                                    events.append(f"{time.time() - t0:.1f}s resumed worker")
                                except Exception as exc:
                                    events.append(f"{time.time() - t0:.1f}s resume failed: {str(exc)[:80]}")
    except Exception as exc:
        events.append(f"{time.time() - t0:.1f}s error {type(exc).__name__}: {str(exc)[:120]}")
    try:  # a crash in progress: give the process a moment to exit
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass
    code = proc.poll()
    res.update({"crashed": code is not None and code != 0, "exit_code": code, "alive_after_s": round(time.time() - t0, 1),
                "events": events})
    res.pop("_resumed", None)
    if code is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    kill_leftovers(str(udd))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--url", default="https://iphey.com/")
    ap.add_argument("--exp", default="none,runtime,autoattach,autoattach_rt,playwright")
    ap.add_argument("--wait", type=float, default=15)
    args = ap.parse_args()
    base = Path(args.scratch).resolve() / "audit-home" / "detectors-noproxy"
    results = []
    for exp in [e.strip() for e in args.exp.split(",") if e.strip()]:
        r = asyncio.run(experiment(exp, args.url, base, args.wait))
        print(json.dumps(r), flush=True)
        results.append(r)
    out = HERE.parent / "raw" / "detector-iphey-crash-isolation.json"
    out.write_text(json.dumps({"url": args.url, "chrome": "154 (ProfilePilot switches, offscreen, no proxy)",
                               "experiments": results}, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
