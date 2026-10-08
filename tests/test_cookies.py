import http.cookiejar
import json
import time
import urllib.request

import pytest

from profilepilot.automation.cookies import (
    NETSCAPE_HEADER,
    CookieFormatError,
    cookie_summary,
    detect_format,
    domain_matches,
    dumps_cookies,
    export_cookies,
    filter_cookies,
    from_cookiejar,
    load_cookie_file,
    parse_cookies_text,
    parse_json_cookies,
    parse_netscape,
    to_cookiejar,
    to_netscape,
    to_playwright,
    to_playwright_list,
    to_portable,
)
from profilepilot.errors import ProfilePilotError

FUTURE = int(time.time()) + 86400 * 30

# What BrowserContext.cookies() returns (expires -1 = session cookie).
PW_COOKIES = [
    {"name": "sid", "value": "abc123", "domain": "shop.example", "path": "/", "expires": float(FUTURE),
     "httpOnly": True, "secure": True, "sameSite": "Lax"},
    {"name": "pref", "value": "dark mode", "domain": ".example.org", "path": "/app", "expires": -1,
     "httpOnly": False, "secure": False, "sameSite": "None"},
    {"name": "csrf", "value": "x=y;z", "domain": "api.example.net", "path": "/", "expires": float(FUTURE),
     "httpOnly": False, "secure": True, "sameSite": "Strict"},
]


def _portable():
    return [to_portable(c) for c in PW_COOKIES]


def test_playwright_to_portable_shape():
    assert to_portable(PW_COOKIES[0]) == {
        "domain": "shop.example", "name": "sid", "value": "abc123", "path": "/", "expires": FUTURE,
        "secure": True, "httpOnly": True, "sameSite": "Lax",
    }
    session = to_portable(PW_COOKIES[1])
    assert session["expires"] is None and session["sameSite"] == "None"


def test_portable_to_playwright_set_cookie_params():
    params = to_playwright_list(_portable())
    assert params[0] == {"name": "sid", "value": "abc123", "domain": "shop.example", "path": "/", "secure": True,
                         "httpOnly": True, "expires": float(FUTURE), "sameSite": "Lax"}
    assert "expires" not in params[1]  # session cookie
    assert "sameSite" not in params[1]  # SameSite=None without Secure is rejected by Chrome: dropped
    assert params[2]["sameSite"] == "Strict"


def test_browser_extension_and_alias_shapes_are_accepted():
    editor = {  # Cookie-Editor / EditThisCookie export
        "domain": "example.com", "expirationDate": 1893456000.5, "hostOnly": False, "httpOnly": True,
        "name": "a", "path": "/", "sameSite": "no_restriction", "secure": True, "session": False,
        "storeId": "0", "value": "v",
    }
    assert to_portable(editor) == {"domain": ".example.com", "name": "a", "value": "v", "path": "/",
                                   "expires": 1893456000, "secure": True, "httpOnly": True, "sameSite": "None"}
    host_only = dict(editor, domain=".example.com", hostOnly=True, session=True)
    assert to_portable(host_only)["domain"] == "example.com" and to_portable(host_only)["expires"] is None
    alias = {"host": "x.example", "name": "b", "value": 1, "http_only": "true", "same_site": "LAX",
             "expiry": "2030-01-01T00:00:00Z"}
    assert to_portable(alias) == {"domain": "x.example", "name": "b", "value": "1", "path": "/",
                                  "expires": 1893456000, "secure": False, "httpOnly": True, "sameSite": "Lax"}
    from_url = to_portable({"url": "https://Sub.Example.com/path", "name": "c", "value": ""})
    assert from_url["domain"] == "sub.example.com"


@pytest.mark.parametrize("bad,match", [
    ({"domain": "x.com", "value": "v"}, "name"),
    ({"name": "n", "value": "v"}, "domain"),
    ({"name": "n", "domain": "x.com", "expires": "next tuesday"}, "expiry"),
    ("not a cookie", "object"),
])
def test_invalid_cookies_raise_clear_errors(bad, match):
    with pytest.raises(CookieFormatError, match=match):
        to_portable(bad)


def test_list_errors_name_the_offending_entry():
    with pytest.raises(CookieFormatError, match="Cookie #2"):
        to_playwright_list([PW_COOKIES[0], {"name": "x"}])
    assert issubclass(CookieFormatError, ProfilePilotError)


