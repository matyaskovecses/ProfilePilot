import pytest

from profilepilot.proxy.url import ProxyEndpoint, ProxyParseError, parse_proxy


@pytest.mark.parametrize(
    "text, expected",
    [
        ("socks5://u:p@1.2.3.4:1080", ProxyEndpoint("socks5", "1.2.3.4", 1080, "u", "p")),
        ("socks5h://u:p@host.example:1080", ProxyEndpoint("socks5", "host.example", 1080, "u", "p")),
        ("http://proxy.example:8080", ProxyEndpoint("http", "proxy.example", 8080)),
        ("https://u:p%40ss@proxy.example:443", ProxyEndpoint("https", "proxy.example", 443, "u", "p@ss")),
        ("socks4a://h:9050", ProxyEndpoint("socks4", "h", 9050)),
        ("1.2.3.4:3128", ProxyEndpoint("http", "1.2.3.4", 3128)),
        ("u:p@1.2.3.4:3128", ProxyEndpoint("http", "1.2.3.4", 3128, "u", "p")),
        ("gate.example:7000:user-zone-1:se:cret", ProxyEndpoint("http", "gate.example", 7000, "user-zone-1", "se:cret")),
        ("socks5://[::1]:1080", ProxyEndpoint("socks5", "::1", 1080)),
    ],
)
def test_parse(text, expected):
    assert parse_proxy(text) == expected


def test_default_scheme_applies_to_bare_forms():
    assert parse_proxy("h:1:u:p", default_scheme="socks5").scheme == "socks5"


@pytest.mark.parametrize("bad", ["", "justhost", "ftp://h:1", "h:notaport", "socks5://h", "h:70000"])
def test_rejects(bad):
    with pytest.raises(ProxyParseError):
        parse_proxy(bad)


def test_round_trip_and_redaction():
    ep = parse_proxy("socks5://us er:p@ss@h:1080")
    assert parse_proxy(ep.to_url()) == ep
    assert ep.to_url(remote_dns=True).startswith("socks5h://")
    assert "p@ss" not in ep.redacted() and "p%40ss" not in ep.redacted()
    assert ProxyEndpoint("socks5", "::1", 1).to_url() == "socks5://[::1]:1"


def test_redacted_hides_the_whole_user():
    """FIX-PLAN step 9 (F10): no part of the user name survives (it used to keep 3 characters)."""
    ep = ProxyEndpoint("socks5", "gate.example", 7000, "customer-alice-zone-us", "se:cret")
    assert ep.redacted() == "socks5://***:***@gate.example:7000"
    for part in ("cus", "alice", "zone", "se:cret", "cret"):
        assert part not in ep.redacted()
    assert ProxyEndpoint("http", "::1", 8080, "u").redacted() == "http://***:***@[::1]:8080"
    assert ProxyEndpoint("http", "h.example", 8080).redacted() == "http://h.example:8080"  # nothing to hide


def test_relay_errors_do_not_name_the_upstream_proxy():
    """The relay's last_error reaches the model (relay hints, profile_status): the upstream's address is
    replaced, the reason is kept."""
    from profilepilot.proxy.relay import upstream_failure

    ep = ProxyEndpoint("socks5", "gate.example", 7000, "alice", "pw")
    exc = ConnectionRefusedError("Couldn't connect to proxy gate.example:7000 [Connection refused]")
    assert upstream_failure(exc, ep) == ("ConnectionRefusedError: Couldn't connect to proxy <upstream proxy> "
                                         "[Connection refused]")
    v6 = ProxyEndpoint("http", "2001:db8::5", 3128)
    assert "2001:db8::5" not in upstream_failure(OSError("proxy [2001:db8::5]:3128 timed out"), v6)
    assert upstream_failure(OSError("no route to x.example:443"), None) == "OSError: no route to x.example:443"
