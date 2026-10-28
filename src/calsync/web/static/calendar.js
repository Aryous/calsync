// The calendar tab: the events calsync syncs, as the family's iPhones will show them, in six views
// (日 / 双日 / 周 / 双周 / 月 / 列表; the phone has no 周 and 双周), with a layer per target calendar
// and each event's delivery state on its block. Events come from GET /api/events for the visible
// range, cached per range and read again after a cycle or a change of the lines. Times are local.
//
// Delivery on a block: delivered unmarked; pending amber outline and flag; paused, blocked,
// failing or retiring hatched; out of the sync range faded. Tapping an event opens its detail
// (#/event/<id>); tapping a day's number opens that day.

import { html, useEffect, useLayoutEffect, useMemo, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t, getLang } from "/static/i18n.js";
import { clockTime, serviceOf, ymd } from "/static/format.js";
import { get, useStatus } from "/static/api.js";
import { CalendarName, Icon } from "/static/icons.js";
import { mapModel } from "/static/map.js";

const PHONE = "(max-width: 760px), (hover: none) and (pointer: coarse) and (max-width: 1024px)";
const HOUR = 48; // px, the --hourH token
const VIEWS = ["day", "2day", "week", "2week", "month", "list"];
const WIDE_ONLY = new Set(["week", "2week"]);
const LIST_DAYS = 14;

// ----- dates (local) -------------------------------------------------------------------------------

export function addDays(d, n) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
}

export function startOfDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/** The Monday of ``d``'s week. */
export function startOfWeek(d) {
  return addDays(startOfDay(d), -((d.getDay() + 6) % 7));
}

function sameDay(a, b) {
  return ymd(a) === ymd(b);
}

