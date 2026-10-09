// Proxies: table with IP-address checks, latency history, bulk import and editing.

import { api, enc } from "./api.js";
import { debounce, fmt, h, icon, replace, sparkline } from "./dom.js";
import { newProfileDialog } from "./profile-dialogs.js";
import { openProfileDrawer } from "./profile-drawer.js";
import {
  cancelProxyTest, isAddressName, loadProfiles, loadProxies, notify, proxyLabel, proxyPlace, secretPlace, startProxyTest, state,
} from "./store.js";
import { busy, changeTracker, chipInput, confirmDialog, countryBadge, emptyState, field, openDialog, openMenu, select, toast } from "./ui.js";

const SCHEMES = [
  { value: "http", label: "HTTP" }, { value: "https", label: "HTTPS" },
  { value: "socks5", label: "SOCKS5" }, { value: "socks4", label: "SOCKS4" },
];

export function createProxiesView() {
  const filters = { q: "", tag: "" };
  const sort = { key: "name", dir: 1 };
  const subtitle = h("p");
  const searchInput = h("input.input", { type: "search", attrs: { placeholder: "Search proxies", "aria-label": "Search proxies", autocomplete: "off" } });
  searchInput.addEventListener("input", () => { filters.q = searchInput.value.trim().toLowerCase(); render(); });
  const tagSelect = h("select.select.sm", { attrs: { "aria-label": "Filter by tag" }, style: { width: "auto", minWidth: "120px" } });
  tagSelect.addEventListener("change", () => { filters.tag = tagSelect.value; render(); });

  const progress = h("div.progress", { attrs: { role: "progressbar", "aria-label": "Proxy test progress", "aria-valuemin": "0" } }, h("div"));
  const progressText = h("span.small");
  const cancelBtn = h("button.btn.xs", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => cancelProxyTest().catch((err) => toast(err.message, { kind: "error" }))) }, "Cancel");
  const progressBox = h("div.test-progress.hidden", { attrs: { role: "status" } }, h("span.spinner.sm"), progressText, h("div.grow", progress), cancelBtn);
  const testAll = h("button.btn", { attrs: { type: "button", title: "Check the IP address and speed of every proxy" }, onclick: () => runTestAll() }, icon("zap"), "Test all");
  const importBtn = h("button.btn.primary", { onclick: () => importDialog(), attrs: { type: "button", title: "Add proxies (N)" } }, icon("plus"), "Add proxies");
  const moreBtn = h("button.btn.icon-only", { attrs: { type: "button", "aria-label": "More proxy actions", title: "More", "aria-haspopup": "menu" } }, icon("more"));
  moreBtn.addEventListener("click", () => openMenu(moreBtn, [
    { label: "Remove failing proxies…", icon: "trash", danger: true, onClick: () => removeFailing() },
  ]));
  const header = h("header.view-header",
    h("div.view-title", h("h1", "Proxies"), subtitle),
    h("div.view-actions", testAll, moreBtn, importBtn));
  const toolbar = h("div.toolbar", h("div.search", icon("search"), searchInput, h("kbd", "/")), tagSelect);
  const body = h("div.view-body");
  const el = h("section.view", { attrs: { "aria-label": "Proxies" } }, header, toolbar, progressBox, body);

  async function runTestAll() {
    if (!state.proxies.length) return;
    try {
      await startProxyTest();
    } catch (err) {
      toast(err.message, { kind: "error" });
    }
  }

  async function testOne(p, button) {
    state.proxyTesting.add(p.id);
    render();
    await busy(button, async () => {
      try {
        const result = await api.post(`/api/proxies/${enc(p.id)}/test`);
        replaceProxy(result.proxy);
        const c = result.proxy.last_check || {};
        toast(c.ok ? `${c.ip}${c.country ? ` · ${c.country}` : ""}${c.city ? `, ${c.city}` : ""} · ${fmt.ms(c.latency_ms)}` : (c.reason || "No answer"),
          { kind: c.ok ? "success" : "error", title: c.ok ? `${proxyLabel(p)} works` : `${proxyLabel(p)} failed`, details: c.ok ? null : c.error });
      } catch (err) {
        toast(err.message, { kind: "error" });
      } finally {
        state.proxyTesting.delete(p.id);
        render();
      }
    });
  }

  function replaceProxy(view) {
    if (!view) return;
    const i = state.proxies.findIndex((x) => x.id === view.id);
    if (i >= 0) state.proxies[i] = { ...state.proxies[i], ...view, used_by: state.proxies[i].used_by };
    notify("proxies");
  }

  function renderProgress() {
    const t = state.proxyTest;
    if (!t || t.finished) {
      progressBox.classList.add("hidden");
      testAll.disabled = false;
      return;
    }
    progressBox.classList.remove("hidden");
    testAll.disabled = true;
    const pct = t.total ? Math.round((t.done / t.total) * 100) : 100;
    progress.firstChild.style.width = `${pct}%`;
    progress.setAttribute("aria-valuemax", String(t.total || 0));
    progress.setAttribute("aria-valuenow", String(t.done || 0));
    progressText.textContent = `Testing proxies… ${t.done} of ${t.total} done`;
  }

  function matches(p) {
    if (filters.tag && !p.tags.some((t) => t.toLowerCase() === filters.tag.toLowerCase())) return false;
    if (!filters.q) return true;
    const c = p.last_check || {};
    return [p.name, proxyLabel(p), p.host, String(p.port), p.scheme, ...p.tags, c.ip, c.country, c.city, c.country_code, ...p.used_by.map((u) => u.name)]
      .filter(Boolean).join(" ").toLowerCase().includes(filters.q);
  }

  function render() {
    if (!state.ready) return;
    const all = state.proxies;
    const okCount = all.filter((p) => p.last_check && p.last_check.ok).length;
    const failed = all.filter((p) => p.last_check && !p.last_check.ok).length;
    subtitle.textContent = all.length
      ? [fmt.plural(all.length, "proxy", "proxies"), okCount ? `${okCount} working` : null, failed ? `${failed} failing` : null].filter(Boolean).join(" · ")
      : "Upstream proxies give each profile its own IP address.";
    const tags = [...new Set(all.flatMap((p) => p.tags))].sort();
    replace(tagSelect, h("option", { value: "" }, "All tags"), ...tags.map((t) => h("option", { value: t }, t)));
    tagSelect.value = tags.includes(filters.tag) ? filters.tag : "";
    tagSelect.classList.toggle("hidden", !tags.length);
    testAll.classList.toggle("hidden", !all.length);
    moreBtn.classList.toggle("hidden", !all.length);
    renderProgress();
    if (!all.length) {
      toolbar.classList.add("hidden");
      replace(body, emptyState({
        icon: "proxies", title: "No proxies yet",
        text: "Paste one or many proxies (HTTP, HTTPS, SOCKS4 or SOCKS5, with or without a password). Each profile can use its own proxy, so every profile gets its own IP address.",
        actions: [h("button.btn.primary", { attrs: { type: "button" }, onclick: () => importDialog() }, icon("plus"), "Add proxies")],
      }));
      return;
    }
    toolbar.classList.remove("hidden");
    const shown = all.filter(matches);
    if (!shown.length) {
      replace(body, emptyState({ icon: "search", title: "No proxies match", text: "Try another search.", actions: [] }));
      return;
    }
    const rows = sortProxies(shown).map((p) => proxyRow(p));
    replace(body, h("div.table-wrap", h("table.table.proxies-table",
      h("thead", h("tr",
        sortable("name", "Name"), h("th.col-type", "Type"), h("th", "Address"), sortable("location", "Location"), h("th", "IP address"),
        sortable("latency", "Latency", "num"), sortable("used", "Used by"), h("th.actions", h("span.sr-only", "Actions")))),
      h("tbody", rows))));
  }

  function sortable(key, label, cls = "") {
    const active = sort.key === key;
    const th = h("th", { class: cls, attrs: { "aria-sort": active ? (sort.dir > 0 ? "ascending" : "descending") : "none" } },
      h("button.th-sort", { attrs: { type: "button" }, onclick: () => {
        sort.dir = sort.key === key ? -sort.dir : 1;
        sort.key = key;
        render();
      } }, label, active ? icon("chevron-down", sort.dir > 0 ? "sort-icon asc" : "sort-icon") : null));
    return th;
  }

  function sortProxies(list) {
    const value = (p) => {
      const c = p.last_check || {};
      if (sort.key === "latency") return c.ok && c.latency_ms != null ? c.latency_ms : Number.MAX_SAFE_INTEGER;
      if (sort.key === "location") return `${c.ok ? (c.country_code || "") : "~"} ${proxyPlace(c)}`.toLowerCase();
      if (sort.key === "used") return -p.used_by.length;
      return proxyLabel(p).toLowerCase();
    };
    return [...list].sort((a, b) => {
      const va = value(a);
      const vb = value(b);
      const cmp = typeof va === "number" && typeof vb === "number" ? va - vb : String(va).localeCompare(String(vb));
      return cmp * sort.dir || a.name.localeCompare(b.name);
    });
  }

  async function removeFailing() {
    const failing = state.proxies.filter((p) => p.last_check && !p.last_check.ok);
    if (!failing.length) {
      toast("No proxy failed its last test.", { kind: "info" });
      return;
    }
    const used = failing.filter((p) => p.used_by.length);
    const yes = await confirmDialog({
      title: `Remove ${fmt.plural(failing.length, "failing proxy", "failing proxies")}?`,
      message: `These proxies failed their last test.${used.length ? ` ${fmt.plural(used.length, "of them is", "of them are")} used by profiles, which will connect directly from their next start.` : ""}`,
      details: h("div.list", { style: { maxHeight: "200px", overflow: "auto" } }, failing.map((p) => h("div.list-item", h("span.scheme", p.scheme),
        h("span.grow.ellipsis", proxyLabel(p)), p.used_by.length ? h("span.badge.amber", `used by ${p.used_by.length}`) : null))),
      confirmLabel: "Remove", danger: true,
    });
    if (!yes) return;
    let removed = 0;
    for (const p of failing) {
      try {
        await api.del(`/api/proxies/${enc(p.id)}${p.used_by.length ? "?force=1" : ""}`);
        removed += 1;
      } catch (err) { /* keep going */ }
    }
    toast(`Removed ${fmt.plural(removed, "proxy", "proxies")}.`, { kind: "success" });
    await Promise.all([loadProxies(), loadProfiles()]);
  }

  function statusCell(p) {
    const c = p.last_check;
    if (state.proxyTesting.has(p.id)) return h("div.cell-main", h("span.badge.violet", h("span.spinner.sm"), "Testing"));
    if (!c) return h("div.cell-main", h("span.badge", "Not tested"));
    if (c.ok) return h("div.cell-main", h("span.mono", c.ip), h("span.cell-sub", fmt.ago(c.checked_at)));
    return h("div.cell-main",
      h("span.badge.red", icon("alert"), "Failed"),
      h("span.cell-sub.reason", c.reason || "No answer"),
      c.error ? h("details.raw-details", h("summary", "Details"), h("code.raw", c.error)) : null,
      h("span.cell-sub", fmt.ago(c.checked_at)));
  }

  function proxyRow(p) {
    const c = p.last_check;
    const testing = state.proxyTesting.has(p.id);
    const latencyClass = !c || c.latency_ms == null ? "" : c.latency_ms < 400 ? "good" : c.latency_ms < 1200 ? "ok" : "slow";
    const testBtn = h("button.btn.xs.ghost.icon-only", { attrs: { type: "button", "aria-label": `Check the IP address of ${proxyLabel(p)}`, title: "Check IP address" } }, icon("zap"));
    testBtn.addEventListener("click", () => testOne(p, testBtn));
    const more = h("button.btn.xs.ghost.icon-only", { attrs: { type: "button", "aria-label": `More actions for ${proxyLabel(p)}`, title: "More", "aria-haspopup": "menu" } }, icon("more"));
    more.addEventListener("click", () => openMenu(more, [
      { label: "Edit…", icon: "edit", onClick: () => editDialog(p) },
      { label: "New profile with this proxy…", icon: "plus", onClick: () => newProfileDialog({ proxyId: p.id, name: isAddressName(p) ? "" : p.name }) },
      { label: "Check IP address", icon: "zap", onClick: () => testOne(p, testBtn) },
      "-",
      { label: "Delete…", icon: "trash", danger: true, onClick: () => deleteProxy(p) },
    ]));
    const unnamed = isAddressName(p);
    const place = proxyPlace(c);
    return h("tr", { class: testing ? "testing" : "" },
      h("td", h("div.cell-main",
        h("span", { class: unnamed ? "muted" : "", style: { fontWeight: unnamed ? "450" : "550" }, attrs: { title: unnamed ? "Not named yet (Edit to name it)" : p.name } }, proxyLabel(p)),
        p.tags.length ? h("div.row.wrap", { style: { gap: "4px" } }, p.tags.map((t) => h("span.tag", t))) : null)),
      h("td.col-type", h("span.scheme", p.scheme)),
      h("td", h("div.cell-main",
        h("span.mono.addr", h("span.scheme.inline-scheme", p.scheme), `${p.host}:${p.port}`),
        h("span.cell-sub", p.has_username ? `user ${p.username}${p.has_password ? " · password saved" : ""}` : "no login"))),
      h("td.loc", c && c.ok ? h("div.row", { attrs: { title: [c.city, c.region, c.country].filter(Boolean).join(", ") } }, countryBadge(c.country_code),
        h("span.ellipsis", place || "—")) : h("span.faint", "—")),
      h("td", statusCell(p)),
      h("td.num", h("div.latency", sparkline(p.history, 54, 20), h("span.ms", { class: latencyClass }, c && c.ok && c.latency_ms != null ? fmt.ms(c.latency_ms) : "—"))),
      h("td", p.used_by.length ? h("div.row.wrap", { style: { gap: "4px" } }, p.used_by.slice(0, 3).map((u) => h("button.chip", { attrs: { type: "button" }, onclick: () => openProfileDrawer(u.id) }, h("span.chip-text", u.name))),
        p.used_by.length > 3 ? h("span.tag", `+${p.used_by.length - 3}`) : null) : h("span.faint", "—")),
      h("td.actions", testBtn, more),
    );
  }

  async function deleteProxy(p) {
    const used = p.used_by.length;
    const yes = await confirmDialog({
      title: `Delete "${proxyLabel(p)}"?`,
      message: used ? `It is used by ${p.used_by.map((u) => u.name).join(", ")}. Those profiles will connect directly (no proxy) from their next start.`
        : "The proxy and its saved password are removed.",
      confirmLabel: "Delete proxy", danger: true,
    });
    if (!yes) return;
    try {
      await api.del(`/api/proxies/${enc(p.id)}${used ? "?force=1" : ""}`);
      toast(`Deleted "${proxyLabel(p)}".`, { kind: "success" });
      await Promise.all([loadProxies(), loadProfiles()]);
    } catch (err) {
      toast(err.message, { kind: "error" });
    }
  }

  return {
    el,
    title: "Proxies",
    update(topics) {
      if (topics.has("proxy-test") || topics.has("proxies") || topics.has("profiles") || topics.has("ready")) render();
    },
    onShow() { render(); },
    focusSearch() { searchInput.focus(); },
    newItem() { importDialog(); },
  };
}

