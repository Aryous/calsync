# Block ③ (slice) — Web UI v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The daemon serves a paired, phone-first web UI. It has two pages: **overview** (status sentence, attention cards, connection strip, next 24 hours as validated coupons, recent changes) and **run history**. It uses the block ② JSON API and works over plain HTTP. It comes in light and dark themes, in English and Chinese.

**Architecture:**
- **Two listeners.** The daemon gains a TCP listener, in addition to the Unix socket from block ②. Both serve one Starlette app factory with the same routes.
  - TCP adds pairing and cookie auth, Host and Origin checks, and static files.
  - The Unix socket stays authless; its file permissions are the authorization.
- **Front end.** Static ES modules in `src/calsync/web/static/`: Preact + htm in the vendored standalone build, and self-hosted fonts. There is no build step and no CDN.

**Tech Stack:** Starlette, uvicorn, Preact 10 + htm 3 (vendored `htm/preact/standalone.module.js`), Barlow Condensed (OFL, self-hosted woff2), and Python Playwright for UI tests.

**Design sources:**
- `docs/superpowers/specs/2026-10-24-calsync-v2-product-design.md` §5 (block ③), sections 5.1, 5.2, 5.3 (overview and run history only), 5.4 and 5.5.
- `PRODUCT.md`.
- The direction contract in `.impeccable/surfaces/src-calsync-web-static-index-html.md`.
- The chosen first-viewport sketch `.impeccable/mocks/decision/challenger-ticket.html`.
- The world reference boards `.impeccable/ref/ticket-board.png` and `.impeccable/ref/ticket-hero.png`. These set the craft bar and the world (type, palette, density, signature move), never the layout.

**Deliberately deferred:**
- the onboarding wizard;
- the settings page and config editing;
- the rule detail page;
- the QR code;
- notifications;
- `approve` / `pause` from the UI.

The menu must not link to pages that do not exist.

## Global Constraints

- Branch: `feat/block2-daemon` (this slice stacks on block ②), worktree `../calsync-worktrees/block2`. Never commit to `main`.
- **Never touch the production installation:**
  - never touch `~/.config/calsync`, `~/.local/state/calsync`, `~/Library/LaunchAgents`, `launchctl` or `uv tool`;
  - never read keychain entries or credential files;
  - tests and screenshots use synthetic state built with `tests/harness.py`.
- **Checks after every task.** All of these must be clean:
  - `uv run pytest -W error -q`
  - `uv run ruff check src tests`
  - `uv run ruff format --check src tests`
  - `uv run pyright`
  - UI tests are marked `ui` and are skipped when the Playwright browsers are missing.
- TDD for Python. For the front end: write the Playwright or ASGI test first where practical.
- **Synthetic data only:** in tests, fixtures, screenshots and docs. Use the sketch's content: 晨跑, 周会, 和朋友吃午饭, 接孩子放学, 家庭视频通话, 缴物业费 and the calendar 家庭日程. No real names, no medical terms.
- **Privacy:** never log titles. The access log is off (uvicorn `access_log=False`), because URLs could carry the pairing key.
- **Plain HTTP:**
  - No Service Worker, Web Push, `navigator.clipboard`-only paths, or `crypto.subtle`.
  - No external network requests from the page: every script, style and font is served by calsync.
- **Exact values:**
  - Default listen address: `127.0.0.1:8766`.
  - Key file: `<state_dir>/web.key`, mode `0o600`, holding `secrets.token_urlsafe(32)`.
  - Cookie:
    - name `calsync_session`;
    - value `hmac.new(key, b"calsync-session-v1", sha256).hexdigest()`;
    - attributes `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age=34560000` (400 days);
    - plus `Secure` when the request scheme is https.
  - Pair URL: `http://<host>:<port>/?k=<key>`.
  - Static mount: `/static`. HTML is served with `Cache-Control: no-cache`.
  - Polling: status and upcoming every 30 s while the page is visible; after "sync now", every 3 s until `last_sync_at` changes or 90 s pass.
  - Breakpoint: the phone layout applies at `max-width: 760px`, or at `(pointer: coarse) and (max-width: 1024px)`.
  - Tap targets ≥ 44 px. Input font size 16 px.
