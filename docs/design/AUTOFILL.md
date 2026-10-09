# Humanized typing, type-paste and identity autofill — implementation spec

Requested by the user. It extends ProfilePilot with three things:
1. **Humanized typing**: key-by-key input with human-like timing.
2. **Type-paste**: put text on the system clipboard and press **Ctrl+Shift+V** (Windows/Linux) or
   **⌘⇧V** (macOS), Chrome's "paste as plain text". The page receives a real, trusted paste.
3. **Identity autofill**: detect the fields of any form and fill them from a *user-entered*
   identity, using type-paste by default. This is what ShardX's "Shard Helper" does with generated
   identities. Here values are **never generated or randomised**.

`src/profilepilot/identity.py` (done, tested in `tests/test_identity.py`) is the data layer: field
registry `FIELDS`, `SENSITIVE_FIELDS`, aliases, validation and normalisation, and `IdentityStore`
with `create/update/set_sensitive/allow_origin/masked/fill_values/check_sensitive_origin`. Read it
first.

## 1. Clipboard: `src/profilepilot/automation/clipboard.py`

```python
class ClipboardUnavailable(ProfilePilotError): ...
@contextmanager
def clipboard_text(text: str, *, sensitive: bool, lock_path: Path, timeout: float = 10.0) -> Iterator[None]
    # 1. acquire a cross-process FileLock(lock_path) so concurrent profiles/servers never interleave
    # 2. snapshot the user's current clipboard (every format that round-trips as bytes; GDI handle
    #    formats such as CF_BITMAP may be skipped, but CF_DIB/CF_DIBV5 must be preserved)
    # 3. set `text` as CF_UNICODETEXT. When sensitive=True ALSO set the registered formats
    #    "ExcludeClipboardContentFromMonitorProcessing" (any data), "CanIncludeInClipboardHistory"
    #    = DWORD 0 and "CanUploadToCloudClipboard" = DWORD 0, so Windows clipboard history (Win+V)
    #    and cloud clipboard never keep SSNs or card numbers. On macOS also set the
    #    "org.nspasteboard.ConcealedType" marker.
    # 4. yield (the caller presses the paste shortcut and waits until the value lands)
    # 5. restore the snapshot (or clear the clipboard if it was empty), even on error
```
- **Windows:** use pywin32 `win32clipboard`. Retry `OpenClipboard` (another app may hold it) for up to
  ~1 s. Never leave the clipboard open.
- **macOS:** `pbcopy`/`pbpaste` (text only). The concealed marker needs AppKit via `pyobjc`; if that
  isn't installed, skip it and log a warning.
- **Linux:** `wl-copy`/`wl-paste` or `xclip`; otherwise raise `ClipboardUnavailable`.
- `paste_shortcut() -> str` returns `"Control+Shift+V"` (win/linux) or `"Meta+Shift+V"` (darwin).

## 2. Typing engine: `src/profilepilot/automation/typing.py`

```python
TypeMethod = Literal["fill", "type", "human", "paste"]
async def enter_text(page, locator, text: str, *, method: TypeMethod = "paste", clear: bool = True,
                     sensitive: bool = False, clipboard_lock: Path, rng: random.Random | None = None,
                     wpm: int | None = None) -> str   # returns the method actually used
```
- `fill`: `locator.fill()`. This is programmatic: no key events.
- `type`: `locator.press_sequentially(text, delay=40)`.
- `human`:
  - Click the field.
  - If `clear`, select all (ControlOrMeta+A) and press Backspace.
  - Type one character at a time: `page.keyboard.press(ch)` for printable ASCII, `keyboard.insert_text(ch)` for anything else.
  - Delays are sampled from a log-normal distribution (about 140–260 cpm by default, or `wpm`). Add longer pauses after spaces and punctuation, a rare 0.3–0.9 s "thinking" pause, and a short random wait before the first key. No typos.
  - All randomness comes from the `rng` argument, so tests are deterministic.
- `paste`:
  - Click the field. If `clear`, select all and press Backspace.
  - Inside `clipboard_text(text, sensitive=…)`, press `paste_shortcut()`.
  - Then wait up to ~1.5 s for the field's value to contain the text. Read it from the element value; for contenteditable, use innerText.
  - If it didn't land (clipboard busy or unavailable, or the page blocks paste), fall back to `human` and report `"human (paste failed: …)"`.
  - Never paste into `<select>`, `type=date|month|time|checkbox|radio|file|range|color`; those are handled by the autofill engine.
