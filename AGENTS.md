# AGENTS.md

Guide for AI agents and contributors working on calsync. Read this before
changing code. User-facing documentation is in [README.md](README.md); the
product roadmap is in [docs/ROADMAP.md](docs/ROADMAP.md).

## 1. What calsync is

A self-hosted, **one-way** calendar sync engine: Google Calendar → CalDAV
(iCloud global and China mainland, Radicale, other CalDAV servers). Written in
Python 3.12, managed with uv, about 14k lines in `src/` and 1,350+ tests.

The motivating use case is a family calendar maintained in Google (often by an
AI assistant writing events) that must appear, **with working alerts**, on a
relative's iPhone. That use case sets the priorities:

1. Never delete or overwrite an event wrongly. A stale copy is acceptable; a
   missing reminder is not.
2. Alerts are first-class data.
3. Cheap enough to poll every minute.
4. Generic: nothing in code refers to a specific account or calendar.

## 2. Source of truth

| Document | Role |
| --- | --- |
| `docs/superpowers/specs/2026-10-23-calsync-design.md` | design spec (v3, in sync with the code). Wins over everything else; if code and spec disagree, report it |
| `docs/superpowers/specs/2026-10-24-calsync-v2-product-design.md` | phase-2 design (full sync, daemon, web UI). Wins over v3 where they conflict; its §10 lists the affected v3 sections |
| `docs/superpowers/plans/2026-10-24-calsync-core-engine.md` | plan 1, executed. Historical: the code has been hardened well beyond its verbatim listings |
| `docs/superpowers/plans/2026-10-24-block1-sync-engine.md` | block ① plan (full sync, field policy, target-edit revert), executed. Historical in the same way |
| `docs/superpowers/plans/2026-10-25-block2-daemon-slice.md`, `…-block3-web-ui-slice.md` | the daemon slice and the first web UI slice, executed. Historical |
| `docs/superpowers/specs/2026-10-27-concept-v4-feasibility.md` | what the v4 GUI concept needs and what calsync can and cannot know; drives phases A–C |
| `docs/superpowers/plans/2026-10-28-block2-control-ops.md` | phase A: block ② completed (operations, config writes, notifications, service), executed. Historical |
| `docs/agent-cli.md` | the CLI contract for AI agents: JSON shapes, exit codes, `needs_human`, approving. Keep it in step with `--help` and the `to_json` methods |
| `docs/ROADMAP.md` | what is done, next, later; product decisions for the web UI |
| `README.md` | user-facing behaviour; keep it accurate when behaviour changes |

## 3. Architecture

```
cli.py ──► wiring.py ──► Engine.cycle()            (engine.py)
                │            │
                │            ├─ 1 ingest  Source.pull() → mirror.apply_pull() → Store.save_source()
                │            ├─ 2 detect  Target.changes() → apply_target_delta() → fetch + render.inspect()
                │            │                + render.content_digest() → CachedObject.verify_digest
                │            ├─ 3 plan    Source.to_series() → model (alarm + field policy, hash, span)
                │            │            → _classify_reads() (edited → reverting) → planner.plan()
                │            ├─ 4 guard   planner.check_delete_guard()
                │            ├─ 5 push    render.render() → Target.create/update/delete (412 fallbacks)
                │            └─ 6 record  Store.record_runs()
                ├─ sources/google.py   (+ google_map.py pure mapping, google_auth.py tokens,
                │                        google_oauth.py browser sign-in)
                └─ targets/caldav.py   (+ dav.py protocol layer)

cli.py ─┐                         ┌─► configfile.py   validated, conflict-checked, backed-up writes
        ├─► opsclient.call ─► ops.run_op ─┤                  + config_change rows
web/app ┘  (daemon socket, or        └─► daemon hooks: request_cycle, approve, borrow_adapters
            in-process with the lock)
daemon.py: scheduler (cycle.run_cycle) + api/web servers + hot reload + notify.Outbox
```

