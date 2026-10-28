"""What calsync wrote, per copy (``target_object.summary``), and what each write changed
(``change.detail``): the data behind "time changed to 09:30, was 10:00"."""

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from calsync.model import Alarm, Occurrence
from calsync.naming import object_id, rule_tag
from calsync.render import inspect, render, summary_of
from tests.factories import at, day, google_event, series
from tests.fakes.google import FakeGoogle
from tests.fakes.target import FakeTarget
from tests.harness import GCAL, FakeHarness, build_fake, config_for, rule

pytestmark = pytest.mark.anyio


# ----- summary_of / inspect ------------------------------------------------------------


def test_summary_of_a_single_event() -> None:
    s = series(title="Lunch", start=at(2026, 10, 28, 12), minutes=60, alarms=(Alarm(timedelta(minutes=-10)),))
    assert summary_of(s) == {
        "title": "Lunch",
        "start": "2026-10-28T12:00:00+08:00",
        "end": "2026-10-28T13:00:00+08:00",
        "all_day": False,
        "location": None,  # empty: None
        "alarms_minutes_before": [10],
        "recurring": False,
    }


def test_summary_of_a_recurring_series_is_its_master_as_written() -> None:
    """The master VEVENT's own fields, even when the first instance is overridden or excluded:
    the series as written, not one of its instances."""
    first = at(2026, 10, 23, 9, 30)
    override = Occurrence(first, at(2026, 10, 23, 11), at(2026, 10, 23, 11, 5), "Moved", "", "Room 2", ())
    s = series(
        title="Standup",
        start=first,
        recurrence=("RRULE:FREQ=DAILY;COUNT=5",),
        overrides=(override,),
        alarms=(Alarm(timedelta(0)), Alarm(timedelta(minutes=-15))),
    )
    got = summary_of(s)
    assert got["title"] == "Standup" and got["location"] is None
    assert got["start"] == "2026-10-23T09:30:00+08:00" and got["recurring"] is True
    assert got["alarms_minutes_before"] == [15, 0]  # sorted descending, like views.upcoming


def test_summary_of_an_all_day_event() -> None:
    s = series(start=day(2026, 10, 28))
    got = summary_of(s)
    assert (got["start"], got["all_day"]) == ("2026-10-28", True)


def test_inspect_reads_the_summary_fields_back() -> None:
    s = series(title="Lunch", start=at(2026, 10, 28, 12), minutes=60, alarms=(Alarm(timedelta(minutes=-10)),))
    body = render(s, uid="u@calsync", rule_name="r", content_hash="h", now=s.start.utc())
    found = inspect(body)
    assert found is not None and found.fields == summary_of(s)


