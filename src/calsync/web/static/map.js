// The sync map: source accounts and their calendars on the left, the calsync hub in the middle,
// target accounts on the right, and one wire per line between them (SVG over the grid). From the
// concept (.impeccable/mocks/canvas/index.html): beziers from a source pill to a port on the
// hub's left side, a trace across the hub, a port on its right side, a bezier to the target pill.
//
// - The wires are measured from the rendered pills (ResizeObserver, fonts), never guessed.
// - A wire carries its line's health: ok solid, paused dashed grey, auth/failing dashed red,
//   blocked amber, retiring thin dashed. A segment shared by lines shows a state only when every
//   line on it has that state.
// - Hovering or focusing a wire or a calendar lights its lines and dims the rest; the inspector's
//   route (a line, a calendar, an account) keeps its lines lit while it is open.
// - Motion: a slow comet along an idle healthy wire every few seconds, a sync wave (a comet on
//   every running line) while a cycle runs; none under reduced motion or while calsync is out of
//   reach. Comets are Web Animations with the id "comet" (the hub's beat: "beat").
// No style strings (CSP): geometry goes through SVG attributes, the tooltip's place through a
// Preact style object.

import { html, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";
import { t } from "/static/i18n.js";
import { accountLabel, calendarLabel, serviceOf } from "/static/format.js";
import { Icon, Service } from "/static/icons.js";

/** The source calendars' colours (classes ``dot <name>``, tokens ``--dot-<name>``): eight quiet
 * hues, each at least 3:1 against the pill in both themes. */
export const PALETTE = ["sage", "teal", "clay", "blue", "violet", "rose", "olive", "steel"];

// ----- the model: what the status says, arranged as the map draws it ----------------------------

/** A rule's health as the wire's class. */
export function wireState(health) {
  switch (health) {
    case "paused":
      return "paused";
    case "auth":
    case "failing":
      return "bad";
    case "blocked":
      return "held";
    case "retiring":
      return "retiring";
    default:
      return "ok"; // ok, never_run
  }
}

/** What a line's health is called (the end of its tooltip). Literal keys for the key test. */
export function healthWord(health) {
  switch (health) {
    case "ok":
      return t("working");
    case "never_run":
      return t("not synced yet");
    case "paused":
      return t("paused");
    case "auth":
      return t("authorisation failed");
    case "failing":
      return t("failing");
    case "blocked":
      return t("deletes waiting for you");
    case "retiring":
      return t("being removed");
    default:
      return health;
  }
}

function sideOf(account, rules) {
  if (rules.some((r) => r.source.account === account.name)) return "src";
  if (rules.some((r) => r.target.account === account.name)) return "tgt";
  return account.provider === "google" ? "src" : "tgt";
}

/** The map's model from /api/status: ``sources`` and ``targets`` (accounts with their pills),
 * ``rules`` (each with its source and target pill keys), ``pills`` by key. A pill's key is
 * ``<account>/<calendar id>``. Source accounts show every cached calendar (unused ones dashed);
 * target accounts show the calendars lines write to. A line whose calendar is not in the cache
 * still gets a pill, from the line's own reference. */
export function mapModel(status) {
  const rules = status.rules || [];
  const pills = new Map();
  const groups = { src: [], tgt: [] };
  for (const account of status.accounts || []) {
    const side = sideOf(account, rules);
    const service = serviceOf(account);
    const group = { name: account.name, service, side, auth: account.auth, pills: [] };
    for (const cal of account.calendars || []) {
      const mine = (cal.rules || []).filter((name) =>
        rules.some((r) => r.name === name && (side === "src" ? r.source : r.target).account === account.name));
      if (side === "tgt" && mine.length === 0) continue;
      const pill = {
        key: `${account.name}/${cal.id}`, account: account.name, id: cal.id, name: cal.name, service, side,
        series: cal.series, rules: mine, colour: "none",
      };
      group.pills.push(pill);
      pills.set(pill.key, pill);
    }
    groups[side].push(group);
  }
  const lines = rules.map((rule) => {
    const ends = {};
    for (const [end, side] of [["source", "src"], ["target", "tgt"]]) {
      const spec = rule[end];
      let pill = [...pills.values()].find((p) => p.side === side && p.account === spec.account && p.rules.includes(rule.name));
      if (!pill) {
        // Not in the calendar cache: a pill from the line's reference (shared by lines that name it).
        const key = `${spec.account}/${spec.calendar}`;
        pill = pills.get(key);
        if (!pill) {
          let group = groups[side].find((g) => g.name === spec.account);
          if (!group) {
            // An account the status does not list: drawn anyway, its header marked, never dropped.
            group = { name: spec.account, service: side === "src" ? "google" : "caldav", side, auth: "unknown", pills: [], missing: true };
            groups[side].push(group);
          }
          // Marked missing only when the account's calendar list is known (non-empty) and lacks
          // it: an empty list means unknown (listing failed or has not run), not gone.
          const listed = (status.accounts || []).find((a) => a.name === spec.account);
          const known = Boolean(listed && (listed.calendars || []).length);
          pill = {
            key, account: spec.account, id: spec.calendar, name: spec.calendar_name || spec.calendar, service: group.service,
            side, series: null, rules: [], colour: "none", missing: known,
          };
          pills.set(key, pill);
          group.pills.push(pill);
        }
        pill.rules.push(rule.name);
      }
      ends[end] = pill;
    }
    return { name: rule.name, health: rule.health, state: wireState(rule.health), from: ends.source, to: ends.target };
  });
  for (const pill of pills.values()) {
    if (pill.side === "src" && pill.rules.length) pill.colour = colourOf(pill.key);
  }
  return { sources: groups.src, targets: groups.tgt, rules: lines, pills };
}

/** A source calendar's colour: a pure function of its key (``<account>/<calendar id>``), the
 * same on every device and whatever the other lines are. FNV-1a over the key's code points, then
 * murmur3's finaliser (FNV's low bits mix poorly for a small modulus), mod the palette. Two
 * calendars may share a colour: the name is always shown beside the dot. */
export function colourOf(key) {
  let h = 0x811c9dc5;
  for (const ch of key) {
    h = Math.imul(h ^ ch.codePointAt(0), 0x01000193) >>> 0;
  }
  h = Math.imul(h ^ (h >>> 16), 0x85ebca6b) >>> 0;
  h ^= h >>> 13;
  h = Math.imul(h, 0xc2b2ae35) >>> 0;
  h = (h ^ (h >>> 16)) >>> 0; // unsigned: "^" alone gives a signed 32-bit number
  return PALETTE[h % PALETTE.length];
}

/** The colour of each synced source calendar (by pill key), as the map paints its dot: the
 * calendar view colours its events the same way. Pure: reads ``status`` only. */
export function sourceColours(status) {
  const colours = new Map();
  for (const pill of mapModel(status).pills.values()) {
    if (pill.side === "src") colours.set(pill.key, pill.colour);
  }
  return colours;
}

/** "Google · personal · 家庭日程 → iCloud · family-icloud · 家庭 · 正常". */
export function lineLabel(line) {
  const name = (p) => calendarLabel({ service: p.service, account: p.account, name: p.name }, { plain: true });
  return `${name(line.from)} → ${name(line.to)} · ${healthWord(line.health)}`;
}

function calendarHref(pill) {
  return `#/calendar/${encodeURIComponent(pill.account)}/${encodeURIComponent(pill.id)}`;
}

/** The state shared by every line in ``lines`` (a segment's), else "ok". */
function sharedState(lines) {
  const states = new Set(lines.map((l) => l.state));
  return states.size === 1 ? [...states][0] : "ok";
}

// ----- geometry --------------------------------------------------------------------------------

function curve(p, q, k = 0.5) {
  const dx = Math.max(24, (q.x - p.x) * k);
  return `C ${p.x + dx} ${p.y} ${q.x - dx} ${q.y} ${q.x} ${q.y}`;
}
const r1 = (n) => Math.round(n * 10) / 10;

/** Where everything is, relative to the canvas: the hub, a port per wired pill on each side of
 * it, and each wired pill's anchor (its edge facing the hub). */
function measure(canvas, model) {
  const cb = canvas.getBoundingClientRect();
  const hubEl = canvas.querySelector(".hub");
  if (!hubEl || cb.width === 0) return null;
  const hb = hubEl.getBoundingClientRect();
  const H = { l: r1(hb.left - cb.left), r: r1(hb.right - cb.left), t: r1(hb.top - cb.top), b: r1(hb.bottom - cb.top) };
  const rad = parseFloat(getComputedStyle(hubEl).borderTopLeftRadius) || 0;
  const pillEls = new Map([...canvas.querySelectorAll(".cal[data-cal]")].map((el) => [el.dataset.cal, el]));
  const wired = (side) => [...model.pills.values()].filter((p) => p.side === side && p.rules.length && pillEls.has(p.key));
  const portY = (i, n) => {
    const inset = Math.min(rad + 4, (H.b - H.t) * 0.3);
    const span = H.b - H.t - inset * 2;
    return r1(H.t + inset + (n === 1 ? span / 2 : (span * i) / (n - 1)));
  };
  const anchor = (key, side) => {
    const b = pillEls.get(key).getBoundingClientRect();
    return { x: r1((side === "src" ? b.right : b.left) - cb.left), y: r1(b.top - cb.top + b.height / 2) };
  };
  const ports = {};
  const anchors = {};
  for (const side of ["src", "tgt"]) {
    const list = wired(side);
    for (const p of list) anchors[p.key] = anchor(p.key, side);
    // Ports top to bottom in the order of their pills, so no two wires cross outside the hub.
    list.sort((a, b) => anchors[a.key].y - anchors[b.key].y);
    list.forEach((p, i) => {
      ports[p.key] = { x: side === "src" ? H.l : H.r, y: portY(i, list.length) };
    });
  }
  canvas.dataset.measured = String(Number(canvas.dataset.measured || 0) + 1); // for the tests
  return { w: r1(cb.width), h: r1(cb.height), H, rad, ports, anchors };
}

function routeOf(geo, line) {
  const A = geo.anchors[line.from.key], P = geo.ports[line.from.key];
  const Q = geo.ports[line.to.key], B = geo.anchors[line.to.key];
  if (!A || !P || !Q || !B) return null;
  return `M ${A.x} ${A.y} ${curve(A, P)} ${curve(P, Q, 0.55)} ${curve(Q, B)}`;
}

/** A line's stretches outside the hub (pill to port, port to pill), where it is hit and hovered;
 * across the hub only a thin trace is (the tile's corners and caption open the settings). */
function outerOf(geo, line) {
  const A = geo.anchors[line.from.key], P = geo.ports[line.from.key];
  const Q = geo.ports[line.to.key], B = geo.anchors[line.to.key];
  if (!A || !P || !Q || !B) return null;
  return `M ${A.x} ${A.y} ${curve(A, P)} M ${Q.x} ${Q.y} ${curve(Q, B)}`;
}

/** The geometry, measured when the model changes (a poll) and when anything on the map resizes
 * (the window, the inspector's shift, fonts): never on hover or a tooltip move. One observer for
 * the map's life. */
function useGeometry(canvasRef, model) {
  const [geo, setGeo] = useState(null);
  const last = useRef("");
  const current = useRef(model);
  current.current = model;
  const update = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !current.current) return;
    const next = measure(canvas, current.current);
    const key = JSON.stringify(next);
    if (key !== last.current) {
      last.current = key;
      setGeo(next);
    }
  }, []);

  useLayoutEffect(update, [model]);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => update());
    observer.observe(canvas);
    for (const el of canvas.querySelectorAll(".col, .hub")) observer.observe(el);
    canvas.dataset.observers = String(Number(canvas.dataset.observers || 0) + 1); // for the tests
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(update);
    return () => observer.disconnect();
  }, []);
  return geo;
}

