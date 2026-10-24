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
from calsync.model import Alarm, Attendee, Occurrence, Series, When, normalize_series
from calsync.sources.base import CalendarMeta, RawEvent

log = logging.getLogger(__name__)

_LOOKS_LIKE_HTML = re.compile(r"</?[a-zA-Z][^>]*>|&[a-zA-Z]+;|&#\d+;|&#x[0-9a-fA-F]+;")
_BLOCK_END = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._href: str | None = None
        self._link_start = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_start = len(self.parts)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            label = "".join(self.parts[self._link_start :]).strip()
            if not label:
                self.parts.append(self._href)
            elif label != self._href:
                self.parts.append(f" ({self._href})")
            self._href = None
        if tag in _BLOCK_END:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(text: str) -> str:
    """Google stores rich descriptions as HTML; targets want plain text."""
    if not _LOOKS_LIKE_HTML.search(text):
        return text.strip()
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    joined = "".join(parser.parts).replace("\xa0", " ")
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


def event_attendees(event: RawEvent) -> tuple[Attendee, ...]:
    """People invited to the event; rooms and other resources are left out."""
    result: list[Attendee] = []
    for entry in event.get("attendees") or ():
        if not isinstance(entry, Mapping) or entry.get("resource"):
            continue
        name = str(entry.get("displayName") or "")
        email = str(entry.get("email") or "")
        if name or email:
            result.append(Attendee(name, email))
    return tuple(result)


def event_conference(event: RawEvent) -> str:
    """The video meeting link: the conference video entry point, else ``hangoutLink``."""
    data = event.get("conferenceData") or {}
    for entry in data.get("entryPoints") or ():
        if isinstance(entry, Mapping) and entry.get("entryPointType") == "video" and entry.get("uri"):
            return str(entry["uri"])
    return str(event.get("hangoutLink") or "")


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
    complete = True
    if rules:
        for ex in exceptions:
            original = ex.get("originalStartTime")
            if not original:
                # Google guarantees id, recurringEventId and originalStartTime on every
                # exception (cancelled ones included), so a missing one is genuinely malformed.
                log.debug(
                    "skipping instance %s of series %s: no originalStartTime", ex.get("id"), master.get("id")
                )
                complete = False
                continue
            try:
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
                        attendees=event_attendees(ex),
                        conference_url=event_conference(ex),
                    )
                )
            except (KeyError, ValueError) as exc:
                complete = False
                # DEBUG: runs every cycle; the engine warns once per process per series.
                log.debug(
                    "skipping malformed instance %s of series %s: %s", ex.get("id"), master.get("id"), exc
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
            attendees=event_attendees(master),
            conference_url=event_conference(master),
            transparent=master.get("transparency") == "transparent",
            complete=complete,
        )
    )


def _is_master(event: RawEvent) -> bool:
    return bool(event.get("id")) and not event.get("recurringEventId") and event.get("status") != "cancelled"


def master_ids(raw_events: Iterable[RawEvent]) -> set[str]:
    """IDs of every live series master, parseable or not (the masters ``build_series`` considers)."""
    return {event["id"] for event in raw_events if _is_master(event)}


def build_series(raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]:
    """Group mirror entries into series: masters plus their modified/cancelled instances.

    Attendees and conference links are read into the model; the renderer never writes
    them as ATTENDEE or ORGANIZER, so a target server cannot send invitations on our behalf.
    """
    masters: dict[str, RawEvent] = {}
    exceptions: dict[str, list[RawEvent]] = defaultdict(list)
    for event in raw_events:
        if not event.get("id"):
            # Never log event content (titles, descriptions): only where the entry belongs.
            log.warning("skipping an entry without id (recurringEventId=%s)", event.get("recurringEventId"))
            continue
        parent = event.get("recurringEventId")
        if parent:
            exceptions[parent].append(event)
        elif _is_master(event):
            masters[event["id"]] = event
    result: list[Series] = []
    for master_id, master in masters.items():
        try:
            result.append(_series(master, exceptions.pop(master_id, []), meta))
        except (KeyError, ValueError) as exc:
            log.debug("skipping malformed event %s: %s", master_id, exc)  # the engine warns once
    for master_id, orphans in exceptions.items():
        live = [o for o in orphans if o.get("status") != "cancelled"]
        if live:
            log.warning("ignoring %d instance(s) of unknown series %s", len(live), master_id)
        else:
            log.debug("ignoring %d cancelled instance(s) of unknown series %s", len(orphans), master_id)
    return result