def test_inspect_fields_of_an_odd_body_never_raise() -> None:
    body = (
        b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x\r\nSUMMARY:Only a title\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    found = inspect(body)
    assert found is not None and found.fields is not None
    assert found.fields["title"] == "Only a title" and "start" not in found.fields  # no DTSTART: left out


# ----- the engine -------------------------------------------------------------------------


def one_event(
    start: str = "2026-10-28T10:00:00+08:00", end: str = "2026-10-28T11:00:00+08:00", **extra: Any
) -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    fake.put_event(GCAL, google_event("planning", "Planning", start, end, **extra))
    return fake


def stored(h: FakeHarness, rule_name: str = "family", source_id: str = "planning") -> dict[str, Any]:
    """The summary stored for the copy (it must have one)."""
    oid = object_id(rule_name, GCAL, source_id)
    row = h.store._db.execute(
        "SELECT summary FROM target_object WHERE rule_tag = ? AND object_id = ?", (rule_tag(rule_name), oid)
    ).fetchone()
    assert row is not None and row[0] is not None
    return json.loads(row[0])


def changes(h: FakeHarness) -> list[tuple[str, dict[str, Any] | None]]:
    rows = h.store._db.execute("SELECT action, detail FROM change ORDER BY id").fetchall()
    return [(action, None if detail is None else json.loads(detail)) for action, detail in rows]


async def test_create_stores_the_summary_and_no_detail(tmp_path: Path) -> None:
    h = build_fake(tmp_path, one_event(), config_for([rule()]))
    assert (await h.engine.cycle()).rules["family"].created == 1
    summary = stored(h)
    assert (summary["title"], summary["start"], summary["end"]) == (
        "Planning",
        "2026-10-28T10:00:00+08:00",
        "2026-10-28T11:00:00+08:00",
    )
    assert changes(h) == [("created", None)]


async def test_summary_survives_a_cycle_that_saves_the_cache_unchanged(tmp_path: Path) -> None:
    """``save_target_cache`` rewrites every row of the calendar: the summary must travel on
    ``CachedObject``, in memory and through a fresh process."""
    fake = one_event()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    first = stored(h)
    fake.put_event(
        GCAL, google_event("other", "Other", "2026-10-29T10:00:00+08:00", "2026-10-29T11:00:00+08:00")
    )
    assert (await h.engine.cycle()).rules["family"].created == 1  # the cache is saved again
    assert stored(h) == first
    fresh = build_fake(tmp_path, fake, config_for([rule()]), target=h.target)  # a new process
    assert (await fresh.engine.cycle()).rules["family"].plan.empty
    assert stored(fresh) == first


async def test_update_detail_lists_exactly_the_changed_fields(tmp_path: Path) -> None:
    fake = one_event()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    fake.put_event(
        GCAL,
        google_event(
            "planning",
            "Planning",
            "2026-10-28T09:30:00+08:00",
            "2026-10-28T11:00:00+08:00",
            location="Room 2",
            description="new notes",  # not a summary field
        ),
    )
    assert (await h.engine.cycle()).rules["family"].updated == 1
    assert changes(h)[-1] == (
        "updated",
        {
            "fields": {
                "start": ["2026-10-28T10:00:00+08:00", "2026-10-28T09:30:00+08:00"],
                "location": [None, "Room 2"],
            }
        },
    )
    assert stored(h)["start"] == "2026-10-28T09:30:00+08:00"


async def test_update_of_fields_outside_the_summary_has_an_empty_detail(tmp_path: Path) -> None:
    """A notes-only change: a previous summary exists, nothing it lists changed."""
    fake = one_event()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    fake.put_event(
        GCAL,
        google_event(
            "planning", "Planning", "2026-10-28T10:00:00+08:00", "2026-10-28T11:00:00+08:00", description="n"
        ),
    )
    await h.engine.cycle()
    assert changes(h)[-1] == ("updated", {"fields": {}})


async def test_update_without_a_previous_summary_has_no_detail(tmp_path: Path) -> None:
    """A copy written by an older calsync (no summary stored): no detail, never a guess."""
    fake = one_event()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    h.store._db.execute("UPDATE target_object SET summary = NULL")
    fresh = build_fake(tmp_path, fake, config_for([rule()]), target=h.target)
    fake.put_event(
        GCAL, google_event("planning", "Planning 2", "2026-10-28T10:00:00+08:00", "2026-10-28T11:00:00+08:00")
    )
    assert (await fresh.engine.cycle()).rules["family"].updated == 1
    assert changes(fresh)[-1] == ("updated", None)
    assert stored(fresh)["title"] == "Planning 2"


async def test_revert_detail_is_what_the_phone_changed(tmp_path: Path) -> None:
    """[what calsync wrote, what the copy showed] for each field edited on the target."""
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, one_event(), config_for([rule()]), target=target)
    await h.engine.cycle()
    await h.engine.cycle()
    cal = target.calendars["Family"]
    href, data = next(iter(target.managed(cal).items()))
    edited = data.replace(
        b"DTSTART;TZID=Asia/Shanghai:20261028T100000", b"DTSTART;TZID=Asia/Shanghai:20261028T093000"
    ).replace(b"SUMMARY:Planning", b"SUMMARY:Planning (moved)")
    assert edited != data
    target.put(href, edited)
    res = (await h.engine.cycle()).rules["family"]
    assert res.reverted == 1
    assert changes(h)[-1] == (
        "reverted",
        {
            "fields": {
                "title": ["Planning", "Planning (moved)"],
                "start": ["2026-10-28T10:00:00+08:00", "2026-10-28T09:30:00+08:00"],
            }
        },
    )
    assert stored(h)["start"] == "2026-10-28T10:00:00+08:00"  # what was written back


