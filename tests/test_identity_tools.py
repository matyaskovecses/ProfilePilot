"""Identity and form autofill MCP tools: identity CRUD and its sensitive-field policy, profile
links, the sensitive-autofill origin policy (checked before any secret is read), the navigation
guard, remote-mode registration, and Chrome end-to-end runs through the MCP client against the
fixture forms in ``tests/fixtures/autofill``.

All values are obvious test data: card 4242 4242 4242 4242 (the public Stripe test number), SSN
000-12-3456, CVV 123, ``Testy McTestface``, ``example.test`` addresses. Only
``test_paste_tools_with_the_real_clipboard`` touches the real clipboard, inside
:func:`tests.fakes.user_clipboard_guard` (the user's clipboard is saved and always restored).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import psutil
import pytest
from mcp import Client
from mcp.types import TextContent

from profilepilot.automation import clipboard
from profilepilot.automation.autofill import AutofillReport
from profilepilot.automation.clipboard import ClipboardUnavailable
from profilepilot.automation.manager import BrowserManager
from profilepilot.errors import PolicyError, ProfilePilotError
from profilepilot.identity import IdentityStore
from profilepilot.secrets import SecretStore
from profilepilot.server import tools_identity
from profilepilot.server.app import create_server
from profilepilot.server.http import build_http_app, describe_plan
from profilepilot.server.tools_identity import autofill_on_origin, kind_is_sensitive, wanted_keys
from profilepilot.store import Store

from .fakes import OriginServer, clipboard_text_now, pages_from_dir, user_clipboard_guard

FIXTURES = Path(__file__).parent / "fixtures" / "autofill"
TEST_CARD = "4242424242424242"
TEST_CARD_SPACED = "4242 4242 4242 4242"
TEST_SSN = "000-12-3456"
TEST_SSN_DIGITS = "000123456"
SECRETS = (TEST_CARD, TEST_CARD_SPACED, TEST_SSN, TEST_SSN_DIGITS)
PUBLIC = {"first_name": "Testy", "last_name": "McTestface", "email": "testy@example.test", "zip": "12345",
          "phone": "+1 555 010 0000", "city": "Testville", "country_code": "US", "dob": "1990-03-14"}


def text_of(result: Any) -> str:
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


class Recorder:
    """Calls tools and keeps every output, so a test can assert that no secret ever leaked."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.outputs: list[str] = []

    async def call(self, name: str, args: dict[str, Any] | None = None, *, ok: bool = True) -> str:
        result = await self.client.call_tool(name, args or {})
        out = text_of(result)
        self.outputs.append(out)
        assert result.is_error is (not ok), f"{name}: {out}"
        assert "Traceback" not in out
        return out

    def assert_no_secrets(self) -> None:
        blob = "\n".join(self.outputs)
        for secret in SECRETS:
            assert secret not in blob, f"{secret!r} leaked into a tool output"


def add_secrets(ids: IdentityStore, name: str = "Testy") -> None:
    """What the user does in a terminal (``profilepilot identity secret``)."""
    ids.set_sensitive(name, "card_number", TEST_CARD_SPACED)
    ids.set_sensitive(name, "card_exp_month", "4")
    ids.set_sensitive(name, "card_exp_year", "2031")
    ids.set_sensitive(name, "card_cvv", "123")
    ids.set_sensitive(name, "ssn", TEST_SSN)


@pytest.fixture
def home(tmp_path) -> Store:
    store = Store(tmp_path / "home")
    config = store.load_config()
    config.default_window = "offscreen"  # never pop windows up on the user's screen
    store.save_config(config)
    return store


class NoClipboard:
    """Fails like a busy clipboard, so nothing ever reaches the user's real clipboard."""

    name = "none"

    def snapshot_and_set(self, text, *, sensitive):
        raise ClipboardUnavailable("The clipboard is held open by another program.")

    def restore(self, snapshot, token):  # pragma: no cover
        return True


@pytest.fixture
def uses_real_clipboard():
    """Opt-in marker fixture: the test brings its own :func:`user_clipboard_guard`."""


@pytest.fixture(autouse=True)
def _no_real_clipboard(request):
    if "uses_real_clipboard" in request.fixturenames:
        yield
        return
    clipboard.set_backend(NoClipboard())
    try:
        yield
    finally:
        clipboard.set_backend(None)