// ----- motion ----------------------------------------------------------------------------------

function reducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** A comet: a short accent stroke travelling along ``d`` (a Web Animation of its dash offset).
 * ``kind``: "wave" (a sync wave's) or "born" (a new line's first). */
function comet(layer, d, duration, delay = 0, kind = "") {
  if (!layer || reducedMotion() || typeof Element.prototype.animate !== "function") return;
  const path = document.createElementNS(layer.namespaceURI, "path"); // the SVG namespace
  path.setAttribute("d", d);
  path.setAttribute("class", kind ? `comet ${kind}` : "comet");
  layer.appendChild(path);
  const length = path.getTotalLength();
  const seg = 34;
  path.setAttribute("stroke-dasharray", `${seg} ${length + seg}`);
  path.setAttribute("stroke-dashoffset", String(seg));
  path.setAttribute("opacity", "0");
  const animation = path.animate(
    [
      { strokeDashoffset: `${seg}px`, opacity: 0 },
      { opacity: 0.9, offset: 0.2 },
      { opacity: 0.9, offset: 0.8 },
      { strokeDashoffset: `${-length}px`, opacity: 0 },
    ],
    { duration, delay, easing: "cubic-bezier(.65,0,.35,1)", fill: "both" },
  );
  animation.id = "comet";
  const done = () => path.remove();
  animation.onfinish = done;
  animation.oncancel = done;
}

