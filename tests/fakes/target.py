"""In-memory fake of a CalDAV target, implementing the ``Target`` protocol.

Writes are strictly conditional, like the strictest real servers: ``create``
answers 412 if the resource exists; ``update`` and ``delete`` answer 412 if
the resource is missing or its etag differs (including ``If-Match: *``).

Two injection points drive edge cases:
- ``fail_changes[ref]``: exception raised by the next ``changes()`` call for a
  calendar (consumed once); ``fail_fetch`` fails every ``fetch()`` while set.
- ``before_write(op, href)``: called at the start of every write; it may mutate
  ``objects`` (to stage a race) or raise (to fail that one write).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from calsync.errors import ConflictError, NotFoundError
from calsync.naming import parse_resource_name
from calsync.targets.base import FetchedObject, TargetCalendar, TargetDelta

HOME = "/dav/home/"


@dataclass
class FakeTarget:
    calendars: dict[str, TargetCalendar] = field(default_factory=dict)  # name -> calendar
    objects: dict[str, tuple[str, bytes]] = field(default_factory=dict)  # href -> (etag, data)
    fail_changes: dict[str, Exception] = field(default_factory=dict)
    fail_fetch: Exception | None = None  # raised by every fetch() while set
    before_write: Callable[[str, str], None] | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    _seq: int = 0

    # ----- test helpers -------------------------------------------------
    def add_calendar(self, name: str) -> TargetCalendar:
        cal = TargetCalendar(f"{HOME}{name.lower()}/", name, "full")
        self.calendars[name] = cal
        return cal

    def put(self, href: str, data: bytes) -> str:
        self._seq += 1
        etag = f'"e{self._seq}"'
        self.objects[href] = (etag, data)
        return etag

    def managed(self, cal: TargetCalendar) -> dict[str, bytes]:
        return {
            h: d for h, (_, d) in self.objects.items() if h.startswith(cal.href) and parse_resource_name(h)
        }

    # ----- protocol -----------------------------------------------------
    def reset(self) -> None:
        pass

    async def list_calendars(self) -> list[TargetCalendar]:
        return list(self.calendars.values())

    async def ensure_calendar(self, ref: str, *, create: bool) -> TargetCalendar:
        if ref in self.calendars:
            return self.calendars[ref]
        if not create:
            raise NotFoundError(f"fake calendar {ref!r} not found")
        return self.add_calendar(ref)

    async def changes(self, cal: TargetCalendar, cursor: str | None) -> TargetDelta:
        self.calls.append(("changes", cal.href))
        failure = self.fail_changes.pop(cal.name, None)
        if failure is not None:
            raise failure
        changed: dict[str, str | None] = {
            h: etag
            for h, (etag, _) in self.objects.items()
            if h.startswith(cal.href) and parse_resource_name(h)
        }
        return TargetDelta(changed, (), True, None)

    async def fetch(self, cal: TargetCalendar, hrefs: Sequence[str]) -> list[FetchedObject]:
        self.calls.append(("fetch", ",".join(hrefs)))
        if self.fail_fetch is not None:
            raise self.fail_fetch
        return [FetchedObject(h, *self.objects[h]) for h in hrefs if h in self.objects]

    def href_for(self, cal: TargetCalendar, name: str) -> str:
        return f"{cal.href}{name}"

    def _hook(self, op: str, href: str) -> None:
        self.calls.append((op, href))
        if self.before_write is not None:
            self.before_write(op, href)

    async def create(self, href: str, data: bytes) -> str | None:
        self._hook("create", href)
        if href in self.objects:
            raise ConflictError(f"412 create {href}")
        return self.put(href, data)

    async def update(self, href: str, etag: str | None, data: bytes) -> str | None:
        self._hook("update", href)
        current = self.objects.get(href)
        if current is None or (etag is not None and current[0] != etag):
            raise ConflictError(f"412 update {href}")
        return self.put(href, data)

    async def delete(self, href: str, etag: str | None) -> None:
        self._hook("delete", href)
        current = self.objects.get(href)
        if current is None or (etag is not None and current[0] != etag):
            raise ConflictError(f"412 delete {href}")
        del self.objects[href]
