import ipaddress
import socket

import pytest

from profilepilot.errors import PolicyError
from profilepilot.safety import UrlPolicy, canonical_host, is_private_address, normalize_url, parse_ip_host

LOCAL = UrlPolicy(remote=False)
REMOTE = UrlPolicy(remote=True)
REMOTE_PRIVATE_OK = UrlPolicy(remote=True, allow_private=True)


@pytest.fixture
def fake_dns(monkeypatch):
    """Deterministic DNS: no network, and the test decides what each name resolves to."""
    answers = {
        "evil.example": ["127.0.0.1"],
        "lan.example": ["93.184.216.34", "192.168.1.20"],  # one private answer is enough to block
        "v6loop.example": ["::1"],
        "metadata.example": ["169.254.169.254"],
        "good.example": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    }
    calls = []

    def getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        if host not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        out = []
        for ip in answers[host]:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, port, 0, 0) if fam == socket.AF_INET6 else (ip, port)
            out.append((fam, socket.SOCK_STREAM, 6, "", sockaddr))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return calls


BLOCKED_ALWAYS = [
    "file:///C:/Windows/win.ini",
    "FILE:///etc/passwd",
    "chrome://settings",
    "chrome://version/",
    "chrome-extension://abcdefghijklmnop/popup.html",
    "chrome-untrusted://terminal/",
    "devtools://devtools/bundled/inspector.html",
    "view-source:https://example.com/",
    "  javascript:alert(1)",
    "JavaScript:alert(document.cookie)",
    "filesystem:https://example.com/temporary/x",
    "edge://settings",
    "about:config",
    "ftp://example.com/file",
    "ws://example.com/socket",
]


@pytest.mark.parametrize("url", BLOCKED_ALWAYS)
@pytest.mark.parametrize("policy", [LOCAL, REMOTE, REMOTE_PRIVATE_OK], ids=["local", "remote", "remote-allow-private"])
def test_dangerous_schemes_are_blocked_in_every_mode(policy, url):
    with pytest.raises(PolicyError):
        policy.check(url)


@pytest.mark.parametrize("url", ["", "   ", "example.com", "http://", "https://exa mple.com\n/x"])
def test_malformed_urls_are_rejected(url):
    with pytest.raises(PolicyError):
        LOCAL.check(url)


def test_local_mode_allows_localhost_and_lan(fake_dns):
    for url in ("http://localhost:3000/", "http://127.0.0.1:8080/x", "http://192.168.1.1/", "http://[::1]:5173/",
                "https://example.com/", "about:blank", "data:text/html,<p>hi</p>", "http://evil.example/"):
        LOCAL.check(url)
    assert fake_dns == []  # local mode never needs DNS


PRIVATE_LITERALS = [
    "http://localhost/",
    "http://LOCALHOST.:8080/",
    "http://app.localhost/",
    "http://printer.local/",
    "http://router.home.arpa/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://127.0.0.1/",
    "http://127.1/",
    "http://127.0.0.1.:80/",
    "http://2130706433/",  # decimal
    "http://0x7f000001/",  # hex
    "http://0x7f.1/",
    "http://0177.0.0.1/",  # octal
    "http://017700000001/",
    "http://%31%32%37.0.0.1/",  # percent-encoded
    "http://\uff11\uff12\uff17.\uff10.\uff10.\uff11/",  # full-width digits
    "http://127\u30020\u30020\u30021/",  # ideographic full stops
    "http://user:pw@127.0.0.1:9222/json/version",
    "http://0.0.0.0:9222/",
    "http://0/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://192.168.0.10/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/",  # CGNAT
    "http://224.0.0.1/",
    "http://255.255.255.255/",
    "http://[::1]/",
    "http://[0:0:0:0:0:0:0:1]/",
    "http://[::]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/",
    "http://[::ffff:a9fe:a9fe]/",  # mapped 169.254.169.254
    "http://[64:ff9b::7f00:1]/",  # NAT64 of 127.0.0.1
    "http://[2002:7f00:1::]/",  # 6to4 of 127.0.0.1
    "http://[fe80::1%25eth0]/",
    "http://[fc00::1]/",
    "http://[fd12:3456::1]/",
    "https://1.2.3.999/",  # invalid IPv4: refused rather than guessed
    "http://0x7f.0.0.0x1/",
    "http://0x7F.1/",
]


@pytest.mark.parametrize("url", PRIVATE_LITERALS)
def test_remote_mode_blocks_local_and_private_hosts(url, fake_dns):
    with pytest.raises(PolicyError):
        REMOTE.check(url)


