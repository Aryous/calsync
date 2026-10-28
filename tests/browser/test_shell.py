"""The v4 shell against a live daemon, in Chromium and WebKit: it loads cleanly, carries the
concept's tokens and font, themes before first paint, fits a 360 px phone, opens and closes the
inspector, runs a cycle from 立即同步, and sends an unpaired browser to the pair page."""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.browser.conftest import (
    DESKTOP,
    NARROW,
    PHONE,
    VIEWPORT_IDS,
    OpenPage,
    ready,
    refresh_status,
    settled,
    sync_button,
    until,
    watch_of,
)
from tests.browser.live import LiveDaemon

pytestmark = pytest.mark.browser

BOTH = pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=VIEWPORT_IDS)
LIGHT_BG = "rgb(248, 250, 252)"  # --bg #F8FAFC
DARK_BG = "rgb(15, 18, 23)"  # --bg #0F1217

# When <html> first carries data-theme, and what the document looked like at that moment.
THEME_PROBE = """
window.__themeProbe = null;
new MutationObserver((records, observer) => {
  const root = document.documentElement;
  if (root && root.hasAttribute("data-theme") && !window.__themeProbe) {
    window.__themeProbe = {
      theme: root.getAttribute("data-theme"),
      readyState: document.readyState,
      body: document.body !== null,
    };
    observer.disconnect();
  }
}).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ["data-theme"] });
document.addEventListener("DOMContentLoaded", () => {
  window.__themeAtDcl = {
    theme: document.documentElement.getAttribute("data-theme"),
    background: getComputedStyle(document.body).backgroundColor,
  };
});
"""


def background(page: Page) -> str:
    return page.evaluate("getComputedStyle(document.body).backgroundColor")


@BOTH
def test_the_shell_loads_without_errors(
    paired_page: Page, live: LiveDaemon, viewport: dict[str, int]
) -> None:
    page = paired_page
    ready(page)
    expect(page).to_have_title("calsync")
    expect(page.locator("html")).to_have_attribute("lang", "zh-CN")
    expect(page.locator(".brand")).to_have_text("calsync")
    expect(sync_button(page)).to_be_visible()
    expect(page.get_by_role("link", name="设置").first).to_be_visible()
    expect(page.get_by_role("region", name="同步地图")).to_be_visible()
    expect(page.get_by_role("tab", name="日历")).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("tab", name="活动")).to_have_attribute("aria-selected", "false")
    assert live.get("/api/status")["summary"]["health"] == "ok"
    expect(page.locator(".result h1")).to_have_text("四条线路都在正常工作")
    # Only the result of 立即同步 is announced; the ticking "N 分钟前" line is not a live region.
    assert page.locator("[aria-live]").count() == 1
    expect(page.locator("[data-role=result-note]")).to_have_attribute("role", "status")
    expect(page.locator("[data-role=result-note]")).to_have_text("")
    # The bar is 68 px (60 on the phone) and its buttons keep their size inside it.
    bar = page.locator(".top").bounding_box()
    assert bar is not None and bar["height"] == (68 if viewport == DESKTOP else 60)
    for box in page.locator(".top .btn").evaluate_all(
        "els => els.map(e => e.getBoundingClientRect().toJSON())"
    ):
        assert box["height"] == (38 if viewport == DESKTOP else 44), box
    assert watch_of(page).problems() == []


def test_the_tokens_are_the_concepts(open_page: OpenPage) -> None:
    light = open_page()
    assert light.evaluate("document.documentElement.dataset.theme") == "light"
    assert background(light) == LIGHT_BG
    ink = light.evaluate("getComputedStyle(document.body).color")
    assert ink == "rgb(31, 41, 55)"  # --ink #1F2937

    dark = open_page(path="/?theme=dark")
    assert dark.evaluate("document.documentElement.dataset.theme") == "dark"
    assert background(dark) == DARK_BG

    system = open_page(color_scheme="dark")  # no choice made: the system's
    assert background(system) == DARK_BG

    chosen = open_page(color_scheme="dark", path="/?theme=light")
    assert background(chosen) == LIGHT_BG


