# Block ② completion — control operations (phase A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything the v4 concept's buttons need exists as one operation layer, reachable from the CLI (for agents) and the daemon's JSON API (for the GUI): safe config writes with history and undo, rule and account editing, pause/resume, approving blocked deletes, a date-range events query with delivery state, `trigger --wait`, calendar-list caching, copy/field summaries, Bark/webhook notifications and `calsync service`.

**Architecture:** A new `calsync.ops` module holds every state-changing operation as a plain function over an `OpContext` (config path, state dir, a writable `Store`, daemon hooks). Two transports call it: in-process (the CLI holds the run lock while it runs one operation) and the daemon (the CLI posts `POST /api/ops/<name>` over the unix socket; the GUI posts the same over TCP behind the existing pairing guard). Read-only views keep reading the state DB directly. Config writes go through `calsync.configfile` (tomlkit, five steps of spec §4.4); the daemon hot-reloads on its own writes and on external edits.

**Tech Stack:** Python 3.12+, asyncio, SQLite (WAL), httpx, Starlette/uvicorn, tomlkit (new dependency, named by the spec), pytest, ruff, pyright.

**Inputs:** v2 spec `docs/superpowers/specs/2026-10-24-calsync-v2-product-design.md` (§1.3, §4.2–4.6, §6, §7); feasibility `docs/superpowers/specs/2026-10-27-concept-v4-feasibility.md` (§2, §3, §6, §7); v3 spec `docs/superpowers/specs/2026-10-23-calsync-design.md` (§11 alerts, §12 exit codes); `AGENTS.md` (invariants — read it first).

## Global Constraints

- **Privacy (AGENTS invariant 7):** no event titles, URLs, hrefs, calendar IDs, response bodies or secrets in log lines. Log the exception type and config names only; details through `debug.details(...)` (CALSYNC_DEBUG=1). Titles may go to stdout, the JSON API and the 0600 state DB.
- **Exit codes (v3 §12):** 0 ok, 1 failed, 2 config (also: validation refused, config conflict, bad arguments), 3 auth, 4 another calsync holds the lock / daemon state conflict. Constants `EXIT_*` in `cli.py`.
- **Every command has `--json`** with a stable shape; every state-changing command also has `--dry-run` (validate and show the config diff / plan; write nothing).
- **One operation layer** (spec §1.3): the CLI and the API call the same `ops` functions; validation lives in one place (`config.parse_config`).
- **D10:** no operation takes a password, token or app-specific password. Anything that needs a human returns `{"needs_human": true, "command": "<runnable command>", "reason": "<one sentence>"}` (JSON) / prints the command (text) and exits 0 for a read, 2 for a write that could not proceed.
- **D12 / §4.4 config writes:** tomlkit round-trip keeps comments and formatting; validate the whole new text with `parse_config`; refuse if the file's fingerprint changed since it was read ("the file was changed outside calsync; reload and try again"); copy to `config.toml.bak`; write a temp file in the same directory with the original's mode, fsync, `os.replace`; record who/what/when.
- **State DB:** only the run-lock holder writes it (the daemon, `calsync run`, or the CLI while it holds the lock for one operation). Schema bumps are additive in-place migrations (follow `_upgrade_3` in `store.py`); `run`, `change` and `config_change` are never dropped.
- **Deletion safety:** the mass-delete guard stays on for every path. An approval releases exactly the blocked set it names (by fingerprint); a different set blocks again.
- **Agents may approve** blocked deletes (user decision 2026-10-27): `approve` works non-interactively with `--yes`; every approval that releases a set sends a notification and is recorded with its origin (`cli`, `ui`), both from the cycle that releases it (wording amended in the final review: an approval that goes stale or fails sends nothing).
- **Tests:** synthetic data only (no real names, e-mail addresses, Apple IDs, calendar IDs). Use fakes and temp dirs; never touch `~/.config/calsync`, launchd or real accounts. `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check src tests`, `uv run pyright` all clean at the end of every task.
- **Language:** code, CLI output and docs in English (as today). The GUI's Chinese strings come in phase B.
- Commit after each task with the repository's message style and the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

## File map

