# Roadmap

calsync grows in plans. Each plan has a spec section or its own spec, an
implementation plan under `docs/superpowers/plans/`, and ends with a reviewed,
tested merge to `main`.

## Done

### Plan 1 — core engine (2026-10-24)

- Google Calendar source with incremental sync; CalDAV target with
  sync-collection / getctag / listing detection; pure planner; mass-delete
  guard; rebuildable SQLite cache; `calsync` CLI (`config check`,
  `calendars`, `auth` with Google browser sign-in, `run`).
- iCloud region auto-detection (`server_url = "icloud"`).
- First real-world run: Google → iCloud China mainland, cross-host calendar
  home, sync-collection mode, recurring series with hundreds of per-instance
  edits and multiple alerts per event, idle cycle at budget.

## Next

### Plan 2 — real-service verification

Measure what iCloud actually does and close the gaps that depend on it
(design spec §13). In priority order:

1. Edits made on the target (iOS): does the body keep `X-CALSYNC-HASH`, does
   the etag change after our own PUT? Decides how to repair target-side edits
   (pinned by the strict xfail `test_manual_target_edit_is_repaired`).
2. Wrong or revoked app-specific password: 401 or 403?
3. Alerts on a calendar shared to another Apple ID. **Measured 2026-10-24
   (spec §13 item 1):** the sharer's alerts do not reach the sharee. The
   sharee only gets alerts from their own iPhone's Default Alert Times, and
   only with notifications for the Calendar app enabled; early and multiple
   alerts are lost. Still to verify: writing directly into the family
   member's own account (experiment B), which is the recommended setup for
   "sync to family".
4. `MKCALENDAR` support, ETag on PUT, HTTP/2, the remaining §13 items.

### Plan 3 — background service and alerts

- `calsync daemon` (asyncio, interval + jitter, per-rule backoff, signals),
  `calsync trigger`, `calsync status`.
- Alerts via Bark / webhook (`[notify]`), including blocked deletes with
  titles and series held as protected/degraded.
- launchd install (`calsync service install`) and a Docker image; first
  deployment target is a home server (Mac mini).
- Persist `protected` / `degraded` / blocked titles in run history.

### Plan 4 — self-hosted web UI (product direction agreed, design pending)

Decisions so far:

- **Form:** a self-hosted web UI served by the calsync service and opened from
  any browser on the network, in the spirit of Home Assistant / Syncthing.
  Visual language inspired by keeper.sh: each sync shown as
  *service · account · calendar → service · account · calendar*.
- **Users:** single administrator in the first version, but accounts and
  rules carry an owner from day one so n8n-style multi-user can be added
  without a data migration.
- **Google sign-in:** users keep bringing their own OAuth client; the UI
  provides a step-by-step wizard (screenshots, deep links, where to paste
  what). No Google app verification for now.
- **iCloud:** app-specific passwords cannot be avoided (Apple offers no OAuth
  for CalDAV); the UI links straight to the right Apple page.
- **Sync to family:** the wizard defaults to writing into the family
  member's own account. If the user picks "my calendar, shared with them"
  instead, the UI says plainly that the other person will not get these
  alerts, only their own default alert.

Open: page layout and flows (next brainstorming step), tech stack, how the UI
and the CLI share configuration.

## Later

- **Contacts** (Google People API → CardDAV): same source → target
  architecture; the natural second data type.
- **Reminders / tasks:** Google Tasks has an API, but current Apple Reminders
  are not exposed over CalDAV — needs research.
- **Mail (IMAP):** possible, but the largest and riskiest; not planned.
- **Notes:** dropped — Apple Notes has no public API and the Google Keep API is
  limited to Google Workspace.
- **Hosted multi-tenant service** (keeper.sh style): a separate decision —
  operating servers and holding other people's calendar data. Not planned.

## Before the first public release

- Choose a license (MIT or Apache-2.0).
- History was rewritten to remove personal data; delete and recreate the
  GitHub repository before making it public so old commit IDs are gone.
- README screenshots, contribution guide, issue templates.
