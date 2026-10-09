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
- **Policy (enforced by the server tool, double-checked here):** sensitive keys are filled only when present in `values`. The server passes them only after `IdentityStore.check_sensitive_origin()` succeeds for the **top-level** page URL.

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
