"""Isolate why browserleaks.com/webgpu reports a different "WebGPU Report Hash" in P/PD than in B1.

  B1     chrome.exe + ProfilePilot switches, the page as start URL, no CDP (as run_detectors B1)
  B1nav  same switches on about:blank, a raw CDP Page.navigate to the page (no Runtime.enable), detach
  B1navfront  as B1nav plus Page.bringToFront 1.5 s after the navigation
  P0     ProfilePilot host launch (RuntimeManager.start) with launch.start_url = the page, no CDP client
  PL     ProfilePilot profile + Playwright, opened by a renderer-initiated navigation
         (page.evaluate: location.href = url) instead of Page.navigate

Every configuration reads the hash afterwards with raw CDP in an isolated world.

    python docs/audit/scripts/webgpu_isolate.py --scratch <scratch> [--configs B1,B1nav,P0,PL]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent.parent / "src"))

URL = "https://browserleaks.com/webgpu"
HASH_JS = r"""
(() => {
  const row = [...document.querySelectorAll('tr')].find(tr => (tr.children[0] || {}).innerText === 'WebGPU Report Hash');
  return {hash: row ? row.children[1].innerText.split('\n')[0] : null, activation: navigator.userActivation.hasBeenActive,
          historyLength: history.length, focus: document.hasFocus(), visibility: document.visibilityState,
          navType: (performance.getEntriesByType('navigation')[0] || {}).type};
})()
"""


async def read_hash(ws_url: str) -> dict:
    from run_detectors import RawCDP
    async with RawCDP(ws_url) as cdp:
        tab = next(t for t in (await cdp.send("Target.getTargets"))["targetInfos"]
                   if t["type"] == "page" and "browserleaks" in t.get("url", ""))
        sid = (await cdp.send("Target.attachToTarget", {"targetId": tab["targetId"], "flatten": True}))["sessionId"]
        tree = (await cdp.send("Page.getFrameTree", {}, sid))["frameTree"]
        world = await cdp.send("Page.createIsolatedWorld", {"frameId": tree["frame"]["id"], "worldName": "pp-audit-h"}, sid)
        res = await cdp.send("Runtime.evaluate", {"expression": HASH_JS, "contextId": world["executionContextId"],
                                                  "returnByValue": True}, sid)
        return (res.get("result") or {}).get("value")


async def plain(cfg: str, base: Path) -> dict:
    import httpx

    from profilepilot.browser.flags import build_chrome_args
    from profilepilot.browser.host import free_port
    from profilepilot.browser.prefs import prepare_user_data_dir
    from profilepilot.models import LaunchOptions
    from profilepilot.paths import find_browser
    from run_detectors import RawCDP, kill_leftovers

    browser = find_browser()
    udd = base / "gpu" / f"{cfg}-{time.strftime('%H%M%S')}"
    launch = LaunchOptions(window="offscreen")
    prepare_user_data_dir(udd, launch)
    port = free_port()
    argv = build_chrome_args(browser=browser, user_data_dir=udd, cdp_port=port, launch=launch, relay_port=None,
                             start_urls=[URL] if cfg == "B1" else [])
    proc = subprocess.Popen([browser.path, *argv], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=0x08000000, close_fds=True)
    try:
        ver = None
        for _ in range(100):
            try:
                ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=1, trust_env=False).json()
                break
            except Exception:
                await asyncio.sleep(0.2)
        if cfg in ("B1nav", "B1navfront"):
            async with RawCDP(ver["webSocketDebuggerUrl"]) as cdp:
                tab = next(t for t in (await cdp.send("Target.getTargets"))["targetInfos"] if t["type"] == "page")
                sid = (await cdp.send("Target.attachToTarget", {"targetId": tab["targetId"], "flatten": True}))["sessionId"]
                await cdp.send("Page.navigate", {"url": URL}, sid)
                if cfg == "B1navfront":  # does giving the tab content the focus remove the difference?
                    await asyncio.sleep(1.5)
                    await cdp.send("Page.bringToFront", {}, sid)
                await cdp.send("Target.detachFromTarget", {"sessionId": sid})
        await asyncio.sleep(14)
        return await read_hash(ver["webSocketDebuggerUrl"])
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        kill_leftovers(str(udd))


async def pp_config(cfg: str, base: Path) -> dict:
    from profilepilot.automation.manager import BrowserManager
    from profilepilot.browser.runtime import RuntimeManager
    from profilepilot.models import LaunchOptions
    from profilepilot.store import Store

    store = Store(base)
    rt = RuntimeManager(store)
    name = f"gpu-{cfg}-{time.strftime('%H%M%S')}"
    launch = LaunchOptions(window="offscreen", start_url=URL if cfg == "P0" else None)
    profile = store.create_profile(name, launch=launch, tags=["audit"])
    try:
        info = rt.start(profile.id)
        if cfg == "PL":
            async with BrowserManager(store, rt) as bm:
                session = await bm.session(name)
                page = session._current([p for p in session.context.pages if not p.is_closed()])
                await page.evaluate("u => { location.href = u; }", URL)
                await asyncio.sleep(1)
        await asyncio.sleep(14)
        info = rt.status(profile.id)
        return await read_hash(info.cdp_ws_url)
    finally:
        rt.stop(profile.id)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--configs", default="B1,B1nav,P0,PL")
    args = ap.parse_args()
    base = Path(args.scratch).resolve() / "audit-home" / "detectors-noproxy"
    os.environ["PROFILEPILOT_HOME"] = str(base)
    os.environ["PROFILEPILOT_SECRETS"] = "file"
    results = {}
    for cfg in [c.strip() for c in args.configs.split(",") if c.strip()]:
        fn = plain if cfg in ("B1", "B1nav", "B1navfront") else pp_config
        try:
            results[cfg] = asyncio.run(fn(cfg, base))
        except Exception as exc:
            results[cfg] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        print(cfg, json.dumps(results[cfg]), flush=True)
    out = HERE.parent / "raw" / "detector-webgpu-isolation.json"
    prev = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"runs": []}
    prev["runs"].append({"when": time.strftime("%Y-%m-%d %H:%M:%S"), "url": URL, "results": results})
    out.write_text(json.dumps(prev, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
