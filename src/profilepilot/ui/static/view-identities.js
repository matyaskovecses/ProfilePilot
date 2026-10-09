// Identities: saved personal details for form autofill. Sensitive values are write-only.

import { api, enc } from "./api.js";
import { avatar, fmt, h, icon, replace } from "./dom.js";
import { openProfileDrawer } from "./profile-drawer.js";
import { loadIdentities, loadProfiles, secretPlace, state } from "./store.js";
import { busy, changeTracker, closeButton, confirmDialog, emptyState, field, nextId, openDialog, openDrawer, setError, toast } from "./ui.js";

const GROUPS = [
  { key: "personal", title: "Personal", icon: "user" },
  { key: "address", title: "Address", icon: "proxies" },
  { key: "card", title: "Card", icon: "card" },
  { key: "sensitive", title: "Sensitive", icon: "lock" },
];

const INPUT_TYPES = {
  email: { type: "email", autocomplete: "off" },
  phone: { type: "tel", autocomplete: "off" },
  birth_date: { type: "date" },
  postal_code: { autocomplete: "off" },
};

/** Friendlier placeholders than the field specs' developer notes. */
const HELP_OVERRIDES = {
  full_name: "Leave empty to combine first and last name",
  birth_date: "",
  phone: "As you want it typed, e.g. +1 555 123 4567",
  state: "Name or code, e.g. California or CA",
  country: "e.g. United States",
  country_code: "Two letters, e.g. US",
};

const BRAND_NAMES = { visa: "Visa", mastercard: "Mastercard", amex: "American Express", discover: "Discover", card: "Card" };

/** "visa •••• 4242" -> "Visa •••• 4242". */
export function prettyMasked(value) {
  const text = String(value || "");
  const brand = text.split(" ")[0];
  return BRAND_NAMES[brand] ? `${BRAND_NAMES[brand]}${text.slice(brand.length)}` : text;
}

function fieldsOf(group) {
  const fields = (state.meta && state.meta.fields) || [];
  return fields.filter((f) => f.group === group);
}

function placeholderOf(f) {
  if (f.key in HELP_OVERRIDES) return HELP_OVERRIDES[f.key];
  const help = f.help || "";
  return help ? help[0].toUpperCase() + help.slice(1) : "";
}

export function createIdentitiesView() {
  const subtitle = h("p");
  const newBtn = h("button.btn.primary", { onclick: () => newIdentityDialog(), attrs: { type: "button", title: "New identity (N)" } }, icon("plus"), "New identity");
  const header = h("header.view-header", h("div.view-title", h("h1", "Identities"), subtitle), h("div.view-actions", newBtn));
  const body = h("div.view-body");
  const el = h("section.view", { attrs: { "aria-label": "Identities" } }, header, body);

  function render() {
    if (!state.ready || !state.meta) return;
    const all = state.identities;
    subtitle.textContent = all.length ? `${fmt.plural(all.length, "identity", "identities")} · used by form autofill` : "Your details for forms, entered once and filled by the AI.";
    if (!all.length) {
      replace(body, emptyState({
        icon: "identities", title: "No identities yet",
        text: `An identity holds your name, address and contact details so the AI can fill forms for you. Card numbers, CVVs, SSNs and passwords are stored in ${secretPlace()}, never shown again, and only filled on sites you allow.`,
        actions: [h("button.btn.primary", { attrs: { type: "button" }, onclick: () => newIdentityDialog() }, icon("plus"), "New identity")],
      }));
      return;
    }
    replace(body, h("div.grid-cards.identity-grid", all.map(identityCard)));
  }

  return {
    el,
    title: "Identities",
    update(topics) { if (topics.has("identities") || topics.has("meta") || topics.has("ready") || topics.has("profiles")) render(); },
    onShow() { render(); },
    newItem() { newIdentityDialog(); },
  };
}

// ------------------------------------------------------------------ browser-saved addresses

const BROWSER_FIELD_WORDS = [
  ["name", ["first_name", "middle_name", "last_name", "full_name"]],
  ["email", ["email"]],
  ["phone", ["phone"]],
  ["company", ["company"]],
  ["address", ["street", "address_line2", "city", "state", "postal_code", "country", "country_code"]],
];

