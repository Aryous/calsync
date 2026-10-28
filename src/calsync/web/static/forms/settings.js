// The settings (#/settings, from the hub or the gear): what every line shares. 出事时提醒我
// (Bark and webhook URLs, the language, the failure threshold, 发送测试), 检查频率, 在别的设备上
// 打开 (the pair and reset-key commands — no key ever reaches the browser — and the allowed
// addresses), and the config file: its recorded changes and 撤销上一次修改 (after a dry run that
// shows the masked diff).
//
// Every value is written at once through ``config.set`` and read back from GET /api/config. A
// push URL is never shown in full: the config answers it cut to scheme and host, and a new one
// typed here leaves the field as soon as it is saved.

import { html, useCallback, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { when } from "/static/format.js";
import { get, op, useStatus } from "/static/api.js";
import { Icon } from "/static/icons.js";
import { Command } from "/static/command.js";
import { Head, Saved, Seg, failureText, useSaved } from "/static/forms/parts.js";
import { seconds } from "/static/forms/rule.js";

const HISTORY = 8; // changes listed

// ----- the config's history, for a person ----------------------------------------------------------

/** The name of a config key, as a person knows the setting. */
function keyName(key) {
  switch (key) {
    case "notify.bark_url": return t("the Bark address");
    case "notify.webhook_url": return t("the webhook address");
    case "notify.public_url": return t("the public address");
    case "notify.failure_threshold": return t("failures before a push");
    case "notify.language": return t("the push language");
    case "runtime.interval": return t("how often calsync checks");
    case "runtime.jitter": return t("the check's random delay");
    case "web.allowed_hosts": return t("the allowed addresses");
    case "google.client_secret_file": return t("the Google app's client file");
    case "window": return t("the sync range");
    case "alarms": return t("the reminders");
    case "paused": return t("pausing");
    case "retire": return t("removing");
    case "fields.content": return t("what is shown");
    case "fields.attendees": return t("the attendees");
    case "fields.conference": return t("the meeting links");
    case "delete_guard.max_count":
    case "delete_guard.max_ratio": return t("the delete guard");
    default: return key;
  }
}

/** A recorded change's summary (the ops' English) in the reader's words; the summary itself
 * when it is not one the UI knows. */
export function changeText(summary) {
  const s = String(summary || "");
  let m;
  if (s === "edited outside calsync") return t("config.toml was edited by hand");
  if ((m = /^undid change \d+ \((.*)\)$/.exec(s))) return t("Undid: {what}", { what: changeText(m[1]) });
  if ((m = /^rule "(.+?)" added: /.exec(s))) return t("Added the line “{name}”", { name: m[1] });
  if ((m = /^rule "(.+?)": (\S+) set$/.exec(s))) return t("Line “{name}”: changed {what}", { name: m[1], what: keyName(m[2]) });
  if ((m = /^rule "(.+?)" removed after its copies were deleted$/.exec(s))) return t("Removed the line “{name}” after deleting its copies", { name: m[1] });
  if ((m = /^rule "(.+?)" removed/.exec(s))) return t("Removed the line “{name}”", { name: m[1] });
  if ((m = /^rule "(.+?)" paused$/.exec(s))) return t("Paused the line “{name}”", { name: m[1] });
  if ((m = /^rule "(.+?)" resumed$/.exec(s))) return t("Resumed the line “{name}”", { name: m[1] });
  if ((m = /^account "(.+?)" added/.exec(s))) return t("Connected the account “{name}”", { name: m[1] });
  if ((m = /^account "(.+?)" removed/.exec(s))) return t("Disconnected the account “{name}”", { name: m[1] });
  if ((m = /^(\S+) set$/.exec(s))) return t("Changed {what}", { what: keyName(m[1]) });
  if ((m = /^(\S+) cleared$/.exec(s))) return t("Reset {what} to its default", { what: keyName(m[1]) });
  return s;
}

export function sourceName(source) {
  switch (source) {
    case "ui": return t("this page");
    case "cli": return t("the terminal");
    case "external": return t("by hand");
    case "daemon": return t("calsync");
    default: return source;
  }
}

// ----- fields ------------------------------------------------------------------------------------------

/** A number written when it is committed (Enter or leaving it); the file's value comes back
 * when the write is refused. */
function NumberInput({ value, label, onCommit, error }) {
  const [text, setText] = useState(value);
  const shown = useRef(value);
  const input = useRef(null);
  useEffect(() => {
    const el = input.current;
    const typing = el && document.activeElement === el && el.value.trim() !== shown.current;
    shown.current = value;
    if (!typing) setText(value);
  }, [value]);
  const commit = (e) => {
    const next = e.currentTarget.value.trim();
    if (next === shown.current) return;
    if (next === "") {
      setText(shown.current);
      return;
    }
    setText(next);
    Promise.resolve(onCommit(next)).then((ok) => {
      if (ok === false && input.current && input.current.value.trim() === next) setText(shown.current);
    });
  };
  return html`<input class=${error ? "field n num err" : "field n num"} type="text" inputmode="numeric" ref=${input}
      aria-label=${label} aria-invalid=${error ? "true" : "false"} value=${text}
      onInput=${(e) => setText(e.currentTarget.value)} onChange=${commit}
      onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); commit(e); } }}/>`;
}

/** A push URL: what is set (cut to scheme and host by the config), 更换 and 移除; or a field
 * to enter one. What was typed leaves the page once it is saved. */
function UrlSetting({ id, name, value, placeholder, onSave, onClear, error }) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const input = useRef(null);
  const open = editing || !value;
  useEffect(() => {
    if (editing && input.current) input.current.focus();
  }, [editing]);
  const save = async () => {
    const text = input.current ? input.current.value.trim() : "";
    if (!text || busy) return;
    setBusy(true);
    const ok = await onSave(text);
    setBusy(false);
    if (ok) {
      if (input.current) input.current.value = ""; // the full URL leaves the page
      setEditing(false);
    }
  };
  return html`<div class="url-set" data-key=${id}>
    <span class="sub">${name}</span>
    ${open
      ? html`<div class="inl url-edit">
          <input class=${error ? "field w err" : "field w"} type="url" ref=${input} placeholder=${placeholder}
              autocomplete="off" spellcheck="false" aria-label=${name} aria-invalid=${error ? "true" : "false"}
              onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); save(); } if (e.key === "Escape" && value) { e.preventDefault(); e.stopPropagation(); setEditing(false); } }}/>
          <button class=${busy ? "btn busy" : "btn"} type="button" aria-disabled=${busy ? "true" : "false"} onClick=${save}>${t("Save")}</button>
          ${value ? html`<button class="btn quiet" type="button" onClick=${() => setEditing(false)}>${t("Cancel")}</button>` : null}
        </div>`
      : html`<div class="inl url-shown">
          <span class="masked num" data-role="masked">${value}/<span aria-hidden="true">••••••••</span></span>
          <button class="btn" type="button" onClick=${() => setEditing(true)}>${t("Change")}</button>
          <button class="btn quiet" type="button" onClick=${onClear}>${t("Remove")}</button>
        </div>`}
    ${error ? html`<p class="errt" role="alert">${error}</p>` : null}
  </div>`;
}