function beat(hub) {
  if (!hub || reducedMotion() || typeof hub.animate !== "function") return;
  const animation = hub.animate(
    [{ transform: "scale(1)" }, { transform: "scale(1.03)", offset: 0.3 }, { transform: "scale(1)" }],
    { duration: 1400, easing: "cubic-bezier(.16,1,.3,1)" },
  );
  animation.id = "beat";
}

const IDLE_EVERY = 4200;
const WAVE_EVERY = 2600;
const flows = (line) => line.state === "ok";

/** The comets: an idle one every few seconds, a wave while ``waving`` (and once when the map is
 * first drawn). Nothing moves while ``still`` (calsync out of reach) or under reduced motion. */
function useMotion(layerRef, hubRef, geo, model, waving, still) {
  const now = useRef({ geo, model, still });
  now.current = { geo, model, still };

  const wave = useCallback(() => {
    const { geo: g, model: m, still: s } = now.current;
    const layer = layerRef.current;
    if (!g || !m || s || document.hidden || !layer || reducedMotion()) return;
    m.rules.filter(flows).forEach((line, i) => {
      const d = routeOf(g, line);
      if (d) comet(layer, d, 2200, i * 160, "wave");
    });
    beat(hubRef.current);
    layer.dataset.waves = String(Number(layer.dataset.waves || 0) + 1); // for the tests
  }, []);

  const drawn = Boolean(geo);
  useEffect(() => {
    if (!drawn) return undefined;
    const first = window.setTimeout(wave, 500);
    const idle = window.setInterval(() => {
      const { geo: g, model: m, still: s } = now.current;
      if (!g || !m || s || document.hidden) return;
      const ok = m.rules.filter(flows);
      if (!ok.length) return;
      const d = routeOf(g, ok[Math.floor(Math.random() * ok.length)]);
      if (d) comet(layerRef.current, d, 3200);
    }, IDLE_EVERY);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(idle);
    };
  }, [drawn]);

  useEffect(() => {
    if (!waving || !drawn) return undefined;
    wave();
    const again = window.setInterval(wave, WAVE_EVERY);
    return () => window.clearInterval(again);
  }, [waving, drawn]);
}

