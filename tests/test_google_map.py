import logging
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from calsync.model import Alarm, When
from calsync.sources.base import CalendarMeta
from calsync.sources.google_map import build_series, html_to_text, master_ids, parse_when
from tests.factories import TZ, ZONE, google_event

META = CalendarMeta(timezone=TZ, default_alarms=(Alarm(timedelta(minutes=-10)),))


def test_html_to_text() -> None:
    assert html_to_text("plain text") == "plain text"
    assert html_to_text("<p>Step one\nStep two</p>") == "Step one\nStep two"
    assert html_to_text("a<br>b<br/>c &amp; d") == "a\nb\nc & d"
    assert html_to_text("<ul><li>x</li><li>y</li></ul>") == "x\ny"


def test_parse_when() -> None:
    w = parse_when({"dateTime": "2026-10-23T22:10:00+08:00", "timeZone": TZ}, "UTC")
    assert w == When(datetime(2026, 10, 23, 22, 10, tzinfo=ZONE), TZ)
    utc = parse_when({"dateTime": "2026-10-23T14:10:00Z"}, TZ)
    assert utc.tzid == TZ and utc.value.hour == 22
    assert parse_when({"date": "2026-10-23"}, TZ) == When(date(2026, 10, 23))


def test_single_event_with_reminder_override() -> None:
    raw = google_event(
        "e1",
        "Review",
        "2026-10-23T09:30:00+08:00",
        "2026-10-23T10:00:00+08:00",
        description="<p>Bring card</p>",
        transparency="transparent",
        attendees=[{"email": "someone@example.com"}],
    )
    (s,) = build_series([raw], META)
    assert s.source_id == "e1"
    assert s.description == "Bring card"
    assert s.alarms == (Alarm(timedelta(0)),)
    assert s.transparent is True


def test_default_and_email_reminders() -> None:
    uses_default = google_event(
        "e1", "A", "2026-10-23T09:00:00+08:00", "2026-10-23T09:05:00+08:00", reminders={"useDefault": True}
    )
    email_only = google_event(
        "e2",
        "B",
        "2026-10-23T09:00:00+08:00",
        "2026-10-23T09:05:00+08:00",
        reminders={"useDefault": False, "overrides": [{"method": "email", "minutes": 30}]},
    )
    by_id = {s.source_id: s for s in build_series([uses_default, email_only], META)}
    assert by_id["e1"].alarms == (Alarm(timedelta(minutes=-10)),)
    assert by_id["e2"].alarms == ()


def test_recurring_series_with_modified_and_cancelled_instances() -> None:
    master = google_event(
        "m1",
        "Standup",
        "2026-10-23T09:30:00+08:00",
        "2026-10-23T09:35:00+08:00",
        recurrence=["RRULE:FREQ=DAILY;COUNT=21", "EXDATE;TZID=Asia/Shanghai:20261030T093000"],
    )
    modified = google_event(
        "m1_20261024T013000Z",
        "Standup 2/21",
        "2026-10-24T21:00:00+08:00",
        "2026-10-24T21:05:00+08:00",
        recurringEventId="m1",
        originalStartTime={"dateTime": "2026-10-24T09:30:00+08:00", "timeZone": TZ},
    )
    cancelled = {
        "id": "m1_20261025T013000Z",
        "status": "cancelled",
        "recurringEventId": "m1",
        "originalStartTime": {"dateTime": "2026-10-25T09:30:00+08:00", "timeZone": TZ},
    }
    (s,) = build_series([modified, master, cancelled], META)
    assert s.recurrence == ("RRULE:FREQ=DAILY;COUNT=21",)
    assert [w.value.day for w in s.exdates] == [25, 30]
    (occ,) = s.overrides
    assert occ.title == "Standup 2/21"
    assert occ.original_start.value == datetime(2026, 10, 24, 9, 30, tzinfo=ZONE)
    assert occ.start.value.hour == 21
    assert occ.alarms == (Alarm(timedelta(0)),)


def test_cancelled_and_orphan_entries_are_dropped() -> None:
    gone = {"id": "x", "status": "cancelled"}
    orphan = google_event(
        "o_1",
        "Orphan",
        "2026-10-24T09:00:00+08:00",
        "2026-10-24T09:05:00+08:00",
        recurringEventId="missing",
        originalStartTime={"dateTime": "2026-10-24T09:00:00+08:00"},
    )
    broken = {"id": "b", "status": "confirmed", "summary": "no start"}
    assert build_series([gone, orphan, broken], META) == []


