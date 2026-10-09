// Application state, change notifications and the actions every view shares.

import { api, enc } from "./api.js";
import { toast } from "./ui.js";

export const state = {
  ready: false,
  locked: false,
  live: false,
  version: "",
  dataRoot: "",
  platform: "",
  secretsBackend: "",
  profiles: new Map(),
  proxies: [],
  identities: [],
  help: [],
  activity: [],
  browsers: [],
  settings: null,
  clients: null,
  chatgpt: null,
  trash: null,
  meta: null,
  proxyTest: null, // {job, done, total, finished}
  pending: new Map(), // profile id -> "starting" | "stopping" | ...
  aiActive: new Map(), // profile id -> {ts, tool, summary} of the AI's latest tool call
};

export const AI_ACTIVE_MS = 8000;

/** Remember that the AI just used a profile (cards show "AI working" for a few seconds). */
export function markAiActive(event) {
  if (!event || !event.profile_id || event.source === "ui" || event.source === "cli") return;
  state.aiActive.set(event.profile_id, { ts: Date.now(), tool: event.tool, summary: event.summary, client: event.client });
  setTimeout(() => notify("profiles"), AI_ACTIVE_MS + 50);
}

export function aiActivity(profileId) {
  const entry = state.aiActive.get(profileId);
  return entry && Date.now() - entry.ts < AI_ACTIVE_MS ? entry : null;
}

const listeners = new Set();
let pendingTopics = new Set();
let scheduled = false;

/** Listen for changes. fn(topics: Set<string>) runs once per animation frame at most. */
export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function notify(...topics) {
  topics.forEach((t) => pendingTopics.add(t));
  if (scheduled) return;
  scheduled = true;
  requestAnimationFrame(() => {
    scheduled = false;
    const topicsNow = pendingTopics;
    pendingTopics = new Set();
    for (const fn of [...listeners]) {
      try { fn(topicsNow); } catch (err) { console.error(err); }
    }
  });
}

// ------------------------------------------------------------------ loaders

export async function loadOverview() {
  const data = await api.get("/api/overview");
  state.version = data.version;
  state.dataRoot = data.data_root;
  state.platform = data.platform;
  state.secretsBackend = data.secrets_backend;
  state.profiles = new Map(data.profiles.map((p) => [p.id, p]));
  state.proxies = data.proxies;
  state.identities = data.identities;
  state.help = data.help;
  state.browsers = data.browsers;
  state.settings = data.settings;
  state.chatgpt = data.chatgpt;
  state.trashCount = data.trash_count;
  if (data.clients) state.clients = data.clients;
  state.ready = true;
  notify("profiles", "proxies", "identities", "help", "settings", "chatgpt", "clients", "ready");
}

export async function loadMeta() {
  state.meta = await api.get("/api/meta");
  notify("meta");
}

export async function loadProfiles() {
  const data = await api.get("/api/profiles");
  state.profiles = new Map(data.profiles.map((p) => [p.id, p]));
  syncHelpFromProfiles();
  notify("profiles", "help");
}

export async function loadProxies() {
  state.proxies = (await api.get("/api/proxies")).proxies;
  notify("proxies");
}

export async function loadIdentities() {
  state.identities = (await api.get("/api/identities")).identities;
  notify("identities");
}

export async function loadActivity() {
  state.activity = (await api.get("/api/activity?limit=300")).events;
  notify("activity");
}

export async function loadSettings() {
  state.settings = await api.get("/api/settings");
  notify("settings");
}

export async function loadTrash() {
  state.trash = (await api.get("/api/trash")).trash;
  state.trashCount = state.trash.length;
  notify("trash");
}

export async function loadClients(refresh = false) {
  state.clients = (await api.get(`/api/clients${refresh ? "?refresh=1" : ""}`)).clients;
  notify("clients");
}

export async function loadChatGPT() {
  state.chatgpt = await api.get("/api/chatgpt");
  notify("chatgpt");
}

// ------------------------------------------------------------------ profile updates

export function upsertProfile(view) {
  if (!view || !view.id) return;
  // A pending start/stop is cleared by the action itself when its request returns.
  state.profiles.set(view.id, view);
  syncHelpFromProfiles();
  notify("profiles", "help");
}