| File | Responsibility |
| --- | --- |
| `src/calsync/configfile.py` (new) | Read with fingerprint; tomlkit edits; validated, conflict-checked, atomic, backed-up write; history rows |
| `src/calsync/ops.py` (new) | `OpContext`, the operations, their result dataclasses (`to_json`) |
| `src/calsync/opsclient.py` (new) | Run an operation in-process (with the lock) or through the daemon's socket |
| `src/calsync/notify.py` (new) | Alert evaluation (dedup in the `alert` table) and Bark/webhook delivery |
| `src/calsync/service.py` (new) | launchd plist rendering, install/uninstall/status |
| `src/calsync/config.py` | `Rule.paused`, `Rule.retire`, `NotifyConfig.public_url` |
| `src/calsync/store.py` | schema v5 (migration from v4): `target_object.summary`, `change.detail`, `account_calendar`, `config_change` |
| `src/calsync/engine.py` | approvals by fingerprint, retire (desired = empty), summaries and field details, skip paused |
| `src/calsync/daemon.py` | hot reload, op execution, `request_cycle(..., wait)`, notifications, calendar refresh |
| `src/calsync/views.py` | `build_events`, `build_event`, `build_blocked`, status additions |
| `src/calsync/api.py` | `GET /api/events`, `/api/event`, `/api/blocked`, `/api/config`, `/api/config/history`, `/api/calendars`; `POST /api/ops/<name>` |
| `src/calsync/cli.py` | the new commands (below) |
| `README.md`, `AGENTS.md`, `docs/agent-cli.md` (new) | user and agent documentation |

New CLI surface (all with `--json`; writers with `--dry-run`):

```
calsync config get [--reveal]                 calsync config set KEY VALUE
calsync config history [--limit N]            calsync config undo
calsync rule add NAME --source ACCOUNT:CALENDAR --target ACCOUNT:CALENDAR [--create-target]
                 [--window all|PAST:FUTURE] [--alarms copy|strip|-10m,0m] [--content full|busy]
                 [--attendees none|notes] [--conference copy|none] [--preview]
calsync rule set NAME KEY VALUE               calsync rule remove NAME [--delete-copies]
calsync pause RULE                            calsync resume RULE
calsync account add NAME --provider google|caldav [--server-url URL|icloud] [--username U]
calsync account remove NAME [--delete-credential]
calsync blocked [--rule RULE]                 calsync approve RULE [--fingerprint F] [--yes] [--wait]
calsync events --from DATE --to DATE [--rule R] [--target ACCOUNT:CALENDAR] [--query TEXT]
calsync event show ID                         calsync trigger [--rule R] [--wait] [--timeout S]
calsync calendars ACCOUNT [--refresh]         calsync notify test
calsync service install|uninstall|status [--listen HOST:PORT]
```

---

### Task 1: Config model additions and the safe config writer

**Files:** modify `src/calsync/config.py`, `pyproject.toml` (add `tomlkit>=0.13`); create `src/calsync/configfile.py`; tests `tests/test_configfile.py`, extend `tests/test_config.py`.

**Interfaces — produces:**
```python
# config.py
@dataclass(frozen=True, slots=True)
class Rule:  # new fields, defaults keep every existing config valid
    ...
    paused: bool = False          # TOML: paused = true
    retire: bool = False          # TOML: retire = true  (remove the rule once its copies are gone)
@dataclass(frozen=True, slots=True)
class NotifyConfig:
    ...
    public_url: str | None = None # links in notifications point here

# configfile.py
@dataclass(frozen=True, slots=True)
class Snapshot:
    path: Path
    text: str
    fingerprint: str              # sha256 hex of the file bytes; "" when the file does not exist
def read_snapshot(path: Path) -> Snapshot
class ConfigConflict(ConfigError)  # problems = ["the file was changed outside calsync; reload and try again"]
def validate_text(text: str, base_dir: Path) -> Config        # tomllib + parse_config; ConfigError on problems
def write_config(snapshot: Snapshot, new_text: str) -> Snapshot
    # 1 validate_text  2 fingerprint check (re-read now; differs -> ConfigConflict)
    # 3 copy current bytes to <path>.bak (mode 0600)  4 temp file in same dir, same mode as the
    # original (0600 for a new file), write, flush, fsync, os.replace, fsync the directory
    # returns the new Snapshot. new_text == snapshot.text -> no write, returns snapshot.
def diff_text(old: str, new: str, name: str = "config.toml") -> str            # unified diff
```
tomlkit editing helpers in `configfile.py` (pure functions over a `tomlkit.TOMLDocument`, each raising `ConfigError([...])` with a precise message for an unknown rule/account/key or a bad value):
```python
def load_doc(text: str) -> tomlkit.TOMLDocument
def rule_table(doc, name) -> tomlkit.items.Table                 # the [[rules]] entry with that name
def set_rule_key(doc, name, key: str, value) -> None             # dotted key: "window", "alarms",
    # "fields.content", "fields.attendees", "fields.conference", "delete_guard.max_count",
    # "delete_guard.max_ratio", "paused", "retire". value None removes the key (e.g. window -> all)
def add_rule(doc, table: dict) -> None                           # appends a [[rules]] entry
def remove_rule(doc, name) -> None
def add_account(doc, name, table: dict) -> None                  # [accounts.NAME]
def remove_account(doc, name) -> None
def set_key(doc, dotted: str, value) -> None                     # "notify.bark_url", "notify.webhook_url",
    # "notify.public_url", "notify.failure_threshold", "runtime.interval", "runtime.jitter",
    # "web.allowed_hosts"; any other key -> ConfigError("unknown setting ...")
```

