"""CalDAV target: discovery, change-detection ladder, conditional writes, throttling."""

import asyncio
import uuid
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx

from calsync.errors import ConfigError, CursorExpired, NotFoundError
from calsync.http import Sleep
from calsync.naming import parse_resource_name
from calsync.targets.base import DetectMode, FetchedObject, TargetCalendar, TargetDelta
from calsync.targets.dav import (
    CALDAV,
    CS,
    DAV,
    MULTIGET_BATCH,
    DavClient,
    DavResponse,
    calendar_query_etags_body,
    multiget_body,
    q,
    sync_collection_body,
)

CALENDAR_PROPS = (
    q(DAV, "resourcetype"),
    q(DAV, "displayname"),
    q(CS, "getctag"),
    q(DAV, "sync-token"),
    q(DAV, "supported-report-set"),
)


class Limiter:
    """Concurrency limit that can drop to 1 when the server starts throttling."""

    def __init__(self, limit: int) -> None:
        self._configured = limit
        self._limit = limit
        self._active = 0
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    def throttle(self) -> None:
        self._limit = 1

    def reset(self) -> None:
        self._limit = self._configured

    async def __aenter__(self) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self._active < self._limit)
            self._active += 1

    async def __aexit__(self, *_: object) -> None:
        async with self._cond:
            self._active -= 1
            self._cond.notify_all()


def _same_href(href: str, ref: str) -> bool:
    """``href`` is absolute; ``ref`` may be an absolute URL or just its path."""
    if "://" not in ref:
        href = urlsplit(href).path
    return href.rstrip("/") == ref.rstrip("/")


def _detect_mode(response: DavResponse) -> DetectMode:
    reports = response.props.get(q(DAV, "supported-report-set"))
    if reports is not None and reports.find(f".//{q(DAV, 'sync-collection')}") is not None:
        return "sync"
    if response.text(q(CS, "getctag")):
        return "ctag"
    return "full"


class CalDavTarget:
    def __init__(
        self,
        client: httpx.AsyncClient,
        server_url: str,
        *,
        max_concurrency: int = 2,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._limiter = Limiter(max_concurrency)
        self._dav = DavClient(client, server_url, sleep=sleep, on_throttle=self._limiter.throttle)
        self._home: str | None = None

    @property
    def limiter(self) -> Limiter:
        return self._limiter

    def reset(self) -> None:
        self._limiter.reset()

    async def _calendar_home(self) -> str:
        if self._home is None:
            (root,) = await self._dav.propfind("", [q(DAV, "current-user-principal")], depth=0)
            principal = root.href_prop(q(DAV, "current-user-principal"))
            if principal is None:
                raise NotFoundError("server did not report current-user-principal")
            (pr,) = await self._dav.propfind(principal, [q(CALDAV, "calendar-home-set")], depth=0)
            home = pr.href_prop(q(CALDAV, "calendar-home-set"))
            if home is None:
                raise NotFoundError("server did not report calendar-home-set")
            self._home = home
        return self._home

    async def list_calendars(self) -> list[TargetCalendar]:
        home = await self._calendar_home()
        calendars = []
        for item in await self._dav.propfind(home, CALENDAR_PROPS, depth=1):
            kind = item.props.get(q(DAV, "resourcetype"))
            if kind is None or kind.find(q(CALDAV, "calendar")) is None:
                continue
            name = item.text(q(DAV, "displayname")) or item.href.rstrip("/").rsplit("/", 1)[-1]
            calendars.append(TargetCalendar(item.href, name, _detect_mode(item)))
        return calendars

    async def ensure_calendar(self, ref: str, *, create: bool) -> TargetCalendar:
        calendars = await self.list_calendars()
        matches = [c for c in calendars if _same_href(c.href, ref)] or [c for c in calendars if c.name == ref]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ConfigError([f"CalDAV calendar {ref!r} matches {len(matches)} calendars; use its href"])
        if not create:
            raise NotFoundError(f"CalDAV calendar {ref!r} not found")
        href = f"{(await self._calendar_home()).rstrip('/')}/{uuid.uuid4().hex}/"
        await self._dav.mkcalendar(href, ref)
        for cal in await self.list_calendars():
            if _same_href(cal.href, href):
                return cal
        raise NotFoundError(f"created calendar {href} but it is not listed")

    @staticmethod
    def _managed(cal: TargetCalendar, responses: list[DavResponse]) -> list[DavResponse]:
        return [r for r in responses if not _same_href(r.href, cal.href) and parse_resource_name(r.href)]

    async def _full_listing(self, cal: TargetCalendar, cursor: str | None) -> TargetDelta:
        responses, _ = await self._dav.report(cal.href, calendar_query_etags_body(), kind="calendar-query")
        changed = {r.href: r.text(q(DAV, "getetag")) for r in self._managed(cal, responses)}
        return TargetDelta(changed, (), True, cursor)

    async def _sync(self, cal: TargetCalendar, token: str | None) -> TargetDelta:
        responses, new_token = await self._dav.report(
            cal.href, sync_collection_body(token), kind="sync-collection"
        )
        changed: dict[str, str | None] = {}
        deleted: list[str] = []
        for r in self._managed(cal, responses):
            if r.status == 404:
                deleted.append(r.href)
            else:
                changed[r.href] = r.text(q(DAV, "getetag"))
        return TargetDelta(changed, tuple(deleted), token is None, new_token)

    async def changes(self, cal: TargetCalendar, cursor: str | None) -> TargetDelta:
        if cal.mode == "sync":
            try:
                return await self._sync(cal, cursor)
            except CursorExpired:
                return await self._sync(cal, None)
        if cal.mode == "ctag":
            (item,) = await self._dav.propfind(cal.href, [q(CS, "getctag")], depth=0)
            ctag = item.text(q(CS, "getctag"))
            if cursor is not None and ctag == cursor:
                return TargetDelta({}, (), False, cursor)
            return await self._full_listing(cal, ctag)
        return await self._full_listing(cal, None)

    async def fetch(self, cal: TargetCalendar, hrefs: Sequence[str]) -> list[FetchedObject]:
        objects: list[FetchedObject] = []
        for start in range(0, len(hrefs), MULTIGET_BATCH):
            batch = hrefs[start : start + MULTIGET_BATCH]
            paths = [urlsplit(href).path for href in batch]
            responses, _ = await self._dav.report(cal.href, multiget_body(paths), kind="calendar-multiget")
            for r in responses:
                data = r.props.get(q(CALDAV, "calendar-data"))
                if r.status == 200 and data is not None and data.text:
                    objects.append(FetchedObject(r.href, r.text(q(DAV, "getetag")), data.text.encode()))
        return objects

    def href_for(self, cal: TargetCalendar, name: str) -> str:
        return f"{cal.href.rstrip('/')}/{name}"

    async def create(self, href: str, data: bytes) -> str | None:
        async with self._limiter:
            return await self._dav.put(href, data, create_only=True)

    async def update(self, href: str, etag: str | None, data: bytes) -> str | None:
        async with self._limiter:
            return await self._dav.put(href, data, if_match=etag)

    async def delete(self, href: str, etag: str | None) -> None:
        async with self._limiter:
            try:
                await self._dav.delete(href, if_match=etag)
            except NotFoundError:
                return
