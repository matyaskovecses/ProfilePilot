// Connections: register ProfilePilot with AI apps, connect ChatGPT, and how it all works.

import { api, enc } from "./api.js";
import { fmt, h, icon, replace } from "./dom.js";
import { loadChatGPT, loadClients, state } from "./store.js";
import { busy, codeBlock, confirmDialog, copyButton, toast } from "./ui.js";

const MARKS = { "claude-desktop": "C", "claude-code": ">_", codex: "Cx", cursor: "Cu" };

export function createConnectionsView() {
  const header = h("header.view-header",
    h("div.view-title", h("h1", "Connections"), h("p", "Let your AI apps use ProfilePilot. Nothing leaves this computer unless you connect ChatGPT.")),
    h("div.view-actions", h("button.btn", { onclick: (e) => busy(e.currentTarget, () => Promise.all([loadClients(true), loadChatGPT()]).catch((err) => toast(err.message, { kind: "error" }))) }, icon("refresh"), "Check again")));
  const clientsBox = h("div.client-grid");
  const chatgptBox = h("div");
  const body = h("div.view-body",
    h("div.section",
      h("div.section-title", h("h2", "AI apps on this computer"), h("p", "One click adds ProfilePilot to the app's settings (a backup is made first).")),
      clientsBox),
    h("div.section",
      h("div.section-title", h("h2", "ChatGPT"), h("p", "Works in the browser, desktop and mobile apps.")),
      chatgptBox),
    h("div.section",
      h("div.section-title", h("h2", "How plugins work"), h("p", "In plain words.")),
      explainer()));
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

  return {
    el,
    title: "Connections",
    onShow() {
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

function statusBadge(registered) {
  if (registered === true) return h("span.badge.green", icon("check"), "Connected");
  if (registered === false) return h("span.badge", "Not set up");
  return h("span.badge.amber", { attrs: { title: "ProfilePilot can't read this app's settings (the CLI may be missing)." } }, "Unknown");
}

function clientCard(c) {
  const action = c.registered === true
    ? h("button.btn.sm", { onclick: (e) => busy(e.currentTarget, () => run(c, "unregister")) }, "Remove")
    : h("button.btn.sm.primary", { onclick: (e) => busy(e.currentTarget, () => run(c, "register")) }, icon("plus"), c.client === "claude-code" ? "Add to Claude Code" : "Connect");
  const where = c.config_paths && c.config_paths.length
    ? h("div.small.faint", { style: { wordBreak: "break-all" } }, "Settings file: ", h("span.mono", c.config_paths[0]))
    : (c.client === "claude-code" ? h("div.small.faint", "Uses the claude command (claude mcp add).") : null);
  return h("div.card.client-card",
    h("div.cc-head",
      h("div.client-mark", { class: c.client }, MARKS[c.client] || "AI"),
      h("div.cc-text", h("div.row", h("h3", c.label), statusBadge(c.registered)), h("p", c.description))),
    h("div.cc-actions", action, h("span.small.faint", c.registered === true ? "Restart the app if it was open." : "")),
    h("details",
      h("summary", icon("chevron-right"), "Set it up by hand"),
      h("div.snippet", where, codeBlock(c.snippet || ""))),
  );
}

async function run(c, action) {
  if (action === "unregister") {
    const yes = await confirmDialog({ title: `Remove ProfilePilot from ${c.label}?`, message: "The app will no longer see ProfilePilot's tools. Your profiles are not touched.", confirmLabel: "Remove" });
    if (!yes) return;
  }
  try {
    const result = await api.post(`/api/clients/${enc(c.client)}/${action}`);
    state.clients = result.clients;
    toast(result.report, { kind: "success", title: action === "register" ? `Connected to ${c.label}` : `Removed from ${c.label}`, timeout: 9000 });
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
  const head = h("div.card-header",
    h("div.client-mark.chatgpt", icon("chat")),
    h("div.stack", { style: { gap: "2px" } }, h("h2", "ChatGPT"),
      h("span.small.muted", running ? `Connected${status.method ? ` via ${status.method}` : ""}${status.started_at ? ` · since ${fmt.ago(epoch(status.started_at))}` : ""}` : "Not connected")),
    h("span.spacer"),
    running ? h("span.badge.green", h("span.dot.running.pulse"), "Online") : h("span.badge", "Offline"));
  const steps = h("div.steps",
    h("div.step", { class: running ? "done" : "" },
      h("div.step-num"),
      h("div.step-body",
        h("strong", "Start the secure connection on this computer"),
        h("p", "ChatGPT runs in the cloud, so it can only reach tools on the internet. This starts a private tunnel to ProfilePilot, protected by a sign-in that only you can approve. Run this in a terminal and keep it open:"),
        codeBlock(commands.connect),
        h("div.row.wrap.small",
          tools["tunnel-client"] ? h("span.badge.green", icon("check"), "OpenAI tunnel client") : null,
          tools.cloudflared ? h("span.badge.green", icon("check"), "cloudflared") : h("span.badge", "cloudflared not installed"),
          tools.ngrok ? h("span.badge.green", icon("check"), "ngrok") : null))),
    h("div.step", { class: running ? "" : "" },
      h("div.step-num"),
      h("div.step-body",
        h("strong", "Add ProfilePilot in ChatGPT"),
        h("p", "In ChatGPT open Settings → Apps & Connectors (developer mode) → Create / Add custom connector, and paste this address. Choose OAuth for authentication."),
        running && status.mcp_url ? h("div.row", h("code", { style: { padding: "6px 10px", fontSize: "13px" } }, status.mcp_url), copyButton(status.mcp_url, { label: "Copy address", small: false }))
          : h("p.small.faint", "The address appears here once the connection is running."))),
    h("div.step",
      h("div.step-num"),
      h("div.step-body",
        h("strong", "Approve with your pairing code"),
        h("p", "ChatGPT opens a ProfilePilot sign-in page. Type this code there. Only someone who can see this screen can approve the connection."),
        running && status.pairing_code ? h("div.row", h("span.pairing-code", status.pairing_code), copyButton(status.pairing_code, { label: "Copy code", small: false }))
          : h("p.small.faint", running ? "Run 'profilepilot connect status' to see the code." : "The code appears here once the connection is running."))),
  );
  const connections = status.connections || [];
  const stopRow = h("div.row.wrap",
    running ? h("button.btn.sm", { onclick: (e) => busy(e.currentTarget, () => stopSharing(false)) }, icon("stop"), "Stop sharing") : null,
    connections.length ? h("button.btn.sm.danger", { onclick: (e) => busy(e.currentTarget, () => stopSharing(true)) }, icon("x"), "Sign out all apps") : null);
  const connList = connections.length ? h("div.list", connections.map((g) => h("div.list-item", icon("chat"),
    h("div.grow", h("strong", g.client_name || g.client_id || "Connected app"),
      h("span.small.faint", `Approved ${fmt.ago(epoch(g.created_at))}${g.last_used_at ? ` · last used ${fmt.ago(epoch(g.last_used_at))}` : ""}`))))) : null;
  const footer = h("div.card-body.stack", { style: { gap: "12px" } },
    connList ? h("strong.small", "Apps you approved") : null, connList,
    h("div.callout", icon("shield"),
      h("span", "To stop sharing, close the terminal or use the button below (or ", h("code", commands.stop || "profilepilot connect stop"),
        "). ChatGPT can only use your profiles while the connection runs. Card numbers and passwords are never sent to ChatGPT.")),
    stopRow);
  return h("div.card", head, h("div.card-body", steps), footer);
}

function epoch(value) {
  if (typeof value === "number") return new Date(value * 1000).toISOString();
  return value;
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
    card("monitor", "Everything runs here", "The browsers, cookies and passwords stay on this computer. Claude Desktop, Claude Code, Codex and Cursor start ProfilePilot by themselves once connected above."),
    card("chat", "ChatGPT needs a tunnel", "ChatGPT lives in the cloud. A secure tunnel lets it reach ProfilePilot while you allow it; your pairing code makes sure nobody else can."),
    card("hand", "You stay in charge", "Take control of any profile with one click: the AI pauses on it until you hand it back. When the AI needs you, for a CAPTCHA or a login, it asks here."),
  );
}
