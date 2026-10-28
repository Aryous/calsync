"""The sync map against a live daemon, in Chromium and WebKit: accounts, calendar pills, the hub
and one wire per line from /api/status; wires that end on the right pills; per-line state on the
wire; tooltips that name the service and account; focus dimming; the status sentence and its
offline state; attention cards; 立即同步's wave; reduced motion; the phone's three columns."""

import re
from typing import Any

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
    sync_button,
    watch_of,
)
from tests.browser.live import LiveDaemon

pytestmark = pytest.mark.browser

BOTH = pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=VIEWPORT_IDS)
OPEN = re.compile(r"\bopen\b")


def pill_key(status: dict[str, Any], rule: str, side: str) -> str:
    """The map's key (``<account>/<calendar id>``) of the calendar a rule reads or writes, from
    the status: the account's cached calendar that lists the rule."""
    spec = next(r for r in status["rules"] if r["name"] == rule)[side]
    account = next(a for a in status["accounts"] if a["name"] == spec["account"])
    calendar = next(c for c in account["calendars"] if rule in c["rules"])
    return f"{account['name']}/{calendar['id']}"


# Geometry, in client coordinates: every route's first and last point, every trace's ends, the
# ports and the pills' edges. Paths are in the overlay's user space; getScreenCTM maps them.
GEOMETRY = """() => {
  const svg = document.querySelector(".canvas svg.wires");
  const m = svg.getScreenCTM();
  const at = (path, s) => { const p = path.getPointAtLength(s).matrixTransform(m); return { x: p.x, y: p.y }; };
  const ends = (path) => [at(path, 0), at(path, path.getTotalLength())];
  const box = (el) => el.getBoundingClientRect().toJSON();
  const out = { routes: {}, traces: {}, ports: {}, pills: {}, hub: box(document.querySelector(".canvas .hub")) };
  for (const p of svg.querySelectorAll(".hit[data-rule]:not(.in)")) out.routes[p.dataset.rule] = { from: p.dataset.from, to: p.dataset.to, ends: ends(p) };
  for (const p of svg.querySelectorAll(".t[data-rule]")) out.traces[p.dataset.rule] = ends(p);
  for (const c of svg.querySelectorAll(".port[data-port]")) { const r = c.getBoundingClientRect(); out.ports[c.dataset.port] = { x: r.x + r.width / 2, y: r.y + r.height / 2 }; }
  for (const el of document.querySelectorAll(".canvas .cal[data-cal]")) out.pills[el.dataset.cal] = box(el);
  return out;
}"""


def geometry(page: Page) -> dict[str, Any]:
    return page.evaluate(GEOMETRY)


def near(p: dict[str, float], x: float, y: float, tolerance: float = 2.5) -> bool:
    return abs(p["x"] - x) <= tolerance and abs(p["y"] - y) <= tolerance


def point_on(page: Page, rule: str, fraction: float) -> tuple[float, float]:
    """A point on a line's route, in client coordinates (``fraction`` of its length)."""
    return page.evaluate(
        """([rule, f]) => {
          const svg = document.querySelector(".canvas svg.wires");
          const path = [...svg.querySelectorAll(".hit[data-rule]:not(.in)")].find((p) => p.dataset.rule === rule);
          const p = path.getPointAtLength(path.getTotalLength() * f).matrixTransform(svg.getScreenCTM());
          return [p.x, p.y];
        }""",
        [rule, fraction],
    )


def classes(page: Page, selector: str, attr: str, value: str) -> str:
    """The class of the element matching ``selector`` whose ``attr`` is ``value`` (ids carry
    ``#`` and ``/``, so no attribute selector)."""
    return page.evaluate(
        """([selector, attr, value]) => {
          const el = [...document.querySelectorAll(selector)].find((e) => e.getAttribute(attr) === value);
          return el ? el.getAttribute("class") : null;
        }""",
        [selector, attr, value],
    )


def dash(page: Page, seg: str) -> str:
    """The computed stroke-dasharray of the wire segment ``data-seg=seg``."""
    return page.evaluate(
        """(seg) => {
          const el = [...document.querySelectorAll(".canvas svg.wires [data-seg]")].find((e) => e.dataset.seg === seg);
          return el ? getComputedStyle(el).strokeDasharray : null;
        }""",
        seg,
    )


