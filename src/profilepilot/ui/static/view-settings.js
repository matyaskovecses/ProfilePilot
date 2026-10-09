// Settings: defaults, browsers, data folder, ShardX, appearance, notifications and the trash.

import { api, enc } from "./api.js";
import { fmt, h, icon, replace } from "./dom.js";
import { AUTO_BROWSER_LABEL, WINDOW_CHOICES } from "./profile-dialogs.js";
import { loadProfiles, loadSettings, loadTrash, secretPlace, secretStoreLabel, state } from "./store.js";
import { busy, confirmDialog, copyable, segmented, select, toast, toggle } from "./ui.js";
import { applyTheme, currentTheme } from "./theme-switch.js";

export function createSettingsView() {
  const header = h("header.view-header", h("div.view-title", h("h1", "Settings"), h("p", "Defaults for new profiles, browsers, integrations and the trash.")));
  const body = h("div.view-body");
  const el = h("section.view", { attrs: { "aria-label": "Settings" } }, header, body);
  let trashRequested = false;

  async function patch(data, message = "Settings saved.") {
    try {
      state.settings = await api.patch("/api/settings", data);
      toast(message, { kind: "success" });
      render();
    } catch (err) {
      toast(err.message, { kind: "error", title: "Could not save" });
      render();
    }
  }

  function setting(title, description, control, { stacked = false, id } = {}) {
    return h("div.setting", { class: stacked ? "stacked" : "", id },
      h("div.s-text", h("strong", title), description ? h("p", description) : null),
      h("div.s-control", control));
  }

  function group(title, iconName, ...rows) {
    return h("div.card", h("div.card-header", icon(iconName), h("h2", title)), h("div.card-body", { style: { paddingTop: "4px", paddingBottom: "4px" } }, rows));
  }

  function render() {
    const s = state.settings;
    if (!s) {
      replace(body, h("div.settings-grid", Array.from({ length: 4 }, () => h("div.card.skeleton", { style: { height: "160px" } }))));
      return;
    }
    // general
    const windowSeg = segmented(WINDOW_CHOICES.map((c) => ({ value: c.value, label: c.title, title: c.desc })), s.default_window, (v) => patch({ default_window: v }), { label: "Default window" });
    const maxInput = h("input.input.sm", { type: "number", value: String(s.max_running), attrs: { min: "0", max: "500", "aria-label": "Maximum running profiles" }, style: { width: "96px" } });
    maxInput.addEventListener("change", () => {
      const n = Number(maxInput.value);
      if (Number.isInteger(n) && n >= 0 && n <= 500) patch({ max_running: n });
      else { toast("Use a whole number from 0 (no limit) to 500.", { kind: "warn" }); maxInput.value = String(s.max_running); }
    });
    // browsers
    const browserRows = state.browsers.length
      ? state.browsers.map((b) => h("div.list-item", icon("tab"), h("div.grow", h("strong", b.label), h("span.small.faint.mono.ellipsis", { attrs: { title: b.path } }, b.path)),
        b.version ? h("span.badge", b.version) : null, s.browser_path === b.path ? h("span.badge.violet", "Default") : null))
      : [h("div.list-item", icon("alert"), h("span", "No Chromium-family browser was found. Install Google Chrome."))];
    const browserSelect = select([{ value: "", label: AUTO_BROWSER_LABEL }, ...state.browsers.map((b) => ({ value: b.path, label: b.label }))], s.browser_path || "", {
      onChange: (v) => patch({ browser_path: v || null }),
    });
    browserSelect.setAttribute("aria-label", "Default browser");
    // shardx
    const sx = s.shardx || {};
    const sxToggle = toggle(sx.enabled, { onChange: (v) => patch({ shardx: { enabled: v } }, v ? "ShardX profiles are available to the AI after the next server start." : "ShardX integration turned off.") });
    sxToggle.input.setAttribute("aria-label", "Use ShardX profiles");
    const sxUrl = h("input.input.sm.mono", { value: sx.base_url || "", disabled: !sx.enabled, attrs: { "aria-label": "ShardX launcher URL" } });
    sxUrl.addEventListener("change", () => patch({ shardx: { base_url: sxUrl.value.trim() } }));
    const sxToken = h("input.input.sm.mono", { type: "password", disabled: !sx.enabled, attrs: { autocomplete: "off", placeholder: sx.token_set ? "Saved; paste to replace" : "Paste the API token", "aria-label": "ShardX API token (write-only)" } });
    const sxSave = h("button.btn.sm", { disabled: !sx.enabled, attrs: { type: "submit" } }, sx.token_set ? "Replace" : "Save");
    const sxForm = h("form.row", { onsubmit: (event) => {
      event.preventDefault();
      if (!sxToken.value.trim()) return;
      busy(sxSave, async () => {
        try {
          state.settings = await api.put("/api/settings/shardx-token", { token: sxToken.value.trim() });
          sxToken.value = "";
          toast(`ShardX token saved in ${secretPlace()}.`, { kind: "success" });
          render();
        } catch (err) { sxToken.value = ""; toast(err.message, { kind: "error" }); }
      });
    } }, sxToken, sxSave, sx.token_set ? h("button.btn.sm.ghost", { disabled: !sx.enabled, attrs: { type: "button" }, onclick: async () => {
      try { state.settings = await api.del("/api/settings/shardx-token"); render(); toast("ShardX token removed.", { kind: "success" }); }
      catch (err) { toast(err.message, { kind: "error" }); }
    } }, "Remove") : null);
    // appearance
    const themeSeg = segmented([{ value: "system", label: "System", icon: "monitor" }, { value: "light", label: "Light", icon: "sun" }, { value: "dark", label: "Dark", icon: "moon" }],
      currentTheme(), (v) => applyTheme(v), { label: "Theme" });
    const notifyState = typeof Notification === "undefined" ? "unsupported" : Notification.permission;
    const notifyControl = notifyState === "granted" ? h("span.badge.green", icon("check"), "On")
      : notifyState === "denied" ? h("span.badge.amber", "Blocked in the browser")
        : notifyState === "unsupported" ? h("span.badge", "Not supported")
          : h("button.btn.sm", { attrs: { type: "button" }, onclick: async () => { await Notification.requestPermission(); render(); } }, icon("bell"), "Turn on");
    // form autofill
    const browserFill = toggle(s.autofill_from_browser !== false, {
      onChange: (v) => patch({ autofill_from_browser: v }, v ? "Forms of profiles without an identity are filled from your browser's saved addresses."
        : "Profiles without an identity no longer use your browser's saved addresses."),
    });
    browserFill.input.setAttribute("aria-label", "Use addresses saved in your browser");
    // advanced
    const escapeToggle = toggle(s.escape_client_job, { onChange: (v) => patch({ escape_client_job: v }) });
    escapeToggle.input.setAttribute("aria-label", "Keep browsers open when an AI app quits");

    replace(body, h("div.settings-grid",
      group("New profiles", "profiles",
        setting("Default window", "Normal windows are the most natural for websites. Off-screen and headless run in the background.", windowSeg),
        setting("Running at once", "The most profiles that may run at the same time (0 = no limit).", maxInput)),
      group("Browsers", "tab",
        setting("Default browser", "Profiles set to Automatic use this one.", browserSelect),
        h("div.list", { style: { margin: "4px 0 14px" } }, browserRows)),
      group("Data", "folder",
        setting("Data folder", `Profiles, proxies and identities live here. Passwords and card details are kept in ${secretPlace()}.`,
          h("div.row", copyable(s.data_root), h("button.btn.sm", { attrs: { type: "button" }, onclick: () => api.post("/api/reveal", { what: "data" }).catch((e) => toast(e.message, { kind: "error" })) }, icon("folder"), "Open")), { stacked: true }),
        setting("Secret storage", "Where proxy passwords, card details and tokens are kept.", h("span.badge.violet", secretStoreLabel()))),
      group("Form autofill", "identities",
        setting("Use addresses saved in your browser",
          "When a profile has no identity, the AI fills forms with the name, email, phone and address saved in that profile's own browser, or else in your active Chrome, Edge or Brave profile. Cards, passwords and ID numbers are never read. You can also connect an identity to a saved address on the Identities page.",
          browserFill)),
      group("Appearance", "sun",
        setting("Theme", null, themeSeg),
        setting("Desktop notifications", "Get a notification when the AI needs you while this window is in the background.", notifyControl)),
      group("ShardX", "link",
        setting("Use ShardX profiles", "Let the AI use profiles from the ShardX launcher as shardx:<name>.", sxToggle),
        h("div.setting-group", { class: sx.enabled ? "" : "disabled", attrs: { "aria-disabled": String(!sx.enabled) } },
          setting("Launcher address", sx.enabled ? "Must be on this computer." : "Turn on ShardX profiles to change this.", sxUrl),
          setting("API token", "From ShardX → Settings → Automation API. Write-only: it is never shown again.", sxForm))),
      group("Advanced", "settings",
        setting("Keep browsers open when an AI app quits", "Windows only. Some AI apps close their tools' processes when they disconnect. On: ProfilePilot restarts its browser host outside that app, so browsers keep running.", escapeToggle)),
      trashCard(),
    ));
  }

  function trashCard() {
    const items = state.trash;
    const head = h("div.card-header", icon("trash"), h("h2", "Trash"), h("span.spacer"),
      items && items.length ? h("button.btn.sm.danger", { attrs: { type: "button" }, onclick: async () => {
        const yes = await confirmDialog({ title: "Empty the trash?", message: `${fmt.plural(items.length, "profile")} and their cookies, logins and history will be deleted permanently. This cannot be undone.`, confirmLabel: "Delete permanently", danger: true });
        if (!yes) return;
        try { const r = await api.del("/api/trash"); toast(`Deleted ${fmt.plural(r.removed, "profile")} permanently.`, { kind: "success" }); await loadTrash(); }
        catch (err) { toast(err.message, { kind: "error" }); }
      } }, "Empty trash") : null);
    let content;
    if (!items) content = h("div.card-body", h("div.skeleton.skeleton-line"));
    else if (!items.length) content = h("div.card-body.trash-empty", icon("check"), h("span", "The trash is empty. Deleted profiles wait here until you empty it, so you can restore them."));
    else {
      content = h("div.card-body", h("div.list", items.map((t) => h("div.list-item",
        icon("profiles"),
        h("div.grow", h("strong", t.name), h("span.small.faint", `Deleted ${fmt.ago(t.deleted_at)} · ${fmt.bytes(t.size_bytes)}`)),
        h("button.btn.sm", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, async () => {
          try { await api.post(`/api/trash/${enc(t.trash_id)}/restore`); toast(`Restored "${t.name}".`, { kind: "success" }); await Promise.all([loadTrash(), loadProfiles()]); }
          catch (err) { toast(err.message, { kind: "error" }); }
        }) }, icon("refresh"), "Restore")))));
    }
    return h("div.card", head, content);
  }

  return {
    el,
    title: "Settings",
    onShow() {
      render();
      if (!trashRequested) { trashRequested = true; loadTrash().catch(() => {}); }
      loadSettings().catch(() => {});
    },
    update(topics) {
      if (topics.has("settings") || topics.has("trash") || topics.has("ready")) render();
    },
  };
}
