# Block ② (slice) — Daemon, Read Views and Control API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the web UI (block ③) everything it reads and the one action it needs. That means persisted run and change history, read-only views (status, upcoming events with delivery state, runs, changes), a long-running `calsync daemon`, and a local JSON control API with `calsync trigger`.

**Architecture:**
- **History.** The store gains a `change` table and richer `run` rows. The engine records one change row per object it writes, deletes, reverts or protects, with a title snapshot.
- **Views.** A new pure-read module, `calsync/views.py`, opens the state database read-only. It never creates or migrates tables, and it tolerates state written by older calsync versions. It turns tables plus config into plain dataclasses with `to_json()`.
- **CLI.** `calsync status / upcoming / runs / changes --json` print those views.
- **Daemon.** `calsync/daemon.py` runs cycles on a jittered interval with per-rule backoff, and serves a Starlette app on a 0600 Unix socket in the state directory. `calsync trigger` posts to that socket.
- **Read-only mode.** `calsync daemon --no-sync` serves the views without building adapters, reading credentials or taking the run lock. This mode is used for demos on a copy of the state.

**Tech Stack:** Python 3.12, uv, httpx, icalendar 7, Starlette, uvicorn, pytest + anyio, ruff, pyright.

**Design source:** `docs/superpowers/specs/2026-10-24-calsync-v2-product-design.md` §4 (block ②) and §5.4 (where UI data comes from). Base runtime spec: `docs/superpowers/specs/2026-10-23-calsync-design.md` §10–§12.

**Deliberately deferred (not in this plan):**
- config hot reload and safe config writes (§4.4);
- Bark/webhook notifications (§4.6);
- `approve`, `pause`/`resume`;
- `service install`;
- `config_change` table.

The README's command list must not advertise them.

## Global Constraints

- Branch: `feat/block2-daemon`, in worktree `../calsync-worktrees/block2`. Never commit to `main`.
- **Never touch the production installation:**
  - Never run anything against `~/.config/calsync`, `~/.local/state/calsync`, `~/Library/LaunchAgents`, `launchctl`, or `uv tool`.
  - Never read keychain entries or credential files.
  - Tests and manual checks use `tmp_path` configs with `env` credentials pointing at unset variables, plus the fakes in `tests/fakes/`.
- After every task, these are all clean:
  - `uv run pytest -W error -q`
  - `uv run ruff check src tests`
  - `uv run ruff format --check src tests`
  - `uv run pyright`
- TDD: write the failing test first, run it, see it fail for the expected reason, then implement.
- Test data is synthetic: `example.com` addresses and neutral titles such as "Weekly review", "Standup", "Lunch". No real names, emails, calendar IDs or medical terms.
- **Privacy invariant:**
  - Never log event titles, descriptions, locations, attendees or URLs.
  - Titles may appear only in `--json`/stdout output, the HTTP API responses, and the `change.title` column of the 0600 state database.
- `AGENTS.md` §4 invariants stay true.
  - The idle request budget (`tests/e2e/test_request_budget.py`) passes unchanged in its existing assertions.
  - Recording history must not add network requests.
- Heavy imports stay lazy in `cli.py`: `calsync --version` and `calsync config check` must not import starlette, uvicorn, httpx or icalendar.
- **Exact values (copy verbatim):**
  - `SCHEMA_VERSION = 4`
  - `CHANGE_RETENTION = timedelta(days=90)`
  - change actions: `"created"`, `"updated"`, `"reverted"`, `"deleted"`, `"protected"`, `"blocked"`
  - socket file: `<state_dir>/calsync.sock`, mode `0o600`
  - backoff: 1, 2, 4, 8, 15 minutes (cap `timedelta(minutes=15)`)
  - default interval and jitter: the existing `RuntimeConfig` (60 s, 0.1)
  - JSON API base path: `/api`
  - views JSON key `"schema"`: `{"version": <int|null>, "current": <bool>}`
  - delivery states: `"delivered"`, `"pending"`, `"unknown"`
  - health states: `"ok"`, `"failing"`, `"blocked"`, `"auth"`, `"never_run"`
- **Commits:**
  - at least one per task;
  - Conventional Commits subject;
  - message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## File Structure