/** One comet along a line just created (``born``: {rule, nonce}), once its wire is drawn. */
function useBorn(layerRef, geo, model, born) {
  const done = useRef(0);
  useEffect(() => {
    if (!born || done.current === born.nonce || !geo || !model) return;
    const line = model.rules.find((l) => l.name === born.rule);
    const d = line ? routeOf(geo, line) : null;
    if (!d) return; // not drawn yet: the next measure brings it
    done.current = born.nonce;
    const layer = layerRef.current;
    if (!layer || reducedMotion()) return;
    comet(layer, d, 2400, 350, "born");
    layer.dataset.born = born.rule; // for the tests
  }, [born, geo, model]);
}

// ----- focus -------------------------------------------------------------------------------------

/** The lines the open inspector is about (lit while it is open), or null. */
function selectedLines(route, model) {
  if (!route || !model) return null;
  if (route.name === "rule") return model.rules.filter((l) => l.name === route.rule).map((l) => l.name);
  if (route.name === "calendar") {
    return model.rules
      .filter((l) => [l.from, l.to].some((p) => p.account === route.account && p.id === route.calendar))
      .map((l) => l.name);
  }
  if (route.name === "account") {
    return model.rules.filter((l) => l.from.account === route.account || l.to.account === route.account).map((l) => l.name);
  }
  return null;
}

