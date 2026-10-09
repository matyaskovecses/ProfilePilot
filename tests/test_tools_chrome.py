"""Browser tools against the real Chrome (off-screen profiles in a temporary data root; every process
started here is stopped again). Regression tests for selector, wait, scroll, screenshot, read,
extract, popup, download and remote-mode behaviour."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import struct
import time
from pathlib import Path
from typing import Any, Iterator

import psutil
import pytest
from mcp import Client
from mcp.types import ImageContent, TextContent

from profilepilot.server.app import create_server
from profilepilot.store import Store

from .fakes import OriginServer

pytestmark = [pytest.mark.chrome, pytest.mark.asyncio]

MAIN = """<!doctype html><html><head><title>Tools page</title></head><body>
<nav style="display:none"><a href="/next">Sign in</a><span>Results ready</span></nav>
<header><a id="signin" href="/next">Sign in</a></header>
<p>Results ready</p>
<input id="q" aria-label="Search">
<select id="s"><option value="a">Alpha</option><option value="b">Beta</option></select>
<ul><li class="item">One</li><li class="item" style="display:none">HIDDEN_ITEM</li><li class="item">Two</li></ul>
<product-card></product-card>
<iframe srcdoc="<p class='inframe'>FRAME_TEXT</p>"></iframe>
<div style="height:6000px">tall</div>
<section style="opacity:0">REVEAL_ME later</section>
<script>customElements.define('product-card', class extends HTMLElement { constructor() { super();
  this.attachShadow({mode: 'open'}).innerHTML = '<h2 class="title">Shadow Title</h2><span class="price">$5</span>'; } });
</script></body></html>"""
NEXT = "<!doctype html><html><head><title>Next page</title></head><body><h1>Next</h1></body></html>"
PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
       b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
FILES = {
    "/file.zip": ("application/zip", b"PK\x03\x04zipdata", {"Content-Disposition": 'attachment; filename="data.zip"'}),
    "/doc.pdf": ("application/pdf", PDF, {}),
}


def text_of(result: Any) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


async def call(client: Client, name: str, args: dict[str, Any], *, ok: bool = True) -> str:
    result = await client.call_tool(name, args)
    out = text_of(result)
    assert result.is_error is (not ok), f"{name}: {out}"
    return out


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) from a JPEG's SOF marker."""
    i = 2
    while i < len(data):
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + length
    raise AssertionError("no SOF marker")


def _kill_leftovers(marker: Path) -> None:
    from profilepilot.browser.runtime import kill_tree

    needle = str(marker).lower()
    me = psutil.Process().pid
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
        except psutil.Error:
            continue
        if needle in cmdline and proc.pid != me:
            kill_tree(proc.pid)


@pytest.fixture
def home(tmp_path) -> Iterator[Store]:
    from tests.chrome_helper import find_test_browser

    find_test_browser()
    store = Store(tmp_path / "home")
    config = store.load_config()
    config.default_window = "offscreen"
    store.save_config(config)
    try:
        yield store
    finally:
        from profilepilot.browser.runtime import RuntimeManager

        with contextlib.suppress(Exception):
            RuntimeManager(store).stop_all(timeout=15)
        _kill_leftovers(tmp_path)


@pytest.fixture
def origin() -> Iterator[OriginServer]:
    with OriginServer({"/": MAIN, "/next": NEXT}, FILES) as server:
        yield server


async def test_selectors_skip_hidden_duplicates_and_fail_fast(home, origin, monkeypatch):
    from profilepilot.server import tools_browser

    monkeypatch.setattr(tools_browser, "ACTION_TIMEOUT_MS", 3_000)
    home.create_profile("p")
    async with Client(create_server(store=home)) as client:
        await call(client, "browser_navigate", {"profile": "p", "url": origin.url + "/"})
        started = time.monotonic()
        assert "is visible" in await call(client, "browser_wait_for", {"profile": "p", "text": "Results ready"})
        assert "is visible" in await call(client, "browser_wait_for", {"profile": "p", "selector": "a"})
        assert time.monotonic() - started < 5  # the hidden copy in the mobile nav comes first in the DOM
        out = await call(client, "browser_wait_for", {"profile": "p", "text": "Sign in", "gone": True, "timeout_s": 1},
                         ok=False)
        assert "Timed out after 1 s: text 'Sign in' is still visible" in out and "try browser_wait_for" not in out

        started = time.monotonic()
        out = await call(client, "browser_click", {"profile": "p", "selector": "#nope"}, ok=False)
        assert "No element matches the selector '#nope'" in out and time.monotonic() - started < 6

        out = await call(client, "browser_select_option", {"profile": "p", "selector": "#s", "values": ["Gamma"]},
                         ok=False)
        assert "No option 'Gamma'" in out and "Alpha (a)" in out and "Beta (b)" in out
        out = await call(client, "browser_select_option", {"profile": "p", "selector": "#s", "values": ["Beta"]})
        assert "Selected ['b']" in out

        out = await call(client, "browser_click", {"profile": "p", "selector": "text=Sign in"})
        assert "Next page" in out


