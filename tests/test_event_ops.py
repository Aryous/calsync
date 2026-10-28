"""event.create / event.edit / event.delete through a daemon over the fake Google and target."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from calsync import configfile, events_view
from calsync.daemon import Daemon
from calsync.naming import object_id
from calsync.ops import OpResult
from calsync.sources.google_write import WriteConflict
from calsync.store import Store
from calsync.views import StateReader
from tests.factories import google_event
from tests.harness import GCAL
from tests.test_daemon import WORK, World, eventually, until
from tests.test_daemon_reload import TEXT

pytestmark = pytest.mark.anyio


class Live:
    """A running daemon (fake time) whose first cycle synced the world's events."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "config.toml"
        self.path.write_text(TEXT)
        self.world = World(block=True)
        fake = self.world.fake
        fake.put_event(
            GCAL,
            google_event(
                "drops",
                "Reminder",
                "2026-10-24T08:00:00+08:00",
                "2026-10-24T08:05:00+08:00",
                recurrence=["RRULE:FREQ=DAILY;COUNT=10"],
            ),
        )
        config = configfile.validate_text(TEXT, tmp_path)
        self.daemon: Daemon = self.world.daemon(config, tmp_path, config_path=self.path)
        self.state_dir = tmp_path
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> "Live":
        self.task = asyncio.create_task(self.daemon.run(self.world.stop))
        await until(lambda: len(self.world.delays) == 1)
        return self

    async def stop(self) -> None:
        self.world.stop.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, 5)

    async def op(
        self, name: str, args: dict[str, Any], *, dry_run: bool = False, source: str = "cli"
    ) -> OpResult:
        return await asyncio.wait_for(self.daemon.run_op(name, args, dry_run, source=source), 5)  # type: ignore[arg-type]

    def runs(self, rule: str = "family") -> int:
        with StateReader(self.state_dir / "state.db") as reader:
            return len(reader.runs(rule))

    async def cycle(self, rule: str = "family") -> None:
        before = self.runs(rule)
        self.daemon.request_cycle(rule)
        await eventually(lambda: self.runs(rule) > before)

    def event_id(self, title: str, day: str = "2026-10-25") -> str:
        """The calsync id of the event titled ``title`` on ``day`` (from ``GET /api/events``)."""
        with StateReader(self.state_dir / "state.db") as reader:
            view = events_view.build_events(
                self.daemon.config,
                reader,
                datetime.fromisoformat(f"{day}T00:00:00+08:00"),
                datetime.fromisoformat(f"{day}T23:59:00+08:00"),
                now=self.world.now,
            )
        return next(e.id for e in view.events if e.instance.title == title)

    def writes(self) -> list[Any]:
        with Store(self.state_dir / "state.db") as store:
            return store.writes()

    def copy_titles(self) -> list[str]:
        return sorted(
            line.split(":", 1)[1].strip()
            for _, data in self.world.target.objects.values()
            for line in data.decode().splitlines()
            if line.startswith("SUMMARY")
        )


@pytest.fixture
async def live(tmp_path: Path) -> Any:
    d = await Live(tmp_path).start()
    yield d
    await d.stop()


async def test_editing_a_single_event_writes_google_records_it_and_syncs(live: Live) -> None:
    eid = live.event_id("Lunch")
    before = live.world.fake.event(GCAL, "lunch")
    result = await live.op(
        "event.edit", {"id": eid, "title": "Long lunch", "expect_updated": before["updated"], "wait": True}
    )
    assert result.changed and result.data["changes"] == [
        {"field": "title", "before": "Lunch", "after": "Long lunch"}
    ]
    assert "Lunch" not in result.summary  # titles stay out of summaries
    assert live.world.fake.event(GCAL, "lunch")["summary"] == "Long lunch"
    assert result.data["cycle"]["ran"] is True
    [row] = live.writes()
    assert (row.op, row.origin, row.title, row.status) == ("edit", "cli", "Long lunch", "ok")
    assert "Long lunch" in live.copy_titles()


