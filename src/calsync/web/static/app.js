// calsync web UI: overview (#/) and run history (#/runs).
// Preact + htm from the vendored standalone module; no build step.
import { html, render, useState, useEffect, useRef, useCallback } from "/static/vendor/htm-preact-standalone.mjs";
import { t, getLang, setLang, locale } from "/static/i18n.js";

const POLL_MS = 30000;
const SYNC_POLL_MS = 3000;
const SYNC_TIMEOUT_MS = 90000;
const THEME_KEY = "calsync.theme";

// ---------------------------------------------------------------- utilities

const cx = (...parts) => parts.filter(Boolean).join(" ");

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) {
    /* storage unavailable: the choice lasts for this page only */
  }
  return null;
}

class ApiError extends Error {
  constructor(kind, status, message) {
    super(message || kind);
    this.kind = kind;
    this.status = status;
  }
}

async function api(path, options) {
  let res;
  try {
    res = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
  } catch (e) {
    throw new ApiError("offline", 0, e && e.message);
  }
  if (res.status === 401) {
    window.location.replace("/");
    throw new ApiError("unpaired", 401);
  }
  let body = null;
  try {
    body = await res.json();
  } catch (e) {
    body = null;
  }
  if (!res.ok) {
    const err = (body && body.error) || {};
    const kind = err.kind || (res.status === 503 ? "no_state" : "http");
    throw new ApiError(kind, res.status, err.message || res.statusText);
  }
  return body;
}

