"""The activity tab and the blocked-delete approval against a live daemon, in Chromium and WebKit:
the timeline (changes merged across targets, sync cycles, config changes, grouped by local day,
the four filters, the wording of updates, iPhone edits changed back and approvals), and the
blocked flow (attention card → the list → 确认删除 → the copies gone and the wire healthy; a list
that changed meanwhile deletes nothing and is shown again)."""

import re
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.browser.conftest import OpenPage, refresh_status, until
from tests.browser.live import LiveDaemon

pytestmark = pytest.mark.browser

OPEN = re.compile(r"\bopen\b")


def activity(page: Page) -> Locator:
    return page.locator("#pane-activity .activity")


def open_activity(open_page: OpenPage, daemon: LiveDaemon | None = None) -> Page:
    page = open_page(daemon=daemon, path="/#/activity") if daemon else open_page(path="/#/activity")
    expect(page.locator("#tab-activity")).to_have_attribute("aria-selected", "true")
    expect(activity(page).locator(".ad").first).to_be_visible()
    return page


def rows(page: Page) -> Locator:
    return activity(page).locator(".ar")


def pick(page: Page, name: str) -> None:
    activity(page).locator(".af").get_by_role("button", name=name, exact=True).click()


# ----- the timeline -------------------------------------------------------------------------------


def test_the_timeline_groups_today_and_folds_a_long_sync(open_page: OpenPage) -> None:
    """The seeded first sync wrote 17 changes: 8 rows, then one that counts the rest."""
    page = open_activity(open_page)
    expect(activity(page).locator(".ad h3").first).to_have_text("今天 · 10月26日")
    added = rows(page).filter(has_text=re.compile("^.*新增「"))
    expect(added.first.locator(".from")).to_contain_text("· alex@example.com")
    expect(rows(page).filter(has_text=re.compile(r"这次同步还有 \d+ 处改动"))).to_have_count(1)
    expect(rows(page).filter(has_text="Busy")).to_have_count(0)
    expect(rows(page).filter(has_text="同步完成").first).to_contain_text("处改动")
    expect(rows(page).filter(has_text="同步完成").first).to_contain_text("4 条线路")


def test_the_filters(open_page: OpenPage) -> None:
    page = open_activity(open_page)
    pick(page, "同步")
    expect(rows(page).first).to_be_visible()
    for kind in rows(page).evaluate_all("(els) => els.map((e) => e.dataset.kind)"):
        assert kind == "sync"
    pick(page, "改动")
    kinds = set(rows(page).evaluate_all("(els) => els.map((e) => e.dataset.kind)"))
    assert kinds <= {"change", "config"} and "change" in kinds
    pick(page, "异常")
    expect(activity(page).locator('[data-role="empty"]')).to_be_visible()
    pick(page, "全部")
    expect(rows(page).filter(has_text="同步完成").first).to_be_visible()


