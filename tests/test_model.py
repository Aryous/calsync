import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from calsync import model
from calsync.model import (
    MAX_TIME,
    SPAN_EXPANSION_LIMIT,
    Alarm,
    Attendee,
    FieldPolicy,
    Occurrence,
    Series,
    When,
    Window,
    apply_alarm_policy,
    apply_field_policy,
    compute_span,
    series_hash,
    series_span,
)
from tests.factories import ZONE, at, day, series


def test_when_validation() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        When(datetime(2026, 1, 1), "UTC")
    with pytest.raises(ValueError, match="needs a tzid"):
        When(datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="all-day"):
        When(date(2026, 1, 1), "UTC")
    assert day(2026, 1, 2).all_day
    assert day(2026, 1, 2).utc() == datetime(2026, 1, 2, tzinfo=UTC)
    assert at(2026, 1, 2, 8).utc() == datetime(2026, 1, 2, 0, tzinfo=UTC)


def test_span_single_event() -> None:
    s = series(start=at(2026, 10, 23, 9, 30))
    assert series_span(s) == (
        datetime(2026, 10, 23, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 23, 1, 35, tzinfo=UTC),
    )


def test_span_count_rrule() -> None:
    s = series(recurrence=("RRULE:FREQ=DAILY;COUNT=21",))
    lo, hi = series_span(s)
    assert lo == datetime(2026, 10, 23, 1, 30, tzinfo=UTC)
    assert hi == datetime(2026, 11, 12, 1, 35, tzinfo=UTC)


def test_span_until_rrule() -> None:
    s = series(recurrence=("RRULE:FREQ=DAILY;UNTIL=20261105T235959Z",))
    assert series_span(s)[1] == datetime(2026, 11, 5, 1, 35, tzinfo=UTC)


def test_span_unbounded_and_invalid_rrule() -> None:
    assert series_span(series(recurrence=("RRULE:FREQ=WEEKLY",)))[1] == MAX_TIME
    assert series_span(series(recurrence=("RRULE:FREQ=BOGUS;COUNT=2",)))[1] == MAX_TIME


def test_span_all_day_rrule() -> None:
    s = series(start=day(2026, 10, 23), recurrence=("RRULE:FREQ=DAILY;COUNT=3",))
    s = replace(s, end=day(2026, 10, 24))
    assert series_span(s) == (datetime(2026, 10, 23, tzinfo=UTC), datetime(2026, 10, 26, tzinfo=UTC))


def test_span_extended_by_moved_override() -> None:
    moved = Occurrence(
        original_start=at(2026, 10, 24, 9, 30),
        start=at(2026, 12, 1, 9, 30),
        end=at(2026, 12, 1, 9, 35),
        title="moved",
        description="",
        location="",
        alarms=(),
    )
    s = series(recurrence=("RRULE:FREQ=DAILY;COUNT=2",), overrides=(moved,))
    assert series_span(s)[1] == datetime(2026, 12, 1, 1, 35, tzinfo=UTC)


def test_compute_span_extra_before_start() -> None:
    early = (at(2026, 10, 1, 8), at(2026, 10, 1, 9))
    lo, _ = compute_span(at(2026, 10, 23, 9), at(2026, 10, 23, 10), (), [early])
    assert lo == datetime(2026, 10, 1, 0, tzinfo=UTC)


def test_window_intersects() -> None:
    now = datetime(2026, 10, 24, tzinfo=UTC)
    w = Window.around(now, timedelta(days=7), timedelta(days=180))
    assert w.intersects((now - timedelta(days=30), now - timedelta(days=1)))
    assert not w.intersects((now - timedelta(days=30), now - timedelta(days=8)))
    assert not w.intersects((now + timedelta(days=181), now + timedelta(days=182)))


def test_window_everything_intersects_any_span() -> None:
    w = Window.everything()
    assert w.intersects((datetime(1970, 1, 1, tzinfo=UTC), datetime(1970, 1, 2, tzinfo=UTC)))
    assert w.intersects((datetime(2026, 10, 1, tzinfo=UTC), MAX_TIME))