// Times: Intl in the browser's time zone, 24-hour clock.
const fmtCache = {};
function fmt(opts) {
  const key = locale() + JSON.stringify(opts);
  if (!fmtCache[key]) fmtCache[key] = new Intl.DateTimeFormat(locale(), opts);
  return fmtCache[key];
}
const clock = (d) => fmt({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(d);
const monthDay = (d) => fmt({ month: "numeric", day: "numeric" }).format(d);

function dayIndex(d) {
  const today = new Date();
  const a = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
  const b = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  return Math.round((b - a) / 86400000);
}

// An all-day start is a local date; read its first 10 characters as such.
function eventStart(ev) {
  if (ev.all_day && typeof ev.start === "string") {
    const [y, m, d] = ev.start.slice(0, 10).split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  return new Date(ev.start);
}

function stamp(d) {
  // time of day for today, "10/24 14:51" otherwise
  return dayIndex(d) === 0 ? clock(d) : `${monthDay(d)} ${clock(d)}`;
}

function ago(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 45) return t("just now");
  if (s < 3600) return t("{n} min ago", { n: Math.max(1, Math.round(s / 60)) });
  if (s < 86400) return t("{n} h ago", { n: Math.round(s / 3600) });
  return t("{n} d ago", { n: Math.round(s / 86400) });
}

function duration(ms) {
  if (ms == null) return "";
  if (ms < 60000) return t("{n} s", { n: (ms / 1000).toFixed(1) });
  return t("{m} min {s} s", { m: Math.floor(ms / 60000), s: Math.round((ms % 60000) / 1000) });
}

function alarmPhrase(m) {
  if (m === 0) return t("at start");
  if (m < 0) return t("{n} min after start", { n: -m });
  if (m % 1440 === 0) return t("{n} d before", { n: m / 1440 });
  if (m % 60 === 0) return t("{n} h before", { n: m / 60 });
  return t("{n} min before", { n: m });
}

function alarmsText(list) {
  if (!list || !list.length) return t("No alerts");
  return t("Alerts: {list}", { list: list.map(alarmPhrase).join(t(", ")) });
}

// Wrap figures in tabular carbon-purple spans.
function Fig({ text }) {
  const parts = String(text).split(/(\d[\d:.,]*(?:[–-]\d[\d:.,]*)?)/);
  return parts.map((p, i) => (i % 2 ? html`<span class="num">${p}</span>` : p));
}

function hostLabel(host) {
  if (host === "Google Calendar") return t("Google");
  return host || "";
}

function healthBadge(h) {
  switch (h) {
    case "ok":
      return t("OK");
    case "auth":
      return t("Sign-in expired");
    case "blocked":
      return t("Blocked");
    case "failing":
      return t("Failed");
    case "never_run":
      return t("Not run yet");
    default:
      return h || "";
  }
}

function actionLabel(a) {
  switch (a) {
    case "created":
      return t("Added");
    case "updated":
      return t("Changed");
    case "reverted":
      return t("Reverted");
    case "deleted":
      return t("Deleted");
    case "protected":
      return t("Protected");
    case "blocked":
      return t("Blocked");
    default:
      return a;
  }
}

function runStatusLabel(s) {
  switch (s) {
    case "ok":
      return t("OK");
    case "partial":
      return t("Partial");
    case "failed":
      return t("Failed");
    case "blocked":
      return t("Blocked");
    case "skipped":
      return t("Skipped");
    default:
      return s;
  }
}

const isReadOnly = (status) => !!status && (!status.daemon || status.daemon.sync === false);

// ---------------------------------------------------------------- icons
// One stroke weight (1.75) on a 24 grid, round caps.
const ICONS = {
  refresh: html`<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8" /><path d="M4 4v4h4" /><path d="M4 13a8 8 0 0 0 14.3 4.9L20 16" /><path d="M20 20v-4h-4" />`,
  menu: html`<path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h16" />`,
  check: html`<path d="M5 12.5l4.5 4.5L19 7.5" />`,
  alert: html`<path d="M12 4l9 16H3z" /><path d="M12 10v4.5" /><path d="M12 17.5v.01" />`,
  arrow: html`<path d="M5 12h14" /><path d="M13 6l6 6-6 6" />`,
  plus: html`<path d="M12 5v14" /><path d="M5 12h14" />`,
  minus: html`<path d="M5 12h14" />`,
  edit: html`<path d="M4 20h4L19 9l-4-4L4 16z" /><path d="M13.5 6.5l4 4" />`,
  revert: html`<path d="M4 9h11a5 5 0 0 1 0 10H9" /><path d="M8 5L4 9l4 4" />`,
  copy: html`<rect x="8" y="8" width="12" height="12" rx="1.5" /><path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8" />`,
  chevron: html`<path d="M6 9l6 6 6-6" />`,
  request: html`<path d="M4 7h13" /><path d="M14 4l3 3-3 3" /><path d="M20 17H7" /><path d="M10 14l-3 3 3 3" />`,
  off: html`<path d="M6 6l12 12" /><path d="M18 6L6 18" />`,
};

function Icon({ name, size = 20, label }) {
  return html`<svg
    class=${cx("icon", `icon--${name}`)}
    width=${size}
    height=${size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.75"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden=${label ? undefined : "true"}
    role=${label ? "img" : undefined}
    aria-label=${label}
  >
    ${ICONS[name]}
  </svg>`;
}

// Authored service monogram: Google "G", iCloud "iC", other CalDAV hosts by initial.
function ServiceMark({ provider, host }) {
  let letters = (host || "?").slice(0, 1).toUpperCase();
  let kind = "dav";
  if (provider === "google") {
    letters = "G";
    kind = "google";
  } else if (host === "iCloud") {
    letters = "iC";
    kind = "icloud";
  }
  return html`<svg class=${cx("mark", `mark--${kind}`)} width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
    <rect x="0.5" y="0.5" width="23" height="23" rx="5" />
    <text x="12" y="16.5" text-anchor="middle">${letters}</text>
  </svg>`;
}

// ---------------------------------------------------------------- header + menu

function headerHealth(status) {
  if (!status) return null;
  if (isReadOnly(status)) return { cls: "demo", word: t("Read-only demo") };
  const h = status.summary && status.summary.health;
  if (h === "auth" || h === "blocked") return { cls: "attn", word: t("Needs you") };
  if (h === "failing") return { cls: "attn", word: t("Error") };
  if (h === "never_run") return { cls: "idle", word: t("Not synced yet") };
  return { cls: "ok", word: t("OK") };
}

function Menu({ lang, onLang, theme, onTheme, version }) {
  const [open, setOpen] = useState(false);
  const [about, setAbout] = useState(false);
  const wrap = useRef(null);
  const btn = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") {
        setOpen(false);
        if (btn.current) btn.current.focus();
      }
    };
    const onDown = (e) => {
      if (wrap.current && !wrap.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
    };
  }, [open]);

  const seg = (value, current, onPick, label) =>
    html`<button type="button" class="seg__opt" aria-pressed=${value === current ? "true" : "false"} onClick=${() => onPick(value)}>
      ${label}
    </button>`;

  return html`<div class="menu" ref=${wrap}>
    <button
      ref=${btn}
      type="button"
      class="icon-btn"
      aria-expanded=${open ? "true" : "false"}
      aria-controls="menu-panel"
      aria-label=${t("Menu")}
      onClick=${() => setOpen(!open)}
    >
      <${Icon} name=${open ? "off" : "menu"} size=${22} />
    </button>
    ${open &&
    html`<div class="menu__panel" id="menu-panel" role="group" aria-label=${t("Settings")}>
      <div class="menu__group">
        <span class="menu__label" id="menu-lang">${t("Language")}</span>
        <div class="seg" role="group" aria-labelledby="menu-lang">
          ${seg("zh", lang, onLang, "中文")} ${seg("en", lang, onLang, "English")}
        </div>
      </div>
      <div class="menu__group">
        <span class="menu__label" id="menu-theme">${t("Appearance")}</span>
        <div class="seg seg--3" role="group" aria-labelledby="menu-theme">
          ${seg("system", theme, onTheme, t("System"))} ${seg("light", theme, onTheme, t("Light"))}
          ${seg("dark", theme, onTheme, t("Dark"))}
        </div>
      </div>
      <div class="menu__group menu__group--about">
        <button type="button" class="menu__about" aria-expanded=${about ? "true" : "false"} onClick=${() => setAbout(!about)}>
          <span>${t("About calsync")}</span><${Icon} name="chevron" size=${18} />
        </button>
        ${about &&
        html`<p class="menu__note">
          <span class="wordmark wordmark--small">calsync</span>${version ? html` <span class="num">${version}</span>` : ""}<br />
          ${t("One-way sync from Google Calendar to iCloud. This page only reads; it never edits your events.")}
        </p>`}
      </div>
    </div>`}
  </div>`;
}

