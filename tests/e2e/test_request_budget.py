"""Spec §8.1 request budget, enforced. If this fails, a change made sync more expensive."""

from collections import Counter
from pathlib import Path

import httpx
import pytest

from calsync.engine import CycleResult, Engine
from calsync.http import RequestLog, make_client
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import StaticToken
from calsync.store import Store
from calsync.targets.caldav import CalDavTarget
from tests.factories import google_event
from tests.fakes.google import BASE_URL, FakeGoogle
from tests.harness import GCAL, NOW, build, config_for, rule

pytestmark = [pytest.mark.anyio, pytest.mark.e2e]

WORK = "work@group.calendar.google.com"
# Two rules, one Google account, two source and two target calendars.
IDLE = Counter({"google-calendarlist": 1, "google-events": 2, "sync-collection": 2})


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

    # The cycle right after our own writes costs exactly an idle cycle: no body read.
    mark = h.log.mark()
    after_write = await h.engine.cycle()
    assert all(r.plan.empty for r in after_write.rules.values())
    assert Counter(e.kind for e in h.log.since(mark)) == IDLE

    for _ in range(2):
        mark = h.log.mark()
        idle = await h.engine.cycle()
        assert all(r.plan.empty for r in idle.rules.values())
        kinds = Counter(e.kind for e in h.log.since(mark))
        # 1 calendarList per Google account + 1 events.list per source calendar
        # + 1 change check per target calendar. Nothing else.
        assert kinds == IDLE
        assert idle.requests == 5


async def test_fresh_process_per_cycle_meets_budget(tmp_path: Path, radicale_url: str, dav_user: str) -> None:
    """`calsync run` from cron: every cycle is a new Engine and Store reading the saved state."""
    fake = fake_with_two_calendars()
    config = config_for([rule("family"), rule("work", source="Work", target="Work")])

    async def one_process() -> tuple[Counter[str], CycleResult]:
        log = RequestLog()
        async with (
            make_client(log, transport=fake.transport()) as google,
            make_client(log, auth=httpx.BasicAuth(dav_user, "pw")) as dav,
        ):
            with Store(tmp_path / "state.db") as store:
                engine = Engine(
                    config,
                    store,
                    {"me": GoogleSource(google, StaticToken("t"), base_url=BASE_URL)},
                    {"dav": CalDavTarget(dav, radicale_url)},
                    request_log=log,
                    clock=lambda: NOW,
                )
                result = await engine.cycle()
        assert all(r.status == "ok" for r in result.rules.values()), result.rules
        return Counter(e.kind for e in log.entries), result

    _, cold = await one_process()
    assert sum(r.created for r in cold.rules.values()) == 6
    after_write, result = await one_process()
    assert after_write == IDLE and all(r.plan.empty for r in result.rules.values())
    for _ in range(2):
        idle, result = await one_process()
        assert idle == IDLE and result.requests == 5


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