/** "2026-10-29" (an all-day bound) or an ISO instant → a local Date. */
function parseWhen(text, allDay) {
  if (allDay) {
    const [y, m, d] = String(text).slice(0, 10).split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  return new Date(text);
}

/** The days a view shows, from ``anchor``: [first, end) and how they are laid out. */
export function rangeOf(view, anchor) {
  switch (view) {
    case "day":
      return { from: anchor, to: addDays(anchor, 1) };
    case "2day":
      return { from: anchor, to: addDays(anchor, 2) };
    case "week":
      return { from: startOfWeek(anchor), to: addDays(startOfWeek(anchor), 7) };
    case "2week":
      return { from: startOfWeek(anchor), to: addDays(startOfWeek(anchor), 14) };
    case "month": {
      const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
      const last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
      const from = startOfWeek(first);
      const weeks = Math.ceil((Math.round((last - from) / 864e5) + 1) / 7);
      return { from, to: addDays(from, weeks * 7) };
    }
    default:
      return { from: anchor, to: addDays(anchor, LIST_DAYS) };
  }
}

function days(from, to) {
  const out = [];
  for (let d = from; d < to; d = addDays(d, 1)) out.push(d);
  return out;
}

function monthTitle(d) {
  if (getLang() === "zh") return `${d.getFullYear()}年${d.getMonth() + 1}月`;
  return d.toLocaleString("en-GB", { month: "long", year: "numeric" });
}

function shortDay(d) {
  if (getLang() === "zh") return `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${d.getDate()} ${d.toLocaleString("en-GB", { month: "short" })}`;
}

function weekdayName(d) {
  return [t("Sun"), t("Mon"), t("Tue"), t("Wed"), t("Thu"), t("Fri"), t("Sat")][d.getDay()];
}

function spanText(a, b) {
  return `${shortDay(a)} – ${shortDay(b)}`;
}

// ----- events and their state ---------------------------------------------------------------------

const targetKey = (target) => `${target.account}:${target.calendar}`;

// The block's look from the deliveries it shows: held (paused/blocked/failing/retiring) outranks
// pending, which outranks out of the range; delivered (and unknown) is unmarked.
const LOOK = { paused: "held", blocked: "held", failing: "held", retiring: "held", pending: "pending", out_of_window: "out" };
const RANK = { held: 3, pending: 2, out: 1, ok: 0 };

export function lookOf(deliveries) {
  let look = "ok";
  for (const d of deliveries) {
    const next = LOOK[d.state] || "ok";
    if (RANK[next] > RANK[look]) look = next;
  }
  return look;
}

/** The events of the API, with Dates and the deliveries the shown layers keep. */
function prepare(events, hidden) {
  return events
    .map((e) => {
      const shown = e.deliveries.filter((d) => !hidden.has(targetKey(d.target)));
      return {
        ...e,
        s: parseWhen(e.start, e.all_day),
        e: parseWhen(e.end, e.all_day),
        shown,
        look: lookOf(shown),
      };
    })
    .filter((e) => e.deliveries.length === 0 || e.shown.length > 0);
}

/** The events on local day ``d`` (an event that crosses midnight is on each day it touches). */
function on(events, d) {
  const start = startOfDay(d);
  const end = addDays(start, 1);
  return events.filter((e) => e.s < end && e.e > start);
}

const byTime = (a, b) => Number(b.all_day) - Number(a.all_day) || a.s - b.s || b.e - a.e;

/** Side-by-side lanes for overlapping timed events: each gets ``lane`` and ``lanes``. */
export function lanes(events) {
  const sorted = [...events].sort((a, b) => a.top - b.top || b.bottom - a.bottom);
  const out = [];
  let group = [];
  let groupEnd = -Infinity;
  const flush = () => {
    const ends = [];
    for (const ev of group) {
      let i = ends.findIndex((end) => end <= ev.top);
      if (i < 0) {
        i = ends.length;
        ends.push(0);
      }
      ends[i] = ev.bottom;
      ev.lane = i;
    }
    for (const ev of group) out.push({ ...ev, lanes: ends.length });
    group = [];
  };
  for (const ev of sorted) {
    if (group.length && ev.top >= groupEnd) flush();
    group.push(ev);
    groupEnd = Math.max(groupEnd, ev.bottom);
  }
  if (group.length) flush();
  return out;
}

// ----- pieces ------------------------------------------------------------------------------------------

function openEvent(id) {
  window.location.hash = `#/event/${id}`;
}

function stateWord(look) {
  if (look === "pending") return t("Waiting to be delivered");
  if (look === "held") return t("Held back");
  if (look === "out") return t("Outside the sync range");
  return "";
}

/** "晨跑, 07:00–07:45, 家庭日程, 等待送达": what a block says to a screen reader. */
function eventLabel(e, colourName) {
  const time = e.all_day ? t("All day") : `${clockTime(e.s)}–${clockTime(e.e)}`;
  return [e.title, time, colourName, stateWord(e.look)].filter(Boolean).join(", ");
}

function Block({ e, colour, day, now }) {
  const start = startOfDay(day);
  const from = Math.max(e.s, start);
  const to = Math.min(e.e, addDays(start, 1));
  const top = ((from - start) / 36e5) * HOUR;
  const height = Math.max(22, ((to - from) / 36e5) * HOUR - 3);
  const width = 100 / e.lanes;
  const style = { top: `${top + 1}px`, height: `${height}px`, left: `calc(${width * e.lane}% + 3px)`, width: `calc(${width}% - 6px)` };
  const cls = ["ev", colour, e.e < now ? "past" : "", e.look !== "ok" ? `st-${e.look}` : ""].filter(Boolean).join(" ");
  return html`<button class=${cls} type="button" style=${style} data-ev=${e.id} aria-label=${eventLabel(e, e.source.calendar_name)}
      onClick=${() => openEvent(e.id)}>
    ${e.look === "pending" || e.look === "held" ? html`<i class=${`flag ${e.look}`} aria-hidden="true"></i>` : null}
    <b>${e.title}</b>${height < 40 ? null : html`<small class="num">${clockTime(e.s)}–${clockTime(e.e)}${e.location ? ` · ${e.location}` : ""}</small>`}
  </button>`;
}

function Chip({ e, colour, flat }) {
  const cls = ["chip", flat ? "flat" : colour, !flat && e.look !== "ok" ? `st-${e.look}` : ""].filter(Boolean).join(" ");
  return html`<button class=${cls} type="button" data-ev=${e.id} aria-label=${eventLabel(e, e.source.calendar_name)} onClick=${() => openEvent(e.id)}>
    ${flat ? html`<i class=${`dot ${colour}`} aria-hidden="true"></i><time class="num">${clockTime(e.s)}</time>` : null}
    <span>${e.title}</span>
    ${flat && e.look !== "ok" ? html`<i class=${`st ${e.look}`} aria-hidden="true"></i>` : null}
  </button>`;
}

function TimeGrid({ dayList, events, colourOf, now, onDay }) {
  const scroller = useRef(null);
  const showNow = dayList.some((d) => sameDay(d, now));
  const nowTop = ((now - startOfDay(now)) / 36e5) * HOUR;
  const key = dayList.map(ymd).join(",");
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = Math.max(0, (showNow ? now.getHours() - 3.2 : 7) * HOUR);
  }, [key]);
  const cols = { "--n": String(dayList.length) };
  return html`<div class="tg" style=${cols}>
    <div class="tg-head"><div></div>
      ${dayList.map((d) => html`<button type="button" class=${["dh", sameDay(d, now) ? "today" : "", [0, 6].includes(d.getDay()) ? "wknd" : ""].filter(Boolean).join(" ")}
          data-day=${ymd(d)} onClick=${() => onDay(d)} aria-label=${t("Open {day}", { day: `${shortDay(d)} ${weekdayName(d)}` })}>
        <span>${weekdayName(d)}</span><b class="num">${d.getDate()}</b></button>`)}
    </div>
    <div class="tg-all"><div class="gut">${t("All day")}</div>
      ${dayList.map((d) => html`<div>${on(events, d).filter((e) => e.all_day).map((e) => html`<${Chip} e=${e} colour=${colourOf(e)}/>`)}</div>`)}
    </div>
    <div class="tg-scroll" ref=${scroller}>
      <div class="tg-body">
        <div class="hrs" aria-hidden="true">
          ${Array.from({ length: 23 }, (_, i) => (showNow && Math.abs((i + 1) * HOUR - nowTop) < 16 ? null
            : html`<span class="num" style=${{ top: `${(i + 1) * HOUR}px` }}>${String(i + 1).padStart(2, "0")}:00</span>`))}
          ${showNow ? html`<span class="nowt num" style=${{ top: `${nowTop}px` }}>${clockTime(now)}</span>` : null}
        </div>
        ${dayList.map((d) => {
          const start = startOfDay(d);
          const timed = on(events, d).filter((e) => !e.all_day).map((e) => ({
            ...e,
            top: Math.max(e.s, start) - start,
            bottom: Math.min(e.e, addDays(start, 1)) - start,
          }));
          return html`<div class=${[0, 6].includes(d.getDay()) ? "dcol wknd" : "dcol"} data-day=${ymd(d)}>
            ${lanes(timed).map((e) => html`<${Block} e=${e} colour=${colourOf(e)} day=${d} now=${now}/>`)}
            ${showNow ? html`<div class=${sameDay(d, now) ? "nowl" : "nowl faint"} style=${{ top: `${nowTop}px` }} aria-hidden="true"></div>` : null}
          </div>`;
        })}
      </div>
    </div>
  </div>`;
}