def test_all_day_event() -> None:
    raw = {
        "id": "d1",
        "status": "confirmed",
        "summary": "Checkup",
        "start": {"date": "2026-11-05"},
        "end": {"date": "2026-11-06"},
    }
    (s,) = build_series([raw], META)
    assert s.start == When(date(2026, 11, 5)) and s.end == When(date(2026, 11, 6))
    assert s.alarms == META.default_alarms


def test_malformed_instance_does_not_drop_series(caplog: pytest.LogCaptureFixture) -> None:
    master = google_event(
        "m1",
        "Standup",
        "2026-10-23T09:30:00+08:00",
        "2026-10-23T09:35:00+08:00",
        recurrence=["RRULE:FREQ=DAILY;COUNT=3"],
    )
    good = google_event(
        "m1_20261024T013000Z",
        "Moved",
        "2026-10-24T21:00:00+08:00",
        "2026-10-24T21:05:00+08:00",
        recurringEventId="m1",
        originalStartTime={"dateTime": "2026-10-24T09:30:00+08:00", "timeZone": TZ},
    )
    bad = {
        "id": "m1_20261025T013000Z",
        "status": "confirmed",
        "recurringEventId": "m1",
        "originalStartTime": {"dateTime": "2026-10-25T09:30:00+08:00", "timeZone": TZ},
    }
    with caplog.at_level(logging.DEBUG):
        (s,) = build_series([master, good, bad], META)
    assert len(s.overrides) == 1
    assert any(
        r.levelno == logging.WARNING and "malformed instance" in r.getMessage() for r in caplog.records
    )


def _orphan(event_id: str, cancelled: bool) -> dict[str, Any]:
    if cancelled:
        return {
            "id": event_id,
            "status": "cancelled",
            "recurringEventId": "missing",
            "originalStartTime": {"dateTime": "2026-10-24T09:00:00+08:00"},
        }
    return google_event(
        event_id,
        "Orphan",
        "2026-10-24T09:00:00+08:00",
        "2026-10-24T09:05:00+08:00",
        recurringEventId="missing",
        originalStartTime={"dateTime": "2026-10-24T09:00:00+08:00"},
    )


def test_orphan_log_levels(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        assert build_series([_orphan("o_1", False)], META) == []
    assert [r.levelno for r in caplog.records if "missing" in r.getMessage()] == [logging.WARNING]
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert build_series([_orphan("o_2", True)], META) == []
    assert [r.levelno for r in caplog.records if "missing" in r.getMessage()] == [logging.DEBUG]


def test_html_to_text_leaves_non_html_alone() -> None:
    assert html_to_text("x<y and z") == "x<y and z"
    assert html_to_text("a & b") == "a & b"


def test_html_to_text_links() -> None:
    assert html_to_text('<a href="https://example.com/x">notes</a>') == "notes (https://example.com/x)"
    assert html_to_text('<a href="https://e.com">https://e.com</a>') == "https://e.com"


def test_html_to_text_nbsp() -> None:
    assert html_to_text("<p>a&nbsp;b</p>") == "a b"
    assert "\xa0" not in html_to_text("<p>a\xa0b</p>")


def test_entries_without_id_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    no_id = {
        "status": "confirmed",
        "summary": "x",
        "start": {"date": "2026-11-05"},
        "end": {"date": "2026-11-06"},
    }
    ok = google_event("e1", "A", "2026-10-23T09:00:00+08:00", "2026-10-23T09:05:00+08:00")
    with caplog.at_level(logging.WARNING):
        result = build_series([no_id, ok], META)
    assert [s.source_id for s in result] == ["e1"]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_master_ids_covers_parseable_and_malformed_masters() -> None:
    good = google_event("good", "Good", "2026-10-25T09:00:00+08:00", "2026-10-25T09:05:00+08:00")
    broken = {k: v for k, v in good.items() if k != "start"} | {"id": "broken"}
    raw = [
        good,
        broken,
        {"id": "gone", "status": "cancelled"},
        {"id": "good_1", "recurringEventId": "good", "status": "confirmed"},
        {"summary": "no id"},
    ]
    assert master_ids(raw) == {"good", "broken"}
    assert {s.source_id for s in build_series(raw, META)} == {"good"}
