// The banner pinned above every view while the AI waits for the user (CAPTCHA, login, 2FA...).
//
// One banner, however many requests: a single request gets its own actions; several get a
// "N things need you" summary with a Review list. The banner is only rebuilt when the requests
// change (never on unrelated updates), and only a *new* request is announced to screen readers.

import { clear, fmt, h, icon, replace } from "./dom.js";
import { openProfileDrawer, requestKind } from "./profile-drawer.js";
import { actions, state, subscribe } from "./store.js";
import { busy, openDialog } from "./ui.js";

export function assistantName(req) {
  const name = String((req && req.requested_by) || "").toLowerCase();
  if (name.includes("claude")) return "Claude";
  if (name.includes("openai") || name.includes("chatgpt")) return "ChatGPT";
  if (name.includes("codex")) return "Codex";
  if (name.includes("cursor")) return "Cursor";
  return "Your AI";
}

/** Help requests are written by the AI: one that asks for the pairing code is suspicious. */
export function pairingCodeWarning(req) {
  if (!/pairing|pair(ing)?[\s-]?code|connection code|sign-?in code for profilepilot/i.test(String(req && req.message))) return null;
  return h("div.pairing-warning", icon("shield"),
    h("span", "ProfilePilot never asks for your pairing code in a help request. Type it only on the ProfilePilot sign-in page you opened yourself."));
}

/** The window action for a request: bring the running window up, or start the profile first. */
export function windowButton(req, { small = true } = {}) {
  const profile = state.profiles.get(req.profile_id);
  const running = profile && profile.state === "running";
  const headless = running && profile.window === "headless";
  if (headless) return null;
  return h("button.btn", { class: small ? "sm" : "", attrs: { type: "button" },
    onclick: (e) => busy(e.currentTarget, () => actions.openWindow(req.profile_id).catch(() => {})) },
  icon(running ? "focus" : "play"), running ? "Show window" : "Start and open window");
}

export function doneButtons(req, { small = true } = {}) {
  const cls = small ? "sm" : "";
  return [
    h("button.btn.primary", { class: cls, attrs: { type: "button", title: "I did it: hand the profile back to the AI" },
      onclick: (e) => busy(e.currentTarget, () => actions.resolveHelp(req.profile_id, req.id, "done").catch(() => {})) },
    icon("check"), "I'm done – hand back"),
    h("button.btn.ghost", { class: cls, attrs: { type: "button", title: "Tell the AI you didn't do it" },
      onclick: (e) => busy(e.currentTarget, () => actions.resolveHelp(req.profile_id, req.id, "dismissed").catch(() => {})) },
    "I can't do this"),
  ];
}

let lastSignature = null;
let whenEl = null;
let announcer = null;
const announced = new Set();
let reviewOpen = null;

function signature(requests) {
  const states = requests.map((r) => {
    const p = state.profiles.get(r.profile_id);
    return `${r.id}:${p ? `${p.state}/${p.window}` : "-"}`;
  });
  const perm = typeof Notification === "undefined" ? "na" : Notification.permission;
  return `${states.join(",")}|${perm}`;
}

function announce(requests) {
  const fresh = requests.filter((r) => !announced.has(r.id));
  requests.forEach((r) => announced.add(r.id));
  if (!fresh.length || !announcer) return;
  const req = fresh[fresh.length - 1];
  announcer.textContent = `${assistantName(req)} needs you in ${req.profile_name}: ${req.message}`;
}

function bellButton(host) {
  if (typeof Notification === "undefined" || Notification.permission !== "default") return null;
  return h("button.btn.sm.ghost.icon-only", {
    attrs: { type: "button", title: "Notify me on the desktop when the AI needs me", "aria-label": "Turn on desktop notifications" },
    onclick: async () => { await Notification.requestPermission(); lastSignature = null; renderHelpBanners(host); },
  }, icon("bell"));
}