async def test_a_stale_version_is_a_conflict_and_writes_nothing(live: Live) -> None:
    eid = live.event_id("Lunch")
    seen = live.world.fake.event(GCAL, "lunch")["updated"]
    live.world.fake.touch(GCAL, "lunch", location="Canteen")
    with pytest.raises(WriteConflict):
        await live.op("event.edit", {"id": eid, "title": "Mine", "expect_updated": seen})
    assert live.world.fake.event(GCAL, "lunch")["summary"] == "Lunch"
    assert live.writes() == []


async def test_a_dry_run_reads_but_writes_nothing(live: Live) -> None:
    eid = live.event_id("Lunch")
    result = await live.op(
        "event.edit", {"id": eid, "start": "2026-10-25T12:30", "end": "2026-10-25T13:30"}, dry_run=True
    )
    assert not result.changed and result.data["would_change"] is True
    assert result.data["changes"][0]["field"] == "time"
    assert live.world.fake.writes == [] and live.writes() == []


async def test_a_timed_edit_keeps_the_events_zone(live: Live) -> None:
    eid = live.event_id("Lunch")
    await live.op("event.edit", {"id": eid, "start": "2026-10-25T04:30:00Z", "end": "2026-10-25T05:30:00Z"})
    start = live.world.fake.event(GCAL, "lunch")["start"]
    assert start == {"dateTime": "2026-10-25T12:30:00+08:00", "timeZone": "Asia/Shanghai"}


async def test_a_repeating_event_needs_a_scope(live: Live) -> None:
    eid = live.event_id("Reminder")
    with pytest.raises(Exception, match="scope"):
        await live.op("event.edit", {"id": eid, "title": "x"})


async def test_only_this_occurrence(live: Live) -> None:
    eid = live.event_id("Reminder")
    result = await live.op("event.edit", {"id": eid, "scope": "this", "title": "Reminder (left)", "wait": True})
    assert result.changed
    exceptions = [e for e in live.world.fake.events(GCAL) if e.get("recurringEventId") == "drops"]
    assert [e["summary"] for e in exceptions] == ["Reminder (left)"]
    assert live.world.fake.event(GCAL, "drops")["summary"] == "Reminder"
    # The mirror knows the exception now; editing it again reads the exception, not the series.
    again = await live.op(
        "event.edit", {"id": live.event_id("Reminder (left)"), "scope": "this", "location": "Home"}
    )
    assert again.changed and [
        e["location"] for e in live.world.fake.events(GCAL) if e.get("recurringEventId")
    ] == ["Home"]


async def test_the_whole_series_title_and_a_move_by_offset(live: Live) -> None:
    eid = live.event_id("Reminder")
    await live.op("event.edit", {"id": eid, "scope": "series", "title": "Daily vitamins"})
    assert live.world.fake.event(GCAL, "drops")["summary"] == "Daily vitamins"
    await live.cycle()
    # Dragging the 25th's occurrence an hour later moves every occurrence, from the series' own start.
    moved = await live.op(
        "event.edit",
        {
            "id": live.event_id("Daily vitamins"),
            "scope": "series",
            "start": "2026-10-25T09:00",
            "end": "2026-10-25T09:10",
            "move": True,
        },
    )
    master = live.world.fake.event(GCAL, "drops")
    assert master["start"]["dateTime"] == "2026-10-24T09:00:00+08:00"
    assert master["end"]["dateTime"] == "2026-10-24T09:10:00+08:00"
    assert live.writes()[0].op == "move" and moved.data["scope"] == "series"