def pills(page: Page, side: str) -> list[str]:
    return page.locator(f".canvas .col.{side} .cal .nm").all_inner_texts()


# ----- what the map shows --------------------------------------------------------------------


@BOTH
def test_the_map_shows_every_account_calendar_and_line(
    paired_page: Page, live: LiveDaemon, viewport: dict[str, int]
) -> None:
    page = paired_page
    ready(page)
    status = live.get("/api/status")
    source = page.locator(".canvas .col.src")
    target = page.locator(".canvas .col.tgt")
    expect(source.locator(".acct-h .an")).to_have_text(["personal", "work"])
    expect(target.locator(".acct-h .an")).to_have_text(["family-icloud", "my-icloud"])
    assert pills(page, "src") == ["家庭日程", "孩子学校", "中国节假日", "工作"]
    assert pills(page, "tgt") == ["家庭", "我的日历"]
    # Each account header carries its service mark, named for assistive tech.
    expect(source.locator(".acct-h svg.svc").first).to_have_attribute("aria-label", "Google 日历")
    expect(target.locator(".acct-h svg.svc").first).to_have_attribute("aria-label", "iCloud 日历")
    # Every line from the status has one wire (a route, its hub trace) and nothing else does.
    rules = sorted(r["name"] for r in status["rules"])
    assert (
        sorted(page.locator(".canvas .hit:not(.in)").evaluate_all("els => els.map(e => e.dataset.rule)"))
        == rules
    )
    assert sorted(page.locator(".canvas .t").evaluate_all("els => els.map(e => e.dataset.rule)")) == rules
    expect(page.locator(".hub-cap small")).to_have_text("4 条线路")
    # A calendar no line uses is dashed and says so; a used one is not.
    holidays = source.locator(".cal", has_text="中国节假日")
    expect(holidays).to_have_class(re.compile(r"\bidle\b"))
    expect(source.locator(".cal", has_text="家庭日程")).not_to_have_class(re.compile(r"\bidle\b"))
    if viewport == DESKTOP:
        expect(source.locator(".acct-h small")).to_have_text(["2 个日历在同步", "1 个日历在同步"])
        expect(target.locator(".acct-h small")).to_have_text(["1 个日历在接收", "1 个日历在接收"])
        expect(holidays.locator("em")).to_have_text("未同步")
        series = {
            c["name"]: c["series"]
            for a in status["accounts"]
            for c in a["calendars"]
            if c["series"] is not None
        }
        expect(source.locator(".cal", has_text="家庭日程").locator("em")).to_have_text(
            str(series["家庭日程"])
        )
        expect(source.locator(".cal", has_text="工作").locator("em")).to_have_text(str(series["工作"]))
        expect(target.locator(".cal", has_text="家庭").locator("em")).to_have_text("2 个来源")
        expect(page.get_by_role("link", name="连接 Google 账号")).to_be_visible()
        expect(page.get_by_role("link", name="连接 iCloud 账号")).to_be_visible()
    # Source pills are coloured by calendar; the unused one has no colour.
    dots = source.locator(".cal .dot").evaluate_all("els => els.map(e => e.getAttribute('class'))")
    assert dots == ["dot blue", "dot sage", "dot none", "dot violet"]
    # Each pill opens its calendar; each header its account (ids with # and / are encoded).
    expect(source.locator(".cal", has_text="中国节假日")).to_have_attribute(
        "href", "#/calendar/personal/zh.china%23holiday%40group.v.calendar.google.com"
    )
    expect(target.locator(".cal", has_text="家庭")).to_have_attribute(
        "href", "#/calendar/family-icloud/%2Fdav%2Fhome%2F%E5%AE%B6%E5%BA%AD%2F"
    )
    expect(source.locator(".acct-h").first).to_have_attribute("href", "#/account/personal")
    assert watch_of(page).problems() == []


