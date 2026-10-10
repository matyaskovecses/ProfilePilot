"""The live cookie jar (profilepilot.browser.cookiejar): normalising, ids, validation and edits
without a browser (a small fake of Chrome's cookie store behind CDP), then round trips against a
throwaway real Chrome (headless, isolated user-data-dir, never the user's own browser).

No test prints or compares a cookie value in an error message: values are secrets.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
import pytest_asyncio

from profilepilot.automation.cookies import dumps_cookies
from profilepilot.browser import cookiejar
from profilepilot.browser.cookiejar import (
    CookieRefusedError,
    InvalidCookieError,
    clear_cookies,
    cookie_key,
    cookie_view,
    delete_cookies,
    domain_counts,
    filter_domain,
    import_cookies,
    list_cookies,
    merge_cookie,
    normalize_cookie,
    parse_import,
    parse_key,
    save_cookie,
    set_cookies,
    validate_cookie,
)
from profilepilot.browser.devtools import CdpError, browser_connection
from profilepilot.errors import NotFoundError, ProfilePilotError

from .fakes import OriginServer

NOW = time.time()
FUTURE = int(NOW) + 86400 * 20
SECRET = "s3cr3t-cookie-value"
TOP = {"topLevelSite": "https://top.test", "hasCrossSiteAncestor": False}
TOP_X = {"topLevelSite": "https://top.test", "hasCrossSiteAncestor": True}
ALLOWED = {"Storage.getCookies", "Storage.setCookies", "Storage.clearCookies", "Target.getTargets",
           "Target.attachToTarget", "Target.detachFromTarget", "Network.deleteCookies"}


def cookie(name: str = "a", domain: str = "example.com", path: str = "/", **kw: Any) -> dict[str, Any]:
    return {"name": name, "value": kw.pop("value", f"{name}-value"), "domain": domain, "path": path,
            "expires": kw.pop("expires", FUTURE), "secure": kw.pop("secure", True), "httpOnly": kw.pop("httpOnly", False),
            **kw}


def assert_native(sent: list[str]) -> None:
    """Only browser-level cookie commands (and the page session for exotic paths): never an enable."""
    assert not [m for m in sent if m.endswith(".enable")], sent
    assert set(sent) <= ALLOWED, set(sent) - ALLOWED


class FakeCdp:
    """Just enough of Chrome's cookie store behind :class:`CdpConnection.call`: refuses what Chrome
    refuses ("Invalid cookie fields" for the whole batch), rewrites paths with spaces, turns a domain
    cookie for a public suffix into a host-only one, deletes on an expired set."""

    def __init__(self, cookies: list[dict[str, Any]] = (), *, pages: bool = True) -> None:  # type: ignore[assignment]
        self.jar: dict[str, dict[str, Any]] = {}
        self.sent: list[str] = []
        self.pages = pages
        for c in cookies:
            self._put(dict(c))

    @staticmethod
    def _invalid(p: dict[str, Any]) -> bool:
        return (not p.get("name") or not str(p.get("path", "/")).startswith("/")
                or (p.get("partitionKey") and not p.get("secure"))
                or (p.get("secure") and p.get("sourceScheme") == "NonSecure")
                or len(p.get("name", "")) + len(p.get("value", "")) > 4096)

    def _put(self, p: dict[str, Any]) -> None:
        c = {"name": p["name"], "value": p.get("value", ""), "domain": p["domain"], "path": p.get("path", "/"),
             "expires": p.get("expires", -1), "size": len(p["name"]) + len(p.get("value", "")),
             "httpOnly": bool(p.get("httpOnly")), "secure": bool(p.get("secure")), "session": "expires" not in p,
             "priority": p.get("priority", "Medium"), "sourceScheme": p.get("sourceScheme", "Secure" if p.get("secure") else "NonSecure"),
             "sourcePort": p.get("sourcePort", 443 if p.get("secure") else 80)}
        for key in ("sameSite", "partitionKey"):
            if p.get(key):
                c[key] = p[key]
        self.jar[cookie_key(c)] = c

    async def call(self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None,
                   timeout: float = 5.0) -> dict[str, Any]:
        self.sent.append(method)
        params = params or {}
        if method == "Storage.getCookies":
            return {"cookies": [dict(c) for c in self.jar.values()]}
        if method == "Storage.setCookies":
            if any(self._invalid(p) for p in params["cookies"]):
                raise CdpError("Invalid cookie fields")
            for p in params["cookies"]:
                p = {**p, "path": p.get("path", "/").replace(" ", "%20")}
                if p["domain"] == ".co.uk":
                    p["domain"] = "co.uk"
                if p.get("expires") is not None and 0 < p["expires"] < time.time():
                    self.jar.pop(cookie_key(p), None)
                else:
                    self._put(p)
            return {}
        if method == "Storage.clearCookies":
            self.jar.clear()
            return {}
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "T1", "type": "page"}] if self.pages else []}
        if method == "Target.attachToTarget":
            return {"sessionId": "S1"}
        if method == "Target.detachFromTarget":
            return {}
        if method == "Network.deleteCookies":
            assert session_id == "S1"
            self.jar.pop(cookie_key(params), None)
            return {}
        raise CdpError(f"'{method}' wasn't found")

    def values(self) -> dict[tuple, str]:
        return {(c["name"], c["domain"], c["path"], json.dumps(c.get("partitionKey"))): c["value"] for c in self.jar.values()}


# ---------------------------------------------------------------------- normalising and ids


def test_normalize_keeps_every_attribute_chrome_reports() -> None:
    raw = {"name": "sid", "value": "v", "domain": ".Example.com", "path": "/app", "expires": 1792455725.70419,
           "size": 4, "httpOnly": True, "secure": True, "session": False, "sameSite": "Lax", "priority": "High",
           "sameParty": False, "sourceScheme": "Secure", "sourcePort": 8443, "partitionKey": TOP_X}
    c = normalize_cookie(raw)
    assert c == {"domain": ".Example.com", "name": "sid", "value": "v", "path": "/app", "expires": 1792455725.70419,
                 "secure": True, "httpOnly": True, "sameSite": "Lax", "partitionKey": TOP_X, "priority": "High",
                 "sourceScheme": "Secure", "sourcePort": 8443, "size": 4}
    session = normalize_cookie({"name": "s", "value": "", "domain": "x.test", "path": "/", "expires": -1,
                                "session": True, "sameSite": "Unspecified"})
    assert session["expires"] is None and session["sameSite"] is None and session["size"] == 1
    assert normalize_cookie({**raw, "expires": 1792455725.0})["expires"] == 1792455725  # whole seconds stay ints


def test_cookie_key_is_the_identity_and_url_safe() -> None:
    base = cookie("a", value="one")
    twins = [base, cookie("a", ".example.com"), cookie("a", path="/p"), cookie("a", partitionKey=TOP),
             cookie("a", partitionKey=TOP_X), cookie("b")]
    keys = [cookie_key(c) for c in twins]
    assert len(set(keys)) == len(keys)
    assert cookie_key(cookie("a", value="other", expires=None, secure=False)) == keys[0]  # value etc. are not identity
    assert cookie_key(cookie("a", "EXAMPLE.com")) == keys[0]
    for key, c in zip(keys, twins):
        assert key.replace("-", "").replace("_", "").isalnum() and "," not in key
        identity = parse_key(key)
        assert (identity["name"], identity["domain"], identity["path"]) == (c["name"], c["domain"], c["path"])
        assert identity["partitionKey"] == c.get("partitionKey")
        assert cookie_key({**identity}) == key
    for bad in ("", "not a key", "W10", "e30", "x" * (cookiejar.MAX_KEY + 1), 42):
        with pytest.raises(InvalidCookieError, match="cookie id"):
            parse_key(bad)  # type: ignore[arg-type]


def test_view_for_the_manager() -> None:
    c = normalize_cookie({**cookie("sid", ".example.com", "/app", value=SECRET, expires=1792455725.5, httpOnly=True,
                                   sameSite="Strict", partitionKey=TOP_X), "size": 23})
    view = cookie_view(c)
    assert view == {"key": cookie_key(c), "name": "sid", "value": SECRET, "domain": ".example.com", "host_only": False,
                    "path": "/app", "expires": "2026-10-20T00:22:05.500Z", "session": False, "http_only": True,
                    "secure": True, "same_site": "Strict", "partitioned": True, "partition_site": "https://top.test",
                    "partition_cross_site": True, "size": 23, "priority": "Medium"}
    session = cookie_view(normalize_cookie(cookie("s", expires=-1)))
    assert session["session"] is True and session["expires"] is None and session["host_only"] is True
    assert session["partitioned"] is False and session["partition_site"] is None
    assert domain_counts([c, cookie("b", "example.com"), cookie("c", "other.test")]) == [
        {"domain": "example.com", "count": 2}, {"domain": "other.test", "count": 1}]
    assert filter_domain(" .Example.COM ") == "example.com" and filter_domain("bücher.de") == "xn--bcher-kva.de"
    with pytest.raises(InvalidCookieError):
        filter_domain("  ")


# ---------------------------------------------------------------------- validation


@pytest.mark.parametrize("changes,match", [
    ({"name": ""}, "needs a name"),
    ({"name": " a"}, "name"),
    ({"name": "a=b"}, "name"),
    ({"value": SECRET + "\n"}, "value"),
    ({"value": SECRET + ";x"}, "value"),
    ({"value": "x" * 4096}, "4096 bytes"),
    ({"domain": ""}, "domain"),
    ({"domain": "exa mple.com"}, "not a valid domain"),
    ({"domain": "-bad-.com"}, "not a valid domain"),
    ({"domain": "a..b.com"}, "not a valid domain"),
    ({"domain": ".com"}, "not allowed"),
    ({"domain": ".127.0.0.1"}, "IP address"),
    ({"domain": "300.1.1.1"}, "IP address"),
    ({"domain": "[::zz]"}, "IPv6"),
    ({"path": "app"}, "start with /"),
    ({"path": "/a b"}, "%20"),
    ({"path": "/a/../b"}, "'..'"),
    ({"path": "/" + "p" * 1100}, "too long"),
    ({"sameSite": "None", "secure": False}, "SameSite=None needs Secure"),
    ({"name": "__Secure-id", "secure": False}, "__Secure-"),
    ({"name": "__Host-id", "domain": ".example.com"}, "__Host-"),
    ({"name": "__Host-id", "path": "/app"}, "__Host-"),
    ({"partitionKey": TOP, "secure": False}, "partitioned cookie must be Secure"),
    ({"partitionKey": {"topLevelSite": "ftp://top.test"}}, "partition"),
    ({"expires": NOW - 60}, "already expired"),
    ({"expires": "next tuesday"}, "expiry"),
    ({"partitionKeyOpaque": True}, "opaque"),
])
def test_validation_explains_and_never_quotes_the_value(changes, match) -> None:
    with pytest.raises(InvalidCookieError, match=match) as info:
        validate_cookie({**cookie(value=SECRET), **changes})
    assert SECRET not in str(info.value)
    assert isinstance(info.value, ProfilePilotError)


def test_validation_normalises() -> None:
    c = validate_cookie(cookie("a", "Bücher.DE", expires=FUTURE + 0.25, priority="high", sourceScheme="NonSecure",
                               sourcePort=80, partitionKey="https://www.top.test:8443", sameSite="no_restriction"))
    assert c["domain"] == "xn--bcher-kva.de" and c["expires"] == FUTURE + 0.25 and c["priority"] == "High"
    assert c["partitionKey"] == {"topLevelSite": "https://www.top.test", "hasCrossSiteAncestor": False}
    assert c["sameSite"] == "None" and "sourceScheme" not in c and "sourcePort" not in c  # Secure + NonSecure: Chrome refuses
    assert validate_cookie(cookie("__Host-ok"))["name"] == "__Host-ok"
    assert validate_cookie(cookie("ip", "127.0.0.1"))["domain"] == "127.0.0.1"
    assert validate_cookie(cookie("v6", "[::1]"))["domain"] == "[::1]"
    assert validate_cookie(cookie(expires=None))["expires"] is None


def test_merge_edits_keep_what_they_do_not_mention() -> None:
    base = normalize_cookie({**cookie("sid", ".example.com", "/app", value="old", expires=1792455725.704219,
                                      sameSite="Lax", priority="High", sourceScheme="Secure", sourcePort=8443),
                             "size": 6})
    view = cookie_view(base)
    # The whole view sent back with one change: Chrome's exact expiry survives the view's milliseconds.
    edited = validate_cookie(merge_cookie(base, {**view, "value": "new"}))
    assert edited == {**{k: v for k, v in base.items() if k != "size"}, "value": "new", "size": 6}
    assert merge_cookie(base, {"expires": view["expires"]})["expires"] == 1792455725.704219
    assert merge_cookie(base, {"expires": "2030-01-01T00:00:00Z"})["expires"] == "2030-01-01T00:00:00Z"
    host_only = merge_cookie(base, {"host_only": True})
    assert host_only["domain"] == "example.com" and "sourcePort" not in host_only  # origin no longer true
    assert merge_cookie(base, {"session": True})["expires"] is None
    with pytest.raises(InvalidCookieError, match="expires"):
        merge_cookie({**base, "expires": None}, {"session": False})
    part = merge_cookie(base, {"partition_site": "https://top.test", "partition_cross_site": True})
    assert part["partitionKey"] == TOP_X
    assert "partitionKey" not in merge_cookie(part, {"partitioned": False})
    with pytest.raises(InvalidCookieError, match="partitioned for"):
        merge_cookie(base, {"partitioned": True})
    # A new cookie from the Add dialog (view names) or any accepted shape.
    new = validate_cookie(merge_cookie(None, {"name": "n", "value": "v", "domain": "example.com", "host_only": False,
                                              "http_only": True, "same_site": "Strict", "session": True,
                                              "partitioned": True, "partition_site": "top.test", "secure": True}))
    assert new["domain"] == ".example.com" and new["httpOnly"] and new["sameSite"] == "Strict" and new["expires"] is None
    assert new["partitionKey"] == {"topLevelSite": "https://top.test", "hasCrossSiteAncestor": False}
    from_url = validate_cookie(merge_cookie(None, {"name": "n", "url": "https://shop.example.com/x", "value": "v"}))
    assert from_url["domain"] == "shop.example.com"
    with pytest.raises(InvalidCookieError, match="object"):
        merge_cookie(None, ["not", "a", "cookie"])  # type: ignore[arg-type]


def test_parse_import_reports_problems_by_position_without_values() -> None:
    text = json.dumps({"cookies": [
        cookie("good", value="v1"),
        {"name": "nodomain", "value": SECRET},
        cookie("expired", value=SECRET, expires=NOW - 10),
        cookie("good", value="v2"),  # same identity: the later one wins
        cookie("sub", "a.example.com", value="v3", partitionKey=TOP),
        cookie("other", "other.test", value="v4"),
        "junk",
    ], "origins": []})
    parsed = parse_import(text)
    assert parsed.format == "json"
    assert [(c["name"], c["value"]) for c in parsed.cookies] == [("good", "v2"), ("sub", "v3"), ("other", "v4")]
    assert parsed.skipped == 4 and len(parsed.problems) == 3
    assert parsed.problems[0].startswith("Cookie #2") and "Cookie #7" in parsed.problems[1]
    assert "'expired' on example.com" in parsed.problems[2] and "already expired" in parsed.problems[2]
    assert not any(SECRET in p for p in parsed.problems)
    only = parse_import(text, domain="Example.com")
    assert {c["name"] for c in only.cookies} == {"good", "sub"} and only.skipped == 5
    netscape = parse_import(dumps_cookies([cookie("n", ".example.com", value="v", httpOnly=True)], "netscape")
                            + "bad line\n")
    assert netscape.format == "netscape" and netscape.cookies[0]["domain"] == ".example.com" and netscape.cookies[0]["httpOnly"]
    assert netscape.problems == ["cookies.txt line 5: expected 7 tab-separated fields, got 1."]
    assert parse_import("{not json").problems[0].startswith("Invalid JSON")
    # Exported JSON keeps Chrome's sub-second expiry and partition, so it imports back exactly.
    exact = cookie("x", expires=FUTURE + 0.704219, partitionKey=TOP_X, priority="Low")
    back = parse_import(dumps_cookies([exact], "json", exact_expiry=True)).cookies[0]
    assert back["expires"] == FUTURE + 0.704219 and back["partitionKey"] == TOP_X and back["priority"] == "Low"


# ---------------------------------------------------------------------- edits against a fake store


@pytest.mark.asyncio
async def test_writes_are_validated_first_and_read_back() -> None:
    fake = FakeCdp([cookie("keep")])
    with pytest.raises(InvalidCookieError, match=r"Cookie #2 \('b' on example.com\)"):
        await set_cookies("ws://unused", [cookie("a"), cookie("b", sameSite="None", secure=False)], cdp=fake)  # type: ignore[arg-type]
    assert fake.sent == []  # nothing sent when one cookie is invalid
    assert await set_cookies("ws://unused", [cookie("a"), cookie("a", value="later"), cookie("b")], cdp=fake) == 2  # type: ignore[arg-type]
    assert fake.values()[("a", "example.com", "/", "null")] == "later"
    # Chrome turns a domain cookie for a public suffix into a host-only one: reported, and undone.
    fake.jar.clear()
    fake._put(cookie("p", "co.uk", value="old"))
    with pytest.raises(CookieRefusedError) as info:
        await set_cookies("ws://unused", [cookie("p", ".co.uk", value=SECRET), cookie("q", ".co.uk")], cdp=fake)  # type: ignore[arg-type]
    assert info.value.refused == ["'p' on co.uk", "'q' on co.uk"] and info.value.done == 0
    assert SECRET not in str(info.value) and "public suffixes" in str(info.value)
    assert fake.values() == {("p", "co.uk", "/", "null"): "old"}  # the old host-only cookie is back, no stray 'q'
    assert_native(fake.sent)


@pytest.mark.asyncio
async def test_a_refused_cookie_in_a_batch_does_not_block_the_others() -> None:
    fake = FakeCdp()
    real_invalid = fake._invalid
    fake._invalid = lambda p: real_invalid(p) or p["name"] == "refused"  # type: ignore[method-assign]
    with pytest.raises(CookieRefusedError) as info:
        await set_cookies("ws://unused", [cookie("a"), cookie("refused"), cookie("b")], cdp=fake)  # type: ignore[arg-type]
    assert info.value.refused == ["'refused' on example.com"] and info.value.done == 2
    assert set(fake.values()) == {("a", "example.com", "/", "null"), ("b", "example.com", "/", "null")}


@pytest.mark.asyncio
async def test_rename_sets_first_and_keeps_the_old_cookie_when_chrome_refuses() -> None:
    fake = FakeCdp([cookie("a", value="v"), cookie("other")])
    key = cookie_key(cookie("a"))
    saved = await save_cookie("ws://unused", {"name": "b"}, replace=key, cdp=fake)  # type: ignore[arg-type]
    assert saved["name"] == "b" and saved["value"] == "v" and saved["expires"] == FUTURE
    assert set(fake.values()) == {("b", "example.com", "/", "null"), ("other", "example.com", "/", "null")}
    fake.sent.clear()
    with pytest.raises(CookieRefusedError):
        await save_cookie("ws://unused", {"domain": ".co.uk"}, replace=cookie_key(saved), cdp=fake)  # type: ignore[arg-type]
    assert set(fake.values()) == {("b", "example.com", "/", "null"), ("other", "example.com", "/", "null")}
    with pytest.raises(NotFoundError):
        await save_cookie("ws://unused", {"value": "x"}, replace=key, cdp=fake)  # type: ignore[arg-type]
    with pytest.raises(InvalidCookieError):
        await save_cookie("ws://unused", {"name": "c", "domain": "example.com", "path": "nope"}, cdp=fake)  # type: ignore[arg-type]
    assert_native(fake.sent)


@pytest.mark.asyncio
async def test_unusual_paths_are_deleted_in_a_page_session_without_touching_their_encoded_twin() -> None:
    weird = {**cookie("w", path="/a b"), "value": "server-set"}
    fake = FakeCdp([cookie("w", path="/a%20b", value="twin"), cookie("keep")])
    fake.jar[cookie_key(weird)] = {**weird, "size": 11, "session": False}  # set by a server: Chrome keeps the raw path
    assert await delete_cookies("ws://unused", [cookie_key(weird)], cdp=fake) == 1  # type: ignore[arg-type]
    assert set(fake.values()) == {("w", "example.com", "/a%20b", "null"), ("keep", "example.com", "/", "null")}
    assert "Network.deleteCookies" in fake.sent and "Target.detachFromTarget" in fake.sent
    assert "Storage.setCookies" not in fake.sent  # an expired copy would have hit '/a%20b'
    # Without a page to attach to, the cookie stays and the caller is told.
    fake = FakeCdp(pages=False)
    fake.jar[cookie_key(weird)] = {**weird, "size": 11, "session": False}
    with pytest.raises(CookieRefusedError, match="did not delete"):
        await delete_cookies("ws://unused", [cookie_key(weird)], cdp=fake)  # type: ignore[arg-type]
    assert_native(fake.sent)


@pytest.mark.asyncio
async def test_import_modes_only_remove_after_writing() -> None:
    start = [cookie("old", "example.com"), cookie("old", "sub.example.com"), cookie("x", "other.test")]
    fake = FakeCdp(start)
    result = await import_cookies("ws://unused", [cookie("new", ".example.com")], mode="merge", cdp=fake)  # type: ignore[arg-type]
    assert (result.imported, result.removed, result.refused) == (1, 0, [])
    assert len(fake.jar) == 4
    result = await import_cookies("ws://unused", [cookie("new", ".example.com", value="2")], mode="replace", cdp=fake)  # type: ignore[arg-type]
    assert result.removed == 2 and set(fake.values()) == {("new", ".example.com", "/", "null"), ("x", "other.test", "/", "null")}
    result = await import_cookies("ws://unused", [cookie("only", "third.test")], mode="replace_all", cdp=fake)  # type: ignore[arg-type]
    assert result.removed == 2 and set(fake.values()) == {("only", "third.test", "/", "null")}
    # Nothing stored: nothing removed either.
    result = await import_cookies("ws://unused", [cookie("p", ".co.uk")], mode="replace_all", cdp=fake)  # type: ignore[arg-type]
    assert result.imported == 0 and result.refused == ["'p' on co.uk"] and set(fake.values()) == {("only", "third.test", "/", "null")}
    with pytest.raises(InvalidCookieError):
        await import_cookies("ws://unused", [], mode="wipe", cdp=fake)  # type: ignore[arg-type]
    assert_native(fake.sent)


@pytest.mark.asyncio
async def test_replace_import_keeps_the_cookies_of_a_site_chrome_refused() -> None:
    fake = FakeCdp([cookie("login", "co.uk"), cookie("stale", "a.test")])
    result = await import_cookies("ws://unused", [cookie("new", "a.test"), cookie("p", ".co.uk")],  # type: ignore[arg-type]
                                  mode="replace", cdp=fake)
    assert (result.imported, result.refused, result.removed) == (1, ["'p' on co.uk"], 1)
    # co.uk received nothing, so its login stays; only a.test was replaced
    assert set(fake.values()) == {("login", "co.uk", "/", "null"), ("new", "a.test", "/", "null")}


@pytest.mark.asyncio
async def test_a_replace_import_that_cannot_remove_an_old_cookie_still_reports_what_it_stored() -> None:
    weird = {**cookie("w", path="/a b"), "value": "server-set"}
    fake = FakeCdp([cookie("old")], pages=False)  # no page to delete '/a b' in
    fake.jar[cookie_key(weird)] = {**weird, "size": 11, "session": False}
    result = await import_cookies("ws://unused", [cookie("new")], mode="replace", cdp=fake)  # type: ignore[arg-type]
    assert (result.imported, result.removed, result.not_removed) == (1, 1, ["'w' on example.com (path /a b)"])
    assert ("new", "example.com", "/", "null") in fake.values() and ("old", "example.com", "/", "null") not in fake.values()


@pytest.mark.asyncio
async def test_an_edit_never_silently_merges_into_another_existing_cookie() -> None:
    host_only, domain_wide = cookie("sid", value="host"), cookie("sid", ".example.com", value="domain")
    fake = FakeCdp([host_only, domain_wide])
    with pytest.raises(cookiejar.CookieExistsError, match="already is a cookie 'sid' for example.com and its subdomains") as caught:
        await save_cookie("ws://unused", {"domain": ".example.com"}, replace=cookie_key(host_only), cdp=fake)  # type: ignore[arg-type]
    assert isinstance(caught.value, ProfilePilotError) and "domain" not in str(caught.value).split("'sid'")[0]
    assert fake.values()[("sid", ".example.com", "/", "null")] == "domain" and len(fake.jar) == 2  # untouched
    saved = await save_cookie("ws://unused", {"domain": ".example.com"}, replace=cookie_key(host_only),  # type: ignore[arg-type]
                              overwrite=True, cdp=fake)
    assert saved["value"] == "host" and set(fake.values()) == {("sid", ".example.com", "/", "null")}
    # an edit that keeps its identity is never a collision
    await save_cookie("ws://unused", {"value": "v2"}, replace=cookie_key(saved), cdp=fake)  # type: ignore[arg-type]
    assert_native(fake.sent)


# ---------------------------------------------------------------------- real Chrome


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    """One throwaway headless Chrome for the module (no window: nothing can take the foreground)."""
    from .chrome_helper import launch_chrome

    with launch_chrome(tmp_path_factory.mktemp("cookiejar") / "udd", "--headless=new") as launched:
        yield launched


@pytest_asyncio.fixture
async def ws(chrome) -> str:
    """The test Chrome's browser websocket, with an empty cookie jar."""
    url = chrome.version_info()["webSocketDebuggerUrl"]
    async with browser_connection(url) as cdp:
        await cdp.call("Storage.clearCookies")
    return url


