// Profiles: a live card grid (or a compact list) with thumbnails, status and quick actions.

import { api, enc } from "./api.js";
import { avatar, fmt, h, hue, icon, replace } from "./dom.js";
import { BROWSER_LABELS, cloneDialog, confirmDelete, newProfileDialog } from "./profile-dialogs.js";
import { openProfileDrawer } from "./profile-drawer.js";
import {
  actions, aiActivity, displayStatus, loadClients, loadProfiles, profileList, profilesByPriority, proxyLabel, state, upsertProfile,
} from "./store.js";
import { attachThumb } from "./thumbs.js";
import {
  busy, chipInput, confirmDialog, countryBadge, emptyState, field, openDialog, openMenu, segmented, select, skeletonCards, statusDot, toast,
} from "./ui.js";

const DENSITY_KEY = "pp-profiles-density";

function savedDensity() {
  try { return localStorage.getItem(DENSITY_KEY) === "list" ? "list" : "cards"; } catch (err) { return "cards"; }
}

/** Does the profile have a problem worth a look (crash, deleted or failing proxy)? */
function hasProblem(p) {
  return p.state === "crashed" || !!(p.proxy && (p.proxy.missing || p.proxy.ok === false));
}

export function createProfilesView() {
  const filters = { q: "", tag: "", status: "all" };
  const cards = new Map();
  const selection = { on: false, ids: new Set() };
  let activeId = null; // the card that holds the grid's single tab stop
  let density = savedDensity();
  const sel = {
    active: () => selection.on,
    has: (id) => selection.ids.has(id),
    toggle: (id) => { if (selection.ids.has(id)) selection.ids.delete(id); else selection.ids.add(id); renderSelection(); },
  };

  const subtitle = h("p");
  const searchInput = h("input.input", { type: "search", attrs: { placeholder: "Search profiles", "aria-label": "Search profiles", autocomplete: "off" } });
  searchInput.addEventListener("input", () => { filters.q = searchInput.value.trim().toLowerCase(); render(); });
  const tagSelect = h("select.select.sm", { attrs: { "aria-label": "Filter by tag" }, style: { width: "auto", minWidth: "120px" } });
  tagSelect.addEventListener("change", () => { filters.tag = tagSelect.value; render(); });
  const statusSeg = segmented([
    { value: "all", label: "All" }, { value: "running", label: "Running" }, { value: "attention", label: "Needs you", title: "The AI asked you for help" },
    { value: "problems", label: "Problems", title: "Crashed, or the proxy is failing or gone" }, { value: "stopped", label: "Stopped" },
  ], "all", (v) => { filters.status = v; render(); }, { label: "Filter by status" });
  const densitySeg = segmented([
    { value: "cards", label: "Cards", title: "Cards with live previews" }, { value: "list", label: "List", title: "One compact row per profile" },
  ], density, (v) => {
    density = v;
    try { localStorage.setItem(DENSITY_KEY, v); } catch (err) { /* storage blocked */ }
    render();
  }, { label: "Layout" });

  const newBtn = h("button.btn.primary", { onclick: () => newProfileDialog({ onCreated: (p) => openProfileDrawer(p.id) }), attrs: { type: "button", title: "New profile (N)" } }, icon("plus"), "New profile");
  const stopAll = h("button.btn.ghost.hidden", { attrs: { type: "button", title: "Close every running profile's browser" }, onclick: async () => {
    const running = profileList().filter((p) => p.state === "running").length;
    const yes = await confirmDialog({ title: `Stop ${fmt.plural(running, "running profile")}?`, message: "Their browsers close; tabs and logins are kept for the next start.", confirmLabel: "Stop all" });
    if (!yes) return;
    busy(stopAll, async () => {
      try {
        const result = await api.post("/api/stop-all");
        toast(`Stopped ${fmt.plural(result.stopped.length, "profile")}.`, { kind: "success" });
      } catch (err) { toast(err.message, { kind: "error" }); }
    });
  } }, icon("stop"), "Stop all");
  const header = h("header.view-header",
    h("div.view-title", h("h1", "Profiles"), subtitle),
    h("div.view-actions", stopAll, newBtn));
  const selectBtn = h("button.btn.sm", { attrs: { type: "button", "aria-pressed": "false", title: "Select several profiles for bulk actions" },
    onclick: () => setSelecting(!selection.on) }, icon("check"), "Select");
  const toolbar = h("div.toolbar",
    h("div.search", icon("search"), searchInput, h("kbd", "/")),
    tagSelect, statusSeg, h("span.spacer"), densitySeg, selectBtn);
  const bulkCount = h("strong");
  const bulkBar = h("div.bulk-bar.hidden", { attrs: { role: "toolbar", "aria-label": "Bulk actions" } },
    bulkCount,
    h("button.btn.sm.ghost", { attrs: { type: "button" }, onclick: () => selectAllShown() }, "Select all"),
    h("span.bulk-sep"),
    h("button.btn.sm", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => bulkRun("start")) }, icon("play"), "Start"),
    h("button.btn.sm", { attrs: { type: "button" }, onclick: (e) => busy(e.currentTarget, () => bulkRun("stop")) }, icon("stop"), "Stop"),
    h("button.btn.sm", { attrs: { type: "button" }, onclick: () => bulkProxyDialog() }, icon("proxies"), "Proxies…"),
    h("button.btn.sm", { attrs: { type: "button" }, onclick: () => bulkTagDialog() }, icon("tag"), "Tags…"),
    h("button.btn.sm.danger", { attrs: { type: "button" }, onclick: () => bulkDelete() }, icon("trash"), "Delete…"),
    h("button.btn.sm.ghost.icon-only", { attrs: { type: "button", "aria-label": "Done selecting", title: "Done (Esc)" }, onclick: () => setSelecting(false) }, icon("x")));
  const grid = h("div.grid-cards", { attrs: { role: "list", "aria-label": "Profiles (arrow keys move between them)" } });
  grid.addEventListener("keydown", onGridKey);
  grid.addEventListener("focusin", (event) => {
    const card = event.target.closest(".profile-card");
    if (card && card.dataset.id !== activeId) { activeId = card.dataset.id; updateRoving(); }
  });
  const body = h("div.view-body", skeletonCards(6));
  const el = h("section.view", { attrs: { "aria-label": "Profiles" } }, header, toolbar, body, bulkBar);

  function setSelecting(on) {
    selection.on = on;
    if (!on) selection.ids.clear();
    selectBtn.setAttribute("aria-pressed", String(on));
    selectBtn.classList.toggle("primary", on);
    renderSelection();
  }

  function renderSelection() {
    for (const id of [...selection.ids]) if (!state.profiles.has(id)) selection.ids.delete(id);
    el.classList.toggle("selecting", selection.on);
    document.body.classList.toggle("bulk-open", selection.on && el.isConnected);
    bulkBar.classList.toggle("hidden", !selection.on);
    bulkCount.textContent = selection.ids.size ? `${selection.ids.size} selected` : "Select profiles";
    for (const [id, card] of cards) {
      card.el.classList.toggle("is-selected", selection.ids.has(id));
      if (selection.on) card.el.setAttribute("aria-selected", String(selection.ids.has(id)));
      else card.el.removeAttribute("aria-selected");
    }
    for (const btn of bulkBar.querySelectorAll("button:not(.ghost)")) btn.disabled = !selection.ids.size;
  }

  function selectAllShown() {
    for (const p of profileList().filter(matches)) selection.ids.add(p.id);
    renderSelection();
  }

  function selected() {
    return [...selection.ids].map((id) => state.profiles.get(id)).filter(Boolean);
  }

  // ---------------------------------------------------------------- keyboard: one tab stop for the grid

  function shownCards() {
    return [...grid.children].filter((c) => c.classList.contains("profile-card"));
  }

  /** Only the active card (and its buttons) is in the tab order; arrow keys move between cards. */
  function updateRoving() {
    const list = shownCards();
    if (!list.length) return;
    if (!activeId || !list.some((c) => c.dataset.id === activeId)) activeId = list[0].dataset.id;
    for (const card of list) {
      const active = card.dataset.id === activeId;
      card.tabIndex = active ? 0 : -1;
      for (const btn of card.querySelectorAll("button, a[href]")) btn.tabIndex = active ? 0 : -1;
    }
  }

  function onGridKey(event) {
    const card = event.target.closest(".profile-card");
    if (!card || event.target !== card) return;
    const list = shownCards();
    const i = list.indexOf(card);
    let perRow = 1;
    if (density === "cards" && list.length > 1) {
      const top = list[0].offsetTop;
      perRow = Math.max(1, list.filter((c) => c.offsetTop === top).length);
    }
    const moves = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: perRow, ArrowUp: -perRow, Home: -i, End: list.length - 1 - i };
    if (!(event.key in moves)) return;
    event.preventDefault();
    const next = list[Math.max(0, Math.min(list.length - 1, i + moves[event.key]))];
    activeId = next.dataset.id;
    updateRoving();
    next.focus();
  }

  async function pool(items, worker, size = 3) {
    const queue = [...items];
    const errors = [];
    await Promise.all(Array.from({ length: Math.min(size, queue.length) }, async () => {
      while (queue.length) {
        const item = queue.shift();
        try { await worker(item); } catch (err) { errors.push(`${item.name}: ${err.message}`); }
      }
    }));
    return errors;
  }

  async function bulkRun(kind) {
    const targets = selected().filter((p) => (kind === "start" ? p.state !== "running" : p.state === "running"));
    if (!targets.length) {
      toast(kind === "start" ? "The selected profiles are already running." : "None of the selected profiles is running.", { kind: "info" });
      return;
    }
    targets.forEach((p) => state.pending.set(p.id, kind === "start" ? "starting" : "stopping"));
    render();
    const errors = await pool(targets, async (p) => {
      try {
        upsertProfile(await api.post(`/api/profiles/${enc(p.id)}/${kind}`, {}));
      } finally {
        state.pending.delete(p.id);
      }
    });
    render();
    toast(errors.length ? errors.join("\n") : `${kind === "start" ? "Started" : "Stopped"} ${fmt.plural(targets.length, "profile")}.`,
      { kind: errors.length ? "error" : "success", title: errors.length ? `${errors.length} of ${targets.length} failed` : null });
  }

  function bulkProxyDialog() {
    const targets = selected();
    if (!targets.length) return;
    const mode = segmented([
      { value: "each", label: "One per profile" }, { value: "same", label: "Same for all" }, { value: "none", label: "No proxy" },
    ], "each", (v) => show(v), { label: "How to assign" });
    const tags = [...new Set(state.proxies.flatMap((x) => x.tags))].sort();
    const poolTag = select([{ value: "", label: "Any saved proxy" }, ...tags.map((t) => ({ value: t, label: `Tagged "${t}"` }))], "", {});
    const onlyWorking = h("input", { type: "checkbox", checked: true });
    const one = select(state.proxies.map((x) => ({ value: x.id, label: `${proxyLabel(x)} (${x.scheme}://${x.host}:${x.port})` })), state.proxies[0] ? state.proxies[0].id : "", {});
    const box = h("div.stack");
    const show = (v) => {
      if (v === "each") {
        replace(box, h("p.small.muted", "Each profile gets a different proxy. Proxies no profile uses yet come first."),
          field("Take proxies from", poolTag),
          h("label.checkbox", onlyWorking, h("span", "Skip proxies whose last test failed")));
      } else if (v === "same") {
        replace(box, field("Proxy", one));
      } else {
        replace(box, h("p.small.muted", "The selected profiles will connect directly from this computer's IP address."));
      }
    };
    show("each");
    const apply = h("button.btn.primary", { attrs: { type: "button" } }, "Apply");
    const dlg = openDialog({
      title: `Proxies for ${fmt.plural(targets.length, "profile")}`, size: "narrow",
      body: h("div.form", mode, box),
      footer: [h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.forceClose() }, "Cancel"), apply],
    });
    apply.addEventListener("click", () => busy(apply, async () => {
      let plan = [];
      if (mode.value === "none") plan = targets.map((p) => [p, ""]);
      else if (mode.value === "same") plan = targets.map((p) => [p, one.value]);
      else {
        const used = new Set([...state.profiles.values()].filter((p) => !selection.ids.has(p.id)).map((p) => p.proxy_id).filter(Boolean));
        let candidates = state.proxies.filter((x) => (!poolTag.value || x.tags.includes(poolTag.value)) && !(onlyWorking.checked && x.last_check && !x.last_check.ok));
        candidates = [...candidates.filter((x) => !used.has(x.id)), ...candidates.filter((x) => used.has(x.id))];
        if (!candidates.length) { toast("No proxies match. Add proxies first or change the filter.", { kind: "warn" }); return; }
        if (candidates.length < targets.length) {
          const yes = await confirmDialog({ title: "Not enough proxies", message: `There are ${candidates.length} matching proxies for ${targets.length} profiles, so some profiles will share one. Continue?`, confirmLabel: "Continue" });
          if (!yes) return;
        }
        plan = targets.map((p, i) => [p, candidates[i % candidates.length].id]);
      }
      const errors = await pool(plan, async ([p, proxyId]) => {
        const result = await api.patch(`/api/profiles/${enc(p.id)}`, { proxy_id: proxyId });
        upsertProfile(result.profile);
      });
      dlg.forceClose();
      toast(errors.length ? errors.join("\n") : `Updated ${fmt.plural(plan.length, "profile")}.`, { kind: errors.length ? "error" : "success" });
    }));
  }

  function bulkTagDialog() {
    const targets = selected();
    if (!targets.length) return;
    const add = chipInput([], { placeholder: "Tags to add" });
    const remove = chipInput([], { placeholder: "Tags to remove" });
    const apply = h("button.btn.primary", { attrs: { type: "button" } }, "Apply");
    const dlg = openDialog({
      title: `Tags for ${fmt.plural(targets.length, "profile")}`, size: "narrow",
      body: h("div.form", field("Add", add), field("Remove", remove)),
      isDirty: () => !!(add.pending || remove.pending),
      footer: [h("span.spacer"), h("button.btn", { attrs: { type: "button" }, onclick: () => dlg.forceClose() }, "Cancel"), apply],
    });
    apply.addEventListener("click", () => busy(apply, async () => {
      const plus = add.values;
      const minus = remove.values.map((t) => t.toLowerCase());
      const errors = await pool(targets, async (p) => {
        const tags = [...p.tags.filter((t) => !minus.includes(t.toLowerCase()))];
        for (const t of plus) if (!tags.some((x) => x.toLowerCase() === t.toLowerCase())) tags.push(t);
        const result = await api.patch(`/api/profiles/${enc(p.id)}`, { tags });
        upsertProfile(result.profile);
      });
      dlg.forceClose();
      toast(errors.length ? errors.join("\n") : `Updated ${fmt.plural(targets.length, "profile")}.`, { kind: errors.length ? "error" : "success" });
    }));
  }

  async function bulkDelete() {
    const targets = selected();
    if (!targets.length) return;
    const running = targets.filter((p) => p.state === "running" || p.state === "starting");
    const yes = await confirmDialog({
      title: `Delete ${fmt.plural(targets.length, "profile")}?`,
      message: `They move to the trash with their cookies, logins and history (restore them in Settings → Trash).${running.length ? ` ${fmt.plural(running.length, "running profile")} will be skipped: stop them first.` : ""}`,
      confirmLabel: "Move to trash", danger: true,
    });
    if (!yes) return;
    const stopped = targets.filter((p) => !running.includes(p));
    const trashIds = [];
    const errors = await pool(stopped, async (p) => { trashIds.push((await api.del(`/api/profiles/${enc(p.id)}`)).trash_id); });
    await loadProfiles();
    setSelecting(false);
    toast(errors.length ? errors.join("\n") : `Moved ${fmt.plural(stopped.length, "profile")} to the trash.`, {
      kind: errors.length ? "error" : "success", timeout: 6000,
      action: trashIds.length ? { label: "Undo", onClick: () => Promise.all(trashIds.map((t) => actions.restore(t))).catch(() => {}) } : null,
    });
  }

  function matches(p) {
    if (filters.tag && !p.tags.some((t) => t.toLowerCase() === filters.tag.toLowerCase())) return false;
    const st = displayStatus(p);
    if (filters.status === "running" && !["running", "starting"].includes(st.runtime)) return false;
    if (filters.status === "stopped" && !["stopped", "crashed"].includes(st.runtime)) return false;
    if (filters.status === "attention" && st.key !== "help") return false;
    if (filters.status === "problems" && !hasProblem(p)) return false;
    if (!filters.q) return true;
    const hay = [p.name, p.notes, ...p.tags, p.proxy && p.proxy.name, p.proxy && proxyLabel(p.proxy), p.identity && p.identity.name, p.id].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(filters.q);
  }

  function renderTags(all) {
    const tags = [...new Set(all.flatMap((p) => p.tags))].sort((a, b) => a.localeCompare(b));
    const current = filters.tag;
    replace(tagSelect, h("option", { value: "" }, "All tags"), ...tags.map((t) => h("option", { value: t }, t)));
    tagSelect.value = tags.includes(current) ? current : "";
    filters.tag = tagSelect.value;
    tagSelect.classList.toggle("hidden", !tags.length);
  }

  function render() {
    if (!state.ready) return;
    const all = profilesByPriority();
    renderTags(all);
    const running = all.filter((p) => p.state === "running").length;
    stopAll.classList.toggle("hidden", running < 2);
    const attention = all.filter((p) => (p.control && (p.control.help || []).length)).length;
    subtitle.textContent = all.length
      ? [fmt.plural(all.length, "profile"), `${running} running`, attention ? `${attention} need${attention === 1 ? "s" : ""} you` : null].filter(Boolean).join(" · ")
      : "Isolated Chrome identities for your AI.";
    if (!all.length) {
      replace(body, gettingStarted());
      toolbar.classList.add("hidden");
      newBtn.classList.add("hidden"); // the welcome steps have their own "New profile" button
      return;
    }
    newBtn.classList.remove("hidden");
    toolbar.classList.remove("hidden");
    grid.classList.toggle("list", density === "list");
    const shown = all.filter(matches);
    if (!shown.length) {
      replace(body, emptyState({
        icon: "search", title: "No profiles match", text: "Try another search or clear the filters.",
        actions: [h("button.btn", { attrs: { type: "button" }, onclick: () => { filters.q = ""; filters.tag = ""; filters.status = "all"; searchInput.value = ""; statusSeg.value = "all"; render(); } }, "Clear filters")],
      }));
      return;
    }
    if (body.firstChild !== grid) replace(body, grid);
    const seen = new Set();
    shown.forEach((p, index) => {
      seen.add(p.id);
      let card = cards.get(p.id);
      if (!card) {
        card = profileCard(p, sel);
        cards.set(p.id, card);
      } else {
        card.update(p);
      }
      card.el.classList.toggle("is-selected", selection.ids.has(p.id));
      if (grid.children[index] !== card.el) {
        const hadFocus = card.el.contains(document.activeElement);
        grid.insertBefore(card.el, grid.children[index] || null);
        if (hadFocus) card.el.focus();
      }
    });
    for (const [id, card] of cards) {
      if (!seen.has(id)) {
        card.el.remove();
        if (!state.profiles.has(id)) { card.destroy(); cards.delete(id); }
      }
    }
    updateRoving();
  }

  return {
    el,
    title: "Profiles",
    update(topics) {
      if (topics.has("profiles") || topics.has("help") || topics.has("proxies") || topics.has("identities") || topics.has("ready")
        || (topics.has("clients") && !state.profiles.size)) render();
    },
    onShow() { render(); renderSelection(); },
    onHide() { document.body.classList.remove("bulk-open"); },
    onEscape() { if (selection.on) { setSelecting(false); return true; } return false; },
    focusSearch() { searchInput.focus(); searchInput.select(); },
    newItem() { newProfileDialog({ onCreated: (p) => openProfileDrawer(p.id) }); },
  };
}

