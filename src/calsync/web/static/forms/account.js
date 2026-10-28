// Accounts: the account inspector (#/account/<name>) and the connect wizard
// (#/connect/<google|icloud>).
//
// The inspector shows how the account is doing (its lines' last runs: 连接正常, 授权失效 with the
// usual reasons, or not known yet), its calendars from the cache with 刷新列表
// (``calendars.refresh``), the reconnect command (``calsync auth <name>``: a secret is only ever
// typed in the terminal, D10), and 断开这个账号: a dry-run ``account.remove`` first, so a refusal
// (lines still use the account) is the op's own and names those lines; the removal itself never
// asks for the credential to be deleted — that stays a terminal command.
//
// The wizard has four steps: a name; the Google client secret (a one-time setup, shown as a
// command until ``GET /api/config`` says it is configured) or the iCloud server and Apple ID;
// ``account.add``, then the ``calsync auth`` command it answers with while the page waits for
// the account's calendars (``calendars.refresh`` every 10 s, and at once when the page becomes
// visible again — the person comes back from the terminal); and the way into a new line from
// those calendars. Everything the wizard waits for stops when the inspector closes (``active``).
// It starts afresh each time it opens: an account added on an earlier visit is on the map, and
// its inspector carries the same command.

import { html, useEffect, useLayoutEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { accountLabel, serviceOf } from "/static/format.js";
import { get, op, useStatus } from "/static/api.js";
import { CalendarName, Icon, Service } from "/static/icons.js";
import { Command } from "/static/command.js";
import { colourOf, mapModel } from "/static/map.js";
import { End, Head, Seg, accountOf, authReasons, failureText, shellWord, slots } from "/static/forms/parts.js";

const REFRESH_EVERY = 10_000; // the wizard's calendars.refresh while it waits for the person
const CONFIG_EVERY = 3_000; // its GET /api/config while it waits for a terminal command
const NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;
// The scheme a CalDAV server's address starts with, as text for a person (a placeholder, a
// message). Composed, so the static check for URLs a page could load stays strict.
const SCHEME = "https:" + "//";

function kindOf(account) {
  const service = serviceOf(account);
  if (service === "google") return t("Google Calendar account");
  if (service === "icloud") return t("iCloud account");
  return account.host ? t("CalDAV account · {host}", { host: account.host }) : t("CalDAV account");
}

/** The lines (status rules) that use ``name`` at either end. */
function linesOf(status, name) {
  return ((status && status.rules) || []).filter((r) => r.source.account === name || r.target.account === name);
}

/** A line named by its ends, as a row that opens it. */
function LineRow({ status, rule }) {
  return html`<a class="row" href=${`#/rule/${encodeURIComponent(rule.name)}`} data-rule=${rule.name}>
    <span class="grow route"><${End} status=${status} spec=${rule.source} fallback="google"/><span class="arrow" aria-hidden="true">→</span><span class="vh">${t("to")}</span><${End} status=${status} spec=${rule.target}/></span>
    <${Icon} name="right"/>
  </a>`;
}

/** Repeat ``check`` every ``every`` ms while ``on`` — and at once when the page becomes visible
 * again. One check at a time: a request that comes during a check runs right after it. A layout
 * effect: it stops in the very commit that turns ``on`` off (the inspector closing), not a frame
 * later. */
function usePoll(on, every, check, deps) {
  useLayoutEffect(() => {
    if (!on) return undefined;
    let alive = true;
    let timer = 0;
    let running = false;
    let again = false;
    const run = async () => {
      window.clearTimeout(timer);
      if (running) {
        again = true;
        return;
      }
      running = true;
      try {
        await check(() => alive);
      } catch (e) {
        // each check reports its own failures
      } finally {
        running = false;
      }
      if (!alive) return;
      if (again) {
        again = false;
        run();
        return;
      }
      timer = window.setTimeout(run, document.hidden ? every * 3 : every);
    };
    const onVisible = () => {
      if (!document.hidden) run();
    };
    run();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [on, ...deps]);
}

// ----- the account inspector ---------------------------------------------------------------------

function StateLine({ status, account }) {
  if (account.auth === "failed") return null; // the note below says it, with the reasons
  if (account.auth === "ok") return html`<div class="statl" data-role="state"><i></i><span>${t("Connected")}</span></div>`;
  const text = linesOf(status, account.name).length ? t("Not known yet: no line using it has synced successfully") : t("No line uses this account yet");
  return html`<div class="statl wait" data-role="state"><i></i><span>${text}</span></div>`;
}

function AuthNote({ status, account, service }) {
  const stopped = (status.attention || [])
    .filter((a) => a.kind === "auth" && a.account === account.name)
    .map((a) => (status.rules || []).find((r) => r.name === a.rule))
    .filter(Boolean);
  return html`<div class="note bad" data-role="auth">
    <p><b>${t("The authorisation has expired.")}</b> ${service === "google" ? t("Google refused calsync's access.") : t("The server refused calsync's sign-in.")}
      ${stopped.length ? t("These lines are stopped; the others carry on:") : null}</p>
    ${stopped.length ? html`<div class="routes">${stopped.map((r) => html`<span class="route"><${End} status=${status} spec=${r.source} fallback="google"/><span class="arrow" aria-hidden="true">→</span><span class="vh">${t("to")}</span><${End} status=${status} spec=${r.target}/></span>`)}</div>` : null}
    <p class="why-h">${t("Usually one of these:")}</p>
    <ul class="why">${authReasons(service).map((r) => html`<li>${r}</li>`)}</ul>
    <p class="why-h">${t("Once it is connected again, the changes held back arrive in one go.")}</p>
  </div>`;
}

function Calendars({ status, account, service }) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null); // {text, command, bad}
  const { refresh } = useStatus();
  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);
  const pills = mapModel(status).pills;
  const calendars = account.calendars || [];

  const reread = async () => {
    if (busy) return;
    setBusy(true);
    setResult(null);
    try {
      const r = await op("calendars.refresh", { account: account.name });
      const entry = (r.data && r.data.accounts && r.data.accounts[account.name]) || {};
      if (!alive.current) return;
      if (r.needs_human) setResult({ text: t("Could not read the calendar list: {account}'s authorisation has expired", { account: accountLabel(account.name) }), command: r.command, bad: true });
      else if (entry.refreshed) setResult({ text: entry.calendars === 1 ? t("Refreshed · 1 calendar") : t("Refreshed · {n} calendars", { n: entry.calendars }) });
      else setResult({ text: t("Could not read the calendar list: calsync can't reach {account} ({kind})", { account: accountLabel(account.name), kind: entry.error_kind || "error" }), bad: true });
      refresh();
    } catch (e) {
      if (alive.current && e.kind !== "unpaired") setResult({ text: failureText(e), bad: true });
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  return html`<div class="fs" data-key="calendars"><span class="lb">${t("Calendars")}</span>
    ${calendars.length
      ? html`<div class="rows">${calendars.map((c) => {
          const key = `${account.name}/${c.id}`;
          const n = (c.rules || []).length;
          const pill = pills.get(key);
          const side = service === "google" ? "source" : "target";
          const href = pill
            ? `#/calendar/${encodeURIComponent(account.name)}/${encodeURIComponent(c.id)}`
            : `#/new?${side}=${encodeURIComponent(account.name)}&calendar=${encodeURIComponent(c.id)}`;
          return html`<a class="row" href=${href} data-cal=${c.id}>
            ${service === "google" ? html`<i class=${`dot ${n ? colourOf(key) : "none"}`} aria-hidden="true"></i>` : null}
            <span class="grow"><${CalendarName} service=${service} account=${account.name} name=${c.name}/></span>
            <span class="r">${n ? (n === 1 ? t("1 line") : t("{n} lines", { n })) : t("not synced")}</span><${Icon} name="right"/>
          </a>`;
        })}</div>`
      : html`<p class="hp">${t("calsync has not read this account's calendars yet. Once it is connected, refresh the list.")}</p>`}
    <div class="acts">
      <button class=${busy ? "btn busy" : "btn"} type="button" aria-busy=${busy ? "true" : "false"} aria-disabled=${busy ? "true" : "false"} onClick=${reread}>
        <${Icon} name="sync"/>${busy ? t("Refreshing…") : t("Refresh the list")}
      </button>
    </div>
    <div data-role="refreshed">
      <p class=${result && result.bad ? "inote bad" : "inote"} role="status">${result ? result.text : ""}</p>
      ${result && result.command ? html`<${Command} command=${result.command}/>` : null}
    </div>
  </div>`;
}