def test_remote_block_message_explains_the_flag(fake_dns):
    with pytest.raises(PolicyError, match="--allow-private-network"):
        REMOTE.check("http://localhost:3000/")
    with pytest.raises(PolicyError, match="127.0.0.1"):
        REMOTE.check("http://2130706433/")


def test_remote_mode_resolves_names_and_blocks_private_answers(fake_dns):
    for url in ("http://evil.example/", "https://lan.example:8443/", "http://v6loop.example/", "http://metadata.example/"):
        with pytest.raises(PolicyError):
            REMOTE.check(url)
    REMOTE.check("https://good.example/path?q=1")
    REMOTE.check("https://unresolvable.example/")  # left to the browser / proxy
    assert "evil.example" in fake_dns and "good.example" in fake_dns


@pytest.mark.asyncio
async def test_async_check_uses_the_same_rules(fake_dns):
    await REMOTE.acheck("https://good.example/")
    with pytest.raises(PolicyError):
        await REMOTE.acheck("http://evil.example/")
    with pytest.raises(PolicyError):
        await REMOTE.acheck("http://[::1]/")
    with pytest.raises(PolicyError):
        await REMOTE.acheck("chrome://settings")


def test_remote_mode_allows_public_literals_without_dns(fake_dns):
    for url in ("https://8.8.8.8/", "http://1.1.1.1:8080/", "https://[2606:4700:4700::1111]/", "about:blank"):
        REMOTE.check(url)
    assert fake_dns == []


def test_remote_mode_blocks_data_urls_unless_private_allowed():
    with pytest.raises(PolicyError):
        REMOTE.check("data:text/html,<script>alert(1)</script>")
    REMOTE_PRIVATE_OK.check("data:text/plain,hi")


def test_allow_private_lifts_only_the_address_restriction(fake_dns):
    for url in ("http://localhost:3000/", "http://192.168.1.1/", "http://evil.example/", "http://[::1]/"):
        REMOTE_PRIVATE_OK.check(url)
    assert REMOTE_PRIVATE_OK.is_allowed("chrome://settings") is False
    assert REMOTE.is_allowed("http://localhost/") is False
    assert REMOTE.is_allowed("https://8.8.8.8/") is True
    assert REMOTE.restricts_private and not LOCAL.restricts_private and not REMOTE_PRIVATE_OK.restricts_private
    assert "remote" in REMOTE.describe() and "local" in LOCAL.describe()


@pytest.mark.parametrize("host,expected", [
    ("127.1", "127.0.0.1"),
    ("0x7f.0x0.0.1", "127.0.0.1"),
    ("3232235777", "192.168.1.1"),
    ("0300.0250.01.01", "192.168.1.1"),
    ("1.2.3.4.", "1.2.3.4"),
    ("[::ffff:1.2.3.4]", "::ffff:102:304"),
    ("example.com", None),
    ("xn--nxasmq6b.example", None),
])
def test_parse_ip_host_follows_browser_rules(host, expected):
    result = parse_ip_host(host)
    assert (str(result) if result is not None else None) == expected


@pytest.mark.parametrize("host", ["1.2.3.256", "1.2.3.4.5", "08.0.0.1", "example.123", "0x.0x.0x.0x100"])
def test_parse_ip_host_rejects_invalid_numeric_hosts(host):
    with pytest.raises(PolicyError):
        parse_ip_host(host)


def test_canonical_host_and_private_ranges():
    assert canonical_host("ExAmPle.COM.") == "example.com"
    assert canonical_host("%6c%6f%63%61%6c%68%6f%73%74") == "localhost"
    assert is_private_address(ipaddress.ip_address("10.0.0.1"))
    assert is_private_address(ipaddress.ip_address("::ffff:192.168.1.1"))
    assert not is_private_address(ipaddress.ip_address("93.184.216.34"))
    assert not is_private_address(ipaddress.ip_address("2606:4700:4700::1111"))


@pytest.mark.parametrize("raw,expected", [
    ("example.com", "https://example.com"),
    ("example.com/path?q=1", "https://example.com/path?q=1"),
    ("//cdn.example.com/x.js", "https://cdn.example.com/x.js"),
    ("localhost:3000", "http://localhost:3000"),
    ("app.localhost/x", "http://app.localhost/x"),
    ("127.0.0.1:8000/api", "http://127.0.0.1:8000/api"),
    ("[::1]:5173", "http://[::1]:5173"),
    ("http://example.com", "http://example.com"),
    ("about:blank", "about:blank"),
    ("chrome://settings", "chrome://settings"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected
    with pytest.raises(PolicyError):
        normalize_url("   ")
