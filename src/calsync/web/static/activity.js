// The activity tab: one timeline, newest first and grouped by local day, of what calsync did —
// the changes it delivered (GET /api/changes: created, updated with what changed, deleted,
// changed back after an iPhone edit, deletes approved, a delete guard raised), its sync cycles
// (GET /api/runs, the runs of one cycle as one row; quiet cycles of a day folded into one) and
// the config's changes (GET /api/config/history). Filters: 全部 / 改动 / 同步 / 异常.

import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t, getLang } from "/static/i18n.js";
import { clockTime, duration, serviceOf, ymd } from "/static/format.js";
import { get, useStatus } from "/static/api.js";
import { CalendarName, Icon } from "/static/icons.js";
import { Seg } from "/static/forms/parts.js";
import { changeText, sourceName } from "/static/forms/settings.js";

const FILTERS = ["all", "changes", "syncs", "problems"];

// ----- words ---------------------------------------------------------------------------------------

function dayHeading(d, now) {
  const today = ymd(now);
  const yesterday = ymd(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1));
  const key = ymd(d);
  const date = getLang() === "zh" ? `${d.getMonth() + 1}月${d.getDate()}日` : `${d.getDate()} ${d.toLocaleString("en-GB", { month: "short" })}`;
  if (key === today) return t("Today · {date}", { date });
  if (key === yesterday) return t("Yesterday · {date}", { date });
  return date;
}

function origin(source) {
  switch (source) {
    case "ui": return t("this page");
    case "cli": return t("the terminal");
    case "run": return t("calsync run --allow-deletes");
    default: return source;
  }
}

function timeOf(value, allDay) {
  if (typeof value !== "string") return String(value);
  if (allDay || /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [y, m, d] = value.slice(0, 10).split("-").map(Number);
    return getLang() === "zh" ? `${m}月${d}日` : `${d}/${m}`;
  }
  return clockTime(value);
}

function fieldValue(name, value) {
  if (value == null || value === "") return t("nothing");
  if (name === "start" || name === "end") return timeOf(value);
  if (name === "alarms_minutes_before") {
    if (!value.length) return t("No reminder");
    return value.map((m) => (m === 0 ? t("At the start") : t("{n} min before", { n: m }))).join(getLang() === "zh" ? "、" : ", ");
  }
  if (name === "all_day") return value ? t("All day") : t("At a time");
  if (name === "recurring") return value ? t("Repeats") : t("Once");
  return String(value);
}

function fieldName(name) {
  switch (name) {
    case "title": return t("Title");
    case "start": return t("Time");
    case "end": return t("End");
    case "location": return t("Location");
    case "alarms_minutes_before": return t("Reminder");
    case "all_day": return t("All day");
    case "recurring": return t("Repeats");
    default: return name;
  }
}

/** "时间改为 09:30 · 原来是 10:00" for an update's first changed field (start before end). */
function updateText(fields) {
  const names = Object.keys(fields || {});
  if (!names.length) return "";
  const name = names.includes("start") ? "start" : names[0];
  const [before, after] = fields[name];
  if (name === "title") return t("Renamed “{after}” · it was “{before}”", { after: fieldValue(name, after), before: fieldValue(name, before) });
  return t("{what} changed to {after} · it was {before}", { what: fieldName(name), after: fieldValue(name, after), before: fieldValue(name, before) });
}

/** "iPhone 上改成了 16:30；Google 里是 16:00" for a change made on the target and changed back. */
function revertText(fields) {
  const names = Object.keys(fields || {});
  if (!names.length) return "";
  const name = names.includes("start") ? "start" : names[0];
  const [edited, ours] = fields[name];
  return t("{what} was changed to {edited} on the iPhone; Google has {ours}", { what: fieldName(name), edited: fieldValue(name, edited), ours: fieldValue(name, ours) });
}

// ----- building the timeline ------------------------------------------------------------------------

/** The end points of each line, from the status. */
function linesOf(status) {
  const out = new Map();
  const service = (account) => {
    const a = ((status && status.accounts) || []).find((x) => x.name === account);
    return a ? serviceOf(a) : "caldav";
  };
  for (const r of (status && status.rules) || []) {
    out.set(r.name, {
      source: { service: service(r.source.account), account: r.source.account, name: r.source.calendar_name || r.source.calendar },
      target: { service: service(r.target.account), account: r.target.account, name: r.target.calendar_name || r.target.calendar },
      busy: r.fields && r.fields.content === "busy",
    });
  }
  return out;
}