async def test_read_only_calendars_and_invitations_are_refused_before_any_request(
    live: Live, tmp_path: Path
) -> None:
    fake = live.world.fake
    fake.put_event(
        GCAL,
        google_event(
            "inv",
            "Invite",
            "2026-10-25T15:00:00+08:00",
            "2026-10-25T16:00:00+08:00",
            organizer={"email": "x@example.com"},
        ),
    )
    await live.cycle()
    with pytest.raises(Exception, match="organiser"):
        await live.op("event.edit", {"id": live.event_id("Invite"), "title": "x"})
    fake.add_calendar(WORK, "Work", access_role="reader")
    await live.op("calendars.refresh", {"account": "me"})
    fake.writes.clear()
    with pytest.raises(Exception, match="read-only"):
        await live.op("event.edit", {"id": live.event_id("Standup", "2026-10-26"), "title": "x"})
    assert fake.writes == []


async def test_without_write_access_the_adapter_is_rebuilt_once_then_a_person_is_asked(live: Live) -> None:
    live.world.fake.write_scope = False
    builds = len(live.world.builds)
    result = await live.op("event.edit", {"id": live.event_id("Lunch"), "title": "x"})
    assert not result.changed and result.data["status"] == "no_write_access"
    assert result.needs_human == {
        "command": "calsync auth me --write",
        "reason": "a person allows calsync to change events in Google (one consent page)",
    }
    assert len(live.world.builds) == builds + 1  # rebuilt from the stored credential, once


async def test_a_delete_in_the_web_ui_pre_approves_its_copy_and_one_from_the_cli_does_not(live: Live) -> None:
    await live.op("event.delete", {"id": live.event_id("Lunch")}, source="ui")
    assert live.world.fake.event(GCAL, "lunch")["status"] == "cancelled"
    assert set(live.daemon._covers["family"]) == {object_id("family", GCAL, "lunch")}  # pyright: ignore[reportPrivateUsage]
    await live.op("event.delete", {"id": live.event_id("Reminder"), "scope": "series"}, source="cli")
    assert set(live.daemon._covers["family"]) == {object_id("family", GCAL, "lunch")}  # pyright: ignore[reportPrivateUsage]
    assert [w.origin for w in live.writes()] == ["cli", "ui"]


async def test_deleting_one_occurrence(live: Live) -> None:
    await live.op("event.delete", {"id": live.event_id("Reminder"), "scope": "this"}, source="ui")
    stubs = [e for e in live.world.fake.events(GCAL) if e.get("recurringEventId") == "drops"]
    assert [e["status"] for e in stubs] == ["cancelled"]
    assert live.daemon._covers == {}  # pyright: ignore[reportPrivateUsage]  # the series' copy stays


