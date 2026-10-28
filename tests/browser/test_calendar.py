"""The calendar tab and the event detail against a live daemon, in Chromium and WebKit: each view
puts the seeded events on their local days (Asia/Shanghai, all-day events included), layers hide a
target calendar's events, delivery states match ``GET /api/events``, the list view names services
and accounts, the event detail matches ``GET /api/event``, the phone has no 周 / 双周 and nothing
overflows at 360 px.

The browser's clock is the daemon's: Monday 2026-10-26, 10:42 local."""

import re
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser.conftest import NARROW, PHONE, OpenPage, refresh_status
from tests.browser.live import PENDING_TITLE, LiveDaemon

pytestmark = pytest.mark.browser

OPEN = re.compile(r"\bopen\b")


def calendar(page: Page) -> Locator:
    return page.locator("#pane-calendar .calendar")


def view_button(page: Page, name: str) -> Locator:
    return calendar(page).locator(".seg.views").get_by_role("button", name=name, exact=True)


def show(page: Page, name: str) -> None:
    view_button(page, name).click()
    expect(view_button(page, name)).to_have_attribute("aria-pressed", "true")
    expect(calendar(page).locator(".cal-frame")).to_have_attribute("aria-busy", "false")


def block(page: Page, title: str) -> Locator:
    return calendar(page).locator(".ev, .chip, .lr").filter(has_text=title)


def events_of(daemon: LiveDaemon, start: str, end: str) -> list[dict[str, Any]]:
    return daemon.get("/api/events", **{"from": start, "to": end})["events"]


def open_calendar(open_page: OpenPage, daemon: LiveDaemon | None = None, **kwargs: Any) -> Page:
    page = open_page(daemon=daemon, **kwargs) if daemon else open_page(**kwargs)
    expect(calendar(page).locator(".cal-frame")).to_have_attribute("aria-busy", "false")
    return page


# ----- the views ---------------------------------------------------------------------------------


def test_the_week_puts_each_event_on_its_local_day(open_page: OpenPage) -> None:
    page = open_calendar(open_page)
    expect(view_button(page, "周")).to_have_attribute("aria-pressed", "true")  # the desktop's default
    expect(calendar(page).locator('[data-role="cal-title"]')).to_have_text("2026年10月")
    expect(calendar(page).locator('[data-role="cal-range"]')).to_have_text("10月26日 – 11月1日")
    monday = calendar(page).locator('.dcol[data-day="2026-10-26"]')
    expect(monday.locator(".ev", has_text="晨跑")).to_be_visible()
    expect(monday.locator(".ev", has_text="周会")).to_be_visible()
    wednesday = calendar(page).locator('.dcol[data-day="2026-10-28"]')
    # The moved run (too short a block to print its time: the label says it).
    expect(wednesday.locator(".ev", has_text="晨跑")).to_have_attribute(
        "aria-label", re.compile("07:30–08:15")
    )
    # All-day events: in the all-day row, on their own local day (Thursday and Saturday).
    all_day = calendar(page).locator(".tg-all > div:not(.gut)")
    expect(all_day.nth(3)).to_contain_text("交物业费")
    expect(all_day.nth(5)).to_contain_text("秋游")
    expect(all_day.nth(2)).not_to_contain_text("交物业费")
    expect(calendar(page).locator(".dh.today")).to_have_attribute("data-day", "2026-10-26")
    expect(calendar(page).locator(".nowl:not(.faint)")).to_have_count(1)


