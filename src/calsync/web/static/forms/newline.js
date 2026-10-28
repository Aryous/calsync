// A new line (#/new, prefilled with ?source=<account>&calendar=<id> or ?target=…): choose a
// source calendar (grouped by account, with its service mark), then a target (or a new iCloud
// calendar, created on the first sync: ``create_target``); pairs that already sync are disabled.
// Once both ends are chosen, 预演 asks ``rule.preview`` what the first sync would do — writing
// nothing — and 创建并开始同步 adds the line (``rule.add``), runs it once (``trigger``) and opens
// it; the map sends one comet along the new wire.
//
// The line's name: "<source account>-to-<target account>", with "-2", "-3"… when it is taken —
// or when an earlier line of that name left copies behind (a dry-run ``rule.add`` says so), since
// a new line of that name would take those copies over.

import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { accountLabel, serviceOf } from "/static/format.js";
import { get, op, useStatus } from "/static/api.js";
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

/** Keyboard for a radiogroup: arrows move to the next enabled radio and pick it. */
function onRadioKeys(e) {
  const moves = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
  if (!(e.key in moves) || e.target.getAttribute("role") !== "radio") return;
  const radios = [...e.currentTarget.querySelectorAll('[role="radio"]:not([disabled])')];
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
          <span class="nm">${c.name}</span>
          <small>${taken ? t("already syncing") : source && c.series != null ? t("{n} series", { n: c.series }) : ""}</small>
        <//>`;
      })}`)}
    ${extra ? extra(stop === "new") : null}
  </div>`;
}

