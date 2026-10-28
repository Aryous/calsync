"""The settings inspector against a live daemon, in Chromium and WebKit: every field written to
config.toml through ``config.set`` (read back from ``GET /api/config`` and the file) and kept
after a reload; a push URL never shown in full once saved; 发送测试 with each channel's outcome
(the pushes go to an in-process stub, never the network); the config's history naming this page
as the source; 撤销上一次修改 after a dry run that shows the diff.

Every test that writes the config has a daemon of its own (``fresh_live``)."""

import json
import re
import tomllib
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from calsync import notify
from tests.browser.conftest import NARROW, OpenPage, until, watch_of
from tests.browser.live import LiveDaemon

pytestmark = pytest.mark.browser

OPEN = re.compile(r"\bopen\b")
REFUSED = r"status of 422"
TOO_SOON = r"status of 429"
SECRET = "Zx9SecretDeviceKey"
BARK = f"https://bark.example/{SECRET}"  # .example: never resolves, whatever happens


def inspector(page: Page) -> Locator:
    return page.locator("aside.insp")


def section(page: Page, key: str) -> Locator:
    return inspector(page).locator(f'[data-key="{key}"]')


def open_settings(open_page: OpenPage, daemon: LiveDaemon, **kwargs: Any) -> Page:
    page = open_page(daemon=daemon, path="/#/settings", **kwargs)
    expect(inspector(page)).to_have_class(OPEN)
    expect(section(page, "notify")).to_be_visible()
    return page


def file_config(daemon: LiveDaemon) -> dict[str, Any]:
    return tomllib.loads(daemon.config_path.read_text(encoding="utf-8"))


def commit(field: Locator, value: str) -> None:
    field.fill(value)
    field.press("Enter")


