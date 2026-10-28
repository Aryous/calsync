// Blocked deletes (#/blocked/<rule>): a sync would delete more copies than the line's delete
// guard allows, so calsync held them back. This lists every copy it would delete (GET
// /api/blocked), says why it stopped, and offers 确认删除 — the approval names exactly the list
// shown (its fingerprint): if the list changed meanwhile, nothing is deleted and the new list is
// shown — or 先不删, which changes nothing.

import { html, useCallback, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { when } from "/static/format.js";
import { get, op, useStatus } from "/static/api.js";
import { Command } from "/static/command.js";
import { Head, RouteChip, failureText } from "/static/forms/parts.js";

function percent(ratio) {
  return `${Math.round(ratio * 1000) / 10}%`;
}

/** Why calsync stopped, from the guard's limits and what the sync wanted. */
function reasonText(entry) {
  const { max_count: maxCount, max_ratio: maxRatio } = entry.limit;
  const overCount = entry.count > maxCount;
  if (overCount) return t("One sync would delete {n} copies, more than the {max} this line allows at once.", { n: entry.count, max: maxCount });
  return t("One sync would delete {share} of this line's copies, more than the {max} it allows at once.", {
    share: percent(entry.ratio || 0),
    max: percent(maxRatio),
  });
}

/** What an approval's answer means, for a person. */
function outcomeText(data) {
  switch (data.status) {
    case "approved":
      return { text: data.deleted === 1 ? t("Deleted 1 copy.") : t("Deleted {n} copies.", { n: data.deleted || 0 }) };
    case "stale":
      return { text: t("The list changed: nothing was deleted. Look at it again."), bad: true, reload: true };
    case "synced":
      return { text: t("Nothing was held back any more: the line synced as usual.") };
    case "not_blocked":
      return { text: t("Nothing is held back now."), reload: true };
    case "pending":
      return { text: t("Confirmed: the deletes happen at the next sync of this line.") };
    default:
      return { text: t("This sync did not finish; nothing was deleted. Try again in a moment."), bad: true };
  }
}

export function BlockedForm({ route, onClose, active = true }) {
  const { status, refresh } = useStatus();
  const name = route.rule;
  const live = status ? (status.rules || []).find((r) => r.name === name) || null : null;
  const [entry, setEntry] = useState(undefined); // null: nothing held back
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null);
  const [human, setHuman] = useState(null);
  const reads = useRef(0);
  const keep = useRef(null);

  const load = useCallback(async () => {
    const mine = ++reads.current;
    try {
      const body = await get("/api/blocked", { rule: name });
      if (mine === reads.current) setEntry((body.blocked || [])[0] || null);
    } catch (e) {
      if (mine === reads.current && e.kind === "unknown_rule") setEntry(null);
    }
  }, [name]);
  const cycles = status && status.daemon ? status.daemon.cycles : null;
  useEffect(() => {
    if (active && !busy && cycles != null) load(); // again after each cycle: the list may change
  }, [load, cycles, active]);

  const approve = async () => {
    if (busy || !entry) return;
    setBusy(true);
    setNote(null);
    try {
      const result = await op("approve", { rule: name, fingerprint: entry.fingerprint, wait: true });
      if (result.needs_human) setHuman(result);
      else {
        const said = outcomeText(result.data || {});
        setNote(said);
      }
    } catch (e) {
      if (e.kind !== "unpaired") setNote({ text: failureText(e), bad: true });
    } finally {
      setBusy(false);
      await load();
      refresh();
    }
  };

  const head = html`<${Head} title=${t("Deletes held back")} sub=${t("Waiting for you to confirm")} onClose=${onClose}/>`;
  if (entry === undefined) return html`${head}<div class="ib"><p class="hp">${t("Reading the list…")}</p></div>`;
  const chip = live ? html`<${RouteChip} status=${status} rule=${live}/>` : null;
  if (entry === null) {
    return html`${head}<div class="ib">${chip}
      ${note ? html`<p class=${note.bad ? "inote bad" : "inote"} role="status" data-role="outcome">${note.text}</p>` : null}
      <div class="note" data-role="nothing">${t("Nothing is held back on this line now.")}</div></div>`;
  }
  const items = entry.items || [];
  return html`${head}
    <div class="ib">
      ${chip}
      <div class="held-sum" data-role="summary">
        <b class="num">${entry.count === 1 ? t("1 copy") : t("{n} copies", { n: entry.count })}</b>
        <span>${t("would be deleted from the target calendar · held back since {time}", { time: when(entry.since) })}</span>
      </div>
      <p class="hp" data-role="reason">${reasonText(entry)}</p>
      <p class="hp">${t("Usually this means the events were deleted or moved in Google on purpose. If not, keep them and check Google Calendar first.")}</p>
      <div class="fs" data-key="items"><span class="lb">${t("What would be deleted")}</span>
        ${items.length
          ? html`<ol class="rows held-list" data-role="items" data-fingerprint=${entry.fingerprint || ""}>
              ${items.map((i) => html`<li class="row"><span class="grow">${i.title || t("(no title)")}</span></li>`)}
            </ol>`
          : html`<p class="hp">${t("The list is not known (it was held back before this version). Run a sync to see it.")}</p>`}
      </div>
      <p class=${note && note.bad ? "inote bad" : "inote"} role="status" data-role="outcome">${note ? note.text : ""}</p>
      ${human ? html`<div class="note"><p>${t("This needs you, in a terminal on the computer calsync runs on:")}</p><${Command} command=${human.command}/></div>` : null}
    </div>
    <div class="ifoot">
      <button class="btn quiet" type="button" ref=${keep} onClick=${onClose}>${t("Not now")}</button>
      <button class=${busy ? "btn danger-solid busy" : "btn danger-solid"} type="button"
          aria-disabled=${busy || !entry.fingerprint ? "true" : "false"} onClick=${approve}>
        ${busy ? t("Deleting…") : t("Confirm deleting")}</button>
    </div>`;
}
