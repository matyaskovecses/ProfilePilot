// JSON API client and the live event stream. The session cookie is HttpOnly and sent automatically
// (same origin); state-changing requests carry the Origin header the server checks.

export class ApiError extends Error {
  constructor(message, status, code, data) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

const unauthorizedHandlers = new Set();
export function onUnauthorized(fn) {
  unauthorizedHandlers.add(fn);
}

async function send(method, path, body, accept = "application/json") {
  const init = { method, headers: { Accept: accept }, credentials: "same-origin", cache: "no-store" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    throw new ApiError("ProfilePilot Manager is not reachable. Is it still running?", 0, "offline");
  }
  if (res.status === 401) {
    unauthorizedHandlers.forEach((fn) => fn());
    throw new ApiError("Your session has ended. Open ProfilePilot Manager again.", 401, "unauthorized");
  }
  return res;
}

async function failure(res) {
  let data = null;
  try {
    data = await res.json();
  } catch (err) {
    data = null;
  }
  return new ApiError((data && data.error) || `Request failed (${res.status})`, res.status, data && data.code, data);
}

async function request(method, path, body) {
  const res = await send(method, path, body);
  if (res.status === 204) return null;
  if (!res.ok) throw await failure(res);
  try {
    return await res.json();
  } catch (err) {
    return null;
  }
}

/**
 * A file the server sends as an attachment (GET, or POST with `body`) -> {blob, filename, headers}.
 * Errors come back as JSON like every other request and are thrown as ApiError.
 */
async function download(path, body) {
  const res = await send(body === undefined ? "GET" : "POST", path, body, "*/*");
  if (!res.ok) throw await failure(res);
  const blob = await res.blob();
  const match = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "");
  return { blob, filename: match ? match[1] : "download", headers: res.headers };
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body = {}) => request("POST", path, body),
  patch: (path, body = {}) => request("PATCH", path, body),
  put: (path, body = {}) => request("PUT", path, body),
  del: (path, body) => request("DELETE", path, body),
  download,
};

export const enc = encodeURIComponent;

/**
 * Subscribe to /api/events. `handlers` maps event names to callbacks; `onStatus(live)` reports
 * whether the stream is connected. Returns a function that closes the stream.
 */
export function connectEvents(handlers, onStatus) {
  let source = null;
  let closed = false;
  let retryTimer = null;

  const open = () => {
    if (closed) return;
    source = new EventSource("/api/events");
    source.addEventListener("ready", () => onStatus && onStatus(true));
    for (const [name, fn] of Object.entries(handlers)) {
      source.addEventListener(name, (event) => {
        let data = null;
        try { data = JSON.parse(event.data); } catch (err) { return; }
        try { fn(data); } catch (err) { console.warn("event handler failed", name, err); }
      });
    }
    source.onerror = () => {
      onStatus && onStatus(false);
      if (source.readyState === EventSource.CLOSED) {
        source = null;
        // Check whether the session or the server is gone before reconnecting.
        request("GET", "/api/ping").then(() => {
          retryTimer = setTimeout(open, 1500);
        }).catch((err) => {
          if (err.status !== 401) retryTimer = setTimeout(open, 3000);
        });
      }
    };
  };
  open();
  return () => {
    closed = true;
    clearTimeout(retryTimer);
    if (source) source.close();
  };
}
