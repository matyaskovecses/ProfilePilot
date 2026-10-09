// "New profile", "Clone profile" and delete confirmation, plus the shared proxy picker.

import { api, enc } from "./api.js";
import { debounce, h, icon, replace } from "./dom.js";
import { actions, loadProxies, proxyLabel, proxyPlace, secretPlace, state, upsertProfile } from "./store.js";
import { busy, changeTracker, chipInput, choiceCards, confirmDialog, field, openDialog, segmented, select, setError, toast } from "./ui.js";

export const BROWSER_LABELS = { auto: "Automatic", chrome: "Google Chrome", edge: "Microsoft Edge", brave: "Brave", chromium: "Chromium" };
export const AUTO_BROWSER_LABEL = "Automatic (Chrome, then Edge, Brave, Chromium)";
export const WINDOW_LABELS = { normal: "Normal window", offscreen: "Off-screen window", headless: "Headless" };

export const WINDOW_CHOICES = [
  { value: "normal", title: "Normal", icon: "monitor", badge: "Recommended", desc: "A regular window, exactly like a person using Chrome. Best for logins and sites that check for bots." },
  { value: "offscreen", title: "Off-screen", icon: "tab", desc: "Placed outside the screen, so it runs in the background, but it is still a real window." },
  { value: "headless", title: "Headless", icon: "eye", desc: "No window at all. Sites can detect it, so use it only for simple scraping." },
];

export const TIMEZONE_HINT = "Leave empty to use this computer's time zone. Set it only to match the proxy's country.";

export function proxyOptions(includeNone = true) {
  const opts = includeNone ? [{ value: "", label: "No proxy (direct connection)" }] : [];
  for (const p of state.proxies) {
    const check = p.last_check;
    const where = check && check.ok ? ` · ${[check.country_code, proxyPlace(check)].filter(Boolean).join(" ")}` : check ? " · last test failed" : "";
    const name = proxyLabel(p);
    const address = `${p.host}:${p.port}`;
    opts.push({ value: p.id, label: `${name}${name === p.host || name === address ? "" : ` — ${address}`}${where}` });
  }
  return opts;
}

export function identityOptions() {
  return [{ value: "", label: "None" }, ...state.identities.map((i) => ({ value: i.id, label: i.name }))];
}

export function browserOptions() {
  const opts = [{ value: "auto", label: AUTO_BROWSER_LABEL }];
  for (const b of state.browsers) opts.push({ value: b.kind, label: `${b.label}${b.version ? ` ${b.version.split(".")[0]}` : ""}` });
  return opts;
}

/** The proxy a new profile should most likely use: a working one no profile uses yet. */
export function suggestedProxyId() {
  const unused = (p) => !(p.used_by || []).length;
  const working = (p) => p.last_check && p.last_check.ok;
  const pick = state.proxies.find((p) => working(p) && unused(p)) || state.proxies.find((p) => unused(p) && !p.last_check)
    || state.proxies.find(working) || state.proxies[0];
  return pick ? pick.id : "";
}

/**
 * Proxy picker: none / saved / paste new (with a live check of the pasted line).
 * Returns {el, value(), clear(), snapshot()}; value() -> {proxy_id} | {proxy_url, proxy_scheme} | {proxy_id: ""}.
 * `suggest`: preselect a saved proxy (new profiles) instead of "No proxy".
 */
export function proxyPicker(currentId = "", { suggest = false } = {}) {
  const initialSaved = currentId || (suggest ? suggestedProxyId() : "") || (state.proxies[0] && state.proxies[0].id) || "";
  const mode0 = currentId || (suggest && state.proxies.length) ? "saved" : "none";
  const savedSelect = select(proxyOptions(false), initialSaved, {});
  savedSelect.setAttribute("aria-label", "Saved proxy");
  const pasteInput = h("input.input.mono", { attrs: { placeholder: "socks5://user:pass@host:port  or  host:port:user:pass", autocomplete: "off", spellcheck: "false", "aria-label": "Proxy address" } });
  const schemeSelect = select(["http", "https", "socks5", "socks4"].map((s) => ({ value: s, label: s.toUpperCase() })), "http", { cls: "sm" });
  schemeSelect.setAttribute("aria-label", "Type when the line has no scheme");
  const check = h("div.paste-check.small", { attrs: { "aria-live": "polite" } });
  const runCheck = debounce(async () => {
    const text = pasteInput.value.trim();
    if (!text) { replace(check); pasteInput.removeAttribute("aria-invalid"); return; }
    try {
      const result = await api.post("/api/proxies/parse", { text, scheme: schemeSelect.value });
      const line = result.lines[0];
      if (line && line.ok) {
        pasteInput.removeAttribute("aria-invalid");
        replace(check, h("span.badge.green", icon("check"), "Looks right"),
          h("span.mono", ` ${line.scheme.toUpperCase()} ${line.host}:${line.port}${line.username ? ` · ${line.username}${line.has_password ? " / ••••" : ""}` : ""}`));
      } else {
        pasteInput.setAttribute("aria-invalid", "true");
        replace(check, h("span.badge.red", icon("x"), "Can't read this"),
          h("span", " Use scheme://user:pass@host:port, host:port:user:pass or host:port."));
      }
    } catch (err) {
      replace(check, h("span.faint", err.message));
    }
  }, 250);
  pasteInput.addEventListener("input", runCheck);
  schemeSelect.addEventListener("change", runCheck);
  const savedRow = h("div", savedSelect);
  const pasteRow = h("div.stack",
    h("div.row", pasteInput, h("div", { style: { width: "110px", flex: "none" } }, schemeSelect)),
    check,
    h("div.hint.small.faint", `The type on the right is used when the line has no scheme. The password is stored in ${secretPlace()} and never shown again.`));
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
    if (mode === "new") pasteInput.focus();
  };
  replace(body, mode0 === "saved" ? savedRow : h("div.hint.small.faint", "The browser connects directly from this computer's own IP address."));
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
    snapshot: () => JSON.stringify([seg.value, savedSelect.value, pasteInput.value.trim(), schemeSelect.value]),
    clear() { pasteInput.value = ""; replace(check); },
  };
}