/** The addresses (host names) this page may be opened under, as pills. */
function Hosts({ hosts, onSet }) {
  const [adding, setAdding] = useState(false);
  const input = useRef(null);
  useEffect(() => {
    if (adding && input.current) input.current.focus();
  }, [adding]);
  const add = async () => {
    const host = input.current ? input.current.value.trim().toLowerCase() : "";
    if (!host) return;
    if (hosts.includes(host)) {
      setAdding(false);
      return;
    }
    if (await onSet([...hosts, host])) setAdding(false);
  };
  return html`<div class="pills" data-role="hosts">
    ${hosts.map((h) => html`<span class="pill num">${h}<button class="x" type="button" aria-label=${t("Remove {what}", { what: h })}
        onClick=${() => onSet(hosts.filter((x) => x !== h))}><${Icon} name="x"/></button></span>`)}
    ${adding
      ? html`<span class="pill-input"><input class="field" type="text" ref=${input} placeholder="mac-mini.tail.ts.net" autocomplete="off"
            spellcheck="false" aria-label=${t("An allowed address")}
            onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setAdding(false); } }}/>
          <button class="btn" type="button" onClick=${add}>${t("Add")}</button></span>`
      : html`<button class="pill add" type="button" onClick=${() => setAdding(true)}><${Icon} name="plus"/>${t("Allowed address")}</button>`}
  </div>`;
}

