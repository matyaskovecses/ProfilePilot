# Contributing to ProfilePilot

Thanks for helping! Bug reports, detector findings, new client integrations and docs fixes are all welcome.

## Development setup

```bash
git clone https://github.com/matyaskovecses/ProfilePilot && cd ProfilePilot
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[test,scrapling]"
```

On macOS or Linux, use `.venv/bin/python` instead.

## Tests

```bash
.venv/Scripts/python -m pytest -m "not chrome"
```

```bash
.venv/Scripts/python -m pytest
```

The first command runs the fast tests: no browser, no network. The second runs everything, including
real-Chrome end-to-end tests. Their windows open off-screen.

- `chrome` tests launch the installed Chrome with throwaway profiles. They only ever close processes they started.
- `network` tests need internet access and are skipped by default. Run them with `-m network`.
- A few clipboard tests use the real system clipboard inside a guard that saves and restores it.

## Ground rules

1. **Native first.** Don't add fingerprint spoofing, stealth init scripts, or switches such as `--disable-blink-features`. If something makes a profile look different from plain Chrome, fix the cause. `docs/FINGERPRINT-AUDIT.md` has the method and a regression probe (`tests/test_native_fingerprint.py`).
2. **Never leak secrets.** Proxy passwords, tokens, cookie values and identity secrets must never appear in tool output, logs, argv or exceptions. Add a test when you touch these paths.
3. **Assume prompt injection.** Anything a page shows the model is untrusted. Tools with side effects need correct MCP annotations (`destructive_hint`, …).
4. **Leave the user's machine alone.** Tests use temporary data roots (`PROFILEPILOT_HOME`) and `PROFILEPILOT_SECRETS=file`. They never touch the user's real Chrome profile or AI-client configs.
5. Keep the code typed and logged (no `print` in library code; stdout is the MCP wire), and match the surrounding style.

## Pull requests

- Describe the behaviour change and how you verified it. For fingerprint-related changes, include before and after results from the probe or the audit harness.
- Add or adjust tests. The fast suite must pass on Windows, macOS and Linux; CI runs it.
- Update `README.md`, `docs/` and `skills/profilepilot/SKILL.md` when tools or behaviour change.