const ICON = { created: "plus", updated: "pen", deleted: "minus", reverted: "revert", approved: "check", guard_loosened: "alert" };

function changeItem(c, lines, cycleOf) {
  const line = lines.get(c.rule);
  const cycle = cycleOf.get(c.run_id) || `run${c.run_id}`;
  const title = c.title || t("(no title)");
  const fields = c.detail && c.detail.fields;
  const base = { kind: "change", at: new Date(c.at), rule: c.rule, line, icon: ICON[c.action] || "pen", targets: [c.rule], key: `c${c.id}`, cycle };
  if (line && line.busy && (c.action === "created" || c.action === "updated" || c.action === "deleted")) {
    // A busy-only line's copies all read "Busy": counted per cycle, not listed by title.
    return { ...base, busy: c.action, merge: `${cycle}|busy|${c.action}|${c.rule}`, count: 1 };
  }
  switch (c.action) {
    case "created":
      return { ...base, head: t("Added “{title}”", { title }), sub: "", merge: `${cycle}|created|${title}` };
    case "updated":
      return { ...base, head: t("Updated “{title}”", { title }), sub: updateText(fields), merge: `${cycle}|updated|${title}|${JSON.stringify(fields || {})}` };
    case "deleted":
      return { ...base, head: t("Deleted “{title}”", { title }), sub: "", merge: `${cycle}|deleted|${title}` };
    case "reverted":
      return { ...base, head: t("“{title}” was changed on the iPhone and changed back", { title }), sub: revertText(fields), cls: "rev" };
    case "approved": {
      const m = /^(\d+) deletes? approved \((\w+)\)$/.exec(c.title || "");
      const n = m ? Number(m[1]) : null;
      const who = m ? origin(m[2]) : "";
      return { ...base, kind: "problem-ok", head: n === 1 ? t("Confirmed deleting 1 copy") : t("Confirmed deleting {n} copies", { n: n == null ? "?" : n }), sub: who ? t("From {who}", { who }) : "" };
    }
    case "blocked":
      // Each copy the guard held back is a row in the log: one row per line and sync here.
      return { ...base, kind: "problem", icon: "alert", cls: "held", heldBack: true, merge: `${cycle}|blocked|${c.rule}`, count: 1,
        link: `#/blocked/${encodeURIComponent(c.rule)}` };
    case "protected":
      return { ...base, kind: "problem", icon: "lock", head: t("Left “{title}” as it is", { title }),
        sub: t("Google could not be read in full, so calsync did not touch its copy") };
    case "guard_loosened": {
      const m = /\((\w+)\)$/.exec(c.title || "");
      return { ...base, kind: "problem-ok", head: t("The delete guard was raised while deletes were held back"), sub: m ? t("From {who}", { who: origin(m[1]) }) : "" };
    }
    default:
      return { ...base, head: c.title || c.action, sub: "" };
  }
}

/** The guard's row, once its count is known. */
function heldHead(item) {
  return item.count === 1 ? t("Deleting 1 copy was held back") : t("Deleting {n} copies was held back", { n: item.count });
}

/** The words of a busy-only line's row, once its count is known. */
function busyHead(item) {
  const n = item.count;
  const where = item.line ? item.line.target.name : item.rule;
  if (item.busy === "created") return n === 1 ? t("Added 1 event shown only as “busy” in “{where}”", { where }) : t("Added {n} events shown only as “busy” in “{where}”", { n, where });
  if (item.busy === "deleted") return n === 1 ? t("Deleted 1 event shown only as “busy” in “{where}”", { where }) : t("Deleted {n} events shown only as “busy” in “{where}”", { n, where });
  return n === 1 ? t("Updated 1 event shown only as “busy” in “{where}”", { where }) : t("Updated {n} events shown only as “busy” in “{where}”", { n, where });
}

