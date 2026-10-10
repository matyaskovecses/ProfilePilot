// Shared UI components: toasts, dialogs, drawers, menus, form controls, empty states.

import { copyText, h, icon, replace } from "./dom.js";

let uid = 0;
export function nextId(prefix = "pp") {
  uid += 1;
  return `${prefix}-${uid}`;
}

// ------------------------------------------------------------------ toasts

/**
 * toast(text, {kind, title, timeout, action: {label, onClick}, details})
 * `details` (text) goes under a "Details" disclosure, e.g. the full report of a client registration.
 * The toast stays while the pointer or the keyboard focus is on it.
 */
export function toast(text, { kind = "info", title, timeout, action, details } = {}) {
  const host = toastHost();
  if (!host) return () => {};
  const icons = { success: "check", error: "alert", info: "info", warn: "alert" };
  const kindIcon = icon(icons[kind] || "info", "toast-icon");
  const el = h("div.toast", { class: kind, attrs: { role: kind === "error" ? "alert" : "status" } },
    kindIcon,
    h("div.toast-body",
      title ? h("div.toast-title", title) : null,
      text ? h("div.toast-text", text) : null,
      details ? h("details.toast-details", h("summary", "Details"), h("div.toast-detail-text", details)) : null),
    action ? h("button.btn.xs", { onclick: () => { action.onClick(); dismiss(); } }, action.label) : null,
    h("button.btn.ghost.xs.icon-only.toast-close", { onclick: () => dismiss(), attrs: { "aria-label": "Dismiss" } }, icon("x")),
  );
  let gone = false;
  let timer = null;
  const dismiss = () => {
    if (gone) return;
    gone = true;
    clearTimeout(timer);
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  };
  const ms = timeout || (kind === "error" ? 8000 : action ? 7000 : 4200);
  const arm = () => { clearTimeout(timer); timer = setTimeout(dismiss, ms); };
  const hold = () => clearTimeout(timer);
  el.addEventListener("mouseenter", hold);
  el.addEventListener("mouseleave", () => { if (!el.contains(document.activeElement)) arm(); });
  el.addEventListener("focusin", hold);
  el.addEventListener("focusout", (event) => { if (!el.contains(event.relatedTarget)) arm(); });
  host.append(el);
  while (host.children.length > 3) host.firstElementChild.remove();
  arm();
  return dismiss;
}

/**
 * Where toasts go: the page's #toasts, or while a modal dialog or drawer is open a host inside the
 * topmost one (everything outside it is inert, so an "Undo" button there could not be clicked). Its
 * toasts move back to the page when that dialog closes.
 */
function toastHost() {
  const main = document.getElementById("toasts");
  const top = [...document.querySelectorAll("dialog[open]")].pop();
  if (!top || !main) return main;
  let host = top.querySelector(":scope > .toasts");
  if (!host) {
    host = h("div.toasts", { attrs: { role: "status", "aria-live": "polite", "aria-atomic": "false" } });
    top.append(host);
    top.addEventListener("close", () => {
      for (const el of [...host.children]) if (!el.classList.contains("leaving")) main.append(el);
      host.remove();
    }, { once: true });
  }
  return host;
}

// ------------------------------------------------------------------ dialogs

/** Track unsaved changes: snapshot() returns a comparable string of the current values. */
export function changeTracker(snapshot) {
  let base = snapshot();
  return {
    dirty: () => snapshot() !== base,
    reset: () => { base = snapshot(); },
  };
}

async function confirmDiscard() {
  return confirmDialog({
    title: "Discard changes?", message: "What you changed here has not been saved.",
    confirmLabel: "Discard", cancelLabel: "Keep editing", danger: true,
  });
}

