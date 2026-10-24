"""Unified event model, independent of any calendar service.

A ``Series`` is the unit of sync: a single event is a series with one
occurrence; a recurring event carries its recurrence lines plus every
individually modified instance (``Occurrence``).
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from itertools import islice
from typing import Any, Literal

from dateutil.rrule import rrulestr

# Bump whenever render.py changes the bytes it produces: every managed
# resource is then rewritten once on the next cycle.
RENDER_VERSION = 1

MAX_TIME = datetime(9999, 12, 31, tzinfo=UTC)
# Beyond this many occurrences the series is treated as unbounded (safe direction: never mistaken for finished).
SPAN_EXPANSION_LIMIT = 10_000

AlarmPolicy = Literal["copy", "strip"] | tuple[timedelta, ...]


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
    transparent: bool = False


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime

    @classmethod
    def around(cls, now: datetime, past: timedelta, future: timedelta) -> "Window":
        return cls(now - past, now + future)

    def intersects(self, span: tuple[datetime, datetime]) -> bool:
        lo, hi = span
        return lo < self.end and hi > self.start


def _alarm_key(alarm: Alarm) -> timedelta:
    return alarm.offset


def _normalize_occurrence(occ: Occurrence) -> Occurrence:
    return replace(
        occ,
        title=occ.title.strip(),
        description=occ.description.strip(),
        location=occ.location.strip(),
        alarms=tuple(sorted(set(occ.alarms), key=_alarm_key)),
    )


def normalize_series(series: Series) -> Series:
    """Canonical form: trimmed text, deduplicated and sorted collections."""
    return replace(
        series,
        title=series.title.strip(),
        description=series.description.strip(),
        location=series.location.strip(),
        recurrence=tuple(line.strip() for line in series.recurrence),
        exdates=tuple(sorted(set(series.exdates), key=When.utc)),
        overrides=tuple(
            sorted(
                (_normalize_occurrence(o) for o in series.overrides),
                key=lambda o: o.original_start.utc(),
            )
        ),
        alarms=tuple(sorted(set(series.alarms), key=_alarm_key)),
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