/** The runs of one cycle (the same start) as one row; quiet ok cycles of a day folded. */
function cycleItems(runs, lines) {
  const cycles = new Map();
  for (const r of runs) {
    const key = r.started_at;
    if (!cycles.has(key)) cycles.set(key, []);
    cycles.get(key).push(r);
  }
  const items = [];
  const quiet = new Map(); // day → {count, at}
  for (const [key, list] of cycles) {
    const at = new Date(key);
    const failed = list.filter((r) => r.status === "failed" || r.status === "error" || (r.failed || 0) > 0);
    const blocked = list.filter((r) => r.status === "blocked");
    const changed = list.reduce((n, r) => n + (r.created || 0) + (r.updated || 0) + (r.deleted || 0) + (r.reverted || 0), 0);
    const ms = Math.max(...list.map((r) => r.duration_ms || 0));
    const requests = list.reduce((n, r) => n + (r.requests || 0), 0);
    const stats = [changed === 1 ? t("1 change") : t("{n} changes", { n: changed }), t("took {time}", { time: duration(ms) }), requests === 1 ? t("1 request") : t("{n} requests", { n: requests })].join(" · ");
    if (failed.length) {
      for (const r of failed) {
        items.push({ kind: "problem", at, icon: "alert", key: `r${r.id}`, rule: r.rule, line: lines.get(r.rule), targets: [r.rule], cls: "bad",
          head: r.error_kind === "auth" ? t("{account}'s authorisation failed", { account: r.error_account || r.rule }) : t("This line's sync failed"),
          sub: r.error_kind === "auth" ? t("Connect the account again; the other lines carry on") : r.error_kind === "config" ? t("calsync's settings for this line need a look") : t("calsync tries again at the next check"),
          link: r.error_kind === "auth" && r.error_account ? `#/account/${encodeURIComponent(r.error_account)}` : `#/rule/${encodeURIComponent(r.rule)}` });
      }
    }
    if (blocked.length) {
      for (const r of blocked) {
        items.push({ kind: "problem", at, icon: "alert", key: `b${r.id}`, rule: r.rule, line: lines.get(r.rule), targets: [r.rule], cls: "held",
          head: t("Deletes were held back"), sub: t("Waiting for you to confirm"), link: `#/blocked/${encodeURIComponent(r.rule)}`, runBlocked: `${key}|${r.rule}` });
      }
    }
    if (failed.length || blocked.length || changed) {
      items.push({ kind: "sync", at, icon: "sync", key: `s${key}`, head: t("Sync done"), sub: stats, lines: new Set(list.map((r) => r.rule)).size });
    } else {
      const day = ymd(at);
      const q = quiet.get(day) || { count: 0, at };
      q.count += 1;
      if (at > q.at) q.at = at;
      quiet.set(day, q);
    }
  }
  for (const [day, q] of quiet) {
    items.push({ kind: "sync", at: q.at, icon: "sync", key: `q${day}`, quiet: true,
      head: q.count === 1 ? t("1 check, nothing new") : t("{n} checks, nothing new", { n: q.count }), sub: t("Last at {time}", { time: clockTime(q.at) }) });
  }
  return items;
}

/** Changes of one cycle with the same action and title (one event, several targets) as one row. */
function mergeTargets(items) {
  const out = [];
  const seen = new Map();
  for (const item of items) {
    if (item.merge && seen.has(item.merge)) {
      const first = seen.get(item.merge);
      if (item.busy || item.heldBack) first.count += 1;
      else if (!first.targets.includes(item.rule)) first.targets.push(item.rule);
      continue;
    }
    const copy = { ...item, targets: [...(item.targets || [])] };
    if (item.merge) seen.set(item.merge, copy);
    out.push(copy);
  }
  return out;
}

const PER_CYCLE = 8; // change rows shown per sync; the rest fold into one row

/** At most PER_CYCLE change rows per sync (a first sync creates hundreds): the rest as one. */
function fold(items) {
  const shown = new Map();
  const out = [];
  for (const item of items) {
    if (item.kind !== "change" || !item.cycle) {
      out.push(item);
      continue;
    }
    const n = shown.get(item.cycle) || 0;
    shown.set(item.cycle, n + 1);
    if (n < PER_CYCLE) out.push(item);
  }
  for (const [cycle, n] of shown) {
    if (n <= PER_CYCLE) continue;
    const at = items.find((i) => i.cycle === cycle).at;
    const rest = n - PER_CYCLE;
    out.push({ kind: "change", at: new Date(at.getTime() - 1), icon: "plus", key: `f${cycle}`, cycle,
      head: rest === 1 ? t("And 1 more change in this sync") : t("And {n} more changes in this sync", { n: rest }), sub: "" });
  }
  return out;
}

