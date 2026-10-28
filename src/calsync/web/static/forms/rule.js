// The line inspector (#/rule/<name>): the line as a chip, its state, 立即同步这一条 and 暂停, and
// the concept's settings, each written at once through ``rule.set`` into config.toml:
// 同步范围, 提醒, 显示内容, 参与人, 会议链接, 目标端被改时 (fixed: revert), 防误删; then 删除这条线路
// with its two ways (keep the copies, or delete them first: the line retires).
//
// The settings come from GET /api/config (the status has no window days or delete guard), read
// again after every write and with every status poll. A choice shows at once and is written in
// order; a refused value (422) is said beside its field and the control goes back to the file's
// value; a conflict (the file changed underneath) reads the line again with a notice.

import { html, useCallback, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { accountLabel, ago, clockTime, when } from "/static/format.js";
import { get, op, useStatus } from "/static/api.js";
import { Icon } from "/static/icons.js";
import { Command } from "/static/command.js";
import { End, Head, RouteChip, Saved, Seg, failureText, slots, useSaved } from "/static/forms/parts.js";

// ----- values as the config writes them and a person reads them --------------------------------

const UNIT = { s: 1, m: 60, h: 3600, d: 86400 };

/** "-10m" → -600 (seconds); null when it is not a duration. */
export function seconds(text) {
  const m = /^([+-]?)(\d+)([smhd])$/.exec(String(text || "").trim());
  if (!m) return null;
  const n = Number(m[2]) * UNIT[m[3]];
  return m[1] === "-" ? -n : n;
}

/** "7d" → "7", "36h" → "1.5": a window bound as days. */
function days(text) {
  const s = seconds(text);
  return s == null ? "" : String(Math.round((s / 86400) * 100) / 100);
}

function amount(secs) {
  const a = Math.abs(secs);
  if (a % 86400 === 0) return a === 86400 ? t("1 day") : t("{n} days", { n: a / 86400 });
  if (a % 3600 === 0) return a === 3600 ? t("1 hour") : t("{n} hours", { n: a / 3600 });
  if (a % 60 === 0) return a === 60 ? t("1 minute") : t("{n} minutes", { n: a / 60 });
  return t("{n} seconds", { n: a });
}

/** "-10m" → "提前 10 分钟", "0s" → "开始时". */
export function offsetLabel(text) {
  const s = seconds(text);
  if (s == null) return String(text);
  if (s === 0) return t("At the start");
  return s < 0 ? t("{amount} before", { amount: amount(s) }) : t("{amount} after the start", { amount: amount(s) });
}

const PRESETS = ["0m", "-5m", "-10m", "-15m", "-30m", "-1h", "-2h", "-1d"];

function percent(ratio) {
  return String(Math.round(ratio * 1000) / 10);
}

/** What a refused value of ``field`` (``typed``, when a person typed it) means, beside it. */
function refusal(field, typed = "") {
  switch (field) {
    case "window.past":
    case "window.future": {
      const what = field === "window.past" ? t("Days in the past") : t("Days in the future");
      if (/^-/.test(typed)) return t("{what} can't be below 0; not saved.", { what });
      if (/^\d*\.\d+$/.test(typed)) return t("{what} must be a whole number; not saved.", { what });
      return t("{what} must be a whole number of 0 or more; not saved.", { what });
    }
    case "delete_guard.max_count":
      return t("Must be a whole number of 0 or more; not saved.");
    case "delete_guard.max_ratio":
      return t("The percentage must be above 0 and at most 100; not saved.");
    case "alarms":
      return t("This reminder was not saved.");
    default:
      return t("calsync refused this value; not saved.");
  }
}

/** The note of 立即同步这一条, from the trigger's answer. */
function syncNote(result, name) {
  const data = result.data || {};
  if (data.ran === false) {
    if (data.reason === "timeout") return { text: t("The sync is still running; look again in a moment") };
    return { text: t("This sync did not finish"), bad: true };
  }
  const run = (data.rules || {})[name];
  if (!run) return { text: t("This sync did not finish"), bad: true };
  if (run.status === "blocked") return { text: t("Copies are waiting for you to confirm deleting"), bad: true };
  if (run.status !== "ok") return { text: t("This sync did not finish"), bad: true };
  const n = (run.created || 0) + (run.updated || 0) + (run.deleted || 0);
  if (n === 0) return { text: t("Done: nothing new to deliver") };
  return { text: n === 1 ? t("Done: 1 change delivered") : t("Done: {n} changes delivered", { n }) };
}

// ----- the state line ------------------------------------------------------------------------------

function StateLine({ live, rule, status }) {
  const health = rule && rule.retire ? "retiring" : live ? live.health : "never_run";
  const copies = live ? live.copies || 0 : 0;
  if (health === "paused") return html`<div class="statl paused"><i></i>${t("Paused · changes wait and arrive together when it resumes")}</div>`;
  if (health === "retiring") {
    const next = status && status.daemon && status.daemon.next_cycle_at;
    const what = !live
      ? t("Being removed · its copies in the target calendar are deleted first, then the line disappears")
      : copies === 0
        ? t("Being removed · no copies to delete, the line disappears at the next check")
        : copies === 1
          ? t("Being removed · its 1 copy in the target calendar is deleted first, then the line disappears")
          : t("Being removed · its {n} copies in the target calendar are deleted first, then the line disappears", { n: copies });
    return html`<div class="statl retiring"><i></i><span>${what}${next ? html` · <span class="num">${t("next check {time}", { time: clockTime(next) })}</span>` : null}</span></div>`;
  }
  if (health === "auth") {
    const account = (live.last_run && live.last_run.error_account) || live.source.account;
    const since = live.last_ok_at ? t("not updated since {time}", { time: when(live.last_ok_at) }) : t("not synced yet");
    return html`<div class="statl bad"><i></i>${t("{account}'s authorisation failed · {since}", { account: accountLabel(account), since })}</div>`;
  }
  if (health === "failing") {
    const n = live.consecutive_failures || 1;
    const last = live.last_ok_at ? t("last synced at {time}", { time: when(live.last_ok_at) }) : t("not synced yet");
    return html`<div class="statl bad"><i></i>${n === 1 ? t("Failed once · {last}", { last }) : t("Failed {n} times in a row · {last}", { n, last })}</div>`;
  }
  if (health === "blocked") {
    return html`<div class="statl held"><i></i><span>${t("Copies are waiting for you to confirm deleting")} · <a href=${`#/blocked/${encodeURIComponent(live.name)}`}>${t("Review")}</a></span></div>`;
  }
  if (health === "never_run" || !live || !live.last_ok_at) return html`<div class="statl wait"><i></i>${t("Not synced yet")}</div>`;
  return html`<div class="statl"><i></i>${t("Working · synced {when} · {n} series synced", { when: ago(live.last_ok_at), n: copies })}</div>`;
}

// ----- the settings ----------------------------------------------------------------------------------

/** A text field that writes when it is committed (Enter or leaving it), not on every key. */
function NumberField({ value, label, onCommit, error }) {
  const [text, setText] = useState(value);
  const shown = useRef(value);
  const input = useRef(null);
  // The file's value, whenever it changes — unless a person is typing a new one in this field.
  useEffect(() => {
    const el = input.current;
    const typing = el && document.activeElement === el && el.value.trim() !== shown.current;
    shown.current = value;
    if (!typing) setText(value);
  }, [value]);
  // The value comes from the field itself: a key can arrive before the render that follows the
  // last input, and this render's ``text`` would then be stale.
  const commit = (e) => {
    const next = e.currentTarget.value.trim();
    if (next === shown.current) return;
    if (next === "") {
      setText(shown.current); // nothing typed: keep what the file says
      return;
    }
    setText(next);
    // Refused: back to what the file says — unless a newer value has been typed since.
    Promise.resolve(onCommit(next)).then((ok) => {
      if (ok === false && input.current && input.current.value.trim() === next) setText(shown.current);
    });
  };
  return html`<input class=${error ? "field n num err" : "field n num"} type="text" inputmode="decimal" ref=${input}
      aria-label=${label} aria-invalid=${error ? "true" : "false"} value=${text}
      onInput=${(e) => setText(e.currentTarget.value)} onChange=${commit}
      onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); commit(e); } }}/>`;
}

function Errors({ errors, keys }) {
  const shown = keys.map((k) => errors[k]).filter(Boolean);
  return shown.map((text) => html`<p class="errt" role="alert">${text}</p>`);
}

function Alarms({ value, onSet }) {
  const [adding, setAdding] = useState(false);
  const mode = Array.isArray(value) ? "custom" : value === "strip" ? "strip" : "copy";
  const list = Array.isArray(value) ? value : [];
  const pick = (next) => {
    setAdding(false);
    if (next === "custom") onSet(["-10m"]);
    else onSet(next);
  };
  const add = (offset) => {
    setAdding(false);
    onSet([...list, offset]);
  };
  const remove = (i) => onSet(list.filter((_, j) => j !== i));
  const have = new Set(list.map(seconds));
  return html`
    <${Seg} label=${t("Reminders")} value=${mode} onPick=${pick}
        options=${[["copy", t("Same as Google")], ["custom", t("Set them here")], ["strip", t("No reminders")]]}/>
    ${mode === "custom" ? html`<div class="pills">
      ${list.map((o, i) => html`<span class="pill">${offsetLabel(o)}${list.length > 1 ? html`<button class="x" type="button"
          aria-label=${t("Remove {what}", { what: offsetLabel(o) })} onClick=${() => remove(i)}><${Icon} name="x"/></button>` : null}</span>`)}
      ${adding ? null : html`<button class="pill add" type="button" onClick=${() => setAdding(true)}><${Icon} name="plus"/>${t("Add a reminder")}</button>`}
    </div>
    ${adding ? html`<div class="pills choices" role="group" aria-label=${t("Add a reminder")}>
      ${PRESETS.filter((p) => !have.has(seconds(p))).map((p) => html`<button class="pill choice" type="button" onClick=${() => add(p)}>${offsetLabel(p)}</button>`)}
      <button class="pill add" type="button" onClick=${() => setAdding(false)}>${t("Cancel")}</button>
    </div>` : null}` : null}`;
}

function Settings({ rule, view, set, errors }) {
  const window_ = view("window"); // with the writes on their way: a bound is written with the other's newest value
  const fileWindow = rule.window || window_; // what the fields show: the file (just switched on: the default)
  const fields = (key) => view(`fields.${key}`);
  const content = fields("content");
  return html`
    <div class="fs" data-key="window"><span class="lb">${t("Sync range")}</span>
      <${Seg} label=${t("Sync range")} value=${window_ ? "window" : "all"}
          onPick=${(v) => set("window", "window", v === "all" ? "all" : "7d:180d", v === "all" ? null : { past: "7d", future: "180d" })}
          options=${[["all", t("Everything")], ["window", t("Only a span of time")]]}/>
      ${window_ ? html`<div class="inl">${t("From")}
          <${NumberField} label=${t("Days in the past")} value=${days(fileWindow.past)} error=${errors["window.past"]}
              onCommit=${(text) => set("window.past", "window", `${text}d:${window_.future}`, { ...window_, past: `${text}d` }, text)}/>
          ${t("days ago to")}
          <${NumberField} label=${t("Days in the future")} value=${days(fileWindow.future)} error=${errors["window.future"]}
              onCommit=${(text) => set("window.future", "window", `${window_.past}:${text}d`, { ...window_, future: `${text}d` }, text)}/>
          ${t("days ahead")}</div>`
        : null}
      <${Errors} errors=${errors} keys=${["window", "window.past", "window.future"]}/>
    </div>
    <div class="fs" data-key="alarms"><span class="lb">${t("Reminders")}</span>
      <${Alarms} value=${view("alarms")} onSet=${(v) => set("alarms", "alarms", Array.isArray(v) ? v.join(",") : v, v)}/>
      <${Errors} errors=${errors} keys=${["alarms"]}/>
    </div>
    <div class="fs" data-key="fields.content"><span class="lb">${t("What is shown")}</span>
      <${Seg} label=${t("What is shown")} value=${content} onPick=${(v) => set("fields.content", "fields.content", v, v)}
          options=${[["full", t("Full content")], ["busy", t("Only “busy”")]]}/>
      <${Errors} errors=${errors} keys=${["fields.content"]}/>
    </div>
    <div class="fs" data-key="fields.attendees"><span class="lb">${t("Attendees")}</span>
      <${Seg} label=${t("Attendees")} value=${fields("attendees")} onPick=${(v) => set("fields.attendees", "fields.attendees", v, v)}
          options=${[["none", t("Don't copy")], ["notes", t("Put them in the notes")]]}/>
      <p class="hp after">${t("Attendees are never sent an invitation.")}</p>
      <${Errors} errors=${errors} keys=${["fields.attendees"]}/>
    </div>
    <div class="fs" data-key="fields.conference"><span class="lb">${t("Meeting links")}</span>
      <${Seg} label=${t("Meeting links")} value=${fields("conference")} onPick=${(v) => set("fields.conference", "fields.conference", v, v)}
          options=${[["copy", t("Copy")], ["none", t("Don't copy")]]}/>
      <${Errors} errors=${errors} keys=${["fields.conference"]}/>
    </div>
    <div class="fs" data-key="on_target_edit"><span class="lb">${t("When the target is edited")}</span>
      <div class="locked"><${Icon} name="lock"/>${t("Change it back to the Google version")}</div>
    </div>
    <div class="fs" data-key="delete_guard"><span class="lb">${t("Delete guard")}</span>
      <div class="inl first">${t("When one sync would delete more than")}
        <${NumberField} label=${t("Most copies deleted at once")} value=${String(rule.delete_guard.max_count)}
            error=${errors["delete_guard.max_count"]}
            onCommit=${(text) => set("delete_guard.max_count", "delete_guard.max_count", text, Number(text))}/>
        ${t("copies, or more than")}
        <${NumberField} label=${t("Most copies deleted at once, as a percentage")} value=${percent(rule.delete_guard.max_ratio)}
            error=${errors["delete_guard.max_ratio"]}
            onCommit=${(text) => {
              const n = /^-?\d+(\.\d+)?$/.test(text) ? Number(text) / 100 : text; // a non-number: the daemon says so
              return set("delete_guard.max_ratio", "delete_guard.max_ratio", n, typeof n === "number" ? n : rule.delete_guard.max_ratio);
            }}/>
        ${t("%, stop and ask me first.")}</div>
      <${Errors} errors=${errors} keys=${["delete_guard.max_count", "delete_guard.max_ratio"]}/>
    </div>`;
}

// ----- deleting --------------------------------------------------------------------------------------

function DeleteChoice({ live, rule, status, onCancel, onConfirm, busy }) {
  const [choice, setChoice] = useState("keep");
  const group = useRef(null);
  useEffect(() => {
    const checked = group.current && group.current.querySelector('[aria-checked="true"]');
    if (checked) checked.focus({ preventScroll: true });
  }, []);
  const copies = live ? live.copies || 0 : null; // null: not known (the status does not list the line)
  const target = html`<${End} status=${status} spec=${live ? live.target : rule.target}/>`;
  const count = html`<b class="num">${copies === 1 ? t("1 copy") : t("{n} copies", { n: copies })}</b>`;
  let drop;
  if (copies === 0) drop = slots(t("calsync has not written any copy into {calendar} yet: the line disappears at the next check."), { calendar: target });
  else if (copies === null) drop = slots(t("calsync first deletes the copies it wrote into {calendar}; when there are many, it stops and waits for you to confirm. Once the copies are gone, the line disappears."), { calendar: target });
  else drop = slots(t("calsync first deletes the {copies} it wrote into {calendar}; when there are many, it stops and waits for you to confirm. Once the copies are gone, the line disappears."), { copies: count, calendar: target });
  const choices = [
    ["keep", t("Keep the copies"), slots(t("What was synced stays in {calendar}: no longer updated, and calsync never deletes it. The line disappears at once."), { calendar: target })],
    ["drop", t("Delete the copies too"), drop],
  ];
  const onKeyDown = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation(); // closes this choice, not the inspector
      onCancel();
      return;
    }
    if (["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(e.key) && e.target.getAttribute("role") === "radio") {
      e.preventDefault();
      const next = choice === "keep" ? "drop" : "keep";
      setChoice(next);
      window.requestAnimationFrame(() => {
        const el = group.current && group.current.querySelector(`[data-choice="${next}"]`);
        if (el) el.focus();
      });
    }
  };
  return html`<div class="del" data-role="delete" role="group" aria-labelledby="del-title" onKeyDown=${onKeyDown}>
    <h3 id="del-title">${t("Delete this line?")}</h3>
    <div role="radiogroup" aria-labelledby="del-title" ref=${group}>
      ${choices.map(([id, title, text]) => html`<button class="radio" type="button" role="radio" data-choice=${id}
          aria-checked=${choice === id ? "true" : "false"} tabindex=${choice === id ? "0" : "-1"} onClick=${() => setChoice(id)}>
        <span class="o" aria-hidden="true"></span><span class="txt"><b>${title}</b><small>${text}</small></span>
      </button>`)}
    </div>
    <div class="del-acts">
      <button class="btn quiet" type="button" onClick=${onCancel}>${t("Cancel")}</button>
      <button class=${busy ? "btn danger-solid busy" : "btn danger-solid"} type="button" aria-disabled=${busy ? "true" : "false"}
          onClick=${() => !busy && onConfirm(choice)}>${t("Delete")}</button>
    </div>
  </div>`;
}

