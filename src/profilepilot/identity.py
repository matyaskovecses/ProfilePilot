"""User-entered identities for form autofill.

An identity is a named set of personal values (name, address, phone, email, date of birth, ...)
plus *sensitive* values (SSN, payment card, password). Nothing is generated or randomised: the user
enters every value.

Storage:
* Non-sensitive values live in ``identities.json`` (like Chrome's own autofill addresses).
* Sensitive values live only in the secret store (OS keyring / DPAPI) under
  ``identity:<id>:<field>``; the JSON records only *which* sensitive fields are set.

Policy (enforced by callers, see :meth:`IdentityStore.check_sensitive_origin`):
* Sensitive values are never returned to an AI model - only masked forms such as ``visa •••• 4242``.
* Sensitive values can only be filled into pages whose origin the user explicitly allow-listed
  for that identity (protects against prompt-injected exfiltration forms).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from pydantic import Field

from .errors import AmbiguousError, ConflictError, NotFoundError, PolicyError, ProfilePilotError
from .jsonio import lock_for, read_json, write_json
from .models import _Model, utcnow
from .paths import new_id

# --------------------------------------------------------------------------- field registry


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    sensitive: bool = False
    group: str = "personal"
    normalize: Callable[[str], str] | None = None
    help: str = ""


def _strip(v: str) -> str:
    return " ".join(str(v).split())


def _email(v: str) -> str:
    v = v.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
        raise ValueError("not a valid email address")
    return v


def _phone(v: str) -> str:
    v = _strip(v)
    digits = re.sub(r"\D", "", v)
    if not 6 <= len(digits) <= 15:
        raise ValueError("a phone number needs 6-15 digits")
    return v


def _date(v: str) -> str:
    v = v.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(v, fmt).date()
            break
        except ValueError:
            continue
    else:
        raise ValueError("use YYYY-MM-DD (or MM/DD/YYYY)")
    if not date(1900, 1, 1) <= d <= date.today():
        raise ValueError("date of birth out of range")
    return d.isoformat()


def _ssn(v: str) -> str:
    digits = re.sub(r"\D", "", v)
    if len(digits) != 9:
        raise ValueError("an SSN has 9 digits")
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


def luhn_ok(number: str) -> bool:
    total, parity = 0, len(number) % 2
    for i, ch in enumerate(number):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card_number(v: str) -> str:
    digits = re.sub(r"[\s-]", "", v)
    if not digits.isdigit() or not 12 <= len(digits) <= 19:
        raise ValueError("a card number has 12-19 digits")
    if not luhn_ok(digits):
        raise ValueError("card number fails the Luhn check (typo?)")
    return digits


def _exp_month(v: str) -> str:
    v = v.strip()
    if not v.isdigit() or not 1 <= int(v) <= 12:
        raise ValueError("expiry month must be 1-12")
    return f"{int(v):02d}"


def _exp_year(v: str) -> str:
    v = v.strip()
    if not v.isdigit() or len(v) not in (2, 4):
        raise ValueError("expiry year must be YY or YYYY")
    year = int(v) + (2000 if len(v) == 2 else 0)
    if not 2000 <= year <= 2100:
        raise ValueError("expiry year out of range")
    return str(year)


def _cvv(v: str) -> str:
    v = v.strip()
    if not v.isdigit() or len(v) not in (3, 4):
        raise ValueError("a CVV has 3 or 4 digits")
    return v


def _country_code(v: str) -> str:
    v = v.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", v):
        raise ValueError("use a 2-letter ISO country code, e.g. US, DE")
    return v


def _gender(v: str) -> str:
    v = v.strip().lower()
    aliases = {"m": "male", "f": "female", "man": "male", "woman": "female"}
    v = aliases.get(v, v)
    if v not in ("male", "female", "other"):
        raise ValueError("use male, female or other")
    return v


FIELDS: dict[str, FieldSpec] = {
    spec.key: spec
    for spec in [
        FieldSpec("first_name", "First name", normalize=_strip),
        FieldSpec("middle_name", "Middle name", normalize=_strip),
        FieldSpec("last_name", "Last name", normalize=_strip),
        FieldSpec("full_name", "Full name", normalize=_strip, help="derived from first/middle/last if unset"),
        FieldSpec("gender", "Gender", normalize=_gender),
        FieldSpec("birth_date", "Date of birth", normalize=_date, help="YYYY-MM-DD"),
        FieldSpec("email", "Email", normalize=_email),
        FieldSpec("username", "Username", normalize=str.strip),
        FieldSpec("phone", "Phone", normalize=_phone, help="as you want it typed, e.g. +1 555 123 4567"),
        FieldSpec("company", "Company", normalize=_strip),
        FieldSpec("street", "Address line 1", group="address", normalize=_strip),
        FieldSpec("address_line2", "Address line 2", group="address", normalize=_strip),
        FieldSpec("city", "City", group="address", normalize=_strip),
        FieldSpec("state", "State / region", group="address", normalize=_strip, help="name or code, e.g. California or CA"),
        FieldSpec("postal_code", "ZIP / postal code", group="address", normalize=_strip),
        FieldSpec("country", "Country", group="address", normalize=_strip, help="name, e.g. United States"),
        FieldSpec("country_code", "Country code", group="address", normalize=_country_code, help="ISO-2, e.g. US"),
        FieldSpec("card_name", "Name on card", group="card", normalize=_strip),
        FieldSpec("card_number", "Card number", sensitive=True, group="card", normalize=_card_number),
        FieldSpec("card_exp_month", "Card expiry month", sensitive=True, group="card", normalize=_exp_month),
        FieldSpec("card_exp_year", "Card expiry year", sensitive=True, group="card", normalize=_exp_year),
        FieldSpec("card_cvv", "Card CVV", sensitive=True, group="card", normalize=_cvv),
        FieldSpec("ssn", "SSN", sensitive=True, normalize=_ssn),
        FieldSpec("password", "Password", sensitive=True, normalize=lambda v: v),
    ]
}
SENSITIVE_FIELDS = frozenset(k for k, s in FIELDS.items() if s.sensitive)
ALIASES = {
    "firstname": "first_name", "given_name": "first_name", "lastname": "last_name", "surname": "last_name",
    "family_name": "last_name", "name": "full_name", "dob": "birth_date", "birthday": "birth_date",
    "address": "street", "address1": "street", "address_line1": "street", "address2": "address_line2",
    "zip": "postal_code", "zip_code": "postal_code", "zipcode": "postal_code", "postcode": "postal_code",
    "region": "state", "province": "state", "tel": "phone", "phone_number": "phone", "mobile": "phone",
    "cc_number": "card_number", "cc": "card_number", "card": "card_number", "cvv": "card_cvv", "cvc": "card_cvv",
    "card_cvc": "card_cvv", "csc": "card_cvv", "exp_month": "card_exp_month", "exp_year": "card_exp_year",
    "cardholder": "card_name", "cardholder_name": "card_name", "social_security_number": "ssn",
    "organization": "company", "org": "company",
}


def field_key(name: str) -> str:
    """Canonical field key for ``name`` (accepts aliases like ``zip``, ``dob``, ``cvv``)."""
    key = re.sub(r"[\s\-]+", "_", str(name).strip().lower())
    key = ALIASES.get(key, key)
    if key not in FIELDS:
        raise ProfilePilotError(f"Unknown identity field '{name}'. Known fields: {', '.join(FIELDS)}.")
    return key


def normalize_value(key: str, value: str) -> str:
    spec = FIELDS[key]
    value = "" if value is None else str(value)
    if not value.strip():
        raise ProfilePilotError(f"{spec.label}: value is empty.")
    try:
        return spec.normalize(value) if spec.normalize else value.strip()
    except ValueError as exc:
        raise ProfilePilotError(f"{spec.label}: {exc}.") from None


def card_brand(number: str) -> str:
    if re.match(r"^4", number):
        return "visa"
    if re.match(r"^(5[1-5]|2[2-7])", number):
        return "mastercard"
    if re.match(r"^3[47]", number):
        return "amex"
    if re.match(r"^(6011|65|64[4-9])", number):
        return "discover"
    return "card"


_SSN_SHAPE = re.compile(r"\d{3}[- ]\d{2}[- ]\d{4}")


def looks_like_secret(key: str, value: str) -> str | None:
    """``"ssn"`` or ``"card_number"`` when a value given for the non-sensitive field ``key`` looks
    like one (an SSN pattern, or a Luhn-valid number of a known card brand; phone numbers are not
    checked for the latter, they can be that long)."""
    text = str(value or "").strip()
    if _SSN_SHAPE.fullmatch(text):
        return "ssn"
    compact = re.sub(r"[\s-]", "", text)
    if (key != "phone" and compact.isdigit() and 13 <= len(compact) <= 19 and luhn_ok(compact)
            and card_brand(compact) != "card"):
        return "card_number"
    return None


def refuse_secret_like(key: str, value: str, identity_name: str) -> None:
    """Raise PolicyError when a non-sensitive value looks like a card number or SSN (it would be
    stored in plain JSON and shown in clear). The value is never echoed."""
    kind = looks_like_secret(key, value)
    if kind:
        what = "an SSN" if kind == "ssn" else "a card number"
        raise PolicyError(
            f"The value for '{key}' looks like {what}. Sensitive values are only stored by the user, in a "
            f"terminal: profilepilot identity secret \"{identity_name}\" {kind}. Nothing was saved."
        )


def mask(key: str, value: str) -> str:
    """Masked rendering of a sensitive value that is safe to show to a model."""
    if key == "card_number":
        return f"{card_brand(value)} •••• {value[-4:]}"
    if key == "ssn":
        return f"•••-••-{value[-4:]}"
    return "set"


def normalize_origin(origin: str) -> str:
    """``https://Shop.example.com/checkout`` -> ``https://shop.example.com``."""
    text = origin.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        hostname, port_number = parts.hostname, parts.port  # .port raises for "about:blank", "data:..."
    except ValueError:
        raise ProfilePilotError(f"Not a web origin: {origin!r}") from None
    if parts.scheme not in ("https", "http") or not hostname:
        raise ProfilePilotError(f"Not a web origin: {origin!r}")
    default_port = 443 if parts.scheme == "https" else 80  # only the scheme's own default is dropped
    port = f":{port_number}" if port_number and port_number != default_port else ""
    host = f"[{hostname.lower()}]" if ":" in hostname else hostname.lower()
    return f"{parts.scheme}://{host}{port}"


