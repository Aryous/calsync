// Theme and language before first paint: ?theme= / ?lang=, then localStorage, then the system.
// Loaded as a classic (blocking) script, first in <head>; the pages' CSP allows no inline script.
(function () {
  var d = document.documentElement, q = new URLSearchParams(location.search), s = null, l = null;
  try { s = localStorage.getItem("calsync.theme"); l = localStorage.getItem("calsync.lang"); } catch (e) {}
  var p = q.get("theme");
  if (p !== "light" && p !== "dark") p = s;
  var dark = p === "dark" || (p !== "light" && window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
  d.setAttribute("data-theme", dark ? "dark" : "light");
  var ql = q.get("lang");
  if (ql !== "zh" && ql !== "en") ql = l === "zh" || l === "en" ? l : ((navigator.language || "").toLowerCase().indexOf("zh") === 0 ? "zh" : "en");
  d.setAttribute("lang", ql === "zh" ? "zh-CN" : "en");
})();