/** ["first_name", "email", "city"] -> "Name, email and address". */
export function browserFieldsText(keys) {
  const have = new Set(keys || []);
  const words = BROWSER_FIELD_WORDS.filter(([, ks]) => ks.some((k) => have.has(k))).map(([w]) => w);
  if (!words.length) return "";
  const text = words.length === 1 ? words[0] : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
  return text[0].toUpperCase() + text.slice(1);
}

/** "Google Chrome profile 'Me' (active)" -> "Google Chrome". */
function browserName(label) {
  return String(label || "your browser").replace(/ profile '.*$/, "");
}

/** The linked browser, in one short line for cards and toasts. */
function linkLine(link) {
  if (!link) return "";
  if (!link.ok) return "Browser link unavailable";
  const what = browserFieldsText(link.fields_from_chrome);
  return what ? `${what} from ${link.label}` : `Linked to ${link.label}`;
}

/** The second line: which saved address, or why nothing is taken from it. */
function linkDetail(link) {
  if (!link) return "";
  if (!link.ok) return link.error || "The saved address could not be read.";
  const address = `${link.pinned ? "Saved address" : "Most used address"}: ${link.address}`;
  return (link.fields_from_chrome || []).length ? address
    : `Nothing is taken from it now: the identity's own name and address replace the browser's. ${address}`;
}

async function connectIdentity(ident, body) {
  const view = await api.post(`/api/identities/${enc(ident.id)}/chrome`, body);
  loadIdentities().catch(() => {});
  return view;
}

async function disconnectIdentity(ident, { onChange } = {}) {
  const before = ident.chrome;
  try {
    const view = await api.del(`/api/identities/${enc(ident.id)}/chrome`);
    loadIdentities().catch(() => {});
    if (onChange) onChange(view);
    toast(`"${ident.name}" no longer takes details from your browser.`, {
      kind: "success", title: "Disconnected", timeout: 6000,
      action: before ? { label: "Undo", onClick: () => connectIdentity(ident, { source: before.source, address: before.address_id || null })
        .then((v) => onChange && onChange(v)).catch((err) => toast(err.message, { kind: "error" })) } : null,
    });
  } catch (err) {
    toast(err.message, { kind: "error", title: "Could not disconnect" });
  }
}

/**
 * "Connect to browser": pick a browser profile and one of its saved addresses (or its most used one).
 * The page only ever sees each address's summary (name and city); the details are read at fill time.
 */
export function connectBrowserDialog(ident, { onChange } = {}) {
  const list = h("div.source-list", { attrs: { "aria-live": "polite" } }, h("div.skeleton.skeleton-line"), h("div.skeleton.skeleton-line"));
  const connect = h("button.btn.primary", { disabled: true, attrs: { type: "button" } }, icon("link"), "Connect");
  const recheck = h("button.btn", { attrs: { type: "button" } }, icon("refresh"), "Check again");
  const group = nextId("src");
  let choices = [];

  function option(value, title, sub, checked) {
    const input = h("input", { type: "radio", checked, attrs: { name: group } });
    choices.push({ input, value });
    return h("label.source-option", input, h("span.stack", { style: { gap: "1px" } }, h("span", title), sub ? h("span.small.faint", sub) : null));
  }

  async function load() {
    replace(list, h("div.skeleton.skeleton-line"), h("div.skeleton.skeleton-line"));
    connect.disabled = true;
    choices = [];
    let sources = [];
    try {
      sources = (await api.get("/api/autofill/sources")).sources;
    } catch (err) {
      replace(list, h("div.callout.warn", icon("alert"), h("span", err.message)));
      return;
    }
    const usable = sources.filter((s) => s.addresses.length);
    if (!usable.length) {
      replace(list, emptyState({
        icon: "identities", compact: true, title: sources.length ? "No saved addresses yet" : "No browser data found",
        text: "Save your address in Chrome, Edge or Brave first (Settings → Autofill and passwords → Addresses and more), then check again.",
      }), ...sources.filter((s) => s.error).map((s) => h("p.small.faint", `${s.label}: ${s.error}`)));
      return;
    }
    const current = ident.chrome || null;
    let first = true;
    const blocks = sources.map((s, index) => {
      const head = h("div.source-head", icon("tab"), h("strong", `${s.browser_label} – ${s.profile_name}`),
        s.active ? h("span.badge.violet", "Active profile") : null);
      if (s.error) return h("div.source-block", head, h("p.small.faint", s.error));
      if (!s.addresses.length) return h("div.source-block", head, h("p.small.faint", "No saved addresses in this profile."));
      // "chrome" follows whichever Chrome profile is active; other profiles are linked by name.
      const symbolic = index === 0 && s.active ? "chrome" : s.ref;
      const isCurrent = (v) => current && current.source === v.source && (current.address_id || null) === (v.address || null);
      const most = { source: symbolic, address: null };
      const rows = [option(most, "Most used address", `${s.addresses[0].summary}${symbolic === "chrome" ? " · follows the profile you use in Chrome" : ""}`,
        current ? isCurrent(most) : first)];
      first = false;
      for (const a of s.addresses) {
        const v = { source: s.ref, address: a.id };
        rows.push(option(v, `${a.number}. ${a.summary}`, a.uses ? `Used ${fmt.plural(a.uses, "time")}` : null,
          current ? (current.source === s.ref && current.address_id === a.id) : false));
      }
      return h("div.source-block", head, h("div.source-options", { attrs: { role: "radiogroup", "aria-label": `${s.browser_label} ${s.profile_name}` } }, rows));
    });
    replace(list, ...blocks);
    if (!choices.some((c) => c.input.checked) && choices[0]) choices[0].input.checked = true;
    connect.disabled = false;
    const picked = choices.find((c) => c.input.checked);
    if (picked && dlg.el.open && !list.contains(document.activeElement)) picked.input.focus();
  }

  const dlg = openDialog({
    title: `Connect "${ident.name}" to your browser`, size: "wide",
    description: "Name, email, phone and address come live from an address you saved in Chrome, Edge or Brave, so edits there apply here too. Values you type into the identity win. Cards, passwords and ID numbers are never read from the browser.",
    body: list,
    footer: [recheck, h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.forceClose() }, "Cancel"), connect],
  });
  recheck.addEventListener("click", () => busy(recheck, load));
  connect.addEventListener("click", () => busy(connect, async () => {
    const picked = choices.find((c) => c.input.checked);
    if (!picked) return;
    try {
      const view = await connectIdentity(ident, picked.value);
      dlg.forceClose();
      toast(linkLine(view.chrome) + ".", { kind: "success", title: `"${ident.name}" is connected` });
      if (onChange) onChange(view);
    } catch (err) {
      toast(err.message, { kind: "error", title: "Could not connect" });
    }
  }));
  load();
  return dlg;
}