function linesOfPill(model, key) {
  const pill = model.pills.get(key);
  return pill ? pill.rules : [];
}

// ----- the parts -----------------------------------------------------------------------------------

function Pill({ pill, focus, selected }) {
  const idle = pill.side === "src" && pill.rules.length === 0;
  const label = calendarLabel({ service: pill.service, account: pill.account, name: pill.name }, { plain: true });
  let meta;
  let metaText;
  if (pill.side === "src") {
    meta = idle ? t("not synced") : pill.series == null ? "" : String(pill.series);
    metaText = idle ? t("not synced") : pill.series == null ? "" : t("{n} series", { n: pill.series });
  } else {
    const sources = new Set(pill.rules).size;
    meta = sources === 1 ? t("1 source") : t("{n} sources", { n: sources });
    metaText = meta;
  }
  if (pill.missing) {
    meta = t("not listed");
    metaText = t("not in the account's calendar list");
  }
  const lit = focus ? pill.rules.some((r) => focus.includes(r)) : null;
  const cls = ["cal", idle ? "idle" : "", pill.missing ? "missing" : "", lit === false ? "dim" : "", selected ? "sel" : ""].filter(Boolean).join(" ");
  return html`<a class=${cls} href=${calendarHref(pill)} data-cal=${pill.key} title=${label}
      aria-label=${metaText ? `${label} · ${metaText}` : label}>
    <i class=${`dot ${pill.colour}`} aria-hidden="true"></i><span class="nm">${pill.name}</span>${meta ? html`<em class="num" aria-hidden="true">${meta}</em>` : null}
  </a>`;
}

