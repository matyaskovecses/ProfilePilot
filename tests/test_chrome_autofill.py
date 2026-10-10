"""Autofill from the addresses saved in the user's browser (:mod:`profilepilot.chrome_autofill`).

Every test builds its own browser "User Data" folders with ``Web Data`` files in the Chrome 154
schema (``addresses`` + ``address_type_tokens``, plus ``credit_cards`` and the ``autofill`` form
history, which must never be read) and points discovery at them; the autouse fixture in
``conftest.py`` keeps every other test away from the user's real browsers. All values are obvious
test data (``example.test`` addresses, 555-01xx phone numbers).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from profilepilot import chrome_autofill
from profilepilot.automation.manager import BrowserManager
from profilepilot.chrome_autofill import (
    ChromeAddress, discover_sources, is_source_ref, pick_address, read_addresses, resolve_source, source_values,
)
from profilepilot.chrome_autofill import _user_data_dirs as real_user_data_dirs  # before conftest patches it
from profilepilot.errors import AmbiguousError, NotFoundError, ProfilePilotError
from profilepilot.identity import IdentityStore, merge_live
from profilepilot.server.app import create_server
from profilepilot.server.tools_identity import browser_fill_values, resolve_fill_source
from profilepilot.store import Store

from .test_identity_tools import Recorder, chrome_home, frame_values  # noqa: F401 (fixture)

FIXTURES = Path(__file__).parent / "fixtures" / "autofill"
TYPES = {name: number for number, name in chrome_autofill.FIELD_TYPES.items()}
FAKE_CARD = "4242424242424242"  # the public Stripe test number
FORM_HISTORY = "typed-into-a-search-box@example.test"

ADA = {"guid": "ada-0001-guid", "uses": 5, "first_name": "Ada", "last_name": "Lovelace", "full_name": "Ada Lovelace",
       "email": "ada@example.test", "phone": "+1 555 010 0199", "company": "Analytical Engines",
       "street_address": "12 Example Street\nApt 3", "city": "Phoenix", "state": "AZ", "postal_code": "85001",
       "country_code": "US"}
BOB = {"guid": "bob-0002-guid", "uses": 1, "first_name": "Bob", "last_name": "Example", "email": "bob@example.test",
       "street_address": "Teststrasse 1", "city": "Berlin", "postal_code": "10115", "country_code": "DE"}
ADA_SUMMARY = "Ada Lovelace - Phoenix, AZ, US"
PRIVATE = ("ada@example.test", "Example Street", "555 010 0199", "85001", "bob@example.test", "Teststrasse")

SCHEMA = """
CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
CREATE TABLE addresses (guid VARCHAR PRIMARY KEY, use_count INTEGER NOT NULL DEFAULT 0,
  use_date INTEGER NOT NULL DEFAULT 0, date_modified INTEGER NOT NULL DEFAULT 0, language_code VARCHAR,
  label VARCHAR, initial_creator_id INTEGER DEFAULT 0, record_type INTEGER);
CREATE TABLE address_type_tokens (guid VARCHAR, type INTEGER, value VARCHAR,
  verification_status INTEGER DEFAULT 0, observations BLOB, PRIMARY KEY (guid, type));
