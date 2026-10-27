"""No URL, href or calendar ID reaches the log: failures are logged by type, with the rule or
account name; their text (built from request URLs and response bodies) only with
``CALSYNC_DEBUG=1``. Every ``calsync.*`` logger is captured at DEBUG."""

import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from calsync.errors import TransientError
from calsync.sources.base import CalendarMeta, RawEvent, SourcePull
from calsync.sources.google import GoogleSource
from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.fakes.target import FakeTarget
from tests.harness import GCAL, build_fake, config_for, rule

pytestmark = pytest.mark.anyio

CAL_ID = "someone@example.com"
HREF = "calsync-0123abcd-0123456789abcdef01234567.ics"
TARGET_URL = f"https://caldav.example.com/calendars/{CAL_ID}/events/"
LEAK = f"GET https://www.example.com/calendars/{CAL_ID}/events: 503 {HREF} upstream said no"
SECRETS = ("https://", f"/calendars/{CAL_ID}/events", HREF, CAL_ID, "upstream said no")


def four() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for i in range(4):
        fake.put_event(
            GCAL,
            google_event(f"e{i}", f"Standup {i}", "2026-10-26T09:00:00+08:00", "2026-10-26T09:15:00+08:00"),
        )
    return fake


class FailingSource:
    """Every calendar fails on its own, as a Google error for that calendar would."""

    async def list_calendars(self) -> list[Any]:
        return []

    async def pull(self, calendar_refs: Sequence[str], state: Mapping[str, Any]) -> SourcePull:
        return SourcePull({}, dict(state), {ref: TransientError(LEAK) for ref in calendar_refs})

    def to_series(self, raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Any]:
        return []

    def series_ids(self, raw_events: Iterable[RawEvent]) -> set[str]:
        return set()


class BrokenMapping(GoogleSource):
    """Pulls like Google, then fails inside the rule: the engine's per-rule isolation logs it."""

    def to_series(self, raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Any]:
        raise RuntimeError(LEAK)


async def source_fails(tmp_path: Path) -> None:
    config = config_for([rule(source=CAL_ID)])
    h = build_fake(tmp_path, four(), config, sources={"me": FailingSource()})
    assert (await h.engine.cycle()).rules["family"].status == "failed"


async def rule_fails(tmp_path: Path) -> None:
    h = build_fake(tmp_path, four(), config_for([rule()]))
    google = h.engine._sources["me"]  # pyright: ignore[reportPrivateUsage]
    assert isinstance(google, GoogleSource)
    google.__class__ = BrokenMapping
    assert (await h.engine.cycle()).rules["family"].status == "failed"


async def write_fails(tmp_path: Path) -> None:
    target = FakeTarget()

    def fail(op: str, href: str) -> None:
        raise TransientError(LEAK)

    target.before_write = fail
    h = build_fake(tmp_path, four(), config_for([rule()]), target=target)
    assert (await h.engine.cycle()).rules["family"].status == "failed"


async def titles_fail(tmp_path: Path) -> None:
    fake = four()
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, fake, config_for([rule()]), target=target)
    await h.engine.cycle()
    for i in range(4):
        fake.cancel_event(GCAL, f"e{i}")
    target.fail_fetch = TransientError(LEAK)  # only the guard's title lookup reads bodies now
    result = (await h.engine.cycle()).rules["family"]
    assert result.status == "blocked" and not result.blocked_titles_complete


async def target_unreadable(tmp_path: Path) -> None:
    """A target calendar configured by URL is never logged either (not an exception)."""
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, four(), config_for([rule(target=TARGET_URL)]), target=target)
    await h.engine.cycle()
    href = next(iter(target.managed(target.calendars[TARGET_URL])))
    target.put(href, target.objects[href][1])  # edited elsewhere: detection must read it
    target.hide_once = {href}
    await h.engine.cycle()


SCENARIOS = {
    "source": (source_fails, "source me: the calendar of rule(s) family failed (TransientError)"),
    "rule": (rule_fails, "rule family failed (RuntimeError)"),
    "write": (write_fails, "rule family: write failed (TransientError)"),
    "titles": (titles_fail, "rule family: could not read the titles of planned deletes (TransientError)"),
    "target": (target_unreadable, "target dav (rule(s) family): 1 changed resource(s) could not be read"),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_failures_are_logged_without_urls_or_ids(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    scenario, expected = SCENARIOS[name]
    caplog.set_level(logging.DEBUG, logger="calsync")
    await scenario(tmp_path)
    assert expected in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text, secret
    assert all(r.exc_info is None for r in caplog.records)


@pytest.mark.parametrize("name", sorted(set(SCENARIOS) - {"target"}))
async def test_calsync_debug_adds_the_details(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("CALSYNC_DEBUG", "1")
    scenario, _ = SCENARIOS[name]
    caplog.set_level(logging.DEBUG, logger="calsync")
    await scenario(tmp_path)
    details = [r for r in caplog.records if r.levelno == logging.DEBUG and r.exc_info is not None]
    assert details and "upstream said no" in caplog.text
