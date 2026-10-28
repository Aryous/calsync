"""The map as a configuration surface, against a live daemon, in Chromium and WebKit: the line
inspector (every setting written to config.toml through ``rule.set`` and read back from
``GET /api/config`` and the file itself, surviving a reload; a refused value shown beside its
field and reverted; a conflict reloaded with a notice; pause, resume and 立即同步这一条; deleting
with or without the copies), the calendar inspector (its lines, the naming rule, the way into a
new line) and the new line (picker, disabled pairs, 预演, a new iCloud calendar, create and
sync, the wire that appears).

Every test that writes the config or runs a cycle has a daemon of its own (``fresh_live``)."""

import re
import tomllib
from collections.abc import Callable
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.browser.conftest import NARROW, PHONE, OpenPage, refresh_status, until, watch_of
from tests.browser.live import FAMILY, HOLIDAYS, SCHOOL, LiveDaemon

pytestmark = pytest.mark.browser

OPEN = re.compile(r"\bopen\b")
PRESSED = "true"
REFUSED = r"status of 422"  # the browser's console line for a refused op (the test provokes it)


# ----- helpers -----------------------------------------------------------------------------------


def inspector(page: Page) -> Locator:
    return page.locator("aside.insp")


def open_rule(open_page: OpenPage, daemon: LiveDaemon, rule: str, **kwargs: Any) -> Page:
    page = open_page(daemon=daemon, path=f"/#/rule/{rule}", **kwargs)
    expect(inspector(page)).to_have_class(OPEN)
    expect(inspector(page).locator(".route-chip")).to_be_visible()
    expect(section(page, "fields.content").get_by_role("button").first).to_be_enabled()
    return page


def section(page: Page, key: str) -> Locator:
    return inspector(page).locator(f'.fs[data-key="{key}"]')


def seg(page: Page, key: str, label: str) -> Locator:
    return section(page, key).get_by_role("button", name=label, exact=True)


def config_rule(daemon: LiveDaemon, rule: str) -> dict[str, Any] | None:
    """The running config's line (``GET /api/config``), None when it is gone."""
    return next((r for r in daemon.get("/api/config")["rules"] if r["name"] == rule), None)


def file_rule(daemon: LiveDaemon, rule: str) -> dict[str, Any] | None:
    """The line as ``config.toml`` on disk has it, None when it is gone."""
    doc = tomllib.loads(daemon.config_path.read_text(encoding="utf-8"))
    return next((r for r in doc.get("rules", []) if r["name"] == rule), None)


def settles(
    daemon: LiveDaemon, rule: str, check: Callable[[dict[str, Any] | None, dict[str, Any] | None], bool]
) -> None:
    """Wait until both the running config and the file satisfy ``check(running, file)``."""
    until(lambda: check(config_rule(daemon, rule), file_rule(daemon, rule)))


def saved(page: Page) -> None:
    expect(inspector(page).locator(".saved")).to_have_class(re.compile(r"\bon\b"))
    expect(inspector(page).locator(".saved")).to_have_text("已保存到 config.toml")


def reload(page: Page) -> None:
    page.reload()
    expect(inspector(page)).to_have_class(OPEN)
    expect(section(page, "fields.content").get_by_role("button").first).to_be_enabled()


def wire_class(page: Page, rule: str) -> str:
    trace = page.locator(f'.canvas svg.wires path.t[data-rule="{rule}"]')
    expect(trace).to_have_count(1)
    return trace.get_attribute("class") or ""


# ----- the line inspector: reading ---------------------------------------------------------------


