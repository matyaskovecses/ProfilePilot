# Using ProfilePilot with ChatGPT

ProfilePilot runs on your computer. ChatGPT runs in OpenAI's cloud, so it cannot start programs on
your computer the way Claude Desktop or Codex can. ChatGPT can only use **MCP servers it can reach
over the internet**. This guide shows three ways to make ProfilePilot reachable while it stays
protected, and how to stop sharing it again.

The short version:

```powershell
profilepilot connect chatgpt
```

The command explains each step, opens a secure tunnel, and prints a **URL** and a **pairing code**.
Paste the URL into ChatGPT and enter the code when ChatGPT's sign-in window asks for it.

## How ChatGPT apps and plugins work

- An "app", "plugin" or "connector" in ChatGPT is a connection to an **MCP server**: a program that
  offers tools. ProfilePilot's tools are things like "start profile", "open a page" and "read the
  page".
- When you ask for something, ChatGPT decides which tools to call and shows each call in the chat.
  Before it runs a tool that changes something (for example deleting a profile), ChatGPT asks you to
  confirm.
- The browser windows open **on your computer**, with each profile's own cookies, logins and proxy.
  ChatGPT only sees what the tools return: page text, element lists and status lines.
- In ChatGPT and Claude, "show my profiles" opens the **ProfilePilot panel** in the chat. It lists
  every profile with its status and proxy and has **Start / Stop** and **Take control** buttons, so you
  can step in without leaving the conversation. Handing a profile back to the AI is done in
  ProfilePilot Manager (or with `profilepilot profile resume "<name>"`), never in the chat: the server
  can't tell the panel's clicks from the AI's, so the panel can only do things that are safe for the
  AI to do too.

## What you need

