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
  proxyTest: null, // {job, done, total, finished, cancelled}
  proxyTesting: new Set(), // ids of the proxies a running test job has not reported yet
  pending: new Map(), // profile id -> "starting" | "stopping" | ...
  aiActive: new Map(), // profile id -> {ts, tool, summary} of the AI's latest tool call
};

export const AI_ACTIVE_MS = 8000;

/** Where secrets are kept, for copy like "Passwords go to …". */
export function secretPlace() {
  if (state.secretsBackend === "keyring") return "your OS keychain";
  return state.platform === "win32" ? "an encrypted file in your data folder" : "a private file in your data folder";
}

/** Short label of the secret store for the Settings badge. */
export function secretStoreLabel() {
  if (state.secretsBackend === "keyring") return "OS keychain";
  return state.platform === "win32" ? "Encrypted file" : "Private file";
}

/** Remember that the AI just used a profile (cards show "AI working" for a few seconds). */
export function markAiActive(event) {
  if (!event || !event.profile_id || event.source === "ui" || event.source === "cli" || event.blocked) return;
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

// ------------------------------------------------------------------ proxy tests

/** A test job started (by this window, a toast, or another window): mark its rows "Testing". */
export function proxyTestStarted(job) {
  if (!job) return;
  // The "started" event and the POST's answer both announce a job: the later one must not mark rows the
  // job already reported (or a job that already finished) as "Testing" again.
  if (state.proxyTest && state.proxyTest.job === job.job) return;
  state.proxyTest = { job: job.job, done: job.done || 0, total: job.total, finished: false };
  for (const id of job.ids || []) state.proxyTesting.add(id);
  notify("proxy-test", "proxies");
}

const queuedTest = new Set(); // proxies to test once the running job ends (asked for while it ran)

export async function startProxyTest(ids, { retry = false } = {}) {
  const job = await api.post("/api/proxies/test", ids && ids.length ? { ids } : {});
  if (job.already_running) {
    // The server did not start a job: another one (from another window, a toast or a double click) is
    // running. Show its progress but leave its rows alone (the ones it already tested are not "Testing"
    // again); the proxies it does not cover are tested when it finishes.
    const wanted = ids && ids.length ? ids : state.proxies.map((p) => p.id);
    if (state.proxyTest && state.proxyTest.job === job.job && state.proxyTest.finished) {
      // It already reported "finished" and is wrapping up: ask again in a moment.
      wanted.forEach((id) => queuedTest.add(id));
      setTimeout(() => proxyTestEnded(), 500);
      return job;
    }
    const covered = new Set(job.ids || []);
    const missing = wanted.filter((id) => !covered.has(id));
    missing.forEach((id) => queuedTest.add(id));
    state.proxyTest = { job: job.job, done: job.done || 0, total: job.total, finished: false };
    notify("proxy-test", "proxies");
    if (missing.length && !retry) toast("Another proxy test is running. These are tested when it finishes.", { kind: "info", title: "Proxy test queued" });
    return job;
  }
  proxyTestStarted(job);
  return job;
}

/** The job this window showed is over, or may be (`cancelled`: the user stopped it, so nothing queued runs). */
export function proxyTestEnded({ cancelled = false } = {}) {
  const ids = [...queuedTest].filter((id) => proxyById(id)); // (minus the ones deleted meanwhile)
  queuedTest.clear();
  if (cancelled || !ids.length) return;
  startProxyTest(ids, { retry: true }).catch((err) => toast(err.message, { kind: "error" }));
}

/** Forget the test this window shows: its "finished" event was missed (offline) or never comes. */
export function proxyTestReset() {
  state.proxyTest = null;
  state.proxyTesting.clear();
  notify("proxy-test", "proxies");
}

export async function cancelProxyTest() {
  const result = await api.del("/api/proxies/test");
  if (result && result.cancelled === false) {
    // Nothing was running: the job ended while this window missed its "finished" event.
    queuedTest.clear();
    proxyTestReset();
  }
  return result;
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

const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" });

export function profileList() {
  return [...state.profiles.values()].sort(byName);
}

/** Sort rank: what needs the user first (help, then their own pause), then running, crashed, stopped. */
export function profileRank(p) {
  const control = p.control || {};
  if (control.help && control.help.length) return 0;
  if (control.paused) return 1;
  const runtime = state.pending.get(p.id) || p.state;
  if (runtime === "running" || runtime === "starting" || runtime === "stopping") return 2;
  if (runtime === "crashed") return 3;
  return 4;
}

/** The profiles grid order: by {@link profileRank}, then by name. */
export function profilesByPriority() {
  return [...state.profiles.values()].sort((a, b) => profileRank(a) - profileRank(b) || byName(a, b));
}

/** The status shown to the user, combining the runtime state and the control state. */
/** The window mode of the current run (a headless profile the user took control of runs in a normal window). */
export function runWindow(p) {
  return (p.runtime && p.runtime.window) || p.window;
}

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

/** Is `name` just the proxy's address (the default name of an unnamed proxy)? */
export function isAddressName(p) {
  const address = `${p.host}:${p.port}`;
  return !p.name || p.name === address || p.name.startsWith(`${address}#`);
}

/** Where a tested proxy exits: "Frankfurt am Main" (the country code is shown as a badge). */
export function proxyPlace(check) {
  if (!check || !check.ok) return "";
  return check.city || check.region || check.country || "";
}

/** A readable proxy name: its own name, or for an unnamed one the place it exits from, else its host. */
export function proxyLabel(p) {
  if (!p) return "";
  if (!isAddressName(p)) return p.name;
  const check = p.last_check || (p.ok ? { ok: p.ok, city: p.city, country_code: p.country_code } : null);
  return proxyPlace(check) || p.host || p.name;
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
  /** `background`: off-screen for this run only (headless if that is the profile's own mode). */
  async start(id, window, { background = false } = {}) {
    state.pending.set(id, "starting");
    notify("profiles");
    try {
      const body = window ? { window } : {};
      if (background) body.background = true;
      const view = await guarded(api.post(`/api/profiles/${enc(id)}/start`, body));
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
  /** Start a stopped profile in a normal window (if needed) and bring its window up. */
  async openWindow(id) {
    const p = state.profiles.get(id);
    if (!p || p.state !== "running") await actions.start(id, "normal");
    return actions.focus(id);
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
    const result = await guarded(api.del(`/api/profiles/${enc(id)}`));
    removeProfile(id);
    toast(`"${p ? p.name : id}" was moved to the trash.`, {
      kind: "success", timeout: 6000,
      action: { label: "Undo", onClick: () => actions.restore(result.trash_id).catch(() => {}) },
    });
    return result;
  },
  async restore(trashId) {
    const view = await guarded(api.post(`/api/trash/${enc(trashId)}/restore`));
    upsertProfile(view);
    if (state.trash) loadTrash().catch(() => {});
    toast(`Restored "${view.name}".`, { kind: "success" });
    return view;
  },
};

export function setLive(live) {
  if (state.live === live) return;
  state.live = live;
  notify("live");
}