async def test_revert_after_a_restart_has_no_detail(tmp_path: Path) -> None:
    """The edited body is read once, by detection; a revert written by another process than the
    one that read it has nothing to compare with (no second read: the request budget)."""
    target = FakeTarget(mode="sync")
    fake = one_event()
    h = build_fake(tmp_path, fake, config_for([rule()]), target=target)
    await h.engine.cycle()
    cal = target.calendars["Family"]
    href, data = next(iter(target.managed(cal).items()))
    target.put(href, data.replace(b"SUMMARY:Planning", b"SUMMARY:Edited"))
    await h.engine.cycle(dry_run=True)  # detection reads and classifies the edit; nothing is written
    fresh = build_fake(tmp_path, fake, config_for([rule()]), target=target)
    res = (await fresh.engine.cycle()).rules["family"]
    assert res.reverted == 1
    assert changes(fresh)[-1] == ("reverted", None)


async def test_adopted_copy_gets_the_summary(tmp_path: Path) -> None:
    """Lost state: the copy is read back, equals what calsync would write, and is adopted with
    no write; its summary is what it shows."""
    fake = one_event()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    h.store.close()
    for suffix in ("", "-wal", "-shm"):  # the state is lost
        (tmp_path / f"state.db{suffix}").unlink(missing_ok=True)
    fresh = build_fake(tmp_path, fake, config_for([rule()]), target=h.target)
    res = (await fresh.engine.cycle()).rules["family"]
    assert res.plan is not None and res.plan.empty
    assert stored(fresh)["title"] == "Planning"


def test_summary_column_round_trips_through_the_store(tmp_path: Path) -> None:
    from calsync.store import Store
    from calsync.targets.base import CachedObject, TargetCache

    with Store(tmp_path / "s.db") as store:
        cache = TargetCache(
            "c1", {"/a.ics": CachedObject('"e"', "t", "o", "h", None, summary='{"title": "X"}')}
        )
        store.save_target_cache("acc", "/cal/", cache)
        assert store.load_target_cache("acc", "/cal/").objects["/a.ics"].summary == '{"title": "X"}'
    db = sqlite3.connect(tmp_path / "s.db")
    assert db.execute("SELECT summary FROM target_object").fetchone() == ('{"title": "X"}',)
    db.close()


# ----- fix round 1 ------------------------------------------------------------------------

START = b"DTSTART;TZID=Asia/Shanghai:20261028T100000"


def test_padded_title_and_location_are_trimmed_as_the_copy_reads_them() -> None:
    """``summary_of`` trims like ``inspect``: a phone that moved only the start must not show a
    title or location change because Google's text carried spaces."""
    from dataclasses import replace

    from calsync.render import changed_fields

    s = replace(series(title="  Planning  ", start=at(2026, 10, 28, 10), minutes=60), location=" Room 2 ")
    assert (summary_of(s)["title"], summary_of(s)["location"]) == ("Planning", "Room 2")
    body = render(s, uid="u@calsync", rule_name="r", content_hash="h", now=s.start.utc())
    edited = body.replace(START, b"DTSTART;TZID=Asia/Shanghai:20261028T093000")
    assert edited != body
    found = inspect(edited)
    assert found is not None and found.fields is not None
    assert changed_fields(summary_of(s), found.fields) == {
        "start": ["2026-10-28T10:00:00+08:00", "2026-10-28T09:30:00+08:00"]
    }


def test_changed_fields_compares_times_as_instants() -> None:
    from calsync.render import changed_fields

    old = {"start": "2026-10-28T10:00:00+08:00", "end": "2026-10-28", "all_day": False}
    same = {"start": "2026-10-28T02:00:00+00:00", "end": "2026-10-28", "all_day": False}
    assert changed_fields(old, same) == {}
    moved = {"start": "2026-10-28T02:30:00+00:00", "end": "2026-10-29", "all_day": False}
    assert changed_fields(old, moved) == {
        "start": ["2026-10-28T10:00:00+08:00", "2026-10-28T02:30:00+00:00"],
        "end": ["2026-10-28", "2026-10-29"],
    }
    # an all-day date is not the instant of its midnight
    assert changed_fields({"start": "2026-10-28"}, {"start": "2026-10-28T00:00:00+00:00"}) != {}


