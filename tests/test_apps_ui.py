"""MCP Apps panel (src/profilepilot/server/apps_ui.py): resource, tool metadata, text fallback,
panel actions, and (chrome-marked) the panel's JavaScript bridge in a real browser with a fake host.

The server is a minimal ``MCPServer`` with the Apps extension and a fake runtime: no browsers are
started (except the throwaway headless Chrome of the chrome-marked test).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import Client
from mcp.client import advertise
from mcp.server import MCPServer
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.types import TextContent

from profilepilot.control import ControlStore
from profilepilot.models import RuntimeInfo
from profilepilot.safety import UrlPolicy
from profilepilot.server.app import AppState
from profilepilot.server.apps_ui import DASHBOARD_HTML, DASHBOARD_URI, META_KEY, build_apps, dashboard_text
from profilepilot.store import Store

SECRET = "Pr0xy-S3cret-pw"
APPS_CLIENT = advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})


class FakeRuntime:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.running: dict[str, RuntimeInfo] = {}
        self.calls: list[tuple[str, str]] = []

    def _info(self, profile_id: str) -> RuntimeInfo:
        profile = self.store.get_profile(profile_id)
        return RuntimeInfo(profile_id=profile.id, profile_name=profile.name, state="running", host_pid=os.getpid(),
                           window="normal")

    def list_running(self) -> list[RuntimeInfo]:
        return list(self.running.values())

    def status(self, ref: str) -> RuntimeInfo | None:
        return self.running.get(self.store.get_profile(ref).id)

    def start(self, ref: str, **kw: Any) -> RuntimeInfo:
        profile = self.store.get_profile(ref)
        self.calls.append(("start", profile.id))
        self.running[profile.id] = self._info(profile.id)
        return self.running[profile.id]

    def stop(self, ref: str, **kw: Any) -> bool:
        profile = self.store.get_profile(ref)
        self.calls.append(("stop", profile.id))
        return self.running.pop(profile.id, None) is not None


class FakeBrowsers:
    def __init__(self) -> None:
        self.disconnected: list[str] = []

    async def disconnect(self, ref: str) -> None:
        self.disconnected.append(ref)

    async def aclose(self) -> None:
        pass


def text_of(result: Any) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "home")


@pytest.fixture
def seeded(store):
    proxy = store.add_proxy(f"socks5://alice:{SECRET}@203.0.113.7:1080", "US-1")
    shop = store.create_profile("shop-us", proxy_id=proxy.id, tags=["shopping"])
    research = store.create_profile("research")
    return {"shop": shop, "research": research, "proxy": proxy}


def make_server(store: Store, runtime: FakeRuntime, **kw: Any) -> tuple[MCPServer, Any]:
    @asynccontextmanager
    async def lifespan(_server):
        yield AppState(store=store, runtime=runtime, browsers=FakeBrowsers(), policy=UrlPolicy())

    apps = build_apps(**kw)
    return MCPServer("pp-apps-test", lifespan=lifespan, extensions=[apps]), apps


# ---------------------------------------------------------------------- resource


def test_dashboard_html_is_self_contained_and_csp_friendly():
    html = DASHBOARD_HTML
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert not re.search(r"https?://", html), "no external URLs"
    assert not re.search(r"\s(src|href|action)\s*=", html), "no external assets"
    assert not re.search(r"<(link|iframe|img|object|embed)\b", html, re.I)
    assert len(re.findall(r"<script\b", html)) == 1 and "<script>" in html  # one inline script, no src
    assert not re.search(r"<[a-z][^>]*\son[a-z]+\s*=", html, re.I), "no inline event handlers"
    assert not re.search(r"\sstyle\s*=", html), "no inline style attributes"
    for forbidden in ("eval(", "new Function", "innerHTML", "outerHTML", "document.write", "insertAdjacentHTML",
                      "import(", "localStorage", "fetch("):
        assert forbidden not in html, forbidden
    for method in ("ui/initialize", "ui/notifications/initialized", "ui/notifications/tool-result", "tools/call",
                   "ui/notifications/size-changed", "ui/resource-teardown", "ui/notifications/host-context-changed",
                   "ui/update-model-context", "ui/message", "ui/request-display-mode"):
        assert f'"{method}"' in html, method
    assert '"2026-01-26"' in html  # MCP Apps protocol version
    assert "event.source !== window.parent" in html  # only the host may talk to the panel


@pytest.mark.asyncio
async def test_resource_and_tool_metadata(store, seeded):
    server, apps = make_server(store, FakeRuntime(store))
    async with Client(server) as client:
        caps = client.server_capabilities
        assert EXTENSION_ID in (caps.extensions or {})
        resources = (await client.list_resources()).resources
        panel = next(r for r in resources if str(r.uri) == DASHBOARD_URI)
        assert panel.mime_type == APP_MIME_TYPE
        read = await client.read_resource(DASHBOARD_URI)
        content = read.contents[0]
        assert content.mime_type == APP_MIME_TYPE and content.text == DASHBOARD_HTML
        assert (content.meta or {}).get("ui", {}).get("prefersBorder") is True

        tools = {t.name: t for t in (await client.list_tools()).tools}
    dash = tools["profiles_dashboard"]
    assert dash.meta["ui"] == {"resourceUri": DASHBOARD_URI, "visibility": ["model", "app"]}
    assert dash.annotations.read_only_hint is True and dash.annotations.destructive_hint is False
    action = tools["dashboard_action"]
    assert action.meta["ui"] == {"visibility": ["app"]}
    assert action.meta["openai/visibility"] == "private"
    for tool in (dash, action):
        assert tool.output_schema is None
        for hint in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"):
            assert isinstance(getattr(tool.annotations, hint), bool), (tool.name, hint)
        for key in ("openai/toolInvocation/invoking", "openai/toolInvocation/invoked"):
            assert 0 < len(tool.meta[key]) <= 64
        assert tool.description and len(tool.description) > 40
    assert set(action.input_schema["properties"]) == {"action", "profile", "request_id", "token"}


# ---------------------------------------------------------------------- tool results


@pytest.mark.asyncio
async def test_text_fallback_and_structured_content(store, seeded):
    runtime = FakeRuntime(store)
    runtime.start(seeded["shop"].id)
    server, apps = make_server(store, runtime)
    async with Client(server) as client:  # no MCP Apps support: text is all the model gets
        result = await client.call_tool("profiles_dashboard", {})
    assert not result.is_error
    text = text_of(result)
    assert text.startswith("ProfilePilot: 2 profile(s), 1 running.")
    assert "- shop-us (id " in text and "running" in text and "proxy US-1" in text
    assert "- research (id " in text and "stopped; no proxy" in text
    assert "panel" not in text
    data = result.structured_content
    assert data["counts"] == {"profiles": 2, "running": 1, "paused": 0, "help": 0}
    assert data["features"] == {"control": True}
    shop = next(p for p in data["profiles"] if p["name"] == "shop-us")
    assert shop["state"] == "running" and shop["proxy"]["name"] == "US-1" and shop["tags"] == ["shopping"]
    assert result.meta[META_KEY]["token"] == apps.action_token
    blob = json.dumps(result.model_dump(mode="json"))
    assert SECRET not in blob and "alice" not in blob  # no proxy credentials anywhere

    async with Client(server, extensions=[APPS_CLIENT]) as client:
        text = text_of(await client.call_tool("profiles_dashboard", {}))
    assert "ProfilePilot panel above" in text


@pytest.mark.asyncio
async def test_panel_actions(store, seeded):
    runtime = FakeRuntime(store)
    server, apps = make_server(store, runtime)
    control = ControlStore(store)
    shop = seeded["shop"]
    async with Client(server, extensions=[APPS_CLIENT]) as client:
        async def act(action: str, token: str | None = None, **kw: Any) -> Any:
            args = {"action": action, "profile": shop.id, "token": apps.action_token if token is None else token, **kw}
            return await client.call_tool("dashboard_action", args)

        # without (or with a wrong) token the model cannot use the panel's tool
        for token in ("", "wrong-token"):
            refused = await act("hand_back", token=token)
            assert refused.is_error and "only works from the buttons" in text_of(refused)
        assert runtime.calls == []

        started = await act("start")
        assert not started.is_error and runtime.calls == [("start", shop.id)]
        assert started.structured_content["message"] == "Started 'shop-us'."
        assert started.meta[META_KEY]["token"] == apps.action_token

        taken = await act("take_control")
        assert control.state_by_id(shop.id).pause is not None
        row = next(p for p in taken.structured_content["profiles"] if p["id"] == shop.id)
        assert row["paused"] is True and row["paused_note"]
        assert "the user has taken control" in text_of(taken)

        back = await act("hand_back")
        assert control.state_by_id(shop.id).pause is None
        assert "Handed 'shop-us' back to the AI." in text_of(back)

        # help requests from the AI show up and can be resolved from the panel
        req = control.request_help(shop.id, "Solve the CAPTCHA", "captcha")
        listed = await client.call_tool("profiles_dashboard", {})
        assert listed.structured_content["help"][0]["message"] == "Solve the CAPTCHA"
        assert listed.structured_content["help"][0]["kind_label"] == "CAPTCHA"
        assert "Waiting for the user in 'shop-us' (CAPTCHA): 'Solve the CAPTCHA'." in text_of(listed)
        done = await act("help_done", request_id=req.id)
        assert not done.is_error and done.structured_content["help"] == []
        assert control.state_by_id(shop.id).effective is None
        missing = await act("help_dismiss", request_id="nope")
        assert missing.is_error and "no longer exists" in text_of(missing)

        stopped = await act("stop")
        assert not stopped.is_error and runtime.calls[-1] == ("stop", shop.id)
        bad = await client.call_tool("dashboard_action", {"action": "explode", "profile": shop.id,
                                                          "token": apps.action_token})
        assert bad.is_error


@pytest.mark.asyncio
async def test_panel_without_control_layer(store, seeded):
    def broken(_store):
        raise ImportError("no control layer")

    server, apps = make_server(store, FakeRuntime(store), control_factory=broken)
    async with Client(server) as client:
        result = await client.call_tool("profiles_dashboard", {})
        assert result.structured_content["features"] == {"control": False}
        refused = await client.call_tool("dashboard_action", {"action": "take_control", "profile": seeded["shop"].id,
                                                              "token": apps.action_token})
        assert refused.is_error and "control layer" in text_of(refused)


def test_dashboard_text_handles_many_profiles():
    rows = [{"id": f"id{i:04d}", "name": f"p{i}", "state": "stopped", "tags": [], "proxy": None} for i in range(60)]
    text = dashboard_text({"counts": {"profiles": 80, "running": 0}, "profiles": rows, "truncated": True,
                           "help": []}, panel=False)
    assert "Showing 60 of 80 profiles" in text
    assert dashboard_text({"counts": {"profiles": 0, "running": 0}, "profiles": [], "help": []},
                          panel=True).count("No profiles yet") == 1


# ---------------------------------------------------------------------- the panel in a real browser

HOST_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Fake MCP Apps host</title></head>
<body style="margin:0;background:%(bg)s"><iframe id="view" sandbox="allow-scripts" style="width:640px;height:520px;border:0"></iframe>
<script>
window.__log = []; window.__calls = [];
const frame = document.getElementById("view");
const send = (msg) => frame.contentWindow.postMessage(msg, "*");
window.__send = send;
window.addEventListener("message", (event) => {
  if (event.source !== frame.contentWindow) return;
  const msg = event.data; window.__log.push(msg);
  if (msg.method === "ui/initialize") {
    send({jsonrpc: "2.0", id: msg.id, result: {protocolVersion: "2026-01-26", hostInfo: {name: "fake", version: "1"},
      hostCapabilities: {serverTools: {}, message: {}, updateModelContext: {}},
      hostContext: {theme: "%(theme)s", displayMode: "inline", availableDisplayModes: ["inline", "fullscreen"]}}});
  } else if (msg.method === "ui/notifications/initialized") {
    send({jsonrpc: "2.0", method: "ui/notifications/tool-input", params: {arguments: {}}});
    send({jsonrpc: "2.0", method: "ui/notifications/tool-result", params: window.__first});
  } else if (msg.method === "tools/call") {
    window.__calls.push(msg.params);
    send({jsonrpc: "2.0", id: msg.id, result: window.__next});
  } else if (msg.id !== undefined && msg.method) {
    send({jsonrpc: "2.0", id: msg.id, result: {}});
  }
});
</script></body></html>"""


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_panel_bridge_in_real_browser(tmp_path, store, seeded):
    """Load the panel in a sandboxed iframe of a fake host page (headless Chrome) and drive the
    MCP Apps protocol: initialize -> tool-result -> render -> a button click -> tools/call."""
    from profilepilot.automation.driver import async_playwright, installed, world_kwargs

    from .chrome_helper import launch_chrome

    runtime = FakeRuntime(store)
    runtime.start(seeded["shop"].id)
    control = ControlStore(store)
    control.request_help(seeded["shop"].id, "Solve the <b>CAPTCHA</b>", "captcha")
    server, apps = make_server(store, runtime)
    async with Client(server, extensions=[APPS_CLIENT]) as client:
        first = (await client.call_tool("profiles_dashboard", {})).model_dump(mode="json", by_alias=True,
                                                                              exclude_none=True)
        after = (await client.call_tool("dashboard_action", {"action": "take_control", "profile": seeded["shop"].id,
                                                             "token": apps.action_token}))
        after = after.model_dump(mode="json", by_alias=True, exclude_none=True)

    shots = os.environ.get("PROFILEPILOT_SHOTS")  # optional: where to save screenshots for a UX review
    with launch_chrome(tmp_path / "udd", "--headless=new") as chrome:
        # Playwright, not patchright: patchright loses sandboxed srcdoc frames of later pages (a test-harness
        # quirk; the panel itself is driver-independent).
        async with async_playwright("playwright" if installed("playwright") else None) as pw:
            browser = await pw.chromium.connect_over_cdp(chrome.http_url, no_defaults=True)
            try:
                errors: list[str] = []
                for theme, bg in (("light", "#ffffff"), ("dark", "#17191e")):
                    page = await browser.contexts[0].new_page()  # a fresh page (and frame) per theme
                    main = world_kwargs(page, "main")  # the fake host's globals live in the main world
                    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    await page.set_content(HOST_PAGE % {"theme": theme, "bg": bg})
                    await page.evaluate("([first, next]) => { window.__first = first; window.__next = next; }",
                                        [first, after], **main)
                    await page.evaluate("(html) => { document.getElementById('view').srcdoc = html; }",
                                        DASHBOARD_HTML)
                    view = page.frame_locator("#view")
                    await view.locator(".row .name", has_text="shop-us").wait_for(timeout=15000)
                    assert await view.locator(".row").count() == 2
                    help_text = await view.locator(".help-card").inner_text()
                    assert "Solve the <b>CAPTCHA</b>" in help_text  # shown as text, never as HTML
                    assert await view.locator(".help-card b").count() == 0
                    summary = await view.locator("#summary").inner_text()
                    assert "2 profiles" in summary and "1 running" in summary
                    if shots:
                        await page.screenshot(path=os.path.join(shots, f"panel-{theme}.png"))
                    await view.get_by_role("button", name="Take control").first.click()
                    await view.locator(".badge.you").wait_for(timeout=10000)
                    calls = await page.evaluate("window.__calls", **main)
                    assert calls[-1]["name"] == "dashboard_action"
                    assert calls[-1]["arguments"]["action"] == "take_control"
                    assert calls[-1]["arguments"]["token"] == apps.action_token
                    log = await page.evaluate("window.__log", **main)
                    methods = [m.get("method") for m in log]
                    assert methods[0] == "ui/initialize" and "ui/notifications/initialized" in methods
                    assert "ui/notifications/size-changed" in methods
                    assert "ui/update-model-context" in methods  # the model learns what the user did
                    init = log[0]["params"]
                    assert init["protocolVersion"] == "2026-01-26" and init["appInfo"]["name"] == "ProfilePilot"
                    if shots:
                        await page.screenshot(path=os.path.join(shots, f"panel-{theme}-taken.png"))
                    await page.close()
                assert errors == [], errors
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_registered_in_create_server(tmp_path):
    """The real server offers the panel (skipped until docs/design/WIRE-IN.md "ChatGPT" is applied)."""
    from profilepilot.server.app import create_server

    home = Store(tmp_path / "real-home")
    server = create_server(store=home)
    async with Client(server, extensions=[APPS_CLIENT]) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        wired = "profiles_dashboard" in tools
        if wired:
            assert EXTENSION_ID in (client.server_capabilities.extensions or {})
            assert DASHBOARD_URI in {str(r.uri) for r in (await client.list_resources()).resources}
            result = await client.call_tool("profiles_dashboard", {})
            assert not result.is_error and "No profiles yet" in text_of(result)
    if not wired:
        pytest.skip("the Apps extension is not registered in create_server yet (docs/design/WIRE-IN.md)")
    assert tools["profiles_dashboard"].meta["ui"]["resourceUri"] == DASHBOARD_URI
    assert tools["dashboard_action"].meta["ui"]["visibility"] == ["app"]