function Header({ status, sync, onSync, menu }) {
  const health = headerHealth(status);
  const demo = isReadOnly(status);
  return html`<header class="bar">
    <div class="bar__in">
      <a class="wordmark" href="#/">calsync</a>
      <span class="health" data-state=${health ? health.cls : "none"}>
        ${health && html`<span class="health__dot" aria-hidden="true"></span><span>${health.word}</span>`}
      </span>
      <button
        type="button"
        class=${cx("btn-sync", sync.busy && "is-busy")}
        disabled=${demo || sync.busy}
        aria-busy=${sync.busy ? "true" : "false"}
        title=${demo ? t("This calsync runs in demo mode and does not sync.") : undefined}
        onClick=${onSync}
      >
        <${Icon} name="refresh" size=${18} />
        <span>${demo ? t("Demo mode") : sync.busy ? t("Syncing…") : t("Sync now")}</span>
      </button>
      <${Menu} ...${menu} />
    </div>
  </header>`;
}

function Tabs({ route }) {
  return html`<nav class="tabs" aria-label=${t("Pages")}>
    <div class="tabs__in">
      <a href="#/" aria-current=${route === "overview" ? "page" : undefined}>${t("Overview")}</a>
      <a href="#/runs" aria-current=${route === "runs" ? "page" : undefined}>${t("Sync history")}</a>
    </div>
  </nav>`;
}

// ---------------------------------------------------------------- overview pieces

function StatusSentence({ status }) {
  const s = status.summary || {};
  const attention = status.attention || [];
  let title;
  let tone = "ok";
  if (attention.length) {
    title = attention.length === 1 ? t("1 thing needs you") : t("{n} things need you", { n: attention.length });
    tone = "attn";
  } else if (s.health === "never_run") {
    title = t("Not synced yet");
    tone = "idle";
  } else {
    title = t("All good");
  }
  const bits = [];
  if (s.last_sync_at) {
    bits.push(attention.length ? t("Last good sync {ago}", { ago: ago(s.last_sync_at) }) : t("Synced {ago}", { ago: ago(s.last_sync_at) }));
  } else if (s.health === "never_run") {
    bits.push(t("The first sync starts soon"));
  }
  if (s.last_sync_at) {
    bits.push(s.changes_24h ? t("{n} changes synced in 24 h", { n: s.changes_24h }) : t("No changes in 24 h"));
  }
  return html`<div class="status" data-tone=${tone} aria-live="polite">
    <h1 class="status__title"><${Fig} text=${title} /></h1>
    ${bits.length > 0 && html`<p class="status__detail"><${Fig} text=${bits.join(" · ")} /></p>`}
  </div>`;
}

function copyText(text, el) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(
      () => "copied",
      () => fallbackCopy(text, el)
    );
  }
  return Promise.resolve(fallbackCopy(text, el));
}

function fallbackCopy(text, el) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (ok) return "copied";
  } catch (e) {
    /* fall through to selecting the text */
  }
  if (el) {
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  return "selected";
}

function attentionMessage(item, status) {
  const rule = (status.rules || []).find((r) => r.name === item.rule);
  if (item.kind === "auth" && item.account) return t("The {account} account needs to be connected again.", { account: item.account });
  if (item.kind === "blocked") {
    const m = /(\d+) deletions/.exec(item.message || "");
    return m ? t("Sync paused: {n} deletions are waiting for your confirmation.", { n: m[1] }) : t("Sync paused: deletions are waiting for your confirmation.");
  }
  if (item.kind === "failing" && rule && rule.consecutive_failures) {
    return t("Sync has failed {n} times in a row.", { n: rule.consecutive_failures });
  }
  return item.message;
}

