"""Source adapter interface.

A source owns its own incremental-sync cursors inside an opaque, JSON
serializable ``state`` dict. The engine persists that dict together with the
mirror in one transaction and hands it back on the next pull.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from calsync.model import Alarm, Series

RawEvent = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CalendarInfo:
    id: str
    name: str
    role: str | None = None  # Google calendarList accessRole: owner, writer, reader, freeBusyReader
    primary: bool = False  # the account's own calendar (Google names it after the account's email)


@dataclass(frozen=True, slots=True)
class CalendarMeta:
    timezone: str = "UTC"
    default_alarms: tuple[Alarm, ...] = ()


@dataclass(slots=True)
class CalendarPull:
    calendar_id: str  # resolved ID, even if the rule referenced the calendar by name
    full: bool  # True: ``upserts`` is the complete calendar, replace the mirror
    upserts: list[dict[str, Any]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    meta: CalendarMeta = CalendarMeta()


@dataclass(slots=True)
class SourcePull:
    calendars: dict[str, CalendarPull]  # keyed by the calendar reference passed in
    state: dict[str, Any]
    # Calendar references that failed on their own (unknown name, 404, ...). Their
    # cursors in ``state`` are unchanged; account-wide failures raise from ``pull``.
    errors: dict[str, Exception] = field(default_factory=dict)


class Source(Protocol):
    async def list_calendars(self) -> list[CalendarInfo]: ...

    async def pull(self, calendar_refs: Sequence[str], state: Mapping[str, Any]) -> SourcePull: ...

    def to_series(self, raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]: ...

    def series_ids(self, raw_events: Iterable[RawEvent]) -> set[str]:
        """Source ids of every live series present, whether ``to_series`` can parse it or not."""
        ...
