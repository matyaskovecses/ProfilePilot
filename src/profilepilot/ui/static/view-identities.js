// Identities: saved personal details for form autofill. Sensitive values are write-only.

import { api, enc } from "./api.js";
import { avatar, fmt, h, icon, replace } from "./dom.js";
import { openProfileDrawer } from "./profile-drawer.js";
import { loadIdentities, loadProfiles, state } from "./store.js";
import { busy, closeButton, confirmDialog, emptyState, field, openDialog, openDrawer, toast } from "./ui.js";

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

function fieldsOf(group) {
  const fields = (state.meta && state.meta.fields) || [];
  return fields.filter((f) => f.group === group);
}

export function createIdentitiesView() {
  const subtitle = h("p");
  const newBtn = h("button.btn.primary", { onclick: () => newIdentityDialog(), attrs: { title: "New identity (N)" } }, icon("plus"), "New identity");
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
        text: "An identity holds your name, address and contact details so the AI can fill forms for you. Card numbers, CVVs, SSNs and passwords are stored in your OS keychain, never shown again, and only filled on sites you allow.",
        actions: [h("button.btn.primary", { onclick: () => newIdentityDialog() }, icon("plus"), "New identity")],
      }));
      return;
    }
    replace(body, h("div.grid-cards", { style: { gridTemplateColumns: "repeat(auto-fill, minmax(380px, 1fr))" } }, all.map(identityCard)));
  }

  return {
    el,
    title: "Identities",
    update(topics) { if (topics.has("identities") || topics.has("meta") || topics.has("ready") || topics.has("profiles")) render(); },
    onShow() { render(); },
    newItem() { newIdentityDialog(); },
  };
}

function identityCard(ident) {
  const groups = [];
  for (const g of GROUPS) {
    const lines = [];
    for (const f of fieldsOf(g.key)) {
      if (f.sensitive) {
        if (g.key === "card" && f.key !== "card_number" && f.key !== "card_cvv") continue;
        const v = ident.sensitive[f.key];
        lines.push(h("div.ic-line", h("span.k", f.label), v ? h("span.v.masked", { class: v === "missing" ? "unset" : "" }, v === "set" ? "•••• set" : v) : h("span.v.unset", "not set")));
      } else if (ident.values[f.key]) {
        lines.push(h("div.ic-line", h("span.k", f.label), h("span.v", { attrs: { title: ident.values[f.key] } }, ident.values[f.key])));
      }
    }
    if (g.key === "card" && ident.sensitive.card_exp_month) {
      lines.splice(1, 0, h("div.ic-line", h("span.k", "Expiry"), h("span.v.masked", "•• / ••")));
    }
    if (!lines.length) continue;
    groups.push(h("div.ic-group", h("h4", icon(g.icon), g.title), lines.slice(0, 7), lines.length > 7 ? h("div.ic-line.faint", `+${lines.length - 7} more`) : null));
  }
  const used = ident.used_by || [];
  return h("article.card.identity-card", {
    attrs: { tabindex: "0", role: "button", "aria-label": `Edit identity ${ident.name}` },
    onclick: () => openIdentityDrawer(ident.id),
    onkeydown: (event) => { if (event.key === "Enter") openIdentityDrawer(ident.id); },
  },
    h("div.ic-head", avatar(ident.name, "md", ident.id),
      h("div.titles", h("strong.ellipsis", ident.name),
        h("span.small.faint", used.length ? `Used by ${used.map((u) => u.name).join(", ")}` : "Not linked to a profile")),
      h("span.spacer"),
      ident.allowed_origins.length ? h("span.badge.violet", { attrs: { title: ident.allowed_origins.join("\n") } }, icon("shield"), `${ident.allowed_origins.length} site${ident.allowed_origins.length === 1 ? "" : "s"}`) : null),
    groups.length ? h("div.ic-groups", groups) : h("div.card-body.faint", "No details yet. Click to add them."),
  );
}

function newIdentityDialog() {
  const name = h("input.input", { attrs: { placeholder: "e.g. Personal, Work, Alex Sample", maxlength: "64", autofocus: true } });
  const create = h("button.btn.primary", "Create");
  const dlg = openDialog({
    title: "New identity", size: "narrow",
    description: "Give it a name; you add the details next.",
    body: h("form.form", { onsubmit: (e) => { e.preventDefault(); create.click(); } }, field("Name", name)),
    footer: [h("span.spacer"), h("button.btn", { onclick: () => dlg.close() }, "Cancel"), create],
  });
  create.addEventListener("click", () => busy(create, async () => {
    try {
      const ident = await api.post("/api/identities", { name: name.value.trim(), values: {} });
      dlg.close();
      await loadIdentities();
      openIdentityDrawer(ident.id);
    } catch (err) {
      toast(err.message, { kind: "error" });
    }
  }));
}