function AttentionCard({ item, status, blockedTitles }) {
  const codeRef = useRef(null);
  const [copied, setCopied] = useState(null);
  const onCopy = () =>
    copyText(item.command, codeRef.current).then((r) => {
      setCopied(r);
      setTimeout(() => setCopied(null), 2400);
    });
  const titles = item.kind === "blocked" ? blockedTitles : null;
  return html`<article class="attn">
    <${Icon} name="alert" size=${22} />
    <div class="attn__body">
      <p class="attn__msg"><${Fig} text=${attentionMessage(item, status)} /></p>
      <p class="attn__meta">${t("Sync line {rule}", { rule: item.rule })}</p>
      ${titles &&
      titles.length > 0 &&
      html`<ul class="attn__list">
        ${titles.map((x) => html`<li>${x || t("(untitled)")}</li>`)}
      </ul>`}
      ${item.command &&
      html`<p class="attn__how">${t("Run this on the computer that runs calsync:")}</p>
        <div class="cmd">
          <code ref=${codeRef}>${item.command}</code>
          <button type="button" class="btn-quiet" onClick=${onCopy}>
            <${Icon} name=${copied === "copied" ? "check" : "copy"} size=${18} />
            <span>${copied === "copied" ? t("Copied") : copied === "selected" ? t("Selected, copy it") : t("Copy")}</span>
          </button>
        </div>`}
    </div>
  </article>`;
}

function scopeText(scope) {
  if (!scope || scope.kind === "all") return t("All events");
  return t("Past {p} days – next {f} days", { p: scope.past_days, f: scope.future_days });
}

function PassEnd({ side, end, accounts }) {
  const acct = (accounts || []).find((a) => a.name === end.account) || {};
  return html`<div class=${cx("pass__end", `pass__end--${side}`)}>
    <span class="cap">${side === "from" ? t("From") : t("To")}</span>
    <span class="svc"><${ServiceMark} provider=${acct.provider} host=${acct.host} /><span>${hostLabel(acct.host) || end.account}</span></span>
    <span class="pass__cal">${end.calendar_name || end.calendar}</span>
    <span class="pass__acct">${end.account}</span>
  </div>`;
}

function ConnectionStrip({ rule, status }) {
  const bad = rule.health !== "ok" && rule.health !== "never_run";
  const foot = [scopeText(rule.scope), t("{n} series", { n: rule.copies || 0 })];
  if (rule.protected) foot.push(t("{n} protected", { n: rule.protected }));
  if (rule.out_of_window) foot.push(t("{n} outside the range", { n: rule.out_of_window }));
  return html`<section class="pass" aria-label=${t("Sync line {rule}", { rule: rule.name })}>
    <div class="pass__route">
      <${PassEnd} side="from" end=${rule.source} accounts=${status.accounts} />
      <div class="pass__arc">
        <svg viewBox="0 0 80 30" width="80" height="30" aria-hidden="true" class="arc">
          <path d="M6 26 Q40 -6 74 26" fill="none" stroke-dasharray="3 3.5" />
          <circle cx="6" cy="26" r="2.5" /><circle cx="74" cy="26" r="2.5" />
        </svg>
        <span class=${cx("badge", bad && "badge--bad", rule.health === "never_run" && "badge--idle")}>
          <${Icon} name=${bad ? "alert" : rule.health === "never_run" ? "minus" : "check"} size=${14} />
          ${healthBadge(rule.health)}
        </span>
      </div>
      <${PassEnd} side="to" end=${rule.target} accounts=${status.accounts} />
    </div>
    <div class="pass__foot">
      <span><${Fig} text=${foot.join(" · ")} /></span>
      <span class="pass__dir">${t("One-way")} <${Icon} name="arrow" size=${16} /></span>
    </div>
    ${status.schema &&
    status.schema.current === false &&
    html`<p class="pass__note">${t("State comes from an older calsync; delivery is unknown")}</p>`}
  </section>`;
}

function deliveryNote(d) {
  if (d === "delivered") return t("On iCloud and identical to Google.");
  if (d === "pending") return t("Changed in Google; the next sync delivers it.");
  return t("State comes from an older calsync; delivery is unknown");
}

function Delivery({ delivery, fresh, tilt }) {
  if (delivery === "delivered") {
    return html`<span class=${cx("stamp", fresh && "stamp--fresh")} style=${`--tilt:${tilt}deg`}>${t("Delivered")}</span>`;
  }
  if (delivery === "pending") return html`<span class="wait">${t("Pending")}</span>`;
  return html`<span class="unknown">${t("Unknown")}</span>`;
}