def test_hash_is_stable_under_normalization() -> None:
    a = series(title="Standup", alarms=(Alarm(timedelta(0)), Alarm(timedelta(minutes=-5))))
    b = series(title="  Standup ", alarms=(Alarm(timedelta(minutes=-5)), Alarm(timedelta(0))))
    assert series_hash(a) == series_hash(b)
    assert len(series_hash(a)) == 32


def test_hash_changes_with_content_and_render_version(monkeypatch: pytest.MonkeyPatch) -> None:
    base = series()
    assert series_hash(base) != series_hash(replace(base, title="Other"))
    assert series_hash(base) != series_hash(replace(base, alarms=()))
    before = series_hash(base)
    monkeypatch.setattr(model, "RENDER_VERSION", model.RENDER_VERSION + 1)
    assert series_hash(base) != before


def test_alarm_policies() -> None:
    occ = Occurrence(
        original_start=at(2026, 10, 24, 9, 30),
        start=at(2026, 10, 24, 21),
        end=at(2026, 10, 24, 21, 5),
        title="late",
        description="",
        location="",
        alarms=(Alarm(timedelta(minutes=-1)),),
    )
    s = series(recurrence=("RRULE:FREQ=DAILY;COUNT=2",), overrides=(occ,))
    assert apply_alarm_policy(s, "copy") is s
    stripped = apply_alarm_policy(s, "strip")
    assert stripped.alarms == () and stripped.overrides[0].alarms == ()
    fixed = apply_alarm_policy(s, (timedelta(minutes=-10),))
    assert fixed.alarms == (Alarm(timedelta(minutes=-10)),)
    assert fixed.overrides[0].alarms == (Alarm(timedelta(minutes=-10)),)


def test_zone_is_preserved() -> None:
    assert at(2026, 10, 23).value.tzinfo == ZONE


def test_span_huge_until_is_bounded_and_fast() -> None:
    s = series(recurrence=("RRULE:FREQ=DAILY;UNTIL=99991230T000000Z",))
    started = time.perf_counter()
    end = series_span(s)[1]
    elapsed = time.perf_counter() - started
    assert end == MAX_TIME
    assert elapsed < 1.0


def test_span_count_over_limit_is_unbounded() -> None:
    over = series(recurrence=(f"RRULE:FREQ=MINUTELY;COUNT={SPAN_EXPANSION_LIMIT + 1}",))
    assert series_span(over)[1] == MAX_TIME
    at_limit = series(recurrence=(f"RRULE:FREQ=MINUTELY;COUNT={SPAN_EXPANSION_LIMIT}",))
    lo, hi = series_span(at_limit)
    assert hi == lo + timedelta(minutes=SPAN_EXPANSION_LIMIT - 1) + timedelta(minutes=5)


def test_span_duration_overflow_is_unbounded() -> None:
    s = series(
        start=at(2026, 12, 31, 23, tz="UTC"),
        minutes=120,
        recurrence=("RRULE:FREQ=YEARLY;UNTIL=99991231T235959Z",),
    )
    assert series_span(s)[1] == MAX_TIME


def test_span_override_conversion_overflow_is_guarded() -> None:
    tz = "America/New_York"
    late = Occurrence(
        original_start=at(2026, 10, 24, 9, 30, tz=tz),
        start=at(9999, 12, 31, 23, tz=tz),
        end=at(9999, 12, 31, 23, 30, tz=tz),
        title="late",
        description="",
        location="",
        alarms=(),
    )
    s = series(
        start=at(2026, 10, 23, 9, 30, tz=tz),
        recurrence=("RRULE:FREQ=DAILY;COUNT=2",),
        overrides=(late,),
    )
    lo, hi = series_span(s)
    assert hi == MAX_TIME
    assert lo == datetime(2026, 10, 23, 13, 30, tzinfo=UTC)


