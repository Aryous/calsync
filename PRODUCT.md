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

- **Primary user:** the person who runs calsync for the household (confirmed 2026-10-25).
  - An AI assistant (for example ChatGPT's Google Calendar connector) writes events into their Google Calendar.
  - calsync copies those events to iCloud calendars that family members see on their iPhones.
- **How they use the UI:**
  - Mostly on the phone, a quick check: is sync working, what changed, will the alerts arrive?
  - On the desktop for configuration.
  - They act only when something needs them.
- **Family members never use the UI.** They only see the result in the iPhone Calendar app.

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

It runs on the user's own machine; no hosted service ever holds their family's calendar data.

## Operating Context

- **Where it runs:** a Mac (later a Mac mini) as a launchd service. The UI is reached on localhost, over Tailscale, or over Surge Ponte, from an iPhone or a laptop.
- **Pairing:** by a key link or QR code. No passwords over plain HTTP.
- **What the user is doing:** checking whether today's events and alerts reached the family, usually right after asking an AI assistant to change the schedule.
- **Terminology:**
  - sync line = rule
  - source (Google) and target (iCloud)
  - alerts
  - delivered / pending
  - reverted (an edit on the target was replaced by the source version)
  - protected (left untouched because the source could not be read in full)
  - blocked (a mass deletion is waiting for confirmation)

## Capabilities and Constraints

- **v1 pages:**
  - overview: status sentence, attention cards, connection map, next 24 hours, recent changes;
  - run history.
- **Data:** read-only views of the local state database, plus one action, "sync now".
- **Not in v1:** an onboarding wizard, settings editing, QR pairing UI, notifications, two-way sync. The overview must not pretend otherwise.
- **The UI never edits events.**
- **Privacy:** event titles may be shown in the UI and the JSON API, never in logs.
- **Languages:** English and Chinese, with the English text as the key.
- **Layout:** phone layout up to 760 px wide (or touch devices up to 1024 px). Tap targets at least 44 px; inputs at 16 px. Safe-area insets.
- **Theme:** dark mode, applied before first paint.
- The UI does not detect or mention shared calendars (D16). It does not distinguish "syncing to myself" from "syncing to family" (D17).

## Brand Commitments

- Name: `calsync`, lowercase.
- No logo exists yet.
- Reference the user gave: the Keeper.sh dashboard's visual language. Lots of white space, service icons, dashed connector lines.

## Evidence on Hand

- Real state: one rule, about 17 synced series, more than 100 run records.
  - Real event titles are personal (a family member's schedule). Never put them in screenshots committed to the repo, in fixtures or in docs.
  - Demo and test data must be synthetic.
- No testimonials, users, metrics or press exist. Do not invent any.

## Product Principles

1. **Quiet when fine, loud only when something needs you.** When all is well, the page is one calm sentence.
2. **Results, not internals.** No tokens, status codes or hashes on the first screen; details sit two levels down.
3. **Every number opens its evidence.** "Updated 15" expands to the events.
4. **Actions only to repair.** Buttons exist for "sync now" and for fixing problems, never for editing events.
5. **Boundaries are visible.** What is synced, what is not, and why. Every "fewer than expected" has an explanation.

## Accessibility & Inclusion

- WCAG AA contrast in both themes.
- Status is never told by colour alone; always add text or an icon.
- Respect `prefers-reduced-motion`.
