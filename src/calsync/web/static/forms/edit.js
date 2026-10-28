// Writing an original in Google (phase C): #/event-edit/<id> changes an event, #/event-new
// creates one. calsync stays one way: the change goes to Google, and the copy in iCloud follows
// at the sync each save asks for (it waits for it, so the detail it returns to already shows the
// copy's state). A repeating event asks first: only this one, or the whole series.
//
// Times are entered in the browser's local zone and sent as instants; Google keeps the event's
// own zone. Only the fields a person changed are sent, with the version they saw
// (``expect_updated``): if the event changed in Google meanwhile, nothing is written and the form
// offers to read it again.

import { html, useCallback, useEffect, useMemo, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { ymd } from "/static/format.js";
import { get, op, useStatus } from "/static/api.js";
import { Command } from "/static/command.js";
import { CalendarName, Icon } from "/static/icons.js";
import { Head, Seg, failureText } from "/static/forms/parts.js";

const pad = (n) => String(n).padStart(2, "0");

/** Minutes before the start a person picks from (at most five of them). */
const PRESETS = [0, 5, 10, 15, 30, 60, 120, 1440];

export function alarmLabel(m) {
  if (m === 0) return t("At the start");
  if (m % 1440 === 0) return t("{n} days before", { n: m / 1440 });
  if (m % 60 === 0) return t("{n} h before", { n: m / 60 });
  return t("{n} min before", { n: m });
}

/** Keyboard for a radiogroup of ``.radio`` buttons: the arrows move and pick. */
export function onRadioKeys(e) {
  const moves = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
  if (!(e.key in moves) || e.target.getAttribute("role") !== "radio") return;
  e.stopPropagation();
  const radios = [...e.currentTarget.querySelectorAll('[role="radio"]:not([disabled])')];
  const i = radios.indexOf(e.target);
  if (i < 0) return;
  e.preventDefault();
  const next = radios[(i + moves[e.key] + radios.length) % radios.length];
  next.focus();
  next.click();
}

/** The 只改这一次 / 改整个系列 choice (``verb``: "change" or "delete"). */
export function ScopeChoice({ value, onPick, verb = "change" }) {
  const choices = verb === "delete"
    ? [["this", t("Only this time"), t("The other times of the series stay.")], ["series", t("All of them"), t("Every time of this repeating event.")]]
    : [["this", t("Only this one"), t("The other times of the series stay as they are.")], ["series", t("The whole series"), t("Every time of this repeating event changes the same way.")]];
  const stop = value || "this";
  return html`<div role="radiogroup" aria-labelledby="scope-lb" data-role="scope" onKeyDown=${onRadioKeys}>
    ${choices.map(([id, title, text]) => html`<button class="radio" type="button" role="radio" data-choice=${id}
        aria-checked=${value === id ? "true" : "false"} tabindex=${stop === id ? "0" : "-1"} onClick=${() => onPick(id)}>
      <span class="o" aria-hidden="true"></span><span class="txt"><b>${title}</b><small>${text}</small></span>
    </button>`)}
  </div>`;
}

// ----- values ------------------------------------------------------------------------------

function localParts(iso) {
  const d = new Date(iso);
  return { date: ymd(d), time: `${pad(d.getHours())}:${pad(d.getMinutes())}` };
}

function addDays(text, n) {
  const [y, m, d] = text.split("-").map(Number);
  return ymd(new Date(y, m - 1, d + n));
}

/** The form's values from an event (``GET /api/event``'s shape). */
function valuesOf(ev) {
  const allDay = Boolean(ev.all_day);
  let date, endDate, startTime, endTime;
  if (allDay) {
    date = String(ev.start).slice(0, 10);
    endDate = addDays(String(ev.end).slice(0, 10), -1); // the last day, as a person counts
    startTime = "09:00";
    endTime = "10:00";
  } else {
    const s = localParts(ev.start);
    const e = localParts(ev.end);
    date = s.date;
    // Ending the next day by less than a day: "次日" on the end time, no second date field.
    const overnight = addDays(s.date, 1) === e.date && new Date(ev.end) - new Date(ev.start) < 86400_000;
    endDate = overnight ? s.date : e.date;
    startTime = s.time;
    endTime = e.time;
  }
  const alarms = [...(ev.alarms_minutes_before || [])].filter((m) => m >= 0).sort((a, b) => a - b);
  return { title: ev.title || "", allDay, date, endDate, startTime, endTime, location: ev.location || "", alarmMode: alarms.length ? "custom" : "none", alarms };
}

/** Defaults for a new event: ``start`` (local "2026-10-26T14:00") or the next half hour, an hour long. */
function newValues(start) {
  let s = start ? new Date(start) : null;
  if (!s || Number.isNaN(s.getTime())) {
    s = new Date();
    s.setSeconds(0, 0);
    s.setMinutes(s.getMinutes() < 30 ? 30 : 60);
  }
  const e = new Date(s.getTime() + 3600_000);
  const a = localParts(s);
  const b = localParts(e);
  // An hour that runs past midnight shows as "次日", not as a second day.
  return { title: "", allDay: false, date: a.date, endDate: a.date, startTime: a.time, endTime: b.time, location: "", alarmMode: "default", alarms: [] };
}

function instant(date, time) {
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = time.split(":").map(Number);
  return new Date(y, m - 1, d, hh, mm);
}

/** [start, end] as the API takes them, or an error text. A timed event that ends before it
 * starts ends the next day. */
function times(v) {
  if (!v.date) return { error: t("Pick a date") };
  if (v.allDay) {
    const last = v.endDate || v.date;
    if (last < v.date) return { error: t("The last day is before the first") };
    return { start: v.date, end: addDays(last, 1) };
  }
  if (!v.startTime || !v.endTime) return { error: t("Pick a start and an end") };
  const s = instant(v.date, v.startTime);
  const lastDay = v.endDate && v.endDate > v.date ? v.endDate : v.date;
  let e = instant(lastDay, v.endTime);
  if (e <= s) {
    if (lastDay !== v.date) return { error: t("The end is before the start") };
    e = new Date(e.getTime() + 86400_000); // an end earlier in the day: the next day
  }
  return { start: s.toISOString(), end: e.toISOString(), nextDay: lastDay === v.date && ymd(e) !== ymd(s) };
}

/** Whole days from date text ``a`` to ``b``. */
function daysFrom(a, b) {
  const [y1, m1, d1] = a.split("-").map(Number);
  const [y2, m2, d2] = b.split("-").map(Number);
  return Math.round((new Date(y2, m2 - 1, d2) - new Date(y1, m1 - 1, d1)) / 86400_000);
}

function sameTime(a, b) {
  const x = times(a);
  const y = times(b);
  return x.start === y.start && x.end === y.end;
}

/** The API's arguments for what changed from ``before`` (everything when there is none). */
function argsOf(v, before) {
  const out = {};
  if (!before || v.title.trim() !== before.title.trim()) out.title = v.title.trim();
  if (!before || v.allDay !== before.allDay || !sameTime(v, before)) {
    const w = times(v);
    out.start = w.start;
    out.end = w.end;
    out.all_day = v.allDay;
  }
  if (!before ? v.location.trim() : v.location.trim() !== before.location.trim()) out.location = v.location.trim();
  const alarmsNow = v.alarmMode === "custom" ? v.alarms.join(",") : v.alarmMode;
  const alarmsThen = before ? (before.alarmMode === "custom" ? before.alarms.join(",") : before.alarmMode) : null;
  if (alarmsNow !== alarmsThen) out.alarms = v.alarmMode === "custom" ? (v.alarms.length ? v.alarms : "none") : v.alarmMode;
  return out;
}

/** Why a write failed, for a person. */
export function writeFailure(e) {
  switch (e.kind) {
    case "conflict":
      return t("This event was just changed in Google. Read it again before you change it.");
    case "not_found":
      return t("Google has no such event any more: it may have been deleted meanwhile.");
    case "unreachable":
      return t("Google did not answer. Try again in a moment.");
    case "forbidden":
      return t("Google refused to change this calendar.");
    case "auth":
      return t("The Google authorisation failed: connect the account again.");
    case "config":
      if ((e.problems || []).some((p) => p.includes("set by its rule"))) return t("This series repeats on set days: only this time can move to another day.");
      return (e.problems && e.problems.length) ? t("calsync refused it: {why}", { why: e.problems[0] }) : failureText(e);
    default:
      return failureText(e);
  }
}

/** Google calendars a person may add events to: synced by some line, not read-only. */
export function writableCalendars(status) {
  const out = [];
  for (const account of (status && status.accounts) || []) {
    if (account.provider !== "google") continue;
    for (const cal of account.calendars || []) {
      if (!(cal.rules || []).length || cal.role === "reader" || cal.role === "freeBusyReader") continue;
      out.push({ account: account.name, id: cal.id, name: cal.name, can_write: account.can_write });
    }
  }
  return out;
}

/** 32 characters of Google's id alphabet (hex is part of it). crypto.getRandomValues works over
 * plain HTTP too (randomUUID needs a secure context). */
function eventId() {
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const CAL_KEY = "calsync.newEventCalendar";

function rememberedCalendar() {
  try {
    return window.localStorage.getItem(CAL_KEY);
  } catch (e) {
    return null;
  }
}

function rememberCalendar(key) {
  try {
    window.localStorage.setItem(CAL_KEY, key);
  } catch (e) {
    // private mode, storage off: the first calendar is picked next time
  }
}

// ----- the form ----------------------------------------------------------------------------

function Field({ id, label, children, hint }) {
  return html`<label class="ef" for=${id}><span class="ef-l">${label}</span>${children}${hint ? html`<small class="ef-h">${hint}</small>` : null}</label>`;
}

function Alarms({ value, onChange }) {
  const pick = (mode) => onChange({ ...value, alarmMode: mode, alarms: mode === "custom" && !value.alarms.length ? [10] : value.alarms });
  const toggle = (m) => {
    const has = value.alarms.includes(m);
    if (!has && value.alarms.length >= 5) return;
    const next = has ? value.alarms.filter((x) => x !== m) : [...value.alarms, m].sort((a, b) => a - b);
    onChange({ ...value, alarms: next });
  };
  const presets = [...new Set([...PRESETS, ...value.alarms])].sort((a, b) => a - b);
  return html`<div class="fs" data-key="alarms"><span class="lb" id="alarm-lb">${t("Reminder")}</span>
    <${Seg} label=${t("Reminder")} value=${value.alarmMode} onPick=${pick}
      options=${[["default", t("Calendar default")], ["none", t("No reminder")], ["custom", t("Choose")]]}/>
    ${value.alarmMode === "custom"
      ? html`<div class="alarm-picks" role="group" aria-labelledby="alarm-lb" data-role="alarm-picks">
          ${presets.map((m) => html`<button type="button" class="pick" aria-pressed=${value.alarms.includes(m) ? "true" : "false"}
              onClick=${() => toggle(m)}>${alarmLabel(m)}</button>`)}
        </div>
        <p class="hp after">${t("Up to five reminders.")}</p>`
      : null}
  </div>`;
}

function Fields({ value, onChange, disabled }) {
  const set = (key) => (e) => onChange({ ...value, [key]: e.currentTarget.value });
  // A new first day moves the last day with it: the event keeps its length.
  const setDate = (e) => {
    const date = e.currentTarget.value;
    const endDate = value.date && date && value.endDate ? addDays(value.endDate, daysFrom(value.date, date)) : date;
    onChange({ ...value, date, endDate });
  };
  const when = times(value);
  const multiDay = Boolean(value.endDate && value.date && value.endDate > value.date);
  return html`<fieldset class="ef-set" disabled=${disabled}>
    <div class="fs" data-key="title">
      <${Field} id="ev-title" label=${t("Title")}>
        <input class="field w" id="ev-title" type="text" value=${value.title} onInput=${set("title")} maxlength="1024" autocomplete="off"/>
      </${Field}>
    </div>
    <div class="fs" data-key="when"><span class="lb">${t("Time")}</span>
      <${Seg} label=${t("Time")} value=${value.allDay ? "day" : "timed"} onPick=${(v) => onChange({ ...value, allDay: v === "day" })}
        options=${[["timed", t("At a time")], ["day", t("All day")]]}/>
      ${value.allDay
        ? html`<div class="ef-row">
            <${Field} id="ev-date" label=${t("First day")}><input class="field" id="ev-date" type="date" value=${value.date} onInput=${setDate}/></${Field}>
            <${Field} id="ev-end-date" label=${t("Last day")}><input class="field" id="ev-end-date" type="date" value=${value.endDate} onInput=${set("endDate")}/></${Field}>
          </div>`
        : html`<div class="ef-row">
            <${Field} id="ev-date" label=${multiDay ? t("First day") : t("Date")}><input class="field" id="ev-date" type="date" value=${value.date} onInput=${setDate}/></${Field}>
            <${Field} id="ev-start" label=${t("Starts")}><input class="field" id="ev-start" type="time" step="300" value=${value.startTime} onInput=${set("startTime")}/></${Field}>
            ${multiDay ? html`<${Field} id="ev-end-date" label=${t("Last day")}><input class="field" id="ev-end-date" type="date" value=${value.endDate} onInput=${set("endDate")}/></${Field}>` : null}
            <${Field} id="ev-end" label=${t("Ends")} hint=${when.nextDay ? t("the next day") : null}>
              <input class="field" id="ev-end" type="time" step="300" value=${value.endTime} onInput=${set("endTime")}/></${Field}>
          </div>`}
      ${when.error ? html`<p class="hp after bad" data-role="when-error">${when.error}</p>` : null}
    </div>
    <div class="fs" data-key="location">
      <${Field} id="ev-location" label=${t("Location")}>
        <input class="field w" id="ev-location" type="text" value=${value.location} onInput=${set("location")} autocomplete="off"/>
      </${Field}>
    </div>
    <${Alarms} value=${value} onChange=${onChange}/>
  </fieldset>`;
}

function CalendarPicker({ calendars, value, onPick }) {
  const stop = value || (calendars[0] && `${calendars[0].account}:${calendars[0].id}`);
  return html`<div class="fs" data-key="calendar"><span class="lb" id="cal-lb">${t("Calendar")}</span>
    <p class="hp">${t("The event goes into Google; its line brings it to iCloud.")}</p>
    <div role="radiogroup" aria-labelledby="cal-lb" data-role="calendars" onKeyDown=${onRadioKeys}>
      ${calendars.map((c) => {
        const key = `${c.account}:${c.id}`;
        return html`<button class="radio" type="button" role="radio" data-calendar=${key} aria-checked=${value === key ? "true" : "false"}
            tabindex=${stop === key ? "0" : "-1"} onClick=${() => onPick(key)}>
          <span class="o" aria-hidden="true"></span><${CalendarName} service="google" account=${c.account} name=${c.name}/>
        </button>`;
      })}
    </div>
  </div>`;
}

/** #/event-edit/<id> and #/event-new (query ``start``: a local time to begin at). */
export function EditForm({ route, onClose, onReplace, active = true }) {
  const creating = route.name === "event-new";
  const { status, refresh } = useStatus();
  const [ev, setEv] = useState(creating ? null : undefined); // the event being changed (undefined: reading)
  const [missing, setMissing] = useState(false);
  const [before, setBefore] = useState(null); // the values as read (null: a new event)
  const [value, setValue] = useState(() => (creating ? newValues(route.query && route.query.start) : null));
  const [scope, setScope] = useState(null); // a repeating event: "this" | "series"
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null); // {text, bad, reload}
  const [human, setHuman] = useState(null);
  const calendars = useMemo(() => writableCalendars(status), [status]);
  // The new event's Google id, chosen once per form: saving again after a lost answer finds the
  // event already made instead of making a second one.
  const sourceId = useMemo(() => eventId(), []);
  const [calendar, setCalendar] = useState(() => (creating ? rememberedCalendar() : null));
  const reads = useRef(0);

  const load = useCallback(async () => {
    if (creating) return;
    const mine = ++reads.current;
    try {
      const body = await get("/api/event", { id: route.event });
      if (mine !== reads.current) return;
      const values = valuesOf(body);
      setEv(body);
      setBefore(values);
      setValue(values);
      setNote(null);
    } catch (e) {
      if (mine === reads.current && (e.kind === "not_found" || e.status === 404)) setMissing(true);
    }
  }, [creating, route.event]);
  useEffect(() => {
    if (active) load();
  }, [load, active]);

  // 重新读取: Google changed meanwhile, so sync its calendar first (calsync reads Google there),
  // then read the event again; the form starts over from it.
  const reread = async () => {
    if (busy || !ev) return;
    setBusy(true);
    try {
      const rule = (ev.deliveries || [])[0];
      if (rule) await op("trigger", { rule: rule.rule, wait: true, timeout: 60 });
    } catch (e) {
      // a paused line, a busy daemon: read what is there
    } finally {
      setBusy(false);
    }
    setScope(null);
    await load();
  };

  const chosen = creating ? calendars.find((c) => `${c.account}:${c.id}` === calendar) || calendars[0] || null : null;
  const title = creating ? t("New event") : t("Change the event");
  const head = html`<${Head} title=${title} sub=${creating ? t("In Google Calendar") : (ev && ev.title) || ""} onClose=${onClose}/>`;
  if (missing) return html`${head}<div class="ib"><div class="note">${t("This event is no longer there: it may have been deleted or moved in Google Calendar.")}</div></div>`;
  if (!creating && ev === undefined) return html`${head}<div class="ib"><p class="hp">${t("Reading the event…")}</p></div>`;
  if (creating && !calendars.length) {
    return html`${head}<div class="ib"><div class="note" data-role="no-calendar">${t("No Google calendar that a line syncs can take events. Add a line first.")}</div></div>`;
  }
  if (ev && ev.editable && !ev.editable.ok) {
    return html`${head}<div class="ib"><div class="note">${ev.editable.reason === "invitation" ? t("Someone invited you to this event: only its organiser can change it.") : t("This calendar is read-only: calsync cannot change its events.")}</div></div>`;
  }

  const recurring = Boolean(ev && ev.recurring);
  const changes = before ? argsOf(value, before) : null;
  const when = times(value);
  const empty = !value.title.trim();
  const nothing = !creating && changes && !Object.keys(changes).length;
  const needsScope = recurring && !scope;
  const blocked = busy || empty || Boolean(when.error) || nothing || needsScope;
  const writeAccess = creating ? (chosen ? chosen.can_write : null) : ev.editable && ev.editable.write_access;
  const account = creating ? chosen && chosen.account : ev.source.account;

  const save = async () => {
    if (blocked) return;
    setBusy(true);
    setNote(null);
    setHuman(null);
    try {
      let result;
      if (creating) {
        rememberCalendar(`${chosen.account}:${chosen.id}`);
        result = await op("event.create", { account: chosen.account, calendar: chosen.id, source_id: sourceId, ...argsOf(value, null), wait: true });
      } else {
        const expect = recurring && scope === "series" ? ev.series_updated : ev.updated;
        result = await op("event.edit", { id: ev.id, ...(recurring ? { scope } : {}), ...(expect ? { expect_updated: expect } : {}), ...changes, wait: true });
      }
      if (result.needs_human) {
        setHuman(result);
        return;
      }
      refresh();
      const next = (result.data && result.data.event_id) || (ev && ev.id);
      if (next) onReplace(`#/event/${encodeURIComponent(next)}`);
      else onClose();
    } catch (e) {
      if (e.kind === "unpaired") return;
      setNote({ text: writeFailure(e), bad: true, reload: e.kind === "conflict" });
    } finally {
      setBusy(false);
    }
  };

  return html`${head}
    <div class="ib" data-role="edit">
      ${creating ? html`<${CalendarPicker} calendars=${calendars} value=${chosen && `${chosen.account}:${chosen.id}`}
          onPick=${(key) => setCalendar(key)}/>` : null}
      ${recurring ? html`<div class="fs" data-key="scope"><span class="lb" id="scope-lb">${t("This is a repeating event")}</span>
          <${ScopeChoice} value=${scope} onPick=${setScope}/></div>` : null}
      <${Fields} value=${value} onChange=${setValue} disabled=${busy}/>
      ${writeAccess === false ? html`<div class="note warn" data-role="write-access">
          <p>${t("calsync may not change Google events for this account yet. Allow it once, in a terminal on the computer calsync runs on:")}</p>
          <${Command} command=${`calsync auth ${account} --write`}/></div>` : null}
      ${human ? html`<div class="note warn" data-role="needs-human"><p>${t("This needs you, in a terminal on the computer calsync runs on:")}</p><${Command} command=${human.command}/></div>` : null}
      <p class=${note && note.bad ? "inote bad" : "inote"} role="status" data-role="outcome">${note ? note.text : ""}</p>
      ${note && note.reload ? html`<button class="btn" type="button" data-role="reload" onClick=${reread}>${t("Read it again")}</button>` : null}
    </div>
    <div class="ifoot">
      <span class="fh"><${Icon} name="pen"/>${creating
        ? (chosen ? t("Saved in Google “{calendar}”; the copy follows.", { calendar: chosen.name }) : "")
        : t("Saved in Google; the copy in iCloud follows at once.")}</span>
      <button class=${busy ? "btn primary busy" : "btn primary"} type="button" data-role="save"
          aria-disabled=${blocked ? "true" : "false"} onClick=${save}>
        ${busy ? html`<${Icon} name="sync"/>${t("Saving…")}` : creating ? t("Create") : t("Save")}</button>
    </div>`;
}
