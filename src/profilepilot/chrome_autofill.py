"""Read the addresses a Chromium browser itself saved ("Addresses and more"), for autofill.

Chrome keeps the user's saved addresses in each profile's ``Web Data`` SQLite database: one row per
address in ``addresses`` and one row per field in ``address_type_tokens`` (``type`` is Chromium's
``FieldType`` number). Verified on Chrome 154 (schema version 154) by saving an address through
chrome://settings/addresses and reading the rows back.

What is read - and what never is:

* Only ``addresses`` and ``address_type_tokens``, and only the field types in :data:`FIELD_TYPES`
  (names, email, phone, company, street, city, state, ZIP, country). Values are read **live** every
  time, so edits made in Chrome apply at once.
* Never: ``credit_cards`` / ``masked_*`` / ``local_ibans`` (payment data, encrypted anyway),
  ``Login Data`` (passwords), the ``autofill`` form-history table, or Chrome's "Autofill AI" store
  (passport, driver's licence, national ID numbers). Chrome stores no SSN. A guard
  (:func:`_guard`) makes the SQLite connection refuse any other table.
* The database is opened read-only with SQLite's ``immutable=1``: no lock is taken and nothing is
  ever written, so this works while that browser is running and cannot disturb it.

Sources are referred to as:

* ``chrome`` - the *active* (last used) profile of the first installed browser (Chrome, then Edge,
  Brave, Chromium);
* ``chrome:<browser>`` - the active profile of that browser (``chrome:edge``);
* ``chrome:<browser>/<profile>`` - a profile by folder (``Default``, ``Profile 1``) or display name;
* ``profile`` - the ProfilePilot profile's own browser (addresses saved in its window).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import AmbiguousError, NotFoundError, ProfilePilotError

log = logging.getLogger("profilepilot.chrome_autofill")

#: Chromium ``FieldType`` numbers (components/autofill/core/browser/field_types.h) -> identity keys.
FIELD_TYPES: dict[int, str] = {
    3: "first_name",        # NAME_FIRST
    4: "middle_name",       # NAME_MIDDLE
    5: "last_name",         # NAME_LAST
    7: "full_name",         # NAME_FULL
    9: "email",             # EMAIL_ADDRESS
    14: "phone",            # PHONE_HOME_WHOLE_NUMBER
    60: "company",          # COMPANY_NAME
    77: "street_address",   # ADDRESS_HOME_STREET_ADDRESS (lines separated by "\n")
    33: "city",             # ADDRESS_HOME_CITY
    34: "state",            # ADDRESS_HOME_STATE
    35: "postal_code",      # ADDRESS_HOME_ZIP
    36: "country_code",     # ADDRESS_HOME_COUNTRY (ISO 3166-1 alpha-2)
}
#: ``AutofillProfile::RecordType``: where Chrome keeps the address.
RECORD_TYPES = {0: "on this device", 1: "in the Google account", 2: "account home", 3: "account work",
                4: "account name & email"}
ALLOWED_TABLES = frozenset({"addresses", "address_type_tokens"})

SOURCE_PREFIX = "chrome"
PROFILE_SOURCE = "profile"
_READ_ATTEMPTS = 3


# --------------------------------------------------------------------------- browsers on this machine


def _user_data_dirs() -> dict[str, Path]:
    """Default user-data folders of the supported browsers (the kinds of :data:`paths.BROWSER_KINDS`)."""
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return {
            "chrome": local / "Google/Chrome/User Data",
            "edge": local / "Microsoft/Edge/User Data",
            "brave": local / "BraveSoftware/Brave-Browser/User Data",
            "chromium": local / "Chromium/User Data",
            "chrome-beta": local / "Google/Chrome Beta/User Data",
            "chrome-dev": local / "Google/Chrome Dev/User Data",
            "chrome-canary": local / "Google/Chrome SxS/User Data",
            "edge-beta": local / "Microsoft/Edge Beta/User Data",
            "edge-dev": local / "Microsoft/Edge Dev/User Data",
            "edge-canary": local / "Microsoft/Edge SxS/User Data",
        }
    if sys.platform == "darwin":
        sup = home / "Library" / "Application Support"
        return {
            "chrome": sup / "Google/Chrome", "edge": sup / "Microsoft Edge",
            "brave": sup / "BraveSoftware/Brave-Browser", "chromium": sup / "Chromium",
            "chrome-beta": sup / "Google/Chrome Beta", "chrome-dev": sup / "Google/Chrome Dev",
            "chrome-canary": sup / "Google/Chrome Canary", "edge-beta": sup / "Microsoft Edge Beta",
            "edge-dev": sup / "Microsoft Edge Dev", "edge-canary": sup / "Microsoft Edge Canary",
        }
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return {
        "chrome": config / "google-chrome", "edge": config / "microsoft-edge",
        "brave": config / "BraveSoftware/Brave-Browser", "chromium": config / "chromium",
        "chrome-beta": config / "google-chrome-beta", "chrome-dev": config / "google-chrome-unstable",
        "edge-beta": config / "microsoft-edge-beta", "edge-dev": config / "microsoft-edge-dev",
    }


@dataclass(frozen=True)
class ChromeSource:
    """One browser profile whose saved addresses can be read."""

    browser: str
    """Browser kind (``chrome``, ``edge`` ...) or ``profilepilot`` for a ProfilePilot profile."""
    profile_dir: Path
    """The browser profile folder (``.../User Data/Default``)."""
    profile_name: str
    active: bool = False

    @property
    def ref(self) -> str:
        if self.browser == "profilepilot":
            return PROFILE_SOURCE
        return f"{SOURCE_PREFIX}:{self.browser}/{self.profile_dir.name}"

    @property
    def label(self) -> str:
        from .paths import BROWSER_LABELS

        if self.browser == "profilepilot":
            return f"this ProfilePilot profile's browser ({self.profile_name})"
        name = BROWSER_LABELS.get(self.browser, self.browser)
        return f"{name} profile '{self.profile_name}'" + (" (active)" if self.active else "")

    @property
    def web_data(self) -> Path:
        return self.profile_dir / "Web Data"


def _profiles_of(browser: str, user_data_dir: Path) -> list[ChromeSource]:
    try:
        state = json.loads((user_data_dir / "Local State").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        state = {}
    info = (state.get("profile") or {}).get("info_cache") or {}
    last = (state.get("profile") or {}).get("last_used") or "Default"
    dirs = list(info) or (["Default"] if (user_data_dir / "Default").is_dir() else [])
    out = []
    for name in dirs:
        folder = user_data_dir / name
        if not (folder / "Web Data").is_file():
            continue
        display = str((info.get(name) or {}).get("name") or name)
        out.append(ChromeSource(browser, folder, display, active=(name == last)))
    out.sort(key=lambda s: (not s.active, s.profile_dir.name.casefold()))
    return out


def discover_sources(browsers: Iterable[str] | None = None) -> list[ChromeSource]:
    """Every installed browser profile with a ``Web Data`` file, active profiles first."""
    found: list[ChromeSource] = []
    for kind, udd in _user_data_dirs().items():
        if browsers is not None and kind not in set(browsers):
            continue
        if udd.is_dir():
            found.extend(_profiles_of(kind, udd))
    return found


def profile_source(store: Any, profile: Any) -> ChromeSource:
    """The source for a ProfilePilot profile's own browser (its ``udd/Default``)."""
    return ChromeSource("profilepilot", store.user_data_dir(profile.id) / "Default", profile.name, active=True)