def test_dark_mode_applies_before_first_paint(open_page: OpenPage) -> None:
    page = open_page(color_scheme="dark")
    page.add_init_script(THEME_PROBE)
    page.reload()
    probe = page.evaluate("window.__themeProbe")
    assert probe == {"theme": "dark", "readyState": "loading", "body": False}
    assert page.evaluate("window.__themeAtDcl") == {"theme": "dark", "background": DARK_BG}

    page.goto("/?theme=dark")
    assert page.evaluate("window.__themeProbe")["body"] is False


def test_instrument_sans_loads(paired_page: Page) -> None:
    page = paired_page
    ready(page)
    loaded = page.evaluate(
        """async () => {
          await document.fonts.ready;
          await document.fonts.load('600 16px "Instrument Sans"', "calsync");
          return [...document.fonts]
            .filter((f) => f.family.replace(/"/g, "") === "Instrument Sans")
            .map((f) => f.status);
        }"""
    )
    assert "loaded" in loaded
    family = page.evaluate("getComputedStyle(document.querySelector('.brand')).fontFamily")
    assert family.replace('"', "").startswith("Instrument Sans")


def test_the_font_has_real_weights_from_400_to_700(paired_page: Page) -> None:
    """The vendored file is variable (wght 400–700): every weight the UI uses is drawn, none
    synthesised. Synthetic bold would make 400 and 500 alike, and 600 and 700 alike."""
    page = paired_page
    ready(page)
    widths = page.evaluate(
        """async () => {
          const out = [];
          for (const w of [400, 500, 600, 700]) {
            await document.fonts.load(`${w} 40px "Instrument Sans"`, "calsync 2026");
            const ctx = document.createElement("canvas").getContext("2d");
            ctx.font = `${w} 40px "Instrument Sans"`;
            out.push(ctx.measureText("calsync 2026 MWmw").width);
          }
          return out;
        }"""
    )
    assert widths == sorted(widths) and len(set(widths)) == 4, widths


@pytest.mark.parametrize("path", ["/", "/#/settings"])
def test_no_horizontal_overflow_at_360(open_page: OpenPage, path: str) -> None:
    page = open_page(viewport=NARROW, path=path)
    ready(page)
    widths = page.evaluate(
        "({ doc: document.documentElement.scrollWidth, body: document.body.scrollWidth, view: window.innerWidth })"
    )
    assert widths["doc"] <= widths["view"] and widths["body"] <= widths["view"], widths
    # Every tap target in the header is at least 44 px on the phone.
    for box in page.locator(".top .btn").evaluate_all(
        "els => els.map(e => e.getBoundingClientRect().toJSON())"
    ):
        assert box["width"] >= 44 and box["height"] >= 44, box


@BOTH
def test_the_inspector_opens_on_settings_and_closes(paired_page: Page, viewport: dict[str, int]) -> None:
    page = paired_page
    ready(page)
    inspector = page.locator("aside.insp")
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))
    expect(inspector).to_have_attribute("aria-hidden", "true")

    page.locator(".top-r").get_by_role("link", name="设置").click()
    expect(page).to_have_url(re.compile(r"#/settings$"))
    expect(inspector).to_have_class(re.compile(r"\bopen\b"))
    expect(inspector.locator("h2")).to_have_text("设置")
    expect(inspector).to_be_focused()
    expect(page.locator("body")).to_have_class(re.compile(r"\binsp-open\b"))
    settled(page)
    if viewport == DESKTOP:  # a right panel, and the page shifts left so the map stays visible
        box = inspector.bounding_box()
        assert box is not None and box["x"] + box["width"] <= DESKTOP["width"] and box["width"] == 440
        canvas = page.locator(".canvas").bounding_box()
        assert canvas is not None and canvas["x"] + canvas["width"] <= box["x"]
    else:  # a bottom sheet, at most 88 % of the viewport high
        box = inspector.bounding_box()
        assert (
            box is not None and box["height"] <= 0.88 * PHONE["height"] + 1 and box["width"] == PHONE["width"]
        )

    page.keyboard.press("Escape")
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))
    expect(page).to_have_url(re.compile(r"/(#/?)?$"))
    expect(page.locator("body")).not_to_have_class(re.compile(r"\binsp-open\b"))

    # The hub opens the settings too (its caption: the tile carries the lines' traces); the close
    # button closes.
    page.locator(".hubwrap .hub-cap").click()
    expect(inspector).to_have_class(re.compile(r"\bopen\b"))
    inspector.get_by_role("button", name="关闭").click()
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))

    page.locator(".top-r").get_by_role("link", name="设置").click()  # and the browser's back
    expect(inspector).to_have_class(re.compile(r"\bopen\b"))
    page.go_back()
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))
    expect(page).to_have_url(re.compile(r"/(#/?)?$"))


