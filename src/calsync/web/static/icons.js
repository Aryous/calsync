// The icon system: one SVG sprite (UI glyphs drawn on a 16-unit grid in one 1.5 stroke, and the
// Google Calendar and iCloud service marks), rendered once by <Sprite/>, used by <Icon/>,
// <Service/> and <CalendarName/>. From the concept (.impeccable/mocks/canvas/index.html).
// No style attributes anywhere (CSP): sizes and colours come from styles.css classes.

import { html } from "/static/vendor/htm-preact-standalone.mjs";
import { calendarLabel, serviceName } from "/static/format.js";

const GLYPHS = {
  sync: html`<path d="M13.2 6.2A5.4 5.4 0 0 0 3.3 4.6"/><path d="M3.1 2.4v2.4h2.4"/><path d="M2.8 9.8a5.4 5.4 0 0 0 9.9 1.6"/><path d="M12.9 13.6v-2.4h-2.4"/>`,
  gear: html`<circle cx="8" cy="8" r="2.1"/><path d="M8 1.8v1.6M8 12.6v1.6M14.2 8h-1.6M3.4 8H1.8M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1M12.4 12.4l-1.1-1.1M4.7 4.7 3.6 3.6"/>`,
  x: html`<path d="M4 4l8 8M12 4l-8 8"/>`,
  plus: html`<path d="M8 3.5v9M3.5 8h9"/>`,
  left: html`<path d="M10 3.5 5.5 8l4.5 4.5"/>`,
  right: html`<path d="M6 3.5 10.5 8 6 12.5"/>`,
  pen: html`<path d="M10.6 3.2l2.2 2.2-7.3 7.3-2.8.6.6-2.8z"/>`,
  revert: html`<path d="M5.5 4 3 6.5 5.5 9"/><path d="M3.3 6.5h6.2a3.3 3.3 0 0 1 0 6.6H7"/>`,
  alert: html`<circle cx="8" cy="8" r="5.8"/><path d="M8 5v3.4M8 10.8v.1"/>`,
  minus: html`<path d="M4 8h8"/>`,
  copy: html`<rect x="5.5" y="5.5" width="7.5" height="7.5" rx="1.6"/><path d="M3 10.5V4.6C3 3.7 3.7 3 4.6 3h5.9"/>`,
  lock: html`<rect x="3.5" y="7" width="9" height="6.5" rx="1.6"/><path d="M5.5 7V5.2a2.5 2.5 0 0 1 5 0V7"/>`,
  check: html`<path d="M3.5 8.4 6.6 11.4 12.5 5"/>`,
};

export const ICONS = Object.keys(GLYPHS);

/** The sprite: once, first in the app. */
export function Sprite() {
  return html`<svg class="sprite" aria-hidden="true" focusable="false">
    <defs>
      <linearGradient id="svc-icloud-sky" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#6CC4F5"/><stop offset="1" stop-color="#3B82E8"/>
      </linearGradient>
      ${Object.entries(GLYPHS).map(([name, body]) => html`<symbol id=${`i-${name}`} viewBox="0 0 16 16">${body}</symbol>`)}
      <symbol id="svc-google" viewBox="0 0 26 26">
        <rect x="1.5" y="2.5" width="23" height="22" rx="5" fill="#fff" stroke="#DADCE0"/>
        <path d="M1.5 7.5a5 5 0 0 1 5-5h13a5 5 0 0 1 5 5v1h-23z" fill="#4285F4"/>
        <text x="13" y="20.5" text-anchor="middle" font-size="10" font-weight="700" fill="#1A73E8" font-family="Instrument Sans, -apple-system, Helvetica">31</text>
      </symbol>
      <symbol id="svc-icloud" viewBox="0 0 26 26">
        <rect x="1.5" y="2.5" width="23" height="22" rx="5" fill="#fff" stroke="#DADCE0"/>
        <path d="M8.8 18.2h9a3.9 3.9 0 0 0 .4-7.8 5.3 5.3 0 0 0-10.2 1A3.4 3.4 0 0 0 8.8 18.2z" fill="url(#svc-icloud-sky)"/>
      </symbol>
      <symbol id="svc-caldav" viewBox="0 0 26 26">
        <rect x="1.5" y="2.5" width="23" height="22" rx="5" fill="#fff" stroke="#DADCE0"/>
        <path d="M1.5 7.5a5 5 0 0 1 5-5h13a5 5 0 0 1 5 5v1h-23z" fill="#8D96A4"/>
        <path d="M8 13.5h10M8 17.5h6" stroke="#8D96A4" stroke-width="1.6" stroke-linecap="round"/>
      </symbol>
    </defs>
  </svg>`;
}

/** A UI glyph: <Icon name="sync"/>. Decorative unless ``label`` is given. */
export function Icon({ name, label, cls = "" }) {
  const a11y = label ? { role: "img", "aria-label": label } : { "aria-hidden": "true" };
  return html`<svg class=${`i ${cls}`.trim()} focusable="false" ...${a11y}><use href=${`#i-${name}`}/></svg>`;
}

/** A service mark (``google``, ``icloud``, ``caldav``), named for assistive tech. */
export function Service({ service, small = false }) {
  const id = service === "google" || service === "icloud" ? service : "caldav";
  return html`<svg class=${small ? "svc xs" : "svc"} role="img" aria-label=${serviceName(id)} focusable="false"><use href=${`#svc-${id}`}/></svg>`;
}

/** A calendar as it is always shown: service mark, name, account. */
export function CalendarName({ service, account, name, small = true }) {
  return html`<span class="cm" title=${calendarLabel({ service, account, name }, { plain: true })}>
    <${Service} service=${service} small=${small}/><span>${calendarLabel({ service, account, name })}</span>
  </span>`;
}

/** The calsync mark: the hub tile with two crossing traces. */
export function Mark() {
  return html`<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <rect class="mark-tile" width="24" height="24" rx="7"/>
    <path class="mark-trace" d="M4.5 9.5h4.2c2.4 0 2.4 5 4.8 5h6M4.5 14.5h4.2c2.4 0 2.4-5 4.8-5h6"/>
    <circle class="mark-port" cx="12" cy="12" r="1.6"/>
  </svg>`;
}
