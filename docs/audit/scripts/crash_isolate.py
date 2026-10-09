"""Isolate the Chrome browser-process crash (exit 0xC0000005) seen on iphey.com in configuration P.

Each experiment launches chrome.exe with ProfilePilot's exact switches (window=offscreen, fresh
user-data-dir, no proxy) on about:blank, attaches a RAW CDP client (websockets) and sends a chosen
set of commands, then navigates with Page.navigate and watches the browser for ``--wait`` seconds.

Result (FIX-PLAN step 3, raw/detector-iphey-crash-isolation-step3.json): the crash depends on the
LENGTH OF THE USER-DATA-DIR PATH, not on DevTools. From 176 characters on, Chrome 154 cannot create
``GPUPersistentCache/DawnGraphiteCache/<32 chars>/cache.*`` (Windows path limit) and iphey.com crashes
the browser a few seconds after loading, also in ``startup`` (no client at all); at <= 175 characters
no experiment crashes. Every result records ``udd_len``; ``--udd-len N`` pads the user-data-dir to
exactly N characters (the upstream reproduction: ``--exp startup --udd-len 176``).

  startup    no client at all: the URL is Chrome's command-line start URL (as the host opens
             launch.start_url); the DevTools HTTP endpoint is only polled (the B1 baseline)
  none       attach to the tab only (Target.attachToTarget flatten), Page.navigate
  runtime    + Runtime.enable on the tab
  autoattach + Target.setAutoAttach(autoAttach, waitForDebuggerOnStart=true, flatten) on the tab;
             every auto-attached child gets Runtime.runIfWaitingForDebugger (no Runtime.enable)
  autoattach_rt  as autoattach, and children also get Runtime.enable (what Playwright does for workers)
  autoattach_ev  Target.setAutoAttach(waitForDebuggerOnStart, flatten) on the tab; every child is resumed
             through its OWN auto-attached session (Target.attachedToTarget event ->
             Runtime.runIfWaitingForDebugger): no second attach, no Runtime.enable. The minimal
             reproduction for an upstream report.
  autoattach_filtered  as autoattach_ev, with the Target.setAutoAttach filter FIX-PLAN step 3 option (a)
             considered: dedicated, shared and service workers are not auto-attached (out-of-process
             iframes still are). Not needed in the end: auto-attach is not the cause
  playwright Playwright connect_over_cdp(no_defaults) + page.goto (= configuration P without the tools)
  patchright ProfilePilot's CDP driver (profilepilot.automation.driver: patchright with ProfilePilot's
             driver patches) connect_over_cdp(no_defaults) + page.goto

    python docs/audit/scripts/crash_isolate.py --scratch <scratch> [--url https://iphey.com/] [--exp a,b]
                                               [--label text] [--append] [--out file] [--udd-len N]
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
#: The Target.setAutoAttach filter of FIX-PLAN step 3 option (a): no worker is auto-attached.
NO_WORKERS_FILTER = [{"type": "worker", "exclude": True}, {"type": "shared_worker", "exclude": True},
                     {"type": "service_worker", "exclude": True}, {"type": "browser", "exclude": True},
                     {"type": "tab", "exclude": True}, {}]


class EventCDP(RawCDP):
    """RawCDP that also hands events (messages without an id) to ``on_event``."""

    def __init__(self, ws_url: str, on_event) -> None:
        super().__init__(ws_url)
        self.on_event = on_event

    async def _read(self) -> None:
        try:
            async for msg in self.ws:
                data = json.loads(msg)
                if "id" not in data:
                    self.on_event(data)
                    continue
                fut = self._pending.pop(data["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(data)
        except Exception as exc:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError(str(exc)))


async def experiment(exp: str, url: str, base: Path, wait: float, udd_len: int | None = None) -> dict:
    import httpx

    from profilepilot.browser.flags import build_chrome_args
    from profilepilot.browser.host import free_port
    from profilepilot.browser.prefs import prepare_user_data_dir
    from profilepilot.models import LaunchOptions
    from profilepilot.paths import find_browser

    browser = find_browser()
    udd = base / "crash" / f"{exp}-{time.strftime('%H%M%S')}"
    if udd_len:  # pad the folder name to an exact path length (the crash depends on it)
        pad = udd_len - len(str(udd.resolve())) - 1
        if pad < 1:
            raise SystemExit(f"--udd-len {udd_len} is shorter than {udd}")
        udd = udd.with_name(udd.name + "-" + "x" * pad)
    launch = LaunchOptions(window="offscreen")
    prepare_user_data_dir(udd, launch)
    port = free_port()
    argv = build_chrome_args(browser=browser, user_data_dir=udd, cdp_port=port, launch=launch, relay_port=None,
                             start_urls=[url] if exp == "startup" else [])
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
        if exp == "startup":
            end = time.time() + wait
            while time.time() < end and proc.poll() is None:
                await asyncio.sleep(0.5)
        elif exp in ("playwright", "patchright"):
            if exp == "playwright":
                from playwright.async_api import async_playwright
            else:
                from profilepilot.automation.driver import async_playwright as driver_playwright

                def async_playwright():
                    return driver_playwright("patchright")
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
        elif exp in ("autoattach_ev", "autoattach_filtered"):
            loop = asyncio.get_running_loop()
            holder: dict = {}

            def on_event(msg: dict) -> None:
                if msg.get("method") != "Target.attachedToTarget":
                    return
                info = msg["params"]["targetInfo"]
                events.append(f"{time.time() - t0:.1f}s auto-attached {info['type']}")
                child = msg["params"]["sessionId"]
                task = loop.create_task(holder["cdp"].send("Runtime.runIfWaitingForDebugger", {}, child, timeout=3))
                task.add_done_callback(lambda t: t.cancelled() or t.exception())

            async with EventCDP(ver["webSocketDebuggerUrl"], on_event) as cdp:
                holder["cdp"] = cdp
                targets = (await cdp.send("Target.getTargets"))["targetInfos"]
                tab = next(t for t in targets if t["type"] == "page")
                sid = (await cdp.send("Target.attachToTarget", {"targetId": tab["targetId"], "flatten": True}))["sessionId"]
                params = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
                if exp == "autoattach_filtered":
                    params["filter"] = NO_WORKERS_FILTER
                await cdp.send("Target.setAutoAttach", params, sid)
                await cdp.send("Page.navigate", {"url": url}, sid)
                end = time.time() + wait
                while time.time() < end and proc.poll() is None:
                    await asyncio.sleep(0.3)
                if proc.poll() is None:  # which child targets exist, and which a client is attached to
                    try:
                        infos = (await cdp.send("Target.getTargets", timeout=3))["targetInfos"]
                        res["targets_at_end"] = sorted(
                            f"{i['type']}{' (attached)' if i.get('attached') else ''}"
                            for i in infos if i["type"] not in ("page", "browser", "tab"))
                    except Exception as exc:
                        events.append(f"{time.time() - t0:.1f}s getTargets failed: {type(exc).__name__}")
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
    res.update({"udd_len": len(str(udd)), "crashed": code is not None and code != 0, "exit_code": code,
                "alive_after_s": round(time.time() - t0, 1),
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
    ap.add_argument("--label", default="", help="stored with every experiment of this run")
    ap.add_argument("--out", default=str(HERE.parent / "raw" / "detector-iphey-crash-isolation.json"))
    ap.add_argument("--append", action="store_true", help="add to the experiments already in --out")
    ap.add_argument("--udd-len", type=int, default=None, help="pad every user-data-dir to exactly this length")
    args = ap.parse_args()
    base = Path(args.scratch).resolve() / "audit-home" / "detectors-noproxy"
    out = Path(args.out)
    results = json.loads(out.read_text(encoding="utf-8"))["experiments"] if args.append and out.exists() else []
    for exp in [e.strip() for e in args.exp.split(",") if e.strip()]:
        r = asyncio.run(experiment(exp, args.url, base, args.wait, args.udd_len))
        if args.label:
            r["label"] = args.label
        r["when"] = time.strftime("%Y-%m-%d %H:%M")
        print(json.dumps(r), flush=True)
        results.append(r)
    out.write_text(json.dumps({"url": args.url, "chrome": "154 (ProfilePilot switches, offscreen, no proxy)",
                               "experiments": results}, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