function Coupon({ ev, index, fresh }) {
  const [open, setOpen] = useState(false);
  const start = eventStart(ev);
  const day = dayIndex(start);
  const id = `cp-${index}`;
  const end = ev.end ? new Date(ev.end) : null;
  return html`<li class=${cx("cp", ev.delivery === "pending" && "cp--pending")}>
    <button type="button" class="cp__btn" aria-expanded=${open ? "true" : "false"} aria-controls=${id} onClick=${() => setOpen(!open)}>
      <span class="cp__time">
        ${day === 1 && html`<small>${t("Tomorrow")}</small>`}
        ${ev.all_day ? html`<span class="cp__allday">${t("All day")}</span>` : html`<span class="num">${clock(start)}</span>`}
      </span>
      <span class="cp__body">
        <span class="cp__title">${ev.title || t("(untitled)")}${ev.location ? html`<span class="cp__loc"> · ${ev.location}</span>` : ""}</span>
        <span class="cp__alerts">
          <${Fig} text=${alarmsText(ev.alarms_minutes_before)} />
          ${ev.delivery === "pending" && html`<b> · ${t("next sync")}</b>`}
        </span>
      </span>
      <span class="cp__deliv"><${Delivery} delivery=${ev.delivery} fresh=${fresh} tilt=${index % 2 ? 3 : -5} /></span>
    </button>
    ${open &&
    html`<dl class="cp__more" id=${id}>
      <div><dt>${t("Sync line")}</dt><dd>${ev.rule}</dd></div>
      <div><dt>${t("Target calendar")}</dt><dd>${ev.target_calendar}</dd></div>
      <div><dt>${t("Repeats")}</dt><dd>${ev.recurring ? t("Recurring event") : t("One-off event")}</dd></div>
      ${!ev.all_day &&
      end &&
      html`<div><dt>${t("Time")}</dt><dd><span class="num">${clock(start)}–${clock(end)}</span></dd></div>`}
      <div class="cp__more-wide"><dt>${t("Delivery")}</dt><dd>${deliveryNote(ev.delivery)}</dd></div>
    </dl>`}
  </li>`;
}

function SkeletonCoupons({ n = 4 }) {
  return html`<ul class="wallet wallet--skeleton" aria-hidden="true">
    ${Array.from({ length: n }, (_, i) => html`<li class="cp cp--skel" key=${i}><span class="sk sk--time"></span><span class="sk sk--body"></span><span class="sk sk--stamp"></span></li>`)}
  </ul>`;
}

// The server compares all-day events in UTC, so east of UTC it can still send yesterday's.
// Hide an all-day event whose (exclusive) end date is on or before the viewer's local today.
function visibleEvents(upcoming) {
  return ((upcoming && upcoming.events) || []).filter((ev) => {
    if (!ev.all_day || typeof ev.end !== "string") return true;
    const [y, m, d] = ev.end.slice(0, 10).split("-").map(Number);
    return dayIndex(new Date(y, m - 1, d)) > 0;
  });
}

function Upcoming({ upcoming, fresh, loading }) {
  const events = visibleEvents(upcoming);
  return html`<section class="block block--coupons" aria-labelledby="h-upcoming">
    <h2 id="h-upcoming">${t("Next 24 hours")} ${!loading && html`<span class="cap">${t("Coupons")} · <span class="num">${events.length}</span></span>`}</h2>
    ${loading
      ? html`<${SkeletonCoupons} n=${5} />`
      : events.length === 0
      ? html`<div class="wallet wallet--empty"><p>${t("No events in the next 24 hours")}</p></div>`
      : html`<ul class="wallet">
          ${events.map((ev, i) => html`<${Coupon} key=${eventKey(ev)} ev=${ev} index=${i} fresh=${fresh.has(eventKey(ev))} />`)}
        </ul>`}
  </section>`;
}

const eventKey = (ev) => `${ev.rule}|${ev.start}|${ev.title}`;
const VOIDED = new Set(["reverted", "deleted"]);

function ChangeRow({ c, showTime = true }) {
  const voided = VOIDED.has(c.action);
  return html`<li class=${cx("chg", voided && "chg--void")}>
    ${showTime && html`<span class="chg__time num">${stamp(new Date(c.at))}</span>`}
    <span class=${cx("tag", `tag--${c.action}`)}>${actionLabel(c.action)}</span>
    <span class="chg__title">${voided ? html`<s>${c.title || t("(untitled)")}</s>` : c.title || t("(untitled)")}</span>
    ${voided && html`<span class="void">${t("Void")}</span>`}
  </li>`;
}

