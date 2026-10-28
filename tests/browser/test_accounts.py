"""Accounts, against a live daemon, in Chromium and WebKit: the account inspector (its state, the
reasons when its authorisation failed, its calendars with 刷新列表, the reconnect command,
断开这个账号 — refused while lines use it, and the credential left to the terminal) and the
connect wizard (a name, the Google client secret or the iCloud server and Apple ID, then
``account.add``, the ``calsync auth`` command while the page waits for the calendars, and the
way into a new line).

The person's part is simulated: ``LiveDaemon.authorise`` stores the account's secret in the
test's fake keychain (``keychain``) and gives its fake service calendars, as ``calsync auth`` and
the real account would; the page's next ``calendars.refresh`` then lists them. Every test that
writes the config has a daemon of its own (``fresh_live``)."""

import json
import re
import tomllib
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, Request, expect

from tests.browser.conftest import NARROW, PHONE, OpenPage, refresh_status, until, watch_of
from tests.browser.live import (
    CONFIG_WITHOUT_GOOGLE,
    FAMILY,
    HOLIDAYS,
    SCHOOL,
    FakeKeychain,
    LiveDaemon,
    live_daemon,
)
from tests.fakes.target import FakeTarget

pytestmark = pytest.mark.browser

OPEN = re.compile(r"\bopen\b")
REFUSED = r"status of 422"  # the browser's console line for a refusal the test provokes
NEEDS_HUMAN = r"status of 403"  # the browser's console line for an op that needs a person
GOOGLE_REASONS = [
    "Google 账号的密码改了",
    "在 Google 账号里撤销了 calsync 的访问权限",
    "Google 应用还在“测试”状态：授权每 7 天就要重新做一次",
]


# ----- helpers -----------------------------------------------------------------------------------


def inspector(page: Page) -> Locator:
    return page.locator("aside.insp")


def step(page: Page, name: str) -> Locator:
    return inspector(page).locator(f'.step[data-step="{name}"]')


def current(page: Page, name: str) -> Locator:
    """The wizard's step ``name``, once it is the current one."""
    expect(step(page, name)).to_have_attribute("aria-current", "step")
    return step(page, name)


def ops_sent(page: Page) -> list[dict[str, Any]]:
    """Every op the page posts from now on: [{name, args, dry_run}]."""
    sent: list[dict[str, Any]] = []

    def record(request: Request) -> None:
        if request.method == "POST" and "/api/ops/" in request.url:
            body = json.loads(request.post_data or "{}")
            sent.append({"name": request.url.rsplit("/", 1)[1], **body})

    page.on("request", record)
    return sent


def file_accounts(daemon: LiveDaemon) -> dict[str, Any]:
    return tomllib.loads(daemon.config_path.read_text(encoding="utf-8")).get("accounts", {})


def no_secret_inputs(page: Page) -> None:
    """D10: nothing in the page asks for a password, a token or an app-specific password."""
    assert page.locator("input[type=password]").count() == 0
    assert (
        page.locator('input[autocomplete="current-password"], input[autocomplete="new-password"]').count()
        == 0
    )


# ----- the account inspector -------------------------------------------------------------------


def test_the_account_inspector_shows_its_state_calendars_and_reconnect(open_page: OpenPage) -> None:
    page = open_page(path="/#/account/personal")
    insp = inspector(page)
    expect(insp).to_have_class(OPEN)
    expect(insp.locator("h2")).to_have_text("账号")
    expect(insp.locator(".ih p")).to_have_text("personal · Google 日历账号")
    expect(insp.locator(".ih p").get_by_role("img", name="Google 日历")).to_have_count(1)
    expect(insp.locator("[data-role=state]")).to_have_text("连接正常")
    # Its calendars, from the cache, named as always, with their lines.
    rows = insp.locator('.fs[data-key="calendars"] .row')
    expect(rows.locator(".cm")).to_have_text(
        ["家庭日程 · personal", "孩子学校 · personal", "中国节假日 · personal"]
    )
    expect(rows.locator(".r")).to_have_text(["2 条线路", "1 条线路", "未同步"])
    expect(rows.get_by_role("img", name="Google 日历")).to_have_count(3)
    expect(rows.nth(0)).to_have_attribute("href", f"#/calendar/personal/{FAMILY.replace('@', '%40')}")
    hol = HOLIDAYS.replace("#", "%23").replace("@", "%40")
    expect(rows.nth(2)).to_have_attribute("href", f"#/calendar/personal/{hol}")
    expect(insp.get_by_role("button", name="刷新列表")).to_be_visible()
    # Reconnecting is a terminal command, with a copy button; no password field anywhere.
    connect = insp.locator('.fs[data-key="connect"]')
    expect(connect.locator(".lb")).to_have_text("连接")
    expect(connect.locator(".cmd code")).to_have_text("calsync auth personal")
    expect(connect.get_by_role("button", name="复制")).to_be_visible()
    expect(connect).to_contain_text("完成后这里会自动更新，不用刷新。")
    expect(insp.locator(".ifoot").get_by_role("button", name="断开这个账号")).to_be_visible()
    no_secret_inputs(page)
    # A calendar row opens its inspector.
    rows.nth(1).click()
    expect(page).to_have_url(re.compile(rf"#/calendar/personal/{SCHOOL.replace('@', '%40')}$"))
    expect(insp.locator("h2")).to_have_text("日历")