/** Esc, a backdrop click and the close button ask before throwing away unsaved changes. */
function guardClose(dialog, isDirty) {
  let asking = false;
  const requestClose = async () => {
    if (!dialog.open || asking) return;
    if (isDirty && isDirty()) {
      asking = true;
      const discard = await confirmDiscard();
      asking = false;
      if (!discard) return;
    }
    if (dialog.open) dialog.close();
  };
  dialog.addEventListener("cancel", (event) => {
    if (isDirty && isDirty()) {
      event.preventDefault();
      requestClose();
    }
  });
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    if (isDirty && isDirty()) return; // a stray click outside never throws work away
    dialog.close();
  });
  return requestClose;
}

/**
 * openDialog({title, description, body, footer, size, isDirty}) -> {el, close, forceClose, body, footer}
 * `body`/`footer` are nodes (or arrays). Esc and a click on the backdrop close the dialog; while
 * `isDirty()` is true they ask "Discard changes?" first (`forceClose` skips that, e.g. after saving).
 */
export function openDialog({ title, description, body, footer, size = "", onClose, label, isDirty } = {}) {
  const titleId = nextId("dlg");
  const dialog = h("dialog.dialog", { class: size, attrs: { "aria-labelledby": titleId } });
  const forceClose = () => { if (dialog.open) dialog.close(); };
  const close = guardClose(dialog, isDirty);
  const header = h("div.dialog-header",
    h("div.dialog-title", h("h2", { id: titleId }, title || label || ""), description ? h("p", description) : null),
    h("button.btn.ghost.sm.icon-only.close", { onclick: () => close(), attrs: { "aria-label": "Close", type: "button" } }, icon("x")),
  );
  const bodyEl = h("div.dialog-body", body);
  const footerEl = footer ? h("div.dialog-footer", footer) : null;
  dialog.append(header, bodyEl);
  if (footerEl) dialog.append(footerEl);
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (onClose) onClose();
  });
  document.body.append(dialog);
  dialog.showModal();
  const first = dialog.querySelector("[autofocus], .dialog-body input:not([type=hidden]), .dialog-body textarea, .dialog-body select");
  if (first) first.focus();
  return { el: dialog, close, forceClose, body: bodyEl, footer: footerEl };
}

export function confirmDialog({ title, message, confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false, details } = {}) {
  return new Promise((resolve) => {
    let answered = false;
    const confirm = h("button.btn", { class: danger ? "danger solid" : "primary", attrs: { type: "button" }, onclick: () => { answered = true; dlg.forceClose(); resolve(true); } }, confirmLabel);
    const dlg = openDialog({
      title, size: "narrow",
      body: h("div.stack", message ? h("p.muted", message) : null, details || null),
      footer: [h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.forceClose() }, cancelLabel), confirm],
      onClose: () => { if (!answered) resolve(false); },
    });
    confirm.focus();
  });
}

// ------------------------------------------------------------------ drawers

/**
 * openDrawer({onClose, label, isDirty}) -> {el, close, forceClose, header, tabs, body, footer, focusInitial}
 * (fill them in, then call focusInitial() so the keyboard starts on the selected tab or first field).
 */
export function openDrawer({ onClose, label = "Details", isDirty } = {}) {
  const dialog = h("dialog.drawer", { attrs: { "aria-label": label } });
  const forceClose = () => { if (dialog.open) dialog.close(); };
  const close = guardClose(dialog, isDirty);
  const header = h("div.drawer-header");
  const tabs = h("div.drawer-tabs", { attrs: { role: "tablist", "aria-label": `${label} sections` } });
  const body = h("div.drawer-body");
  const footer = h("div.drawer-footer.hidden");
  dialog.append(header, tabs, body, footer);
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (onClose) onClose();
  });
  document.body.append(dialog);
  dialog.showModal();
  const focusInitial = () => {
    const target = dialog.querySelector('[role="tab"][aria-selected="true"]')
      || body.querySelector("input:not([type=hidden]), select, textarea, button")
      || header.querySelector(".close");
    if (target) target.focus();
  };
  return { el: dialog, close, forceClose, header, tabs, body, footer, focusInitial };
}

