// ProfilePilot Manager: app shell, routing, live events and keyboard shortcuts.

import { connectEvents, onUnauthorized } from "./api.js";
import { h, icon, isTyping, replace } from "./dom.js";
import { notifyHelp, renderHelpBanners } from "./help-banner.js";
import { closeProfileDrawer, openProfileDrawer } from "./profile-drawer.js";
import {
  loadChatGPT, loadIdentities, loadMeta, loadOverview, loadProxies, loadSettings, loadTrash, markAiActive, notify,
  proxyTestEnded, proxyTestReset, proxyTestStarted, removeProfile, setLive, state, subscribe, upsertProfile,
} from "./store.js";
import { applyTheme, currentTheme, onThemeChange } from "./theme-switch.js";
import { closeMenu, openDialog, toast } from "./ui.js";
import { createActivityView } from "./view-activity.js";
import { createConnectionsView } from "./view-connections.js";
import { createIdentitiesView } from "./view-identities.js";
import { createProfilesView } from "./view-profiles.js";
import { createProxiesView } from "./view-proxies.js";
import { createSettingsView } from "./view-settings.js";

const NAV = [
  { key: "profiles", label: "Profiles", icon: "profiles", hotkey: "p" },
  { key: "proxies", label: "Proxies", icon: "proxies", hotkey: "x" },
  { key: "identities", label: "Identities", icon: "identities", hotkey: "i" },
  { key: "activity", label: "Activity", icon: "activity", hotkey: "a" },
  { key: "connections", label: "Connections", icon: "connections", hotkey: "c" },
  { key: "settings", label: "Settings", icon: "settings", hotkey: "s" },
];
const THEMES = [["system", "monitor", "System theme"], ["light", "sun", "Light theme"], ["dark", "moon", "Dark theme"]];

const views = {};
let currentKey = null;
const navLinks = {};
const counts = {};
let mainHost;
let mainEl;
let bannerHost;
let offlineBanner;
let runningPill;
let versionLabel;

function factory(key) {
  switch (key) {
    case "profiles": return createProfilesView();
    case "proxies": return createProxiesView();
    case "identities": return createIdentitiesView();
    case "activity": return createActivityView();
    case "connections": return createConnectionsView();
    case "settings": return createSettingsView();
    default: return null;
  }
}

function view(key) {
  if (!views[key]) views[key] = factory(key);
  return views[key];
}

// ------------------------------------------------------------------ shell

function buildShell() {
  const app = document.getElementById("app");
  const nav = h("nav.nav", { attrs: { "aria-label": "Sections" } });
  for (const item of NAV) {
    const count = h("span.count");
    counts[item.key] = count;
    const link = h("a.nav-item", { href: `#/${item.key}`, attrs: { title: `${item.label} (G then ${item.hotkey.toUpperCase()})` } }, icon(item.icon, "lg"), h("span.label", item.label), count);
    navLinks[item.key] = link;
    nav.append(link);
  }
  runningPill = h("div.running-pill", { attrs: { title: "Profiles running now" } }, h("span.dot.stopped"), h("span.label", "No profiles running"));
  const themeButtons = THEMES.map(([value, iconName, label]) => h("button", { attrs: { type: "button", "aria-label": label, title: label, "aria-pressed": String(currentTheme() === value) }, onclick: () => applyTheme(value) }, icon(iconName)));
  // Narrow sidebar: one button that cycles System -> Light -> Dark.
  const themeCycle = h("button.btn.ghost.xs.icon-only.theme-cycle", { attrs: { type: "button" }, onclick: () => {
    const i = THEMES.findIndex(([v]) => v === currentTheme());
    applyTheme(THEMES[(i + 1) % THEMES.length][0]);
  } });
  const syncTheme = (value) => {
    themeButtons.forEach((b, i) => b.setAttribute("aria-pressed", String(THEMES[i][0] === value)));
    const [, iconName, label] = THEMES.find(([v]) => v === value) || THEMES[0];
    replace(themeCycle, icon(iconName));
    themeCycle.setAttribute("aria-label", `${label} (click to change)`);
    themeCycle.title = `${label} (click to change)`;
  };
  syncTheme(currentTheme());
  onThemeChange(syncTheme);
  versionLabel = h("span.version", "");
  const helpBtn = h("button.btn.ghost.xs.icon-only", { attrs: { type: "button", "aria-label": "Keyboard shortcuts", title: "Keyboard shortcuts (?)" }, onclick: () => shortcutsDialog() }, icon("keyboard"));
  const sidebar = h("aside.sidebar", { attrs: { "aria-label": "Main navigation" } },
    h("div.brand", h("img", { src: "/static/logo.svg", alt: "", width: 28, height: 28 }), h("div.brand-text", h("span.brand-name", "ProfilePilot"), h("span.brand-sub", "Manager"))),
    nav,
    h("div.sidebar-footer", runningPill,
      h("div.sidebar-meta", h("div.theme-switch", { attrs: { role: "group", "aria-label": "Theme" } }, themeButtons), themeCycle, helpBtn, versionLabel)));
  bannerHost = h("div.banners");
  offlineBanner = h("div.offline-banner.hidden", { attrs: { role: "status" } }, icon("alert"), "Lost the connection to ProfilePilot Manager. Retrying…");
  mainHost = h("div.view-host.view");
  mainEl = h("main.main", { id: "main", attrs: { tabindex: "-1" } }, offlineBanner, bannerHost, mainHost);
  const skip = h("a.skip-link", { href: "#main", onclick: (event) => { event.preventDefault(); focusMain(); } }, "Skip to content");
  replace(app, skip, sidebar, mainEl);
  app.removeAttribute("aria-busy");
}