def test_the_line_inspector_shows_the_line_and_its_settings(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_rule(open_page, live, "family-mine")
    insp = inspector(page)
    expect(insp.locator("h2")).to_have_text("线路")
    chip = insp.locator(".route-chip")
    expect(chip.locator(".cm")).to_have_text(["家庭日程 · personal", "我的日历 · my-icloud"])
    expect(chip.get_by_role("img", name="Google 日历")).to_have_count(1)
    expect(chip.get_by_role("img", name="iCloud 日历")).to_have_count(1)
    expect(insp.locator(".statl")).to_have_text(re.compile(r"^正常 · 刚刚同步 · 已同步 \d+ 个系列$"))
    for key, label in [
        ("window", "全部"),
        ("alarms", "跟 Google 一样"),
        ("fields.content", "只显示忙碌"),
        ("fields.attendees", "写进备注"),
        ("fields.conference", "复制"),
    ]:
        expect(seg(page, key, label)).to_have_attribute("aria-pressed", PRESSED)
    expect(section(page, "on_target_edit").locator(".locked")).to_have_text("改回 Google 里的版本")
    expect(section(page, "delete_guard").get_by_label("最多删除的个数")).to_have_value("10")
    expect(section(page, "delete_guard").get_by_label("最多删除的百分比")).to_have_value("50")
    expect(insp.get_by_role("button", name="暂停")).to_be_visible()
    expect(insp.get_by_role("button", name="立即同步这一条")).to_be_enabled()
    expect(insp.locator(".ifoot").get_by_role("button", name="删除这条线路")).to_be_visible()
    # The open line's wire is lit on the map, the others dimmed.
    assert " hot" in wire_class(page, "family-mine")
    assert " dim" in wire_class(page, "work-mine")


def test_a_line_that_is_gone_says_so(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_page(path="/#/rule/no-such-line")
    expect(inspector(page)).to_have_class(OPEN)
    expect(inspector(page).locator(".ib")).to_contain_text("这条线路已经不在了")


# ----- the line inspector: every control writes config.toml --------------------------------------


def test_content_attendees_and_conference_are_saved(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_rule(open_page, fresh_live, "family-home")
    seg(page, "fields.content", "只显示忙碌").click()
    saved(page)
    seg(page, "fields.attendees", "不复制").click()
    seg(page, "fields.conference", "不复制").click()
    want = {"content": "busy", "attendees": "none", "conference": "none"}
    settles(
        fresh_live,
        "family-home",
        lambda run, disk: bool(run and disk and run["fields"] == want and disk["fields"] == want),
    )
    expect(section(page, "fields.content").locator(".hp")).to_have_text(
        "对方只看到一段被占用的时间，没有标题、地点和备注。"
    )
    reload(page)
    expect(seg(page, "fields.content", "只显示忙碌")).to_have_attribute("aria-pressed", PRESSED)
    expect(seg(page, "fields.attendees", "不复制")).to_have_attribute("aria-pressed", PRESSED)
    expect(seg(page, "fields.conference", "不复制")).to_have_attribute("aria-pressed", PRESSED)


def test_the_sync_range_is_saved(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_rule(open_page, fresh_live, "work-mine")
    seg(page, "window", "只同步一段时间").click()
    saved(page)
    settles(
        fresh_live,
        "work-mine",
        lambda run, disk: bool(
            run
            and disk
            and run["window"] == {"past": "7d", "future": "180d"}
            and disk.get("window") == {"past": "7d", "future": "180d"}
        ),
    )
    past = section(page, "window").get_by_label("过去的天数")
    future = section(page, "window").get_by_label("未来的天数")
    expect(past).to_have_value("7")
    past.fill("30")
    past.press("Enter")
    future.fill("365")
    future.press("Tab")
    want = {"past": "30d", "future": "365d"}
    settles(
        fresh_live,
        "work-mine",
        lambda run, disk: bool(run and disk and run["window"] == want and disk["window"] == want),
    )
    reload(page)
    expect(seg(page, "window", "只同步一段时间")).to_have_attribute("aria-pressed", PRESSED)
    expect(section(page, "window").get_by_label("过去的天数")).to_have_value("30")
    expect(section(page, "window").get_by_label("未来的天数")).to_have_value("365")
    seg(page, "window", "全部").click()
    settles(
        fresh_live,
        "work-mine",
        lambda run, disk: bool(run and disk and run["window"] is None and "window" not in disk),
    )


def test_reminders_are_saved(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_rule(open_page, fresh_live, "work-mine")
    seg(page, "alarms", "不要提醒").click()
    settles(
        fresh_live,
        "work-mine",
        lambda run, disk: bool(run and disk and run["alarms"] == "strip" and disk["alarms"] == "strip"),
    )
    seg(page, "alarms", "统一设置").click()
    settles(fresh_live, "work-mine", lambda run, disk: bool(run and disk and run["alarms"] == ["-10m"]))
    pills = section(page, "alarms").locator(".pill:not(.add)")
    expect(pills).to_have_text(["提前 10 分钟"])
    section(page, "alarms").get_by_role("button", name="添加提醒").click()
    section(page, "alarms").get_by_role("button", name="提前 1 小时", exact=True).click()
    settles(
        fresh_live,
        "work-mine",
        lambda run, disk: bool(
            run and disk and run["alarms"] == ["-10m", "-1h"] and disk["alarms"] == ["-10m", "-1h"]
        ),
    )
    expect(pills).to_have_text(["提前 10 分钟", "提前 1 小时"])
    section(page, "alarms").get_by_role("button", name="移除 提前 10 分钟").click()
    settles(fresh_live, "work-mine", lambda run, disk: bool(run and run["alarms"] == ["-1h"]))
    reload(page)
    expect(seg(page, "alarms", "统一设置")).to_have_attribute("aria-pressed", PRESSED)
    expect(section(page, "alarms").locator(".pill:not(.add)")).to_have_text(["提前 1 小时"])
    seg(page, "alarms", "跟 Google 一样").click()
    settles(fresh_live, "work-mine", lambda run, disk: bool(run and disk and run["alarms"] == "copy"))


def test_the_delete_guard_is_saved(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_rule(open_page, fresh_live, "family-home")
    count = section(page, "delete_guard").get_by_label("最多删除的个数")
    ratio = section(page, "delete_guard").get_by_label("最多删除的百分比")
    count.fill("20")
    count.press("Enter")
    saved(page)
    ratio.fill("30")
    ratio.press("Enter")
    want = {"max_count": 20, "max_ratio": 0.3}
    settles(
        fresh_live,
        "family-home",
        lambda run, disk: bool(run and disk and run["delete_guard"] == want and disk["delete_guard"] == want),
    )
    reload(page)
    expect(section(page, "delete_guard").get_by_label("最多删除的个数")).to_have_value("20")
    expect(section(page, "delete_guard").get_by_label("最多删除的百分比")).to_have_value("30")


@pytest.mark.parametrize(
    ("label", "typed", "message", "kept"),
    [
        ("最多删除的百分比", "0", "百分比要大于 0、不超过 100，没有保存。", "50"),
        ("最多删除的百分比", "150", "百分比要大于 0、不超过 100，没有保存。", "50"),
        ("最多删除的个数", "-1", "要是 0 或更大的整数，没有保存。", "10"),
        ("最多删除的个数", "2.5", "要是 0 或更大的整数，没有保存。", "10"),
    ],
)
def test_a_refused_value_is_shown_beside_its_field_and_reverted(
    open_page: OpenPage, fresh_live: LiveDaemon, label: str, typed: str, message: str, kept: str
) -> None:
    page = open_rule(open_page, fresh_live, "family-home")
    watch_of(page).allow(REFUSED)
    before = fresh_live.config_path.read_text(encoding="utf-8")
    field = section(page, "delete_guard").get_by_label(label)
    with page.expect_response(lambda r: r.url.endswith("/api/ops/rule.set")) as answer:
        field.fill(typed)
        field.press("Enter")
    assert answer.value.status == 422  # the daemon refused it: the check is the config's own
    error = section(page, "delete_guard").locator(".errt")
    expect(error).to_have_text(message)
    expect(field).to_have_value(kept)
    expect(field).to_have_class(re.compile(r"\berr\b"))
    assert fresh_live.config_path.read_text(encoding="utf-8") == before
    assert config_rule(fresh_live, "family-home")["delete_guard"] == {"max_count": 10, "max_ratio": 0.5}  # type: ignore[index]
    # A good value clears the error.
    field.fill("12")
    field.press("Enter")
    expect(error).to_have_count(0)


def test_a_refused_day_count_is_shown_beside_the_range(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_rule(open_page, fresh_live, "work-mine")
    watch_of(page).allow(REFUSED)
    seg(page, "window", "只同步一段时间").click()
    past = section(page, "window").get_by_label("过去的天数")
    expect(past).to_have_value("7")
    past.fill("-3")
    past.press("Enter")
    expect(section(page, "window").locator(".errt")).to_have_text("天数要是 0 或更大的数字，没有保存。")
    expect(past).to_have_value("7")
    assert config_rule(fresh_live, "work-mine")["window"] == {"past": "7d", "future": "180d"}  # type: ignore[index]


def test_a_conflict_reloads_the_line_with_a_notice(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    """The file changed underneath (here: by the CLI) and the write is answered 422
    ``config_conflict``: the inspector reads the line again and says so."""
    page = open_rule(open_page, fresh_live, "family-home")
    watch_of(page).allow(REFUSED)
    fresh_live.op("rule.set", {"name": "family-home", "key": "fields.content", "value": "busy"})

    def conflict(route: Any) -> None:
        route.fulfill(
            status=422,
            json={
                "error": {
                    "kind": "config_conflict",
                    "message": "the config file changed",
                    "problems": ["the config file changed"],
                }
            },
        )

    page.route("**/api/ops/rule.set", conflict, times=1)
    seg(page, "fields.attendees", "不复制").click()
    notice = inspector(page).locator(".note[data-role=conflict]")
    expect(notice).to_have_text("config.toml 刚在别处被改过，这里已经重新读取。要改的话请再改一次。")
    expect(seg(page, "fields.content", "只显示忙碌")).to_have_attribute("aria-pressed", PRESSED)  # reloaded
    expect(seg(page, "fields.attendees", "写进备注")).to_have_attribute("aria-pressed", PRESSED)  # reverted
    seg(page, "fields.attendees", "不复制").click()  # once more: written, the notice goes
    settles(fresh_live, "family-home", lambda run, disk: bool(run and run["fields"]["attendees"] == "none"))
    expect(notice).to_have_count(0)


# ----- pause, resume, 立即同步这一条 --------------------------------------------------------------


def test_pausing_dashes_the_wire_and_pauses_its_events(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_rule(open_page, fresh_live, "school-home")
    insp = inspector(page)
    insp.get_by_role("button", name="暂停").click()
    settles(
        fresh_live,
        "school-home",
        lambda run, disk: bool(run and disk and run["paused"] is True and disk.get("paused") is True),
    )
    expect(insp.locator(".statl")).to_have_text("已暂停 · 改动会攒着，恢复后一次送达")
    expect(insp.get_by_role("button", name="恢复同步")).to_be_visible()
    expect(insp.get_by_role("button", name="立即同步这一条")).to_be_disabled()
    expect(page.locator('.canvas svg.wires path.t[data-rule="school-home"]')).to_have_class(
        re.compile(r"\bpaused\b")
    )
    assert page.evaluate(
        """() => getComputedStyle(document.querySelector('.canvas svg.wires path.t[data-rule="school-home"]')).strokeDasharray"""
    ) not in ("none", "")
    # What the calendar view (Task 6) will draw: every delivery of the line is paused.
    events = fresh_live.get("/api/events", rule="school-home")["events"]
    states = {d["state"] for e in events for d in e["deliveries"] if d["rule"] == "school-home"}
    assert events and states == {"paused"}
    reload(page)
    expect(inspector(page).locator(".statl")).to_have_text("已暂停 · 改动会攒着，恢复后一次送达")
    inspector(page).get_by_role("button", name="恢复同步").click()
    settles(
        fresh_live,
        "school-home",
        lambda run, disk: bool(run and disk and run["paused"] is False and "paused" not in disk),
    )
    expect(page.locator('.canvas svg.wires path.t[data-rule="school-home"]')).not_to_have_class(
        re.compile(r"\bpaused\b")
    )
    expect(inspector(page).get_by_role("button", name="暂停")).to_be_visible()


def test_sync_this_line_runs_one_cycle_of_it(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_rule(open_page, fresh_live, "family-mine")
    before = fresh_live.cycles()
    inspector(page).get_by_role("button", name="立即同步这一条").click()
    expect(inspector(page).locator("[data-role=sync-note]")).to_have_text("同步完成，送达了 1 处改动")
    assert fresh_live.cycles() == before + 1
    expect(inspector(page).get_by_role("button", name="立即同步这一条")).to_be_enabled()


# ----- deleting a line ---------------------------------------------------------------------------


def open_delete(page: Page) -> Locator:
    inspector(page).locator(".ifoot").get_by_role("button", name="删除这条线路").click()
    panel = inspector(page).locator("[data-role=delete]")
    expect(panel).to_be_visible()
    return panel


def test_the_delete_choice_explains_both_ways(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_rule(open_page, live, "school-home")
    panel = open_delete(page)
    keep = panel.get_by_role("radio", name=re.compile("^保留副本"))
    drop = panel.get_by_role("radio", name=re.compile("^连副本一起删除"))
    expect(keep).to_have_attribute("aria-checked", "true")  # the safe one is the default
    expect(keep).to_contain_text("线路马上消失")
    expect(drop).to_contain_text("先删除 calsync 写进")
    expect(drop).to_contain_text("3 个副本")
    expect(drop).to_contain_text("数量多时会先停下来，等你确认")
    expect(drop).to_contain_text("副本删完后，这条线路才会消失")
    expect(drop.locator(".cm")).to_have_text("家庭 · family-icloud")
    page.keyboard.press("Escape")  # closes the choice, not the inspector
    expect(panel).to_have_count(0)
    expect(inspector(page)).to_have_class(OPEN)
    assert config_rule(live, "school-home") is not None


def test_deleting_and_keeping_the_copies_removes_the_wire(
    open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    copies = fresh_live.in_loop(
        lambda: len(
            fresh_live.world.targets["family-icloud"].managed(
                fresh_live.world.targets["family-icloud"].calendars["家庭"]
            )
        )
    )
    page = open_rule(open_page, fresh_live, "school-home")
    panel = open_delete(page)
    panel.get_by_role("button", name="删除", exact=True).click()
    expect(inspector(page)).not_to_have_class(OPEN)
    expect(page).to_have_url(re.compile(r"/(#/?)?$"))
    expect(page.locator('.canvas .hit[data-rule="school-home"]')).to_have_count(0)
    expect(page.locator(".hub-cap small")).to_have_text("3 条线路")
    settles(fresh_live, "school-home", lambda run, disk: run is None and disk is None)
    after = fresh_live.in_loop(
        lambda: len(
            fresh_live.world.targets["family-icloud"].managed(
                fresh_live.world.targets["family-icloud"].calendars["家庭"]
            )
        )
    )
    assert after == copies  # the copies stay in 家庭


def test_deleting_with_the_copies_shows_the_line_retiring(
    open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    page = open_rule(open_page, fresh_live, "school-home")
    panel = open_delete(page)
    panel.get_by_role("radio", name=re.compile("^连副本一起删除")).click()
    panel.get_by_role("button", name="删除", exact=True).click()
    settles(
        fresh_live,
        "school-home",
        lambda run, disk: bool(run and disk and run["retire"] is True and disk.get("retire") is True),
    )
    insp = inspector(page)
    expect(insp).to_have_class(OPEN)
    expect(insp.locator(".statl")).to_have_text(
        re.compile(r"^正在移除 · 先删除目标日历里的 3 个副本，删完后这条线路会消失")
    )
    expect(page.locator('.canvas svg.wires path.t[data-rule="school-home"]')).to_have_class(
        re.compile(r"\bretiring\b")
    )
    expect(insp.locator(".fs[data-key]")).to_have_count(0)  # nothing to set on a line being removed
    expect(insp.get_by_role("button", name="取消移除")).to_be_visible()


# ----- the calendar inspector --------------------------------------------------------------------


def test_a_source_calendar_lists_its_lines(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_page(path=f"/#/calendar/personal/{FAMILY.replace('@', '%40')}")
    insp = inspector(page)
    expect(insp).to_have_class(OPEN)
    expect(insp.locator("h2")).to_have_text("日历")
    sub = insp.locator(".ih p")
    expect(sub.locator(".cm")).to_have_text("家庭日程 · personal")
    expect(sub.get_by_role("img", name="Google 日历")).to_have_count(1)
    expect(sub).not_to_contain_text("@group")  # never the raw id
    expect(insp.locator(".fs .lb").first).to_have_text("同步到")
    rows = insp.locator(".rows .row")
    expect(rows.locator(".cm")).to_have_text(["家庭 · family-icloud", "我的日历 · my-icloud"])
    expect(rows.locator(".r")).to_have_text(["正常", "正常"])
    rows.first.click()
    expect(page).to_have_url(re.compile(r"#/rule/family-home$"))
    expect(insp.locator(".route-chip")).to_be_visible()


def test_a_target_calendar_says_what_calsync_manages(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_page(path="/#/calendar/family-icloud/%2Fdav%2Fhome%2F%E5%AE%B6%E5%BA%AD%2F")
    insp = inspector(page)
    expect(insp.locator(".ih p .cm")).to_have_text("家庭 · family-icloud")
    expect(insp.locator(".fs .lb").first).to_have_text("来自")
    expect(insp.locator(".rows .row .cm")).to_have_text(["家庭日程 · personal", "孩子学校 · personal"])
    expect(insp).to_contain_text("calsync 只管理自己写进去的日程。")
    insp.get_by_role("link", name="同步到这个日历").click()
    expect(page).to_have_url(re.compile(r"#/new\?target="))
    expect(radio(page, "targets", "家庭")).to_have_attribute("aria-checked", "true")


def test_an_unsynced_calendar_offers_a_new_line(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_page(path=f"/#/calendar/personal/{HOLIDAYS.replace('#', '%23').replace('@', '%40')}")
    insp = inspector(page)
    expect(insp.locator(".ih p .cm")).to_have_text("中国节假日 · personal")
    expect(insp).to_contain_text("还没有线路。日历里的日程不会被同步。")
    insp.get_by_role("link", name="从这个日历新建线路").click()
    expect(page).to_have_url(re.compile(r"#/new\?source="))
    expect(insp.locator("h2")).to_have_text("新线路")
    expect(radio(page, "sources", "中国节假日")).to_have_attribute("aria-checked", "true")


# ----- the new line ------------------------------------------------------------------------------


def radio(page: Page, side: str, name: str) -> Locator:
    """A calendar to pick, among the sources or the targets."""
    return inspector(page).locator(f"[data-role={side}]").get_by_role("radio", name=re.compile(f"^{name}"))


def test_a_new_line_from_the_map_previews_creates_and_syncs(
    open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    page = open_page(daemon=fresh_live)
    page.locator(".canvas").get_by_role("link", name="新线路").click()
    insp = inspector(page)
    expect(page).to_have_url(re.compile(r"#/new$"))
    expect(insp.locator("h2")).to_have_text("新线路")
    # Sources grouped by account, each group with its service mark.
    groups = insp.locator("[data-role=sources] .grp")
    expect(groups).to_have_text(["personal", "work"])
    expect(groups.get_by_role("img", name="Google 日历")).to_have_count(2)
    expect(insp.locator("[data-role=targets] .grp")).to_have_text(["family-icloud", "my-icloud"])
    create = insp.get_by_role("button", name="创建并开始同步")
    expect(create).to_be_disabled()
    # Pairs that already sync are disabled.
    radio(page, "sources", "家庭日程").click()
    expect(radio(page, "targets", "家庭")).to_be_disabled()
    expect(radio(page, "targets", "我的日历")).to_be_disabled()
    expect(radio(page, "targets", "我的日历")).to_contain_text("已经在同步")
    radio(page, "sources", "孩子学校").click()
    expect(radio(page, "targets", "家庭")).to_be_disabled()
    expect(radio(page, "targets", "我的日历")).to_be_enabled()
    radio(page, "targets", "我的日历").click()
    preview = insp.locator(".preview")
    expect(preview).to_contain_text("将在")
    expect(preview.locator(".cm")).to_have_text(["我的日历 · my-icloud", "我的日历 · my-icloud"])
    expect(preview.locator("b.num")).to_have_text("3")
    expect(preview).to_contain_text("个系列")
    expect(preview).to_contain_text("里原有的日程不会被动。")
    before = fresh_live.cycles()
    create.click()
    expect(page).to_have_url(re.compile(r"#/rule/personal-to-my-icloud$"))
    settles(
        fresh_live,
        "personal-to-my-icloud",
        lambda run, disk: bool(
            run
            and disk
            and run["source"] == {"account": "personal", "calendar": "孩子学校"}
            and run["target"]["account"] == "my-icloud"
            and run["target"]["calendar"] == "我的日历"
            and "create_if_missing" not in disk["target"]
        ),
    )
    until(lambda: fresh_live.cycles() == before + 1)
    expect(page.locator('.canvas .hit:not(.in)[data-rule="personal-to-my-icloud"]')).to_have_count(1)
    expect(page.locator(".canvas svg.wires .comets")).to_have_attribute("data-born", "personal-to-my-icloud")
    expect(page.locator(".hub-cap small")).to_have_text("5 条线路")
    expect(inspector(page).locator(".statl")).to_have_text(re.compile(r"^正常 · 刚刚同步 · 已同步 3 个系列$"))


def test_a_new_line_can_create_its_icloud_calendar(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_page(
        daemon=fresh_live,
        path=f"/#/new?source=personal&calendar={HOLIDAYS.replace('#', '%23').replace('@', '%40')}",
    )
    insp = inspector(page)
    expect(radio(page, "sources", "中国节假日")).to_have_attribute("aria-checked", "true")
    radio(page, "targets", "在 iCloud 里新建一个日历").click()
    insp.locator("[data-role=new-calendar]").get_by_role("radio", name="family-icloud").click()
    insp.get_by_label("新日历的名字").fill("节假日")
    preview = insp.locator(".preview")
    expect(preview).to_contain_text("这个日历还不存在，创建线路时会在 iCloud 里新建它。")
    expect(preview.locator("b.num")).to_have_text("0")
    insp.get_by_role("button", name="创建并开始同步").click()
    expect(page).to_have_url(re.compile(r"#/rule/personal-to-family-icloud$"))
    settles(
        fresh_live,
        "personal-to-family-icloud",
        lambda run, disk: bool(
            run
            and disk
            and run["target"] == {"account": "family-icloud", "calendar": "节假日", "create_if_missing": True}
            and disk["target"].get("create_if_missing") is True
        ),
    )
    expect(page.locator('.canvas .hit:not(.in)[data-rule="personal-to-family-icloud"]')).to_have_count(1)


def test_a_preview_that_needs_a_person_shows_the_command(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.in_loop(lambda: fresh_live.world.refused.add("personal"))
    page = open_page(daemon=fresh_live, path="/#/new")
    radio(page, "sources", "中国节假日").click()
    radio(page, "targets", "家庭").click()
    note = inspector(page).locator("[data-role=preview-error]")
    expect(note).to_contain_text("预演没有完成")
    expect(note.locator(".cmd code")).to_have_text("calsync auth personal")
    expect(note.get_by_role("button", name="复制")).to_be_visible()
    expect(inspector(page).get_by_role("button", name="创建并开始同步")).to_be_disabled()


def test_preview_and_disabled_pairs_follow_the_status(open_page: OpenPage, live: LiveDaemon) -> None:
    """A pair is matched by the calendar either end names (its name or its id)."""
    page = open_page(path=f"/#/new?source=personal&calendar={SCHOOL.replace('@', '%40')}")
    expect(radio(page, "sources", "孩子学校")).to_have_attribute("aria-checked", "true")
    expect(radio(page, "targets", "家庭")).to_be_disabled()
    refresh_status(page)
    expect(radio(page, "targets", "家庭")).to_be_disabled()


# ----- the phone ---------------------------------------------------------------------------------


@pytest.mark.parametrize("viewport", [PHONE, NARROW], ids=["390", "360"])
def test_the_line_inspector_fits_a_phone(
    open_page: OpenPage, live: LiveDaemon, viewport: dict[str, int]
) -> None:
    page = open_rule(open_page, live, "family-mine", viewport=viewport)
    seg(page, "alarms", "跟 Google 一样").wait_for()
    sizes = page.evaluate(
        """() => [...document.querySelectorAll('aside.insp .ib button, aside.insp .ib input, aside.insp .ifoot button')]
          .filter((el) => el.offsetParent !== null)
          .map((el) => ({ h: el.getBoundingClientRect().height, f: getComputedStyle(el).fontSize, tag: el.tagName, t: el.textContent }))"""
    )
    assert sizes
    for item in sizes:
        assert item["h"] >= 44 - 0.01, item  # layout units: 43.99994 is 44
        if item["tag"] == "INPUT":
            assert item["f"] == "16px", item
    overflow = page.evaluate(
        "() => { const b = document.querySelector('aside.insp .ib'); return b.scrollWidth - b.clientWidth; }"
    )
    assert overflow <= 0
    assert page.evaluate("document.documentElement.scrollWidth") <= viewport["width"]