// ------------------------------------------------------------------ first run

let clientsRequested = false;

function gettingStarted() {
  if (!state.clients && !clientsRequested) {
    clientsRequested = true;
    loadClients().catch(() => {});
  }
  const connected = (state.clients || []).filter((c) => c.registered === true).map((c) => c.label);
  if (state.chatgpt && state.chatgpt.running) connected.push("ChatGPT");
  const steps = [
    {
      done: connected.length > 0, title: "Connect your AI app", icon: "connections",
      text: connected.length ? `Connected: ${connected.join(", ")}.`
        : "Add ProfilePilot to Claude Desktop, Claude Code, Codex or Cursor in one click, or connect ChatGPT.",
      action: h("a.btn.sm", { href: "#/connections" }, connected.length ? "Manage" : "Connect", icon("arrow-right")),
    },
    {
      done: state.proxies.length > 0, title: "Add proxies", icon: "proxies", optional: true,
      text: state.proxies.length ? `${fmt.plural(state.proxies.length, "proxy", "proxies")} saved.`
        : "Give every profile its own IP address. Paste a whole list at once.",
      action: h("a.btn.sm", { href: "#/proxies" }, state.proxies.length ? "View" : "Add proxies", icon("arrow-right")),
    },
    {
      done: false, title: "Create your first profile", icon: "profiles",
      text: "A separate, real Chrome with its own cookies, logins and history. The AI browses with it; you can take over at any time.",
      action: h("button.btn.sm.primary", { attrs: { type: "button", title: "New profile (N)" }, onclick: () => newProfileDialog({ onCreated: (p) => openProfileDrawer(p.id) }) }, icon("plus"), "New profile"),
    },
  ];
  return h("div.onboarding",
    h("div.onb-hero",
      h("img", { src: "/static/logo.svg", alt: "", width: 56, height: 56 }),
      h("div.stack", { style: { gap: "4px" } },
        h("h2", "Welcome to ProfilePilot"),
        h("p", "Your AI gets its own real Chrome profiles: separate logins, cookies and IP addresses, and you stay in charge of every one of them."))),
    h("ol.onb-steps", steps.map((step, i) => h("li.onb-step", { class: step.done ? "done" : "" },
      h("span.onb-num", step.done ? icon("check") : String(i + 1)),
      h("div.grow.stack", { style: { gap: "2px" } },
        h("strong", step.title, step.optional && !step.done ? h("span.badge", { style: { marginLeft: "8px" } }, "Optional") : null),
        h("span.small.muted", step.text)),
      step.action))),
  );
}

