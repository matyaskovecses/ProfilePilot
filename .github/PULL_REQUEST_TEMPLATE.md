## What & why

## How I verified it
- [ ] `pytest -m "not chrome"` passes
- [ ] Real-Chrome tests (`pytest -m chrome`) pass, or not affected
- [ ] Fingerprint-related: probe / audit results before → after attached
- [ ] No secrets in outputs, logs or argv; docs and SKILL.md updated if tools changed
