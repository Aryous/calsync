// Dates, times and names as a person reads them. Times are shown in the browser's local zone
// (the API sends ISO 8601 instants with offsets). A calendar is never named without its service
// and account: calendarLabel() is the one place that composes that text.

import { t, getLang } from "/static/i18n.js";

const pad = (n) => String(n).padStart(2, "0");
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function toDate(value) {
  if (value == null) return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "10:42", local. */
export function clockTime(value) {
  const d = toDate(value);
  return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : "";
}

/** "2026-10-26", the local calendar day. */
export function ymd(value) {
  const d = toDate(value);
  return d ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` : "";
}

function weekday(d) {
  switch (WEEKDAYS[d.getDay()]) {
    case "Sun": return t("Sun");
    case "Mon": return t("Mon");
    case "Tue": return t("Tue");
    case "Wed": return t("Wed");
    case "Thu": return t("Thu");
    case "Fri": return t("Fri");
    default: return t("Sat");
  }
}

/** "10月26日 周一" / "Mon 26 Oct", local. */
export function dayLabel(value) {
  const d = toDate(value);
  if (!d) return "";
  if (getLang() === "zh") return `${d.getMonth() + 1}月${d.getDate()}日 ${weekday(d)}`;
  const month = d.toLocaleString("en-GB", { month: "short" });
  return `${weekday(d)} ${d.getDate()} ${month}`;
}

/** "刚刚" / "3 分钟前" / "2 小时前" / "4 天前" relative to ``now`` (ms, default Date.now()). */
export function ago(value, now = Date.now()) {
  const d = toDate(value);
  if (!d) return "";
  const s = Math.max(0, Math.round((now - d.getTime()) / 1000));
  if (s < 45) return t("just now");
  const m = Math.round(s / 60);
  if (m < 60) return t("{n} min ago", { n: m });
  const h = Math.round(m / 60);
  if (h < 36) return t("{n} h ago", { n: h });
  return t("{n} d ago", { n: Math.round(h / 24) });
}

/** "1.2 秒" style durations: "{n} s" under a minute, "{m} min {s} s" above. */
export function duration(ms) {
  if (ms == null) return "";
  const total = ms / 1000;
  if (total < 60) return t("{n} s", { n: total < 10 ? total.toFixed(1) : Math.round(total) });
  const m = Math.floor(total / 60);
  return t("{m} min {s} s", { m, s: Math.round(total - m * 60) });
}

/** The service a calendar lives in, from an account's ``provider`` and ``host`` (status JSON). */
export function serviceOf({ provider, host } = {}) {
  if (provider === "google") return "google";
  if (host === "iCloud") return "icloud";
  return "caldav";
}

export function serviceName(service) {
  if (service === "google") return t("Google Calendar");
  if (service === "icloud") return t("iCloud Calendar");
  return t("CalDAV calendar");
}

/** "家庭日程 · personal": a calendar's name with its account, to render next to the service icon
 * (icons.js CalendarName does both). With ``plain`` (text where no icon can go: a title, an
 * aria-label) the service is named too: "家庭日程 · personal · Google 日历". */
export function calendarLabel({ service, account, name }, { plain = false } = {}) {
  const parts = [name, account];
  if (plain && service) parts.push(serviceName(service));
  return parts.filter(Boolean).join(" · ");
}
