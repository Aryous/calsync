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

### Plan 2 — block ①: sync engine adjustments (2026-10-24)

- Full sync by default; the time window is an optional advanced setting.
- Configurable field policy (`fields`: attendees into notes, conference link,
  busy-only mode).
- `on_target_edit = "revert"`: edits made on the target (e.g. on an iPhone)
  are reverted to the source on the next cycle; deleted copies are recreated;
  a rewrite-loop breaker.
- Still open: the iPhone check of reverts against iCloud (experiment E1);
  401 vs 403 for a bad app-specific password; `MKCALENDAR` on iCloud.
- Settled: writing into a family member's own account (E5) is the situation
  of the first real run, in daily use since 2026-10-24. Alerts on a calendar
  shared to another Apple ID do not reach the sharee (measured 2026-10-24,
  spec v3 §13 item 1).

### Plan 3 — block ②: background service, control and notifications

The phase-2 design is in
[`docs/superpowers/specs/2026-10-24-calsync-v2-product-design.md`](superpowers/specs/2026-10-24-calsync-v2-product-design.md)
(product principles, decisions D1–D17, three blocks built in order).

**The daemon slice** (2026-10-25,
[`plans/2026-10-25-block2-daemon-slice.md`](superpowers/plans/2026-10-25-block2-daemon-slice.md)):
run and change history; read-only views (`status`, `upcoming`, `runs`,
`changes`, each with `--json`); `calsync daemon` (jittered schedule, per-rule
backoff, credential pickup, `SIGUSR1`, `--no-sync`); the local JSON API on a
0600 unix socket and `calsync trigger`. A first web UI slice (pairing, status
page, the TCP listener) followed
([`plans/2026-10-25-block3-web-ui-slice.md`](superpowers/plans/2026-10-25-block3-web-ui-slice.md)).

**Phase A — block ② completed** (2026-10-28,
[`plans/2026-10-28-block2-control-ops.md`](superpowers/plans/2026-10-28-block2-control-ops.md)),
everything the v4 GUI concept's buttons need, as one operation layer shared by
the CLI and the API:

- Safe config writes (comment-preserving, validated, conflict-checked, atomic,
  backed up, recorded) with `config get|set|history|undo`, and hot reload of
  the file in the daemon (external edits validated and recorded).
- Rule and account editing: `rule add|set|remove`, `account add|remove`,
  `pause` / `resume`; `rule add --preview`; retiring a rule with its copies
  (`rule remove --delete-copies`).
- `blocked` and `approve` (by fingerprint; agents may approve; every approval
  pushed and recorded).
- `events` / `event show`: events by date with a delivery state per rule;
  copy summaries and change details; `trigger --wait`; a cached calendar list
  (`calendars [--refresh]`); URL-free stored errors; status additions.
- Bark and webhook notifications (`notify test`, `notify.language`,
  damping).
- `calsync service install|uninstall|status` for launchd.
- The operations over the web API (masked), a sealed test environment, and
  the [agent guide](agent-cli.md).

Still open in block ②: deployment on the Mac mini (`calsync service
uninstall` on the old machine, `install` on the new one); a Docker image for
a home server or VPS.

## Next

### Plan 4 — block ③: the web UI after concept v4

The feasibility review of concept v4 is in
[`specs/2026-10-27-concept-v4-feasibility.md`](superpowers/specs/2026-10-27-concept-v4-feasibility.md).

- **Phase B — the GUI rewrite.** The pages of concept v4 (overview with the
  connection map and attention cards, calendar view with delivery states,
  rule detail, history, settings, onboarding), phone-first, in Chinese,
  served by the daemon and built on the phase A operations. Wording follows
  what calsync can know ("delivered" means written to the server, not seen on
  the phone). Secrets are entered only on localhost or HTTPS.
- **Phase C — editing events.** Creating and changing events from the GUI,
  written to Google (calsync stays one way: the copy follows); read-only
  Google calendars (by their `role`) greyed out.

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
