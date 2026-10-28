// The event detail (#/event/<id>): the event, where it comes from, where it is delivered and in
// what state, and whether each copy matches the original (GET /api/event: ``original``, each
// delivery's ``copy`` and ``differs``). Its footer changes the original in Google (修改 opens
// #/event-edit/<id>) or deletes it, unless calsync may not (a read-only calendar, an
// invitation: the footer says why).

import { html, useCallback, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t, getLang } from "/static/i18n.js";
import { accountLabel, calendarTitle, clockTime, serviceName, serviceOf, serviceShort, when, ymd } from "/static/format.js";
import { get, op, useStatus } from "/static/api.js";
import { Command } from "/static/command.js";
import { Icon, Service } from "/static/icons.js";
import { Head, Seg, accountOf } from "/static/forms/parts.js";
import { ScopeChoice, writeFailure } from "/static/forms/edit.js";

function parseWhen(text, allDay) {
  if (allDay) {
    const [y, m, d] = String(text).slice(0, 10).split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  return new Date(text);
}

function dateText(d) {
  if (getLang() === "zh") return `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${d.getDate()} ${d.toLocaleString("en-GB", { month: "short" })}`;
}

/** "10月26日 07:00–07:45" (a single event), "07:00–07:45" (each occurrence of a series), "全天". */
function whenText(item, { series = false } = {}) {
  const s = parseWhen(item.start, item.all_day);
  const e = parseWhen(item.end, item.all_day);
  if (item.all_day) {
    const last = new Date(e.getFullYear(), e.getMonth(), e.getDate() - 1);
    const span = ymd(last) === ymd(s) ? dateText(s) : `${dateText(s)} – ${dateText(last)}`;
    return series ? t("All day") : `${span} ${t("All day")}`;
  }
  const time = `${clockTime(s)}–${clockTime(e)}`;
  return series ? time : `${dateText(s)} ${time}`;
}

function alarmText(minutes) {
  if (!minutes || !minutes.length) return t("No reminder");
  return minutes
    .map((m) => {
      if (m === 0) return t("At the start");
      if (m < 0) return t("{n} min after the start", { n: -m });
      if (m % 1440 === 0) return t("{n} days before", { n: m / 1440 });
      if (m % 60 === 0) return t("{n} h before", { n: m / 60 });
      return t("{n} min before", { n: m });
    })
    .join(getLang() === "zh" ? "、" : ", ");
}

// The fields compared, and the API's names of the ones that can differ.
const ROWS = [
  ["title", () => t("Title"), (x) => x.title || "—", ["title"]],
  ["when", () => t("Time"), (x, series) => whenText(x, { series }), ["start", "end", "all_day"]],
  ["location", () => t("Location"), (x) => x.location || "—", ["location"]],
  ["alarms", () => t("Reminder"), (x) => alarmText(x.alarms_minutes_before), ["alarms_minutes_before", "alarms"]],
];

/** One delivery's state, for a person: [text, tone]. */
function stateText(d, status) {
  const next = status && status.daemon && status.daemon.next_cycle_at;
  const rule = status ? (status.rules || []).find((r) => r.name === d.rule) : null;
  const svc = serviceShort(d.service);
  switch (d.state) {
    case "delivered":
      return [d.delivered_at ? t("Delivered to {service} · {time}", { service: svc, time: when(d.delivered_at) }) : t("Delivered to {service}", { service: svc }), "ok"];
    case "pending":
      return [next ? t("Waiting · next check {time}", { time: clockTime(next) }) : t("Waiting to be delivered"), "wait"];
    case "paused":
      return [t("Paused"), "bad"];
    case "failing":
      return [rule && rule.health === "auth" ? t("Not delivered · authorisation failed") : t("Not delivered · the last sync failed"), "bad"];
    case "blocked":
      return [t("Deletes held back, waiting for you"), "wait"];
    case "retiring":
      return [t("The line is being removed"), "bad"];
    case "out_of_window":
      return [t("Outside the sync range"), ""];
    default:
      return [t("Not known yet"), ""];
  }
}

function Compare({ ev, delivery, status }) {
  const source = serviceOf(accountOf(status, ev.source.account) || { provider: ev.source.provider });
  const original = ev.original || ev;
  const copy = delivery.copy;
  const differs = new Set(delivery.differs || []);
  const busy = delivery.content === "busy";
  const rows = ROWS.filter(([key]) => key !== "location" || original.location || (copy && copy.location));
  return html`<table class="cmp" data-role="compare">
      <thead><tr><th></th><th>${t("{service} original", { service: serviceShort(source) })}</th>
        <th>${t("The copy in “{calendar}”", { calendar: delivery.target.calendar_name })}</th></tr></thead>
      <tbody>${rows.map(([key, label, show, names]) => {
        const off = copy && names.some((n) => differs.has(n));
        const busyTitle = busy && key === "title";
        return html`<tr data-field=${key}><td>${label()}</td><td>${show(original, original.recurring)}</td>
          <td class=${!copy ? "wait" : off ? "wait" : "ok"} data-differs=${off ? "true" : "false"}>${!copy
            ? (delivery.state === "paused" ? t("Paused") : t("Not written yet"))
            : busyTitle ? t("Busy") : show(copy, copy.recurring)}</td></tr>`;
      })}</tbody>
    </table>
    ${busy ? html`<p class="hp after" data-role="busy-note">${t("In “{calendar}” this shows only as “busy”: that is the line's setting.", { calendar: delivery.target.calendar_name })}</p>` : null}
`;
}

/** 删除: a confirmation (and, for a repeating event, which times) before anything is written. */
function DeleteConfirm({ ev, onCancel, onDone }) {
  const { refresh } = useStatus();
  const [scope, setScope] = useState(ev.recurring ? null : "single");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null);
  const [human, setHuman] = useState(null);
  const box = useRef(null);
  useEffect(() => {
    if (box.current) box.current.scrollIntoView({ block: "nearest" });
  }, []);
  const confirm = async () => {
    if (busy || !scope) return;
    setBusy(true);
    setNote(null);
    try {
      const expect = scope === "series" ? ev.series_updated : ev.updated;
      const result = await op("event.delete", { id: ev.id, ...(ev.recurring ? { scope } : {}), ...(expect ? { expect_updated: expect } : {}), wait: true });
      if (result.needs_human) {
        setHuman(result);
        return;
      }
      refresh();
      onDone();
    } catch (e) {
      if (e.kind !== "unpaired") setNote(writeFailure(e));
    } finally {
      setBusy(false);
    }
  };
  return html`<div class="del" data-role="delete" ref=${box} role="group" aria-labelledby="del-title">
    <h3 id="del-title">${t("Delete “{title}” in Google?", { title: ev.title || t("(no title)") })}</h3>
    <p class="hp">${t("Its copies are deleted at the same sync.")}</p>
    ${ev.recurring ? html`<${ScopeChoice} value=${scope} onPick=${setScope} verb="delete"/>` : null}
    ${human ? html`<div class="note warn"><p>${t("This needs you, in a terminal on the computer calsync runs on:")}</p><${Command} command=${human.command}/></div>` : null}
    <p class="inote bad" role="status" data-role="delete-outcome">${note || ""}</p>
    <div class="del-acts">
      <button class="btn quiet" type="button" onClick=${onCancel}>${t("Cancel")}</button>
      <button class=${busy ? "btn danger-solid busy" : "btn danger-solid"} type="button" data-role="confirm-delete"
          aria-disabled=${busy || !scope ? "true" : "false"} onClick=${confirm}>${busy ? t("Deleting…") : t("Delete")}</button>
    </div>
  </div>`;
}

