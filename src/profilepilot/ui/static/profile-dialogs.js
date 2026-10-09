// "New profile", "Clone profile" and delete confirmation.

import { api, enc } from "./api.js";
import { clear, h, icon, replace } from "./dom.js";
import { actions, loadProxies, state, upsertProfile } from "./store.js";
import { busy, chipInput, choiceCards, confirmDialog, field, openDialog, segmented, select, toast } from "./ui.js";

export const BROWSER_LABELS = { auto: "Automatic", chrome: "Google Chrome", edge: "Microsoft Edge", brave: "Brave", chromium: "Chromium" };
export const WINDOW_LABELS = { normal: "Normal window", offscreen: "Off-screen window", headless: "Headless" };

export const WINDOW_CHOICES = [
  { value: "normal", title: "Normal", icon: "monitor", badge: "Recommended", desc: "A regular window, exactly like a person using Chrome. Best for logins and sites that check for bots." },
  { value: "offscreen", title: "Off-screen", icon: "tab", desc: "A real window placed outside the screen. Runs in the background and stays native." },
  { value: "headless", title: "Headless", icon: "eye", desc: "No window at all. Sites can detect it, so use it only for simple scraping." },
];

export function proxyOptions(includeNone = true) {
  const opts = includeNone ? [{ value: "", label: "No proxy (direct connection)" }] : [];
  for (const p of state.proxies) {
    const check = p.last_check;
    const where = check && check.ok ? ` · ${[check.country_code, check.city].filter(Boolean).join(" ")}` : "";
    opts.push({ value: p.id, label: `${p.name} — ${p.scheme}://${p.host}:${p.port}${where}` });
  }
  return opts;
}

export function identityOptions() {
  return [{ value: "", label: "None" }, ...state.identities.map((i) => ({ value: i.id, label: i.name }))];
}

export function browserOptions() {
  const opts = [{ value: "auto", label: "Automatic (Chrome, then Edge)" }];
  for (const b of state.browsers) opts.push({ value: b.kind, label: `${b.label}${b.version ? ` ${b.version.split(".")[0]}` : ""}` });
  return opts;
}

/** Proxy picker: none / saved / paste new. Returns {el, value()} with value() -> {proxy_id} | {proxy_url, proxy_scheme} | {proxy_id: ""} */
export function proxyPicker(currentId = "") {
  const mode0 = currentId ? "saved" : (state.proxies.length ? "none" : "none");
  const savedSelect = select(proxyOptions(false), currentId || (state.proxies[0] && state.proxies[0].id) || "", {});
  const pasteInput = h("input.input.mono", { attrs: { placeholder: "socks5://user:pass@host:port  or  host:port:user:pass", autocomplete: "off", spellcheck: "false" } });
  const schemeSelect = select(["http", "https", "socks5", "socks4"].map((s) => ({ value: s, label: s.toUpperCase() })), "http", { cls: "sm" });
  const savedRow = h("div", savedSelect);
  const pasteRow = h("div.stack",
    h("div.row", pasteInput, h("div", { style: { width: "110px", flex: "none" } }, schemeSelect)),
    h("div.hint.small.faint", "The type on the right is used when the line has no scheme. The password is stored in your OS keychain and never shown again."));
  const seg = segmented([
    { value: "none", label: "No proxy" },
    { value: "saved", label: `Saved${state.proxies.length ? ` (${state.proxies.length})` : ""}` },
    { value: "new", label: "Paste new" },
  ], mode0, (v) => show(v), { label: "Proxy" });
  if (!state.proxies.length) seg.querySelectorAll("button")[1].disabled = true;
  const body = h("div.stack");
  const show = (mode) => {
    replace(body, mode === "saved" ? savedRow : mode === "new" ? pasteRow : h("div.hint.small.faint",
      "The browser connects directly from this computer's own IP address."));
  };
  show(mode0);
  const el = h("div.stack", seg, body);
  return {
    el,
    value() {
      if (seg.value === "saved") return { proxy_id: savedSelect.value };
      if (seg.value === "new") {
        const text = pasteInput.value.trim();
        if (!text) return { proxy_id: "" };
        return { proxy_url: text, proxy_scheme: schemeSelect.value };
      }
      return { proxy_id: "" };
    },
    clear() { pasteInput.value = ""; },
  };
}