// ----- 发送测试 ------------------------------------------------------------------------------------------

function channelText(channel, outcome) {
  const who = channel === "bark" ? "Bark" : "Webhook";
  if (outcome === "ok") return t("{who}: delivered", { who });
  if (outcome === "not configured") return t("{who}: not set", { who });
  const m = /^failed \((.+)\)$/.exec(String(outcome));
  return t("{who}: not delivered ({why})", { who, why: m ? m[1] : outcome });
}

function TestPush({ configured }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null); // {lines: [[text, ok]], bad}
  const send = async () => {
    if (busy) return;
    setBusy(true);
    setResult(null);
    try {
      const answer = await op("notify.test");
      const channels = (answer.data && answer.data.channels) || {};
      setResult({ lines: Object.entries(channels).map(([c, o]) => [channelText(c, o), o === "ok"]) });
    } catch (e) {
      if (e.kind === "too_soon") setResult({ lines: [[t("A test was just sent; try again in a few seconds"), false]] });
      else if (e.kind !== "unpaired") setResult({ lines: [[failureText(e), false]] });
    } finally {
      setBusy(false);
    }
  };
  return html`<div class="test-push">
    <button class=${busy ? "btn busy" : "btn"} type="button" aria-disabled=${busy || !configured ? "true" : "false"}
        onClick=${() => configured && send()}>${busy ? t("Sending…") : t("Send a test")}</button>
    <ul class="test-result" role="status" data-role="test-result">
      ${!configured ? html`<li class="hp">${t("Set an address first")}</li>` : null}
      ${result ? result.lines.map(([text, ok]) => html`<li class=${ok ? "ok" : "bad"}>${text}</li>`) : null}
    </ul>
  </div>`;
}

// ----- the config file: history and undo ---------------------------------------------------------------

function Diff({ text }) {
  const lines = String(text || "").split("\n").filter((l) => !/^(---|\+\+\+) /.test(l));
  return html`<pre class="diff" data-role="diff">${lines.map((l) => {
    const cls = l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : "";
    return html`<span class=${cls}>${l}\n</span>`;
  })}</pre>`;
}