- [ ] **Step 1: failing tests** in `tests/test_configfile.py` (all on temp files with synthetic content):
  - comments and key order survive `set_rule_key` + `write_config` (assert a `# comment` line and the original order are in the output);
  - a value that fails validation (`delete_guard.max_ratio = 1.5`) raises `ConfigError` naming `rules[NAME].delete_guard.max_ratio`, and the file on disk is byte-for-byte unchanged;
  - external change between `read_snapshot` and `write_config` raises `ConfigConflict`, file unchanged;
  - `.bak` holds the previous bytes; the new file keeps mode 0600; no temp file is left behind (also when `os.replace` is monkeypatched to raise);
  - `write_config` with identical text performs no write (mtime unchanged);
  - `set_rule_key(doc, "r", "window", None)` removes the `window` table; setting `"window"` to `{"past": "30d", "future": "365d"}` writes an inline or standard table that `parse_config` reads back as `WindowSpec(30d, 365d)`;
  - `add_rule`/`remove_rule`/`add_account`/`remove_account` round-trip through `parse_config`;
  - unknown rule / unknown key -> `ConfigError` with the exact message.
  In `tests/test_config.py`: `paused`/`retire` default False, parse `true`, reject non-bool with `rules[NAME].paused: must be true or false`; `notify.public_url` must be an http(s) URL.
- [ ] **Step 2:** run them, see them fail.
- [ ] **Step 3:** implement. Keep `config.py` free of tomlkit (it parses with `tomllib`); only `configfile.py` imports tomlkit.
- [ ] **Step 4:** tests pass; full suite, ruff, pyright clean.
- [ ] **Step 5:** commit `feat(config): safe config writes with tomlkit, paused/retire rules, notify.public_url`.

### Task 2: Schema v5 — config history, calendar cache, copy summaries, change details

**Files:** modify `src/calsync/store.py`, `src/calsync/views.py` (reader accessors); tests `tests/test_store.py` (migration), `tests/test_views.py` (accessors).