def test_json_file_round_trip(tmp_path):
    path = export_cookies(PW_COOKIES, tmp_path / "out" / "cookies.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == _portable()
    assert not list(path.parent.glob(".*.tmp"))  # atomic write leaves no temp file
    assert load_cookie_file(path) == to_playwright_list(_portable())


def test_netscape_file_round_trip(tmp_path):
    path = export_cookies(PW_COOKIES, tmp_path / "cookies.txt")
    text = path.read_text(encoding="utf-8")
    assert text.startswith(NETSCAPE_HEADER)
    lines = [l for l in text.splitlines() if l and not l.startswith("# ")]
    assert lines[0] == "\t".join(["#HttpOnly_shop.example", "FALSE", "/", "TRUE", str(FUTURE), "sid", "abc123"])
    assert lines[1] == "\t".join([".example.org", "TRUE", "/app", "FALSE", "0", "pref", "dark mode"])
    back = parse_netscape(text)
    for original, parsed in zip(_portable(), back):
        assert parsed == dict(original, sameSite=None)  # cookies.txt has no SameSite column
    assert load_cookie_file(path)[0]["httpOnly"] is True


def test_format_is_detected_from_content_not_extension(tmp_path):
    json_in_txt = tmp_path / "really-json.txt"
    json_in_txt.write_text(dumps_cookies(PW_COOKIES, "json"), encoding="utf-8")
    netscape_in_json = tmp_path / "really-netscape.json"
    netscape_in_json.write_text(to_netscape(PW_COOKIES), encoding="utf-8")
    assert load_cookie_file(json_in_txt) == to_playwright_list(_portable())
    assert [c["name"] for c in load_cookie_file(netscape_in_json)] == ["sid", "pref", "csrf"]
    assert detect_format('﻿  [{"name": "a"}]') == "json"
    assert detect_format("# Netscape HTTP Cookie File\n") == "netscape"
    assert detect_format("example.com\tFALSE\t/\tFALSE\t0\ta\tb") == "netscape"


def test_load_handles_bom_and_utf16(tmp_path):
    path = tmp_path / "c.json"
    path.write_bytes(json.dumps(_portable()).encode("utf-16"))
    assert len(load_cookie_file(path)) == 3
    path.write_bytes(b"\xef\xbb\xbf" + to_netscape(PW_COOKIES).encode())
    assert len(load_cookie_file(path)) == 3


def test_storage_state_and_single_object_json():
    state = {"cookies": PW_COOKIES, "origins": [{"origin": "https://x", "localStorage": []}]}
    assert parse_json_cookies(state) == _portable()
    assert parse_json_cookies(PW_COOKIES[0]) == _portable()[:1]
    with pytest.raises(CookieFormatError):
        parse_json_cookies({"something": "else"})
    with pytest.raises(CookieFormatError):
        parse_json_cookies("nope")


def test_netscape_parser_tolerates_real_world_files():
    text = (
        "# Netscape HTTP Cookie File\n"
        "# https://curl.se/docs/http-cookies.html\n"
        "\n"
        ".example.com\tTRUE\t/\tFALSE\t0\tsession\t\n"  # empty value
        "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t1893456000\ttok\tval with spaces\n"
        "example.com\tFALSE\t/x\tFALSE\t1893456000\tplain\tv\r\n"
    )
    cookies = parse_cookies_text(text)
    assert [(c["domain"], c["name"], c["value"], c["expires"], c["httpOnly"]) for c in cookies] == [
        (".example.com", "session", "", None, False),
        (".example.com", "tok", "val with spaces", 1893456000, True),
        ("example.com", "plain", "v", 1893456000, False),
    ]
    with pytest.raises(CookieFormatError, match="line 2"):
        parse_netscape("# header\nonly\tthree\tfields\n")
    with pytest.raises(CookieFormatError, match="expiry"):
        parse_netscape("a.com\tFALSE\t/\tFALSE\tsoon\tn\tv\n")


def test_values_that_cannot_be_written_as_cookies_txt():
    with pytest.raises(CookieFormatError, match="tabs or newlines"):
        to_netscape([{"domain": "a.com", "name": "n", "value": "a\tb"}])


def test_invalid_json_and_unknown_format(tmp_path):
    with pytest.raises(CookieFormatError, match="Invalid JSON"):
        parse_cookies_text("[{oops")
    with pytest.raises(CookieFormatError):
        dumps_cookies(PW_COOKIES, "xml")  # type: ignore[arg-type]
    with pytest.raises(CookieFormatError, match="not found"):
        load_cookie_file(tmp_path / "missing.json")


def test_cookiejar_round_trip_and_request_header():
    jar = to_cookiejar(PW_COOKIES)
    assert isinstance(jar, http.cookiejar.CookieJar)
    back = sorted(from_cookiejar(jar), key=lambda c: c["name"])
    assert back == sorted(_portable(), key=lambda c: c["name"])

    req = urllib.request.Request("https://shop.example/cart")
    jar.add_cookie_header(req)
    assert req.get_header("Cookie") == "sid=abc123"
    req = urllib.request.Request("http://www.example.org/app/settings")
    jar.add_cookie_header(req)
    assert req.get_header("Cookie") == "pref=dark mode"  # domain cookie matches subdomains
    req = urllib.request.Request("http://api.example.net/")
    jar.add_cookie_header(req)
    assert req.get_header("Cookie") is None  # secure cookie never sent over http


def test_model_facing_summary_never_includes_values_by_default():
    summary = cookie_summary(PW_COOKIES)
    assert all("value" not in c for c in summary)
    assert summary[0]["value_length"] == 6 and summary[1]["expires"] == "session"
    assert summary[0]["expires"].endswith("Z")
    assert "abc123" not in json.dumps(summary)
    assert cookie_summary(PW_COOKIES, names_only=True)[2] == {"domain": "api.example.net", "name": "csrf"}
    assert cookie_summary(PW_COOKIES, include_values=True)[0]["value"] == "abc123"
    expired = cookie_summary([dict(PW_COOKIES[0], expires=1000.0)])[0]
    assert expired["expired"] is True


def test_domain_filtering():
    assert domain_matches(".example.org", "example.org")
    assert domain_matches("www.example.org", "example.org")
    assert not domain_matches("badexample.org", "example.org")
    assert not domain_matches("example.org", "")
    assert [c["name"] for c in filter_cookies(PW_COOKIES, domain="example.org")] == ["pref"]
    assert len(filter_cookies(PW_COOKIES)) == 3