| File | Change |
| --- | --- |
| `src/calsync/store.py` | schema 4: `run` gains `reverted`, `protected`, `out_of_window`, `error_kind`; new `change` table; `record_changes`, retention |
| `src/calsync/engine.py` | collect `ChangeRecord`s per rule; record them with the runs |
| `src/calsync/model.py` | `expand_instances(series, start, end)` |
| `src/calsync/views.py` (new) | read-only state access and view builders |
| `src/calsync/daemon.py` (new) | scheduler, backoff, signals, socket server |
| `src/calsync/api.py` (new) | Starlette app: JSON endpoints over the views plus trigger |
| `src/calsync/cli.py` | `status`, `upcoming`, `runs`, `changes`, `daemon`, `trigger`; `run` refusal message |
| `pyproject.toml` | add `starlette>=0.40,<1`, `uvicorn>=0.30,<1` |
| tests | per task |
| `README.md`, `AGENTS.md`, `docs/ROADMAP.md` | Task 4 |

---

### Task 1: Run and change history

**Files:**
- Modify: `src/calsync/store.py`
- Modify: `src/calsync/engine.py`
- Modify: `src/calsync/cli.py` (the skipped-rule `RunRecord` construction)
- Test: `tests/test_store.py`, `tests/test_engine.py` (or a new `tests/test_history.py`)

**Interfaces:**
- **Produces:**
  - `RunRecord` gains, appended with defaults so existing positional constructions keep working: `reverted: int = 0`, `protected: int = 0`, `out_of_window: int = 0`, `error_kind: str | None = None`.
  - New frozen dataclass `ChangeRecord(rule: str, object_id: str, action: str, at: datetime, title: str | None)`.
  - `Store.record_runs(runs, changes: Sequence[ChangeRecord] = ())` writes both in one transaction. Each change row gets `run_id` = the id of the run row of the same rule inserted in that call.
  - Retention runs in the same transaction: delete `change` rows with `at < now - CHANGE_RETENTION`, where `now` is the newest `finished_at` in the call. Change rows are pruned by age only, never together with run rows: at a 60 s interval `RUN_RETENTION` keeps about 8 hours of runs, while changes are kept 90 days, so a change's `run_id` may point at a pruned run.
  - `RuleResult.changes: list[ChangeRecord]`.
- **Schema (version 4, the usual drop-and-rebuild):**
  ```sql
  CREATE TABLE run (
      id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, started_at TEXT NOT NULL,
      finished_at TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL,
      updated INTEGER NOT NULL, deleted INTEGER NOT NULL, failed INTEGER NOT NULL,
      requests INTEGER NOT NULL, error TEXT, reverted INTEGER NOT NULL DEFAULT 0,
      protected INTEGER NOT NULL DEFAULT 0, out_of_window INTEGER NOT NULL DEFAULT 0,
      error_kind TEXT
  );
  CREATE TABLE change (
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, rule TEXT NOT NULL,
      object_id TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL, title TEXT
  );
  CREATE INDEX change_rule ON change (rule, id);
  ```
  Add `"change"` to `_TABLES`.

