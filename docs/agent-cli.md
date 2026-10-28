# calsync for AI agents

This guide is for an AI agent (or a script) that drives calsync on a person's
behalf: it writes events into Google Calendar with its own Google tool, then
uses the `calsync` command line to make sure they reached the CalDAV calendar
(iCloud, …) that the family reads.

The command line is the agent interface. Every command an agent uses takes
`--json` and prints one JSON document with stable keys (`config get` always
does); `config check`, `auth`, `run`, `daemon` and `web` have no `--json`
(they are for people). Every command that changes the configuration also
takes `--dry-run`. The HTTP API the daemon serves is for
the web UI and the CLI itself: do not script against it. There is no MCP
server.

All examples below are real output shapes with synthetic data. Keys may be
added in later versions; existing keys keep their meaning.

---

## Contents

- [Ground rules](#ground-rules)
- [The loop](#the-loop)
- [Exit codes and errors](#exit-codes-and-errors)
- [When a person is needed (`needs_human`)](#when-a-person-is-needed-needs_human)
- [Writing events through calsync](#writing-events-through-calsync)
- [Approving blocked deletes](#approving-blocked-deletes)
- [Changing the configuration](#changing-the-configuration)
- [Pausing a rule](#pausing-a-rule)
- [Commands](#commands)
- [Event ids, dates and time zones](#event-ids-dates-and-time-zones)
- [Privacy](#privacy)

## Ground rules

- **calsync is one way.** It copies Google → CalDAV. Write, move and delete
  events in Google; never in the target calendar (calsync reverts edits made
  there on its next cycle).
- **Never handle a password, token or app-specific password.** No calsync
  command takes one. When a credential is missing or expired, calsync tells
  you which command a person has to run (see
  [`needs_human`](#when-a-person-is-needed-needs_human)); relay it.
- **Never edit `~/.config/calsync/config.toml` by hand** while the daemon
  runs, and never touch the state directory (`~/.local/state/calsync`). Use
  `calsync config set`, `rule …`, `account …`, `pause` / `resume`: they
  validate, refuse a conflicting write, keep a backup and record who changed
  what.
- **Do not run `calsync auth`, `calsync service install|uninstall`,
  `calsync web reset-key` or `calsync account remove --delete-credential`**
  unless the person explicitly asks you to. They involve the person's
  credentials or how calsync runs on their machine.
- **Pass `--json`** and read the exit code. Text output is for people and may
  change.
- **Never change the delete guard to get past a block.** Raising a blocked
  rule's `delete_guard.max_count` or `max_ratio` lets its deletes through
  without the person seeing them; calsync records and pushes it, but the
  way past a block is `blocked` + `approve` (see
  [Approving blocked deletes](#approving-blocked-deletes)).
- **Never use `calsync run --allow-deletes`; use `blocked` + `approve`.** It
  lets a cycle through the guard without showing anyone the set (it is for a
  person at the terminal); calsync records and pushes it as an approval from
  `run`, but you would not have checked what it deleted.

## The loop

The everyday job: put something in the calendar and confirm that it arrived.

1. **Write to Google** into the source calendar of a rule (`calsync status
   --json` lists each rule's `source` and `target`): with calsync's own
   commands ([Writing events through calsync](#writing-events-through-calsync):
   one event, or many in one idempotent batch), or with your own Google
   Calendar tool. calsync's commands request the sync themselves.
2. **Sync now and wait for the result:**

   ```sh
   calsync trigger --wait --json            # every rule that is not paused
   calsync trigger --rule family --wait --json
   ```

   With a running daemon, this asks it for a cycle and waits (at most
   `--timeout` seconds, default 120, at most 600) for the first cycle that
   starts after the request. Without a daemon, the cycle runs in this process.
   The answer is the same either way:

   ```json
   {
     "op": "trigger",
     "changed": true,
     "summary": "no daemon runs, so the cycle ran here: family ok",
     "diff": "",
     "data": {
       "ran": true,
       "rules": {
         "family": {"status": "ok", "created": 1, "updated": 0, "deleted": 0, "failed": 0, "error_kind": null}
       },
       "requests": 5
     },
     "needs_human": false,
     "command": null,
     "reason": null
   }
   ```

   Check `data.rules.<rule>.status` (`ok`, `partial`, `failed`, `blocked`)
   and `error_kind` (`auth`, `config`, `guard`, `other`, or `null`). Exit 0
   means every rule that ran is `ok`. A `blocked` rule (`error_kind:
   "guard"`, exit 1) means the mass-delete guard held deletes back: see
   [Approving blocked deletes](#approving-blocked-deletes). An `auth` failure
   (exit 3) needs a person: `calsync status --json` names the account and
   the command in `attention`.

3. **Check each event's delivery** for the days you wrote:

   ```sh
   calsync events --from 2026-11-03 --to 2026-11-05 --json
   ```

   Find your event (by `title` and `start`) and read `deliveries[].state`:
   `delivered` means the copy in the target calendar is what calsync would
   write now. See [`events`](#events) for the other states and what to do
   about each.

If an event you wrote is missing from `events`, check that you wrote it
into the rule's source calendar and that the rule is not paused (a paused
rule does not read its source), then trigger once more.

## Exit codes and errors

| Exit | Meaning | What to do |
| --- | --- | --- |
| 0 | ok | — |
| 1 | failed: a rule failed or was blocked, a network or server error, a refused trigger (paused rule), `stale` approval, no such event, an event that changed in Google since you read it (`event_conflict`), no state database yet, lost contact with the daemon, a test push too soon | read the JSON; retry later for transient errors |
| 2 | configuration error, a write refused by validation, a config conflict, bad arguments, `approve` without `--yes`, nothing to approve | fix the arguments; for a conflict, read again and redo the change |
| 3 | authentication: an account's credential is missing, expired or revoked | relay the `calsync auth ACCOUNT` command to a person |
| 4 | another calsync holds the lock (a `calsync run` in progress), or another daemon serves this state directory | wait and retry; never kill it |

With several rules, the most actionable code wins: 3, then 2, then 1.

A command that could not run at all prints an error object instead of its
usual JSON (with `--json`; read commands too, also for an invalid config
file):

```json
{"error": {"kind": "config", "message": "runtime.interval: invalid duration 'soon'; expected e.g. 30s, 5m, 2h, 7d, -15m", "problems": ["runtime.interval: invalid duration 'soon'; expected e.g. 30s, 5m, 2h, 7d, -15m"]}}
```

| `error.kind` | Exit | Meaning |
| --- | --- | --- |
| `config` | 2 | invalid arguments or configuration; `problems` lists every problem. Also `trigger --rule R --wait` for an unknown rule when a daemon runs the cycle (exit 2, like `unknown_rule`) |
| `conflict` | 2 | the config file changed between calsync reading and writing it ("the file was changed outside calsync; reload and try again"): run the command again |
| `locked` | 4 | another calsync process holds the run lock |
| `daemon_lost` | 1 | the daemon accepted the operation and went away before answering: the change **may have been applied**. Check `calsync config history --json` before you retry |
| `paused` | 1 | `trigger --rule R` for a paused rule |
| `read_only` | 1 | `trigger` through a daemon started with `--no-sync` (it runs no cycles) |
| `unavailable` | 1 | `trigger` through a daemon that is stopping |
| `too_soon` | 1 | `notify test` through the daemon while one is being sent, or less than 10 seconds after the last one that reached a channel |
| `unknown_rule` | 2 | `--rule R` names no configured rule (`events`, `blocked`, `upcoming`, `trigger`) |
| `unknown_target`, `bad_request` | 2 | `events` arguments (a range longer than 62 days, or more than 10 years from today, is `bad_request`) |
| `not_found` | 1 | `event show`: no such event (now) |
| `no_state` | 1 | no state database yet (calsync never ran): run a cycle (`calsync trigger --wait`) |
| `state_unreadable` | 1 | the state database could not be read (not a database, corrupt, locked) |
| `auth` | 3 | `calendars --refresh`: the account's credential failed (`command` / `reason` say what a person runs) |
| `other` | 1 | `calendars --refresh`: the list could not be fetched (a network or server error) |
| `error` | 1 | anything else |

Without `--json`, the same errors are one line of text (for example `no
state yet: run \`calsync run\` or start the daemon`).

## When a person is needed (`needs_human`)

calsync never asks you for a secret. When something needs a person — signing
in to Google in a browser, typing an iCloud app-specific password — it tells
you the exact command to give them. It appears in three places:

- **Operation results** (`account add`, `rule add --preview`, …) have three
  top-level keys:

  ```json
  {
    "op": "account.add",
    "changed": true,
    "summary": "account \"kid-icloud\" added",
    "diff": "…",
    "data": {},
    "needs_human": true,
    "command": "calsync auth kid-icloud",
    "reason": "a person signs in (Google) or types the app-specific password (iCloud)"
  }
  ```

  `needs_human` is `false` (and `command` / `reason` are `null`) when no
  person is needed.

- **`calendars --refresh`** that fails for a credential puts them in the
  error: `{"error": {"kind": "auth", "message": "…", "command": "calsync auth
  me-google", "reason": "…"}}` (exit 3).

- **`status --json`** lists what needs a person in `attention`, each with a
  `command` when there is one to run:

  ```json
  "attention": [
    {"kind": "auth", "rule": "family", "account": "me-google",
     "message": "The me-google account needs to be connected again.", "command": "calsync auth me-google"},
    {"kind": "blocked", "rule": "family", "account": null,
     "message": "12 copies would be deleted — check `calsync blocked --rule family`, then approve if intended",
     "command": "calsync approve family"},
    {"kind": "failing", "rule": "work", "account": null,
     "message": "Sync has failed 3 times in a row.", "command": null}
  ]
  ```

**Relay, do not act.** Tell the person, in one or two sentences, what is
wrong and the command to run on the machine where calsync runs, for example:
"calsync can no longer read your Google calendar. Please run `calsync auth
me-google` on the Mac mini and sign in." Do not try to run `calsync auth`
yourself: it opens a browser or reads a password from the terminal.

A `blocked` attention is the exception: an agent may approve it, carefully
(next section).

## Writing events through calsync

calsync can write Google's originals itself, once the person has given it
write access (`calsync auth <account> --write`, a consent page: a
`needs_human` result names the command). It stays one way: the copies follow
at the next cycle, which every write requests (`--wait` waits for it and
answers with it in `data.cycle`, shaped like `trigger`'s `data`).

| Command | What it does |
| --- | --- |
| `calsync event new --account A --calendar C --title T --start S --end E [--all-day] [--location L] [--description D] [--alarms X] [--repeat RRULE…] [--key K]` | one new event (not idempotent: for writes you may repeat, use `event apply` with keys) |
| `calsync event edit ID [--this \| --series] [--expect ETAG \| --expect-updated TIME] [fields…]` | change one event, one occurrence or the whole series |
| `calsync event delete ID [--this \| --series] [--expect… ]` | delete it |
| `calsync event apply FILE\|- [--account A] [--calendar C] [--prune-prefix P]` | many writes in batches of 50, idempotent |
| `calsync writes` | the history of writes made through calsync |

All take `--json`, `--dry-run` (reads Google, writes nothing) and `--wait`.

- **Times.** `2026-10-25` is a date (all day; an all-day event's `--end` is
  the day after its last day), `2026-10-25T09:30` is in the calendar's zone,
  or give an offset (`2026-10-25T09:30+08:00`).
- **Reminders** (`--alarms`, `"alarms"`): `default` (the calendar's), `none`,
  minutes before the start (`10,0` or `[10, 0]`), or offsets as rules write
  them (`-10m,0m`). At most 5, at most four weeks before.
- **Repeating events.** An `ID` from `events --json` names one occurrence.
  `--this` changes or deletes only it; `--series` all of them (a new time for
  the occurrence moves the whole series by the same offset, on the wall clock
  of the event's zone). One of the two is required for a repeating event. A
  series whose rule fixes its days (`BYDAY=MO`, …) can move in time but not
  to another day (exit 2). "This and following" is not offered.
- **Conflicts.** Pass what you read: `--expect-updated` with the event's
  `updated` (for `--series`, its `series_updated`) or `--expect` with an
  etag. If the event changed in Google since, nothing is written: exit 1,
  `"kind": "event_conflict"`. Read it again and decide.
- **Refused before any request** (exit 2): a read-only calendar, an
  invitation (only its organiser may change it: `editable.reason` in
  `events --json` says so in advance), bad fields.
- **Deletes and the guard.** Deletes made through the CLI meet the
  mass-delete guard like any other: past 10 copies (or half a calendar) the
  rule is blocked until someone [approves](#approving-blocked-deletes) the
  set. Only a delete a person makes in the web UI releases its own copy.
- Notifications to attendees are never sent (`sendUpdates=none`).

### Batches: `event apply`

The file is a list of items, or `{"account", "calendar", "events": [...],
"prune_prefix", "tz"}` (flags override the file). Each item names its event:

- by `"key"`: your own identity for it (stored in the event's private
  extended properties). calsync creates it if no event has the key, updates
  it if a field differs, else leaves it (`unchanged`);
- or by `"id"`: an existing event from `events --json` (update or delete
  only). The id of one occurrence of a repeating event needs `"scope":
  "series"` and then names the whole series (no new times for a series here:
  use `event edit --series`); one occurrence alone is `event edit` / `event
  delete --this`. `"expect_updated"` (the `updated` you read) makes the item
  fail with `"conflict"` if the event changed since.

Items naming an invitation fail with `"invitation"`. New events get an id of
calsync's choosing, so a batch resent after a lost answer does not make them
twice.

`"delete": true` deletes (an absent key is `absent`, not an error).
`prune_prefix` also deletes the keyed events whose key starts with it and
that the list does not name: the way to replace a whole plan.

```json
{"account": "me-google", "calendar": "Family", "prune_prefix": "eye-2026-11/",
 "events": [
   {"key": "eye-2026-11/001", "title": "Eye drops (1/84)", "start": "2026-11-13T08:00", "end": "2026-11-13T08:05", "alarms": [0]},
   {"key": "eye-2026-11/002", "title": "Eye drops (2/84)", "start": "2026-11-13T12:00", "end": "2026-11-13T12:05", "alarms": [0]}
 ]}
```

A bad item refuses the whole call before any write (`problems` names
`events[i]`). The answer counts `create`, `update`, `delete`, `unchanged`,
`absent`, `failed` and lists every item with its `action`, `status` (`ok`,
`failed`) and `reason`. Running the same file again does only what is left:
after a partial failure, just run it again. At most 2000 items per call.

### Recipe: removing duplicates

calsync has no duplicates command; compose one from the commands above.

1. List the days concerned: `calsync events --from … --to … --json` (62 days
   at most per call).
2. Group the events of one calendar (`source.account`, `source.calendar`)
   that have the same title (ignoring case and repeated spaces), `start`,
   `end` and `repeats`. Count each repeating series once (by `source_id`).
3. In each group keep one: the one whose `deliveries[].state` is
   `delivered`, else the oldest `created`.
4. **Show the groups to the person and get their yes.**
5. Delete the rest in one call: `event apply` with `{"id": …, "delete":
   true, "expect_updated": …}` items (for a duplicated repeating series, its
   occurrence's id with `"scope": "series"`).
6. If more than 10 copies go, the rule is blocked: `calsync blocked --json`
   shows the set; it should be exactly the duplicates' copies. Then
   `calsync approve <rule>` (or ask the person to press 放行 in the web UI).

## Approving blocked deletes

The mass-delete guard blocks a rule's cycle when it would delete more than
`delete_guard.max_count` copies (default 10) or, from 3 deletions on, more
than `max_ratio` (default half) of the rule's copies. A blocked rule makes no
changes at all until someone decides. Agents may approve. Every approval
that releases a set is recorded in the change history (`calsync changes`:
action `approved`, `object_id` the fingerprint, `title` such as `12 deletes
approved (cli)`) and sent to the person as a notification when a push
channel is configured. Both come from the cycle that releases the set: an
approval that goes `stale`, `failed` or `synced` (or is dropped because the
rule was paused or removed, or the daemon stopped) records and pushes
nothing; a set that blocks again is pushed as blocked again.

1. **Read the blocked set:**

   ```sh
   calsync blocked --rule family --json
   ```

   ```json
   {
     "blocked": [
       {
         "rule": "family",
         "since": "2026-11-02T08:00:12+00:00",
         "count": 12,
         "copies": 12,
         "ratio": 1.0,
         "limit": {"max_count": 10, "max_ratio": 0.5},
         "reason": "plan deletes 12 events, more than max_count=10",
         "fingerprint": "62e91e30f08a3b04",
         "items": [
           {"object_id": "96993b307db0a7aea4863717", "title": "Swimming lesson"},
           {"object_id": "cb27894a9a30615a76695acc", "title": "Piano"}
         ]
       }
     ]
   }
   ```

   The top-level key is `blocked` (an empty list: nothing is blocked).
   `title` may be `null` when calsync could not read it.

2. **Check that these are the events the person asked to delete.** Compare
   the titles (and the count) with what you deleted in Google. If anything is
   unexpected — a calendar that suddenly looks empty, titles you did not
   touch — **do not approve**: tell the person what calsync would delete and
   let them decide. A source calendar that was renamed, unshared or emptied by
   mistake looks exactly like this.

3. **Approve exactly that set:**

   ```sh
   calsync approve family --fingerprint 62e91e30f08a3b04 --yes --json
   ```

   Always pass `--fingerprint` with the value you checked: the approval then
   releases that set and nothing else. Without a terminal (and always with
   `--json`) `--yes` is required; without it calsync exits 2.

   ```json
   {
     "op": "approve",
     "changed": true,
     "summary": "rule \"family\": 12 copies deleted",
     "diff": "",
     "data": {"rule": "family", "status": "approved", "fingerprint": "62e91e30f08a3b04", "count": 12, "deleted": 12},
     "needs_human": false, "command": null, "reason": null
   }
   ```

| `data.status` | Exit | Meaning |
| --- | --- | --- |
| `approved` | 0 | the cycle that followed deleted the set |
| `pending` | 0 | through the daemon without `--wait`: approved for its next cycle (requested at once). Add `--wait` to wait for that cycle |
| `would_approve` | 0 | `--dry-run`: nothing approved |
| `stale` | 1 | the blocked set is different now (`fingerprint` and `count` give the new one; `approved_fingerprint` the one you sent). Nothing was deleted. Go back to step 1 |
| `failed` | 1 | the approval cycle failed; nothing more was approved |
| `synced` | 0 | the guard no longer held anything back when the cycle ran (the set shrank): the rule synced as usual, `deleted` says how many copies went; the approval was not needed (nothing recorded as approved) |
| `not_blocked` | 2 | nothing to approve |

A paused rule cannot be approved (exit 2): resume it first. Without a daemon,
`approve` runs the rule's cycle itself. A later, different set blocks again
and needs its own approval.

## Changing the configuration

Every change goes through one validated, conflict-checked write that keeps
comments and formatting, backs the old file up as `config.toml.bak` and is
recorded in `calsync config history`. The daemon picks up changes at once
(hot reload); there is nothing to restart, except for `[web] listen`.

1. **Dry run first** and show the person the `diff` if the change matters:

   ```sh
   calsync config set runtime.interval 2m --dry-run --json
   ```

   ```json
   {
     "op": "config.set",
     "changed": false,
     "summary": "dry run, nothing written: runtime.interval set",
     "diff": "--- config.toml\n+++ config.toml\n@@ -15,3 +15,6 @@\n…\n+[runtime]\n+interval = \"2m\"\n",
     "data": {"would_change": true},
     "needs_human": false, "command": null, "reason": null
   }
   ```

2. **Write it:** the same command without `--dry-run`. `changed: false` means
   the file already said that (nothing written, nothing recorded).
3. **Undo** the newest change not yet undone, if needed:
   `calsync config undo --dry-run --json`, then `calsync config undo --json`.
   An undo is itself recorded (`undoes`: the id it undid).

A `conflict` error means the file changed between the read and the write
(someone edited it by hand, or the daemon removed a retired rule): run the
command again; it reads the file anew. The `diff` shows the file's lines as
they are, URLs, usernames and credential references included: treat it like
the configuration itself and do not forward it.

## Pausing a rule

```sh
calsync pause family --json      # no cycle syncs it
calsync resume family --json
```

A paused rule does nothing: its source is not read and its target is not
written, so events you add in Google meanwhile do not show up in `events`
until it is resumed. `trigger --rule family` is refused (exit 1, `kind:
"paused"`); a plain `trigger` skips it (and answers `{"ran": false,
"reason": "paused"}`, exit 0, when every rule is paused). Its health in
`status` is `paused` and its deliveries show `paused`.

## Commands

The global options `--config PATH` and `--state-dir DIR` go before the
command (defaults: `$CALSYNC_CONFIG` or `~/.config/calsync/config.toml`;
`$CALSYNC_STATE_DIR` or `~/.local/state/calsync`).

Write commands run through the daemon when it runs (between its cycles), and
otherwise in this process while holding the run lock. Read commands read the
state database directly; they work with or without a daemon.

### Operation results

Every write command (`config set|undo`, `rule add|set|remove`, `pause`,
`resume`, `account add|remove`, `approve`, `trigger`, `notify test`)
answers with the same envelope (`calendars --refresh` answers as
[`calendars`](#calendars)):

| Key | Meaning |
| --- | --- |
| `op` | the operation, e.g. `rule.add`, `config.set`, `trigger` |
| `changed` | whether it changed anything |
| `summary` | one sentence for a person (config names only, never a value) |
| `diff` | the config file's unified diff (`""` when there is none) |
| `data` | operation-specific results (below); a dry run adds `would_change` |
| `needs_human`, `command`, `reason` | see [above](#when-a-person-is-needed-needs_human) |

### Reading the state

#### `status`

`calsync status --json`: health per rule, accounts, what needs attention,
and the running daemon.

```json
{
  "generated_at": "2026-11-02T08:01:00+00:00",
  "schema": {"version": 6, "current": true},
  "summary": {"health": "ok", "rules": 1, "attention": 0, "last_sync_at": "2026-11-02T08:00:41+00:00",
              "changes_24h": 3, "delivered_today": 3},
  "accounts": [
    {"name": "me-google", "provider": "google", "host": "Google Calendar", "auth": "ok",
     "calendars": [{"id": "family@group.calendar.google.com", "name": "Family", "role": "owner",
                    "series": 41, "rules": ["family"], "synced": true}]},
    {"name": "me-icloud", "provider": "caldav", "host": "caldav.icloud.com", "auth": "ok", "calendars": []}
  ],
  "rules": [
    {
      "name": "family",
      "source": {"account": "me-google", "calendar": "Family", "calendar_name": "Family"},
      "target": {"account": "me-icloud", "calendar": "Family", "calendar_name": "Family"},
      "scope": {"kind": "all"},
      "alarms": "copy",
      "fields": {"attendees": "notes", "conference": "copy", "content": "full"},
      "health": "ok",
      "last_run": {"id": 812, "rule": "family", "status": "ok", "created": 1, "updated": 0, "deleted": 0,
                   "failed": 0, "reverted": 0, "protected": 0, "out_of_window": 0, "requests": 5,
                   "started_at": "…", "finished_at": "…", "duration_ms": 640,
                   "error": null, "error_kind": null, "error_account": null},
      "last_ok_at": "2026-11-02T08:00:41+00:00",
      "consecutive_failures": 0,
      "copies": 41,
      "out_of_window": 0,
      "protected": 0
    }
  ],
  "attention": [],
  "daemon": {"running": true, "sync": true, "pid": 4242, "started_at": "…", "last_cycle_at": "…",
             "next_cycle_at": "…", "cycles": 96, "cycle_started_at": null}
}
```

- `health`: `ok`, `failing`, `blocked`, `auth`, `never_run`, `paused`,
  `retiring` (the summary's is the worst). `daemon` is `null` when no daemon
  runs.
- `accounts[].auth`: `ok`, `failed` or `unknown`; `calendars` comes from the
  cached calendar list (`role` is Google's access role; `null` for CalDAV).
- `copies` counts every copy recorded for the rule, also in a calendar it
  targeted before; a retiring rule finishes when none is left in its current
  target.
- `error` texts never contain URLs or titles; `error_account` names the
  account whose credential or setup failed.

Exit 0.

#### `events`

`calsync events [--from DATE] [--to DATE] [--rule R] [--target ACCOUNT:CALENDAR] [--query TEXT] --json`:
the source events in a range, each with its delivery per rule. `--from`
defaults to today, `--to` (exclusive) to `--from` + 7 days; at most 62 days.
`--query` matches the title or location (case-insensitive). A recurring
series appears once per instance.

```json
{
  "from": "2026-11-03T00:00:00+08:00",
  "to": "2026-11-05T00:00:00+08:00",
  "schema": {"version": 6, "current": true},
  "truncated": false,
  "events": [
    {
      "source_id": "abc123",
      "updated": "2026-11-01T09:12:00.000Z",
      "series_updated": null,
      "created": "2026-11-01T09:12:00.000Z",
      "key": null,
      "repeats": null,
      "editable": {"ok": true, "reason": null, "write_access": true, "command": null},
      "id": "bWUtZ29vZ2xlL2ZhbWlseUBncm91cC5jYWxlbmRhci5nb29nbGUuY29tL2FiYzEyMw",
      "title": "Swimming lesson",
      "start": "2026-11-03T16:00:00+08:00",
      "end": "2026-11-03T17:00:00+08:00",
      "all_day": false,
      "location": null,
      "recurring": false,
      "alarms_minutes_before": [30],
      "source": {"account": "me-google", "provider": "google", "calendar": "Family", "calendar_name": "Family"},
      "deliveries": [
        {
          "rule": "family",
          "target": {"account": "me-icloud", "provider": "caldav", "calendar": "Family", "calendar_name": "Family"},
          "state": "delivered",
          "delivered_at": "2026-11-02T08:00:41+00:00",
          "content": "full",
          "alarms_minutes_before": [30]
        }
      ]
    }
  ]
}
```

`deliveries[].state` — the first that applies, in this order:

| State | Meaning | What to do |
| --- | --- | --- |
| `unknown` | the state database comes from another calsync version | run a cycle |
| `paused` | the rule is paused | resume it if the person wants it synced |
| `retiring` | the rule is being removed with its copies | nothing |
| `out_of_window` | outside the rule's `window`: never copied | nothing (or widen the window) |
| `delivered` | the copy is what calsync would write now | nothing |
| `blocked` | not delivered, and the rule is blocked by the delete guard | [approve](#approving-blocked-deletes) or ask the person |
| `failing` | not delivered, and the rule's latest run failed (auth too) | `status --json` → `attention` |
| `pending` | not written yet | `trigger --wait`, then look again |

The source facts: `source_id` (Google's id of the event or series),
`updated` (of this occurrence; `series_updated`: of its series), `created`,
`key` (from `event apply`), `repeats` (the recurrence lines, or `null`) and
`editable`: `ok` false with a `reason` (`read_only_calendar`, `invitation`)
when calsync may not change it; `write_access` false (with the `command` a
person runs) when the account's grant has no write scope, `null` when not
known yet.

`delivered_at` is the last time calsync wrote the copy (`null`: never
recorded); it stays set while the rule is paused. `alarms_minutes_before` on
a delivery is what the copy carries after the rule's alarm policy.
`truncated: true` means a very dense recurring series was cut short; narrow
the range. Exit 0; 2 for bad arguments.

#### `event show`

`calsync event show ID --json`: one event (an `id` from `events --json`),
with each delivery's `copy` (what calsync last wrote, from its records;
`null` when nothing is recorded), `differs` (when the state is not
`delivered`: the fields in which that copy differs from what calsync would
write now) and the `original` (the source event). Exit 1 with `kind:
"not_found"` when the id names no current event.

```json
{
  "id": "bWUtZ29vZ2xlL2ZhbWlseUBncm91cC5jYWxlbmRhci5nb29nbGUuY29tL2FiYzEyMw",
  "title": "Swimming lesson",
  "start": "2026-11-03T16:00:00+08:00",
  "end": "2026-11-03T17:00:00+08:00",
  "all_day": false, "location": null, "recurring": false, "alarms_minutes_before": [30],
  "source": {"account": "me-google", "provider": "google", "calendar": "Family", "calendar_name": "Family"},
  "deliveries": [
    {
      "rule": "family",
      "target": {"account": "me-icloud", "provider": "caldav", "calendar": "Family", "calendar_name": "Family"},
      "state": "delivered", "delivered_at": "2026-11-02T08:00:41+00:00", "content": "full",
      "alarms_minutes_before": [30],
      "copy": {"title": "Swimming lesson", "start": "2026-11-03T16:00:00+08:00", "end": "2026-11-03T17:00:00+08:00",
               "all_day": false, "location": null, "recurring": false, "alarms_minutes_before": [30]},
      "differs": []
    }
  ],
  "original": {"title": "Swimming lesson", "start": "2026-11-03T16:00:00+08:00", "end": "2026-11-03T17:00:00+08:00",
               "all_day": false, "location": null, "alarms_minutes_before": [30], "recurring": false}
}
```

#### `upcoming`

`calsync upcoming [--hours N] [--rule R] --json`: events starting in the
next N hours (default 24) with their alarms and a simpler delivery state
(`delivered`, `pending`, `unknown`). Each event: `rule`, `start`, `end`,
`all_day`, `title`, `location`, `alarms_minutes_before`, `recurring`,
`target_calendar`, `delivery`. For checking specific events prefer `events`.

#### `runs` and `changes`

- `calsync runs [--rule R] [--limit N] --json` → `{"runs": [...]}`, newest
  first, each as `status`'s `last_run`.
- `calsync changes [--rule R] [--since TIME] [--limit N] --json` →
  `{"changes": [...]}`, newest first (kept 90 days):

  ```json
  {"id": 38, "run_id": 6, "rule": "family", "object_id": "2dac63a60fac16f514a67be0", "action": "updated",
   "at": "2026-11-02T08:00:41+00:00", "title": "Swimming lesson",
   "detail": {"fields": {"start": ["2026-11-03T15:00:00+08:00", "2026-11-03T16:00:00+08:00"],
                         "end": ["2026-11-03T16:00:00+08:00", "2026-11-03T17:00:00+08:00"]}}}
  ```

  `action`: `created`, `updated`, `reverted` (an edit on the target written
  back), `deleted`, `protected` (a source series calsync cannot parse: its
  copy is kept), `blocked`, `approved` (an approval, or `calsync run
  --allow-deletes`, released a set the guard holds back: `object_id` is its
  fingerprint, `title` e.g. `12 deletes approved (cli)`, origin `run` for
  `--allow-deletes`)
  and `guard_loosened` (a config write raised a blocked rule's delete guard:
  `object_id` is `guard`, `title` e.g. `delete guard loosened (ui)`). The
  last two are records of a decision, not of a copy. `detail` (or `null`)
  lists the fields that changed as `[before, after]`. `--since` keeps the
  changes recorded at or after that time, whatever their order.

Both exit 0; `--rule` may name a rule removed since.

#### `blocked`

See [Approving blocked deletes](#approving-blocked-deletes). Read-only, exit
0.

#### `calendars`

`calsync calendars ACCOUNT [--refresh] --json`: the account's calendars from
calsync's cache (the daemon refreshes it at start and periodically;
`--refresh` fetches it now, through the daemon if it runs). Use `id` or
`name` in `rule add --source/--target`.

```json
{
  "account": "me-google",
  "fetched_at": "2026-11-02T07:58:10+00:00",
  "calendars": [{"id": "family@group.calendar.google.com", "name": "Family", "role": "owner"}]
}
```

CalDAV calendars have their URL as `id` and `role: null`. Exit 2 for an
unknown account; a failed refresh exits by its kind (3 auth, 2 config, 1
other).

#### `config get` and `config history`

- `calsync config get --json` prints the parsed configuration (always JSON).
  URLs are cut to scheme and host and accounts show only `name`, `provider`,
  `host` and `credential_configured`. `--reveal` shows them in full (the
  same keys plus `server_url`, `username`, `credential`, …): do not use it
  unless the person asks, and never forward its output.
- `calsync config history [--limit N] --json` → `{"changes": [{"id", "at",
  "source", "summary", "after_fingerprint", "undoes"}]}`, newest first (200
  kept). `source` is `cli`, `ui`, `daemon` (it removed a retired rule) or
  `external` (a hand edit the daemon picked up).

### Syncing

#### `trigger`

`calsync trigger [--rule R] [--wait] [--timeout S] --json` — see
[The loop](#the-loop). Through a daemon without `--wait` it only asks for a
cycle: `data` is `{"accepted": true, "rule": "family"}` (exit 0). With
`--wait`, `data` is the cycle's result, or `{"ran": false, "reason":
"timeout"}` / `{"ran": false, "reason": "cycle_failed"}` (exit 1). The exit
code of a cycle that ran follows `calsync run`. `--dry-run` is refused (use
`calsync run --dry-run` to see a plan; it needs the lock, so it only works
without a daemon).

Do not use `calsync run` while the daemon runs: it exits 4. `trigger` works
either way.

### Rules

#### `rule add`

```sh
calsync rule add NAME --source ACCOUNT:CALENDAR --target ACCOUNT:CALENDAR \
    [--create-target] [--window all|PAST:FUTURE] [--alarms copy|strip|-10m,0m] \
    [--content full|busy] [--attendees none|notes] [--conference copy|none] [--retire] [--preview] [--dry-run] --json
```

`CALENDAR` is a name or an id from `calendars`. Write negative alarm offsets
with `=`: `--alarms=-10m,0m`. `--window 30d:365d` keeps only events from 30
days back to a year ahead (default: everything). `--create-target` creates
the target calendar if it does not exist.

`--preview` reads the source and target now and writes no config (a
preview may refresh calsync's cached calendar list):

```json
{
  "op": "rule.preview",
  "changed": false,
  "summary": "would create 41 series in \"Family\" (calendar not found; add --create-target to create it)",
  "diff": "",
  "data": {"series": 41, "instances_in_window": null, "target_exists": false,
           "target_would_be_created": false, "source_mirrored": true,
           "leftover": [{"name": "family-old", "copies": 12}]},
  "needs_human": false, "command": null, "reason": null
}
```

An account it cannot reach is reported in `data` (`error`, `error_kind`,
`error_account`) and sets the exit code (3 auth, 2 config, 1 other). Check
the preview with the person before you add a rule that would create many
copies.

If copies left by an earlier rule of the same name are recorded, `rule add`
says so in its `summary` and in `data.earlier_copies` (a count): the new
rule takes those copies over and deletes the ones its source does not have
(through the delete guard). Tell the person before you go on.

The preview's `data.leftover` lists copies in the target calendar left by
rules that are gone (removed with their copies kept), each with the earlier
rule's `name` (null when the history does not know it) and `copies`. A new
rule of another name writes its own copies beside them: duplicates. Ask the
person which they want, then either take the copies over (add the rule under
that earlier `name`) or clean them up first: `rule add <earlier name>
--source … --target … --retire` adds a rule that only deletes those copies
(through the delete guard) and then disappears; add the new rule after it is
gone.

#### `rule set`

`calsync rule set NAME KEY VALUE [--dry-run] --json`. Keys: `window`
(`all` or `PAST:FUTURE`), `alarms` (`copy`, `strip` or offsets), `paused`,
`retire` (`true`/`false`), `fields.content`, `fields.attendees`,
`fields.conference`, `delete_guard.max_count`, `delete_guard.max_ratio`. An
empty VALUE removes the key (back to its default). A value starting with `-`
needs `--` before the positional arguments:
`calsync rule set --json -- family alarms -10m,0m`. Changing `alarms` or
`fields` rewrites the affected copies once, on the next cycle.

#### `rule remove`

`calsync rule remove NAME [--delete-copies] [--dry-run] --json`. Without
`--delete-copies` the rule goes and its copies stay in the target calendar,
untouched until a rule of the same name is added again (which takes them
over, see `rule add`). With it, the rule is marked `retire = true`: the next
cycles delete all its copies, also those outside its `window` (through the
delete guard, so a large calendar blocks and needs an
[approval](#approving-blocked-deletes)), and then remove the rule from the
config (`config history` shows `source: "daemon"`). Its health is
`retiring` meanwhile. Do not change a retiring rule's target: it would
retire against the new calendar and leave the copies in the old one.

#### `pause` / `resume`

See [Pausing a rule](#pausing-a-rule). `--dry-run` shows the diff.

### Accounts

- `calsync account add NAME --provider google|caldav [--server-url icloud|URL] [--username LOGIN] [--dry-run] --json`
  adds the account with a keychain credential reference; a person then has to
  run `calsync auth NAME` (the result says so in `needs_human`).
- `calsync account remove NAME [--delete-credential] [--dry-run] --json`
  refuses (exit 2) while a rule uses the account. `data.credential` says
  what became of its credential: `kept`, `deleted`, `failed` (the keychain
  refused; the summary says what to remove by hand), `manual` (an `env` or
  `file` credential, which a person removes) or `none`. Leave
  `--delete-credential` to the person.

### Configuration

- `calsync config set KEY VALUE [--dry-run] --json` — keys:
  `google.client_secret_file`, `notify.bark_url`, `notify.webhook_url`,
  `notify.public_url`, `notify.failure_threshold`, `notify.language` (`zh`
  or `en`), `runtime.interval` (e.g. `2m`), `runtime.jitter`,
  `web.allowed_hosts` (comma-separated). An empty VALUE removes the setting.
  Push and webhook URLs are secrets of a kind (a Bark URL contains the
  device key): set them only with values the person gave you, and do not
  repeat them back.
- `calsync config undo [--dry-run] --json` — `data.undoes` is the id undone.
- `calsync config check` — validates the file (text; exit 2 with every
  problem listed).

### Notifications

`calsync notify test --json` sends a test push to every configured channel:

```json
{"op": "notify.test", "changed": false, "summary": "test push: bark ok, webhook not configured", "diff": "",
 "data": {"channels": {"bark": "ok", "webhook": "not configured"}},
 "needs_human": false, "command": null, "reason": null}
```

Each channel is `ok`, `failed (<ErrorType>)` or `not configured`. Exit 0 when
every configured channel took it, 1 when one failed, 2 when none is
configured. Through the daemon, a test push while one is being sent, or within
10 seconds of the last one that reached a channel, is refused (`too_soon`,
exit 1); a refused request or a test whose every channel failed does not
count.

### The service

`calsync service status --json` (read-only) → `{"installed", "loaded",
"state", "pid", "last_exit", "plist", "log", "others", "warnings"}`. `others`
lists other launchd jobs that run calsync (they would compete for the lock).
Installing or removing the service is the person's decision.

## Event ids, dates and time zones

- **Event ids are opaque.** Take them from `events --json` and pass them to
  `event show` unchanged; do not build or parse them. An instance of a
  recurring series has its own id. An id stops working when its event is
  gone (or moved, for an instance).
- **Dates are the machine's local days.** `--from 2026-11-03` means local
  midnight on the computer calsync runs on, not your time zone or UTC. Give
  a full time with an offset (`2026-11-03T00:00+08:00`) when the machine's
  zone might differ from the person's. A time without an offset is local
  time too, also for `changes --since`.
- **Output times** are ISO 8601 with an offset: event times in the event's
  own zone, record times (`at`, `started_at`, …) in UTC. An all-day event has
  `all_day: true` and plain dates (`"start": "2026-11-05", "end":
  "2026-11-06"`, the end exclusive).

## Privacy

- `--json` output contains event titles, locations and the calendar names.
  It goes only to your standard output — calsync never logs titles — but **if
  you are a cloud-hosted agent, everything you read leaves the person's
  machine with your context.** Read only what the task needs (`--rule`,
  `--query`, a narrow `--from`/`--to`) and do not quote calendar contents
  into places the person did not ask for.
- Event ids encode the source calendar's id (often an e-mail address). Treat
  them like the titles. Rule sources and targets (`status`, `config get`,
  `config history` summaries, operation summaries) may be calendar ids too,
  that is addresses.
- Notifications (Bark, webhook) carry titles too: a blocked-deletes push
  names up to five of them. They pass through Bark's servers and Apple's
  push service, or the webhook's server.
- Never print or store passwords, tokens or the web UI's pairing key, and
  never ask the person to give you one. `calsync web pair` prints a link that
  grants access to the web UI: do not run it for the person unless asked, and
  do not forward the link. A paired browser can change the configuration
  much as the CLI can (not credentials): it could point the push and webhook
  URLs elsewhere.
