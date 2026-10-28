// calsync web UI (concept v4, the sync map): the root component, the hash router, the polled
// status and the page layout. Later modules fill the map (map.js), the inspectors (inspector.js,
// forms/*), the calendar (calendar.js) and the activity (activity.js).

import { html, render, useCallback, useEffect, useLayoutEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t, getLang } from "/static/i18n.js";
import { accountLabel, ago, clockTime, serviceOf, when } from "/static/format.js";
import { get, op, StatusContext, useStatus, useStatusPoller } from "/static/api.js";
import { CalendarName, Icon, Mark, Service, Sprite } from "/static/icons.js";
import { Command } from "/static/command.js";
import { SyncMap } from "/static/map.js";
import { Inspector } from "/static/inspector.js";
import { headline } from "/static/sentence.js";

// ----- router ---------------------------------------------------------------------------------
// #/                              the map (no inspector)
// #/activity                      the map with the 活动 tab selected
// #/rule/<name>                   a line's inspector
// #/calendar/<account>/<id>       a calendar's inspector
// #/account/<name>                an account's inspector
// #/settings                      the settings
// #/new                           a new line (query: prefill)
// #/event/<id>                    an event's detail
// #/blocked/<rule>                a rule's blocked deletes
// #/connect/<google|icloud>       connect an account (the wizard)
// Anything else is replaced by #/. Segments are URI-encoded; "?k=v" after the path is ``query``.

const ROUTES = [
  [/^\/activity$/, () => ({ name: "activity" })],
  [/^\/rule\/([^/]+)$/, (m) => ({ name: "rule", rule: m[1] })],
  [/^\/calendar\/([^/]+)\/([^/]+)$/, (m) => ({ name: "calendar", account: m[1], calendar: m[2] })],
  [/^\/account\/([^/]+)$/, (m) => ({ name: "account", account: m[1] })],
  [/^\/settings$/, () => ({ name: "settings" })],
  [/^\/new$/, () => ({ name: "new" })],
  [/^\/event\/([^/]+)$/, (m) => ({ name: "event", event: m[1] })],
  [/^\/blocked\/([^/]+)$/, (m) => ({ name: "blocked", rule: m[1] })],
  [/^\/connect\/(google|icloud)$/, (m) => ({ name: "connect", service: m[1] })],
];

const ROOT = { name: "root", query: {}, key: "/" };

function decode(part) {
  try {
    return decodeURIComponent(part);
  } catch (e) {
    return null;
  }
}

/** The route of a location hash ("#/rule/family"), or null when no route matches. */
export function parseRoute(hash) {
  const raw = (hash || "").replace(/^#/, "");
  const [path, search = ""] = raw.split("?", 2);
  if (path === "" || path === "/") return ROOT;
  const query = Object.fromEntries(new URLSearchParams(search));
  for (const [pattern, build] of ROUTES) {
    const m = path.match(pattern);
    if (!m) continue;
    const parts = m.map((p, i) => (i === 0 ? p : decode(p)));
    if (parts.includes(null)) return null;
    return { ...build(parts), query, key: raw };
  }
  return null;
}

/** Inspector routes: every route but the map itself and its activity tab. */
export function opensInspector(route) {
  return route.name !== "root" && route.name !== "activity";
}

// Closing an inspector returns to the map entry it was opened from, so the browser's Back never
// reopens an inspector that was closed: each history entry records (in history.state) how many
// inspector entries lead to it since the map (``depth``) and whether a map entry precedes them
// (``mapBehind``). Closing goes back ``depth`` entries; an inspector loaded directly from a link
// (no map entry behind it) goes back to the first inspector entry and turns it into the map.
const MAP_ENTRY = { depth: 0, mapBehind: false };

function entryOf(next, previous) {
  if (!opensInspector(next)) return MAP_ENTRY;
  if (previous === null) return { depth: 1, mapBehind: false }; // loaded with this hash
  if (previous.depth === 0) return { depth: 1, mapBehind: true };
  return { depth: previous.depth + 1, mapBehind: previous.mapBehind };
}

function remember(entry, url) {
  const state = window.history.state && typeof window.history.state === "object" ? window.history.state : {};
  window.history.replaceState({ ...state, calsync: entry }, "", url);
}

function useRoute() {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash) || ROOT);
  const entry = useRef(null); // the current history entry's {depth, mapBehind}
  const toMap = useRef(false); // closing: the entry we arrive at becomes the map
  const closing = useRef(0); // a close's traversal is under way (timer id): further closes wait

  // A layout effect: it runs within the first render, so the listeners and the first entry's
  // info exist before anything can change the hash (a passive effect waits for a paint).
  useLayoutEffect(() => {
    // Routing follows the history entry. A traversal between two entries with the same fragment
    // fires only popstate, a new fragment fires both: both run this, which is idempotent (the
    // second sees the entry info the first stored).
    const onEntry = () => {
      window.clearTimeout(closing.current);
      closing.current = 0;
      const next = parseRoute(window.location.hash);
      if (next === null || toMap.current) {
        toMap.current = false;
        remember(MAP_ENTRY, "#/");
        entry.current = MAP_ENTRY;
        setRoute(ROOT);
        return;
      }
      const stored = window.history.state && window.history.state.calsync;
      entry.current = stored || entryOf(next, entry.current);
      if (!stored) remember(entry.current);
      setRoute((current) => (current.key === next.key ? current : next));
    };
    window.addEventListener("popstate", onEntry);
    window.addEventListener("hashchange", onEntry);
    onEntry();
    return () => {
      window.removeEventListener("popstate", onEntry);
      window.removeEventListener("hashchange", onEntry);
    };
  }, []);

  const closeInspector = useCallback(() => {
    if (closing.current) return; // the first close is still travelling back
    const here = entry.current || MAP_ENTRY;
    const back = here.mapBehind ? here.depth : here.depth - 1;
    if (back > 0) {
      toMap.current = !here.mapBehind;
      // Cleared by the entry we arrive at; the timer only guards a traversal that never comes.
      closing.current = window.setTimeout(() => {
        closing.current = 0;
      }, 1000);
      window.history.go(-back);
    } else {
      remember(MAP_ENTRY, "#/");
      entry.current = MAP_ENTRY;
      setRoute(ROOT);
    }
  }, []);

  return [route, closeInspector];
}