class SecretReads:
    """Records every identity secret read from the secret store (keys only)."""

    def __init__(self, monkeypatch) -> None:
        self.keys: list[str] = []
        original = SecretStore.get

        def spy(store_self, key, *args, **kwargs):
            if str(key).startswith("identity:"):
                self.keys.append(key)
            return original(store_self, key, *args, **kwargs)

        monkeypatch.setattr(SecretStore, "get", spy)


# ---------------------------------------------------------------------- pure helpers


def test_field_arguments_and_sensitive_kinds():
    assert wanted_keys(None) is None
    assert wanted_keys(["ZIP", "dob", "cvv"]) == {"postal_code", "birth_date", "card_cvv"}
    assert wanted_keys(["card_exp"]) == {"card_exp_month", "card_exp_year"}  # a detected kind
    with pytest.raises(ProfilePilotError, match="Unknown field") as exc:
        wanted_keys(["favourite colour", TEST_CARD])
    assert TEST_CARD not in str(exc.value) and "favourite colour" in str(exc.value)
    with pytest.raises(ProfilePilotError, match="empty"):
        wanted_keys(["  "])
    assert kind_is_sensitive("card_number") and kind_is_sensitive("card_exp") and kind_is_sensitive("ssn")
    assert not kind_is_sensitive("card_name") and not kind_is_sensitive("card_type") and not kind_is_sensitive("email")


def test_store_links_profiles_to_identities(home):
    ids = IdentityStore(home)
    ident = ids.create("Testy", {"first_name": "Testy"})
    profile = home.create_profile("p", identity_id="testy")  # a name (any case), id or id prefix
    assert profile.identity_id == ident.id and profile.summary()["identity_id"] == ident.id
    assert "identity_id" not in home.create_profile("q").summary()
    with pytest.raises(ProfilePilotError, match="not found"):
        home.update_profile("p", identity_id="nobody")
    assert home.get_profile("p").identity_id == ident.id  # unchanged after the failed update
    assert home.update_profile("p", identity_id="").identity_id is None
    assert home.update_profile("p", identity_id=ident.id[:4]).identity_id == ident.id
    assert home.update_profile("p", notes="x").identity_id == ident.id  # other updates keep the link
    assert [p.name for p in home.profiles_using_identity(ident.id)] == ["p"]
    ids.delete("Testy")
    assert home.clone_profile("p", "p-copy").identity_id is None  # a dead link is not copied


# ---------------------------------------------------------------------- identity tools (no browser)