function savedPill(value) {
  if (!value) return h("span.pill", "Not set");
  if (value === "missing") return h("span.pill.amber", { attrs: { title: "The stored value could not be read: enter it again" } }, "Missing – re-enter");
  return h("span.pill.green", icon("check"), value === "set" ? "Saved" : prettyMasked(value));
}

function identityCard(ident) {
  const groups = [];
  for (const g of GROUPS) {
    const lines = [];
    for (const f of fieldsOf(g.key)) {
      if (f.sensitive) {
        if (f.key === "card_exp_month" || f.key === "card_exp_year") continue;
        const v = ident.sensitive[f.key];
        if (!v) continue; // hide what was never set (a card group for someone without a card)
        const short = f.label.replace(/^Card /, "");
        lines.push(h("div.ic-line", h("span.k", short[0].toUpperCase() + short.slice(1)), h("span.v.masked", { class: v === "missing" ? "unset" : "" },
          v === "set" ? "•••• saved" : v === "missing" ? "missing" : prettyMasked(v))));
      } else if (ident.values[f.key]) {
        lines.push(h("div.ic-line", h("span.k", f.label), h("span.v", { attrs: { title: ident.values[f.key] } }, ident.values[f.key])));
      }
    }
    if (g.key === "card" && ident.sensitive.card_exp_month) {
      lines.splice(Math.min(lines.length, 2), 0, h("div.ic-line", h("span.k", "Expiry"), h("span.v.masked", "•• / ••")));
    }
    if (!lines.length) continue;
    groups.push(h("div.ic-group", h("h4", icon(g.icon), g.title), lines.slice(0, 7), lines.length > 7 ? h("div.ic-line.faint", `+${lines.length - 7} more`) : null));
  }
  const used = ident.used_by || [];
  const insecure = (ident.insecure_origins || []).length;
  const link = ident.chrome;
  const linkBox = link ? h("div.ic-link", { class: link.ok ? "" : "warn" },
    icon(link.ok ? "link" : "alert"),
    h("div.stack.grow", { style: { gap: "1px" } },
      h("span", linkLine(link)),
      h("span.small.faint.ellipsis", { attrs: { title: linkDetail(link) } }, linkDetail(link)))) : null;
  const open = () => openIdentityDrawer(ident.id);
  const actions = h("div.ic-actions",
    link
      ? [h("button.btn.sm", { attrs: { type: "button", title: "Pick another browser profile or saved address" }, onclick: () => connectBrowserDialog(ident) }, icon("link"), "Change"),
        h("button.btn.sm.ghost", { attrs: { type: "button", title: "Stop taking details from the browser" }, onclick: (e) => busy(e.currentTarget, () => disconnectIdentity(ident)) }, "Disconnect")]
      : h("button.btn.sm", { attrs: { type: "button", title: "Take name, email, phone and address from an address saved in Chrome, Edge or Brave" }, onclick: () => connectBrowserDialog(ident) }, icon("link"), "Connect to browser"),
    h("span.spacer"),
    h("button.btn.sm.ghost", { attrs: { type: "button", "aria-label": `Edit ${ident.name}` }, onclick: open }, icon("edit"), "Edit"));
  return h("article.card.identity-card", {
    attrs: { "aria-label": `Identity ${ident.name}` },
    onclick: (event) => { if (!event.target.closest("button, a")) open(); },
  },
    h("div.ic-head", avatar(ident.name, "md", ident.id),
      h("div.titles", h("button.ic-name.ellipsis", { attrs: { type: "button", title: `Edit ${ident.name}` }, onclick: open }, ident.name),
        h("span.small.faint", used.length ? `Used by ${used.map((u) => u.name).join(", ")}` : "Not linked to a profile")),
      h("span.spacer"),
      insecure ? h("span.badge.red", { attrs: { title: ident.insecure_origins.join("\n") } }, icon("alert"), "Not secure site") : null,
      ident.allowed_origins.length ? h("span.badge.violet", { attrs: { title: ident.allowed_origins.join("\n") } }, icon("shield"), `${ident.allowed_origins.length} site${ident.allowed_origins.length === 1 ? "" : "s"}`) : null),
    linkBox,
    groups.length ? h("div.ic-groups", groups) : h("div.card-body.faint", link && link.ok ? "No details of its own: everything comes from the browser." : "No details yet. Click to add them."),
    actions,
  );
}