// ------------------------------------------------------------------ dialogs

export function importDialog() {
  const text = h("textarea.textarea.mono", {
    attrs: { rows: "8", spellcheck: "false", autocomplete: "off", placeholder: "socks5://user:pass@proxy.example.net:1080\nproxy.example.net:8080:user:pass  # Berlin\nuser:pass@203.0.113.10:3128\n203.0.113.11:8000" },
  });
  const scheme = select(SCHEMES, "http", {});
  const tags = chipInput([], { placeholder: "Tags for all of them, e.g. residential, de" });
  const testAfter = h("input", { type: "checkbox", checked: true });
  const preview = h("div.stack", { attrs: { "aria-live": "polite" } });
  const add = h("button.btn.primary", { disabled: true, attrs: { type: "button" } }, "Add proxies");
  const runPreview = debounce(async () => {
    const value = text.value;
    if (!value.trim()) {
      replace(preview, h("div.hint.small.faint", "One proxy per line. Lines starting with # are ignored; text after \" # \" names the proxy."));
      add.disabled = true;
      add.textContent = "Add proxies";
      return;
    }
    try {
      const result = await api.post("/api/proxies/parse", { text: value, scheme: scheme.value });
      add.disabled = result.valid === 0;
      add.textContent = result.valid ? `Add ${fmt.plural(result.valid, "proxy", "proxies")}` : "Add proxies";
      const list = h("div.list", { style: { maxHeight: "220px", overflow: "auto" } }, result.lines.slice(0, 200).map((line) => h("div.list-item",
        line.ok ? h("span.badge.green", { attrs: { "aria-label": "Readable" } }, icon("check")) : h("span.badge.red", { attrs: { "aria-label": "Can't be read" } }, icon("x")),
        h("span.faint.small.mono", { style: { width: "42px" } }, `#${line.line}`),
        line.ok ? h("span.scheme", line.scheme) : null,
        line.ok ? h("span.grow.ellipsis.mono.small", `${line.host}:${line.port}${line.username ? `  ·  ${line.username}${line.has_password ? " / ••••" : ""}` : ""}`)
          : h("span.grow.ellipsis.small", { style: { color: "var(--red)" } }, "Can't read this line"),
        line.name ? h("span.tag", line.name) : null)));
      replace(preview,
        h("div.row.small", h("strong", `${result.valid} valid`), result.invalid ? h("span.badge.red", `${result.invalid} can't be read`) : null),
        list,
        result.total > 200 ? h("div.hint.small.faint", `Showing the first 200 of ${result.total} lines.`) : null);
    } catch (err) {
      replace(preview, h("div.callout.warn", icon("alert"), err.message));
    }
  }, 250);
  text.addEventListener("input", runPreview);
  scheme.addEventListener("change", runPreview);
  runPreview();

  const changes = changeTracker(() => JSON.stringify([text.value.trim(), tags.pending]));
  const dlg = openDialog({
    title: "Add proxies", size: "wide",
    description: `Formats: scheme://user:pass@host:port, user:pass@host:port, host:port or host:port:user:pass. Passwords go to ${secretPlace()} and are never shown again.`,
    body: h("div.form",
      field("Proxies", text),
      h("div.field-row", field("Type for lines without a scheme", scheme), field("Tags", tags)),
      h("div.field", h("span.field-label", "Preview"), preview)),
    isDirty: () => changes.dirty(),
    footer: [h("label.checkbox", testAfter, h("span", "Test them after adding")), h("span.spacer"),
      h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.close() }, "Cancel"), add],
  });
  add.addEventListener("click", () => busy(add, async () => {
    try {
      const result = await api.post("/api/proxies", { text: text.value, scheme: scheme.value, tags: tags.values });
      text.value = "";
      dlg.forceClose();
      const parts = [`${result.created} new`];
      if (result.existing) parts.push(`${result.existing} already saved`);
      if (result.errors.length) parts.push(`${result.errors.length} skipped`);
      const ids = result.added.map((p) => p.id);
      const test = () => startProxyTest(ids).catch((err) => toast(err.message, { kind: "error" }));
      await loadProxies();
      if (testAfter.checked && ids.length) {
        test();
        toast(`${parts.join(" · ")}. Testing them now.`, { kind: result.errors.length ? "warn" : "success", title: "Proxies added",
          details: result.errors.length ? result.errors.join("\n") : null });
      } else {
        toast(parts.join(" · "), { kind: result.errors.length ? "warn" : "success", title: "Proxies added",
          details: result.errors.length ? result.errors.join("\n") : null,
          action: ids.length ? { label: "Test them", onClick: test } : null });
      }
    } catch (err) {
      toast(err.message, { kind: "error", title: "Nothing was added" });
    }
  }));
}

