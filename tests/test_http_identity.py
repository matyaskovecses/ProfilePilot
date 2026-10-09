"""FIX-PLAN step 7 (F9): requests made next to the browser carry the running browser's own identity.

Unit tests cover header construction and the curl_cffi target choice; chrome-marked tests read the
identity of a real Chrome (and Edge, when installed) and compare it with what that browser itself
sends, and with what a curl_cffi session built from it sends.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

import pytest

from profilepilot.automation.http_identity import (
    HttpIdentity, accept_language, curl_headers, impersonate_target, read_http_identity, websocket_url,
)
from profilepilot.errors import BrowserNotFoundError
from profilepilot.paths import find_browser

from .chrome_helper import launch_chrome
from .fakes import OriginServer

EDGE_HINTS = {
    "brands": [{"brand": "Chromium", "version": "154"}, {"brand": "Microsoft Edge", "version": "154"},
               {"brand": "Not A(Brand", "version": "99"}],
    "mobile": False, "platform": "Windows", "platformVersion": "19.0.0",
}


def test_headers_come_from_the_browser_values():
    ident = HttpIdentity.from_values(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0",
        "Chrome/154.0.4258.62", EDGE_HINTS, ["de-DE", "de", "en-US"])
    headers = ident.headers()
    assert headers["User-Agent"].endswith("Edg/154.0.0.0") and "Macintosh" not in headers["User-Agent"]
    assert headers["sec-ch-ua"] == '"Chromium";v="154", "Microsoft Edge";v="154", "Not A(Brand";v="99"'
    assert headers["sec-ch-ua-platform"] == '"Windows"' and headers["sec-ch-ua-mobile"] == "?0"
    assert headers["Accept-Language"] == "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"
    assert ident.family == "edge" and ident.major == 154
    assert ident.describe() == "Microsoft Edge 154 on Windows"


def test_without_client_hints_curl_drops_its_builtin_ones():
    ident = HttpIdentity.from_values("Mozilla/5.0 ... Chrome/154.0.0.0 Safari/537.36")
    headers = curl_headers(ident)
    assert not ident.has_client_hints
    assert all(headers[h] is None for h in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"))


def test_accept_language_format():
    assert accept_language(["en-US"]) == "en-US,en;q=0.9"            # Chrome adds the base language
    assert accept_language(["en-US", "en"]) == "en-US,en;q=0.9"
    assert accept_language(["en-US", "en-GB", "fr"]) == "en-US,en-GB;q=0.9,en;q=0.8,fr;q=0.7"
    assert accept_language(["de"]) == "de"
    assert accept_language([]) is None
    assert accept_language(["x;evil"]) is None


@pytest.mark.parametrize("family, major, expected", [
    ("chrome", 154, "chrome146"),     # newest target not newer than the browser
    ("chrome", 120, "chrome120"),
    ("edge", 154, "chrome146"),       # a newer Chrome target beats an older Edge target
    ("edge", 101, "edge101"),         # tie on version: the Edge target wins
    ("brave", 154, "chrome146"),
    ("chrome", 90, "chrome99"),       # older than every target: the oldest
    ("chrome", None, "chrome146"),
])
def test_impersonate_target(family, major, expected):
    targets = ["chrome99", "chrome101", "chrome120", "chrome146", "edge99", "edge101", "safari17_0", "firefox133"]
    assert impersonate_target(family, major, targets) == expected


def test_impersonate_target_without_targets():
    assert impersonate_target("chrome", 154, []) == "chrome"


# ---------------------------------------------------------------------- real browsers


def _open(port: int, url: str) -> None:
    """Open ``url`` in a new tab through the plain HTTP endpoint (no CDP client attached)."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        json.loads(r.read())


async def _browser_request_headers(origin: OriginServer, port: int, path: str) -> dict[str, str]:
    _open(port, f"{origin.url}{path}")
    for _ in range(100):
        hits = [r for r in origin.requests if r["path"] == path]
        if hits:
            return {k.lower(): v for k, v in hits[0]["headers"].items()}
        await asyncio.sleep(0.1)
    raise AssertionError("the browser never requested the page")


def _browser_kinds() -> list[str]:
    kinds = []
    for kind in ("chrome", "edge"):
        try:
            find_browser(kind)
            kinds.append(kind)
        except BrowserNotFoundError:
            pass
    return kinds


@pytest.mark.chrome
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", _browser_kinds() or ["chrome"])
async def test_identity_matches_what_the_browser_sends(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("PROFILEPILOT_BROWSER", find_browser(kind).path)
    with OriginServer() as origin, launch_chrome(tmp_path / f"udd-{kind}") as chrome:
        ws = await websocket_url(chrome.http_url)
        assert ws and ws.startswith("ws://")
        ident = await read_http_identity(ws)
        assert ident is not None and ident.has_client_hints and ident.languages
        assert ident.family == kind
        sent = await _browser_request_headers(origin, chrome.port, "/who")
        # The browser's own navigation and the identity agree on every identity header.
        assert sent["user-agent"] == ident.user_agent
        assert sent["sec-ch-ua-platform"] == ident.headers()["sec-ch-ua-platform"]
        assert sent["sec-ch-ua"] == ident.sec_ch_ua()
        assert sent["accept-language"] == ident.accept_language()
        if kind == "edge":
            assert "Microsoft Edge" in ident.sec_ch_ua() and "Edg/" in ident.user_agent


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_curl_cffi_request_carries_the_browser_identity(tmp_path):
    curl = pytest.importorskip("curl_cffi.requests")
    with OriginServer() as origin, launch_chrome(tmp_path / "udd") as chrome:
        ident = await read_http_identity(await websocket_url(chrome.http_url))
        assert ident is not None
        sent = await _browser_request_headers(origin, chrome.port, "/browser")
        headers = {k: v for k, v in curl_headers(ident).items() if v is not None}
        target = impersonate_target(ident.family, ident.major)
        with curl.Session(impersonate=target) as s:
            s.get(f"{origin.url}/curl", headers=headers, timeout=10)
        got = {k.lower(): v for k, v in next(r for r in origin.requests if r["path"] == "/curl")["headers"].items()}
        assert "Macintosh" not in got["user-agent"]
        for name in ("user-agent", "sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile", "accept-language"):
            assert got[name] == sent[name], name