- **Design (binding, from the direction contract):**
  - carrier navy `#0D1B3D` header field;
  - jet red `#D7262D`, used only for the validation stamp and for attention;
  - coupon white `#FFFFFF` on a stock-white `#F2F2F0` ground;
  - carbon purple `#4B2E83` for filled figures (times, counts);
  - hairline navy rules;
  - dashed perforations between coupons;
  - uppercase, letter-spaced tab-caps labels;
  - Barlow Condensed for the Latin display, labels and numerals, with tabular figures; the system CJK stack (`-apple-system, "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", sans-serif`) for Chinese and body text;
  - dark mode: a navy-black ground, deep-navy coupons and a brighter stamp red, all at AA contrast.
- **Signature interaction:** when a coupon's delivery changes from `pending` to `delivered` while the page is open, the red `已送达 · 验讫` stamp comes down once in about 180 ms (scale 1.25 → 1, opacity 0 → 1, with a slight rotation settle). Under `prefers-reduced-motion` it appears instantly. Reverted and deleted changes render struck through with a `作废` / `VOID` stamp.
- **Impeccable craft floor:** `~/.claude/plugins/cache/impeccable/impeccable/4.7.0/skills/impeccable/reference/craft-floor.md`. Read it before writing UI code. In particular:
  - no eyebrow or kicker above headings;
  - no emoji or Unicode glyph icons; icons are inline SVG in one stroke weight;
  - no nested cards;
  - no coloured side borders thicker than 1 px;
  - themed selection, focus rings and scrollbars;
  - tabular numerals.
- **Commits:**
  - Conventional Commits.
  - Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## File Structure

| File | Change |
| --- | --- |
| `src/calsync/config.py` | `[web]` section: `WebConfig(listen: str = "127.0.0.1:8766", allowed_hosts: tuple[str, ...] = ())` |
| `src/calsync/web/__init__.py`, `src/calsync/web/auth.py` (new) | key file, cookie token, pairing routes, Host and Origin checks |
| `src/calsync/web/app.py` (new) | `create_web_app(...)`: the block ② API routes behind auth, plus pairing, index and static |
| `src/calsync/daemon.py` | serve TCP with `create_web_app`; `--listen` override |
| `src/calsync/cli.py` | `daemon --listen HOST:PORT`; `web pair`; `web reset-key` |
| `src/calsync/web/static/index.html` | shell; theme applied before first paint; module entry |
| `src/calsync/web/static/app.js` | Preact components: App, Header, StatusLine, AttentionCard, ConnectionStrip, CouponList, Coupon, Stamp, ChangeList, RunsPage |
| `src/calsync/web/static/i18n.js` | English keys → Chinese |
| `src/calsync/web/static/styles.css` | tokens (light/dark), layout, components |
| `src/calsync/web/static/pair.html` | "paste your key" page for browsers without a cookie |
| `src/calsync/web/static/vendor/` | `htm-preact-standalone.mjs` plus LICENSE files; `fonts/barlow-condensed-*.woff2` plus OFL.txt |
| `pyproject.toml` | package data includes `web/static/**`; dev group adds `playwright` |
| `tests/test_web_auth.py`, `tests/test_web_app.py`, `tests/ui/test_ui.py` | tests |
| `README.md`, `docs/ROADMAP.md` | Task 7 |

---

### Task 5: TCP listener, pairing and auth

**Files:**
- `config.py`, `web/auth.py`, `web/app.py`, `daemon.py`, `cli.py`
- tests