def test_span_count_reaching_year_9999_is_unbounded() -> None:
    s = series(
        start=at(2026, 12, 31, 23, tz="America/New_York"),
        recurrence=("RRULE:FREQ=YEARLY;COUNT=9000",),
    )
    assert series_span(s)[1] == MAX_TIME


def test_attendees_are_normalized_and_hashed() -> None:
    a = replace(
        series(), attendees=(Attendee("Bob", "bob@example.com"), Attendee("Alice", "alice@example.com"))
    )
    b = replace(
        series(), attendees=(Attendee("Alice", "alice@example.com"), Attendee("Bob", "bob@example.com"))
    )
    assert series_hash(a) == series_hash(b)
    assert series_hash(a) != series_hash(series())
    assert series_hash(replace(series(), conference_url="https://meet.example.com/a")) != series_hash(
        series()
    )


def test_attendee_order_and_case_do_not_change_the_hash() -> None:
    a = replace(
        series(),
        attendees=(
            Attendee("A", "Bob@example.com"),
            Attendee("Alice", "alice@example.com"),
            Attendee("A", "bob@example.com"),
        ),
    )
    b = replace(
        series(),
        attendees=(
            Attendee("A", "bob@example.com"),
            Attendee("Alice", "alice@example.com"),
            Attendee("A", "Bob@example.com"),
        ),
    )
    # Case-insensitive duplicates collapse to one entry, chosen deterministically.
    assert model.normalize_series(a).attendees == (
        Attendee("Alice", "alice@example.com"),
        Attendee("A", "Bob@example.com"),
    )
    assert model.normalize_series(b).attendees == model.normalize_series(a).attendees
    assert series_hash(a) == series_hash(b)


def test_named_attendee_wins_over_unnamed_with_same_email() -> None:
    s = replace(
        series(),
        attendees=(
            Attendee("", "carol@example.com"),
            Attendee(" Carol ", "Carol@example.com "),
            Attendee("Dan", ""),
            Attendee(" Dan", ""),
        ),
    )
    assert model.normalize_series(s).attendees == (
        Attendee("Dan", ""),
        Attendee("Carol", "Carol@example.com"),
    )


def test_blank_attendees_are_dropped() -> None:
    s = replace(series(), attendees=(Attendee("   ", ""), Attendee("", " "), Attendee("Eve", "")))
    assert model.normalize_series(s).attendees == (Attendee("Eve", ""),)


def test_attendee_normalization_does_not_depend_on_hash_seed() -> None:
    script = (
        "from calsync.model import Attendee, _normalize_attendees as n\n"
        "print(n((Attendee('A', 'Bob@example.com'), Attendee('A', 'bob@example.com'),"
        " Attendee('B', 'x@example.com'), Attendee('B', 'X@example.com'))))\n"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in range(6)
    }
    assert len(outputs) == 1


def test_exdate_order_does_not_depend_on_hash_seed() -> None:
    """Two EXDATEs for the same instant in different zones tie on the instant; the hash must
    still be the same in every process."""
    script = (
        "from datetime import UTC, datetime\n"
        "from zoneinfo import ZoneInfo\n"
        "from calsync.model import When, series_hash\n"
        "from tests.factories import series\n"
        "a = When(datetime(2026, 10, 30, 9, 30, tzinfo=ZoneInfo('Asia/Shanghai')), 'Asia/Shanghai')\n"
        "b = When(datetime(2026, 10, 30, 1, 30, tzinfo=UTC), 'UTC')\n"
        "c = When(datetime(2026, 10, 30, 1, 30, tzinfo=ZoneInfo('Europe/London')), 'Europe/London')\n"
        "print(series_hash(series(recurrence=('RRULE:FREQ=DAILY;COUNT=10',), exdates=(a, b, c))))\n"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in range(8)
    }
    assert len(outputs) == 1


