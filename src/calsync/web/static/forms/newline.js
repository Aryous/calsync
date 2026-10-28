// A new line (#/new, prefilled with ?source=<account>&calendar=<id> or ?target=…, or led by an
// account alone: ?source=<account> / ?target=<account>): choose a
// source calendar (grouped by account, with its service mark), then a target (or a new iCloud
// calendar, created on the first sync: ``create_target``); pairs that already sync are disabled.
// Once both ends are chosen, 预演 asks ``rule.preview`` what the first sync would do — writing
// nothing — and 创建并开始同步 adds the line (``rule.add``), runs it once (``trigger``) and opens
// it; the map sends one comet along the new wire.
//
// The line's name: "<source account>-to-<target account>", with "-2", "-3"… when a line in the
// config has it — or when an earlier line of that name left copies behind (a dry-run ``rule.add``
// says so), since a new line of that name would take those copies over and prune them. Those
// copies stay where they are; the preview says so.
//
// Picks settle for 250 ms before a preview starts, and a superseded preview's requests are
// aborted, so quick picks do not queue dry runs at the daemon.

import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { accountLabel, serviceOf, calendarTitle } from "/static/format.js";
import { ApiError, get, op, useStatus } from "/static/api.js";
import { CalendarName, Icon, Service } from "/static/icons.js";
import { Command } from "/static/command.js";
import { colourOf } from "/static/map.js";
import { Head, failureText, slots } from "/static/forms/parts.js";

const sleep = (ms) => new Promise((done) => window.setTimeout(done, ms));

/** The calendars of one side: [{account, service, calendars: [{key, id, name, series, rules}]}]. */
function sideOf(status, source) {
  return ((status && status.accounts) || [])
    .filter((a) => (a.provider === "google") === source)
    .map((a) => ({
      account: a.name,
      service: serviceOf(a),
      calendars: (a.calendars || []).map((c) => ({ ...c, key: `${a.name}/${c.id}`, account: a.name })),
    }));
}

/** How the config names a calendar: by its name when the account has only one of that name. */
function refOf(group, cal) {
  const same = group.calendars.filter((c) => c.name === cal.name).length;
  return `${group.account}:${same === 1 ? cal.name : cal.id}`;
}

/** Keyboard for a radiogroup: arrows move to the next enabled radio of this group and pick it.
 * Groups are never nested; the key stops here all the same. */
function onRadioKeys(e) {
  const moves = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
  if (!(e.key in moves) || e.target.getAttribute("role") !== "radio") return;
  e.stopPropagation();
  const radios = [...e.currentTarget.querySelectorAll('[role="radio"]:not([disabled])')]
    .filter((r) => r.closest('[role="radiogroup"]') === e.currentTarget);
  const i = radios.indexOf(e.target);
  if (i < 0) return;
  e.preventDefault();
  const next = radios[(i + moves[e.key] + radios.length) % radios.length];
  next.focus();
  next.click();
}

function Radio({ checked, disabled, onPick, tabbable, children, cls = "" }) {
  return html`<button class=${`radio ${cls}`.trim()} type="button" role="radio" aria-checked=${checked ? "true" : "false"}
      disabled=${disabled} tabindex=${tabbable ? "0" : "-1"} onClick=${onPick}>
    <span class="o" aria-hidden="true"></span>${children}
  </button>`;
}

/** Which radio of ``items`` (keys, null for a disabled one) takes the tab stop. */
function tabStop(items, chosen) {
  return items.includes(chosen) ? chosen : items.find((k) => k !== null) || null;
}

function Picker({ groups, chosen, onPick, isTaken, source, extra, role }) {
  const keys = groups.flatMap((g) => g.calendars.map((c) => (isTaken(c) ? null : c.key)));
  const stop = tabStop(extra ? [...keys, "new"] : keys, chosen);
  return html`<div role="radiogroup" aria-labelledby=${`${role}-lb`} data-role=${role} onKeyDown=${onRadioKeys}>
    ${groups.map((g) => html`<p class="grp"><${Service} service=${g.service} small=${true}/>${accountLabel(g.account)}</p>
      ${g.calendars.map((c) => {
        const taken = isTaken(c);
        const coloured = source && (c.rules || []).length;
        return html`<${Radio} checked=${chosen === c.key} disabled=${taken} tabbable=${stop === c.key} onPick=${() => onPick(g, c)}>
          ${source ? html`<i class=${`dot ${coloured ? colourOf(c.key) : "none"}`} aria-hidden="true"></i>` : null}
          <span class="nm">${calendarTitle(g.account, c.name)}</span>
          <small>${taken ? t("already syncing") : source && c.series != null ? t("{n} series", { n: c.series }) : ""}</small>
        <//>`;
      })}`)}
    ${extra ? extra(stop === "new") : null}
  </div>`;
}

