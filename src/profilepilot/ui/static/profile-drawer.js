// The profile details drawer: Overview, Tabs, Activity, Settings.

import { api, enc } from "./api.js";
import { avatar, clear, debounce, fmt, h, icon, replace } from "./dom.js";
import { renderFeed } from "./feed.js";
import { BROWSER_LABELS, WINDOW_CHOICES, WINDOW_LABELS, browserOptions, cloneDialog, confirmDelete, identityOptions, proxyPicker } from "./profile-dialogs.js";
import { actions, displayStatus, loadProxies, state, subscribe, upsertProfile } from "./store.js";
import { attachThumb } from "./thumbs.js";
import { busy, chipInput, closeButton, copyable, countryBadge, emptyState, field, openDrawer, segmented, select, statusDot, toast, toggle } from "./ui.js";

let current = null;

export function closeProfileDrawer() {
  if (current) current.drawer.close();
}

export function openProfileDrawer(id, tab = "overview") {
  if (current && current.id === id) {
    current.show(tab);
    return;
  }
  closeProfileDrawer();
  const p0 = state.profiles.get(id);
  if (!p0) return;
  const ctx = { id, tab, check: null, settingsBuilt: false };
  const drawer = openDrawer({
    label: `Profile ${p0.name}`,
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

  const panes = { overview: h("div.stack"), tabs: h("div.stack"), activity: h("div.stack"), settings: h("div.stack") };
  const tabButtons = {};
  const tabDefs = [["overview", "Overview", "info"], ["tabs", "Tabs", "tab"], ["activity", "Activity", "activity"], ["settings", "Settings", "settings"]];
  for (const [key, label, iconName] of tabDefs) {
    const btn = h("button", { attrs: { role: "tab", type: "button", "aria-selected": "false" }, onclick: () => show(key) }, icon(iconName), label);
    tabButtons[key] = btn;
    drawer.tabs.append(btn);
  }

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
        : p.window === "headless" ? [icon("eye"), "Headless: no window"] : [h("span.spinner.sm"), "Loading preview…"])];
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
    const bits = [statusDot(st.key), h("span", st.label)];
    if (p.state === "running" && p.runtime) bits.push(h("span.faint", `· for ${fmt.since(p.runtime.started_at)}`));
    replace(statusLine, ...bits);
  }

  function show(key) {
    ctx.tab = key;
    for (const [k, btn] of Object.entries(tabButtons)) btn.setAttribute("aria-selected", String(k === key));
    replace(drawer.body, panes[key]);
    const p = state.profiles.get(id);
    if (!p) return;
    if (key === "overview") renderOverview(p);
    if (key === "tabs") renderTabs(p);
    if (key === "activity") renderActivity(p);
    if (key === "settings" && !ctx.settingsBuilt) renderSettings(p);
  }
  ctx.show = show;

  // ------------------------------------------------------------ overview

  function renderOverview(p) {
    const st = displayStatus(p);
    const items = [];
    const control = p.control || {};
    for (const req of control.help || []) {
      items.push(h("div.callout.warn",
        icon("robot"),
        h("div.stack.grow", { style: { gap: "6px" } },
          h("strong", `${requestKind(req.kind)} requested ${fmt.ago(req.created_at)}`),
          h("span", req.message),
          h("div.row", { style: { marginTop: "4px" } },
            p.state === "running" ? h("button.btn.sm", { onclick: () => actions.focus(id).catch(() => {}) }, icon("focus"), "Show window") : null,
            h("button.btn.sm.primary", { onclick: (e) => busy(e.currentTarget, () => actions.resolveHelp(id, req.id, "done").catch(() => {})) }, icon("check"), "Done, hand back"),
            h("button.btn.sm.ghost", { onclick: (e) => busy(e.currentTarget, () => actions.resolveHelp(id, req.id, "dismissed").catch(() => {})) }, "Dismiss")))));
    }
    if (control.user_pause) {
      items.push(h("div.callout", icon("hand"),
        h("div.stack.grow", { style: { gap: "6px" } },
          h("strong", "You're in control"),
          h("span", `The AI won't act on this profile until you hand it back${control.user_pause.since ? ` (since ${fmt.clock(control.user_pause.since)})` : ""}.`),
          h("div.row", h("button.btn.sm.primary", { onclick: (e) => busy(e.currentTarget, () => actions.handBack(id).catch(() => {})) }, icon("sparkles"), "Hand back to AI")))));
    }
    if (p.state === "crashed") {
      items.push(h("div.callout.warn", icon("alert"), h("span", `The browser crashed (${p.crash || "unknown cause"}). Start it again; if it keeps crashing, check host.log in the profile folder.`)));
    }
    if (p.runtime && p.runtime.client_job) {
      items.push(h("div.callout.warn", icon("info"), h("span", "This browser was started by an AI client that closes it when the client disconnects. Start it from here to keep it running.")));
    }
    items.push(bigThumb);
    renderBigState();
    if (p.state === "running" && ctx.thumbState === "live") bigState.classList.add("hidden");
    else bigState.classList.remove("hidden");
    if (p.state !== "running") { bigImg.classList.remove("loaded"); }

    const running = p.state === "running";
    const pending = state.pending.get(id);
    const bar = h("div.action-bar");
    if (pending) bar.append(h("button.btn", { disabled: true }, h("span.spinner"), pending === "stopping" ? "Stopping…" : "Starting…"));
    else if (running || p.state === "starting") bar.append(h("button.btn", { onclick: () => actions.stop(id).catch(() => {}) }, icon("stop"), "Stop"));
    else bar.append(h("button.btn.primary", { onclick: () => actions.start(id).catch(() => {}) }, icon("play"), "Start"));
    if (running && p.window !== "headless") bar.append(h("button.btn", { onclick: (e) => busy(e.currentTarget, () => actions.focus(id).catch(() => {})) }, icon("focus"), "Show window"));
    if (control.paused) bar.append(h("button.btn", { class: control.help && control.help.length ? "" : "primary", onclick: (e) => busy(e.currentTarget, () => actions.handBack(id).catch(() => {})) }, icon("sparkles"), "Hand back to AI"));
    else bar.append(h("button.btn", { onclick: (e) => busy(e.currentTarget, () => actions.takeControl(id).catch(() => {})) }, icon("hand"), "Take control"));
    bar.append(h("button.btn", { onclick: (e) => busy(e.currentTarget, () => checkRoute(p)) }, icon("zap"), "Test exit IP"));
    items.push(bar);
    items.push(openPageForm(p));

    if (ctx.check) items.push(checkCallout(ctx.check));

    const kv = h("dl.kv");
    const row = (k, ...v) => kv.append(h("dt", k), h("dd", ...v));
    row("Status", statusDot(st.key), st.label, p.state === "running" && p.runtime ? h("span.faint", `· ${fmt.since(p.runtime.started_at)}`) : null);
    row("Window", WINDOW_LABELS[(p.runtime && p.runtime.window) || p.window] || p.window);
    row("Browser", (p.runtime && p.runtime.browser_version) ? `${BROWSER_LABELS[p.browser] || p.browser} · ${p.runtime.browser_version}` : (BROWSER_LABELS[p.browser] || p.browser));
    row("Proxy", ...proxyLine(p));
    row("Identity", p.identity ? h("span", p.identity.name, p.identity.missing ? h("span.badge.amber", "missing") : null) : h("span.faint", "None"));
    row("Last AI action", lastAction);
    if (running && p.runtime && p.runtime.cdp_http_url) row("DevTools", copyable(p.runtime.cdp_http_url));
    row("Data folder", copyable(p.path), h("button.btn.xs.ghost", { onclick: () => api.post("/api/reveal", { what: "profile", id }).catch((e) => toast(e.message, { kind: "error" })) }, icon("folder"), "Open"));
    row("Created", fmt.dateTime(p.created_at));
    row("Last started", p.last_started_at ? fmt.ago(p.last_started_at) : h("span.faint", "Never"));
    row("Total runtime", fmt.duration(p.total_runtime_s));
    if (p.tags.length) row("Tags", ...p.tags.map((t) => h("span.tag", t)));
    if (p.notes) row("Notes", h("span", { style: { whiteSpace: "pre-wrap" } }, p.notes));
    items.push(h("div.card.card-pad", kv));
    replace(panes.overview, ...items);
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
    const parts = [h("span", p.proxy.name)];
    if (p.proxy.missing) parts.push(h("span.badge.amber", "missing"));
    else {
      parts.push(h("code", `${p.proxy.scheme}://${p.proxy.host}:${p.proxy.port}`));
      if (p.proxy.country_code) parts.push(countryBadge(p.proxy.country_code));
      if (p.proxy.ok === false) parts.push(h("span.badge.red", "last test failed"));
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
      ctx.check = { route: "", check: { ok: false, error: err.message } };
    }
    if (ctx.tab === "overview") renderOverview(state.profiles.get(id));
  }

  const lastAction = h("span.faint", "…");
  async function refreshLastAction() {
    try {
      const events = (await api.get(`/api/activity?profile=${enc(id)}&limit=20`)).events;
      const e = events.find((x) => x.source !== "ui" && x.source !== "cli");
      replace(lastAction, e ? [h("code", e.tool), " ", h("span.muted", { attrs: { title: e.summary || "" } }, `${(e.summary || "").slice(0, 80)}`),
        h("span.faint", ` · ${fmt.ago(e.ts)}`)] : h("span.faint", "None yet"));
      lastAction.className = "";
    } catch (err) {
      replace(lastAction, "");
    }
  }
  refreshLastAction();

  // ------------------------------------------------------------ tabs

  async function renderTabs(p) {
    if (p.state !== "running") {
      replace(panes.tabs, emptyState({ icon: "tab", title: "Not running", text: "Start the profile to see its open tabs.",
        actions: [h("button.btn.primary", { onclick: () => actions.start(id).then(() => renderTabs(state.profiles.get(id))).catch(() => {}) }, icon("play"), "Start")] }));
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
        h("button.btn.xs", { onclick: (e) => busy(e.currentTarget, () => api.post(`/api/profiles/${enc(id)}/tabs/${enc(t.id)}/activate`).then(() => renderTabs(state.profiles.get(id))).catch((err) => toast(err.message, { kind: "error" }))) }, "Show"),
        tabs.length > 1 ? h("button.btn.xs.ghost.icon-only", { attrs: { "aria-label": "Close tab", title: "Close tab" }, onclick: (e) => busy(e.currentTarget, () => api.post(`/api/profiles/${enc(id)}/tabs/${enc(t.id)}/close`).then(() => renderTabs(state.profiles.get(id))).catch((err) => toast(err.message, { kind: "error" }))) }, icon("x")) : null,
      ));
    }
    replace(panes.tabs, 
      h("div.row", h("span.muted.small", `${tabs.length} open ${tabs.length === 1 ? "tab" : "tabs"} · most recently used first`), h("span.spacer"),
        h("button.btn.sm.ghost", { onclick: () => renderTabs(state.profiles.get(id)) }, icon("refresh"), "Refresh")),
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
    const tags = chipInput(p.tags);
    const notes = h("textarea.textarea", { value: p.notes || "", attrs: { rows: "3" } });
    const proxy = proxyPicker(p.proxy_id || "");
    const identity = select(identityOptions(), p.identity_id || "");
    const browser = select(browserOptions(), p.browser || "auto");
    const win = segmented(WINDOW_CHOICES.map((c) => ({ value: c.value, label: c.title })), p.launch.window, null, { label: "Window" });
    const lang = h("input.input", { value: p.launch.lang || "", attrs: { placeholder: "System language" } });
    const tz = h("input.input", { value: p.launch.timezone || "", attrs: { placeholder: "This computer's timezone", list: "pp-timezones" } });
    const startUrl = h("input.input", { value: p.launch.start_url || "", attrs: { placeholder: "https://…" } });
    const restore = toggle(p.launch.restore_session !== false, { label: "Reopen tabs and keep session cookies" });
    const webrtc = select([{ value: "auto", label: "Automatic (proxy only when proxied)" }, { value: "proxy_only", label: "Proxy only (never leak the real IP)" }, { value: "default", label: "Chrome default" }], p.launch.webrtc || "auto");
    const save = h("button.btn.primary", { attrs: { type: "submit" } }, "Save changes");
    const form = h("form.form", {
      onsubmit: (event) => {
        event.preventDefault();
        busy(save, async () => {
          const payload = {
            name: name.value.trim(), tags: tags.values, notes: notes.value, identity_id: identity.value || "",
            browser: browser.value, ...proxy.value(),
            launch: { window: win.value, lang: lang.value.trim() || null, timezone: tz.value.trim() || null, start_url: startUrl.value.trim() || null,
              restore_session: restore.checked, webrtc: webrtc.value },
          };
          try {
            const result = await api.patch(`/api/profiles/${enc(id)}`, payload);
            proxy.clear();
            upsertProfile(result.profile);
            if (payload.proxy_url) loadProxies().catch(() => {});
            toast(result.notes && result.notes.length ? result.notes.join(" ") : "Settings saved.", { kind: "success", title: "Saved" });
            ctx.settingsBuilt = false;
            renderSettings(state.profiles.get(id));
          } catch (err) {
            toast(err.message, { kind: "error", title: "Could not save" });
          }
        });
      },
    },
      h("div.form-section", h("h3", "General"),
        field("Name", name), field("Tags", tags), field("Notes", notes, { hint: "Visible to the AI in profile_list." })),
      h("div.form-section", h("h3", "Connection"),
        h("div.field", h("span.field-label", "Proxy"), proxy.el, h("div.hint", "Changing the proxy of a running profile switches it live for new connections.")),
        field("WebRTC", webrtc)),
      h("div.form-section", h("h3", "Browser"),
        h("div.field-row", field("Browser", browser), field("Identity for autofill", identity)),
        h("div.field", h("span.field-label", "Window"), win, h("div.hint", "Normal is the most natural. Window and browser changes apply at the next start.")),
        h("div.field-row", field("Language", lang), field("Timezone", tz)),
        field("Start page", startUrl), restore),
      h("div.row", h("span.spacer"), save),
      h("div.form-section", h("h3", "Manage"),
        h("div.row.wrap",
          h("button.btn", { attrs: { type: "button" }, onclick: () => cloneDialog(state.profiles.get(id)) }, icon("clone"), "Clone…"),
          h("button.btn.danger", { attrs: { type: "button" }, onclick: async () => { if (await confirmDelete(state.profiles.get(id))) drawer.close(); } }, icon("trash"), "Delete…"))),
    );
    replace(panes.settings, form);
  }

  const unsub = subscribe((topics) => {
    const p = state.profiles.get(id);
    if (!p) {
      if (topics.has("profiles")) drawer.close();
      return;
    }
    if (topics.has("profiles") || topics.has("proxies") || topics.has("identities")) {
      header(p);
      if (ctx.tab === "overview") renderOverview(p);
    }
    if (topics.has("activity")) { refreshActivity(); refreshLastActionSoon(); }
  });

  current = ctx;
  header(p0);
  show(tab);
}

function hueOf(p) {
  let hash = 0;
  for (const ch of String(p.id)) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  return hash % 360;
}

export function requestKind(kind) {
  return { captcha: "CAPTCHA", login: "Login", verification: "Verification code", payment: "Payment step", other: "Help" }[kind] || "Help";
}

function checkCallout(result) {
  const c = result.check || {};
  if (c.ok) {
    return h("div.callout", icon("check"), h("div.stack", { style: { gap: "2px" } },
      h("strong", `Exit IP ${c.ip}`),
      h("span", [c.country, c.city, c.isp].filter(Boolean).join(" · ") || "Location unknown"),
      h("span.faint", `${result.route} · ${fmt.ms(c.latency_ms)}${c.provider ? ` via ${c.provider}` : ""}`)));
  }
  return h("div.callout.warn", icon("alert"), h("div.stack", { style: { gap: "2px" } },
    h("strong", "The route check failed"), h("span", c.error || "No answer"), result.route ? h("span.faint", result.route) : null));
}