def test_override_order_with_tied_instants_does_not_change_the_hash() -> None:
    def occ(original: When) -> Occurrence:
        return Occurrence(original, at(2026, 10, 30, 10, 0), at(2026, 10, 30, 10, 5), "Moved", "", "", ())

    local = at(2026, 10, 30, 9, 30)
    utc = When(local.value.astimezone(UTC), "UTC")
    one = series(recurrence=("RRULE:FREQ=DAILY;COUNT=10",), overrides=(occ(local), occ(utc)))
    two = replace(one, overrides=(occ(utc), occ(local)))
    assert series_hash(one) == series_hash(two)


ALICE = Attendee("Alice", "alice@example.com")
BOB = Attendee("", "bob@example.com")


def rich() -> Series:
    occ = Occurrence(
        original_start=at(2026, 10, 30, 9, 30),
        start=at(2026, 10, 30, 10, 0),
        end=at(2026, 10, 30, 10, 5),
        title="Standup moved",
        description="",
        location="Room B",
        alarms=(),
        attendees=(BOB,),
        conference_url="",
    )
    return replace(
        series(recurrence=("RRULE:FREQ=DAILY;COUNT=10",), overrides=(occ,)),
        description="Agenda",
        location="Room A",
        attendees=(ALICE, BOB),
        conference_url="https://meet.example.com/abc",
    )


def test_notes_and_copy_by_default() -> None:
    s = apply_field_policy(rich(), FieldPolicy())
    assert s.description == (
        "Agenda\n\nAttendees: Alice <alice@example.com>, bob@example.com\n\n"
        "Conference: https://meet.example.com/abc"
    )
    assert s.attendees == () and s.conference_url == "https://meet.example.com/abc"
    assert s.overrides[0].description == "Attendees: bob@example.com"
    assert s.overrides[0].attendees == ()


def test_none_drops_attendees_and_conference() -> None:
    s = apply_field_policy(rich(), FieldPolicy(attendees="none", conference="none"))
    assert s.description == "Agenda"
    assert s.attendees == () and s.conference_url == ""


def test_busy_hides_content_but_keeps_time_and_alarms() -> None:
    original = rich()
    s = apply_field_policy(original, FieldPolicy(content="busy"))
    assert (s.title, s.description, s.location, s.conference_url) == ("Busy", "", "", "")
    assert s.start == original.start and s.alarms == original.alarms
    occ = s.overrides[0]
    assert (occ.title, occ.description, occ.location) == ("Busy", "", "")
    assert occ.start == original.overrides[0].start


def test_policy_changes_the_hash() -> None:
    assert series_hash(apply_field_policy(rich(), FieldPolicy())) != series_hash(
        apply_field_policy(rich(), FieldPolicy(attendees="none"))
    )


# ----- expand_instances --------------------------------------------------

BERLIN = "Europe/Berlin"


def _utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def _starts(instances: "list[model.Instance]") -> list[datetime]:
    return [i.start.utc() for i in instances]


def test_expand_daily_keeps_wall_clock_across_dst() -> None:
    # EU clocks go back on 2026-10-25: 09:00 Berlin is 07:00Z before, 08:00Z after.
    s = series(start=at(2026, 10, 23, 9, 0, tz=BERLIN), recurrence=("RRULE:FREQ=DAILY;COUNT=5",))
    got = model.expand_instances(s, _utc(2026, 10, 1), _utc(2026, 11, 1))
    assert [i.start.value.hour for i in got] == [9] * 5
    assert all(i.start.tzid == BERLIN and i.recurring for i in got)
    assert _starts(got) == [
        _utc(2026, 10, 23, 7),
        _utc(2026, 10, 24, 7),
        _utc(2026, 10, 25, 8),
        _utc(2026, 10, 26, 8),
        _utc(2026, 10, 27, 8),
    ]
    assert all(i.end.utc() - i.start.utc() == timedelta(minutes=5) for i in got)