**Interfaces — produces:**
```sql
-- added by the v4 -> v5 step (and in the fresh schema)
ALTER TABLE target_object ADD COLUMN summary TEXT;     -- JSON of what calsync wrote (see below)
ALTER TABLE change ADD COLUMN detail TEXT;             -- JSON {"fields": {"<name>": [old, new]}}
ALTER TABLE run ADD COLUMN error_account TEXT;         -- the account whose failure stopped the rule (Task 10 fills it)
CREATE TABLE account_calendar (
    account TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
    role TEXT, fetched_at TEXT NOT NULL,               -- role: Google accessRole / CalDAV "rw"|"ro"
    PRIMARY KEY (account, id));
CREATE TABLE config_change (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, source TEXT NOT NULL,  -- cli|ui|daemon|external
    summary TEXT NOT NULL, before TEXT, after_fingerprint TEXT NOT NULL, undoes INTEGER);
CREATE INDEX config_change_at ON config_change (at);
```
`summary` JSON: `{"title": str|null, "start": iso, "end": iso, "all_day": bool, "location": str|null, "alarms_minutes_before": [int], "recurring": bool}` (the shaped series as written; for a recurring series the master's first instance).

```python
# store.py
def record_config_change(self, *, at, source, summary, before: str | None, after_fingerprint, undoes: int | None = None) -> int
def config_changes(self, limit: int = 50) -> list[ConfigChangeRecord]       # newest first
def save_account_calendars(self, account: str, calendars: Sequence[AccountCalendar], at: datetime) -> None  # replaces the account's rows
# retention: keep the newest 200 config_change rows
# views.StateReader (read-only twins)
def config_changes(self, limit: int = 50) -> list[ConfigChangeView]
def account_calendars(self, account: str) -> list[AccountCalendarView]
def copy_summary(self, tag: str, object_id: str) -> dict | None
def delivered_at(self, rule: str, object_id: str) -> datetime | None          # latest create/update/revert change
def series_count(self, account: str, calendar: str) -> int                    # rows in source_event
```
- [ ] **Step 1: failing tests:** a v4 DB (build it from the v4 DDL copied from `git show HEAD:src/calsync/store.py` into the test as a string) with rows in every table migrates in place to v5 keeping all rows; the new columns are NULL; the new tables exist with the same shape as a fresh v5 DB (compare `PRAGMA table_info` and index lists); v3 -> v5 runs both steps; accessors return what was written; `config_change` retention keeps 200.
- [ ] **Step 2–4:** implement the `_upgrade_4` step in one transaction (it drops each table it creates first — `DROP TABLE IF EXISTS account_calendar`, `config_change` — per the store docstring's rollback rule; an `ADD COLUMN` that already exists after a rollback cannot happen because the older code rebuilds those tables), extend the fresh DDL, bump `SCHEMA_VERSION` to 5; `schema_current()` in views accepts 5.
- [ ] **Step 5:** commit `feat(store): schema v5 — config history, calendar cache, copy summaries, change details`.

### Task 3: The operation layer and its two transports (config, rules, accounts, pause)

**Files:** create `src/calsync/ops.py`, `src/calsync/opsclient.py`; modify `src/calsync/cli.py`, `src/calsync/api.py` (socket route only in this task); tests `tests/test_ops.py`, `tests/test_cli_ops.py`.

**Interfaces — produces:**
```python
# ops.py
@dataclass
class OpContext:
    config_path: Path
    state_dir: Path
    store: Store                         # writable; the caller holds the run lock
    source: Literal["cli", "ui", "daemon"]
    clock: Callable[[], datetime]
    on_config_written: Callable[[Snapshot], None] = lambda s: None   # the daemon reloads here
    daemon: "DaemonHooks | None" = None  # request_cycle/wait, approvals, adapters (Task 5, 6)

@dataclass(frozen=True)
class OpResult:
    op: str; changed: bool; summary: str           # one plain sentence, e.g. 'rule "family" paused'
    diff: str = ""                                 # unified config diff ("" when nothing changed)
    data: dict[str, Any] = field(default_factory=dict)
    needs_human: dict[str, str] | None = None      # {"command": ..., "reason": ...}
    def to_json(self) -> dict[str, Any]

OPS: dict[str, Callable[[OpContext, dict[str, Any], bool], OpResult]]   # name -> fn(ctx, args, dry_run)
# names in this task: "config.set", "config.undo", "rule.add", "rule.set", "rule.remove",
# "rule.pause", "rule.resume", "account.add", "account.remove"
def run_op(ctx: OpContext, name: str, args: dict[str, Any], dry_run: bool) -> OpResult
    # unknown name -> ConfigError; args validated per op (types, required keys) -> ConfigError
    # a config-changing op: read_snapshot -> edit doc -> new text -> (dry_run: return diff)
    #   -> write_config -> store.record_config_change(source=ctx.source, summary=..., before=old text,
    #   after_fingerprint=new.fingerprint) -> ctx.on_config_written(new)

# opsclient.py
def call(name: str, args: dict, *, dry_run: bool, config_path: Path, state_dir: Path) -> OpResult
    # daemon socket alive (connect to wiring.socket_path(state_dir) succeeds): POST /api/ops/<name>
    #   {"args": ..., "dry_run": ...} over the unix socket (httpx with a UDS transport) -> OpResult
    # otherwise: take RunLock (AlreadyRunning -> exit 4 as today), open Store, run_op in-process

# daemon.py — the one mutual-exclusion point inside the daemon (Tasks 4–8 inherit it)
self._cycle_lock: asyncio.Lock
    # held by: _cycle (the whole cycle, including recording), op execution (run_op and its
    # on_config_written), config apply on reload, the calendar-list refresh, the retire removal.
    # An op that arrives over the socket during a cycle waits for it; nothing swaps the engine,
    # the adapters or the config under a running cycle, and only one writer touches the Store.
```
Operation semantics:
- `rule.add`: args `name, source "ACCOUNT:CALENDAR", target "ACCOUNT:CALENDAR", create_target, window, alarms, content, attendees, conference`. Unknown accounts or wrong providers come back as the `parse_config` problems. A duplicate (same source and target as an existing rule) -> `ConfigError(['a rule already syncs "<src>" to "<tgt>": "<name>"'])`.
- `rule.set`: `name, key, value` with the keys of `set_rule_key`; values as CLI text: window `all` or `30d:365d`; alarms `copy`, `strip` or `-10m,0m`; booleans `true|false`; numbers.
- `rule.remove`: without `delete_copies`, removes the entry (copies stay in the target calendar; summary says so). With `delete_copies`, sets `retire = true` instead (Task 4 finishes it).
- `rule.pause` / `rule.resume`: set/remove `paused`.
- `account.add`: `name, provider, server_url, username`; writes `credential = { backend = "keychain", ref = "calsync:<name>" }`; result `needs_human = {"command": "calsync auth <name>", "reason": "a person signs in (Google) or types the app-specific password (iCloud)"}`.
- `account.remove`: refuses while a rule uses the account (message lists the rules). `delete_credential` deletes the keychain entry through `credentials` (add `delete_secret(ref)`); without it the summary says the credential was kept.
- `config.set`: the keys of `configfile.set_key`; `web.allowed_hosts` takes a comma list.
- `config.undo`: the newest `config_change` row that is not an undo and not yet undone -> write its `before` text through `write_config` (conflict check applies), record a row with `undoes = <id>`. Nothing to undo -> `changed=False`, summary "nothing to undo".

CLI: `config set/undo`, `rule add/set/remove`, `pause`, `resume`, `account add/remove` call `opsclient.call`; text output is `summary` (+ the diff with `--dry-run`, + the `needs_human` command); `--json` prints `OpResult.to_json()`. `config get [--reveal]` prints the parsed config as JSON (bark/webhook URLs masked to scheme+host unless `--reveal`). `config history` reads `StateReader.config_changes`.

API (unix socket app only in this task): `POST /api/ops/{name}` -> `run_op` inside the daemon (run between cycles, never during one) with `source="cli"`. In this task the daemon's `on_config_written` only stores the new snapshot and logs "config changed; applied at restart"; Task 4 makes it apply the config.

- [ ] **Step 1: failing tests:** each op on a temp config (comments preserved, history row written, `--dry-run` writes nothing and returns the diff); validation and conflict errors map to exit 2 with the problems listed; `account.remove` refusal; `config.undo` restores the previous text and a second undo undoes the next older change; `opsclient.call` uses the socket when a fake daemon serves it (use the real `api.unix_socket` + a test app) and the in-process path otherwise; the in-process path exits 4 while another process holds the lock.
- [ ] **Step 2–4:** implement; `cli.py` grows one `_ops_command` helper rather than nine copies.
- [ ] **Step 5:** commit `feat(ops): one operation layer for config, rules, accounts and pause, from the CLI or the daemon`.

### Task 4: Daemon hot reload, paused and retiring rules

**Files:** modify `src/calsync/daemon.py`, `src/calsync/engine.py`, `src/calsync/views.py` (status health `paused`, `retiring`); tests `tests/test_daemon_reload.py`, extend `tests/test_engine*.py`.

**Behaviour:**
- The daemon remembers the config `Snapshot` it runs. While waiting it re-reads the file's fingerprint every 2 s (cheap `stat` first: size+mtime_ns, hash only when they change). A changed fingerprint: `validate_text`; invalid -> keep the running config, log `config reload failed (N problems)` (no text), raise the `reload_failed` alert (Task 8 sends it; until then record it); valid -> apply, and record a `config_change` row with `source="external"`, summary `"edited outside calsync"`, `before` = the previous text.
- Its own writes (ops run inside the daemon) call `on_config_written`, which applies the new config at once and updates the remembered snapshot, so the watcher never sees them as external.
- Applying a config: rules list; accounts whose `Account` value changed, were added or removed get their adapters rebuilt or closed (`_build` / `_close`); the running `Engine` gets `Engine.replace_config(config)` (new method: swaps `_config`, drops the per-rule memories `_last_blocked`/`_last_protected`/`_warned_degraded` only for rules that no longer exist or whose source/target changed, keeps mirrors, metadata and target caches), all under the cycle lock — never a fresh Engine, which would re-record every blocked/protected set and re-fetch blocked bodies (undoing review fix I-4); `web.allowed_hosts` take effect through the existing `config_provider`; a changed `web` listen address logs "restart the daemon to listen on HOST:PORT" (the value is shown — it is config, not a URL of the user's data).
- Paused rules: never selected by the scheduler; `request_cycle(rule)` for a paused rule raises `ValueError("rule X is paused")` -> API 409 `paused`; no run rows while paused. Resuming needs no special step (the next cycle plans from current state, so changes made while paused are delivered at once).
- Retiring rules (`retire = true`): the engine plans with `desired = {}` (every managed copy is a delete), the guard applies as usual (a large rule blocks and waits for `approve`). After a cycle in which the rule ends `ok` with `copies(tag) == 0`, the daemon (or `calsync run`/in-process trigger) runs `rule.remove` with `source="daemon"`, summary `rule "X" removed after its copies were deleted`.
- `status`: health `paused` for paused rules (attention none), `retiring` for retiring ones, before the run-based health.

- [ ] **Step 1: failing tests** (fake clock/sleep as in the existing daemon tests): external edit applied within one poll; invalid edit keeps the old config and records nothing; own write not recorded as external; account change rebuilds only that account; engine replaced only between cycles; paused rule skipped and `trigger` refused; retiring rule deletes copies (guard allowing) and is then removed from the file; a retiring rule over the guard blocks.
- [ ] **Step 2–4:** implement.
- [ ] **Step 5:** commit `feat(daemon): hot reload, paused rules, rules that retire after deleting their copies`.

### Task 5: Approving blocked deletes

**Files:** modify `src/calsync/engine.py`, `src/calsync/cycle.py`, `src/calsync/daemon.py`, `src/calsync/ops.py`, `src/calsync/views.py`, `src/calsync/api.py`, `src/calsync/cli.py`; tests `tests/test_approve.py`.

**Interfaces — produces:**
```python
# engine.py
def delete_fingerprint(object_ids: Iterable[str]) -> str     # sha256 of "\n".join(sorted(ids)), first 16 hex
RuleResult.blocked_fingerprint: str | None                   # set when status == "blocked"
Engine.cycle(..., approvals: Mapping[str, str] = {})          # rule -> fingerprint; a rule whose planned
    # deletes have exactly that fingerprint passes the guard this cycle; any other set blocks as today.
    # `allow_deletes=True` (calsync run --allow-deletes) keeps working.
# views.py
def build_blocked(config, reader, rule: str | None = None) -> BlockedView
    # per blocked rule (latest run status "blocked"): rule, since (first run of this block), count,
    # ratio/limit from the guard, fingerprint (from the latest set of "blocked" change rows, same
    # function), items [{object_id, title}] — all of them, in title order
# ops.py: "approve" args {rule, fingerprint?, wait?}
```
- `calsync blocked [--rule R] [--json]`: read-only.
- `calsync approve RULE [--fingerprint F] [--yes] [--wait] [--json]`: without `--fingerprint`, uses the current block's fingerprint (refuses with exit 2 if the rule is not blocked). On a tty without `--yes`, prints the count and the first 10 titles and asks the user to type the rule name; without a tty and without `--yes`: exit 2 "pass --yes to approve without a prompt". Through the daemon: the daemon stores the approval for that rule (in memory, until consumed or until the rule's block changes), requests a cycle for it and (with `--wait`) returns the cycle's result for that rule. In-process: runs one cycle for that rule with `approvals={rule: fp}`.
- Result: `approved` (the cycle deleted the set), `stale` (the block changed: new fingerprint shown, nothing deleted — exit 1), `not_blocked`.
- An approval is not a config change: the deletes are recorded as `change` rows as today, and the approval raises an `approved` alert event (Task 8) with its origin (`cli`/`ui`) and count.
- The attention card and `status` command for a blocked rule now say: `calsync approve RULE` (message: "N copies would be deleted — check `calsync blocked --rule RULE`, then approve if intended"). Remove the "stop the daemon first" advice added by the review fix.

- [ ] **Step 1: failing tests:** fingerprint equality between engine and view; approve releases exactly that set; a grown set after approval blocks again with a new fingerprint (`stale`); approval through a fake daemon socket and in-process; non-tty without `--yes` exits 2; `blocked --json` shape.
- [ ] **Step 2–4:** implement.
- [ ] **Step 5:** commit `feat: calsync blocked and approve — release exactly the blocked set`.

### Task 6: Copy summaries, change details, the calendar cache, rule preview and URL-free stored errors

**Files:** modify `src/calsync/engine.py`, `src/calsync/render.py` (a `summary_of(series)` helper), `src/calsync/sources/google.py` (`accessRole` into the calendar list), `src/calsync/targets/caldav.py` (role from privileges if already parsed, else `null`), `src/calsync/daemon.py`, `src/calsync/ops.py`; tests extend engine/daemon tests.

**Behaviour:**
- When the engine writes a copy (create/update/revert) it stores `summary_of(shaped series)` in `target_object.summary`. On update, the change row's `detail` is `{"fields": {...}}` for each of title, start, end, all_day, location, alarms_minutes_before that differs between the previous summary and the new one (old summary missing -> no detail). On revert, `detail` compares the fetched edited copy (`render.inspect`) with the summary: what the phone changed.
- Calendar cache: the daemon refreshes each account's calendar list at start and every 6 h (and on op `calendars.refresh`), storing `account_calendar` rows; a failure keeps the old rows and is logged by type. `calsync calendars ACCOUNT` prints from the cache with `fetched_at`; `--refresh` fetches now (through the daemon if it runs). JSON: `{"account", "fetched_at", "calendars": [{"id", "name", "role"}]}`.
- `role` for Google comes from `calendarList` `accessRole` (`owner`, `writer`, `reader`, `freeBusyReader`) — phase C uses it to grey out read-only calendars.

- **Preview (`rule.preview` op, `calsync rule add ... --preview`):** args as `rule.add`; nothing is written. Returns `{"series": N, "instances_in_window": M|null, "target_exists": bool, "target_would_be_created": bool, "source_mirrored": bool}`. The source count comes from `source_event` when that calendar is already mirrored; otherwise one live pull of that calendar with empty state through the source adapter (the daemon's adapters under the cycle lock, or in-process ones), discarded afterwards. The target side uses the calendar cache (refreshing it once if the calendar is not there). `rule add --preview` prints it and stops; the TCP `POST /api/ops/rule.preview` serves the GUI's new-line flow.
- **Errors without URLs in stored text:** `RuleResult.error` / `run.error` are shown by `status`, `runs` and the API, so they must not carry exception text (it can contain request URLs and calendar IDs). Store `"<ExceptionType>: <plain sentence>"` built from the error class and config names only (e.g. `"TransientError: the source of rule family could not be reached"`); keep the full text only in `debug.details`. Fill `run.error_account` (Task 2) with the account whose adapter raised.
- [ ] Tests: summary stored on create/update; detail lists exactly the changed fields; revert detail; calendar refresh writes rows and survives a failure; `calendars --json` shape; preview from the mirror and from a live pull (fake source, nothing persisted); stored errors contain no `http`, `/calendars/` or href even when the exception text does; `error_account` set for an auth failure.
- [ ] Commit `feat: copy summaries, change details, calendar cache, rule preview, URL-free stored errors`.

### Task 7a: The events query, event detail and status additions

**Files:** modify `src/calsync/views.py`, `src/calsync/api.py`, `src/calsync/cli.py`, `src/calsync/daemon.py`, `src/calsync/ops.py`; tests `tests/test_events_view.py`, extend api/cli/daemon tests.

**Interfaces — produces:**
```python
def build_events(config, reader, start: datetime, end: datetime, *, rule=None, target=None, query=None, now) -> EventsView
def build_event(config, reader, event_id: str, now) -> EventDetail      # NotFound -> 404 / exit 1
```
Events are grouped by source occurrence across rules. `id` = `"<source account>/<source calendar id>/<source_id>"` + `"@<instance start ISO>"` for recurring instances, URL-safe base64 of that string (opaque to clients). Event JSON:
```json
{"id": "...", "title": "...", "start": "...", "end": "...", "all_day": false, "location": null,
 "recurring": true, "alarms_minutes_before": [10, 0],
 "source": {"account": "personal", "provider": "google", "calendar": "<config ref>", "calendar_name": "Family"},
 "deliveries": [{"rule": "family", "target": {"account": "family-icloud", "provider": "caldav",
     "calendar": "<config ref>", "calendar_name": "Home"},
   "state": "delivered|pending|paused|blocked|failing|retiring|out_of_window|unknown",
   "delivered_at": "...|null", "content": "full|busy", "alarms_minutes_before": [10, 0]}]}
```
State rules: `paused` (rule paused), `retiring`, `out_of_window`, `blocked`/`failing` (rule's latest run blocked / failed for this rule and the copy is not up to date), otherwise `delivered` when the recorded hash equals the desired hash, else `pending`; `unknown` when the schema is not current. `build_upcoming` becomes a thin wrapper over the same per-rule code (its JSON stays as it is).
`EventDetail` adds `original` (the source fields) and per delivery `copy` = the stored summary and `differs` = the fields where the stored summary differs from the desired one (empty when delivered).
Status additions: `accounts[].calendars` from the cache with `series` (source) / `rules` (names using it) and `synced: bool`; `accounts[].auth` = `failed` when a rule using it last failed with `error_kind == "auth"`, else `ok`/`unknown`; `summary.delivered_today` (create/update/revert change rows since local midnight); `daemon` block (`next_cycle_at`, `last_cycle_at`, `cycles`) — the CLI fetches it from the socket when the daemon runs, else `null`.
API: `GET /api/events?from&to&rule&target&q` (`to - from` at most 62 days, 400 otherwise), `GET /api/event?id`, `GET /api/blocked`, `GET /api/config`, `GET /api/config/history`, `GET /api/calendars?account`, on both the socket and the TCP app. `DaemonInfo` gains `cycle_started_at: datetime | None` (set while a cycle runs, else null) so the GUI can animate a cycle in progress.
`config get` / `GET /api/config` without `--reveal` show for each account only `name`, `provider`, `host` (as `views.host_for`) and whether a credential is configured — never `username` (an Apple ID), the credential `ref`, or the full push/webhook URLs (scheme + host only). `--reveal` exists only on the CLI.
- [ ] Tests (7a): a weekly series with an exception and a cancellation expands correctly into a 2-week range; one source event with two rules yields one event with two deliveries; each delivery state; query filter on title; ids round-trip; `event show` differs; status additions; `cycle_started_at`; masking in `config get` and `GET /api/config`; API range limit.
- [ ] Commit (7a) `feat: events by date range with delivery state, event detail, status additions`.

### Task 7b: `trigger --wait` and the operations over the web API

**Files:** modify `src/calsync/daemon.py`, `src/calsync/api.py`, `src/calsync/web/app.py`, `src/calsync/cli.py`, `src/calsync/ops.py`; tests extend daemon/api/web/cli tests.

`trigger --wait [--timeout 120]`: the daemon's `request_cycle(rule, wait=True)` returns an awaitable resolved with the result of the first cycle that starts after the request and includes the rule(s); API `POST /api/ops/trigger {"rule", "wait", "timeout"}`; JSON `{"ran": true, "rules": {"<name>": {"status", "created", "updated", "deleted", "failed", "error_kind"}}, "requests": N}`; timeout -> exit 1 with `{"ran": false, "reason": "timeout"}`. Without a daemon, `trigger` runs one cycle in-process (holding the lock), same JSON.
`POST /api/ops/<name>` also on the TCP app (behind the existing guard; `source="ui"`), for every op except `account.remove` with `delete_credential` (credentials are managed from the terminal: return `needs_human` with the command).
- [ ] Tests (7b): `trigger --wait` resolves with the right cycle (requests during a cycle wait for the follow-up), times out, works in-process; TCP ops need the pairing cookie and a same-origin `Origin`; `source="ui"` recorded in `config_change`.
- [ ] Commit (7b) `feat: trigger --wait, and the operations over the web API`.

### Task 8: Notifications (Bark and webhook)

**Files:** create `src/calsync/notify.py`; modify `src/calsync/daemon.py`, `src/calsync/cycle.py` (in-process paths), `src/calsync/ops.py` (`notify.test`), `src/calsync/cli.py`; tests `tests/test_notify.py` with a fake HTTP server (httpx `MockTransport`).

**Before coding:** read Bark's current API documentation (the bark-server README / API v2 on GitHub, `Finb/bark-server`) and confirm the request shape for a POST with JSON `{"title", "body", "url", "group"}` to the device URL. Note the doc URL and date in a comment.

**Behaviour (v3 §11 + v2 §4.6):** alerts for `auth` (per account), `blocked` (per rule + fingerprint; body: count and up to 5 titles), `failing` (rule failed `failure_threshold` cycles in a row), `recovered` (first success after an open alert for that rule/account), `reload_failed` (count of problems, no text), `approved` (rule, count, origin). Dedup with the `alert` table: a key is sent once until it closes; `recovered` closes it. Each push carries `url = public_url + "#/rule/<name>"` (or `#/account/<name>`) when `public_url` is set. Bark: POST JSON to `bark_url`; webhook: POST JSON `{"event", "rule", "account", "title", "body", "url", "at"}` to `webhook_url`. A failed push logs `notification failed (Type)` and never fails a cycle; 5 s timeout. `calsync notify test` sends one "calsync test" push to every configured channel and reports per channel ok/failed.

- [ ] Tests: each trigger sends once and not again until recovered; titles appear in the push body but never in logs; failure is contained; webhook shape; `notify test`.
- [ ] Commit `feat(notify): Bark and webhook alerts with dedup`.

### Task 9: `calsync service`

**Files:** create `src/calsync/service.py`; modify `src/calsync/cli.py`; tests `tests/test_service.py` (render and command construction only; `launchctl` is injected and faked).

**Behaviour:** label `dev.calsync.daemon` (as README already documents), plist at `~/Library/LaunchAgents/dev.calsync.daemon.plist`: `ProgramArguments` = absolute path of the running `calsync` executable (`sys.argv[0]` resolved, or `shutil.which("calsync")`), `daemon`, plus `--config`/`--state-dir`/`--listen` when given; `RunAtLoad` and `KeepAlive` true; `StandardOutPath`/`StandardErrorPath` `~/Library/Logs/calsync/daemon.log`; `ExitTimeOut` 120; `EnvironmentVariables` with `PATH`. `install`: refuses to overwrite an existing plist that differs unless `--force`; lists other loaded launchd jobs whose label or program contains "calsync" and warns that two schedulers would compete (it does not touch them); `launchctl bootstrap gui/<uid> <plist>`; `--dry-run` prints the plist and the commands. `uninstall`: `launchctl bootout gui/<uid>/dev.calsync.daemon`, remove the plist. `status`: loaded or not, pid, last exit status (parsed from `launchctl print`), JSON too.

- [ ] Tests: plist content (plistlib round-trip), refusal without `--force`, the warning for another calsync job, commands passed to the fake launchctl, `--dry-run` writes nothing.
- [ ] Commit `feat: calsync service install/uninstall/status for launchd`.

### Task 10: Documentation for people and agents; backlog minors

**Files:** `README.md`, `AGENTS.md`, create `docs/agent-cli.md`, `docs/ROADMAP.md`; small fixes in `src/calsync/views.py`, `src/calsync/daemon.py`, `src/calsync/api.py`.

- `docs/agent-cli.md` (spec §1.3): the agent loop (write to Google → `calsync trigger --wait --json` → `calsync events --from --to --json`), every command with its JSON shape and exit codes, the `needs_human` pattern, approving blocked deletes (`blocked` → check → `approve --yes`), and the privacy note (titles leave the machine if the output is given to a cloud agent).
- README: the new commands, `calsync service`, hot reload, config history/undo, notifications.
- ROADMAP: block ② done; next is block ③ (GUI per concept v4) and its last part (editing events).
- Backlog minors from the final review, now cheap: M-5 (rebuild adapters after a `config` error too), M-6 (the auth attention uses `run.error_account`, filled since Task 6, instead of guessing from the error text), M-10 (`build_changes` stops scanning past `since`), `--listen` help wording, a startup warning when `web.key` is unreadable or has a wrong mode.
- [ ] Tests for the code items; docs reviewed against the actual `--help`.
- [ ] Commit `docs: agent CLI guide, README and roadmap for block ②; small fixes from the final review`.
