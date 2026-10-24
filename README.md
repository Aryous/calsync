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

## Use

```sh
calsync config check
calsync auth family-icloud                      # reads an app-specific password from stdin
calsync auth me-google --refresh-token-stdin    # until the browser sign-in lands
calsync calendars family-icloud
calsync run --dry-run                           # show the plan
calsync run                                     # one sync cycle
```

Known limitation: sync is one way, but a manual edit of a synced event on the
target (e.g. moving it on the iPhone) is only overwritten when the edit drops
or changes the event's `X-CALSYNC-HASH` property. Most clients keep it, so such
an edit stays until the source event changes again. Full repair is planned
once plan 2 has verified how iCloud treats these edits.

`calsync run --allow-deletes` lets one cycle through the mass-delete guard.

A blocked mass delete prints the titles of the events it would delete.

`calsync auth` verifies a credential before storing it; if verification
fails, the existing credential is kept.

Exit codes: 0 ok, 1 a rule failed, 2 configuration error, 3 authentication
error, 4 another calsync process is running.

## Develop

```sh
uv sync
uv run pytest            # unit tests plus end-to-end tests against a local Radicale
uv run ruff check src tests && uv run pyright
```

Test data is synthetic. Never commit real calendar content or credentials.
