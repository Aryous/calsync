# Design

The calsync web UI as it ships (concept v4, "the sync map"). It is recorded from the code in
`src/calsync/web/static/` (`styles.css` holds every token), not from intentions. `PRODUCT.md`
says who the UI is for and what it must do. The approved concept is
`.impeccable/mocks/canvas/index.html`.

## Principles

1. **Relationships before results.** The home page is a map of where events flow: Google calendars
   on the left, the calsync hub in the middle, iCloud calendars on the right, a wire per line. No
   metric cards as the hero; one sentence under the map says how things are.
2. **Spatial composition.** Sources left, hub centre, targets right, on the phone too (the columns
   shrink, they never stack).
3. **Quiet creativity.** Low saturation, thin lines, large whitespace, slow motion. No glow, no 3D,
   no neon. The one signature movement is a slow comet along a healthy wire, and a wave along
   the wires when a sync runs.
4. **A calendar is never named alone.** Wherever a calendar appears it carries its service mark
   and its account: `[Google] 家庭日程 · personal`. The one helper that composes the words is
   `calendarLabel()` in `format.js`; `CalendarName` in `icons.js` adds the mark.
5. **Every number is evidence.** A count links to what it counts where a place for it exists.
6. **Configure and repair, never edit events** (until phase C). No secret is ever typed into the
   browser: a step that needs a person shows the command to run, with a copy button.

## Colour

Tokens on `:root`, redefined for dark under `prefers-color-scheme` (unless `data-theme="light"`)
and under `[data-theme="dark"]`. `theme.js` sets the theme before first paint.

| Role | Light | Dark | Use |
| --- | --- | --- | --- |
| `--bg` | `#F8FAFC` | `#0F1217` | page |
| `--surface` | `#FFFFFF` | `#161A21` | pills, panels, buttons |
| `--sunk` | `#F1F4F8` | `#12161C` | wells: route chip, source row, segmented control |
| `--line` / `--line-2` / `--slate` | `#E5E8EE` / `#D6DDE6` / `#CBD6E1` | `#242A33` / `#2F3742` / `#3A4452` | hairlines, borders, hover borders |
| `--ink` / `--ink-2` / `--ink-3` | `#1F2937` / `#4B5563` / `#66707F` | `#E7EAF0` / `#B4BBC6` / `#8D96A4` | text, three steps |
| `--accent` / `--accent-wash` | `#5E6FB3` / `#EEF1FA` | (dark set) | selection, focus, the chosen radio |
| `--ok` | `#3B7A58` | `#74B895` | delivered, healthy |
| `--wait` / `--wait-wash` | `#9A5B0B` / `#FBF3E6` | `#D9A55A` / `#2A2216` | pending, held back |
| `--bad` / `--bad-wash` | `#B0453D` / `#FBEEEC` | `#E2857C` / `#2C1B1A` | failures, the now-line, delete buttons |
| `--wire` | `#C2CBD7` | (dark set) | a healthy wire |
| `--hub`, `--hub-trace`, `--hub-port`, `--hub-hot` | dark tile, light traces | light tile, dark traces | the hub |

**Source colours.** Each synced Google calendar gets one of eight quiet hues (`sage`, `teal`,
`clay`, `blue`, `violet`, `rose`, `olive`, `steel`). The hue is a pure function of the
calendar's key (`colourOf()` in `map.js`: FNV-1a plus murmur3's finaliser, mod 8), the same on
every device; two calendars may share a hue because the name is always beside the dot.
`--dot-<hue>` is the dot on the map and in legends (at least 3:1 on its pill in both themes).
`--ev-<hue>-bg` / `--ev-<hue>-fg` colour event blocks in the calendar.

## Type

- **Instrument Sans** (variable, 400–700, self-hosted with its OFL licence) for Latin and figures;
  **PingFang SC** (system) for Chinese. `--sans` lists both. `--mono` for commands.
- Sizes: headline 20–22 px / 600; section labels 13.5 px / 600; body 13–14 px; secondary text
  12.5 px in `--ink-3`; inputs 16 px (no zoom on iOS). Figures use the `num` class (tabular).