function PreviewBlock({ state, target, leftover, choice, onChoice }) {
  return html`<div data-role="preview" role="status">${previewBody(state, target)}</div>
    ${leftover ? html`<${Leftover} leftover=${leftover} target=${target} choice=${choice} onChoice=${onChoice}/>` : null}`;
}

/** The old copies in the target that a removed line left (``leftover``: {name, copies, total}):
 * created as it is, the new line writes its own beside them. The person chooses: take them over
 * (the new line takes the earlier line's name) or delete them first. */
function Leftover({ leftover, target, choice, onChoice }) {
  const count = html`<b class="num">${leftover.total === 1 ? t("1 old copy") : t("{n} old copies", { n: leftover.total })}</b>`;
  const warn = leftover.name
    ? slots(t("{calendar} still has {copies} left by the removed line “{name}”. Created as it is, this line would write its own beside them: the events would show twice."), { calendar: target, copies: count, name: leftover.name })
    : slots(t("{calendar} still has {copies} left by a removed line. Created as it is, this line would write its own beside them: the events would show twice."), { calendar: target, copies: count });
  if (!leftover.name) {
    return html`<div class="note warn leftover" data-role="leftover"><p>${warn}</p>
      <p class="why-h">${t("calsync does not know that line's name any more, so it can't clean them up from here.")}</p></div>`;
  }
  const choices = [
    ["adopt", t("Take the old copies over"), t("The new line takes the name “{name}”: copies that match Google are kept as they are, the others are deleted (when there are many, it stops and asks you first).", { name: leftover.name })],
    ["clear", t("Delete the old copies first"), t("calsync deletes the old copies (when there are many, it stops and asks you first), then creates the new line.")],
  ];
  return html`<div class="note warn leftover" data-role="leftover"><p>${warn}</p>
    <div role="radiogroup" aria-label=${t("What to do with the old copies")} onKeyDown=${onRadioKeys}>
      ${choices.map(([id, title, text]) => html`<button class="radio" type="button" role="radio" data-choice=${id}
          aria-checked=${choice === id ? "true" : "false"} tabindex=${choice === id ? "0" : "-1"} onClick=${() => onChoice(id)}>
        <span class="o" aria-hidden="true"></span><span class="txt"><b>${title}</b><small>${text}</small></span>
      </button>`)}
    </div></div>`;
}

function previewBody(state, target) {
  if (!state) return null;
  if (state.loading) return html`<p class="hp flush" aria-busy="true">${t("Previewing…")}</p>`;
  if (state.error) {
    return html`<div class="note bad" data-role="preview-error">
      <p>${state.error}</p>
      ${state.command ? html`<p class="why-h">${t("Connect it again from a terminal on this computer:")}</p><${Command} command=${state.command}/>` : null}
    </div>`;
  }
  const d = state.data;
  const n = html`<b class="num">${d.series}</b>`;
  return html`<div class="preview">
    <p>${slots(t("Creates {n} series in {calendar}."), { n, calendar: target })}${d.instances_in_window != null ? html` ${t("{n} occurrences in the range.", { n: d.instances_in_window })}` : null}</p>
    <p>${d.target_exists
      ? slots(t("The events already in {calendar} are left alone."), { calendar: target })
      : d.target_would_be_created
        ? t("This calendar does not exist yet: creating the line creates it.")
        : t("This calendar was not found in the account.")}</p>
    ${(state.earlier || []).filter((e) => !(d.leftover || []).some((l) => l.name === e.name)).map((e) => html`<p class="earlier" data-role="earlier">${e.copies === 1
      ? t("An earlier line called {name} left 1 copy behind; it stays in its calendar, and this line does not touch it.", { name: e.name })
      : t("An earlier line called {name} left {n} copies behind; they stay in their calendar, and this line does not touch them.", { name: e.name, n: e.copies })}</p>`)}
  </div>`;
}

