// calsync web UI (concept v4, the sync map): the root component, the hash router, the polled
// status and the page layout. Later modules fill the map (map.js), the inspectors (inspector.js,
// forms/*), the calendar (calendar.js) and the activity (activity.js).

import { html, render, useCallback, useEffect, useLayoutEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t, getLang } from "/static/i18n.js";
import { ago, clockTime } from "/static/format.js";
import { op, StatusContext, useStatus, useStatusPoller } from "/static/api.js";
import { Icon, Mark, Sprite } from "/static/icons.js";

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

function SyncButton({ onResult }) {
  const { cycleRunning, refresh } = useStatus();
  const [busy, setBusy] = useState(false);
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

function Header({ onResult }) {
  return html`<header class="top">
    <a class="brand" href="#/"><${Mark}/>calsync</a>
    <div class="top-r">
      <${SyncButton} onResult=${onResult}/>
      <a class="btn icon" href="#/settings" aria-label=${t("Settings")}><${Icon} name="gear"/></a>
    </div>
  </header>`;
}

// ----- the map frame and the result sentence --------------------------------------------------

function linesText(n) {
  return n === 1 ? t("1 line") : t("{n} lines", { n });
}

/** The map (Task 2 draws the accounts, calendars and wires into the two columns). */
function MapFrame() {
  const { status } = useStatus();
  const rules = status ? status.rules.length : null;
  return html`<section class="canvas" aria-label=${t("Sync map")}>
    <div class="col src"></div>
    <a class="hubwrap" href="#/settings" aria-label=${t("calsync hub: open the settings")}>
      <span class="hub"></span>
      <span class="hub-cap"><b>calsync</b>${rules === null ? null : html`<small class="num">${linesText(rules)}</small>`}</span>
    </a>
    <div class="col tgt"></div>
  </section>`;
}

function headline(status) {
  const summary = status.summary;
  if (summary.rules === 0) return t("No line yet");
  switch (summary.health) {
    case "ok":
      return summary.rules === 1 ? t("The only line is working") : t("Every line is working");
    case "never_run":
      return t("Not synced yet");
    case "paused":
      return t("Some lines are paused");
    default:
      return t("A line needs you");
  }
}

function detailLine(status) {
  const parts = [];
  const last = status.summary.last_sync_at;
  if (last) parts.push(t("Synced {when}", { when: ago(last) }));
  const next = status.daemon && status.daemon.next_cycle_at;
  if (next) parts.push(t("next check {time}", { time: clockTime(next) }));
  return parts.join(" · ");
}

function Result({ note }) {
  const { status, error } = useStatus();
  let title = t("Reading the sync state…");
  let line = "";
  let bad = false;
  if (status) {
    title = headline(status);
    line = detailLine(status);
  } else if (error) {
    line = errorNote(error).text;
    bad = true;
  }
  // Only the result of 立即同步 is announced (role=status); the ticking "N 分钟前" line is not.
  return html`<section class="result">
    <h1>${title}</h1>
    ${line && !note ? html`<p class=${bad ? "bad" : ""}>${line}</p>` : null}
    <p class=${note && note.bad ? "note bad" : "note"} role="status" aria-live="polite" data-role="result-note">${note ? note.text : ""}</p>
  </section>`;
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

  return html`<section class="lower">
    <div class="tabs" role="tablist" onKeyDown=${onKeyDown}>
      ${TABS.map((id) => html`<button class="tab" type="button" role="tab" id=${`tab-${id}`}
          aria-selected=${tab === id ? "true" : "false"} aria-controls=${`pane-${id}`}
          tabindex=${tab === id ? "0" : "-1"} onClick=${() => setTab(id)}>${label(id)}</button>`)}
    </div>
    ${TABS.map((id) => html`<div class="pane" role="tabpanel" id=${`pane-${id}`} tabindex="0"
        aria-labelledby=${`tab-${id}`} hidden=${tab !== id}></div>`)}
  </section>`;
}

// ----- the inspector container ------------------------------------------------------------------

/** The title and subtitle of an inspector route (the bodies come with each inspector's task). */
function inspectorHead(route) {
  switch (route.name) {
    case "settings":
      return [t("Settings"), t("Shared by every line")];
    case "rule":
      return [t("Line"), route.rule];
    case "calendar":
      return [t("Calendar"), route.calendar];
    case "account":
      return [t("Account"), route.account];
    case "new":
      return [t("New line"), t("Choose a source, then a target")];
    case "event":
      return [t("Event"), ""];
    case "blocked":
      return [t("Blocked deletes"), route.rule];
    default:
      return ["", ""];
  }
}

function Inspector({ route, onClose }) {
  const open = opensInspector(route);
  const panel = useRef(null);
  const returnTo = useRef(null);
  const [title, sub] = open ? inspectorHead(route) : ["", ""];

  // A layout effect: focus and the Esc key belong to the panel from the frame it opens in.
  useLayoutEffect(() => {
    document.body.classList.toggle("insp-open", open);
    if (open) {
      if (!returnTo.current) returnTo.current = document.activeElement;
      if (panel.current) panel.current.focus({ preventScroll: true });
      const onKey = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          onClose();
        }
      };
      document.addEventListener("keydown", onKey);
      return () => document.removeEventListener("keydown", onKey);
    }
    const back = returnTo.current;
    returnTo.current = null;
    if (back && back.isConnected && typeof back.focus === "function") back.focus({ preventScroll: true });
    return undefined;
  }, [open, route]);

  return html`<aside class=${open ? "insp open" : "insp"} ref=${panel} tabindex="-1"
      role="dialog" aria-modal="false" aria-labelledby="insp-title" aria-hidden=${open ? "false" : "true"}
      inert=${!open} data-route=${open ? route.name : ""}>
    <div class="ih">
      <div>
        <h2 id="insp-title">${title}</h2>
        ${sub ? html`<p>${sub}</p>` : null}
      </div>
      <button class="btn icon quiet" type="button" aria-label=${t("Close")} onClick=${onClose}><${Icon} name="x"/></button>
    </div>
    <div class="ib"></div>
    <div class="ifoot"></div>
  </aside>`;
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
  const noteTimer = useRef(0);

  const onResult = useCallback((next) => {
    window.clearTimeout(noteTimer.current);
    setNote(next);
    if (next) noteTimer.current = window.setTimeout(() => setNote(null), 20_000);
  }, []);

  return html`<${StatusContext.Provider} value=${status}>
    <${Sprite}/>
    <div class="wrap">
      <${Header} onResult=${onResult}/>
      <main>
        <${MapFrame}/>
        <${Result} note=${note}/>
        <${Lower} route=${route}/>
      </main>
      <${Footer}/>
    </div>
    <${Inspector} route=${route} onClose=${closeInspector}/>
  <//>`;
}

const root = document.getElementById("app");
render(html`<${App}/>`, root);
