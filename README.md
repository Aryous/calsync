# calsync

One-way calendar sync engine. It keeps one or more target calendars in step
with a source calendar, continuously and cheaply.

First release: **Google Calendar → CalDAV** (iCloud, iCloud China, Fastmail,
Nextcloud, Radicale, …).

## Why

Google calendars can only be shared with Google accounts, iCloud calendars
only with Apple IDs. calsync bridges them one way: write events wherever you
like (for example through an AI assistant connected to Google Calendar) and
have them appear, with their alerts, in an iCloud calendar.

## Design in one paragraph

calsync keeps a local mirror of each source calendar, updated with Google's
incremental `syncToken`, and detects target changes with CalDAV
`sync-collection` (falling back to `getctag`, then a plain listing). Every
resource it writes has a deterministic name (`calsync-<rule>-<id>.ics`), so
ownership is visible from a listing alone and calsync never touches events it
did not create. All local state is a rebuildable cache. An idle cycle costs one
`calendarList` request per Google account, one `events.list` request per source
calendar and one change check per target calendar, and the test suite enforces
that budget. See `docs/superpowers/specs/2026-10-23-calsync-design.md`.

## Status

Plan 1 (this release) is the sync engine and a one-shot `calsync run`. There
is no daemon and no alerting yet: run `calsync run` from cron or launchd
(for example every few minutes) and watch its exit code; a failure is only
visible there and in its output.

## Install

```sh
uv tool install git+https://github.com/Aryous/calsync
```

## Configure

`~/.config/calsync/config.toml` (override with `CALSYNC_CONFIG`):

```toml
[google]
client_secret_file = "~/.config/calsync/google-client.json"

[accounts.me-google]
provider = "google"
credential = { backend = "keychain", ref = "calsync/me-google" }

[accounts.family-icloud]
provider = "caldav"
server_url = "https://caldav.icloud.com/"   # China mainland: https://caldav.icloud.com.cn/
username = "family@example.com"
credential = { backend = "keychain", ref = "calsync/family-icloud" }

[[rules]]
name = "family"
source = { account = "me-google", calendar = "Family" }
target = { account = "family-icloud", calendar = "Family", create_if_missing = true }
window = { past = "7d", future = "180d" }
alarms = "copy"          # or "strip", or ["-5m", "0m"]
```

Credential backends: `keychain` (macOS keychain via keyring), `env`
(environment variable), `file` (path to a file with mode 0600).

Google: create a Google Cloud project with a "Desktop app" OAuth client and
publish its OAuth consent screen as **In production**. While it is in
"Testing", Google expires refresh tokens after 7 days and sync stops with
exit code 3. Personal use needs no verification; you will see an "unverified
app" warning when you sign in.

Rule names are part of every resource calsync writes. Renaming or removing a
rule leaves the copies written under the old name in the target calendar:
they are never updated or deleted again, so delete them by hand. Two calsync
installs that use the same rule name on the same target calendar would treat
each other's copies as their own and fight over them.

## Use

```sh
calsync config check
calsync auth family-icloud                      # reads an app-specific password from stdin
calsync auth me-google --refresh-token-stdin    # until the browser sign-in lands
calsync calendars family-icloud
calsync run --dry-run                           # show the plan
calsync run                                     # one sync cycle
calsync run --rule family                       # only this rule (repeatable)
```

Global options: `--config` (default `$CALSYNC_CONFIG`, else
`~/.config/calsync/config.toml`) and `--state-dir` (default
`$CALSYNC_STATE_DIR`, else `~/.local/state/calsync`). The state directory
holds a rebuildable cache and the run lock.

`calsync auth` verifies a credential before storing it; if verification
fails, the existing credential is kept.

Mass-delete guard: when a cycle would delete too many of a rule's events, the
whole rule is blocked for that cycle (no creates or updates either) and the
titles of the events it would delete are printed. `calsync run
--allow-deletes` lets one cycle through; it applies to every selected rule, so
scope it with `--rule`.

Known limitation: sync is one way, but a manual edit of a synced event on the
target (e.g. moving it on the iPhone) is only overwritten when the edit drops
or changes the event's `X-CALSYNC-HASH` property. Clients usually keep unknown
properties (plan 2 verifies this for iOS), so such an edit is likely to stay
until the source event changes again. Full repair is planned
once plan 2 has verified how iCloud treats these edits.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | every rule succeeded |
| 1 | a rule failed, partly failed or was blocked by the mass-delete guard; also network, database or filesystem errors, and a server refusing a request (HTTP 403) |
| 2 | configuration error, including one found while setting up an account (e.g. a credential file that is not 0600) |
| 3 | authentication error: run `calsync auth <account>` |
| 4 | another calsync process is running |

With several rules, `calsync run` reports the most actionable code: 3 if any
rule hit an authentication error, else 2 if any hit a configuration error,
else 1 if any did not succeed.

## Develop

```sh
uv sync
uv run pytest            # unit tests plus end-to-end tests against a local Radicale
uv run ruff check src tests && uv run pyright
```

Test data is synthetic. Never commit real calendar content or credentials.