- **What the engine records (only in real runs; a dry run records nothing):**
  - `created`: after a successful create, with title `want.series.title`.
  - `updated` / `reverted`: after a successful update. The action is `reverted` when the write was a revert (the existing `reverting` flag in `update()`), else `updated`. The title is `want.series.title`.
  - `deleted`: after a successful delete. The title is the most recent non-null `change.title` for that `(rule, object_id)`.
    - Add `Store.last_title(rule, object_id) -> str | None`.
    - Look it up before the delete. The source event is gone, so this snapshot is the only title source.
    - It may be `None`.
  - `protected`: once per cycle per protected object id (`_run_rule`'s `protected` set). The title comes from `desired[oid].series.title` when present, else `None`. Record it only when the protected set differs from the previous cycle's set for that rule, so a protected series does not add a row every 60 s.
    - Keep `self._last_protected: dict[str, frozenset[str]]` on the Engine (in memory).
    - A fresh process records the set once.
  - `blocked`: when the delete guard blocks, one row per planned delete. The title is the summary read from that object's body, or `None` when the body is empty or unreadable (per row).
    - Same dedup as `protected`: record only when the blocked set of object ids differs from the previous cycle's (`self._last_blocked`).
- `error_kind` and the new counters come from `RuleResult`.
- In `cli.py`, the skipped-rule `RunRecord` gets `error_kind=r.error_kind`.

- [ ] **Step 1: Failing store tests.**
  - `record_runs` with changes writes the rows with the right `run_id`.
  - Changes older than 90 days relative to the newest `finished_at` are pruned.
  - `last_title` returns the newest non-null title.
  - A v3 database is rebuilt to v4. Use the existing schema-rebuild test pattern.
  - Run them and confirm they fail.
- [ ] **Step 2: Implement the store changes.** Run the store tests and confirm they pass.
- [ ] **Step 3: Failing engine tests, using `tests/harness.py` (FakeGoogle + FakeTarget).**
  - Create two events → two `created` rows with their titles.
  - Change one title → one `updated` row with the new title.
  - Edit a copy on the target (see `tests/test_engine_edges.py` revert tests) → one `reverted` row.
  - Delete the source event → one `deleted` row carrying the title from the earlier row.
  - Dry run → no rows.
  - A protected series over three cycles → one `protected` row.
  - A guard-blocked delete twice → `blocked` rows only once.
  - Idle cycle → no rows.
- [ ] **Step 4: Implement the engine changes.** Run the full suite, ruff and pyright.
- [ ] **Step 5: Commit** `feat(store): persist per-object change history and richer run rows`.

---

### Task 2: Read-only views and CLI read commands

**Files:**
- Create: `src/calsync/views.py`
- Modify: `src/calsync/model.py` (`expand_instances`)
- Modify: `src/calsync/cli.py`
- Test: `tests/test_views.py`, `tests/test_model.py`, `tests/test_cli.py`

**Interfaces:**
- **Produces:**
  - `model.expand_instances(series: Series, start: datetime, end: datetime, *, limit: int = 1000) -> list[Instance]`.
    - `Instance` is a frozen dataclass: `(start: When, end: When, title: str, location: str, alarms: tuple[Alarm, ...], recurring: bool)`.
    - Returns instances whose `[start, end)` intersects `[start, end)` (UTC), sorted by start.
    - Expands the series' RRULE/RDATE with dateutil (`rrulestr` with a `forceset`, anchored like `_rrule_anchor`, zone-aware for timed series via the series `tzid`).
    - Removes EXDATEs and any original start that has an override. Adds the overrides themselves, using their own fields.
    - A non-recurring series yields itself.
    - Bounded by `limit`: stop iterating once past `end` or after `limit` candidates.
    - Never raises on a bad RRULE. A bad RRULE yields only the master occurrence, if it is in range.
  - `views.StateReader(db_path: Path)`.
    - Opens `file:<path>?mode=ro` with `uri=True`. Never writes, never creates or migrates tables.
    - `schema_version() -> int | None`.
    - Each query checks table/column existence (`PRAGMA table_info`) and degrades. A missing `change` table means no changes. Missing `run.reverted` etc. read as 0 or None.
    - A missing database file raises `StateUnavailable`; the CLI prints "no state yet: run `calsync run` or start the daemon" and exits 1.
  - View builders, each taking `(config: Config, reader: StateReader, now: datetime, ...)` and returning a dataclass with `to_json() -> dict`:
    - `build_status(config, reader, now) -> StatusView`
    - `build_upcoming(config, reader, now, hours=24, rule=None) -> UpcomingView`
    - `build_runs(config, reader, rule=None, limit=50) -> RunsView`
    - `build_changes(config, reader, rule=None, since=None, limit=100) -> ChangesView`

- **JSON shapes (exact keys; timestamps ISO 8601 with offset; durations in ms):**
  ```json
  status: {
    "generated_at": "...",
    "schema": {"version": 4, "current": true},
    "summary": {"health": "ok", "rules": 1, "attention": 0, "last_sync_at": "...", "changes_24h": 3},
    "accounts": [{"name": "me-google", "provider": "google", "host": "Google Calendar"},
                 {"name": "me-icloud", "provider": "caldav", "host": "iCloud"}],
    "rules": [{
      "name": "family",
      "source": {"account": "me-google", "calendar": "<ref>", "calendar_name": "Family"},
      "target": {"account": "me-icloud", "calendar": "<ref>", "calendar_name": "Family"},
      "scope": {"kind": "all"} | {"kind": "window", "past_days": 7, "future_days": 30},
      "alarms": "copy" | "strip" | {"minutes_before": [10, 0]},
      "fields": {"attendees": "notes", "conference": "copy", "content": "full"},
      "health": "ok",
      "last_run": <run json or null>,
      "last_ok_at": "..." | null,
      "consecutive_failures": 0,
      "copies": 17,
      "out_of_window": 0,
      "protected": 0
    }],
    "attention": [{"kind": "auth" | "blocked" | "failing", "rule": "family", "account": "me-google" | null,
                   "message": "<plain English>", "command": "calsync auth me-google" | null}]
  }
  run: {"id": 1, "rule": "family", "started_at": "...", "finished_at": "...", "duration_ms": 812,
        "status": "ok", "created": 0, "updated": 0, "deleted": 0, "failed": 0, "reverted": 0,
        "protected": 0, "out_of_window": 0, "requests": 4, "error": null, "error_kind": null}
  upcoming: {"from": "...", "to": "...", "hours": 24, "schema": {...},
             "events": [{"rule": "family", "start": "...", "end": "...", "all_day": false,
                         "title": "...", "location": "...", "alarms_minutes_before": [0],
                         "recurring": true, "target_calendar": "Family", "delivery": "delivered"}]}
  runs: {"runs": [<run>, ...]}            (newest first)
  changes: {"changes": [{"id": 1, "run_id": 1, "rule": "family", "object_id": "...",
                         "action": "created", "at": "...", "title": "..." | null}, ...]}  (newest first)
  ```
- **Rules:**
  - **`host`:** `"Google Calendar"` for google. For caldav, derive it from `server_url`: `"iCloud"` when the URL is `"icloud"` or contains `icloud.com` / `icloud.com.cn`, otherwise the URL's hostname.
  - **`calendar_name`:**
    - Source: from `source_state.meta[<id>].summary_override or summary`. Resolve the ref with the same logic as `GoogleSource._resolve`; move that logic to a module-level function `resolve_calendar_ref(ref, meta)` in `sources/google.py` and reuse it.
    - Target: from `target_calendar.name`.
    - Fall back to the ref.
  - **`health` per rule:**
    - `never_run` when there is no run row.
    - Otherwise from the latest run: `ok` if `status == "ok"` or `"partial"` with `failed == 0`; `auth` if `error_kind == "auth"` or (for old rows without `error_kind`) `error` starts with `"AuthError"`; `blocked` if `status == "blocked"`; else `failing`.
  - **`summary.health`:**
    - the worst rule health, in the order `auth` > `blocked` > `failing` > `never_run` > `ok`;
    - `"ok"` with zero rules.
  - **`consecutive_failures`:** the number of newest runs in a row with status other than `ok`.
  - **`attention`:** one entry per rule with health `auth` / `blocked` / `failing`, with these messages (exact):
    - **`auth`:** `"The {account} account needs to be connected again."`, command `calsync auth {account}`. For `account`, use the rule's source account if the error mentions Google or the source fails, else the target. Simplest rule: the source account if `error` contains "google" (case-insensitive) or "oauth", else the target account.
    - **`blocked`:** `"Sync paused: {n} deletions are waiting for your confirmation."`, command `calsync run --rule {rule} --allow-deletes`. Here `n` is the number of `blocked` change rows sharing the greatest `run_id` among the rule's blocked rows (the latest recorded blocked set), or 0 when unknown; with 0, the message is `"Sync paused: deletions are waiting for your confirmation."`.
    - **`failing`:** `"Sync has failed {k} times in a row."`, command `null`.
  - **`changes_24h`:** the sum of `created + updated + deleted` over runs that finished within 24 h.
  - **`copies`:** the number of `target_object` rows with this rule's `rule_tag` (`naming.rule_tag`).
  - **upcoming:**
    - Load the source mirror rows for the rule's resolved source calendar (`source_event`). Build series with `sources.google_map.build_series(raw, meta)`, with meta from `source_state` (reuse `_meta_from_state`; make it public as `meta_from_state`).
    - Apply `apply_alarm_policy` and `apply_field_policy` exactly as `Engine._desired` does.
    - Expand into `[now, now + hours)`.
    - Skip series outside the rule's window when the rule has one.
    - Merge all rules and sort by start.
    - `alarms_minutes_before` = `[-int(a.offset.total_seconds() // 60) for a in alarms]`, sorted descending (largest lead first). Offsets after the start become negative numbers.
  - **`delivery`:**
    - `"unknown"` when `schema.current` is false (state written by another calsync version: hashes are not comparable).
    - Otherwise `"delivered"` when the series' `target_object` row (`object_id(rule.name, cal_id, source_id)`) exists, its `content_hash == series_hash(shaped)`, and its `reverting` column (if present) is 0.
    - Else `"pending"`.
- **CLI commands** (all accept the global `--config` / `--state-dir`; `--json` prints `json.dumps(view.to_json(), ensure_ascii=False, indent=2)`):
  - `calsync status [--json]`. Text output: one line per rule, `rule=<name> health=<h> last_sync=<iso|never> copies=<n>`, then one indented line per attention item.
  - `calsync upcoming [--hours N] [--rule R] [--json]`. Text: `<local start HH:MM or "all day">  <title>  [<target_calendar>]  alarms=<list>  <delivery>`. Titles are flattened with `_flat`.
  - `calsync runs [--rule R] [--limit N] [--json]`. Text: one run per line.
  - `calsync changes [--rule R] [--since ISO] [--limit N] [--json]`. Text: one change per line.
  - Exit code 0, or 1 with `StateUnavailable`, 2 for a config error.

- [ ] **Step 1: Failing `expand_instances` tests:**
  - a daily RRULE across a DST change in `Europe/Berlin`: wall-clock time stays fixed;
  - EXDATE removed;
  - an override moved to another time replaces its original;
  - a cancelled instance (an exdate);
  - all-day series;
  - COUNT/UNTIL;
  - an unparseable RRULE → master only;
  - the limit is respected.
- [ ] **Step 2: Implement `expand_instances`.**
- [ ] **Step 3: Failing view tests.**
  - Build state with the engine and fakes (`tests/harness.py`); run one real cycle into a `tmp_path` store; then build views.
  - Status health in each of `ok` / `auth` / `blocked` / `failing` / `never_run`, by inserting run rows directly.
  - Attention messages exact.
  - Upcoming: the next 24 h contains the expanded instances with policy-applied alarms; `delivered` after a cycle; `pending` after the source changes but before the next cycle.
  - A v1-style database is handled without error and reports `schema.current == false` and `delivery == "unknown"`. Create it with raw SQL holding only `meta(schema_version=1)`, `source_state`, `source_event`, `target_calendar`, `target_object` without the newer columns, and `run` without the newer columns.
  - The reader never modifies the file: compare mtime and bytes before and after.
- [ ] **Step 4: Implement `views.py`.**
- [ ] **Step 5: Failing CLI tests:**
  - `status --json` parses as JSON with the exact top-level keys;
  - `upcoming --json --hours 48`;
  - the text outputs;
  - a missing state → exit 1 with the message;
  - `calsync --version` still does not import `starlette` / `icalendar` (assert via `sys.modules` in a subprocess).
- [ ] **Step 6: Implement the CLI commands.** Run the full checks.
- [ ] **Step 7: Commit** `feat(views): read-only status, upcoming, runs and changes with --json`.

---

### Task 3: `calsync daemon`

**Files:**
- Create: `src/calsync/daemon.py`
- Modify: `src/calsync/cli.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- **Consumes:** `wiring.build_adapters`, `build_engine`, `accounts_in_use`, `state_paths`; `lock.RunLock`; `Store`; `Engine.cycle`.
- **Produces:**
  - `class Daemon` with:
    - `__init__(config, state_dir, *, sync: bool = True, clock=..., sleep=..., rng=random.random)`;
    - `async run(stop: asyncio.Event) -> None`;
    - `request_cycle(rule: str | None = None) -> None`;
    - `snapshot() -> DaemonInfo`.
  - `DaemonInfo(running: bool, sync: bool, pid: int, started_at: datetime, last_cycle_at: datetime | None, next_cycle_at: datetime | None, cycles: int)` with `to_json()`.
- **Behaviour:**
  - **With `sync=True`:**
    - Take `RunLock` for the process lifetime; a held lock raises `AlreadyRunning` (exit 4).
    - Open one `Store`.
    - Build adapters for every account in use once, plus one `Engine` reused across cycles. This is the in-memory mirror case the engine was written for.
    - A rule whose account setup failed is recorded as a failed run each cycle, as `cli._run` does. Move that "skipped rule" logic into a shared helper `run_cycle(engine, store, config, adapters, rules, *, dry_run, allow_deletes) -> CycleResult` in a new module `src/calsync/cycle.py`, and make `cli._run` use it too.
  - **Schedule:**
    - Start the first cycle immediately.
    - After each cycle, wait `interval × (1 + jitter × (2·rng() − 1))`.
    - A `request_cycle()` wakes the wait early. Use an `asyncio.Event`.
    - Requests that arrive during a cycle collapse into one follow-up cycle.
    - `request_cycle(rule)` runs only that rule in the next cycle. A request without a rule means all rules.
  - **Backoff:**
    - Per rule, keyed by name. After `k` consecutive non-ok results (status `failed` or `partial` with failures; not `blocked`), skip the rule until `last_failure + BACKOFF[min(k, 5) - 1]`, with `BACKOFF = (1, 2, 4, 8, 15)` minutes.
    - An explicit `request_cycle` for that rule (or for all rules) ignores the backoff.
    - A skipped rule records no run.
  - **Auth failures:**
    - After a cycle in which a rule's `error_kind == "auth"`, rebuild the adapters for that rule's accounts before its next attempt (cheap, and picks up a credential stored by `calsync auth`).
  - **`RequestLog`:** the per-cycle request count already uses `mark()`. Additionally, reset the log's internal list or counter at the start of each cycle if it grows without bound (check `http.RequestLog`; v3 left this open).
  - **`stop` event:**
    - Finish the current cycle (`Engine.cycle` is not cancelled mid-write), then exit.
    - Close adapters, store and lock.
  - **Signals:**
    - `SIGTERM` / `SIGINT` set `stop`.
    - `SIGUSR1` calls `request_cycle()`.
    - Use `loop.add_signal_handler` in the CLI entry, not in `Daemon`, so tests stay signal-free.
  - **Logging:**
    - one info line per cycle with counts only, same format as `_print_cycle` minus titles;
    - warnings as the engine already logs.
    - Logging goes to stderr via `logging.basicConfig(level=INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")`, configured in the CLI `daemon` command only.
  - **With `sync=False` (`--no-sync`):**
    - No lock, no adapters, no credential reads, no `Store` (the state DB is never opened for writing).
    - Only serves the API (Task 4).
    - `next_cycle_at` is `None`.
- **CLI:** `calsync daemon [--no-sync]`. With `sync=True` it refuses to start when the config has no rules (exit 2).
- **`calsync run` with the daemon running:** the lock is held, so `_describe` for `AlreadyRunning` now says `"another calsync process holds the lock (pid N); the daemon is running: use `calsync trigger`"`. Drop the "(a later version will support …)" text.

- [ ] **Step 1: Failing tests:**
  - With a fake clock and fake sleep (inject `sleep` that records the delay and returns immediately, or waits on the trigger event), and the harness engine with FakeGoogle/FakeTarget built from a `tmp_path` config:
    - first cycle immediate;
    - the delay is within `[54, 66]` s for `rng` 0 and 1;
    - `request_cycle` during the wait starts a cycle without waiting;
    - two requests during a cycle → exactly one follow-up;
    - backoff skips a failing rule for 1 then 2 minutes while the other rule keeps running;
    - an explicit trigger ignores the backoff;
    - `stop` exits after the current cycle;
    - `--no-sync` never calls `build_adapters` (monkeypatch it to raise) and never creates `run.lock`.
  - To inject fakes, give `Daemon` an optional `adapters_factory` parameter (default `wiring.build_adapters`).
- [ ] **Step 2: Implement `cycle.py`, `daemon.py` and the CLI command.**
- [ ] **Step 3: Full checks; `tests/e2e/test_request_budget.py` unchanged and passing.**
- [ ] **Step 4: Commit** `feat(daemon): calsync daemon with jittered schedule, per-rule backoff and --no-sync`.

---

### Task 4: Local control API and `calsync trigger`; docs

**Files:**
- Create: `src/calsync/api.py`
- Modify: `src/calsync/daemon.py` (serve the socket)
- Modify: `src/calsync/cli.py` (`trigger`)
- Modify: `pyproject.toml` (starlette, uvicorn)
- Modify: `README.md`, `AGENTS.md`, `docs/ROADMAP.md`
- Test: `tests/test_api.py`, `tests/test_daemon.py`

**Interfaces:**
- **Produces:**
  - `api.create_app(*, config_provider: Callable[[], Config], state_dir: Path, daemon: Daemon | None, clock=...) -> Starlette`.
    - Block ③ adds authentication and static files around this same app. Keep the routes in a `Router` or `list[Route]` that block ③ can mount.
- **Routes (all JSON; errors as `{"error": {"kind": "...", "message": "..."}}`):**
  - `GET /api/status` → status view plus `"daemon": DaemonInfo.to_json() | null`
  - `GET /api/upcoming?hours=24&rule=R` (`hours` clamped to 1..168)
  - `GET /api/runs?rule=R&limit=50` (limit 1..500)
  - `GET /api/changes?rule=R&since=ISO&run_id=N&limit=100` (limit 1..1000; `run_id` returns only that run's changes, so the run history page can expand one run). Add the matching optional `run_id` parameter to `views.build_changes`.
  - `POST /api/trigger` with an optional JSON body `{"rule": "R"}` → `202 {"accepted": true, "rule": "R" | null}`
    - 409 `{"error": {"kind": "read_only", ...}}` in `--no-sync`;
    - 404 for an unknown rule.
  - A missing state → 503 `{"error": {"kind": "no_state", "message": "No sync has run yet."}}`.
  - A `StateReader` is opened per request and closed after. View building runs in a thread (`anyio.to_thread.run_sync`) so the event loop never blocks on SQLite.
- **Socket:**
  - The daemon serves the app with uvicorn on `uds=<state_dir>/calsync.sock`. Use `uvicorn.Server(Config(app, uds=..., log_level="warning", lifespan="off"))`, running `server.serve()` as a task beside the scheduler. Set `server.install_signal_handlers` per the installed uvicorn's API, so uvicorn never installs signal handlers.
  - Before binding, remove a stale socket file only if connecting to it fails. A live socket means another daemon: raise `AlreadyRunning(None)`.
  - After binding, `chmod 0o600`. Remove the socket on exit.
  - In `--no-sync` mode the socket lives in the given `--state-dir`, which for demos is a copy.
- **`calsync trigger [--rule R]`:**
  - POST over the socket with `httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=...), base_url="http://calsync")`.
  - Prints `ok: cycle requested` (exit 0).
  - No socket or a refused connection → `the daemon is not running; use calsync run` (exit 1).
  - 409 → `the daemon runs with --no-sync` (exit 1).
- **Docs:**
  - README:
    - a "Running as a daemon" section: `calsync daemon`, `trigger`, `status`/`upcoming`/`runs`/`changes` with `--json`, and the privacy note that `--json` output contains titles;
    - remove the "a later version will support `calsync trigger`" wording;
    - keep the launchd section accurate. `service install` is not implemented yet, so show a manual plist with `KeepAlive` and `calsync daemon`, labelled as an example.
  - AGENTS.md: a short "Agent interface" note — use the CLI with `--json`; the HTTP API is for the web UI.
  - ROADMAP: mark this slice done under Plan 3 and list the deferred items.

- [ ] **Step 1: Failing API tests** with `starlette.testclient.TestClient` (or `httpx.ASGITransport`) on a `tmp_path` state built by one harness cycle:
  - each GET's status code and top-level keys;
  - parameter clamping;
  - trigger 202 with a fake daemon (records `request_cycle` calls), 409 in read-only, 404 for an unknown rule;
  - 503 without state.
- [ ] **Step 2: Implement `api.py`.**
- [ ] **Step 3: Failing socket test.**
  - Start `Daemon(sync=False)` with the socket in `tmp_path`, `GET /api/status` over the socket with httpx UDS transport, and check that the socket mode is 0600.
  - Check the stale-socket cleanup: create a dangling socket file first.
  - Check that `calsync trigger` against the running read-only daemon prints the `--no-sync` message.
  - Note: macOS limits AF_UNIX paths to 104 bytes. If `tmp_path` is too long, use `tempfile.mkdtemp(dir="/tmp")` in that test.
- [ ] **Step 4: Implement the socket serving and `trigger`.** The TCP listener and `--listen` belong to block ③; do not add them here.
- [ ] **Step 5: Docs.** Run the full checks.
- [ ] **Step 6: Commit** `feat(api): local control API over a unix socket and calsync trigger`, plus `docs: daemon, trigger and read commands`.