**Interfaces:**
- **Consumes:** the block ② `api.create_app` routes. Refactor if needed so the route list can be mounted in a second app.
- **Produces:**
  - `create_web_app(*, config_provider, state_dir, daemon, listen_host, clock=...) -> Starlette`;
  - `auth.load_or_create_key(state_dir) -> str`;
  - `auth.reset_key(state_dir) -> str`;
  - `auth.session_token(key) -> str`.
- **Behaviour:**
  - **`GET /`**
    - If the query has `k`:
      - A valid key (constant-time compare via `hmac.compare_digest`) sets the cookie and returns 303 to `/` without the query.
      - An invalid key returns 403 with the pair page and a short "This link is not valid any more." notice.
    - Without `k`: with a valid cookie it serves `index.html`; otherwise it serves `pair.html` with 200.
  - **`POST /pair`** (form field `key`): same check; it sets the cookie and returns 303 to `/`. Rate limit: after 10 wrong keys in 10 minutes from one client IP, return 429.
  - **`/api/*`** over TCP requires the cookie, or returns 401 JSON `{"error": {"kind": "unpaired", ...}}`.
  - **`/static/*`** is public: no secrets are served there.
  - **Host check** on every request:
    - The `Host` header's hostname must be one of: `localhost`, `127.0.0.1`, `::1`, the listen host when it is not `0.0.0.0` / `::`, or anything in `allowed_hosts`.
    - When the listen host is `0.0.0.0` or `::`, also allow any IP literal; hostnames still need `allowed_hosts`.
    - Otherwise 400. This is protection against DNS rebinding.
  - **Origin check** on non-GET requests: when `Origin` is present, its host:port must equal the `Host` header; otherwise 403.
  - **Key file:** the key is read from disk on each pairing attempt and the cookie token is cached with the file's mtime, so `calsync web reset-key` takes effect without a restart.
  - **Daemon:** serves TCP on `--listen` or `config.web.listen` with `access_log=False`, alongside the Unix socket. In `--no-sync` mode as well.
  - **`calsync web pair`:**
    - Creates the key if missing.
    - Prints one pair URL per address: `localhost`, plus each `allowed_hosts` entry, plus the listen host when it is a concrete IP.
    - Then prints "Anyone with this link can see your calendar sync status. Reset it with `calsync web reset-key`."
  - **`calsync web reset-key`:** rewrites the key and prints the new pair URL(s).
  - **`pair.html`:** a minimal page in the product's look, with:
    - a heading,
    - one sentence on how to get the link (`calsync web pair`),
    - a key input (16 px font),
    - a "Pair" button.

  No JavaScript is required for it.
- [ ] **Step 1: Failing tests** (Starlette `TestClient`; `base_url` sets the Host):
  - **Pairing:** key → cookie → 303; a bad key → 403; a cookie that becomes invalid after `reset_key`.
  - **API:** 401 without a cookie; 200 with one.
  - **Host check:** an unknown host → 400; an allowed host → 200; an IP literal is allowed only with a wildcard listen host.
  - **Origin check:** a cross-origin `POST /api/trigger` → 403.
  - **Rate limit:** 429 after 10 bad keys.
  - **Key file:** mode `0o600`.
  - **CLI:** `web pair` output format.
  - **Daemon:** `--no-sync` serves TCP on an ephemeral port. A real request over TCP with `httpx` checks `/static/...` and a 401 on `/api`.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Full checks, then commit** `feat(web): tcp listener with key-link pairing, cookie auth and host checks`.

---

### Task 6: Front end, overview and run history

**Files:**
- `src/calsync/web/static/*` (all)
- `pyproject.toml` (package data)
- `tests/test_web_app.py` (index and static served)