def test_every_view_shows_the_seeded_events(open_page: OpenPage) -> None:
    page = open_calendar(open_page)
    show(page, "日")
    expect(calendar(page).locator(".dcol")).to_have_count(1)
    expect(block(page, "晨跑")).to_be_visible()
    expect(calendar(page).locator('[data-role="cal-range"]')).to_have_text("10月26日 周一")

    show(page, "双日")
    expect(calendar(page).locator(".dcol")).to_have_count(2)
    expect(block(page, "设计评审")).to_be_visible()

    show(page, "双周")
    expect(calendar(page).locator(".wc")).to_have_count(14)
    expect(calendar(page).locator('.wc[data-day="2026-10-29"]')).to_contain_text("交物业费")
    expect(calendar(page).locator('.wc[data-day="2026-11-03"]')).to_contain_text("钢琴课")

    show(page, "月")
    expect(calendar(page).locator('.wc[data-day="2026-10-31"]')).to_contain_text("秋游")
    expect(calendar(page).locator('.wc[data-day="2026-10-01"]')).not_to_have_class(re.compile(r"\bout\b"))
    expect(calendar(page).locator('.wc[data-day="2026-11-01"]')).to_have_class(re.compile(r"\bout\b"))

    show(page, "列表")
    expect(calendar(page).locator(".ls h3").first).to_contain_text("今天 · 10月26日")
    run = calendar(page).locator(".lr", has_text="晨跑").first
    expect(run.locator("small")).to_contain_text("家庭日程 · personal")
    expect(run.locator("small")).to_contain_text("家庭 · family-icloud")
    expect(run.locator('small svg.svc[aria-label="Google 日历"]')).to_have_count(1)
    expect(run.locator('small svg.svc[aria-label="iCloud 日历"]').first).to_be_visible()


def test_a_days_number_opens_that_day(open_page: OpenPage) -> None:
    page = open_calendar(open_page)
    show(page, "月")
    calendar(page).locator('.wc[data-day="2026-10-29"] .dn').click()
    expect(view_button(page, "日")).to_have_attribute("aria-pressed", "true")
    expect(calendar(page).locator('[data-role="cal-range"]')).to_have_text("10月29日 周四")
    expect(calendar(page).locator(".tg-all")).to_contain_text("交物业费")


def test_the_arrows_and_today_move_the_range(open_page: OpenPage) -> None:
    page = open_calendar(open_page)
    calendar(page).get_by_role("button", name="下一段").click()
    expect(calendar(page).locator('[data-role="cal-range"]')).to_have_text("11月2日 – 11月8日")
    expect(calendar(page).locator('[data-role="cal-title"]')).to_have_text("2026年11月")
    calendar(page).get_by_role("button", name="今天").click()
    expect(calendar(page).locator('[data-role="cal-range"]')).to_have_text("10月26日 – 11月1日")


# ----- delivery state and layers ------------------------------------------------------------------


