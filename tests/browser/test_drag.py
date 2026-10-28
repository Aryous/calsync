"""Dragging events in the calendar, in Chromium and WebKit, against a daemon of each test's own.

The browser's clock is the daemon's: Monday 2026-10-26, 10:42 local. The week shows 26 Oct – 1 Nov."""

import re
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser.conftest import OpenPage
from tests.browser.live import FAMILY, LiveDaemon
from tests.factories import google_event

pytestmark = pytest.mark.browser


def calendar(page: Page) -> Locator:
    return page.locator("#pane-calendar .calendar")


def ready(page: Page) -> None:
    expect(calendar(page).locator(".cal-frame")).to_have_attribute("aria-busy", "false")


def block(page: Page, day: str, title: str) -> Locator:
    return calendar(page).locator(f'.dcol[data-day="{day}"] .ev:not(.ghost)', has_text=title)


def hour(page: Page) -> float:
    return calendar(page).locator(".dcol").first.evaluate("el => el.getBoundingClientRect().height / 24")


def google(daemon: LiveDaemon, event_id: str) -> dict[str, Any]:
    return daemon.in_loop(lambda: daemon.world.google["personal"].event(FAMILY, event_id))


def drag(page: Page, target: Locator, dx: float, dy: float, *, at: float = 0.3) -> None:
    target.scroll_into_view_if_needed()
    box = target.bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] * at
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx / 2, y + dy / 2, steps=4)
    page.mouse.move(x + dx, y + dy, steps=4)
    page.mouse.up()


def open_week(open_page: OpenPage, daemon: LiveDaemon) -> Page:
    page = open_page(daemon=daemon)
    ready(page)
    return page