function newIdentityDialog() {
  const name = h("input.input", { attrs: { placeholder: "e.g. Personal, Work, Alex Sample", maxlength: "64", autofocus: true } });
  name.addEventListener("input", () => setError(name, ""));
  const create = h("button.btn.primary", { attrs: { type: "button" } }, "Create");
  const dlg = openDialog({
    title: "New identity", size: "narrow",
    description: "Give it a name; you add the details next.",
    body: h("form.form", { onsubmit: (e) => { e.preventDefault(); create.click(); } }, field("Name", name)),
    isDirty: () => !!name.value.trim(),
    footer: [h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.forceClose() }, "Cancel"), create],
  });
  create.addEventListener("click", () => busy(create, async () => {
    if (!name.value.trim()) { setError(name, "Give the identity a name."); name.focus(); return; }
    try {
      const ident = await api.post("/api/identities", { name: name.value.trim(), values: {} });
      dlg.forceClose();
      await loadIdentities();
      openIdentityDrawer(ident.id);
    } catch (err) {
      setError(name, err.message);
    }
  }));
}

// ------------------------------------------------------------------ drawer

const EXPIRY_RE = /^\s*(\d{1,2})\s*\/\s*(\d{2}|\d{4})\s*$/;

/** Client-side checks for write-only values (the server checks again, e.g. the Luhn digit). */
function checkSecret(key, value) {
  const v = value.trim();
  if (key === "card_number" && !/^[\d\s-]{13,23}$/.test(v)) return "A card number has 13 to 19 digits.";
  if (key === "card_cvv" && !/^\d{3,4}$/.test(v)) return "A CVV has 3 or 4 digits.";
  if (key === "card_exp") {
    const m = EXPIRY_RE.exec(v);
    if (!m) return "Use MM/YY, e.g. 08/29.";
    if (Number(m[1]) < 1 || Number(m[1]) > 12) return "The month must be 01 to 12.";
  }
  if (key === "ssn" && !/^\d{3}[- ]?\d{2}[- ]?\d{4}$/.test(v)) return "Use the format 123-45-6789.";
  return "";
}

