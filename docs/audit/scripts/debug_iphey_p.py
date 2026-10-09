"""Why does the iphey.com tab disappear in configuration P? Drive one ProfilePilot profile like P and
log page/target lifecycle events (close, crash, new pages, browser disconnect) with timestamps.

    python docs/audit/scripts/debug_iphey_p.py --scratch <scratch> [--url https://iphey.com/] [--read]

Uses the same tmp data root as run_detectors.py. Prints only event names, URLs and timings.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent.parent / "src"))


async def main_async(args: argparse.Namespace, base: Path) -> None:
    from profilepilot.automation import content
    from profilepilot.automation.manager import BrowserManager
    from profilepilot.browser.runtime import RuntimeManager
    from profilepilot.models import LaunchOptions
    from profilepilot.safety import UrlPolicy
    from profilepilot.server import tools_browser
    from profilepilot.server.app import AppState
    from profilepilot.store import Store

    store = Store(base)
    rt = RuntimeManager(store)
    name = f"dbg-iphey-{time.strftime('%H%M%S')}"
    profile = store.create_profile(name, launch=LaunchOptions(window="offscreen"), tags=["audit"])
    t0 = time.time()

    def log(msg: str) -> None:
        print(f"[{time.time() - t0:6.1f}s] {msg}", flush=True)

    async with BrowserManager(store, rt) as bm:
        state = AppState(store=store, runtime=rt, browsers=bm, policy=UrlPolicy())
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=state))
        session = await bm.session(name)
        browser_cdp = await session.browser.new_browser_cdp_session()
        await browser_cdp.send("Target.setDiscoverTargets", {"discover": True})
        browser_cdp.on("Target.targetCrashed", lambda e: log(f"Target.targetCrashed {e.get('status')} {e.get('errorCode')}"))
        browser_cdp.on("Target.targetDestroyed", lambda e: log(f"Target.targetDestroyed {e.get('targetId', '')[:8]}"))
        browser_cdp.on("Target.targetCreated", lambda e: log(f"Target.targetCreated {e['targetInfo'].get('type')} {e['targetInfo'].get('url', '')[:80]}"))
        session.browser.on("disconnected", lambda b: log("browser disconnected"))

        def watch(page):
            page.on("close", lambda p: log(f"page close {p.url[:80]}"))
            page.on("crash", lambda p: log(f"page CRASH {p.url[:80]}"))
            page.on("framenavigated", lambda f: log(f"framenavigated main={f == f.page.main_frame} {f.url[:80]}") if f == f.page.main_frame else None)

        for p in session.context.pages:
            watch(p)
        session.context.on("page", lambda p: (log(f"context page {p.url[:80]}"), watch(p)))
        out = await tools_browser.browser_navigate(ctx, name, args.url)
        log("navigate: " + out.splitlines()[-1][:120])
        for _ in range(int(args.wait / 2)):
            await asyncio.sleep(2)
            pages = [p for p in session.context.pages if not p.is_closed()]
            info = rt.status(profile.id)
            log(f"pages={len(pages)} running={info is not None} connected={session.is_connected}")
        if args.read:
            try:
                page = await session.page(None, interactive=False)
                md = await content.read_page(page, fmt="markdown")
                log(f"read_page ok: {len(md)} chars")
            except Exception as exc:
                log(f"read_page failed: {type(exc).__name__}: {str(exc)[:200]}")
            for _ in range(5):
                await asyncio.sleep(2)
                pages = [p for p in session.context.pages if not p.is_closed()]
                log(f"pages={len(pages)} running={rt.status(profile.id) is not None} connected={session.is_connected}")
    log("stop -> " + str(rt.stop(profile.id)))
    host_log = store.host_log(profile.id)
    try:
        tail = host_log.read_text("utf-8", "replace").splitlines()[-8:]
        for ln in tail:
            if " launching " not in ln:
                log("host.log: " + ln[:200])
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", required=True)
    ap.add_argument("--url", default="https://iphey.com/")
    ap.add_argument("--wait", type=float, default=24)
    ap.add_argument("--read", action="store_true")
    args = ap.parse_args()
    base = Path(args.scratch).resolve() / "audit-home" / "detectors-noproxy"
    os.environ["PROFILEPILOT_HOME"] = str(base)
    os.environ["PROFILEPILOT_SECRETS"] = "file"
    asyncio.run(main_async(args, base))
    return 0


if __name__ == "__main__":
    sys.exit(main())