@pytest.mark.parametrize(
    ("hash_", "title"),
    [
        ("#/rule/life-shared", "线路"),
        ("#/account/main", "账号"),
        ("#/new", "新线路"),
        ("#/blocked/work-me", "被拦下的删除"),
    ],
)
def test_every_inspector_route_opens_the_container(open_page: OpenPage, hash_: str, title: str) -> None:
    page = open_page(path=f"/{hash_}")
    inspector = page.locator("aside.insp")
    expect(inspector).to_have_class(re.compile(r"\bopen\b"))
    expect(inspector.locator("h2")).to_have_text(title)
    page.keyboard.press("Escape")  # opened by loading the link: closing stays on this page
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))
    expect(page).to_have_url(re.compile(r"/(#/?)?$"))


def test_an_unknown_route_goes_to_the_map(open_page: OpenPage) -> None:
    page = open_page(path="/#/nowhere/at/all")
    expect(page).to_have_url(re.compile(r"/(#/?)?$"))
    expect(page.locator("aside.insp")).not_to_have_class(re.compile(r"\bopen\b"))


def test_the_activity_route_selects_the_tab(open_page: OpenPage) -> None:
    page = open_page(path="/#/activity")
    expect(page.get_by_role("tab", name="活动")).to_have_attribute("aria-selected", "true")
    expect(page.locator("aside.insp")).not_to_have_class(re.compile(r"\bopen\b"))
    page.get_by_role("tab", name="日历").click()
    expect(page.locator("#pane-calendar")).to_be_visible()
    expect(page.locator("#pane-activity")).to_be_hidden()


def test_english_is_available(open_page: OpenPage) -> None:
    page = open_page(path="/?lang=en")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.get_by_role("button", name="Sync now")).to_be_visible()
    expect(page.get_by_role("tab", name="Activity")).to_be_visible()
    page.goto("/")  # remembered
    expect(page.get_by_role("tab", name="Activity")).to_be_visible()
    page.get_by_role("link", name="中文").click()
    expect(page.get_by_role("tab", name="活动")).to_be_visible()


@BOTH
def test_sync_now_runs_a_cycle_and_shows_the_result(
    open_page: OpenPage, fresh_live: LiveDaemon, viewport: dict[str, int]
) -> None:
    page = open_page(daemon=fresh_live, viewport=viewport)
    ready(page)
    before = fresh_live.cycles()
    sync_button(page).click()
    # The pending lunch reaches 我的日历: the one change of this cycle.
    expect(page.locator("[data-role=result-note]")).to_have_text("同步完成，送达了 1 处改动")
    assert fresh_live.cycles() == before + 1
    expect(sync_button(page)).to_have_accessible_name("立即同步")
    expect(sync_button(page)).to_be_enabled()
    status = fresh_live.get("/api/status")
    assert status["summary"]["health"] == "ok"


def test_an_unpaired_browser_gets_the_pair_page(open_page: OpenPage) -> None:
    page = open_page(paired=False)
    expect(page.locator("form[action='/pair'] input#key")).to_be_visible()
    expect(page.get_by_role("heading", name="连接这个浏览器")).to_be_visible()
    assert background(page) == LIGHT_BG
    assert watch_of(page).problems() == []