- **Verify empirically on Chrome 154 (Windows) and record the results in the module docstring:**
  - Does a CDP-dispatched `Control+Shift+V` (Playwright `keyboard.press`) paste the system clipboard into a focused input when the Chrome window is NOT the OS foreground window?
  - Does the page get a trusted `paste` event (`isTrusted == true`) and `inputType == "insertFromPaste"`?
  - Does `Control+V` behave the same?

  If Ctrl+Shift+V doesn't work but Ctrl+V does, still prefer Ctrl+Shift+V and fall back to Ctrl+V.
- **Never log the text.**

## 3. Autofill engine: `src/profilepilot/automation/autofill.py`

```python
@dataclass class DetectedField:  frame_url, kind, confidence, descriptor (short human label: tag/type/label/name, NO value),
                                 element (ElementHandle), control ("text"|"select"|"date"|"radio"|"checkbox"|"contenteditable"),
                                 group_index (for split fields)
async def detect_fields(page, *, scope: Locator | None = None) -> list[DetectedField]
async def autofill(page, values: dict[str, str], *, method="paste", sensitive_keys: set[str], clipboard_lock: Path,
                   only: set[str] | None = None, overwrite: bool = False, rng=None) -> AutofillReport
@dataclass class AutofillReport: filled: list[dict]; skipped: list[dict]   # entries: {kind, field: descriptor, method|reason}
```
- **Frames.** Scan the main frame and **all child frames, including cross-origin ones** (Stripe and Braintree card iframes) with `page.frames`. Skip invisible, disabled and readonly controls. Ignore `type=hidden|submit|button|image|reset|file`. Don't touch fields that already have a value unless `overwrite`.
- **Visibility (autofill phishing).** "Invisible" covers more than `display`/`visibility`/size: the
  ancestors are walked (across shadow roots) for a product of opacities below 0.1, `clip-path`
  `inset(>=50%)` / `circle(0)`, `clip: rect(0,0,0,0)`, clipping by an `overflow: hidden|clip` (or
  `contain: paint`) box in the field's containing-block chain down to under 2 px, zero-size scroll
  containers, and absolutely positioned fields beyond the right edge of the page. Child frames are
  scanned only when every `<iframe>` up the chain passes the same checks and is at least 10x10 px.
  Right before a field is filled it is scrolled into view and hit-tested (`elementFromPoint` at a
  few points, through shadow roots and every `<iframe>` up to the page): a covered field is skipped
  as "not visible (covered or clipped)". Limit: an overlay with `pointer-events: none` is invisible
  to the hit test.
- **Frame origins (sensitive values).** A sensitive value goes into a child frame only when
  `autofill(..., sensitive_frame_origins=policy)` allows that frame's origin *as it is right before
  the value is entered* (re-read, so a frame swapped to another origin between passes is caught)
  and the origin of every frame around it (a payment iframe nested in an untrusted frame is
  refused). Without a policy no sensitive value goes into any child frame (fail closed). Refused
  fields are skipped as "sensitive field in a third-party frame (<origin>)".
- **Detection signals, highest first:**
  1. The `autocomplete` attribute tokens. Map every standard token: `given-name`, `additional-name`, `family-name`, `name`, `email`, `username`, `new-password`/`current-password`, `tel`, `tel-national`, `tel-country-code`, `organization`, `street-address`, `address-line1/2`, `address-level1/2`, `postal-code`, `country`, `country-name`, `bday`, `bday-day/month/year`, `sex`, `cc-name`, `cc-given-name`, `cc-family-name`, `cc-number`, `cc-exp`, `cc-exp-month`, `cc-exp-year`, `cc-csc`, `cc-type`. A `section-*`/`billing`/`shipping` prefix is allowed.
  2. `input type` (email, tel, password; `date` combined with a birth signal).
  3. Regexes over name, id, placeholder, aria-label, the associated `<label>`, `aria-labelledby` and nearby preceding text. English plus common German, French and Spanish words.
  4. `inputmode` and `maxlength` hints.

  SSN detection: `ssn|social.?security|sozialversicherung` and the like. Card fields often use names such as `cardnumber`, `exp-date`, `cvc`, and Stripe uses `cardnumber` and `exp-date` in iframes. Write detection as one JS function evaluated per frame that returns element handles via `frame.evaluate_handle`. **Do not mutate the DOM**: no marker attributes.