function WeekGrid({ from, weeks, month, events, colourOf, now, onDay }) {
  const max = weeks === 2 ? 6 : 3;
  const cells = days(from, addDays(from, weeks * 7));
  const heads = cells.slice(0, 7);
  return html`<div class="wg-head">${heads.map((d) => html`<div>${weekdayName(d)}</div>`)}</div>
    <div class=${weeks === 2 ? "wg two" : "wg"}>
      ${cells.map((d) => {
        const list = on(events, d).sort(byTime);
        const out = month != null && d.getMonth() !== month;
        const label = d.getDate() === 1 ? shortDay(d) : String(d.getDate());
        return html`<div class=${["wc", sameDay(d, now) ? "today" : "", out ? "out" : ""].filter(Boolean).join(" ")} data-day=${ymd(d)}>
          <button class="dn num" type="button" onClick=${() => onDay(d)} aria-label=${t("Open {day}", { day: `${shortDay(d)} ${weekdayName(d)}` })}>${label}</button>
          ${list.slice(0, max).map((e) => html`<${Chip} e=${e} colour=${colourOf(e)} flat=${!e.all_day}/>`)}
          ${list.length > max ? html`<button class="more" type="button" onClick=${() => onDay(d)}>${t("{n} more", { n: list.length - max })}</button>` : null}
          <div class="dots" aria-hidden="true">${list.slice(0, 5).map((e) => html`<i class=${`dot ${colourOf(e)}`}></i>`)}</div>
        </div>`;
      })}
    </div>`;
}

function Targets({ e, layerNames }) {
  return e.shown.map((d) => {
    const layer = layerNames.get(targetKey(d.target));
    return html`<${CalendarName} service=${layer ? layer.service : "caldav"} account=${d.target.account} name=${d.target.calendar_name}/>`;
  });
}