def by_identity(cookies: list[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    return {(c["name"], c["domain"], c["path"], json.dumps(c.get("partitionKey"))): c for c in cookies}


ROUND_TRIP = [
    cookie("host", "example.com", value="h", expires=FUTURE + 0.704219, sameSite="Strict", httpOnly=True),
    cookie("host", ".example.com", value="d", sameSite="Lax", priority="High"),
    cookie("host", "example.com", "/app", value="p", sameSite="None", priority="Low", sourcePort=8443),
    cookie("session", "sub.example.com", value="s", expires=None, secure=False),
    cookie("chips", "example.com", value="c1", partitionKey=TOP),
    cookie("chips", "example.com", value="c2", partitionKey=TOP_X),
    cookie("__Host-id", "example.com", value="hid", httpOnly=True),
    cookie("ip", "127.0.0.1", value="i", secure=False, sameSite="Lax"),
]


def same_attributes(got: dict[str, Any], want: dict[str, Any]) -> None:
    for key in ("name", "value", "domain", "path", "expires", "secure", "httpOnly"):
        assert got[key] == want[key], (key, got["name"])
    assert got["sameSite"] == want.get("sameSite"), got["name"]
    assert got.get("partitionKey") == want.get("partitionKey"), got["name"]
    assert got.get("priority", "Medium") == want.get("priority", "Medium"), got["name"]
    if "sourcePort" in want:
        assert got["sourcePort"] == want["sourcePort"]


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_chrome_every_attribute_round_trips(ws) -> None:
    async with browser_connection(ws) as cdp:
        assert await set_cookies(ws, ROUND_TRIP, cdp=cdp) == len(ROUND_TRIP)
        listed = await list_cookies(ws, cdp=cdp)
        assert len(listed) == len(ROUND_TRIP)
        got = by_identity(listed)
        for want in ROUND_TRIP:
            same_attributes(got[(want["name"], want["domain"], want["path"], json.dumps(want.get("partitionKey")))], want)
        # export -> clear -> import gives the very same jar (every attribute Chrome reports).
        exported = dumps_cookies(listed, "json", exact_expiry=True)
        assert await clear_cookies(ws, cdp=cdp) == len(ROUND_TRIP) and await list_cookies(ws, cdp=cdp) == []
        parsed = parse_import(exported)
        assert parsed.problems == [] and len(parsed.cookies) == len(ROUND_TRIP)
        result = await import_cookies(ws, parsed.cookies, cdp=cdp)
        assert result.imported == len(ROUND_TRIP) and result.refused == []
        again = await list_cookies(ws, cdp=cdp)
        assert again == listed
        # list -> edit (the whole view sent back, value changed) -> save keeps everything else.
        target = got[("host", "example.com", "/", "null")]
        saved = await save_cookie(ws, {**cookie_view(target), "value": "edited"}, replace=cookie_key(target), cdp=cdp)
        assert saved == {**target, "value": "edited", "size": target["size"] - 1 + len("edited")}
        assert_native(cdp.sent)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_chrome_edit_rename_and_refusal(ws) -> None:
    async with browser_connection(ws) as cdp:
        await set_cookies(ws, [cookie("a", value="one"), cookie("a", ".example.com", value="dom"),
                               cookie("p", "co.uk", value="old")], cdp=cdp)
        key = cookie_key(cookie("a"))
        renamed = await save_cookie(ws, {"name": "b", "path": "/new"}, replace=key, cdp=cdp)
        assert (renamed["name"], renamed["path"], renamed["value"], renamed["expires"]) == ("b", "/new", "one", FUTURE)
        jar = by_identity(await list_cookies(ws, cdp=cdp))
        assert set(jar) == {("b", "example.com", "/new", "null"), ("a", ".example.com", "/", "null"),
                            ("p", "co.uk", "/", "null")}
        # Host-only -> domain cookie (and no longer Secure): a new identity, so the old cookie goes.
        moved = await save_cookie(ws, {"host_only": False, "secure": False}, replace=cookie_key(renamed), cdp=cdp)
        assert moved["domain"] == ".example.com" and moved["secure"] is False
        assert ("b", "example.com", "/new", "null") not in by_identity(await list_cookies(ws, cdp=cdp))
        # Chrome keeps no domain cookie for a public suffix: refused, the old cookie and the jar stay as they were.
        before = await list_cookies(ws, cdp=cdp)
        with pytest.raises(CookieRefusedError, match="public suffixes"):
            await save_cookie(ws, {"domain": ".co.uk", "value": "x"}, replace=cookie_key(moved), cdp=cdp)
        assert await list_cookies(ws, cdp=cdp) == before
        with pytest.raises(CookieRefusedError):  # Chrome writes it over the host-only 'p' on co.uk: put back
            await set_cookies(ws, [cookie("p", ".co.uk", value="new")], cdp=cdp)
        assert await list_cookies(ws, cdp=cdp) == before
        with pytest.raises(NotFoundError):
            await save_cookie(ws, {"value": "x"}, replace=key, cdp=cdp)
        assert_native(cdp.sent)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_chrome_deletes_exactly_one_cookie(ws) -> None:
    twins = [
        cookie("a", "example.com", value="host-only"),
        cookie("a", ".example.com", value="domain"),
        cookie("a", "example.com", "/p", value="path"),
        cookie("a", "example.com", "/p/q", value="deeper"),
        cookie("a", "example.com", value="partitioned", partitionKey=TOP),
        cookie("a", "example.com", value="cross-site", partitionKey=TOP_X),
        cookie("a", "example.com", value="other-top", partitionKey={"topLevelSite": "https://other.test",
                                                                  "hasCrossSiteAncestor": False}),
        cookie("a", "sub.example.com", value="sub"),
    ]
    async with browser_connection(ws) as cdp:
        await set_cookies(ws, twins, cdp=cdp)
        remaining = {c["value"] for c in await list_cookies(ws, cdp=cdp)}
        assert remaining == {c["value"] for c in twins}
        for c in twins:
            assert await delete_cookies(ws, [cookie_key(c)], cdp=cdp) == 1
            remaining.discard(c["value"])
            assert {x["value"] for x in await list_cookies(ws, cdp=cdp)} == remaining, c["value"]
        assert await delete_cookies(ws, [cookie_key(twins[0])], cdp=cdp) == 0  # gone already: ignored
        assert "Network.deleteCookies" not in cdp.sent  # plain paths: expired copies on the browser target
        assert_native(cdp.sent)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_chrome_unusual_path_is_deleted_without_touching_its_encoded_twin(ws) -> None:
    pages = {"/weird": ("text/html", b"<p>cookie</p>", {"Set-Cookie": "w=server; Path=/a b; Max-Age=3600"})}
    with OriginServer(files=pages) as server:
        async with browser_connection(ws) as cdp:
            await set_cookies(ws, [cookie("w", "127.0.0.1", "/a%20b", value="twin", secure=False)], cdp=cdp)
            tab = await cdp.call("Target.createTarget", {"url": f"{server.url}/weird"})  # the test's own tab
            try:
                for _ in range(100):
                    weird = [c for c in await list_cookies(ws) if c["path"] == "/a b"]
                    if weird:
                        break
                    await asyncio.sleep(0.1)
                assert weird, "the page did not set its cookie"
                cdp.sent.clear()
                assert await delete_cookies(ws, [cookie_key(weird[0])], cdp=cdp) == 1
                left = await list_cookies(ws, cdp=cdp)
                assert [(c["path"], c["value"]) for c in left] == [("/a%20b", "twin")]
                assert "Network.deleteCookies" in cdp.sent and "Target.detachFromTarget" in cdp.sent
                assert_native(cdp.sent)
            finally:
                await cdp.call("Target.closeTarget", {"targetId": tab["targetId"]})


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_chrome_clear_by_domain_and_all(ws) -> None:
    jar = [cookie("a", "example.com"), cookie("b", ".example.com"), cookie("c", "sub.example.com"),
           cookie("d", "example.com", partitionKey=TOP), cookie("e", "notexample.com"), cookie("f", "other.test"),
           cookie("g", "other.test", partitionKey={"topLevelSite": "https://example.com", "hasCrossSiteAncestor": False})]
    async with browser_connection(ws) as cdp:
        await set_cookies(ws, jar, cdp=cdp)
        assert await clear_cookies(ws, domain=".Example.com", cdp=cdp) == 4
        assert {c["name"] for c in await list_cookies(ws, cdp=cdp)} == {"e", "f", "g"}
        assert await clear_cookies(ws, domain="nothing.test", cdp=cdp) == 0
        assert await clear_cookies(ws, cdp=cdp) == 3
        assert await list_cookies(ws, cdp=cdp) == []
        with pytest.raises(InvalidCookieError):
            await clear_cookies(ws, domain=" ", cdp=cdp)
        assert_native(cdp.sent)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_chrome_import_replace_modes(ws) -> None:
    async with browser_connection(ws) as cdp:
        await set_cookies(ws, [cookie("old", "example.com"), cookie("old", ".sub.example.com"), cookie("x", "other.test")],
                          cdp=cdp)
        parsed = parse_import(json.dumps([cookie("new", ".example.com"), cookie("p", ".co.uk")]))
        result = await import_cookies(ws, parsed.cookies, mode="replace", cdp=cdp)
        assert result.imported == 1 and result.refused == ["'p' on co.uk"] and result.removed == 2
        assert {(c["name"], c["domain"]) for c in await list_cookies(ws, cdp=cdp)} == {("new", ".example.com"),
                                                                                       ("x", "other.test")}
        result = await import_cookies(ws, [cookie("only", "third.test")], mode="replace_all", cdp=cdp)
        assert result.removed == 2 and [c["name"] for c in await list_cookies(ws, cdp=cdp)] == ["only"]
        assert_native(cdp.sent)