// ----- header and 立即同步 ------------------------------------------------------------------

function changesOf(data) {
  let n = 0;
  let unfinished = false;
  for (const r of Object.values((data && data.rules) || {})) {
    n += (r.created || 0) + (r.updated || 0) + (r.deleted || 0);
    if (r.status !== "ok") unfinished = true;
  }
  return { n, unfinished };
}

/** The one-line result of a 立即同步, from the trigger operation's answer. */
function syncNote(result) {
  const data = result.data || {};
  if (data.ran === false) {
    if (data.reason === "timeout") return { text: t("The sync is still running; look again in a moment") };
    if (data.reason === "paused") return { text: t("Every line is paused: nothing to sync") };
    return { text: t("This sync did not finish"), bad: true };
  }
  const { n, unfinished } = changesOf(data);
  if (unfinished) return { text: t("Done, but some lines did not finish"), bad: true };
  if (n === 0) return { text: t("Done: nothing new to deliver") };
  if (n === 1) return { text: t("Done: 1 change delivered") };
  return { text: t("Done: {n} changes delivered", { n }) };
}

function errorNote(error) {
  if (error.kind === "offline") return { text: t("Can't reach calsync. Is it running?"), bad: true };
  if (error.kind === "read_only") return { text: t("This calsync does not run syncs (--no-sync)"), bad: true };
  return { text: t("Something went wrong ({kind})", { kind: error.kind }), bad: true };
}

function SyncButton({ onResult, busy, setBusy }) {
  const { cycleRunning, refresh } = useStatus();
  const pending = busy || cycleRunning;
  const label = pending ? t("Syncing…") : t("Sync now");

  const run = async () => {
    if (pending) return; // one cycle at a time: ours, or one the daemon is running
    setBusy(true);
    onResult(null);
    try {
      onResult(syncNote(await op("trigger", { wait: true, timeout: 120 })));
    } catch (e) {
      if (e.kind !== "unpaired") onResult(errorNote(e));
    } finally {
      setBusy(false);
      refresh();
    }
  };

  return html`<button class=${pending ? "btn busy" : "btn"} type="button" aria-label=${label}
      aria-busy=${pending ? "true" : "false"} aria-disabled=${pending ? "true" : "false"} onClick=${run}>
    <${Icon} name="sync"/><span class="lbl">${label}</span>
  </button>`;
}

function Header({ onResult, busy, setBusy }) {
  return html`<header class="top">
    <a class="brand" href="#/"><${Mark}/>calsync</a>
    <div class="top-r">
      <${SyncButton} onResult=${onResult} busy=${busy} setBusy=${setBusy}/>
      <a class="btn icon" href="#/settings" aria-label=${t("Settings")}><${Icon} name="gear"/></a>
    </div>
  </header>`;
}

