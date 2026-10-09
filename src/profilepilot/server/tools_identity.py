"""``identity_*`` and ``form_*`` MCP tools: the user's saved identities and form autofill.

An identity (:mod:`profilepilot.identity`) holds details the *user* entered: name, email, phone,
address, date of birth, ... (plain, in ``identities.json``) and sensitive values - card number,
expiry, CVV, SSN, password - that live only in the OS secret store. Policy:

* The model may read and change non-sensitive values (``identity_show`` / ``identity_create`` /
  ``identity_update``). Sensitive values are only ever shown masked (``visa •••• 4242``) and only
  the user stores them, in a terminal (``profilepilot identity secret NAME FIELD``): the tools
  refuse them and name that exact command. Deleting an identity is CLI-only.
* ``form_autofill`` fills non-sensitive fields only.
* ``form_autofill_sensitive`` fills card / SSN / password fields. Every call needs the user's
  approval (``anthropic/requiresUserInteraction``; ChatGPT asks because the tool is destructive);
  the top-level page must be on an origin the user allow-listed for the identity
  (``profilepilot identity allow``), checked *before* any secret is read; and the fill is stopped if
  the page moves to another origin meanwhile (so a second detection pass can never run on a
  foreign page). Child frames of another origin get nothing unless the user allow-listed that
  origin too, or (card fields only) it is a known payment processor's frame
  (:func:`frame_origin_policy`). Every value it entered is redacted from later page reads of the
  profile (:meth:`AppState.redact`). Remote (HTTP) servers only offer it with
  ``--allow-sensitive-autofill``.
* Output never contains values: autofill reports name field kinds, page labels and methods.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from functools import partial
from typing import Annotated, Any, Callable, Iterable, Literal
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from playwright.async_api import Locator, Page
from pydantic import Field

from ..automation.autofill import (
    COUNTRY_MISMATCH,
    KIND_SOURCES,
    NOT_SECRET_KINDS,
    NOT_STORED,
    NOT_VISIBLE,
    THIRD_PARTY_FRAME,
    AutofillReport,
    autofill,
    detect_fields,
    dispose_fields,
)
from ..automation.manager import ProfileSession, is_shardx_ref
from ..cli import shell_name as cli_name
from ..errors import NotFoundError, PolicyError, ProfilePilotError
from ..identity import FIELDS, SENSITIVE_FIELDS, Identity, IdentityStore, field_key, normalize_origin
from .app import (
    MIN_REDACTED_LENGTH,
    REQUIRES_USER_INTERACTION,
    AppState,
    NoneOK,
    ProfileArg,
    TabArg,
    add_tool,
    get_state,
    is_blank,
    run_sync,
)
from .tools_browser import after_action, clip_text, open_page, respond, target

log = logging.getLogger("profilepilot.server")

IdentityRefArg = Annotated[str, Field(description="Identity name, id or unique id prefix (see identity_list).")]
IdentityArg = Annotated[
    str,
    NoneOK,
    Field(description="Identity name or id (default: the identity linked to the profile; see identity_list)."),
]
FieldValuesArg = Annotated[
    dict[str, str | int | None] | None,
    Field(description="Identity fields, e.g. {\"first_name\": \"Jane\", \"email\": \"jane@example.com\", \"zip\": "
                      "\"94105\", \"birth_date\": \"1990-03-14\"}. Keys: first_name, middle_name, last_name, full_name, "
                      "gender, birth_date, email, username, phone, company, street, address_line2, city, state, "
                      "postal_code, country, country_code, card_name (aliases such as zip, dob, address work)."),
]
FillFieldsArg = Annotated[
    list[str] | None,
    Field(description="Only these identity fields, e.g. ['email', 'phone'] or ['card_number', 'card_cvv'] "
                      "(keys from identity_show; aliases such as zip, dob or cvv work). Default: all."),
]
MethodArg = Annotated[
    Literal["paste", "human", "type", "fill"],
    Field(description="How text fields are filled: paste (default) = a real paste through the system clipboard "
                      "(Ctrl/Cmd+Shift+V; the user's clipboard is restored), human = key by key with human timing "
                      "(slow: about 3-4 s per field, so a long form takes a minute), type = key by key, fill = set "
                      "the value at once. Selects, radios and date inputs are chosen directly."),
]
ScopeRefArg = Annotated[
    str,
    NoneOK,
    Field(description="Only fields inside this element (a ref from browser_snapshot, e.g. the <form>)."),
]
ScopeSelectorArg = Annotated[
    str,
    NoneOK,
    Field(description="Only fields inside the first visible element matching this CSS selector (when there is "
                      "no ref)."),
]
OverwriteArg = Annotated[bool, Field(description="Also replace values that are already in the form.")]

SensitiveFieldsArg = Annotated[
    list[str] | None,
    Field(description="Only these fields, e.g. ['card_number', 'card_cvv'] or ['ssn']: at least one card, SSN or "
                      "password key (non-sensitive keys listed with them are filled too). Default: every stored "
                      "sensitive field."),
]

SENSITIVE_KEYS_TEXT = ", ".join(k for k in FIELDS if k in SENSITIVE_FIELDS)
NO_SUBMIT_NOTE = ("Nothing was submitted: check the form (browser_snapshot) and confirm with the user before "
                  "you submit it.")
SENSITIVE_NO_SUBMIT_NOTE = ("Nothing was submitted: check which fields are filled with form_detect (values are not "
                            "shown) and confirm with the user before you submit it.")
_KEYISH = re.compile(r"[A-Za-z][A-Za-z _-]{0,39}")

CARD_KINDS = frozenset({"card_number", "card_exp", "card_exp_month", "card_exp_year", "card_cvv", "card_name",
                        "card_type"})
PAYMENT_FRAME_HOSTS = frozenset({"js.stripe.com"})
PAYMENT_FRAME_SUFFIXES = (".braintreegateway.com", ".adyen.com", ".checkout.com", ".paypal.com", ".squareup.com")
"""Card fields (never SSN or password) may go into iframes of these payment processors (https only)
on an allow-listed page without allow-listing the processor's origin too."""
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