// ------------------------------------------------------------------ the card

function profileCard(p0, sel) {
  let p = p0;
  const img = h("img", { attrs: { alt: "", decoding: "async" } });
  const placeholder = h("div.thumb-placeholder");
  const overlays = h("div");
  const check = h("span.select-check", { attrs: { "aria-hidden": "true" } }, icon("check"));
  const thumb = h("div.thumb", img, placeholder, overlays, check);
  const body = h("div.pc-body");
  const actionsRow = h("div.pc-actions");
  const el = h("article.card.profile-card", {
    dataset: { id: p.id },
    attrs: { role: "listitem", tabindex: "-1", "aria-label": p.name },
    onclick: (event) => {
      if (sel.active()) { event.preventDefault(); sel.toggle(p.id); return; }
      if (event.target.closest("button, a")) return;
      openProfileDrawer(p.id);
    },
    onkeydown: (event) => {
      if ((event.key === "Enter" || event.key === " ") && event.target === el) {
        event.preventDefault();
        if (sel.active()) sel.toggle(p.id); else openProfileDrawer(p.id);
      }
    },
  }, thumb, body, actionsRow);
  let thumbState = null;
  const live = attachThumb(thumb, img, p.id, {
    active: () => p.state === "running",
    interval: 3500,
    onState: (s) => { if (s !== thumbState) { thumbState = s; renderThumb(); } },
  });

  function renderThumb() {
    const st = displayStatus(p);
    const running = p.state === "running";
    const showImage = running && thumbState === "live";
    placeholder.classList.toggle("hidden", showImage);
    if (!running) live.clear();
    placeholder.style.setProperty("--hue", String(hue(p.id)));
    let stateLine;
    if (running) {
      stateLine = thumbState === "minimized" ? h("div.thumb-state", icon("tab"), "Window is minimized")
        : thumbState === "page-error" ? h("div.thumb-state.warn", icon("alert"), p.proxy ? "Page couldn't load – check the proxy" : "Page couldn't load")
          : p.window === "headless" ? h("div.thumb-state", icon("eye"), "Headless: no window")
            : h("div.thumb-state", h("span.spinner.sm"), "Loading preview…");
    } else if (st.runtime === "starting" || st.runtime === "stopping") {
      stateLine = h("div.thumb-state", h("span.spinner.sm"), st.runtime === "starting" ? "Starting Chrome…" : "Closing…");
    } else if (p.state === "crashed") {
      stateLine = h("div.thumb-state", statusDot("crashed"), "Chrome closed unexpectedly");
    } else {
      stateLine = h("div.thumb-state", statusDot("stopped"), "Stopped");
    }
    replace(placeholder, avatar(p.name, "", p.id), stateLine);
    const over = [];
    const busyAi = aiActivity(p.id);
    if (running) over.push(h("div.thumb-live", { class: busyAi ? "ai" : "" }, busyAi ? icon("sparkles") : statusDot("running"),
      busyAi ? "AI working" : "Live"));
    const win = (p.runtime && p.runtime.window) || p.window;
    if (win && win !== "normal") over.push(h("div.thumb-window", h("span.badge", win === "offscreen" ? "Off-screen" : "Headless")));
    const control = p.control || {};
    if (control.help && control.help.length) {
      const req = control.help[0];
      over.push(h("div.thumb-overlay.help", icon("robot"), h("span.ellipsis", req.message)));
    } else if (control.paused) {
      over.push(h("div.thumb-overlay.paused", icon("hand"), h("span.ellipsis", "You're in control · AI paused")));
    }
    replace(overlays, ...over);
  }

  function renderBody() {
    const st = displayStatus(p);
    const chips = [];
    if (p.proxy) {
      const pr = p.proxy;
      if (pr.missing) {
        chips.push(h("span.chip.warn", { attrs: { title: "This proxy was deleted: the profile connects directly" } }, icon("proxies"), h("span.chip-text", "Proxy deleted")));
      } else if (pr.ok === false) {
        chips.push(h("span.chip.warn", { attrs: { title: `${proxyLabel(pr)}: ${pr.reason || "the last check failed"}` } }, icon("proxies"), h("span.chip-text", "Proxy not reachable")));
      } else {
        const title = [`${pr.host}:${pr.port}`, pr.ip, pr.city, pr.checked_at ? `checked ${fmt.ago(pr.checked_at)}` : "not tested yet"].filter(Boolean).join(" · ");
        chips.push(h("span.chip", { attrs: { title } }, icon("proxies"), pr.country_code ? countryBadge(pr.country_code) : null, h("span.chip-text", proxyLabel(pr))));
      }
    } else {
      chips.push(h("span.chip", { attrs: { title: "No proxy: the browser uses this computer's own IP" } }, icon("proxies"), h("span.chip-text", "Direct")));
    }
    const browserName = BROWSER_LABELS[p.browser] || p.browser;
    const version = p.runtime && p.runtime.browser_version ? ` ${p.runtime.browser_version.split(".")[0]}` : "";
    chips.push(h("span.chip", { attrs: { title: `${browserName}${version}` } }, icon("tab"), h("span.chip-text", p.browser === "auto" ? `Chrome${version}` : `${browserName.replace("Google ", "").replace("Microsoft ", "")}${version}`)));
    if (p.identity) chips.push(h("span.chip", { class: p.identity.missing ? "warn" : "", attrs: { title: "Identity for form autofill" } }, icon("user"), h("span.chip-text", p.identity.name)));
    replace(body,
      h("div.pc-title", statusDot(st.key), h("span.pc-name", { attrs: { title: p.name } }, p.name),
        h("span.pc-status", st.label)),
      h("div.pc-chips", chips),
      p.tags.length ? h("div.pc-tags", p.tags.slice(0, 5).map((t) => h("span.tag", t)), p.tags.length > 5 ? h("span.tag", `+${p.tags.length - 5}`) : null) : null,
    );
    el.setAttribute("aria-label", `${p.name}, ${st.label}`);
    el.classList.toggle("is-help", st.key === "help");
    el.classList.toggle("is-paused", st.key === "paused");
    el.classList.toggle("is-stopped", !["running", "starting", "stopping"].includes(st.runtime));
  }

  function renderActions() {
    const st = displayStatus(p);
    const control = p.control || {};
    const running = st.runtime === "running";
    const pending = state.pending.get(p.id) || (st.runtime === "starting" || st.runtime === "stopping" ? st.runtime : null);
    const help = control.help && control.help.length ? control.help[0] : null;
    const items = [];
    if (pending) {
      items.push(h("button.btn.sm", { disabled: true }, h("span.spinner"), pending === "stopping" ? "Stopping…" : "Starting…"));
    } else if (help && p.window !== "headless") {
      items.push(h("button.btn.sm", { attrs: { type: "button", title: running ? "Bring the window to the front" : "Start the profile in a normal window" },
        onclick: (e) => busy(e.currentTarget, () => actions.openWindow(p.id).catch(() => {})) }, icon(running ? "focus" : "play"), running ? "Show window" : "Open window"));
    } else if (!running) {
      items.push(h("button.btn.sm", { class: control.paused ? "" : "primary", attrs: { type: "button" }, onclick: () => actions.start(p.id).catch(() => {}) }, icon("play"), "Start"));
    } else if (p.window !== "headless") {
      items.push(h("button.btn.sm", { attrs: { type: "button", title: "Bring the window to the front" }, onclick: (e) => busy(e.currentTarget, () => actions.focus(p.id).catch(() => {})) }, icon("focus"), "Show"));
    }
    if (help) {
      items.push(h("button.btn.sm.primary", { attrs: { type: "button", title: "I did it: hand the profile back to the AI" }, onclick: (e) => busy(e.currentTarget, () => actions.resolveHelp(p.id, help.id, "done").catch(() => {})) }, icon("check"), "I'm done"));
    } else if (control.paused) {
      items.push(h("button.btn.sm.primary", { attrs: { type: "button", title: "Let the AI use this profile again" }, onclick: (e) => busy(e.currentTarget, () => actions.handBack(p.id).catch(() => {})) }, icon("sparkles"), "Hand back"));
    } else if (running || !pending) {
      items.push(h("button.btn.sm", { class: running ? "" : "ghost", attrs: { type: "button", title: "Pause the AI on this profile and work in it yourself" }, onclick: (e) => busy(e.currentTarget, () => actions.takeControl(p.id).catch(() => {})) }, icon("hand"), "Take control"));
    }
    items.push(h("span.spacer"));
    if (running && !pending) {
      items.push(h("button.btn.sm.ghost.icon-only", { onclick: () => actions.stop(p.id).catch(() => {}), attrs: { type: "button", "aria-label": `Stop ${p.name}`, title: "Stop" } }, icon("stop")));
    }
    const more = h("button.btn.sm.ghost.icon-only", { attrs: { type: "button", "aria-label": `More actions for ${p.name}`, title: "More", "aria-haspopup": "menu" } }, icon("more"));
    more.addEventListener("click", (event) => { event.stopPropagation(); profileMenu(p, more); });
    items.push(more);
    const active = el.tabIndex === 0;
    replace(actionsRow, ...items);
    for (const btn of actionsRow.querySelectorAll("button")) btn.tabIndex = active ? 0 : -1;
  }

  function update(next) {
    const wasRunning = p.state === "running";
    p = next;
    if (wasRunning && p.state !== "running") thumbState = null;
    if (!wasRunning && p.state === "running") { thumbState = null; live.refresh(); }
    renderThumb();
    renderBody();
    renderActions();
  }
  update(p0);
  return { el, update, destroy: () => live.stop() };
}

export function profileMenu(p, anchor) {
  const running = p.state === "running";
  openMenu(anchor, [
    { label: "Open details", icon: "info", onClick: () => openProfileDrawer(p.id) },
    { label: "Edit settings", icon: "edit", onClick: () => openProfileDrawer(p.id, "settings") },
    running ? { label: "Open tabs", icon: "tab", onClick: () => openProfileDrawer(p.id, "tabs") } : null,
    !running ? { label: "Start off-screen", icon: "play", onClick: () => actions.start(p.id, "offscreen").catch(() => {}) } : null,
    { label: "Open data folder", icon: "folder", onClick: () => api.post("/api/reveal", { what: "profile", id: p.id }).catch((e) => toast(e.message, { kind: "error" })) },
    { label: "Clone…", icon: "clone", onClick: () => cloneDialog(p) },
    "-",
    { label: "Delete…", icon: "trash", danger: true, onClick: () => confirmDelete(p).catch(() => {}) },
  ]);
}
