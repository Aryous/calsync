import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from calsync.model import Alarm, Attendee, Occurrence, Series, series_hash, series_span
from calsync.naming import object_id, parse_resource_name, resource_name, rule_tag, uid_for
from calsync.render import content_digest, inspect, render
from tests.factories import at, day, series

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def test_naming_is_deterministic_and_parseable() -> None:
    oid = object_id("family", "cal@group.calendar.google.com", "evt1")
    assert oid == object_id("family", "cal@group.calendar.google.com", "evt1")
    assert oid != object_id("other", "cal@group.calendar.google.com", "evt1")
    assert len(oid) == 24 and len(rule_tag("family")) == 8
    name = resource_name("family", oid)
    assert name == f"calsync-{rule_tag('family')}-{oid}.ics"
    assert parse_resource_name(f"/1234/calendars/home/{name}") == (rule_tag("family"), oid)
    assert parse_resource_name("/1234/calendars/home/ABC-DEF.ics") is None
    assert uid_for(oid) == f"{oid}@calsync"


def _recurring():
    occ = Occurrence(
        original_start=at(2026, 10, 24, 9, 30),
        start=at(2026, 10, 24, 21),
        end=at(2026, 10, 24, 21, 5),
        title="Standup (late)",
        description="",
        location="",
        alarms=(Alarm(timedelta(minutes=-5)),),
    )
    return series(
        recurrence=("RRULE:FREQ=DAILY;COUNT=21",),
        exdates=(at(2026, 10, 30, 9, 30),),
        overrides=(occ,),
    )


def test_render_recurring_series() -> None:
    s = _recurring()
    ics = render(s, uid="abc@calsync", rule_name="family", content_hash="h1", now=NOW).decode()
    assert "BEGIN:VTIMEZONE" in ics and "TZID:Asia/Shanghai" in ics
    assert "RRULE:FREQ=DAILY;COUNT=21" in ics
    assert "EXDATE;TZID=Asia/Shanghai:20261030T093000" in ics
    assert "RECURRENCE-ID;TZID=Asia/Shanghai:20261024T093000" in ics
    assert ics.count("BEGIN:VALARM") == 2  # master + override, each with its own alarm
    assert "TRIGGER:-PT5M" in ics and "TRIGGER:P0D" in ics
    assert ics.count("X-CALSYNC-RULE:family") == 2
    assert "ATTENDEE" not in ics and "ORGANIZER" not in ics


def test_render_all_day_and_no_alarms() -> None:
    s = series(start=day(2026, 11, 5), alarms=())
    ics = render(s, uid="d@calsync", rule_name="r", content_hash="h", now=NOW).decode()
    assert "DTSTART;VALUE=DATE:20261105" in ics
    assert "VALARM" not in ics


def test_inspect_roundtrip() -> None:
    s = _recurring()
    h = series_hash(s)
    found = inspect(render(s, uid="abc@calsync", rule_name="family", content_hash=h, now=NOW))
    assert found is not None
    assert (found.uid, found.rule, found.content_hash) == ("abc@calsync", "family", h)
    assert found.span == series_span(s)
    assert found.summary == "Standup"  # the master's, not the override's "Standup (late)"


def test_inspect_foreign_and_garbage() -> None:
    foreign = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:f1\r\n"
        b"DTSTAMP:20261024T000000Z\r\nDTSTART:20261025T010000Z\r\nDTEND:20261025T020000Z\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    found = inspect(foreign)
    assert found is not None and found.rule is None and found.content_hash is None
    assert found.summary is None
    assert found.span == (datetime(2026, 10, 25, 1, tzinfo=UTC), datetime(2026, 10, 25, 2, tzinfo=UTC))
    assert inspect(b"not ical") is None


def _body(*lines: str) -> bytes:
    head = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\nUID:f1\r\nDTSTAMP:20261024T000000Z\r\n"
    )
    return (head + "".join(f"{line}\r\n" for line in lines) + "END:VEVENT\r\nEND:VCALENDAR\r\n").encode()