function RecentChanges({ changes, loading }) {
  const rows = ((changes && changes.changes) || []).slice(0, 5);
  return html`<section class="block block--changes" aria-labelledby="h-changes">
    <h2 id="h-changes">${t("Recent changes")} <span class="cap">${t("Changes")}</span></h2>
    ${loading
      ? html`<${SkeletonCoupons} n=${3} />`
      : rows.length === 0
      ? html`<div class="wallet wallet--empty"><p>${t("No changes yet")}</p></div>`
      : html`<ul class="wallet log">
          ${rows.map((c) => html`<${ChangeRow} key=${c.id} c=${c} />`)}
        </ul>`}
    <a class="more" href="#/runs">${t("All sync history")} <${Icon} name="arrow" size=${16} /></a>
  </section>`;
}

function blockedTitlesFor(rule, changes) {
  const rows = ((changes && changes.changes) || []).filter((c) => c.rule === rule && c.action === "blocked");
  if (!rows.length) return [];
  const latest = Math.max(...rows.map((c) => c.run_id));
  return rows.filter((c) => c.run_id === latest).map((c) => c.title);
}

function Overview({ data, fresh }) {
  const { status, upcoming, changes } = data;
  const loading = !status;
  return html`<div class="overview">
    <div class="ov-lead">
      ${loading
        ? html`<div class="status"><span class="sk sk--title"></span><span class="sk sk--line"></span></div>`
        : html`<${StatusSentence} status=${status} />
            ${(status.attention || []).length > 0 &&
            html`<div class="attn-list">
              ${status.attention.map(
                (item, i) => html`<${AttentionCard} key=${i} item=${item} status=${status} blockedTitles=${blockedTitlesFor(item.rule, changes)} />`
              )}
            </div>`}`}
    </div>
    <div class="ov-strips">
      ${loading
        ? html`<div class="pass pass--skel" aria-hidden="true"><span class="sk sk--line"></span><span class="sk sk--line"></span></div>`
        : (status.rules || []).map((r) => html`<${ConnectionStrip} key=${r.name} rule=${r} status=${status} />`)}
    </div>
    <div class="ov-coupons"><${Upcoming} upcoming=${upcoming} fresh=${fresh} loading=${!upcoming} /></div>
    <div class="ov-changes"><${RecentChanges} changes=${changes} loading=${!changes} /></div>
  </div>`;
}

// ---------------------------------------------------------------- run history

const COUNT_KEYS = [
  ["created", "plus"],
  ["updated", "edit"],
  ["reverted", "revert"],
  ["deleted", "minus"],
];

function isIdle(run) {
  return run.status === "ok" && !run.created && !run.updated && !run.deleted && !run.reverted && !run.failed;
}

function groupRuns(runs) {
  const out = [];
  for (const r of runs) {
    const last = out[out.length - 1];
    if (isIdle(r) && last && last.idle) last.runs.push(r);
    else if (isIdle(r)) out.push({ idle: true, runs: [r] });
    else out.push({ idle: false, run: r });
  }
  return out;
}

function countLabel(key, n) {
  switch (key) {
    case "created":
      return t("{n} added", { n });
    case "updated":
      return t("{n} changed", { n });
    case "reverted":
      return t("{n} reverted", { n });
    default:
      return t("{n} deleted", { n });
  }
}

function Counts({ run }) {
  return html`<span class="counts">
    ${COUNT_KEYS.map(
      ([k, icon]) => html`<span class=${cx("count", !run[k] && "count--zero")} title=${countLabel(k, run[k] || 0)}>
        <${Icon} name=${icon} size=${14} /><span class="num">${run[k] || 0}</span>
        <span class="sr">${countLabel(k, run[k] || 0)}</span>
      </span>`
    )}
  </span>`;
}

function IdleGroup({ runs }) {
  const newest = new Date(runs[0].started_at);
  const oldest = new Date(runs[runs.length - 1].started_at);
  const span = runs.length === 1 ? stamp(newest) : `${stamp(oldest)}–${clock(newest)}`;
  return html`<li class="run run--idle">
    <span class="run__when num">${span}</span>
    <span class="run__idle"><${Fig} text=${runs.length === 1 ? t("1 idle sync") : t("{n} idle syncs", { n: runs.length })} /></span>
  </li>`;
}

