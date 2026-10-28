"""Screenshots of the UI for a person (or the finish review) to look at, from the live daemon's
synthetic data. Skipped unless ``CALSYNC_SHOTS`` names a directory:

    CALSYNC_SHOTS=.superpowers/sdd/shots/task-1 uv run pytest -m browser tests/browser/test_shots.py

Files: ``<browser>-<phone|desktop>-<light|dark>[-<state>].png``; ``-attention`` is a daemon of its
own with a refused credential (work-me) and blocked deletes (course-shared).
"""

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from calsync import events_view
from tests.browser.conftest import DESKTOP, PHONE, OpenPage, refresh_status, watch_of
from tests.browser.live import FAMILY, SCHOOL, FakeKeychain, LiveDaemon

pytestmark = pytest.mark.browser

SHOTS = os.environ.get("CALSYNC_SHOTS")
SIZES = {"phone": PHONE, "desktop": DESKTOP}


def _preview(page: Page) -> None:
    targets = page.locator("aside.insp [data-role=targets]")
    targets.get_by_role("radio", name=re.compile("^我的日历")).click()
    page.locator("aside.insp .preview").wait_for()


def _delete(page: Page) -> None:
    page.locator("aside.insp .ifoot").get_by_role("button", name="删除这条线路").click()
    page.locator("aside.insp [data-role=delete]").wait_for()


def _remove_refused(page: Page) -> None:
    watch_of(page).allow(r"status of 422")  # the dry run's refusal (lines use the account)
    page.locator("aside.insp .ifoot").get_by_role("button", name="断开这个账号").click()
    page.locator("aside.insp [data-role=remove-refused]").wait_for()


def _wizard_details(page: Page) -> None:
    page.locator('aside.insp .step[data-step="name"]').get_by_role("button", name="下一步").click()
    page.locator('aside.insp .step[data-step="details"][aria-current="step"]').wait_for()


def _icloud_details(page: Page) -> None:
    _wizard_details(page)
    page.locator("aside.insp").get_by_label("Apple ID").fill("grandma@example.com")


def _google_setup(page: Page) -> None:
    """The one-time client setup: the config is served as if no client secret were set."""

    def unset(route: Any) -> None:
        response = route.fetch()
        body = response.json()
        body["google"]["client_secret_file"] = {"configured": False}
        route.fulfill(response=response, json=body)

    page.route("**/api/config", unset)
    page.evaluate("location.hash = '#/connect/google'")
    _wizard_details(page)
    page.locator("aside.insp [data-role=waiting]").wait_for()


DENTIST = events_view.event_id("main", FAMILY, "dentist", None)
RUN = events_view.event_id("main", FAMILY, "run", "2026-10-26T07:00:00+08:00")


def _delete_event(page: Page) -> None:
    page.locator('aside.insp [data-role="delete-event"]').click()
    page.locator("aside.insp [data-role=delete]").wait_for()


def _scope(page: Page) -> None:
    page.locator('aside.insp [data-role="scope"] [data-choice="this"]').click()
    page.locator("aside.insp #ev-title").fill("晨跑（公园）")
    page.locator('aside.insp [data-key="alarms"] .seg button').nth(2).click()


def _move_scope(page: Page) -> None:
    """A repeating event dragged an hour later: the question, before anything is written."""
    run = page.locator('#pane-calendar .dcol[data-day="2026-10-26"] .ev', has_text="晨跑")
    run.scroll_into_view_if_needed()
    box = run.bounding_box()
    assert box is not None
    hour = page.locator("#pane-calendar .dcol").first.evaluate("el => el.getBoundingClientRect().height / 24")
    x, y = box["x"] + box["width"] / 2, box["y"] + 8
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x, y + hour, steps=6)
    page.mouse.up()
    page.locator('[data-role="move-scope"]').wait_for()


# ``focus``: a line's inspector open, the map dimmed around that line. ``rule``, ``calendar``,
# ``newline`` (with its preview) and ``delete``: the inspectors of Task 3. Each state is a path
# and what to do there (read-only: the preview writes nothing).
STATES: dict[str, tuple[str, Callable[[Page], None] | None]] = {
    "": ("/", None),
    "settings": ("/#/settings", None),
    "focus": ("/#/rule/course-shared", None),
    "rule": ("/#/rule/life-me", None),
    "calendar": ("/#/calendar/icloud-shared/%2Fdav%2Fhome%2F%E5%85%B1%E4%BA%AB%E6%97%A5%E5%8E%86%2F", None),
    "newline": (f"/#/new?source=main&calendar={SCHOOL.replace('@', '%40')}", _preview),
    "delete": ("/#/rule/course-shared", _delete),
    # Task 4: the account inspector, 断开 refused, the wizard's first two steps (read-only: the
    # refusal is a dry run, and nothing is added).
    "account": ("/#/account/main", None),
    "remove-refused": ("/#/account/main", _remove_refused),
    "connect-icloud-name": ("/#/connect/icloud", None),
    "connect-icloud-details": ("/#/connect/icloud", _icloud_details),
    "connect-google-details": ("/#/connect/google", _wizard_details),
    "connect-google-setup": ("/", _google_setup),
    # Phase C: the detail's actions, the form (a repeating event, then a new one), a delete
    # (read-only: nothing is saved or deleted).
    "event": (f"/#/event/{DENTIST}", None),
    "event-edit": (f"/#/event-edit/{RUN}", _scope),
    "event-delete": (f"/#/event/{RUN}", _delete_event),
    "event-new": ("/#/event-new?start=2026-10-27T15:00", None),
    "move-scope": ("/", _move_scope),
}