@pytest.mark.asyncio
async def test_identity_tools_keep_sensitive_values_out_of_model_hands(home):
    ids = IdentityStore(home)
    async with Client(create_server(store=home)) as client:
        rec = Recorder(client)
        assert "No identities yet" in await rec.call("identity_list")

        out = await rec.call("identity_create", {"name": "Testy", "fields": {**PUBLIC, "zip": 12345}})
        assert "Created identity 'Testy'" in out and "profilepilot identity secret Testy <field>" in out
        assert ids.get("Testy").values["postal_code"] == "12345"

        refused = await rec.call("identity_create", {"name": "Other", "fields": {"first_name": "A", "ssn": TEST_SSN,
                                                                                "cvv": "123"}}, ok=False)
        assert "profilepilot identity secret Other ssn" in refused and "profilepilot identity secret Other card_cvv" \
            in refused and "Nothing was saved" in refused
        assert [i.name for i in ids.list()] == ["Testy"]

        refused = await rec.call("identity_update", {"identity": "Testy", "fields": {"card_number": TEST_CARD}},
                                 ok=False)
        assert "profilepilot identity secret Testy card_number" in refused
        out = await rec.call("identity_update", {"identity": "Testy", "fields": {"company": "Example Test Co",
                                                                                 "city": None}, "notes": "test"})
        assert "set company" in out and "removed city" in out
        assert "city" not in ids.get("Testy").values
        bad = await rec.call("identity_update", {"identity": "Testy", "fields": {"email": "nope"}}, ok=False)
        assert "not a valid email" in bad
        # a value passed as a key is never echoed back (nor logged by the SDK)
        unknown = await rec.call("identity_update", {"identity": "Testy", "fields": {TEST_CARD: "x"}}, ok=False)
        assert "Unknown identity field (a value that is not a field name)" in unknown
        unknown = await rec.call("identity_create", {"name": "Other", "fields": {TEST_SSN: "ssn"}}, ok=False)
        assert "Unknown identity field" in unknown
        assert "Unknown identity field 'favourite colour'" in await rec.call(
            "identity_update", {"identity": "Testy", "fields": {"favourite colour": "x"}}, ok=False)
        # a card number or SSN given as a non-sensitive value is refused (it would be stored in clear)
        for field, value in (("company", TEST_CARD_SPACED), ("city", TEST_SSN)):
            refused = await rec.call("identity_update", {"identity": "Testy", "fields": {field: value}}, ok=False)
            assert "profilepilot identity secret" in refused and "Nothing was saved" in refused
        assert ids.get("Testy").values.get("company") == "Example Test Co"

        add_secrets(ids)  # the user, in a terminal
        shown = await rec.call("identity_show", {"identity": "Testy"})
        assert "card_number: visa •••• 4242 (sensitive)" in shown and "ssn: •••-••-3456 (sensitive)" in shown
        assert "first_name: Testy" in shown and "not allowed on any site yet" in shown
        assert "profilepilot identity allow Testy <origin>" in shown
        listing = await rec.call("identity_list")
        assert "Testy" in listing and "sensitive stored: card_number" in listing

        out = await rec.call("profile_create", {"name": "p", "identity": "Testy"})
        assert "Identity for form autofill: Testy." in out
        assert home.get_profile("p").identity_id == ids.get("Testy").id
        assert "identity Testy" in await rec.call("profile_list")
        assert "Linked profiles: p." in await rec.call("identity_show", {"identity": "Testy"})
        await rec.call("profile_update", {"profile": "p", "identity": "nobody"}, ok=False)
        out = await rec.call("profile_update", {"profile": "p", "identity": ""})
        assert "No identity is linked any more" in out and home.get_profile("p").identity_id is None
        await rec.call("profile_update", {"profile": "p", "identity": "Testy"})
        clone = home.clone_profile("p", "p2")
        assert clone.identity_id == ids.get("Testy").id
        rec.assert_no_secrets()


@pytest.mark.asyncio
async def test_form_tools_explain_missing_identities_without_starting_chrome(home, monkeypatch):
    async def no_browser(self, ref, **kw):
        raise AssertionError("no browser should be started")

    monkeypatch.setattr(BrowserManager, "session", no_browser)
    home.create_profile("p")
    async with Client(create_server(store=home)) as client:
        rec = Recorder(client)
        out = await rec.call("form_autofill", {"profile": "p"}, ok=False)
        assert "has no linked identity" in out and "identity_create" in out
        IdentityStore(home).create("Testy", {"first_name": "Testy"})
        out = await rec.call("form_autofill", {"profile": "p"}, ok=False)
        assert "profile_update(profile='p', identity=<name>)" in out and "Existing identities: Testy" in out
        out = await rec.call("form_autofill", {"profile": "p", "identity": "Testy", "fields": ["ssn"]}, ok=False)
        assert "only filled by form_autofill_sensitive" in out
        out = await rec.call("form_autofill_sensitive", {"profile": "p", "identity": "Testy"}, ok=False)
        assert "no sensitive value stored" in out and "profilepilot identity secret Testy <field>" in out
        await rec.call("form_autofill", {"profile": "shardx:x"}, ok=False)