- **Value mapping per control type.**
  - **Text:**
    - `card_exp` for a single "MM/YY" field (use maxlength or the placeholder to choose MM/YY or MM/YYYY).
    - `full_name` when only a single name field exists.
    - `phone` versus `phone_digits` from `type=tel` + maxlength or pattern.
    - Postal code as is.
  - **`<select>`:** match option value or text case-insensitively and fuzzily:
    - country: name, ISO-2 or ISO-3 (embed a compact ISO-3166 table);
    - state: name or code (embed US states and Canadian provinces);
    - month: `01`, `1`, `Jan`, `January`;
    - year: 2- or 4-digit;
    - day;
    - gender.
  - **Date input:** `locator.fill(YYYY-MM-DD)`.
  - **Radio:** gender. Click the radio whose label matches.
  - **Split fields:** consecutive inputs of the same kind:
    - phone 3-3-4;
    - SSN 3-2-4;
    - card 4×4;
    - DOB MM/DD/YYYY.

    Distribute the digits by maxlength, and fill each part.
- Use `typing.enter_text` with the requested method for text controls. Pass `sensitive=True` for keys in `sensitive_keys`. Small human pauses (150–600 ms) between fields when the method is `human` or `paste`.
- **The report never contains values**, only kinds, descriptors and the method used.
- **Policy (enforced by the server tool, double-checked here):** sensitive keys are filled only when present in `values`. The server passes them only after `IdentityStore.check_sensitive_origin()` succeeds for the **top-level** page URL. Its frame policy allows child frames of that same origin, origins the user allow-listed for the identity (`profilepilot identity allow`, https or localhost), and - for card kinds only - the https frames of known payment processors (`js.stripe.com`, `*.braintreegateway.com`, `*.adyen.com`, `*.checkout.com`, `*.paypal.com`, `*.squareup.com`). An SSN or a password never goes into a frame of another origin unless the user allow-listed it.

## 4. Profile ↔ identity link

- Add `identity_id: str | None = None` to `Profile` (models.py) and to `Profile.summary()`.
- `Store.update_profile(..., identity_id=...)` validates it through `IdentityStore(store).get()`; `None` or `""` clears it.
- `create_profile` accepts `identity_id`.

## 5. MCP tools (server)

All tools take `profile` and, where relevant, `ref|selector`. They are registered with annotations and invoking/invoked meta, like the others.

- **`browser_type`** gains `method: "fill"|"type"|"human"|"paste"` (default `"fill"`, for backwards compatibility).
  - The existing `slowly` parameter maps to `"type"`.
  - Description: `human` = realistic key timing; `paste` = system clipboard + Ctrl/⌘+Shift+V, a real paste event.
- **`browser_paste(profile, ref|selector, text, clear=True)`**: convenience for `method="paste"`.
- **`identity_list()`**, **`identity_show(identity)`** (masked), **`identity_create(name, fields: dict)`**, **`identity_update(identity, fields: dict)`**:
  - These handle non-sensitive fields only. Sensitive keys are refused with the exact CLI command the user must run.
  - `identity_delete` is not exposed to the model; deletion is CLI only.
- **`form_detect(profile, scope_ref?)`**: lists the detected fields (kind, descriptor, frame). Read-only.
- **`form_autofill(profile, identity?, fields?, method="paste", overwrite=false, scope_ref?)`**:
  - Fills non-sensitive kinds only.
  - The identity defaults to the profile's linked identity; if there's none, return an error telling the model to pass `identity` or link one.
- **`form_autofill_sensitive(profile, identity?, fields?, method="paste", scope_ref?)`**:
  - Fills sensitive kinds (plus the non-sensitive ones in `fields`, if given).
  - Annotations: `destructive_hint=True`, `idempotent_hint=False`.
  - `meta`: `{"anthropic/requiresUserInteraction": True, "openai/toolInvocation/invoking": "Filling sensitive form fields", ...}`, so every call needs human approval in Claude Code. ChatGPT asks because of `destructive_hint`.
  - Enforce `IdentityStore.check_sensitive_origin(identity, page.url)` on the top-level URL **before** reading any secret.
  - In remote (HTTP) mode the tool is **not registered** unless the server was started with `--allow-sensitive-autofill`.
