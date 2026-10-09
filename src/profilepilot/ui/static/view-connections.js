// Connections: register ProfilePilot with AI apps, connect ChatGPT, and how it all works.

import { api, enc } from "./api.js";
import { copyText, fmt, h, icon, replace } from "./dom.js";
import { loadChatGPT, loadClients, state } from "./store.js";
import { busy, codeBlock, confirmDialog, copyButton, openDialog, toast } from "./ui.js";

const MARKS = { "claude-desktop": "C", "claude-code": ">_", codex: "Cx", cursor: "Cu" };
const EXPLAINER_KEY = "pp-explainer-open";

export function createConnectionsView() {
  const header = h("header.view-header",
    h("div.view-title", h("h1", "Connections"), h("p", "Let your AI apps use ProfilePilot. Nothing leaves this computer unless you connect ChatGPT.")),
    h("div.view-actions", h("button.btn", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => Promise.all([loadClients(true), loadChatGPT()]).catch((err) => toast(err.message, { kind: "error" }))) }, icon("refresh"), "Check again")));
  const clientsBox = h("div.client-grid");
  const chatgptBox = h("div");
  const explainerBox = h("div");
  const body = h("div.view-body",
    explainerBox,
    h("div.section",
      h("div.section-title", h("h2", "AI apps on this computer"), h("p", "One click adds ProfilePilot to the app's settings (a backup is made first).")),
      clientsBox),
    h("div.section",
      h("div.section-title", h("h2", "ChatGPT"), h("p", "Works in the browser, desktop and mobile apps.")),
      chatgptBox));
  const el = h("section.view", { attrs: { "aria-label": "Connections" } }, header, body);
  let requested = false;

  function renderClients() {
    if (!state.clients) {
      replace(clientsBox, ...Array.from({ length: 4 }, () => h("div.card.skeleton", { style: { height: "150px" } })));
      return;
    }
    replace(clientsBox, ...state.clients.map(clientCard));
  }

  function renderChatGPT() {
    replace(chatgptBox, chatgptCard(state.chatgpt));
  }

  function renderExplainer() {
    if (explainerBox.firstChild) return;
    const connected = (state.clients || []).some((c) => c.registered === true) || (state.chatgpt && state.chatgpt.running);
    let open = !connected;
    try { const saved = localStorage.getItem(EXPLAINER_KEY); if (saved !== null) open = saved === "1"; } catch (err) { /* storage blocked */ }
    replace(explainerBox, h("details.explainer-box", {
      open,
      ontoggle: (e) => { try { localStorage.setItem(EXPLAINER_KEY, e.currentTarget.open ? "1" : "0"); } catch (err) { /* storage blocked */ } },
    }, h("summary", icon("chevron-right"), h("strong", "New to this? How it works")), explainer()));
  }

  return {
    el,
    title: "Connections",
    onShow() {
      renderExplainer();
      renderClients();
      renderChatGPT();
      if (!requested) {
        requested = true;
        loadClients().catch((err) => toast(err.message, { kind: "error" }));
        loadChatGPT().catch(() => {});
      }
    },
    update(topics) {
      if (topics.has("clients")) renderClients();
      if (topics.has("chatgpt")) renderChatGPT();
    },
  };
}

function statusBadge(c) {
  if (c.registered === true) return h("span.badge.green", icon("check"), "Connected");
  if (c.registered === false) return h("span.badge", "Not set up");
  if (c.client === "claude-code") return h("span.badge.amber", { attrs: { title: "The claude command was not found on this computer, so ProfilePilot can't add itself. Copy the setup command and run it in a terminal." } }, "Claude Code not found");
  return h("span.badge.amber", { attrs: { title: "ProfilePilot can't read this app's settings file." } }, "Unknown");
}