**Interfaces:**
- **Consumes:** the JSON shapes in the block ② plan, Task 2 and Task 4: `/api/status`, `/api/upcoming`, `/api/runs`, `/api/changes?run_id=`, `POST /api/trigger`.
- **Vendored files** (exact versions, downloaded once into the repo, licenses alongside):
  - `https://cdn.jsdelivr.net/npm/htm@3.1.1/preact/standalone.module.js` → `vendor/htm-preact-standalone.mjs`, plus the htm and preact MIT licenses;
  - Barlow Condensed 500, 600 and 700 latin woff2 from `@fontsource/barlow-condensed@5.1.0` (`files/barlow-condensed-latin-{500,600,700}-normal.woff2`) → `vendor/fonts/`, plus OFL.txt.
  - Verify each file's size is non-trivial and its first bytes are as expected (`wOF2` for the fonts; JavaScript for the module).
- **Behaviour (overview, `#/`):**
  - **Header:** carrier-navy flat field. Contents:
    - the `calsync` wordmark in Barlow Condensed;
    - a status dot plus a word (正常 / 需要处理 / 出错 / 只读演示);
    - a white primary button `立即同步` with a refresh icon;
    - a menu button (SVG). The menu holds the language (中文 / English), the theme (跟随系统 / 浅色 / 深色), and "关于 calsync" (version).
  - **`立即同步` button:**
    - Disabled with the label "演示模式" when `daemon.sync` is false or `daemon` is null.
    - On click it POSTs to `/api/trigger` and shows a busy state, then polls as specified.
    - Errors appear inline under the header.
  - **Tabs:** `总览` · `同步记录`, standard and keyboard-accessible.
  - **Status sentence:**
    - `一切正常`, with a detail line such as `1 分钟前同步 · 24 小时内同步了 12 处改动`, built from `summary.last_sync_at` and `summary.changes_24h`;
    - `有 N 件事需要你处理` when `attention` is non-empty;
    - `还没有同步过` when health is `never_run`.
  - **Attention cards:** only when there is something to show. One card per item, with a localized message composed from `kind` + `rule` + `account` (fallback: the API `message`) and the command shown as copyable text. Copy tries `navigator.clipboard`, then `execCommand`, then selects the text.
  - **Connection strip** (one per rule): a boarding-pass strip.
    - `出发 FROM`: the service mark (authored SVG monogram), the host name, the calendar name and the account.
    - A dashed arc with a health badge (`正常` / `授权失效` / `已拦截` / `失败` / `未运行`).
    - `到达 TO` for the target, laid out the same way.
    - Footer: the scope (`全部日程` or `过去 N 天 – 未来 M 天`), the copy count (`17 个系列`) and the direction `单向`.
    - When `schema.current` is false, a quiet note: `状态来自旧版本 calsync，送达状态未知`.
  - **`接下来 24 小时` coupon list:**
    - One coupon per event: the time in carbon-purple tabular figures (or `全天`, with `明天` as a prefix for tomorrow), the title, the location, and the alerts (`提醒：开始时`, `提前 10 分钟`, or `无提醒` when empty).
    - The delivery cell on the right:
      - `delivered` → the red rotated stamp `已送达 · 验讫`;
      - `pending` → an outlined navy box `待送达`;
      - `unknown` → a quiet `未知`.
    - Coupons are separated by dashed perforations, with notches at the perforation ends drawn with radial masks or pseudo-elements: no images.
    - Tapping a coupon expands it in place to show the rule, the target calendar, and whether the event is recurring.
  - **`最近改动`:** the latest 5 changes from `/api/changes`.
    - Each row shows the time, the action and the title.
    - The action is shown as a small tab-caps tag: 新增 / 修改 / 改回 / 删除 / 受保护 / 已拦截.
    - `reverted` and `deleted` rows strike the title through and add the `作废` stamp.
    - A link `全部同步记录 →` opens the runs page.
  - **Desktop layout** (wider than the breakpoint): status and attention across the top; below them, the left column holds the connection strips and recent changes, and the right column holds the coupon list. Content max-width is about 1200 px.