export function closeButton(close) {
  return h("button.btn.ghost.sm.icon-only.close", { onclick: () => close(), attrs: { "aria-label": "Close", title: "Close (Esc)", type: "button" } }, icon("x"));
}

// ------------------------------------------------------------------ menus

let openMenuEl = null;

export function closeMenu() {
  if (openMenuEl) {
    openMenuEl.remove();
    openMenuEl = null;
  }
}

/** A popup menu next to `anchor`. items: [{label, icon, onClick, danger} | "-"] */
export function openMenu(anchor, items) {
  closeMenu();
  const menu = h("div.menu", { attrs: { role: "menu" } });
  for (const item of items) {
    if (item === "-") { menu.append(h("hr")); continue; }
    if (!item) continue;
    menu.append(h("button", {
      class: item.danger ? "danger" : "", attrs: { role: "menuitem", type: "button", title: item.title || null },
      onclick: (event) => { event.stopPropagation(); closeMenu(); item.onClick(); },
    }, item.icon ? icon(item.icon) : null, item.label));
  }
  // Drop separators at the edges or next to each other (items may be conditional).
  for (const hr of [...menu.querySelectorAll("hr")]) {
    const prev = hr.previousElementSibling;
    const next = hr.nextElementSibling;
    if (!prev || !next || next.tagName === "HR") hr.remove();
  }
  // A menu opened from a drawer or dialog lives inside it: the rest of the page is inert while a modal is open.
  (anchor.closest("dialog[open]") || document.body).append(menu);
  const rect = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth;
  const mh = menu.offsetHeight;
  let left = rect.right - mw;
  let top = rect.bottom + 6;
  if (left < 8) left = 8;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, rect.top - mh - 6);
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  openMenuEl = menu;
  const first = menu.querySelector("button");
  if (first) first.focus();
  const onKey = (event) => {
    const buttons = [...menu.querySelectorAll("button")];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeMenu(); anchor.focus(); } // (not the drawer too)
    else if (event.key === "Tab") { event.preventDefault(); closeMenu(); anchor.focus(); }
    else if (event.key === "ArrowDown") { event.preventDefault(); buttons[(index + 1) % buttons.length].focus(); }
    else if (event.key === "ArrowUp") { event.preventDefault(); buttons[(index - 1 + buttons.length) % buttons.length].focus(); }
    else if (event.key === "Home") { event.preventDefault(); buttons[0].focus(); }
    else if (event.key === "End") { event.preventDefault(); buttons[buttons.length - 1].focus(); }
  };
  menu.addEventListener("keydown", onKey);
  setTimeout(() => {
    const away = (event) => {
      if (!menu.contains(event.target)) {
        closeMenu();
        document.removeEventListener("mousedown", away, true);
        window.removeEventListener("blur", closeMenu);
      }
    };
    document.addEventListener("mousedown", away, true);
    window.addEventListener("blur", closeMenu, { once: true });
  });
  return menu;
}

// ------------------------------------------------------------------ controls

export function copyButton(text, { label = "Copy", small = true } = {}) {
  const btn = h("button.btn.ghost.icon-only", { class: small ? "xs" : "sm", attrs: { "aria-label": label, title: label, type: "button" } }, icon("copy"));
  btn.addEventListener("click", async (event) => {
    event.stopPropagation();
    try {
      await copyText(typeof text === "function" ? text() : text);
      replace(btn, icon("check"));
      setTimeout(() => replace(btn, icon("copy")), 1400);
    } catch (err) {
      toast("Could not copy to the clipboard.", { kind: "error" });
    }
  });
  return btn;
}

export function codeBlock(text) {
  return h("div.codeblock", h("pre", h("code", text)), copyButton(text, { label: "Copy to clipboard", small: false }));
}

export function copyable(text, display) {
  return h("span.copyable", h("code", { title: text }, display || text), copyButton(text));
}

/**
 * Segmented control (a radio group). options: [{value, label, icon, title}]; returns element with .value.
 * One tab stop for the group (roving tabindex); arrow keys move the selection.
 */
