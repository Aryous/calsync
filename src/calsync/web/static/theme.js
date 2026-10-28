// Theme and language before first paint. Loaded as a classic (blocking) script, first in <head>:
// the pages' CSP allows no inline script.
// - theme: ?theme=light|dark, else the stored choice ("calsync.theme"), else the system's;
// - language: ?lang=zh|en (remembered), else the stored choice ("calsync.lang"), else Chinese.
(function () {
  var d = document.documentElement, q = new URLSearchParams(location.search), s = null, l = null;
  try { s = localStorage.getItem("calsync.theme"); l = localStorage.getItem("calsync.lang"); } catch (e) {}
  var p = q.get("theme");
  if (p !== "light" && p !== "dark") p = s;
  var dark = p === "dark" || (p !== "light" && window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
  d.setAttribute("data-theme", dark ? "dark" : "light");
  if (p !== "light" && p !== "dark" && window.matchMedia) {  // following the system: keep following it
    var mq = matchMedia("(prefers-color-scheme: dark)");
    var follow = function (e) { d.setAttribute("data-theme", e.matches ? "dark" : "light"); };
    if (mq.addEventListener) mq.addEventListener("change", follow);
  }
  var ql = q.get("lang");
  if (ql === "zh" || ql === "en") {
    try { localStorage.setItem("calsync.lang", ql); } catch (e) {}
  } else {
    ql = l === "en" ? "en" : "zh";
  }
  d.setAttribute("lang", ql === "zh" ? "zh-CN" : "en");
})();
