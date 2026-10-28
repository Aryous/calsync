// The daemon's JSON API, as the UI uses it: reads (get), operations (op) and the polled status.
//
// Errors are ApiError with a ``kind``:
// - "unpaired": 401 (the cookie is gone or the key was reset) — the page reloads to "/", which
//   serves the pair page;
// - "offline": the request never got an answer (calsync stopped, the network dropped);
// - "aborted": the caller gave up on it (its ``signal``), e.g. a preview superseded by a newer one;
// - "config": 422, the config change was refused; ``problems`` lists why (field by field);
// - "conflict": 422 config_conflict, the file changed underneath (reload and try again);
// - any other: the API's own error kind (``paused``, ``read_only``, ``too_soon``, …).
// A 403 ``needs_human`` from op() is not an error: it is answered like an operation that needs a
// person ({needs_human: true, command, reason}), the shape of every operation's result.

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "/static/vendor/htm-preact-standalone.mjs";

export class ApiError extends Error {
  constructor(kind, { status = 0, message = "", problems = [], error = null } = {}) {
    super(message || kind);
    this.kind = kind;
    this.status = status;
    this.problems = problems;
    this.error = error; // the API's error object, when there was one
  }
}

let leaving = false;

function unpaired() {
  if (!leaving) {
    leaving = true;
    window.location.replace("/");
  }
  return new ApiError("unpaired", { status: 401 });
}

async function request(path, init) {
  let res;
  try {
    res = await fetch(path, { credentials: "same-origin", cache: "no-store", ...init });
  } catch (e) {
    if (e && e.name === "AbortError") throw new ApiError("aborted");
    throw new ApiError("offline");
  }
  if (res.status === 401) throw unpaired();
  let body = null;
  try {
    body = await res.json();
  } catch (e) {
    body = null;
  }
  return { res, body };
}

function failure(res, body) {
  const error = (body && body.error) || {};
  return new ApiError(error.kind || "http", {
    status: res.status,
    message: error.message || "",
    problems: error.problems || [],
    error,
  });
}

/** GET /api/<path> with query ``params`` (null and undefined left out); the JSON body.
 * ``signal``: an AbortSignal (an aborted request is ApiError "aborted"). */
export async function get(path, params, { signal } = {}) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params || {})) {
    if (value != null) url.searchParams.set(key, String(value));
  }
  const { res, body } = await request(url.pathname + url.search, { headers: { Accept: "application/json" }, signal });
  if (!res.ok || body == null) throw failure(res, body);
  return body;
}

/** POST /api/ops/<name> {args, dry_run}: the operation's result ({op, changed, summary, diff,
 * data, needs_human, command, reason}). ``signal``: as for get(). An aborted request may still
 * have reached the daemon: abort only what writes nothing (a preview, a dry run). */
export async function op(name, args = {}, { dryRun = false, signal } = {}) {
  const { res, body } = await request(`/api/ops/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ args, dry_run: dryRun }),
    signal,
  });
  if (res.ok && body) return body;
  const error = (body && body.error) || {};
  if (res.status === 403 && error.needs_human) {
    return {
      op: name,
      changed: false,
      summary: error.message || "",
      diff: "",
      data: {},
      needs_human: true,
      command: error.command || null,
      reason: error.reason || null,
    };
  }
  if (res.status === 422) {
    throw new ApiError(error.kind === "config_conflict" ? "conflict" : "config", {
      status: 422,
      message: error.message || "",
      problems: error.problems || [],
      error,
    });
  }
  throw failure(res, body);
}

// ----- the polled status ----------------------------------------------------------------------

const VISIBLE_EVERY = 10_000;
const HIDDEN_EVERY = 30_000;

export const StatusContext = createContext({ status: null, error: null, cycleRunning: false, refresh: () => {} });

/** The status for every component under the root: ``status`` (GET /api/status, null until the
 * first answer), ``error`` (the latest failure, null once an answer comes), ``cycleRunning``
 * (a cycle runs now: ``daemon.cycle_started_at``), ``refresh()`` (fetch now). */
export function useStatus() {
  return useContext(StatusContext);
}

/** Polls GET /api/status: every 10 s while the page is visible, every 30 s while it is hidden,
 * and at once when it becomes visible again. For the root component only. */
export function useStatusPoller() {
  const [state, setState] = useState({ status: null, error: null });
  const timer = useRef(0);
  const seq = useRef(0);

  const refresh = useCallback(async () => {
    window.clearTimeout(timer.current);
    const mine = ++seq.current;
    try {
      const status = await get("/api/status");
      if (mine === seq.current) setState({ status, error: null });
    } catch (e) {
      if (e.kind !== "unpaired" && mine === seq.current) setState((s) => ({ ...s, error: e }));
    } finally {
      if (mine === seq.current) {
        timer.current = window.setTimeout(refresh, document.hidden ? HIDDEN_EVERY : VISIBLE_EVERY);
      }
    }
  }, []);

  useEffect(() => {
    refresh();
    const onVisibility = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearTimeout(timer.current);
    };
  }, [refresh]);

  const daemon = state.status && state.status.daemon;
  return { ...state, cycleRunning: Boolean(daemon && daemon.cycle_started_at), refresh };
}