async def test_scroll_ends_with_a_focused_input_and_capped_screenshots(home, origin):
    home.create_profile("p")
    async with Client(create_server(store=home)) as client:
        await call(client, "browser_navigate", {"profile": "p", "url": origin.url + "/"})
        await call(client, "browser_type", {"profile": "p", "selector": "#q", "text": "usb hub"})
        out = await call(client, "browser_scroll", {"profile": "p", "direction": "bottom"})
        assert "Position x=0, y=0" not in out and "did not move" not in out, out
        out = await call(client, "browser_scroll", {"profile": "p", "direction": "top"})
        assert "y=0" in out
        out = await call(client, "browser_scroll", {"profile": "p", "direction": "top"})
        assert "did not move" in out

        shot = await client.call_tool("browser_screenshot", {"profile": "p", "full_page": True})
        assert not shot.is_error, text_of(shot)
        image = next(c for c in shot.content if isinstance(c, ImageContent))
        width, height = jpeg_size(base64.b64decode(image.data))
        assert height <= 4000 and width <= 4000
        assert "only the top" in text_of(shot)


async def test_read_and_extract_cover_shadow_dom_frames_and_hidden_content(home, origin):
    home.create_profile("p")
    async with Client(create_server(store=home)) as client:
        await call(client, "browser_navigate", {"profile": "p", "url": origin.url + "/"})
        assert "Shadow Title" in await call(client, "browser_read", {"profile": "p", "selector": ".title"})
        page = await call(client, "browser_read", {"profile": "p"})
        assert "REVEAL_ME" not in page and "HIDDEN_ITEM" not in page
        assert "1 iframe(s) not included" in page
        assert "hidden text block(s) below the visible area" in page

        assert '"$5"' in await call(client, "browser_extract", {"profile": "p", "css": "product-card .price"})
        items = await call(client, "browser_extract", {"profile": "p", "css": "li.item"})
        assert '"One"' in items and '"Two"' in items and "HIDDEN_ITEM" not in items
        everything = await call(client, "browser_extract", {"profile": "p", "css": "li.item", "include_hidden": True})
        assert "HIDDEN_ITEM" in everything
        assert '"FRAME_TEXT"' in await call(client, "browser_extract", {"profile": "p", "css": ".inframe"})
        assert "drop ::text" in await call(client, "browser_extract", {"profile": "p", "css": "header::text"})


async def test_popups_downloads_pdfs_and_data_urls(home, origin):
    p = home.create_profile("p")
    async with Client(create_server(store=home)) as client:
        await call(client, "browser_navigate", {"profile": "p", "url": origin.url + "/"})
        out = await call(client, "browser_evaluate", {"profile": "p", "expression": "window.open('/next'); 1"})
        assert "A new tab opened (tab 1) and is now the active tab." in out
        tabs = await call(client, "browser_tabs", {"profile": "p"})
        assert "* 1: Next page" in tabs
        await call(client, "browser_tabs", {"profile": "p", "action": "close", "index": 1})

        downloads = home.downloads_dir(p.id)
        out = await call(client, "browser_navigate", {"profile": "p", "url": origin.url + "/file.zip"}, ok=False)
        assert str(downloads) in out and "http_fetch" in out
        out = await call(client, "http_fetch", {"profile": "p", "url": origin.url + "/file.zip"})
        assert "saved to" in out and str(downloads) in out

        await call(client, "browser_navigate", {"profile": "p", "url": origin.url + "/doc.pdf"})
        out = await call(client, "browser_read", {"profile": "p"})
        assert "shows a PDF" in out and "try browser_wait_for" not in out

        out = await call(client, "browser_navigate", {"profile": "p", "url": "data:text/html,<p>hi</p>"})
        assert "no HTTP response for data:/about: URLs" in out


async def test_remote_mode_never_returns_content_of_pages_that_moved_to_private_addresses(home, origin):
    home.create_profile("r")
    target = f"http://127.0.0.1:{origin.port}/"
    async with Client(create_server(store=home, remote=True)) as client:
        for tool, args in (("browser_read", {}), ("browser_snapshot", {}), ("browser_extract", {"css": "h1"}),
                           ("browser_screenshot", {})):
            await call(client, "browser_evaluate", {
                "profile": "r", "expression": f"setTimeout(() => {{ location.href = '{target}'; }}, 1000); 1",
            })
            await asyncio.sleep(2.5)
            out = await call(client, tool, {"profile": "r", **args}, ok=False)
            assert "blocked in remote mode" in out and "Tools page" not in out, tool
        tabs = await call(client, "browser_tabs", {"profile": "r"})
        assert "127.0.0.1" not in tabs

        await call(client, "browser_evaluate", {"profile": "r", "expression": f"setTimeout(() => window.open('{target}'), 500); 1"})
        await asyncio.sleep(2.5)
        tabs = await call(client, "browser_tabs", {"profile": "r"})
        assert "were blanked" in tabs and "127.0.0.1" not in tabs and "Tools page" not in tabs