# ---------------------------------------------------------------------- helpers


def secret_command(identity: Identity | str, key: str = "<field>") -> str:
    ref = cli_name(identity.name, identity.id) if isinstance(identity, Identity) else cli_name(identity)
    return f"profilepilot identity secret {ref} {key}"


def allow_command(identity: Identity, origin: str = "<origin>") -> str:
    return f"profilepilot identity allow {cli_name(identity.name, identity.id)} {origin}"


def _sensitive_refusal(keys: list[str], identity: Identity | str) -> PolicyError:
    one = len(keys) == 1
    commands = "\n".join(f"  {secret_command(identity, key)}" for key in keys)
    return PolicyError(
        f"{', '.join(keys)} {'is a sensitive field' if one else 'are sensitive fields'}: only the user can store "
        f"{'it' if one else 'them'}, in a terminal, never through the AI. Nothing was saved. Leave "
        f"{'it' if one else 'them'} out of this call and ask the user to run:\n{commands}\n"
        "Never repeat card numbers, SSNs or passwords in the chat."
    )


def _split_values(values: dict[str, Any] | None) -> tuple[dict[str, str | None], list[str]]:
    """Canonical keys -> value (None removes) and the sensitive keys that were given."""
    clean: dict[str, str | None] = {}
    sensitive: list[str] = []
    for raw_key, raw_value in (values or {}).items():
        try:
            key = field_key(raw_key)
        except ProfilePilotError:  # never echo something that may be a value passed as a key
            text = str(raw_key).strip()
            shown = f"'{text}'" if _KEYISH.fullmatch(text) else "(a value that is not a field name)"
            raise ProfilePilotError(f"Unknown identity field {shown}. Known fields: {', '.join(FIELDS)}.") from None
        if FIELDS[key].sensitive:
            if key not in sensitive:
                sensitive.append(key)
            continue
        clean[key] = None if raw_value is None else str(raw_value)
    return clean, sensitive