class Pushes:
    """The push endpoint: records what was posted and answers as Bark does."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.posted.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"code": 200, "message": "success"})


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch) -> Pushes:
    stub = Pushes()
    monkeypatch.setattr(notify, "TRANSPORT", httpx.MockTransport(stub.handler))
    return stub


# ----- fields ------------------------------------------------------------------------------------


def test_each_setting_is_written_and_survives_a_reload(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_settings(open_page, fresh_live)

    commit(section(page, "notify.failure_threshold").get_by_role("textbox"), "5")
    until(lambda: fresh_live.get("/api/config")["notify"]["failure_threshold"] == 5)
    expect(inspector(page).locator(".saved.on")).to_be_visible()

    section(page, "notify.language").get_by_role("button", name="English", exact=True).click()
    until(lambda: fresh_live.get("/api/config")["notify"]["language"] == "en")

    commit(section(page, "runtime.interval").get_by_role("textbox"), "120")
    until(lambda: file_config(fresh_live)["runtime"]["interval"] == "120s")

    hosts = section(page, "web").locator('[data-role="hosts"]')
    hosts.get_by_role("button", name="允许的地址").click()
    commit(hosts.get_by_role("textbox"), "mac-mini.tail.ts.net")
    until(lambda: fresh_live.get("/api/config")["web"]["allowed_hosts"] == ["mac-mini.tail.ts.net"])
    expect(hosts.locator(".pill", has_text="mac-mini.tail.ts.net")).to_be_visible()

    file = file_config(fresh_live)
    assert file["notify"]["failure_threshold"] == 5
    assert file["notify"]["language"] == "en"
    assert file["web"]["allowed_hosts"] == ["mac-mini.tail.ts.net"]

    page.reload()
    expect(section(page, "notify.failure_threshold").get_by_role("textbox")).to_have_value("5")
    expect(section(page, "runtime.interval").get_by_role("textbox")).to_have_value("120")
    expect(
        section(page, "notify.language").get_by_role("button", name="English", exact=True)
    ).to_have_attribute("aria-pressed", "true")
    hosts = section(page, "web").locator('[data-role="hosts"]')
    hosts.get_by_role("button", name="移除 mac-mini.tail.ts.net").click()
    until(lambda: fresh_live.get("/api/config")["web"]["allowed_hosts"] == [])
    expect(hosts.locator(".pill", has_text="mac-mini.tail.ts.net")).to_have_count(0)


def test_a_refused_value_is_said_beside_its_field_and_reverted(
    open_page: OpenPage, fresh_live: LiveDaemon
) -> None:
    page = open_settings(open_page, fresh_live)
    watch_of(page).allow(REFUSED)
    before = fresh_live.config_path.read_bytes()
    field = section(page, "notify.failure_threshold").get_by_role("textbox")
    commit(field, "0")
    expect(section(page, "notify").get_by_role("alert")).to_contain_text("要填 1 或更大的整数")
    expect(field).to_have_value("3")
    assert fresh_live.config_path.read_bytes() == before


def test_a_push_url_is_never_shown_in_full(
    open_page: OpenPage, fresh_live: LiveDaemon, pushes: Pushes
) -> None:
    page = open_settings(open_page, fresh_live)
    bark = section(page, "notify.bark_url")
    bark.get_by_role("textbox").fill(BARK)
    bark.get_by_role("button", name="保存").click()
    expect(bark.locator('[data-role="masked"]')).to_contain_text("https://bark.example")
    until(lambda: file_config(fresh_live)["notify"]["bark_url"] == BARK)
    assert SECRET not in page.content()
    typed = page.evaluate("() => [...document.querySelectorAll('input')].map((i) => i.value).join(' ')")
    assert SECRET not in typed
    assert fresh_live.get("/api/config")["notify"]["bark_url"] == "https://bark.example"
    page.reload()
    expect(section(page, "notify.bark_url").locator('[data-role="masked"]')).to_be_visible()
    assert SECRET not in page.content()

    section(page, "notify.bark_url").get_by_role("button", name="移除").click()
    until(lambda: "bark_url" not in file_config(fresh_live).get("notify", {}))
    expect(section(page, "notify.bark_url").get_by_role("textbox")).to_be_visible()


def test_a_test_push_says_how_each_channel_did(
    open_page: OpenPage, fresh_live: LiveDaemon, pushes: Pushes
) -> None:
    page = open_settings(open_page, fresh_live)
    send = section(page, "notify").get_by_role("button", name="发送测试")
    expect(send).to_have_attribute("aria-disabled", "true")  # nothing to send to yet
    bark = section(page, "notify.bark_url")
    bark.get_by_role("textbox").fill(BARK)
    bark.get_by_role("button", name="保存").click()
    expect(bark.locator('[data-role="masked"]')).to_be_visible()

    send.click()
    result = section(page, "notify").locator('[data-role="test-result"]')
    expect(result).to_contain_text("Bark：已送达")
    expect(result).to_contain_text("Webhook：没有设置")
    assert len(pushes.posted) == 1
    assert pushes.posted[0][0] == BARK

    watch_of(page).allow(TOO_SOON)
    send.click()
    expect(result).to_contain_text("刚发过一次测试")
    assert len(pushes.posted) == 1


# ----- the history and undo ------------------------------------------------------------------------


def test_the_history_names_this_page_and_undo_restores(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_settings(open_page, fresh_live)
    history = section(page, "config")
    commit(section(page, "notify.failure_threshold").get_by_role("textbox"), "7")
    until(lambda: fresh_live.get("/api/config")["notify"]["failure_threshold"] == 7)
    first = history.locator('[data-role="history"] .row').first
    expect(first).to_contain_text("界面")
    expect(first).to_contain_text("改了提醒前的连续失败次数")

    history.get_by_role("button", name="撤销上一次修改").click()
    ask = history.locator('[data-role="undo-ask"]')
    expect(ask).to_be_visible()
    expect(ask.locator('[data-role="diff"]')).to_contain_text("failure_threshold = 7")
    assert fresh_live.get("/api/config")["notify"]["failure_threshold"] == 7  # the dry run wrote nothing

    ask.get_by_role("button", name="撤销", exact=True).click()
    until(lambda: fresh_live.get("/api/config")["notify"]["failure_threshold"] == 3)
    expect(history.locator('[data-role="undo-note"]')).to_have_text("已撤销")
    expect(history.locator('[data-role="history"] .row').first).to_contain_text(
        "撤销了：改了提醒前的连续失败次数"
    )
    expect(section(page, "notify.failure_threshold").get_by_role("textbox")).to_have_value("3")
    expect(history.get_by_role("button", name="撤销上一次修改")).to_be_focused()


def test_undo_can_be_called_off(open_page: OpenPage, fresh_live: LiveDaemon) -> None:
    page = open_settings(open_page, fresh_live)
    history = section(page, "config")
    section(page, "notify.language").get_by_role("button", name="English", exact=True).click()
    until(lambda: fresh_live.get("/api/config")["notify"]["language"] == "en")
    history.get_by_role("button", name="撤销上一次修改").click()
    ask = history.locator('[data-role="undo-ask"]')
    expect(ask.get_by_role("button", name="取消")).to_be_focused()  # the safe answer
    page.keyboard.press("Escape")
    expect(ask).to_have_count(0)
    expect(inspector(page)).to_have_class(OPEN)  # Esc closed the question, not the panel
    assert fresh_live.get("/api/config")["notify"]["language"] == "en"


def test_the_settings_fit_a_small_phone(open_page: OpenPage, live: LiveDaemon) -> None:
    page = open_settings(open_page, live, viewport=NARROW, has_touch=True)
    width = page.evaluate("() => document.documentElement.scrollWidth")
    assert width <= NARROW["width"]
    sheet = inspector(page).locator(".ib")
    assert page.evaluate("(el) => el.scrollWidth <= el.clientWidth", sheet.element_handle())
