"""Why does the WebGPU report hash (browserleaks.com/webgpu, browserscan "WebGPU Report") differ
between B1 and P? Dump the WebGPU adapter (info, features, limits, WGSL features) on a neutral
page in both configurations, reading in an isolated world (no main-world script, no Runtime.enable
from the reader), and diff.

    python docs/audit/scripts/webgpu_probe.py --scratch <scratch> [--url https://example.com/]

Writes docs/audit/raw/detector-webgpu-probe.json.
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
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent.parent / "src"))

DUMP_JS = r"""
(async () => {
  const out = {secure: isSecureContext, hasGpu: !!navigator.gpu};
  if (!navigator.gpu) return out;
  const dump = async (opts) => {
    const a = await navigator.gpu.requestAdapter(opts);
    if (!a) return null;
    const info = a.info || {};
    const limits = {};
    for (const k in a.limits) limits[k] = a.limits[k];
    return {info: {vendor: info.vendor, architecture: info.architecture, device: info.device,
                   description: info.description, subgroupMinSize: info.subgroupMinSize,
                   subgroupMaxSize: info.subgroupMaxSize, isFallbackAdapter: info.isFallbackAdapter},
            features: [...a.features].sort(), limits};
  };
  out.default = await dump();
  out.highPerf = await dump({powerPreference: 'high-performance'});
  out.lowPower = await dump({powerPreference: 'low-power'});
  out.wgsl = navigator.gpu.wgslLanguageFeatures ? [...navigator.gpu.wgslLanguageFeatures].sort() : null;
  out.preferredFormat = navigator.gpu.getPreferredCanvasFormat();
  return out;
})()
"""


async def iso_eval(send, expression: str):
    tree = (await send("Page.getFrameTree", {}))["frameTree"]
    world = await send("Page.createIsolatedWorld", {"frameId": tree["frame"]["id"], "worldName": "pp-audit-gpu"})
    res = await send("Runtime.evaluate", {"expression": expression, "contextId": world["executionContextId"],
                                          "returnByValue": True, "awaitPromise": True})
    if res.get("exceptionDetails"):
        return {"error": str(res["exceptionDetails"])[:300]}
    return (res.get("result") or {}).get("value")


async def run_b1(url: str, base: Path) -> dict:
    import httpx

    from profilepilot.browser.flags import build_chrome_args
    from profilepilot.browser.host import free_port
    from profilepilot.browser.prefs import prepare_user_data_dir
    from profilepilot.models import LaunchOptions
    from profilepilot.paths import find_browser
    from run_detectors import RawCDP, kill_leftovers

    browser = find_browser()
    udd = base / "gpu" / f"B1-{time.strftime('%H%M%S')}"
    launch = LaunchOptions(window="offscreen")
    prepare_user_data_dir(udd, launch)
    port = free_port()
    argv = build_chrome_args(browser=browser, user_data_dir=udd, cdp_port=port, launch=launch, relay_port=None,
                             start_urls=[url])
    proc = subprocess.Popen([browser.path, *argv], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=0x08000000, close_fds=True)
    try:
        await asyncio.sleep(6)
        ver = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=3, trust_env=False).json()
        async with RawCDP(ver["webSocketDebuggerUrl"]) as cdp:
            tab = next(t for t in (await cdp.send("Target.getTargets"))["targetInfos"] if t["type"] == "page")
            sid = (await cdp.send("Target.attachToTarget", {"targetId": tab["targetId"], "flatten": True}))["sessionId"]
            return await iso_eval(lambda m, p: cdp.send(m, p, sid), DUMP_JS)
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        kill_leftovers(str(udd))


async def run_p(url: str, base: Path) -> dict:
    from profilepilot.automation.manager import BrowserManager
    from profilepilot.browser.runtime import RuntimeManager
    from profilepilot.models import LaunchOptions
    from profilepilot.safety import UrlPolicy
    from profilepilot.server import tools_browser
    from profilepilot.server.app import AppState
    from profilepilot.store import Store

    store = Store(base)
    rt = RuntimeManager(store)
    name = f"gpu-P-{time.strftime('%H%M%S')}"
    profile = store.create_profile(name, launch=LaunchOptions(window="offscreen"), tags=["audit"])
    try:
        async with BrowserManager(store, rt) as bm:
            state = AppState(store=store, runtime=rt, browsers=bm, policy=UrlPolicy())
            ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
            await tools_browser.browser_navigate(ctx, name, url)
            await asyncio.sleep(3)
            session = await bm.session(name)
            page = session._current([p for p in session.context.pages if not p.is_closed()])
            cdp = await session.context.new_cdp_session(page)
            return await iso_eval(lambda m, p: cdp.send(m, p), DUMP_JS)
    finally:
        rt.stop(profile.id)


def diff(a, b, path=""):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in dict.fromkeys(list(a) + list(b)):
            out += diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)
    elif isinstance(a, list) and isinstance(b, list) and a != b:
        out.append({"path": path, "only_B1": sorted(set(a) - set(b)), "only_P": sorted(set(b) - set(a))})
    elif a != b:
        out.append({"path": path, "B1": a, "P": b})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--url", default="https://example.com/")
    args = ap.parse_args()
    base = Path(args.scratch).resolve() / "audit-home" / "detectors-noproxy"
    os.environ["PROFILEPILOT_HOME"] = str(base)
    os.environ["PROFILEPILOT_SECRETS"] = "file"
    b1 = asyncio.run(run_b1(args.url, base))
    p = asyncio.run(run_p(args.url, base))
    d = diff(b1, p)
    print(json.dumps(d, indent=1))
    out = HERE.parent / "raw" / "detector-webgpu-probe.json"
    out.write_text(json.dumps({"url": args.url, "B1": b1, "P": p, "diff": d}, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