def test_expand_removes_exdates() -> None:
    s = series(
        start=at(2026, 10, 23, 9, 30),
        recurrence=("RRULE:FREQ=DAILY;COUNT=3",),
        exdates=(When(datetime(2026, 10, 24, 1, 30, tzinfo=UTC), "UTC"),),  # same instant, other zone
    )
    got = model.expand_instances(s, _utc(2026, 10, 1), _utc(2026, 11, 1))
    assert _starts(got) == [_utc(2026, 10, 23, 1, 30), _utc(2026, 10, 25, 1, 30)]


def test_expand_override_replaces_its_original() -> None:
    moved = Occurrence(
        original_start=at(2026, 10, 24, 9, 30),
        start=at(2026, 10, 24, 21, 0),
        end=at(2026, 10, 24, 21, 5),
        title="Standup (late)",
        description="",
        location="Room 2",
        alarms=(Alarm(timedelta(minutes=-10)),),
    )
    s = series(start=at(2026, 10, 23, 9, 30), recurrence=("RRULE:FREQ=DAILY;COUNT=3",), overrides=(moved,))
    got = model.expand_instances(s, _utc(2026, 10, 1), _utc(2026, 11, 1))
    assert _starts(got) == [_utc(2026, 10, 23, 1, 30), _utc(2026, 10, 24, 13), _utc(2026, 10, 25, 1, 30)]
    late = got[1]
    assert (late.title, late.location, late.alarms, late.recurring) == (
        "Standup (late)",
        "Room 2",
        (Alarm(timedelta(minutes=-10)),),
        True,
    )
    assert got[0].title == "Standup" and got[0].alarms == (Alarm(timedelta(0)),)


def test_expand_cancelled_instance_is_an_exdate() -> None:
    # The Google mapper turns a cancelled instance into an exdate in the instance's own zone.
    cancelled = at(2026, 10, 24, 9, 30)
    s = series(start=at(2026, 10, 23, 9, 30), recurrence=("RRULE:FREQ=DAILY;COUNT=3",), exdates=(cancelled,))
    got = model.expand_instances(s, _utc(2026, 10, 1), _utc(2026, 11, 1))
    assert cancelled.utc() not in _starts(got) and len(got) == 2


def test_expand_all_day_series() -> None:
    s = series(start=day(2026, 10, 23), recurrence=("RRULE:FREQ=WEEKLY;UNTIL=20261106",))
    s = replace(s, end=day(2026, 10, 24))
    got = model.expand_instances(s, _utc(2026, 10, 1), _utc(2026, 12, 1))
    assert [i.start for i in got] == [day(2026, 10, 23), day(2026, 10, 30), day(2026, 11, 6)]
    assert [i.end for i in got] == [day(2026, 10, 24), day(2026, 10, 31), day(2026, 11, 7)]
    assert all(i.start.all_day for i in got)


def test_expand_all_day_with_a_utc_until() -> None:
    s = series(start=day(2026, 10, 23), recurrence=("RRULE:FREQ=DAILY;UNTIL=20261025T235959Z",))
    got = model.expand_instances(s, _utc(2026, 10, 1), _utc(2026, 12, 1))
    assert [i.start for i in got] == [day(2026, 10, 23), day(2026, 10, 24), day(2026, 10, 25)]


def test_expand_count_and_until() -> None:
    count = series(start=at(2026, 10, 23, 9, 30), recurrence=("RRULE:FREQ=DAILY;COUNT=2",))
    assert len(model.expand_instances(count, _utc(2026, 1, 1), _utc(2027, 1, 1))) == 2
    until = series(start=at(2026, 10, 23, 9, 30), recurrence=("RRULE:FREQ=DAILY;UNTIL=20261025T013000Z",))
    assert _starts(model.expand_instances(until, _utc(2026, 1, 1), _utc(2027, 1, 1))) == [
        _utc(2026, 10, 23, 1, 30),
        _utc(2026, 10, 24, 1, 30),
        _utc(2026, 10, 25, 1, 30),
    ]
    floating = series(start=at(2026, 10, 23, 9, 30), recurrence=("RRULE:FREQ=DAILY;UNTIL=20261024T093000",))
    assert len(model.expand_instances(floating, _utc(2026, 1, 1), _utc(2027, 1, 1))) == 2


