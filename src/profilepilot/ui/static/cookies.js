// Cookies of a running profile: the drawer's Cookies tab, the cookie editor and the import dialog.
// The live browser is the source of truth (docs/design/COOKIES.md). Cookie names and values are always
// set as text (never markup), and values stay masked until the user reveals them.

import { api, enc } from "./api.js";
import { clear, copyText, debounce, fmt, h, icon, isTyping, replace, saveFile } from "./dom.js";
import { actions, state } from "./store.js";
import {
  busy, changeTracker, choiceCards, confirmDialog, copyButton, emptyState, field, nextId, openDialog, openMenu, segmented, select,
  setError, toast, toggle,
} from "./ui.js";

const MASK = "••••••••";
const MAX_NAME_VALUE = 4096; // bytes of name + value Chrome accepts
const MAX_PATH = 1024;
const MAX_IMPORT = 8 * 1024 * 1024;
const MAX_DAYS = 400; // Chrome shortens longer expiries to this
const AUTO_OPEN = 250; // up to this many rows are shown without opening their site; larger jars open per site

const SAME_SITE_HINTS = {
  "": "Not set: Chrome treats it as Lax.",
  Lax: "Lax: sent when you open the site, also from a link elsewhere, but not with requests other sites make in the background.",
  Strict: "Strict: only sent when the request starts on this site. Arriving from a link on another site comes without it.",
  None: "None: sent with every request, also when other sites embed this one. Needs Secure.",
};
const FORMAT_LABELS = { json: "JSON", netscape: "cookies.txt" };

const LABEL = /^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$/;
const IPV4 = /^\d{1,3}(?:\.\d{1,3}){3}$/;
const PATH = /^\/[A-Za-z0-9\-._~!$&'()*+,=:@%/]*$/;
const BAD_ESCAPE = /%(?![0-9A-Fa-f]{2})/;
const CONTROL = /[\x00-\x1f\x7f]/;

const encoder = new TextEncoder();
const bytes = (text) => encoder.encode(String(text || "")).length;
const num = (n) => Number(n || 0).toLocaleString();
/** "1,284 cookies" (fmt.plural without thousands separators would read "1284"). */
const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : many || `${one}s`}`;
const cap = (text) => (text ? text[0].toUpperCase() + text.slice(1) : "");
const rtf = typeof Intl !== "undefined" && Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat(undefined, { numeric: "always" }) : null;

/** ".example.com" -> "example.com" (host-only and domain cookies of one host form one site). */
export function siteOf(domain) {
  return String(domain || "").replace(/^\./, "").toLowerCase();
}

/** Is a cookie of `domain` one of `site`'s (the site itself or a subdomain)? */
function inSite(domain, site) {
  const d = siteOf(domain);
  return d === site || d.endsWith(`.${site}`);
}

function shorten(text, max = 60) {
  const s = String(text || "");
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** "in 3 days" for a number of seconds ahead. */
function inTime(seconds) {
  const table = [[3600, "minute", 60], [86400, "hour", 3600], [86400 * 45, "day", 86400], [86400 * 330, "month", 2629800], [Infinity, "year", 31557600]];
  for (const [limit, unit, div] of table) {
    if (seconds < limit) {
      const n = Math.max(1, Math.round(seconds / div));
      return rtf ? rtf.format(n, unit) : `in ${n} ${unit}${n === 1 ? "" : "s"}`;
    }
  }
  return "";
}

/** {text, cls, title} for a cookie's expiry: "Session", "in 3 days", "Expired". */
export function expiryLabel(c) {
  if (c.session || !c.expires) return { text: "Session", cls: "session", title: "Deleted when the browser session ends" };
  const left = (Date.parse(c.expires) - Date.now()) / 1000;
  if (Number.isNaN(left)) return { text: "—", cls: "", title: "" };
  if (left <= 0) return { text: "Expired", cls: "expired", title: `Expired ${fmt.dateTime(c.expires, true)}` };
  return { text: inTime(left), cls: "", title: `Expires ${fmt.dateTime(c.expires, true)}` };
}

function flagPills(c) {
  const site = siteOf(c.domain);
  const pills = [];
  const pill = (text, title, cls = "") => h("span.ck-flag", { class: cls, attrs: { title } }, text);
  if (!c.host_only) pills.push(pill("Subdomains", `Domain cookie: also sent to every subdomain of ${site}`, "scope"));
  if (c.http_only) pills.push(pill("HttpOnly", "HttpOnly: the page's scripts can't read it"));
  if (c.secure) pills.push(pill("Secure", "Secure: only sent over HTTPS"));
  if (c.same_site) pills.push(pill(c.same_site, `SameSite=${SAME_SITE_HINTS[c.same_site] || c.same_site}`));
  if (c.partitioned) {
    pills.push(pill("Partitioned", c.partition_site ? `Partitioned (CHIPS): only used while ${c.partition_site} is the site in the address bar`
      : "Partitioned for an opaque site (e.g. a sandboxed frame): Chrome doesn't let it be changed", "partition"));
  }
  return pills;
}

/** The fields the API takes to write `c` again (e.g. to undo a deletion). */
function cookieBody(c) {
  const keys = ["name", "value", "domain", "host_only", "path", "expires", "session", "http_only", "secure", "same_site", "priority",
    "partitioned", "partition_site", "partition_cross_site"];
  return Object.fromEntries(keys.map((k) => [k, c[k] === undefined ? null : c[k]]));
}

function isReadOnly(c) {
  return !!(c && c.partitioned && !c.partition_site);
}

function sitesText(domains, max = 3) {
  const names = domains.map((d) => d.domain || d);
  if (names.length <= max) return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}` : names[0] || "";
  return `${names.slice(0, max).join(", ")} and ${names.length - max} more`;
}

// ------------------------------------------------------------------ the Cookies tab

/**
 * The Cookies tab of profile `id`: {el, show(p), update(p)}. show() runs when the tab opens (and
 * reloads the jar); update() follows the profile's state (started / stopped) while the tab is open.
 */