def test_dragging_a_block_down_moves_it_an_hour_later(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_week(open_page, fresh_live)
    dentist = block(page, "2026-10-27", "社区活动")
    drag(page, dentist, 0, hour(page))
    expect(block(page, "2026-10-27", "社区活动")).to_have_attribute("aria-label", re.compile("15:00–16:00"))
    assert google(fresh_live, "dentist")["start"]["dateTime"] == "2026-10-27T15:00:00+08:00"
    assert google(fresh_live, "dentist")["end"]["dateTime"] == "2026-10-27T16:00:00+08:00"
    page.get_by_role("tab", name="活动").click()
    expect(page.locator(".ar", has_text="在 Google 挪动了「社区活动」")).to_contain_text("时间改为")


def test_dragging_sideways_moves_it_to_another_day(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_week(open_page, fresh_live)
    dentist = block(page, "2026-10-27", "社区活动")
    width = calendar(page).locator('.dcol[data-day="2026-10-28"]').bounding_box()["width"]  # type: ignore[index]
    drag(page, dentist, width, 0)
    expect(block(page, "2026-10-28", "社区活动")).to_be_visible()
    assert google(fresh_live, "dentist")["start"]["dateTime"] == "2026-10-28T14:00:00+08:00"


def test_the_bottom_edge_changes_the_end(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_week(open_page, fresh_live)
    edge = block(page, "2026-10-27", "社区活动").locator(".rs")
    edge.scroll_into_view_if_needed()
    box = edge.bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x, y + hour(page) / 2, steps=6)
    page.mouse.up()
    expect(block(page, "2026-10-27", "社区活动")).to_have_attribute("aria-label", re.compile("14:00–15:30"))
    stored = google(fresh_live, "dentist")
    assert (
        stored["start"]["dateTime"] == "2026-10-27T14:00:00+08:00"
        and stored["end"]["dateTime"] == "2026-10-27T15:30:00+08:00"
    )


def test_a_repeating_event_asks_and_cancel_puts_it_back(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_week(open_page, fresh_live)
    run = block(page, "2026-10-26", "晨跑")
    drag(page, run, 0, hour(page))
    sheet = page.locator('[data-role="move-scope"]')
    expect(sheet).to_contain_text("这是重复日程")
    sheet.get_by_role("button", name="取消").click()
    expect(sheet).to_have_count(0)
    expect(calendar(page).locator(".ev.ghost")).to_have_count(0)
    assert fresh_live.in_loop(lambda: fresh_live.world.google["personal"].writes) == []
    drag(page, block(page, "2026-10-26", "晨跑"), 0, hour(page))
    sheet.get_by_role("button", name="只改这一次").click()
    expect(block(page, "2026-10-26", "晨跑")).to_have_attribute("aria-label", re.compile("08:00–08:45"))
    fake = fresh_live.world.google["personal"]
    moved = [
        e
        for e in fresh_live.in_loop(lambda: fake.events(FAMILY))
        if e.get("recurringEventId") == "run" and e["start"]["dateTime"].startswith("2026-10-26")
    ]
    assert [e["start"]["dateTime"] for e in moved] == ["2026-10-26T08:00:00+08:00"]
    assert google(fresh_live, "run")["start"]["dateTime"].endswith("07:00:00+08:00")  # the series stays


def test_a_click_still_opens_the_detail(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_week(open_page, fresh_live)
    block(page, "2026-10-27", "社区活动").click()
    expect(page).to_have_url(re.compile(r"#/event/"))
    assert fresh_live.in_loop(lambda: fresh_live.world.google["personal"].writes) == []


def test_an_invitation_does_not_move(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fake = fresh_live.world.google["personal"]
    invite = google_event(
        "invite", "同学聚会", "2026-10-29T18:00:00+08:00", "2026-10-29T20:00:00+08:00",
        tz="Asia/Shanghai", organizer={"email": "classmate@example.com"},
    )  # fmt: skip
    fresh_live.in_loop(lambda: fake.put_event(FAMILY, invite))
    fresh_live.trigger("family-home")
    page = open_week(open_page, fresh_live)
    party = block(page, "2026-10-29", "同学聚会")
    expect(party).not_to_have_class(re.compile(r"\bdrag\b"))
    drag(page, party, 0, hour(page))
    expect(page).to_have_url(re.compile(r"#/event/"))  # a press without a drag is a click
    assert fresh_live.in_loop(lambda: fake.writes) == []


TOUCH = """async (el, [dy, hold]) => {
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + 6;
  const opts = (cx, cy) => ({ bubbles: true, cancelable: true, pointerId: 7, pointerType: "touch", isPrimary: true, clientX: cx, clientY: cy, button: 0, buttons: 1 });
  el.dispatchEvent(new PointerEvent("pointerdown", opts(x, y)));
  await new Promise((done) => setTimeout(done, hold));
  for (let i = 1; i <= 4; i++) window.dispatchEvent(new PointerEvent("pointermove", opts(x, y + (dy * i) / 4)));
  window.dispatchEvent(new PointerEvent("pointerup", opts(x, y + dy)));
}"""


def test_touch_needs_a_hold_and_a_swipe_does_not_drag(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_page(daemon=fresh_live, has_touch=True)
    ready(page)
    dentist = block(page, "2026-10-27", "社区活动")
    dentist.scroll_into_view_if_needed()
    h = hour(page)
    dentist.evaluate(TOUCH, [h, 50])  # a swipe: no hold
    page.wait_for_timeout(300)
    assert fresh_live.in_loop(lambda: fresh_live.world.google["personal"].writes) == []
    dentist.evaluate(TOUCH, [h, 600])  # hold, then drag an hour down
    expect(block(page, "2026-10-27", "社区活动")).to_have_attribute("aria-label", re.compile("15:00–16:00"))


def test_a_chip_moves_to_another_day_in_the_month(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_week(open_page, fresh_live)
    calendar(page).locator(".seg.views").get_by_role("button", name="月", exact=True).click()
    ready(page)
    chip = calendar(page).locator('.wc[data-day="2026-10-29"] .chip', has_text="交物业费")
    target = calendar(page).locator('.wc[data-day="2026-10-30"]')
    chip.scroll_into_view_if_needed()
    a, b = chip.bounding_box(), target.bounding_box()
    assert a is not None and b is not None
    drag(
        page,
        chip,
        b["x"] + b["width"] / 2 - (a["x"] + a["width"] / 2),
        b["y"] + b["height"] / 2 - (a["y"] + a["height"] * 0.3),
    )
    expect(calendar(page).locator('.wc[data-day="2026-10-30"] .chip', has_text="交物业费")).to_be_visible()
    stored = google(fresh_live, "fees")
    assert stored["start"] == {"date": "2026-10-30"} and stored["end"] == {"date": "2026-10-31"}


def test_a_series_on_set_days_moves_to_another_day_only_once(
    open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    page = open_week(open_page, fresh_live)
    weekly = block(page, "2026-10-26", "周会")  # every Monday (BYDAY=MO)
    width = calendar(page).locator('.dcol[data-day="2026-10-27"]').bounding_box()["width"]  # type: ignore[index]
    drag(page, weekly, width, 0)
    sheet = page.locator('[data-role="move-scope"]')
    expect(sheet.locator('[data-role="fixed-days"]')).to_be_visible()
    expect(sheet.get_by_role("button", name="改整个系列")).to_be_disabled()
    sheet.get_by_role("button", name="取消").click()


def test_escape_cancels_and_the_release_opens_nothing(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_week(open_page, fresh_live)
    dentist = block(page, "2026-10-27", "社区活动")
    dentist.scroll_into_view_if_needed()
    box = dentist.bounding_box()
    assert box is not None
    x, y = box["x"] + box["width"] / 2, box["y"] + 10
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x, y + hour(page), steps=5)
    expect(calendar(page).locator(".ev.ghost")).to_have_count(1)
    page.keyboard.press("Escape")
    expect(calendar(page).locator(".ev.ghost")).to_have_count(0)
    page.mouse.up()
    page.wait_for_timeout(200)
    expect(page).not_to_have_url(re.compile(r"#/event/"))
    assert fresh_live.in_loop(lambda: fresh_live.world.google["personal"].writes) == []