/** ``groups`` with ``account``'s first (an account the person came here from). */
function leading(groups, account) {
  if (!account) return groups;
  return [...groups.filter((g) => g.account === account), ...groups.filter((g) => g.account !== account)];
}

export function NewLineForm({ route, onClose, onCreated, onReplace, active = true }) {
  const { status, refresh } = useStatus();
  const q = route.query || {};
  // ?source=<account> or ?target=<account> without a calendar: that account comes first, and its
  // calendar is chosen when it has only one (a newly connected account, from the wizard).
  const leadSource = q.source && !q.calendar ? q.source : null;
  const leadTarget = q.target && !q.calendar ? q.target : null;
  const sources = leading(sideOf(status, true), leadSource);
  const targets = leading(sideOf(status, false), leadTarget);
  const all = (groups) => groups.flatMap((g) => g.calendars.map((c) => [g, c]));
  const find = (groups, key) => all(groups).find(([, c]) => c.key === key) || null;

  const [src, setSrc] = useState(q.source && q.calendar ? `${q.source}/${q.calendar}` : null);
  const [tgt, setTgt] = useState(q.target && q.calendar ? `${q.target}/${q.calendar}` : null); // a key, or "new"
  const [newAccount, setNewAccount] = useState(null);
  const [newName, setNewName] = useState("");
  const [typed, setTyped] = useState(""); // newName, settled (the preview waits for typing to pause)
  const [preview, setPreview] = useState(null); // {loading} | {error, command} | {data, name, args}
  const [creating, setCreating] = useState(false);
  const [failure, setFailure] = useState("");
  const [choice, setChoice] = useState("adopt"); // the old copies: "adopt" | "clear"
  const [stage, setStage] = useState(""); // what creating does now (the old copies first)
  const seq = useRef(0);
  const alive = useRef(true);
  // Closed — by ✕, Esc or Back, even while the panel still slides out: a sync that finishes
  // later opens nothing.
  useEffect(() => {
    if (!active) alive.current = false;
  }, [active]);
  useEffect(() => () => {
    alive.current = false;
  }, []);
  const prefilled = useRef(false);
  useEffect(() => {
    if (prefilled.current || !status || !(leadSource || leadTarget)) return;
    prefilled.current = true;
    const group = (leadSource ? sources : targets).find((g) => g.account === (leadSource || leadTarget));
    if (!group || group.calendars.length !== 1) return;
    if (leadSource) setSrc(group.calendars[0].key);
    else setTgt(group.calendars[0].key);
  }, [Boolean(status)]);

  const srcPair = find(sources, src);
  const tgtPair = tgt && tgt !== "new" ? find(targets, tgt) : null;
  // The line that deletes old copies before this one is created holds the same pair meanwhile:
  // not a line that takes the pair.
  const clearing = useRef(null);
  const taken = (s, c) => Boolean(s) && (s.rules || []).some((r) => r !== clearing.current && (c.rules || []).includes(r));
  const isTakenTarget = (c) => Boolean(srcPair) && taken(srcPair[1], c);
  const isTakenSource = (c) => Boolean(tgtPair) && taken(c, tgtPair[1]);
  const newGroups = targets.filter((g) => g.account);
  const newGroup = newGroups.find((g) => g.account === newAccount) || newGroups[0] || null;

  useEffect(() => {
    const timer = window.setTimeout(() => setTyped(newName.trim()), 350);
    return () => window.clearTimeout(timer);
  }, [newName]);

  // A target that the chosen source already syncs to is no choice any more.
  useEffect(() => {
    if (srcPair && tgtPair && taken(srcPair[1], tgtPair[1])) setTgt(null);
  }, [src, tgt, Boolean(status)]);

  // The arguments of rule.add (without its name), once both ends are chosen.
  let args = null;
  let targetName = null;
  if (srcPair && tgtPair && !taken(srcPair[1], tgtPair[1])) {
    args = { source: refOf(...srcPair), target: refOf(...tgtPair) };
    targetName = html`<${CalendarName} service=${tgtPair[0].service} account=${tgtPair[0].account} name=${tgtPair[1].name}/>`;
  } else if (srcPair && tgt === "new" && newGroup && typed) {
    args = { source: refOf(...srcPair), target: `${newGroup.account}:${typed}`, create_target: true };
    targetName = html`<${CalendarName} service=${newGroup.service} account=${newGroup.account} name=${typed}/>`;
  }
  const argsKey = args ? JSON.stringify(args) : "";

  useEffect(() => {
    setFailure("");
    if (!args) {
      setPreview(null);
      return undefined;
    }
    const mine = ++seq.current;
    const abort = new AbortController();
    const { signal } = abort;
    setPreview({ loading: true });
    const from = srcPair[0].account;
    const to = args.target.split(":")[0];
    const timer = window.setTimeout(async () => {
      try {
        const { name, earlier } = await freeName(args, status, from, to, signal);
        const result = await op("rule.preview", { name, ...args }, { signal });
        if (mine !== seq.current) return;
        const data = result.data || {};
        if (result.needs_human || data.error) {
          const account = data.error_account || from;
          setPreview({ error: t("The preview did not finish: calsync can't reach {account}.", { account: accountLabel(account) }), command: result.needs_human ? result.command : null });
        } else setPreview({ data, name, args, earlier });
      } catch (e) {
        if (mine !== seq.current || e.kind === "unpaired" || e.kind === "aborted") return;
        setPreview({ error: e.kind === "config" ? t("calsync refused this line.") : failureText(e) });
      }
    }, 250);
    return () => {
      window.clearTimeout(timer);
      abort.abort();
    };
  }, [argsKey]);

  const ready = Boolean(preview && preview.data && (preview.data.target_exists || preview.data.target_would_be_created));
  const left = (preview && preview.data && preview.data.leftover) || [];
  const leftover = left.length
    ? { name: left[0].name, copies: left[0].copies, total: left.reduce((n, x) => n + x.copies, 0) }
    : null;

  /** Delete the copies the removed line ``old`` left (a line of that name that only retires),
   * then report whether they are gone (false: held back by the guard, or not finished yet). A
   * scheduled sync may finish the job (and remove that line) before ours runs: then it is done. */
  const clearOld = async (old, chosen) => {
    setStage(t("Deleting the old copies…"));
    clearing.current = old;
    const has = async () => ((await get("/api/config")).rules || []).some((r) => r.name === old);
    if (!(await has())) {
      // A retry after a hold finds the line still there: no second one.
      const added = await op("rule.add", { name: old, source: chosen.source, target: chosen.target, retire: true });
      if (added.needs_human) return false;
    }
    for (let i = 0; i < 50; i += 1) {
      const now = await get("/api/status");
      if ((now.rules || []).some((r) => r.name === old)) break;
      if (!(await has())) return true; // already retired by a sync of its own
      await sleep(100);
    }
    let held = false;
    try {
      const run = await op("trigger", { rule: old, wait: true, timeout: 120 });
      const r = ((run.data && run.data.rules) || {})[old];
      held = Boolean(r && r.status === "blocked");
    } catch (e) {
      // gone before the trigger (an "unknown rule"): a sync of its own retired it
    }
    if (held) return false;
    for (let i = 0; i < 20; i += 1) {
      if (!(await has())) return true;
      await sleep(150);
    }
    return false;
  };

  const create = async () => {
    if (!ready || creating) return;
    const { args: chosen } = preview;
    let { name } = preview;
    setCreating(true);
    setFailure("");
    try {
      if (leftover && leftover.name && choice === "adopt") name = leftover.name;
      if (leftover && leftover.name && choice === "clear" && !(await clearOld(leftover.name, chosen))) {
        refresh();
        setFailure(t("Deleting the old copies was held back or did not finish: look at it first, then create the line again."));
        setPreview((p) => ({ ...p, blockedOld: leftover.name }));
        return;
      }
      setStage("");
      const added = await op("rule.add", { name, ...chosen });
      if (added.needs_human) {
        setPreview({ error: t("This needs you, in a terminal on the computer calsync runs on:"), command: added.command });
        return;
      }
      // The daemon runs the new line once it has read the new config.
      for (let i = 0; i < 50; i += 1) {
        const now = await get("/api/status");
        if ((now.rules || []).some((r) => r.name === name)) break;
        await sleep(100);
      }
      onCreated(name);
      try {
        await op("trigger", { rule: name, wait: true, timeout: 120 });
      } catch (e) {
        // the line exists; its inspector shows how it is doing
      }
      refresh();
      // The line takes this form's place in the history (Back must not return to a form whose
      // line exists now) — unless the person has left the form meanwhile.
      if (alive.current) onReplace(`#/rule/${encodeURIComponent(name)}`);
    } catch (e) {
      if (e.kind !== "unpaired") setFailure(e.kind === "config" ? t("calsync refused this line.") : failureText(e));
    } finally {
      setCreating(false);
      setStage("");
    }
  };

  const pickSource = (g, c) => setSrc(c.key);
  const pickTarget = (g, c) => setTgt(c.key);
  const newCalendar = (tabbable) => html`<${Radio} cls="new" checked=${tgt === "new"} tabbable=${tabbable} onPick=${() => setTgt("new")}>
      <${Icon} name="plus"/><span class="nm">${t("Create a new calendar")}</span>
    <//>`;
  // Beside the targets' radiogroup, not in it: a radiogroup of its own.
  const newCalendarDetails = tgt === "new" ? html`<div class="newcal">
      ${newGroups.length > 1 ? html`<div role="radiogroup" aria-label=${t("In which account")} data-role="new-calendar" onKeyDown=${onRadioKeys}>
        ${newGroups.map((g) => html`<${Radio} checked=${newGroup === g} tabbable=${newGroup === g} onPick=${() => setNewAccount(g.account)}>
          <${Service} service=${g.service} small=${true}/><span class="nm">${accountLabel(g.account)}</span>
        <//>`)}
      </div>` : null}
      <input class="field w" type="text" aria-label=${t("The new calendar's name")} placeholder=${t("The new calendar's name")}
          value=${newName} onInput=${(e) => setNewName(e.currentTarget.value)}/>
    </div>` : null;

  return html`<${Head} title=${t("New line")} sub=${t("Choose a source, then a target")} onClose=${onClose}/>
    <div class="ib">
      ${status ? null : html`<p class="hp">${t("Reading the sync state…")}</p>`}
      <div class="fs"><span class="lb" id="sources-lb">${t("Source")}</span>
        <${Picker} role="sources" groups=${sources} chosen=${src} onPick=${pickSource} isTaken=${isTakenSource} source=${true}/>
      </div>
      <div class="fs"><span class="lb" id="targets-lb">${t("Target")}</span>
        <${Picker} role="targets" groups=${targets} chosen=${tgt} onPick=${pickTarget} isTaken=${isTakenTarget} source=${false} extra=${newCalendar}/>
        ${newCalendarDetails}
      </div>
      <div class="fs"><span class="lb">${t("Preview")}</span><${PreviewBlock} state=${preview} target=${targetName}
          leftover=${ready ? leftover : null} choice=${choice} onChoice=${setChoice}/></div>
      ${failure ? html`<p class="errt" role="alert">${failure}${preview && preview.blockedOld
        ? html` <a href=${`#/blocked/${encodeURIComponent(preview.blockedOld)}`}>${t("Review")}</a>` : null}</p>` : null}
      ${stage ? html`<p class="inote" role="status" data-role="stage">${stage}</p>` : null}
    </div>
    <div class="ifoot">
      <span></span>
      <button class=${creating ? "btn primary busy" : "btn primary"} type="button" disabled=${!ready}
          aria-busy=${creating ? "true" : "false"} onClick=${create}>${creating ? t("Creating and syncing…") : t("Create and start syncing")}</button>
    </div>`;
}

/** A name for the new line that no line in the config (or the status) has, and that no earlier
 * line left copies under: {name, earlier: [{name, copies}]} (the names passed over for their
 * copies). A dry-run refusal is a real one: thrown. */
async function freeName(args, status, from, to, signal) {
  const config = await get("/api/config", null, { signal });
  const used = new Set([...(config.rules || []), ...((status && status.rules) || [])].map((r) => r.name));
  const base = `${from}-to-${to}`;
  const earlier = [];
  for (let i = 1; i <= 50; i += 1) {
    const name = i === 1 ? base : `${base}-${i}`;
    if (used.has(name)) continue;
    const dry = await op("rule.add", { name, ...args }, { dryRun: true, signal });
    const copies = (dry.data && dry.data.earlier_copies) || 0;
    if (!copies) return { name, earlier };
    earlier.push({ name, copies });
  }
  throw new ApiError("no_name");
}