function ListView({ from, events, colourOf, now, onDay, sourceService, layerNames }) {
  const groups = days(from, addDays(from, LIST_DAYS))
    .map((d) => [d, on(events, d).sort(byTime)])
    .filter(([, list]) => list.length);
  if (!groups.length) return html`<div class="ls"><p class="hp pad">${t("No events in this period.")}</p></div>`;
  return html`<div class="ls">${groups.map(([d, list]) => {
    const rel = sameDay(d, now) ? t("Today") : sameDay(d, addDays(now, 1)) ? t("Tomorrow") : "";
    return html`<h3><button class="dn-l" type="button" onClick=${() => onDay(d)}>${rel ? `${rel} · ` : ""}${shortDay(d)} <em>${weekdayName(d)}</em></button></h3>
      ${list.map((e) => html`<button class="lr" type="button" data-ev=${e.id} onClick=${() => openEvent(e.id)}>
        <time class="num">${e.all_day ? t("All day") : `${clockTime(e.s)} – ${clockTime(e.e)}`}</time>
        <span class="tt"><i class=${`dot ${colourOf(e)}`} aria-hidden="true"></i><b>${e.title}</b>
          <small><${CalendarName} service=${sourceService(e)} account=${e.source.account} name=${e.source.calendar_name}/>
            <span class="arrow" aria-hidden="true">→</span><span class="vh">${t("to")}</span><${Targets} e=${e} layerNames=${layerNames}/></small></span>
        <span class=${`stt ${e.look}`}>${stateWord(e.look)}</span>
      </button>`)}`;
  })}</div>`;
}

// ----- the tab -----------------------------------------------------------------------------------------

