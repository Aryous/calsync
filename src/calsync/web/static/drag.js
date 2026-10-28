// Dragging events in the calendar (phase C): a block in the day and week grids moves up and down
// (its time) and sideways (its day), its bottom edge changes its end; a chip in the month grid or
// the all-day row moves to another day, keeping its time. Steps of 15 minutes.
//
// Mouse and pen: a press that moves more than 4 px starts a drag; a press without moving is still
// a click (the detail opens). Touch: hold about 0.4 s first; a plain swipe still scrolls. While
// a drag runs, ``drag.ghost`` says where the event would go; the grids draw it. Releasing hands
// the move to ``onDrop(ev, start, end)`` (local Dates); Escape cancels. Keyboard users change the
// time in the detail's form instead.

import { useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";

const MOUSE_SLOP = 4; // px before a mouse press becomes a drag
const TOUCH_SLOP = 8; // px a finger may move while holding
const HOLD_MS = 400;
const STEP_MIN = 15;
const MIN = 60_000;
const DAY = 86_400_000;

function dayOf(el) {
  const cell = el && el.closest("[data-day]");
  return cell ? cell.getAttribute("data-day") : null;
}

function dateOf(ymdText) {
  const [y, m, d] = ymdText.split("-").map(Number);
  return new Date(y, m - 1, d);
}

/** Whole days from local day ``a`` to ``b`` (DST-safe: by calendar date). */
function daysBetween(a, b) {
  return Math.round((dateOf(b) - dateOf(a)) / DAY);
}

function shiftDays(d, n) {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

/** ``useDrag({hour, onDrop})``: ``bind(ev, mode)`` gives a draggable element its handlers
 * (``mode``: "time", "resize" or "day"); ``ghost`` is the drag in progress (or the move being
 * saved): {id, mode, start, end, day, saving}; ``wasDrag()`` tells a click that ended a drag. */
export function useDrag({ hour, onDrop }) {
  const [ghost, setGhost] = useState(null);
  const press = useRef(null); // {ev, mode, x, y, day, pointerId, type, timer, active}
  const dragged = useRef(false);
  const hourRef = useRef(hour);
  hourRef.current = hour;
  const dropRef = useRef(onDrop);
  dropRef.current = onDrop;

  const compute = (p, x, y) => {
    const ev = p.ev;
    const under = document.elementFromPoint(x, y);
    const day = dayOf(under) || p.day;
    const days = p.mode === "resize" ? 0 : daysBetween(p.day, day);
    if (p.mode === "day") {
      return { start: shiftDays(ev.s, days), end: shiftDays(ev.e, days), day };
    }
    const steps = Math.round(((y - p.y) / hourRef.current) * (60 / STEP_MIN));
    const delta = steps * STEP_MIN * MIN;
    if (p.mode === "resize") {
      const end = new Date(Math.max(ev.s.getTime() + STEP_MIN * MIN, ev.e.getTime() + delta));
      return { start: ev.s, end, day: p.day };
    }
    const start = new Date(shiftDays(ev.s, days).getTime() + delta);
    return { start, end: new Date(start.getTime() + (ev.e - ev.s)), day };
  };

  useEffect(() => {
    const move = (e) => {
      const p = press.current;
      if (!p || e.pointerId !== p.pointerId) return;
      const dist = Math.hypot(e.clientX - p.x, e.clientY - p.y);
      if (!p.active) {
        if (p.type === "touch") {
          if (dist > TOUCH_SLOP) cancel(); // a swipe: let the page scroll
          return;
        }
        if (dist <= MOUSE_SLOP) return;
        p.active = true;
      }
      e.preventDefault();
      const at = compute(p, e.clientX, e.clientY);
      setGhost({ id: p.ev.id, ev: p.ev, mode: p.mode, ...at, saving: false });
    };
    const up = (e) => {
      const p = press.current;
      if (!p || e.pointerId !== p.pointerId) return;
      press.current = null;
      window.clearTimeout(p.timer);
      if (!p.active) return;
      dragged.current = true;
      window.setTimeout(() => (dragged.current = false), 0);
      const at = compute(p, e.clientX, e.clientY);
      if (at.start.getTime() === p.ev.s.getTime() && at.end.getTime() === p.ev.e.getTime()) {
        setGhost(null);
        return;
      }
      setGhost({ id: p.ev.id, ev: p.ev, mode: p.mode, ...at, saving: true });
      dropRef.current(p.ev, at.start, at.end);
    };
    const cancel = () => {
      const p = press.current;
      if (!p) return;
      window.clearTimeout(p.timer);
      press.current = null;
      if (p.active) setGhost(null);
    };
    const key = (e) => {
      if (e.key === "Escape" && press.current && press.current.active) {
        e.preventDefault();
        cancel();
      }
    };
    // While a touch drag runs the page must not scroll: a non-passive listener can refuse it.
    const touch = (e) => {
      if (press.current && press.current.active) e.preventDefault();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("keydown", key);
    window.addEventListener("touchmove", touch, { passive: false });
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("keydown", key);
      window.removeEventListener("touchmove", touch);
    };
  }, []);

  const bind = (ev, mode) => ({
    onPointerDown: (e) => {
      if (e.button !== 0 || press.current) return;
      if (mode === "resize") e.stopPropagation();
      const p = { ev, mode, x: e.clientX, y: e.clientY, day: dayOf(e.currentTarget), pointerId: e.pointerId, type: e.pointerType, timer: 0, active: false };
      if (!p.day) return;
      press.current = p;
      if (e.pointerType !== "touch") {
        try {
          e.currentTarget.setPointerCapture(e.pointerId); // the release still arrives outside the window
        } catch (err) {
          // an element that cannot capture: the window's listeners still see the pointer
        }
      }
      if (e.pointerType === "touch") {
        p.timer = window.setTimeout(() => {
          if (press.current !== p) return;
          p.active = true;
          setGhost({ id: ev.id, ev, mode, start: ev.s, end: ev.e, day: p.day, saving: false });
        }, HOLD_MS);
      }
    },
    // A long press must not open the phone's text menu or select the title.
    onContextMenu: (e) => {
      if (press.current) e.preventDefault();
    },
  });

  return {
    ghost,
    bind,
    wasDrag: () => dragged.current,
    clear: () => setGhost(null),
  };
}