def wanted_keys(fields: Iterable[str] | None) -> set[str] | None:
    """Identity keys for a ``fields`` argument (keys, aliases or detected kinds such as card_exp);
    None when no restriction was given."""
    if fields is None:
        return None
    keys: set[str] = set()
    unknown: list[str] = []
    for raw in fields:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            keys.add(field_key(text))
            continue
        except ProfilePilotError:
            pass
        kind = re.sub(r"[\s\-]+", "_", text.lower())
        if kind in KIND_SOURCES:
            keys |= KIND_SOURCES[kind]
        else:  # never echo something that may be a value passed by mistake
            unknown.append(text if _KEYISH.fullmatch(text) else "(a value that is not a field name)")
    if unknown:
        raise ProfilePilotError(f"Unknown field(s): {', '.join(unknown)}. Use identity field keys: "
                                f"{', '.join(FIELDS)}.")
    if not keys:
        raise ProfilePilotError("'fields' is empty; leave it out to fill every field.")
    return keys


def kind_is_sensitive(kind: str) -> bool:
    return kind not in NOT_SECRET_KINDS and bool(KIND_SOURCES.get(kind, frozenset({kind})) & SENSITIVE_FIELDS)


def _origin_of(url: str) -> str | None:
    try:
        return normalize_origin(url) if url.lower().startswith(("http://", "https://")) else None
    except ProfilePilotError:
        return None


def is_payment_frame_origin(origin: str) -> bool:
    """An https origin of a known card-field iframe provider (Stripe, Braintree, Adyen, ...)."""
    parts = urlsplit(origin)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host in PAYMENT_FRAME_HOSTS or host.endswith(PAYMENT_FRAME_SUFFIXES))


def frame_origin_policy(top_origin: str, allowed_origins: Iterable[str]) -> Callable[[str, str], bool]:
    """Which child frames may receive sensitive values on the allow-listed page ``top_origin``:
    frames of that same origin, of an origin the user allow-listed for the identity
    (``profilepilot identity allow``), and - for card fields only - the known payment processors'
    https frames. An SSN or a password never goes into a frame of another origin unless the user
    allow-listed it."""
    allowed = set(allowed_origins)

    def allows(kind: str, frame_origin: str) -> bool:
        origin = _origin_of(frame_origin or "")
        if origin is None:
            return False
        if origin == top_origin:
            return True
        if origin in allowed:
            host = urlsplit(origin).hostname or ""
            return origin.startswith("https://") or host in _LOCAL_HOSTS
        return kind in CARD_KINDS and is_payment_frame_origin(origin)

    return allows


def secret_variants(values: dict[str, str], typed: Iterable[str] = ()) -> set[str]:
    """The sensitive values in ``values`` as a page may show them - raw, digits only, a card number
    grouped by 4 (or 4-6-5) with spaces or dashes, an SSN with and without dashes - plus the exact
    ``typed`` texts. Only strings of at least :data:`MIN_REDACTED_LENGTH` characters (shorter ones,
    such as a CVV or the last 4 digits that the masked view shows anyway, would mangle other text)."""
    out: set[str] = set()

    def add(text: str) -> None:
        text = str(text or "").strip()
        if len(text) >= MIN_REDACTED_LENGTH:
            out.add(text)

    for key in SENSITIVE_FIELDS:
        value = values.get(key)
        if not value:
            continue
        add(value)
        digits = re.sub(r"\D", "", value)
        if key == "card_number" and digits:
            add(digits)
            groupings = [[digits[i:i + 4] for i in range(0, len(digits), 4)]]
            if len(digits) == 15:  # American Express: 4-6-5
                groupings.append([digits[:4], digits[4:10], digits[10:]])
            for groups in groupings:
                add(" ".join(groups))
                add("-".join(groups))
                for part in groups:
                    add(part)
        elif key == "ssn" and len(digits) == 9:
            add(digits)
            add(f"{digits[:3]}-{digits[3:5]}-{digits[5:]}")
            add(f"{digits[:3]} {digits[3:5]} {digits[5:]}")
    for text in typed:
        add(text)
    return out