def is_source_ref(value: str | None) -> bool:
    text = (value or "").strip().lower()
    return text == SOURCE_PREFIX or text.startswith(SOURCE_PREFIX + ":") or text == PROFILE_SOURCE


def resolve_source(ref: str, *, store: Any = None, profile: Any = None) -> ChromeSource:
    """Turn ``chrome`` / ``chrome:<browser>`` / ``chrome:<browser>/<profile>`` / ``profile`` into a source."""
    text = (ref or "").strip()
    low = text.lower()
    if low == PROFILE_SOURCE:
        if store is None or profile is None:
            raise ProfilePilotError("The 'profile' source needs a ProfilePilot profile.")
        return profile_source(store, profile)
    if not (low == SOURCE_PREFIX or low.startswith(SOURCE_PREFIX + ":")):
        raise ProfilePilotError(f"Not a browser source: {ref!r}. Use chrome, chrome:edge, chrome:chrome/Default or profile.")
    rest = text[len(SOURCE_PREFIX):].lstrip(":")
    browser, _, wanted = rest.partition("/")
    browser = browser.strip().lower()
    sources = discover_sources([browser] if browser else None)
    if browser and not sources:
        raise NotFoundError(f"No saved browser data found for '{browser}' on this computer.")
    if not sources:
        raise NotFoundError("No Chrome, Edge, Brave or Chromium profile with saved data was found on this computer.")
    if not wanted:
        if not browser:  # plain "chrome": the first browser that has one, its active profile
            first = sources[0].browser
            sources = [s for s in sources if s.browser == first]
        active = [s for s in sources if s.active]
        return (active or sources)[0]
    matches = [s for s in sources if s.profile_dir.name.casefold() == wanted.strip().casefold()]
    matches = matches or [s for s in sources if s.profile_name.casefold() == wanted.strip().casefold()]
    if len(matches) > 1:
        raise AmbiguousError(f"'{wanted}' matches several profiles: " + ", ".join(s.ref for s in matches))
    if not matches:
        names = ", ".join(f"{s.profile_dir.name} ({s.profile_name})" for s in sources)
        raise NotFoundError(f"Browser profile '{wanted}' not found. Available: {names}.")
    return matches[0]


# --------------------------------------------------------------------------- addresses