function RunRow({ run, multiRule, details, onOpen }) {
  const [open, setOpen] = useState(false);
  const id = `run-${run.id}`;
  const toggle = () => {
    if (!open) onOpen(run.id);
    setOpen(!open);
  };
  const det = details[run.id];
  const bad = run.status !== "ok";
  return html`<li class=${cx("run", bad && "run--bad")}>
    <button type="button" class="run__btn" aria-expanded=${open ? "true" : "false"} aria-controls=${id} onClick=${toggle}>
      <span class="run__line">
        <span class="run__when num">${stamp(new Date(run.started_at))}</span>
        <span class="run__dur"><${Fig} text=${duration(run.duration_ms)} /></span>
        ${multiRule && html`<span class="run__rule">${run.rule}</span>`}
        <span class=${cx("tag", `tag--run-${run.status}`)}>${runStatusLabel(run.status)}</span>
      </span>
      <span class="run__line run__line--figs">
        <${Counts} run=${run} />
        <span class="run__req"><${Icon} name="request" size=${14} /><${Fig} text=${t("{n} requests", { n: run.requests })} /></span>
        <${Icon} name="chevron" size=${18} />
      </span>
    </button>
    ${open &&
    html`<div class="run__more" id=${id}>
      ${run.error && html`<p class="run__err"><span>${t("Error")}</span> <code>${run.error}</code></p>`}
      ${!det
        ? html`<span class="sk sk--line"></span>`
        : det.error
        ? html`<p class="run__muted">${t("Could not load the changes of this sync.")}</p>`
        : det.changes.length === 0
        ? html`<p class="run__muted">${run.error ? t("Nothing was written in this sync.") : t("The details of this sync are no longer kept.")}</p>`
        : html`<ul class="run__changes">
            ${det.changes.map((c) => html`<${ChangeRow} key=${c.id} c=${c} showTime=${false} />`)}
          </ul>`}
    </div>`}
  </li>`;
}

function Runs({ runs, status, details, onOpen }) {
  if (!runs) {
    return html`<div class="runs"><h1 class="page-title">${t("Sync history")}</h1><${SkeletonCoupons} n=${6} /></div>`;
  }
  const list = runs.runs || [];
  const multiRule = !!status && (status.rules || []).length > 1;
  const groups = groupRuns(list);
  return html`<div class="runs">
    <h1 class="page-title">${t("Sync history")}</h1>
    <p class="page-sub"><${Fig} text=${list.length ? t("Latest {n} syncs, newest first", { n: list.length }) : t("No syncs recorded yet")} /></p>
    ${list.length > 0 &&
    html`<ul class="carbon">
      ${groups.map((g) =>
        g.idle
          ? html`<${IdleGroup} key=${`idle-${g.runs[0].id}`} runs=${g.runs} />`
          : html`<${RunRow} key=${g.run.id} run=${g.run} multiRule=${multiRule} details=${details} onOpen=${onOpen} />`
      )}
    </ul>`}
  </div>`;
}

// ---------------------------------------------------------------- app