def test_a_401_sends_the_browser_to_the_pair_page(open_page: OpenPage) -> None:
    page = open_page()
    ready(page)
    watch_of(page).allow(r"\b401\b")  # the refused request is logged by the browser
    page.context.clear_cookies()
    sync_button(page).click()
    expect(page.locator("form[action='/pair'] input#key")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/$"))


def test_the_tabs_work_from_the_keyboard(open_page: OpenPage) -> None:
    """The ARIA tabs pattern: one tab stop; ←/→ wrap, Home and End jump; each moves focus and
    selects."""
    page = open_page()
    ready(page)
    calendar, activity = page.get_by_role("tab", name="日历"), page.get_by_role("tab", name="活动")
    expect(activity).to_have_attribute("tabindex", "-1")
    calendar.focus()  # where Tab lands in the tab list: the selected tab
    page.keyboard.press("ArrowRight")
    expect(activity).to_be_focused()
    expect(activity).to_have_attribute("aria-selected", "true")
    expect(page.locator("#pane-activity")).to_be_visible()
    expect(page.locator("#pane-calendar")).to_be_hidden()
    page.keyboard.press("ArrowRight")  # wraps
    expect(calendar).to_be_focused()
    expect(calendar).to_have_attribute("aria-selected", "true")
    page.keyboard.press("End")
    expect(activity).to_be_focused()
    page.keyboard.press("Home")
    expect(calendar).to_be_focused()
    page.keyboard.press("ArrowLeft")  # wraps the other way
    expect(activity).to_have_attribute("aria-selected", "true")


def test_the_tabs_are_reached_with_tab_alone(open_page: OpenPage) -> None:
    page = open_page()
    ready(page)
    activity = page.get_by_role("tab", name="活动")
    for _ in range(40):  # from the top of the page, Tab only, then an arrow
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement && document.activeElement.getAttribute('role')") == "tab":
            break
    expect(page.get_by_role("tab", name="日历")).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(activity).to_be_focused()
    expect(activity).to_have_attribute("aria-selected", "true")


def test_sync_now_waits_while_a_cycle_runs(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    """A cycle the daemon is running (the poll's cycle_started_at) shows 同步中… and makes the
    button aria-disabled: a second trigger cannot fire."""
    page = open_page(daemon=fresh_live)
    ready(page)
    fresh_live.hold_cycles()
    fresh_live.hold_cycles()  # holding twice keeps one gate: the release below frees the cycle
    before = fresh_live.cycles()
    try:
        assert fresh_live.in_loop(lambda: fresh_live.daemon.request_cycle()) is True
        refresh_status(page)
        button = sync_button(page)
        expect(button).to_have_accessible_name("同步中…")
        expect(button).to_have_attribute("aria-disabled", "true")
        expect(button).to_have_attribute("aria-busy", "true")
        button.click(force=True)  # aria-disabled only: still clickable, and the guard sends nothing
    finally:
        fresh_live.release_cycles()
    until(lambda: fresh_live.cycles() == before + 1)
    refresh_status(page)
    expect(sync_button(page)).to_have_accessible_name("立即同步")
    expect(sync_button(page)).to_have_attribute("aria-disabled", "false")
    expect(page.locator("[data-role=result-note]")).to_have_text("")
    page.wait_for_timeout(300)
    assert fresh_live.cycles() == before + 1  # the click during the cycle requested nothing


def test_focus_stays_on_sync_now_while_it_runs(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    """From the keyboard: Enter starts a sync, focus stays on the button through 同步中… (it is
    aria-disabled, not disabled), a second Enter sends nothing, and the result is announced."""
    page = open_page(daemon=fresh_live)
    ready(page)
    button = sync_button(page)
    button.focus()
    fresh_live.hold_cycles()
    before = fresh_live.cycles()
    try:
        page.keyboard.press("Enter")
        expect(button).to_have_accessible_name("同步中…")
        expect(button).to_have_attribute("aria-disabled", "true")
        expect(button).to_be_focused()
        page.keyboard.press("Enter")
    finally:
        fresh_live.release_cycles()
    expect(page.locator("[data-role=result-note]")).to_have_text("同步完成，送达了 1 处改动")
    expect(button).to_have_accessible_name("立即同步")
    expect(button).to_be_focused()
    page.wait_for_timeout(300)
    assert fresh_live.cycles() == before + 1


def test_closing_leaves_no_inspector_behind_in_history(open_page: OpenPage) -> None:
    """Map → settings → a line: closing returns to the map entry, so Back does not reopen
    either inspector."""
    page = open_page()
    ready(page)
    inspector = page.locator("aside.insp")
    page.evaluate("location.hash = '#/activity'")  # a map entry of its own
    expect(page.get_by_role("tab", name="活动")).to_have_attribute("aria-selected", "true")
    page.locator(".top-r").get_by_role("link", name="设置").click()
    expect(inspector.locator("h2")).to_have_text("设置")
    page.evaluate("location.hash = '#/rule/life-shared'")
    expect(inspector.locator("h2")).to_have_text("线路")
    page.evaluate("window.scrollTo(0, 0)")
    page.keyboard.press("Escape")
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))
    expect(page).to_have_url(re.compile(r"#/activity$"))
    page.wait_for_timeout(200)
    assert page.evaluate("window.scrollY") == 0  # coming back to #/activity does not scroll
    page.go_back()
    expect(page).to_have_url(re.compile(r"/(#/?)?$"))
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))


