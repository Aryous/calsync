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
- [Quick start](#quick-start)
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
| **What is synced** | title, description (HTML converted to text), location, start/end with time zones, all-day events, recurrence rules, exceptions and per-instance changes, cancelled instances, free/busy, **alerts** |
| **What is never synced** | attendees, organizer, conference links — so a target server can never send invitations on your behalf |
| **Alert policy per rule** | copy Google's alerts, strip them, or replace them with fixed offsets such as `["-5m", "0m"]` |
| **Time window per rule** | only events overlapping e.g. the last 7 days and the next 180 days are kept in the target |
| **Many-to-many** | any number of accounts and rules; one source can feed several targets |
| **Cheap when idle** | an unchanged cycle costs one request per Google account, one per source calendar and one per target calendar — enforced by the test suite |
| **Safe by construction** | only touches events it created, mass-delete guard, keeps the last good copy when source data is malformed (see [Safety guarantees](#safety-guarantees)) |
| **Credentials** | macOS Keychain, environment variables, or 0600 files; verified before they are stored |
| **Proxies** | honours `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` |

## How it works

```
                 ┌──────────────────────── one sync cycle ────────────────────────┐
 Google API ───► │ 1 pull     incremental delta per source calendar → local mirror │
                 │ 2 detect   CalDAV change check per target calendar (sync-collection,
                 │            falling back to getctag, then a listing)             │
                 │ 3 plan     desired (mirror ∩ time window) vs. actual (managed   │
                 │            events in the target) → create / update / delete     │
                 │ 4 guard    block suspicious mass deletes                        │
 CalDAV ◄─────── │ 5 push     conditional writes (If-None-Match / If-Match)        │
                 │ 6 record   run history in a local SQLite file                   │
                 └─────────────────────────────────────────────────────────────────┘
```

- Every event calsync writes has a deterministic resource name,
  `calsync-<rule>-<id>.ics`. Ownership is visible from a listing alone, so
  calsync never needs to read — let alone modify — your other events.
- All local state (mirror, cursors, target cache, run history) is a
  **rebuildable cache**. Delete the state directory and the next run
  reconciles from both ends without creating duplicates.
- A recurring series with many individually edited instances is written as one
  iCalendar resource with `RECURRENCE-ID` overrides, each carrying its own
  alerts.

The full design lives in [`docs/superpowers/specs/2026-10-23-calsync-design.md`](docs/superpowers/specs/2026-10-23-calsync-design.md).

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
| `window` | `{ past = "7d", future = "180d" }` | events overlapping this range are kept in the target |
| `alarms` | `"copy"` | `"copy"`, `"strip"`, or a list of offsets such as `["-5m", "0m"]` |
| `delete_guard` | `{ max_ratio = 0.5, max_count = 10 }` | block a cycle that would delete more than `max_count` events, or (from 3 deletions on) more than `max_ratio` of the rule's events |

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
number of HTTP requests the cycle made.

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
  has it (or it left the time window) and its target copy was actually read.
  An unreadable resource holds the change cursor instead of being dropped.
- **Mass-delete guard.** A cycle that would delete too much is blocked for that
  rule — no creates or updates either — and prints the titles it would have
  deleted. `--allow-deletes` (scope it with `--rule`) lets one cycle through.
- **Malformed source data keeps the last good copy.** A series Google returns
  but calsync cannot parse — or one with an unparseable instance — is never
  updated or deleted in the target; it is reported as `protected` /
  `degraded`.
- **Isolation.** One failing calendar, account or rule never stops the others.
- **Conditional writes.** Every write is `If-None-Match` / `If-Match`; on a
  conflict calsync re-reads before retrying.
- **Rebuildable state.** Losing the state directory costs one slower cycle,
  never duplicates.

## Privacy and security

- Google access is read-only (`calendar.readonly`).
- Secrets are read from stdin (hidden on a terminal), verified, then stored in
  the keychain or a 0600 file. They are never printed or logged.
- Logs and error messages never contain event titles, descriptions or
  locations.
- The state database holds a copy of your calendar data and is created with
  mode 0600.
- Redirects are followed manually (credentials are re-sent to the new host, as
  iCloud requires), and a redirect from HTTPS down to HTTP is refused.

## Known limitations

- **Edits made on the target are not repaired yet.** Sync is one way, but if
  someone moves a synced event on the iPhone, calsync currently keeps that
  edit until the event changes in Google again. Repair is planned once its
  behaviour on iCloud has been measured.
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