export function createCookiesPane(id) {
  const st = {
    cookies: [], domains: [], total: 0, loaded: false, error: null, notRunning: false, seq: 0,
    q: "", site: "", shown: [],
    selected: new Set(), revealed: new Set(), revealAll: false,
    expanded: new Map(), // site -> open (the user's choice wins over the automatic one)
    lastKey: null, flash: null, running: null, pending: null,
  };
  const rows = new Map(); // key -> {row, check, paintValue} of the rendered rows
  const groupChecks = []; // [{check, keys}]

  const search = h("input.input", { type: "search", attrs: { placeholder: "Search names, sites and values", "aria-label": "Search cookies", autocomplete: "off", spellcheck: "false" } });
  search.addEventListener("input", debounce(() => { st.q = search.value.trim().toLowerCase(); filtersChanged(); }, 120));
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !search.value) return;
    event.preventDefault(); // (clears the search; the next Esc closes the drawer)
    event.stopPropagation();
    search.value = "";
    st.q = "";
    filtersChanged();
  });
  const siteSelect = h("select.select.sm.ck-site-select", { attrs: { "aria-label": "Show the cookies of one site" } });
  siteSelect.addEventListener("change", () => { st.site = siteSelect.value; filtersChanged(); });
  const revealBtn = h("button.btn.sm.ck-reveal", { attrs: { type: "button", "aria-pressed": "false", title: "Show or hide every value" } });
  revealBtn.addEventListener("click", () => {
    st.revealAll = !st.revealAll;
    st.revealed = st.revealAll ? new Set(st.cookies.map((c) => c.key)) : new Set();
    paintReveal();
    for (const r of rows.values()) r.paintValue();
  });
  const refreshBtn = h("button.btn.sm.ghost.icon-only", { attrs: { type: "button", "aria-label": "Refresh cookies", title: "Refresh (shows what the AI or websites changed)" } }, icon("refresh"));
  refreshBtn.addEventListener("click", () => busy(refreshBtn, () => load()));
  const importBtn = h("button.btn.sm", { attrs: { type: "button", title: "Import cookies from a file or text" }, onclick: () => importCookiesDialog(id, { total: st.total, onDone: () => load() }) },
    icon("import"), h("span.ck-btn-label", "Import"));
  const exportBtn = h("button.btn.sm", { attrs: { type: "button", title: "Save cookies to a file", "aria-haspopup": "menu" } }, icon("export"), h("span.ck-btn-label", "Export"));
  exportBtn.addEventListener("click", () => openMenu(exportBtn, exportItems()));
  const moreBtn = h("button.btn.sm.icon-only", { attrs: { type: "button", "aria-label": "More cookie actions", title: "More", "aria-haspopup": "menu" } }, icon("more"));
  moreBtn.addEventListener("click", () => openMenu(moreBtn, [
    { label: "Open every site", icon: "chevron-down", onClick: () => expandAll(true) },
    { label: "Close every site", icon: "chevron-right", onClick: () => expandAll(false) },
    "-",
    st.site ? { label: `Clear the cookies of ${st.site}…`, icon: "trash", danger: true, onClick: () => clearCookies(st.site) } : null,
    { label: "Clear all cookies…", icon: "trash", danger: true, onClick: () => clearCookies(null) },
  ]));
  const addBtn = h("button.btn.sm.primary", { attrs: { type: "button", title: "Add a cookie" }, onclick: () => openEditor(null, "add") }, icon("plus"), h("span.ck-btn-label", "Add"));
  const summary = h("span.ck-summary", { attrs: { "aria-live": "polite" } });
  const actionsRow = h("div.ck-actions", summary, h("span.spacer"), refreshBtn, importBtn, exportBtn, moreBtn, addBtn);

  const selCount = h("strong");
  const selAll = h("button.btn.xs.ghost", { attrs: { type: "button" }, onclick: () => { for (const c of st.shown) if (!isReadOnly(c)) st.selected.add(c.key); syncSelection(); } });
  const selExport = h("button.btn.sm", { attrs: { type: "button", "aria-haspopup": "menu" } }, icon("export"), "Export");
  selExport.addEventListener("click", () => openMenu(selExport, [
    { label: "Selected as JSON", icon: "file", onClick: () => exportCookies({ keys: [...st.selected] }, "json") },
    { label: "Selected as cookies.txt", icon: "file", onClick: () => exportCookies({ keys: [...st.selected] }, "netscape") },
  ]));
  const selDelete = h("button.btn.sm.danger", { attrs: { type: "button" } }, icon("trash"), "Delete");
  selDelete.addEventListener("click", () => deleteSelected(selDelete));
  const selClear = h("button.btn.sm.ghost.icon-only", { attrs: { type: "button", "aria-label": "Clear the selection", title: "Clear the selection" }, onclick: () => { st.selected.clear(); syncSelection(); } }, icon("x"));
  const selectionRow = h("div.ck-actions.ck-selection.hidden", { attrs: { role: "region", "aria-label": "Selected cookies" } },
    icon("check"), selCount, selAll, h("span.spacer"), selExport, selDelete, selClear);

  const filterRow = h("div.ck-filters", h("div.search", icon("search"), search), siteSelect, revealBtn);
  const toolbar = h("div.ck-toolbar", actionsRow, selectionRow, filterRow);
  const listHost = h("div.ck-body");
  const el = h("div.ck-pane", toolbar, listHost);
  el.addEventListener("keydown", (event) => {
    if (event.key === "/" && !isTyping(event.target) && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      search.focus();
    }
  });

  // ------------------------------------------------------------ loading

  async function load() {
    const seq = ++st.seq;
    if (!st.loaded) replace(listHost, h("div.ck-loading", h("div.skeleton.skeleton-line"), h("div.skeleton.skeleton-line"), h("div.skeleton.skeleton-line")));
    if (!st.loaded) toolbar.classList.add("hidden");
    let data;
    try {
      data = await api.get(`/api/profiles/${enc(id)}/cookies`);
    } catch (err) {
      if (seq !== st.seq) return;
      if (err.code === "not_running") st.notRunning = true;
      else if (st.loaded) toast(err.message, { kind: "error", title: "Couldn't refresh the cookies" }); // (the list stays)
      else st.error = err;
      render();
      return;
    }
    if (seq !== st.seq) return;
    st.cookies = data.cookies.map((c) => ({ ...c, hay: `${c.name}\n${c.domain}\n${c.value}`.toLowerCase() }));
    st.domains = data.domains;
    st.total = data.total;
    st.loaded = true;
    st.error = null;
    st.notRunning = false;
    const keys = new Set(st.cookies.map((c) => c.key));
    st.selected = new Set([...st.selected].filter((k) => keys.has(k)));
    st.revealed = st.revealAll ? keys : new Set([...st.revealed].filter((k) => keys.has(k)));
    fillSites();
    render();
  }

  function fillSites() {
    const options = [h("option", { value: "" }, `All sites (${num(st.domains.length)})`),
      ...st.domains.map((d) => h("option", { value: d.domain }, `${d.domain} (${num(d.count)})`))];
    if (st.site && !st.domains.some((d) => d.domain === st.site)) options.push(h("option", { value: st.site }, `${st.site} (0)`));
    replace(siteSelect, ...options);
    siteSelect.value = st.site;
  }

  // ------------------------------------------------------------ rendering

  function profile() {
    return state.profiles.get(id);
  }

  function render() {
    const p = profile();
    const running = p && p.state === "running";
    if (!running || st.notRunning) {
      toolbar.classList.add("hidden");
      replace(listHost, notRunningState(p));
      return;
    }
    if (st.error && !st.loaded) {
      toolbar.classList.add("hidden");
      replace(listHost, errorState(st.error));
      return;
    }
    if (!st.loaded) return; // the skeleton is up
    toolbar.classList.remove("hidden");
    renderList();
  }

  function notRunningState(p) {
    const pending = state.pending.get(id) || (p && p.state === "starting" ? "starting" : null);
    if (pending === "starting") {
      return h("div.empty", h("div.empty-icon", icon("cookie")), h("h2", "Starting the browser…"),
        h("p", "The cookies show up as soon as it is ready."), h("div.row", h("span.spinner")));
    }
    const bg = h("button.btn.primary", { attrs: { type: "button" } }, icon("play"), "Start in background");
    bg.addEventListener("click", () => busy(bg, () => actions.start(id, null, { background: true }).catch(() => {})));
    const normal = h("button.btn", { attrs: { type: "button" } }, "Start");
    normal.addEventListener("click", () => busy(normal, () => actions.start(id).catch(() => {})));
    return emptyState({
      icon: "cookie", title: "Start the profile to manage its cookies",
      text: "Chrome keeps cookies encrypted on disk, so they are read and changed in the running browser. Start in background runs it out of sight, without a window on your screen.",
      actions: [bg, normal],
    });
  }

  function errorState(err) {
    const retry = h("button.btn.sm", { attrs: { type: "button" } }, icon("refresh"), "Try again");
    retry.addEventListener("click", () => busy(retry, () => load()));
    return h("div.callout.warn", icon("alert"), h("div.stack.grow", { style: { gap: "6px" } },
      h("strong", "Couldn't read the cookies"), h("span", err.message), h("div.row", retry)));
  }

  function filtered() {
    let list = st.cookies;
    if (st.site) list = list.filter((c) => inSite(c.domain, st.site));
    if (st.q) list = list.filter((c) => c.hay.includes(st.q));
    return list;
  }

  function filtersChanged() {
    st.shown = filtered();
    const shownKeys = new Set(st.shown.map((c) => c.key));
    st.selected = new Set([...st.selected].filter((k) => shownKeys.has(k))); // never act on what is not visible
    renderList(true);
  }

  function renderSummary() {
    const filtering = st.q || st.site;
    summary.textContent = filtering ? `${num(st.shown.length)} of ${plural(st.total, "cookie")}` : `${plural(st.total, "cookie")} on ${plural(st.domains.length, "site")}`;
    exportBtn.disabled = !st.total;
    moreBtn.disabled = !st.total;
    filterRow.classList.toggle("hidden", !st.total);
  }

  function paintReveal() {
    revealBtn.setAttribute("aria-pressed", String(st.revealAll));
    replace(revealBtn, icon(st.revealAll ? "eye-off" : "eye"), st.revealAll ? "Hide values" : "Show values");
  }

  function renderList(keepScroll = true) {
    st.shown = filtered();
    renderSummary();
    paintReveal();
    const scroller = listHost.querySelector(".ck-scroll");
    const top = keepScroll && scroller ? scroller.scrollTop : 0;
    rows.clear();
    groupChecks.length = 0;
    if (!st.cookies.length) {
      replace(listHost, emptyState({
        icon: "cookie", title: "No cookies yet",
        text: "Sites set cookies as this profile browses. You can also add one, or import an export from another browser or tool.",
        actions: [h("button.btn.primary", { attrs: { type: "button" }, onclick: () => importCookiesDialog(id, { total: 0, onDone: () => load() }) }, icon("import"), "Import cookies"),
          h("button.btn", { attrs: { type: "button" }, onclick: () => openEditor(null, "add") }, icon("plus"), "Add a cookie")],
      }));
      syncSelection();
      return;
    }
    if (!st.shown.length) {
      replace(listHost, emptyState({
        icon: "search", compact: true, title: "No cookies match",
        text: st.q ? "Nothing has this text in its name, site or value." : "This site has no cookies right now.",
        actions: [h("button.btn.sm", { attrs: { type: "button" }, onclick: () => resetFilters() }, "Show all cookies")],
      }));
      syncSelection();
      return;
    }
    const groups = [];
    for (const c of st.shown) {
      const site = siteOf(c.domain);
      const last = groups[groups.length - 1];
      if (last && last.site === site) last.cookies.push(c);
      else groups.push({ site, cookies: [c] });
    }
    const filtering = !!(st.q || st.site);
    const openAll = st.shown.length <= AUTO_OPEN || groups.length === 1;
    let budget = AUTO_OPEN;
    const headCheck = h("input.ck-check", { type: "checkbox", attrs: { "aria-label": "Select every cookie shown" } });
    headCheck.addEventListener("change", () => {
      for (const c of st.shown) if (!isReadOnly(c)) { if (headCheck.checked) st.selected.add(c.key); else st.selected.delete(c.key); }
      syncSelection();
    });
    groupChecks.push({ check: headCheck, keys: st.shown.filter((c) => !isReadOnly(c)).map((c) => c.key) });
    const head = h("div.ck-head", headCheck, h("span", "Name"), h("span.ck-col-value", "Value"), h("span.ck-col-path", "Path"),
      h("span.ck-col-exp", "Expires"), h("span.ck-col-flags", "Attributes"), h("span"));
    const scroll = h("div.ck-scroll", { attrs: { role: "region", "aria-label": "Cookies by site", tabindex: "-1" } }, head);
    for (const g of groups) {
      let open;
      if (st.expanded.has(g.site)) open = st.expanded.get(g.site);
      else if (openAll) open = true;
      else if (filtering && g.cookies.length <= budget) { open = true; budget -= g.cookies.length; }
      else open = false;
      scroll.append(groupEl(g, open));
    }
    replace(listHost, scroll);
    scroll.scrollTop = top;
    syncSelection();
    if (st.flash && rows.has(st.flash)) {
      const row = rows.get(st.flash).row;
      row.classList.add("flash");
      row.scrollIntoView({ block: "nearest" });
      refocus(row.querySelector(".ck-name"));
    }
    st.flash = null;
  }

  function groupEl(g, open) {
    const rowsId = nextId("ck");
    const n = g.cookies.length;
    const rowsEl = h("div.ck-rows", { id: rowsId });
    const keys = g.cookies.filter((c) => !isReadOnly(c)).map((c) => c.key);
    const check = h("input.ck-check", { type: "checkbox", disabled: !keys.length, attrs: { "aria-label": `Select the ${plural(n, "cookie")} of ${g.site}` } });
    check.addEventListener("change", () => {
      for (const k of keys) { if (check.checked) st.selected.add(k); else st.selected.delete(k); }
      syncSelection();
    });
    groupChecks.push({ check, keys });
    const toggleBtn = h("button.ck-group-toggle", { attrs: { type: "button", "aria-expanded": String(open), "aria-controls": rowsId } },
      icon("chevron-right", "ck-chevron"), h("span.ck-site", { attrs: { title: g.site } }, g.site), h("span.ck-count", num(n)));
    const more = h("button.btn.ghost.xs.icon-only", { attrs: { type: "button", "aria-label": `More actions for ${g.site}`, title: "More", "aria-haspopup": "menu" } }, icon("more"));
    more.addEventListener("click", () => openMenu(more, [
      { label: `Add a cookie for ${g.site}…`, icon: "plus", onClick: () => openEditor(null, "add", g.site) },
      { label: "Export as JSON", icon: "export", onClick: () => exportCookies({ domain: g.site }, "json") },
      { label: "Export as cookies.txt", icon: "export", onClick: () => exportCookies({ domain: g.site }, "netscape") },
      "-",
      { label: `Clear ${g.site}…`, icon: "trash", danger: true, onClick: () => clearCookies(g.site) },
    ]));
    const section = h("section.ck-group", { class: open ? "open" : "", attrs: { "aria-label": `${g.site}, ${plural(n, "cookie")}` } },
      h("div.ck-group-head", check, toggleBtn, more), rowsEl);
    const fill = () => replace(rowsEl, ...g.cookies.map(cookieRow));
    if (open) fill();
    toggleBtn.addEventListener("click", () => {
      open = !open;
      st.expanded.set(g.site, open);
      section.classList.toggle("open", open);
      toggleBtn.setAttribute("aria-expanded", String(open));
      if (open) fill();
      else {
        for (const c of g.cookies) rows.delete(c.key);
        clear(rowsEl);
      }
      syncSelection();
    });
    return section;
  }

  function cookieRow(c) {
    const readOnly = isReadOnly(c);
    const check = h("input.ck-check", { type: "checkbox", checked: st.selected.has(c.key), disabled: readOnly,
      attrs: { "aria-label": `Select cookie ${c.name}`, title: readOnly ? "Chrome doesn't let this cookie be changed or deleted" : null } });
    check.addEventListener("click", (event) => onCheck(c, check.checked, event.shiftKey));
    const name = h("button.ck-name", { attrs: { type: "button", title: `${shorten(c.name, 200)} – ${readOnly ? "details" : "edit"}` } }, c.name || "(no name)");
    name.addEventListener("click", () => openEditor(c, readOnly ? "view" : "edit"));
    const value = h("div.ck-value");
    const exp = expiryLabel(c);
    const more = h("button.btn.ghost.xs.icon-only.ck-more", { attrs: { type: "button", "aria-label": `More actions for cookie ${c.name}`, title: "More", "aria-haspopup": "menu" } }, icon("more"));
    more.addEventListener("click", () => openMenu(more, [
      { label: readOnly ? "Details" : "Edit…", icon: readOnly ? "info" : "edit", onClick: () => openEditor(c, readOnly ? "view" : "edit") },
      !readOnly ? { label: "Duplicate…", icon: "clone", onClick: () => openEditor(c, "duplicate") } : null,
      c.value ? { label: "Copy value", icon: "copy", onClick: () => copyText(c.value).then(() => toast("Value copied.", { kind: "success" }), () => toast("Could not copy to the clipboard.", { kind: "error" })) } : null,
      "-",
      !readOnly ? { label: "Delete", icon: "trash", danger: true, onClick: () => deleteOne(c) } : null,
    ]));
    const row = h("div.ck-row", { class: [st.selected.has(c.key) ? "selected" : "", readOnly ? "readonly" : ""] },
      check, name, value,
      h("span.ck-path", { class: c.path === "/" ? "root" : "", attrs: { title: `Path ${c.path}` } }, c.path),
      h("span.ck-exp", { class: exp.cls, attrs: { title: exp.title } }, exp.text),
      h("div.ck-flags", flagPills(c)),
      more);

    function paintValue() {
      const shown = st.revealed.has(c.key);
      if (!c.value) {
        replace(value, h("span.ck-val.empty", "empty"));
        return;
      }
      const eye = h("button.btn.ghost.xs.icon-only", { attrs: { type: "button", "aria-pressed": String(shown), "aria-label": `${shown ? "Hide" : "Show"} the value of ${c.name}`, title: shown ? "Hide value" : "Show value" } },
        icon(shown ? "eye-off" : "eye"));
      eye.addEventListener("click", () => reveal(!shown));
      const text = shown
        ? h("span.ck-val.revealed", { attrs: { title: shorten(c.value, 400) } }, c.value)
        : h("span.ck-val.masked", { attrs: { title: "Show value" }, onclick: () => reveal(true) }, h("span", { attrs: { "aria-hidden": "true" } }, MASK), h("span.sr-only", "hidden"));
      replace(value, text, eye, copyButton(() => c.value, { label: `Copy the value of ${c.name}` }));
    }
    function reveal(on) {
      if (on) st.revealed.add(c.key); else st.revealed.delete(c.key);
      paintValue();
      const eye = value.querySelector("button");
      if (eye) eye.focus();
    }
    paintValue();
    rows.set(c.key, { row, check, paintValue });
    return row;
  }

  function onCheck(c, checked, shift) {
    if (shift && st.lastKey && st.lastKey !== c.key) {
      const order = [...rows.keys()];
      const a = order.indexOf(st.lastKey);
      const b = order.indexOf(c.key);
      if (a >= 0 && b >= 0) {
        const byKey = new Map(st.shown.map((x) => [x.key, x]));
        for (const k of order.slice(Math.min(a, b), Math.max(a, b) + 1)) {
          if (isReadOnly(byKey.get(k))) continue;
          if (checked) st.selected.add(k); else st.selected.delete(k);
        }
      }
    }
    if (checked) st.selected.add(c.key); else st.selected.delete(c.key);
    st.lastKey = c.key;
    syncSelection();
  }

  function syncSelection() {
    for (const [key, r] of rows) {
      const on = st.selected.has(key);
      r.check.checked = on;
      r.row.classList.toggle("selected", on);
    }
    for (const { check, keys } of groupChecks) {
      const n = keys.filter((k) => st.selected.has(k)).length;
      check.checked = !!keys.length && n === keys.length;
      check.indeterminate = n > 0 && n < keys.length;
    }
    const count = st.selected.size;
    const selectable = st.shown.filter((c) => !isReadOnly(c)).length;
    actionsRow.classList.toggle("hidden", count > 0);
    selectionRow.classList.toggle("hidden", count === 0);
    selCount.textContent = `${num(count)} selected`;
    selAll.textContent = `Select all ${num(selectable)} shown`;
    selAll.classList.toggle("hidden", count >= selectable);
  }

  function expandAll(open) {
    for (const d of st.domains) st.expanded.set(d.domain, open);
    if (open && st.shown.length > 2000) toast("Opening every site of a large jar can take a moment.", { kind: "info" });
    renderList();
  }

  function resetFilters() {
    st.q = "";
    st.site = "";
    search.value = "";
    siteSelect.value = "";
    filtersChanged();
    search.focus();
  }

  // ------------------------------------------------------------ actions

  function failed(err, title) {
    if (err.code === "not_running") {
      st.notRunning = true;
      render();
      toast("The profile's browser is not running anymore.", { kind: "error", title });
      return;
    }
    toast(err.message, { kind: "error", title });
  }

  function openEditor(c, mode, site) {
    cookieEditor(id, c, {
      mode, site: site || st.site || "",
      onSaved: (saved) => {
        st.flash = saved.key;
        st.expanded.set(siteOf(saved.domain), true);
        load();
      },
      onDelete: (cookie) => deleteOne(cookie),
    });
  }

  function exportItems() {
    const filtering = st.q || st.site;
    const n = st.shown.length;
    const shownScope = () => (st.q ? { keys: st.shown.filter((c) => !isReadOnly(c)).map((c) => c.key) } : { domain: st.site });
    return [
      { label: "All cookies as JSON", icon: "file", onClick: () => exportCookies({}, "json") },
      { label: "All cookies as cookies.txt", icon: "file", onClick: () => exportCookies({}, "netscape") },
      filtering ? "-" : null,
      filtering ? { label: `The ${plural(n, "cookie")} shown as JSON`, icon: "filter", onClick: () => exportCookies(shownScope(), "json") } : null,
      filtering ? { label: `The ${plural(n, "cookie")} shown as cookies.txt`, icon: "filter", onClick: () => exportCookies(shownScope(), "netscape") } : null,
    ];
  }

  async function exportCookies(scope, format) {
    const body = { format };
    if (scope.domain) body.domain = scope.domain;
    if (scope.keys) body.keys = scope.keys;
    try {
      const { blob, filename, headers } = await api.download(`/api/profiles/${enc(id)}/cookies/export`, body);
      saveFile(blob, filename);
      const count = Number(headers.get("X-Cookie-Count") || 0);
      const omitted = Number(headers.get("X-Cookies-Omitted") || 0);
      const left = omitted ? ` ${plural(omitted, "partitioned cookie was", "partitioned cookies were")} left out${format === "netscape" ? " (cookies.txt can't hold them; JSON can)" : ""}.` : "";
      toast(`${plural(count, "cookie")} saved as ${filename}.${left} Keep the file private: it holds this profile's sign-ins.`,
        { kind: omitted ? "warn" : "success", title: "Cookies exported", timeout: 7000 });
    } catch (err) {
      failed(err, "Could not export the cookies");
    }
  }

  async function deleteOne(c) {
    try {
      await api.del(`/api/profiles/${enc(id)}/cookies`, { keys: [c.key] });
    } catch (err) {
      failed(err, "Could not delete the cookie");
      return;
    }
    const at = [...rows.keys()].indexOf(c.key);
    st.cookies = st.cookies.filter((x) => x.key !== c.key);
    st.selected.delete(c.key);
    st.total = Math.max(0, st.total - 1);
    renderList();
    const next = [...rows.values()][Math.min(Math.max(at, 0), rows.size - 1)]; // the keyboard stays in the list
    refocus(next ? next.row.querySelector(".ck-name") : search);
    toast(`Deleted "${shorten(c.name, 40)}" of ${siteOf(c.domain)}.`, {
      kind: "success",
      action: { label: "Undo", onClick: () => restore(c) },
    });
    load();
  }

  async function restore(c) {
    try {
      const result = await api.post(`/api/profiles/${enc(id)}/cookies`, { cookie: cookieBody(c) });
      st.flash = result.cookie.key;
      st.expanded.set(siteOf(result.cookie.domain), true);
      toast(`"${shorten(c.name, 40)}" is back.`, { kind: "success" });
      load();
    } catch (err) {
      failed(err, "Could not restore the cookie");
    }
  }

  async function deleteSelected(button) {
    const chosen = st.cookies.filter((c) => st.selected.has(c.key));
    if (!chosen.length) return;
    const sites = [...new Set(chosen.map((c) => siteOf(c.domain)))];
    const yes = await confirmDialog({
      title: `Delete ${plural(chosen.length, "cookie")}?`,
      message: `From ${sitesText(sites)}. Sites that use them may sign this profile out or forget its settings. This can't be undone.`,
      details: cookieList(chosen),
      confirmLabel: "Delete", danger: true,
    });
    if (!yes) return;
    await busy(button, async () => {
      try {
        const result = await api.del(`/api/profiles/${enc(id)}/cookies`, { keys: chosen.map((c) => c.key) });
        st.selected.clear();
        toast(`Deleted ${plural(result.deleted, "cookie")}.`, { kind: "success" });
        await load();
        refocus(search);
      } catch (err) {
        failed(err, "Could not delete the cookies");
        load();
      }
    });
  }

  async function clearCookies(site) {
    const affected = site ? st.cookies.filter((c) => inSite(c.domain, site)) : st.cookies;
    if (!affected.length) {
      toast(site ? `${site} has no cookies.` : "This profile has no cookies.", { kind: "info" });
      return;
    }
    const backup = h("button.btn.sm", { attrs: { type: "button" } }, icon("export"), "Export them first");
    backup.addEventListener("click", () => busy(backup, () => exportCookies(site ? { domain: site } : {}, "json")));
    const yes = await confirmDialog({
      title: site ? `Clear the cookies of ${site}?` : "Clear all cookies?",
      message: site
        ? `Deletes ${plural(affected.length, "cookie")} of ${site} and its subdomains. This logs the profile out of these sites and can't be undone.`
        : `Deletes all ${plural(affected.length, "cookie")} of this profile. This logs it out of every site and can't be undone.`,
      details: h("div.row", backup),
      confirmLabel: site ? "Clear cookies" : "Clear all", danger: true,
    });
    if (!yes) return;
    try {
      const result = await api.del(`/api/profiles/${enc(id)}/cookies`, site ? { domain: site } : { all: true });
      toast(`Deleted ${plural(result.deleted, "cookie")}${site ? ` of ${site}` : ""}.`, { kind: "success" });
    } catch (err) {
      failed(err, "Could not clear the cookies");
    }
    await load();
    refocus(search);
  }

  // ------------------------------------------------------------ the drawer's hooks

  function show() {
    const p = profile();
    st.running = !!p && p.state === "running";
    st.pending = state.pending.get(id) || null;
    if (st.running) {
      st.notRunning = false;
      load();
    } else {
      render();
    }
  }

  function update(p) {
    const running = !!p && p.state === "running";
    const pending = state.pending.get(id) || null;
    if (running === st.running && pending === st.pending) return; // (every cookie change republishes the profile)
    st.running = running;
    st.pending = pending;
    if (running) {
      st.notRunning = false;
      load();
    } else {
      st.loaded = false;
      st.cookies = [];
      st.selected.clear();
      render();
    }
  }

  return { el, show, update };
}