class FakePage:
    """Just enough of a Playwright page for the policy checks and the navigation guard."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.main_frame = SimpleNamespace(url=url)
        self.handlers: dict[str, list[Any]] = {}

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "Pay"

    async def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        pass

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        self.handlers[event].remove(handler)

    def navigate(self, url: str) -> None:
        self.url = self.main_frame.url = url
        for handler in list(self.handlers.get("framenavigated", [])):
            handler(self.main_frame)


@pytest.mark.asyncio
async def test_sensitive_autofill_checks_the_origin_before_reading_any_secret(home, monkeypatch):
    ids = IdentityStore(home)
    ids.create("Testy", {"first_name": "Testy", "email": "testy@example.test", "zip": "12345"})
    add_secrets(ids)
    home.create_profile("p", identity_id="Testy")
    page = FakePage("https://evil.example.test/pay")

    async def fake_session(self, ref, **kw):
        async def get_page(tab=None, *, interactive=True):
            return page

        return SimpleNamespace(label="p", key="p-id", page=get_page, drain_new_tabs=list, drain_dialogs=list)

    monkeypatch.setattr(BrowserManager, "session", fake_session)
    filled: list[dict[str, str]] = []

    async def fake_autofill(page_, values, **kw):
        filled.append(dict(values))
        return AutofillReport()

    monkeypatch.setattr(tools_identity, "autofill", fake_autofill)
    reads = SecretReads(monkeypatch)
    async with Client(create_server(store=home)) as client:
        rec = Recorder(client)
        # 'fields' with no sensitive key is refused (never widened to every secret), before anything is read
        out = await rec.call("form_autofill_sensitive", {"profile": "p", "fields": ["phone"]}, ok=False)
        assert "form_autofill" in out and "names no card, SSN or password field" in out
        assert reads.keys == [] and filled == []
        out = await rec.call("form_autofill_sensitive", {"profile": "p"}, ok=False)
        assert "not allowed on https://evil.example.test" in out
        assert 'profilepilot identity allow "Testy" https://evil.example.test' in out
        page.url = "http://shop.example.test/pay"
        ids.allow_origin("Testy", "http://shop.example.test")
        assert "requires HTTPS" in await rec.call("form_autofill_sensitive", {"profile": "p"}, ok=False)
        assert reads.keys == [] and filled == []  # refused before any secret was read

        page.url = page.main_frame.url = "https://shop.example.test/pay"
        ids.allow_origin("Testy", "https://shop.example.test")
        out = await rec.call("form_autofill_sensitive", {"profile": "p", "fields": ["card_number", "zip"]})
        assert out.startswith("[p] Pay — https://shop.example.test/pay")
        assert "Sensitive autofill from identity 'Testy' on https://shop.example.test (paste)" in out
        assert reads.keys and len(filled) == 1  # only what was asked for reached the engine
        assert filled[0]["card_number"] == TEST_CARD and filled[0]["postal_code"] == "12345"
        assert "ssn" not in filled[0] and "card_cvv" not in filled[0] and "email" not in filled[0]
        rec.assert_no_secrets()


@pytest.mark.asyncio
async def test_autofill_on_origin_stops_when_the_page_leaves(monkeypatch):
    events: list[str] = []

    async def leaves(page, values, **kw):
        events.append("start")
        page.navigate("https://shop.example.test/next")  # same origin: keeps going
        await asyncio.sleep(0)
        page.navigate("https://evil.example.test/")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        return AutofillReport()  # pragma: no cover

    monkeypatch.setattr(tools_identity, "autofill", leaves)
    page = FakePage("https://shop.example.test/pay")
    with pytest.raises(PolicyError, match="moved to https://evil.example.test"):
        await autofill_on_origin(page, "https://shop.example.test", {"card_number": TEST_CARD})
    assert events == ["start", "cancelled"] and page.handlers["framenavigated"] == []

    async def finishes(page, values, **kw):
        return AutofillReport(filled=[{"kind": "card_number", "field": "input", "method": "fill"}])

    monkeypatch.setattr(tools_identity, "autofill", finishes)
    page = FakePage("https://shop.example.test/pay")
    report = await autofill_on_origin(page, "https://shop.example.test", {})
    assert len(report.filled) == 1 and page.handlers["framenavigated"] == []
    page.navigate("https://evil.example.test/")  # after the fill: no listener left, nothing to stop
    with pytest.raises(PolicyError, match="before autofill started"):
        await autofill_on_origin(FakePage("https://evil.example.test/"), "https://shop.example.test", {})


@pytest.mark.asyncio
async def test_sensitive_autofill_is_not_offered_remotely_unless_enabled(home):
    async with Client(create_server(store=home, remote=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "form_autofill_sensitive" not in names and {"form_autofill", "form_detect", "identity_show"} <= names
    async with Client(create_server(store=home, remote=True, allow_sensitive_autofill=True)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert tools["form_autofill_sensitive"].meta["anthropic/requiresUserInteraction"] is True

    plan = build_http_app(store=home, auth="token", token="t" * 32)
    names = {t.name for t in await plan.server.list_tools()}
    assert "form_autofill" in names and "form_autofill_sensitive" not in names
    assert "--allow-sensitive-autofill enables it" in describe_plan(plan)
    plan = build_http_app(store=home, auth="token", token="t" * 32, allow_sensitive_autofill=True)
    assert "form_autofill_sensitive" in {t.name for t in await plan.server.list_tools()}
    assert "Sensitive autofill (card, SSN, password) is ON" in describe_plan(plan)


# ---------------------------------------------------------------------- real Chrome, through the MCP client


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
def chrome_home(tmp_path) -> Iterator[Store]:
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


@pytest.fixture(scope="module")
def servers():
    """(main origin on 127.0.0.1, card origin on localhost: a different site -> out-of-process iframe,
    widget origin: an untrusted third party)."""
    with OriginServer(pages_from_dir(FIXTURES)) as card, OriginServer(pages_from_dir(FIXTURES)) as widget:
        card_origin, widget_origin = f"http://localhost:{card.port}", f"http://localhost:{widget.port}"
        pages = pages_from_dir(FIXTURES, {"{{CARD_ORIGIN}}": card_origin, "{{WIDGET_ORIGIN}}": widget_origin})
        with OriginServer(pages) as main:
            yield main, card_origin, widget_origin


async def frame_values(store: Store, profile: str, url_prefix: str) -> dict[str, Any]:
    """Field values of the frame whose URL starts with ``url_prefix`` (read over a separate CDP
    connection: cross-origin iframes are out of reach for browser_evaluate)."""
    from playwright.async_api import async_playwright

    from profilepilot.browser.runtime import RuntimeManager

    info = RuntimeManager(store).status(store.get_profile(profile).id)
    assert info is not None
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(info.cdp_http_url, no_defaults=True)
        try:
            for _ in range(50):  # out-of-process iframes are attached shortly after connecting
                for page in browser.contexts[0].pages:
                    for frame in page.frames:
                        # an out-of-process iframe found by a late connection reports url '' until it
                        # navigates again: ask the document itself
                        with contextlib.suppress(Exception):
                            data = await frame.evaluate(
                                "() => [location.href, Object.fromEntries([...document.querySelectorAll("
                                "'input,select')].map(e => [e.name || e.id, e.value]))]")
                            if data[0].startswith(url_prefix):
                                return data[1]
                await asyncio.sleep(0.1)
        finally:
            await browser.close()  # only disconnects
    raise AssertionError(f"no frame at {url_prefix}")


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_detect_autofill_and_sensitive_autofill_end_to_end(chrome_home, servers, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="profilepilot")
    main, card_origin, _ = servers
    ids = IdentityStore(chrome_home)
    ids.create("Testy", {k: v for k, v in PUBLIC.items()})
    add_secrets(ids)
    reads = SecretReads(monkeypatch)
    async with Client(create_server(store=chrome_home)) as client:
        rec = Recorder(client)
        await rec.call("profile_create", {"name": "shop", "identity": "Testy"})
        await rec.call("browser_navigate", {"profile": "shop", "url": f"{main.url}/checkout.html", "wait_until": "load"})

        detected = await rec.call("form_detect", {"profile": "shop"})
        assert "- email: " in detected and "- card_name: " in detected and "- postal_code: " in detected
        assert f"- card_number: input \"Credit or debit card number\" (text) [in frame {card_origin}] [sensitive]" \
            in detected
        assert "- card_exp: " in detected and "- card_cvv: " in detected and "form_autofill_sensitive" in detected

        out = await rec.call("form_autofill", {"profile": "shop", "method": "type"})
        assert "3 field(s) filled, 3 skipped" in out and "sensitive field (not included in this fill)" in out
        assert "call form_autofill_sensitive" in out and "Nothing was submitted" in out
        assert await frame_values(chrome_home, "shop", main.url) == {
            "receipt": "testy@example.test", "ccname": "Testy McTestface", "billing_zip": "12345"}
        assert reads.keys == []  # the non-sensitive tool never reads a secret

        refused = await rec.call("form_autofill_sensitive", {"profile": "shop", "method": "fill"}, ok=False)
        assert f"not allowed on {main.url}" in refused and f"profilepilot identity allow \"Testy\" {main.url}" in refused
        assert reads.keys == []  # refused before any secret was read
        assert (await frame_values(chrome_home, "shop", card_origin))["cardnumber"] == ""

        ids.allow_origin("Testy", main.url)  # the user, in a terminal
        # the card fields live in a frame of another origin that is no known payment processor: refused
        out = await rec.call("form_autofill_sensitive", {"profile": "shop", "method": "fill"})
        assert "0 field(s) filled, 3 skipped" in out
        assert f"sensitive field in a third-party frame ({card_origin})" in out
        assert f"profilepilot identity allow Testy {card_origin}" in out
        assert (await frame_values(chrome_home, "shop", card_origin))["cardnumber"] == ""
        ids.allow_origin("Testy", card_origin)  # the user trusts their card processor's frame
        out = await rec.call("form_autofill_sensitive", {"profile": "shop", "method": "fill"})
        assert f"Sensitive autofill from identity 'Testy' on {main.url} (fill): 3 field(s) filled" in out
        assert f"[in frame {card_origin}]" in out and "form_detect (values are not shown)" in out
        assert await frame_values(chrome_home, "shop", card_origin) == {
            "cardnumber": TEST_CARD_SPACED, "exp-date": "04 / 31", "cvc": "123"}

        await rec.call("browser_navigate", {"profile": "shop", "url": f"{main.url}/split.html", "wait_until": "load"})
        out = await rec.call("form_autofill_sensitive", {"profile": "shop", "fields": ["ssn"], "method": "type"})
        assert "(part 2/3)" in out
        values = await frame_values(chrome_home, "shop", f"{main.url}/split.html")
        assert (values["s1"], values["s2"], values["s3"]) == ("000", "12", "3456")
        assert values["c1"] == "" and values["cvv"] == "" and values["p1"] == ""  # only what was asked for

        rec.assert_no_secrets()
    for secret in SECRETS:
        assert secret not in caplog.text, f"{secret!r} was logged"


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_paste_tools_with_the_real_clipboard(chrome_home, servers, uses_real_clipboard):
    """browser_paste and form_autofill_sensitive (default method: paste) through the MCP client with
    the real system clipboard; the user's clipboard is saved first and always restored."""
    main, card_origin, _ = servers
    ids = IdentityStore(chrome_home)
    ids.create("Testy", {"first_name": "Testy"})
    add_secrets(ids)
    ids.allow_origin("Testy", main.url)
    ids.allow_origin("Testy", card_origin)
    async with Client(create_server(store=chrome_home)) as client:
        rec = Recorder(client)
        await rec.call("profile_create", {"name": "shop", "identity": "Testy"})
        await rec.call("browser_navigate", {"profile": "shop", "url": f"{main.url}/checkout.html", "wait_until": "load"})
        await rec.call("browser_type", {"profile": "shop", "selector": "input[name=ccname]", "text": "Testy",
                                        "method": "human"})
        with user_clipboard_guard("PP-TEST-SENTINEL-TOOLS", image=False) as backend:
            out = await rec.call("browser_paste", {"profile": "shop", "selector": "input[name=billing_zip]",
                                                   "text": "12345"})
            assert "Pasted 5 character(s) into selector 'input[name=billing_zip]'" in out
            out = await rec.call("form_autofill_sensitive", {"profile": "shop", "fields": ["card_number"]})
            assert clipboard_text_now(backend) == "PP-TEST-SENTINEL-TOOLS"
        assert "(paste): 1 field(s) filled" in out and "via paste" in out
        main_values = await frame_values(chrome_home, "shop", main.url)
        assert main_values["ccname"] == "Testy" and main_values["billing_zip"] == "12345"
        card = await frame_values(chrome_home, "shop", card_origin)
        assert card == {"cardnumber": TEST_CARD_SPACED, "exp-date": "", "cvc": ""}
        rec.assert_no_secrets()
    assert json.dumps(rec.outputs).count("PP-TEST-SENTINEL") == 0