export function newProfileDialog({ onCreated, proxyId = "", name: suggested = "" } = {}) {
  const name = h("input.input", { value: suggested, attrs: { placeholder: "e.g. shop-us, mail-de, research", autocomplete: "off", maxlength: "64", autofocus: true, "aria-required": "true" } });
  name.addEventListener("input", () => { if (name.value.trim()) setError(name, ""); });
  const proxy = proxyPicker(proxyId, { suggest: true });
  const identity = select(identityOptions(), "", {});
  const browser = select(browserOptions(), "auto", {});
  const windowMode = choiceCards(WINDOW_CHOICES, (state.settings && state.settings.default_window) || "normal", null, { label: "Window" });
  const tags = chipInput([], { placeholder: "Add tags, e.g. shop, us" });
  const notes = h("textarea.textarea", { attrs: { rows: "2", placeholder: "Anything worth remembering about this profile" } });
  const lang = h("input.input", { attrs: { placeholder: "e.g. de-DE (default: your system language)", autocomplete: "off" } });
  const tz = h("input.input", { attrs: { placeholder: "e.g. Europe/Berlin", autocomplete: "off", list: "pp-timezones" } });
  const startUrl = h("input.input", { attrs: { placeholder: "https://…", autocomplete: "off", type: "url" } });
  const startNow = h("input", { type: "checkbox", checked: true });
  const error = h("div.callout.warn.hidden", { attrs: { role: "alert" } });

  const body = h("form.form", { onsubmit: (event) => { event.preventDefault(); create.click(); } },
    field("Name", name, { hint: "A short name the AI can refer to. Each profile has its own cookies, logins and history." }),
    h("div.field", h("span.field-label", "Proxy"), proxy.el),
    h("div.field", h("span.field-label", "Window"), windowMode),
    h("details.more-options",
      h("summary", icon("chevron-right"), "More options", h("span.faint", " – identity, browser, tags, notes, language, time zone, start page")),
      h("div.form", { style: { marginTop: "12px" } },
        h("div.field-row",
          field("Identity for autofill", identity, { hint: "Your saved details for forms." }),
          field("Browser", browser)),
        field("Tags", tags),
        field("Notes", notes, { hint: "Your AI can read these notes." }),
        h("div.field-row", field("Language", lang), field("Time zone", tz, { hint: TIMEZONE_HINT })),
        field("Start page", startUrl))),
    error,
    timezoneList(),
  );

  const snapshot = () => JSON.stringify([name.value.trim(), proxy.snapshot(), identity.value, browser.value, windowMode.value,
    tags.pending, notes.value, lang.value, tz.value, startUrl.value]);
  const changes = changeTracker(snapshot);
  const create = h("button.btn.primary", { attrs: { type: "button" } }, "Create profile");
  const dlg = openDialog({
    title: "New profile", description: "A separate, real Chrome identity with its own cookies, storage and proxy.",
    body, size: "wide", isDirty: () => changes.dirty(),
    footer: [h("label.checkbox", startNow, h("span", "Start it now")), h("span.spacer"),
      h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.close() }, "Cancel"), create],
  });
  create.addEventListener("click", () => busy(create, async () => {
    error.classList.add("hidden");
    if (!name.value.trim()) {
      setError(name, "Give the profile a name.");
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
      dlg.forceClose();
      toast(`"${view.name}" is ready.`, { kind: "success", title: "Profile created" });
      if (onCreated) onCreated(view);
      if (startNow.checked) actions.start(view.id).catch(() => {});
    } catch (err) {
      if (/name/i.test(err.message) && err.status === 409) setError(name, err.message);
      else {
        replace(error, icon("alert"), h("span", err.message));
        error.classList.remove("hidden");
      }
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
    body: h("form.form", { onsubmit: (event) => { event.preventDefault(); go.click(); } },
      field("Name of the copy", name),
      h("label.checkbox", copyData, h("span.stack", { style: { gap: "2px" } }, h("span", "Also copy browser data"),
        h("span.small.faint", running ? "Stop the profile first to copy its cookies, logins and history."
          : "Cookies, logins and history. Without it the copy starts fresh with the same settings.")))),
    footer: [h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.forceClose() }, "Cancel"), go],
  });
  if (running) copyData.disabled = true;
  go.addEventListener("click", () => busy(go, async () => {
    try {
      const view = await api.post(`/api/profiles/${enc(p.id)}/clone`, { name: name.value.trim(), copy_data: copyData.checked });
      upsertProfile(view);
      dlg.forceClose();
      toast(`Created "${view.name}".`, { kind: "success", title: "Profile cloned" });
    } catch (err) {
      setError(name, err.message);
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
export function timezoneList() {
  if (tzList) return tzList.cloneNode(true);
  let zones = [];
  try { zones = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : []; } catch (err) { zones = []; }
  tzList = h("datalist", { id: "pp-timezones" }, zones.map((z) => h("option", { value: z })));
  return tzList.cloneNode(true);
}
