// Theme selection: system (prefers-color-scheme), light or dark, remembered per browser.

const listeners = new Set();

export function currentTheme() {
  const value = document.documentElement.getAttribute("data-theme");
  return value === "light" || value === "dark" ? value : "system";
}

export function applyTheme(theme) {
  const value = theme === "light" || theme === "dark" ? theme : "system";
  document.documentElement.setAttribute("data-theme", value);
  try { localStorage.setItem("pp-theme", value); } catch (err) { /* storage blocked */ }
  listeners.forEach((fn) => fn(value));
}

export function onThemeChange(fn) {
  listeners.add(fn);
}