@dataclass
class ChromeAddress:
    """One address saved in the browser. ``values`` use identity field keys."""

    guid: str
    values: dict[str, str] = field(default_factory=dict)
    use_count: int = 0
    use_date: int = 0
    record_type: int = 0
    label: str = ""

    def summary(self) -> str:
        """Model-safe one-liner: the name and the locality only (no street, email or phone)."""
        v = self.values
        name = v.get("full_name") or " ".join(x for x in (v.get("first_name"), v.get("last_name")) if x) \
            or v.get("company") or "(no name)"
        place = ", ".join(x for x in (v.get("city"), v.get("state"), v.get("country_code")) if x)
        extra = f" [{self.label}]" if self.label else ""
        return f"{name}{' - ' + place if place else ''}{extra}"

    def identity_values(self) -> dict[str, str]:
        """Identity field values (validated; anything Chrome stores in another shape is dropped)."""
        from .identity import FIELDS, normalize_value

        raw = dict(self.values)
        street = raw.pop("street_address", "")
        lines = [line.strip() for line in street.replace("\r", "").split("\n") if line.strip()]
        if lines:
            raw["street"] = lines[0]
            if len(lines) > 1:
                raw["address_line2"] = ", ".join(lines[1:])
        if raw.get("country_code"):
            from .automation.autofill import find_country

            country = find_country(raw["country_code"])
            if country is not None and country.names:
                raw.setdefault("country", country.names[0])
        out: dict[str, str] = {}
        for key, value in raw.items():
            if key not in FIELDS or FIELDS[key].sensitive or not str(value).strip():
                continue
            try:
                out[key] = normalize_value(key, value)
            except ProfilePilotError:
                log.debug("skipping a saved %s that does not validate", key)
        return out


def _guard(action: int, arg1: str | None, arg2: str | None, db: str | None, trigger: str | None) -> int:
    """SQLite authorizer: reads of anything but the two address tables are denied outright."""
    if action == sqlite3.SQLITE_READ and arg1 is not None and arg1 not in ALLOWED_TABLES:
        return sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _connect(path: Path) -> sqlite3.Connection:
    quoted = path.resolve().as_posix().replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    uri = "file:" + quoted + "?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True, timeout=1.0)
    con.set_authorizer(_guard)
    return con


def read_addresses(source: ChromeSource) -> list[ChromeAddress]:
    """The source's saved addresses, most used first (Chrome's own ranking is frecency-based)."""
    path = source.web_data
    if not path.is_file():
        return []
    last: Exception | None = None
    for attempt in range(_READ_ATTEMPTS):  # an immutable read can catch a page mid-write: retry
        try:
            con = _connect(path)
            try:
                rows = con.execute(
                    "SELECT guid, use_count, use_date, record_type, label FROM addresses").fetchall()
                types = ",".join(str(t) for t in FIELD_TYPES)
                tokens = con.execute(
                    f"SELECT guid, type, value FROM address_type_tokens WHERE type IN ({types})").fetchall()
            finally:
                con.close()
            by_guid: dict[str, dict[str, str]] = {}
            for guid, kind, value in tokens:
                if value:
                    by_guid.setdefault(guid, {})[FIELD_TYPES[int(kind)]] = str(value)
            out = [ChromeAddress(guid=g, values=by_guid.get(g, {}), use_count=int(c or 0), use_date=int(d or 0),
                                 record_type=int(r or 0), label=str(l or ""))
                   for g, c, d, r, l in rows if by_guid.get(g)]
            out.sort(key=lambda a: (-a.use_count, -a.use_date))
            return out
        except sqlite3.DatabaseError as exc:  # schema change, a torn read, or not a database
            last = exc
            time.sleep(0.2)
    raise ProfilePilotError(f"Could not read the saved addresses of {source.label}: {type(last).__name__}.")


def pick_address(addresses: list[ChromeAddress], which: int | str | None = None) -> ChromeAddress:
    """``which``: None = the most used, an int = 1-based position in :func:`read_addresses` order,
    or a GUID (prefix)."""
    if not addresses:
        raise NotFoundError("That browser profile has no saved addresses (Chrome settings > Autofill > Addresses).")
    if which is None or str(which).strip() == "":
        return addresses[0]
    text = str(which).strip()
    if text.isdigit():
        index = int(text)
        if not 1 <= index <= len(addresses):
            raise NotFoundError(f"There are {len(addresses)} saved addresses; pick 1-{len(addresses)}.")
        return addresses[index - 1]
    matches = [a for a in addresses if a.guid.lower().startswith(text.lower())]
    if len(matches) != 1:
        raise NotFoundError(f"No single saved address matches '{text}'.")
    return matches[0]


def source_values(ref: str, *, address: int | str | None = None, store: Any = None, profile: Any = None
                  ) -> tuple[dict[str, str], ChromeSource, ChromeAddress]:
    """Identity values of one saved address of the ``ref`` source (non-sensitive fields only)."""
    source = resolve_source(ref, store=store, profile=profile)
    chosen = pick_address(read_addresses(source), address)
    return chosen.identity_values(), source, chosen


__all__ = [
    "ChromeAddress", "ChromeSource", "FIELD_TYPES", "PROFILE_SOURCE", "discover_sources", "is_source_ref",
    "pick_address", "profile_source", "read_addresses", "resolve_source", "source_values",
]