function History({ changes, onUndone }) {
  const [confirm, setConfirm] = useState(null); // the dry run: {summary, diff}
  const [note, setNote] = useState(null);
  const [busy, setBusy] = useState(false);
  const button = useRef(null);
  const keep = useRef(null); // 取消: where focus goes when the question opens (the safe answer)
  useEffect(() => {
    if (confirm && keep.current) keep.current.focus();
  }, [confirm]);
  const ask = async () => {
    if (busy) return;
    setBusy(true);
    setNote(null);
    try {
      const dry = await op("config.undo", {}, { dryRun: true });
      if (dry.data && dry.data.would_change === false) setNote({ text: t("There is no change to undo") });
      else setConfirm({ summary: dry.summary, diff: dry.diff });
    } catch (e) {
      setNote({ text: e.kind === "config" ? t("config.toml was changed after that, by hand or elsewhere: undoing it would lose those edits. Edit the file by hand instead.") : failureText(e), bad: true });
    } finally {
      setBusy(false);
    }
  };
  const undo = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await op("config.undo");
      setConfirm(null);
      setNote({ text: t("Undone") });
      await onUndone();
      window.requestAnimationFrame(() => button.current && button.current.focus());
    } catch (e) {
      setConfirm(null);
      setNote({ text: e.kind === "config" ? t("config.toml was changed after that, by hand or elsewhere: undoing it would lose those edits. Edit the file by hand instead.") : failureText(e), bad: true });
    } finally {
      setBusy(false);
    }
  };
  const cancel = () => {
    setConfirm(null);
    window.requestAnimationFrame(() => button.current && button.current.focus());
  };
  return html`
    ${changes.length
      ? html`<ol class="rows hist" data-role="history">${changes.map((c) => html`<li class="row">
          <span class="grow"><span class="when num">${when(c.at)} · ${sourceName(c.source)}</span><small>${changeText(c.summary)}</small></span>
        </li>`)}</ol>`
      : html`<p class="hp after">${t("No change recorded yet.")}</p>`}
    ${confirm
      ? html`<div class="undo-ask" data-role="undo-ask" role="group" aria-labelledby="undo-title"
            onKeyDown=${(e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel(); } }}>
          <h3 id="undo-title">${t("Undo this change?")}</h3>
          <p class="hp">${changeText(String(confirm.summary).replace(/^dry run, nothing written: /, ""))}</p>
          ${confirm.diff ? html`<${Diff} text=${confirm.diff}/>` : null}
          <div class="del-acts">
            <button class="btn quiet" type="button" ref=${keep} onClick=${cancel}>${t("Cancel")}</button>
            <button class=${busy ? "btn primary busy" : "btn primary"} type="button" aria-disabled=${busy ? "true" : "false"} onClick=${undo}>${t("Undo")}</button>
          </div>
        </div>`
      : html`<div class="acts"><button class="btn" type="button" ref=${button} aria-disabled=${busy || !changes.length ? "true" : "false"}
          onClick=${() => changes.length && ask()}><${Icon} name="revert"/>${t("Undo the last change")}</button></div>`}
    <p class=${note && note.bad ? "inote bad" : "inote"} role="status" data-role="undo-note">${note ? note.text : ""}</p>`;
}

// ----- the inspector -----------------------------------------------------------------------------------

/** What a refused setting means, beside it. */
function refusal(key) {
  switch (key) {
    case "notify.bark_url":
    case "notify.webhook_url":
      return t("This is not an http(s) address; not saved.");
    case "notify.failure_threshold":
      return t("Must be a whole number of 1 or more; not saved.");
    case "runtime.interval":
      return t("Must be a whole number of seconds above 0; not saved.");
    case "web.allowed_hosts":
      return t("This is not a host name calsync accepts; not saved.");
    default:
      return t("calsync refused this value; not saved.");
  }
}