def report_hints(report: AutofillReport, ident: Identity, *, sensitive_tool: bool) -> list[str]:
    """Actionable lines for skip reasons the model should do something about (never values)."""
    reasons = [str(e.get("reason", "")) for e in report.skipped]
    lines: list[str] = []
    if COUNTRY_MISMATCH in reasons:
        lines.append("The form's preselected country differs from the identity's: the address is inconsistent "
                     "until you call form_autofill(..., fields=['country', 'state'], overwrite=true).")
    if NOT_VISIBLE in reasons:
        lines.append("Fields marked 'not visible' are covered or clipped on the page, so a person could not see "
                     "them: close a dialog or banner that covers the form and try again (hidden fields are never "
                     "filled).")
    if sensitive_tool:
        frames = sorted({r[len(THIRD_PARTY_FRAME) + 2:-1] for r in reasons if r.startswith(THIRD_PARTY_FRAME + " (")})
        for origin in frames:
            lines.append(f"Fields in a frame from {origin} were not filled: that origin is not allowed for sensitive "
                         "autofill (card fields in Stripe, Braintree, Adyen, Checkout.com, PayPal and Square frames "
                         "are). If the user trusts it, they run in a terminal: "
                         f"{allow_command(ident, _origin_of(origin) or origin)}")
        missing = {k for e in report.skipped if e.get("reason") == NOT_STORED
                   for k in KIND_SOURCES.get(str(e.get("kind")), frozenset()) & SENSITIVE_FIELDS}
        if missing:
            keys = [k for k in FIELDS if k in missing]
            lines.append(f"Not stored in identity '{ident.name}': {', '.join(keys)}. Only the user can add "
                         f"{'it' if len(keys) == 1 else 'them'}, in a terminal: "
                         + "; ".join(secret_command(ident, k) for k in keys))
    return lines


def progress_reporter(ctx: Context) -> Callable[[int, int], Any]:
    """Autofill progress as MCP progress notifications (clients show them for long human-typing fills)."""

    async def report(done: int, total: int) -> None:
        await ctx.report_progress(done, total)

    return report


async def resolve_identity(state: AppState, profile: str, identity: str | None) -> Identity:
    """``identity`` if given, else the profile's linked identity (clear errors otherwise)."""

    def resolve() -> Identity:
        ids = IdentityStore(state.store)
        if not is_blank(identity):
            return ids.get(str(identity))
        if is_shardx_ref(profile):
            raise ProfilePilotError("ShardX profiles have no linked identity: pass identity=<name> (see identity_list).")
        prof = state.store.get_profile(profile)
        if not prof.identity_id:
            names = ", ".join(i.name for i in ids.list()[:20])
            hint = (f"Existing identities: {names}." if names else
                    "There are no identities yet: create one with identity_create(name, fields) from details the "
                    "user gives you (never invent them).")
            raise ProfilePilotError(
                f"Profile '{prof.name}' has no linked identity. Pass identity=<name>, or link one with "
                f"profile_update(profile='{prof.name}', identity=<name>). {hint}"
            )
        try:
            return ids.get(prof.identity_id)
        except NotFoundError:
            raise ProfilePilotError(
                f"The identity linked to profile '{prof.name}' no longer exists. Pass identity=<name>, or link "
                "another one with profile_update(profile, identity=<name>)."
            ) from None

    return await run_sync(resolve)


async def _scope(session: ProfileSession, page: Page, ref: str | None, selector: str | None) -> Locator | None:
    if is_blank(ref) and is_blank(selector):
        return None
    locator, _label = await target(session, page, ref, selector)
    return locator


def _field_line(row: dict[str, Any]) -> str:
    line = f"- {row['kind']}: {row['field']} ({row['control']}"
    if row.get("part"):
        line += f", part {row['part']}"
    line += ")"
    if row.get("frame"):
        line += f" [in frame {row['frame']}]"
    if kind_is_sensitive(row["kind"]):
        line += " [sensitive]"
    if row.get("has_value"):
        line += " - already has a value"
    return line


def report_text(report: AutofillReport, header: str) -> str:
    """The autofill report for the model: kinds, labels and methods only (never values)."""
    if not report.filled and not report.skipped:
        return (f"{header}\nNo fillable form fields matched (none were detected, or none of the requested kinds). "
                "Check the page with form_detect or browser_snapshot; the form may still be loading.")
    lines = [f"{header} {len(report.filled)} field(s) filled, {len(report.skipped)} skipped. Values are not shown."]
    lines += [f"  {line}" for line in report.lines()]
    return "\n".join(lines)


# ---------------------------------------------------------------------- identity tools