def test_a_pending_event_is_marked_as_the_api_says(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_calendar(open_page)
    states = {
        d["state"]
        for e in events_of(live, "2026-10-26", "2026-10-27")
        if e["title"] == PENDING_TITLE
        for d in e["deliveries"]
    }
    assert "pending" in states
    lunch = block(page, PENDING_TITLE)
    expect(lunch).to_have_class(re.compile(r"\bst-pending\b"))
    expect(lunch.locator(".flag.pending")).to_have_count(1)
    expect(block(page, "晨跑").first).not_to_have_class(re.compile(r"\bst-"))


def test_layers_hide_their_events(open_page: OpenPage) -> None:
    page = open_calendar(open_page)
    layers = calendar(page).locator(".layers")
    mine = layers.locator(".layer", has_text="我的日历")
    home = layers.locator(".layer", has_text="家庭")
    expect(mine.locator("svg.svc")).to_have_attribute("aria-label", "iCloud 日历")
    expect(mine).to_contain_text("我的日历 · my-icloud")
    mine.click()
    expect(mine).to_have_attribute("aria-pressed", "false")
    expect(block(page, "周会")).to_have_count(0)  # only 我的日历 has it
    expect(block(page, "晨跑").first).to_be_visible()  # 家庭 has it too
    expect(calendar(page).locator('[data-role="legend"]')).not_to_contain_text("工作")
    home.click()
    expect(block(page, "晨跑")).to_have_count(0)
    mine.click()
    expect(block(page, "周会").first).to_be_visible()


def test_a_paused_line_hatches_its_events(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.pause("work-mine")
    page = open_calendar(open_page, fresh_live)
    refresh_status(page)
    meeting = block(page, "周会")
    expect(meeting).to_have_class(re.compile(r"\bst-held\b"))
    states = {
        d["state"]
        for e in events_of(fresh_live, "2026-10-26", "2026-10-27")
        if e["title"] == "周会"
        for d in e["deliveries"]
    }
    assert states == {"paused"}


# ----- the event detail ------------------------------------------------------------------------------


def test_the_event_detail_matches_the_api(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_calendar(open_page)
    run = next(e for e in events_of(live, "2026-10-26", "2026-10-27") if e["title"] == "晨跑")
    detail = live.get("/api/event", id=run["id"])
    calendar(page).locator('.dcol[data-day="2026-10-26"] .ev', has_text="晨跑").click()
    expect(page).to_have_url(re.compile(rf"#/event/{re.escape(run['id'])}$"))
    insp = page.locator("aside.insp")
    expect(insp).to_have_class(OPEN)
    expect(insp.locator("#insp-title")).to_have_text("晨跑")
    expect(insp.locator(".ih p")).to_contain_text("10月26日 07:00–07:45")
    source = insp.locator('[data-role="source"]')
    expect(source).to_contain_text("家庭日程")
    expect(source).to_contain_text("personal · Google 日历")
    expect(source.locator(".tag")).to_have_text("重复")
    rows = insp.locator('[data-key="deliveries"] .row')
    expect(rows).to_have_count(len(detail["deliveries"]))
    expect(rows.first).to_contain_text("已送达 iCloud")
    expect(rows.first).to_contain_text("family-icloud · iCloud 日历")

    table = insp.locator('[data-role="compare"]')
    expect(table.locator('tr[data-field="title"] td').nth(1)).to_have_text(detail["original"]["title"])
    expect(table.locator('[data-differs="true"]')).to_have_count(0)
    expect(table.locator("thead")).to_contain_text("「家庭」里的副本")

    insp.locator('[data-key="compare"] .seg').get_by_role("button", name="我的日历").click()
    expect(table.locator('tr[data-field="title"] td').nth(2)).to_have_text("忙碌")
    expect(insp.locator('[data-role="busy-note"]')).to_contain_text("「我的日历」里这条只显示为“忙碌”")


def test_a_pending_event_says_when_it_will_arrive(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_calendar(open_page)
    block(page, PENDING_TITLE).click()
    insp = page.locator("aside.insp")
    expect(insp.locator('[data-key="deliveries"] .row[data-state="pending"]').first).to_contain_text(
        "等待送达 · 下次检查"
    )
    expect(insp.locator(".ifoot")).to_contain_text("在 Google 日历里修改")


def test_an_unknown_event_says_it_is_gone(open_page: OpenPage) -> None:
    from tests.browser.conftest import watch_of

    page = open_page(path="/#/event/bm90LWFuLWV2ZW50")
    watch_of(page).allow(r"status of 404")
    expect(page.locator("aside.insp .note")).to_contain_text("这条日程已经不在了")


# ----- the phone ---------------------------------------------------------------------------------


def test_the_phone_has_no_week_views(open_page: OpenPage) -> None:
    page = open_calendar(open_page, viewport=PHONE, has_touch=True)
    views = calendar(page).locator(".seg.views button")
    expect(views).to_have_text(["日", "双日", "月", "列表"])
    expect(view_button(page, "日")).to_have_attribute("aria-pressed", "true")


@pytest.mark.parametrize("name", ["日", "月", "列表"])
def test_nothing_overflows_at_360(open_page: OpenPage, name: str) -> None:
    page = open_calendar(open_page, viewport=NARROW, has_touch=True)
    show(page, name)
    assert page.evaluate("() => document.documentElement.scrollWidth") <= NARROW["width"]
    frame = calendar(page).locator(".cal-frame")
    assert frame.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