export function newProfileDialog({ onCreated, proxyId = "", name: suggested = "" } = {}) {
  const name = h("input.input", { value: suggested, attrs: { placeholder: "e.g. shop-us, mail-de, research", autocomplete: "off", maxlength: "64", autofocus: true } });
  const proxy = proxyPicker(proxyId);
  const identity = select(identityOptions(), "", {});
  const browser = select(browserOptions(), "auto", {});
  const windowMode = choiceCards(WINDOW_CHOICES, (state.settings && state.settings.default_window) || "normal", null, { label: "Window" });
  const tags = chipInput([], { placeholder: "Add tags, e.g. shop, us" });
  const notes = h("textarea.textarea", { attrs: { rows: "2", placeholder: "Anything worth remembering about this profile (visible to the AI)" } });
  const lang = h("input.input", { attrs: { placeholder: "e.g. de-DE (default: your system language)", autocomplete: "off" } });
  const tz = h("input.input", { attrs: { placeholder: "e.g. Europe/Berlin (default: this computer's)", autocomplete: "off", list: "pp-timezones" } });
  const startUrl = h("input.input", { attrs: { placeholder: "https://…", autocomplete: "off", type: "url" } });
  const startNow = h("input", { type: "checkbox", checked: true });
  const error = h("div.callout.warn.hidden", { attrs: { role: "alert" } });

  const body = h("form.form", { onsubmit: (event) => { event.preventDefault(); create.click(); } },
    field("Name", name, { hint: "A short name the AI can refer to. Each profile has its own cookies, logins and history." }),
    h("div.field", h("span.field-label", "Proxy"), proxy.el),
    h("div.field-row",
      field("Identity for autofill", identity, { hint: "Your saved details for forms." }),
      field("Browser", browser)),
    h("div.field", h("span.field-label", "Window"), windowMode),
    field("Tags", tags),
    field("Notes", notes),
    h("details.stack",
      h("summary.small.muted", { style: { cursor: "pointer" } }, "Advanced: language, timezone, start page"),
      h("div.form", { style: { marginTop: "12px" } },
        h("div.field-row", field("Language", lang), field("Timezone", tz, { hint: "An override is a spoof: leave empty unless the proxy's country needs it." })),
        field("Start page", startUrl))),
    error,
    timezoneList(),
  );

  const create = h("button.btn.primary", { attrs: { type: "button" } }, "Create profile");
  const dlg = openDialog({
    title: "New profile", description: "A separate, real Chrome identity with its own cookies, storage and proxy.",
    body, size: "wide",
    footer: [h("label.checkbox", startNow, h("span", "Start it now")), h("span.spacer"),
      h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.close() }, "Cancel"), create],
  });
  create.addEventListener("click", () => busy(create, async () => {
    error.classList.add("hidden");
    if (!name.value.trim()) {
      name.setAttribute("aria-invalid", "true");
      name.focus();
      return;
    }
    const payload = {
      name: name.value.trim(), ...proxy.value(), identity_id: identity.value || null, browser: browser.value,
      window: windowMode.value, tags: tags.values, notes: notes.value,
      lang: lang.value.trim() || null, timezone: tz.value.trim() || null, start_url: startUrl.value.trim() || null,
    };
    try {
      const view = await api.post("/api/profiles", payload);
      proxy.clear();
      upsertProfile(view);
      if (payload.proxy_url) loadProxies().catch(() => {});
      dlg.close();
      toast(`"${view.name}" is ready.`, { kind: "success", title: "Profile created" });
      if (onCreated) onCreated(view);
      if (startNow.checked) actions.start(view.id).catch(() => {});
    } catch (err) {
      replace(error, icon("alert"), h("span", err.message));
      error.classList.remove("hidden");
    }
  }));
  return dlg;
}

export function cloneDialog(p) {
  const name = h("input.input", { value: `${p.name} copy`, attrs: { maxlength: "64", autofocus: true } });
  const copyData = h("input", { type: "checkbox" });
  const running = p.state !== "stopped" && p.state !== "crashed";
  const go = h("button.btn.primary", { attrs: { type: "button" } }, "Clone profile");
  const dlg = openDialog({
    title: `Clone "${p.name}"`, size: "narrow",
    body: h("div.form",
      field("Name of the copy", name),
      h("label.checkbox", copyData, h("span.stack", { style: { gap: "2px" } }, h("span", "Also copy browser data"),
        h("span.small.faint", running ? "Stop the profile first to copy its cookies, logins and history."
          : "Cookies, logins and history. Without it the copy starts fresh with the same settings.")))),
    footer: [h("span.spacer"), h("button.btn", { onclick: () => dlg.close() }, "Cancel"), go],
  });
  if (running) copyData.disabled = true;
  go.addEventListener("click", () => busy(go, async () => {
    try {
      const view = await api.post(`/api/profiles/${enc(p.id)}/clone`, { name: name.value.trim(), copy_data: copyData.checked });
      upsertProfile(view);
      dlg.close();
      toast(`Created "${view.name}".`, { kind: "success", title: "Profile cloned" });
    } catch (err) {
      toast(err.message, { kind: "error", title: "Could not clone" });
    }
  }));
}

export async function confirmDelete(p) {
  if (p.state === "running" || p.state === "starting") {
    toast("Stop the profile before you delete it.", { kind: "warn", title: `"${p.name}" is running` });
    return false;
  }
  const yes = await confirmDialog({
    title: `Delete "${p.name}"?`,
    message: "The profile and its cookies, logins and history move to the trash. You can restore it from Settings → Trash.",
    confirmLabel: "Move to trash", danger: true,
  });
  if (!yes) return false;
  await actions.deleteProfile(p.id);
  return true;
}

let tzList = null;
function timezoneList() {
  if (tzList) return tzList.cloneNode(true);
  let zones = [];
  try { zones = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : []; } catch (err) { zones = []; }
  tzList = h("datalist", { id: "pp-timezones" }, zones.map((z) => h("option", { value: z })));
  return tzList.cloneNode(true);
}
