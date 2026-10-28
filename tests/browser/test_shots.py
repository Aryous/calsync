"""Screenshots of the UI for a person (or the finish review) to look at, from the live daemon's
synthetic data. Skipped unless ``CALSYNC_SHOTS`` names a directory:

    CALSYNC_SHOTS=.superpowers/sdd/shots/task-1 uv run pytest -m browser tests/browser/test_shots.py

Files: ``<browser>-<phone|desktop>-<light|dark>[-<state>].png``.
"""

import os
from pathlib import Path

import pytest

from tests.browser.conftest import DESKTOP, PHONE, OpenPage, watch_of

pytestmark = pytest.mark.browser

SHOTS = os.environ.get("CALSYNC_SHOTS")
SIZES = {"phone": PHONE, "desktop": DESKTOP}
STATES = {"": "/", "settings": "/#/settings"}


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
    page.evaluate("document.fonts.ready")
    page.wait_for_function("() => document.getAnimations().every((a) => a.playState !== 'running')")
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
    page.evaluate("document.fonts.ready")
    page.screenshot(path=str(Path(SHOTS) / f"{browser_name}-phone-{theme}-pair.png"), caret="initial")
