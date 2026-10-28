// The headline under the map, from the lines' healths. It names what is true and nothing more:
// a paused, retiring or never-synced line is not "正常工作", and "其他都正常" is said only when
// every other line is ok. States, most urgent first: stopped (auth, failing, blocked), paused,
// retiring (being removed), fresh (never run); then ok.
//
// - every line in one state: "四条线路都在正常工作", "唯一的线路停了", "所有线路都暂停了", …
// - one urgent state, the rest ok: "一条线路停了，其他都正常"
// - otherwise the lead state and a count of each other: "一条线路停了，一条暂停中，两条正常"

import { t, getLang } from "/static/i18n.js";
import { countWord } from "/static/format.js";

const STOPPED = new Set(["auth", "failing", "blocked"]);

/** How many lines are in each state: {total, ok, stopped, paused, retiring, fresh}. */
export function lineCounts(rules) {
  const c = { total: rules.length, ok: 0, stopped: 0, paused: 0, retiring: 0, fresh: 0 };
  for (const rule of rules) {
    if (STOPPED.has(rule.health)) c.stopped += 1;
    else if (rule.health === "paused") c.paused += 1;
    else if (rule.health === "retiring") c.retiring += 1;
    else if (rule.health === "never_run") c.fresh += 1;
    else c.ok += 1;
  }
  return c;
}

// Each literal, so the key test sees it.
function all(state, n) {
  const one = n === 1;
  switch (state) {
    case "ok":
      return one ? t("The only line is working") : t("All {n} lines are working", { n: countWord(n) });
    case "stopped":
      return one ? t("The only line has stopped") : t("Every line has stopped");
    case "paused":
      return one ? t("The only line is paused") : t("Every line is paused");
    case "retiring":
      return one ? t("The only line is being removed") : t("Every line is being removed");
    default:
      return t("Not synced yet");
  }
}

function lead(state, n) {
  const one = n === 1;
  switch (state) {
    case "stopped":
      return one ? t("One line has stopped") : t("{n} lines have stopped", { n: countWord(n) });
    case "paused":
      return one ? t("One line is paused") : t("{n} lines are paused", { n: countWord(n) });
    case "retiring":
      return one ? t("One line is being removed") : t("{n} lines are being removed", { n: countWord(n) });
    default:
      return one ? t("One line has not synced yet") : t("{n} lines have not synced yet", { n: countWord(n) });
  }
}

function rest(state, n) {
  const k = countWord(n);
  switch (state) {
    case "ok":
      return t("{n} working", { n: k });
    case "paused":
      return t("{n} paused", { n: k });
    case "retiring":
      return t("{n} being removed", { n: k });
    default:
      return t("{n} not synced yet", { n: k });
  }
}

const ORDER = ["stopped", "paused", "retiring", "fresh", "ok"];

/** The headline for ``rules`` (the status's lines). */
export function headline(rules) {
  const c = lineCounts(rules);
  if (c.total === 0) return t("No line yet");
  const present = ORDER.filter((state) => c[state] > 0);
  if (present.length === 1) return all(present[0], c.total);
  const zh = getLang() === "zh";
  const [first, ...others] = present;
  if (others.length === 1 && others[0] === "ok") return `${lead(first, c[first])}${zh ? "，" : "; "}${t("the others are fine")}`;
  return [lead(first, c[first]), ...others.map((state) => rest(state, c[state]))].join(zh ? "，" : ", ");
}