export function removeProfile(id) {
  state.profiles.delete(id);
  state.pending.delete(id);
  syncHelpFromProfiles();
  notify("profiles", "help");
}

function syncHelpFromProfiles() {
  const help = [];
  for (const p of state.profiles.values()) {
    for (const req of (p.control && p.control.help) || []) help.push({ ...req, profile_name: p.name });
  }
  help.sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  state.help = help;
}

export function profileList() {
  return [...state.profiles.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
}

/** The status shown to the user, combining the runtime state and the control state. */
export function displayStatus(p) {
  const pending = state.pending.get(p.id);
  const runtime = pending || p.state;
  const control = p.control || {};
  if (control.help && control.help.length) return { key: "help", label: "Needs you", runtime };
  if (control.paused) return { key: "paused", label: runtime === "running" ? "You're in control" : "AI paused", runtime };
  const labels = { running: "Running", starting: "Starting…", stopping: "Stopping…", stopped: "Stopped", crashed: "Crashed" };
  return { key: runtime, label: labels[runtime] || runtime, runtime };
}

export function proxyById(id) {
  return state.proxies.find((p) => p.id === id) || null;
}

// ------------------------------------------------------------------ actions

async function guarded(promise, success) {
  try {
    const result = await promise;
    if (success) toast(typeof success === "function" ? success(result) : success, { kind: "success" });
    return result;
  } catch (err) {
    toast(err.message, { kind: "error", title: "Something went wrong" });
    throw err;
  }
}

export const actions = {
  async start(id, window) {
    state.pending.set(id, "starting");
    notify("profiles");
    try {
      const view = await guarded(api.post(`/api/profiles/${enc(id)}/start`, window ? { window } : {}));
      state.pending.delete(id);
      upsertProfile(view);
      return view;
    } catch (err) {
      state.pending.delete(id);
      notify("profiles");
      throw err;
    }
  },
  async stop(id) {
    state.pending.set(id, "stopping");
    notify("profiles");
    try {
      const view = await guarded(api.post(`/api/profiles/${enc(id)}/stop`));
      state.pending.delete(id);
      upsertProfile(view);
      return view;
    } catch (err) {
      state.pending.delete(id);
      notify("profiles");
      throw err;
    }
  },
  async focus(id, target) {
    const result = await guarded(api.post(`/api/profiles/${enc(id)}/focus`, target ? { target } : {}));
    if (result && !result.focused) {
      toast("Windows did not let the window come to the front. Look for it in the taskbar.", { kind: "info", title: "Window is flashing in the taskbar" });
    }
    return result;
  },
  async takeControl(id) {
    const p = state.profiles.get(id);
    const result = await guarded(api.post(`/api/profiles/${enc(id)}/pause`, { note: "" }));
    upsertProfile(result.profile);
    toast(`The AI won't touch "${p ? p.name : id}" until you hand it back.`, { kind: "success", title: "You're in control" });
    if (result.profile.state === "running") {
      actions.focus(id).catch(() => {});
    } else if (result.profile.state === "stopped" || result.profile.state === "crashed") {
      await actions.start(id, "normal");
      actions.focus(id).catch(() => {});
    }
  },
  async handBack(id) {
    const p = state.profiles.get(id);
    const result = await guarded(api.post(`/api/profiles/${enc(id)}/resume`));
    upsertProfile(result.profile);
    toast(`The AI can use "${p ? p.name : id}" again.`, { kind: "success", title: "Handed back to the AI" });
  },
  async resolveHelp(profileId, requestId, status, note = "") {
    const result = await guarded(api.post(`/api/help/${enc(profileId)}/${enc(requestId)}`, { status, note }));
    upsertProfile(result.profile);
    return result;
  },
  async deleteProfile(id) {
    const p = state.profiles.get(id);
    const result = await guarded(api.del(`/api/profiles/${enc(id)}`), `"${p ? p.name : id}" was moved to the trash.`);
    removeProfile(id);
    return result;
  },
};

export function setLive(live) {
  if (state.live === live) return;
  state.live = live;
  notify("live");
}