export function segmented(options, value, onChange, { label, block = false } = {}) {
  const el = h("div.segmented", { class: block ? "block" : "", attrs: { role: "radiogroup", "aria-label": label || null } });
  let current = value;
  const buttons = options.map((opt) => {
    const btn = h("button", {
      attrs: { type: "button", role: "radio", "aria-checked": String(opt.value === current), title: opt.title || null },
      onclick: () => set(opt.value, true),
    }, opt.icon ? icon(opt.icon) : null, opt.label);
    el.append(btn);
    return btn;
  });
  function sync() {
    const selected = options.some((o) => o.value === current) ? current : options[0] && options[0].value;
    buttons.forEach((b, i) => {
      b.setAttribute("aria-checked", String(options[i].value === current));
      b.tabIndex = options[i].value === selected ? 0 : -1;
    });
  }
  function set(v, fire) {
    current = v;
    sync();
    if (fire && onChange) onChange(v);
  }
  el.addEventListener("keydown", (event) => {
    const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    if (!(event.key in keys)) return;
    event.preventDefault();
    const enabled = options.filter((o, i) => !buttons[i].disabled);
    const i = enabled.findIndex((o) => o.value === current);
    const next = enabled[(i + keys[event.key] + enabled.length) % enabled.length];
    set(next.value, true);
    buttons[options.indexOf(next)].focus();
  });
  sync();
  Object.defineProperty(el, "value", { get: () => current, set: (v) => set(v, false) });
  return el;
}

/** Large radio cards. options: [{value, title, desc, icon, badge}] */
export function choiceCards(options, value, onChange, { label } = {}) {
  const el = h("div.choice-cards", { attrs: { role: "radiogroup", "aria-label": label || null } });
  let current = value;
  const cards = options.map((opt) => {
    const card = h("button.choice-card", {
      attrs: { type: "button", role: "radio", "aria-checked": String(opt.value === current) },
      onclick: () => set(opt.value, true),
    },
      h("span.choice-title", opt.icon ? icon(opt.icon) : null, opt.title, opt.badge ? h("span.badge.green", opt.badge) : null),
      h("span.choice-desc", opt.desc));
    el.append(card);
    return card;
  });
  function set(v, fire) {
    current = v;
    cards.forEach((c, i) => {
      c.setAttribute("aria-checked", String(options[i].value === v));
      c.tabIndex = options[i].value === v ? 0 : -1;
    });
    if (fire && onChange) onChange(v);
  }
  el.addEventListener("keydown", (event) => {
    const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    if (!(event.key in keys)) return;
    event.preventDefault();
    const i = options.findIndex((o) => o.value === current);
    const next = options[(i + keys[event.key] + options.length) % options.length];
    set(next.value, true);
    cards[options.indexOf(next)].focus();
  });
  set(current, false);
  Object.defineProperty(el, "value", { get: () => current, set: (v) => set(v, false) });
  return el;
}

/** Tag input with chips. Returns element with .values */
export function chipInput(values = [], { placeholder = "Add a tag…", id, onChange } = {}) {
  let tags = [...values];
  const input = h("input", { id, attrs: { placeholder, "aria-label": placeholder, autocomplete: "off" } });
  const el = h("div.chip-input", { onclick: () => input.focus() });
  const render = () => {
    replace(el, ...tags.map((t) => h("span.tag", t, h("button", {
      attrs: { type: "button", "aria-label": `Remove ${t}` },
      onclick: (event) => { event.stopPropagation(); tags = tags.filter((x) => x !== t); render(); onChange && onChange(tags); },
    }, icon("x")))), input);
  };
  const commit = () => {
    const parts = input.value.split(",").map((x) => x.trim()).filter(Boolean);
    let changed = false;
    for (const part of parts) {
      if (!tags.some((t) => t.toLowerCase() === part.toLowerCase())) { tags.push(part.slice(0, 32)); changed = true; }
    }
    input.value = "";
    if (changed) { render(); input.focus(); onChange && onChange(tags); }
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === ",") { event.preventDefault(); commit(); }
    else if (event.key === "Backspace" && !input.value && tags.length) { tags.pop(); render(); input.focus(); onChange && onChange(tags); }
  });
  input.addEventListener("blur", commit);
  render();
  Object.defineProperty(el, "values", { get: () => { commit(); return [...tags]; } });
  // Pending text counts as a change (for "Discard changes?") without committing it.
  Object.defineProperty(el, "pending", { get: () => [...tags, input.value.trim()].filter(Boolean).join(",") });
  return el;
}

