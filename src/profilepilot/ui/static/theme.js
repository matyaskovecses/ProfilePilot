// Applies the saved theme before the first paint (loaded as a classic, render-blocking script).
(function () {
  var theme = "system";
  try { theme = localStorage.getItem("pp-theme") || "system"; } catch (e) { /* storage blocked */ }
  if (theme !== "light" && theme !== "dark") theme = "system";
  document.documentElement.setAttribute("data-theme", theme);
})();
