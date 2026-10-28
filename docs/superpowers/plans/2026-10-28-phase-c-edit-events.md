# Phase C — writing events to Google: implementation plan

> Controller-implemented (the user's choice for this phase): one person writes every task in
> order, commits per task, and one Opus subagent reviews the whole branch at the end. Steps use
> checkbox (`- [ ]`) syntax. Ledger: `.superpowers/sdd/progress.md`.

**Goal:** calsync can write to Google Calendar, with a person (web UI) or an agent (CLI) in
charge: create an event, change it (this occurrence or the whole series), delete it, write
many events in one idempotent batch, and find and remove duplicates. Every write is followed by
a sync, so the iCloud copy follows within one cycle. calsync itself stays one-way.

**Architecture:** a thin Google write client (`sources/google_write.py`) beside the existing
read-only source; one new operations module (`event_ops.py`) whose operations are registered in
`ops.ASYNC_OPS`, so the CLI and the daemon API share them; schema 6 adds the write history and
each account's granted scopes; the engine learns a *pre-approval* so deletes a person asked for
do not trip the mass-delete guard; the web UI gets an edit form, a new-event form, a delete
action and a duplicates inspector.

**Tech stack:** as before: Python 3.12+/asyncio, httpx, Starlette, SQLite, tomlkit; Preact + htm
without a build step; pytest, Playwright (Chromium, WebKit).

## Scope

The feasibility review (§3.7, §8) put in C: write access per account, changing the original,
"only this one / the whole series", sync right after saving, CLI `event edit`. It left creating,
deleting and dragging to "after C". The ROADMAP puts creating in C. On 2026-10-28 the user added
two features as required: **removing duplicates** and **batch writes** (the recurring reminder
schedule: 330 events written by a script, and duplicates an assistant had created).

- **In C:** write access (`calsync auth <account> --write`); create; edit (this occurrence /
  whole series); delete (this occurrence / whole series); batch apply (CLI and API, for agents);
  find duplicates (read-only view, attention card) and remove them (web UI and CLI); write
  history in the activity timeline; read-only calendars and invitations greyed out.
- **Not in C:** "this and following" (splitting a series); drag to move; editing attendees or
  sending invitations (calsync always writes with `sendUpdates=none`); editing iCloud copies
  (one-way, D5); a batch-import form in the web UI (agents use the CLI; the UI shows the result).

## Facts checked against Google's documentation (2026-10-28)

- The refresh-token response carries `scope`: "the scopes of access granted by the
  access_token, … space-delimited". (OAuth 2.0 for installed apps.)
- Incremental authorization is **not supported for installed apps**: asking for write access
  means asking for every scope again (`calendar.readonly calendar.events`).
- Revoking an access token revokes its refresh token, and calsync's client is shared with any
  script using the same client file. **calsync never calls the revoke endpoint**, and no
  helper script in this phase does either.
- Conditional changes: send the event's `etag` in `If-Match` on patch/delete; a changed event
  answers **412 Precondition Failed**. Inserts have no conditional form.
- Batch: `POST https://www.googleapis.com/batch/calendar/v3`, `multipart/mixed`, each part
  `Content-Type: application/http` with a path-only request; response parts in request order,
  `Content-ID` echoed as `response-<id>`; calls may run in any order. calsync sends at most
  **50 calls per batch** (Google allows more; 50 keeps one failing batch small).
- `events.instances` takes `originalStart`: the one instance of a series that starts there.

## Global constraints

- Never call Google's token revoke endpoint. No test talks to real Google or iCloud; all test
  data is synthetic (no real names, calendar ids, personal health data).
- Every write operation supports `--dry-run` / `dry_run` and `--json`; a dry run makes no
  write request (it may read).
- Edits and deletes are conditional: GET the live event, compare with what the caller saw
  (`expect`: the `etag`, or the mirror's `updated`), then PATCH/DELETE with `If-Match`. A
  mismatch or a 412 is `conflict` (HTTP 409 in the API) and writes nothing.
- Writes use `sendUpdates=none`. Events the account does not organise (`organizer.self` false)
  and calendars whose role is `reader` / `freeBusyReader` are not writable; the UI greys them
  out and the operations refuse them before any request.
- A write operation builds its own Google adapter from the **stored** credential (never the
  daemon's cached one), so a credential stored by `calsync auth --write` works at once.
- Missing write access is `needs_human`: `{"command": "calsync auth <account> --write",
  "reason": …}`. No secret is typed into the browser.
- After a successful write, the operation requests a cycle for every rule whose source is the
  written calendar (it does not wait unless `wait` is given).
- Copy deletes that follow from deletes a person or agent asked for in calsync are
  pre-approved (see Task 2) and never shown as "held back".
- Plain words in the UI, as in DESIGN.md: 原件 / 副本 / 线路 / 已送达 iCloud. Chinese by default,
  every string through `t()`. Phone tap targets at least 44 px.
- Summaries (`OpResult.summary`) name accounts, calendars and counts, never an event title (titles
  are personal data; they go in `data`, which the history keeps as the change log already does).

## File map

| File | Responsibility |
| --- | --- |
| `src/calsync/sources/google_write.py` (new) | `EventFields` (the editable fields) ↔ Google resource; `GoogleWriter`: get, get_instance, insert, patch, delete, batch |
| `src/calsync/sources/google.py` | `EVENT_FIELDS` += `created,organizer(self),extendedProperties(private)` (one full re-pull per calendar, by design) |
| `src/calsync/sources/google_auth.py` | `RefreshTokenProvider.scopes`: the scopes the last refresh reported |
| `src/calsync/sources/google_oauth.py`, `cli.py` (`auth`) | `--write`: ask for `calendar.readonly calendar.events` |
| `src/calsync/store.py` | schema 6: `event_write` (history), `account_access` (scopes seen); `_upgrade_5` |
| `src/calsync/engine.py`, `planner.py` | pre-approval: deletes in `Approval.covers` do not count for the guard |
| `src/calsync/event_ops.py` (new) | `event.create`, `event.edit`, `event.delete`, `events.apply`, `duplicates.remove` |
| `src/calsync/duplicates.py` (new) | pure: find duplicate groups in a calendar's mirror, choose the one to keep |
| `src/calsync/ops.py` | register the new ops; `DaemonHooks.preapprove` |
| `src/calsync/daemon.py` | store pre-approvals per rule, hand them to the next cycle; record scopes after a pull |
| `src/calsync/events_view.py`, `views.py` | `editable` in the event detail; `can_write` per account in status; duplicates view; write history |
| `src/calsync/api.py` | `GET /api/duplicates`, `GET /api/writes`; ops through `/api/ops/*` as today |
| `src/calsync/cli.py` | `event new|edit|delete|apply`, `duplicates [--remove]`, `writes`, `auth --write` |
| `src/calsync/web/static/forms/edit.js` (new) | the event form (new and edit), the delete confirmation |
| `src/calsync/web/static/forms/event.js` | 修改 / 删除 in the detail; greyed reasons |
| `src/calsync/web/static/forms/duplicates.js` (new) | the duplicates inspector |
| `src/calsync/web/static/calendar.js`, `app.js`, `activity.js`, `i18n.js`, `styles.css` | 新建日程 button, routes `#/event-new`, `#/event-edit/<id>`, `#/duplicates`, attention card, write rows |
| `tests/…` | unit, ops, API, CLI and browser tests per task |
| `docs/agent-cli.md`, `DESIGN.md`, `README.md`, `docs/ROADMAP.md` | the new commands and principle 6 |

---

### Task 1: Google write client and write access

**Files:** create `src/calsync/sources/google_write.py`; modify `sources/google.py`,
`sources/google_auth.py`, `sources/google_oauth.py`, `cli.py` (`auth`); tests
`tests/test_google_write.py`, `tests/test_google_auth.py`, `tests/test_cli_auth.py`.

**Produces:**

```python
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

@dataclass(frozen=True, slots=True)
class EventFields:          # what calsync lets a person or an agent set
    title: str | None = None
    start: When | None = None          # model.When: a date (all day) or an aware datetime + tzid
    end: When | None = None
    location: str | None = None
    description: str | None = None
    alarms: tuple[int, ...] | None = None   # minutes before; () = none; None = calendar default
    key: str | None = None             # extendedProperties.private.calsync_key (batch identity)

def to_resource(fields: EventFields, *, patch: bool) -> dict[str, Any]   # only the set fields when patch
def from_resource(raw: Mapping[str, Any], default_tz: str) -> EventFields
def field_changes(before: EventFields, after: EventFields) -> list[str]  # "time 10:00 → 09:30", for the history

class WriteConflict(CalsyncError)      # 412, or expect mismatch
class NoWriteAccess(AuthError)         # 403 insufficientPermissions / ACCESS_TOKEN_SCOPE_INSUFFICIENT

class GoogleWriter:
    def __init__(self, client, token: TokenProvider, *, base_url=BASE_URL, batch_url=BATCH_URL)
    async def get(self, calendar_id, event_id) -> dict
    async def instance(self, calendar_id, series_id, original_start: str) -> dict   # events.instances?originalStart
    async def find_key(self, calendar_id, key) -> dict | None  # privateExtendedProperty=calsync_key=<key>
    async def insert(self, calendar_id, body) -> dict
    async def patch(self, calendar_id, event_id, body, *, etag: str) -> dict
    async def delete(self, calendar_id, event_id, *, etag: str | None) -> None   # 404/410: already gone, fine
    async def batch(self, calls: Sequence[BatchCall]) -> list[BatchReply]           # ≤ 50 per request, split here
```

- [ ] Tests first (`httpx.MockTransport`): all-day and timed resources both ways (timed keeps
  the `timeZone`); `alarms=None` → `reminders.useDefault=true`, `()` → `useDefault=false,
  overrides=[]`; patch bodies carry only set fields; `If-Match` header on patch/delete;
  `sendUpdates=none` on every write; 412 → `WriteConflict`; 403 with
  `insufficientPermissions` → `NoWriteAccess`; 403 rate limit → `TransientError`; 401 refreshes
  once (as `GoogleSource._get`); delete 404/410 is success; batch of 120 calls → 3 requests of
  50/50/20, parts parsed in order, a part's 4xx is that call's error, not the batch's;
  `find_key` sends `privateExtendedProperty`.
- [ ] Implement. Reuse `request_with_retry`, `_error_reasons`, `_is_rate_limited` from
  `google.py` (move the two helpers to a shared place only if a cycle of imports forces it).
- [ ] `EVENT_FIELDS` += `created,organizer(self),extendedProperties(private)`; a test that `EVENTS_PARAMS_ID` changed (so
  every calendar re-pulls fully once: intended).
- [ ] `RefreshTokenProvider.scopes: frozenset[str] | None` from the refresh response's `scope`.
- [ ] `calsync auth <account> --write`: `authorization_url(..., scope=READ+WRITE)`; stored
  credential format unchanged; output "ok: credentials stored (read and write), N calendars
  visible". Without `--write` nothing changes (read only).
- [ ] Commit `feat(google): write client, write access with auth --write`.

### Task 2: schema 6, write history, scopes, pre-approval

**Files:** `store.py`, `engine.py`, `planner.py`, `daemon.py`, `ops.py` (`DaemonHooks`);
tests `tests/test_store.py`, `tests/test_engine_guard.py`, `tests/test_daemon.py`.

**Produces:**

```sql
CREATE TABLE event_write (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, origin TEXT NOT NULL,  -- cli | ui
    op TEXT NOT NULL,            -- create | edit | delete | apply | dedupe
    account TEXT NOT NULL, calendar TEXT NOT NULL, event_id TEXT, title TEXT,
    detail TEXT,                 -- JSON: {"changes": [...], "count": n, "scope": "this|series", "failed": n}
    status TEXT NOT NULL         -- ok | partial | failed
);
CREATE TABLE account_access (account TEXT PRIMARY KEY, scopes TEXT NOT NULL, seen_at TEXT NOT NULL);
```

```python
Store.record_write(row: WriteRecord) -> int ; Store.writes(limit, since=None) -> list[WriteRecord]
Store.save_scopes(account, scopes: frozenset[str], at) ; Store.scopes(account) -> (frozenset, at) | None
@dataclass(frozen=True) class Approval: fingerprint: str | None; origin: str; covers: frozenset[str] = frozenset()
DaemonHooks.preapprove(rule: str, object_ids: frozenset[str], origin: Source) -> None
```

- [ ] Store tests: fresh schema 6 equals an upgraded 5 (`test_upgraded_schema_equals_a_fresh_one`);
  the upgrade keeps `run`, `change`, `config_change`; `event_write` retention (newest 500).
- [ ] Guard tests: planned deletes ⊆ covers → no block, an `approved` change row with origin;
  covers ∪ 12 other deletes → the guard judges the 12 alone (blocks: max_count 10); covers that
  name objects not planned are ignored; a pre-approval is consumed by the cycle that performs
  its deletes and expires after 24 h.
- [ ] Engine: `check_delete_guard` gets the plan minus covered deletes; the rest as today.
- [ ] Daemon: `preapprove` merges object ids per rule; the next cycle of that rule gets them;
  after a cycle, `account_access` is updated from each Google adapter's `scopes`.
- [ ] Commit `feat(store): write history, account scopes; pre-approved deletes`.

### Task 3: create, edit, delete

**Files:** create `src/calsync/event_ops.py`; modify `ops.py` (registry), `events_view.py`
(`event_target(id)`: account, calendar, source id, original start); tests `tests/test_event_ops.py`.

**Arguments (JSON / CLI):**

- `event.create`: `account`, `calendar` (id or name), `title`, `start`, `end`, `all_day`,
  `location`, `description`, `alarms` (`"default"`, `""`, `"-10m,0m"` like rules, or a list of
  minutes), `wait`.
- `event.edit`: `id` (calsync's event id), `scope` (`this` | `series`, required for an instance),
  `expect` (an etag) or `expect_updated` (the `updated` the caller saw), the fields to change, `wait`.
- `event.delete`: `id`, `scope`, `expect` / `expect_updated`, `wait`.

**Behaviour:**

- Resolve the event from the mirror (`events_view`); unknown → `ConfigError` ("no such event").
- Refuse before any request: the calendar's cached role is `reader`/`freeBusyReader` →
  "read-only calendar"; `organizer.self` false → "an invitation: only its organiser can change
  it"; `account_access` known and without `calendar.events` → `needs_human`.
- Build a Google writer for the account from the stored credential (`wiring`), under the
  daemon's cycle lock, each request bounded by `WRITE_TIMEOUT = 60.0`.
- GET live (`instance()` for scope `this`, else the master); `expect` ≠ live `etag`, or
  `expect_updated` ≠ live `updated` → `WriteConflict`. Dry run: return `{"before": …, "after": …, "changes": [...]}` and stop.
- Write; on `NoWriteAccess` → `needs_human` (and save the scopes seen); on 412 → conflict.
- Delete: pre-approve the copies: `object_id(rule, calendar_id, source_id)` for every rule whose
  source is this calendar (a series delete: the series' object; an instance delete changes the
  series object, nothing to pre-approve).
- Record an `event_write` row; request cycles for the affected rules (`wait`: wait and return
  each rule's result).
- Result `data`: `{"event": {...live after...}, "event_id": <calsync id>, "rules": [...], "changes": [...]}`.

- [ ] Tests with a fake writer: each refusal makes no request; conflict on etag; dry run makes
  no write; scope `this` uses `instance()` with the instance's original start; scope `series`
  patches the master; a timed edit keeps the event's time zone; delete pre-approves exactly the
  copy ids; the history row and the cycle request; `needs_human` on missing scope.
- [ ] Commit `feat(ops): create, edit and delete events in Google`.

### Task 4: batch apply and duplicates

**Files:** create `src/calsync/duplicates.py`; extend `event_ops.py`; tests
`tests/test_duplicates.py`, `tests/test_event_apply.py`.

**`events.apply`** — `account`, `calendar`, `events`: a list of items, each
`{"key": "...", "delete": false, …EventFields…}`; `prune_prefix` (optional): delete events
whose key starts with it and are not in the list.

- Find the keyed events in the mirror (`extendedProperties.private.calsync_key`, fetched since
  Task 1); before writing, pull the calendar once so the mirror is current.
- Plan per item: create (no event with the key), update (fields differ), unchanged, delete
  (`delete: true`, or pruned). Dry run returns the plan with counts.
- Write in batches of 50; per-item result `ok | failed (reason)`; a re-run after a partial
  failure only does what is left (the key is the identity).
- Pre-approve the copies of every delete; one `event_write` row (`op = apply`, counts in detail);
  request the cycle once.

**Duplicates** (`duplicates.py`, pure):

```python
@dataclass(frozen=True) class DuplicateGroup:
    account: str; calendar: str; title: str; start: str; end: str
    keep: str               # source id kept by default
    remove: tuple[str, ...]
def find_duplicates(raw_events: Iterable[RawEvent], tz: str) -> list[DuplicateGroup]
```

- Duplicates: same calendar, not cancelled, same normalised title (trimmed, inner whitespace
  collapsed, case-folded), same start and end (all-day: same dates; timed: same instant), and
  the same recurrence (both single, or the same `RRULE`/`EXDATE` lines). Instances of a series
  are never compared one by one.
- Keep: the one whose copy is already delivered (fewest changes in iCloud); otherwise the oldest
  `created`; otherwise the smallest id. Description, location and alarms of the others are not
  merged (a person picks another to keep if needed).
- `GET /api/duplicates` / `calsync duplicates`: groups for every source calendar a rule syncs.
- `duplicates.remove`: `groups` as `[{"keep": id, "remove": [ids]}]` (from the view; the op
  re-checks each against the mirror and refuses ids that are not duplicates of `keep`), or
  `all: true`. Pulls the calendar first, re-checks every id against the fresh mirror, then
  deletes in batches without `If-Match` (the mirror keeps no etag, and a GET per event would
  double the requests; the fresh re-check stands in for it). Pre-approves copies, records
  one `event_write` row (`op = dedupe`), requests the cycle.

- [ ] Tests: titles differing in case/whitespace group; different end or recurrence does not;
  keep rule order; apply plan (create/update/unchanged/delete/prune) and idempotent re-run;
  batch partial failure; remove refuses a non-duplicate id.
- [ ] Commit `feat(ops): batch apply with idempotent keys, duplicates and their removal`.

### Task 5: API, CLI, views

**Files:** `api.py`, `cli.py`, `views.py`, `events_view.py`, `docs/agent-cli.md`; tests
`tests/test_api_events_write.py`, `tests/test_cli_events.py`.

- [ ] Event detail gains `editable: {"ok": bool, "reason": null | "read_only_calendar" |
  "invitation" | "no_write_access" | "no_source_rule", "command": null | "calsync auth X --write"}`
  and `etag: null` (the mirror has no etag; `updated` is sent as `expect_updated` instead and
  the op compares it with the live `updated`). Status: each Google account gains `can_write:
  true | false | null` (null: not seen yet).
- [ ] `GET /api/duplicates`, `GET /api/writes?limit` (the history, newest first).
- [ ] CLI: `calsync event new --account A --calendar C --title … --start … --end … [--all-day]
  [--location] [--alarms]`, `calsync event edit <id> [--this|--series] [fields]`, `calsync event
  delete <id> [--this|--series]`, `calsync event apply <file.json|->`, `calsync duplicates
  [--remove [--all]]`, `calsync writes`; all with `--dry-run`, `--json`, `--wait`. Exit codes as
  today; conflict → exit 1 with `"kind": "conflict"`.
- [ ] `docs/agent-cli.md`: the write loop for agents: write → `trigger --wait` (automatic) →
  `events --json` to check delivery; batch file format; duplicates.
- [ ] Commit `feat(cli,api): event writes, duplicates and the write history`.

### Task 6: web UI — edit and delete in the event detail

**Files:** create `web/static/forms/edit.js`; modify `forms/event.js`, `app.js`, `i18n.js`,
`styles.css`; tests `tests/browser/test_edit.py`; the browser fake Google source gains a writer.

- [ ] The detail's footer: **修改** and **删除** (danger, quiet) when `editable.ok`; otherwise
  one line why ("这是只读日历" / "这是别人邀请你的日程，只有组织者能改" / "还没开写权限" with the
  command to copy).
- [ ] `#/event-edit/<id>`: title, all-day switch, date + start/end time (native inputs, 16 px),
  location, reminder (日历默认 / 不提醒 / list of minutes), description. A recurring instance asks
  first: **只改这一次 / 改整个系列** (radio rows). Save → dry run is not needed (the form shows the
  change list itself); the op runs with `expect_updated`; then the inspector returns to the
  detail, which shows 等待送达 → 已送达 as the cycle runs.
- [ ] Conflict: "这条日程刚在 Google 里被改过" with **重新载入**. `needs_human`: the command.
- [ ] Delete: a confirmation sheet naming the calendar and, for a series, the scope choice; then
  the detail closes and the calendar reloads.
- [ ] Browser tests (Chromium, WebKit): edit title and time of a single event; edit one
  instance vs the series; conflict path; read-only calendar shows no buttons; delete; phone
  viewport tap targets and overflow (`test_layout.py` gains the new routes).
- [ ] Commit `feat(web): change and delete events from the detail`.

### Task 7: web UI — new event

**Files:** `calendar.js`, `forms/edit.js`, `app.js`; tests `tests/browser/test_new_event.py`.

- [ ] **新建日程** in the calendar toolbar (an icon button with a name on the phone), and a click on
  an empty hour of the day/week grid prefills that hour. `#/event-new?start=…`.
- [ ] Source calendar picker: Google calendars that a rule syncs and that are writable, shown with
  the service mark and account (`CalendarName`); remembered per browser (localStorage, guarded).
- [ ] Default: the next half hour, one hour long, the calendar's default reminders.
- [ ] After saving, the new event appears in the calendar at once (reload after the op) with
  等待送达, then 已送达.
- [ ] Commit `feat(web): create events`.

### Task 8: web UI — duplicates and the write history

**Files:** create `forms/duplicates.js`; modify `sentence.js`/`app.js` (attention card),
`activity.js`; tests `tests/browser/test_duplicates.py`, `tests/browser/test_activity.py`.

- [ ] Attention card when any synced calendar has duplicates: "家庭日程 里有 12 组重复日程（多了
  31 个）" → `#/duplicates`.
- [ ] Inspector: groups by calendar; each group: title, time, how many, which one stays (radio
  rows; default the API's `keep`, marked 已送达 if its copy is delivered); **删除 31 个重复** →
  confirm → progress → result ("已删除 31 个，iCloud 副本下次同步时一起删除").
- [ ] Activity: write rows ("在 calsync 里修改了 · 时间 10:00 → 09:30", "批量写入 330 个",
  "删除了 31 个重复") with origin 界面 / 命令行.
- [ ] Commit `feat(web): duplicates and the write history`.

### Task 9: docs, full verification, review, merge

- [ ] DESIGN.md principle 6 → "Configure, repair and write originals; never edit copies";
  components for the form; README; ROADMAP (C done; "this and following", drag: later).
- [ ] Full suite: `uv run ruff check`, `ruff format --check`, `pyright`, `pytest` (unit), browser
  suite. Screenshots at phone and desktop width, light and dark.
- [ ] One Opus subagent reviews the whole branch (`model: "opus"`), with the list of deferred
  minors from the ledger; fix its Critical/Important findings; merge to main locally (no push).

## Decisions for the user

1. Scope as above (create, edit, delete, batch, duplicates in; "this and following", drag,
   attendees, batch form in the UI out).
2. Duplicate = same calendar, title (ignoring case and spaces), start, end and repetition. Kept by
   default: the one already delivered to iCloud, else the oldest. Nothing is removed without a
   person or agent confirming the list.
3. Batch writes are for agents (CLI/API, a JSON file with a key per event); the UI only shows the
   result in the activity timeline.
4. Deletes asked for in calsync release their own iCloud copy deletes (pre-approval); any other
   deletes in the same cycle still meet the guard.
