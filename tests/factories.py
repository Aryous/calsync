"""Builders for synthetic test data. Never put real personal data in tests."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from calsync.model import Alarm, Occurrence, Series, When

TZ = "Asia/Shanghai"
ZONE = ZoneInfo(TZ)


def at(y: int, mo: int, d: int, h: int = 0, mi: int = 0, tz: str = TZ) -> When:
    return When(datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)), tz)


def day(y: int, mo: int, d: int) -> When:
    return When(date(y, mo, d))


def series(
    source_id: str = "evt1",
    title: str = "Standup",
    start: When | None = None,
    minutes: int = 5,
    recurrence: tuple[str, ...] = (),
    exdates: tuple[When, ...] = (),
    overrides: tuple[Occurrence, ...] = (),
    alarms: tuple[Alarm, ...] = (Alarm(timedelta(0)),),
) -> Series:
    start = start or at(2026, 10, 23, 9, 30)
    end = When(start.value + timedelta(minutes=minutes), start.tzid) if not start.all_day else start
    return Series(
        source_id=source_id,
        title=title,
        description="",
        location="",
        start=start,
        end=end,
        recurrence=recurrence,
        exdates=exdates,
        overrides=overrides,
        alarms=alarms,
    )


def google_event(
    event_id: str,
    summary: str,
    start: str,
    end: str,
    *,
    tz: str = TZ,
    recurrence: list[str] | None = None,
    reminders: dict | None = None,
    **extra: object,
) -> dict:
    event: dict = {
        "id": event_id,
        "status": "confirmed",
        "summary": summary,
        "start": {"dateTime": start, "timeZone": tz},
        "end": {"dateTime": end, "timeZone": tz},
        "reminders": reminders or {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
    }
    if recurrence:
        event["recurrence"] = recurrence
    event.update(extra)
    return event