/** Move the keyboard to the current view's first control (the skip link). */
function focusMain() {
  const target = mainHost.querySelector(".view-body button, .view-body a[href], .view-body input, .view-body [tabindex='0'], .view-header button")
    || mainEl;
  target.focus();
}

function renderChrome() {
  const profiles = [...state.profiles.values()];
  const running = profiles.filter((p) => p.state === "running").length;
  const help = state.help.length;
  counts.profiles.classList.toggle("alert", help > 0);
  counts.profiles.title = help ? `${help} waiting for you` : "";
  replace(counts.profiles, ...(help ? [h("span.dot.help", { attrs: { "aria-hidden": "true" } }), h("span", String(help))] : [profiles.length ? String(profiles.length) : ""]));
  counts.proxies.textContent = state.proxies.length ? String(state.proxies.length) : "";
  counts.identities.textContent = state.identities.length ? String(state.identities.length) : "";
  replace(counts.activity, state.live ? h("span.dot.running", { attrs: { title: "Live" } }) : "");
  const clients = state.clients || [];
  const connected = clients.filter((c) => c.registered === true).length + (state.chatgpt && state.chatgpt.running ? 1 : 0);
  counts.connections.textContent = connected ? String(connected) : "";
  for (const el of Object.values(counts)) el.classList.toggle("empty", !el.textContent && !el.firstElementChild);
  replace(runningPill, h("span.dot", { class: running ? "running pulse" : "stopped" }),
    h("span.pill-count", { attrs: { "aria-hidden": "true" } }, running ? String(running) : ""),
    h("span.label", running ? h("span", h("strong", String(running)), ` ${running === 1 ? "profile" : "profiles"} running`) : "No profiles running"));
  runningPill.title = running ? `${running} ${running === 1 ? "profile" : "profiles"} running` : "No profiles running";
  versionLabel.textContent = state.version ? `v${state.version}` : "";
  document.title = help ? `(${help}) ProfilePilot Manager` : "ProfilePilot Manager";
  renderHelpBanners(bannerHost);
}

// ------------------------------------------------------------------ routing