async def identity_list(ctx: Context) -> str:
    """List the user's saved identities (personal details for form autofill) and the profiles linked
    to them. Values are not shown here: use identity_show."""
    state = get_state(ctx)

    def collect() -> tuple[list[Identity], dict[str, list[str]]]:
        users: dict[str, list[str]] = {}
        for p in state.store.list_profiles():
            if p.identity_id:
                users.setdefault(p.identity_id, []).append(p.name)
        return IdentityStore(state.store).list(), users

    identities, users = await run_sync(collect)
    if not identities:
        return ("No identities yet. Create one with identity_create(name, fields) from details the user gives you "
                "(never invent them). Card, SSN and password values are added by the user in a terminal.")
    lines = [f"{len(identities)} identity(ies):"]
    for ident in identities:
        parts = [f"- {ident.name} (id {ident.id})", f"fields: {', '.join(k for k in FIELDS if k in ident.values) or 'none'}"]
        if ident.sensitive_set:
            parts.append(f"sensitive stored: {', '.join(k for k in FIELDS if k in ident.sensitive_set)}")
        if ident.allowed_origins:
            parts.append("sensitive autofill allowed on " + ", ".join(ident.allowed_origins))
        if users.get(ident.id):
            parts.append("linked to " + ", ".join(users[ident.id]))
        lines.append(" | ".join(parts))
    return "\n".join(lines)


async def identity_show(ctx: Context, identity: IdentityRefArg) -> str:
    """Show an identity: non-sensitive values in clear, card / SSN / password masked (e.g.
    'visa •••• 4242'), the sites where sensitive autofill is allowed and the linked profiles."""
    state = get_state(ctx)

    def collect() -> tuple[Identity, dict[str, Any], list[str]]:
        ids = IdentityStore(state.store)
        ident = ids.get(identity)
        return ident, ids.masked(ident.id), [p.name for p in state.store.profiles_using_identity(ident.id)]

    ident, view, linked = await run_sync(collect)
    lines = [f"Identity '{ident.name}' (id {ident.id})"]
    if ident.notes:
        lines.append(f"Notes: {clip_text(ident.notes, 300)}")
    fields = view["fields"]
    shown = [k for k in FIELDS if k in fields]
    if shown:
        lines += [f"  {k}: {fields[k]}{' (sensitive)' if k in SENSITIVE_FIELDS else ''}" for k in shown]
    else:
        lines.append("  (no values yet: add them with identity_update)")
    missing = [k for k in FIELDS if k in SENSITIVE_FIELDS and k not in fields]
    if missing:
        lines.append(f"Sensitive fields not stored: {', '.join(missing)}. Only the user can add them, in a terminal: "
                     f"{secret_command(ident)}")
    if ident.allowed_origins:
        lines.append("Sensitive autofill is allowed on: " + ", ".join(ident.allowed_origins))
    else:
        lines.append(f"Sensitive autofill is not allowed on any site yet (the user allows one with: "
                     f"{allow_command(ident)}).")
    lines.append(f"Linked profiles: {', '.join(linked)}." if linked else
                 f"No profile is linked; link one with profile_update(profile, identity='{ident.name}').")
    return "\n".join(lines)


async def identity_create(
    ctx: Context,
    name: Annotated[str, Field(description="Unique name, e.g. 'Jane (personal)'.")],
    fields: FieldValuesArg = None,
    notes: Annotated[str, NoneOK, Field(description="Free-form notes.")] = None,
) -> str:
    """Save a new identity for form autofill from details the user gave you (names, email, phone,
    address, date of birth, company, ...). Never invent values. Card numbers, CVVs, SSNs and
    passwords are refused: the user stores those in a terminal."""
    state = get_state(ctx)
    values, sensitive = _split_values(fields)
    if sensitive:
        raise _sensitive_refusal(sensitive, name)
    keep = {k: v for k, v in values.items() if v is not None and v.strip()}
    ident = await run_sync(partial(IdentityStore(state.store).create, name, keep, notes=notes or ""))
    stored = [k for k in FIELDS if k in ident.values]
    return "\n".join([
        f"Created identity '{ident.name}' (id {ident.id}) with {len(stored)} field(s): {', '.join(stored) or 'none'}.",
        f"Use it with form_autofill(profile, identity='{ident.name}'), or link it to a profile with "
        f"profile_update(profile, identity='{ident.name}').",
        f"Card, SSN and password values ({SENSITIVE_KEYS_TEXT}) can only be added by the user, in a terminal: "
        f"{secret_command(ident)}",
    ])