function routeFromHash() {
  return window.location.hash.replace(/^#/, "").startsWith("/runs") ? "runs" : "overview";
}

function initialTheme() {
  const q = new URLSearchParams(window.location.search).get("theme");
  if (q === "light" || q === "dark") return q;
  const s = store(THEME_KEY);
  return s === "light" || s === "dark" ? s : "system";
}

function applyTheme(pref) {
  const dark = pref === "dark" || (pref === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
}

function App() {
  const [route, setRoute] = useState(routeFromHash());
  const [lang, setLangState] = useState(getLang());
  const [theme, setTheme] = useState(initialTheme());
  const [data, setData] = useState({ status: null, upcoming: null, changes: null });
  const [runs, setRuns] = useState(null);
  const [details, setDetails] = useState({});
  const [fresh, setFresh] = useState(new Set());
  const [problem, setProblem] = useState(null); // null | "offline" | "no_state" | "http"
  const [sync, setSync] = useState({ busy: false, error: null, note: null });
  const prevDelivery = useRef(new Map());
  const routeRef = useRef(route);
  routeRef.current = route;

  const takeUpcoming = (up) => {
    const now = new Set();
    const prev = prevDelivery.current;
    for (const ev of up.events || []) {
      const k = eventKey(ev);
      if (prev.get(k) === "pending" && ev.delivery === "delivered") now.add(k);
    }
    prevDelivery.current = new Map((up.events || []).map((ev) => [eventKey(ev), ev.delivery]));
    setFresh(now);
  };

  const refresh = useCallback(async () => {
    try {
      if (routeRef.current === "runs") {
        const [status, rs] = await Promise.all([api("/api/status"), api("/api/runs?limit=100")]);
        setData((d) => ({ ...d, status }));
        setRuns(rs);
        setProblem(null);
        return status;
      }
      const [status, upcoming, changes] = await Promise.all([
        api("/api/status"),
        api("/api/upcoming?hours=24"),
        api("/api/changes?limit=50"),
      ]);
      takeUpcoming(upcoming);
      setData({ status, upcoming, changes });
      setProblem(null);
      return status;
    } catch (e) {
      if (e.kind === "unpaired") return null;
      setProblem(e.kind === "no_state" ? "no_state" : e.kind === "offline" ? "offline" : "http");
      return null;
    }
  }, []);

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    refresh();
  }, [route]);

  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") refresh();
    };
    const timer = setInterval(tick, POLL_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, []);

  useEffect(() => {
    applyTheme(theme);
    if (theme !== "system") return undefined;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const on = () => applyTheme("system");
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [theme]);

  const onTheme = (v) => {
    store(THEME_KEY, v);
    setTheme(v);
  };
  const onLang = (v) => {
    setLang(v);
    setLangState(v);
  };

  const onSync = async () => {
    const before = data.status && data.status.summary ? data.status.summary.last_sync_at : null;
    setSync({ busy: true, error: null, note: null });
    try {
      await api("/api/trigger", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    } catch (e) {
      if (e.kind === "unpaired") return;
      const error =
        e.kind === "read_only"
          ? t("This calsync runs in demo mode and does not sync.")
          : e.kind === "offline"
          ? t("Can't reach calsync. Check the connection and try again.")
          : t("Sync could not start: {message}", { message: e.message });
      setSync({ busy: false, error, note: null });
      return;
    }
    const started = Date.now();
    const poll = async () => {
      const status = await refresh();
      const after = status && status.summary ? status.summary.last_sync_at : null;
      if (after && after !== before) {
        setSync({ busy: false, error: null, note: null });
        return;
      }
      if (Date.now() - started >= SYNC_TIMEOUT_MS) {
        setSync({ busy: false, error: null, note: t("The sync is taking longer than usual. This page keeps checking.") });
        return;
      }
      setTimeout(poll, SYNC_POLL_MS);
    };
    setTimeout(poll, SYNC_POLL_MS);
  };

  const onOpenRun = async (id) => {
    if (details[id] && !details[id].error) return;
    try {
      const res = await api(`/api/changes?run_id=${encodeURIComponent(id)}&limit=1000`);
      setDetails((d) => ({ ...d, [id]: { changes: res.changes || [] } }));
    } catch (e) {
      setDetails((d) => ({ ...d, [id]: { error: true, changes: [] } }));
    }
  };

  const status = data.status;
  const version = status && status.version;
  document.documentElement.setAttribute("lang", lang === "zh" ? "zh-CN" : "en");
  document.title = route === "runs" ? `${t("Sync history")} · calsync` : "calsync";

  let body;
  if (problem === "no_state") {
    body = html`<div class="empty-state">
      <h1 class="status__title">${t("No sync data yet")}</h1>
      <p>${t("No sync data yet. Once sync starts, its status shows up here.")}</p>
    </div>`;
  } else if (problem && !status) {
    body = null; // the banner says what is wrong; skeletons would promise data that is not coming
  } else if (route === "runs") {
    body = html`<${Runs} runs=${runs} status=${status} details=${details} onOpen=${onOpenRun} />`;
  } else {
    body = html`<${Overview} data=${data} fresh=${fresh} />`;
  }

  return html`
    <${Header} status=${status} sync=${sync} onSync=${onSync} menu=${{ lang, onLang, theme, onTheme, version }} />
    <${Tabs} route=${route} />
    <main class="page" id="main">
      ${problem === "offline" &&
      html`<div class="banner" role="alert">
        <${Icon} name="alert" size=${20} />
        <span>${t("Can't reach calsync")}</span>
        <button type="button" class="btn-quiet" onClick=${refresh}><${Icon} name="refresh" size=${18} />${t("Retry")}</button>
      </div>`}
      ${problem === "http" &&
      html`<div class="banner" role="alert">
        <${Icon} name="alert" size=${20} />
        <span>${t("calsync answered with an error")}</span>
        <button type="button" class="btn-quiet" onClick=${refresh}><${Icon} name="refresh" size=${18} />${t("Retry")}</button>
      </div>`}
      ${sync.error && html`<p class="inline-msg inline-msg--error" role="alert"><${Icon} name="alert" size=${18} />${sync.error}</p>`}
      ${sync.note && html`<p class="inline-msg" role="status">${sync.note}</p>`}
      ${body}
    </main>
  `;
}

const root = document.getElementById("app");
root.textContent = "";
render(html`<${App} />`, root);
