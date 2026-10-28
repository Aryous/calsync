"""Changing and deleting originals from the event detail, in Chromium and WebKit, against a daemon
of each test's own (the writes reach its fake Google; the copies follow in the same request).

The browser's clock is the daemon's: Monday 2026-10-26, 10:42 local."""

import re
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser.conftest import PHONE, OpenPage, watch_of
from tests.browser.live import FAMILY, LiveDaemon
from tests.factories import google_event

pytestmark = pytest.mark.browser


def event_of(daemon: LiveDaemon, title: str, day: str) -> dict[str, Any]:
    events = daemon.get("/api/events", **{"from": day, "to": day[:8] + f"{int(day[8:]) + 1:02d}"})["events"]
    return next(e for e in events if e["title"] == title)


def google(daemon: LiveDaemon, event_id: str) -> dict[str, Any]:
    return daemon.in_loop(lambda: daemon.world.google["personal"].event(FAMILY, event_id))


def insp(page: Page) -> Locator:
    return page.locator("aside.insp")


def open_detail(
    open_page: OpenPage, daemon: LiveDaemon, title: str, day: str, **kwargs: Any
) -> tuple[Page, dict[str, Any]]:
    ev = event_of(daemon, title, day)
    page = open_page(daemon=daemon, path=f"/#/event/{ev['id']}", **kwargs)
    expect(insp(page).locator("#insp-title")).to_have_text(title)
    return page, ev