async def identity_update(
    ctx: Context,
    identity: IdentityRefArg,
    fields: FieldValuesArg = None,
    name: Annotated[str, NoneOK, Field(description="New name.")] = None,
    notes: Annotated[str, NoneOK, Field(description="New notes ('' clears them).")] = None,
) -> str:
    """Change an identity's non-sensitive values (a null or '' value removes the field), its name or
    its notes. Card numbers, CVVs, SSNs and passwords are refused: the user sets them in a terminal."""
    state = get_state(ctx)
    values, sensitive = _split_values(fields)
    ids = IdentityStore(state.store)
    if sensitive:
        raise _sensitive_refusal(sensitive, await run_sync(ids.get, identity))
    if not values and name is None and notes is None:
        raise ProfilePilotError("Nothing to change: give fields, name or notes.")
    updated = await run_sync(partial(ids.update, identity, values, name=name, notes=notes))
    removed = [k for k, v in values.items() if v is None or not v.strip()]
    changed = [k for k in values if k not in removed]
    parts = []
    if changed:
        parts.append("set " + ", ".join(changed))
    if removed:
        parts.append("removed " + ", ".join(removed))
    if name is not None:
        parts.append("renamed")
    if notes is not None:
        parts.append("notes changed")
    return f"Updated identity '{updated.name}' (id {updated.id}): {'; '.join(parts)}."


# ---------------------------------------------------------------------- form tools


async def form_detect(
    ctx: Context,
    profile: ProfileArg,
    scope_ref: ScopeRefArg = None,
    scope_selector: ScopeSelectorArg = None,
    tab: TabArg = None,
) -> str:
    """List the form fields on the page that autofill can fill: kind (first_name, email, card_number,
    ...), label, control type, iframe origin (card fields often live in a cross-origin iframe) and
    split-field part. Read-only: nothing is typed or changed."""
    state, session, page = await open_page(ctx, profile, tab, interactive=False)
    scope = await _scope(session, page, scope_ref, scope_selector)
    fields = await detect_fields(page, scope=scope)
    try:
        rows = [f.as_dict() for f in fields]
    finally:
        await dispose_fields(fields)
    if not rows:
        body = ("No fillable form fields detected (visible, enabled inputs, selects and text areas, also in "
                "iframes). The form may still be loading: browser_wait_for, then try again, or check the page with "
                "browser_snapshot.")
        return await respond(session, page, body, state=state)
    lines = [f"Detected {len(rows)} field(s) (nothing was typed):"] + [_field_line(r) for r in rows]
    lines.append("Fill them from the user's saved identity with form_autofill(profile, identity?).")
    if any(kind_is_sensitive(r["kind"]) for r in rows):
        if state.sensitive_autofill:
            lines.append("[sensitive] fields (card, SSN, password) are filled only by form_autofill_sensitive: the "
                         "user approves each call, and the site must be allow-listed for the identity.")
        else:
            lines.append("[sensitive] fields (card, SSN, password) cannot be filled through this server; the user "
                         "fills them in the profile's window.")
    return await respond(session, page, "\n".join(lines), state=state)