// ----- the inspector ---------------------------------------------------------------------------------

export function RuleForm({ route, onClose }) {
  const { status, cycleRunning, refresh } = useStatus();
  const name = route.rule;
  const live = status ? (status.rules || []).find((r) => r.name === name) || null : null;
  const [rule, setRule] = useState(undefined); // the config's line; null: there is none
  const [overlay, setOverlay] = useState({}); // choices shown before their write lands: key → value
  const [errors, setErrors] = useState({});
  const [notice, setNotice] = useState(false); // a conflict reloaded the line
  const [human, setHuman] = useState(null); // an op that needs a person: {command, reason}
  const [savedOn, flash] = useSaved();
  const [note, setNote] = useState(null); // 立即同步这一条
  const [syncing, setSyncing] = useState(false);
  const [working, setWorking] = useState(""); // "pause" | "delete" | "unretire"
  const [deleting, setDeleting] = useState(false);
  const chain = useRef(Promise.resolve());
  const deleteButton = useRef(null);
  const unretire = useRef(null);
  const focusUnretire = useRef(false); // after "delete with the copies": focus 取消移除, not the body
  const removedHere = useRef(false); // deleted (copies kept) from here: the panel closes as it was
  const [retired, setRetired] = useState(false); // seen retiring, then gone: its copies were deleted
  const reads = useRef(0); // only the newest read of the config is shown
  const current = useRef(undefined); // the rule as last rendered
  const lastView = useRef(null);
  current.current = rule;

  const load = useCallback(async () => {
    const mine = ++reads.current;
    try {
      const config = await get("/api/config");
      if (mine !== reads.current) return; // a newer read was asked for since
      const found = (config.rules || []).find((r) => r.name === name) || null;
      if (found === null && removedHere.current) return;
      if (found === null && current.current && current.current.retire) setRetired(true);
      setRule(found);
    } catch (e) {
      // keep what is shown; the sentence under the map says when calsync is out of reach
    }
  }, [name]);

  useEffect(() => {
    if (focusUnretire.current && unretire.current) {
      focusUnretire.current = false;
      unretire.current.focus();
    }
  });
  const stamp = status ? status.generated_at : "";
  useEffect(() => {
    load();
  }, [load, stamp]);

  const view = (key) => {
    if (key in overlay) return overlay[key].value;
    if (!rule) return undefined;
    const [head, leaf] = key.split(".");
    return leaf ? rule[head] && rule[head][leaf] : rule[key];
  };

  /** Write ``key`` (rule.set) — shown as ``shown`` at once — in order with the other writes;
   * resolves to whether it was saved. */
  const set = (field, key, value, shown, typed = "") => {
    const token = {};
    setOverlay((o) => ({ ...o, [key]: { value: shown, token } }));
    let ok = false;
    chain.current = chain.current.then(async () => {
      try {
        const result = await op("rule.set", { name, key, value });
        if (result.needs_human) setHuman(result);
        else {
          ok = true;
          setHuman(null);
          setNotice(false);
          setErrors((x) => ({ ...x, [field]: null })); // cleared when a write of it lands, not when queued
          flash();
        }
      } catch (e) {
        if (e.kind === "config") setErrors((x) => ({ ...x, [field]: refusal(field, typed) }));
        else if (e.kind === "conflict") setNotice(true);
        else if (e.kind !== "unpaired") setErrors((x) => ({ ...x, [field]: failureText(e) }));
      }
      await load();
      setOverlay((o) => {
        if (!o[key] || o[key].token !== token) return o;
        const next = { ...o };
        delete next[key];
        return next;
      });
      refresh();
    });
    return chain.current.then(() => ok);
  };

  /** An op that is not a setting (pause, resume, remove): its own busy state. */
  const act = async (what, opName, args, after) => {
    if (working) return;
    setWorking(what);
    try {
      const result = await op(opName, args);
      if (result.needs_human) setHuman(result);
      else {
        setHuman(null);
        if (after) await after(result);
        else flash();
      }
    } catch (e) {
      if (e.kind === "conflict") setNotice(true);
      else if (e.kind !== "unpaired") setNote({ text: failureText(e), bad: true });
    } finally {
      setWorking("");
      await load();
      refresh();
    }
  };

  const syncOne = async () => {
    if (syncing) return;
    setSyncing(true);
    setNote(null);
    try {
      setNote(syncNote(await op("trigger", { rule: name, wait: true, timeout: 120 }), name));
    } catch (e) {
      if (e.kind !== "unpaired") setNote({ text: failureText(e), bad: true });
    } finally {
      setSyncing(false);
      refresh();
    }
  };

  const remove = (choice) =>
    act("delete", "rule.remove", choice === "drop" ? { name, delete_copies: true } : { name }, async () => {
      setDeleting(false);
      if (choice === "keep") {
        removedHere.current = true;
        refresh();
        onClose();
      } else {
        focusUnretire.current = true;
        flash();
      }
    });

  if (removedHere.current && lastView.current) return lastView.current; // sliding out as it was
  const head = html`<${Head} title=${t("Line")} sub=${t("Line settings · changes save at once")} onClose=${onClose}/>`;
  if (rule === undefined) return html`${head}<div class="ib"><p class="hp">${t("Reading the line…")}</p></div>`;
  if (rule === null) {
    return html`${head}<div class="ib"><div class="note">${retired
      ? t("Its copies have been deleted, and the line has been removed.")
      : t("This line is no longer there. It may have been deleted from the terminal or in config.toml.")}</div></div>`;
  }

  const paused = view("paused");
  const retiring = Boolean(rule.retire);
  const spec = live || { name, source: rule.source, target: rule.target };
  const syncLabel = syncing ? t("Syncing…") : t("Sync this line now");
  lastView.current = html`${head}
    <div class="ib">
      <${RouteChip} status=${status} rule=${spec}/>
      <${StateLine} live=${live} rule=${rule} status=${status}/>
      <div class="acts">
        <button class=${syncing ? "btn primary busy" : "btn primary"} type="button" disabled=${paused}
            aria-busy=${syncing ? "true" : "false"} aria-disabled=${syncing || cycleRunning ? "true" : "false"}
            onClick=${() => !cycleRunning && syncOne()}><${Icon} name="sync"/>${syncLabel}</button>
        ${retiring
          ? html`<button class="btn" type="button" ref=${unretire} onClick=${() => act("unretire", "rule.set", { name, key: "retire", value: false })}>${t("Stop removing")}</button>`
          : html`<button class="btn" type="button" aria-disabled=${working === "pause" ? "true" : "false"}
              onClick=${() => act("pause", paused ? "rule.resume" : "rule.pause", { name })}>${paused ? t("Resume") : t("Pause")}</button>`}
        ${live && live.health === "auth" ? html`<a class="btn" href=${`#/account/${encodeURIComponent((live.last_run && live.last_run.error_account) || live.source.account)}`}>${t("Reconnect")}</a>` : null}
      </div>
      <p class=${note && note.bad ? "inote bad" : "inote"} role="status" data-role="sync-note">${note ? note.text : ""}</p>
      ${notice ? html`<div class="note warn" data-role="conflict">${t("config.toml was just changed elsewhere; it has been read again here. Make the change once more if you still want it.")}</div>` : null}
      ${human ? html`<div class="note" data-role="human"><p>${t("This needs you, in a terminal on the computer calsync runs on:")}</p><${Command} command=${human.command}/></div>` : null}
      ${retiring ? null : html`<${Settings} rule=${rule} view=${view} set=${set} errors=${errors}/>`}
    </div>
    <div class=${deleting ? "ifoot open" : "ifoot"}>
      ${deleting
        ? html`<${DeleteChoice} live=${live} rule=${rule} status=${status} busy=${working === "delete"} onConfirm=${remove}
            onCancel=${() => { setDeleting(false); window.requestAnimationFrame(() => deleteButton.current && deleteButton.current.focus()); }}/>`
        : html`${retiring ? html`<span></span>` : html`<button class="btn danger" type="button" ref=${deleteButton} onClick=${() => setDeleting(true)}>${t("Delete this line")}</button>`}
          <${Saved} on=${savedOn}/>`}
    </div>`;
  return lastView.current;
}