def test_changing_a_single_event(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page, _ = open_detail(open_page, fresh_live, "社区活动", "2026-10-27")
    insp(page).locator('[data-role="edit-event"]').click()
    expect(page).to_have_url(re.compile(r"#/event-edit/"))
    form = insp(page)
    expect(form.locator("#insp-title")).to_have_text("修改日程")
    expect(form.locator("#ev-title")).to_have_value("社区活动")
    expect(form.locator("#ev-date")).to_have_value("2026-10-27")
    expect(form.locator("#ev-start")).to_have_value("14:00")
    save = form.locator('[data-role="save"]')
    expect(save).to_have_attribute("aria-disabled", "true")  # nothing changed yet
    form.locator("#ev-title").fill("社区活动（改期）")
    form.locator("#ev-start").fill("15:00")
    form.locator("#ev-end").fill("16:00")
    save.click()
    expect(page).to_have_url(re.compile(r"#/event/[^/]+$"))
    expect(insp(page).locator("#insp-title")).to_have_text("社区活动（改期）")
    expect(insp(page).locator(".ih p")).to_contain_text("15:00–16:00")
    expect(insp(page).locator('[data-key="deliveries"] .row').first).to_contain_text("已送达 iCloud")
    stored = google(fresh_live, "dentist")
    assert stored["summary"] == "社区活动（改期）"
    assert stored["start"] == {"dateTime": "2026-10-27T15:00:00+08:00", "timeZone": "Asia/Shanghai"}
    [write] = fresh_live.get("/api/writes")["writes"]
    assert write["origin"] == "ui" and write["op"] == "edit"


def test_a_repeating_event_asks_which_times(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page, _ = open_detail(open_page, fresh_live, "晨跑", "2026-10-26")
    insp(page).locator('[data-role="edit-event"]').click()
    form = insp(page)
    scope = form.locator('[data-role="scope"]')
    expect(scope.get_by_role("radio")).to_have_text([re.compile("只改这一次"), re.compile("改整个系列")])
    form.locator("#ev-title").fill("晨跑（公园）")
    save = form.locator('[data-role="save"]')
    expect(save).to_have_attribute("aria-disabled", "true")  # which times, first
    scope.get_by_role("radio", name=re.compile("只改这一次")).click()
    expect(save).to_have_attribute("aria-disabled", "false")
    save.click()
    expect(insp(page).locator("#insp-title")).to_have_text("晨跑（公园）")
    fake = fresh_live.world.google["personal"]
    events = fresh_live.in_loop(lambda: fake.events(FAMILY))
    assert [
        e["summary"]
        for e in events
        if e.get("recurringEventId") == "run" and e["start"]["dateTime"].startswith("2026-10-26")
    ] == ["晨跑（公园）"]
    assert google(fresh_live, "run")["summary"] == "晨跑"


def test_a_change_made_elsewhere_meanwhile_is_not_overwritten(
    open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    page, _ = open_detail(open_page, fresh_live, "家庭聚餐", "2026-10-30")
    insp(page).locator('[data-role="edit-event"]').click()
    form = insp(page)
    expect(form.locator("#ev-title")).to_have_value("家庭聚餐")
    fake = fresh_live.world.google["personal"]
    fresh_live.in_loop(lambda: fake.touch(FAMILY, "dinner", location="奶奶家"))
    watch_of(page).allow(r"status of 409")
    form.locator("#ev-title").fill("周五聚餐")
    form.locator('[data-role="save"]').click()
    expect(form.locator('[data-role="outcome"]')).to_contain_text("刚在 Google 里被改过")
    assert google(fresh_live, "dinner")["summary"] == "家庭聚餐"
    form.locator('[data-role="reload"]').click()  # read the event again: the form starts over
    expect(form.locator("#ev-location")).to_have_value("奶奶家")


def test_an_invitation_cannot_be_changed(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fake = fresh_live.world.google["personal"]
    invite = google_event(
        "invite", "同学聚会", "2026-10-29T18:00:00+08:00", "2026-10-29T20:00:00+08:00",
        tz="Asia/Shanghai", organizer={"email": "classmate@example.com"},
    )  # fmt: skip
    fresh_live.in_loop(lambda: fake.put_event(FAMILY, invite))
    fresh_live.trigger("family-home")
    page, _ = open_detail(open_page, fresh_live, "同学聚会", "2026-10-29")
    expect(insp(page).locator('[data-role="locked"]')).to_have_text("这是别人邀请你的日程，只有组织者能改。")
    expect(insp(page).locator('[data-role="edit-event"]')).to_have_count(0)


def test_deleting_an_event_and_its_copies(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page, _ = open_detail(open_page, fresh_live, "家庭视频通话", "2026-10-26")
    insp(page).locator('[data-role="delete-event"]').click()
    box = insp(page).locator('[data-role="delete"]')
    expect(box.locator("h3")).to_have_text("在 Google 里删除「家庭视频通话」？")
    box.locator('[data-role="confirm-delete"]').click()
    expect(insp(page)).not_to_have_class(re.compile(r"\bopen\b"))
    assert google(fresh_live, "call")["status"] == "cancelled"
    assert not [
        e
        for e in fresh_live.get("/api/events", **{"from": "2026-10-26", "to": "2026-10-27"})["events"]
        if e["title"] == "家庭视频通话"
    ]


def test_without_write_access_the_command_is_shown(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fake = fresh_live.world.google["personal"]
    fresh_live.in_loop(lambda: setattr(fake, "write_scope", False))
    page, _ = open_detail(open_page, fresh_live, "社区活动", "2026-10-27")
    insp(page).locator('[data-role="edit-event"]').click()
    form = insp(page)
    form.locator("#ev-title").fill("x")
    form.locator('[data-role="save"]').click()
    expect(form.locator('[data-role="needs-human"] code')).to_have_text("calsync auth personal --write")
    assert google(fresh_live, "dentist")["summary"] == "社区活动"


def test_the_form_fits_the_phone(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page, _ = open_detail(open_page, fresh_live, "晨跑", "2026-10-26", viewport=PHONE, has_touch=True)
    insp(page).locator('[data-role="edit-event"]').click()
    form = insp(page)
    expect(form.locator("#ev-title")).to_be_visible()
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= PHONE["width"]
    for target in form.locator('[role="radio"], button.pick, .seg button, input.field').all():
        if target.is_visible():
            box = target.bounding_box()
            assert box is not None and box["height"] >= 44, target.evaluate("e => e.outerHTML")
