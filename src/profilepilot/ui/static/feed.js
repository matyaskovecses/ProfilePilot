// Activity feed rendering, shared by the Activity view and the profile drawer.

import { fmt, h, icon } from "./dom.js";

export function isUserEvent(e) {
  return e.source === "ui" || e.source === "cli";
}

export function clientLabel(e) {
  const name = String(e.client || "").toLowerCase();
  if (isUserEvent(e)) return "You";
  if (name.includes("claude")) return "Claude";
  if (name.includes("openai") || name.includes("chatgpt")) return "ChatGPT";
  if (name.includes("codex")) return "Codex";
  if (name.includes("cursor")) return "Cursor";
  if (e.source === "mcp-http") return "Remote AI";
  return "AI";
}

export function feedItem(e, { fresh = false, onProfile } = {}) {
  const user = isUserEvent(e);
  const who = clientLabel(e);
  return h("div.feed-item", { class: fresh ? "new" : "", attrs: { role: "listitem" } },
    h("span.time", { attrs: { title: fmt.dateTime(e.ts) } }, fmt.time(e.ts)),
    h("span.who", { class: user ? "user" : "", attrs: { title: who, "aria-label": who } }, icon(user ? "user" : "sparkles")),
    h("span.prof", e.profile_name
      ? h("button.chip", { attrs: { type: "button", title: `Open ${e.profile_name}` }, onclick: () => onProfile && e.profile_id && onProfile(e.profile_id) },
        h("span.chip-text", e.profile_name))
      : h("span.faint", "—")),
    h("span.tool", { attrs: { title: e.tool } }, e.tool),
    h("span.summary", { class: e.ok ? "" : "err", attrs: { title: e.summary || "" } },
      e.ok ? null : h("span.sr-only", "Error: "), e.summary || (e.ok ? "OK" : "Failed")),
    h("span.dur", e.ms ? fmt.ms(e.ms) : ""),
  );
}

/** A day-grouped feed of events (newest first). */
export function renderFeed(events, { compact = false, freshIds = new Set(), onProfile } = {}) {
  const feed = h("div.feed", { class: compact ? "compact" : "", attrs: { role: "list", "aria-label": "Activity" } });
  let day = null;
  for (const e of events) {
    const label = fmt.day(e.ts);
    if (label !== day) {
      day = label;
      feed.append(h("div.feed-day", label));
    }
    feed.append(feedItem(e, { fresh: freshIds.has(eventKey(e)), onProfile }));
  }
  return feed;
}

export function eventKey(e) {
  return `${e.ts}|${e.tool}|${e.profile_id || ""}|${e.summary || ""}`;
}