CREATE TABLE credit_cards (guid VARCHAR PRIMARY KEY, name_on_card VARCHAR, expiration_month INTEGER,
  expiration_year INTEGER, card_number_encrypted BLOB, use_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE autofill (name VARCHAR, value VARCHAR, value_lower VARCHAR, count INTEGER DEFAULT 1,
  PRIMARY KEY (name, value));
"""


def write_web_data(path: Path, addresses: list[dict[str, Any]]) -> Path:
    """A ``Web Data`` file like Chrome 154 writes it (schema version 154)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        con.execute("INSERT INTO meta VALUES ('version', '154')")
        for n, address in enumerate(addresses):
            con.execute("INSERT INTO addresses (guid, use_count, use_date, record_type, label) VALUES (?,?,?,?,?)",
                        (address["guid"], address.get("uses", 0), 1_700_000_000 + n, address.get("record_type", 0),
                         address.get("label", "")))
            for key, value in address.items():
                if key in TYPES:
                    con.execute("INSERT INTO address_type_tokens (guid, type, value) VALUES (?,?,?)",
                                (address["guid"], TYPES[key], value))
        con.execute("INSERT INTO credit_cards VALUES ('card-1', 'Ada Lovelace', 4, 2031, ?, 9)", (FAKE_CARD.encode(),))
        con.execute("INSERT INTO autofill (name, value, value_lower) VALUES ('q', ?, ?)", (FORM_HISTORY, FORM_HISTORY))
        con.commit()
    finally:
        con.close()
    return path


def make_browser(udd: Path, profiles: dict[str, tuple[str, list[dict[str, Any]] | None]],
                 last_used: str | None = None) -> Path:
    """A browser "User Data" folder: ``profiles`` = folder -> (display name, addresses or None for a
    profile without a Web Data file). ``last_used=None`` writes no Local State at all."""
    for folder, (_name, addresses) in profiles.items():
        (udd / folder).mkdir(parents=True, exist_ok=True)
        if addresses is not None:
            write_web_data(udd / folder / "Web Data", addresses)
    if last_used is not None:
        state = {"profile": {"info_cache": {f: {"name": n} for f, (n, _a) in profiles.items()}, "last_used": last_used}}
        (udd / "Local State").write_text(json.dumps(state), encoding="utf-8")
    return udd


@pytest.fixture
def browsers(tmp_path, monkeypatch) -> dict[str, Path]:
    """Chrome with two profiles (active: 'Me' = Profile 1 with Ada and Bob; 'Work' = Default with Bob;
    'Empty' = Profile 2 without a Web Data file) and Edge without a Local State file."""
    chrome = make_browser(tmp_path / "Chrome" / "User Data", {
        "Default": ("Work", [BOB]), "Profile 1": ("Me", [BOB, ADA]), "Profile 2": ("Empty", None)}, "Profile 1")
    edge = make_browser(tmp_path / "Edge" / "User Data", {"Default": ("Default", [BOB])})
    dirs = {"chrome": chrome, "edge": edge, "brave": tmp_path / "not-installed"}
    monkeypatch.setattr(chrome_autofill, "_user_data_dirs", lambda: dict(dirs))
    return dirs


@pytest.fixture
def home(tmp_path) -> Store:
    store = Store(tmp_path / "home")
    config = store.load_config()
    config.default_window = "offscreen"
    store.save_config(config)
    return store


# --------------------------------------------------------------------------- discovery


def test_discovery_finds_profiles_with_saved_data_active_first(browsers):
    sources = discover_sources()
    assert [s.ref for s in sources] == ["chrome:chrome/Profile 1", "chrome:chrome/Default", "chrome:edge/Default"]
    assert [s.active for s in sources] == [True, False, True]  # Edge without Local State: Default is in use
    assert sources[0].label == "Google Chrome profile 'Me' (active)"
    assert sources[1].label == "Google Chrome profile 'Work'"
    assert sources[2].label == "Microsoft Edge profile 'Default' (active)"
    assert [s.ref for s in discover_sources(["edge"])] == ["chrome:edge/Default"]


def test_default_browser_folders_are_the_ones_each_os_uses(tmp_path, monkeypatch):
    for var in ("LOCALAPPDATA", "XDG_CONFIG_HOME", "HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(tmp_path / var))
    dirs = real_user_data_dirs()
    assert {"chrome", "edge", "brave", "chromium"} <= set(dirs)
    assert not {"opera", "vivaldi"} & set(dirs)
    assert all(str(path).startswith(str(tmp_path)) for path in dirs.values())


def test_sources_are_resolved_by_browser_folder_or_display_name(browsers):
    assert resolve_source("chrome").ref == "chrome:chrome/Profile 1"  # the active profile of the first browser
    assert resolve_source(" Chrome ").ref == "chrome:chrome/Profile 1"
    assert resolve_source("chrome:edge").ref == "chrome:edge/Default"
    assert resolve_source("chrome:chrome/default").ref == "chrome:chrome/Default"
    assert resolve_source("chrome:chrome/work").ref == "chrome:chrome/Default"  # display name
    with pytest.raises(NotFoundError, match=r"Available: Profile 1 \(Me\), Default \(Work\)"):
        resolve_source("chrome:chrome/Nope")
    with pytest.raises(NotFoundError, match="No saved browser data found for 'brave'"):
        resolve_source("chrome:brave")
    with pytest.raises(ProfilePilotError, match="Not a browser source"):
        resolve_source("firefox")
    with pytest.raises(ProfilePilotError, match="needs a ProfilePilot profile"):
        resolve_source("profile")
    assert is_source_ref("chrome:edge") and is_source_ref("Profile") and not is_source_ref("chromebook")


def test_ambiguous_display_names_and_no_browsers_at_all(tmp_path, monkeypatch):
    udd = make_browser(tmp_path / "udd", {"Default": ("Me", [ADA]), "Profile 1": ("Me", [BOB])}, "Default")
    monkeypatch.setattr(chrome_autofill, "_user_data_dirs", lambda: {"chrome": udd})
    with pytest.raises(AmbiguousError, match="matches several profiles"):
        resolve_source("chrome:chrome/Me")
    monkeypatch.setattr(chrome_autofill, "_user_data_dirs", lambda: {})
    with pytest.raises(NotFoundError, match="No Chrome, Edge, Brave or Chromium profile"):
        resolve_source("chrome")


# --------------------------------------------------------------------------- reading


def test_addresses_are_mapped_and_ranked_by_use(tmp_path, browsers):
    nameless = {"guid": "empty-guid"}  # an address row without tokens is skipped
    odd = {**ADA, "guid": "odd-guid", "uses": 0, "email": "not an email", "country_code": "USA"}
    path = write_web_data(tmp_path / "x" / "Web Data", [BOB, nameless, odd, ADA])
    con = sqlite3.connect(path)
    con.execute("INSERT INTO address_type_tokens (guid, type, value) VALUES ('ada-0001-guid', 999, 'unknown type')")
    con.commit()
    con.close()
    source = chrome_autofill.ChromeSource("chrome", path.parent, "x")
    addresses = read_addresses(source)
    assert [a.guid for a in addresses] == ["ada-0001-guid", "bob-0002-guid", "odd-guid"]
    ada = addresses[0]
    assert ada.values == {k: v for k, v in ADA.items() if k in TYPES}
    assert ada.summary() == ADA_SUMMARY
    assert not any(secret in ada.summary() for secret in PRIVATE)
    assert ada.identity_values() == {
        "first_name": "Ada", "last_name": "Lovelace", "full_name": "Ada Lovelace", "email": "ada@example.test",
        "phone": "+1 555 010 0199", "company": "Analytical Engines", "street": "12 Example Street",
        "address_line2": "Apt 3", "city": "Phoenix", "state": "AZ", "postal_code": "85001", "country_code": "US",
        "country": "United States"}
    odd_values = addresses[2].identity_values()  # values that do not validate are dropped, not guessed
    assert "email" not in odd_values and "country_code" not in odd_values and odd_values["city"] == "Phoenix"
    assert ChromeAddress("g", {"company": "Analytical Engines"}, label="Work").summary() == \
        "Analytical Engines [Work]"


def test_only_the_address_tables_can_be_read(tmp_path):
    path = write_web_data(tmp_path / "Web Data", [ADA])
    con = chrome_autofill._connect(path)
    try:
        assert con.execute("SELECT count(*) FROM addresses").fetchone() == (1,)
        for sql in ("SELECT * FROM credit_cards", "SELECT card_number_encrypted FROM credit_cards",
                    "SELECT count(*) FROM credit_cards", "SELECT value FROM autofill", "SELECT * FROM meta",
                    "SELECT sql FROM sqlite_master", "PRAGMA table_info(credit_cards)",
                    "INSERT INTO addresses (guid) VALUES ('x')", "ATTACH DATABASE ':memory:' AS other"):
            with pytest.raises(sqlite3.DatabaseError):
                con.execute(sql).fetchall()
    finally:
        con.close()
    raw = path.read_bytes()
    assert raw.count(FAKE_CARD.encode()) == 1 and raw.count(FORM_HISTORY.encode()) >= 1  # nothing was written


def test_reading_works_while_the_browser_holds_the_database(tmp_path):
    path = write_web_data(tmp_path / "p" / "Web Data", [ADA])
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")  # what a running Chrome may hold
    try:
        source = chrome_autofill.ChromeSource("chrome", path.parent, "p")
        assert [a.guid for a in read_addresses(source)] == [ADA["guid"]]
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_missing_or_broken_web_data(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome_autofill.time, "sleep", lambda s: None)
    source = chrome_autofill.ChromeSource("chrome", tmp_path / "p", "p")
    assert read_addresses(source) == []
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "Web Data").write_bytes(b"this is not a database " * 200)
    with pytest.raises(ProfilePilotError, match="Could not read the saved addresses of Google Chrome profile 'p'"):
        read_addresses(source)


def test_picking_an_address(browsers):
    addresses = read_addresses(resolve_source("chrome"))
    assert pick_address(addresses).guid == ADA["guid"]  # the most used
    assert pick_address(addresses, 2).guid == pick_address(addresses, " 2 ").guid == BOB["guid"]
    assert pick_address(addresses, "BOB-").guid == BOB["guid"]
    with pytest.raises(NotFoundError, match="pick 1-2"):
        pick_address(addresses, 3)
    with pytest.raises(NotFoundError, match="No single saved address"):
        pick_address(addresses, "zzz")
    with pytest.raises(NotFoundError, match="no saved addresses"):
        pick_address([])
    values, source, chosen = source_values("chrome:chrome/Work")
    assert source.ref == "chrome:chrome/Default" and chosen.guid == BOB["guid"]
    assert values["street"] == "Teststrasse 1" and values["country"] == "Germany" and "address_line2" not in values


# --------------------------------------------------------------------------- identities linked to a browser


def test_identity_link_is_live_and_own_values_win(home, browsers):
    ids = IdentityStore(home)
    ids.create("Ada", {"last_name": "Byron", "birth_date": "1985-12-10"})
    ident = ids.connect_chrome("Ada")
    assert (ident.chrome_source, ident.chrome_address) == ("chrome", None)
    assert ids.get("Ada").summary()["chrome"] == "chrome"
    values = ids.fill_values("Ada")
    # the identity sets a name part, so the whole name is its own; the rest comes from the browser
    assert values["last_name"] == "Byron" and "first_name" not in values and values["full_name"] == "Byron"
    assert values["email"] == "ada@example.test" and values["street"] == "12 Example Street"
    assert values["birth_date"] == "1985-12-10" and values["phone_digits"] == "15550100199"
    masked = ids.masked("Ada")
    assert masked["chrome"]["source"] == "chrome"
    assert masked["chrome"]["address"] == f"Google Chrome profile 'Me' (active): {ADA_SUMMARY}"
    assert "email" in masked["chrome"]["fields_from_chrome"] and "last_name" not in masked["chrome"]["fields_from_chrome"]
    assert not any(secret in json.dumps(masked) for secret in PRIVATE)  # masked() shows no browser values
    on_disk = ids.file.read_text(encoding="utf-8")
    assert "ada@example.test" not in on_disk and "Phoenix" not in on_disk  # only the link is stored

    con = sqlite3.connect(browsers["chrome"] / "Profile 1" / "Web Data")  # the user edits it in Chrome
    con.execute("UPDATE address_type_tokens SET value='Tucson' WHERE guid=? AND type=33", (ADA["guid"],))
    con.commit()
    con.close()
    assert ids.fill_values("Ada")["city"] == "Tucson"

    pinned = ids.connect_chrome("Ada", "chrome", "2")  # a picked address pins its browser profile
    assert (pinned.chrome_source, pinned.chrome_address) == ("chrome:chrome/Profile 1", BOB["guid"])
    assert ids.fill_values("Ada")["email"] == "bob@example.test"
    assert ids.connect_chrome("Ada", "chrome:edge").chrome_source == "chrome:edge"
    assert ids.connect_chrome("Ada", "chrome:chrome/Work").chrome_source == "chrome:chrome/Default"

    with pytest.raises(ProfilePilotError, match="not to 'profile'"):
        ids.connect_chrome("Ada", "profile")
    with pytest.raises(NotFoundError):
        ids.connect_chrome("Ada", "chrome:brave")
    with pytest.raises(NotFoundError, match="pick 1-1"):
        ids.connect_chrome("Ada", "chrome:edge", 4)
    assert ids.get("Ada").chrome_source == "chrome:chrome/Default"  # failed links change nothing

    (browsers["chrome"] / "Default" / "Web Data").unlink()  # the browser profile is gone: no crash
    live, note = ids.chrome_values(ids.get("Ada"))
    assert live == {} and note.startswith("browser link unavailable")
    assert ids.fill_values("Ada")["last_name"] == "Byron"
    assert ids.disconnect_chrome("Ada").chrome_source is None
    assert "chrome" not in ids.masked("Ada") and "email" not in ids.fill_values("Ada")


def test_merge_never_stitches_names_or_addresses():
    live = {"first_name": "Ada", "last_name": "Lovelace", "street": "12 Example Street", "address_line2": "Apt 3",
            "city": "Phoenix", "email": "ada@example.test", "phone": "+1 555 010 0199"}
    assert merge_live({}, live) == live
    merged = merge_live({"street": "1 Other Road", "email": "me@example.test"}, live)
    assert merged == {"first_name": "Ada", "last_name": "Lovelace", "street": "1 Other Road",
                      "email": "me@example.test", "phone": "+1 555 010 0199"}


def test_browser_values_never_carry_sensitive_keys():
    out = browser_fill_values({"first_name": "Ada", "last_name": "Lovelace", "phone": "+1 555 010 0199",
                               "card_number": FAKE_CARD, "ssn": "000-12-3456"}, None)
    assert out["full_name"] == "Ada Lovelace" and out["phone_digits"] == "15550100199"
    assert "card_number" not in out and "ssn" not in out
    # like an identity's fill: a wanted field brings the values derived from it
    assert set(browser_fill_values({"first_name": "Ada", "last_name": "Lovelace", "city": "Phoenix"},
                                   {"first_name"})) == {"first_name", "full_name", "card_name"}


# --------------------------------------------------------------------------- MCP tools


@pytest.mark.asyncio
async def test_fill_source_resolution(home, browsers):
    state = type("State", (), {"store": home})()
    home.create_profile("p")
    source, values = await resolve_fill_source(state, "p", "chrome", None)
    assert source.browser_ref == "chrome:chrome/Profile 1" and source.ident is None
    assert source.label == f"Google Chrome profile 'Me' (active), saved address '{ADA_SUMMARY}'"
    assert values["email"] == "ada@example.test"
    source, values = await resolve_fill_source(state, "p", "chrome", "2")
    assert values["email"] == "bob@example.test"
    source, values = await resolve_fill_source(state, "p", None, None)  # no identity: the active Chrome
    assert source.browser_ref == "chrome:chrome/Profile 1"

    prof = home.get_profile("p")  # addresses saved in the profile's own window come first
    write_web_data(home.user_data_dir(prof.id) / "Default" / "Web Data", [BOB])
    source, values = await resolve_fill_source(state, "p", None, None)
    assert source.browser_ref == "profile" and source.label.startswith("this ProfilePilot profile's browser (p)")
    assert values["city"] == "Berlin"

    with pytest.raises(NotFoundError):  # a named identity never falls back to the browser
        await resolve_fill_source(state, "p", "Nobody", None)
    IdentityStore(home).create("Ada", {"first_name": "Ada"})
    home.update_profile("p", identity_id=IdentityStore(home).get("Ada").id)
    source, values = await resolve_fill_source(state, "p", None, None)
    assert source.ident is not None and source.ident.name == "Ada" and values is None

    IdentityStore(home).delete("Ada")  # a deleted persona must never turn into the user's real address
    with pytest.raises(ProfilePilotError, match="no longer exists"):
        await resolve_fill_source(state, "p", None, None)
    with pytest.raises(ProfilePilotError, match="ShardX profiles have no linked identity"):
        await resolve_fill_source(state, "shardx:x", None, None)  # their own persona, never the local Chrome

    home.update_profile("p", identity_id=None)
    config = home.load_config()
    config.autofill_from_browser = False
    home.save_config(config)
    with pytest.raises(ProfilePilotError, match="has no linked identity"):
        await resolve_fill_source(state, "p", None, None)


@pytest.mark.asyncio
async def test_fallback_tries_every_browsers_active_profile(home, tmp_path, monkeypatch):
    chrome = make_browser(tmp_path / "c", {"Default": ("Empty", [])}, "Default")  # active Chrome: nothing saved
    edge = make_browser(tmp_path / "e", {"Default": ("Work", [BOB])}, "Default")
    monkeypatch.setattr(chrome_autofill, "_user_data_dirs", lambda: {"chrome": chrome, "edge": edge})
    home.create_profile("p")
    source, values = await resolve_fill_source(type("State", (), {"store": home})(), "p", None, None)
    assert source.browser_ref == "chrome:edge/Default" and values["city"] == "Berlin"


def test_identity_names_cannot_shadow_browser_sources(home):
    ids = IdentityStore(home)
    for name in ("chrome", "Profile", "chrome:edge", " CHROME "):
        with pytest.raises(ProfilePilotError, match="reserved for the browser's saved addresses"):
            ids.create(name)
    ids.create("Chromebook")  # only the exact source names are reserved
    with pytest.raises(ProfilePilotError, match="reserved"):
        ids.update("Chromebook", name="profile")


@pytest.mark.asyncio
async def test_autofill_sources_tool_shows_no_private_details(home, browsers, monkeypatch):
    async def no_browser(self, ref, **kw):
        raise AssertionError("no browser should be started")

    monkeypatch.setattr(BrowserManager, "session", no_browser)
    ids = IdentityStore(home)
    ids.create("Ada", {"birth_date": "1985-12-10"})
    ids.connect_chrome("Ada")
    prof = home.create_profile("p")
    write_web_data(home.user_data_dir(prof.id) / "Default" / "Web Data", [BOB])
    async with Client(create_server(store=home)) as client:
        rec = Recorder(client)
        out = await rec.call("autofill_sources", {"profile": "p"})
        assert "Identities: Ada (linked to chrome)" in out
        assert "profile - this ProfilePilot profile's browser (p): 1 saved address(es)\n  1. Bob Example - Berlin, DE" in out
        assert f"chrome:chrome/Profile 1 - Google Chrome profile 'Me' (active): 2 saved address(es)\n  1. {ADA_SUMMARY}\n" \
               "  2. Bob Example - Berlin, DE" in out
        assert "chrome:edge/Default - Microsoft Edge profile 'Default' (active): 1 saved address(es)" in out
        assert "Profile 2" not in out and "brave" not in out
        refused = await rec.call("form_autofill_sensitive", {"profile": "p", "identity": "chrome"}, ok=False)
        assert "never taken from the browser's saved data" in refused
        shown = await rec.call("identity_show", {"identity": "Ada"})
        assert ADA_SUMMARY in shown
        blob = "\n".join(rec.outputs)
        assert not any(secret in blob for secret in PRIVATE) and FAKE_CARD not in blob


@pytest.mark.asyncio
async def test_form_autofill_without_identity_or_saved_addresses_explains_both(home, monkeypatch):
    async def no_browser(self, ref, **kw):
        raise AssertionError("no browser should be started")

    monkeypatch.setattr(BrowserManager, "session", no_browser)
    home.create_profile("p")
    async with Client(create_server(store=home)) as client:
        rec = Recorder(client)
        out = await rec.call("form_autofill", {"profile": "p"}, ok=False)
        assert "has no linked identity" in out and "no saved addresses either (see autofill_sources)" in out
        out = await rec.call("form_autofill", {"profile": "p", "identity": "chrome"}, ok=False)
        assert "No Chrome, Edge, Brave or Chromium profile" in out
        assert "No identities and no addresses saved" in await rec.call("autofill_sources")


# --------------------------------------------------------------------------- CLI


def test_cli_lists_links_and_imports_browser_addresses(tmp_path, monkeypatch):
    from .test_cli import make_cli

    cli = make_cli(tmp_path)
    users = cli.users  # type: ignore[attr-defined]
    cli.env.update({"LOCALAPPDATA": str(users / "Local")})  # type: ignore[attr-defined]
    for var in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(var, str(users))
    monkeypatch.setenv("LOCALAPPDATA", str(users / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", cli.env["XDG_CONFIG_HOME"])  # type: ignore[attr-defined]
    make_browser(real_user_data_dirs()["chrome"], {"Default": ("Me", [ADA, BOB])}, "Default")

    out = cli("identity", "sources").stdout
    assert "chrome:chrome/Default" in out and "Google Chrome profile 'Me' (active)" in out and ADA_SUMMARY in out
    data = json.loads(cli("identity", "sources", "--json").stdout)
    assert [a["summary"] for a in data[0]["addresses"]] == [ADA_SUMMARY, "Bob Example - Berlin, DE"]

    out = cli("identity", "connect-chrome", "Ada", "--create").stdout
    assert "now takes its name, email, phone and address live from Google Chrome profile 'Me'" in out
    assert "linked browser: Google Chrome profile 'Me' (active): " + ADA_SUMMARY in cli("identity", "show", "Ada").stdout
    refused = cli("identity", "connect-chrome", "Ada", "--source", "profile", ok=False)
    assert "not to 'profile'" in refused.stderr
    failed = cli("identity", "connect-chrome", "Ghost", "--create", "--address", "7", ok=False)
    assert "pick 1-2" in failed.stderr
    assert "Ghost" not in cli("identity", "list").stdout  # no empty identity left behind
    cli("identity", "disconnect-chrome", "Ada")
    assert "linked browser" not in cli("identity", "show", "Ada").stdout

    out = cli("identity", "import-chrome", "Bob", "--address", "2").stdout
    assert "Created identity 'Bob' from Google Chrome profile 'Me' (active) (Bob Example - Berlin, DE)" in out
    bob = IdentityStore(Store(cli.home)).get("Bob")  # type: ignore[attr-defined]
    assert bob.values["street"] == "Teststrasse 1" and bob.chrome_source is None  # a snapshot, not a link
    # importing another address into it replaces the whole name and address: no Bob-Ada mix
    cli("identity", "set", "Bob", "middle_name=Q", "state=Berlin", "birth_date=1990-01-01")
    out = cli("identity", "import-chrome", "Bob", "--address", "1").stdout
    assert "Updated identity 'Bob'" in out
    bob = IdentityStore(Store(cli.home)).get("Bob")  # type: ignore[attr-defined]
    assert (bob.values["first_name"], bob.values["street"], bob.values["state"]) == ("Ada", "12 Example Street", "AZ")
    assert "middle_name" not in bob.values and bob.values["birth_date"] == "1990-01-01"  # other fields stay
    assert not any(FAKE_CARD in text or FORM_HISTORY in text for text in cli.history)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- end to end


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_form_autofill_from_the_browsers_saved_address(chrome_home, browsers):  # noqa: F811
    from .fakes import OriginServer, pages_from_dir

    expected = {"fn": "Ada", "ln": "Lovelace", "em": "ada@example.test", "co": "Analytical Engines",
                "a1": "12 Example Street", "a2": "Apt 3", "ci": "Phoenix", "st": "AZ", "zp": "85001", "cn": "US",
                "pw": ""}
    with OriginServer(pages_from_dir(FIXTURES)) as site:
        url = f"{site.url}/signup.html"
        async with Client(create_server(store=chrome_home)) as client:
            rec = Recorder(client)
            await rec.call("profile_create", {"name": "shop"})
            await rec.call("browser_navigate", {"profile": "shop", "url": url, "wait_until": "load"})
            out = await rec.call("form_autofill", {"profile": "shop", "method": "fill"})  # no identity: the browser
            assert f"Autofill from Google Chrome profile 'Me' (active), saved address '{ADA_SUMMARY}' (fill):" in out
            values = await frame_values(chrome_home, "shop", url)
            assert {k: values[k] for k in expected} == expected

            ids = IdentityStore(chrome_home)
            ids.create("Ada", {"last_name": "Byron", "first_name": "Augusta"})
            ids.connect_chrome("Ada")
            await rec.call("browser_navigate", {"profile": "shop", "url": url, "wait_until": "load"})
            out = await rec.call("form_autofill", {"profile": "shop", "identity": "Ada", "method": "fill"})
            assert "Autofill from identity 'Ada' (fill):" in out
            values = await frame_values(chrome_home, "shop", url)
            assert {k: values[k] for k in expected} == {**expected, "fn": "Augusta", "ln": "Byron"}
            blob = "\n".join(rec.outputs)
            assert not any(secret in blob for secret in PRIVATE)
