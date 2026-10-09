// The banner pinned above every view while the AI waits for the user (CAPTCHA, login, 2FA...).

import { clear, fmt, h, icon, replace } from "./dom.js";
import { openProfileDrawer, requestKind } from "./profile-drawer.js";
import { actions, state } from "./store.js";
import { busy } from "./ui.js";

export function assistantName(req) {
  const name = String((req && req.requested_by) || "").toLowerCase();
  if (name.includes("claude")) return "Claude";
  if (name.includes("openai") || name.includes("chatgpt")) return "ChatGPT";
  if (name.includes("codex")) return "Codex";
  if (name.includes("cursor")) return "Cursor";
  return "Your AI";
}

export function renderHelpBanners(host) {
  const requests = state.help;
  if (!requests.length) {
    clear(host);
    return;
  }
  const shown = requests.slice(0, 3);
  const banners = shown.map((req) => {
    const profile = state.profiles.get(req.profile_id);
    const running = profile && profile.state === "running";
    return h("div.help-banner", { attrs: { role: "alert" } },
      h("div.bot", icon("robot")),
      h("div.msg",
        h("span.msg-title", `${assistantName(req)} needs you in `, h("a", { href: "#/profiles", onclick: (e) => { e.preventDefault(); openProfileDrawer(req.profile_id); } }, req.profile_name),
          h("span.faint", ` · ${requestKind(req.kind)}`)),
        h("span.msg-text", { attrs: { title: req.message } }, `“${req.message}”`)),
      h("span.when", fmt.ago(req.created_at)),
      running ? h("button.btn.sm", { onclick: (e) => busy(e.currentTarget, () => actions.focus(req.profile_id).catch(() => {})) }, icon("focus"), "Focus window")
        : h("button.btn.sm", { onclick: (e) => busy(e.currentTarget, () => actions.start(req.profile_id, "normal").then(() => actions.focus(req.profile_id)).catch(() => {})) }, icon("play"), "Open window"),
      h("button.btn.sm.primary", { onclick: (e) => busy(e.currentTarget, () => actions.resolveHelp(req.profile_id, req.id, "done").catch(() => {})), attrs: { title: "I did it: hand the profile back to the AI" } }, icon("check"), "Done"),
      h("button.btn.sm.ghost", { onclick: (e) => busy(e.currentTarget, () => actions.resolveHelp(req.profile_id, req.id, "dismissed").catch(() => {})), attrs: { title: "Dismiss: the AI is told you did not do it" } }, "Dismiss"),
    );
  });
  if (typeof Notification !== "undefined" && Notification.permission === "default") {
    const last = banners[banners.length - 1];
    last.append(h("button.btn.sm.ghost.icon-only", {
      attrs: { title: "Notify me on the desktop when the AI needs me", "aria-label": "Turn on desktop notifications" },
      onclick: async () => { await Notification.requestPermission(); renderHelpBanners(host); },
    }, icon("bell")));
  }
  const extra = requests.length > shown.length
    ? h("div.help-more", `+${requests.length - shown.length} more ${requests.length - shown.length === 1 ? "request" : "requests"} waiting · see the profiles marked “Needs you”`)
    : null;
  replace(host, ...banners, ...(extra ? [extra] : []));
}

export function notifyHelp(req, onClick) {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try {
    const n = new Notification(`${assistantName(req)} needs you in ${req.profile_name || "a profile"}`, {
      body: req.message, tag: `pp-help-${req.id}`, icon: "/static/logo.svg", requireInteraction: true,
    });
    n.onclick = () => { window.focus(); if (onClick) onClick(); n.close(); };
  } catch (err) {
    /* notifications unavailable */
  }
}