# ---------------------------------------------------------------------- frame policy, redaction, hints (unit)


def test_frame_origin_policy_for_sensitive_values():
    from profilepilot.server.tools_identity import frame_origin_policy

    allows = frame_origin_policy("https://shop.example.test", ["https://shop.example.test", "https://pay.example.test",
                                                              "http://insecure.example.test"])
    assert allows("ssn", "https://shop.example.test") and allows("password", "https://shop.example.test:443")
    assert allows("ssn", "https://pay.example.test")  # the user allow-listed that frame origin
    assert not allows("card_number", "http://insecure.example.test")  # allow-listed, but not https
    assert allows("card_number", "https://js.stripe.com") and allows("card_cvv", "https://assets.braintreegateway.com")
    assert allows("card_exp", "https://checkoutshopper-live.adyen.com")
    assert not allows("ssn", "https://js.stripe.com") and not allows("password", "https://www.paypal.com")
    assert not allows("card_number", "http://js.stripe.com")  # payment processors only over https
    assert not allows("card_number", "https://js.stripe.com.evil.example.test")
    assert not allows("card_number", "https://evilstripe.com") and not allows("card_number", "https://notpaypal.com")
    for bad in ("null", "", "about:blank", "data:text/html,x"):
        assert not allows("card_number", bad)


