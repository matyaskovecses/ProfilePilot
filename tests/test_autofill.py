"""Autofill engine: classification, option matching and value formats (pure Python), and
end-to-end detection + filling of the fixture forms in ``tests/fixtures/autofill`` with real Chrome.

The checkout fixture embeds the card fields from a second ``OriginServer`` reached as
``localhost`` from a ``127.0.0.1`` page, so Chrome runs them in a cross-site, out-of-process
iframe (like Stripe Elements). Only ``test_checkout_card_iframe_with_paste`` uses the real
clipboard, inside :func:`tests.fakes.user_clipboard_guard`. All values are obvious test data.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from profilepilot.automation import clipboard
from profilepilot.automation.autofill import (
    COUNTRY_MISMATCH,
    NOT_STORED,
    NOT_VISIBLE,
    AutofillReport,
    autocomplete_kind,
    autofill,
    classify,
    detect_fields,
    dispose_fields,
    find_country,
    find_region,
    match_option,
    options_shape,
    prepare_values,
    split_street,
    split_values,
    text_value,
)
from profilepilot.automation.clipboard import ClipboardUnavailable
from profilepilot.automation.driver import world_kwargs
from profilepilot.identity import SENSITIVE_FIELDS

from .chrome_helper import cdp_driver, default_driver_only  # noqa: F401 - cdp_driver is a fixture
from .fakes import OriginServer, clipboard_text_now, pages_from_dir, user_clipboard_guard

FIXTURES = Path(__file__).parent / "fixtures" / "autofill"
TEST_CARD = "4242424242424242"
TEST_SSN = "000-12-3456"
TEST_PASSWORD = "Test-Only-Pass-1"
VALUES = {
    "first_name": "Testy", "last_name": "McTestface", "email": "testy@example.test", "username": "testy_mctest",
    "password": TEST_PASSWORD, "phone": "+1 555 010 0000", "company": "Example Test Co",
    "street": "123 Test Street", "address_line2": "Apt 4", "city": "Testville", "state": "California",
    "postal_code": "12345", "country_code": "US", "birth_date": "1990-03-14", "gender": "female",
    "card_number": TEST_CARD, "card_exp_month": "04", "card_exp_year": "2031", "card_cvv": "123", "ssn": TEST_SSN,
}
PUBLIC_VALUES = {k: v for k, v in VALUES.items() if k not in SENSITIVE_FIELDS}
SECRETS = (TEST_CARD, "4242 4242 4242 4242", TEST_SSN, "000123456", TEST_PASSWORD)

DUMP_JS = """() => {
  const out = {};
  for (const e of document.querySelectorAll('input,select,textarea,[contenteditable]')) {
    const key = (e.name || e.id) + (e.type === 'radio' ? ':' + e.value : '');
    out[key] = e.type === 'radio' || e.type === 'checkbox' ? e.checked
      : (e.matches('[contenteditable]') ? e.innerText : e.value);
  }
  return out;
}"""


def info(**kw: Any) -> dict[str, Any]:
    base = {"tag": "input", "type": "text", "control": "text", "autocomplete": "", "name": "", "id": "",
            "placeholder": "", "aria": "", "labelledby": "", "label": "", "title": "", "nearby": "", "legend": "",
            "inputmode": "", "maxLength": None, "pattern": "", "options": None, "visible": True}
    base.update(kw)
    return base


def assert_no_secrets(report: AutofillReport) -> None:
    text = json.dumps(report.as_dict(), ensure_ascii=False) + "\n".join(report.lines())
    for secret in SECRETS:
        assert secret not in text
    for value in ("McTestface", "testy@example.test", "123 Test Street", "Testville"):
        assert value not in text  # descriptors are labels, never values


# ---------------------------------------------------------------------- unit: classification


def test_autocomplete_tokens():
    assert autocomplete_kind("given-name") == ("first_name", True)
    assert autocomplete_kind("section-ship shipping address-line1") == ("street", True)
    assert autocomplete_kind("billing cc-exp-month") == ("card_exp_month", True)
    assert autocomplete_kind("work tel") == ("phone", True)
    assert autocomplete_kind("one-time-code") == (None, True)  # never filled
    assert autocomplete_kind("fax tel") == (None, True)
    assert autocomplete_kind("off") == (None, False)  # falls through to the other signals
    assert autocomplete_kind("") == (None, False)


@pytest.mark.parametrize(("signals", "expected"), [
    ({"label": "Vorname"}, "first_name"),
    ({"label": "Nachname"}, "last_name"),
    ({"label": "Prénom"}, "first_name"),
    ({"label": "Nom de famille"}, "last_name"),
    ({"label": "Apellidos"}, "last_name"),
    ({"label": "Nombre completo"}, "full_name"),
    ({"placeholder": "E-Mail"}, "email"),
    ({"label": "Correo electrónico"}, "email"),
    ({"name": "billing_zip"}, "postal_code"),
    ({"label": "Código postal"}, "postal_code"),
    ({"nearby": "Straße und Hausnummer"}, "street"),
    ({"label": "Adresszusatz"}, "address_line2"),
    ({"label": "Teléfono móvil"}, "phone"),
    ({"label": "Numéro de carte"}, "card_number"),
    ({"name": "cardnumber"}, "card_number"),
    ({"aria": "Credit or debit card expiration date"}, "card_exp"),
    ({"name": "exp-date", "placeholder": "MM / YY"}, "card_exp"),
    ({"aria": "Credit or debit card CVC/CVV"}, "card_cvv"),
    ({"label": "Name on card"}, "card_name"),
    ({"label": "Karteninhaber"}, "card_name"),
    ({"label": "Social Security Number"}, "ssn"),
    ({"label": "Sozialversicherungsnummer"}, "ssn"),
    ({"nearby": "Geburtsdatum", "placeholder": "TT.MM.JJJJ"}, "birth_date"),
    ({"label": "Firma"}, "company"),
    ({"label": "Bundesland"}, "state"),
    ({"label": "Username"}, "username"),
    ({"label": "Geburtsort"}, None),
    ({"label": "Promo code"}, None),
    ({"label": "Search"}, None),
    ({"label": "Favourite colour"}, None),
    ({"type": "tel"}, "phone"),
    ({"type": "email", "label": "Login"}, "email"),
    ({"type": "tel", "label": "Card number"}, "card_number"),
    ({"type": "password", "label": "Confirm password"}, "password"),
    ({"type": "date", "control": "date", "label": "Date of birth"}, "birth_date"),
    ({"type": "date", "control": "date", "label": "Check-in"}, None),
    ({"type": "date", "control": "date", "label": "Last visit date"}, None),
    ({"label": "Country/Region"}, "country"),
    ({"label": "Pet name"}, None),
    ({"label": "Product name"}, None),
    ({"label": "Name"}, "name_generic"),  # resolved per form: full or last name
    ({"autocomplete": "one-time-code", "label": "Email code"}, None),
])
def test_classify_signals(signals, expected):
    assert classify(info(**signals))[0] == expected


def test_classify_selects_by_name_legend_and_option_shape():
    days = [["", "Day", False]] + [[str(d), str(d), False] for d in range(1, 32)]
    months = [["", "Month", False]] + [[str(m), n, False] for m, n in enumerate(
        ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
         "November", "December"], 1)]
    years = [["", "Year", False]] + [[str(y), str(y), False] for y in range(2010, 1929, -1)]
    sel = {"tag": "select", "type": "", "control": "select"}
    assert options_shape(days) == "day" and options_shape(months) == "month" and options_shape(years) == "year"
    assert classify(info(**sel, name="dob_day", options=days))[0] == "birth_day"
    assert classify(info(**sel, legend="Date of birth", options=months))[0] == "birth_month"
    assert classify(info(**sel, legend="Geburtsdatum", options=years))[0] == "birth_year"
    assert classify(info(**sel, name="exp_month"))[0] == "card_exp_month"
    assert classify(info(**sel, label="Expiration year"))[0] == "card_exp_year"
    assert classify(info(**sel, label="Anrede"))[0] == "gender"
    assert classify(info(**sel, label="Land"))[0] == "country"
    assert classify(info(**sel, label="Year of manufacture", options=years))[0] is None  # no birth/card context


def _raws(*infos: dict[str, Any]) -> list:
    from profilepilot.automation.autofill import _Raw

    out = []
    for order, i in enumerate(infos):
        i = {"order": order, "form": 0, "parent": order, "grand": 100 + order, "visible": True, "inputVisible": True,
             "hasValue": False, **i}
        out.append(_Raw(frame=None, frame_url="http://127.0.0.1/", frame_index=0, lang="en",  # type: ignore[arg-type]
                        element=object(), info=i))  # type: ignore[arg-type]
    return out


def test_group_level_classification():
    from profilepilot.automation.autofill import classify_raw

    radio = {"tag": "input", "type": "radio", "control": "radio"}
    survey = classify_raw(_raws(info(**radio, name="src", legend="How did you hear about us?", label="Friend"),
                                info(**radio, name="src", legend="How did you hear about us?", label="Other")))
    assert survey == []  # a lone "Other" is not a gender question
    titles = classify_raw(_raws(info(**radio, name="t", label="Mr"), info(**radio, name="t", label="Mrs"),
                                info(**radio, name="t", label="Dr")))
    assert [(f.kind, f.control, len(f.members)) for f in titles] == [("gender", "radio", 3)]

    german = classify_raw(_raws(info(label="Vorname"), info(label="Name"), info(label="E-Mail")))
    assert [f.kind for f in german] == ["first_name", "last_name", "email"]
    single = classify_raw(_raws(info(label="Name"), info(label="E-Mail")))
    assert [f.kind for f in single] == ["full_name", "email"]

    shared = {"parent": 7, "grand": 70}
    phone = classify_raw(_raws(info(label="Phone", maxLength=3, **shared), info(maxLength=3, **shared),
                               info(maxLength=4, **shared), info(label="City")))
    assert [(f.kind, f.group_index, f.group_size) for f in phone] == [
        ("phone", 0, 3), ("phone", 1, 3), ("phone", 2, 3), ("city", None, None)]
    assert phone[0].split_lengths == [3, 3, 4]
    full = classify_raw(_raws(info(label="Phone"), info(label="Mobile phone")))  # two whole numbers
    assert [(f.kind, f.group_index) for f in full] == [("phone", None), ("phone", None)]
    hidden = classify_raw(_raws(info(label="First name", visible=False), info(label="Email", disabled=True),
                                info(label="Last name", readonly=True)))
    assert hidden == []


# ---------------------------------------------------------------------- unit: option matching


def opts(*pairs: Any) -> list[list[Any]]:
    return [[v, t, False] for v, t in pairs]


def test_country_matching():
    vals = prepare_values({"country_code": "US"})
    assert vals["country"] == "United States"
    assert match_option("country", vals, opts(("", "Select"), ("CAN", "Canada"), ("USA", "United States of America"))) == 2
    # <option> without a value attribute: the DOM reports its text as the value
    german = opts(("Bitte wählen", "Bitte wählen"), ("Deutschland", "Deutschland"), ("Vereinigte Staaten", "Vereinigte Staaten"))
    assert match_option("country", vals, german) == 2
    assert match_option("country", vals, opts(("124", "Canada"), ("840", "U.S."))) == 1
    assert match_option("country", vals, opts(("UM", "United States Minor Outlying Islands"), ("US", "USA"))) == 1
    de = prepare_values({"country": "Deutschland"})
    assert de["country_code"] == "DE"
    assert match_option("country", de, opts(("AT", "Austria"), ("DE", "Germany"))) == 1
    assert find_country("GBR").code2 == "GB" and find_country("Großbritannien").code2 == "GB"
    assert find_country("Nowhere-land") is None


def test_state_and_province_matching():
    ca = prepare_values({"state": "California", "country_code": "US"})
    assert match_option("state", ca, opts(("", "Select"), ("AZ", "Arizona"), ("CA", "California"))) == 2
    assert match_option("state", ca, opts(("1", "AZ - Arizona"), ("2", "CA - California"))) == 1
    code = prepare_values({"state": "ca", "country_code": "US"})
    assert match_option("state", code, opts(("Arizona", "Arizona"), ("California", "California"))) == 1
    qc = prepare_values({"state": "QC", "country_code": "CA"})
    assert find_region("QC", "CA") == ("QC", "Quebec")
    assert match_option("state", qc, opts(("ON", "Ontario"), ("x", "Québec"))) == 1


def test_month_year_day_gender_and_brand_matching():
    vals = prepare_values({"birth_date": "1990-03-14", "card_exp_month": "04", "card_exp_year": "2031",
                           "gender": "female", "card_number": TEST_CARD})
    assert match_option("card_exp_month", vals, opts(("", "MM"), ("1", "01 - Jan"), ("4", "04 - Apr"))) == 2
    assert match_option("card_exp_month", vals, opts(("x", "März"), ("y", "April"))) == 1
    assert match_option("birth_month", vals, opts(("a", "Januar"), ("b", "Februar"), ("c", "März"))) == 2
    assert match_option("birth_month", vals, opts(("01", "01"), ("03", "03"))) == 1
    assert match_option("card_exp_year", vals, opts(("30", "2030"), ("31", "2031"))) == 1
    assert match_option("card_exp_year", vals, opts(("a", "30"), ("b", "31"))) == 1
    assert match_option("birth_day", vals, opts(("13", "13"), ("14", "14"))) == 1
    assert match_option("birth_year", vals, opts(("1989", "1989"), ("1990", "1990"))) == 1
    assert match_option("gender", vals, opts(("", "Prefer not to say"), ("m", "Male"), ("f", "Female"))) == 2
    assert match_option("gender", vals, opts(("herr", "Herr"), ("frau", "Frau"))) == 1
    assert match_option("gender", vals, opts(("1", "Male (he/him)"), ("2", "Female (she/her)"))) == 1
    assert match_option("card_type", vals, opts(("AX", "American Express"), ("VI", "Visa"))) == 1
    assert match_option("gender", prepare_values({}), opts(("m", "Male"))) is None  # no value: nothing chosen


def test_value_formats():
    vals = prepare_values(VALUES)
    assert text_value("card_exp", info(placeholder="MM / YY"), vals) == "04 / 31"
    assert text_value("card_exp", info(placeholder="MM/YYYY"), vals) == "04/2031"
    assert text_value("card_exp", info(maxLength=5), vals) == "04/31"
    assert text_value("card_exp", info(maxLength=7), vals) == "04/2031"
    assert text_value("card_exp_year", info(maxLength=2), vals) == "31"
    assert text_value("card_number", info(), vals) == TEST_CARD
    assert text_value("phone", info(), vals) == "+1 555 010 0000"
    assert text_value("phone", info(maxLength=10), vals) == "5550100000"
    assert text_value("phone", info(inputmode="numeric"), vals) == "15550100000"
    assert text_value("phone", info(pattern="[0-9]{3}-[0-9]{3}-[0-9]{4}"), vals) == "555-010-0000"
    assert text_value("phone_national", info(autocomplete="tel-national"), vals) == "555 010 0000"
    assert text_value("phone_country_code", info(), vals) == "+1"
    assert text_value("ssn", info(), vals) == TEST_SSN
    assert text_value("ssn", info(maxLength=9), vals) == "000123456"
    assert text_value("ssn", info(maxLength=4), vals) == "3456"
    assert text_value("birth_date", info(placeholder="MM/DD/YYYY"), vals) == "03/14/1990"
    assert text_value("birth_date", info(placeholder="TT.MM.JJJJ"), vals) == "14.03.1990"
    assert text_value("birth_date", info(placeholder="JJ/MM/AAAA"), vals) == "14/03/1990"
    assert text_value("birth_date", info(lang="de"), vals) == "14.03.1990"
    assert text_value("birth_date", info(lang="en-us"), vals) == "03/14/1990"
    assert text_value("country", info(autocomplete="country"), vals) == "US"
    assert text_value("country", info(maxLength=3), vals) == "USA"
    assert text_value("country", info(), vals) == "United States"
    assert text_value("state", info(maxLength=2), vals) == "CA"
    assert text_value("street", info(tag="textarea"), vals) == "123 Test Street\nApt 4"
    assert text_value("full_name", info(), vals) == "Testy McTestface"


def test_split_values():
    from profilepilot.automation.autofill import DetectedField

    vals = prepare_values(VALUES)

    def part(kind: str, index: int, lengths: list[int | None]) -> str | None:
        f = DetectedField(frame_url="", kind=kind, confidence=1, descriptor="", element=None,  # type: ignore[arg-type]
                          control="text", group_index=index, group_size=len(lengths), split_lengths=lengths)
        return split_values(f, vals)

    assert [part("phone", i, [3, 3, 4]) for i in range(3)] == ["555", "010", "0000"]
    assert [part("ssn", i, [3, 2, 4]) for i in range(3)] == ["000", "12", "3456"]
    assert [part("card_number", i, [4, 4, 4, 4]) for i in range(4)] == ["4242"] * 4
    assert [part("ssn", i, [None, None, None]) for i in range(3)] == ["000", "12", "3456"]  # standard 3-2-4
    amex = prepare_values({"card_number": "378282246310005"})  # public test Amex number
    f = [DetectedField(frame_url="", kind="card_number", confidence=1, descriptor="", element=None,  # type: ignore[arg-type]
                       control="text", group_index=i, group_size=3) for i in range(3)]
    assert [split_values(x, amex) for x in f] == ["3782", "822463", "10005"]


# ---------------------------------------------------------------------- real Chrome


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    from .chrome_helper import launch_chrome

    with launch_chrome(tmp_path_factory.mktemp("autofill") / "udd") as launched:
        yield launched


@pytest.fixture(scope="module")
def servers():
    """(main origin on 127.0.0.1, card origin on localhost: a different site -> OOPIF, and a third
    origin for untrusted widgets)."""
    with OriginServer(pages_from_dir(FIXTURES)) as card, OriginServer(pages_from_dir(FIXTURES)) as widget:
        card_origin, widget_origin = f"http://localhost:{card.port}", f"http://localhost:{widget.port}"
        pages = pages_from_dir(FIXTURES, {"{{CARD_ORIGIN}}": card_origin, "{{WIDGET_ORIGIN}}": widget_origin})
        with OriginServer(pages) as main:
            yield main, card_origin, widget_origin


@pytest_asyncio.fixture
async def open_page(chrome, servers, cdp_driver):
    """Opens a fixture page in a new tab, through each CDP driver in turn (patchright evaluates in
    an isolated world: detection and filling must work the same)."""
    from profilepilot.automation.driver import async_playwright

    main = servers[0]
    async with async_playwright(cdp_driver) as pw:
        browser = await pw.chromium.connect_over_cdp(chrome.http_url, no_defaults=True)
        tabs = []

        async def opener(name: str):
            tab = await browser.contexts[0].new_page()
            tabs.append(tab)
            await tab.goto(f"{main.url}/{name}.html", wait_until="load")
            return tab

        try:
            yield opener
        finally:
            for tab in tabs:
                await tab.close()
            await browser.close()


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
    """Every autofill test runs without the real clipboard unless it opts in."""
    if "uses_real_clipboard" in request.fixturenames:
        yield
        return
    clipboard.set_backend(NoClipboard())
    try:
        yield
    finally:
        clipboard.set_backend(None)


async def fill(page, values=VALUES, *, method="fill", tmp_path: Path, **kw):
    return await autofill(page, values, method=method, sensitive_keys=set(SENSITIVE_FIELDS),
                          clipboard_lock=tmp_path / "cb.lock", rng=random.Random(1), pause=(0, 0), **kw)


async def dump(frame) -> dict[str, Any]:
    return await frame.evaluate(DUMP_JS)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_signup_form_with_autocomplete_attributes(open_page, tmp_path):
    page = await open_page("signup")
    # detection is read-only: no attribute or node changes anywhere
    await page.evaluate("""() => { window.__mut = 0; new MutationObserver(r => { window.__mut += r.length; })
        .observe(document, {subtree: true, childList: true, attributes: true, characterData: true}); }""")
    fields = await detect_fields(page)
    await dispose_fields(fields)
    await page.wait_for_timeout(50)
    assert await page.evaluate("window.__mut") == 0
    assert {f.kind for f in fields} == {"first_name", "last_name", "email", "username", "password", "phone",
                                        "company", "street", "address_line2", "city", "state", "postal_code",
                                        "country", "birth_date"}

    report = await fill(page, tmp_path=tmp_path)
    assert await dump(page) == {
        "fn": "Testy", "ln": "McTestface", "em": "testy@example.test", "un": "testy_mctest", "pw": TEST_PASSWORD,
        "ph": "+1 555 010 0000", "fax": "", "co": "Example Test Co", "a1": "123 Test Street", "a2": "Apt 4",
        "ci": "Testville", "st": "CA", "zp": "12345", "cn": "US", "bd": "1990-03-14", "notes": "", "otp": "",
        "hp": "", "hp2": "", "ro": "", "dis": "", "nl": False, "q": "",
    }
    assert len(report.filled) == 14 and not report.skipped
    assert {e["method"] for e in report.filled} == {"fill", "select"}
    assert_no_secrets(report)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_labels_placeholders_and_german_without_autocomplete(open_page, tmp_path):
    page = await open_page("labels_only")
    report = await fill(page, PUBLIC_VALUES, method="type", tmp_path=tmp_path)
    assert await dump(page) == {
        "vorname": "Testy", "name": "McTestface", "kontakt_mail": "testy@example.test", "f5": "123 Test Street",
        "f6": "Apt 4", "f7": "12345", "f8": "Testville", "f9": "California", "f10": "Vereinigte Staaten",
        "f11": "+1 555 010 0000", "f12": "14.03.1990", "f13": "", "f14": "", "login": "testy_mctest",
        "mail2": "testy@example.test", "firma-box": "Example Test Co",
    }
    methods = {e["kind"]: e["method"] for e in report.filled}
    assert methods["company"] == "type" and methods["country"] == "select"
    assert any(e["field"] == 'input "Straße und Hausnummer"' for e in report.filled)
    assert_no_secrets(report)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_checkout_card_iframe_with_paste(open_page, servers, tmp_path, uses_real_clipboard, cdp_driver):
    """Sensitive card values pasted into a cross-site (out-of-process) iframe, like Stripe."""
    default_driver_only(cdp_driver, "uses the user's real clipboard")
    card_origin = servers[1]
    page = await open_page("checkout")
    card_frame = next(f for f in page.frames if f.url.startswith(card_origin))
    cdp = await page.context.new_cdp_session(page)
    targets = (await cdp.send("Target.getTargets"))["targetInfos"]
    assert any(t["type"] == "iframe" and t["url"].startswith(card_origin) for t in targets)  # really an OOPIF
    await cdp.detach()

    with user_clipboard_guard("PP-TEST-SENTINEL-AUTOFILL", image=False) as backend:
        report = await autofill(page, VALUES, method="paste", sensitive_keys=set(SENSITIVE_FIELDS),
                                clipboard_lock=tmp_path / "cb.lock", rng=random.Random(2), pause=(0.02, 0.05),
                                sensitive_frame_origins=lambda kind, origin: origin == card_origin)
        assert clipboard_text_now(backend) == "PP-TEST-SENTINEL-AUTOFILL"
    assert await dump(page) == {"receipt": "testy@example.test", "ccname": "Testy McTestface", "billing_zip": "12345"}
    assert await dump(card_frame) == {"cardnumber": "4242 4242 4242 4242", "exp-date": "04 / 31", "cvc": "123"}
    assert {e["method"] for e in report.filled} == {"paste"} and len(report.filled) == 6
    in_frame = [e for e in report.filled if e.get("frame")]
    assert {e["kind"] for e in in_frame} == {"card_number", "card_exp", "card_cvv"}
    assert all(e["frame"] == card_origin for e in in_frame)
    events = await card_frame.evaluate("events", **world_kwargs(card_frame, "main"))  # a page global
    pastes = [e for e in events if e["type"] == "paste"]
    assert len(pastes) == 3 and all(e["trusted"] for e in pastes)
    assert any(e["inputType"] == "insertFromPaste" and e["trusted"] for e in events)
    assert_no_secrets(report)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_selects_country_state_expiry_dob_gender(open_page, tmp_path):
    page = await open_page("selects")
    report = await fill(page, tmp_path=tmp_path)
    assert await dump(page) == {"region": "CA", "country": "USA", "exp_month": "4", "exp_year": "31",
                                "dob_day": "14", "dob_month": "3", "dob_year": "1990", "gender": "f",
                                "cardtype": "VI"}
    assert report.filled[-1]["kind"] == "state"  # waited for the region list loaded after the country
    assert not report.skipped
    assert_no_secrets(report)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_split_phone_ssn_card_and_dob(open_page, tmp_path):
    page = await open_page("split")
    report = await fill(page, method="type", tmp_path=tmp_path)
    assert await dump(page) == {"p1": "555", "p2": "010", "p3": "0000", "s1": "000", "s2": "12", "s3": "3456",
                                "c1": "4242", "c2": "4242", "c3": "4242", "c4": "4242", "dm": "03", "dd": "14",
                                "dy": "1990", "cvv": "123", "ssn4": "3456"}
    parts = [(e["kind"], e.get("part")) for e in report.filled]
    assert ("phone", "3/3") in parts and ("ssn", "2/3") in parts and ("card_number", "4/4") in parts
    assert ("birth_year", "3/3") in parts
    assert_no_secrets(report)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_sensitive_fields_are_only_filled_when_given(open_page, tmp_path):
    page = await open_page("split")
    report = await fill(page, PUBLIC_VALUES, method="type", tmp_path=tmp_path)
    values = await dump(page)
    assert values["p1"] == "555" and values["dy"] == "1990"
    assert all(values[k] == "" for k in ("s1", "s2", "s3", "c1", "c2", "c3", "c4", "cvv", "ssn4"))
    reasons = {e["kind"]: e["reason"] for e in report.skipped}
    assert reasons == {"ssn": "sensitive field (not included in this fill)",
                       "card_number": "sensitive field (not included in this fill)",
                       "card_cvv": "sensitive field (not included in this fill)"}


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_date_inputs(open_page, tmp_path):
    page = await open_page("dob_date")
    report = await fill(page, method="paste", tmp_path=tmp_path)  # date inputs never use the clipboard
    assert await dump(page) == {"dob": "1990-03-14", "appt": "", "exp": "2031-04"}
    assert [(e["kind"], e["method"]) for e in report.filled] == [("birth_date", "fill"), ("card_exp", "fill")]


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_gender_radios_including_custom_styled_ones(open_page, tmp_path):
    page = await open_page("gender_radio")
    report = await fill(page, tmp_path=tmp_path)
    assert await dump(page) == {"g:m": False, "g:f": True, "g:o": False, "anrede:herr": False, "anrede:frau": True,
                                "ship:std": False, "ship:exp": False}
    assert [(e["kind"], e["field"], e["method"]) for e in report.filled] == [
        ("gender", 'radio group "Gender"', "click"), ("gender", 'radio group "Anrede"', "click")]
    report = await fill(page, {"gender": "male"}, tmp_path=tmp_path)  # already chosen: kept
    assert {e["reason"] for e in report.skipped} == {"already has a value"}
    assert (await dump(page))["g:f"] is True


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_rerendered_and_revealed_fields_get_a_second_pass(open_page, tmp_path, monkeypatch):
    from profilepilot.automation import autofill as autofill_mod

    stale_hits = []
    original = autofill_mod._stale
    monkeypatch.setattr(autofill_mod, "_stale", lambda exc: stale_hits.append(1) or original(exc))
    page = await open_page("rerender")
    report = await fill(page, PUBLIC_VALUES, tmp_path=tmp_path)
    assert await dump(page) == {"country": "US", "city": "Testville", "zip": "12345"}
    assert sorted(e["kind"] for e in report.filled) == ["city", "country", "postal_code"]
    assert not report.skipped
    assert stale_hits  # the old City input was replaced after the country was chosen


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_overwrite_scope_only_and_human_method(open_page, tmp_path):
    page = await open_page("prefilled")
    report = await fill(page, PUBLIC_VALUES, method="human", wpm=400, tmp_path=tmp_path,
                        only={"first_name", "email", "country", "city"})
    assert await dump(page) == {"fn": "Testy", "em": "already@example.test", "cn": "DE", "ci": "Testville",
                                "bci": "Testville", "bzp": ""}
    # the form's preselected Germany differs from the identity's United States: an actionable reason
    assert {e["kind"]: e["reason"] for e in report.skipped} == {"email": "already has a value",
                                                                "country": COUNTRY_MISMATCH}
    assert_no_secrets(report)
    assert {e["method"] for e in report.filled} == {"human"}

    report = await fill(page, PUBLIC_VALUES, tmp_path=tmp_path, overwrite=True,
                        scope=page.locator("#billing"))
    assert [e["kind"] for e in report.filled] == ["city", "postal_code"]
    assert (await dump(page))["bzp"] == "12345" and (await dump(page))["em"] == "already@example.test"

    report = await fill(page, PUBLIC_VALUES, tmp_path=tmp_path, overwrite=True)
    values = await dump(page)
    assert values["em"] == "testy@example.test" and values["cn"] == "US"
    assert not report.skipped


# ---------------------------------------------------------------------- house numbers (unit)


@pytest.mark.parametrize(("signals", "expected"), [
    ({"label": "Hausnr."}, "house_number"),
    ({"label": "Hausnummer"}, "house_number"),
    ({"label": "House number"}, "house_number"),
    ({"name": "street_no"}, "house_number"),
    ({"label": "Straße"}, "street"),
    ({"label": "Straße und Hausnummer"}, "street"),  # one combined field
    ({"label": "Street and house number"}, "street"),
])
def test_house_number_classification(signals, expected):
    assert classify(info(**signals))[0] == expected


def test_street_name_next_to_a_house_number_field_and_split_street():
    from profilepilot.automation.autofill import classify_raw

    form = classify_raw(_raws(info(label="Straße"), info(label="Hausnr.", maxLength=6), info(label="Ort")))
    assert [f.kind for f in form] == ["street_name", "house_number", "city"]
    assert [f.kind for f in classify_raw(_raws(info(label="Straße"), info(label="Ort")))] == ["street", "city"]
    assert split_street("123 Test Street") == ("Test Street", "123")
    assert split_street("Teststraße 12a") == ("Teststraße", "12a")
    assert split_street("Hauptstr. 5/7") == ("Hauptstr.", "5/7")
    assert split_street("12-14 Test Road") == ("Test Road", "12-14")
    assert split_street("Am Testweg") is None and split_street("") is None
    vals = prepare_values({"street": "Teststraße 12"})
    assert text_value("street_name", info(), vals) == "Teststraße" and text_value("house_number", info(), vals) == "12"
    assert text_value("street", info(), vals) == "Teststraße 12"  # a single street field gets the whole line


# ---------------------------------------------------------------------- hostile pages (real Chrome)


def card_frames_only(card_origin: str):
    """What the server allows on the test checkout: the card processor's frame, card fields only."""
    card_kinds = {"card_number", "card_exp", "card_exp_month", "card_exp_year", "card_cvv", "card_name"}
    return lambda kind, origin: origin == card_origin and kind in card_kinds


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_sensitive_values_stay_out_of_third_party_frames(open_page, servers, tmp_path):
    _, card_origin, widget_origin = servers
    page = await open_page("secure")
    widget = next(f for f in page.frames if f.url.startswith(widget_origin))
    card = next(f for f in page.frames if f.url.startswith(card_origin))

    # no frame policy: fail closed, no sensitive value goes into any child frame
    report = await fill(page, tmp_path=tmp_path)
    assert await dump(card) == {"cardnumber": "", "exp-date": "", "cvc": ""}
    assert await dump(widget) == {"cardnumber_3p": "", "ssn_3p": ""}
    framed = {(e["kind"], e["reason"]) for e in report.skipped if e.get("frame")}
    assert ("card_number", f"sensitive field in a third-party frame ({card_origin})") in framed
    assert ("ssn", f"sensitive field in a third-party frame ({widget_origin})") in framed
    assert {e["kind"] for e in report.filled} == {"email", "ssn", "password"}  # the main frame is the allowed page
    assert_no_secrets(report)

    page = await open_page("secure")
    widget = next(f for f in page.frames if f.url.startswith(widget_origin))
    card = next(f for f in page.frames if f.url.startswith(card_origin))
    progress: list[tuple[int, int]] = []

    async def on_progress(done: int, total: int) -> None:
        progress.append((done, total))

    report = await fill(page, tmp_path=tmp_path, sensitive_frame_origins=card_frames_only(card_origin),
                        progress=on_progress)
    assert await dump(card) == {"cardnumber": "4242 4242 4242 4242", "exp-date": "04 / 31", "cvc": "123"}
    assert await dump(widget) == {"cardnumber_3p": "", "ssn_3p": ""}  # an allowed page's widget gets nothing
    reasons = {(e["kind"], e["reason"]) for e in report.skipped}
    assert reasons == {("card_number", f"sensitive field in a third-party frame ({widget_origin})"),
                       ("ssn", f"sensitive field in a third-party frame ({widget_origin})")}
    assert [d for d, _ in progress] == list(range(1, len(progress) + 1)) and len(progress) >= 8
    assert TEST_CARD in report.secret_texts and TEST_SSN in report.secret_texts  # private, for redaction
    assert_no_secrets(report)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_hidden_covered_and_clipped_fields_are_never_filled(open_page, tmp_path):
    for method in ("fill", "paste"):  # paste: no clipboard here, so it falls back to typing key by key
        page = await open_page("hidden")
        report = await fill(page, PUBLIC_VALUES, method=method, tmp_path=tmp_path, wpm=900)
        values = await dump(page)
        assert values["email"] == "testy@example.test"
        assert all(values[k] == "" for k in ("fullname", "street", "tel", "bday", "city", "zip", "country", "org",
                                             "faded")), values
        for frame in page.frames[1:]:
            assert await dump(frame) == {"given": "", "phone": ""}
        assert [e["kind"] for e in report.filled] == ["email"]
        assert [(e["kind"], e["reason"]) for e in report.skipped] == [("company", NOT_VISIBLE)]  # the covered one
        assert_no_secrets(report)


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_street_and_house_number_fields(open_page, tmp_path):
    page = await open_page("address_de")
    report = await fill(page, {**PUBLIC_VALUES, "street": "Teststraße 12"}, method="type", tmp_path=tmp_path)
    values = await dump(page)
    assert (values["strasse"], values["hausnr"]) == ("Teststraße", "12")
    assert (values["street_en"], values["house_en"]) == ("Teststraße", "12")
    assert values["line1"] == "Teststraße 12"
    assert values["plz"] == "12345" and values["ort"] == "Testville"
    assert not report.skipped
    assert all("Teststraße" not in json.dumps(e, ensure_ascii=False) for e in report.filled)

    page = await open_page("address_de")
    report = await fill(page, {**PUBLIC_VALUES, "street": "Am Testweg"}, tmp_path=tmp_path)  # method fill
    values = await dump(page)
    assert (values["strasse"], values["hausnr"]) == ("Am Testweg", "")
    reasons = {e["field"]: e["reason"] for e in report.skipped}
    assert reasons['input "Hausnr."'].startswith("the identity's street has no house number")


@pytest.mark.chrome
@pytest.mark.asyncio
async def test_missing_secrets_are_reported_as_not_stored(open_page, tmp_path):
    page = await open_page("split")
    report = await fill(page, {"card_number": TEST_CARD}, method="type", tmp_path=tmp_path)
    reasons = {e["kind"]: e["reason"] for e in report.skipped}
    assert reasons["ssn"] == NOT_STORED and reasons["card_cvv"] == NOT_STORED
    assert (await dump(page))["c4"] == "4242"
