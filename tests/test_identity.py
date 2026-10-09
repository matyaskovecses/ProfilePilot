"""Identity store: validation, secret handling, masking and the sensitive-origin policy.

All values here are obvious test data (4242... is the public Stripe test card number).
"""

import json

import pytest

from profilepilot.errors import ConflictError, PolicyError, ProfilePilotError
from profilepilot.identity import (
    SENSITIVE_FIELDS, IdentityStore, card_brand, derived_values, field_key, luhn_ok, normalize_origin,
)

TEST_CARD = "4242 4242 4242 4242"


@pytest.fixture
def ids(store):
    return IdentityStore(store)


def test_create_with_aliases_and_normalisation(ids):
    ident = ids.create("Me", {"firstname": " Jane ", "surname": "Doe", "dob": "03/14/1990", "zip": "94105",
                              "email": "jane@example.test", "phone": "+1 (555) 010-0000", "country_code": "us"})
    assert ident.values == {
        "first_name": "Jane", "last_name": "Doe", "birth_date": "1990-03-14", "postal_code": "94105",
        "email": "jane@example.test", "phone": "+1 (555) 010-0000", "country_code": "US",
    }
    with pytest.raises(ConflictError):
        ids.create("me")
    with pytest.raises(ProfilePilotError, match="Email: not a valid email"):
        ids.create("bad", {"email": "nope"})
    with pytest.raises(ProfilePilotError, match="Unknown identity field"):
        ids.create("bad2", {"favourite_colour": "blue"})


def test_sensitive_fields_cannot_be_set_through_normal_paths(ids):
    ids.create("Me")
    for field in ("ssn", "card_number", "cvv", "card_exp_month", "password"):
        with pytest.raises(PolicyError, match="sensitive"):
            ids.update("Me", {field: "123"})
        with pytest.raises(PolicyError):
            ids.create(f"x-{field}", {field: "123"})


def test_sensitive_values_live_only_in_the_secret_store(ids, store):
    ident = ids.create("Me", {"first_name": "Jane", "last_name": "Doe"})
    ids.set_sensitive("Me", "card_number", TEST_CARD)
    ids.set_sensitive("Me", "cvv", "123")
    ids.set_sensitive("Me", "card_exp_month", "4")
    ids.set_sensitive("Me", "card_exp_year", "31")
    ids.set_sensitive("Me", "ssn", "000-12-3456")
    raw = (store.root / "identities.json").read_text()
    assert "4242424242424242" not in raw and "000-12-3456" not in raw and '"123"' not in raw
    view = ids.masked("Me")
    assert view["fields"]["card_number"] == "visa •••• 4242"
    assert view["fields"]["ssn"] == "•••-••-3456"
    assert view["fields"]["card_cvv"] == "set"
    assert "4242424242424242" not in json.dumps(view)
    # fill values: sensitive only on request, derived values computed
    plain = ids.fill_values("Me")
    assert "card_number" not in plain and plain["full_name"] == "Jane Doe" and plain["card_name"] == "Jane Doe"
    full = ids.fill_values("Me", include_sensitive=True)
    assert full["card_number"] == "4242424242424242" and full["card_exp"] == "04/31" and full["card_exp_full"] == "04/2031"
    assert full["card_type"] == "visa" and full["ssn_digits"] == "000123456" and full["ssn_serial"] == "3456"
    # clearing removes from the secret store
    ids.set_sensitive("Me", "cvv", None)
    assert "card_cvv" not in ids.fill_values("Me", include_sensitive=True)
    assert store.secrets.get(f"identity:{ident.id}:card_cvv") is None
    # deleting the identity removes all its secrets
    ids.delete("Me")
    assert store.secrets.get(f"identity:{ident.id}:card_number") is None


def test_card_validation():
    assert luhn_ok("4242424242424242") and not luhn_ok("4242424242424241")
    assert card_brand("5555555555554444") == "mastercard" and card_brand("378282246310005") == "amex"