# --------------------------------------------------------------------------- models / store


class Identity(_Model):
    id: str
    name: str
    notes: str = ""
    values: dict[str, str] = Field(default_factory=dict)
    sensitive_set: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)
    """Origins where sensitive fields may be autofilled (managed by the user via the CLI)."""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def summary(self) -> dict[str, Any]:
        """Model-safe view: non-sensitive values in clear, sensitive ones masked by the caller."""
        return {"id": self.id, "name": self.name, "fields": sorted([*self.values, *self.sensitive_set])}


class IdentityStore:
    """Identities live next to the profile store and share its secret store."""

    def __init__(self, store) -> None:  # store: profilepilot.store.Store (avoid import cycle)
        self.store = store
        self.file = store.root / "identities.json"

    # ------------------------------------------------------------------ io

    def _load(self) -> list[Identity]:
        data = read_json(self.file, {}) or {}
        out = []
        for item in data.get("identities", []):
            try:
                out.append(Identity.model_validate(item))
            except Exception:
                continue
        return out

    def _save(self, items: list[Identity]) -> None:
        write_json(self.file, {"identities": [i.model_dump(mode="json") for i in items]})

    def _secret_key(self, identity_id: str, field: str) -> str:
        return f"identity:{identity_id}:{field}"

    # ------------------------------------------------------------------ queries

    def list(self) -> list[Identity]:
        return sorted(self._load(), key=lambda i: (i.name.casefold(), i.id))

    def get(self, ref: str) -> Identity:
        ref = (ref or "").strip()
        items = self._load()
        for i in items:
            if i.id == ref.lower():
                return i
        by_name = [i for i in items if i.name.casefold() == ref.casefold()]
        if len(by_name) == 1:
            return by_name[0]
        if len(ref) >= 3:
            by_prefix = [i for i in items if i.id.startswith(ref.lower())]
            if len(by_prefix) == 1:
                return by_prefix[0]
            if len(by_prefix) > 1:
                raise AmbiguousError(f"'{ref}' matches several identities: " + ", ".join(i.name for i in by_prefix))
        names = ", ".join(i.name for i in items[:20]) or "none yet"
        raise NotFoundError(f"Identity '{ref}' not found. Existing identities: {names}.")

    # ------------------------------------------------------------------ mutations

    def create(self, name: str, values: dict[str, str] | None = None, *, notes: str = "") -> Identity:
        name = _valid_name(name)
        clean = self._clean_values(values or {}, allow_sensitive=False)
        for key, value in clean.items():
            refuse_secret_like(key, value, name)
        with lock_for(self.file):
            items = self._load()
            if any(i.name.casefold() == name.casefold() for i in items):
                raise ConflictError(f"An identity named '{name}' already exists.")
            ident = Identity(id=new_id(), name=name, notes=notes, values=clean)
            items.append(ident)
            self._save(items)
        return ident

    def update(self, ref: str, values: dict[str, str | None] | None = None, *, name: str | None = None,
               notes: str | None = None) -> Identity:
        """Set (or, with value None / "", remove) non-sensitive values."""
        with lock_for(self.file):
            items = self._load()
            ident = _find(items, self.get(ref).id)
            for raw_key, raw_val in (values or {}).items():
                key = field_key(raw_key)
                if FIELDS[key].sensitive:
                    raise PolicyError(
                        f"'{key}' is sensitive and can only be set by the user in a terminal: "
                        f"profilepilot identity secret \"{ident.name}\" {key}"
                    )
                if raw_val is None or not str(raw_val).strip():
                    ident.values.pop(key, None)
                else:
                    value = normalize_value(key, raw_val)
                    refuse_secret_like(key, value, ident.name)
                    ident.values[key] = value
            if name is not None:
                new = _valid_name(name)
                if any(i.name.casefold() == new.casefold() and i.id != ident.id for i in items):
                    raise ConflictError(f"An identity named '{new}' already exists.")
                ident.name = new
            if notes is not None:
                ident.notes = notes
            ident.updated_at = utcnow()
            self._save(items)
            return ident

    def set_sensitive(self, ref: str, field: str, value: str | None) -> Identity:
        """Store (or clear with None) a sensitive value. Only call this from user-driven code paths
        (the CLI) - never from a tool an AI model can invoke."""
        key = field_key(field)
        if not FIELDS[key].sensitive:
            raise ProfilePilotError(f"'{key}' is not a sensitive field; set it with 'identity set'.")
        with lock_for(self.file):
            items = self._load()
            ident = _find(items, self.get(ref).id)
            secret_key = self._secret_key(ident.id, key)
            if value is None or not str(value).strip():
                self.store.secrets.delete(secret_key)
                ident.sensitive_set = [k for k in ident.sensitive_set if k != key]
            else:
                self.store.secrets.set(secret_key, normalize_value(key, value))
                if key not in ident.sensitive_set:
                    ident.sensitive_set.append(key)
            ident.updated_at = utcnow()
            self._save(items)
            return ident

    def allow_origin(self, ref: str, origin: str) -> Identity:
        norm = normalize_origin(origin)
        with lock_for(self.file):
            items = self._load()
            ident = _find(items, self.get(ref).id)
            if norm not in ident.allowed_origins:
                ident.allowed_origins.append(norm)
            self._save(items)
            return ident

    def disallow_origin(self, ref: str, origin: str) -> Identity:
        norm = normalize_origin(origin)
        with lock_for(self.file):
            items = self._load()
            ident = _find(items, self.get(ref).id)
            ident.allowed_origins = [o for o in ident.allowed_origins if o != norm]
            self._save(items)
            return ident

    def delete(self, ref: str) -> str:
        with lock_for(self.file):
            items = self._load()
            ident = self.get(ref)
            for key in ident.sensitive_set:
                self.store.secrets.delete(self._secret_key(ident.id, key))
            self._save([i for i in items if i.id != ident.id])
        return ident.name

    # ------------------------------------------------------------------ views

    def masked(self, ref: str) -> dict[str, Any]:
        """Model-safe view: non-sensitive values in clear, sensitive values masked."""
        ident = self.get(ref)
        fields: dict[str, str] = dict(sorted(ident.values.items()))
        for key in sorted(ident.sensitive_set):
            value = self.store.secrets.get(self._secret_key(ident.id, key))
            fields[key] = mask(key, value) if value else "missing (re-enter it)"
        return {
            "id": ident.id, "name": ident.name, "notes": ident.notes, "fields": fields,
            "sensitive_allowed_origins": ident.allowed_origins,
        }

    def fill_values(self, ref: str, *, include_sensitive: bool = False, fields: Iterable[str] | None = None) -> dict[str, str]:
        """Resolved values for autofill (incl. derived ones). Sensitive values only when
        ``include_sensitive`` - callers must enforce :meth:`check_sensitive_origin` first."""
        ident = self.get(ref)
        vals = dict(ident.values)
        if include_sensitive:
            for key in ident.sensitive_set:
                value = self.store.secrets.get(self._secret_key(ident.id, key))
                if value:
                    vals[key] = value
        vals.update(derived_values(vals))
        if fields is not None:
            wanted = {field_key(f) for f in fields}
            vals = {k: v for k, v in vals.items() if k in wanted or _derived_from(k) & wanted}
        return vals

    def check_sensitive_origin(self, ref: str, page_url: str) -> str:
        """Raise PolicyError unless ``page_url``'s origin is allow-listed for sensitive autofill."""
        ident = self.get(ref)
        try:
            origin = normalize_origin(page_url)
        except ProfilePilotError:
            raise PolicyError("Sensitive autofill only works on http(s) pages.") from None
        host = urlsplit(origin).hostname or ""
        if not origin.startswith("https://") and host not in ("127.0.0.1", "localhost", "::1"):
            raise PolicyError(f"Sensitive autofill requires HTTPS (page origin is {origin}).")
        if origin not in ident.allowed_origins:
            raise PolicyError(
                f"Sensitive fields of identity '{ident.name}' are not allowed on {origin}. If the user trusts this "
                f"site, they must run in a terminal: profilepilot identity allow \"{ident.name}\" {origin}"
            )
        return origin

    def _clean_values(self, values: dict[str, str], *, allow_sensitive: bool) -> dict[str, str]:
        out: dict[str, str] = {}
        for raw_key, raw_val in values.items():
            key = field_key(raw_key)
            if FIELDS[key].sensitive and not allow_sensitive:
                raise PolicyError(f"'{key}' is sensitive; set it with: profilepilot identity secret <identity> {key}")
            if raw_val is not None and str(raw_val).strip():
                out[key] = normalize_value(key, raw_val)
        return out