export function renderHelpBanners(host) {
  if (!announcer) {
    announcer = h("div.sr-only", { attrs: { role: "alert" } });
    host.parentElement && host.parentElement.insertBefore(announcer, host);
    host.setAttribute("role", "region");
    host.setAttribute("aria-label", "Requests from your AI");
  }
  const requests = state.help;
  if (!requests.length) {
    if (lastSignature !== "") clear(host);
    lastSignature = "";
    whenEl = null;
    return;
  }
  const sig = signature(requests);
  if (sig === lastSignature) {
    if (whenEl) whenEl.textContent = fmt.ago(requests[0].created_at); // keep "2 minutes ago" fresh, quietly
    return;
  }
  lastSignature = sig;
  announce(requests);
  const first = requests[0];
  whenEl = h("span.when", fmt.ago(first.created_at));
  let banner;
  if (requests.length === 1) {
    banner = h("div.help-banner",
      h("div.bot", icon("robot")),
      h("div.msg",
        h("span.msg-title", `${assistantName(first)} needs you in `,
          h("a", { href: "#/profiles", onclick: (e) => { e.preventDefault(); openProfileDrawer(first.profile_id); } }, first.profile_name),
          h("span.faint", ` · ${requestKind(first.kind)}`)),
        h("span.msg-text", { attrs: { title: first.message } }, `“${first.message}”`),
        pairingCodeWarning(first)),
      whenEl,
      h("div.help-actions", windowButton(first), ...doneButtons(first), bellButton(host)));
  } else {
    banner = h("div.help-banner.multi",
      h("div.bot", icon("robot"), h("span.bot-count", String(requests.length))),
      h("div.msg",
        h("span.msg-title", `${requests.length} things need you`),
        h("span.msg-text", { attrs: { title: first.message } },
          `${assistantName(first)}: ${first.message} in ${first.profile_name}`)),
      whenEl,
      h("div.help-actions",
        h("button.btn.sm.primary", { attrs: { type: "button" }, onclick: () => reviewRequests() }, "Review"),
        bellButton(host)));
  }
  replace(host, banner);
}

/** The list of every open request, with the actions for each. */
export function reviewRequests() {
  if (reviewOpen) return;
  const list = h("div.help-review");
  const render = () => {
    if (!state.help.length) { dlg.forceClose(); return; }
    replace(list, ...state.help.map((req) => h("div.help-review-item",
      h("div.row",
        h("strong", h("a", { href: "#/profiles", onclick: (e) => { e.preventDefault(); dlg.forceClose(); openProfileDrawer(req.profile_id); } }, req.profile_name)),
        h("span.badge.amber", requestKind(req.kind)),
        h("span.spacer"),
        h("span.small.faint", `${assistantName(req)} · ${fmt.ago(req.created_at)}`)),
      h("p", req.message),
      pairingCodeWarning(req),
      h("div.row.wrap", windowButton(req), ...doneButtons(req)))));
  };
  const dlg = openDialog({
    title: `${state.help.length} requests from your AI`, size: "wide",
    description: "The AI waits on each of these profiles until you answer.",
    body: list,
    onClose: () => { unsub(); reviewOpen = null; },
  });
  const unsub = subscribe((topics) => { if (topics.has("help") || topics.has("profiles")) render(); });
  reviewOpen = dlg;
  render();
}

/** A desktop notification, only when this window is in the background (the banner covers the rest). */
export function notifyHelp(req, onClick) {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  if (!document.hidden && document.hasFocus()) return;
  try {
    const n = new Notification(`${assistantName(req)} needs you in ${req.profile_name || "a profile"}`, {
      body: req.message, tag: `pp-help-${req.id}`, icon: "/static/logo.svg", requireInteraction: true,
    });
    n.onclick = () => { window.focus(); if (onClick) onClick(); n.close(); };
  } catch (err) {
    /* notifications unavailable */
  }
}
