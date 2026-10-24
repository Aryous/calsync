# AGENTS.md

Guide for AI agents and contributors working on calsync. Read this before
changing code. User-facing documentation is in [README.md](README.md); the
product roadmap is in [docs/ROADMAP.md](docs/ROADMAP.md).

## 1. What calsync is

A self-hosted, **one-way** calendar sync engine: Google Calendar → CalDAV
(iCloud global and China mainland, Radicale, other CalDAV servers). Written in
Python 3.12, managed with uv, about 4k lines in `src/` and 300+ tests.

The motivating use case is a family calendar maintained in Google (often by an
AI assistant writing events) that must appear, **with working alerts**, on a
relative's iPhone. That use case sets the priorities:

1. Never delete or overwrite an event wrongly. A stale copy is acceptable; a
   missing reminder reminder is not.
2. Alerts are first-class data.
3. Cheap enough to poll every minute.
4. Generic: nothing in code refers to a specific account or calendar.

## 2. Source of truth

| Document | Role |
| --- | --- |
| `docs/superpowers/specs/2026-10-23-calsync-design.md` | design spec (v3, in sync with the code). Wins over everything else; if code and spec disagree, report it |
| `docs/superpowers/plans/2026-10-24-calsync-core-engine.md` | plan 1, executed. Historical: the code has been hardened well beyond its verbatim listings |
| `docs/ROADMAP.md` | what is done, next, later; product decisions for the web UI |
| `README.md` | user-facing behaviour; keep it accurate when behaviour changes |

## 3. Architecture

```
cli.py ──► wiring.py ──► Engine.cycle()            (engine.py)
                │            │
                │            ├─ 1 ingest  Source.pull() → mirror.apply_pull() → Store.save_source()
                │            ├─ 2 detect  Target.changes() → apply_target_delta() → fetch + render.inspect()
                │            ├─ 3 plan    Source.to_series() → model (policy, hash, span) → planner.plan()
                │            ├─ 4 guard   planner.check_delete_guard()
                │            ├─ 5 push    render.render() → Target.create/update/delete (412 fallbacks)
                │            └─ 6 record  Store.record_runs()
                ├─ sources/google.py   (+ google_map.py pure mapping, google_auth.py tokens,
                │                        google_oauth.py browser sign-in)
                └─ targets/caldav.py   (+ dav.py protocol layer)
```

| Module | Responsibility |
| --- | --- |
| `model.py` | `When`, `Alarm`, `Occurrence`, `Series` (`complete` flag), `Window`; normalization, `series_hash` (includes `RENDER_VERSION`), `compute_span` (bounded by `SPAN_EXPANSION_LIMIT`, overflow-safe), alarm policies |
| `naming.py` | `rule_tag`, `object_id`, resource name `calsync-<8hex>-<24hex>.ics`, UID `<id>@calsync` |
| `render.py` | Series → iCalendar bytes (VTIMEZONE, RRULE/EXDATE/RDATE, RECURRENCE-ID overrides each with own VALARMs, `X-CALSYNC-RULE` / `X-CALSYNC-HASH`); `inspect()` reads them back and never raises |
| `planner.py` | pure `plan(desired, actual, window, protected)`; `check_delete_guard` (ratio only from 3 deletions on) |
| `engine.py` | one cycle; per-calendar/per-rule isolation; protected/degraded handling; cursor hold; delete-guard titles |
| `store.py` | SQLite (WAL, 0600): `source_state`, `source_event`, `target_calendar` (keyed by account+ref), `target_object`, `run` (500 per rule), `alert`; schema mismatch → rebuild; corrupt DB → quarantined |
| `sources/google.py` | REST client: calendarList + events.list with `syncToken`, 410 → full resync, per-calendar `SourcePull.errors`, 403 rate limit → transient, 401 → one refresh + retry |
| `targets/dav.py` | PROPFIND/REPORT/PUT/DELETE/MKCALENDAR, manual redirects, absolute hrefs, error mapping (401 auth, 403 `ForbiddenError`, `valid-sync-token` → `CursorExpired`, 412 conflict) |
| `targets/caldav.py` | discovery (iCloud region detection via `server_url = "icloud"`), detection ladder sync → ctag → full, multiget, conditional writes, adaptive `Limiter` |
| `config.py` / `credentials.py` | validated TOML (all problems at once); keychain / env / 0600-file secrets |
| `wiring.py` | the only place account definitions become clients; one `CalDavTarget` per account; `build_adapters` is **async**; per-account setup errors in `Adapters.errors` |
| `cli.py` | commands, exit codes, output; heavy imports are lazy |

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
   updated or deleted; it is excluded from the delete-guard denominator. A
   degraded series with no copy is created from its parseable part.
6. **Request budget (spec §8.1).** Idle cycle = 1 calendarList per Google
   account + 1 events.list per source calendar + 1 change check per target
   calendar, and no body reads — also in the cycle right after our own writes
   and in a fresh process. Enforced by `tests/e2e/test_request_budget.py`;
   if it fails, fix the code, not the test.
7. **Privacy.** Never log or print event titles, descriptions, locations or
   secrets. Delete-guard titles go to the CLI output only. Test data is
   synthetic (`example.com`, neutral titles).
8. **Isolation.** A failure in one calendar, account or rule fails only what
   depends on it; `cycle()` always returns a `CycleResult`.
9. **Error taxonomy → exit codes.** `AuthError` → 3 (only 401 / invalid_grant
   mean auth), `ConfigError` → 2, everything else that fails → 1, lock busy → 4.
   Precedence for several rules: 3 > 2 > 1.

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

## 7. Real-world status

The first real run (Google → iCloud China mainland, recurring series with
hundreds of per-instance edits, events with up to three alerts) worked end to
end: discovery across hosts, region detection, sync-collection mode, all alerts
visible in Apple Calendar, idle second cycle at budget. Still unverified on
iCloud: repair of edits made on the phone, 401 vs 403 for a bad password,
alerts on calendars shared to another Apple ID, `MKCALENDAR` (spec §13).

## 8. Known limitations and debt

- Target-side edits are not repaired (strict xfail
  `test_manual_target_edit_is_repaired`).
- Renaming or removing a rule orphans its events.
- `RequestLog` grows forever (fine for `run`, must be reset per cycle in a
  daemon).
- `protected` / `degraded` / delete-guard titles live only on `RuleResult`,
  not in run history.
- No byte counts in run history; no `invalid_client` → `ConfigError` mapping;
  held cursor has no cap or escalation.
- `[runtime]` and `[notify]` are validated but unused until the daemon exists.

## 9. Next work

See [docs/ROADMAP.md](docs/ROADMAP.md): plan 2 (real-service verification,
starting with target-side edits), plan 3 (daemon, alerts, launchd/Docker,
deployment on a home server), plan 4 (self-hosted web UI — product decisions
recorded there, page layout still to be designed).