function clientCard(c) {
  const cliMissing = c.client === "claude-code" && c.registered === null;
  let action;
  if (c.registered === true) action = h("button.btn.sm", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => run(c, "unregister")) }, "Remove");
  else if (cliMissing) {
    action = [
      h("button.btn.sm", { attrs: { type: "button", title: "Copy the command that adds ProfilePilot to Claude Code" }, onclick: (e) => busy(e.currentTarget, () => run(c, "register")) }, icon("copy"), "Copy setup command"),
    ];
  } else action = h("button.btn.sm.primary", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => run(c, "register")) }, icon("plus"), c.client === "claude-code" ? "Add to Claude Code" : "Connect");
  const where = c.config_paths && c.config_paths.length
    ? h("div.small.faint", { style: { wordBreak: "break-all" } }, "Settings file: ", h("span.mono", c.config_paths[0]))
    : (c.client === "claude-code" ? h("div.small.faint", "Uses the claude command (claude mcp add).") : null);
  return h("div.card.client-card",
    h("div.cc-head",
      h("div.client-mark", { class: c.client }, MARKS[c.client] || "AI"),
      h("div.cc-text", h("div.row.wrap", h("h3", c.label), statusBadge(c)), h("p", c.description))),
    h("div.cc-actions", action, h("span.small.faint", c.registered === true ? "Restart the app if it was open." : cliMissing ? "Run it in a terminal, then check again." : "")),
    h("details",
      h("summary", icon("chevron-right"), "Set it up by hand"),
      h("div.snippet", where, codeBlock(c.snippet || ""))),
  );
}

/** Nothing could be registered automatically: show the exact command and let the user check again. */
function manualSetup(c, result) {
  const check = h("button.btn.primary", { attrs: { type: "button" } }, icon("refresh"), "I ran it – check again");
  const dlg = openDialog({
    title: "One more step", size: "wide",
    description: result.summary || `Run this command in a terminal to add ProfilePilot to ${c.label}:`,
    body: h("div.stack",
      codeBlock(result.command || ""),
      result.copied ? h("p.small", h("span.badge.green", icon("check"), "Copied to the clipboard")) : null,
      h("p.small.muted", "Open a terminal (PowerShell, Terminal or your shell), paste the command and press Enter. Then come back and check again.")),
    footer: [h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.forceClose() }, "Close"), check],
  });
  check.addEventListener("click", () => busy(check, async () => {
    try {
      await loadClients(true);
      const now = (state.clients || []).find((x) => x.client === c.client);
      if (now && now.registered === true) {
        dlg.forceClose();
        toast(`ProfilePilot is set up in ${c.label}.`, { kind: "success", title: "Connected" });
      } else {
        toast(now && now.registered === null ? "The claude command still isn't found from here. If you just installed it, restart ProfilePilot Manager." : "ProfilePilot isn't registered yet.", { kind: "warn", title: "Not connected yet" });
      }
    } catch (err) {
      toast(err.message, { kind: "error" });
    }
  }));
}

async function run(c, action) {
  if (action === "unregister") {
    const yes = await confirmDialog({ title: `Remove ProfilePilot from ${c.label}?`, message: "The app will no longer see ProfilePilot's tools. Your profiles are not touched.", confirmLabel: "Remove" });
    if (!yes) return;
  }
  try {
    const result = await api.post(`/api/clients/${enc(c.client)}/${action}`);
    state.clients = result.clients;
    if (result.manual) {
      let copied = false;
      try { await copyText(result.command || ""); copied = true; } catch (err) { /* the dialog has a copy button */ }
      manualSetup(c, { ...result, copied });
      return;
    }
    toast(result.summary || "Done.", { kind: "success", title: action === "register" ? `Connected to ${c.label}` : `Removed from ${c.label}`, details: result.report, timeout: 7000 });
    loadClients().catch(() => {});
  } catch (err) {
    toast(err.message, { kind: "error", title: "That did not work" });
  }
}