@BOTH
def test_the_wires_connect_the_right_pills(
    paired_page: Page, live: LiveDaemon, viewport: dict[str, int]
) -> None:
    """Each line's route starts at its source pill's right edge, ends at its target pill's left
    edge (the pills the status names), and its hub trace runs between two ports on the hub's
    sides."""
    page = paired_page
    ready(page)
    status = live.get("/api/status")
    geo = geometry(page)
    hub = geo["hub"]
    for rule in status["rules"]:
        name = rule["name"]
        src, tgt = pill_key(status, name, "source"), pill_key(status, name, "target")
        route = geo["routes"][name]
        assert (route["from"], route["to"]) == (src, tgt)
        start, end = route["ends"]
        a, b = geo["pills"][src], geo["pills"][tgt]
        assert near(start, a["right"], a["top"] + a["height"] / 2), (name, start, a)
        assert near(end, b["left"], b["top"] + b["height"] / 2), (name, end, b)
        p, q = geo["ports"][f"s:{src}"], geo["ports"][f"t:{tgt}"]
        assert near(p, hub["left"], p["y"], 1) and hub["top"] < p["y"] < hub["bottom"], (name, p, hub)
        assert near(q, hub["right"], q["y"], 1) and hub["top"] < q["y"] < hub["bottom"], (name, q, hub)
        trace = geo["traces"][name]
        assert near(trace[0], p["x"], p["y"]) and near(trace[1], q["x"], q["y"]), (name, trace, p, q)


def test_the_wires_follow_a_resize(paired_page: Page) -> None:
    page = paired_page
    ready(page)
    page.set_viewport_size(PHONE)  # pyright: ignore[reportArgumentType]
    page.wait_for_function(
        """() => {
          const svg = document.querySelector(".canvas svg.wires");
          const path = svg.querySelector(".hit[data-rule]:not(.in)");
          const pill = [...document.querySelectorAll(".canvas .cal")].find((c) => c.dataset.cal === path.dataset.from);
          const p = path.getPointAtLength(0).matrixTransform(svg.getScreenCTM());
          return Math.abs(p.x - pill.getBoundingClientRect().right) < 2.5;
        }"""
    )


# ----- state on the wire ---------------------------------------------------------------------


