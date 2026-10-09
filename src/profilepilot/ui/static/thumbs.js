// Live thumbnails of running profiles. Only visible thumbnails refresh, only while the Manager
// window is visible, and the server throttles captures to one per profile every 2 seconds.

const entries = new Set();
let timer = null;

const observer = typeof IntersectionObserver !== "undefined"
  ? new IntersectionObserver((items) => {
    for (const item of items) {
      const entry = item.target.__thumb;
      if (entry) {
        entry.visible = item.isIntersecting;
        if (entry.visible && entry.due <= Date.now()) refresh(entry);
      }
    }
  }, { rootMargin: "120px" })
  : null;

function schedule() {
  if (timer) return;
  timer = setInterval(() => {
    if (document.hidden) return;
    const now = Date.now();
    for (const entry of entries) {
      if (!entry.el.isConnected) { detach(entry); continue; }
      if (entry.visible && entry.active() && !entry.loading && entry.due <= now) refresh(entry);
    }
    if (!entries.size) { clearInterval(timer); timer = null; }
  }, 500);
}

async function refresh(entry) {
  if (entry.loading || !entry.active()) return;
  entry.loading = true;
  entry.due = Date.now() + entry.interval;
  try {
    const res = await fetch(`/api/profiles/${encodeURIComponent(entry.id)}/screenshot`, { cache: "no-store", credentials: "same-origin" });
    if (res.status === 200) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const probe = new Image();
      probe.src = url;
      try { await probe.decode(); } catch (err) { /* keep going: the img element will try */ }
      if (!entry.el.isConnected) { URL.revokeObjectURL(url); return; }
      const old = entry.url;
      entry.img.src = url;
      entry.img.classList.add("loaded");
      entry.url = url;
      if (old) setTimeout(() => URL.revokeObjectURL(old), 1000);
      entry.onState && entry.onState("live");
    } else if (res.status === 204) {
      entry.onState && entry.onState(res.headers.get("X-Thumb-State") || "unavailable");
    }
  } catch (err) {
    /* offline: try again later */
  } finally {
    entry.loading = false;
  }
}

function detach(entry) {
  entries.delete(entry);
  if (observer) observer.unobserve(entry.el);
  if (entry.url) URL.revokeObjectURL(entry.url);
  delete entry.el.__thumb;
}

/**
 * Keep `img` (inside `el`) showing profile `id` while `active()` returns true.
 * Returns {refresh, stop}.
 */
export function attachThumb(el, img, id, { active, interval = 3000, onState } = {}) {
  const entry = { el, img, id, active: active || (() => true), interval, onState, visible: !observer, due: 0, loading: false, url: null };
  el.__thumb = entry;
  entries.add(entry);
  if (observer) observer.observe(el);
  schedule();
  return {
    refresh: () => { entry.due = 0; refresh(entry); },
    stop: () => detach(entry),
    clear: () => {
      img.classList.remove("loaded");
      if (entry.url) { URL.revokeObjectURL(entry.url); entry.url = null; }
      img.removeAttribute("src");
    },
  };
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) for (const entry of entries) entry.due = 0;
});