- **`profile_create` / `profile_update`** gain `identity` (name or id; `""` unlinks).

Outputs follow the existing style: `[profile] <title> — <url>` header, then the report lines. They never contain values.

## 6. CLI

Commands, all supporting `--json` where it makes sense:

- `profilepilot identity list`
- `profilepilot identity show NAME`: masked.
- `profilepilot identity create NAME [--set key=value ...]`
- `profilepilot identity set NAME key=value ...`: non-sensitive only.
- `profilepilot identity secret NAME FIELD`:
  - Prompts with `getpass` (no echo) and asks twice for `card_number`, `ssn` and `password`.
  - Or `--stdin` reads one line.
  - **Never as an argv value.**
- `profilepilot identity clear NAME FIELD`
- `profilepilot identity allow NAME ORIGIN`, `... disallow NAME ORIGIN`
- `profilepilot identity delete NAME`: confirmation prompt; `--yes` skips it.
- `profilepilot identity fields`: lists every field key with its label, sensitivity and aliases.
- `profilepilot profile update NAME --identity ID`

## 7. Tests (no real personal data: use obvious test values; card 4242424242424242, SSN 000-12-3456)

- `tests/test_typing.py` (chrome-marked where Chrome is needed):
  - human typing produces keydown/keyup per character, with inter-key delays inside the configured distribution (seeded rng);
  - the paste method makes a page `paste` listener see `isTrusted && inputType == "insertFromPaste"`;
  - the user's clipboard content (text, and an image/DIB when possible) is restored afterwards;
  - when sensitive, the clipboard carries the exclusion formats during the paste (check from inside the context manager);
  - concurrent pastes from two profiles are serialised by the lock;
  - the fallback path works when the clipboard is unavailable (monkeypatched).
- `tests/test_autofill.py`, using local fixture pages served by `tests.fakes.OriginServer`:
  1. A sign-up form with autocomplete attributes.
  2. A form with no autocomplete attributes, only labels, placeholders and names, including German labels.
  3. A checkout with a cross-origin card iframe. Serve the iframe from a second `OriginServer` on a different port, so it's cross-origin.
  4. Selects for country, state, exp month/year and DOB day/month/year.
  5. Split phone, SSN and card inputs.
  6. A `type=date` DOB.
  7. A gender radio.

  For each, assert every field receives the right value and the report contains no values. Also check:
  - `overwrite=False` keeps pre-filled fields;
  - the sensitive-origin policy blocks a non-allow-listed origin through the server tool;
  - the tool outputs never contain the test SSN or card number;
  - `form_autofill_sensitive` carries the `requiresUserInteraction` meta and isn't registered in remote mode by default.
- **CLI tests:** `identity secret` via `--stdin` stores the value in the secret store and never prints it. `identity show` is masked.

## 8. Integration notes (as built)

Sections 4-6 live in `models.py` / `store.py`, `server/tools_identity.py`, `server/tools_browser.py`,
`server/tools_profiles.py`, `server/http.py` and `cli.py`; their tests are `tests/test_identity_tools.py`
and `tests/test_cli.py`. Choices the text above leaves open:

- **Clipboard lock:** the server uses `<data root>/clipboard.lock` (`Store.clipboard_lock`). Servers
  on different data roots therefore do not serialise against each other; the engine also offers a
  machine-wide `clipboard.default_lock_path()`.
- **`fields` on the form tools** takes identity keys, aliases (`zip`, `dob`, `cvv`) and detected
  kinds (`card_exp` = month + year). `form_autofill` refuses sensitive keys. For
  `form_autofill_sensitive`, sensitive keys in `fields` narrow which secrets are read and filled;
  non-sensitive keys there are filled too; a `fields` list with no sensitive key is refused (use
  `form_autofill`), and leaving `fields` out fills every stored sensitive key. Only the requested
  values (and the values derived from them) reach the engine.
- **`overwrite`** is accepted by `form_autofill_sensitive` too (default false).
- **Navigation guard:** `form_autofill_sensitive` checks the top-level origin, reads the secrets,
  re-checks the origin and runs the fill as a task that is cancelled as soon as the main frame
  navigates to another origin. Without it, the engine's second detection pass could run on a page
  the user never allow-listed. The tool then reports a policy error. Child frames are covered by the
  frame-origin rule in section 3, checked right before each value is entered.
