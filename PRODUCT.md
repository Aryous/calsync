# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

- The web UI is served by the `calsync daemon` process (Starlette).
- Front end: Preact + htm with no build step. The library files ship inside the Python package; no CDN.
- It must work completely over plain HTTP. Tailscale or Surge Ponte only pass the traffic through, so no HTTPS-only browser APIs.
- Decided in the phase-2 design, decision D13.

## Users

- **Primary user:** anyone who keeps their calendar in Google and wants it in another calendar
  service too (iCloud today, more services later). A product for the general public, not for one
  household (the user's decision, 2026-10-29).
  - They, or an AI assistant acting for them, write events into Google Calendar.
  - calsync copies them one way to the target calendars. Who reads a target (the user on their
    own iPhone, a colleague, a family member) is the user's business: the UI never assumes it.
- **How they use the UI:**
  - Mostly on the phone, a quick check: is sync working, what changed, will the alerts arrive?
  - On the desktop for configuration: the map is also where lines, accounts and settings are changed.
  - They act only when something needs them.
- **Whoever reads a target calendar** never needs the UI: they see the result in their own calendar app.

## Product Purpose

The AI writes, calsync delivers, the user verifies. The UI answers three questions:
1. **Is it connected?** Is every sync line healthy?
2. **What changed?** What changed in the source recently, and what did calsync write?
3. **Will it arrive?** Which events are coming up in the target calendars, with which alerts, and is each one already delivered?

Success: a glance on the phone answers all three. When something is wrong, the page says what to do.

## Positioning

calsync is a self-hosted, one-way mirror from Google Calendar to CalDAV/iCloud:
- **Full sync** by default.
- **Alerts** are copied or set per rule.
- **Edits made on the target are reverted** to the source.

It runs on the user's own machine; no hosted service ever holds their calendar data.

## Operating Context

- **Where it runs:** a Mac (later a Mac mini) as a launchd service. The UI is reached on localhost, over Tailscale, or over Surge Ponte, from an iPhone or a laptop.
- **Pairing:** by a key link or QR code. No passwords over plain HTTP.
- **What the user is doing:** checking whether today's events and alerts reached the target calendars, usually right after they (or an AI assistant) changed the schedule.
- **Terminology:**
  - sync line = rule
  - source (Google) and target (iCloud today; other services later)
  - alerts
  - delivered / pending
  - reverted (an edit on the target was replaced by the source version)
  - protected (left untouched because the source could not be read in full)
  - blocked (a mass deletion is waiting for confirmation)

## Capabilities and Constraints

- **Scope (v4, phase B):**
  - the sync map as the home page and the configuration surface: sources, the hub, targets and the lines between them; touching a line, a calendar, an account, the hub or the gear opens its inspector;
  - the result sentence and attention cards under the map;
  - the calendar with six views (day, two days, week, two weeks, month, list; the phone hides week and two weeks), target layers, source colours and the delivery state on each event;
  - the event detail: the Google original next to each iCloud copy;
  - the activity timeline: changes and sync cycles together;
  - settings: notifications, check interval, pairing other devices, allowed addresses, the config file and its history with undo;
  - the account wizard (connect Google / iCloud), reconnect and remove;
  - the blocked-delete flow: see the list, approve exactly that set, or leave it.
- **Data and changes:** reads `GET /api/*`; every change goes through `POST /api/ops/<name>` (recorded with source `ui`, the same operations the CLI runs), written to `config.toml` with a backup and an undo.
- **Events are written to the Google original** (create, change by form or drag, delete), never to the copy; the copy follows at the sync each write requests (phase C).
- **No secrets in the browser (D10):** no password, token or app-specific password field anywhere. Every step that needs a person shows the command to run on the Mac (with a copy button) and waits until the state changes.
- **Plain HTTP:** nothing needs a secure context; copy buttons select the text and fall back to `execCommand("copy")`.
- **Privacy:** event titles may be shown in the UI and the JSON API, never in logs; the browser console gets errors by kind only.
- **Languages:** Chinese by default, English available, with the English text as the key.
- **Layout:** phone layout up to 760 px wide (or touch devices up to 1024 px): the map stays horizontal and the inspector is a bottom sheet (at most 88 vh). Desktop: the inspector is a right panel and the page shifts left so the map stays visible. Tap targets at least 44 px; inputs at 16 px; safe-area insets; no horizontal scroll at 360 px.
- **Theme:** light and dark, applied before first paint; `prefers-reduced-motion` stops the wire comets and the hub breathing.
- The UI does not detect or mention shared calendars (D16). It does not distinguish "syncing to myself" from "syncing to family" (D17).

## Brand Commitments

- Name: `calsync`, lowercase, set in Instrument Sans next to the hub mark (a dark rounded tile with two crossing traces).
- **The user's three principles (2026-10-26):**
  1. Relationships before results: the map of who syncs to whom is the hero; no metric cards.
  2. Spatial composition: sources left, the hub in the middle, targets right.
  3. Quiet creativity: low saturation, thin lines, large whitespace, slow motion with a slight breathing; no glow, no 3D, no neon.
- **Palette (Moodboard 01):** white `#FFFFFF`, pale `#F8FAFC`, cool grey `#E5E8EE`, slate `#CBD6E1`, soft blue `#AEB8D9`, charcoal `#1F2937`; the full light and dark token set is in the concept and `styles.css`.
- **Type:** Instrument Sans (self-hosted, OFL) for Latin, numerals and the wordmark; the system PingFang SC for Chinese.
- **Naming rule:** a calendar name is never shown without its service icon and account. An
  account is named by its email (a Google account's address, an Apple ID); the config's name for
  it is secondary, shown where a terminal command needs it. An account's own calendar (Google
  names it after the email) is shown as 主日历, never as the email, so it cannot be mistaken for the
  account.
- **Copy rule (2026-10-29):** no explanation of what a person sees at a glance; a hint stays only
  where it prevents a mistake (a delete, a secret, a refusal) or names an empty or loading state.
  General flows never name a target service (no "iCloud", "iPhone", "家人"): they say 副本 /
  目标日历, or the line's own service by name. Only service-specific steps (connecting an iCloud
  account) name the service.
- The direction contract is `.impeccable/surfaces/src-calsync-web-static-index-html.md`; the approved concept is `.impeccable/mocks/canvas/index.html`.

## Evidence on Hand

- Real state: one rule, about 17 synced series, more than 100 run records.
  - Real event titles are personal. Never put them in screenshots committed to the repo, in fixtures or in docs.
  - Demo and test data must be synthetic.
- No testimonials, users, metrics or press exist. Do not invent any.

## Product Principles

1. **Quiet when fine, loud only when something needs you.** When all is well, the page is one calm sentence.
2. **Results, not internals.** No tokens, status codes or hashes on the first screen; details sit two levels down.
3. **Every number opens its evidence.** "Updated 15" expands to the events.
4. **Actions configure and repair, never edit events.** Buttons exist for "sync now", for the settings of a line, an account or calsync itself, and for fixing problems.
5. **Boundaries are visible.** What is synced, what is not, and why. Every "fewer than expected" has an explanation.

## Accessibility & Inclusion

- WCAG AA contrast in both themes.
- Status is never told by colour alone; always add text or an icon.
- Respect `prefers-reduced-motion`.