/** Why calsync may not change this event, for a person. */
function lockedText(reason) {
  if (reason === "invitation") return t("Someone invited you to this event: only its organiser can change it.");
  return t("This calendar is read-only: calsync cannot change its events.");
}

export function EventForm({ route, onClose, active = true }) {
  const { status } = useStatus();
  const [deleting, setDeleting] = useState(false);
  const [ev, setEv] = useState(undefined); // null: no such event
  const [pick, setPick] = useState(null); // the delivery the table compares
  const reads = useRef(0);
  const id = route.event;

  const load = useCallback(async () => {
    const mine = ++reads.current;
    try {
      const body = await get("/api/event", { id });
      if (mine === reads.current) setEv(body);
    } catch (e) {
      if (mine === reads.current && (e.kind === "not_found" || e.status === 404)) setEv(null);
    }
  }, [id]);
  const cycles = status && status.daemon ? status.daemon.cycles : null;
  useEffect(() => {
    if (active && cycles != null) load(); // again after each cycle: its delivery may change
  }, [load, cycles, active]);

  if (ev === undefined) return html`<${Head} title=${t("Event")} sub="" onClose=${onClose}/><div class="ib"><p class="hp">${t("Reading the event…")}</p></div>`;
  if (ev === null) {
    return html`<${Head} title=${t("Event")} sub="" onClose=${onClose}/>
      <div class="ib"><div class="note">${t("This event is no longer there: it may have been deleted or moved in Google Calendar.")}</div></div>`;
  }

  const srcAccount = accountOf(status, ev.source.account);
  const srcService = serviceOf(srcAccount || { provider: ev.source.provider });
  const deliveries = ev.deliveries.map((d) => {
    const acc = accountOf(status, d.target.account);
    return { ...d, service: serviceOf(acc || { provider: d.target.provider }) };
  });
  const withCopy = deliveries.find((d) => (d.differs || []).length) || deliveries.find((d) => d.copy) || deliveries[0];
  const chosen = deliveries.find((d) => d.rule === pick) || withCopy;
  const sub = [whenText(ev), ev.location].filter(Boolean).join(" · ");
  const editable = ev.editable || { ok: false, reason: "read_only_calendar" };
  return html`<${Head} title=${ev.title || t("(no title)")} sub=${sub} onClose=${onClose}/>
    <div class="ib">
      <div class="src-row" data-role="source">
        <span class="lb2">${t("Comes from")}</span><${Service} service=${srcService}/>
        <span class="grow"><b>${calendarTitle(ev.source.account, ev.source.calendar_name)}</b><small>${accountLabel(ev.source.account)} · ${serviceName(srcService)}</small></span>
        ${ev.recurring ? html`<span class="tag">${t("Repeats")}</span>` : null}
      </div>
      <div class="fs" data-key="deliveries"><span class="lb">${t("Delivered to")}</span>
        ${deliveries.length
          ? html`<div class="rows">${deliveries.map((d) => {
              const [text, tone] = stateText(d, status);
              return html`<a class="row" href=${`#/rule/${encodeURIComponent(d.rule)}`} data-rule=${d.rule} data-state=${d.state}>
                <${Service} service=${d.service}/>
                <span class="grow">${calendarTitle(d.target.account, d.target.calendar_name)}<small>${accountLabel(d.target.account)} · ${serviceName(d.service)}</small></span>
                <span class=${`r ${tone}`}>${text}</span>
                ${d.state === "blocked" ? html`<span class="vh">${t("Review")}</span>` : null}
              </a>`;
            })}</div>`
          : html`<p class="hp">${t("No line syncs this calendar now.")}</p>`}
      </div>
      ${chosen ? html`<div class="fs" data-key="compare"><span class="lb">${t("Do the original and the copy match?")}</span>
        ${deliveries.length > 1 ? html`<${Seg} label=${t("Compare with")} value=${chosen.rule} onPick=${setPick}
            options=${deliveries.map((d) => [d.rule, d.target.calendar_name])}/>` : null}
        <${Compare} ev=${ev} delivery=${chosen} status=${status}/>
      </div>` : null}
      ${deleting ? html`<${DeleteConfirm} ev=${ev} onCancel=${() => setDeleting(false)} onDone=${onClose}/>` : null}
      ${editable.ok && editable.write_access === false ? html`<div class="note warn" data-role="write-access">
          <p>${t("calsync may not change Google events for this account yet. Allow it once, in a terminal on the computer calsync runs on:")}</p>
          <${Command} command=${editable.command || `calsync auth ${ev.source.account} --write`}/></div>` : null}
    </div>
    ${deleting
      ? null
      : editable.ok
      ? html`<div class="ifoot" data-role="actions">
          <button class="btn danger" type="button" data-role="delete-event" aria-expanded=${deleting ? "true" : "false"}
              onClick=${() => setDeleting(true)}>${t("Delete")}</button>
          <a class="btn primary" href=${`#/event-edit/${encodeURIComponent(ev.id)}`} data-role="edit-event"><${Icon} name="pen"/>${t("Change it")}</a>
        </div>`
      : html`<div class="ifoot"><span class="fh" data-role="locked"><${Icon} name="lock"/>${lockedText(editable.reason)}</span></div>`}`;
}