- **Reading values back:** `browser_snapshot` masks (`••••`) the value of every card number,
  expiry, CVV, SSN, password and one-time-code field, in every frame, also values the user typed by
  hand (classified by `autofill.SENSITIVE_FIELD_JS`, generated from the detection expressions;
  fail closed when a ref cannot be checked). In addition the server remembers, per profile, every
  sensitive value `form_autofill_sensitive` entered (raw, digits only, card grouped 4-4-4-4 / 4-6-5
  with spaces or dashes, SSN with and without dashes, and the exact texts typed; 5 characters or
  more; registered in a `finally`, so partial fills count) and replaces them with `[redacted]` in
  every page-reading tool output of that profile (`tools_browser.respond`, before pagination), in
  `browser_tabs` and in `browser_evaluate` errors, until the profile is deleted or the server exits.
  The tool's closing note points to `form_detect` (which shows *whether* a field has a value) rather
  than `browser_snapshot`. **Limits:** `browser_screenshot` shows the digits as pixels, and
  `browser_evaluate` can transform a value (reverse it, base64 it) past the text redaction; a CVV is
  too short to redact in free text (snapshots mask it by meaning). In remote mode both tools are
  refused on a page that received sensitive values until the tab navigates away.
- **Keys go only to the target:** `enter_text` refuses disabled (also `<fieldset disabled>`) and
  read-only fields up front, verifies the focus after the click (a covered field is found by a
  750 ms probe, clicked through its own floating `<label>` or focused with `focus()`), again inside
  the clipboard window right before each paste chord, and before every human-timed key; a typed
  value that did not arrive is an error. `browser_type` sends every method through `enter_text`
  (`type` without `clear` appends at the end). `browser_press_key` refuses paste chords (Ctrl/Cmd+V,
  Shift+Insert in all spellings): they would paste the user's own clipboard; `browser_paste` pastes
  a given text instead.
- **Clipboard window:** the text is on the clipboard only for the paste chord itself (Chrome has
  read it when `keyboard.press` returns); the landing wait runs with the user's clipboard already
  restored, and the Ctrl+V retry takes the clipboard again. If restoring sensitive text fails, a
  background thread retries for about 30 s (holding the lock) and finally empties the clipboard if
  it still holds our text. No snapshot of the user's clipboard is ever written to disk.
- **Addresses:** a separate house-number field ("Hausnr.", "House number") gets the number split off
  the identity's street (`split_street`: number first or last), and the street field next to it the
  street name only; "Straße und Hausnummer" stays one field. A form's preselected country that
  differs from the identity's is kept (`overwrite=false`) but reported with an actionable reason,
  and the tools add a line telling the model to refill `country` and `state` with `overwrite=true`.
- **Skip reasons:** "sensitive value not stored in the identity" (the sensitive tool then names the
  `profilepilot identity secret` commands), "sensitive field (not included in this fill)" (from
  `form_autofill`), "filled together with the card number by form_autofill_sensitive" (card type),
  "not visible (covered or clipped)", "sensitive field in a third-party frame (<origin>)" (the tool
  names the `profilepilot identity allow` command for it). Both form tools send MCP progress
  notifications per field (human typing takes about 3-4 s per field).
- **Identity values:** `identity_create` / `identity_update` (and the CLI's `identity set`) refuse a
  non-sensitive value that looks like an SSN or a Luhn-valid card number of a known brand, and
  never echo an unknown field name that is not shaped like one. `normalize_origin` drops only the
  scheme's own default port (`http://x:443` and `https://x` are different origins) and calls
  `about:` / `data:` pages "not a web origin" (a policy error, not an internal one).
- **Refusals:** `identity_create` / `identity_update` refuse the *whole* call when a sensitive key is
  present, so nothing is half-saved, and name one `profilepilot identity secret` command per key.
  Values are never echoed (also not unknown field names that look like values).
- **`browser_type`:** `slowly=true` with the default method means `type`. With `paste`, an
  `<input type=password>` target gets the concealed clipboard formats.
- **CLI:** `identity secret` refuses a value given as an extra argument (it would be in the shell
  history) and asks for card number, SSN and password twice. `identity delete` unlinks the profiles
  that used the identity. `profile clone` copies the identity link, unless the identity no longer
  exists. `serve --allow-sensitive-autofill` is only accepted with `--http`, and the HTTP startup
  banner says whether sensitive autofill is on.