function Reconnect({ status, account, service }) {
  const failed = account.auth === "failed";
  const attention = (status.attention || []).find((a) => a.kind === "auth" && a.account === account.name && a.command);
  const command = attention ? attention.command : `calsync auth ${shellWord(account.name)}`;
  return html`<div class="fs" data-key="connect"><span class="lb">${failed ? t("Reconnect") : t("Connect")}</span>
    <p class="hp">${service === "google"
      ? t("For safety, signing in to Google happens only on the computer calsync runs on. In a terminal there, run:")
      : t("For safety, the app-specific password is typed only on the computer calsync runs on. In a terminal there, run:")}</p>
    <${Command} command=${command}/>
    <p class="hp after">${t("Once it is done this page updates by itself; no need to reload.")}</p>
  </div>`;
}

function RemovePanel({ status, name, onCancel, onRemoved }) {
  const [state, setState] = useState({ checking: true }); // {checking} | {allowed} | {refused: [rules]} | {error}
  const [busy, setBusy] = useState(false);
  const panel = useRef(null);
  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);

  // The op decides (a dry run writes nothing); a refusal names the lines that use the account.
  const check = async () => {
    try {
      await op("account.remove", { name }, { dryRun: true });
      if (alive.current) setState({ allowed: true });
    } catch (e) {
      if (!alive.current || e.kind === "unpaired") return;
      if (e.kind !== "config") {
        setState({ error: failureText(e) });
        return;
      }
      let rules = linesOf(status, name);
      try {
        const config = await get("/api/config");
        const using = (config.rules || []).filter((r) => r.source.account === name || r.target.account === name);
        rules = using.map((r) => (status.rules || []).find((s) => s.name === r.name) || r);
      } catch (err) {
        // the status's lines, then
      }
      if (alive.current) setState(rules.length ? { refused: rules } : { error: t("calsync refused to disconnect this account.") });
    }
  };
  useEffect(() => {
    check();
  }, []);
  useEffect(() => {
    const cancel = panel.current && panel.current.querySelector(".del-acts button");
    if (cancel) cancel.focus({ preventScroll: true });
  }, []);

  const confirm = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await op("account.remove", { name }); // never delete_credential: that is the terminal's
      if (alive.current) onRemoved();
    } catch (e) {
      if (!alive.current || e.kind === "unpaired") return;
      if (e.kind === "config") {
        setState({ checking: true });
        check();
      } else setState({ error: failureText(e) });
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const onKeyDown = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation(); // closes this panel, not the inspector
      onCancel();
    }
  };
  const command = `calsync account remove ${shellWord(name)} --delete-credential`;
  let body;
  if (state.checking) body = html`<p class="hp flush" aria-busy="true">${t("Checking…")}</p>`;
  else if (state.refused) {
    const n = state.refused.length;
    body = html`<div class="note bad" data-role="remove-refused">
      <p>${n === 1 ? t("1 line still uses this account. Delete it first, then the account can be disconnected:") : t("{n} lines still use this account. Delete them first, then the account can be disconnected:", { n })}</p>
      <div class="rows">${state.refused.map((r) => html`<${LineRow} status=${status} rule=${r}/>`)}</div>
    </div>`;
  } else if (state.error) body = html`<p class="errt" role="alert">${state.error}</p>`;
  else {
    body = html`<p class="del-p">${t("calsync stops using this account: it will be deleted from config.toml.")}</p>
      <p class="hp flush">${t("The credential saved in the keychain stays. To delete it as well, run this in a terminal on this computer:")}</p>
      <${Command} command=${command}/>`;
  }
  return html`<div class="del" data-role="remove" role="group" aria-labelledby="rm-title" onKeyDown=${onKeyDown} ref=${panel}>
    <h3 id="rm-title">${t("Disconnect this account?")}</h3>
    ${body}
    <div class="del-acts">
      <button class="btn quiet" type="button" onClick=${onCancel}>${t("Cancel")}</button>
      ${state.allowed ? html`<button class=${busy ? "btn danger-solid busy" : "btn danger-solid"} type="button"
          aria-disabled=${busy ? "true" : "false"} onClick=${confirm}>${t("Disconnect")}</button>` : null}
    </div>
  </div>`;
}

