"""In-memory fake of a CalDAV target, implementing the ``Target`` protocol.

Writes are strictly conditional, like the strictest real servers: ``create``
answers 412 if the resource exists; ``update`` and ``delete`` answer 412 if
the resource is missing or its etag differs (including ``If-Match: *``).

``mode="full"`` (default) lists every managed resource on each ``changes()``;
``mode="sync"`` behaves like sync-collection: a full listing only when the
cursor is None, otherwise just what was written or deleted since the cursor.
Use ``put``/``remove`` (not ``objects`` directly) to change resources in sync mode.

Injection points drive edge cases:
- ``fail_changes[ref]``: exception raised by the next ``changes()`` call for a
  calendar (consumed once); ``fail_fetch`` fails every ``fetch()`` while set.
- ``fail_discovery``: raised by every ``list_calendars()`` / ``ensure_calendar()`` while set
  (a discovery failure, e.g. a 404 on the principal or the calendar home).
- ``before_write(op, href)``: called at the start of every write; it may mutate
  ``objects`` (to stage a race) or raise (to fail that one write).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from calsync.errors import CalendarNotListed, ConflictError
from calsync.naming import parse_resource_name
from calsync.targets.base import DetectMode, FetchedObject, TargetCalendar, TargetDelta

HOME = "/dav/home/"


@dataclass
class FakeTarget:
    calendars: dict[str, TargetCalendar] = field(default_factory=dict)  # name -> calendar
    objects: dict[str, tuple[str, bytes]] = field(default_factory=dict)  # href -> (etag, data)
    fail_changes: dict[str, Exception] = field(default_factory=dict)
    fail_fetch: Exception | None = None  # raised by every fetch() while set
    fail_discovery: Exception | None = None  # raised by list_calendars/ensure_calendar while set
    hide_once: set[str] = field(default_factory=set)  # left out of the next fetch() only
    before_write: Callable[[str, str], None] | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    mode: DetectMode = "full"  # detection mode of calendars created from now on
    _seq: int = 0
    _touched: dict[str, int] = field(default_factory=dict)  # href -> seq of its last write/delete

    # ----- test helpers -------------------------------------------------
    def add_calendar(self, name: str) -> TargetCalendar:
        cal = TargetCalendar(f"{HOME}{name.lower()}/", name, self.mode)
        self.calendars[name] = cal
        return cal

    def put(self, href: str, data: bytes) -> str:
        self._seq += 1
        etag = f'"e{self._seq}"'
        self.objects[href] = (etag, data)
        self._touched[href] = self._seq
        return etag

    def remove(self, href: str) -> None:
        self._seq += 1
        del self.objects[href]
        self._touched[href] = self._seq

    def managed(self, cal: TargetCalendar) -> dict[str, bytes]:
        return {
            h: d for h, (_, d) in self.objects.items() if h.startswith(cal.href) and parse_resource_name(h)
        }

    # ----- protocol -----------------------------------------------------
    def reset(self) -> None:
        pass

    async def list_calendars(self) -> list[TargetCalendar]:
        if self.fail_discovery is not None:
            raise self.fail_discovery
        return list(self.calendars.values())

    async def ensure_calendar(self, ref: str, *, create: bool) -> TargetCalendar:
        if self.fail_discovery is not None:
            raise self.fail_discovery
        if ref in self.calendars:
            return self.calendars[ref]
        if not create:
            raise CalendarNotListed(f"fake calendar {ref!r} not found")
        return self.add_calendar(ref)

    async def changes(self, cal: TargetCalendar, cursor: str | None) -> TargetDelta:
        self.calls.append(("changes", cal.href))
        failure = self.fail_changes.pop(cal.name, None)
        if failure is not None:
            raise failure
        mine = [
            h for h in {*self.objects, *self._touched} if h.startswith(cal.href) and parse_resource_name(h)
        ]
        if cal.mode == "sync" and cursor is not None:
            since = [h for h in mine if self._touched.get(h, 0) > int(cursor)]
            changed: dict[str, str | None] = {h: self.objects[h][0] for h in since if h in self.objects}
            deleted = tuple(h for h in since if h not in self.objects)
            return TargetDelta(changed, deleted, False, str(self._seq))
        changed = {h: self.objects[h][0] for h in mine if h in self.objects}
        return TargetDelta(changed, (), True, str(self._seq) if cal.mode == "sync" else None)

    async def fetch(self, cal: TargetCalendar, hrefs: Sequence[str]) -> list[FetchedObject]:
        self.calls.append(("fetch", ",".join(hrefs)))
        if self.fail_fetch is not None:
            raise self.fail_fetch
        hidden, self.hide_once = self.hide_once, set()
        return [FetchedObject(h, *self.objects[h]) for h in hrefs if h in self.objects and h not in hidden]

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
        self.remove(href)
