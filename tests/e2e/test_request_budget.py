"""Spec §8.1 request budget, enforced. If this fails, a change made sync more expensive."""

from collections import Counter
from pathlib import Path

import pytest

from tests.factories import google_event
from tests.fakes.google import FakeGoogle
from tests.harness import GCAL, build, config_for, rule

pytestmark = [pytest.mark.anyio, pytest.mark.e2e]

WORK = "work@group.calendar.google.com"


def fake_with_two_calendars() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.add_calendar(WORK, "Work")
    for i in range(5):
        fake.put_event(
            GCAL,
            google_event(
                f"f{i}",
                f"F{i}",
                f"2026-10-2{5 + i % 3}T09:00:00+08:00",
                f"2026-10-2{5 + i % 3}T09:05:00+08:00",
            ),
        )
    fake.put_event(
        WORK,
        google_event(
            "w1",
            "Standup",
            "2026-10-26T09:00:00+08:00",
            "2026-10-26T09:15:00+08:00",
            recurrence=["RRULE:FREQ=DAILY;COUNT=10"],
        ),
    )
    return fake


async def test_idle_cycles_meet_budget(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    fake = fake_with_two_calendars()
    rules = [rule("family"), rule("work", source="Work", target="Work")]
    h = build(tmp_path, radicale_url, dav_user, fake, config_for(rules))
    await h.engine.cycle()  # cold start

    # The cycle right after our own writes must not download any body.
    mark = h.log.mark()
    after_write = await h.engine.cycle()
    assert all(r.plan.empty for r in after_write.rules.values())
    assert "calendar-multiget" not in [e.kind for e in h.log.since(mark)]

    for _ in range(2):
        mark = h.log.mark()
        idle = await h.engine.cycle()
        assert all(r.plan.empty for r in idle.rules.values())
        kinds = Counter(e.kind for e in h.log.since(mark))
        # 1 calendarList per Google account + 1 events.list per source calendar
        # + 1 change check per target calendar. Nothing else.
        assert kinds == Counter({"google-calendarlist": 1, "google-events": 2, "sync-collection": 2})
        assert idle.requests == 5


async def test_k_changes_cost_k_writes(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    fake = fake_with_two_calendars()
    h = build(tmp_path, radicale_url, dav_user, fake, config_for([rule("family")]))
    await h.engine.cycle()
    await h.engine.cycle()
    for i in range(3):
        fake.put_event(
            GCAL, google_event(f"f{i}", f"F{i} v2", "2026-10-27T10:00:00+08:00", "2026-10-27T10:05:00+08:00")
        )
    mark = h.log.mark()
    result = await h.engine.cycle()
    assert result.rules["family"].updated == 3
    kinds = Counter(e.kind for e in h.log.since(mark))
    assert kinds == Counter({"google-calendarlist": 1, "google-events": 1, "sync-collection": 1, "put": 3})
