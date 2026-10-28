"""Fixtures for the browser tests: a live daemon (``tests.browser.live``) and Playwright pages
paired with it, in Chromium and WebKit.

- ``live``: the session's daemon, seeded as ``live.py`` describes. Read-only tests share it.
- ``fresh_live``: a daemon of the test's own, for tests that run cycles or change the config.
- ``browser``: one per engine, parametrized over ``BROWSERS`` (every browser test runs in each).
- ``viewport``: ``DESKTOP`` unless a test parametrizes it (``@pytest.mark.parametrize("viewport",
  [PHONE, DESKTOP], ids=VIEWPORT_IDS)``).
- ``open_page(...)``: a new page in a new context (``zh-CN``, ``Asia/Shanghai``, the browser's
  clock pinned to the daemon's), paired through the key link unless ``paired=False``. Each page
  has a ``Watch`` (``watch_of(page)``) recording console errors, uncaught errors and CSP
  violations; at teardown a paired page must have none except those a test allowed.
- ``paired_page``: ``open_page()`` on ``live`` at ``viewport``, showing ``/``.

Every test module here sets ``pytestmark = pytest.mark.browser``; they run with
``uv run pytest -m browser`` and need ``uv run playwright install chromium webkit``. Without
``-m browser`` the root conftest deselects everything in this directory.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, ConsoleMessage, Page, Playwright, sync_playwright

from tests.browser.live import LiveDaemon, live_daemon, seal_session


def _browsers_path() -> str:
    """Where ``playwright install`` put the browsers, from the real ``HOME`` (read at import,
    before the sealed environment replaces ``HOME``)."""
    home = Path.home()
    if sys.platform == "darwin":
        return str(home / "Library" / "Caches" / "ms-playwright")
    if sys.platform == "win32":
        return str(Path(os.environ.get("LOCALAPPDATA", home)) / "ms-playwright")
    return str(Path(os.environ.get("XDG_CACHE_HOME", home / ".cache")) / "ms-playwright")


os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _browsers_path())

BROWSERS = ("chromium", "webkit")
PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1440, "height": 900}
NARROW = {"width": 360, "height": 760}


def VIEWPORT_IDS(viewport: dict[str, int]) -> str:  # used like a constant
    return f"{viewport['width']}x{viewport['height']}"


# CSP violations, reported to the test through an exposed binding (it survives navigations).
# Playwright injects init scripts itself, outside the page's CSP.
CSP_LISTENER = """
document.addEventListener("securitypolicyviolation", (e) => {
  window.__calsyncCsp({ directive: e.violatedDirective, blocked: e.blockedURI, sample: e.sample || "" });
}, true);
"""


@dataclass
class Watch:
    """What went wrong in a page: console errors, uncaught errors, CSP violations."""

    console: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    csp: list[dict[str, Any]] = field(default_factory=list)
    allowed: list[re.Pattern[str]] = field(default_factory=list)

    def allow(self, pattern: str) -> None:
        """Console errors matching ``pattern`` are expected (a 422 the test provokes, say)."""
        self.allowed.append(re.compile(pattern))

    def problems(self) -> list[str]:
        def noise(message: str) -> bool:
            return any(p.search(message) for p in [*self.allowed, *NAVIGATION_NOISE])

        console = [m for m in self.console if not noise(m)]
        errors = [e for e in self.errors if not noise(e)]
        return [*console, *(f"pageerror: {e}" for e in errors), *(f"csp: {v}" for v in self.csp)]


# WebKit reports a same-origin fetch that a navigation cancelled (the status poll, when a test
# goes to another URL) as "Fetch API cannot load <url> due to access control checks". The app
# fetches only its own origin, and CSP violations are recorded separately, so this text is noise.
NAVIGATION_NOISE = [re.compile(r"due to access control checks\.?$")]


_WATCHES: dict[int, Watch] = {}


def watch_of(page: Page) -> Watch:
    return _WATCHES[id(page.context)]


@pytest.fixture(scope="session")
def session_seal(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """The environment every live daemon here runs in, for the whole session: the environment
    part of the per-test seal (which a session fixture cannot use) and ``TZ=Asia/Shanghai``. The
    per-test network guard still applies during each test; nothing a daemon runs leaves the
    process or loopback."""
    with seal_session(tmp_path_factory.mktemp("browser-session")):
        yield


@pytest.fixture(scope="session")
def live(session_seal: None) -> Iterator[LiveDaemon]:
    """The session's daemon: keep it as seeded (see ``fresh_live``)."""
    with live_daemon() as daemon:
        yield daemon


@pytest.fixture
def fresh_live(session_seal: None) -> Iterator[LiveDaemon]:
    """A daemon of this test's own, for tests that run cycles or change the config."""
    with live_daemon() as daemon:
        yield daemon


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[Playwright]:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session", params=BROWSERS)
def browser_name(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright, browser_name: str) -> Iterator[Browser]:
    launched = getattr(playwright_instance, browser_name).launch()
    try:
        yield launched
    finally:
        launched.close()


@pytest.fixture
def viewport() -> dict[str, int]:
    return DESKTOP


OpenPage = Callable[..., Page]


@pytest.fixture
def open_page(browser: Browser, live: LiveDaemon) -> Iterator[OpenPage]:
    contexts: list[tuple[BrowserContext, Watch, bool]] = []

    def open_page(
        *,
        daemon: LiveDaemon | None = None,
        viewport: dict[str, int] = DESKTOP,
        paired: bool = True,
        path: str = "/",
        color_scheme: str = "light",
        reduced_motion: str = "no-preference",
        locale: str = "zh-CN",
        has_touch: bool = False,
    ) -> Page:
        daemon = daemon or live
        context = browser.new_context(
            viewport=viewport,  # pyright: ignore[reportArgumentType]
            locale=locale,
            timezone_id="Asia/Shanghai",
            color_scheme=color_scheme,  # pyright: ignore[reportArgumentType]
            reduced_motion=reduced_motion,  # pyright: ignore[reportArgumentType]
            has_touch=has_touch,
            base_url=daemon.url,
        )
        watch = Watch()
        _WATCHES[id(context)] = watch
        contexts.append((context, watch, paired))
        context.expose_binding("__calsyncCsp", lambda source, violation: watch.csp.append(violation))
        context.add_init_script(CSP_LISTENER)
        page = context.new_page()
        page.clock.set_fixed_time(daemon.clock())

        def on_console(message: ConsoleMessage) -> None:
            if message.type == "error":
                watch.console.append(message.text)

        page.on("console", on_console)
        page.on("pageerror", lambda error: watch.errors.append(str(error)))
        if paired:
            page.goto(daemon.pair_url)  # 303 to "/" with the cookie
            if path != "/":
                page.goto(path)
        else:
            page.goto(path)
        return page

    yield open_page
    failures = []
    for context, watch, paired in contexts:
        if paired and watch.problems():
            failures.append(watch.problems())
        _WATCHES.pop(id(context), None)
        context.close()
    assert not failures, f"the page reported problems: {failures}"


@pytest.fixture
def paired_page(open_page: OpenPage, viewport: dict[str, int]) -> Page:
    return open_page(viewport=viewport)