// ----- the result sentence and the attention cards ----------------------------------------------
// Under the map, one sentence says how it went: a headline (every line working, one stopped, some
// paused…) and a detail line (when it last synced, how many changes reached iCloud today — a link
// to the activity behind the number — and the next check). When the status can no longer be
// refreshed the sentence says so, with the time calsync was last seen, until a poll succeeds.
// Under it, one card per thing that needs a person (status.attention).

/** ``template`` with ``{slot}`` replaced by ``node`` (a link inside a translated sentence). */
function withSlot(template, slot, node) {
  const [before, after = ""] = template.split(`{${slot}}`);
  return html`${before}${node}${after}`;
}

function joined(parts) {
  return parts.map((part, i) => html`${i ? " · " : ""}${part}`);
}

/** No line yet: what to do first (the concept's 连接 … 账号 and 新线路), as links. */
function startParts(status) {
  const accounts = status.accounts || [];
  const steps = [];
  if (!accounts.some((a) => a.provider === "google")) steps.push(html`<a href="#/connect/google">${t("Connect a Google account")}</a>`);
  if (!accounts.some((a) => a.provider !== "google")) steps.push(html`<a href="#/connect/icloud">${t("Connect an iCloud account")}</a>`);
  steps.push(html`<a href="#/new">${t("Add a new line")}</a>`);
  return [withSlot(t("To start: {step}"), "step", steps[0]), ...steps.slice(1)];
}

function detailParts(status, cycleRunning) {
  if (!(status.rules || []).length) return startParts(status);
  const parts = [];
  const summary = status.summary;
  if (cycleRunning) parts.push(t("Syncing now"));
  else if (summary.last_sync_at) parts.push(t("Synced {when}", { when: ago(summary.last_sync_at) }));
  const n = summary.delivered_today || 0;
  if (n > 0) {
    // The count is evidence: it opens the activity behind it, and brings the tabs into view
    // (here, on the click: coming back to #/activity from an inspector must not scroll).
    const show = () => window.requestAnimationFrame(() => {
      const lower = document.getElementById("lower");
      if (lower) lower.scrollIntoView({ block: "start" });
    });
    const changes = html`<a class="num" href="#/activity" onClick=${show}>${n === 1 ? t("1 change") : t("{n} changes", { n })}</a>`;
    parts.push(withSlot(t("Delivered {changes} today"), "changes", changes));
  } else if (summary.last_sync_at) {
    parts.push(t("Nothing new delivered today"));
  }
  const next = status.daemon && status.daemon.next_cycle_at;
  if (next && !cycleRunning) parts.push(html`<span class="num">${t("next check {time}", { time: clockTime(next) })}</span>`);
  return parts;
}

function Result({ note }) {
  const { status, error, cycleRunning } = useStatus();
  let title = t("Reading the sync state…");
  let parts = [];
  let bad = false;
  if (status && error) {
    // Read before, out of reach now: never claim it is all fine with a frozen time.
    const offline = error.kind === "offline";
    title = offline ? t("Can't reach calsync") : t("Can't read the latest state");
    parts = [t("Last seen at {time}", { time: when(status.generated_at) }), offline ? t("retrying") : errorNote(error).text];
    bad = true;
  } else if (status) {
    title = headline(status.rules || []);
    parts = detailParts(status, cycleRunning);
  } else if (error) {
    title = error.kind === "offline" ? t("Can't reach calsync") : t("Can't read the sync state");
    parts = [error.kind === "offline" ? t("Is it running? Retrying") : errorNote(error).text];
    bad = true;
  }
  // Only the result of 立即同步 is announced (role=status); the ticking "N 分钟前" line is not.
  return html`<section class="result">
    <h1>${title}</h1>
    ${parts.length ? html`<p class=${bad ? "detail bad" : "detail"}>${joined(parts)}</p>` : null}
    <p class=${note && note.bad ? "note bad" : "note"} role="status" aria-live="polite" data-role="result-note">${note ? note.text : ""}</p>
    ${status && !error ? html`<${Attention} status=${status}/>` : null}
  </section>`;
}

// ----- attention cards ---------------------------------------------------------------------------

function serviceOfAccount(status, name) {
  const account = (status.accounts || []).find((a) => a.name === name);
  return account ? serviceOf(account) : "caldav";
}

/** A line as it is always named: [icon] 工作 · work → [icon] 我的日历 · my-icloud. */
function LineName({ status, rule }) {
  const end = (spec) => html`<${CalendarName} service=${serviceOfAccount(status, spec.account)}
      account=${spec.account} name=${spec.calendar_name || spec.calendar}/>`;
  return html`<span class="route">${end(rule.source)}<span class="arrow" aria-label=${t("to")}>→</span>${end(rule.target)}</span>`;
}