export function AccountForm({ route, onClose }) {
  const { status, refresh } = useStatus();
  const name = route.account;
  const [removing, setRemoving] = useState(false);
  const removed = useRef(false);
  const lastView = useRef(null);
  const removeButton = useRef(null);
  // Removed here: the panel slides out with the account as it was (no "gone" flash).
  if (removed.current && lastView.current) return lastView.current;

  const account = accountOf(status, name);
  if (!status || !account) {
    return html`<${Head} title=${t("Account")} sub=${accountLabel(name)} onClose=${onClose}/>
      <div class="ib">${status
        ? html`<div class="note">${t("calsync knows no such account. It may have been removed in a terminal or in config.toml.")}</div>`
        : html`<p class="hp">${t("Reading the sync state…")}</p>`}</div>`;
  }
  const service = serviceOf(account);
  const sub = html`<${Service} service=${service} small=${true}/><span>${accountLabel(account)} · ${kindOf(account)}</span>`;
  const cancel = () => {
    setRemoving(false);
    window.requestAnimationFrame(() => removeButton.current && removeButton.current.focus());
  };
  const onRemoved = () => {
    removed.current = true;
    refresh();
    onClose();
  };
  lastView.current = html`<${Head} title=${t("Account")} sub=${sub} onClose=${onClose}/>
    <div class="ib">
      <${StateLine} status=${status} account=${account}/>
      ${account.auth === "failed" ? html`<${AuthNote} status=${status} account=${account} service=${service}/>` : null}
      <${Calendars} status=${status} account=${account} service=${service}/>
      <${Reconnect} status=${status} account=${account} service=${service}/>
    </div>
    <div class=${removing ? "ifoot open" : "ifoot"}>
      ${removing
        ? html`<${RemovePanel} status=${status} name=${name} onCancel=${cancel} onRemoved=${onRemoved}/>`
        : html`<button class="btn danger" type="button" ref=${removeButton} onClick=${() => setRemoving(true)}>${t("Disconnect this account")}</button>`}
    </div>`;
  return lastView.current;
}

