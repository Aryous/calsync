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


@dataclass(slots=True)
class TargetCache:
    cursor: str | None = None
    objects: dict[str, CachedObject] = field(default_factory=dict)  # href -> object


class Target(Protocol):
    def reset(self) -> None: ...

    async def list_calendars(self) -> list[TargetCalendar]: ...

    async def ensure_calendar(self, ref: str, *, create: bool) -> TargetCalendar: ...

    async def changes(self, cal: TargetCalendar, cursor: str | None) -> TargetDelta: ...

    async def fetch(self, cal: TargetCalendar, hrefs: Sequence[str]) -> list[FetchedObject]: ...

    def href_for(self, cal: TargetCalendar, name: str) -> str: ...

    async def create(self, href: str, data: bytes) -> str | None: ...

    async def update(self, href: str, etag: str | None, data: bytes) -> str | None: ...

    async def delete(self, href: str, etag: str | None) -> None: ...