function AccountGroup({ group, focus, route }) {
  let sub;
  if (group.missing) sub = html`<small class="bad">${t("not in the account list")}</small>`;
  else if (group.auth === "failed") sub = html`<small class="bad">${t("authorisation expired")}</small>`;
  else {
    const n = group.pills.filter((p) => p.rules.length).length;
    let text;
    if (group.side === "src") text = n === 1 ? t("1 calendar syncing") : t("{n} calendars syncing", { n });
    else text = n === 1 ? t("1 calendar receiving") : t("{n} calendars receiving", { n });
    sub = html`<small>${text}</small>`;
  }
  const selected = (p) => route && route.name === "calendar" && route.account === p.account && route.calendar === p.id;
  return html`<div class="acct" data-acct=${group.name}>
    <a class="acct-h" href=${`#/account/${encodeURIComponent(group.name)}`}>
      <${Service} service=${group.service}/>
      <span><b class="an">${accountLabel(group.name)}</b>${sub}</span>
    </a>
    ${group.pills.map((pill) => html`<${Pill} key=${pill.key} pill=${pill} focus=${focus} selected=${selected(pill)}/>`)}
  </div>`;
}

function Wires({ geo, model, focus, layerRef, onHit }) {
  if (!geo) return html`<svg class="wires" aria-hidden="true"><g class="comets" ref=${layerRef}></g></svg>`;
  const { H, rad, ports, anchors } = geo;
  const on = focus ? new Set(focus) : null;
  const mark = (names) => (on === null ? "" : names.some((n) => on.has(n)) ? " hot" : " dim");
  const segments = [];
  for (const side of ["src", "tgt"]) {
    for (const pill of model.pills.values()) {
      if (pill.side !== side || !ports[pill.key]) continue;
      const lines = model.rules.filter((l) => (side === "src" ? l.from : l.to).key === pill.key);
      const A = anchors[pill.key], P = ports[pill.key];
      const d = side === "src" ? `M ${A.x} ${A.y} ${curve(A, P)}` : `M ${P.x} ${P.y} ${curve(P, A)}`;
      segments.push({ seg: `${side === "src" ? "s" : "t"}:${pill.key}`, d, state: sharedState(lines), names: lines.map((l) => l.name), A, P });
    }
  }
  const lines = model.rules
    .map((line) => ({ line, P: ports[line.from.key], Q: ports[line.to.key], d: outerOf(geo, line) }))
    .filter((x) => x.P && x.Q && x.d);
  const open = (line) => () => {
    window.location.hash = `#/rule/${encodeURIComponent(line.name)}`;
  };
  return html`<svg class="wires" role="group" aria-label=${t("Lines")}>
    <defs><clipPath id="hubclip"><rect x=${H.l} y=${H.t} width=${r1(H.r - H.l)} height=${r1(H.b - H.t)} rx=${rad}/></clipPath></defs>
    ${segments.map((s) => html`<path key=${s.seg} class=${`w ${s.state}${mark(s.names)}`} data-seg=${s.seg} d=${s.d}/>`)}
    <g clip-path="url(#hubclip)">
      ${lines.map(({ line, P, Q }) => html`<path key=${line.name} class=${`t ${line.state}${mark([line.name])}`}
          data-seg=${`r:${line.name}`} data-rule=${line.name} d=${`M ${P.x} ${P.y} ${curve(P, Q, 0.55)}`}/>`)}
    </g>
    ${segments.map((s) => html`<circle key=${`e${s.seg}`} class="end" cx=${s.A.x} cy=${s.A.y} r="3"/>`)}
    ${segments.map((s) => html`<circle key=${`p${s.seg}`} class="port" data-port=${s.seg} cx=${s.P.x} cy=${s.P.y} r="3.2"/>`)}
    <g class="comets" ref=${layerRef}></g>
    ${lines.map(({ line, d }) => {
      const label = lineLabel(line);
      const go = open(line);
      // Lit on keyboard focus only: focus coming back after a click must not keep the map dimmed.
      const onFocus = (e) => {
        if (e.target.matches(":focus-visible")) onHit(line.name);
      };
      return html`<path key=${`h${line.name}`} class="hit" data-rule=${line.name} data-from=${line.from.key}
          data-to=${line.to.key} d=${d} tabindex="0" role="link" aria-label=${label}
          onClick=${go} onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); go(); } }}
          onFocus=${onFocus} onBlur=${() => onHit(null)}><title>${label}</title></path>`;
    })}
    ${lines.map(({ line, P, Q }) => html`<path key=${`i${line.name}`} class="hit in" data-rule=${line.name}
        d=${`M ${P.x} ${P.y} ${curve(P, Q, 0.55)}`} aria-hidden="true" onClick=${open(line)}><title>${lineLabel(line)}</title></path>`)}
  </svg>`;
}

// ----- the map -------------------------------------------------------------------------------------

/** The sync map, from ``status`` (null until read). ``route``: the open inspector's (its lines
 * stay lit). ``waving``: a cycle runs (ours or the daemon's). ``stale``: the status could not be
 * refreshed (calsync out of reach): nothing flows. */