// ----- the connect wizard ------------------------------------------------------------------------

const STEPS = ["name", "details", "auth", "calendars"];

function nameProblem(name, taken) {
  if (!name) return t("Give the account a name");
  if (!NAME.test(name)) return t("Letters, digits, - and _ only, starting with a letter or digit, at most 32 characters");
  if (taken.has(name)) return t("There is already an account called {name}", { name });
  return "";
}

function Step({ id, stage, title, summary, children }) {
  const at = STEPS.indexOf(stage);
  const mine = STEPS.indexOf(id);
  const state = mine < at ? "done" : mine === at ? "current" : "todo";
  return html`<li class=${`step ${state}`} data-step=${id} aria-current=${state === "current" ? "step" : null}>
    <div class="st-h">
      <span class="n" aria-hidden="true">${state === "done" ? html`<${Icon} name="check"/>` : mine + 1}</span>
      <span class="st-t">${title}</span>
      ${state === "done" && summary ? html`<span class="sum">${summary}</span>` : null}
    </div>
    ${state === "current" ? html`<div class="st-b">${children}</div>` : null}
  </li>`;
}

export function ConnectForm({ route, onClose, onReplace, active = true }) {
  const { status, refresh } = useStatus();
  const google = route.service === "google";
  const service = google ? "google" : "icloud";
  const [stage, setStage] = useState("name");
  const [config, setConfig] = useState(null); // GET /api/config
  const [name, setName] = useState(null); // null until a free default is known
  const [nameError, setNameError] = useState("");
  const [server, setServer] = useState("icloud"); // "icloud" | "other"
  const [url, setUrl] = useState("");
  const [user, setUser] = useState("");
  const [error, setError] = useState({ field: "", text: "" }); // the details step's
  const [adding, setAdding] = useState(false);
  const [terminalAdd, setTerminalAdd] = useState(null); // the op's command: add it in the terminal
  const [command, setCommand] = useState(null); // `calsync auth <name>`, from the op
  const [checked, setChecked] = useState(null); // the last calendars.refresh: {kind} when it failed
  const [count, setCount] = useState(null); // calendars found (step 4)
  const body = useRef(null);
  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);

  const taken = new Set([...Object.keys((config && config.accounts) || {}), ...((status && status.accounts) || []).map((a) => a.name)]);
  const configured = Boolean(config && config.google && config.google.client_secret_file && config.google.client_secret_file.configured);
  const typed = (name || "").trim();

  const readConfig = async () => {
    const next = await get("/api/config");
    if (alive.current) setConfig(next);
    return next;
  };
  useEffect(() => {
    readConfig().catch(() => null);
  }, []);
  // A free name to start from: "google", "google-2"… — unless the person typed one already.
  useEffect(() => {
    if (!config) return;
    const base = google ? "google" : "icloud";
    let i = 1;
    while (taken.has(i === 1 ? base : `${base}-${i}`)) i += 1;
    setName((typedSoFar) => (typedSoFar === null ? (i === 1 ? base : `${base}-${i}`) : typedSoFar));
  }, [Boolean(config)]);

  // A step the wizard moves to takes focus where it starts (its first field or button); on
  // opening, the panel itself has it.
  const moved = useRef(false);
  useEffect(() => {
    if (!moved.current) {
      moved.current = true;
      return;
    }
    const here = body.current && body.current.querySelector('.step[aria-current="step"]');
    const target = here && (here.querySelector("[data-autofocus]") || here.querySelector("input, button:not([disabled])"));
    if (target) target.focus({ preventScroll: false });
  }, [stage]);

  // Waiting for a terminal command that changes the config: the client secret, or an account
  // the browser may not add itself.
  const waitingForConfig = active && stage === "details" && ((google && config && !configured) || Boolean(terminalAdd));
  usePoll(waitingForConfig, CONFIG_EVERY, async (on) => {
    const next = await readConfig();
    if (on() && terminalAdd && next.accounts && next.accounts[typed]) {
      setTerminalAdd(null);
      setCommand(`calsync auth ${shellWord(typed)}`);
      setStage("auth");
    }
  }, [Boolean(terminalAdd), typed]);

  // Waiting for the person to authorise: list the account's calendars until it works.
  usePoll(active && stage === "auth", REFRESH_EVERY, async (on) => {
    let r;
    try {
      r = await op("calendars.refresh", { account: typed });
    } catch (e) {
      if (on() && e.kind !== "unpaired") setChecked({ kind: e.kind });
      return;
    }
    if (!on()) return;
    const entry = (r.data && r.data.accounts && r.data.accounts[typed]) || {};
    if (r.needs_human && r.command) setCommand(r.command);
    if (entry.refreshed) {
      setCount(entry.calendars || 0);
      setStage("calendars");
      refresh();
    } else setChecked(r.needs_human ? null : { kind: entry.error_kind || "error" });
  }, [typed]);

  const next = () => {
    const problem = nameProblem(typed, taken);
    setNameError(problem);
    if (!problem) setStage("details");
  };

  const add = async () => {
    if (adding) return;
    const args = { name: typed, provider: google ? "google" : "caldav" };
    if (!google) {
      const login = user.trim();
      const address = url.trim();
      if (server === "other" && !/^https:\/\/\S+$/.test(address)) return setError({ field: "url", text: t("The server address must start with {scheme}", { scheme: SCHEME }) });
      if (!login) return setError({ field: "user", text: server === "icloud" ? t("Fill in this account's Apple ID") : t("Fill in the user name for this server") });
      if (server === "icloud" && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(login)) return setError({ field: "user", text: t("An Apple ID is an email address") });
      args.server_url = server === "icloud" ? "icloud" : address;
      args.username = login;
    }
    setError({ field: "", text: "" });
    setAdding(true);
    try {
      const r = await op("account.add", args);
      if (!alive.current) return;
      if (r.changed) {
        setTerminalAdd(null);
        setCommand(r.command || `calsync auth ${shellWord(typed)}`);
        setStage("auth");
        refresh();
      } else if (r.needs_human) setTerminalAdd(r.command);
      else setError({ field: "", text: t("calsync refused this account.") });
    } catch (e) {
      if (!alive.current || e.kind === "unpaired") return;
      if (e.kind === "config") {
        // Taken meanwhile (the terminal, another page)? Then the name is the problem.
        const fresh = await readConfig().catch(() => null);
        if (fresh && fresh.accounts && fresh.accounts[typed]) {
          setNameError(t("There is already an account called {name}", { name: typed }));
          setStage("name");
        } else setError({ field: "", text: t("calsync refused this account.") });
      } else setError({ field: "", text: failureText(e) });
    } finally {
      if (alive.current) setAdding(false);
    }
  };

  const enter = (fn) => (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      fn();
    }
  };
  const at = STEPS.indexOf(stage) + 1;
  const title = google ? t("Connect a Google account") : t("Connect an iCloud account");
  const sub = t("Setup · step {n} of {total}", { n: at, total: STEPS.length });
  const addButton = (disabled) => html`<button class=${adding ? "btn primary busy" : "btn primary"} type="button" disabled=${disabled}
      data-autofocus=${google && !disabled ? "" : null}
      aria-busy=${adding ? "true" : "false"} onClick=${add}>${adding ? t("Adding…") : t("Add the account")}</button>`;
  const back = html`<button class="btn quiet" type="button" onClick=${() => { setTerminalAdd(null); setStage("name"); }}>${t("Back")}</button>`;
  const errorText = error.text ? html`<p class="errt" role="alert">${error.text}</p>` : null;
  const terminalNote = terminalAdd ? html`<div class="note" data-role="terminal-add">
      <p>${t("The keychain already holds a credential for {name}. For safety, this account is added in a terminal:", { name: typed })}</p>
      <${Command} command=${terminalAdd}/>
      <p class="hp after" role="status">${t("Once it is added, this page goes on by itself.")}</p>
    </div>` : null;

  let details;
  if (google) {
    if (!config) details = html`<p class="hp" aria-busy="true">${t("Reading the settings…")}</p>`;
    else if (configured) {
      details = html`<p class="hp">${t("The Google app credentials are set up already. calsync signs in to this account with them.")}</p>
        ${errorText}${terminalNote}
        <div class="acts">${back}${addButton(Boolean(terminalAdd))}</div>`;
    } else {
      details = html`<div class="note">${t("calsync signs in with Google app credentials of your own: your events never pass through anybody else's server. This is needed once.")}</div>
        <ol class="howto">
          <li>${t("In the Google Cloud console, create a project and enable the Google Calendar API.")}</li>
          <li>${t("Under Credentials, create an OAuth client of type “Desktop app”, download its JSON file and put it on the computer calsync runs on.")}</li>
          <li>${t("Publish the app (“In production”): while it is in “testing”, its authorisation has to be renewed every 7 days.")}</li>
          <li>${t("In a terminal on that computer, run this; if the file is not in Downloads, change the path to where it is:")}</li>
        </ol>
        <${Command} command="calsync config set google.client_secret_file ~/Downloads/client_secret_*.json"/>
        <p class="hp after wait" data-role="waiting" role="status"><span class="wdot" aria-hidden="true"></span>${t("Once it is set, this page goes on by itself.")}</p>
        <div class="acts">${back}${addButton(true)}</div>`;
    }
  } else {
    const icloud = server === "icloud";
    details = html`<${Seg} label=${t("Server")} value=${server} options=${[["icloud", "iCloud"], ["other", t("Another CalDAV server")]]}
        onPick=${(v) => { setServer(v); setError({ field: "", text: "" }); }}/>
      <p class="hp after">${icloud ? t("calsync works out by itself whether it is the global or the China mainland iCloud.") : t("Any CalDAV server that speaks https.")}</p>
      ${icloud ? null : html`<label class="fl">${t("Server address")}
        <input class=${error.field === "url" ? "field w err" : "field w"} type="url" inputmode="url" autocomplete="url" spellcheck="false"
            placeholder=${`${SCHEME}caldav.example.com`} value=${url} aria-invalid=${error.field === "url" ? "true" : "false"}
            onInput=${(e) => setUrl(e.currentTarget.value)} onKeyDown=${enter(add)}/></label>`}
      <label class="fl">${icloud ? t("Apple ID") : t("User name")}
        <input class=${error.field === "user" ? "field w err" : "field w"} type=${icloud ? "email" : "text"} inputmode=${icloud ? "email" : "text"}
            autocomplete="username" spellcheck="false" autocapitalize="off" data-autofocus=""
            placeholder=${icloud ? "name@example.com" : ""} value=${user} aria-invalid=${error.field === "user" ? "true" : "false"}
            onInput=${(e) => setUser(e.currentTarget.value)} onKeyDown=${enter(add)}/></label>
      <p class="hp after">${t("It is not a secret: it is written to config.toml. The app-specific password comes next, in the terminal, never through the browser.")}</p>
      ${errorText}${terminalNote}
      <div class="acts">${back}${addButton(Boolean(terminalAdd))}</div>`;
  }

  const found = (accountOf(status, typed) || {}).calendars || [];
  const detailsSummary = google ? t("App credentials set up") : server === "icloud" ? `iCloud · ${user.trim()}` : `${url.trim()} · ${user.trim()}`;

  return html`<${Head} title=${title} sub=${sub} onClose=${onClose}/>
    <div class="ib" ref=${body}>
      <ol class="steps">
        <${Step} id="name" stage=${stage} title=${t("A name for the account")} summary=${typed}>
          <label class="fl">${t("Account name")}
            <input class=${nameError ? "field w err" : "field w"} type="text" spellcheck="false" autocapitalize="off" autocomplete="off"
                value=${name || ""} aria-invalid=${nameError ? "true" : "false"} aria-describedby="name-hint"
                onInput=${(e) => { setName(e.currentTarget.value); if (nameError) setNameError(""); }} onKeyDown=${enter(next)}/></label>
          <p class="hp after" id="name-hint">${t("Only used inside calsync and in its terminal commands, e.g. grandma-icloud.")}</p>
          ${nameError ? html`<p class="errt" role="alert">${nameError}</p>` : null}
          <div class="acts"><button class="btn primary" type="button" onClick=${next}>${t("Next")}</button></div>
        <//>
        <${Step} id="details" stage=${stage} title=${google ? t("Google app credentials") : t("Server and Apple ID")} summary=${detailsSummary}>
          ${details}
        <//>
        <${Step} id="auth" stage=${stage} title=${t("Authorise in the terminal")} summary=${t("Authorised")}>
          <p class="hp">${google
            ? t("On the computer calsync runs on, run this in a terminal. It opens a browser to sign in to Google and let calsync read the calendars.")
            : t("First create an app-specific password at account.apple.com → Sign-In and Security → App-Specific Passwords (not the Apple ID password). Then, on the computer calsync runs on, run this in a terminal and paste it when asked:")}</p>
          <${Command} command=${command || `calsync auth ${shellWord(typed)}`}/>
          <p class="hp after wait" data-role="waiting" role="status"><span class="wdot" aria-hidden="true"></span>${checked
            ? t("Waiting for you to finish in the terminal · the last check could not reach the account ({kind})", { kind: checked.kind })
            : t("Waiting for you to finish in the terminal · checking every 10 seconds")}</p>
        <//>
        <${Step} id="calendars" stage=${stage} title=${t("Choose the calendars")}>
          <p class="hp">${count === 0
            ? t("Connected, but this account has no calendar yet.")
            : slots(t("Found {n}:"), { n: count === 1 ? t("1 calendar") : t("{n} calendars", { n: count == null ? found.length : count }) })}</p>
          ${found.length ? html`<div class="rows">${found.map((c) => html`<div class="row"><span class="grow"><${CalendarName} service=${service} account=${typed} name=${c.name}/></span></div>`)}</div>` : null}
          <div class="acts"><button class="btn primary" type="button" disabled=${count === 0}
              onClick=${() => onReplace(`#/new?${google ? "source" : "target"}=${encodeURIComponent(typed)}`)}>${t("Choose the calendars to sync")}</button></div>
        <//>
      </ol>
    </div>`;
}