def test_inspect_malformed_dates_give_no_span() -> None:
    bad = [
        ("DTSTART:garbage", "DTEND:20261025T020000Z"),
        ("DTSTART:20261025T010000Z", "DTEND:xx"),
        ("DTSTART;VALUE=PERIOD:20261025T010000Z/PT1H", "DTEND:20261025T020000Z"),
    ]
    for lines in bad:
        found = inspect(_body(*lines))
        assert found is not None, lines
        assert found.uid == "f1" and found.span is None, lines


def test_inspect_duplicate_markers_use_first() -> None:
    found = inspect(
        _body(
            "DTSTART:20261025T010000Z",
            "DTEND:20261025T020000Z",
            "X-CALSYNC-HASH:first",
            "X-CALSYNC-HASH:second",
            "X-CALSYNC-RULE:r1",
            "X-CALSYNC-RULE:r2",
        )
    )
    assert found is not None
    assert (found.content_hash, found.rule) == ("first", "r1")


def test_inspect_duration_without_dtend() -> None:
    found = inspect(_body("DTSTART:20261025T010000Z", "DURATION:PT2H"))
    assert found is not None
    assert found.span == (datetime(2026, 10, 25, 1, tzinfo=UTC), datetime(2026, 10, 25, 3, tzinfo=UTC))


def test_render_rdate() -> None:
    s = series(recurrence=("RRULE:FREQ=DAILY;COUNT=2", "RDATE;TZID=Asia/Shanghai:20261201T093000"))
    ics = render(s, uid="r@calsync", rule_name="r", content_hash="h", now=NOW).decode()
    assert "RDATE;TZID=Asia/Shanghai:20261201T093000" in ics


def test_inspect_span_overflow_gives_no_span() -> None:
    """Untrusted bytes: a DURATION past year 9999 overflows; inspect must not raise."""
    found = inspect(_body("DTSTART:99991231T230000Z", "DURATION:PT2H"))
    assert found is not None
    assert found.uid == "f1" and found.span is None


