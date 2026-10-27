"""Events by date range, each with its delivery state per rule, and one event's detail: what
``calsync events`` / ``calsync event show``, ``GET /api/events`` and ``GET /api/event`` show.

One event is one occurrence of a source series (a recurring series: one per instance). Its
``deliveries`` are the rules that sync its source calendar, each with the state of the rule's
copy of the series. Read-only, over ``views.StateReader``; the JSON shapes are a contract.

An event's ``id`` is opaque to clients: URL-safe base64 (no padding) of
``"<source account>/<source calendar id>/<source id>"``, plus ``"@<instance start>"`` for an
instance of a recurring series (the start as the event's ``start`` shows it).
"""

import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from calsync.config import Config, Rule
from calsync.model import Alarm, Instance, Series, When, expand, expand_instances, series_span
from calsync.views import RuleReading, RunView, StateReader, _iso, is_ok, source_series

# Most days one events query may span (the GUI's widest view is a month, plus its edges).
MAX_EVENT_DAYS = 62
# How far from now a query (and an event id's instance) may lie: expanding a recurring series
# costs its instances from its start up to the range, whatever the range's width.
HORIZON_YEARS = 10
HORIZON = timedelta(days=round(365.25 * HORIZON_YEARS))

# Worst first: the first that applies is the state. ``unknown``: the state comes from another
# calsync version (its hashes are not comparable); ``delivered``: the copy's recorded hash is the
# one calsync would write now; ``blocked`` / ``failing``: not delivered, and the rule's latest run
# was blocked by the delete guard / failed (an auth failure too); ``pending``: not delivered yet.
DeliveryState = Literal[
    "unknown", "paused", "retiring", "out_of_window", "delivered", "blocked", "failing", "pending"
]


class NotFound(Exception):
    """No such event: a malformed id, or one that names no current occurrence."""


def _in_horizon(moment: datetime, now: datetime) -> bool:
    return abs(moment - now) <= HORIZON  # a difference of two datetimes never overflows


def event_range(start: datetime | None, end: datetime | None, now: datetime) -> tuple[datetime, datetime]:
    """The range an events query covers: from ``start`` (default: today's local midnight) to
    ``end`` (exclusive; default: a week later). ``ValueError`` (its text for the person) for a
    range that ends before it starts, spans more than ``MAX_EVENT_DAYS`` days, or does not lie
    within ``HORIZON_YEARS`` of ``now``."""
    if start is None:
        start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    if not _in_horizon(start, now):  # before the default end: that could overflow
        raise ValueError(f"from: must be within {HORIZON_YEARS} years of today")
    defaulted = end is None
    if end is None:
        try:
            end = start + timedelta(days=7)
        except OverflowError:
            raise ValueError("from: too far from today") from None
    if not _in_horizon(end, now):
        if defaulted:  # the person gave no end: name what they gave
            raise ValueError(
                f"from: the default end (a week later) must be within {HORIZON_YEARS} years of today"
            )
        raise ValueError(f"to: must be within {HORIZON_YEARS} years of today")
    if end < start:
        raise ValueError("to: must not be before from")
    # An hour more: local midnights 62 days apart across a DST change differ by that much.
    if end - start > timedelta(days=MAX_EVENT_DAYS, hours=1):
        raise ValueError(f"to: at most {MAX_EVENT_DAYS} days after from")
    return start, end


def parse_target(text: str) -> tuple[str, str]:
    """``ACCOUNT:CALENDAR`` (the calendar as the rule's ``target.calendar`` names it)."""
    account, sep, calendar = text.partition(":")
    if not sep or not account or not calendar:
        raise ValueError(f"target: expected ACCOUNT:CALENDAR, got {text!r}")
    return account, calendar


def known_target(config: Config, target: tuple[str, str]) -> bool:
    return any((r.target.account, r.target.calendar) == target for r in config.rules)