| Module | Responsibility |
| --- | --- |
| `model.py` | `When`, `Alarm`, `Attendee`, `Occurrence`, `Series` (`complete` flag, attendees, `conference_url`), `Window` (`everything()` for full sync); normalization (attendees deduplicated and totally ordered), `series_hash` (includes `RENDER_VERSION`), `compute_span` (bounded by `SPAN_EXPANSION_LIMIT`, overflow-safe), alarm policies, `FieldPolicy` / `apply_field_policy` (attendees into notes or dropped, conference link to `URL` + notes, busy-only; structured attendees never survive it); `expand(series, start, end)` → `Expansion(instances, truncated)` for read-only views: at most `EXPANSION_LIMIT` candidates from the range on and `EXPANSION_ITERATIONS` occurrences in all (a dense series started long ago is cut, `truncated`) |
| `naming.py` | `rule_tag`, `object_id`, resource name `calsync-<8hex>-<24hex>.ics`, UID `<id>@calsync`; `same_href(href, ref)` matches a listed calendar URL against a configured URL or path (trailing slash ignored) |
| `render.py` | `summary_of` / `changed_fields`: what a person sees of a series as JSON (stored per copy) and the fields that differ (change details, `event show`); Series → iCalendar bytes (VTIMEZONE, RRULE/EXDATE/RDATE, RECURRENCE-ID overrides each with own VALARMs, `URL`, `X-CALSYNC-RULE` / `X-CALSYNC-HASH`); `inspect()` reads them back and never raises; `content_digest()` hashes only what a person sees (ignores DTSTAMP, SEQUENCE, X- properties, property/event order, VTIMEZONE text, zone labels on RECURRENCE-ID/EXDATE/RDATE, alarm ACTION/DESCRIPTION and phone alarm bookkeeping); `None` for unreadable bodies, never raises |
| `planner.py` | pure `plan(desired, actual, window, protected)` (counts `out_of_window`); `check_delete_guard` (ratio only from 3 deletions on) |
| `engine.py` | one cycle; per-calendar/per-rule isolation; paused rules skipped; a retiring rule plans with `desired = {}` (not ingested, never creates its target, the guard applies); `approvals` (rule → `delete_fingerprint` of the planned deletes) let exactly that set through; every write stores the copy's `summary` and a change `detail` (fields before/after); protected/degraded handling; cursor hold; delete-guard titles; full sync when `rule.window is None`; `_classify_reads` compares a read body's digest with the digest of what we would write: equal → adopted as current whatever the marker says (no write); different while the recorded hash (what we wrote or adopted; `_detect` keeps it for known hrefs, only a cache miss takes the body's marker) is current, or a revert is pending → `reverting` (the update that follows counts as `reverted`); different with an outdated or unknown recorded hash → ordinary update; rewrite-loop breaker: a copy reverted `REVERT_LIMIT` (3) times within `REVERT_WINDOW` (6 h) that differs again is adopted instead for `REVERT_HOLD` (24 h), one warning (rule + object id) per hold; when the hold ends the copy becomes `reverting` with an unknown hash (no body read), so the source version is written back once, counted as a revert |
| `store.py` | SQLite (WAL, 0600): `source_state`, `source_event`, `target_calendar` (keyed by account+ref), `target_object` (incl. `verify_digest`, `reverting`, `revert_times`, `hold_until`, `summary` — what was written, JSON), `run` (500 per rule; `error_kind`, `error_account`), `change` (90 days; `title` snapshot, `detail` JSON), `alert` (open/closed notification keys), `account_calendar` (calendar-list cache with Google `role`), `config_change` (200 newest: source, summary, the text before, fingerprint after, `undoes`); `SCHEMA_VERSION` 5 (v4 → v5 by `_upgrade_4`); an older version with an upgrade step is migrated in place (`run`, `change` and `config_change` history kept), any other mismatch → rebuild; corrupt DB → quarantined |
| `sources/google.py` | REST client: calendarList + events.list with `syncToken`, 410 → full resync, request-params fingerprint (`events_params` state key) → full resync when `EVENTS_LIST_PARAMS` change, attendees and conference data requested, per-calendar `SourcePull.errors`, 403 rate limit → transient, 401 → one refresh + retry |
| `targets/dav.py` | PROPFIND/REPORT/PUT/DELETE/MKCALENDAR, manual redirects, absolute hrefs, error mapping (401 auth, 403 `ForbiddenError`, `valid-sync-token` → `CursorExpired`, 412 conflict) |
| `targets/base.py` | target protocol; `TargetCache` / `CachedObject` — `verify_digest` (digest of a read body its rule has not classified yet; persisted so an edit survives a cycle the rule does not finish) `reverting` (classified as edited; cleared by the successful write-back; carried across detection reads), and the loop breaker's `revert_times` / `hold_until` (carried across detection reads and writes) |
| `targets/caldav.py` | discovery (iCloud region detection via `server_url = "icloud"`), detection ladder sync → ctag → full, multiget, conditional writes, adaptive `Limiter` |
| `config.py` / `credentials.py` | validated TOML (all problems at once; `Rule.window` is `None` when omitted = full sync, `window = {}` = 7d/180d; `fields` → `FieldPolicy`; `on_target_edit` accepts only `"revert"`; `Rule.paused`, `Rule.retire`; `[notify]` with `public_url` and `language`; `[web] listen` / `allowed_hosts`); keychain / env / 0600-file secrets |
| `configfile.py` | the config file as text: `read_snapshot` (bytes + fingerprint), tomlkit edits (`set_key`, `set_rule_key`, `add_rule`, `add_account`, …, names checked against `SETTINGS` / `RULE_KEYS`), `validate_text` (the whole new text, as `load_config`), `write_config` (refuses a changed fingerprint: `ConfigConflict`; `.bak`; temp file with the original's mode, fsync, `os.replace`), `diff_text` |
| `ops.py` | every state-changing capability once (`run_op(ctx, name, args, dry_run)` → `OpResult.to_json()`, the stable envelope): `config.set/undo`, `rule.add/set/remove/pause/resume/preview`, `account.add/remove`, `approve`, `trigger`, `calendars.refresh`, `notify.test`. Config ops: read → edit → validate → (dry run: diff) → write → `config_change` row → `on_config_written`. Summaries name config keys, never values. No op takes a secret (D10): `needs_human` |
| `opsclient.py` | where an op runs: posted to the daemon's socket when one listens, else in-process holding the run lock (`AlreadyRunning`, exit 4, when another process holds it); `DaemonLost` when the daemon took it and went away |
| `wiring.py` | the only place account definitions become clients; one `CalDavTarget` per account; `build_adapters` is **async**; per-account setup errors in `Adapters.errors` |
| `cycle.py` | `run_cycle`: one cycle as `calsync run` and the daemon run it (paused rules skipped; rules whose account failed to set up are recorded as failed, with `error_account`); `counts_line`; `retired` / `remove_retired` (a retiring rule that ended `ok` with no copy left in its current target is removed from the config, source `daemon`) |
| `views.py` | read-only `StateReader` (never creates or migrates; tolerates older schemas) and the `status` (accounts with cached calendars, `attention` from `run.error_account`), `upcoming`, `runs`, `changes` (stops scanning at the first row older than `since`), `blocked` (`build_blocked`: the latest recorded set and its fingerprint) views with `to_json()` — the JSON contract shared by `--json`, the API and the web UI; `config_json` / `mask_config_text` (masked unless revealed), `config_history_json` |
| `events_view.py` | `build_events` (a date range, ≤ 62 days, within 10 years; one entry per occurrence; a delivery state per rule: `unknown`, `paused`, `retiring`, `out_of_window`, `delivered`, `blocked`, `failing`, `pending`) and `build_event` (one opaque id: the stored copy summary and the fields it differs in) |
| `notify.py` | alert evaluation after each recorded cycle (`auth`, `blocked` with damping, `failing`, `recovered`, `reload_failed`, `approved`), dedup in the `alert` table (keys opened before the push), Bark / webhook delivery (`Notifier`, the daemon's background `Outbox`); texts in `zh` / `en`. Titles in pushes only, never in logs |
| `service.py` | `calsync service`: plist rendering (absolute executable, `--config` / `--state-dir`, `KeepAlive`, `ExitTimeOut`), install / uninstall / status through a `Launchctl` (the real one runs `launchctl`; tests pass a fake), other calsync jobs found best effort, bootout then poll `print` (`IN_PROGRESS_CODE` 36 is still stopping) |
| `daemon.py` | `Daemon`: jittered schedule, per-rule backoff, adapter rebuilds after an auth failure or a config-kind setup error, ops run under `_cycle_lock` (never during a cycle), approvals kept for the rule's next cycle, waiters (`trigger --wait`, `approve --wait`), notifications after `_record`, the calendar-list refresh (start + every 6 h), a startup warning when `web.key` is unusable, `request_cycle`, `--no-sync`; runs the scheduler and the API server as services that stop each other through `stop`, never by cancelling a cycle; hot reload of the config file (between cycles, under `_cycle_lock`: `Engine.replace_config`, only changed accounts rebuilt; external edits recorded as `config_change` rows), paused rules never selected, retiring rules removed once their copies are gone |
| `api.py` | Starlette routes under `/api` (`api_routes`, `create_app`): `status`, `upcoming`, `runs`, `changes`, `events`, `event`, `blocked`, `config` (masked), `config/history`, `calendars`, `trigger`, `POST /api/ops/<name>`; a `StateReader` per request in a worker thread; `ContainErrors` (any escaping error → fixed 500, type-only log), `unix_socket` (stale check, 0600) and `tcp_socket`, and `serve` (uvicorn without signal handlers, no proxy headers) |
| `web/auth.py` | the pairing key (`web.key`, 0600), the session cookie token, pair links; standard library only |
| `web/app.py` | `create_web_app`: `/`, `/pair`, `/static`, the `/api` routes behind the cookie, the operations among them (`POST /api/ops/<name>`, source `ui`, a same-origin `Origin` required; their diffs masked as `GET /api/config` masks; deleting a credential, or adding an account whose keychain entry already exists, answers `needs_human`); Host, Origin and rate-limit checks |
| `debug.py` | `CALSYNC_DEBUG=1`: tracebacks at DEBUG for failures otherwise logged by type only |
| `cli.py` | commands, exit codes, output; heavy imports are lazy |

### Agent interface

Agents use the CLI, documented for them in [docs/agent-cli.md](docs/agent-cli.md):
write to Google with their own tool, `calsync trigger --wait --json`, then
`calsync events --json` to check each delivery; `blocked` → `approve --fingerprint
F --yes` for blocked deletes; config changes with `--dry-run` first. Shapes come
from the `to_json` methods (`views.py`, `events_view.py`, `ops.OpResult`) and
are a contract: add keys, never change or drop one. The HTTP API on the
daemon's socket is for the CLI and the web UI; do not script against it.
`--json` output contains event titles: keep it out of logs.

## 4. Invariants — do not break

1. **One way.** Source → target only.
2. **Ownership by resource name.** Only hrefs matching `calsync-<8hex>-<24hex>.ics`
   are managed. Everything else in a target calendar is invisible to the engine.
3. **Local state is a cache.** Deleting the state directory must lead to the same
   target contents, with no duplicates (create → 412 → re-read → update).
4. **Deletes need evidence.** Delete only an `actual` whose source series is gone
   or out of window, and whose body was read. If a changed href cannot be
   fetched, drop it from the cache and **hold the cursor**.
5. **Protected and degraded series.** A series present in the source but
   unparseable (`Source.series_ids` minus parsed ids), or parsed with a broken
   instance (`Series.complete == False`), never has its existing target copy
   updated, reverted or deleted; it is excluded from the delete-guard denominator. A
   degraded series with no copy is created from its parseable part.
6. **Request budget (spec §8.1).** Idle cycle = 1 calendarList per Google
   account + 1 events.list per source calendar + 1 change check per target
   calendar, and no body reads — also in the cycle right after our own writes
   (including a revert) and in a fresh process. Enforced by `tests/e2e/test_request_budget.py`;
   if it fails, fix the code, not the test.
7. **Privacy.** Never log or print event titles, descriptions, locations,
   attendees, URLs or secrets. Delete-guard titles go to the CLI output only;
   titles also appear in `--json` / view output, the API responses and the
   `change.title` column. The daemon keeps `httpx` / `httpcore` at WARNING
   (they log request URLs at INFO). Test data is synthetic (`example.com`,
   neutral titles).
8. **Isolation.** A failure in one calendar, account or rule fails only what
   depends on it; `cycle()` always returns a `CycleResult`.
9. **Error taxonomy → exit codes.** `AuthError` → 3 (only 401 / invalid_grant
   mean auth), `ConfigError` → 2 (also a refused validation, `ConfigConflict`,
   bad arguments), everything else that fails → 1, lock busy or another daemon
   serving the state directory → 4. Precedence for several rules: 3 > 2 > 1.
10. **One writer.** Only the run-lock holder writes the state DB: the daemon,
    `calsync run`, or the CLI while it holds the lock for one operation. Every
    other reader uses `StateReader` (never creates or migrates). Schema changes
    are additive in-place upgrades (`_upgrade_3`, `_upgrade_4`); `run`, `change`
    and `config_change` are never dropped.
11. **One operation layer.** Every state change goes through `ops.run_op`, from
    the CLI and the API alike; config validation lives only in
    `parse_config` (via `configfile.validate_text`). In the daemon, operations
    run under `_cycle_lock`, between cycles. No operation takes a password or
    token: what needs a person is answered with `needs_human`.
12. **The guard is on every path.** Retiring a rule and approving go through
    `check_delete_guard` like any cycle; only `calsync run --allow-deletes` (a
    person, one cycle) bypasses it. An approval releases exactly the set whose
    fingerprint it names, and a different set blocks again.
13. **Sealed tests.** No test may reach a real config, the keychain, the
    network (beyond loopback and unix sockets) or `launchctl`. The autouse seal
    in `tests/conftest.py` enforces it; never weaken it, fake per test instead.

## 5. Working on the code

```sh
uv sync
uv run pytest                  # ~10 s; e2e tests spawn a throwaway Radicale
uv run pytest -W error -q      # must stay clean
uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright
```

- **TDD.** Write the failing test first and record why it fails. Prefer the
  strict fakes over mocks: `tests/fakes/google.py` (`FakeGoogle`: syncToken
  semantics, 410, param fingerprints, minimal cancelled payloads) and
  `tests/fakes/target.py` (`FakeTarget`: strict 412, `mode="sync"`).
  `tests/harness.py` builds an engine against FakeGoogle + Radicale
  (`build`) or FakeTarget (`build_fake`).
- **Sealed tests.** An autouse fixture in `tests/conftest.py` gives every test a
  temporary `HOME`, `CALSYNC_CONFIG` and `CALSYNC_STATE_DIR`, a keychain that
  refuses every call (`PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring` for
  child processes), no proxy, and no connection or name lookup beyond loopback
  and unix sockets, and `service._run` refuses to run `launchctl` (service tests
  pass a fake `Launchctl`). A refusal fails the test even if the code swallows
  it. Fake the keychain per test (patch `credentials._keyring`); never weaken
  the seal.
- **Commits:** small, one logical change, Conventional Commits subject; end the
  message with `Co-Authored-By: Claude <model> <noreply@anthropic.com>` naming
  the model that actually made the change.
- **Branches:** work on a feature branch; `main` is kept green and is what is
  pushed. History was rewritten once to remove personal data — never
  reintroduce it.
- **Process used so far:** brainstorm → spec → plan → subagent execution with
  a review after every task → whole-branch review. Reviews found real bugs in
  almost every task; keep them.

## 6. Gotchas learned the hard way

- `uv sync` before `src/calsync` exists leaves no editable `.pth`; run
  `uv sync --reinstall-package calsync` once.
- Do not call `ElementTree.register_namespace` (global state; Radicale changes
  it too). Tests parse XML before asserting.
- `sqlite3` `executescript()` commits an open transaction; create tables with
  individual `execute` calls. `_Transaction` rolls back only when
  `in_transaction`.
- icalendar 7 parses leniently: `decoded("dtstart")` can be a `str` or a PERIOD
  tuple. `when_from_ical` rejects non-dates; `inspect` catches everything.
- A zero-minute alert serializes as `TRIGGER:P0D` (valid).
- Google: `syncToken` cannot be combined with `timeMin`/`timeMax`/`updatedMin`;
  every other parameter must equal the initial sync; `fields` must keep
  `nextSyncToken,nextPageToken`. A rate-limit 403 is not an auth failure.
  Cancelled exceptions are guaranteed only `id`, `recurringEventId`,
  `originalStartTime`.
- CalDAV: `sync-collection` must be sent with `Depth: 0` (RFC 6578);
  a 507 on the collection means truncated. iCloud's calendar home is on another
  host (`pNN-caldav.icloud.com[.cn]`); httpx would drop credentials on that
  redirect, hence manual redirects. Strict servers answer 412, not 404, to
  `If-Match` on a missing resource.
- iCloud China accounts must use `caldav.icloud.com.cn`; `server_url = "icloud"`
  tries global first and moves on only on 401 (never on network errors).
- The keychain backend needs a logged-in session (launchd agent, not a
  daemon at the login screen); otherwise use the `file` backend.
- macOS `time.monotonic` stops during sleep — token expiry uses wall time.
- `content_digest` must ignore phone bookkeeping (ACKNOWLEDGED, snoozed VALARMs
  with RELATED-TO, X-APPLE-DEFAULT-ALARM); otherwise calsync would undo snoozes.
  VALARMs with an absolute trigger are ignored for the same reason, and alarm
  ACTION/DESCRIPTION are not content.
- Detection reads a changed body once; classifying it is the job of its rule,
  which may not run this cycle (`--rule`), may fail first, or the process may
  stop. Hence `CachedObject.verify_digest` / `reverting` are persisted and
  classification is saved before planning, also in a dry run. Never classify
  from in-memory state only.
- Digest equality wins over the marker. iOS may drop or keep a stale
  `X-CALSYNC-HASH` when it saves a snooze; a read copy whose visible content
  equals what we would write is adopted (desired hash and span cached), never
  rewritten.
- The body's marker never classifies a copy we already know. `_detect` keeps
  the cached `content_hash` (what we last wrote or adopted) and `reverting`
  for a known href and takes the marker only on a cache miss (cold start,
  lost state); an unparseable body gets `None` (rewritten). Once the digests
  differ, the recorded hash decides between "edited on the target" (revert,
  counts toward the breaker) and "source changed" (ordinary update).
  Overwriting it from the body would let a target that strips the marker
  turn every edit into an uncounted update. Detection logs (counts only) how
  many copies came back with no marker at all — the iOS check. Another marker
  is not reported: a copy adopted on a cold start keeps its old one.
- Rewrite-loop breaker: a server or phone that rewrites visible content after
  every write would otherwise be reverted every cycle. The breaker uses the
  engine's injected clock, is persisted on `CachedObject`, and must be carried
  wherever a `CachedObject` is rebuilt (`_detect`, `remember()`). It only
  holds reverts (recorded hash current, digest differs); a source change is
  still written during a hold. A held copy is recorded as current, so the hold
  must end with a write: `_classify_reads` turns an expired hold into
  `reverting` with `content_hash=None` (no body read, request budget
  unchanged); that write counts as one revert (`revert_times` was emptied when
  the hold started, so it cannot start a new hold by itself). Digest equality
  clears a hold. A protected copy keeps its hold until it is readable again.
- Changing `EVENTS_LIST_PARAMS` (e.g. `EVENT_FIELDS`) changes the
  `events_params` fingerprint and forces a full pull of every source calendar
  once; changing rendered bytes needs a `RENDER_VERSION` bump. A bump rewrites
  only copies whose visible content differs when their body is read; bodies
  are read on a cold start (state lost or `SCHEMA_VERSION` bumped). With a warm
  state no body is read, so a bump alone rewrites every copy once — pair it
  with a `SCHEMA_VERSION` bump if that matters.

## 7. Real-world status

The first real run (Google → iCloud China mainland, recurring series with
hundreds of per-instance edits, events with up to three alerts) worked end to
end: discovery across hosts, region detection, sync-collection mode, all alerts
visible in Apple Calendar, idle second cycle at budget. Measured: alerts on a
calendar shared to another Apple ID do **not** reach the sharee (per-user
`VALARM`); the sharee sees only their own iPhone default alert (spec §13
item 1). Reverting edits made on the target is implemented and tested against
Radicale and `FakeTarget`; the real-device check (an edit and a snooze on an
iPhone against iCloud, experiment E1) is still pending. Writing into a family
member's own account (E5) needs no separate check: it is the situation of the
first real run (an account's own app-specific password, alerts firing on that
account's iPhone), in daily use since 2026-10-24. Still unverified on iCloud:
401 vs 403 for a bad password, `MKCALENDAR` (spec §13).

## 8. Known limitations and debt

- Target edits that touch only properties outside `content_digest` (STATUS,
  CLASS, CATEGORIES, …) are not detected; an edit to a protected series is
  reverted only after the series parses again.
- Only Google pop-up reminders are synced; attendees are never invitees.
- Renaming a rule orphans its events (removing one does too, unless
  `rule remove --delete-copies`).
- `degraded` counts live only on `RuleResult`, not in run history (protected
  and blocked series are recorded, with their titles, in the `change` table).
- No byte counts in run history; no `invalid_client` → `ConfigError` mapping;
  held cursor has no cap or escalation.
- The daemon reloads the config file while it runs (polled every 2 s between
  cycles), except the `[web]` listen address, which needs a restart.
- `build_changes` stops at the first row older than `since`: it assumes change
  ids follow time (a wall clock set back between cycles could hide a few rows).
- A blocked set's fingerprint is recomputed from the `change` rows, which are
  kept 90 days: a block older than that can no longer be approved by
  fingerprint (`unrecorded_block`).
- Notifications: a push that fails is not retried; after the damping window
  no catch-up push is sent for a blocked set that changed meanwhile.

## 9. Next work

See [docs/ROADMAP.md](docs/ROADMAP.md). Blocks ① and ② are implemented (the
iPhone check E1 is still pending). Next is block ③: phase B rewrites the web
UI after concept v4 on top of the operations; phase C adds editing events.