export function editDialog(p) {
  const name = h("input.input", { value: isAddressName(p) ? "" : p.name, attrs: { maxlength: "64", placeholder: `${p.host}:${p.port}` } });
  const scheme = select(SCHEMES, p.scheme, {});
  const host = h("input.input.mono", { value: p.host, attrs: { autocomplete: "off", spellcheck: "false" } });
  const port = h("input.input.mono", { value: String(p.port), type: "number", attrs: { min: "1", max: "65535" } });
  const username = h("input.input.mono", { attrs: { autocomplete: "off", spellcheck: "false", placeholder: p.has_username ? `${p.username} (saved; type to replace)` : "No username" } });
  const password = h("input.input.mono", { type: "password", attrs: { autocomplete: "new-password", placeholder: p.has_password ? "•••••••• saved; type to replace" : "No password" } });
  const clearPw = h("input", { type: "checkbox" });
  const tags = chipInput(p.tags);
  const notes = h("textarea.textarea", { value: p.notes || "", attrs: { rows: "2" } });
  const save = h("button.btn.primary", { attrs: { type: "button" } }, "Save");
  const changes = changeTracker(() => JSON.stringify([name.value.trim(), scheme.value, host.value.trim(), port.value, username.value, password.value,
    clearPw.checked, tags.pending, notes.value]));
  const dlg = openDialog({
    title: `Edit "${proxyLabel(p)}"`,
    body: h("form.form", { onsubmit: (event) => { event.preventDefault(); save.click(); } },
      field("Name", name, { hint: isAddressName(p) ? "Leave empty to keep using the address as its name." : null }),
      h("div.field-row.addr", field("Type", scheme), field("Host", host), field("Port", port)),
      h("div.field-row", field("Username", username), field("Password", password)),
      p.has_password ? h("label.checkbox", clearPw, h("span", "Remove the saved password")) : null,
      h("div.hint.small.faint", `The saved username and password are never shown. Leave the fields empty to keep them. Passwords go to ${secretPlace()}.`),
      field("Tags", tags), field("Notes", notes)),
    isDirty: () => changes.dirty(),
    footer: [h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.close() }, "Cancel"), save],
  });
  save.addEventListener("click", () => busy(save, async () => {
    const payload = { tags: tags.values, notes: notes.value };
    if (name.value.trim()) payload.name = name.value.trim();
    const endpointChanged = scheme.value !== p.scheme || host.value.trim() !== p.host || Number(port.value) !== p.port
      || username.value.trim() || password.value || clearPw.checked;
    if (endpointChanged) {
      Object.assign(payload, { scheme: scheme.value, host: host.value.trim(), port: Number(port.value) });
      if (username.value.trim()) payload.username = username.value.trim();
      if (password.value) payload.password = password.value;
      if (clearPw.checked) payload.clear_password = true;
    }
    try {
      const result = await api.patch(`/api/proxies/${enc(p.id)}`, payload);
      password.value = "";
      dlg.forceClose();
      toast(result.notes && result.notes.length ? result.notes.join(" ") : "Proxy saved.", { kind: "success" });
      await loadProxies();
    } catch (err) {
      password.value = "";
      toast(err.message, { kind: "error", title: "Could not save" });
    }
  }));
}