/** Move the keyboard focus to `target` when it was lost (its button was re-rendered or its dialog closed). */
function refocus(target) {
  const active = document.activeElement;
  if (target && target.isConnected && (!active || active === document.body || !active.isConnected || active.matches("dialog"))) target.focus();
}

/** Up to 8 cookie labels for a confirmation. */
function cookieList(cookies) {
  const shown = cookies.slice(0, 8);
  return h("div.list.ck-confirm-list",
    shown.map((c) => h("div.list-item", h("span.ck-name-static.ellipsis", c.name), h("span.faint.small.ellipsis", siteOf(c.domain) + (c.path !== "/" ? c.path : "")))),
    cookies.length > shown.length ? h("div.list-item.small.faint", `and ${num(cookies.length - shown.length)} more`) : null);
}

// ------------------------------------------------------------------ the cookie editor

function pad(n) {
  return String(n).padStart(2, "0");
}

/** ISO -> the value of a datetime-local input (local time, seconds). */
function toLocalInput(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fromLocalInput(value) {
  if (!value) return null;
  const d = new Date(value); // (no offset: local time)
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

function daysFromNow(days) {
  const d = new Date(Date.now() + days * 86400000);
  d.setSeconds(0, 0);
  return d.toISOString();
}

/** "Example.COM" / ".example.com" / "https://example.com/x" -> {host, dotted}. */
function parseDomain(text) {
  let host = String(text || "").trim();
  if (host.includes("://")) {
    try { host = new URL(host).hostname; } catch (err) { /* checked below */ }
  }
  const dotted = host.startsWith(".");
  return { host: host.replace(/^\.+/, "").toLowerCase(), dotted };
}

function domainProblem(host, domainScope) {
  if (!host) return "The cookie needs a domain, e.g. example.com.";
  if (host.startsWith("[") && host.endsWith("]")) return domainScope ? "An IP address can only have host-only cookies." : null;
  let ascii = host;
  if (!/^[\x00-\x7f]*$/.test(ascii)) {
    try { ascii = new URL(`http://${host}`).hostname; } catch (err) { return `'${host}' is not a valid domain.`; }
  }
  if (ascii.length > 253 || ascii.endsWith(".") || ascii.includes("..")) return `'${host}' is not a valid domain.`;
  if (IPV4.test(ascii)) {
    if (ascii.split(".").some((part) => Number(part) > 255)) return `'${host}' is not a valid IP address.`;
    return domainScope ? "An IP address can only have host-only cookies." : null;
  }
  if (!ascii.split(".").every((label) => LABEL.test(label))) return `'${host}' is not a valid domain (letters, digits, '-' and '.' only).`;
  if (domainScope && !ascii.includes(".")) return `A cookie for every site under '.${ascii}' is not allowed. Give a site such as example.${ascii}, or pick "This host only".`;
  return null;
}

function pathProblem(path) {
  if (bytes(path) > MAX_PATH) return `The path is too long (at most ${MAX_PATH} characters).`;
  if (!PATH.test(path) || BAD_ESCAPE.test(path)) return "The path must start with / and contain only URL characters (write a space as %20).";
  if (path.split("/").some((seg) => seg === "." || seg === "..")) return "The path can't contain '.' or '..' segments.";
  return null;
}

/** "top.example" / "https://top.example/x" -> "https://top.example", or null. */
function partitionSite(text) {
  let value = String(text || "").trim();
  if (!value) return null;
  if (!value.includes("://")) value = `https://${value}`;
  try {
    const url = new URL(value);
    if ((url.protocol !== "https:" && url.protocol !== "http:") || !url.hostname) return null;
    return `${url.protocol}//${url.hostname}`;
  } catch (err) {
    return null;
  }
}

/**
 * Edit a cookie (`mode` "edit"), add one ("add", optionally for `site`), copy one ("duplicate") or
 * look at one Chrome won't let be changed ("view"). Validation mirrors the server's, so most
 * mistakes are explained before anything is sent.
 */
export function cookieEditor(profileId, cookie, { mode = cookie ? "edit" : "add", site = "", onSaved, onDelete } = {}) {
  const c = cookie || {};
  const readOnly = mode === "view";
  let saveMode = mode;
  let overwriteExisting = false; // set after the user confirms replacing another cookie with this identity
  const formId = nextId("cookie");
  const startDomain = c.domain ? siteOf(c.domain) : site;
  const startSession = mode === "add" ? false : !!(c.session || !c.expires);
  const originalWhen = c.expires ? toLocalInput(c.expires) : "";

  const name = h("input.input.mono", { value: c.name || "", attrs: { autocomplete: "off", spellcheck: "false", placeholder: "e.g. session_id" } });
  const value = h("textarea.textarea.mono.ck-value-input", { value: c.value || "", attrs: { rows: "3", spellcheck: "false", autocomplete: "off", placeholder: "Leave empty for an empty value" } });
  const size = h("span.ck-size");
  const domain = h("input.input.mono", { value: startDomain, attrs: { autocomplete: "off", spellcheck: "false", placeholder: "example.com" } });
  const scope = segmented([{ value: "host", label: "This host only" }, { value: "domain", label: "Host and subdomains" }],
    c.host_only === false ? "domain" : "host", () => sync(), { label: "Sent to" });
  const scopeHint = h("div.hint");
  const path = h("input.input.mono", { value: c.path || "/", attrs: { autocomplete: "off", spellcheck: "false", placeholder: "/" } });
  const expiry = segmented([{ value: "session", label: "When the session ends" }, { value: "date", label: "On a date" }],
    startSession ? "session" : "date", () => sync(), { label: "Expires" });
  const when = h("input.input.ck-when", { type: "datetime-local", value: originalWhen || toLocalInput(daysFromNow(365)), attrs: { step: "1", "aria-label": "Expiry date and time" } });
  const presets = [["+1 day", 1], ["+30 days", 30], ["+1 year", 365]].map(([label, days]) => h("button.btn.xs.ghost", {
    attrs: { type: "button", title: `Expire ${label.slice(1)} from now` }, onclick: () => { when.value = toLocalInput(daysFromNow(days)); sync(); },
  }, label));
  const whenRow = h("div.ck-when-row", when, h("div.row", presets));
  const whenHint = h("div.hint");
  const secure = toggle(!!c.secure, { label: "Secure", onChange: () => sync() });
  const httpOnly = toggle(!!c.http_only, { label: "HttpOnly", onChange: () => sync() });
  const sameSite = segmented([{ value: "", label: "Not set" }, { value: "Lax", label: "Lax" }, { value: "Strict", label: "Strict" }, { value: "None", label: "None" }],
    c.same_site || "", (v) => {
      if (v === "None" && !secure.checked) secure.checked = true; // SameSite=None needs Secure
      sync();
    }, { label: "SameSite" });
  const sameSiteHint = h("div.hint");
  const priority = select([{ value: "Low", label: "Low" }, { value: "Medium", label: "Medium (usual)" }, { value: "High", label: "High" }], c.priority || "Medium", { onChange: () => sync() });
  const partitioned = toggle(!!c.partitioned, { label: "Partitioned (CHIPS)", onChange: () => sync() });
  const partSite = h("input.input.mono", { value: c.partition_site || "", attrs: { autocomplete: "off", spellcheck: "false", placeholder: "https://example.com" } });
  const crossSite = h("input", { type: "checkbox", checked: !!c.partition_cross_site });
  const partFields = h("div.stack.ck-part-fields",
    field("Top-level site", partSite, { hint: "The site in the address bar while this cookie is used (the site that embeds this one)." }),
    h("label.checkbox", crossSite, h("span", "Set inside a cross-site frame (cross-site ancestor)")));
  const problem = h("div.callout.warn.hidden", { attrs: { role: "alert" } });

  const valueLabel = h("div.ck-label-row", h("label", { attrs: { for: nextIdFor(value) } }, "Value"), size);
  const form = h("form.form.ck-form", {
    id: formId,
    onsubmit: (event) => { event.preventDefault(); submit(); },
  },
    problem,
    h("div.field-row", field("Name", name), field("Domain", domain)),
    h("div.field", valueLabel, value),
    h("div.field-row.ck-scope-row",
      h("div.field", h("span.field-label", "Sent to"), scope, scopeHint),
      field("Path", path, { hint: "/ is the whole site." })),
    h("div.field", h("span.field-label", "Expires"), expiry, whenRow, whenHint),
    h("div.field-row",
      h("div.field", secure, h("div.hint", "Only sent over HTTPS.")),
      h("div.field", httpOnly, h("div.hint", "The page's scripts can't read it."))),
    h("div.field", h("span.field-label", "SameSite"), sameSite, sameSiteHint),
    h("details.more-options", { open: !!(c.partitioned || (c.priority && c.priority !== "Medium")) },
      h("summary", icon("chevron-right"), "Advanced", h("span.faint", " · priority, partitioning")),
      h("div.form", { style: { marginTop: "12px" } },
        field("Priority", priority, { hint: "Which cookies Chrome removes last when a site has too many." }),
        h("div.field", partitioned, h("div.hint", "Keeps a separate copy for each site that embeds this one."), partFields))));

  if (readOnly) {
    for (const control of form.querySelectorAll("input, textarea, select, .segmented button, .ck-when-row button")) control.disabled = true;
    problem.classList.remove("hidden");
    replace(problem, icon("info"), h("span", "This cookie is partitioned for an opaque site (for example a sandboxed frame). Chrome doesn't let it be edited, deleted or exported."));
  }

  value.addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); }); // values are one line
  value.addEventListener("input", () => {
    if (/[\r\n]/.test(value.value)) value.value = value.value.replace(/[\r\n]+/g, "");
  });
  name.addEventListener("blur", () => { if (name.value !== name.value.trim()) { name.value = name.value.trim(); sync(); } });
  value.addEventListener("blur", () => { const t = value.value.replace(/^[ \t]+|[ \t]+$/g, ""); if (t !== value.value) { value.value = t; sync(); } });
  domain.addEventListener("blur", () => {
    const { host, dotted } = parseDomain(domain.value);
    if (domain.value && host !== domain.value) domain.value = host;
    if (dotted) scope.value = "domain";
    sync();
  });
  for (const control of [name, value, domain, path, when, partSite]) control.addEventListener("input", () => sync());
  crossSite.addEventListener("change", () => sync());

  let attempted = false;
  const snapshot = () => JSON.stringify([name.value, value.value, domain.value, scope.value, path.value, expiry.value, when.value, secure.checked,
    httpOnly.checked, sameSite.value, priority.value, partitioned.checked, partSite.value, crossSite.checked]);
  const changes = changeTracker(snapshot);

  function problems() {
    const out = [];
    const n = name.value.trim();
    const v = value.value;
    const { host } = parseDomain(domain.value);
    const domainScope = scope.value === "domain";
    if (!n) out.push([name, "The cookie needs a name."]);
    else if (CONTROL.test(n) || n.includes(";") || n.includes("=")) out.push([name, "The name can't contain ';', '=' or control characters."]);
    if (CONTROL.test(v) || v.includes(";")) out.push([value, "The value can't contain ';' or control characters."]);
    else if (bytes(n) + bytes(v) > MAX_NAME_VALUE) out.push([value, `Too large: name and value together can be at most ${num(MAX_NAME_VALUE)} bytes.`]);
    const dp = domainProblem(host, domainScope);
    if (dp) out.push([domain, dp]);
    const pp = pathProblem(path.value.trim() || "/");
    if (pp) out.push([path, pp]);
    if (expiry.value === "date") {
      const iso = fromLocalInput(when.value);
      if (!iso) out.push([when, "Pick when the cookie expires, or let it end with the session."]);
      else if (Date.parse(iso) <= Date.now() && when.value !== originalWhen) out.push([when, "That moment has passed. Pick a future date, or let it end with the session."]);
    }
    if (sameSite.value === "None" && !secure.checked) out.push([sameSite, "SameSite=None needs Secure (or pick Lax or Strict)."]);
    const lower = n.toLowerCase();
    if (lower.startsWith("__secure-") && !secure.checked) out.push([name, "Cookies named __Secure-… must be Secure."]);
    if (lower.startsWith("__host-") && !(secure.checked && !domainScope && (path.value.trim() || "/") === "/")) {
      out.push([name, "Cookies named __Host-… must be Secure, for this host only, with the path /."]);
    }
    if (partitioned.checked) {
      if (!secure.checked) out.push([partitioned.input, "A partitioned cookie must be Secure."]);
      if (!partitionSite(partSite.value)) out.push([partSite, "Give the site it is partitioned for, such as https://example.com."]);
    }
    return out;
  }

  function showProblems(list) {
    for (const control of [name, value, domain, path, when, sameSite, partitioned.input, partSite]) setError(control, "");
    const seen = new Set();
    for (const [control, message] of list) {
      if (seen.has(control)) continue;
      seen.add(control);
      setError(control, message);
    }
  }

  function sync() {
    const total = bytes(name.value.trim()) + bytes(value.value);
    size.textContent = `${num(total)} / ${num(MAX_NAME_VALUE)} bytes`;
    size.classList.toggle("over", total > MAX_NAME_VALUE);
    const { host } = parseDomain(domain.value);
    const shown = host || "the site";
    scopeHint.textContent = scope.value === "domain"
      ? `Sent to ${shown} and every subdomain, e.g. www.${host || "example.com"}.`
      : `Sent to ${shown} only, not to its subdomains.`;
    const dated = expiry.value === "date";
    whenRow.classList.toggle("hidden", !dated);
    if (dated) {
      const iso = fromLocalInput(when.value);
      const left = iso ? (Date.parse(iso) - Date.now()) / 1000 : NaN;
      whenHint.textContent = Number.isNaN(left) ? "" : left <= 0 ? "This moment has passed."
        : left > MAX_DAYS * 86400 ? `${cap(inTime(left))}. Chrome shortens it to ${MAX_DAYS} days from now.`
          : `${cap(inTime(left))} (local time).`;
    } else {
      whenHint.textContent = "Deleted when the browser session ends. Profiles that reopen their tabs keep it.";
    }
    sameSiteHint.textContent = SAME_SITE_HINTS[sameSite.value];
    partFields.classList.toggle("hidden", !partitioned.checked);
    if (attempted) showProblems(problems());
  }

  function payload() {
    const { host } = parseDomain(domain.value);
    const session = expiry.value === "session";
    let expires = null;
    if (!session) expires = c.expires && when.value === originalWhen ? c.expires : fromLocalInput(when.value);
    const part = partitioned.checked;
    return {
      name: name.value.trim(), value: value.value, domain: host, host_only: scope.value === "host", path: path.value.trim() || "/",
      session, expires, secure: secure.checked, http_only: httpOnly.checked, same_site: sameSite.value || null, priority: priority.value,
      partitioned: part, partition_site: part ? partitionSite(partSite.value) : null, partition_cross_site: part ? crossSite.checked : false,
    };
  }

  function showProblem(message, action) {
    replace(problem, icon("alert"), h("div.stack.grow", { style: { gap: "6px" } }, h("span", message),
      action ? h("div.row", h("button.btn.sm", { attrs: { type: "button" }, onclick: action.onClick }, action.label)) : null));
    problem.classList.remove("hidden");
    problem.scrollIntoView({ block: "nearest" });
  }

  async function submit() {
    if (readOnly) return;
    attempted = true;
    const list = problems();
    showProblems(list);
    if (list.length) {
      const first = list[0][0];
      (first.matches && first.matches("input, textarea, select") ? first : first.querySelector("button[tabindex='0'], button") || first).focus();
      return;
    }
    problem.classList.add("hidden");
    const body = { cookie: payload() };
    if (saveMode === "edit") body.replace = c.key;
    if (overwriteExisting) body.overwrite = true;
    await busy(save, async () => {
      try {
        const result = await api.post(`/api/profiles/${enc(profileId)}/cookies`, body);
        changes.reset();
        dlg.forceClose();
        const saved = result.cookie;
        toast(`${saveMode === "edit" ? "Saved" : "Added"} "${shorten(saved.name, 40)}" for ${siteOf(saved.domain)}.`, { kind: "success" });
        if (onSaved) onSaved(saved);
      } catch (err) {
        if (err.code === "not_found" && saveMode === "edit") {
          showProblem("This cookie is gone: the site, the AI or another window changed it in the meantime.",
            { label: "Save it as a new cookie", onClick: () => { saveMode = "add"; submit(); } });
        } else if (err.code === "cookie_exists") {
          showProblem(err.message, { label: "Replace it", onClick: () => { overwriteExisting = true; submit(); } });
        } else if (err.code === "invalid" || err.code === "refused") {
          showProblem(err.code === "refused" ? `Chrome did not accept it: ${err.message}` : err.message);
        } else {
          toast(err.message, { kind: "error", title: "Could not save the cookie" });
        }
      }
    });
  }

  const save = h("button.btn.primary", { attrs: { type: "submit", form: formId } }, mode === "edit" ? "Save" : "Add cookie");
  const del = mode === "edit" && onDelete ? h("button.btn.danger", { attrs: { type: "button" } }, icon("trash"), "Delete") : null;
  if (del) del.addEventListener("click", () => { dlg.forceClose(); onDelete(c); });
  const titles = { edit: "Edit cookie", add: "Add a cookie", duplicate: "Duplicate cookie", view: "Cookie details" };
  const descriptions = {
    edit: `"${shorten(c.name)}" for ${siteOf(c.domain)}. Changes are saved straight into the profile's browser.`,
    add: "It is saved straight into the profile's browser and works like one a website set.",
    duplicate: "Change the name, domain or path for a new cookie; with the same ones it replaces the original.",
    view: `"${shorten(c.name)}" for ${siteOf(c.domain)}.`,
  };
  const dlg = openDialog({
    title: titles[mode], description: descriptions[mode], size: "wide",
    body: form,
    isDirty: () => !readOnly && changes.dirty(),
    footer: readOnly
      ? [h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.forceClose() }, "Close")]
      : [del, h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.close() }, "Cancel"), save],
  });
  dlg.el.classList.add("ck-editor");
  sync();
  if (mode === "edit") { value.focus(); value.setSelectionRange(0, 0); }
  return dlg;
}