function LineNames({ status, rules }) {
  return html`<span class="routes">${rules.map((rule) => html`<${LineName} status=${status} rule=${rule}/>`)}</span>`;
}

const REASONS = {
  google: () => [
    t("The Google account's password changed"),
    t("calsync's access was revoked in the Google account"),
    t("The Google app is still in “testing”: its authorisation has to be renewed every 7 days"),
  ],
  caldav: () => [
    t("The Apple ID password changed"),
    t("The app-specific password was revoked or replaced"),
  ],
};

function Card({ kind, tone, children, action }) {
  return html`<div class=${`attn ${tone}`} data-kind=${kind}>
    <span class="mk"><${Icon} name="alert"/></span>
    <div class="body">${children}</div>
    ${action}
  </div>`;
}

function AuthCard({ status, account, rules, command }) {
  const service = serviceOfAccount(status, account);
  const since = rules.map((r) => r.last_ok_at).filter(Boolean).sort().pop();
  const reasons = (service === "google" ? REASONS.google : REASONS.caldav)();
  return html`<${Card} kind="auth" tone="bad"
      action=${html`<a class="btn" href=${`#/account/${encodeURIComponent(account)}`}>${t("The account")}</a>`}>
    <b><${Service} service=${service} small=${true}/> ${t("{account}'s authorisation has expired", { account: accountLabel(account) })}</b>
    <small><${LineNames} status=${status} rules=${rules}/>${since ? t("Not updated since {time}; the other lines carry on", { time: when(since) }) : t("Stopped; the other lines carry on")}</small>
    <p class="why-h">${t("Usually one of these:")}</p>
    <ul class="why">${reasons.map((r) => html`<li>${r}</li>`)}</ul>
    <p class="why-h">${t("Connect it again from a terminal on this computer:")}</p>
    <${Command} command=${command || `calsync auth ${account}`}/>
  <//>`;
}

function BlockedCard({ status, rule, count }) {
  const href = `#/blocked/${encodeURIComponent(rule.name)}`;
  const copies = html`<a class="num" href=${href}>${count === 1 ? t("1 copy") : t("{n} copies", { n: count })}</a>`;
  return html`<${Card} kind="blocked" tone="held"
      action=${html`<a class="btn primary" href=${href}>${t("Review")}</a>`}>
    <b>${count ? withSlot(t("{copies} waiting for you to confirm deleting"), "copies", copies) : t("Copies are waiting for you to confirm deleting")}</b>
    <small><${LineNames} status=${status} rules=${[rule]}/>${t("calsync held these deletes back: nothing is deleted until you confirm")}</small>
  <//>`;
}

function FailingCard({ status, rule }) {
  const href = `#/rule/${encodeURIComponent(rule.name)}`;
  const n = rule.consecutive_failures || 1;
  const times = html`<a class="num" href=${href}>${n === 1 ? t("1 time") : t("{n} times", { n })}</a>`;
  return html`<${Card} kind="failing" tone="bad"
      action=${html`<a class="btn" href=${href}>${t("Open the line")}</a>`}>
    <b>${withSlot(t("Failed to sync {times} in a row"), "times", times)}</b>
    <small><${LineNames} status=${status} rules=${[rule]}/>${rule.last_ok_at ? t("Last synced at {time}; the other lines carry on", { time: when(rule.last_ok_at) }) : t("Not synced yet; the other lines carry on")}</small>
  <//>`;
}

/** The blocked count per rule (GET /api/blocked), refetched when the blocked set changes. */
function useBlockedCounts(names, stamp) {
  const [counts, setCounts] = useState({});
  const key = names.join("\n");
  useEffect(() => {
    if (!names.length) return undefined;
    let live = true;
    get("/api/blocked")
      .then((body) => {
        if (live) setCounts(Object.fromEntries((body.blocked || []).map((b) => [b.rule, b.count])));
      })
      .catch(() => null); // the card still shows, without the number
    return () => {
      live = false;
    };
  }, [key, stamp]);
  return counts;
}

