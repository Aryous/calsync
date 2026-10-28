"""Screenshots of the UI for a person (or the finish review) to look at, from the live daemon's
synthetic data. Skipped unless ``CALSYNC_SHOTS`` names a directory:

    CALSYNC_SHOTS=.superpowers/sdd/shots/task-1 uv run pytest -m browser tests/browser/test_shots.py

Files: ``<browser>-<phone|desktop>-<light|dark>[-<state>].png``.
"""

import os
from pathlib import Path

import pytest
from playwright.sync_api import Page

from tests.browser.conftest import DESKTOP, PHONE, OpenPage, watch_of

pytestmark = pytest.mark.browser

SHOTS = os.environ.get("CALSYNC_SHOTS")
SIZES = {"phone": PHONE, "desktop": DESKTOP}
STATES = {"": "/", "settings": "/#/settings"}


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
    page = open_page(viewport=SIZES[size], color_scheme=theme, reduced_motion="reduce", path=STATES[state])
    page.locator(".hub-cap small").wait_for(state="attached")
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
