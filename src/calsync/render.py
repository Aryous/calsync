"""Render a Series to iCalendar bytes, and inspect bytes we (may have) written."""

from dataclasses import dataclass
from datetime import UTC, datetime

from icalendar import Alarm as IcalAlarm
from icalendar import Calendar, Component, Event, vRecur

from calsync.icalutil import parse_date_line, when_from_ical
from calsync.model import Alarm, Series, When, compute_span

PRODID = "-//calsync//calsync//EN"


def _event(
    uid: str,
    start: When,
    end: When,
    title: str,
    description: str,
    location: str,
    url: str,
    alarms: tuple[Alarm, ...],
    transparent: bool,
    rule_name: str,
    content_hash: str,
    now: datetime,
) -> Event:
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", now.astimezone(UTC))
    event.add("dtstart", start.value)
    event.add("dtend", end.value)
    event.add("summary", title)
    if description:
        event.add("description", description)
    if location:
        event.add("location", location)
    if url:
        event.add("url", url)
    event.add("transp", "TRANSPARENT" if transparent else "OPAQUE")
    event.add("x-calsync-rule", rule_name)
    event.add("x-calsync-hash", content_hash)
    for alarm in alarms:
        valarm = IcalAlarm()
        valarm.add("action", "DISPLAY")
        valarm.add("description", title or "Reminder")
        valarm.add("trigger", alarm.offset)
        event.add_component(valarm)
    return event


def render(series: Series, *, uid: str, rule_name: str, content_hash: str, now: datetime) -> bytes:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    master = _event(
        uid,
        series.start,
        series.end,
        series.title,
        series.description,
        series.location,
        series.conference_url,
        series.alarms,
        series.transparent,
        rule_name,
        content_hash,
        now,
    )
    default_tz = series.start.tzid or "UTC"
    for line in series.recurrence:
        name, _, value = line.partition(":")
        head = name.split(";", 1)[0].upper()
        if head == "RRULE":
            master.add("rrule", vRecur.from_ical(value))
        elif head == "RDATE":
            for when in parse_date_line(line, default_tz):
                master.add("rdate", when.value)
    for when in series.exdates:
        master.add("exdate", when.value)
    cal.add_component(master)
    for occ in series.overrides:
        event = _event(
            uid,
            occ.start,
            occ.end,
            occ.title,
            occ.description,
            occ.location,
            occ.conference_url,
            occ.alarms,
            series.transparent,
            rule_name,
            content_hash,
            now,
        )
        event.add("recurrence-id", occ.original_start.value)
        cal.add_component(event)
    cal.add_missing_timezones()
    return cal.to_ical()


@dataclass(frozen=True, slots=True)
class Inspected:
    uid: str
    rule: str | None
    content_hash: str | None
    span: tuple[datetime, datetime] | None
    summary: str | None = None  # the master VEVENT's SUMMARY


def _rrule_lines(component: Component) -> list[str]:
    value = component.get("rrule")
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [f"RRULE:{v.to_ical().decode()}" for v in values]


def _optional(component: Component, name: str) -> str | None:
    value = component.get(name)
    if isinstance(value, list):
        value = value[0] if value else None
    return None if value is None else str(value)


def inspect(data: bytes) -> Inspected | None:
    """Read identity, hash and span from a resource body; None if it is not parseable.

    The body comes from the target server and may be anything, so this never raises."""
    try:
        cal = Calendar.from_ical(data)
        events = list(cal.walk("VEVENT"))
    except Exception:  # untrusted bytes: any parser failure means "not ours to read"
        return None
    if not events:
        return None
    master = next((e for e in events if e.get("recurrence-id") is None), events[0])
    span: tuple[datetime, datetime] | None
    try:
        start = when_from_ical(master.decoded("dtstart"))
        if "dtend" in master:
            end = when_from_ical(master.decoded("dtend"))
        elif "duration" in master:
            end = when_from_ical(start.value + master.decoded("duration"))
        else:
            end = start
        extra = [
            (when_from_ical(e.decoded("dtstart")), when_from_ical(e.decoded("dtend")))
            for e in events
            if e is not master and "dtstart" in e and "dtend" in e
        ]
        span = compute_span(start, end, _rrule_lines(master), extra)
    except Exception:  # malformed or out-of-range dates (OverflowError included)
        span = None
    return Inspected(
        uid=str(master.get("uid", "")),
        rule=_optional(master, "x-calsync-rule"),
        content_hash=_optional(master, "x-calsync-hash"),
        span=span,
        summary=_optional(master, "summary"),
    )