def settle(page: Page) -> None:
    """Fonts loaded, no animation running, and two frames painted since."""
    page.evaluate(
        """async () => {
          await document.fonts.ready;
          await Promise.all(document.getAnimations().map((a) => a.finished.catch(() => null)));
          await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
        }"""
    )


@pytest.mark.skipif(not SHOTS, reason="set CALSYNC_SHOTS to a directory to take screenshots")
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", list(SIZES))
@pytest.mark.parametrize("state", list(STATES), ids=lambda s: s or "home")
def test_screenshot(open_page: OpenPage, browser_name: str, size: str, theme: str, state: str) -> None:
    assert SHOTS
    out = Path(SHOTS)
    out.mkdir(parents=True, exist_ok=True)
    path, act = STATES[state]
    page = open_page(viewport=SIZES[size], color_scheme=theme, reduced_motion="reduce", path=path)
    page.locator(".hub-cap small").wait_for(state="attached")
    if act is not None:
        act(page)
    settle(page)
    if state and state != "move-scope":
        # WebKit paints the SVG <use> icons of a panel that has just become visible a few frames
        # late (the close button's ✕ was missing at 0 ms, present from 300 ms on).
        page.locator("aside.insp .ih button").wait_for(state="visible")
        page.wait_for_timeout(400)
    name = "-".join(part for part in (browser_name, size, theme, state) if part)
    watch = watch_of(page)
    assert watch.problems() == []
    page.screenshot(path=str(out / f"{name}.png"), full_page=state == "", caret="initial")
    # Playwright's WebKit screenshot injects a <style> of its own, which the CSP refuses: what the
    # screenshot caused is not the page's.
    watch.console.clear()
    watch.csp.clear()


@pytest.mark.skipif(not SHOTS, reason="set CALSYNC_SHOTS to a directory to take screenshots")
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_screenshot_of_the_pair_page(open_page: OpenPage, browser_name: str, theme: str) -> None:
    assert SHOTS
    page = open_page(viewport=PHONE, color_scheme=theme, paired=False)
    settle(page)
    page.screenshot(path=str(Path(SHOTS) / f"{browser_name}-phone-{theme}-pair.png"), caret="initial")


@pytest.mark.skipif(not SHOTS, reason="set CALSYNC_SHOTS to a directory to take screenshots")
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", list(SIZES))
def test_screenshot_with_attention(
    open_page: OpenPage, fresh_live: LiveDaemon, browser_name: str, size: str, theme: str
) -> None:
    assert SHOTS
    fresh_live.stage_auth()
    fresh_live.stage_blocked()
    page = open_page(daemon=fresh_live, viewport=SIZES[size], color_scheme=theme, reduced_motion="reduce")
    page.locator(".attn[data-kind=blocked] b a").wait_for(state="attached")  # the count is read
    settle(page)
    page.wait_for_timeout(300)
    watch = watch_of(page)
    assert watch.problems() == []
    page.screenshot(
        path=str(Path(SHOTS) / f"{browser_name}-{size}-{theme}-attention.png"),
        full_page=True,
        caret="initial",
    )
    watch.console.clear()
    watch.csp.clear()


def _shoot(page: Page, name: str) -> None:
    settle(page)
    page.locator("aside.insp .ih button").wait_for(state="visible")
    page.wait_for_timeout(400)
    watch = watch_of(page)
    assert watch.problems() == []
    page.screenshot(path=str(Path(SHOTS or ".") / f"{name}.png"), caret="initial")
    watch.console.clear()
    watch.csp.clear()


@pytest.mark.skipif(not SHOTS, reason="set CALSYNC_SHOTS to a directory to take screenshots")
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", list(SIZES))
def test_screenshot_of_an_expired_account(
    open_page: OpenPage, fresh_live: LiveDaemon, browser_name: str, size: str, theme: str
) -> None:
    fresh_live.stage_auth()
    page = open_page(
        daemon=fresh_live,
        viewport=SIZES[size],
        color_scheme=theme,
        reduced_motion="reduce",
        path="/#/account/work",
    )
    page.locator("aside.insp [data-role=auth]").wait_for()
    _shoot(page, f"{browser_name}-{size}-{theme}-account-auth")


@pytest.mark.skipif(not SHOTS, reason="set CALSYNC_SHOTS to a directory to take screenshots")
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", list(SIZES))
@pytest.mark.parametrize("service", ["icloud", "google"])
def test_screenshot_of_the_wizard_waiting_and_done(
    keychain: FakeKeychain,
    open_page: OpenPage,
    fresh_live: LiveDaemon,
    browser_name: str,
    size: str,
    theme: str,
    service: str,
) -> None:
    """Steps 3 and 4: the account is added (its own daemon), the page waits for ``calsync auth``,
    then the calendars appear."""
    page = open_page(
        daemon=fresh_live,
        viewport=SIZES[size],
        color_scheme=theme,
        reduced_motion="reduce",
        path=f"/#/connect/{service}",
    )
    insp = page.locator("aside.insp")
    name = "grandma" if service == "icloud" else "family-google"
    insp.get_by_label("账号名字").fill(name)
    _wizard_details(page)
    if service == "icloud":
        insp.get_by_label("Apple ID").fill("grandma@example.com")
    insp.get_by_role("button", name="添加账号").click()
    page.locator('aside.insp .step[data-step="auth"][aria-current="step"]').wait_for()
    _shoot(page, f"{browser_name}-{size}-{theme}-connect-{service}-auth")
    fresh_live.authorise(
        name, ["外婆的日历", "外婆的生日"] if service == "icloud" else ["共享日历安排", "孩子活动"]
    )
    refresh_status(page)
    page.locator('aside.insp .step[data-step="calendars"][aria-current="step"] .row').first.wait_for()
    _shoot(page, f"{browser_name}-{size}-{theme}-connect-{service}-calendars")