export function timeline({ changes, runs, history, status }) {
  const lines = linesOf(status);
  const cycleOf = new Map(runs.map((r) => [r.id, r.started_at]));
  const merged = fold(mergeTargets(changes.map((c) => changeItem(c, lines, cycleOf))).map((i) => (i.busy ? { ...i, head: busyHead(i), sub: "" } : i.heldBack ? { ...i, head: heldHead(i), sub: t("Waiting for you to confirm") } : i)));
  const items = [
    ...merged,
    ...cycleItems(runs, lines),
    ...history.map((h) => ({ kind: "config", at: new Date(h.at), icon: "gear", key: `h${h.id}`, head: changeText(h.summary), sub: t("Settings · from {who}", { who: sourceName(h.source) }) })),
  ];
  const said = new Set(merged.filter((i) => i.heldBack).map((i) => `${i.cycle}|${i.rule}`));
  return items.filter((i) => !(i.runBlocked && said.has(i.runBlocked))).sort((a, b) => b.at - a.at);
}

function matches(item, filter) {
  if (filter === "all") return true;
  if (filter === "changes") return item.kind === "change" || item.kind === "config";
  if (filter === "syncs") return item.kind === "sync";
  return item.kind === "problem" || item.kind === "problem-ok" || item.cls === "rev";
}

// ----- the tab ----------------------------------------------------------------------------------------

function Row({ item, lines }) {
  const line = item.line;
  const targets = (item.targets || []).map((r) => lines.get(r)).filter(Boolean);
  const inner = html`
    <time class="num">${clockTime(item.at)}</time>
    <span class=${`ic ${item.cls || ""}`} aria-hidden="true"><${Icon} name=${item.icon}/></span>
    <span class="grow">
      <b>${item.head}</b>
      ${item.sub ? html`<small>${item.sub}</small>` : null}
      ${line ? html`<small class="from"><${CalendarName} service=${line.source.service} account=${line.source.account} name=${line.source.name}/>
        <span class="arrow" aria-hidden="true">→</span><span class="vh">${t("to")}</span>
        ${targets.map((l) => html`<${CalendarName} service=${l.target.service} account=${l.target.account} name=${l.target.name}/>`)}</small>` : null}
      ${item.kind === "sync" && !item.quiet && item.lines ? html`<small>${item.lines === 1 ? t("1 line") : t("{n} lines", { n: item.lines })}</small>` : null}
    </span>`;
  return item.link
    ? html`<a class="ar" href=${item.link} data-kind=${item.kind}>${inner}</a>`
    : html`<div class="ar" data-kind=${item.kind}>${inner}</div>`;
}

export function ActivityPane() {
  const { status } = useStatus();
  const [filter, setFilter] = useState("all");
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);
  const reads = useRef(0);
  const cycles = status && status.daemon ? status.daemon.cycles : 0;
  const generated = status ? status.generated_at : "";

  useEffect(() => {
    if (!status) return undefined; // the lines name the rows: read once they are known
    const mine = ++reads.current;
    Promise.all([get("/api/changes", { limit: 200 }), get("/api/runs", { limit: 200 }), get("/api/config/history", { limit: 50 })])
      .then(([c, r, h]) => {
        if (mine !== reads.current) return;
        setData({ changes: c.changes || [], runs: r.runs || [], history: h.changes || [] });
        setFailed(false);
      })
      .catch((e) => {
        if (mine === reads.current && e.kind !== "unpaired") setFailed(true);
      });
  }, [cycles, Math.floor(Date.parse(generated || "0") / 60000), Boolean(status)]);

  const lines = linesOf(status);
  const now = new Date();
  const items = data ? timeline({ ...data, status }).filter((i) => matches(i, filter)) : [];
  const groups = [];
  for (const item of items) {
    const day = ymd(item.at);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.items.push(item);
    else groups.push({ day, at: item.at, items: [item] });
  }
  const names = { all: t("All"), changes: t("Changes"), syncs: t("Syncs"), problems: t("Problems") };
  return html`<div class="activity">
    <div class="af"><${Seg} label=${t("Show")} value=${filter} onPick=${setFilter} options=${FILTERS.map((f) => [f, names[f]])}/></div>
    ${failed && !data ? html`<p class="note">${t("Can't read the activity right now.")}</p>` : null}
    ${data && !groups.length ? html`<p class="hp pad" data-role="empty">${t("Nothing here yet.")}</p>` : null}
    ${groups.map((g) => html`<section class="ad" data-day=${g.day}>
      <h3>${dayHeading(g.at, now)}</h3>
      ${g.items.map((item) => html`<${Row} key=${item.key} item=${item} lines=${lines}/>`)}
    </section>`)}
  </div>`;
}
