"""Layout and accessibility across viewports, in Chromium and WebKit: at 360×760, 390×844,
768×1024 (touch), 1280×800 and 1440×900, each screen — the map, a line's inspector, the settings,
an event's detail, the calendar's views and the activity — has no horizontal overflow, no button
cut off at the viewport's edge and no two map labels on top of each other; the phone's sheet is at
most 88 % of the viewport and scrolls; every focusable control has a name; focus is visible."""

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from calsync import events_view
from tests.browser.conftest import OpenPage, ready
from tests.browser.live import FAMILY

pytestmark = pytest.mark.browser

VIEWPORTS = [
    pytest.param({"width": 360, "height": 760}, True, id="360"),
    pytest.param({"width": 390, "height": 844}, True, id="390"),
    pytest.param({"width": 768, "height": 1024}, True, id="768-touch"),
    pytest.param({"width": 1280, "height": 800}, False, id="1280"),
    pytest.param({"width": 1440, "height": 900}, False, id="1440"),
]

# A seeded single event (社区活动), by the id calsync gives it.
DENTIST = events_view.event_id("main", FAMILY, "dentist", None)

SCREENS = {
    "map": "/",
    "rule": "/#/rule/life-shared",
    "settings": "/#/settings",
    "activity": "/#/activity",
    "event": f"/#/event/{DENTIST}",
    "event-edit": f"/#/event-edit/{DENTIST}",
    "event-new": "/#/event-new",
}

# The page's own problems, measured in the page: what sticks out sideways, which visible buttons
# are cut by the viewport's edges, and which focusable controls have no name.
MEASURE = """() => {
  const vw = document.documentElement.clientWidth;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && !el.closest("[inert], [hidden]");
  };
  const clipped = [...document.querySelectorAll("button, a.btn, .layer, .pill")].filter(visible)
    .filter((el) => { const r = el.getBoundingClientRect(); return r.left < -1 || r.right > vw + 1; })
    .map((el) => (el.getAttribute("aria-label") || el.textContent || el.tagName).trim().slice(0, 40));
  const unnamed = [...document.querySelectorAll("button, a[href], input, [role=radio], [role=tab], [tabindex='0']")]
    .filter(visible)
    .filter((el) => !(el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || (el.labels && el.labels.length) || el.textContent.trim() || el.getAttribute("title") || el.getAttribute("placeholder")))
    .map((el) => el.outerHTML.slice(0, 80));
  return { overflow: document.documentElement.scrollWidth - vw, clipped, unnamed };
}"""

# Map pill labels whose boxes overlap another's (text on text).
OVERLAPS = """() => {
  const boxes = [...document.querySelectorAll(".canvas .cal, .canvas .acct-h, .canvas .hub-cap, .canvas .add-acct, .canvas .new-line")].map((el) => [el, el.getBoundingClientRect()])
    .filter(([, r]) => r.width > 0 && r.height > 0);
  const out = [];
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const [, a] = boxes[i], [, b] = boxes[j];
    const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    if (x > 2 && y > 2 && !boxes[i][0].contains(boxes[j][0]) && !boxes[j][0].contains(boxes[i][0]))
      out.push(`${boxes[i][0].textContent.trim().slice(0, 20)} / ${boxes[j][0].textContent.trim().slice(0, 20)}`);
  }
  return out;
}"""


def problems(page: Page) -> dict[str, Any]:
    return page.evaluate(MEASURE)


def check(page: Page, where: str) -> None:
    found = problems(page)
    assert found["overflow"] <= 0, f"{where}: the page is {found['overflow']} px wider than the viewport"
    assert not found["clipped"], f"{where}: buttons cut off: {found['clipped']}"
    assert not found["unnamed"], f"{where}: controls without a name: {found['unnamed']}"


@pytest.mark.parametrize(("viewport", "touch"), VIEWPORTS)
@pytest.mark.parametrize("screen", list(SCREENS))
def test_each_screen_fits(open_page: OpenPage, viewport: dict[str, int], touch: bool, screen: str) -> None:
    page = open_page(viewport=viewport, has_touch=touch, path=SCREENS[screen])
    ready(page)
    page.wait_for_timeout(600)  # the inspector's slide
    check(page, screen)
    if screen == "map":
        assert page.evaluate(OVERLAPS) == []