function Attention({ status }) {
  const items = status.attention || [];
  const rule = (name) => (status.rules || []).find((r) => r.name === name);
  const blocked = items.filter((a) => a.kind === "blocked" && rule(a.rule)).map((a) => a.rule);
  const counts = useBlockedCounts(blocked, status.summary.last_sync_at);
  if (!items.length) return null;
  const cards = [];
  const accounts = new Map(); // one auth card per account, naming every line it stopped
  for (const a of items) {
    const r = rule(a.rule);
    if (!r) continue;
    if (a.kind === "auth") {
      const account = a.account || r.source.account;
      if (!accounts.has(account)) {
        const entry = { account, rules: [], command: a.command };
        accounts.set(account, entry);
        cards.push(entry);
      }
      accounts.get(account).rules.push(r);
    } else cards.push({ kind: a.kind, rule: r });
  }
  return html`<div class="attns" role="group" aria-label=${t("Needs you")}>
    ${cards.map((c) => {
      if (c.account) return html`<${AuthCard} key=${`auth:${c.account}`} status=${status} ...${c}/>`;
      if (c.kind === "blocked") return html`<${BlockedCard} key=${`blocked:${c.rule.name}`} status=${status} rule=${c.rule} count=${counts[c.rule.name] || 0}/>`;
      return html`<${FailingCard} key=${`failing:${c.rule.name}`} status=${status} rule=${c.rule}/>`;
    })}
  </div>`;
}

// ----- lower: 日历 | 活动 --------------------------------------------------------------------

const TABS = ["calendar", "activity"];

function Lower({ route }) {
  const [tab, setTab] = useState(route.name === "activity" ? "activity" : "calendar");
  useEffect(() => {
    if (route.name === "activity") setTab("activity");
  }, [route.name]);
  const label = (id) => (id === "calendar" ? t("Calendar") : t("Activity"));

  // The ARIA tabs pattern: one tab stop; ←/→ (wrapping), Home and End move focus and select.
  const onKeyDown = (e) => {
    const i = TABS.indexOf(tab);
    const moves = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 };
    if (!(e.key in moves)) return;
    e.preventDefault();
    const next = TABS[(moves[e.key] + TABS.length) % TABS.length];
    setTab(next);
    const button = document.getElementById(`tab-${next}`);
    if (button) button.focus();
  };

  return html`<section class="lower" id="lower">
    <div class="tabs" role="tablist" onKeyDown=${onKeyDown}>
      ${TABS.map((id) => html`<button class="tab" type="button" role="tab" id=${`tab-${id}`}
          aria-selected=${tab === id ? "true" : "false"} aria-controls=${`pane-${id}`}
          tabindex=${tab === id ? "0" : "-1"} onClick=${() => setTab(id)}>${label(id)}</button>`)}
    </div>
    ${TABS.map((id) => html`<div class="pane" role="tabpanel" id=${`pane-${id}`} tabindex="0"
        aria-labelledby=${`tab-${id}`} hidden=${tab !== id}></div>`)}
  </section>`;
}

// ----- footer and root --------------------------------------------------------------------------

function Footer() {
  const lang = getLang();
  const hash = window.location.hash || "";
  return html`<footer class="foot">
    <span>calsync · ${t("One-way sync from Google Calendar to iCloud.")}</span>
    <nav class="langs" aria-label=${t("Language")}>
      <a href=${`?lang=zh${hash}`} lang="zh-CN" aria-current=${lang === "zh" ? "true" : "false"}>中文</a>
      <a href=${`?lang=en${hash}`} lang="en" aria-current=${lang === "en" ? "true" : "false"}>English</a>
    </nav>
  </footer>`;
}

function App() {
  const status = useStatusPoller();
  const [route, closeInspector] = useRoute();
  const [note, setNote] = useState(null);
  const [busy, setBusy] = useState(false); // our 立即同步 is running
  const [born, setBorn] = useState(null); // a line just created: {rule, nonce}, for its first comet
  const onCreated = useCallback((rule) => setBorn({ rule, nonce: Date.now() }), []);
  const noteTimer = useRef(0);

  const onResult = useCallback((next) => {
    window.clearTimeout(noteTimer.current);
    setNote(next);
    if (next) noteTimer.current = window.setTimeout(() => setNote(null), 20_000);
  }, []);

  return html`<${StatusContext.Provider} value=${status}>
    <${Sprite}/>
    <div class="wrap">
      <${Header} onResult=${onResult} busy=${busy} setBusy=${setBusy}/>
      <main>
        <${SyncMap} status=${status.status} route=${opensInspector(route) ? route : null}
            waving=${busy || status.cycleRunning} stale=${Boolean(status.status && status.error)} born=${born}/>
        <${Result} note=${note}/>
        <${Lower} route=${route}/>
      </main>
      <${Footer}/>
    </div>
    <${Inspector} route=${route} open=${opensInspector(route)} onClose=${closeInspector} onCreated=${onCreated}/>
  <//>`;
}

const root = document.getElementById("app");
render(html`<${App}/>`, root);
