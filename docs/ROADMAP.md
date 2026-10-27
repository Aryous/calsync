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

The phase-2 design is in
[`docs/superpowers/specs/2026-10-24-calsync-v2-product-design.md`](superpowers/specs/2026-10-24-calsync-v2-product-design.md)
(product principles, decisions D1–D17, three blocks built in order).

### Plan 2 — block ①: sync engine adjustments

- Full sync by default; the time window becomes an optional advanced setting.
- Configurable field policy (`fields`: attendees into notes, conference link,
  busy-only mode).
- `on_target_edit = "revert"`: edits made on the target (e.g. on an iPhone)
  are reverted to the source on the next cycle; deleted copies are recreated.
  First task: measure on iCloud whether phone edits keep `X-CALSYNC-HASH`
  and whether PUT returns an `ETag` (experiments E1, E2).
- Still open from the first real run: 401 vs 403 for a bad app-specific
  password; `MKCALENDAR`.
- Settled by the first real run: writing into a family member's own account
  (E5). That run is the same situation (calsync writing with an account's own
  app-specific password into that account's own calendar, whose alerts fire
  on that account's iPhone) and has been in daily use since 2026-10-24, so no
  separate experiment is needed. What is left is operational: generate the
  family member's app-specific password, add their account and a rule, and
  make sure Calendar notifications are on on their phone.
- Already measured (2026-10-24, spec v3 §13 item 1): alerts on a calendar
  shared to another Apple ID do not reach the sharee.

### Plan 3 — block ②: background service and notifications

**Done — the daemon slice** (2026-10-25,
[`plans/2026-10-25-block2-daemon-slice.md`](superpowers/plans/2026-10-25-block2-daemon-slice.md)):

- Run and change history (`change` table, richer `run` rows, 90-day change
  retention).
- Read-only views: `calsync status`, `upcoming`, `runs`, `changes`, each with
  `--json` for agents; they read the state database directly.
- `calsync daemon`: jittered schedule, per-rule backoff, credential pickup
  after `calsync auth`, `SIGUSR1`, and `--no-sync` for demos on a copy of the
  state.
- Local JSON API under `/api` on a 0600 unix socket in the state directory,
  and `calsync trigger` as its client.

**Still to do (deferred from the slice):**

- Config hot reload and safe writes (comment-preserving, validated,
  conflict-checked, atomic, backed up, attributed); the `config_change` table.
- Bark and webhook notifications.
- `approve` and `pause`/`resume`.
- `calsync service install` for launchd; Mac mini deployment.
- The TCP listener (`--listen`) belongs to block ③, with its authentication.

### Plan 4 — block ③: web UI v1

- Served by the daemon (Starlette), Preact + htm without a build step, usable
  over plain HTTP (Tailscale or Surge Ponte), phone-first for viewing pages.
- Pairing by key link / QR code; secrets entered only on localhost or HTTPS.
- Pages: overview (connection map, status, attention cards, next 24 hours),
  rule detail, run history, settings, onboarding wizard.

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