async def form_autofill(
    ctx: Context,
    profile: ProfileArg,
    identity: IdentityArg = None,
    fields: FillFieldsArg = None,
    method: MethodArg = "paste",
    overwrite: OverwriteArg = False,
    scope_ref: ScopeRefArg = None,
    scope_selector: ScopeSelectorArg = None,
    tab: TabArg = None,
) -> str:
    """Fill the page's form from the user's saved identity (default: the profile's linked identity):
    names, email, phone, address, date of birth, company and so on, in every frame. Card, SSN and
    password fields are skipped (form_autofill_sensitive). Values are never shown, fields that
    already have a value are kept (unless overwrite), and nothing is submitted."""
    state = get_state(ctx)
    wanted = wanted_keys(fields)
    if wanted and wanted & SENSITIVE_FIELDS:
        raise ProfilePilotError(f"{', '.join(k for k in FIELDS if k in wanted & SENSITIVE_FIELDS)}: card, SSN and "
                                "password fields are only filled by form_autofill_sensitive.")
    ident = await resolve_identity(state, profile, identity)
    state, session, page = await open_page(ctx, profile, tab)
    scope = await _scope(session, page, scope_ref, scope_selector)
    values = await run_sync(partial(IdentityStore(state.store).fill_values, ident.id, include_sensitive=False,
                                    fields=wanted))
    if not values:
        raise ProfilePilotError(f"Identity '{ident.name}' has no values{' for those fields' if wanted else ''} to fill; "
                                "add them with identity_update (details the user gives you).")
    report = await autofill(page, values, method=method, sensitive_keys=set(SENSITIVE_FIELDS),
                            clipboard_lock=state.clipboard_lock, only=wanted, overwrite=overwrite, scope=scope,
                            progress=progress_reporter(ctx))
    log.info("form_autofill: %d filled, %d skipped (identity %s)", len(report.filled), len(report.skipped), ident.id)
    lines = [report_text(report, f"Autofill from identity '{ident.name}' ({method}):")]
    lines += report_hints(report, ident, sensitive_tool=False)
    if any(e.get("reason", "").startswith("sensitive field") for e in report.skipped):
        lines.append("Card, SSN and password fields were left empty. If the user wants them filled, call "
                     "form_autofill_sensitive (the user approves it)." if state.sensitive_autofill else
                     "Card, SSN and password fields were left empty: the user fills them in the profile's window.")
    lines.append(NO_SUBMIT_NOTE)
    return await after_action(state, session, page, "\n".join(lines))


async def form_autofill_sensitive(
    ctx: Context,
    profile: ProfileArg,
    identity: IdentityArg = None,
    fields: SensitiveFieldsArg = None,
    method: MethodArg = "paste",
    overwrite: OverwriteArg = False,
    scope_ref: ScopeRefArg = None,
    scope_selector: ScopeSelectorArg = None,
    tab: TabArg = None,
) -> str:
    """Fill card number, expiry, CVV, SSN or password fields from the user's saved identity. Works
    only on sites the user allow-listed for that identity, and the user approves every call. Values
    are never shown to you, and later page reads of this profile show them as [redacted] (snapshots
    mask such fields); a screenshot would show them, so do not take one of the filled form. Card
    fields in a payment provider's iframe (Stripe, Braintree, ...) are filled; other iframes of a
    different site only if the user allow-listed them. Run form_autofill first for the other fields.
    'fields' limits what is filled and must name at least one sensitive key; non-sensitive keys
    listed with them are filled too. Default: every stored sensitive field."""
    state = get_state(ctx)
    if not state.sensitive_autofill:  # not registered then; defence in depth
        raise PolicyError("Sensitive autofill is not enabled on this server.")
    wanted = wanted_keys(fields)
    if wanted is not None and not wanted & SENSITIVE_FIELDS:  # never widen a narrow request to every secret
        raise ProfilePilotError("'fields' names no card, SSN or password field: fill the other fields with "
                                f"form_autofill. Sensitive keys: {SENSITIVE_KEYS_TEXT}.")
    secret_keys = wanted & SENSITIVE_FIELDS if wanted else set(SENSITIVE_FIELDS)
    keys = secret_keys | ((wanted or set()) - SENSITIVE_FIELDS)
    ident = await resolve_identity(state, profile, identity)
    if not secret_keys & set(ident.sensitive_set):
        names = ", ".join(k for k in FIELDS if k in secret_keys)
        raise ProfilePilotError(f"Identity '{ident.name}' has no sensitive value stored for {names}. Only the user "
                                f"can add them, in a terminal: {secret_command(ident)}")
    state, session, page = await open_page(ctx, profile, tab)
    store = IdentityStore(state.store)
    # The policy check comes first: no secret is read for a page outside the allow-list.
    origin = await run_sync(store.check_sensitive_origin, ident.id, page.url)
    scope = await _scope(session, page, scope_ref, scope_selector)
    values = await run_sync(partial(store.fill_values, ident.id, include_sensitive=True, fields=keys))
    filled_url = page.url
    report: AutofillReport | None = None
    try:
        report = await autofill_on_origin(
            page, origin, values, method=method, sensitive_keys=set(SENSITIVE_FIELDS),
            clipboard_lock=state.clipboard_lock, only=keys, overwrite=overwrite, scope=scope,
            sensitive_frame_origins=frame_origin_policy(origin, ident.allowed_origins), progress=progress_reporter(ctx))
    finally:  # also after a partial or stopped fill: whatever reached the page is redacted from now on
        state.remember_secrets(session.key, secret_variants(values, report.secret_texts if report else ()),
                               filled_url)
    assert report is not None
    log.info("form_autofill_sensitive: %d filled, %d skipped on %s (identity %s)", len(report.filled),
             len(report.skipped), origin, ident.id)
    lines = [report_text(report, f"Sensitive autofill from identity '{ident.name}' on {origin} ({method}):")]
    lines += report_hints(report, ident, sensitive_tool=True)
    lines.append(SENSITIVE_NO_SUBMIT_NOTE)
    return await after_action(state, session, page, "\n".join(lines))


