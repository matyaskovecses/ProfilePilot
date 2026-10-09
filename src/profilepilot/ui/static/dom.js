// Small DOM and formatting helpers. Text is always set with textContent (never innerHTML) so that
// page titles, URLs and AI-written help messages can never inject markup.

const SVG_NS = "http://www.w3.org/2000/svg";
const PROPS = new Set(["value", "checked", "disabled", "selected", "hidden", "readOnly", "required", "multiple", "indeterminate", "open"]);

/**
 * h("div.card", {onclick, dataset, attrs, style}, ...children)
 * The tag may carry classes ("button.btn.primary"). Children: nodes, strings, numbers, arrays, null.
 */
export function h(tag, props, ...children) {
  if (props == null || typeof props !== "object" || props instanceof Node || Array.isArray(props)) {
    if (props != null) children.unshift(props);
    props = {};
  }
  const [name, ...classes] = tag.split(".");
  const el = document.createElement(name || "div");
  if (classes.length) el.classList.add(...classes);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") {
      for (const c of [].concat(value)) if (c) String(c).split(/\s+/).filter(Boolean).forEach((x) => el.classList.add(x));
    } else if (key === "dataset") {
      Object.assign(el.dataset, value);
    } else if (key === "style") {
      for (const [prop, v] of Object.entries(value)) {
        if (v == null) continue;
        if (prop.startsWith("--")) el.style.setProperty(prop, String(v));
        else el.style[prop] = v;
      }
    } else if (key === "text") {
      el.textContent = value;
    } else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (PROPS.has(key)) {
      el[key] = value;
    } else if (key === "attrs") {
      for (const [a, v] of Object.entries(value)) if (v != null && v !== false) el.setAttribute(a, v === true ? "" : String(v));
    } else if (key in el && typeof value !== "string") {
      el[key] = value;
    } else {
      el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false || child === true) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function replace(el, ...children) {
  clear(el);
  return append(el, children);
}

export function icon(name, cls = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", `icon ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", `/static/icons.svg#i-${name}`);
  svg.append(use);
  return svg;
}

/** A small SVG sparkline of latency points ({ms, ok}). */
export function sparkline(points, width = 64, height = 20) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("aria-hidden", "true");
  const pts = (points || []).slice(-16);
  const values = pts.map((p) => (p.ok && p.ms != null ? p.ms : null));
  const valid = values.filter((v) => v != null);
  if (pts.length < 2 || !valid.length) return svg;
  const max = Math.max(...valid) * 1.15 || 1;
  const min = Math.min(...valid) * 0.8;
  const step = (width - 4) / (pts.length - 1);
  const coords = [];
  pts.forEach((p, i) => {
    const x = 2 + i * step;
    const v = values[i];
    const y = v == null ? height - 2 : height - 2 - ((v - min) / Math.max(1, max - min)) * (height - 4);
    coords.push([x, y, v == null]);
  });
  const line = document.createElementNS(SVG_NS, "polyline");
  line.setAttribute("points", coords.filter((c) => !c[2]).map((c) => `${c[0].toFixed(1)},${c[1].toFixed(1)}`).join(" "));
  svg.append(line);
  const last = coords[coords.length - 1];
  const dot = document.createElementNS(SVG_NS, "circle");
  dot.setAttribute("cx", last[0].toFixed(1));
  dot.setAttribute("cy", last[1].toFixed(1));
  dot.setAttribute("r", "2.2");
  if (last[2]) dot.setAttribute("class", "fail");
  svg.append(dot);
  return svg;
}

export function debounce(fn, ms = 250) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export function initials(name) {
  const words = String(name || "?").replace(/[^\p{L}\p{N}\s_-]/gu, " ").split(/[\s_-]+/).filter(Boolean);
  if (!words.length) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

export function hue(text) {
  let hash = 0;
  for (const ch of String(text || "")) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  return hash % 360;
}

export function avatar(name, cls = "", seed) {
  return h("div.avatar", { class: cls, style: { "--hue": hue(seed || name) }, attrs: { "aria-hidden": "true" } }, initials(name));
}

// ------------------------------------------------------------------ formatting

const rtf = typeof Intl !== "undefined" && Intl.RelativeTimeFormat ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }) : null;

export const fmt = {
  ago(iso) {
    if (!iso) return "never";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const s = Math.round((then - Date.now()) / 1000);
    const abs = Math.abs(s);
    if (abs < 45) return "just now";
    const table = [[60, "second", 1], [3600, "minute", 60], [86400, "hour", 3600], [604800, "day", 86400], [2629800, "week", 604800], [31557600, "month", 2629800], [Infinity, "year", 31557600]];
    for (const [limit, unit, div] of table) {
      if (abs < limit) return rtf ? rtf.format(Math.round(s / div), unit) : `${Math.round(abs / div)} ${unit}s ago`;
    }
    return "";
  },
  time(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  },
  clock(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  },
  dateTime(iso, seconds = false) {
    if (!iso) return "";
    return new Date(iso).toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      second: seconds ? "2-digit" : undefined });
  },
  /** A length of time in words: "45 s", "5 min", "2 h 5 min", "3 days". */
  span(seconds) {
    const s = Math.max(0, Math.round(seconds || 0));
    if (s < 60) return `${s} s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} min`;
    const hrs = Math.floor(m / 60);
    if (hrs < 48) return m % 60 ? `${hrs} h ${m % 60} min` : `${hrs} h`;
    const days = Math.floor(hrs / 24);
    return `${days} days`;
  },
  /** "5 min ago" style, short. */
  agoShort(iso) {
    if (!iso) return "";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (Number.isNaN(s)) return "";
    if (s < 45) return "just now";
    return `${fmt.span(s)} ago`;
  },
  day(iso) {
    const d = new Date(iso);
    const today = new Date();
    const yesterday = new Date(Date.now() - 86400000);
    if (d.toDateString() === today.toDateString()) return "Today";
    if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
  },
  duration(seconds) {
    const s = Math.max(0, Math.round(seconds || 0));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m`;
    const hrs = Math.floor(m / 60);
    if (hrs < 48) return `${hrs}h ${m % 60}m`;
    return `${Math.floor(hrs / 24)}d ${hrs % 24}h`;
  },
  since(iso) {
    if (!iso) return "";
    return fmt.duration((Date.now() - new Date(iso).getTime()) / 1000);
  },
  bytes(n) {
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
  },
  ms(n) {
    if (n == null) return "";
    return n < 1000 ? `${n} ms` : `${(n / 1000).toFixed(1)} s`;
  },
  plural(n, one, many) {
    return `${n} ${n === 1 ? one : many || one + "s"}`;
  },
};

export function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
  const area = h("textarea", { value: text, attrs: { readonly: true } });
  area.classList.add("sr-only");
  document.body.append(area);
  area.select();
  try { document.execCommand("copy"); } finally { area.remove(); }
  return Promise.resolve();
}

export function isTyping(target) {
  if (!target) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}