def test_a_paused_line_is_dashed(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.pause("school-home")
    page = open_page(daemon=fresh_live)
    ready(page)
    status = fresh_live.get("/api/status")
    school = pill_key(status, "school-home", "source")
    home = pill_key(status, "school-home", "target")
    expect(page.locator(".canvas .t[data-rule='school-home']")).to_have_class(re.compile(r"\bpaused\b"))
    assert dash(page, "r:school-home") not in ("none", "", None)
    assert dash(page, f"s:{school}") not in ("none", "", None)  # 孩子学校 has no other line
    assert dash(page, "r:family-home") == "none"
    assert dash(page, f"t:{home}") == "none"  # 家庭 still receives from 家庭日程
    expect(page.locator(".result h1")).to_have_text("一条线路暂停中，其他都正常")


def test_a_line_that_needs_a_person_shows_it_on_the_map(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.stage_auth()  # work-mine: the work account's credential is refused
    fresh_live.stage_blocked()  # school-home: three deletes held by the guard
    page = open_page(daemon=fresh_live)
    ready(page)
    expect(page.locator(".canvas .t[data-rule='work-mine']")).to_have_class(re.compile(r"\bbad\b"))
    expect(page.locator(".canvas .t[data-rule='school-home']")).to_have_class(re.compile(r"\bheld\b"))
    expect(page.locator(".canvas .t[data-rule='family-home']")).to_have_class(re.compile(r"^t ok\b"))
    assert dash(page, "r:work-mine") not in ("none", "", None)
    work = page.locator(".canvas .col.src .acct").filter(has=page.locator(".an", has_text="work"))
    expect(work.locator(".acct-h small")).to_have_text("授权已失效")
    # The tooltip says what is wrong.
    title = page.locator(".canvas .hit[data-rule='work-mine']:not(.in) title")
    expect(title).to_have_text("Google · work · 工作 → iCloud · my-icloud · 我的日历 · 授权失效")


# ----- tooltips, clicks and focus --------------------------------------------------------------


def test_the_wire_tooltip_names_service_and_account(paired_page: Page) -> None:
    page = paired_page
    ready(page)
    text = "Google · work · 工作 → iCloud · my-icloud · 我的日历 · 正常"
    hit = page.locator(".canvas .hit[data-rule='work-mine']:not(.in)")
    expect(hit.locator("title")).to_have_text(text)
    expect(hit).to_have_attribute("aria-label", text)
    x, y = point_on(page, "work-mine", 0.12)  # between 工作 and the hub: no other line runs there
    page.mouse.move(x, y)
    tip = page.locator(".canvas .tip")
    expect(tip).to_have_class(re.compile(r"\bon\b"))
    expect(tip).to_have_text(text)
    # While hovered, the other lines step back.
    expect(page.locator(".canvas .t[data-rule='work-mine']")).to_have_class(re.compile(r"\bhot\b"))
    expect(page.locator(".canvas .t[data-rule='family-home']")).to_have_class(re.compile(r"\bdim\b"))
    page.mouse.move(5, 5)
    expect(tip).not_to_have_class(re.compile(r"\bon\b"))
    expect(page.locator(".canvas .t[data-rule='family-home']")).not_to_have_class(re.compile(r"\bdim\b"))


def test_a_pill_tooltip_names_service_and_account(paired_page: Page) -> None:
    page = paired_page
    ready(page)
    pill = page.locator(".canvas .col.src .cal", has_text="家庭日程")
    expect(pill).to_have_attribute("title", "Google · personal · 家庭日程")
    expect(pill).to_have_accessible_name(re.compile(r"^Google · personal · 家庭日程"))


def test_clicking_a_wire_opens_its_line(paired_page: Page, live: LiveDaemon) -> None:
    page = paired_page
    ready(page)
    x, y = point_on(page, "school-home", 0.12)  # 孩子学校's own stretch
    page.mouse.click(x, y)
    expect(page).to_have_url(re.compile(r"#/rule/school-home$"))
    expect(page.locator("aside.insp")).to_have_class(OPEN)
    # The selected line stays lit; what it does not touch is dimmed.
    status = live.get("/api/status")
    lit = {pill_key(status, "school-home", "source"), pill_key(status, "school-home", "target")}
    for key, cls in page.locator(".canvas .cal").evaluate_all(
        "els => els.map(e => [e.dataset.cal, e.getAttribute('class')])"
    ):
        assert ("dim" in cls.split()) == (key not in lit), (key, cls)
    expect(page.locator(".canvas .t[data-rule='school-home']")).to_have_class(re.compile(r"\bhot\b"))
    expect(page.locator(".canvas .t[data-rule='work-mine']")).to_have_class(re.compile(r"\bdim\b"))
    page.mouse.move(5, 5)  # off the map: the open line alone keeps the others dimmed
    expect(page.locator(".canvas .t[data-rule='work-mine']")).to_have_class(re.compile(r"\bdim\b"))
    page.locator("aside.insp").get_by_role("button", name="关闭").click()
    expect(page.locator("aside.insp")).not_to_have_class(OPEN)
    expect(page.locator(".canvas .cal.dim")).to_have_count(0)


def test_a_wire_opens_from_the_keyboard(paired_page: Page) -> None:
    page = paired_page
    ready(page)
    wire = page.locator(".canvas .hit[data-rule='family-mine']:not(.in)")
    expect(wire).to_have_attribute("role", "link")
    wire.focus()
    expect(page.locator(".canvas .t[data-rule='family-mine']")).to_have_class(re.compile(r"\bhot\b"))
    page.keyboard.press("Enter")
    expect(page).to_have_url(re.compile(r"#/rule/family-mine$"))


def test_hovering_a_calendar_dims_the_rest(paired_page: Page) -> None:
    page = paired_page
    ready(page)
    page.locator(".canvas .col.src .cal", has_text="工作").hover()
    expect(page.locator(".canvas .t[data-rule='work-mine']")).to_have_class(re.compile(r"\bhot\b"))
    for other in ("family-home", "school-home", "family-mine"):
        expect(page.locator(f".canvas .t[data-rule='{other}']")).to_have_class(re.compile(r"\bdim\b"))
    expect(page.locator(".canvas .col.src .cal", has_text="家庭日程")).to_have_class(re.compile(r"\bdim\b"))
    expect(page.locator(".canvas .col.tgt .cal", has_text="我的日历")).not_to_have_class(
        re.compile(r"\bdim\b")
    )


def test_an_open_calendar_keeps_its_lines_lit(open_page: OpenPage) -> None:
    page = open_page(path="/#/account/work")
    ready(page)
    expect(page.locator(".canvas .t[data-rule='work-mine']")).to_have_class(re.compile(r"\bhot\b"))
    expect(page.locator(".canvas .t[data-rule='family-home']")).to_have_class(re.compile(r"\bdim\b"))


@pytest.mark.parametrize("service", ["Google", "iCloud"])
def test_the_connect_links_open_the_wizard(paired_page: Page, service: str) -> None:
    page = paired_page
    ready(page)
    page.get_by_role("link", name=f"连接 {service} 账号").click()
    expect(page).to_have_url(re.compile(rf"#/connect/{service.lower()}$"))
    expect(page.locator("aside.insp")).to_have_class(OPEN)
    expect(page.locator("aside.insp h2")).to_have_text(f"连接 {service} 账号")


# ----- the status sentence ---------------------------------------------------------------------


def test_the_status_sentence_matches_the_status(paired_page: Page, live: LiveDaemon) -> None:
    page = paired_page
    ready(page)
    status = live.get("/api/status")
    assert status["summary"]["health"] == "ok" and status["summary"]["rules"] == 4
    expect(page.locator(".result h1")).to_have_text("四条线路都在正常工作")
    delivered = status["summary"]["delivered_today"]
    # 11:42 local: next_cycle_at, an hour after the pinned 10:42.
    assert status["daemon"]["next_cycle_at"] == "2026-10-26T03:42:00+00:00"
    expect(page.locator(".result .detail")).to_have_text(
        f"刚刚同步 · 今天送达了 {delivered} 处改动 · 下次检查 11:42"
    )
    # The count is evidence: it opens the activity behind it.
    link = page.locator(".result .detail a")
    expect(link).to_have_text(f"{delivered} 处改动")
    link.click()
    expect(page.get_by_role("tab", name="活动")).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("tab", name="活动")).to_be_in_viewport()
    page.wait_for_function("() => window.scrollY > 0")  # the tabs were brought into view


def test_the_sentence_says_when_calsync_is_unreachable(paired_page: Page) -> None:
    """The poll fails after the status was read: the sentence stops claiming all is well and says
    when calsync was last seen; the next good poll restores it. (The browser refuses the request
    with page.route: the daemon keeps running, the page sees what a stopped one would cause.)"""
    page = paired_page
    ready(page)
    watch_of(page).allow(r"Failed to load resource|ERR_FAILED|Load failed|/api/status")
    page.route("**/api/status", lambda route: route.abort())
    refresh_status(page)
    expect(page.locator(".result h1")).to_have_text("连不上 calsync")
    expect(page.locator(".result .detail")).to_have_text("最后一次看到是 10:42 · 正在重试")
    expect(page.locator(".canvas")).to_have_class(re.compile(r"\bstale\b"))
    page.unroute("**/api/status")
    refresh_status(page)
    expect(page.locator(".result h1")).to_have_text("四条线路都在正常工作")
    expect(page.locator(".canvas")).not_to_have_class(re.compile(r"\bstale\b"))


def test_sync_now_runs_a_cycle_and_the_sentence_updates(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_page(daemon=fresh_live)
    ready(page)
    before = fresh_live.get("/api/status")["summary"]["delivered_today"]
    expect(page.locator(".result .detail a")).to_have_text(f"{before} 处改动")
    cycles = fresh_live.cycles()
    fresh_live.hold_cycles()
    try:
        sync_button(page).click()
        expect(sync_button(page)).to_have_accessible_name("同步中…")
        # The sync wave: a comet on every line that runs.
        page.wait_for_function("() => document.querySelectorAll('.canvas .comets path').length >= 4")
    finally:
        fresh_live.release_cycles()
    expect(page.locator("[data-role=result-note]")).to_have_text("同步完成，送达了 1 处改动")
    after = fresh_live.get("/api/status")["summary"]["delivered_today"]
    assert after == before + 1 and fresh_live.cycles() == cycles + 1
    expect(page.locator(".result .detail a")).to_have_text(f"{after} 处改动")
    expect(sync_button(page)).to_have_accessible_name("立即同步")


# ----- attention cards ---------------------------------------------------------------------------


def test_an_auth_card_lists_the_reasons_and_the_command(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.stage_auth()
    page = open_page(daemon=fresh_live)
    ready(page)
    expect(page.locator(".result h1")).to_have_text("一条线路停了，其他都正常")
    card = page.locator(".attn[data-kind=auth]")
    expect(card).to_have_count(1)
    expect(card.locator("b").first).to_have_text("work 的授权失效了")
    expect(card.locator("b svg.svc").first).to_have_attribute("aria-label", "Google 日历")
    expect(card.locator(".why li")).to_have_text(
        [
            "Google 账号的密码改了",
            "在 Google 账号里撤销了 calsync 的访问权限",
            "Google 应用还在“测试”状态：授权每 7 天就要重新做一次",
        ]
    )
    expect(card.locator(".cmd code")).to_have_text("calsync auth work")
    # The line it stopped, named with service and account.
    expect(card.locator(".route .cm")).to_have_count(2)
    expect(card.locator(".route")).to_contain_text("工作 · work")
    expect(card.locator(".route")).to_contain_text("我的日历 · my-icloud")
    card.get_by_role("button", name="复制").click()
    expect(card.locator(".copied")).to_have_text(re.compile(r"^(已复制|已选中，请手动复制)$"))
    assert page.evaluate("String(window.getSelection())") in ("calsync auth work", "")


def test_a_blocked_card_opens_the_blocked_deletes(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.stage_blocked()
    page = open_page(daemon=fresh_live)
    ready(page)
    card = page.locator(".attn[data-kind=blocked]")
    expect(card).to_have_count(1)
    expect(card.locator("b").first).to_contain_text("3 个副本等你确认删除")
    expect(card.locator("b a")).to_have_attribute("href", "#/blocked/school-home")
    card.get_by_role("link", name="查看并处理").click()
    expect(page).to_have_url(re.compile(r"#/blocked/school-home$"))
    expect(page.locator("aside.insp")).to_have_class(OPEN)


def test_a_failing_card_opens_the_line(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.stage_failing()
    page = open_page(daemon=fresh_live)
    ready(page)
    card = page.locator(".attn[data-kind=failing]")
    expect(card).to_have_count(1)
    expect(card.locator("b").first).to_contain_text("连续 1 次同步失败")
    expect(page.locator(".canvas .t[data-rule='family-mine']")).to_have_class(re.compile(r"\bbad\b"))
    card.get_by_role("link", name="查看线路").click()
    expect(page).to_have_url(re.compile(r"#/rule/family-mine$"))


def test_a_healthy_map_has_no_attention_card(paired_page: Page) -> None:
    page = paired_page
    ready(page)
    expect(page.locator(".attn")).to_have_count(0)


# ----- motion ----------------------------------------------------------------------------------------


def test_comets_run_while_idle(paired_page: Page) -> None:
    page = paired_page
    ready(page)
    page.wait_for_function(
        """() => document.getAnimations().some((a) => a.id === "comet" && a.playState === "running")""",
        timeout=8000,
    )


def test_reduced_motion_has_no_running_animations(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_page(daemon=fresh_live, reduced_motion="reduce")
    ready(page)
    sync_button(page).click()
    expect(page.locator("[data-role=result-note]")).to_have_text("同步完成，送达了 1 处改动")
    page.wait_for_timeout(5000)  # longer than the idle comets' interval
    running = page.evaluate(
        """() => document.getAnimations().filter((a) => a.playState === "running").map((a) => a.id || "css")"""
    )
    assert running == []
    expect(page.locator(".canvas .comets path")).to_have_count(0)


# ----- the phone ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("viewport", [NARROW, PHONE], ids=VIEWPORT_IDS)
def test_the_phone_map_keeps_three_columns(open_page: OpenPage, viewport: dict[str, int]) -> None:
    page = open_page(viewport=viewport)
    ready(page)
    columns = page.evaluate("getComputedStyle(document.querySelector('.canvas')).gridTemplateColumns").split()
    assert len(columns) == 3 and columns[1] == "64px", columns
    assert page.evaluate("document.documentElement.scrollWidth") <= viewport["width"]
    hub = page.locator(".canvas .hub").bounding_box()
    assert hub is not None
    for side, edge in (("src", "right"), ("tgt", "left")):
        for box in page.locator(f".canvas .col.{side} .cal").evaluate_all(
            "els => els.map(e => e.getBoundingClientRect().toJSON())"
        ):
            assert box["height"] >= 44, box
            assert box["left"] >= 0 and box["right"] <= viewport["width"], box
            if edge == "right":
                assert box["right"] < hub["x"], (box, hub)
            else:
                assert box["left"] > hub["x"] + hub["width"], (box, hub)
    for box in page.locator(".canvas .acct-h").evaluate_all(
        "els => els.map(e => e.getBoundingClientRect().toJSON())"
    ):
        assert box["height"] >= 44, box
