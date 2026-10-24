import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

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