async def test_creating_an_event(live: Live) -> None:
    result = await live.op(
        "event.create",
        {
            "account": "me",
            "calendar": "Family",
            "title": "Dentist",
            "start": "2026-10-27T10:00",
            "end": "2026-10-27T10:30",
            "alarms": "-30m",
            "wait": True,
        },
    )
    assert result.changed and result.data["rules"] == ["family"]
    made = live.world.fake.event(GCAL, result.data["event"]["source_id"])
    assert made["summary"] == "Dentist" and made["start"]["timeZone"] == "Asia/Shanghai"
    assert made["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}
    assert live.event_id("Dentist", "2026-10-27") == result.data["event_id"]
    assert "Dentist" in live.copy_titles()
    assert live.writes()[0].op == "create"


async def test_create_refuses_bad_fields(live: Live) -> None:
    with pytest.raises(Exception, match="end: must be after the start"):
        await live.op(
            "event.create",
            {
                "account": "me",
                "calendar": "Family",
                "title": "x",
                "start": "2026-10-27T10:00",
                "end": "2026-10-27T09:00",
            },
        )
    with pytest.raises(Exception, match="not a configured Google account"):
        await live.op(
            "event.create",
            {
                "account": "dav",
                "calendar": "Family",
                "title": "x",
                "start": "2026-10-27",
                "end": "2026-10-28",
            },
        )
    assert live.world.fake.writes == []


# ----- review fixes: moving a whole series ---------------------------------------------------------


def _target_at(start: str, zone: str) -> Any:
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    from calsync.event_ops import Target
    from calsync.events_view import Located
    from calsync.model import Instance, When

    at = dt.fromisoformat(start).astimezone(ZoneInfo(zone))
    inst = Instance(When(at, zone), When(at, zone), "x", "", (), True)
    located = Located("me", "cal", "cal", "Cal", "s1", inst, start, zone, ("family",))
    return Target(located, "owner", None, None, "x")


def _moved(start: str, end: str, zone: str) -> Any:
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    from calsync.model import When
    from calsync.sources.google_write import EventFields

    z = ZoneInfo(zone)
    return EventFields(
        start=When(dt.fromisoformat(start).astimezone(z), zone),
        end=When(dt.fromisoformat(end).astimezone(z), zone),
    )


def test_a_series_moved_across_a_dst_change_keeps_its_hour() -> None:
    from calsync.event_ops import _series_shift  # pyright: ignore[reportPrivateUsage]

    zone = "Europe/Berlin"
    live = {
        "start": {"dateTime": "2026-07-01T09:00:00+02:00", "timeZone": zone},
        "recurrence": ["RRULE:FREQ=DAILY"],
    }
    # The Saturday occurrence (summer time) dragged to Sunday 09:00, the day winter time begins.
    target = _target_at("2026-10-24T09:00:00+02:00", zone)
    out = _series_shift(
        _moved("2026-10-25T09:00:00+01:00", "2026-10-25T09:30:00+01:00", zone), target, live, zone
    )
    assert out.start is not None and out.end is not None
    assert out.start.value.isoformat() == "2026-07-02T09:00:00+02:00"  # one day later, still 09:00
    assert out.end.value.isoformat() == "2026-07-02T09:30:00+02:00"
    # An hour later on the same day: every occurrence an hour later.
    out = _series_shift(
        _moved("2026-10-24T10:00:00+02:00", "2026-10-24T10:30:00+02:00", zone), target, live, zone
    )
    assert out.start is not None and out.start.value.isoformat() == "2026-07-01T10:00:00+02:00"


def test_a_series_whose_rule_fixes_its_days_moves_in_time_only() -> None:
    from calsync.errors import ConfigError
    from calsync.event_ops import _series_shift  # pyright: ignore[reportPrivateUsage]

    zone = "Asia/Shanghai"
    live = {
        "start": {"dateTime": "2026-10-05T09:30:00+08:00", "timeZone": zone},
        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"],
    }
    target = _target_at("2026-10-26T09:30:00+08:00", zone)
    with pytest.raises(ConfigError, match="set by its rule"):
        _series_shift(
            _moved("2026-10-27T09:30:00+08:00", "2026-10-27T10:30:00+08:00", zone), target, live, zone
        )
    out = _series_shift(
        _moved("2026-10-26T11:00:00+08:00", "2026-10-26T12:00:00+08:00", zone), target, live, zone
    )
    assert out.start is not None and out.start.value.isoformat() == "2026-10-05T11:00:00+08:00"


async def test_create_takes_the_callers_id_and_a_second_save_finds_the_event(live: Live) -> None:
    args = {
        "account": "me",
        "calendar": "Family",
        "title": "Dentist",
        "start": "2026-10-27T10:00",
        "end": "2026-10-27T10:30",
        "source_id": "abc0123456789",
    }
    first = await live.op("event.create", args)
    again = await live.op("event.create", args)  # the answer to the first was lost: saved again
    assert first.data["event"]["source_id"] == again.data["event"]["source_id"] == "abc0123456789"
    assert [e["summary"] for e in live.world.fake.events(GCAL)].count("Dentist") == 1
    with pytest.raises(Exception, match="source_id"):
        await live.op("event.create", {**args, "source_id": "UPPER-case"})
