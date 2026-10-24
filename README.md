# calsync

**One-way calendar sync that you host yourself.** calsync keeps a CalDAV
calendar (iCloud, iCloud China, Radicale, …) continuously in step with a
Google Calendar — events, recurrence, per-instance changes and alerts — and
never touches anything it did not create.

```
Google Calendar ──► calsync (your machine) ──► iCloud / any CalDAV ──► iPhone, Mac, …
   you write here       incremental, safe         people read here
```

> **Status: early.** The sync engine and the `calsync` command line are
> complete and tested; a background service, alerts and a web UI are next
> (see [Roadmap](docs/ROADMAP.md)). Today you run `calsync run` yourself or
> from a scheduler.

---

## Contents

- [Why](#why)
- [Features](#features)
- [How it works](#how-it-works)
- [What calsync writes into your calendars](#what-calsync-writes-into-your-calendars)
- [Quick start](#quick-start)
- [Upgrading](#upgrading)
- [Configuration](#configuration)
- [Command line](#command-line)
- [Safety guarantees](#safety-guarantees)
- [Privacy and security](#privacy-and-security)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

## Why

Google calendars can only be shared with Google accounts and iCloud calendars
only with Apple IDs, so a calendar you maintain in Google — for example
through an AI assistant connected to Google Calendar — cannot simply appear on
a family member's iPhone. ICS subscription links are read-only, refresh
slowly and drop alerts.

calsync bridges the gap one way: keep writing in Google, and the events show
up, with their alerts, in an iCloud calendar that you can share or that lives
directly in the other person's account.

## Features

| | |
| --- | --- |
| **Sources** | Google Calendar (official REST API, incremental `syncToken` sync) |
| **Targets** | iCloud — global and China mainland, detected automatically · Radicale · other CalDAV servers (standards-based; untested so far) |
| **What is synced** | title, description (HTML converted to text), location, start/end with time zones, all-day events, recurrence rules, exceptions and per-instance changes, cancelled instances, free/busy, **alerts** (pop-up reminders), conference links, and the attendee list as text in the notes |
| **What is never synced** | attendees and organizer as real participants — so a target server can never send invitations on your behalf; Google e-mail reminders |
| **Full sync by default** | every event, past and future; optionally limit a rule to a time window such as the last 7 days and the next 180 days |
| **Edits on the target are reverted** | an event changed on the iPhone (or any CalDAV client) is written back to the Google version on the next cycle, and a deleted copy is re-created — sync stays strictly one way |
| **Field policy per rule** | attendees listed in the notes or left out, conference link copied or left out, or a busy-only mode that writes just "Busy", the times and the alerts (see [`fields`](#rules)) |
| **Alert policy per rule** | copy Google's alerts, strip them, or replace them with fixed offsets such as `["-5m", "0m"]` |
| **Many-to-many** | any number of accounts and rules; one source can feed several targets |
| **Cheap when idle** | an unchanged cycle costs one request per Google account, one per source calendar and one per target calendar — enforced by the test suite |
| **Safe by construction** | only touches events it created, mass-delete guard, keeps the last good copy when source data is malformed (see [Safety guarantees](#safety-guarantees)) |
| **Credentials** | macOS Keychain, environment variables, or 0600 files; verified before they are stored |
| **Proxies** | honours `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` |

## How it works

```
                 ┌──────────────────────── one sync cycle ────────────────────────┐
 Google API ───► │ 1 pull     incremental delta per source calendar → local mirror │
                 │ 2 detect   CalDAV change check per target calendar              │
                 │            (sync-collection, then getctag, then a listing);     │
                 │            changed copies of our own events are read back       │
                 │ 3 plan     desired (the mirror, or the part inside the rule's   │
                 │            window) vs. actual (managed events in the target)    │
                 │            → create / update / delete; edited copies → revert   │
                 │ 4 guard    block suspicious mass deletes                        │
 CalDAV ◄─────── │ 5 push     conditional writes (If-None-Match / If-Match)        │
                 │ 6 record   run history in a local SQLite file                   │
                 └─────────────────────────────────────────────────────────────────┘
```

- Every event calsync writes has a deterministic resource name,
  `calsync-<rule>-<id>.ics`. Ownership is visible from a listing alone, so
  calsync never needs to read — let alone modify — your other events (see
  [What calsync writes into your calendars](#what-calsync-writes-into-your-calendars)).
- When one of its own events changed on the target, calsync reads it back and
  compares what a person sees — times, title, notes, location, link,
  recurrence, free/busy, alerts — with the version it would write. If they
  differ, the source version is written back. Bookkeeping that servers and
  phones add on their own (stamps, sequence numbers, private `X-` properties,
  a dismissed or snoozed alert) is not an edit and is never undone. A copy
  that still shows exactly the source version is never rewritten, even if
  the phone dropped or changed calsync's hidden marker; a check on a real
  iPhone is pending. Reverting does not depend on that marker either: calsync
  remembers what it wrote, so an edit is reverted whether or not the phone
  keeps the marker (only after the local state was lost does a copy without
  the marker that differs count as an ordinary update instead of a revert).
- If a copy keeps changing on the target right after calsync writes it (a
  server or app that rewrites events on its own), calsync stops reverting
  that copy after three reverts within six hours, for 24 hours, and logs one
  warning naming the rule and the event's internal id. Changes made in
  Google are still written meanwhile. When the 24 hours are over, calsync
  writes the Google version back once (reported as `reverted=1`, without an
  edit on the target being needed); if the target rewrites it again, the
  same rule applies.
- All local state (mirror, cursors, target cache, run history) is a
  **rebuildable cache**. Delete the state directory and the next run
  reconciles from both ends without creating duplicates.
- A recurring series with many individually edited instances is written as one
  iCalendar resource with `RECURRENCE-ID` overrides, each carrying its own
  alerts.

The full design lives in [`docs/superpowers/specs/2026-10-23-calsync-design.md`](docs/superpowers/specs/2026-10-23-calsync-design.md).

## What calsync writes into your calendars

Each Google event — a recurring series with all its per-instance changes
counts as one — becomes one iCalendar resource in the target calendar:

- **Resource name** `calsync-<rule>-<id>.ics`, where `<rule>` is a short tag
  (8 hex digits) derived from the rule name and `<id>` (24 hex digits) is
  derived from the rule name, the source calendar and the Google event ID.
  This name is how calsync recognises its own events: it only ever reads,
  changes or deletes resources it named. Everything else in the calendar —
  events you or other apps created — is never touched.
- **`UID`** `<id>@calsync`.
- **Two hidden properties** on every event (calendar apps do not display
  `X-` properties):
  - `X-CALSYNC-RULE` — the name of the rule that wrote the event, so you can
    tell where a copy came from when you look at its raw data. calsync itself
    decides ownership by the resource name, not by this property.
  - `X-CALSYNC-HASH` — a fingerprint of the source version that was written.
    When the local state is rebuilt, calsync reads it back to know which
    copies are already current, so they are not rewritten. Edits made on the
    target are found by comparing the visible content instead, so they are
    caught whether or not the editing app keeps this property.
- **The event itself:** title, notes, location, start and end (with
  `VTIMEZONE`s), recurrence (`RRULE`, `EXDATE`, `RDATE`, `RECURRENCE-ID`
  overrides), free/busy, and one display alert per reminder. With the default
  [`fields`](#rules), the notes are the Google description followed by
  `Attendees: Name <email>, …` and `Conference: <url>`, each block separated
  by a blank line, and the conference link is also the event's `URL`. With
  `content = "busy"` the title is `Busy` and there are no notes, location or
  `URL`; times, free/busy and alerts are kept.
- **Never written:** `ATTENDEE` or `ORGANIZER` (so no server sends
  invitations), meeting rooms and other resources, Google e-mail reminders.

## Quick start

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). About 15 minutes,
most of it in the Google console.

### 1. Install

```sh
uv tool install git+https://github.com/Aryous/calsync
calsync --version
```

### 2. Create a Google OAuth client (once)

calsync reads your calendars with your own Google Cloud OAuth client:

1. Open the [Google Cloud console](https://console.cloud.google.com/), create a
   project, and enable the **Google Calendar API**.
2. Configure the **OAuth consent screen** (External), add yourself, then
   **publish it to "In production"**. While it stays in "Testing", Google
   expires refresh tokens after 7 days and sync stops.
3. Create credentials → **OAuth client ID** → application type **Desktop app**,
   and download the JSON file.
4. Put it where calsync looks for it:

   ```sh
   mkdir -p ~/.config/calsync
   cp ~/Downloads/client_secret_*.json ~/.config/calsync/google-client.json
   chmod 600 ~/.config/calsync/google-client.json
   ```

Personal use needs no Google verification; you will see an "unverified app"
warning when you sign in (choose *Advanced → continue*). calsync only asks for
read-only calendar access.

### 3. Create an iCloud app-specific password

Apple does not offer OAuth for CalDAV. Go to
[account.apple.com](https://account.apple.com) → *Sign-In and Security* →
*App-Specific Passwords* and create one named `calsync`.

### 4. Write the configuration

`~/.config/calsync/config.toml`:

```toml
[google]
client_secret_file = "~/.config/calsync/google-client.json"

[accounts.me-google]
provider = "google"
credential = { backend = "keychain", ref = "calsync/me-google" }

[accounts.me-icloud]
provider = "caldav"
server_url = "icloud"              # detects the global or China mainland region
username = "you@icloud.com"        # your Apple ID
credential = { backend = "keychain", ref = "calsync/me-icloud" }

[[rules]]
name = "family"
source = { account = "me-google", calendar = "Family" }      # name or calendar ID
target = { account = "me-icloud", calendar = "Family", create_if_missing = true }
```

```sh
calsync config check
```

### 5. Sign in

```sh
calsync auth me-google     # opens your browser for Google sign-in
calsync auth me-icloud     # prompts for the app-specific password
```

Both commands verify the credential before storing it and keep the previous
one if verification fails.

### 6. Look, then sync

```sh
calsync calendars me-google    # IDs and names of your Google calendars
calsync calendars me-icloud    # your iCloud calendars
calsync run --dry-run          # print what would be created / updated / deleted
calsync run                    # one sync cycle
```

### 7. Keep it running

Until the built-in service ships, schedule `calsync run`, for example every
five minutes with a launchd agent on macOS
(`~/Library/LaunchAgents/dev.calsync.run.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.calsync.run</string>
  <key>ProgramArguments</key>
  <array><string>/Users/YOU/.local/bin/calsync</string><string>run</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>/Users/YOU/Library/Logs/calsync.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/Library/Logs/calsync.log</string>
</dict></plist>
```

```sh
launchctl load ~/Library/LaunchAgents/dev.calsync.run.plist
```

The keychain backend needs a logged-in user session; on a machine that sits
at the login screen use the `file` backend instead. Watch the exit code in
the log — there are no notifications yet.

## Upgrading

```sh
uv tool install --reinstall git+https://github.com/Aryous/calsync
calsync config check
calsync run --dry-run          # review before the first real cycle
```

### Upgrading to the version with full sync and reverts

The first `calsync run` after this upgrade is a one-time heavy cycle; the
cycles after it are back to the idle cost.

- **Every managed event is read once.** The local state format changed, so
  the state database is rebuilt from both ends (no duplicates, as after any
  state loss). The rebuild also clears the local run history.
- **Every source calendar is pulled from Google in full once.** calsync now
  asks Google for attendees and conference links, and a change of request
  fields always starts a fresh full pull.
- **Only events whose visible content changed are rewritten.** The written
  format changed (for example the new `URL` and the attendee and conference
  lines in the notes). Each copy read during the rebuild is compared with what
  calsync would write now; a copy that already shows exactly that is kept as
  it is, so its snoozed or dismissed alerts stay. Events with attendees or a
  conference link are rewritten once.
- **Rules without `window` now sync everything.** A rule from an older
  configuration that had no `window` used to keep the last 7 days and the
  next 180 days; it now creates the older and later events too. To keep the
  old range, set `window = {}`. On its first full-sync cycle such a rule may
  also delete leftover copies whose Google event was deleted after the copy
  had left the old window. These deletes go through the mass-delete guard
  like any other: if the guard blocks the cycle, check the printed titles and
  use `--allow-deletes`.

## Configuration

Location: `~/.config/calsync/config.toml`, or the path in `$CALSYNC_CONFIG`
or `--config`. Relative paths inside the file are resolved against the file's
directory. Every problem in the file is reported at once.

### `[google]`

| Key | Required | Meaning |
| --- | --- | --- |
| `client_secret_file` | when any Google account exists | the OAuth client JSON downloaded from Google Cloud |

### `[accounts.<name>]`

| Key | Default | Meaning |
| --- | --- | --- |
| `provider` | — | `"google"` (source) or `"caldav"` (target) |
| `credential` | — | `{ backend = "keychain" \| "env" \| "file", ref = "…" }` — keychain entry name, environment variable name, or path to a 0600 file |
| `server_url` | — | caldav only: `"icloud"` (auto-detects global or China mainland) or any `http(s)://` CalDAV URL |
| `username` | — | caldav only: the login, e.g. your Apple ID |
| `max_concurrency` | `2` | caldav only: parallel writes; drops to 1 for the rest of a cycle when the server throttles |

### `[[rules]]`

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | — | unique; part of every resource the rule writes (see [Known limitations](#known-limitations)) |
| `source` | — | `{ account = "…", calendar = "<name or ID>" }`; names are matched against the display name you see in Google |
| `target` | — | `{ account = "…", calendar = "<name or URL>", create_if_missing = false }` |
| `window` | omitted: every event, past and future | optional `{ past = "…", future = "…" }`: only events overlapping this range around now are kept in the target; a key left out means `past = "7d"` / `future = "180d"`, so `window = {}` is the last 7 days and the next 180 days |
| `alarms` | `"copy"` | `"copy"`, `"strip"`, or a list of offsets such as `["-5m", "0m"]` |
| `fields` | `{ attendees = "notes", conference = "copy", content = "full" }` | what of each event reaches the target; see below |
| `on_target_edit` | `"revert"` | the only value: an edit made on the target is written back to the source version on the next cycle, and a copy deleted on the target is re-created |
| `delete_guard` | `{ max_ratio = 0.5, max_count = 10 }` | block a cycle that would delete more than `max_count` events, or (from 3 deletions on) more than `max_ratio` of the rule's events |

`fields` — every key is optional:

| Key | Values | Default | Meaning |
| --- | --- | --- | --- |
| `attendees` | `"none"`, `"notes"` | `"notes"` | `"notes"`: append `Attendees: Name <email>, …` to the notes (a person with only a name or only an address is listed by that). Attendees are never written as invitees, so no invitations are sent. `"none"`: leave them out |
| `conference` | `"copy"`, `"none"` | `"copy"` | `"copy"`: the video meeting link becomes the event's `URL` and a `Conference: <url>` line in the notes. `"none"`: leave it out |
| `content` | `"full"`, `"busy"` | `"full"` | `"busy"`: title `Busy`, no notes, location or link; times, recurrence, free/busy and alerts are kept — e.g. to show a work calendar in a personal one |

```toml
[[rules]]
name = "work-busy"
source = { account = "me-google", calendar = "Work" }
target = { account = "me-icloud", calendar = "Work (busy)", create_if_missing = true }
window = { past = "7d", future = "90d" }
fields = { content = "busy" }
```

Changing `alarms` or `fields` rewrites the events it affects once, on the
next cycle.

Durations are written as `30s`, `5m`, `2h`, `7d`, `1w`, with an optional sign.

`[runtime]` (`interval`, `jitter`) and `[notify]` (`bark_url`, `webhook_url`,
`failure_threshold`) are validated today and will be used by the background
service and alerts.

### Files calsync creates

| Path | Contents |
| --- | --- |
| `~/.config/calsync/` | your configuration and OAuth client file |
| macOS Keychain, service `calsync/<ref>` | refresh tokens and app-specific passwords (keychain backend) |
| `~/.local/state/calsync/state.db` (or `$CALSYNC_STATE_DIR`, `--state-dir`) | rebuildable cache and run history, mode 0600 |
| `~/.local/state/calsync/run.lock` | prevents two `calsync run` at once |

## Command line

```
calsync [--config PATH] [--state-dir DIR] <command>

  config check                     validate the configuration
  calendars <account>              list an account's calendars (ID/URL and name)
  auth <account>                   google: browser sign-in; caldav: read the password
       [--refresh-token-stdin]     google: store an existing refresh token instead
  run [--rule NAME]…               one sync cycle (all rules, or the named ones)
      [--dry-run]                  show the plan, write nothing
      [--allow-deletes]            let this cycle through the mass-delete guard
  --version
```

`calsync run` prints one line per rule (`status`, `created`, `updated`,
`deleted`, `failed`, plus `protected` / `degraded` when relevant) and the
number of HTTP requests the cycle made. Two more counts appear when they are
not zero:

- `out_of_window=N` — source events (a recurring series counts once) that the
  rule's `window` left out; only rules with a `window` show it.
- `reverted=N` — copies edited on the target that were written back to the
  source version; they are also counted in `updated`. A copy deleted on the
  target and re-created counts as `created`.

| Exit code | Meaning |
| --- | --- |
| 0 | every rule succeeded |
| 1 | a rule failed, partly failed or was blocked by the mass-delete guard; also network, database or filesystem errors and a server refusing a request (HTTP 403) |
| 2 | configuration error, including one found while setting up an account |
| 3 | authentication error — run `calsync auth <account>` |
| 4 | another calsync process is running |

With several rules the most actionable code wins: 3, then 2, then 1.

## Safety guarantees

calsync is written for calendars where a wrongly deleted event is worse than a
stale one.

- **Only its own events.** Anything without a `calsync-…` resource name is
  invisible to the engine: never read, changed or deleted.
- **Deletes need evidence.** An event is deleted only when the source no longer
  has it (or it left the rule's time window) and its target copy was actually
  read.
  An unreadable resource holds the change cursor instead of being dropped.
- **Mass-delete guard.** A cycle that would delete too much is blocked for that
  rule — no creates or updates either — and prints the titles it would have
  deleted. `--allow-deletes` (scope it with `--rule`) lets one cycle through.
- **Malformed source data keeps the last good copy.** A series Google returns
  but calsync cannot parse — or one with an unparseable instance — is never
  updated or deleted in the target; it is reported as `protected` /
  `degraded`.
- **Careful reverts.** If calsync cannot work out the visible content of a
  copy, or of the version it would write, the copy is not treated as edited.
  If the source changed too, the new source version is written. An edit read
  in a cycle that then failed, was a dry run, ran only other rules or was cut
  short is remembered in the local state and reverted by the next cycle of
  its rule.
- **Isolation.** One failing calendar, account or rule never stops the others.
- **Conditional writes.** Every write is `If-None-Match` / `If-Match`; on a
  conflict calsync re-reads before retrying.
- **Rebuildable state.** Losing the state directory costs one slower cycle,
  never duplicates.

## Privacy and security

- Google access is read-only (`calendar.readonly`).
- Secrets are read from stdin (hidden on a terminal), verified, then stored in
  the keychain or a 0600 file. They are never printed or logged.
- Logs and error messages never contain event titles, descriptions,
  locations, attendees or links.
- The state database holds a copy of your calendar data and is created with
  mode 0600.
- Redirects are followed manually (credentials are re-sent to the new host, as
  iCloud requires), and a redirect from HTTPS down to HTTP is refused.

## Known limitations

- **Only pop-up reminders are synced; Google e-mail reminders are not.**
- **Attendees are never added as invitees; with `attendees = "notes"` they
  are listed in the notes.** Replies and RSVP status are not synced.
- **Some target edits go unnoticed.** Reverting compares what a calendar app
  normally shows: times, title, notes, location, link, recurrence, free/busy
  and alerts. A change only to properties outside that set (for example
  `STATUS`, `CLASS` or `CATEGORIES`) is not detected and stays until the
  event is rewritten for another reason. Alerts added on the phone that use an
  absolute time, are snoozes (`RELATED-TO`) or are the app's default alerts
  are treated as the person's own and are never reverted.
- **Edits to a protected series wait.** An edit made on the target to a
  series calsync currently cannot fully parse (reported as `protected`) is
  reverted only after the series parses again.
- **Reverting phone edits has not been checked on iCloud yet.** It is tested
  against Radicale and a strict fake CalDAV server; a check with a real
  iPhone and iCloud is still pending.
- **Renaming or removing a rule orphans its events.** Copies written under the
  old rule name are never updated or deleted again; delete them by hand. Two
  installs using the same rule name on the same target calendar would fight
  over each other's events.
- **Alerts on shared iCloud calendars.** Apple stores alerts per user, so
  alerts synced into your calendar do not reach the people you share it
  with. On their iPhone the shared events get only their own Default Alert
  Time (Settings → Apps → Calendar), so "at time of event" works, but early or
  multiple alerts are lost. To deliver every alert, sync directly into a
  calendar in their own account (add their iCloud account and a second rule).
- **Sources are Google only, targets CalDAV only** for now.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `auth` for iCloud: rejected by every iCloud region | wrong Apple ID or app-specific password (often a pasted space); create a new password |
| sync stops after about a week with exit code 3 | the Google OAuth consent screen is still in "Testing"; publish it, then `calsync auth` again |
| Google sign-in says "access blocked" | your account is not a test user of a "Testing" consent screen; publish it or add yourself |
| `status=blocked` | the mass-delete guard fired; check the printed titles, then `calsync run --rule NAME --allow-deletes` if intended |
| exit code 4 | another `calsync run` holds the lock; wait for it |
| an event changed on the iPhone snaps back (`reverted=1`) | expected: sync is one way, the Google version wins. Make the change in Google |
| log says an object "was reverted 3 times within 6:00:00 … not reverting it until …; the source version will be written back then" | something on the target rewrites that event after every write (the loop breaker stepped in). calsync leaves it alone for 24 hours (changes made in Google still sync), then writes the Google version back once ("the hold on 1 copy(ies) ended"). If it keeps happening, look for another app or sync service editing that calendar |
| log says "copy(ies) read back with no or another marker than calsync wrote" | information only: the target (e.g. iOS) dropped or replaced calsync's hidden `X-CALSYNC-HASH`. Edits are still judged by visible content and reverted as usual |
| events arrive on a family member's iPhone but no alert fires | check, in order: notifications for the Calendar app are on (Settings → Notifications → Calendar); the calendar's own Event Alerts switch is on; the calendar is shared rather than in their account, in which case only their Default Alert Time applies (see Known limitations) |
| no alert while the Calendar app is open | expected: iOS shows the alert inside the app instead of as a notification |
| behind a proxy | set `HTTPS_PROXY`; calsync honours the standard proxy variables |

## Development

```sh
git clone https://github.com/Aryous/calsync && cd calsync
uv sync
uv run pytest                 # unit + end-to-end tests (spawns a local Radicale)
uv run pytest -W error -q     # must stay clean
uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright
```

```
src/calsync/
  cli.py  wiring.py  lock.py          command line, account wiring, run lock
  config.py  credentials.py           TOML config, secret backends
  engine.py  planner.py  mirror.py    the sync cycle, pure planning, source mirror
  model.py  naming.py  render.py      unified event model, resource names, iCalendar
  store.py  http.py  errors.py        SQLite cache, HTTP/retry, error types
  sources/  google*.py                Google REST client, OAuth, event mapping
  targets/  dav.py  caldav.py         WebDAV protocol layer, CalDAV target
tests/                                pytest; fakes/ has a strict Google fake and a CalDAV fake
docs/                                 design spec, implementation plan, roadmap
```

Contributor and agent guide: [AGENTS.md](AGENTS.md). Test data is synthetic —
never commit real calendar content or credentials.

## License

Not licensed yet. A license will be added before the first public release.