def test_secret_variants_and_redaction():
    from profilepilot.server.app import REDACTED, AppState
    from profilepilot.server.tools_identity import secret_variants

    variants = secret_variants({"card_number": TEST_CARD, "ssn": TEST_SSN, "password": "Test-Only-Pass-1",
                                "card_cvv": "123", "card_exp_month": "04", "postal_code": "12345"},
                               typed=["822463", "3456"])
    assert {TEST_CARD, TEST_CARD_SPACED, "4242-4242-4242-4242", TEST_SSN, TEST_SSN_DIGITS, "000 12 3456",
            "Test-Only-Pass-1", "822463"} <= variants
    assert not variants & {"123", "04", "3456", "12345", "4242"}  # short or not sensitive: never redacted
    amex = secret_variants({"card_number": "378282246310005"})  # public test Amex number
    assert {"3782 822463 10005", "822463", "10005"} <= amex

    state = AppState(store=None, runtime=None, browsers=None, policy=None)  # type: ignore[arg-type]
    state.remember_secrets("p1", variants, "https://shop.example.test/pay#step2")
    page = f'textbox "Card" [ref=e12]: {TEST_CARD_SPACED}\ne123 SSN {TEST_SSN_DIGITS} pw Test-Only-Pass-1 zip 12345'
    redacted = state.redact("p1", page)
    assert TEST_CARD_SPACED not in redacted and TEST_SSN_DIGITS not in redacted and "Test-Only-Pass" not in redacted
    assert redacted.count(REDACTED) == 3 and "[ref=e12]" in redacted and "e123" in redacted and "12345" in redacted
    assert state.redact("other", page) == page  # per profile
    assert state.holds_secrets("p1", "https://shop.example.test/pay") and not state.holds_secrets("p1", "https://x.test/")
    state.forget_secrets("p1")
    assert state.redact("p1", page) == page and not state.holds_secrets("p1", "https://shop.example.test/pay")