async def autofill_on_origin(page: Page, origin: str, values: dict[str, str], **kwargs: Any) -> AutofillReport:
    """:func:`autofill`, stopped as soon as the top-level page leaves ``origin``.

    The engine detects fields again after a first pass; without this guard a page that navigates
    away (a script, a meta refresh) could receive sensitive values on a site the user never
    allow-listed."""
    if _origin_of(page.url) != origin:
        raise PolicyError("The page left the allow-listed site before autofill started; nothing was filled.")
    moved: list[str] = []
    task = asyncio.ensure_future(autofill(page, values, **kwargs))
    # Always retrieve the outcome, also when the task is left to finish its clean-up on its own.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def on_navigated(frame: Any) -> None:
        if frame == page.main_frame and not task.done() and not moved and _origin_of(frame.url) != origin:
            moved.append(frame.url)
            task.cancel()

    page.on("framenavigated", on_navigated)
    try:
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:  # the tool call itself was cancelled: stop the fill too
            task.cancel()
            raise
    finally:
        with contextlib.suppress(Exception):
            page.remove_listener("framenavigated", on_navigated)
    if task.cancelled():
        where = (_origin_of(moved[0]) or clip_text(moved[0], 80)) if moved else "another page"
        raise PolicyError(f"The page moved to {where} while sensitive fields were being filled, so autofill stopped. "
                          "Check the page and tell the user.")
    return task.result()  # a navigation after the fill had finished does not undo it


# ---------------------------------------------------------------------- registration


def register(server: MCPServer, *, sensitive: bool = True) -> None:
    """Register the identity and form tools (``form_autofill_sensitive`` only when ``sensitive``)."""
    add_tool(server, identity_list, title="List identities", read_only=True, destructive=False, idempotent=True,
             open_world=False, invoking="Listing identities…", invoked="Identities listed")
    add_tool(server, identity_show, title="Show identity", read_only=True, destructive=False, idempotent=True,
             open_world=False, invoking="Reading the identity…", invoked="Identity shown")
    add_tool(server, identity_create, title="Create identity", read_only=False, destructive=False, idempotent=False,
             open_world=False, invoking="Saving the identity…", invoked="Identity saved")
    add_tool(server, identity_update, title="Update identity", read_only=False, destructive=False, idempotent=True,
             open_world=False, invoking="Updating the identity…", invoked="Identity updated")
    add_tool(server, form_detect, title="Detect form fields", read_only=True, destructive=False, idempotent=True,
             open_world=False, invoking="Finding form fields…", invoked="Form fields found")
    add_tool(server, form_autofill, title="Autofill form", read_only=False, destructive=False, idempotent=True,
             open_world=True, invoking="Filling the form…", invoked="Form filled")
    if sensitive:
        add_tool(server, form_autofill_sensitive, title="Autofill card, SSN or password", read_only=False,
                 destructive=True, idempotent=False, open_world=True, invoking="Filling sensitive form fields",
                 invoked="Sensitive form fields filled", meta={REQUIRES_USER_INTERACTION: True})


__all__ = ["register", "resolve_identity", "wanted_keys", "kind_is_sensitive", "autofill_on_origin", "report_text"]