// ------------------------------------------------------------------ drawer

export function openIdentityDrawer(id) {
  let ident = state.identities.find((i) => i.id === id);
  if (!ident) return;
  const drawer = openDrawer({ label: `Identity ${ident.name}` });
  drawer.tabs.classList.add("hidden");
  const title = h("h2");
  const sub = h("div.small.muted");
  drawer.header.append(h("div", avatar(ident.name, "md", ident.id)), h("div.titles", title, sub), closeButton(drawer.close));

  const inputs = {};
  const nameInput = h("input.input", { value: ident.name, attrs: { maxlength: "64" } });
  const notesInput = h("textarea.textarea", { value: ident.notes || "", attrs: { rows: "2" } });

  function plainSection(group, heading) {
    const fields = fieldsOf(group).filter((f) => !f.sensitive);
    if (!fields.length) return null;
    const rows = [];
    let pair = [];
    for (const f of fields) {
      let control;
      if (f.key === "gender") {
        control = h("select.select", h("option", { value: "" }, "—"), ["female", "male", "other"].map((g) => h("option", { value: g }, g[0].toUpperCase() + g.slice(1))));
      } else {
        const spec = INPUT_TYPES[f.key] || {};
        control = h("input.input", { type: spec.type || "text", attrs: { autocomplete: spec.autocomplete || "off", placeholder: f.help || "" } });
        if (f.key === "country_code") control.setAttribute("maxlength", "2");
      }
      control.value = ident.values[f.key] || "";
      inputs[f.key] = control;
      pair.push(field(f.label, control));
      if (pair.length === 2) { rows.push(h("div.field-row", pair)); pair = []; }
    }
    if (pair.length) rows.push(h("div.field-row", pair, h("div")));
    return h("div.form-section", h("h3", heading), rows);
  }

  const secretsBox = h("div");
  function renderSecrets() {
    const sensitive = ((state.meta && state.meta.fields) || []).filter((f) => f.sensitive);
    const rows = sensitive.map((f) => {
      const value = ident.sensitive[f.key];
      const input = h("input.input.mono.sm", {
        type: f.key === "card_number" || f.key === "password" || f.key === "ssn" || f.key === "card_cvv" ? "password" : "text",
        attrs: { autocomplete: "off", "aria-label": `${f.label} (write-only)`, placeholder: value && value !== "missing" ? "Replace…" : f.help || "Enter…",
          inputmode: ["card_number", "card_cvv", "card_exp_month", "card_exp_year", "ssn"].includes(f.key) ? "numeric" : null },
      });
      const setBtn = h("button.btn.sm", { attrs: { type: "submit" } }, value && value !== "missing" ? "Replace" : "Set");
      const form = h("form", {
        onsubmit: (event) => {
          event.preventDefault();
          if (!input.value) return;
          busy(setBtn, async () => {
            try {
              ident = await api.put(`/api/identities/${enc(id)}/secret/${enc(f.key)}`, { value: input.value });
              input.value = "";
              toast(`${f.label} saved securely.`, { kind: "success" });
              renderSecrets();
              loadIdentities().catch(() => {});
            } catch (err) {
              input.value = "";
              toast(err.message, { kind: "error" });
            }
          });
        },
      }, input, setBtn,
        value ? h("button.btn.sm.ghost", {
          attrs: { type: "button", title: `Clear ${f.label}` },
          onclick: (e) => busy(e.currentTarget, async () => {
            try {
              ident = await api.del(`/api/identities/${enc(id)}/secret/${enc(f.key)}`);
              renderSecrets();
              loadIdentities().catch(() => {});
            } catch (err) { toast(err.message, { kind: "error" }); }
          }),
        }, "Clear") : null);
      return h("div.secret-row",
        h("div.label", f.label, h("span.state", { class: value && value !== "missing" ? "set" : "" }, value === "set" ? "•••• set" : value === "missing" ? "missing, re-enter" : value || "not set")),
        form);
    });
    const card = ident.card ? h("div.cardface",
      h("div.brand-line", h("span", ident.card.brand), h("span", "ProfilePilot")),
      h("div.number", `•••• •••• •••• ${ident.card.last4}`),
      h("div.brand-line", h("span", ident.values.card_name || [ident.values.first_name, ident.values.last_name].filter(Boolean).join(" ") || " "),
        h("span", ident.sensitive.card_exp_month ? "•• / ••" : ""))) : null;
    replace(secretsBox, h("div.form-section",
      h("h3", "Card and sensitive details"),
      h("div.callout", icon("lock"), h("span", "These values are ", h("strong", "write-only"), ": they go straight to your OS keychain and are never shown again, only masked. The AI can fill them only with your approval, and only on the sites below.")),
      card, h("div", rows)));
  }

  const originsBox = h("div");
  function renderOrigins() {
    const input = h("input.input.sm", { attrs: { placeholder: "https://shop.example.com", autocomplete: "off", "aria-label": "Allowed site" } });
    const add = h("button.btn.sm", { attrs: { type: "submit" } }, icon("plus"), "Allow");
    replace(originsBox, h("div.form-section",
      h("h3", "Sites allowed for sensitive autofill"),
      h("p.small.muted", "Card numbers, CVVs, SSNs and passwords are only filled on these exact sites (HTTPS). This protects you if a page tricks the AI into filling a form somewhere else."),
      ident.allowed_origins.length ? h("div.row.wrap", ident.allowed_origins.map((o) => h("span.tag", { style: { height: "26px", fontSize: "12px" } }, h("span.mono", o),
        h("button", { attrs: { type: "button", "aria-label": `Remove ${o}` }, onclick: async () => {
          try { ident = await api.del(`/api/identities/${enc(id)}/origins`, { origin: o }); renderOrigins(); loadIdentities().catch(() => {}); }
          catch (err) { toast(err.message, { kind: "error" }); }
        } }, icon("x"))))) : h("p.small.faint", "No sites yet: sensitive autofill is off for this identity."),
      h("form.row", {
        onsubmit: (event) => {
          event.preventDefault();
          if (!input.value.trim()) return;
          busy(add, async () => {
            try { ident = await api.post(`/api/identities/${enc(id)}/origins`, { origin: input.value.trim() }); renderOrigins(); loadIdentities().catch(() => {}); }
            catch (err) { toast(err.message, { kind: "error" }); }
          });
        },
      }, input, add)));
  }

  const save = h("button.btn.primary", "Save details");
  const header = () => {
    title.textContent = ident.name;
    const used = ident.used_by || [];
    replace(sub, used.length ? h("span", "Used by ", used.map((u, i) => [i ? ", " : "", h("a", { href: "#/profiles", onclick: () => { drawer.close(); openProfileDrawer(u.id); } }, u.name)])) : "Not linked to a profile (link it in a profile's settings)");
  };
  header();
  renderSecrets();
  renderOrigins();
  drawer.body.append(
    h("div.form",
      h("div.form-section", h("h3", "Identity"), field("Name", nameInput), field("Notes", notesInput)),
      plainSection("personal", "Personal"),
      plainSection("address", "Address"),
      plainSection("card", "Card holder"),
      h("div.row", h("span.spacer"), save)),
    secretsBox, originsBox,
    h("div.form-section", h("h3", "Delete"),
      h("div.row", h("span.small.muted.grow", "Removes the identity and its stored card, SSN and password values. Linked profiles are unlinked."),
        h("button.btn.danger", { onclick: async () => {
          const yes = await confirmDialog({ title: `Delete "${ident.name}"?`, message: "Its details and stored secrets are deleted permanently.", confirmLabel: "Delete identity", danger: true });
          if (!yes) return;
          try {
            await api.del(`/api/identities/${enc(id)}`);
            drawer.close();
            toast(`Deleted "${ident.name}".`, { kind: "success" });
            await Promise.all([loadIdentities(), loadProfiles()]);
          } catch (err) { toast(err.message, { kind: "error" }); }
        } }, icon("trash"), "Delete…"))),
  );
  save.addEventListener("click", () => busy(save, async () => {
    const values = {};
    for (const [key, control] of Object.entries(inputs)) {
      const before = ident.values[key] || "";
      const now = control.value.trim();
      if (now !== before) values[key] = now || null;
    }
    try {
      ident = await api.patch(`/api/identities/${enc(id)}`, { name: nameInput.value.trim(), notes: notesInput.value, values });
      header();
      toast("Details saved.", { kind: "success" });
      loadIdentities().catch(() => {});
    } catch (err) {
      toast(err.message, { kind: "error", title: "Could not save" });
    }
  }));
}
