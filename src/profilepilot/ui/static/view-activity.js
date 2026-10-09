// Activity: a live, filterable feed of what the AI (and you) did, and when.

import { debounce, h, icon, replace } from "./dom.js";
import { clientLabel, eventKey, isBlockedEvent, isUserEvent, renderFeed, toolLabel } from "./feed.js";
import { openProfileDrawer } from "./profile-drawer.js";
import { loadActivity, profileList, state } from "./store.js";
import { emptyState, segmented } from "./ui.js";

export function createActivityView() {
  const filters = { profile: "", status: "all", who: "all", q: "" };
  let fresh = new Set();
  const live = h("span.live-indicator", h("span.dot.running.pulse"), "Live");
  const subtitle = h("p", "Every tool call and every action you take here, newest first.");
  const profileSelect = h("select.select.sm", { attrs: { "aria-label": "Filter by profile" }, style: { width: "auto", minWidth: "150px" } });
  profileSelect.addEventListener("change", () => { filters.profile = profileSelect.value; render(); });
  const statusSeg = segmented([{ value: "all", label: "All" }, { value: "errors", label: "Errors" }], "all", (v) => { filters.status = v; render(); }, { label: "Status" });
  const whoSeg = segmented([{ value: "all", label: "Everyone" }, { value: "ai", label: "AI", icon: "sparkles" }, { value: "user", label: "You", icon: "user" }], "all", (v) => { filters.who = v; render(); }, { label: "Who" });
  const search = h("input.input", { type: "search", attrs: { placeholder: "Search actions and results", "aria-label": "Search activity" } });
  search.addEventListener("input", debounce(() => { filters.q = search.value.trim().toLowerCase(); render(); }, 150));
  const header = h("header.view-header", h("div.view-title", h("h1", "Activity"), subtitle), h("div.view-actions", live));
  const toolbar = h("div.toolbar", h("div.search", icon("search"), search, h("kbd", "/")), profileSelect, whoSeg, statusSeg);
  const body = h("div.view-body");
  const el = h("section.view", { attrs: { "aria-label": "Activity" } }, header, toolbar, body);
  let loaded = false;

  function renderProfiles() {
    const current = filters.profile;
    replace(profileSelect, h("option", { value: "" }, "All profiles"), ...profileList().map((p) => h("option", { value: p.id }, p.name)));
    profileSelect.value = current;
  }

  function render() {
    live.classList.toggle("off", !state.live);
    live.lastChild.textContent = state.live ? "Live" : "Reconnecting…";
    if (!loaded) {
      replace(body, h("div.stack", Array.from({ length: 8 }, () => h("div.skeleton", { style: { height: "38px" } }))));
      return;
    }
    let events = state.activity;
    if (filters.profile) events = events.filter((e) => e.profile_id === filters.profile);
    if (filters.status === "errors") events = events.filter((e) => !e.ok && !isBlockedEvent(e));
    if (filters.who === "ai") events = events.filter((e) => !isUserEvent(e));
    if (filters.who === "user") events = events.filter((e) => isUserEvent(e));
    if (filters.q) events = events.filter((e) => `${e.tool} ${toolLabel(e.tool)} ${clientLabel(e)} ${e.summary} ${e.profile_name || ""}`.toLowerCase().includes(filters.q));
    if (!state.activity.length) {
      replace(body, emptyState({ icon: "activity", title: "Nothing has happened yet",
        text: "When your AI uses ProfilePilot, each step appears here as it happens: which profile, which tool, and what came of it." }));
      return;
    }
    if (!events.length) {
      replace(body, emptyState({ icon: "filter", title: "No matching activity", text: "Try other filters." }));
      return;
    }
    replace(body, renderFeed(events.slice(0, 400), { freshIds: fresh, onProfile: (id) => openProfileDrawer(id) }));
    fresh = new Set();
  }

  return {
    el,
    title: "Activity",
    async onShow() {
      renderProfiles();
      if (!loaded) {
        render();
        try { await loadActivity(); } catch (err) { /* shown as empty */ }
        loaded = true;
      }
      render();
    },
    update(topics) {
      if (topics.has("profiles")) renderProfiles();
      if (topics.has("activity") || topics.has("live")) render();
    },
    markFresh(event) { fresh.add(eventKey(event)); },
    focusSearch() { search.focus(); },
  };
}