def test_an_icloud_account_names_its_service(open_page: OpenPage) -> None:
    page = open_page(path="/#/account/my-icloud")
    insp = inspector(page)
    expect(insp.locator(".ih p")).to_have_text("my-icloud · iCloud 账号")
    expect(insp.locator(".ih p").get_by_role("img", name="iCloud 日历")).to_have_count(1)
    rows = insp.locator('.fs[data-key="calendars"] .row')
    expect(rows.locator(".cm")).to_have_text(["我的日历 · my-icloud"])
    expect(rows.locator(".r")).to_have_text(["2 条线路"])
    expect(insp.locator('.fs[data-key="connect"] .cmd code')).to_have_text("calsync auth my-icloud")


def test_an_account_no_line_uses_says_so(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    fresh_live.op(
        "account.add",
        {"name": "spare", "provider": "caldav", "server_url": "icloud", "username": "spare@example.com"},
    )
    page = open_page(daemon=fresh_live, path="/#/account/spare")
    insp = inspector(page)
    expect(insp.locator("[data-role=state]")).to_have_text("还没有线路用到这个账号")
    expect(insp.locator('.fs[data-key="calendars"]')).to_contain_text("还没有读到这个账号的日历")


def test_an_account_gone_from_the_config_says_so(open_page: OpenPage) -> None:
    page = open_page(path="/#/account/nobody")
    expect(inspector(page).locator(".note")).to_have_text(
        "calsync 不知道这个账号，它可能已经在终端或 config.toml 里删掉了。"
    )


def test_an_expired_authorisation_lists_the_reasons_and_the_command(
    open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    fresh_live.stage_auth()  # work's Google credential is refused; work-mine ran on it
    page = open_page(daemon=fresh_live, path="/#/account/work")
    insp = inspector(page)
    note = insp.locator(".note.bad[data-role=auth]")
    expect(note.locator("b")).to_have_text("授权失效了。")
    expect(note.locator(".why li")).to_have_text(GOOGLE_REASONS)
    expect(note.locator(".cm")).to_have_text(["工作 · work", "我的日历 · my-icloud"])
    connect = insp.locator('.fs[data-key="connect"]')
    expect(connect.locator(".lb")).to_have_text("重新连接")
    expect(connect.locator(".cmd code")).to_have_text("calsync auth work")
    # 刷新列表 on the refused account: the op says it needs a person, with its command.
    sent = ops_sent(page)
    insp.get_by_role("button", name="刷新列表").click()
    result = insp.locator("[data-role=refreshed]")
    expect(result).to_contain_text("没能读到日历列表：work 的授权失效了")
    expect(result.locator(".cmd code")).to_have_text("calsync auth work")
    assert [s["name"] for s in sent] == ["calendars.refresh"]
    assert sent[0]["args"] == {"account": "work"}


def test_refreshing_reads_the_calendar_list_again(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_page(daemon=fresh_live, path="/#/account/personal")
    insp = inspector(page)
    rows = insp.locator('.fs[data-key="calendars"] .row')
    expect(rows).to_have_count(3)
    fresh_live.in_loop(
        lambda: fresh_live.world.google["personal"].add_calendar("trip@group.calendar.google.com", "旅行")
    )
    sent = ops_sent(page)
    insp.get_by_role("button", name="刷新列表").click()
    expect(insp.locator("[data-role=refreshed]")).to_have_text("已刷新 · 4 个日历")
    expect(rows).to_have_count(4)
    trip = rows.filter(has_text="旅行")
    expect(trip.locator(".cm")).to_have_text("旅行 · personal")
    expect(trip.locator(".r")).to_have_text("未同步")
    assert sent == [{"name": "calendars.refresh", "args": {"account": "personal"}, "dry_run": False}]


# ----- removing an account ---------------------------------------------------------------------


def test_removing_an_account_in_use_is_refused_with_its_lines(open_page: OpenPage, live: LiveDaemon) -> None:
    before = live.config_path.read_text(encoding="utf-8")
    page = open_page(path="/#/account/personal")
    watch_of(page).allow(REFUSED)
    insp = inspector(page)
    sent = ops_sent(page)
    insp.locator(".ifoot").get_by_role("button", name="断开这个账号").click()
    panel = insp.locator("[data-role=remove]")
    refused = panel.locator("[data-role=remove-refused]")
    expect(refused).to_contain_text("还有 3 条线路在用这个账号，先删除这些线路，才能断开它：")
    lines = refused.locator("a.row")
    expect(lines).to_have_count(3)
    expect(lines.nth(0)).to_have_attribute("href", "#/rule/family-home")
    expect(lines.nth(0).locator(".cm")).to_have_text(["家庭日程 · personal", "家庭 · family-icloud"])
    expect(lines.nth(1)).to_have_attribute("href", "#/rule/school-home")
    expect(lines.nth(2)).to_have_attribute("href", "#/rule/family-mine")
    expect(panel.get_by_role("button", name="断开", exact=True)).to_have_count(0)
    # The refusal is the op's: a dry run, refused; nothing written.
    assert sent == [{"name": "account.remove", "args": {"name": "personal"}, "dry_run": True}]
    assert live.config_path.read_text(encoding="utf-8") == before
    # A line opens from the list; 取消 closes the panel, not the inspector.
    panel.get_by_role("button", name="取消").click()
    expect(panel).to_have_count(0)
    expect(insp).to_have_class(OPEN)
    expect(insp.locator(".ifoot").get_by_role("button", name="断开这个账号")).to_be_focused()


def test_removing_an_unused_account_keeps_its_credential(
    keychain: FakeKeychain, open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    keychain.store[("calsync:spare", "calsync")] = "app-specific"
    fresh_live.op(
        "account.add",
        {"name": "spare", "provider": "caldav", "server_url": "icloud", "username": "spare@example.com"},
    )
    page = open_page(daemon=fresh_live, path="/#/account/spare")
    insp = inspector(page)
    expect(page.locator('.canvas .acct[data-acct="spare"]')).to_have_count(1)
    sent = ops_sent(page)
    insp.locator(".ifoot").get_by_role("button", name="断开这个账号").click()
    panel = insp.locator("[data-role=remove]")
    expect(panel).to_contain_text("config.toml 里的这个账号会被删除")
    expect(panel).to_contain_text("保存在钥匙串里的凭证会留着。要连凭证一起删除，请在这台电脑的终端里运行：")
    # Deleting the credential is only ever a command for the terminal.
    expect(panel.locator(".cmd code")).to_have_text("calsync account remove spare --delete-credential")
    expect(panel.get_by_role("checkbox")).to_have_count(0)
    panel.get_by_role("button", name="断开", exact=True).click()
    expect(insp).not_to_have_class(OPEN)
    until(lambda: "spare" not in file_accounts(fresh_live))
    until(lambda: "spare" not in fresh_live.get("/api/config")["accounts"])
    expect(page.locator('.canvas .acct[data-acct="spare"]')).to_have_count(0)
    assert [s["name"] for s in sent] == ["account.remove", "account.remove"]
    assert [s["dry_run"] for s in sent] == [True, False]
    assert all(s["args"] == {"name": "spare"} for s in sent)  # never delete_credential
    assert keychain.store == {("calsync:spare", "calsync"): "app-specific"}


# ----- the connect wizard ----------------------------------------------------------------------


def test_the_wizard_checks_the_name_and_the_apple_id(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_page()
    page.locator(".canvas").get_by_role("link", name="连接 iCloud 账号").click()
    insp = inspector(page)
    expect(insp.locator("h2")).to_have_text("连接 iCloud 账号")
    expect(insp.locator(".ih p")).to_have_text("接入向导 · 第 1 步，共 4 步")
    first = current(page, "name")
    name = first.get_by_label("账号名字")
    expect(name).to_have_value("icloud")  # a free name to start from
    error = first.locator(".errt")
    for typed, message in [
        ("", "给账号起个名字"),
        ("my icloud", "只能用字母、数字、- 和 _，以字母或数字开头，最多 32 个字符"),
        ("home:icloud", "只能用字母、数字、- 和 _，以字母或数字开头，最多 32 个字符"),
        ("my-icloud", "已经有一个叫 my-icloud 的账号了"),
    ]:
        name.fill(typed)
        first.get_by_role("button", name="下一步").click()
        expect(error).to_have_text(message)
        expect(name).to_have_attribute("aria-invalid", "true")
        expect(step(page, "name")).to_have_attribute("aria-current", "step")
    name.fill("grandma")
    name.press("Enter")
    second = current(page, "details")
    expect(insp.locator(".ih p")).to_have_text("接入向导 · 第 2 步，共 4 步")
    expect(step(page, "name")).to_contain_text("grandma")
    expect(second.get_by_role("button", name="iCloud", exact=True)).to_have_attribute("aria-pressed", "true")
    apple = second.get_by_label("Apple ID")
    expect(apple).to_be_focused()  # the step it moved to takes focus
    expect(apple).to_have_attribute("type", "email")
    for typed, message in [("", "填写这个账号的 Apple ID"), ("grandma", "Apple ID 是一个邮箱地址")]:
        apple.fill(typed)
        second.get_by_role("button", name="添加账号").click()
        expect(second.locator(".errt")).to_have_text(message)
    # Another CalDAV server needs its address.
    second.get_by_role("button", name="其他 CalDAV 服务器").click()
    url = second.get_by_label("服务器地址")
    second.get_by_label("用户名").fill("grandma@example.com")
    url.fill("dav.example.com")
    second.get_by_role("button", name="添加账号").click()
    expect(second.locator(".errt")).to_have_text("服务器地址要以 https:// 开头")
    # 上一步 keeps what was typed.
    second.get_by_role("button", name="上一步").click()
    expect(current(page, "name").get_by_label("账号名字")).to_have_value("grandma")
    expect(current(page, "name").get_by_label("账号名字")).to_be_focused()
    no_secret_inputs(page)
    assert "grandma" not in file_accounts(live)


def test_the_icloud_wizard_adds_the_account_and_waits_for_its_calendars(
    keychain: FakeKeychain, open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    page = open_page(daemon=fresh_live)
    console: list[str] = []
    page.on("console", lambda m: console.append(m.text))
    sent = ops_sent(page)
    page.locator(".canvas").get_by_role("link", name="连接 iCloud 账号").click()
    first = current(page, "name")
    first.get_by_label("账号名字").fill("grandma")
    first.get_by_role("button", name="下一步").click()
    second = current(page, "details")
    second.get_by_label("Apple ID").fill("grandma@example.com")
    second.get_by_role("button", name="添加账号").click()
    # account.add wrote the account (no secret in it: a keychain reference).
    third = current(page, "auth")
    expect(inspector(page).locator(".ih p")).to_have_text("接入向导 · 第 3 步，共 4 步")
    until(lambda: "grandma" in file_accounts(fresh_live))
    assert file_accounts(fresh_live)["grandma"] == {
        "provider": "caldav",
        "server_url": "icloud",
        "username": "grandma@example.com",
        "credential": {"backend": "keychain", "ref": "calsync:grandma"},
    }
    assert fresh_live.get("/api/config")["accounts"]["grandma"]["provider"] == "caldav"
    # The person's step: the command from the op, with a copy button, and a page that waits.
    expect(third.locator(".cmd code")).to_have_text("calsync auth grandma")
    expect(third.get_by_role("button", name="复制")).to_be_visible()
    expect(third).to_contain_text("App 专用密码")
    expect(third.locator("[data-role=waiting]")).to_contain_text("正在等你在终端里完成")
    expect(page.locator('.canvas .acct[data-acct="grandma"]')).to_have_count(1)
    until(lambda: [s["name"] for s in sent].count("calendars.refresh") >= 1)
    assert sent[0] == {
        "name": "account.add",
        "args": {
            "name": "grandma",
            "provider": "caldav",
            "server_url": "icloud",
            "username": "grandma@example.com",
        },
        "dry_run": False,
    }
    assert {"name": "calendars.refresh", "args": {"account": "grandma"}, "dry_run": False} in sent
    # The person runs `calsync auth grandma`; back in the browser, the page looks at once.
    fresh_live.authorise("grandma", ["外婆的日历"])
    refresh_status(page)
    fourth = current(page, "calendars")
    expect(fourth.locator(".cm")).to_have_text(["外婆的日历 · grandma"])
    expect(fourth).to_contain_text("找到了 1 个日历")
    expect(step(page, "auth")).to_have_class(re.compile(r"\bdone\b"))
    fourth.get_by_role("button", name="选择要同步的日历").click()
    # The new line, with the account preselected: Task 3's flow from here, unchanged.
    expect(page).to_have_url(re.compile(r"#/new\?target=grandma$"))
    insp = inspector(page)
    expect(insp.locator("h2")).to_have_text("新线路")
    target = insp.locator("[data-role=targets]").get_by_role("radio", name=re.compile("^外婆的日历"))
    expect(target).to_have_attribute("aria-checked", "true")
    insp.locator("[data-role=sources]").get_by_role("radio", name=re.compile("^家庭日程")).click()
    expect(insp.locator(".preview")).to_contain_text("将在")
    insp.get_by_role("button", name="创建并开始同步").click()
    expect(page).to_have_url(re.compile(r"#/rule/personal-to-grandma$"), timeout=15_000)
    expect(page.locator('.canvas .hit:not(.in)[data-rule="personal-to-grandma"]')).to_have_count(1)
    no_secret_inputs(page)
    assert not any("grandma@example.com" in line for line in console)


def test_the_google_wizard_sets_up_the_client_secret_first(
    keychain: FakeKeychain, session_seal: None, open_page: OpenPage
) -> None:
    with live_daemon(config=CONFIG_WITHOUT_GOOGLE, stage=False) as daemon:
        page = open_page(daemon=daemon, path="/#/connect/google")
        insp = inspector(page)
        expect(insp.locator("h2")).to_have_text("连接 Google 账号")
        first = current(page, "name")
        expect(first.get_by_label("账号名字")).to_have_value("google")
        first.get_by_role("button", name="下一步").click()
        second = current(page, "details")
        # The one-time setup: a desktop OAuth client of the person's own, then one command.
        expect(second).to_contain_text("桌面应用")
        expect(second).to_contain_text("Google Calendar API")
        expect(second.locator(".cmd code")).to_have_text(
            "calsync config set google.client_secret_file <path>"
        )
        expect(second.locator("[data-role=waiting]")).to_contain_text("设置好后这里会自动继续")
        add = second.get_by_role("button", name="添加账号")
        expect(add).to_be_disabled()
        # The person runs it in the terminal; the page sees it in the config.
        daemon.op("config.set", {"key": "google.client_secret_file", "value": "client.json"})
        refresh_status(page)
        expect(second).to_contain_text("Google 应用凭证已经设置好了")
        expect(add).to_be_enabled()
        add.click()
        third = current(page, "auth")
        until(lambda: "google" in file_accounts(daemon))
        assert file_accounts(daemon)["google"] == {
            "provider": "google",
            "credential": {"backend": "keychain", "ref": "calsync:google"},
        }
        expect(third.locator(".cmd code")).to_have_text("calsync auth google")
        expect(third).to_contain_text("浏览器")
        daemon.authorise("google", ["家庭安排"])
        refresh_status(page)
        fourth = current(page, "calendars")
        expect(fourth.locator(".cm")).to_have_text(["家庭安排 · google"])
        fourth.get_by_role("button", name="选择要同步的日历").click()
        expect(page).to_have_url(re.compile(r"#/new\?source=google$"))
        source = insp.locator("[data-role=sources]").get_by_role("radio", name=re.compile("^家庭安排"))
        expect(source).to_have_attribute("aria-checked", "true")
        no_secret_inputs(page)


def test_a_configured_client_secret_needs_no_setup(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_page(path="/#/connect/google")
    first = current(page, "name")
    expect(first.get_by_label("账号名字")).to_have_value("google")
    first.get_by_role("button", name="下一步").click()
    second = current(page, "details")
    expect(second).to_contain_text("Google 应用凭证已经设置好了")
    expect(second.locator(".cmd")).to_have_count(0)
    expect(second.get_by_role("button", name="添加账号")).to_be_enabled()


def test_an_account_the_keychain_knows_is_added_from_the_terminal(
    keychain: FakeKeychain, open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    keychain.store[("calsync:old", "calsync")] = "left behind by a removed account"
    page = open_page(daemon=fresh_live, path="/#/connect/icloud")
    watch_of(page).allow(NEEDS_HUMAN)
    first = current(page, "name")
    first.get_by_label("账号名字").fill("old")
    first.get_by_role("button", name="下一步").click()
    second = current(page, "details")
    second.get_by_label("Apple ID").fill("old@example.com")
    second.get_by_role("button", name="添加账号").click()
    # Not added: the browser may not point a stored password at a server of its choosing.
    note = second.locator("[data-role=terminal-add]")
    expect(note).to_contain_text("钥匙串里已经有 old 的凭证。为了安全，这个账号要在终端里添加：")
    expect(note.locator(".cmd code")).to_have_text(
        "calsync account add old --provider caldav --server-url icloud --username old@example.com"
    )
    assert "old" not in file_accounts(fresh_live)
    # Added from the terminal, where the credential it finds already works: the page sees the
    # account, lists its calendars at once and goes on to them.
    fresh_live.in_loop(
        lambda: fresh_live.world.targets.setdefault("old", FakeTarget()).add_calendar("旧日历")
    )
    fresh_live.op(
        "account.add",
        {"name": "old", "provider": "caldav", "server_url": "icloud", "username": "old@example.com"},
    )
    refresh_status(page)
    fourth = current(page, "calendars")
    expect(fourth.locator(".cm")).to_have_text(["旧日历 · old"])


def test_the_wizard_polls_every_ten_seconds_and_stops_when_closed(
    keychain: FakeKeychain, open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    page = open_page(daemon=fresh_live, path="/#/connect/icloud")
    refreshes: list[str] = []
    configs: list[str] = []

    def record(request: Request) -> None:
        if request.url.endswith("/api/ops/calendars.refresh"):
            refreshes.append(request.url)
        elif "/api/config" in request.url:
            configs.append(request.url)

    page.on("request", record)
    first = current(page, "name")
    first.get_by_label("账号名字").fill("slow")
    first.get_by_role("button", name="下一步").click()
    second = current(page, "details")
    second.get_by_label("Apple ID").fill("slow@example.com")
    second.get_by_role("button", name="添加账号").click()
    current(page, "auth")
    until(lambda: len(refreshes) == 1)
    page.wait_for_timeout(10_800)
    assert len(refreshes) == 2  # every 10 s while the step is open
    page.keyboard.press("Escape")
    expect(inspector(page)).not_to_have_class(OPEN)
    after_refreshes, after_configs = len(refreshes), len(configs)
    refresh_status(page)  # coming back to the page looks again only while the step is open
    page.wait_for_timeout(10_800)
    assert len(refreshes) == after_refreshes
    assert len(configs) == after_configs


@pytest.mark.parametrize("viewport", [PHONE, NARROW], ids=lambda v: f"{v['width']}")
@pytest.mark.parametrize("state", ["account", "remove", "name", "details"])
def test_the_account_inspectors_fit_a_phone(
    open_page: OpenPage, viewport: dict[str, int], state: str
) -> None:
    if state in ("account", "remove"):
        page = open_page(viewport=viewport, path="/#/account/personal")
        expect(inspector(page).locator('.fs[data-key="calendars"] .row')).to_have_count(3)
        if state == "remove":
            watch_of(page).allow(REFUSED)
            inspector(page).locator(".ifoot").get_by_role("button", name="断开这个账号").click()
            expect(inspector(page).locator("[data-role=remove-refused]")).to_be_visible()
    else:
        page = open_page(viewport=viewport, path="/#/connect/icloud")
        current(page, "name")
        if state == "details":
            current(page, "name").get_by_role("button", name="下一步").click()
            current(page, "details").get_by_role("button", name="其他 CalDAV 服务器").click()
            expect(current(page, "details").get_by_label("服务器地址")).to_be_visible()
    sizes = page.evaluate(
        """() => [...document.querySelectorAll('aside.insp .ib button, aside.insp .ib input, aside.insp .ib a.btn, aside.insp .ifoot button')]
          .filter((el) => el.offsetParent !== null)
          .map((el) => ({ h: el.getBoundingClientRect().height, f: getComputedStyle(el).fontSize, tag: el.tagName, t: el.textContent }))"""
    )
    assert sizes
    for item in sizes:
        assert item["h"] >= 44 - 0.01, item
        if item["tag"] == "INPUT":
            assert item["f"] == "16px", item
    overflow = page.evaluate(
        "() => { const b = document.querySelector('aside.insp .ib'); return b.scrollWidth - b.clientWidth; }"
    )
    assert overflow <= 0
    assert page.evaluate("document.documentElement.scrollWidth") <= viewport["width"]
