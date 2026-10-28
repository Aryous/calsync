// What the inspectors share: the panel's head, segmented choices, the quiet "已保存到 config.toml"
// indicator, a calendar named as it always is, and the words for an op's refusal.

import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { serviceOf } from "/static/format.js";
import { CalendarName, Icon } from "/static/icons.js";

/** The inspector's head: the title, a subtitle (text or nodes) and the close button. */
export function Head({ title, sub, onClose }) {
  return html`<div class="ih">
    <div>
      <h2 id="insp-title">${title}</h2>
      ${sub ? html`<p>${sub}</p>` : null}
    </div>
    <button class="btn icon quiet" type="button" aria-label=${t("Close")} onClick=${onClose}><${Icon} name="x"/></button>
  </div>`;
}

/** Segmented choice: ``options`` [[value, label]], the pressed one is ``value``. */
export function Seg({ value, options, onPick, disabled = false, label }) {
  return html`<div class="seg" role="group" aria-label=${label}>
    ${options.map(([v, text]) => html`<button type="button" aria-pressed=${v === value ? "true" : "false"}
        disabled=${disabled} onClick=${() => v !== value && onPick(v)}>${text}</button>`)}
  </div>`;
}

/** The quiet indicator after a write: [saved, flash()]. */
export function useSaved(ms = 2400) {
  const [on, setOn] = useState(false);
  const timer = useRef(0);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const flash = () => {
    setOn(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setOn(false), ms);
  };
  return [on, flash];
}

export function Saved({ on }) {
  return html`<span class=${on ? "saved on" : "saved"} role="status"><${Icon} name="check"/>${on ? t("Saved to config.toml") : ""}</span>`;
}

/** The status account named ``name`` (null when it is not listed). */
export function accountOf(status, name) {
  return ((status && status.accounts) || []).find((a) => a.name === name) || null;
}

export function serviceOfAccount(status, name, fallback = "caldav") {
  const account = accountOf(status, name);
  return account ? serviceOf(account) : fallback;
}

/** The name of the calendar ``ref`` (an id or a name, as the config has it) of ``account``,
 * from the status's calendar lists; the reference itself only when no list has it (a calendar
 * to be created, a list not read yet). */
export function calendarNameOf(status, account, ref) {
  const listed = accountOf(status, account);
  const cal = ((listed && listed.calendars) || []).find((c) => c.id === ref || c.name === ref);
  return cal ? cal.name : ref;
}

/** One end of a line (a status rule's ``source`` or ``target``, or a config rule's), named with
 * icon and account — by the calendar's name, never its id when the name is known. */
export function End({ status, spec, fallback }) {
  return html`<${CalendarName} service=${serviceOfAccount(status, spec.account, fallback)}
      account=${spec.account} name=${spec.calendar_name || calendarNameOf(status, spec.account, spec.calendar)}/>`;
}

/** The line as a chip: [icon] 家庭日程 · personal → [icon] 家庭 · family-icloud. */
export function RouteChip({ status, rule }) {
  return html`<div class="route-chip">
    <${End} status=${status} spec=${rule.source} fallback="google"/>
    <span class="arrow" aria-hidden="true">→</span><span class="vh">${t("to")}</span>
    <${End} status=${status} spec=${rule.target}/>
  </div>`;
}

/** ``template`` with each ``{slot}`` replaced by ``nodes[slot]`` (links, names, numbers inside a
 * translated sentence, whatever order the language puts them in). */
export function slots(template, nodes) {
  return template.split(/(\{\w+\})/).map((part) => {
    const m = /^\{(\w+)\}$/.exec(part);
    return m && m[1] in nodes ? nodes[m[1]] : part;
  });
}

/** Why an account's authorisation usually fails, for a person (the auth card, the account
 * inspector): Google's three, or the Apple ID's two. */
export function authReasons(service) {
  if (service === "google") {
    return [
      t("The Google account's password changed"),
      t("calsync's access was revoked in the Google account"),
      t("The Google app is still in “testing”: its authorisation has to be renewed every 7 days"),
    ];
  }
  return [t("The Apple ID password changed"), t("The app-specific password was revoked or replaced")];
}

/** ``word`` as one shell word, quoted only when it has to be (a command a person pastes). */
export function shellWord(word) {
  return /^[A-Za-z0-9._@:\/=+-]+$/.test(word) ? word : `'${String(word).replace(/'/g, "'\\''")}'`;
}

/** What went wrong with an op, for a person (never the API's English text). */
export function failureText(error) {
  if (!error) return "";
  if (error.kind === "offline") return t("Can't reach calsync. Is it running?");
  if (error.kind === "paused") return t("This line is paused");
  if (error.kind === "read_only") return t("This calsync does not run syncs (--no-sync)");
  if (error.kind === "config") return t("calsync refused the change");
  return t("Something went wrong ({kind})", { kind: error.kind });
}
