// The profile details drawer: Overview, Tabs, Cookies, Activity, Settings.

import { api, enc } from "./api.js";
import { createCookiesPane } from "./cookies.js";
import { avatar, copyText, debounce, fmt, h, icon, replace } from "./dom.js";
import { clientLabel, isUserEvent, renderFeed, toolLabel } from "./feed.js";
import { assistantName, doneButtons, pairingCodeWarning, windowButton } from "./help-banner.js";
import {
  AUTO_BROWSER_LABEL, BROWSER_LABELS, TIMEZONE_HINT, WINDOW_CHOICES, WINDOW_LABELS, browserOptions, cloneDialog, confirmDelete,
  identityOptions, proxyPicker, timezoneList,
} from "./profile-dialogs.js";
import { actions, displayStatus, loadProxies, proxyLabel, runWindow, state, subscribe, upsertProfile } from "./store.js";
import { attachThumb } from "./thumbs.js";
import {
  busy, changeTracker, chipInput, closeButton, copyable, countryBadge, emptyState, field, nextId, openDrawer, openMenu, segmented,
  select, statusDot, toast, toggle,
} from "./ui.js";

let current = null;

export function closeProfileDrawer() {
  if (current) current.drawer.close();
}

export function requestKind(kind) {
  return { captcha: "CAPTCHA", login: "Login", verification: "Verification code", payment: "Payment step", other: "Help" }[kind] || "Help";
}

/** Seconds a profile has run in total, including the current session. */
export function totalRuntime(p) {
  let total = p.total_runtime_s || 0;
  if (p.state === "running" && p.runtime && p.runtime.started_at) {
    total += Math.max(0, (Date.now() - new Date(p.runtime.started_at).getTime()) / 1000);
  }
  return total;
}

/** "Needs you · CAPTCHA, 5 min ago" / "You're in control · since 14:02" / "Running for 5 min". */
export function statusDetail(p) {
  const st = displayStatus(p);
  const control = p.control || {};
  if (st.key === "help") {
    const req = control.help[0];
    const more = control.help.length > 1 ? ` (+${control.help.length - 1} more)` : "";
    return `${st.label} · ${requestKind(req.kind)}${more}, ${fmt.agoShort(req.created_at)}`;
  }
  if (st.key === "paused") {
    const since = control.pause && control.pause.since;
    return `${st.label}${since ? ` · since ${fmt.clock(since)}` : ""}`;
  }
  if (st.runtime === "running" && p.runtime && p.runtime.started_at) return `Running for ${fmt.span((Date.now() - new Date(p.runtime.started_at).getTime()) / 1000)}`;
  return st.label;
}

