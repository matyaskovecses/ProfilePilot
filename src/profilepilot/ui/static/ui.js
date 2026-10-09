// Shared UI components: toasts, dialogs, drawers, menus, form controls, empty states.

import { copyText, h, icon, replace } from "./dom.js";

let uid = 0;
export function nextId(prefix = "pp") {
  uid += 1;
  return `${prefix}-${uid}`;
}

// ------------------------------------------------------------------ toasts

export function toast(text, { kind = "info", title, timeout, action } = {}) {
  const host = document.getElementById("toasts");
  if (!host) return;
  const icons = { success: "check", error: "alert", info: "info", warn: "alert" };
  const el = h("div.toast", { class: kind, attrs: { role: kind === "error" ? "alert" : "status" } },
    icon(icons[kind] || "info"),
    h("div.toast-body", title ? h("div.toast-title", title) : null, h("div.toast-text", text)),
    action ? h("button.btn.xs", { onclick: () => { action.onClick(); dismiss(); } }, action.label) : null,
    h("button.btn.ghost.xs.icon-only", { onclick: () => dismiss(), attrs: { "aria-label": "Dismiss" } }, icon("x")),
  );
  let gone = false;
  const dismiss = () => {
    if (gone) return;
    gone = true;
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  };
  host.append(el);
  while (host.children.length > 4) host.firstElementChild.remove();
  setTimeout(dismiss, timeout || (kind === "error" ? 8000 : 4200));
  return dismiss;
}

// ------------------------------------------------------------------ dialogs

/**
 * openDialog({title, description, body, footer, size}) -> {el, close, body, footer}
 * `body`/`footer` are nodes (or arrays). Esc and a click on the backdrop close the dialog.
 */
export function openDialog({ title, description, body, footer, size = "", onClose, label } = {}) {
  const titleId = nextId("dlg");
  const dialog = h("dialog.dialog", { class: size, attrs: { "aria-labelledby": titleId } });
  const close = () => { if (dialog.open) dialog.close(); };
  const header = h("div.dialog-header",
    h("div.dialog-title", h("h2", { id: titleId }, title || label || ""), description ? h("p", description) : null),
    h("button.btn.ghost.sm.icon-only.close", { onclick: close, attrs: { "aria-label": "Close" } }, icon("x")),
  );
  const bodyEl = h("div.dialog-body", body);
  const footerEl = footer ? h("div.dialog-footer", footer) : null;
  dialog.append(header, bodyEl);
  if (footerEl) dialog.append(footerEl);
  dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (onClose) onClose();
  });
  document.body.append(dialog);
  dialog.showModal();
  const first = dialog.querySelector("[autofocus], .dialog-body input, .dialog-body textarea, .dialog-body select");
  if (first) first.focus();
  return { el: dialog, close, body: bodyEl, footer: footerEl };
}

export function confirmDialog({ title, message, confirmLabel = "Confirm", danger = false, details } = {}) {
  return new Promise((resolve) => {
    let answered = false;
    const confirm = h("button.btn", { class: danger ? "danger solid" : "primary", onclick: () => { answered = true; dlg.close(); resolve(true); } }, confirmLabel);
    const dlg = openDialog({
      title, size: "narrow",
      body: h("div.stack", h("p.muted", message), details || null),
      footer: [h("span.spacer"), h("button.btn", { onclick: () => dlg.close() }, "Cancel"), confirm],
      onClose: () => { if (!answered) resolve(false); },
    });
    confirm.focus();
  });
}

// ------------------------------------------------------------------ drawers

/** openDrawer({onClose}) -> {el, close, header, tabs, body, footer} (fill them in). */
export function openDrawer({ onClose, label = "Details" } = {}) {
  const dialog = h("dialog.drawer", { attrs: { "aria-label": label } });
  const close = () => { if (dialog.open) dialog.close(); };
  const header = h("div.drawer-header");
  const tabs = h("div.drawer-tabs", { attrs: { role: "tablist" } });
  const body = h("div.drawer-body");
  const footer = h("div.drawer-footer.hidden");
  dialog.append(header, tabs, body, footer);
  dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (onClose) onClose();
  });
  document.body.append(dialog);
  dialog.showModal();
  return { el: dialog, close, header, tabs, body, footer };
}

export function closeButton(close) {
  return h("button.btn.ghost.sm.icon-only.close", { onclick: close, attrs: { "aria-label": "Close", title: "Close (Esc)" } }, icon("x"));
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
      class: item.danger ? "danger" : "", attrs: { role: "menuitem" },
      onclick: (event) => { event.stopPropagation(); closeMenu(); item.onClick(); },
    }, item.icon ? icon(item.icon) : null, item.label));
  }
  document.body.append(menu);
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
    if (event.key === "Escape") { closeMenu(); anchor.focus(); }
    else if (event.key === "ArrowDown") { event.preventDefault(); buttons[(index + 1) % buttons.length].focus(); }
    else if (event.key === "ArrowUp") { event.preventDefault(); buttons[(index - 1 + buttons.length) % buttons.length].focus(); }
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

/** Segmented control. options: [{value, label, icon}]; returns element with .value */
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
  function set(v, fire) {
    current = v;
    buttons.forEach((b, i) => b.setAttribute("aria-checked", String(options[i].value === v)));
    if (fire && onChange) onChange(v);
  }
  el.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    event.preventDefault();
    const i = options.findIndex((o) => o.value === current);
    const next = options[(i + (event.key === "ArrowRight" ? 1 : options.length - 1)) % options.length];
    set(next.value, true);
    buttons[options.indexOf(next)].focus();
  });
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
    cards.forEach((c, i) => c.setAttribute("aria-checked", String(options[i].value === v)));
    if (fire && onChange) onChange(v);
  }
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
  return el;
}

export function field(label, control, { hint, id, error } = {}) {
  const controlId = id || control.id || nextId("f");
  if (!control.id && control.tagName && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) control.id = controlId;
  return h("div.field",
    label ? h("label", { attrs: { for: control.id || controlId } }, label) : null,
    control,
    hint ? h("div.hint", hint) : null,
    error ? h("div.error-text", error) : null,
  );
}

export function select(options, value, { id, onChange, cls = "" } = {}) {
  const el = h("select.select", { id, class: cls, onchange: () => onChange && onChange(el.value) },
    options.map((o) => h("option", { value: o.value, selected: o.value === value, disabled: o.disabled || false }, o.label)));
  el.value = value == null ? "" : value;
  return el;
}

export function toggle(checked, { label, onChange, id } = {}) {
  const input = h("input", { type: "checkbox", id, checked, attrs: { role: "switch" } });
  input.addEventListener("change", () => onChange && onChange(input.checked));
  const el = h("label.switch", input, h("span.track"), label ? h("span", label) : null);
  Object.defineProperty(el, "checked", { get: () => input.checked, set: (v) => { input.checked = v; } });
  el.input = input;
  return el;
}

export function emptyState({ icon: iconName = "info", title, text, actions = [] }) {
  return h("div.empty",
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