- **Behaviour (run history, `#/runs`):**
  - One row per run (`/api/runs?limit=100`), newest first, styled as carbon copies.
  - Each row shows the start time, the duration, and the counts as figures: `+created`, `~updated`, `↺reverted`, `−deleted`. Use small SVG icons for these, not Unicode arrows.
  - It also shows the request count and a status tag.
  - Idle runs (all counts zero, status ok) collapse into one row per contiguous group: `14:20–15:20 · 61 次空闲同步`.
  - Expanding a run with changes loads `/api/changes?run_id=N` and lists them.
- **States:**
  - **Loading:** coupon-shaped skeletons, not spinners.
  - **Empty upcoming:** `接下来 24 小时没有日程`.
  - **`no_state` (503):** `还没有同步数据，启动同步后这里会显示状态`.
  - **401:** redirect to `/`, which serves the pair page.
  - **Network error:** a banner `连不上 calsync` with a retry button.
- **i18n:**
  - English strings are the keys; Chinese is the default when `navigator.language` starts with `zh`.
  - The choice is stored in `localStorage`, wrapped in try/catch.
  - Dates and times use `Intl.DateTimeFormat` in the browser's time zone.
- **Theme:**
  - An inline `<script>` in `<head>` sets `data-theme` before first paint, from `localStorage` (try/catch) or `prefers-color-scheme`.
  - Tokens are CSS custom properties for light and dark.
- **Accessibility:**
  - Status is always shown with a word, not only colour.
  - Visible, themed focus rings.
  - `aria-live="polite"` on the status sentence.
  - Coupons are buttons with `aria-expanded`.
- [ ] **Step 1: Failing ASGI tests:**
  - `/` with a cookie returns HTML that references `/static/app.js`;
  - every static file referenced from `index.html` is served with 200;
  - the vendored files exist and are non-empty;
  - `index.html` contains no `http://` or `https://` URL except inside comments or license text.
- [ ] **Step 2: Vendor the libraries and fonts. Build the front end.**
- [ ] **Step 3: Check by looking.**
  - Start a `--no-sync` daemon on a synthetic state: write a small script that builds a `tmp` state with the harness and two cycles, then runs the daemon with `--listen 127.0.0.1:<port>`.
  - Capture with headless Chrome or Playwright: 390×844 and 1440×900, light and dark.
  - Fix what one look shows before the next look; at most two rounds.
- [ ] **Step 4: Full checks, then commit** `feat(web): ticket-wallet overview and run history`.

---

### Task 7: UI tests, docs and finish

**Files:**
- `tests/ui/test_ui.py`
- `pyproject.toml` (dev dependency `playwright`, pytest marker `ui`)
- `README.md`, `docs/ROADMAP.md`

- [ ] **Step 1: Playwright tests**, marked `ui` and skipped when `playwright` or its browsers are missing.
  - Start the app in-process with uvicorn on an ephemeral port, on synthetic state.
  - Pair by visiting `/?k=<key>`.
  - Run in Chromium and WebKit:
    - at 360×760 and 1440×900, `document.documentElement.scrollWidth <= innerWidth`;
    - the header, the status sentence, the connection strip and at least one coupon are visible;
    - the `立即同步` button is at least 44 px tall;
    - no console errors;
    - switching to English changes the tab labels;
    - the dark theme applies `data-theme="dark"` before the first paint (check the attribute at `DOMContentLoaded` via an init script);
    - the runs page lists runs.
  - In a test with a pending event: run another cycle, wait for the stamp, and assert that it becomes `已送达`.
- [ ] **Step 2: Docs.**
  - README section "Web UI": start the daemon, `calsync web pair`, open the link, remote access via Tailscale or Surge Ponte using `[web] listen` and `allowed_hosts`, and the privacy note.
  - ROADMAP: mark the block ③ slice done and list the deferred pages.
- [ ] **Step 3: Full checks, then commit** `test(web): playwright checks for layout, theme and language` and `docs: web ui`.
