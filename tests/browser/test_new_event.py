"""Creating an event from the calendar, in Chromium and WebKit, against a daemon of each test's own.

The browser's clock is the daemon's: Monday 2026-10-26, 10:42 local."""

import re
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.browser.conftest import OpenPage
from tests.browser.live import FAMILY, SCHOOL, LiveDaemon

pytestmark = pytest.mark.browser


def calendar(page: Page) -> Any:
    return page.locator("#pane-calendar .calendar")


def created(daemon: LiveDaemon, cid: str, title: str) -> dict[str, Any]:
    fake = daemon.world.google["personal"]
    return next(e for e in daemon.in_loop(lambda: fake.events(cid)) if e.get("summary") == title)


def test_the_toolbar_creates_an_event(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_page(daemon=fresh_live)
    expect(calendar(page).locator(".cal-frame")).to_have_attribute("aria-busy", "false")
    calendar(page).locator('[data-role="new-event"]').click()
    expect(page).to_have_url(re.compile(r"#/event-new$"))
    form = page.locator("aside.insp")
    expect(form.locator("#insp-title")).to_have_text("新建日程")
    radios = form.locator('[data-role="calendars"] [role="radio"]')
    expect(radios).to_have_count(3)  # 家庭日程, 孩子学校, 工作: synced and writable (not 中国节假日)
    expect(radios.first).to_have_attribute("aria-checked", "true")
    expect(form.locator("#ev-date")).to_have_value("2026-10-26")
    expect(form.locator("#ev-start")).to_have_value("11:00")  # the next half hour after 10:42
    expect(form.locator("#ev-end")).to_have_value("12:00")
    create = form.locator('[data-role="save"]')
    expect(create).to_have_attribute("aria-disabled", "true")  # no title yet
    form.locator("#ev-title").fill("买菜")
    create.click()
    expect(page).to_have_url(re.compile(r"#/event/[^/]+$"))
    expect(form.locator("#insp-title")).to_have_text("买菜")
    expect(form.locator('[data-key="deliveries"] .row').first).to_contain_text("已送达 iCloud")
    made = created(fresh_live, FAMILY, "买菜")
    assert made["start"]["dateTime"] == "2026-10-26T11:00:00+08:00"
    assert made["reminders"] == {"useDefault": True, "overrides": []}


def test_a_click_on_an_empty_hour_and_the_remembered_calendar(
    open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    page = open_page(daemon=fresh_live)
    cal = calendar(page)
    cal.locator(".seg.views").get_by_role("button", name="日", exact=True).click()
    expect(cal.locator(".cal-frame")).to_have_attribute("aria-busy", "false")
    column = cal.locator('.dcol[data-day="2026-10-26"]')
    column.scroll_into_view_if_needed()
    hour = column.evaluate("el => el.getBoundingClientRect().height / 24")
    column.click(position={"x": 10, "y": hour * 15 + 2})  # 15:00, an empty slot
    expect(page).to_have_url(re.compile(r"#/event-new\?start=2026-10-26T15%3A00$"))
    form = page.locator("aside.insp")
    expect(form.locator("#ev-start")).to_have_value("15:00")
    form.locator('[data-role="calendars"]').get_by_role("radio", name=re.compile("孩子学校")).click()
    form.locator("#ev-title").fill("接孩子")
    form.locator('[data-key="alarms"] .seg').get_by_role("button", name="不提醒").click()
    form.locator('[data-role="save"]').click()
    expect(form.locator("#insp-title")).to_have_text("接孩子")
    assert created(fresh_live, SCHOOL, "接孩子")["reminders"] == {"useDefault": False, "overrides": []}
    page.evaluate("location.hash = '#/event-new'")
    expect(form.locator('[data-role="calendars"] [aria-checked="true"]')).to_contain_text("孩子学校")
