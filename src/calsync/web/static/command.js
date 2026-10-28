// A command for the terminal, with a copy button. The UI is served over plain HTTP, where
// navigator.clipboard may be missing: the button selects the command's text and asks the browser
// to copy the selection (document.execCommand), and says "已选中，请手动复制" when it refuses.

import { html, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { Icon } from "/static/icons.js";

/** Select ``el``'s text; true when the browser copied it. */
export function copyText(el) {
  const selection = window.getSelection();
  if (!el || !selection) return false;
  const range = document.createRange();
  range.selectNodeContents(el);
  selection.removeAllRanges();
  selection.addRange(range);
  try {
    return document.execCommand("copy") === true;
  } catch (e) {
    return false;
  }
}

/** ``calsync auth work`` in a code box, and 复制. */
export function Command({ command }) {
  const code = useRef(null);
  const [said, setSaid] = useState("");
  const timer = useRef(0);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = () => {
    setSaid(copyText(code.current) ? t("Copied") : t("Selected: copy it by hand"));
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setSaid(""), 6000);
  };
  return html`<div class="cmd">
    <code ref=${code}>${command}</code>
    <button class="btn icon quiet" type="button" aria-label=${t("Copy")} onClick=${copy}><${Icon} name="copy"/></button>
    <span class="copied" role="status">${said}</span>
  </div>`;
}