def event_id(account: str, calendar: str, source_id: str, instance_start: str | None) -> str:
    """The opaque id of a source occurrence (``instance_start``: None for a single event)."""
    key = f"{account}/{calendar}/{source_id}"
    if instance_start is not None:
        key += f"@{instance_start}"
    return base64.urlsafe_b64encode(key.encode()).rstrip(b"=").decode("ascii")


def _decode(eid: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(eid + "=" * (-len(eid) % 4))
        if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != eid:
            raise NotFound(eid)  # not in the canonical form: never one of ours
        return raw.decode()
    except (binascii.Error, ValueError):  # UnicodeDecodeError is a ValueError
        raise NotFound(eid) from None


def _when(when: When) -> str:
    return when.value.isoformat()


def _minutes_before(alarms: Sequence[Alarm]) -> list[int]:
    return sorted((-int(a.offset.total_seconds() // 60) for a in alarms), reverse=True)


def _instance_id(account: str, cid: str, item: Series, inst: Instance) -> str:
    return event_id(account, cid, item.source_id, _when(inst.start) if inst.recurring else None)


# ----- per rule ------------------------------------------------------------------------------------


class _RuleCopies:
    """One rule's copies, read once per request (``RuleReading`` plus its latest run and when it
    last wrote each object)."""

    def __init__(self, reading: RuleReading, config: Config, reader: StateReader) -> None:
        self.reading = reading
        rule = reading.rule
        runs = reader.runs(rule.name, limit=1)
        self.last: RunView | None = runs[0] if runs else None
        self.times = reader.delivered_times(rule.name)
        self.provider = config.accounts[rule.target.account].provider

    @property
    def rule(self) -> Rule:
        return self.reading.rule

    def state(
        self, item: Series, span: tuple[datetime, datetime] | None
    ) -> tuple[DeliveryState, Series, str]:
        """The state of the rule's copy of ``item``, the series as the rule writes it, its object id."""
        reading, rule = self.reading, self.reading.rule
        shaped, oid = reading.shape(item), reading.object_id(item)
        if not reading.current:
            return "unknown", shaped, oid
        if rule.paused:
            return "paused", shaped, oid
        if rule.retire:
            return "retiring", shaped, oid
        if span is not None and not reading.in_window(span):
            return "out_of_window", shaped, oid
        if reading.delivered(oid, shaped):
            return "delivered", shaped, oid
        last = self.last
        if last is not None and last.status == "blocked":
            return "blocked", shaped, oid
        if last is not None and not is_ok(last.status, last.failed):
            return "failing", shaped, oid
        return "pending", shaped, oid

    def alarms(self, inst: Instance) -> list[int]:
        """The instance's alarms as the rule writes them."""
        policy = self.rule.alarms
        if policy == "copy":
            return _minutes_before(inst.alarms)
        if policy == "strip":
            return []
        return _minutes_before([Alarm(o) for o in policy])


@dataclass(frozen=True, slots=True)
class EventDelivery:
    rule: str
    target: dict[str, Any]
    state: DeliveryState
    delivered_at: datetime | None
    content: str
    alarms_minutes_before: list[int]
    # The detail only: the stored summary of the copy, and the fields in which it differs from
    # what calsync would write now (empty when delivered, or with no summary stored).
    copy: dict[str, Any] | None = None
    differs: tuple[str, ...] = ()

    def to_json(self, *, detail: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "rule": self.rule,
            "target": self.target,
            "state": self.state,
            "delivered_at": _iso(self.delivered_at),
            "content": self.content,
            "alarms_minutes_before": self.alarms_minutes_before,
        }
        if detail:
            body["copy"] = self.copy
            body["differs"] = list(self.differs)
        return body


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    instance: Instance  # the source's own occurrence
    source: dict[str, Any]
    deliveries: tuple[EventDelivery, ...]

    def _json(self, *, detail: bool) -> dict[str, Any]:
        i = self.instance
        return {
            "id": self.id,
            "title": i.title,
            "start": _when(i.start),
            "end": _when(i.end),
            "all_day": i.start.all_day,
            "location": i.location.strip() or None,
            "recurring": i.recurring,
            "alarms_minutes_before": _minutes_before(i.alarms),
            "source": self.source,
            "deliveries": [d.to_json(detail=detail) for d in self.deliveries],
        }

    def to_json(self) -> dict[str, Any]:
        return self._json(detail=False)


@dataclass(frozen=True, slots=True)
class EventsView:
    start: datetime
    end: datetime
    schema_version: int | None
    schema_current: bool
    events: tuple[Event, ...]
    # Some recurring series has more instances in the range than one expansion considers
    # (``model.expand``'s limit): the later ones are missing.
    truncated: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "schema": {"version": self.schema_version, "current": self.schema_current},
            "truncated": self.truncated,
            "events": [e.to_json() for e in self.events],
        }


@dataclass(frozen=True, slots=True)
class EventDetail:
    event: Event
    original: dict[str, Any]  # the source series' own fields (``render.summary_of``'s shape)

    def to_json(self) -> dict[str, Any]:
        return {**self.event._json(detail=True), "original": self.original}


# ----- grouping ----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Calendar:
    """A source calendar and the rules that sync it, in config order."""

    account: str
    cid: str
    ref: str  # as the first of its rules names it
    name: str
    entry: dict[str, Any]
    rules: list[_RuleCopies]


def _calendars(
    config: Config,
    reader: StateReader,
    now: datetime,
    *,
    rule: str | None = None,
    target: tuple[str, str] | None = None,
) -> list[_Calendar]:
    current = reader.schema_current()
    found: dict[tuple[str, str], _Calendar] = {}
    for r in config.rules:
        if rule is not None and r.name != rule:
            continue
        if target is not None and (r.target.account, r.target.calendar) != target:
            continue
        reading = RuleReading(r, reader, now, current)
        if reading.cid is None:  # never synced: nothing mirrored to show
            continue
        key = (r.source.account, reading.cid)
        if key not in found:
            entry = reading.entry
            name = entry.get("summary_override") or entry.get("summary") or r.source.calendar
            found[key] = _Calendar(key[0], key[1], r.source.calendar, name, dict(entry), [])
        found[key].rules.append(_RuleCopies(reading, config, reader))
    return list(found.values())


def _source_json(config: Config, cal: _Calendar) -> dict[str, Any]:
    return {
        "account": cal.account,
        "provider": config.accounts[cal.account].provider,
        "calendar": cal.ref,
        "calendar_name": cal.name,
    }


def _target_json(copies: _RuleCopies) -> dict[str, Any]:
    rule = copies.rule
    return {
        "account": rule.target.account,
        "provider": copies.provider,
        "calendar": rule.target.calendar,
        "calendar_name": copies.reading.target_name,
    }


def _span(cal: _Calendar, item: Series) -> tuple[datetime, datetime] | None:
    """The series' span when some rule has a window (shaping never moves times)."""
    return series_span(item) if any(c.reading.window is not None for c in cal.rules) else None


def _matches(inst: Instance, needle: str) -> bool:
    return needle in inst.title.casefold() or needle in inst.location.casefold()


def build_events(
    config: Config,
    reader: StateReader,
    start: datetime,
    end: datetime,
    *,
    rule: str | None = None,
    target: tuple[str, str] | None = None,
    query: str | None = None,
    now: datetime,
) -> EventsView:
    """The source occurrences in ``[start, end)``, in start order, each with a delivery per rule
    that syncs its calendar (only ``rule``'s, or the rules into the ``(account, calendar)``
    ``target``, when given). ``query``: a case-insensitive substring of the title or the
    location. Each series is expanded once, whatever the number of rules."""
    needle = query.casefold() if query else None
    events: list[Event] = []
    truncated = False
    for cal in _calendars(config, reader, now, rule=rule, target=target):
        source = _source_json(config, cal)
        targets = [_target_json(c) for c in cal.rules]
        for item in source_series(reader, cal.account, cal.cid, cal.entry):
            expansion = expand(item, start, end)
            truncated = truncated or expansion.truncated
            instances = expansion.instances
            if needle is not None:
                instances = [i for i in instances if _matches(i, needle)]
            if not instances:
                continue
            span = _span(cal, item)
            per_rule = [(c, t, *c.state(item, span)) for c, t in zip(cal.rules, targets, strict=True)]
            for inst in instances:
                deliveries = tuple(
                    EventDelivery(
                        rule=c.rule.name,
                        target=t,
                        state=state,
                        delivered_at=c.times.get(oid),
                        content=c.rule.fields.content,
                        alarms_minutes_before=c.alarms(inst),
                    )
                    for c, t, state, _, oid in per_rule
                )
                events.append(Event(_instance_id(cal.account, cal.cid, item, inst), inst, source, deliveries))
    events.sort(key=lambda e: (e.instance.start.utc(), e.instance.title, e.id))
    return EventsView(
        start=start,
        end=end,
        schema_version=reader.schema_version(),
        schema_current=reader.schema_current(),
        events=tuple(events),
        truncated=truncated,
    )


# ----- one event ----------------------------------------------------------------------------------


def _parse_start(text: str, now: datetime) -> tuple[datetime, datetime]:
    """A small range that holds only instances starting at ``text`` (an instance's ``start``).
    ``NotFound`` for a start that is no time, or lies beyond the query horizon (no events
    query lists it, and expanding a series up to it could take long)."""
    try:
        if "T" in text:
            lo = datetime.fromisoformat(text)
            if lo.tzinfo is None:
                raise NotFound(text)
            span = timedelta(seconds=1)
        else:
            day = date.fromisoformat(text)
            lo = datetime(day.year, day.month, day.day, tzinfo=UTC)  # as ``When.utc`` anchors a date
            span = timedelta(days=1)
        if not _in_horizon(lo, now):
            raise NotFound(text)
        return lo, lo + span
    except (ValueError, OverflowError):
        raise NotFound(text) from None


def _occurrence(item: Series, rest: str, now: datetime) -> Instance | None:
    """The occurrence of ``item`` that the id's tail (``rest``: after the calendar) names."""
    if rest == item.source_id:
        if item.recurrence:
            return None  # a recurring series: only its instances are events
        return Instance(item.start, item.end, item.title, item.location, item.alarms, False)
    prefix = f"{item.source_id}@"
    if not rest.startswith(prefix) or not item.recurrence:
        return None
    text = rest[len(prefix) :]
    lo, hi = _parse_start(text, now)
    return next((i for i in expand_instances(item, lo, hi) if _when(i.start) == text), None)


def build_event(config: Config, reader: StateReader, eid: str, now: datetime) -> EventDetail:
    """The event ``eid`` names, with the source series' own fields (``original``) and, per
    delivery, the stored summary of the copy and the fields it differs in. ``NotFound``: a
    malformed id, or no such occurrence now."""
    from calsync.render import changed_fields, summary_of

    key = _decode(eid)
    for cal in _calendars(config, reader, now):
        prefix = f"{cal.account}/{cal.cid}/"
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        for item in source_series(reader, cal.account, cal.cid, cal.entry):
            inst = _occurrence(item, rest, now)
            if inst is None:
                continue
            span = _span(cal, item)
            deliveries: list[EventDelivery] = []
            for copies in cal.rules:
                state, shaped, oid = copies.state(item, span)
                copy = reader.copy_summary(copies.reading.tag, oid)
                differs: tuple[str, ...] = ()
                if copy is not None and state != "delivered":
                    differs = tuple(changed_fields(copy, summary_of(shaped)))
                deliveries.append(
                    EventDelivery(
                        rule=copies.rule.name,
                        target=_target_json(copies),
                        state=state,
                        delivered_at=copies.times.get(oid),
                        content=copies.rule.fields.content,
                        alarms_minutes_before=copies.alarms(inst),
                        copy=copy,
                        differs=differs,
                    )
                )
            event = Event(eid, inst, _source_json(config, cal), tuple(deliveries))
            return EventDetail(event, summary_of(item))
    raise NotFound(eid)
