// The inspector: a right panel beside the map on the desktop (the page shifts left), a bottom
// sheet on the phone. It opens for every inspector route, takes focus, and closes with Esc, its
// close button or the browser's Back (the router's close rule: back to the map entry it was
// opened from); closed, it is inert. Each route's body is a form of its own (forms/*.js).

import { html, useEffect, useLayoutEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { Head } from "/static/forms/parts.js";
import { RuleForm } from "/static/forms/rule.js";
import { CalendarForm } from "/static/forms/calendar.js";
import { NewLineForm } from "/static/forms/newline.js";
import { AccountForm, ConnectForm } from "/static/forms/account.js";
import { SettingsForm } from "/static/forms/settings.js";
import { EventForm } from "/static/forms/event.js";

/** The title and subtitle of a route whose inspector comes with a later task. */
function placeholderHead(route) {
  switch (route.name) {
    case "blocked":
      return [t("Blocked deletes"), route.rule];
    default:
      return ["", ""];
  }
}

function Placeholder({ route, onClose }) {
  const [title, sub] = placeholderHead(route);
  return html`<${Head} title=${title} sub=${sub} onClose=${onClose}/><div class="ib"></div>`;
}

const FORMS = { rule: RuleForm, calendar: CalendarForm, new: NewLineForm, account: AccountForm, connect: ConnectForm, settings: SettingsForm, event: EventForm };

/** ``open``: the route opens an inspector. ``onCreated(rule)``: a new line was added.
 * ``onReplace(hash)``: this inspector becomes another, in the same history entry. A form gets
 * ``active``: false while it slides out after a close (whatever it polls stops then). */
export function Inspector({ route, open, onClose, onCreated, onReplace }) {
  const panel = useRef(null);
  const returnTo = useRef(null);
  // Closing, the panel slides out with what it showed; then the form goes (no reads behind it).
  const shown = useRef(null);
  const [lingering, setLingering] = useState(false);
  if (open) shown.current = route;
  useEffect(() => {
    if (open) return undefined;
    setLingering(true);
    const timer = window.setTimeout(() => setLingering(false), 500);
    return () => window.clearTimeout(timer);
  }, [open]);

  // A layout effect: focus and the Esc key belong to the panel from the frame it opens in.
  useLayoutEffect(() => {
    document.body.classList.toggle("insp-open", open);
    if (open) {
      if (!returnTo.current) returnTo.current = document.activeElement;
      if (panel.current) panel.current.focus({ preventScroll: true });
      const onKey = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          onClose();
        }
      };
      document.addEventListener("keydown", onKey);
      return () => document.removeEventListener("keydown", onKey);
    }
    const back = returnTo.current;
    returnTo.current = null;
    if (back && back.isConnected && typeof back.focus === "function") back.focus({ preventScroll: true });
    return undefined;
  }, [open, route]);

  const current = open ? route : lingering ? shown.current : null;
  const Form = current ? FORMS[current.name] || Placeholder : null;
  return html`<aside class=${open ? "insp open" : "insp"} ref=${panel} tabindex="-1"
      role="dialog" aria-modal="false" aria-labelledby="insp-title" aria-hidden=${open ? "false" : "true"}
      inert=${!open} data-route=${open ? route.name : ""}>
    ${Form ? html`<${Form} key=${current.key} route=${current} onClose=${onClose} onCreated=${onCreated} onReplace=${onReplace} active=${open}/>` : html`<div class="ih"><div><h2 id="insp-title"></h2></div></div><div class="ib"></div>`}
  </aside>`;
}
