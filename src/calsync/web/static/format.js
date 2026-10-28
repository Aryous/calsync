// Dates, times and names as a person reads them. Times are shown in the browser's local zone
// (the API sends ISO 8601 instants with offsets). A calendar is never named without its service
// and account: calendarLabel() is the one place that composes that text, accountLabel() the one
// place that names an account.

import { t, getLang } from "/static/i18n.js";

const pad = (n) => String(n).padStart(2, "0");

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

/** The weekday's short name; each key is a literal for the key test. */
function weekday(d) {
  return [t("Sun"), t("Mon"), t("Tue"), t("Wed"), t("Thu"), t("Fri"), t("Sat")][d.getDay()];
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

/** The service's short name, where no icon can say it (a tooltip, an aria-label). */
export function serviceShort(service) {
  if (service === "google") return "Google";
  if (service === "icloud") return "iCloud";
  return "CalDAV";
}

/** How an account is named to a person. The config has no display label yet, so this is the
 * account's key ("personal") — the one place to change when a label field exists. Takes the key
 * or a status account ({name, …}). */
export function accountLabel(account) {
  if (account == null) return "";
  return typeof account === "string" ? account : account.name;
}

/** A calendar's name with its account. Visible text (beside the service icon, which
 * icons.js CalendarName draws): "家庭日程 · personal". With ``plain`` — text where no icon can
 * go (an SVG <title>, a title attribute, an aria-label) — the service is spelled out first:
 * "Google · personal · 家庭日程". */
export function calendarLabel({ service, account, name }, { plain = false } = {}) {
  if (plain) return [service ? serviceShort(service) : "", accountLabel(account), name].filter(Boolean).join(" · ");
  return [name, accountLabel(account)].filter(Boolean).join(" · ");
}

/** A small count as a word, as the concept's sentence has it: "四条线路" (Chinese numerals 1–10,
 * 两 for 2), digits above ten and in English. */
export function countWord(n) {
  if (getLang() !== "zh") return String(n);
  if (n < 1 || n > 10) return `${n} `; // "12 条线路": digits keep their space
  return ["一", "两", "三", "四", "五", "六", "七", "八", "九", "十"][n - 1];
}

/** "10:42" today, "10月25日 周日 10:42" another day. */
export function when(value) {
  if (!value) return "";
  return ymd(value) === ymd(Date.now()) ? clockTime(value) : `${dayLabel(value)} ${clockTime(value)}`;
}