def test_report_hints_name_what_to_do_without_values():
    from profilepilot.automation.autofill import COUNTRY_MISMATCH, NOT_STORED, NOT_VISIBLE
    from profilepilot.identity import Identity
    from profilepilot.server.tools_identity import report_hints

    ident = Identity(id="abcd1234", name="Testy")
    report = AutofillReport(skipped=[
        {"kind": "country", "field": 'select "Country"', "reason": COUNTRY_MISMATCH},
        {"kind": "company", "field": 'input "Org"', "reason": NOT_VISIBLE},
        {"kind": "ssn", "field": 'input "SSN"', "reason": NOT_STORED},
        {"kind": "card_number", "field": "input", "frame": "https://widget.example.test",
         "reason": "sensitive field in a third-party frame (https://widget.example.test)"},
    ])
    hints = "\n".join(report_hints(report, ident, sensitive_tool=True))
    assert "fields=['country', 'state'], overwrite=true" in hints
    assert "not visible" in hints and "profilepilot identity secret Testy ssn" in hints
    assert "profilepilot identity allow Testy https://widget.example.test" in hints
    plain = "\n".join(report_hints(report, ident, sensitive_tool=False))
    assert "identity allow" not in plain and "identity secret" not in plain and "preselected country" in plain


# ---------------------------------------------------------------------- reading sensitive values back (real Chrome)

