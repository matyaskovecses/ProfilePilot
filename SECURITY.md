# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/matyaskovecses/ProfilePilot/security/advisories/new)
instead. Include steps to reproduce, and what an attacker could achieve. You'll get an answer within a week.

## Security model (what ProfilePilot protects, and what it doesn't)

ProfilePilot gives an AI model control of real, logged-in browser profiles. Its design assumes the
model **can be prompt-injected by the pages it reads**, so these protections don't rely on the model
behaving well:

| Asset | Protection |
|---|---|
| Proxy passwords, API tokens | Stored in the OS keyring, or in a DPAPI-encrypted file. Never written to profile/proxy JSON, Chrome's command line, logs or any tool output. Chrome only ever sees a credential-free relay on `127.0.0.1`. |
| Identity sensitive fields (SSN, card number/expiry/CVV, password) | Only the user can set them, in a terminal (`profilepilot identity secret`, hidden input). The model only sees masked values. They can be autofilled only on origins the user allow-listed (`profilepilot identity allow`), and the tool always asks for human approval. Values the AI typed are redacted from snapshots, read output and evaluate output afterwards. Clipboard pastes of sensitive values are excluded from Windows clipboard history and cloud sync. |
| Remote mode (`serve --http`, e.g. for ChatGPT) | The URL needs a secret path or a bearer token, with DNS-rebinding protection. Navigation to loopback, private, link-local and `file:` targets is blocked, along with private proxy hosts. Cookie files are confined to export folders. Sensitive autofill is off unless you pass `--allow-sensitive-autofill`. |
| Host control API | Listens on 127.0.0.1 only, and every request needs a per-run token. |

Known limits:
- The Chrome DevTools port and the proxy relay listen on `127.0.0.1` without authentication. Chrome can't authenticate either one. **Any program running as your user** can drive a running profile or use its proxy. Don't run ProfilePilot on shared machines.
- Remote mode isn't a firewall. JavaScript in a page can still make requests the browser allows. Remote mode also re-checks URLs it doesn't resolve itself.
- Native mode doesn't hide your hardware fingerprint (GPU, fonts, screen). Profiles on one machine can be linked by fingerprinting vendors. See [docs/FINGERPRINT-AUDIT.md](docs/FINGERPRINT-AUDIT.md).

## Responsible use

ProfilePilot is a tool for legitimate automation. Respect websites' terms and robots.txt, and only use
accounts and identities you're entitled to. It doesn't solve CAPTCHAs or bypass access controls.