- **A ChatGPT plan that allows custom MCP servers.** OpenAI offers full MCP support (tools that can
  change things) in developer mode on **Business, Enterprise and Edu** workspaces, on chatgpt.com in
  a browser. Plus and Pro accounts have had more limited, changing access. Look for **Add custom MCP
  server** under [chatgpt.com/plugins](https://chatgpt.com/plugins) (or **Settings > Apps &
  Connectors**). If it's missing, your workspace admin may have to turn on developer mode. OpenAI's
  current rules are in [this help article](https://help.openai.com/en/articles/12584461).
- **ProfilePilot installed** (see the README) and Chrome, Edge, Brave or Chromium.
- **One of the connection options** below. The wizard detects what you have.

## Option A: OpenAI Secure MCP Tunnel (no public address)

This option is for workspaces that have tunnels. The `tunnel-client` program connects **outbound** to
OpenAI and starts ProfilePilot locally. Nothing on your computer is reachable from the internet, so
ProfilePilot needs no sign-in page.

Run `profilepilot connect chatgpt --via tunnel-client`. It prints these steps with your own Python
path filled in:

1. In the [OpenAI Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels),
   click **Create tunnel** and copy its id (`tunnel_...`). This needs the Tunnels **Read + Manage**
   permissions. The tunnel must include your ChatGPT workspace.
2. Download `tunnel-client` from
   [github.com/openai/tunnel-client/releases/latest](https://github.com/openai/tunnel-client/releases/latest).
3. In PowerShell, using an API key with Tunnels **Read + Use**:

   ```powershell
   $env:CONTROL_PLANE_API_KEY = "sk-..."
   tunnel-client init --sample sample_mcp_stdio_local --profile profilepilot --tunnel-id tunnel_... --mcp-command 'C:/path/to/python.exe -m profilepilot serve'
   tunnel-client run --profile profilepilot
   ```

4. In ChatGPT, open **chatgpt.com/plugins**, click **+**, then **Add custom MCP server**. Set:
   - **Name:** ProfilePilot
   - **Connection:** **Tunnel**, then pick your tunnel
   - **Authentication:** **No auth**

   Then click **I understand and want to continue** and **Create as a plugin**.

Keep `tunnel-client run` open while you use ChatGPT.

## Option B: Cloudflare quick tunnel (no account, recommended for most people)

```powershell
profilepilot connect chatgpt
```

If `cloudflared` isn't installed, the wizard shows the install command
(`winget install --id Cloudflare.cloudflared`). Add `--install` to let the wizard run it for you.
Nothing is installed without `--install`.

The wizard then:

1. Starts `cloudflared tunnel --url http://127.0.0.1:8931` and reads the address it gets, such as
   `https://sunny-river-velvet-orbit.trycloudflare.com`.
2. Starts `profilepilot serve --http --auth oauth --public-host <that address>`. This is the MCP
   server, protected by a sign-in (see [Security](#security-how-the-pairing-code-protects-you)).
3. Prints a card like this:

   ```
   +--------------------------------------------------------------------------+
   |  URL to paste:   https://sunny-river-velvet-orbit.trycloudflare.com/mcp  |
   |  Pairing code:   KM7Q-4XRT                                               |
   +--------------------------------------------------------------------------+
   ```

Then, in ChatGPT:

1. Open **chatgpt.com/plugins** (or **Settings > Apps & Connectors**).
2. Click **+**, then **Add custom MCP server**. Set:
   - **Name:** ProfilePilot
   - **Connection:** paste the URL from the card
   - **Authentication:** **OAuth**
3. Confirm the risk warning and create the plugin. A window opens with **"Allow ChatGPT to use
   ProfilePilot?"**. Check that **Returns to** says `chatgpt.com`, then type the pairing code and
   click **Approve**.
4. Back in ChatGPT, the ProfilePilot tools are listed. Start a new chat and try "show my profiles".

The wizard prints **Connected: ChatGPT** when the sign-in succeeds. After each use it prints the
next pairing code, because a code works only once. Keep the window open while you use ChatGPT.
Never give the pairing code to anyone, and never type it into a chat: ProfilePilot only asks for it
on its own sign-in page.

A quick tunnel gets a **new address every time**. Next time, edit the plugin's URL in ChatGPT, or
remove the plugin and add it again.

## Option C: ngrok (free account, one fixed address)

1. Create a free account at [ngrok.com](https://ngrok.com), install ngrok
   (`winget install --id Ngrok.Ngrok`), and connect it to your account once:
   `ngrok config add-authtoken <token>`.
2. Optionally, claim your free static domain in the ngrok dashboard, so the URL in ChatGPT never
   changes.
3. Start sharing:

   ```powershell
   profilepilot connect chatgpt --via ngrok --ngrok-domain your-name.ngrok-free.app
   ```

The ChatGPT steps are the same as in option B. On the free plan, ngrok shows a "You are about to
visit..." page the first time your browser opens the sign-in page. Click **Visit Site**.

## Option D: your own tunnel or reverse proxy

If you already have an HTTPS address that forwards to this computer (a Cloudflare named tunnel, Tailscale
Funnel, or a reverse proxy):

```powershell
profilepilot connect chatgpt --via url --public-url https://pp.example.com --port 8931
```

It must forward to `http://127.0.0.1:<port>` and keep the `Host` header. To run the server without the
wizard, use `profilepilot serve --http --auth oauth --public-host pp.example.com`. It makes a new
pairing code every time it starts and prints it only in an interactive terminal (not into a log
file); `profilepilot connect status` always shows the current one.

## Claude on the web

[claude.ai](https://claude.ai) can use the same URL as a custom connector. Go to **Settings >
Connectors > Add custom connector**, paste the URL, and approve it with the pairing code. The
sign-in page shows `claude.ai` as the destination. Claude Desktop and Claude Code don't need any of
this, because they start ProfilePilot directly (see docs/CLIENTS.md).

## Security: how the pairing code protects you

Anyone who connects can drive your browser profiles and use their logins. So the public address is
protected by a sign-in that only someone who can see your screen can complete:

- **OAuth 2.1 sign-in.** ChatGPT has to get a token from ProfilePilot's own sign-in server. The
  server uses authorization code with PKCE (S256) only, exact redirect addresses, and RFC 9207
  issuer checks. Every token is bound to this server's address (RFC 8707). There are no passwords
  and no API keys.
- **The pairing code.** The sign-in page asks for an 8-character code such as `KM7Q-4XRT`. It has no
  look-alike characters such as 0/O or 1/I. The code is shown only in the wizard's terminal, in
  ProfilePilot Manager (Connections) and by `profilepilot connect status`. It **changes after every
  successful sign-in** and whenever the server starts. ProfilePilot never asks for it anywhere but
  its own sign-in page, so never give it to anyone, including an AI in a chat.
- **Guessing is limited.** At most 5 wrong codes are checked in 10 minutes, for all sign-ins
  together; after that no code is checked, not even the right one, until the 10 minutes are over.
  Many guesses sent at the same moment count too. A sign-in page closes after 5 wrong codes. If
  someone else used up the attempts (anyone who knows the address can try), run
  `profilepilot connect unlock` on your computer: it makes a new pairing code that works right away
  for the next 5 minutes, so nobody can lock you out. That code is longer (12 characters, such as
  `KM7Q-4XRT-9PWA`), because wrong guesses don't lock the page during those 5 minutes.
- **Check the sign-in page.** It shows which app is asking, where it will be sent back to (for
  example `chatgpt.com`) and how long ago the request was made. The green ChatGPT or Claude badge
  only appears for their exact sign-in addresses. The page always reminds you: only approve a
  sign-in you started yourself, just now. If someone sent you the link, click **Deny**.
- **No redirects to strangers.** Apps can only register `https://` return addresses, `localhost`,
  or app links such as `cursor://` and `vscode://`. Other link types, such as Windows `search-ms:`
  or `ms-officecmd:` links and `smb://` network shares, are refused. Errors and "Deny" are only sent
  back to ChatGPT, Claude, apps on this computer and apps you approved before. Anyone else gets an
  error page, so the address can't be used to bounce visitors to another site.
- **Tokens.** Access tokens last 1 hour and refresh tokens 30 days. Each refresh rotates the refresh
  token. If an answer gets lost and the app retries within a minute, it gets the same new tokens
  again, never a second set. Reusing an old token after that signs the app out. Tokens only work
  at the address they were approved for: when the tunnel address changes, apps must sign in again.
  Only SHA-256 hashes of tokens are stored, in `oauth.json` in the data folder.
- **The page itself.** It loads nothing from other sites, runs no scripts, can't be embedded in
  another page (`frame-ancestors 'none'`), and checks a CSRF token.
- **Remote mode limits.** Remote clients can't open `localhost` or private-network addresses. Card,
  SSN and password autofill (`form_autofill_sensitive`) is off for remote clients.
- **You stay in charge.** **Take control**, in the chat panel or in ProfilePilot Manager, pauses the
  AI on a profile until you hand it back. You hand it back in ProfilePilot Manager or with
  `profilepilot profile resume "<name>"`, never in the chat. While a profile is yours, the panel's
  Start and Stop buttons are hidden and refused. When the AI needs a CAPTCHA solved or a 2FA code typed, it asks
  you instead of trying itself; you click **Done** in ProfilePilot Manager when you've finished.
  ProfilePilot Manager's Activity view lists every tool call.

## How to stop sharing

- **Stop the tunnel:** press **Ctrl+C** in the wizard's window, or run `profilepilot connect stop`
  from any terminal. ChatGPT can no longer reach your computer at all. Apps you approved stay
  signed in, though: when sharing starts again at the **same** address (an ngrok domain or your own
  URL), they reconnect without a new pairing code. Use `--revoke` (below) to sign them out too.
- **Sign out every app:** `profilepilot connect stop --revoke` stops sharing, deletes all tokens
  and makes a new pairing code. The next connection has to be approved again.
- **Remove the plugin:** in ChatGPT, open **chatgpt.com/plugins**, open ProfilePilot and delete it.
- **Check the state:** `profilepilot connect status` shows whether sharing is on, the URL, the
  current pairing code, whether sign-in is locked after wrong codes, and which apps are connected.
  Add `--json` for scripts.
- **Locked out by wrong codes:** `profilepilot connect unlock` makes a new (longer) pairing code
  that the sign-in page accepts right away.

## Troubleshooting

| Problem | What to do |
|---|---|
| No **Add custom MCP server** button | Your plan or workspace doesn't allow custom MCP servers, or developer mode is off. See [What you need](#what-you-need). |
| "That pairing code is not right" | Codes work once. Use the **latest** code from the wizard window or `profilepilot connect status`. Spaces, dashes and lower case don't matter. |
| "Too many wrong pairing codes" | Run `profilepilot connect unlock` and enter the new code it shows (reload the sign-in page first). Or wait 10 minutes. If you didn't type wrong codes yourself, someone else knows the address: consider a new tunnel afterwards. |
| "This sign-in link is no longer valid" | The sign-in page expired (10 minutes), the server restarted, or 5 wrong codes were entered on it. Start the connection again in ChatGPT. |
| "This sign-in request can't be completed" | The app asked for something ProfilePilot doesn't allow (for example another server's address). Start the connection again from the app. |
| ChatGPT says it can't reach the server | Make sure the wizard is still running. Quick tunnels can take up to a minute to resolve. Check that the URL in ChatGPT is the latest one (quick tunnels change address) and ends in `/mcp`. Firewalls and VPNs can block `cloudflared`. |
| The sign-in worked before, but now ChatGPT asks again | The address changed (new quick tunnel), tokens expired, or you ran `connect stop --revoke`. Approve again with the current pairing code. |
| ngrok: "authentication failed" / `ERR_NGROK_4018` | Run `ngrok config add-authtoken <token>` once with the token from your ngrok dashboard. |
| "ProfilePilot is already shared" | Another `connect chatgpt` is running. Use it, or run `profilepilot connect stop` first. |
| "Port 8931 is already in use" | Another program, or a `profilepilot serve --http`, uses the port. Leave out `--port` to pick a free one, or stop the other program. |
| The panel doesn't appear | Older clients and some plans show text instead of the panel. The same information is in the reply. Ask "show my profiles" in a **new** chat after refreshing the plugin. |
| Tools are missing after an update | In ChatGPT, open the plugin and click **Refresh**, then start a new chat. |
| The AI says a profile is "controlled by the user" | You (or the panel) clicked **Take control**. When you're done, click **Hand back to AI** in ProfilePilot Manager, or run `profilepilot profile resume "<name>"`. |
| The panel has no **Hand back** or **Done** button | That's on purpose. The AI could press those too, so they're only in ProfilePilot Manager. |
| The panel won't start or stop a profile | You have control of it, or the AI is waiting for you there. Start it or close its window yourself (in ProfilePilot Manager), or hand it back first. |
