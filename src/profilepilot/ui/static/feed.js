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

/** Plain-language names of the AI's tools (the raw tool id stays in the tooltip). */
export const TOOL_LABELS = {
  browser_navigate: "Opened a page",
  browser_click: "Clicked",
  browser_type: "Typed",
  browser_paste: "Pasted text",
  browser_press_key: "Pressed a key",
  browser_hover: "Pointed at an element",
  browser_scroll: "Scrolled",
  browser_select_option: "Chose an option",
  browser_snapshot: "Looked at the page",
  browser_read: "Read the page",
  browser_extract: "Collected data",
  browser_screenshot: "Took a screenshot",
  browser_evaluate: "Ran a script",
  browser_tabs: "Managed tabs",
  browser_list: "Listed open pages",
  browser_wait_for: "Waited for the page",
  form_autofill: "Filled a form",
  form_autofill_sensitive: "Filled card or password details",
  form_detect: "Checked a form",
  http_fetch: "Fetched a page",
  cookies_get: "Read cookies",
  cookies_set: "Set cookies",
  cookies_clear: "Cleared cookies",
  cookies_export: "Saved cookies to a file",
  cookies_import: "Loaded cookies from a file",
  profile_list: "Listed profiles",
  profile_create: "Created a profile",
  profile_update: "Changed a profile",
  profile_delete: "Deleted a profile",
  profile_clone: "Copied a profile",
  profile_start: "Started the browser",
  profile_stop: "Closed the browser",
  profile_status: "Checked the status",
  profile_set_proxy: "Changed the proxy",
  profile_request_help: "Asked you for help",
  profiles_dashboard: "Showed the dashboard",
  dashboard_action: "Used the dashboard",
  proxy_list: "Listed proxies",
  proxy_add: "Added proxies",
  proxy_remove: "Removed a proxy",
  proxy_test: "Checked an IP address",
  identity_list: "Listed identities",
  identity_show: "Looked up an identity",
  identity_create: "Created an identity",
  identity_update: "Changed an identity",
  shardx_profiles: "Listed ShardX profiles",
  shardx_start: "Started a ShardX profile",
  shardx_stop: "Stopped a ShardX profile",
  shardx_status: "Checked ShardX",
};

/** "browser_navigate" -> "Opened a page"; Manager actions ("take control") -> "Take control". */
export function toolLabel(tool) {
  const raw = String(tool || "");
  if (TOOL_LABELS[raw]) return TOOL_LABELS[raw];
  const words = raw.replace(/_/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Action";
}

const BLOCKED_TEXT = /^(The user has taken control of profile|Profile '.*' is waiting for the user|Proxy '.*' is used by profile)/;

/** A call refused because the user was in control: not an error, the pause did its job. */
export function isBlockedEvent(e) {
  return !!e.blocked || (!e.ok && BLOCKED_TEXT.test(e.summary || ""));
}

export function feedItem(e, { fresh = false, onProfile } = {}) {
  const user = isUserEvent(e);
  const who = clientLabel(e);
  const blocked = isBlockedEvent(e);
  const label = toolLabel(e.tool);
  let summary;
  if (blocked) {
    summary = h("span.summary.blocked", { attrs: { title: e.summary || "" } }, icon("hand"), "Blocked – you were in control");
  } else {
    summary = h("span.summary", { class: e.ok ? "" : "err", attrs: { title: e.summary || "" } },
      e.ok ? null : h("span.sr-only", "Error: "), e.summary || (e.ok ? "Done" : "Failed"));
  }
  return h("div.feed-item", { class: [fresh ? "new" : "", blocked ? "is-blocked" : ""], attrs: { role: "listitem" } },
    h("span.time", { attrs: { title: fmt.dateTime(e.ts, true) } }, fmt.clock(e.ts)),
    h("span.who", { class: user ? "user" : "", attrs: { title: who, "aria-hidden": "true" } }, icon(user ? "user" : "sparkles")),
    h("span.prof", e.profile_name
      ? h("button.chip", { attrs: { type: "button", title: `Open ${e.profile_name}` }, onclick: () => onProfile && e.profile_id && onProfile(e.profile_id) },
        h("span.chip-text", e.profile_name))
      : h("span.faint", "—")),
    h("div.what",
      h("span.tool", { attrs: { title: e.tool } }, h("span.by", { class: user ? "user" : "" }, who), label),
      summary),
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
      feed.append(h("div.feed-day", { attrs: { role: "presentation" } }, label));
    }
    feed.append(feedItem(e, { fresh: freshIds.has(eventKey(e)), onProfile }));
  }
  return feed;
}

export function eventKey(e) {
  return `${e.ts}|${e.tool}|${e.profile_id || ""}|${e.summary || ""}`;
}