/** The id of a form control (assigned now if it has none), for a <label for>. */
function nextIdFor(control) {
  if (!control.id) control.id = nextId("f");
  return control.id;
}

// ------------------------------------------------------------------ the import dialog

/**
 * Import cookies into profile `profileId` from pasted text or a file (JSON in the usual shapes, or
 * cookies.txt), with a live preview. `total` is how many cookies the profile has now (for the
 * "replace everything" confirmation); `onDone(result)` runs after a successful import.
 */
export function importCookiesDialog(profileId, { total = 0, onDone } = {}) {
  let fileText = null;
  let previewSeq = 0;
  let parsed = null; // the latest preview: {format, count, skipped, domains, problems}
  const text = h("textarea.textarea.mono.ck-import-text", {
    attrs: { rows: "7", spellcheck: "false", autocomplete: "off", "aria-label": "Cookies to import",
      placeholder: "Paste a JSON export (Cookie-Editor, EditThisCookie, Playwright, ProfilePilot) or the lines of a cookies.txt file." },
  });
  const fileInput = h("input.sr-only", { type: "file", attrs: { accept: ".json,.txt,.cookies,application/json,text/plain", tabindex: "-1", "aria-hidden": "true" } });
  const pick = h("button.btn.sm", { attrs: { type: "button" }, onclick: () => fileInput.click() }, icon("file"), "Choose a file…");
  const fileChip = h("div.ck-file.hidden");
  const drop = h("div.ck-drop", text, fileChip, h("div.ck-drop-foot", pick, h("span.small.faint", "or drop a file here"), fileInput));
  const format = segmented([{ value: "auto", label: "Detect" }, { value: "json", label: "JSON" }, { value: "netscape", label: "cookies.txt" }],
    "auto", () => runPreview(), { label: "Format" });
  const only = h("input.input.mono", { attrs: { autocomplete: "off", spellcheck: "false", placeholder: "e.g. example.com" } });
  const preview = h("div.ck-preview", { attrs: { "aria-live": "polite" } });
  const mode = choiceCards([
    { value: "merge", title: "Add and update", icon: "plus", desc: "Keeps the other cookies. A cookie with the same name, site and path is overwritten." },
    { value: "replace", title: "Replace these sites", icon: "refresh", desc: "Also deletes the cookies of the imported sites that are not in the file." },
    { value: "replace_all", title: "Replace everything", icon: "trash", desc: "Deletes every cookie that is not in the file. Other sites log out." },
  ], "merge", () => syncButton(), { label: "How to import" });
  const go = h("button.btn.primary", { disabled: true, attrs: { type: "button" } }, icon("import"), "Import");

  const source = () => (fileText != null ? fileText : text.value);
  const onlySite = () => parseDomain(only.value).host;

  function setFile(file, content) {
    fileText = content;
    text.classList.add("hidden");
    const remove = h("button.btn.xs.ghost", { attrs: { type: "button" } }, icon("x"), "Remove");
    remove.addEventListener("click", () => {
      fileText = null;
      fileInput.value = "";
      fileChip.classList.add("hidden");
      text.classList.remove("hidden");
      text.focus();
      runPreview();
    });
    replace(fileChip, icon("file"), h("span.ellipsis.ck-file-name", file.name), h("span.faint.small", fmt.bytes(file.size)), h("span.spacer"), remove);
    fileChip.classList.remove("hidden");
    runPreview();
  }

  async function useFile(file) {
    if (!file) return;
    if (file.size > MAX_IMPORT) {
      toast(`${file.name} is larger than 8 MB.`, { kind: "error", title: "Can't import this file" });
      return;
    }
    let content;
    try {
      content = await file.text();
    } catch (err) {
      toast(`${file.name} could not be read.`, { kind: "error" });
      return;
    }
    setFile(file, content);
  }
  fileInput.addEventListener("change", () => useFile(fileInput.files && fileInput.files[0]));

  function counts(result) {
    const site = onlySite();
    if (!site) return { count: result.count, domains: result.domains, skipped: result.skipped };
    const domains = result.domains.filter((d) => inSite(d.domain, site));
    const count = domains.reduce((sum, d) => sum + d.count, 0);
    return { count, domains, skipped: result.skipped + (result.count - count) };
  }

  function renderPreview() {
    if (!parsed) return;
    const { count, domains, skipped } = counts(parsed);
    const head = h("div.row.wrap.small",
      count ? h("span.badge.green", icon("check"), plural(count, "cookie")) : h("span.badge.red", icon("x"), "Nothing to import"),
      count ? h("span.muted", `on ${plural(domains.length, "site")}`) : null,
      h("span.faint", `· ${FORMAT_LABELS[parsed.format] || parsed.format}`),
      skipped ? h("span.badge.amber", `${num(skipped)} skipped`) : null);
    const chips = domains.length ? h("div.ck-domain-chips", domains.slice(0, 18).map((d) => h("span.tag", d.domain, h("span.faint", num(d.count)))),
      domains.length > 18 ? h("span.tag", `+${num(domains.length - 18)} more`) : null) : null;
    const problems = parsed.problems.length ? h("details.raw-details.ck-problems", { open: parsed.problems.length <= 3 },
      h("summary", `${plural(parsed.problems.length, "problem")}${parsed.skipped > parsed.problems.length ? " (first ones)" : ""}`),
      h("ul", parsed.problems.map((p) => h("li", p)))) : null;
    replace(preview, head, chips, problems);
    syncButton();
  }

  const runPreview = debounce(async () => {
    const value = source();
    const seq = ++previewSeq;
    if (!value.trim()) {
      parsed = null;
      replace(preview, h("p.small.faint", "A preview of what is in the text shows up here. Nothing is written until you click Import."));
      syncButton();
      return;
    }
    if (value.length > MAX_IMPORT) {
      parsed = null;
      replace(preview, h("div.callout.warn", icon("alert"), "That is more than 8 MB of text. Import a smaller file."));
      syncButton();
      return;
    }
    replace(preview, h("div.row.small.muted", h("span.spinner.sm"), "Reading…"));
    try {
      const result = await api.post("/api/cookies/parse", { text: value, format: format.value });
      if (seq !== previewSeq) return;
      parsed = result;
      renderPreview();
    } catch (err) {
      if (seq !== previewSeq) return;
      parsed = null;
      replace(preview, h("div.callout.warn", icon("alert"), err.message));
      syncButton();
    }
  }, 300);

  function syncButton() {
    const count = parsed ? counts(parsed).count : 0;
    go.disabled = !count;
    replace(go, icon("import"), count ? `Import ${plural(count, "cookie")}` : "Import");
    go.classList.toggle("danger", mode.value === "replace_all" && !!count);
    go.classList.toggle("solid", mode.value === "replace_all" && !!count);
    go.classList.toggle("primary", !(mode.value === "replace_all" && count));
  }

  text.addEventListener("input", () => runPreview());
  only.addEventListener("input", debounce(() => { renderPreview(); }, 200));

  const changes = changeTracker(() => JSON.stringify([text.value.trim(), fileText != null, only.value.trim()]));
  const dlg = openDialog({
    title: "Import cookies", size: "wide",
    description: "JSON (Cookie-Editor, EditThisCookie, Playwright storage state, ProfilePilot) or a Netscape cookies.txt file.",
    body: h("div.form",
      drop,
      h("div.field-row",
        h("div.field", h("span.field-label", "Format"), format),
        field("Only the cookies of", only, { hint: "Optional: a site, with its subdomains." })),
      h("div.field", h("span.field-label", "Preview"), preview),
      h("div.field", h("span.field-label", "How to import"), mode)),
    isDirty: () => changes.dirty(),
    footer: [h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.close() }, "Cancel"), go],
  });
  dlg.el.classList.add("ck-import");
  // A file dropped anywhere on the dialog is imported (and never opened by the browser instead).
  dlg.el.addEventListener("dragover", (event) => { event.preventDefault(); drop.classList.add("dragging"); });
  dlg.el.addEventListener("dragleave", (event) => { if (!dlg.el.contains(event.relatedTarget)) drop.classList.remove("dragging"); });
  dlg.el.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("dragging");
    const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) useFile(file);
  });
  runPreview();

  go.addEventListener("click", async () => {
    if (!parsed) return;
    const { count, domains } = counts(parsed);
    const how = mode.value;
    if (how === "replace") {
      const yes = await confirmDialog({
        title: `Replace the cookies of ${plural(domains.length, "site")}?`,
        message: `After the import, cookies of ${sitesText(domains)} (and their subdomains) that are not in the file are deleted. That can log the profile out of these sites.`,
        confirmLabel: "Import and replace", danger: true,
      });
      if (!yes) return;
    } else if (how === "replace_all") {
      const yes = await confirmDialog({
        title: "Replace every cookie?",
        message: `After the import, every cookie of this profile that is not in the file is deleted${total ? ` (it has ${plural(total, "cookie")} now)` : ""}. This logs it out of all other sites.`,
        confirmLabel: "Replace everything", danger: true,
      });
      if (!yes) return;
    }
    await busy(go, async () => {
      const body = { text: source(), mode: how };
      if (format.value !== "auto") body.format = format.value;
      if (onlySite()) body.domain = onlySite();
      try {
        const result = await api.post(`/api/profiles/${enc(profileId)}/cookies/import`, body);
        changes.reset();
        dlg.forceClose();
        const parts = [`Imported ${plural(result.imported, "cookie")}${result.domains.length ? ` on ${plural(result.domains.length, "site")}` : ""}.`];
        if (result.removed) parts.push(`Removed ${plural(result.removed, "older cookie")}.`);
        if (result.skipped) parts.push(`${plural(result.skipped, "entry", "entries")} skipped (see Details).`);
        toast(parts.join(" "), { kind: result.skipped ? "warn" : "success", title: "Cookies imported",
          details: result.problems.length ? result.problems.join("\n") : null });
        if (onDone) onDone(result);
      } catch (err) {
        if (err.code === "nothing_to_import" || err.code === "invalid") {
          replace(preview, h("div.callout.warn", icon("alert"), err.message));
        } else {
          toast(err.message, { kind: "error", title: "Nothing was imported" });
        }
      }
    });
  });
  return dlg;
}