def test_bad_sensitive_values_are_rejected_without_echoing_them(ids):
    ids.create("Me")
    with pytest.raises(ProfilePilotError) as exc:
        ids.set_sensitive("Me", "card_number", "4242 4242 4242 4241")
    assert "Luhn" in str(exc.value) and "4241" not in str(exc.value)
    with pytest.raises(ProfilePilotError, match="9 digits"):
        ids.set_sensitive("Me", "ssn", "12345")


def test_sensitive_origin_policy(ids):
    ids.create("Me")
    with pytest.raises(PolicyError, match="profilepilot identity allow"):
        ids.check_sensitive_origin("Me", "https://shop.example.com/checkout")
    ids.allow_origin("Me", "Shop.Example.com/anything")
    assert ids.get("Me").allowed_origins == ["https://shop.example.com"]
    assert ids.check_sensitive_origin("Me", "https://shop.example.com/pay?x=1") == "https://shop.example.com"
    with pytest.raises(PolicyError):  # different subdomain
        ids.check_sensitive_origin("Me", "https://evil.shop.example.com/")
    with pytest.raises(PolicyError, match="HTTPS"):
        ids.check_sensitive_origin("Me", "http://shop.example.com/")
    with pytest.raises(PolicyError, match="HTTPS"):  # prefix trick must not count as localhost
        ids.check_sensitive_origin("Me", "http://127.0.0.1.evil.test/")
    ids.allow_origin("Me", "http://127.0.0.1:8000")
    assert ids.check_sensitive_origin("Me", "http://127.0.0.1:8000/form") == "http://127.0.0.1:8000"
    ids.disallow_origin("Me", "https://shop.example.com")
    with pytest.raises(PolicyError):
        ids.check_sensitive_origin("Me", "https://shop.example.com/")


def test_helpers():
    assert field_key("ZIP") == "postal_code" and field_key("cvc") == "card_cvv" and field_key("DOB") == "birth_date"
    assert normalize_origin("https://a.test:443/x") == "https://a.test"
    assert normalize_origin("http://a.test:8080/x") == "http://a.test:8080"
    assert derived_values({"birth_date": "1990-03-14"}) == {"birth_year": "1990", "birth_month": "03", "birth_day": "14"}
    assert {"ssn", "card_number", "card_cvv", "card_exp_month", "card_exp_year", "password"} == SENSITIVE_FIELDS


def test_origins_keep_non_default_ports_and_reject_non_web_pages(ids):
    assert normalize_origin("https://Shop.example.test:443/pay") == "https://shop.example.test"
    assert normalize_origin("http://shop.example.test:80/") == "http://shop.example.test"
    # only the scheme's own default port is dropped: these are different origins
    assert normalize_origin("http://shop.example.test:443") == "http://shop.example.test:443"
    assert normalize_origin("https://shop.example.test:80") == "https://shop.example.test:80"
    assert normalize_origin("http://[::1]:8080/x") == "http://[::1]:8080"
    for page in ("about:blank", "data:text/html,<p>x</p>", "chrome://newtab", "https://x.test:notaport/"):
        with pytest.raises(ProfilePilotError, match="Not a web origin"):
            normalize_origin(page)
    ids.create("Me")
    for page in ("about:blank", "data:text/html,<p>x</p>"):
        with pytest.raises(PolicyError, match="only works on http"):
            ids.check_sensitive_origin("Me", page)


def test_card_numbers_and_ssns_are_refused_as_plain_values(ids):
    ids.create("Me", {"phone": "+49 4111 1111 1111 1"})  # a long phone number is not a card number
    for key, value in (("company", TEST_CARD), ("username", "4242424242424242"), ("city", "000-12-3456"),
                       ("address_line2", "000 12 3456")):
        with pytest.raises(PolicyError, match="profilepilot identity secret") as exc:
            ids.update("Me", {key: value})
        assert value not in str(exc.value) and "Nothing was saved" in str(exc.value)
        with pytest.raises(PolicyError):
            ids.create(f"Other {key}", {key: value})
    assert ids.get("Me").values == {"phone": "+49 4111 1111 1111 1"}
    ids.update("Me", {"company": "Example Test Co 4242", "postal_code": "12345-6789"})  # ordinary values pass