@pytest.mark.parametrize(("viewport", "touch"), VIEWPORTS)
def test_the_calendar_views_fit(open_page: OpenPage, viewport: dict[str, int], touch: bool) -> None:
    page = open_page(viewport=viewport, has_touch=touch)
    ready(page)
    views = page.locator("#pane-calendar .seg.views button")
    for i in range(views.count()):
        views.nth(i).click()
        expect(page.locator("#pane-calendar .cal-frame")).to_have_attribute("aria-busy", "false")
        check(page, f"calendar view {views.nth(i).inner_text()}")


@pytest.mark.parametrize(("viewport", "touch"), VIEWPORTS[:3])
def test_the_phone_sheet_is_at_most_88_percent_and_scrolls(
    open_page: OpenPage, viewport: dict[str, int], touch: bool
) -> None:
    page = open_page(viewport=viewport, has_touch=touch, path="/#/rule/life-shared")
    ready(page)
    page.wait_for_timeout(600)
    sheet = page.locator("aside.insp")
    box = sheet.bounding_box()
    assert box is not None and box["height"] <= viewport["height"] * 0.88 + 1
    body = sheet.locator(".ib")
    assert body.evaluate(
        "(el) => el.scrollHeight > el.clientHeight && getComputedStyle(el).overflowY === 'auto'"
    )


def test_focus_is_visible_on_every_kind_of_control(open_page: OpenPage) -> None:
    """Tab through the page: each focused control draws a focus ring (outline or box-shadow)."""
    page = open_page(path="/#/rule/life-shared")
    ready(page)
    page.wait_for_timeout(600)
    seen = 0
    for _ in range(25):
        page.keyboard.press("Tab")
        ring = page.evaluate(
            """() => {
              const el = document.activeElement;
              if (!el || el === document.body) return null;
              if (el.closest("svg")) return null; // a wire: its ring is drawn on the visible wire (test_map)
              const s = getComputedStyle(el);
              return (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0) || s.boxShadow !== "none";
            }"""
        )
        if ring is None:
            continue
        seen += 1
        assert ring, page.evaluate("() => document.activeElement.outerHTML.slice(0, 120)")
    assert seen >= 10


# Controls a finger taps, measured on the phone: buttons, links styled as buttons, radios, tabs,
# layer toggles and segmented choices (inline text links, the map's pills and the calendar's event
# blocks are exempt: a pill is a row, a block is as tall as its event, a link sits in a sentence).
SMALL_TARGETS = """() => [...document.querySelectorAll(
    "button, a.btn, [role=radio], [role=tab], .layer, .seg button")]
  .filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && !el.closest("[inert], [hidden]") && getComputedStyle(el).visibility !== "hidden"
      && !el.matches(".ev, .chip, .cal");
  })
  .filter((el) => { const r = el.getBoundingClientRect(); return r.height < 43.5 || r.width < 43.5; })
  .map((el) => `${(el.getAttribute("aria-label") || el.textContent).trim().slice(0, 24)} ${Math.round(el.getBoundingClientRect().width)}x${Math.round(el.getBoundingClientRect().height)}`)"""


@pytest.mark.parametrize(
    "screen",
    [
        "/",
        "/#/rule/life-shared",
        "/#/settings",
        "/#/activity",
        "/#/new",
        f"/#/event-edit/{DENTIST}",
        "/#/event-new",
    ],
)
def test_tap_targets_on_the_phone_are_44px(open_page: OpenPage, screen: str) -> None:
    page = open_page(viewport={"width": 390, "height": 844}, has_touch=True, path=screen)
    ready(page)
    page.wait_for_timeout(600)
    small = page.evaluate(SMALL_TARGETS)
    assert not small, "too small to tap: " + " | ".join(small)


@pytest.mark.parametrize("view", ["日", "双日", "月", "列表"])
def test_tap_targets_in_each_phone_calendar_view(open_page: OpenPage, view: str) -> None:
    page = open_page(viewport={"width": 390, "height": 844}, has_touch=True)
    ready(page)
    page.locator("#pane-calendar .seg.views").get_by_role("button", name=view, exact=True).click()
    expect(page.locator("#pane-calendar .cal-frame")).to_have_attribute("aria-busy", "false")
    small = page.evaluate(SMALL_TARGETS)
    assert not small, "too small to tap: " + " | ".join(small)