def test_a_config_change_has_its_own_row(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.op("rule.set", {"name": "life-shared", "key": "fields.content", "value": "busy"})
    page = open_activity(open_page, fresh_live)
    row = rows(page).filter(has_text="线路「life-shared」：改了显示内容")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("来自 命令行")
    expect(row).to_have_attribute("data-kind", "config")


FAKE_CHANGES = [
    {
        "id": 909,
        "run_id": 1,
        "rule": "course-shared",
        "object_id": "h1",
        "action": "blocked",
        "at": "2026-10-26T02:20:00+00:00",
        "title": "钢琴课",
        "detail": None,
    },
    {
        "id": 908,
        "run_id": 1,
        "rule": "course-shared",
        "object_id": "h2",
        "action": "blocked",
        "at": "2026-10-26T02:20:00+00:00",
        "title": "团建",
        "detail": None,
    },
    {
        "id": 910,
        "run_id": 1,
        "rule": "life-shared",
        "object_id": "p1",
        "action": "protected",
        "at": "2026-10-26T02:20:00+00:00",
        "title": "周五聚餐",
        "detail": None,
    },
    {
        "id": 906,
        "run_id": 1,
        "rule": "life-shared",
        "object_id": "m1",
        "action": "created",
        "at": "2026-10-26T02:30:00+00:00",
        "title": "团建",
        "detail": None,
    },
    {
        "id": 905,
        "run_id": 1,
        "rule": "work-me",
        "object_id": "m2",
        "action": "created",
        "at": "2026-10-26T02:30:00+00:00",
        "title": "团建",
        "detail": None,
    },
    {
        "id": 904,
        "run_id": 1,
        "rule": "life-me",
        "object_id": "b1",
        "action": "created",
        "at": "2026-10-26T02:30:00+00:00",
        "title": "Busy",
        "detail": None,
    },
    {
        "id": 907,
        "run_id": 1,
        "rule": "life-me",
        "object_id": "b2",
        "action": "created",
        "at": "2026-10-26T02:30:00+00:00",
        "title": "Busy",
        "detail": None,
    },
    {
        "id": 903,
        "run_id": 1,
        "rule": "work-me",
        "object_id": "a",
        "action": "updated",
        "at": "2026-10-26T01:58:00+00:00",
        "title": "周会",
        "detail": {"fields": {"start": ["2026-10-26T10:00:00+08:00", "2026-10-26T09:30:00+08:00"]}},
    },
    {
        "id": 902,
        "run_id": 1,
        "rule": "course-shared",
        "object_id": "b",
        "action": "reverted",
        "at": "2026-10-26T01:12:00+00:00",
        "title": "取快递",
        "detail": {"fields": {"start": ["2026-10-26T16:30:00+08:00", "2026-10-26T16:00:00+08:00"]}},
    },
    {
        "id": 901,
        "run_id": 1,
        "rule": "course-shared",
        "object_id": "fp",
        "action": "approved",
        "at": "2026-10-25T13:04:00+00:00",
        "title": "3 deletes approved (ui)",
        "detail": None,
    },
]


def test_the_wording_of_updates_reverts_and_approvals(open_page: OpenPage) -> None:
    page = open_page()

    def fake(route: Route) -> None:
        route.fulfill(json={"changes": FAKE_CHANGES})

    page.route(re.compile(r"/api/changes\b"), fake)
    page.goto("/#/activity")
    team = rows(page).filter(has_text="新增「团建」")
    expect(team).to_have_count(1)  # one row for both lines
    expect(team.locator(".from")).to_contain_text("共享日历 · shared@example.com")
    expect(team.locator(".from")).to_contain_text("我的日历 · me@example.com")
    expect(rows(page).filter(has_text="在「我的日历」新增 2 条只显示“忙碌”的日程")).to_have_count(1)
    expect(rows(page).filter(has_text="Busy")).to_have_count(0)
    held = rows(page).filter(has_text="2 个副本的删除被拦下了")
    expect(held).to_have_count(1)
    expect(held).to_have_attribute("href", "#/blocked/course-shared")
    expect(rows(page).filter(has_text="钢琴课")).to_have_count(0)  # not shown as an edit
    expect(rows(page).filter(has_text="「周五聚餐」暂时不动")).to_contain_text("没能完整读到")
    expect(rows(page).filter(has_text="更新「周会」")).to_contain_text("时间改为 09:30 · 原来是 10:00")
    reverted = rows(page).filter(has_text="「取快递」的副本被改动，已改回")
    expect(reverted).to_contain_text("副本的时间被改成了 16:30；Google 里是 16:00")
    assert "有人" not in activity(page).inner_text()
    expect(activity(page).locator(".ad h3").nth(1)).to_have_text("昨天 · 10月25日")
    approved = rows(page).filter(has_text="确认删除了 3 个副本")
    expect(approved).to_contain_text("来自 界面")
    pick(page, "异常")
    expect(rows(page).filter(has_text="已改回")).to_have_count(1)
    expect(rows(page).filter(has_text="确认删除了 3 个副本")).to_have_count(1)


# ----- blocked deletes ----------------------------------------------------------------------------


def blocked_panel(page: Page) -> Locator:
    return page.locator("aside.insp")


def open_blocked(open_page: OpenPage, daemon: LiveDaemon) -> Page:
    page = open_page(daemon=daemon)
    card = page.locator('.attn[data-kind="blocked"]', has_text="课程")
    expect(card).to_be_visible()
    card.get_by_role("link").first.click()
    expect(page).to_have_url(re.compile(r"#/blocked/course-shared$"))
    expect(blocked_panel(page)).to_have_class(OPEN)
    expect(blocked_panel(page).locator('[data-role="items"] li')).to_have_count(3)
    return page


def school_copies(daemon: LiveDaemon) -> int:
    rule: dict[str, Any] = next(r for r in daemon.get("/api/status")["rules"] if r["name"] == "course-shared")
    return int(rule.get("copies") or 0)


def test_approving_deletes_exactly_the_list_shown(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.stage_blocked()
    page = open_blocked(open_page, fresh_live)
    panel = blocked_panel(page)
    expect(panel.locator('[data-role="summary"]')).to_contain_text("3 个副本")
    expect(panel.locator('[data-role="reason"]')).to_contain_text("超过")
    for title in ("钢琴课", "项目例会", "团建"):
        expect(panel.locator('[data-role="items"]')).to_contain_text(title)
    panel.get_by_role("button", name="确认删除").click()
    expect(panel.locator('[data-role="outcome"]')).to_contain_text("已删除 3 个副本")
    until(lambda: school_copies(fresh_live) == 0)
    refresh_status(page)
    wire = page.locator('path.t[data-rule="course-shared"]')
    expect(wire).to_have_class(re.compile(r"\bok\b"))
    expect(panel.locator('[data-role="nothing"]')).to_be_visible()


def test_a_list_that_changed_deletes_nothing_and_is_shown_again(
    open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    """The page shows an older list (another fingerprint) than the daemon holds now: approving it
    deletes nothing, says so and shows the list again."""
    fresh_live.stage_blocked()
    page = open_page(daemon=fresh_live)
    stale = {"on": True}

    def older(route: Route) -> None:
        body = route.fetch().json()
        if stale["on"] and body["blocked"]:
            entry = body["blocked"][0]
            entry["fingerprint"] = "0000000000000000"
            entry["items"] = [*entry["items"], {"object_id": "gone", "title": "早就删掉的日程"}]
            entry["count"] += 1
        route.fulfill(json=body)

    page.route(re.compile(r"/api/blocked\b"), older)
    page.goto("/#/blocked/course-shared")
    panel = blocked_panel(page)
    expect(panel.locator('[data-role="items"] li')).to_have_count(4)
    stale["on"] = False  # from now on the page reads the list as it is
    panel.get_by_role("button", name="确认删除").click()
    expect(panel.locator('[data-role="outcome"]')).to_contain_text("清单变了")
    expect(panel.locator('[data-role="items"] li')).to_have_count(3)
    assert school_copies(fresh_live) == 3  # nothing was deleted


def test_not_now_changes_nothing(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.stage_blocked()
    page = open_blocked(open_page, fresh_live)
    blocked_panel(page).get_by_role("button", name="先不删").click()
    expect(blocked_panel(page)).not_to_have_class(OPEN)
    assert school_copies(fresh_live) == 3