function chatgptCard(s) {
  const status = s || {};
  const running = !!status.running;
  const tools = status.tools || {};
  const commands = status.commands || { connect: "profilepilot connect chatgpt", stop: "profilepilot connect stop" };
  const connections = status.connections || [];
  const approved = running && connections.length > 0;
  const head = h("div.card-header",
    h("div.client-mark.chatgpt", icon("chat")),
    h("div.stack", { style: { gap: "2px" } }, h("h2", "ChatGPT"),
      h("span.small.muted", running ? `Connected${status.method ? ` via ${status.method}` : ""}${status.started_at ? ` · since ${fmt.ago(epoch(status.started_at))}` : ""}` : "Not connected")),
    h("span.spacer"),
    running ? h("span.badge.green", h("span.dot.running.pulse"), "Online") : h("span.badge", "Offline"));

  const codeRow = () => {
    if (!running || !status.pairing_code) return h("p.small.faint", running ? "Run 'profilepilot connect status' in a terminal to see the code." : "The code appears here once the connection is running.");
    const code = h("span.pairing-code", status.pairing_code);
    return h("div.stack", { style: { gap: "8px" } },
      h("div.row", code, copyButton(status.pairing_code, { label: "Copy code", small: false })),
      h("p.small.muted", "ProfilePilot never asks for this code anywhere else: not in a chat, not in a help request, not on another site."));
  };

  let main;
  if (approved) {
    // Connected and approved: a short summary; the steps and the code stay out of the way.
    const showCode = h("details.code-disclosure", h("summary", icon("chevron-right"), "Show pairing code (to approve another app)"), codeRow());
    main = h("div.card-body.stack", { style: { gap: "10px" } },
      h("div.connected-summary", h("span.badge.green", icon("check"), "Connected"),
        h("span", `ChatGPT can use your profiles while this connection runs${status.mcp_url ? "" : "."}`),
        status.mcp_url ? h("span.row", h("code", status.mcp_url), copyButton(status.mcp_url, { label: "Copy address" })) : null),
      showCode);
  } else {
    const startButton = !running ? h("button.btn.primary", { attrs: { type: "button", title: "Opens a terminal window that starts the secure connection" },
      onclick: (e) => busy(e.currentTarget, () => startConnection()) }, icon("play"), "Start connection") : null;
    const steps = h("div.steps",
      h("div.step", { class: running ? "done" : "" },
        h("div.step-num"),
        h("div.step-body",
          h("strong", running ? "The secure connection is running" : "Start the secure connection on this computer"),
          running
            ? h("p", "Keep its terminal window open. ChatGPT can reach ProfilePilot only while it runs.")
            : [h("p", "ChatGPT runs in the cloud, so it can only reach tools on the internet. This starts a private tunnel to ProfilePilot, protected by a sign-in that only you can approve. Keep the window it opens running."),
              h("div.row.wrap", startButton, h("span.small.faint", "or run it in a terminal yourself:")),
              codeBlock(commands.connect)],
          h("div.row.wrap.small",
            tools["tunnel-client"] ? h("span.badge.green", icon("check"), "OpenAI tunnel client") : null,
            tools.cloudflared ? h("span.badge.green", icon("check"), "cloudflared") : h("span.badge", { attrs: { title: "Not needed if you use the OpenAI tunnel client or ngrok" } }, "Optional helper (cloudflared) not installed"),
            tools.ngrok ? h("span.badge.green", icon("check"), "ngrok") : null))),
      h("div.step", { class: approved ? "done" : "" },
        h("div.step-num"),
        h("div.step-body",
          h("strong", "Add ProfilePilot in ChatGPT"),
          h("p", "In ChatGPT open Settings → Apps & Connectors (developer mode) → Create / Add custom connector, and paste this address. When ChatGPT asks how to sign in, choose OAuth."),
          running && status.mcp_url ? h("div.row", h("code", { style: { padding: "6px 10px", fontSize: "13px" } }, status.mcp_url), copyButton(status.mcp_url, { label: "Copy address", small: false }))
            : h("p.small.faint", "The address appears here once the connection is running."))),
      h("div.step", { class: approved ? "done" : "" },
        h("div.step-num"),
        h("div.step-body",
          h("strong", "Approve with your pairing code"),
          h("p", "ChatGPT opens a ProfilePilot sign-in page. Type this code there. Only someone who can see this screen can approve the connection."),
          codeRow())),
    );
    main = h("div.card-body", steps);
  }

  const stopRow = h("div.row.wrap",
    running ? h("button.btn.sm", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => stopSharing(false)) }, icon("stop"), "Stop sharing") : null,
    connections.length ? h("button.btn.sm.danger", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => stopSharing(true)) }, icon("x"), "Sign out all apps") : null);
  const connList = connections.length ? h("div.list", connections.map((g) => h("div.list-item", icon("chat"),
    h("div.grow", h("strong", g.client_name || g.client_id || "Connected app"),
      h("span.small.faint", `Approved ${fmt.ago(epoch(g.created_at))}${g.last_used_at ? ` · last used ${fmt.ago(epoch(g.last_used_at))}` : ""}`))))) : null;
  const footer = h("div.card-body.stack.card-footer", { style: { gap: "12px" } },
    connList ? h("strong.small", "Apps you approved") : null, connList,
    h("div.callout", icon("shield"),
      h("span", running
        ? ["To stop sharing, close the connection's terminal window or use Stop sharing (or run ", h("code", commands.stop || "profilepilot connect stop"), "). "]
        : "Nothing is shared right now. ",
      "ChatGPT can only use your profiles while the connection runs. Card numbers and passwords are never sent to ChatGPT.")),
    stopRow.childElementCount ? stopRow : null);
  return h("div.card.chatgpt-card", head, main, footer);
}