export function openProfileDrawer(id, tab = "overview") {
  if (current && current.id === id) {
    current.show(tab);
    return;
  }
  if (current) {
    const leaving = current;
    const closing = leaving.drawer.close();
    if (leaving.drawer.el.open) {
      // Unsaved settings: "Discard changes?" is up. Open this profile only once that drawer has closed.
      closing.then(() => {
        if (leaving.drawer.el.open) return; // "Keep editing"
        if (current === leaving) current = null;
        openProfileDrawer(id, tab);
      });
      return;
    }
  }
  const p0 = state.profiles.get(id);
  if (!p0) return;
  const ctx = { id, tab, check: null, settingsBuilt: false, settingsDirty: () => false };
  const drawer = openDrawer({
    label: `Profile ${p0.name}`,
    isDirty: () => ctx.settingsDirty(),
    onClose: () => {
      unsub();
      if (ctx.thumb) ctx.thumb.stop();
      if (current === ctx) current = null;
    },
  });
  ctx.drawer = drawer;

  const titleName = h("h2");
  const statusLine = h("div.row.small.muted");
  const avatarHost = h("div");
  drawer.header.append(avatarHost, h("div.titles", titleName, statusLine), closeButton(drawer.close));

  const panelId = nextId("panel");
  const panel = h("div.drawer-panel", { id: panelId, attrs: { role: "tabpanel", tabindex: "-1" } });
  drawer.body.append(panel);
  const cookies = createCookiesPane(id);
  const panes = { overview: h("div.stack"), tabs: h("div.stack"), cookies: cookies.el, activity: h("div.stack"), settings: h("div.stack") };
  const tabButtons = {};
  const tabDefs = [["overview", "Overview", "info"], ["tabs", "Tabs", "tab"], ["cookies", "Cookies", "cookie"], ["activity", "Activity", "activity"],
    ["settings", "Settings", "settings"]];
  for (const [key, label, iconName] of tabDefs) {
    const btn = h("button", { id: `${panelId}-${key}`, attrs: { role: "tab", type: "button", "aria-selected": "false", "aria-controls": panelId, tabindex: "-1" }, onclick: () => show(key) }, icon(iconName), label);
    tabButtons[key] = btn;
    drawer.tabs.append(btn);
  }
  drawer.tabs.addEventListener("keydown", (event) => {
    const keys = tabDefs.map(([k]) => k);
    const i = keys.indexOf(ctx.tab);
    let next = null;
    if (event.key === "ArrowRight") next = keys[(i + 1) % keys.length];
    else if (event.key === "ArrowLeft") next = keys[(i - 1 + keys.length) % keys.length];
    else if (event.key === "Home") next = keys[0];
    else if (event.key === "End") next = keys[keys.length - 1];
    if (!next) return;
    event.preventDefault();
    show(next);
    tabButtons[next].focus();
  });

  // persistent big thumbnail
  const bigImg = h("img", { attrs: { alt: "" } });
  const bigState = h("div.thumb-placeholder");
  const bigThumb = h("div.thumb.big-thumb", bigImg, bigState);
  ctx.thumb = attachThumb(bigThumb, bigImg, id, {
    active: () => { const p = state.profiles.get(id); return !!p && p.state === "running"; },
    interval: 2000,
    onState: (s) => { ctx.thumbState = s; bigState.classList.toggle("hidden", s === "live"); if (s !== "live") renderBigState(); },
  });

  function renderBigState() {
    const p = state.profiles.get(id);
    if (!p) return;
    const st = displayStatus(p);
    let content;
    if (p.state === "running") {
      const reason = ctx.thumbState;
      content = [avatar(p.name, "", p.id), h("div.thumb-state", reason === "minimized" ? [icon("tab"), "Window is minimized"]
        : reason === "page-error" ? [icon("alert"), p.proxy ? "Page couldn't load – check the proxy" : "Page couldn't load"]
          : runWindow(p) === "headless" ? [icon("eye"), "Headless: no window"] : [h("span.spinner.sm"), "Loading preview…"])];
    } else {
      content = [avatar(p.name, "", p.id), h("div.thumb-state", statusDot(st.key), st.label)];
    }
    bigState.style.setProperty("--hue", String(hueOf(p)));
    replace(bigState, ...content);
  }

  function header(p) {
    const st = displayStatus(p);
    titleName.textContent = p.name;
    replace(avatarHost, avatar(p.name, "md", p.id));
    replace(statusLine, statusDot(st.key), h("span", statusDetail(p)));
  }

  function show(key) {
    ctx.tab = key;
    for (const [k, btn] of Object.entries(tabButtons)) {
      btn.setAttribute("aria-selected", String(k === key));
      btn.tabIndex = k === key ? 0 : -1;
    }
    panel.setAttribute("aria-labelledby", tabButtons[key].id);
    replace(panel, panes[key]);
    drawer.footer.classList.toggle("hidden", key !== "settings");
    drawer.el.classList.toggle("cookies-tab", key === "cookies"); // a wider drawer for the cookie list
    const p = state.profiles.get(id);
    if (!p) return;
    if (key === "overview") renderOverview(p);
    if (key === "tabs") renderTabs(p);
    if (key === "cookies") cookies.show(p);
    if (key === "activity") renderActivity(p);
    if (key === "settings" && !ctx.settingsBuilt) renderSettings(p);
  }
  ctx.show = show;

  // ------------------------------------------------------------ overview

  function renderOverview(p) {
    const st = displayStatus(p);
    const items = [];
    const control = p.control || {};
    const help = control.help || [];
    for (const req of help) {
      items.push(h("div.callout.warn",
        icon("robot"),
        h("div.stack.grow", { style: { gap: "6px" } },
          h("strong", `${assistantName(req)} needs you: ${requestKind(req.kind)}`, h("span.faint", { style: { fontWeight: 400 } }, ` · ${fmt.agoShort(req.created_at)}`)),
          h("span", req.message),
          pairingCodeWarning(req),
          h("div.row.wrap", { style: { marginTop: "4px" } }, windowButton(req), ...doneButtons(req)))));
    }
    if (control.user_pause) {
      items.push(h("div.callout", icon("hand"),
        h("div.stack.grow", { style: { gap: "6px" } },
          h("strong", "You're in control"),
          h("span", control.user_pause.note && /could not read/i.test(control.user_pause.note) ? control.user_pause.note
            : `The AI won't act on this profile until you hand it back${control.user_pause.since ? ` (since ${fmt.clock(control.user_pause.since)})` : ""}.`),
          h("div.row", h("button.btn.sm.primary", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => actions.handBack(id).catch(() => {})) }, icon("sparkles"), "Hand back to AI")))));
    }
    if (p.state === "crashed") {
      items.push(h("div.callout.warn", icon("alert"), h("div.stack.grow", { style: { gap: "6px" } },
        h("strong", "Chrome closed unexpectedly. Start it again."),
        h("span.faint", { attrs: { title: p.crash || "" } }, p.crash ? `Reported: ${p.crash}` : ""),
        h("div.row", h("button.btn.sm", { attrs: { type: "button" }, onclick: () => api.post("/api/reveal", { what: "log", id }).catch((e) => toast(e.message, { kind: "error" })) }, icon("file"), "Open log")))));
    }
    if (p.proxy && p.proxy.ok === false && !help.length) {
      items.push(h("div.callout.warn", icon("proxies"), h("div.stack.grow", { style: { gap: "2px" } },
        h("strong", "Proxy not reachable"),
        h("span", p.proxy.reason || "The last check of this profile's proxy failed."))));
    }
    if (p.runtime && p.runtime.client_job) {
      items.push(h("div.callout.warn", icon("info"), h("span", "This browser was started by an AI app that closes it when the app disconnects. Start it from here to keep it running.")));
    }
    items.push(bigThumb);
    renderBigState();
    if (p.state === "running" && ctx.thumbState === "live") bigState.classList.add("hidden");
    else bigState.classList.remove("hidden");
    if (p.state !== "running") { bigImg.classList.remove("loaded"); }

    const running = p.state === "running";
    const pending = state.pending.get(id);
    const bar = h("div.action-bar");
    if (running && runWindow(p) !== "headless" && !help.length) bar.append(h("button.btn", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => actions.focus(id).catch(() => {})) }, icon("focus"), "Show window"));
    if (pending) bar.append(h("button.btn", { disabled: true }, h("span.spinner"), pending === "stopping" ? "Stopping…" : "Starting…"));
    else if (running || p.state === "starting") bar.append(h("button.btn", { attrs: { type: "button" }, onclick: () => actions.stop(id).catch(() => {}) }, icon("stop"), "Stop"));
    else if (!help.length) bar.append(h("button.btn", { class: control.paused ? "" : "primary", attrs: { type: "button" }, onclick: () => actions.start(id).catch(() => {}) }, icon("play"), "Start"));
    // Hand back lives in the callout above; the bar only offers to take control.
    if (!control.paused) bar.append(h("button.btn", { attrs: { type: "button", title: "Pause the AI on this profile and work in it yourself" }, onclick: (e) => busy(e.currentTarget, () => actions.takeControl(id).catch(() => {})) }, icon("hand"), "Take control"));
    bar.append(h("button.btn", { attrs: { type: "button", title: "Which IP address and country websites see for this profile" }, onclick: (e) => busy(e.currentTarget, () => checkRoute(p)) }, icon("zap"), "Check IP address"));
    const more = h("button.btn.icon-only", { attrs: { type: "button", "aria-label": "More actions", title: "More", "aria-haspopup": "menu" } }, icon("more"));
    more.addEventListener("click", () => moreMenu(p, more));
    bar.append(more);
    items.push(bar);
    items.push(openPageForm(p));

    if (ctx.check) items.push(checkCallout(ctx.check));

    const kv = h("dl.kv");
    const row = (k, ...v) => kv.append(h("dt", k), h("dd", ...v));
    row("Status", statusDot(st.key), statusDetail(p));
    row("Window", WINDOW_LABELS[runWindow(p)] || p.window);
    const browserName = p.browser === "auto" ? AUTO_BROWSER_LABEL : (BROWSER_LABELS[p.browser] || p.browser);
    row("Browser", (p.runtime && p.runtime.browser_version) ? `${browserName} · ${p.runtime.browser_version}` : browserName);
    row("Proxy", ...proxyLine(p));
    row("Identity", p.identity ? h("span", p.identity.name, p.identity.missing ? h("span.badge.amber", "missing") : null) : h("span.faint", "None"));
    row("Last AI action", lastAction);
    row("Data folder", copyable(p.path), h("button.btn.xs.ghost", { attrs: { type: "button" }, onclick: () => api.post("/api/reveal", { what: "profile", id }).catch((e) => toast(e.message, { kind: "error" })) }, icon("folder"), "Open"));
    row("Created", fmt.dateTime(p.created_at));
    row("Last started", p.last_started_at ? fmt.ago(p.last_started_at) : h("span.faint", "Never"));
    row("Total runtime", fmt.span(totalRuntime(p)));
    if (p.tags.length) row("Tags", ...p.tags.map((t) => h("span.tag", t)));
    if (p.notes) row("Notes", h("span", { style: { whiteSpace: "pre-wrap" } }, p.notes));
    const advanced = running && p.runtime && p.runtime.cdp_http_url
      ? h("details.advanced", { open: ctx.advancedOpen || false, ontoggle: (e) => { ctx.advancedOpen = e.currentTarget.open; } },
        h("summary", icon("chevron-right"), "Advanced"),
        h("dl.kv", h("dt", "DevTools address"), h("dd", copyable(p.runtime.cdp_http_url)),
          h("dt", "Browser process"), h("dd", h("span.mono", String(p.runtime.chrome_pid || "—")))))
      : null;
    items.push(h("div.card.card-pad", kv, advanced));
    replace(panes.overview, ...items);
  }

  function moreMenu(p, anchor) {
    const running = p.state === "running";
    openMenu(anchor, [
      !running ? { label: "Start off-screen", icon: "play", onClick: () => actions.start(id, "offscreen").catch(() => {}) } : null,
      running ? { label: "Open tabs", icon: "tab", onClick: () => show("tabs") } : null,
      { label: "Edit settings", icon: "edit", onClick: () => show("settings") },
      { label: "Clone…", icon: "clone", onClick: () => cloneDialog(p) },
      { label: "Open data folder", icon: "folder", onClick: () => api.post("/api/reveal", { what: "profile", id }).catch((e) => toast(e.message, { kind: "error" })) },
      running && p.runtime && p.runtime.cdp_http_url ? { label: "Copy DevTools address (advanced)", icon: "code", onClick: () => copyText(p.runtime.cdp_http_url).then(() => toast("DevTools address copied.", { kind: "success" })) } : null,
      "-",
      { label: "Delete…", icon: "trash", danger: true, onClick: async () => { if (await confirmDelete(state.profiles.get(id))) drawer.forceClose(); } },
    ]);
  }

  function openPageForm(p) {
    const input = h("input.input", { type: "text", attrs: { placeholder: p.state === "running" ? "Open a page in this profile, e.g. example.com" : "Start this profile on a page, e.g. example.com",
      "aria-label": "Address to open", autocomplete: "off", spellcheck: "false" } });
    const go = h("button.btn", { attrs: { type: "submit" } }, icon("arrow-right"), p.state === "running" ? "Open" : "Start here");
    return h("form.row", {
      onsubmit: (event) => {
        event.preventDefault();
        if (!input.value.trim()) { input.focus(); return; }
        busy(go, async () => {
          try {
            const result = await api.post(`/api/profiles/${enc(id)}/open`, { url: input.value.trim() });
            input.value = "";
            upsertProfile(result.profile);
            toast(result.started ? "The profile started on that page." : "Opened in a new tab.", { kind: "success" });
          } catch (err) {
            toast(err.message, { kind: "error", title: "Could not open the page" });
          }
        });
      },
    }, h("div.grow", input), go);
  }

  function proxyLine(p) {
    if (!p.proxy) return [h("span.faint", "Direct connection (no proxy)")];
    const parts = [h("span", proxyLabel(p.proxy))];
    if (p.proxy.missing) parts.push(h("span.badge.amber", "missing"));
    else {
      parts.push(h("code", `${p.proxy.scheme}://${p.proxy.host}:${p.proxy.port}`));
      if (p.proxy.country_code) parts.push(countryBadge(p.proxy.country_code));
      if (p.proxy.ok === false) parts.push(h("span.badge.red", { attrs: { title: p.proxy.reason || "" } }, "Not reachable"));
    }
    if (p.runtime && p.runtime.proxy_id !== (p.proxy_id || null) && p.state === "running") parts.push(h("span.badge.amber", "restart to apply"));
    return parts;
  }

  async function checkRoute(p) {
    try {
      const result = await api.post(`/api/profiles/${enc(p.id)}/check`);
      ctx.check = result;
      if (result.route.startsWith("saved proxy")) loadProxies().catch(() => {});
    } catch (err) {
      ctx.check = { route: "", check: { ok: false, reason: err.message } };
    }
    if (ctx.tab === "overview") renderOverview(state.profiles.get(id));
  }

  const lastAction = h("span.faint", "…");
  async function refreshLastAction() {
    try {
      const events = (await api.get(`/api/activity?profile=${enc(id)}&limit=20`)).events;
      const e = events.find((x) => !isUserEvent(x));
      replace(lastAction, e ? [h("span.client-chip", clientLabel(e)), " ", h("span", { attrs: { title: e.tool } }, toolLabel(e.tool)),
        e.summary ? h("span.muted.ellipsis", { attrs: { title: e.summary } }, ` · ${e.summary.slice(0, 80)}`) : null,
        h("span.faint", ` · ${fmt.ago(e.ts)}`)] : h("span.faint", "None yet"));
      lastAction.className = "last-action";
    } catch (err) {
      replace(lastAction, "");
    }
  }
  refreshLastAction();

  // ------------------------------------------------------------ tabs

  async function renderTabs(p) {
    if (p.state !== "running") {
      replace(panes.tabs, emptyState({ icon: "tab", title: "Not running", text: "Start the profile to see its open tabs.",
        actions: [h("button.btn.primary", { attrs: { type: "button" }, onclick: () => actions.start(id).then(() => renderTabs(state.profiles.get(id))).catch(() => {}) }, icon("play"), "Start")] }));
      return;
    }
    replace(panes.tabs, h("div.skeleton.skeleton-line"), h("div.skeleton.skeleton-line"));
    let tabs;
    try {
      tabs = (await api.get(`/api/profiles/${enc(id)}/tabs`)).tabs;
    } catch (err) {
      replace(panes.tabs, h("div.callout.warn", icon("alert"), err.message));
      return;
    }
    const list = h("div.list");
    for (const t of tabs) {
      list.append(h("div.list-item",
        icon("tab"),
        h("div.grow", h("span.ellipsis", { attrs: { title: t.title } }, t.title || "Untitled"), h("span.ellipsis.small.faint", { attrs: { title: t.url } }, t.url)),
        t.active ? h("span.badge.violet", "Active") : null,
        h("button.btn.xs", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => api.post(`/api/profiles/${enc(id)}/tabs/${enc(t.id)}/activate`).then(() => renderTabs(state.profiles.get(id))).catch((err) => toast(err.message, { kind: "error" }))) }, "Show"),
        tabs.length > 1 ? h("button.btn.xs.ghost.icon-only", { attrs: { type: "button", "aria-label": `Close tab ${t.title || ""}`.trim(), title: "Close tab" }, onclick: (e) => busy(e.currentTarget, () => api.post(`/api/profiles/${enc(id)}/tabs/${enc(t.id)}/close`).then(() => renderTabs(state.profiles.get(id))).catch((err) => toast(err.message, { kind: "error" }))) }, icon("x")) : null,
      ));
    }
    replace(panes.tabs,
      h("div.row", h("span.muted.small", `${tabs.length} open ${tabs.length === 1 ? "tab" : "tabs"} · most recently used first`), h("span.spacer"),
        h("button.btn.sm.ghost", { attrs: { type: "button" }, onclick: () => renderTabs(state.profiles.get(id)) }, icon("refresh"), "Refresh")),
      list);
  }

  // ------------------------------------------------------------ activity

  async function renderActivity() {
    let events = [];
    try {
      events = (await api.get(`/api/activity?profile=${enc(id)}&limit=150`)).events;
    } catch (err) {
      replace(panes.activity, h("div.callout.warn", icon("alert"), err.message));
      return;
    }
    if (!events.length) {
      replace(panes.activity, emptyState({ icon: "activity", title: "No activity yet", text: "Everything the AI and you do with this profile shows up here." }));
      return;
    }
    replace(panes.activity, renderFeed(events, { compact: true }));
  }
  const refreshActivity = debounce(() => { if (ctx.tab === "activity") renderActivity(); }, 400);
  const refreshLastActionSoon = debounce(() => refreshLastAction(), 600);

  // ------------------------------------------------------------ settings

  function renderSettings(p) {
    ctx.settingsBuilt = true;
    const name = h("input.input", { value: p.name, attrs: { maxlength: "64" } });
    const tags = chipInput(p.tags, { onChange: () => sync() });
    const notes = h("textarea.textarea", { value: p.notes || "", attrs: { rows: "3" } });
    const proxy = proxyPicker(p.proxy_id || "");
    const identity = select(identityOptions(), p.identity_id || "");
    const browser = select(browserOptions(), p.browser || "auto");
    const win = segmented(WINDOW_CHOICES.map((c) => ({ value: c.value, label: c.title })), p.launch.window, () => sync(), { label: "Window" });
    const lang = h("input.input", { value: p.launch.lang || "", attrs: { placeholder: "System language" } });
    const tz = h("input.input", { value: p.launch.timezone || "", attrs: { placeholder: "This computer's time zone", list: "pp-timezones" } });
    const startUrl = h("input.input", { value: p.launch.start_url || "", attrs: { placeholder: "https://…" } });
    const restore = toggle(p.launch.restore_session !== false, { label: "Reopen tabs and keep session cookies" });
    const webrtc = select([{ value: "auto", label: "Automatic (proxy only when proxied)" }, { value: "proxy_only", label: "Proxy only (never leak the real IP)" }, { value: "default", label: "Chrome default" }], p.launch.webrtc || "auto");
    const payload = () => ({
      name: name.value.trim(), tags: tags.values, notes: notes.value, identity_id: identity.value || "",
      browser: browser.value, ...proxy.value(),
      launch: { window: win.value, lang: lang.value.trim() || null, timezone: tz.value.trim() || null, start_url: startUrl.value.trim() || null,
        restore_session: restore.checked, webrtc: webrtc.value },
    });
    const changes = changeTracker(() => JSON.stringify([name.value.trim(), tags.pending, notes.value, identity.value, browser.value, proxy.snapshot(),
      win.value, lang.value.trim(), tz.value.trim(), startUrl.value.trim(), restore.checked, webrtc.value]));
    ctx.settingsDirty = () => changes.dirty();
    const save = h("button.btn.primary", { attrs: { type: "submit", form: `${panelId}-settings` } }, "Save changes");
    const discard = h("button.btn.ghost", { attrs: { type: "button" }, onclick: () => { ctx.settingsBuilt = false; ctx.settingsDirty = () => false; renderSettings(state.profiles.get(id)); } }, "Discard");
    const unsaved = h("span.small.muted", "Unsaved changes");
    function sync() {
      const dirty = changes.dirty();
      save.disabled = !dirty;
      discard.classList.toggle("hidden", !dirty);
      unsaved.classList.toggle("hidden", !dirty);
    }
    const form = h("form.form", {
      id: `${panelId}-settings`,
      onsubmit: (event) => {
        event.preventDefault();
        if (!changes.dirty()) return;
        busy(save, async () => {
          const data = payload();
          try {
            const result = await api.patch(`/api/profiles/${enc(id)}`, data);
            proxy.clear();
            upsertProfile(result.profile);
            if (data.proxy_url) loadProxies().catch(() => {});
            toast(result.notes && result.notes.length ? result.notes.join(" ") : "Settings saved.", { kind: "success", title: "Saved" });
            ctx.settingsBuilt = false;
            ctx.settingsDirty = () => false;
            renderSettings(state.profiles.get(id));
          } catch (err) {
            toast(err.message, { kind: "error", title: "Could not save" });
          }
        });
      },
    },
      h("div.form-section", h("h3", "General"),
        field("Name", name), field("Tags", tags), field("Notes", notes, { hint: "Your AI can read these notes." })),
      h("div.form-section", h("h3", "Connection"),
        h("div.field", h("span.field-label", "Proxy"), proxy.el, h("div.hint", "Changing the proxy of a running profile switches it live for new connections."))),
      h("div.form-section", h("h3", "Browser"),
        h("div.field-row", field("Browser", browser), field("Identity for autofill", identity)),
        h("div.field", h("span.field-label", "Window"), win, h("div.hint", "Normal is the most natural. Window and browser changes apply at the next start.")),
        h("div.field-row", field("Language", lang), field("Time zone", tz, { hint: TIMEZONE_HINT })),
        field("Start page", startUrl), restore),
      h("details.form-section.advanced",
        h("summary", icon("chevron-right"), "Advanced"),
        h("div.form", { style: { marginTop: "10px" } },
          field("WebRTC", webrtc, { hint: "Keeps video-call and peer-to-peer connections from revealing this computer's real IP address." }))),
      h("div.form-section", h("h3", "Manage"),
        h("div.row.wrap",
          h("button.btn", { attrs: { type: "button" }, onclick: () => cloneDialog(state.profiles.get(id)) }, icon("clone"), "Clone…"),
          h("button.btn.danger", { attrs: { type: "button" }, onclick: async () => { if (await confirmDelete(state.profiles.get(id))) drawer.forceClose(); } }, icon("trash"), "Delete…"))),
      timezoneList(),
    );
    for (const type of ["input", "change", "click", "keyup"]) form.addEventListener(type, () => setTimeout(sync));
    replace(panes.settings, form);
    replace(drawer.footer, unsaved, h("span.spacer"), discard, save);
    sync();
  }

  const unsub = subscribe((topics) => {
    const p = state.profiles.get(id);
    if (!p) {
      if (topics.has("profiles")) drawer.forceClose();
      return;
    }
    if (topics.has("profiles") || topics.has("proxies") || topics.has("identities")) {
      header(p);
      if (ctx.tab === "overview") renderOverview(p);
      if (ctx.tab === "cookies") cookies.update(p);
    }
    if (topics.has("activity")) { refreshActivity(); refreshLastActionSoon(); }
  });

  current = ctx;
  header(p0);
  show(tab);
  drawer.focusInitial();
}

function hueOf(p) {
  let hash = 0;
  for (const ch of String(p.id)) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  return hash % 360;
}

function checkCallout(result) {
  const c = result.check || {};
  if (c.ok) {
    return h("div.callout", icon("check"), h("div.stack", { style: { gap: "2px" } },
      h("strong", `Websites see IP address ${c.ip}`),
      h("span", [c.country, c.city, c.isp].filter(Boolean).join(" · ") || "Location unknown"),
      h("span.faint", `${result.route} · ${fmt.ms(c.latency_ms)}${c.provider ? ` via ${c.provider}` : ""}`)));
  }
  return h("div.callout.warn", icon("alert"), h("div.stack", { style: { gap: "4px" } },
    h("strong", "Couldn't reach the internet through this proxy"),
    h("span", c.reason || "No answer."),
    c.error ? h("details.raw-details", h("summary", "Details"), h("code.raw", c.error)) : null,
    result.route ? h("span.faint", result.route) : null));
}