function route() {
  const hash = location.hash.replace(/^#\/?/, "");
  const [key] = hash.split("/");
  const target = NAV.some((n) => n.key === key) ? key : "profiles";
  show(target);
}

function show(key) {
  closeMenu();
  if (currentKey === key && mainHost.firstChild) return;
  const leaving = currentKey && views[currentKey];
  if (leaving && leaving.onHide) leaving.onHide();
  currentKey = key;
  for (const [k, link] of Object.entries(navLinks)) {
    if (k === key) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const v = view(key);
  replace(mainHost, v.el);
  if (v.onShow) v.onShow();
  if (!location.hash.startsWith(`#/${key}`)) history.replaceState(null, "", `#/${key}`);
}

// ------------------------------------------------------------------ live events

function startEvents() {
  let offlineTimer = null;
  let dropped = false;
  connectEvents({
    activity(e) {
      markAiActive(e);
      state.activity.unshift(e);
      if (state.activity.length > 600) state.activity.length = 600;
      if (views.activity) views.activity.markFresh(e);
      notify("activity", "profiles");
    },
    profile(view) { upsertProfile(view); },
    "profile-removed"(data) { removeProfile(data.id); },
    help(req) {
      // The banner above every view shows it; a desktop notification only when this window is in the background.
      notifyHelp(req, () => openProfileDrawer(req.profile_id));
    },
    proxies() { loadProxies().catch(() => {}); },
    identities() { loadIdentities().catch(() => {}); },
    settings() { loadSettings().catch(() => {}); },
    trash() { if (state.trash) loadTrash().catch(() => {}); },
    chatgpt() { loadChatGPT().catch(() => {}); },
    clients(data) { if (data && data.clients) { state.clients = data.clients; notify("clients"); } },
    "proxy-test"(data) {
      if (data.started) { proxyTestStarted(data); return; }
      state.proxyTest = { job: data.job, done: data.done, total: data.total, finished: !!data.finished, cancelled: !!data.cancelled };
      const id = data.proxy_id || (data.proxy && data.proxy.id);
      if (id) state.proxyTesting.delete(id);
      if (data.proxy) {
        const i = state.proxies.findIndex((p) => p.id === data.proxy.id);
        if (i >= 0) state.proxies[i] = { ...state.proxies[i], ...data.proxy, used_by: state.proxies[i].used_by };
      }
      if (data.finished) {
        state.proxyTesting.clear();
        if (data.cancelled) toast(`Stopped after ${data.done} of ${data.total}.`, { kind: "info", title: "Proxy test stopped" });
        else toast(`${data.ok} of ${data.total} working.`, { kind: data.ok === data.total ? "success" : "warn", title: "Proxy test finished" });
        proxyTestEnded({ cancelled: !!data.cancelled });
      }
      notify("proxy-test", "proxies");
    },
  }, (live) => {
    setLive(live);
    clearTimeout(offlineTimer);
    if (live) offlineBanner.classList.add("hidden");
    else offlineTimer = setTimeout(() => { if (!state.live) offlineBanner.classList.remove("hidden"); }, 4000);
    if (!live) dropped = true;
    else if (dropped) {
      dropped = false;
      // Proxy-test events sent while the stream was down are lost: a missed "finished" would leave the
      // progress stuck. A job that is still running shows up again with its next event.
      if (state.proxyTest && !state.proxyTest.finished) proxyTestReset();
      proxyTestEnded();
    }
    if (live && state.ready) loadOverview().catch(() => {});
  });
}

// ------------------------------------------------------------------ keyboard

let pendingG = 0;

function onKey(event) {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
  if (document.querySelector("dialog[open]")) return; // dialogs handle their own keys (Esc closes)
  if (isTyping(event.target)) return;
  const key = event.key;
  const v = currentKey && views[currentKey];
  if (pendingG && Date.now() - pendingG < 1200) {
    pendingG = 0;
    const target = NAV.find((n) => n.hotkey === key.toLowerCase());
    if (target) { event.preventDefault(); location.hash = `#/${target.key}`; }
    return;
  }
  if (key === "/" && v && v.focusSearch) { event.preventDefault(); v.focusSearch(); }
  else if ((key === "n" || key === "N") && v && v.newItem) { event.preventDefault(); v.newItem(); }
  else if (key === "g") { pendingG = Date.now(); }
  else if (key === "?") { event.preventDefault(); shortcutsDialog(); }
  else if (/^[1-6]$/.test(key)) { event.preventDefault(); location.hash = `#/${NAV[Number(key) - 1].key}`; }
  else if (key === "Escape") { closeMenu(); if (v && v.onEscape) v.onEscape(); }
}

function shortcutsDialog() {
  const then = (a, b) => [h("kbd", a), h("span.faint", " then "), h("kbd", b)];
  const rows = [
    ["Search the current list", [h("kbd", "/")]],
    ["New profile, proxy or identity", [h("kbd", "N")]],
    ["Go to a section by number", [h("kbd", "1"), h("span.faint", " – "), h("kbd", "6")]],
    ...NAV.map((n) => [`Go to ${n.label}`, then("G", n.hotkey.toUpperCase())]),
    ["Move between profiles", [h("kbd", "←"), h("kbd", "→"), h("kbd", "↑"), h("kbd", "↓")]],
    ["Close a dialog, menu or drawer", [h("kbd", "Esc")]],
    ["Show this list", [h("kbd", "?")]],
  ];
  openDialog({
    title: "Keyboard shortcuts", size: "narrow",
    body: h("div.kbd-list", rows.flatMap(([label, keys]) => [h("span", label), h("div.row.kbd-keys", keys)])),
  });
}

// ------------------------------------------------------------------ boot

function lockedScreen() {
  if (document.querySelector(".overlay-page")) return;
  state.locked = true;
  document.body.append(h("div.overlay-page", h("main.locked-card",
    h("img", { src: "/static/logo.svg", alt: "", width: 56, height: 56 }),
    h("h1", "Session ended"),
    h("p", "For your security this window is no longer signed in. Open ProfilePilot Manager again from its shortcut, or run ", h("code", "profilepilot ui"), " in a terminal."))));
}

function errorScreen(err, retry) {
  replace(mainHost, h("div.view-body", { style: { paddingTop: "48px" } }, h("div.empty",
    h("div.empty-icon", icon("alert")), h("h2", "Could not load ProfilePilot"), h("p", err.message),
    h("div.row", h("button.btn.primary", { onclick: retry }, icon("refresh"), "Try again")))));
}

async function boot() {
  buildShell();
  onUnauthorized(lockedScreen);
  subscribe((topics) => {
    if (state.ready) renderChrome();
    const v = currentKey && views[currentKey];
    if (v && v.update) v.update(topics);
  });
  window.addEventListener("hashchange", route);
  document.addEventListener("keydown", onKey);
  route();
  const load = async () => {
    try {
      await Promise.all([loadOverview(), loadMeta()]);
      startEvents();
      setInterval(() => notify("profiles"), 30000);
    } catch (err) {
      if (err.status !== 401) errorScreen(err, () => { route(); load(); });
    }
  };
  await load();
  window.addEventListener("beforeunload", () => closeProfileDrawer());
}

boot();