export function SyncMap({ status, route, waving, stale, born }) {
  const canvasRef = useRef(null);
  const layerRef = useRef(null);
  const hubRef = useRef(null);
  const model = useMemo(() => (status ? mapModel(status) : null), [status]);
  const geo = useGeometry(canvasRef, model);
  const [hover, setHoverState] = useState(null); // the lines under the pointer or keyboard focus
  const setHover = (names) => setHoverState((current) => (String(current) === String(names) ? current : names));
  const tipRef = useRef(null); // the tooltip's setter
  const setTip = (tip) => {
    if (tipRef.current) tipRef.current(tip);
  };
  useMotion(layerRef, hubRef, geo, model, waving, stale);
  useBorn(layerRef, geo, model, born);

  const selected = selectedLines(route, model);
  const focus = hover || selected;

  const onPointerMove = (e) => {
    if (!model) return;
    const hit = e.target.closest && e.target.closest(".hit");
    if (hit) {
      const line = model.rules.find((l) => l.name === hit.dataset.rule);
      const cb = canvasRef.current.getBoundingClientRect();
      const half = Math.min(cb.width / 2, 150);
      const x = Math.min(Math.max(e.clientX - cb.left, half), cb.width - half);
      setTip({ text: line ? lineLabel(line) : "", x, y: e.clientY - cb.top });
      setHover([hit.dataset.rule]);
      return;
    }
    setTip(null);
    const cal = e.target.closest && e.target.closest(".cal[data-cal]");
    const names = cal ? linesOfPill(model, cal.dataset.cal) : [];
    setHover(names.length ? names : null);
  };
  const onLeave = () => {
    setTip(null);
    setHover(null);
  };
  const onFocusIn = (e) => {
    const cal = e.target.closest && e.target.closest(".cal[data-cal]");
    // Lit on keyboard focus only: focus returned to a pill when its inspector closes after a
    // click must not keep the map dimmed.
    if (cal && model && cal.matches(":focus-visible")) {
      const names = linesOfPill(model, cal.dataset.cal);
      setHover(names.length ? names : null);
    }
  };
  const onHit = (name) => {
    setTip(null);
    setHover(name ? [name] : null);
  };

  const n = status ? status.rules.length : null;
  return html`<section class=${stale ? "canvas stale" : "canvas"} ref=${canvasRef} aria-label=${t("Sync map")}
      onpointermove=${onPointerMove} onpointerleave=${onLeave} onfocusin=${onFocusIn} onfocusout=${onLeave}>
    <${Wires} geo=${geo} model=${model} focus=${focus} layerRef=${layerRef} onHit=${onHit}/>
    <div class="col src">
      ${model ? model.sources.map((g) => html`<${AccountGroup} key=${g.name} group=${g} focus=${focus} route=${route}/>`) : null}
      <a class="add-acct" href="#/connect/google"><${Icon} name="plus"/>${t("Connect a Google account")}</a>
    </div>
    <a class="hubwrap" href="#/settings" aria-label=${t("calsync hub: open the settings")}>
      <span class="hub" ref=${hubRef}></span>
      <span class="hub-cap"><b>calsync</b>${n === null ? null : html`<small class="num">${n === 1 ? t("1 line") : t("{n} lines", { n })}</small>`}</span>
    </a>
    <div class="col tgt">
      ${model ? model.targets.map((g) => html`<${AccountGroup} key=${g.name} group=${g} focus=${focus} route=${route}/>`) : null}
      <a class="add-acct" href="#/connect/icloud"><${Icon} name="plus"/>${t("Connect an iCloud account")}</a>
    </div>
    <a class="btn new-line" href="#/new"><${Icon} name="plus"/>${t("New line")}</a>
    <${Tip} setter=${tipRef}/>
  </section>`;
}

/** The wire's tooltip ({text, x, y} or null), set through ``setter`` so that following the
 * pointer re-renders this alone. */
function Tip({ setter }) {
  const [tip, set] = useState(null);
  setter.current = (next) =>
    set((now) => (now === next || (now && next && now.text === next.text && now.x === next.x && now.y === next.y) ? now : next));
  return html`<div class=${tip ? "tip on" : "tip"} aria-hidden="true" style=${tip ? { left: tip.x, top: tip.y } : {}}>${tip ? tip.text : ""}</div>`;
}
