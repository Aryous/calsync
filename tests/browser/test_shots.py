"""Screenshots of the UI for a person (or the finish review) to look at, from the live daemon's
synthetic data. Skipped unless ``CALSYNC_SHOTS`` names a directory:

    CALSYNC_SHOTS=.superpowers/sdd/shots/task-1 uv run pytest -m browser tests/browser/test_shots.py

Files: ``<browser>-<phone|desktop>-<light|dark>[-<state>].png``; ``-attention`` is a daemon of its
own with a refused credential (work-mine) and blocked deletes (school-home).
"""

import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page

from tests.browser.conftest import DESKTOP, PHONE, OpenPage, watch_of
from tests.browser.live import SCHOOL, LiveDaemon

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


# ``focus``: a line's inspector open, the map dimmed around that line. ``rule``, ``calendar``,
# ``newline`` (with its preview) and ``delete``: the inspectors of Task 3. Each state is a path
# and what to do there (read-only: the preview writes nothing).
STATES: dict[str, tuple[str, Callable[[Page], None] | None]] = {
    "": ("/", None),
    "settings": ("/#/settings", None),
    "focus": ("/#/rule/school-home", None),
    "rule": ("/#/rule/family-mine", None),
    "calendar": ("/#/calendar/family-icloud/%2Fdav%2Fhome%2F%E5%AE%B6%E5%BA%AD%2F", None),
    "newline": (f"/#/new?source=personal&calendar={SCHOOL.replace('@', '%40')}", _preview),
    "delete": ("/#/rule/school-home", _delete),
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
    if state:
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