function usePhone() {
  const [phone, setPhone] = useState(() => window.matchMedia(PHONE).matches);
  useEffect(() => {
    const mq = window.matchMedia(PHONE);
    const onChange = () => setPhone(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return phone;
}

/** What the events depend on besides the range: cycles run and the lines' states. */
function stampOf(status) {
  if (!status) return "";
  const cycles = status.daemon ? status.daemon.cycles : 0;
  return `${cycles}|${(status.rules || []).map((r) => `${r.name}:${r.health}`).join(",")}`;
}

export function CalendarPane() {
  const { status } = useStatus();
  const phone = usePhone();
  const [view, setView] = useState(() => (window.matchMedia(PHONE).matches ? "day" : "week"));
  const [anchor, setAnchor] = useState(() => startOfDay(new Date()));
  const [hidden, setHidden] = useState(() => new Set());
  const [data, setData] = useState(null); // {key, events, truncated}
  const [failed, setFailed] = useState(false);
  const cache = useRef(new Map());
  const reads = useRef(0);

  const shownView = phone && WIDE_ONLY.has(view) ? "day" : view;
  const range = rangeOf(shownView, anchor);
  const key = `${ymd(range.from)}/${ymd(range.to)}`;
  const stamp = stampOf(status);

  useEffect(() => {
    if (!status) return; // read once the lines are known: their states are part of the answer
    const cached = cache.current.get(key);
    if (cached && cached.stamp === stamp) {
      setData(cached);
      return;
    }
    if (cached) setData(cached); // shown while it is read again
    const mine = ++reads.current;
    get("/api/events", { from: ymd(range.from), to: ymd(range.to) })
      .then((body) => {
        const entry = { key, stamp, events: body.events || [], truncated: Boolean(body.truncated) };
        cache.current.set(key, entry);
        if (mine === reads.current) {
          setData(entry);
          setFailed(false);
        }
      })
      .catch((e) => {
        if (mine === reads.current && e.kind !== "aborted" && e.kind !== "unpaired") setFailed(true);
      });
  }, [key, stamp, Boolean(status)]);

  // Layers: one per target calendar a line writes to; the legend: the sources of the shown layers.
  const model = useMemo(() => (status ? mapModel(status) : null), [status]);
  const layers = useMemo(() => {
    const out = new Map();
    for (const rule of (status && status.rules) || []) {
      const k = targetKey(rule.target);
      if (out.has(k)) continue;
      const account = ((status && status.accounts) || []).find((a) => a.name === rule.target.account);
      out.set(k, { key: k, account: rule.target.account, name: rule.target.calendar_name || rule.target.calendar, service: account ? serviceOf(account) : "caldav" });
    }
    return out;
  }, [status]);
  const sourcePill = (src) => {
    if (!model) return null;
    for (const p of model.pills.values()) {
      if (p.side === "src" && p.account === src.account && (p.id === src.calendar || p.name === src.calendar)) return p;
    }
    return null;
  };
  const colourOf = (e) => {
    const pill = sourcePill(e.source);
    return pill && pill.colour !== "none" ? pill.colour : "steel";
  };
  const sourceService = (e) => {
    const pill = sourcePill(e.source);
    return pill ? pill.service : "google";
  };
  const legend = useMemo(() => {
    const out = new Map();
    for (const rule of (status && status.rules) || []) {
      if (hidden.has(targetKey(rule.target))) continue;
      const pill = sourcePill(rule.source);
      const k = pill ? pill.key : `${rule.source.account}/${rule.source.calendar}`;
      if (!out.has(k)) out.set(k, { key: k, account: rule.source.account, name: rule.source.calendar_name || rule.source.calendar, colour: pill && pill.colour !== "none" ? pill.colour : "steel", service: pill ? pill.service : "google" });
    }
    return [...out.values()];
  }, [status, hidden, model]);

  const events = data ? prepare(data.events, hidden) : [];
  const now = new Date();
  const goDay = (d) => {
    setAnchor(startOfDay(d));
    setView("day");
  };
  const step = (dir) => {
    if (shownView === "month") setAnchor(new Date(anchor.getFullYear(), anchor.getMonth() + dir, 1));
    else setAnchor(addDays(anchor, dir * { day: 1, "2day": 2, week: 7, "2week": 14, list: LIST_DAYS }[shownView]));
  };
  const toggle = (k) => {
    const next = new Set(hidden);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    setHidden(next);
  };

  const last = addDays(range.to, -1);
  const rangeText = shownView === "day" ? `${shortDay(anchor)} ${weekdayName(anchor)}` : shownView === "month" ? "" : spanText(range.from, last);
  const titleDay = shownView === "month" ? anchor : range.from;
  const viewNames = { day: t("Day"), "2day": t("2 days"), week: t("Week"), "2week": t("2 weeks"), month: t("Month"), list: t("List") };

  let body;
  if (shownView === "day" || shownView === "2day" || shownView === "week") {
    body = html`<${TimeGrid} dayList=${days(range.from, range.to)} events=${events} colourOf=${colourOf} now=${now} onDay=${goDay}/>`;
  } else if (shownView === "2week") {
    body = html`<${WeekGrid} from=${range.from} weeks=${2} events=${events} colourOf=${colourOf} now=${now} onDay=${goDay}/>`;
  } else if (shownView === "month") {
    body = html`<${WeekGrid} from=${range.from} weeks=${Math.round((range.to - range.from) / 864e5 / 7)} month=${anchor.getMonth()}
        events=${events} colourOf=${colourOf} now=${now} onDay=${goDay}/>`;
  } else {
    body = html`<${ListView} from=${range.from} events=${events} colourOf=${colourOf} now=${now} onDay=${goDay}
        sourceService=${sourceService} layerNames=${layers}/>`;
  }

  return html`<div class="calendar" data-view=${shownView}>
    <div class="cbar">
      <div class="ttl"><h2 data-role="cal-title">${monthTitle(titleDay)}</h2><span class="rng num" data-role="cal-range">${rangeText}</span></div>
      <div class="navbtns">
        <button class="btn icon quiet" type="button" aria-label=${t("Earlier")} onClick=${() => step(-1)}><${Icon} name="left"/></button>
        <button class="btn" type="button" onClick=${() => setAnchor(startOfDay(new Date()))}>${t("Today")}</button>
        <button class="btn icon quiet" type="button" aria-label=${t("Later")} onClick=${() => step(1)}><${Icon} name="right"/></button>
      </div>
      <div class="layers" role="group" aria-label=${t("Target calendars shown")}>
        ${[...layers.values()].map((l) => html`<button class="layer" type="button" aria-pressed=${hidden.has(l.key) ? "false" : "true"}
            data-layer=${l.key} onClick=${() => toggle(l.key)}><i aria-hidden="true"></i><${CalendarName} service=${l.service} account=${l.account} name=${l.name}/></button>`)}
      </div>
      <div class="seg views" role="group" aria-label=${t("View")}>
        ${VIEWS.filter((v) => !(phone && WIDE_ONLY.has(v))).map((v) => html`<button type="button" data-v=${v}
            aria-pressed=${v === shownView ? "true" : "false"} onClick=${() => setView(v)}>${viewNames[v]}</button>`)}
      </div>
    </div>
    <div class="legend" data-role="legend">
      ${legend.map((s) => html`<span><i class=${`dot ${s.colour}`} aria-hidden="true"></i><${CalendarName} service=${s.service} account=${s.account} name=${s.name}/></span>`)}
      <span class="push"><i class="key pending" aria-hidden="true"></i>${t("Waiting to be delivered")}</span>
      <span><i class="key held" aria-hidden="true"></i>${t("Held back")}</span>
    </div>
    ${data && data.truncated ? html`<p class="note warn cal-note" role="status">${t("Too many events in this period: only part of them is shown.")}</p>` : null}
    ${failed && !data ? html`<p class="note cal-note" role="status">${t("Can't read the events right now.")}</p>` : null}
    <div class="cal-frame" aria-busy=${data && data.key === key ? "false" : "true"}>${body}</div>
  </div>`;
}