TEST_PASSWORD = "Test-Only-Pass-1"


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_sensitive_values_never_come_back_through_page_reads(chrome_home, servers):
    """After form_autofill_sensitive, snapshots mask the sensitive fields (also in the card iframe),
    every page read is redacted, the third-party widget gets nothing, and in remote mode JavaScript
    and screenshots are refused on that page until it navigates away."""
    main, card_origin, widget_origin = servers
    ids = IdentityStore(chrome_home)
    ids.create("Testy", dict(PUBLIC))
    add_secrets(ids)
    ids.set_sensitive("Testy", "password", TEST_PASSWORD)
    ids.allow_origin("Testy", main.url)
    ids.allow_origin("Testy", card_origin)
    secrets = (*SECRETS, TEST_PASSWORD)
    url = f"{main.url}/secure.html"
    async with Client(create_server(store=chrome_home)) as client:
        rec = Recorder(client)
        await rec.call("profile_create", {"name": "shop", "identity": "Testy"})
        await rec.call("browser_navigate", {"profile": "shop", "url": url, "wait_until": "load"})
        await rec.call("form_autofill", {"profile": "shop", "method": "fill"})
        out = await rec.call("form_autofill_sensitive", {"profile": "shop", "method": "fill"})
        assert "5 field(s) filled, 2 skipped" in out
        assert f"Fields in a frame from {widget_origin} were not filled" in out
        assert "browser_snapshot" not in out  # no longer recommended after a sensitive fill
        assert await frame_values(chrome_home, "shop", widget_origin) == {"cardnumber_3p": "", "ssn_3p": ""}
        main_values = await frame_values(chrome_home, "shop", url)
        assert main_values["ssn_main"] == TEST_SSN and main_values["pw"] == TEST_PASSWORD  # really filled

        snap = await rec.call("browser_snapshot", {"profile": "shop"})
        assert "testy@example.test" in snap  # non-sensitive values stay visible
        assert snap.count("••••") >= 5  # SSN, password, card number, expiry, CVC
        assert not any(line.rstrip().endswith((': "123"', ": 123")) for line in snap.splitlines())
        evaluated = await rec.call("browser_evaluate", {
            "profile": "shop", "expression": "() => [...document.querySelectorAll('input')].map(e => e.value)"})
        assert "[redacted]" in evaluated and "testy@example.test" in evaluated
        thrown = await rec.call("browser_evaluate", {
            "profile": "shop",
            "expression": "() => { throw new Error(document.querySelector('[name=ssn_main]').value) }"}, ok=False)
        assert "[redacted]" in thrown
        read = await rec.call("browser_read", {"profile": "shop", "format": "html"})
        tabs = await rec.call("browser_tabs", {"profile": "shop"})
        for text in (snap, evaluated, thrown, read, tabs):
            for secret in secrets:
                assert secret not in text
        rec.assert_no_secrets()

    # remote mode: no JavaScript or screenshots on a page that holds sensitive values
    async with Client(create_server(store=chrome_home, remote=True, allow_private=True,
                                    allow_sensitive_autofill=True)) as client:
        rec = Recorder(client)
        await rec.call("browser_navigate", {"profile": "shop", "url": url, "wait_until": "load"})
        await rec.call("form_autofill_sensitive", {"profile": "shop", "method": "fill", "fields": ["ssn"]})
        for tool, args in (("browser_evaluate", {"expression": "1 + 1"}), ("browser_screenshot", {})):
            refused = await rec.call(tool, {"profile": "shop", **args}, ok=False)
            assert "disabled on it in remote mode" in refused
        await rec.call("browser_navigate", {"profile": "shop", "url": f"{main.url}/signup.html", "wait_until": "load"})
        assert "Result:\n2" in await rec.call("browser_evaluate", {"profile": "shop", "expression": "1 + 1"})
        rec.assert_no_secrets()