def test_expand_rdate_adds_instances_in_their_zone() -> None:
    s = series(
        start=at(2026, 10, 23, 9, 30),
        recurrence=("RRULE:FREQ=DAILY;COUNT=1", "RDATE;TZID=Asia/Shanghai:20261030T093000"),
    )
    got = model.expand_instances(s, _utc(2026, 10, 1), _utc(2026, 11, 1))
    assert _starts(got) == [_utc(2026, 10, 23, 1, 30), _utc(2026, 10, 30, 1, 30)]
    assert got[1].start.tzid == "Asia/Shanghai"


def test_expand_only_returns_instances_in_range() -> None:
    s = series(start=at(2026, 10, 23, 9, 30), minutes=60, recurrence=("RRULE:FREQ=DAILY",))
    # 02:00Z on Oct 25 is inside the instance that runs 01:30Z-02:30Z: it intersects.
    got = model.expand_instances(s, _utc(2026, 10, 25, 2), _utc(2026, 10, 27, 1, 30))
    assert _starts(got) == [_utc(2026, 10, 25, 1, 30), _utc(2026, 10, 26, 1, 30)]


def test_expand_finds_instances_of_an_old_series() -> None:
    s = series(start=at(2020, 1, 1, 9, 30), recurrence=("RRULE:FREQ=DAILY",))
    got = model.expand_instances(s, _utc(2026, 10, 24), _utc(2026, 10, 26), limit=10)
    assert _starts(got) == [_utc(2026, 10, 24, 1, 30), _utc(2026, 10, 25, 1, 30)]


def test_expand_unparseable_rrule_yields_the_master_only() -> None:
    s = series(start=at(2026, 10, 23, 9, 30), recurrence=("RRULE:FREQ=BOGUS;COUNT=2",))
    got = model.expand_instances(s, _utc(2026, 10, 1), _utc(2026, 11, 1))
    assert _starts(got) == [_utc(2026, 10, 23, 1, 30)]
    assert model.expand_instances(s, _utc(2026, 11, 1), _utc(2026, 12, 1)) == []


def test_expand_respects_the_limit() -> None:
    s = series(start=at(2026, 10, 23, 9, 30), recurrence=("RRULE:FREQ=MINUTELY",))
    got = model.expand_instances(s, _utc(2026, 10, 1), _utc(2027, 1, 1), limit=7)
    assert len(got) == 7


def test_expand_bounds_the_iterations_of_a_dense_old_series() -> None:
    import time

    # A secondly series started a year before the range: ~31.5 million occurrences before it.
    s = series(start=at(2025, 10, 23, 9, 30), recurrence=("RRULE:FREQ=SECONDLY",))
    began = time.monotonic()
    got = model.expand(s, _utc(2026, 10, 24), _utc(2026, 10, 25))
    assert time.monotonic() - began < 5
    assert got.truncated and got.instances == []


def test_expand_finds_instances_within_the_iteration_bound() -> None:
    s = series(start=at(2026, 10, 23, 9, 30), recurrence=("RRULE:FREQ=MINUTELY",))
    got = model.expand(s, _utc(2026, 10, 24), _utc(2026, 10, 24, 0, 10))  # 1350 minutes later
    assert not got.truncated and _utc(2026, 10, 24, 0, 9) in _starts(got.instances)


def test_expand_non_recurring_yields_itself() -> None:
    s = series(start=at(2026, 10, 23, 9, 30))
    (only,) = model.expand_instances(s, _utc(2026, 10, 23), _utc(2026, 10, 24))
    assert only == model.Instance(s.start, s.end, "Standup", "", (Alarm(timedelta(0)),), False)
    assert model.expand_instances(s, _utc(2026, 10, 24), _utc(2026, 10, 25)) == []


# ----- all-day instances on the range's own days; truncation ---------------------------

PLUS8 = timezone(timedelta(hours=8))
MINUS5 = timezone(timedelta(hours=-5))