function epoch(value) {
  if (typeof value === "number") return new Date(value * 1000).toISOString();
  return value;
}

async function startConnection() {
  try {
    const result = await api.post("/api/chatgpt/start");
    toast(result.started ? "A terminal window opened and is starting the connection. Follow it there; this page updates when it runs." : "The connection is already running.",
      { kind: "success", title: result.started ? "Starting the connection" : "Already connected", timeout: 8000 });
    loadChatGPT().catch(() => {});
  } catch (err) {
    toast(err.message, { kind: "error", title: "Could not start it from here" });
  }
}

async function stopSharing(revoke) {
  if (revoke) {
    const yes = await confirmDialog({ title: "Sign out every connected app?", message: "ChatGPT (and any other app you approved) loses access. The next connection needs a new pairing code.", confirmLabel: "Sign out all", danger: true });
    if (!yes) return;
  }
  try {
    const result = await api.post("/api/chatgpt/stop", { revoke });
    state.chatgpt = result.status;
    toast(result.report, { kind: "success" });
    loadChatGPT().catch(() => {});
  } catch (err) {
    toast(err.message, { kind: "error" });
  }
}

function explainer() {
  const card = (iconName, title, text) => h("div.card", icon(iconName), h("h3", title), h("p", text));
  return h("div.explainer",
    card("connections", "Plugins are tools", "Claude and ChatGPT can use extra tools through a standard called MCP. ProfilePilot is one of these tools: it gives the AI its own set of real Chrome browsers."),
    card("monitor", "Everything runs here", "The browsers, cookies and passwords stay on this computer. Claude Desktop, Claude Code, Codex and Cursor start ProfilePilot by themselves once connected below."),
    card("chat", "ChatGPT needs a tunnel", "ChatGPT lives in the cloud. A secure tunnel lets it reach ProfilePilot while you allow it; your pairing code makes sure nobody else can."),
    card("hand", "You stay in charge", "Take control of any profile with one click: the AI pauses on it until you hand it back. When the AI needs you, for a CAPTCHA or a login, it asks here."),
  );
}