export function openIdentityDrawer(id) {
  let ident = state.identities.find((i) => i.id === id);
  if (!ident) return;
  let changes = { dirty: () => false, reset() {} };
  const drawer = openDrawer({ label: `Identity ${ident.name}`, isDirty: () => changes.dirty() });
  drawer.tabs.classList.add("hidden");
  const title = h("h2");
  const sub = h("div.small.muted");
  const avatarHost = h("div");
  drawer.header.append(avatarHost, h("div.titles", title, sub), closeButton(drawer.close));
  const save = h("button.btn.primary", { attrs: { type: "button" } }, "Save details");
  const discard = h("button.btn.ghost", { attrs: { type: "button" }, onclick: () => build() }, "Discard");
  const unsaved = h("span.small.muted", "Unsaved changes");
  drawer.footer.classList.remove("hidden");
  replace(drawer.footer, unsaved, h("span.spacer"), discard, save);

  let inputs = {};
  let secretInputs = {};
  let nameInput;
  let notesInput;

  function sync() {
    const dirty = changes.dirty();
    save.disabled = !dirty;
    discard.classList.toggle("hidden", !dirty);
    unsaved.classList.toggle("hidden", !dirty);
  }

  function headerRender() {
    title.textContent = ident.name;
    replace(avatarHost, avatar(ident.name, "md", ident.id));
    const used = ident.used_by || [];
    replace(sub, used.length ? h("span", "Used by ", used.map((u, i) => [i ? ", " : "", h("a", { href: "#/profiles", onclick: (e) => { e.preventDefault(); drawer.forceClose(); openProfileDrawer(u.id); } }, u.name)])) : "Not linked to a profile (link it in a profile's settings)");
  }

  function plainControl(f) {
    let control;
    if (f.key === "gender") {
      control = h("select.select", h("option", { value: "" }, "—"), ["female", "male", "other"].map((g) => h("option", { value: g }, g[0].toUpperCase() + g.slice(1))));
    } else {
      const spec = INPUT_TYPES[f.key] || {};
      const link = ident.chrome;
      const fromBrowser = link && link.ok && (link.fields_from_chrome || []).includes(f.key);
      control = h("input.input", { type: spec.type || "text", attrs: { autocomplete: spec.autocomplete || "off",
        placeholder: fromBrowser ? `From ${browserName(link.label)} (type to use your own)` : placeholderOf(f) } });
      if (f.key === "country_code") control.setAttribute("maxlength", "2");
    }
    control.value = ident.values[f.key] || "";
    control.addEventListener("input", () => setError(control, ""));
    inputs[f.key] = control;
    return field(f.label, control);
  }

  function plainSection(group, heading) {
    const fields = fieldsOf(group).filter((f) => !f.sensitive);
    if (!fields.length) return null;
    const rows = [];
    let pair = [];
    for (const f of fields) {
      pair.push(plainControl(f));
      if (pair.length === 2) { rows.push(h("div.field-row", pair)); pair = []; }
    }
    if (pair.length) rows.push(h("div.field-row", pair, h("div")));
    return h("div.form-section", h("h3", heading), rows);
  }

  /** A write-only value: state pill, an input that saves with "Save details", and Clear. */
  function secretRow(key, label, value, { placeholder, inputmode, password = false, clearKeys = [key] } = {}) {
    const input = h("input.input.mono.sm", {
      type: password ? "password" : "text",
      attrs: { autocomplete: "off", "aria-label": `${label} (write-only)`, placeholder: value && value !== "missing" ? "Type to replace" : placeholder || "Enter…", inputmode: inputmode || null, spellcheck: "false" },
    });
    input.addEventListener("input", () => setError(input, ""));
    secretInputs[key] = input;
    const clear = value ? h("button.btn.sm.ghost", {
      attrs: { type: "button", title: `Delete the saved ${label.toLowerCase()}` },
      onclick: async (e) => {
        const button = e.currentTarget;
        const yes = await confirmDialog({ title: `Clear the ${label.toLowerCase()}?`, message: "The saved value is deleted. You can enter it again later.", confirmLabel: "Clear", danger: true });
        if (!yes) return;
        busy(button, async () => {
          try {
            for (const k of clearKeys) ident = await api.del(`/api/identities/${enc(id)}/secret/${enc(k)}`);
            toast(`${label} cleared.`, { kind: "success" });
            build({ keepTyped: true });
            loadIdentities().catch(() => {});
          } catch (err) { toast(err.message, { kind: "error" }); }
        });
      },
    }, "Clear") : null;
    return h("div.secret-row",
      h("div.label", label, savedPill(value)),
      h("div.field.secret-field", input),
      clear || h("span"));
  }

  function cardSection() {
    const s = ident.sensitive;
    const expiry = s.card_exp_month && s.card_exp_year ? (s.card_exp_month === "missing" || s.card_exp_year === "missing" ? "missing" : "set") : null;
    const card = ident.card ? h("div.cardface",
      h("div.brand-line", h("span", BRAND_NAMES[ident.card.brand] || ident.card.brand), h("span", "ProfilePilot")),
      h("div.number", `•••• •••• •••• ${ident.card.last4}`),
      h("div.brand-line", h("span", ident.values.card_name || [ident.values.first_name, ident.values.last_name].filter(Boolean).join(" ") || " "),
        h("span", expiry ? "•• / ••" : ""))) : null;
    const holder = fieldsOf("card").filter((f) => !f.sensitive).map((f) => plainControl(f));
    return h("div.form-section",
      h("h3", "Card"),
      card,
      holder,
      secretRow("card_number", "Card number", s.card_number, { placeholder: "4242 4242 4242 4242", inputmode: "numeric", password: true }),
      secretRow("card_exp", "Expiry", expiry, { placeholder: "MM/YY", inputmode: "numeric", clearKeys: ["card_exp_month", "card_exp_year"] }),
      secretRow("card_cvv", "CVV", s.card_cvv, { placeholder: "3 or 4 digits", inputmode: "numeric", password: true }));
  }

  function sensitiveSection() {
    const s = ident.sensitive;
    return h("div.form-section",
      h("h3", "Other sensitive details"),
      h("div.callout", icon("lock"), h("span", "Card details, the SSN and the password are ", h("strong", "write-only"), `: they go straight to ${secretPlace()} and are never shown again, only masked. The AI can fill them only with your approval, and only on the sites below.`)),
      secretRow("ssn", "SSN", s.ssn, { placeholder: "123-45-6789", inputmode: "numeric", password: true }),
      secretRow("password", "Password", s.password, { placeholder: "Website password", password: true }));
  }

  /** "From your browser": the live link to an address saved in Chrome / Edge / Brave. */
  function browserSection() {
    const link = ident.chrome;
    const onChange = (view) => { ident = view; build({ keepTyped: true }); };
    const connectBtn = h("button.btn.sm", { attrs: { type: "button" }, onclick: () => connectBrowserDialog(ident, { onChange }) },
      icon("link"), link ? "Change…" : "Connect to browser…");
    if (!link) {
      return h("div.form-section",
        h("h3", "From your browser"),
        h("div.row.wrap", h("span.small.muted.grow", "Take the name, email, phone and address from an address you saved in Chrome, Edge or Brave. It stays up to date when you edit it there."),
          connectBtn));
    }
    const disconnect = h("button.btn.sm.ghost", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => disconnectIdentity(ident, { onChange })) }, "Disconnect");
    const body = link.ok
      ? h("div.stack.grow", { style: { gap: "4px" } },
        h("strong", linkLine(link)),
        h("span", linkDetail(link)),
        h("span.small.faint", "Read live each time a form is filled. Values you type below win. Cards, passwords and ID numbers are never read from the browser."))
      : h("div.stack.grow", { style: { gap: "4px" } },
        h("strong", "The browser link is unavailable"),
        h("span", link.error || "The saved address could not be read."),
        h("span.small.faint", "Until it is fixed, forms are filled with this identity's own values only."));
    return h("div.form-section",
      h("h3", "From your browser"),
      h("div.callout", { class: link.ok ? "" : "warn" }, icon(link.ok ? "link" : "alert"), body),
      h("div.row", connectBtn, disconnect));
  }

  function originsSection() {
    const input = h("input.input.sm", { attrs: { placeholder: "https://shop.example.com", autocomplete: "off", "aria-label": "Allowed site" } });
    input.addEventListener("input", () => setError(input, ""));
    const add = h("button.btn.sm", { attrs: { type: "submit" } }, icon("plus"), "Allow");
    const insecure = new Set(ident.insecure_origins || []);
    return h("div.form-section",
      h("h3", "Sites allowed for sensitive autofill"),
      h("p.small.muted", "Card numbers, CVVs, SSNs and passwords are only filled on these exact sites, over HTTPS. This protects you if a page tricks the AI into filling a form somewhere else."),
      ident.allowed_origins.length ? h("div.row.wrap", ident.allowed_origins.map((o) => h("span.tag.origin", { class: insecure.has(o) ? "danger" : "" },
        insecure.has(o) ? h("span.not-secure", { attrs: { title: "Card details are never filled on insecure (http://) pages. Remove this site." } }, icon("alert"), "Not secure") : null,
        h("span.mono", o),
        h("button", { attrs: { type: "button", "aria-label": `Remove ${o}` }, onclick: async () => {
          try { ident = await api.del(`/api/identities/${enc(id)}/origins`, { origin: o }); build({ keepTyped: true }); loadIdentities().catch(() => {}); }
          catch (err) { toast(err.message, { kind: "error" }); }
        } }, icon("x"))))) : h("p.small.faint", "No sites yet: sensitive autofill is off for this identity."),
      h("form.field", {
        onsubmit: (event) => {
          event.preventDefault();
          if (!input.value.trim()) return;
          busy(add, async () => {
            try { ident = await api.post(`/api/identities/${enc(id)}/origins`, { origin: input.value.trim() }); build({ keepTyped: true }); loadIdentities().catch(() => {}); }
            catch (err) { setError(input, err.message); input.focus(); }
          });
        },
      }, h("div.row", input, add)));
  }

  /** (Re)build the drawer body from `ident`. keepTyped: keep values typed but not saved yet. */
  function build({ keepTyped = false } = {}) {
    const typed = keepTyped ? {
      name: nameInput && nameInput.value, notes: notesInput && notesInput.value,
      plain: Object.fromEntries(Object.entries(inputs).map(([k, c]) => [k, c.value])),
      secrets: Object.fromEntries(Object.entries(secretInputs).map(([k, c]) => [k, c.value])),
    } : null;
    inputs = {};
    secretInputs = {};
    headerRender();
    nameInput = h("input.input", { value: ident.name, attrs: { maxlength: "64" } });
    notesInput = h("textarea.textarea", { value: ident.notes || "", attrs: { rows: "2" } });
    const form = h("form.form", { onsubmit: (event) => { event.preventDefault(); save.click(); } },
      h("div.form-section", h("h3", "Identity"), field("Name", nameInput), field("Notes", notesInput)),
      plainSection("personal", "Personal"),
      plainSection("address", "Address"),
      cardSection(),
      sensitiveSection(),
      h("button", { attrs: { type: "submit", hidden: true, tabindex: "-1" } }));
    const snapshot = () => JSON.stringify([nameInput.value.trim(), notesInput.value,
      Object.entries(inputs).map(([k, c]) => [k, c.value.trim()]), Object.entries(secretInputs).map(([k, c]) => [k, c.value])]);
    changes = changeTracker(snapshot);
    if (typed) {
      // restore what the user typed elsewhere in the drawer
      if (typed.name != null) nameInput.value = typed.name;
      if (typed.notes != null) notesInput.value = typed.notes;
      for (const [k, v] of Object.entries(typed.plain)) if (inputs[k]) inputs[k].value = v;
      for (const [k, v] of Object.entries(typed.secrets)) if (secretInputs[k]) secretInputs[k].value = v;
    }
    for (const type of ["input", "change"]) form.addEventListener(type, () => setTimeout(sync));
    replace(drawer.body,
      browserSection(),
      form,
      originsSection(),
      h("div.form-section", h("h3", "Delete"),
        h("div.row", h("span.small.muted.grow", "Removes the identity and its stored card, SSN and password values. Linked profiles are unlinked."),
          h("button.btn.danger", { attrs: { type: "button" }, onclick: async () => {
            const yes = await confirmDialog({ title: `Delete "${ident.name}"?`, message: "Its details and stored secrets are deleted permanently.", confirmLabel: "Delete identity", danger: true });
            if (!yes) return;
            try {
              await api.del(`/api/identities/${enc(id)}`);
              drawer.forceClose();
              toast(`Deleted "${ident.name}".`, { kind: "success" });
              await Promise.all([loadIdentities(), loadProfiles()]);
            } catch (err) { toast(err.message, { kind: "error" }); }
          } }, icon("trash"), "Delete…"))));
    sync();
  }

  async function saveAll() {
    // 1. check the write-only values first (inline errors, nothing sent for a bad one)
    const pendingSecrets = Object.entries(secretInputs).filter(([, c]) => c.value.trim());
    let bad = null;
    for (const [key, control] of pendingSecrets) {
      const problem = checkSecret(key, control.value);
      setError(control, problem);
      if (problem && !bad) bad = control;
    }
    if (!nameInput.value.trim()) { setError(nameInput, "Give the identity a name."); bad = bad || nameInput; }
    if (bad) { bad.focus(); return; }
    // 2. plain details
    const values = {};
    for (const [key, control] of Object.entries(inputs)) {
      const before = ident.values[key] || "";
      const now = control.value.trim();
      if (now !== before) values[key] = now || null;
    }
    const plainChanged = Object.keys(values).length || nameInput.value.trim() !== ident.name || notesInput.value !== (ident.notes || "");
    if (plainChanged) {
      try {
        ident = await api.patch(`/api/identities/${enc(id)}`, { name: nameInput.value.trim(), notes: notesInput.value, values });
      } catch (err) {
        const key = Object.keys(values).find((k) => err.message.includes((state.meta.fields.find((f) => f.key === k) || {}).label || "\u0000"));
        if (key) { setError(inputs[key], err.message); inputs[key].focus(); }
        else toast(err.message, { kind: "error", title: "Could not save" });
        return;
      }
    }
    // 3. write-only values, one by one; a failing one keeps its typed value and shows why
    let failed = null;
    for (const [key, control] of pendingSecrets) {
      try {
        if (key === "card_exp") {
          const [, month, year] = EXPIRY_RE.exec(control.value);
          ident = await api.put(`/api/identities/${enc(id)}/secret/card_exp_month`, { value: month });
          ident = await api.put(`/api/identities/${enc(id)}/secret/card_exp_year`, { value: year });
        } else {
          ident = await api.put(`/api/identities/${enc(id)}/secret/${enc(key)}`, { value: control.value.trim() });
        }
        control.value = "";
      } catch (err) {
        setError(control, err.message.replace(/^[^:]+:\s*/, "").replace(/^\w/, (c) => c.toUpperCase()));
        failed = failed || control;
      }
    }
    loadIdentities().catch(() => {});
    if (failed) {
      build({ keepTyped: true });
      const again = secretInputs[Object.keys(secretInputs).find((k) => secretInputs[k].value)];
      for (const [key, control] of pendingSecrets) {
        if (control.value && secretInputs[key]) setError(secretInputs[key], control.getAttribute("aria-invalid") ? (control.closest(".field").querySelector(".error-text") || {}).textContent : "");
      }
      if (again) again.focus();
      return;
    }
    build();
    toast(pendingSecrets.length ? "Details saved. Sensitive values are stored and hidden." : "Details saved.", { kind: "success" });
  }

  save.addEventListener("click", () => busy(save, saveAll));
  build();
  drawer.focusInitial();
}
