"""Small iCalendar helpers shared by the Google mapper and the renderer."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from icalendar import Event

from calsync.model import When


def when_from_ical(value: datetime | date, default_tz: str = "UTC") -> When:
    """Convert a decoded iCalendar value into a When, keeping its IANA zone."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(default_tz))
        key = getattr(value.tzinfo, "key", None)
        if key is None:
            return When(value.astimezone(UTC), "UTC")
        return When(value, key)
    return When(value)


def parse_date_line(line: str, default_tz: str) -> list[When]:
    """Parse one EXDATE or RDATE content line, e.g. ``EXDATE;TZID=Asia/Shanghai:20261025T221000``."""
    name = line.split(":", 1)[0].split(";", 1)[0].upper()
    component = Event.from_ical(f"BEGIN:VEVENT\r\n{line}\r\nEND:VEVENT\r\n")
    prop = component.get(name)
    if prop is None:
        return []
    props = prop if isinstance(prop, list) else [prop]
    return [when_from_ical(item.dt, default_tz) for p in props for item in p.dts]