def _all_day(first: date, last: date, *, weekly: bool = False) -> Series:
    """An all-day event from ``first`` to ``last`` (inclusive); ``weekly``: the second of three
    weekly instances (the series starts a week earlier)."""
    span = (last - first).days + 1
    if weekly:
        first -= timedelta(days=7)
    s = series(start=When(first), recurrence=("RRULE:FREQ=WEEKLY;COUNT=3",) if weekly else ())
    return replace(s, end=When(first + timedelta(days=span)))


@pytest.mark.parametrize("weekly", [False, True], ids=["single", "recurring"])
@pytest.mark.parametrize("zone", [PLUS8, MINUS5], ids=["utc+8", "utc-5"])
def test_all_day_events_fall_on_the_ranges_own_days(zone: timezone, weekly: bool) -> None:
    # A day view of 28 October in the viewer's zone: midnight to midnight there.
    lo = datetime(2026, 10, 28, tzinfo=zone)
    hi = lo + timedelta(days=1)
    shown = {
        d: [i.start.value for i in model.expand_instances(_all_day(d, d, weekly=weekly), lo, hi)]
        for d in (date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29))
    }
    assert shown == {date(2026, 10, 27): [], date(2026, 10, 28): [date(2026, 10, 28)], date(2026, 10, 29): []}


@pytest.mark.parametrize("zone", [PLUS8, MINUS5], ids=["utc+8", "utc-5"])
def test_a_multi_day_all_day_event_spanning_the_range(zone: timezone) -> None:
    lo = datetime(2026, 10, 28, tzinfo=zone)
    hi = lo + timedelta(days=1)

    def shown(first: date, last: date) -> bool:
        return bool(model.expand_instances(_all_day(first, last), lo, hi))

    assert shown(date(2026, 10, 26), date(2026, 10, 30))  # covers the day
    assert shown(date(2026, 10, 27), date(2026, 10, 28))  # ends on it
    assert shown(date(2026, 10, 28), date(2026, 10, 29))  # starts on it
    assert not shown(date(2026, 10, 25), date(2026, 10, 27))  # over the day before
    assert not shown(date(2026, 10, 29), date(2026, 10, 31))  # from the day after


def test_all_day_events_on_a_range_between_midnights() -> None:
    # 07:00 on the 28th to 07:00 on the 29th (+08:00) touches both days there; in UTC it starts
    # on the 27th.
    lo = datetime(2026, 10, 28, 7, tzinfo=PLUS8)
    hi = lo + timedelta(days=1)
    days = [date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)]
    assert [bool(model.expand_instances(_all_day(d, d), lo, hi)) for d in days] == [False, True, True, False]


def test_all_day_ranges_in_utc_are_unchanged() -> None:
    lo, hi = _utc(2026, 10, 28), _utc(2026, 10, 29)
    days = [date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)]
    assert [bool(model.expand_instances(_all_day(d, d, weekly=True), lo, hi)) for d in days] == [
        False,
        True,
        False,
    ]


def test_expansion_reports_hitting_the_limit() -> None:
    hourly = series(start=at(2026, 10, 1, 9, 0), recurrence=("RRULE:FREQ=HOURLY",))
    lo = _utc(2026, 10, 1)
    cut = model.expand(hourly, lo, lo + timedelta(days=62))
    assert len(cut.instances) == 1000 and cut.truncated
    daily = series(start=at(2026, 10, 1, 9, 0), recurrence=("RRULE:FREQ=DAILY",))
    whole = model.expand(daily, lo, lo + timedelta(days=62))
    assert len(whole.instances) == 62 and not whole.truncated
    # Exactly the limit inside the range is not a cut.
    exact = model.expand(daily, lo, _utc(2026, 10, 8), limit=7)
    assert len(exact.instances) == 7 and not exact.truncated
    assert model.expand(daily, lo, _utc(2026, 10, 9), limit=7).truncated
    assert model.expand_instances(hourly, lo, lo + timedelta(days=62)) == cut.instances
