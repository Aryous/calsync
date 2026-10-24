"""Map Google Calendar API event resources to the unified model (pure functions)."""

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

from calsync.icalutil import parse_date_line
from calsync.model import Alarm, Occurrence, Series, When, normalize_series
from calsync.sources.base import CalendarMeta, RawEvent

log = logging.getLogger(__name__)

_BLOCK_END = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_END:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(text: str) -> str:
    """Google stores rich descriptions as HTML; targets want plain text."""
    if "<" not in text and "&" not in text:
        return text.strip()
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    joined = "".join(parser.parts)
    lines = [line.rstrip() for line in joined.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_when(obj: Mapping[str, Any], default_tz: str) -> When:
    if "date" in obj:
        return When(date.fromisoformat(obj["date"]))
    tzid = obj.get("timeZone") or default_tz
    zone = ZoneInfo(tzid)
    value = datetime.fromisoformat(obj["dateTime"])
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return When(value.astimezone(zone), tzid)


def alarms_from_reminders(reminders: Iterable[Mapping[str, Any]]) -> tuple[Alarm, ...]:
    return tuple(
        Alarm(timedelta(minutes=-int(r.get("minutes", 0)))) for r in reminders if r.get("method") == "popup"
    )


def event_alarms(event: RawEvent, defaults: tuple[Alarm, ...]) -> tuple[Alarm, ...]:
    reminders = event.get("reminders") or {}
    if reminders.get("useDefault", True):
        return defaults
    return alarms_from_reminders(reminders.get("overrides") or [])


def split_recurrence(lines: Iterable[str], tz: str) -> tuple[tuple[str, ...], tuple[When, ...]]:
    rules: list[str] = []
    exdates: list[When] = []
    for line in lines:
        head = line.split(":", 1)[0].split(";", 1)[0].upper()
        if head == "EXDATE":
            exdates.extend(parse_date_line(line, tz))
        elif head in ("RRULE", "RDATE"):
            rules.append(line)
        else:
            log.debug("ignoring recurrence line %r", line)
    return tuple(rules), tuple(exdates)


def _series(master: RawEvent, exceptions: list[RawEvent], meta: CalendarMeta) -> Series:
    tz = master.get("start", {}).get("timeZone") or meta.timezone
    rules, exdates = split_recurrence(master.get("recurrence") or (), tz)
    excluded = list(exdates)
    overrides: list[Occurrence] = []
    if rules:
        for ex in exceptions:
            original = ex.get("originalStartTime")
            if not original:
                continue
            original_start = parse_when(original, tz)
            if ex.get("status") == "cancelled":
                excluded.append(original_start)
                continue
            overrides.append(
                Occurrence(
                    original_start=original_start,
                    start=parse_when(ex["start"], tz),
                    end=parse_when(ex["end"], tz),
                    title=ex.get("summary", ""),
                    description=html_to_text(ex.get("description", "")),
                    location=ex.get("location", ""),
                    alarms=event_alarms(ex, meta.default_alarms),
                )
            )
    return normalize_series(
        Series(
            source_id=master["id"],
            title=master.get("summary", ""),
            description=html_to_text(master.get("description", "")),
            location=master.get("location", ""),
            start=parse_when(master["start"], tz),
            end=parse_when(master["end"], tz),
            recurrence=rules,
            exdates=tuple(excluded),
            overrides=tuple(overrides),
            alarms=event_alarms(master, meta.default_alarms),
            transparent=master.get("transparency") == "transparent",
        )
    )


def build_series(raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]:
    """Group mirror entries into series: masters plus their modified/cancelled instances.

    Attendees, organizer and conference data are deliberately never read, so
    a target server can never send invitations on our behalf.
    """
    masters: dict[str, RawEvent] = {}
    exceptions: dict[str, list[RawEvent]] = defaultdict(list)
    for event in raw_events:
        parent = event.get("recurringEventId")
        if parent:
            exceptions[parent].append(event)
        elif event.get("status") != "cancelled":
            masters[event["id"]] = event
    result: list[Series] = []
    for master_id, master in masters.items():
        try:
            result.append(_series(master, exceptions.pop(master_id, []), meta))
        except (KeyError, ValueError) as exc:
            log.warning("skipping malformed event %s: %s", master_id, exc)
    for master_id, orphans in exceptions.items():
        log.debug("ignoring %d instance(s) of unknown series %s", len(orphans), master_id)
    return result