def test_closing_a_linked_inspector_chain_turns_it_into_the_map(open_page: OpenPage) -> None:
    """An inspector loaded from a link, then another: closing goes back to the first and makes
    that entry the map; Back from there reopens nothing."""
    page = open_page()
    ready(page)
    page.goto("/?lang=zh#/settings")  # a fresh document, loaded with an inspector hash
    inspector = page.locator("aside.insp")
    expect(inspector.locator("h2")).to_have_text("设置")
    page.evaluate("location.hash = '#/account/main'")
    expect(inspector.locator("h2")).to_have_text("账号")
    page.keyboard.press("Escape")
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))
    expect(page).to_have_url(re.compile(r"\?lang=zh#/$"))
    page.go_back()  # to the page before this document: no inspector of ours reopens
    expect(page).not_to_have_url(re.compile(r"#/(settings|account)"))


def frames(page: Page) -> None:
    """Two painted frames: the app's effects have run, as for a person who acts after seeing it."""
    page.evaluate("new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")


def in_app(page: Page, live: LiveDaemon) -> None:
    """Still this app's document: the page did not leave it."""
    assert page.url.startswith(live.url), page.url
    expect(page.locator(".brand")).to_have_text("calsync")


def test_closing_a_linked_chain_back_to_the_same_inspector(open_page: OpenPage, live: LiveDaemon) -> None:
    """Loaded at #/settings, then #/account/main, then #/settings again: the first close
    travels to the first entry (same fragment: only popstate fires) and closes there; further
    closes are harmless and the page never leaves the app."""
    page = open_page()
    ready(page)
    page.goto("/?lang=zh#/settings")
    inspector = page.locator("aside.insp")
    expect(inspector.locator("h2")).to_have_text("设置")
    frames(page)
    page.evaluate("location.hash = '#/account/main'")
    expect(inspector.locator("h2")).to_have_text("账号")
    frames(page)
    page.evaluate("location.hash = '#/settings'")
    expect(inspector.locator("h2")).to_have_text("设置")
    page.keyboard.press("Escape")
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))
    expect(page).to_have_url(re.compile(r"\?lang=zh#/$"))
    page.keyboard.press("Escape")  # nothing open: nothing happens
    page.wait_for_timeout(300)
    in_app(page, live)
    expect(page).to_have_url(re.compile(r"\?lang=zh#/$"))


@pytest.mark.parametrize(
    "steps",
    [
        ["#/settings"],
        ["#/settings", "#/rule/life-shared"],
        ["#/settings", "#/account/main", "#/settings"],
        ["#/activity", "#/settings", "#/settings"],
        ["#/rule/life-shared", "#/", "#/settings", "#/account/work", "#/settings", "#/account/work"],
    ],
    ids=lambda steps: "-".join(step.strip("#/").replace("/", ".") or "map" for step in steps),
)
@pytest.mark.parametrize("loaded", [False, True], ids=["from-map", "from-link"])
def test_after_any_close_the_app_is_loaded_and_closed(
    open_page: OpenPage, live: LiveDaemon, steps: list[str], loaded: bool
) -> None:
    """Whatever the trail of hashes (from the map, or from a page loaded with the first one),
    one close leaves the inspector closed in this app, and a quick second close changes nothing."""
    page = open_page()
    ready(page)
    trail = list(steps)
    if loaded:
        page.goto(f"/?lang=zh{trail.pop(0)}")
    for step in trail:
        frames(page)
        page.evaluate(f"location.hash = {step!r}")
    page.wait_for_timeout(100)
    inspector = page.locator("aside.insp")
    if "open" not in (inspector.get_attribute("class") or ""):
        return  # the trail ended on the map: nothing to close
    inspector.get_by_role("button", name="关闭").click()
    page.keyboard.press("Escape")  # a second close at once, while the first may still travel
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))
    page.wait_for_timeout(300)
    in_app(page, live)
    expect(inspector).not_to_have_class(re.compile(r"\bopen\b"))
    assert parse_hash(page) in {"", "#/", "#/activity"}


def parse_hash(page: Page) -> str:
    return page.evaluate("location.hash")
