// The calendar inspector (#/calendar/<account>/<id>): the calendar named as always (service mark,
// name · account), its lines (to the targets it syncs to, or from the sources that sync into
// it), the way into a new line from or to it, and for a target what calsync manages there.
// The calendar is the map's pill (mapModel), so both always agree on its side and its lines.

import { html } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { serviceName } from "/static/format.js";
import { useStatus } from "/static/api.js";
import { CalendarName, Icon } from "/static/icons.js";
import { healthWord, mapModel } from "/static/map.js";
import { Head } from "/static/forms/parts.js";

const TONE = { ok: "ok", never_run: "", paused: "", auth: "bad", failing: "bad", blocked: "wait", retiring: "" };

export function newLineHref(pill) {
  const side = pill.side === "src" ? "source" : "target";
  return `#/new?${side}=${encodeURIComponent(pill.account)}&calendar=${encodeURIComponent(pill.id)}`;
}

export function CalendarForm({ route, onClose }) {
  const { status } = useStatus();
  const key = `${route.account}/${route.calendar}`;
  const model = status ? mapModel(status) : null;
  const pill = model ? model.pills.get(key) : null;
  if (!pill) {
    return html`<${Head} title=${t("Calendar")} sub=${status ? t("Not in the calendar lists") : ""} onClose=${onClose}/>
      <div class="ib">${status ? html`<div class="note">${t("calsync knows no such calendar. It may have been removed from its account.")}</div>` : html`<p class="hp">${t("Reading the sync state…")}</p>`}</div>`;
  }
  const source = pill.side === "src";
  const lines = model.rules.filter((l) => (source ? l.from : l.to).key === pill.key);
  const meta = [serviceName(pill.service)];
  if (source && pill.series != null) meta.push(t("{n} series", { n: pill.series }));
  if (!source) meta.push(t("target calendar"));
  const sub = html`<${CalendarName} service=${pill.service} account=${pill.account} name=${pill.name}/><span class="meta"> · ${meta.join(" · ")}</span>`;
  return html`<${Head} title=${t("Calendar")} sub=${sub} onClose=${onClose}/>
    <div class="ib">
      <div class="fs"><span class="lb">${source ? t("Syncs to") : t("Comes from")}</span>
        ${lines.length
          ? html`<div class="rows">${lines.map((line) => {
              const other = source ? line.to : line.from;
              return html`<a class="row" href=${`#/rule/${encodeURIComponent(line.name)}`} data-rule=${line.name}>
                <span class="grow"><${CalendarName} service=${other.service} account=${other.account} name=${other.name}/><small>${serviceName(other.service)}</small></span>
                <span class=${`r ${TONE[line.health] || ""}`}>${healthWord(line.health)}</span><${Icon} name="right"/>
              </a>`;
            })}</div>`
          : html`<p class="hp">${t("No line yet. Its events are not synced.")}</p>`}
      </div>
      <div class="fs"><a class="btn" href=${newLineHref(pill)}><${Icon} name="plus"/>${source ? t("New line from this calendar") : t("Sync into this calendar")}</a></div>
      ${source ? null : html`<div class="fs"><span class="lb">${t("Other events in this calendar")}</span>
        <p class="hp flush">${t("calsync changes only the events it wrote.")}</p></div>`}
    </div>`;
}
