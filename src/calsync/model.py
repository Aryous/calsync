"""Unified event model, independent of any calendar service.

A ``Series`` is the unit of sync: a single event is a series with one
occurrence; a recurring event carries its recurrence lines plus every
individually modified instance (``Occurrence``).
"""

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from itertools import islice
from typing import Any, Literal
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule, rruleset, rrulestr

# Bump whenever render.py changes the bytes it produces: every managed
# resource is then rewritten once on the next cycle.
RENDER_VERSION = 2

MAX_TIME = datetime(9999, 12, 31, tzinfo=UTC)
# Beyond this many occurrences the series is treated as unbounded (safe direction: never mistaken for finished).
SPAN_EXPANSION_LIMIT = 10_000
# Most candidate instances one read-only expansion (``expand``) considers from its range on.
EXPANSION_LIMIT = 1000

AlarmPolicy = Literal["copy", "strip"] | tuple[timedelta, ...]
AttendeeMode = Literal["none", "notes"]
ConferenceMode = Literal["copy", "none"]
ContentMode = Literal["full", "busy"]
BUSY_TITLE = "Busy"


@dataclass(frozen=True, slots=True)
class When:
    """A point in time: an aware datetime plus its IANA zone, or an all-day date."""

    value: datetime | date
    tzid: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.value, datetime):
            if self.value.tzinfo is None:
                raise ValueError("When datetime must be timezone-aware")
            if self.tzid is None:
                raise ValueError("When datetime needs a tzid")
        elif self.tzid is not None:
            raise ValueError("all-day When must not carry a tzid")

    @property
    def all_day(self) -> bool:
        return not isinstance(self.value, datetime)

    def utc(self) -> datetime:
        """Instant in UTC. All-day dates are anchored at 00:00 UTC."""
        if isinstance(self.value, datetime):
            return self.value.astimezone(UTC)
        return datetime(self.value.year, self.value.month, self.value.day, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Alarm:
    offset: timedelta  # relative to the start; negative means before


@dataclass(frozen=True, slots=True)
class Attendee:
    name: str
    email: str


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One modified instance of a recurring series.

    Fields are complete: per RFC 5545 a RECURRENCE-ID component replaces the
    instance entirely and inherits nothing from the master.
    """

    original_start: When
    start: When
    end: When
    title: str
    description: str
    location: str
    alarms: tuple[Alarm, ...]
    attendees: tuple[Attendee, ...] = ()
    conference_url: str = ""


@dataclass(frozen=True, slots=True)
class Series:
    source_id: str
    title: str
    description: str
    location: str
    start: When
    end: When
    recurrence: tuple[str, ...] = ()  # raw RRULE / RDATE lines
    exdates: tuple[When, ...] = ()
    overrides: tuple[Occurrence, ...] = ()
    alarms: tuple[Alarm, ...] = ()
    attendees: tuple[Attendee, ...] = ()
    conference_url: str = ""
    transparent: bool = False
    # False: some instance failed to parse and was left out ("degraded"). An existing
    # target copy of a degraded series is kept as is; a missing one is created anyway.
    complete: bool = True


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime

    @classmethod
    def around(cls, now: datetime, past: timedelta, future: timedelta) -> "Window":
        return cls(now - past, now + future)

    @classmethod
    def everything(cls) -> "Window":
        """All of time: every span intersects it (full sync)."""
        return cls(datetime(1, 1, 1, tzinfo=UTC), MAX_TIME)

    def intersects(self, span: tuple[datetime, datetime]) -> bool:
        lo, hi = span
        return lo < self.end and hi > self.start


def _alarm_key(alarm: Alarm) -> timedelta:
    return alarm.offset


def _when_order(when: When) -> tuple[datetime, str, bool, str]:
    """Total order on ``When``: the instant first, then what tells equal instants apart.

    Sorting a ``set`` by the instant alone would leave ties in hash-seed order, and the
    result feeds ``series_hash``."""
    return (when.utc(), when.tzid or "", when.all_day, when.value.isoformat())


def _occurrence_order(occ: Occurrence) -> tuple[tuple[datetime, str, bool, str], str]:
    return (_when_order(occ.original_start), json.dumps(_canon(occ), ensure_ascii=False, sort_keys=True))


def _attendee_preference(a: Attendee) -> tuple[bool, str, str]:
    # Lower is better: a named entry first, then the smallest (name, email).
    return (not a.name, a.name, a.email)


def _normalize_attendees(attendees: tuple[Attendee, ...]) -> tuple[Attendee, ...]:
    """Trimmed, blank-free, deduplicated (emails case-insensitively) and totally ordered.

    The result must not depend on input order or on ``PYTHONHASHSEED``: it feeds ``series_hash``.
    """
    by_email: dict[str, Attendee] = {}
    by_name: dict[str, Attendee] = {}
    for a in attendees:
        cleaned = Attendee(a.name.strip(), a.email.strip())
        if cleaned.email:
            key = cleaned.email.lower()
            kept = by_email.get(key)
            if kept is None or _attendee_preference(cleaned) < _attendee_preference(kept):
                by_email[key] = cleaned
        elif cleaned.name:
            by_name.setdefault(cleaned.name, cleaned)
    return tuple(
        sorted([*by_email.values(), *by_name.values()], key=lambda a: (a.email.lower(), a.email, a.name))
    )


def _normalize_occurrence(occ: Occurrence) -> Occurrence:
    return replace(
        occ,
        title=occ.title.strip(),
        description=occ.description.strip(),
        location=occ.location.strip(),
        alarms=tuple(sorted(set(occ.alarms), key=_alarm_key)),
        attendees=_normalize_attendees(occ.attendees),
        conference_url=occ.conference_url.strip(),
    )


def normalize_series(series: Series) -> Series:
    """Canonical form: trimmed text, deduplicated and sorted collections."""
    return replace(
        series,
        title=series.title.strip(),
        description=series.description.strip(),
        location=series.location.strip(),
        recurrence=tuple(line.strip() for line in series.recurrence),
        exdates=tuple(sorted(set(series.exdates), key=_when_order)),
        overrides=tuple(sorted((_normalize_occurrence(o) for o in series.overrides), key=_occurrence_order)),
        alarms=tuple(sorted(set(series.alarms), key=_alarm_key)),
        attendees=_normalize_attendees(series.attendees),
        conference_url=series.conference_url.strip(),
    )


def _canon(value: Any) -> Any:
    if isinstance(value, When):
        return [value.value.isoformat(), value.tzid]
    if isinstance(value, Alarm):
        return int(value.offset.total_seconds())
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canon(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple | list):
        return [_canon(item) for item in value]
    return value


def series_hash(series: Series) -> str:
    """Stable content hash of the normalized series plus RENDER_VERSION."""
    payload = json.dumps(
        [RENDER_VERSION, _canon(normalize_series(series))],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()


def apply_alarm_policy(series: Series, policy: AlarmPolicy) -> Series:
    if policy == "copy":
        return series
    alarms: tuple[Alarm, ...] = () if policy == "strip" else tuple(Alarm(o) for o in policy)
    return replace(
        series,
        alarms=alarms,
        overrides=tuple(replace(o, alarms=alarms) for o in series.overrides),
    )


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    attendees: AttendeeMode = "notes"
    conference: ConferenceMode = "copy"
    content: ContentMode = "full"


def _attendee_label(a: Attendee) -> str:
    return f"{a.name} <{a.email}>" if a.name and a.email else a.name or a.email


def _notes(description: str, attendees: tuple[Attendee, ...], url: str, policy: FieldPolicy) -> str:
    blocks = [description.strip()] if description.strip() else []
    if policy.attendees == "notes" and attendees:
        blocks.append("Attendees: " + ", ".join(_attendee_label(a) for a in attendees))
    if policy.conference == "copy" and url:
        blocks.append("Conference: " + url)
    return "\n\n".join(blocks)


def apply_field_policy(series: Series, policy: FieldPolicy) -> Series:
    """Shape what reaches the target. Attendees never stay structured: they are
    either written into the notes or dropped, so no server can invite anyone."""
    if policy.content == "busy":
        return replace(
            series,
            title=BUSY_TITLE,
            description="",
            location="",
            attendees=(),
            conference_url="",
            overrides=tuple(
                replace(o, title=BUSY_TITLE, description="", location="", attendees=(), conference_url="")
                for o in series.overrides
            ),
        )
    keep_url = policy.conference == "copy"
    return replace(
        series,
        description=_notes(series.description, series.attendees, series.conference_url, policy),
        attendees=(),
        conference_url=series.conference_url if keep_url else "",
        overrides=tuple(
            replace(
                o,
                description=_notes(o.description, o.attendees, o.conference_url, policy),
                attendees=(),
                conference_url=o.conference_url if keep_url else "",
            )
            for o in series.overrides
        ),
    )


def _rrule_anchor(start: When) -> datetime:
    if isinstance(start.value, datetime):
        return start.value
    return datetime(start.value.year, start.value.month, start.value.day)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def compute_span(
    start: When,
    end: When,
    recurrence: Sequence[str] = (),
    extra: Iterable[tuple[When, When]] = (),
) -> tuple[datetime, datetime]:
    """Time range from the first occurrence's start to the last occurrence's end.

    An RRULE without COUNT/UNTIL, or one that cannot be parsed, is treated as
    unbounded so the series is never mistaken for "already over".
    """
    lo = start.utc()
    duration = end.utc() - lo
    last_start = lo
    unbounded = False
    for line in recurrence:
        name, _, value = line.partition(":")
        if name.split(";", 1)[0].upper() != "RRULE":
            continue
        upper = value.upper()
        if "COUNT=" not in upper and "UNTIL=" not in upper:
            unbounded = True
            continue
        last: datetime | None = None
        count = 0
        try:
            rule = rrulestr(value, dtstart=_rrule_anchor(start))
            for occurrence in islice(rule, SPAN_EXPANSION_LIMIT + 1):
                last = occurrence
                count += 1
        except (ValueError, TypeError):
            unbounded = True
            continue
        if count > SPAN_EXPANSION_LIMIT:
            unbounded = True
            continue
        if last is not None:
            try:
                last_start = max(last_start, _as_utc(last))
            except OverflowError:
                unbounded = True
    hi = MAX_TIME
    if not unbounded:
        try:
            hi = last_start + duration
        except OverflowError:
            unbounded = True
    for occ_start, occ_end in extra:
        with suppress(OverflowError):  # unrepresentable start: leave lo as is
            lo = min(lo, occ_start.utc())
        if not unbounded:
            try:
                hi = max(hi, occ_end.utc())
            except OverflowError:
                unbounded = True
                hi = MAX_TIME
    return lo, hi


def series_span(series: Series) -> tuple[datetime, datetime]:
    return compute_span(
        series.start,
        series.end,
        series.recurrence,
        ((o.start, o.end) for o in series.overrides),
    )


# ----- instance expansion (read-only views) -------------------------------


@dataclass(frozen=True, slots=True)
class Instance:
    """One occurrence of a series as a person sees it in their calendar."""

    start: When
    end: When
    title: str
    location: str
    alarms: tuple[Alarm, ...]
    recurring: bool


_UNTIL = re.compile(r"UNTIL=(\d{8})(T\d{6})?(Z?)", re.IGNORECASE)


def _zone(tzid: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tzid or "UTC")
    except (ValueError, KeyError):  # ZoneInfoNotFoundError is a KeyError
        return ZoneInfo("UTC")


def _until_like_anchor(value: str, zone: ZoneInfo | None) -> str:
    """dateutil wants UNTIL in UTC for a zoned DTSTART and floating for a naive one; Google sends both."""

    def fix(match: re.Match[str]) -> str:
        day, clock, utc = match.group(1), match.group(2), match.group(3)
        if zone is None:  # all-day series: floating UNTIL
            return f"UNTIL={day}{clock or ''}"
        if utc:
            return match.group(0)
        local = datetime.strptime(day + (clock or "T235959"), "%Y%m%dT%H%M%S").replace(tzinfo=zone)
        return "UNTIL=" + local.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    return _UNTIL.sub(fix, value)


def _midnight(value: date) -> datetime:
    return datetime(value.year, value.month, value.day)


def _occurrence_set(series: Series, zone: ZoneInfo | None) -> rruleset:
    """The series' RRULE/RDATE expansion, zoned (timed) or naive (all-day ``zone=None``).
    DTSTART is always the first instance (RFC 5545)."""
    from calsync.icalutil import parse_date_line  # icalendar: loaded only when expanding

    value = series.start.value
    anchor = value.astimezone(zone) if zone and isinstance(value, datetime) else _rrule_anchor(series.start)
    rset = rruleset()
    rset.rdate(anchor)
    for line in series.recurrence:
        name, _, rest = line.partition(":")
        head = name.split(";", 1)[0].upper()
        if head == "RRULE":
            rule = rrulestr(_until_like_anchor(rest, zone), dtstart=anchor)
            if not isinstance(rule, rrule):
                raise ValueError("not a single RRULE")
            rset.rrule(rule)
        elif head == "RDATE":
            for when in parse_date_line(line, series.start.tzid or "UTC"):
                extra = when.value
                if zone is None:
                    rset.rdate(_midnight(extra.date() if isinstance(extra, datetime) else extra))
                elif isinstance(extra, datetime):
                    rset.rdate(extra.astimezone(zone))
                else:  # a date for a timed series: that day at the series' time of day
                    rset.rdate(datetime.combine(extra, anchor.timetz()))
    return rset


def _overlaps(start: When, end: When, lo: datetime, hi: datetime) -> bool:
    s, e = start.utc(), end.utc()
    return s < hi and (e > lo or (s == e and s >= lo))


def _day_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """``[start, end)`` as the days it touches, each taken in its own zone, anchored at 00:00
    UTC as ``When.utc()`` anchors a date: a day view of 28 October at +08:00 holds the all-day
    events of the 28th, not of the 27th (which 16:00Z on the 27th would)."""
    lo = datetime(start.year, start.month, start.day, tzinfo=UTC)
    hi = datetime(end.year, end.month, end.day, tzinfo=UTC)
    if end != end.replace(hour=0, minute=0, second=0, microsecond=0):
        with suppress(OverflowError):  # the last representable day: as far as it goes
            hi += timedelta(days=1)  # a day only partly in range is in it
    return lo, hi


@dataclass(frozen=True, slots=True)
class Expansion:
    instances: list[Instance]
    truncated: bool  # the limit cut the expansion short inside the range


def expand(series: Series, start: datetime, end: datetime, *, limit: int = EXPANSION_LIMIT) -> Expansion:
    """The instances of ``series`` that intersect ``[start, end)``, sorted by start.

    Wall-clock times stay fixed across DST (the expansion runs in the series' zone). An all-day
    series is matched by day: against the days ``[start, end)`` touches in its own zone (see
    ``_day_range``). EXDATEs and the original starts of overrides are removed; overrides appear
    with their own fields. At most ``limit`` candidates are considered from the window on
    (``truncated`` when that cut the range short). Never raises on a bad recurrence: the master
    occurrence alone is returned (if it is in range).
    """
    if series.start.all_day:
        start, end = _day_range(start, end)
    recurring = bool(series.recurrence)
    master = Instance(series.start, series.end, series.title, series.location, series.alarms, recurring)
    only_master = [master] if _overlaps(series.start, series.end, start, end) else []
    if not recurring:
        return Expansion(only_master, False)
    all_day = series.start.all_day
    zone = None if all_day else _zone(series.start.tzid)
    duration = series.end.utc() - series.start.utc()

    def key(when: When) -> datetime | date:
        if all_day:
            return when.value.date() if isinstance(when.value, datetime) else when.value
        return when.utc()

    removed = {key(w) for w in series.exdates} | {key(o.original_start) for o in series.overrides}
    found: list[Instance] = []
    truncated = False
    try:
        rset = _occurrence_set(series, zone)
        lo, hi = start - duration, end
        if zone is None:  # naive midnights; dates are anchored at 00:00 UTC, as in When.utc()
            lo, hi = lo.astimezone(UTC).replace(tzinfo=None), hi.astimezone(UTC).replace(tzinfo=None)
        for count, dt in enumerate(rset.xafter(lo, inc=True)):
            if dt >= hi:
                break
            if count >= limit:
                truncated = True
                break
            if zone is None:
                inst_start, inst_end = When(dt.date()), When(dt.date() + duration)
            else:
                inst_start = When(dt, series.start.tzid or "UTC")
                inst_end = When((dt.astimezone(UTC) + duration).astimezone(zone), inst_start.tzid)
            if key(inst_start) in removed or not _overlaps(inst_start, inst_end, start, end):
                continue
            found.append(Instance(inst_start, inst_end, series.title, series.location, series.alarms, True))
    except Exception:  # a bad RRULE/RDATE (dateutil and icalendar raise many types): the master only
        return Expansion(only_master, False)
    found.extend(
        Instance(o.start, o.end, o.title, o.location, o.alarms, True)
        for o in series.overrides
        if _overlaps(o.start, o.end, start, end)
    )
    return Expansion(sorted(found, key=lambda i: _when_order(i.start)), truncated)


def expand_instances(
    series: Series, start: datetime, end: datetime, *, limit: int = EXPANSION_LIMIT
) -> list[Instance]:
    """``expand(...).instances``."""
    return expand(series, start, end, limit=limit).instances