## Layout

- **Page column** up to 1200 px with a 32 px gutter. Under the map: the result sentence and
  attention cards, then two tabs, 日历 and 活动.
- **The map** (`.canvas`): a three-column grid `minmax(210px, 264px) 1fr minmax(210px, 264px)`.
  Wires are SVG beziers measured from the pills' and hub ports' real positions after every
  render and resize. On the phone the columns stay side by side.
- **The inspector**: every route but the map opens one. Desktop: a 440 px panel on the right,
  and the page shifts left so the map stays visible. Phone (≤ 760 px, or touch ≤ 1024 px): a
  bottom sheet at most 88 % of the dynamic viewport height. Head (title, subtitle, close), a
  scrolling body of sections (`.fs`, each a label, a hint and its controls), a footer for the
  panel's actions and the quiet "已保存到 config.toml".
- **The calendar**: a toolbar (month title and range, ‹ 今天 ›, one layer toggle per target
  calendar, the six views), a legend of source calendars, then the view in a framed card. The
  time grid is 24 hours of `--hourH` (48 px) with an all-day row and a now-line in local time;
  overlapping events share the column in lanes.

## Components

- **Buttons** (`.btn`): 38 px tall, 11 px radius, surface with a hairline; `primary` is ink on
  ink; `quiet` has no border; `danger` / `danger-solid` for deletes; `icon` is square. Busy shows
  a spinning icon at full strength. On the phone every tap target is at least 44 px.
- **Reminders** (`.cal`): a calendar on the map, a colour dot, its name, the series count or 未同步
  (dashed). **Chips** (`.chip`): an event in the week and month grids.
- **Segmented control** (`.seg`): toggle buttons (`aria-pressed`) in a sunk well.
- **Radio rows** (`.radio`, `role="radio"` in a `radiogroup`): the new-line picker, the delete
  choice, the old-copies choice. Arrow keys move within one group.
- **Notes** (`.note`, `.note.warn`, `.note.bad`): a quiet well for an explanation or a warning.
- **Command** (`command.js`): a command in mono with a copy button that works over plain HTTP.
- **Attention cards** (`.attn`): one per problem under the sentence: what happened, which lines,
  what to do.
- **Rows** (`.row`, `.ar`): lists in inspectors and the activity timeline (time, an icon tile,
  a headline, a secondary line with the route).

## Delivery state, everywhere the same

| State | Wire | Event block | Words |
| --- | --- | --- | --- |
| delivered | solid | unmarked | 已送达 iCloud · 10:41 |
| pending | — | amber outline and flag | 等待送达 · 下次检查 10:43 |
| paused | dashed grey | hatched | 暂停中 |
| auth / failing | dashed red | hatched | 没送达 · 授权失效 / 上次同步失败 |
| blocked | amber | hatched | 删除被拦下，等你确认 |
| retiring | thin dashed | hatched | 线路正在移除 |
| out of range | — | faded | 不在同步范围 |

## Motion

`--ease: cubic-bezier(.16, 1, .3, 1)`. The inspector slides in 450 ms; the page shifts with it.
Comets (Web Animations, id `comet`) travel a healthy wire every few seconds while idle; a sync
sends a wave along the wires; a new line gets one comet. `prefers-reduced-motion` stops all of
it (transitions and animations at 0.01 ms).

## Words

Chinese by default, English on request (`?lang=en` or the footer); the English text is the key in
`i18n.js`, and every visible string goes through `t()`. Plain words over jargon: 线路 for a rule,
副本 for a copy, 已送达 iCloud, "在 iPhone 上被改了，已改回" (never "有人"). An authorisation
failure lists the usual reasons, including the Google app in "testing" status, which has to be
renewed every 7 days.

## Accessibility

WCAG AA as the target. One `role="status"` live region per place a result is announced. Tabs
follow the ARIA tabs pattern. The inspector takes focus on open and gives it back on close,
and is inert while closed. Focus is always visible (the wires draw a 4 px accent band). Contrast
is checked for the source dots and the wire focus ring; the browser tests check overflow, tap
targets and names at five viewports in Chromium and WebKit.