export function field(label, control, { hint, id, error } = {}) {
  const controlId = id || control.id || nextId("f");
  if (!control.id && control.tagName && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) control.id = controlId;
  let hintEl = null;
  if (hint) {
    hintEl = h("div.hint", { id: nextId("hint") }, hint);
    if (control.setAttribute && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName || "")) control.setAttribute("aria-describedby", hintEl.id);
  }
  return h("div.field",
    label ? h("label", { attrs: { for: control.id || controlId } }, label) : null,
    control,
    hintEl,
    error ? h("div.error-text", error) : null,
  );
}

/** Show (or clear, with an empty message) an inline error under `control` and mark it invalid. */
export function setError(control, message) {
  const host = control.closest(".field") || control.closest("form") || control.parentElement;
  if (!host) return;
  let err = host.querySelector(":scope > .error-text");
  if (!message) {
    control.removeAttribute("aria-invalid");
    if (err) err.remove();
    return;
  }
  if (!err) {
    err = h("div.error-text", { id: nextId("err"), attrs: { role: "alert" } });
    host.append(err);
  }
  err.textContent = message;
  control.setAttribute("aria-invalid", "true");
  control.setAttribute("aria-errormessage", err.id);
}

export function select(options, value, { id, onChange, cls = "" } = {}) {
  const el = h("select.select", { id, class: cls, onchange: () => onChange && onChange(el.value) },
    options.map((o) => h("option", { value: o.value, selected: o.value === value, disabled: o.disabled || false }, o.label)));
  el.value = value == null ? "" : value;
  return el;
}

export function toggle(checked, { label, onChange, id, disabled = false } = {}) {
  const input = h("input", { type: "checkbox", id, checked, disabled, attrs: { role: "switch" } });
  input.addEventListener("change", () => onChange && onChange(input.checked));
  const el = h("label.switch", input, h("span.track"), label ? h("span", label) : null);
  Object.defineProperty(el, "checked", { get: () => input.checked, set: (v) => { input.checked = v; } });
  el.input = input;
  return el;
}

export function emptyState({ icon: iconName = "info", title, text, actions = [], compact = false }) {
  return h("div.empty", { class: compact ? "compact" : "" },
    h("div.empty-icon", icon(iconName)),
    h("h2", title),
    text ? h("p", text) : null,
    actions.length ? h("div.row", actions) : null);
}

export function skeletonCards(n = 6) {
  return h("div.grid-cards", Array.from({ length: n }, () => h("div.card.skeleton.skeleton-card")));
}

/** Run an async action while a button shows a spinner. */
export async function busy(button, fn) {
  if (!button) return fn();
  const original = [...button.childNodes];
  const width = button.offsetWidth;
  button.disabled = true;
  button.style.minWidth = `${width}px`;
  replace(button, h("span.spinner"));
  try {
    return await fn();
  } finally {
    button.disabled = false;
    replace(button, ...original);
    button.style.minWidth = "";
  }
}

export function statusDot(key, extra = "") {
  return h("span.dot", { class: `${key} ${extra}`.trim(), attrs: { "aria-hidden": "true" } });
}

export function countryBadge(code) {
  if (!code) return null;
  return h("span.cc", { attrs: { title: code } }, String(code).toUpperCase().slice(0, 3));
}