export function SettingsForm({ onClose, active = true }) {
  const { status } = useStatus();
  const [config, setConfig] = useState(null);
  const [changes, setChanges] = useState([]);
  const [errors, setErrors] = useState({});
  const [notice, setNotice] = useState(false);
  const [savedOn, flash] = useSaved();
  const reads = useRef(0);
  const chain = useRef(Promise.resolve());

  const load = useCallback(async () => {
    const mine = ++reads.current;
    try {
      const [cfg, hist] = await Promise.all([get("/api/config"), get("/api/config/history", { limit: HISTORY })]);
      if (mine !== reads.current) return;
      setConfig(cfg);
      setChanges(hist.changes || []);
    } catch (e) {
      // keep what is shown; the sentence under the map says when calsync is out of reach
    }
  }, []);
  const stamp = status ? status.generated_at : "";
  useEffect(() => {
    if (active) load();
  }, [load, stamp, active]);

  /** Write setting ``key`` (config.set) in order with the others; resolves to whether it saved. */
  const set = (key, value) => {
    let ok = false;
    chain.current = chain.current.then(async () => {
      try {
        await op("config.set", { key, value });
        ok = true;
        setNotice(false);
        setErrors((x) => ({ ...x, [key]: null }));
        flash();
      } catch (e) {
        if (e.kind === "config") setErrors((x) => ({ ...x, [key]: refusal(key) }));
        else if (e.kind === "conflict") setNotice(true);
        else if (e.kind !== "unpaired") setErrors((x) => ({ ...x, [key]: failureText(e) }));
      }
      await load();
    });
    return chain.current.then(() => ok);
  };

  const head = html`<${Head} title=${t("Settings")} sub=${t("Shared by every line")} onClose=${onClose}/>`;
  if (!config) return html`${head}<div class="ib"><p class="hp">${t("Reading the settings…")}</p></div>`;
  const notify = config.notify || {};
  const interval = seconds((config.runtime || {}).interval);
  const hosts = (config.web || {}).allowed_hosts || [];
  const pushSet = Boolean(notify.bark_url || notify.webhook_url);
  return html`${head}
    <div class="ib">
      ${notice ? html`<div class="note warn" data-role="conflict">${t("config.toml was just changed elsewhere; it has been read again here. Make the change once more if you still want it.")}</div>` : null}
      <div class="fs" data-key="notify"><span class="lb">${t("Tell me when something goes wrong")}</span>
        <${UrlSetting} id="notify.bark_url" name="Bark" value=${notify.bark_url} placeholder=${t("The address the Bark app shows")}
            error=${errors["notify.bark_url"]} onSave=${(v) => set("notify.bark_url", v)} onClear=${() => set("notify.bark_url", "")}/>
        <${UrlSetting} id="notify.webhook_url" name="Webhook" value=${notify.webhook_url} placeholder=${t("The webhook's address")}
            error=${errors["notify.webhook_url"]} onSave=${(v) => set("notify.webhook_url", v)} onClear=${() => set("notify.webhook_url", "")}/>
        <div class="inl" data-key="notify.failure_threshold">${t("Only after")}
          <${NumberInput} label=${t("Failures in a row before a push")} value=${String(notify.failure_threshold)}
              error=${errors["notify.failure_threshold"]} onCommit=${(v) => set("notify.failure_threshold", v)}/>
          ${t("failures in a row")}</div>
        ${errors["notify.failure_threshold"] ? html`<p class="errt" role="alert">${errors["notify.failure_threshold"]}</p>` : null}
        <div class="sub-row" data-key="notify.language"><span class="sub">${t("Push language")}</span>
          <${Seg} label=${t("Push language")} value=${notify.language} onPick=${(v) => set("notify.language", v)}
              options=${[["zh", "中文"], ["en", "English"]]}/></div>
        <${TestPush} configured=${pushSet}/>
      </div>
      <div class="fs" data-key="runtime.interval"><span class="lb">${t("How often to check")}</span>
        <div class="inl">${t("Every")}
          <${NumberInput} label=${t("Seconds between checks")} value=${interval == null ? "" : String(interval)}
              error=${errors["runtime.interval"]} onCommit=${(v) => set("runtime.interval", /^\d+$/.test(v) ? `${v}s` : v)}/>
          ${t("seconds, calsync asks Google what changed")}</div>
        ${errors["runtime.interval"] ? html`<p class="errt" role="alert">${errors["runtime.interval"]}</p>` : null}
      </div>
      <div class="fs" data-key="web"><span class="lb">${t("Open on another device")}</span>
        <p class="hp">${t("Run this on the computer calsync runs on, then open the link it prints (or scan its QR code) on the other device; it stays signed in.")}</p>
        <${Command} command="calsync web pair"/>
        <p class="hp after">${t("To sign every device out, reset the key; each one then has to pair again, this one too.")}</p>
        <${Command} command="calsync web reset-key"/>
        <span class="sub top">${t("Allowed addresses")}</span>
        <p class="hp">${t("Opening this page under another name (a Tailscale address, say)? Add the name here first.")}</p>
        <${Hosts} hosts=${hosts} onSet=${(list) => set("web.allowed_hosts", list.join(","))}/>
        ${errors["web.allowed_hosts"] ? html`<p class="errt" role="alert">${errors["web.allowed_hosts"]}</p>` : null}
      </div>
      <div class="fs" data-key="config"><span class="lb">${t("Config file")}</span>
        <${History} changes=${changes} onUndone=${load}/>
      </div>
    </div>
    <div class="ifoot"><span></span><${Saved} on=${savedOn}/></div>`;
}