function PreviewBlock({ state, target }) {
  if (!state) return html`<p class="hp flush">${t("Once both ends are chosen, calsync works out here what would happen. Nothing is written until you confirm.")}</p>`;
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
        ? t("This calendar does not exist yet: creating the line creates it in iCloud.")
        : t("This calendar was not found in the account.")}</p>
  </div>`;
}

export function NewLineForm({ route, onClose, onCreated }) {
  const { status, refresh } = useStatus();
  const sources = sideOf(status, true);
  const targets = sideOf(status, false);
  const all = (groups) => groups.flatMap((g) => g.calendars.map((c) => [g, c]));
  const find = (groups, key) => all(groups).find(([, c]) => c.key === key) || null;

  const q = route.query || {};
  const [src, setSrc] = useState(q.source && q.calendar ? `${q.source}/${q.calendar}` : null);
  const [tgt, setTgt] = useState(q.target && q.calendar ? `${q.target}/${q.calendar}` : null); // a key, or "new"
  const [newAccount, setNewAccount] = useState(null);
  const [newName, setNewName] = useState("");
  const [typed, setTyped] = useState(""); // newName, settled (the preview waits for typing to pause)
  const [preview, setPreview] = useState(null); // {loading} | {error, command} | {data, name, args}
  const [creating, setCreating] = useState(false);
  const [failure, setFailure] = useState("");
  const seq = useRef(0);

  const srcPair = find(sources, src);
  const tgtPair = tgt && tgt !== "new" ? find(targets, tgt) : null;
  const taken = (s, c) => Boolean(s) && (s.rules || []).some((r) => (c.rules || []).includes(r));
  const isTakenTarget = (c) => Boolean(srcPair) && taken(srcPair[1], c);
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
    setPreview({ loading: true });
    (async () => {
      try {
        const name = await freeName(args, status, srcPair[0].account, args.target.split(":")[0]);
        const result = await op("rule.preview", { name, ...args });
        if (mine !== seq.current) return;
        const data = result.data || {};
        if (result.needs_human || data.error) {
          const account = data.error_account || srcPair[0].account;
          setPreview({ error: t("The preview did not finish: calsync can't reach {account}.", { account: accountLabel(account) }), command: result.needs_human ? result.command : null });
        } else setPreview({ data, name, args });
      } catch (e) {
        if (mine !== seq.current || e.kind === "unpaired") return;
        setPreview({ error: e.kind === "config" ? t("calsync refused this line.") : failureText(e) });
      }
    })();
    return undefined;
  }, [argsKey]);

  const ready = Boolean(preview && preview.data && (preview.data.target_exists || preview.data.target_would_be_created));

  const create = async () => {
    if (!ready || creating) return;
    const { name, args: chosen } = preview;
    setCreating(true);
    setFailure("");
    try {
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
      window.location.hash = `#/rule/${encodeURIComponent(name)}`;
    } catch (e) {
      if (e.kind !== "unpaired") setFailure(e.kind === "config" ? t("calsync refused this line.") : failureText(e));
    } finally {
      setCreating(false);
    }
  };

  const pickSource = (g, c) => setSrc(c.key);
  const pickTarget = (g, c) => setTgt(c.key);
  const newCalendar = (tabbable) => html`<${Radio} cls="new" checked=${tgt === "new"} tabbable=${tabbable} onPick=${() => setTgt("new")}>
      <${Icon} name="plus"/><span class="nm">${t("Create a new calendar in iCloud")}</span>
    <//>
    ${tgt === "new" ? html`<div class="newcal">
      ${newGroups.length > 1 ? html`<div role="radiogroup" aria-label=${t("In which account")} data-role="new-calendar" onKeyDown=${onRadioKeys}>
        ${newGroups.map((g) => html`<${Radio} checked=${newGroup === g} tabbable=${newGroup === g} onPick=${() => setNewAccount(g.account)}>
          <${Service} service=${g.service} small=${true}/><span class="nm">${accountLabel(g.account)}</span>
        <//>`)}
      </div>` : null}
      <input class="field w" type="text" aria-label=${t("The new calendar's name")} placeholder=${t("The new calendar's name")}
          value=${newName} onInput=${(e) => setNewName(e.currentTarget.value)}/>
    </div>` : null}`;

  return html`<${Head} title=${t("New line")} sub=${t("Choose a source, then a target")} onClose=${onClose}/>
    <div class="ib">
      ${status ? null : html`<p class="hp">${t("Reading the sync state…")}</p>`}
      <div class="fs"><span class="lb" id="sources-lb">${t("Source")}</span>
        <${Picker} role="sources" groups=${sources} chosen=${src} onPick=${pickSource} isTaken=${() => false} source=${true}/>
      </div>
      <div class="fs"><span class="lb" id="targets-lb">${t("Target")}</span>
        <${Picker} role="targets" groups=${targets} chosen=${tgt} onPick=${pickTarget} isTaken=${isTakenTarget} source=${false} extra=${newCalendar}/>
      </div>
      <div class="fs"><span class="lb">${t("Preview")}</span><${PreviewBlock} state=${preview} target=${targetName}/></div>
      ${failure ? html`<p class="errt" role="alert">${failure}</p>` : null}
    </div>
    <div class="ifoot">
      <span class="hp flush">${t("The other settings can be changed afterwards")}</span>
      <button class=${creating ? "btn primary busy" : "btn primary"} type="button" disabled=${!ready}
          aria-busy=${creating ? "true" : "false"} onClick=${create}>${creating ? t("Creating and syncing…") : t("Create and start syncing")}</button>
    </div>`;
}

/** A name for the new line that no line has, and that no earlier line left copies under. */
async function freeName(args, status, from, to) {
  const used = new Set(((status && status.rules) || []).map((r) => r.name));
  const base = `${from}-to-${to}`;
  for (let i = 1; i <= 20; i += 1) {
    const name = i === 1 ? base : `${base}-${i}`;
    if (used.has(name)) continue;
    try {
      const dry = await op("rule.add", { name, ...args }, { dryRun: true });
      if (dry.data && dry.data.earlier_copies) continue;
      return name;
    } catch (e) {
      if (e.kind === "config" && (e.problems || []).some((p) => /duplicate rule name/.test(p))) continue;
      throw e;
    }
  }
  return `${base}-${Date.now() % 100000}`;
}
