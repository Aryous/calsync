"""Fixtures for the browser tests: a live daemon (``tests.browser.live``) and Playwright pages
paired with it, in Chromium and WebKit.

- ``live``: the session's daemon, seeded as ``live.py`` describes. Read-only tests share it.
- ``fresh_live``: a daemon of the test's own, for tests that run cycles or change the config.
- ``keychain``: an in-memory keychain in place of the seal's refusing one, for tests that add
  accounts (``account.add`` from the UI asks it; ``LiveDaemon.authorise`` stores a secret).
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
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Locator,
    Page,
    Playwright,
    expect,
    sync_playwright,
)

from tests.browser.live import FakeKeychain, LiveDaemon, live_daemon, seal_session


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


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    """An in-memory keychain for this test (``credentials._keyring``, read at call time, on the
    daemon's thread too), in place of the seal's refusing one: adding an account asks it whether
    an entry exists, and ``LiveDaemon.authorise`` stores one. Request it before ``fresh_live``,
    so it outlives the test's daemon."""
    from calsync import credentials

    fake = FakeKeychain()
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    return fake


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


# ----- helpers for the tests ------------------------------------------------------------------


def ready(page: Page) -> None:
    """The app rendered, read /api/status and the calendar tab's events (nothing left in flight
    that a test changing the page under it could catch)."""
    expect(page.locator(".result h1")).not_to_have_text("正在读取同步状态…")
    expect(page.locator(".hub-cap small")).to_have_text("4 条线路")
    expect(page.locator("#pane-calendar .cal-frame")).to_have_attribute("aria-busy", "false")


def sync_button(page: Page) -> Locator:
    return page.get_by_role("button", name=re.compile("立即同步|同步中"))


def settled(page: Page) -> None:
    """The inspector's slide (and the page's shift beside it) has finished. The map's comets
    (animations with the id ``comet``) are ambient and never "finish" as a page does: ignored."""
    page.wait_for_function(
        """() => document.getAnimations()
          .filter((a) => a.playState === "running" && a.id !== "comet").length === 0"""
    )


def refresh_status(page: Page) -> None:
    """Fetch /api/status now, as the page does when it becomes visible again (not in 10 s)."""
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def until(condition: Callable[[], bool], timeout: float = 10) -> None:
    """Wait, Python-side, for something in the daemon."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the condition never became true"
        time.sleep(0.02)


def edited_status(page: Page, edit: Any) -> None:
    """Serve the live /api/status through ``edit(status) -> status`` from now on, and refetch."""

    def handle(route: Any) -> None:
        response = route.fetch()
        route.fulfill(response=response, json=edit(response.json()))

    page.route("**/api/status", handle)
    refresh_status(page)