CUSTOM_ZONE = b"""BEGIN:VTIMEZONE\r
TZID:Phone Zone\r
BEGIN:STANDARD\r
DTSTART:19700101T000000\r
TZOFFSETFROM:+0800\r
TZOFFSETTO:+0800\r
TZNAME:PZ\r
END:STANDARD\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
"""


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda data: data.replace(START, b"DTSTART:20261028T020000Z"),  # the same instant in UTC
        lambda data: data.replace(b"BEGIN:VEVENT\r\n", CUSTOM_ZONE, 1).replace(
            START, b"DTSTART;TZID=Phone Zone:20261028T100000"
        ),  # a VTIMEZONE of the phone's own
    ],
    ids=["utc", "custom-vtimezone"],
)
async def test_revert_detail_ignores_a_start_rewritten_in_another_zone(tmp_path: Path, rewrite: Any) -> None:
    """The phone wrote the same start in another zone (and moved nothing): the copy differs (the
    zone drives how a series expands, so it is reverted), but no field a person sees changed."""
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, one_event(), config_for([rule()]), target=target)
    await h.engine.cycle()
    cal = target.calendars["Family"]
    href, data = next(iter(target.managed(cal).items()))
    edited = rewrite(data)
    assert edited != data
    found = inspect(edited)
    assert found is not None and found.fields is not None and "start" in found.fields  # not vacuous
    target.put(href, edited)
    assert (await h.engine.cycle()).rules["family"].reverted == 1
    assert changes(h)[-1] == ("reverted", {"fields": {}})


async def test_update_detail_of_an_alarms_only_change(tmp_path: Path) -> None:
    fake = one_event()
    h = build_fake(tmp_path, fake, config_for([rule()]))
    await h.engine.cycle()
    fake.put_event(
        GCAL,
        google_event(
            "planning",
            "Planning",
            "2026-10-28T10:00:00+08:00",
            "2026-10-28T11:00:00+08:00",
            reminders={"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]},
        ),
    )
    assert (await h.engine.cycle()).rules["family"].updated == 1
    assert changes(h)[-1] == ("updated", {"fields": {"alarms_minutes_before": [[0], [15]]}})


async def test_edited_copies_are_consumed_by_the_revert(tmp_path: Path) -> None:
    target = FakeTarget(mode="sync")
    h = build_fake(tmp_path, one_event(), config_for([rule()]), target=target)
    await h.engine.cycle()
    cal = target.calendars["Family"]
    href, data = next(iter(target.managed(cal).items()))
    target.put(href, data.replace(b"SUMMARY:Planning", b"SUMMARY:Edited"))
    assert (await h.engine.cycle()).rules["family"].reverted == 1
    assert h.engine._edited == {}


@pytest.mark.parametrize("change", ["removed", "retargeted"])
async def test_edited_copies_of_a_removed_or_retargeted_rule_are_dropped(tmp_path: Path, change: str) -> None:
    """Detection read an edit (a dry run: nothing written back); the rule is then removed or
    pointed elsewhere by a reload: what was read for its revert detail goes with it. Another
    rule's entries stay."""
    target = FakeTarget(mode="sync")
    fake = one_event()
    fake.add_calendar("other@group.calendar.google.com", "Other")
    fake.put_event(
        "other@group.calendar.google.com",
        google_event("o1", "O1", "2026-10-29T10:00:00+08:00", "2026-10-29T11:00:00+08:00"),
    )
    rules = [rule(), rule("other", source="Other", target="Other")]
    h = build_fake(tmp_path, fake, config_for(rules), target=target)
    await h.engine.cycle()
    for name in ("Family", "Other"):
        cal = target.calendars[name]
        href, data = next(iter(target.managed(cal).items()))
        target.put(href, data.replace(b"SUMMARY:", b"SUMMARY:Edited "))
    await h.engine.cycle(dry_run=True)
    assert {key[0] for key in h.engine._edited} == {rule_tag("family"), rule_tag("other")}
    if change == "removed":
        h.engine.replace_config(config_for(rules[1:]))
    else:
        h.engine.replace_config(config_for([rule(target="Elsewhere"), rules[1]]))
    assert {key[0] for key in h.engine._edited} == {rule_tag("other")}