# --------------------------------------------------------------------------- derived values

DERIVED_SOURCES = {
    "full_name": {"first_name", "middle_name", "last_name"},
    "birth_day": {"birth_date"}, "birth_month": {"birth_date"}, "birth_year": {"birth_date"},
    "phone_digits": {"phone"}, "card_exp": {"card_exp_month", "card_exp_year"},
    "card_exp_full": {"card_exp_month", "card_exp_year"}, "card_type": {"card_number"},
    "ssn_digits": {"ssn"}, "ssn_area": {"ssn"}, "ssn_group": {"ssn"}, "ssn_serial": {"ssn"},
    "card_name": {"first_name", "last_name"},
}


def _derived_from(key: str) -> set[str]:
    return DERIVED_SOURCES.get(key, set())


def derived_values(vals: dict[str, str]) -> dict[str, str]:
    """Values computed from others (only where the user did not set them explicitly)."""
    out: dict[str, str] = {}
    if "full_name" not in vals:
        parts = [vals.get(k) for k in ("first_name", "middle_name", "last_name") if vals.get(k)]
        if parts:
            out["full_name"] = " ".join(parts)
    if "card_name" not in vals and (vals.get("first_name") or vals.get("last_name")):
        out["card_name"] = " ".join(p for p in (vals.get("first_name"), vals.get("last_name")) if p)
    if vals.get("birth_date"):
        y, m, d = vals["birth_date"].split("-")
        out.update(birth_year=y, birth_month=m, birth_day=d)
    if vals.get("phone"):
        out["phone_digits"] = re.sub(r"\D", "", vals["phone"])
    if vals.get("card_exp_month") and vals.get("card_exp_year"):
        out["card_exp"] = f"{vals['card_exp_month']}/{vals['card_exp_year'][-2:]}"
        out["card_exp_full"] = f"{vals['card_exp_month']}/{vals['card_exp_year']}"
    if vals.get("card_number"):
        out["card_type"] = card_brand(vals["card_number"])
    if vals.get("ssn"):
        digits = vals["ssn"].replace("-", "")
        out.update(ssn_digits=digits, ssn_area=digits[:3], ssn_group=digits[3:5], ssn_serial=digits[5:])
    return out


def _valid_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
        raise ProfilePilotError("Identity name must be 1-64 printable characters.")
    return name


def _find(items: list[Identity], identity_id: str) -> Identity:
    for i in items:
        if i.id == identity_id:
            return i
    raise NotFoundError(f"Identity {identity_id} no longer exists.")