@pytest.mark.parametrize("exc", [IndexError("parser bug"), RecursionError(), OverflowError(), TypeError()])
def test_inspect_never_raises_from_the_parser(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    from calsync import render as render_mod

    def broken(_: bytes) -> object:
        raise exc

    monkeypatch.setattr(render_mod.Calendar, "from_ical", staticmethod(broken))
    assert inspect(_body("DTSTART:20261025T010000Z")) is None


def test_url_is_rendered_and_attendees_never_are() -> None:
    s = replace(
        series(),
        conference_url="https://meet.example.com/abc",
        attendees=(Attendee("Alice", "alice@example.com"),),
    )
    data = render(s, uid="u@calsync", rule_name="r", content_hash="h", now=NOW)
    assert b"URL:https://meet.example.com/abc" in data
    assert b"ATTENDEE" not in data and b"ORGANIZER" not in data


def body(s: Series, *, now: datetime = NOW, content_hash: str = "h") -> bytes:
    return render(s, uid="u@calsync", rule_name="r", content_hash=content_hash, now=now)


def daily() -> Series:
    occ = Occurrence(
        original_start=at(2026, 10, 25, 9, 30),
        start=at(2026, 10, 25, 8, 0),
        end=at(2026, 10, 25, 8, 5),
        title="Reminder 1/4",
        description="",
        location="",
        alarms=(Alarm(timedelta(0)),),
    )
    return replace(series(recurrence=("RRULE:FREQ=DAILY;COUNT=21",), overrides=(occ,)), description="Note")


def test_digest_ignores_stamp_marker_and_property_order() -> None:
    a = body(daily())
    b = body(daily(), now=NOW + timedelta(days=3), content_hash="other")
    assert content_digest(a) == content_digest(b) is not None


def test_digest_ignores_server_and_phone_additions() -> None:
    original = body(daily())
    noisy = original.replace(
        b"BEGIN:VALARM\r\n",
        b"BEGIN:VALARM\r\nACKNOWLEDGED:20261025T000500Z\r\nX-WR-ALARMUID:abc\r\nUID:abc\r\n",
    ).replace(
        b"END:VEVENT\r\n",
        b"SEQUENCE:3\r\nCREATED:20261024T000000Z\r\nLAST-MODIFIED:20261024T000000Z\r\n"
        b"X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC\r\n"
        b"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder\r\n"
        b"TRIGGER;VALUE=DATE-TIME:20261025T001500Z\r\nRELATED-TO:abc\r\nEND:VALARM\r\n"
        b"BEGIN:VALARM\r\nACTION:NONE\r\nTRIGGER;VALUE=DATE-TIME:19760401T005545Z\r\n"
        b"X-APPLE-DEFAULT-ALARM:TRUE\r\nEND:VALARM\r\n"
        b"END:VEVENT\r\n",
    )
    assert noisy != original
    assert content_digest(noisy) == content_digest(original) is not None


@pytest.mark.parametrize(
    "change",
    [
        lambda s: replace(s, title="Other"),
        lambda s: replace(s, start=at(2026, 10, 23, 10, 0), end=at(2026, 10, 23, 10, 5)),
        lambda s: replace(s, alarms=(Alarm(timedelta(minutes=-5)),)),
        lambda s: replace(s, alarms=()),
        lambda s: replace(s, exdates=(at(2026, 10, 27, 9, 30),)),
        lambda s: replace(s, overrides=(replace(s.overrides[0], start=at(2026, 10, 25, 7, 0)),)),
        lambda s: replace(s, conference_url="https://meet.example.com/x"),
        lambda s: replace(s, description="Changed"),
    ],
)
def test_digest_sees_what_people_see(change: Callable[[Series], Series]) -> None:
    assert content_digest(body(change(daily()))) != content_digest(body(daily()))


def test_digest_of_garbage_is_none() -> None:
    assert content_digest(b"not a calendar") is None
    assert content_digest(b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n") is None


def test_digest_all_day() -> None:
    a = body(series(start=day(2026, 11, 5)))
    assert content_digest(a) is not None
    assert content_digest(a) == content_digest(
        body(series(start=day(2026, 11, 5)), now=NOW + timedelta(days=1))
    )
    assert content_digest(a) != content_digest(body(series(start=day(2026, 11, 6))))
    midnight = series(start=at(2026, 11, 5, tz="UTC"), minutes=0)
    assert content_digest(a) != content_digest(body(midnight))


def test_digest_duration_equals_dtend() -> None:
    original = body(series())
    assert b"DTEND;TZID=Asia/Shanghai:20261023T093500\r\n" in original
    with_duration = original.replace(b"DTEND;TZID=Asia/Shanghai:20261023T093500\r\n", b"DURATION:PT5M\r\n")
    assert content_digest(with_duration) == content_digest(original)
    longer = original.replace(b"DTEND;TZID=Asia/Shanghai:20261023T093500\r\n", b"DURATION:PT10M\r\n")
    assert content_digest(longer) != content_digest(original)


def test_digest_ignores_missing_vtimezone_for_iana_tzid() -> None:
    original = body(daily())
    stripped = re.sub(rb"BEGIN:VTIMEZONE\r\n.*?END:VTIMEZONE\r\n", b"", original, flags=re.DOTALL)
    assert b"VTIMEZONE" not in stripped and b"TZID=Asia/Shanghai" in stripped
    assert content_digest(stripped) == content_digest(original)


@pytest.mark.parametrize("exc", [IndexError("parser bug"), RecursionError(), OverflowError(), TypeError()])
def test_digest_never_raises_from_the_parser(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    from calsync import render as render_mod

    def broken(_: bytes) -> object:
        raise exc

    monkeypatch.setattr(render_mod.Calendar, "from_ical", staticmethod(broken))
    assert content_digest(body(daily())) is None


def test_digest_of_malformed_event_is_none() -> None:
    assert content_digest(_body("DTSTART:garbage")) is None
    assert content_digest(_body("SUMMARY:no start")) is None
    assert content_digest(_body("DTSTART:99991231T230000Z", "DURATION:PT2H")) is None
