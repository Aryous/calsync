"""Target adapter interface and the local cache of managed target resources."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

DetectMode = Literal["sync", "ctag", "full"]


@dataclass(frozen=True, slots=True)
class TargetCalendar:
    href: str
    name: str
    mode: DetectMode  # best change-detection rung the server supports


@dataclass(frozen=True, slots=True)
class TargetDelta:
    changed: dict[str, str | None]  # managed href -> etag (None if the server omitted it)
    deleted: tuple[str, ...]
    full: bool  # True: ``changed`` lists every managed resource that exists
    cursor: str | None


@dataclass(frozen=True, slots=True)
class FetchedObject:
    href: str
    etag: str | None
    data: bytes


@dataclass(frozen=True, slots=True)
class CachedObject:
    etag: str | None  # None: we wrote it and the server returned no ETag yet
    rule_tag: str
    object_id: str
    content_hash: str | None
    span: tuple[datetime, datetime] | None
    # content_digest of a body read from the target that its rule has not classified yet
    # (None: nothing pending, or the body could not be digested). Persisted, so an edit read by
    # detection survives a cycle in which its rule does not run, fails, or the process stops.
    verify_digest: str | None = None
    # Classified as edited on the target; the source version has not been written back yet.
    reverting: bool = False
    # Rewrite-loop breaker (see engine.REVERT_LIMIT): when recent reverts of this copy were
    # written, and until when further target edits to it are adopted instead of reverted.
    revert_times: tuple[datetime, ...] = ()
    hold_until: datetime | None = None
    # ``render.summary_of`` of what calsync last wrote (or adopted) here, as JSON text; None:
    # unknown. Event content (titles): stored in the 0600 state DB, never logged. Carried
    # wherever a ``CachedObject`` is rebuilt, or a cache save would forget it.
    summary: str | None = None


@dataclass(slots=True)
class TargetCache:
    cursor: str | None = None
    objects: dict[str, CachedObject] = field(default_factory=dict)  # href -> object


class Target(Protocol):
    def reset(self) -> None: ...

    async def list_calendars(self) -> list[TargetCalendar]: ...

    async def ensure_calendar(self, ref: str, *, create: bool) -> TargetCalendar:
        """The calendar ``ref`` names (an href or a display name), created when missing and
        ``create``. Raises ``CalendarNotListed`` only when the calendar listing was read and no
        calendar in it matches ``ref`` (evidence that it does not exist); a failure to read the
        listing (a 404 during discovery, the network) raises anything else."""
        ...

    async def changes(self, cal: TargetCalendar, cursor: str | None) -> TargetDelta: ...

    async def fetch(self, cal: TargetCalendar, hrefs: Sequence[str]) -> list[FetchedObject]: ...

    def href_for(self, cal: TargetCalendar, name: str) -> str: ...

    async def create(self, href: str, data: bytes) -> str | None: ...

    async def update(self, href: str, etag: str | None, data: bytes) -> str | None: ...

    async def delete(self, href: str, etag: str | None) -> None: ...
