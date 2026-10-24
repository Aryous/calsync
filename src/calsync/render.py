"""Render a Series to iCalendar bytes, and inspect bytes we (may have) written."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

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


def _when_key(value: Any) -> list[Any]:
    """DTSTART/DTEND: the zone stays in the key, because it drives how the RRULE
    expands across DST changes."""
    when = when_from_ical(value)
    return [when.utc().isoformat(), when.tzid, when.all_day]


def _instant_key(value: Any) -> list[Any]:
    """RECURRENCE-ID/EXDATE/RDATE: only the instant counts. A client may write the
    same instant in UTC or another zone, and a person sees no difference."""
    when = when_from_ical(value)
    return [when.utc().isoformat(), when.all_day]


def _dates(component: Component, name: str) -> list[list[Any]]:
    prop = component.get(name)
    if prop is None:
        return []
    props = prop if isinstance(prop, list) else [prop]
    return sorted(_instant_key(item.dt) for p in props for item in p.dts)


def _alarm_keys(event: Component) -> list[list[Any]]:
    """Relative alarms only. Snoozes (RELATED-TO, absolute trigger) and client
    default alarms are a person's own bookkeeping, not content."""
    keys: list[list[Any]] = []
    for alarm in event.subcomponents:
        if alarm.name != "VALARM" or "related-to" in alarm:
            continue
        if str(alarm.get("x-apple-default-alarm", "")).upper() == "TRUE":
            continue
        trigger = alarm.get("trigger")
        if trigger is None:
            continue
        offset = alarm.decoded("trigger")
        if not isinstance(offset, timedelta):
            continue
        related = str(trigger.params.get("RELATED", "START")).upper()
        keys.append([int(offset.total_seconds()), related])
    return sorted(keys)


def _event_key(event: Component) -> dict[str, Any]:
    start = event.decoded("dtstart")
    if "dtend" in event:
        end: Any = event.decoded("dtend")
    elif "duration" in event:
        end = start + event.decoded("duration")
    else:
        end = start
    rid = event.decoded("recurrence-id") if "recurrence-id" in event else None
    return {
        "rid": None if rid is None else _instant_key(rid),
        "start": _when_key(start),
        "end": _when_key(end),
        "summary": str(event.get("summary", "")).strip(),
        "description": str(event.get("description", "")).strip(),
        "location": str(event.get("location", "")).strip(),
        "url": str(event.get("url", "")).strip(),
        "transp": str(event.get("transp", "OPAQUE")).upper(),
        "rrule": sorted(_rrule_lines(event)),
        "rdate": _dates(event, "rdate"),
        "exdate": _dates(event, "exdate"),
        "alarms": _alarm_keys(event),
    }


def content_digest(data: bytes) -> str | None:
    """Digest of what a person sees in the events of a resource body.

    Ignores what servers and phones add on their own: DTSTAMP, SEQUENCE,
    CREATED/LAST-MODIFIED, X- properties, property and event order, VTIMEZONE
    text, the zone label on RECURRENCE-ID/EXDATE/RDATE, and alarm bookkeeping. None if the body holds no readable event. Never raises:
    the body comes from the target server and may be anything."""
    try:
        events = list(Calendar.from_ical(data).walk("VEVENT"))
        if not events:
            return None
        # total order on the whole key: duplicate RECURRENCE-IDs must not make the digest depend on input order
        keys = sorted((_event_key(e) for e in events), key=lambda k: json.dumps(k, sort_keys=True))
        payload = json.dumps(keys, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()
    except Exception:  # untrusted bytes: any parser failure means "unreadable"
        return None
