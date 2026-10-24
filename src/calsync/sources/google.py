"""Google Calendar source: thin REST client with incremental sync.

Per cycle and Google account this costs exactly one ``calendarList`` request
plus one ``events.list`` request per source calendar when nothing changed.
"""

import asyncio
import copy
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx

from calsync.errors import AuthError, CalsyncError, ConfigError, CursorExpired, NotFoundError
from calsync.http import request_with_retry
from calsync.model import Alarm, Series
from calsync.sources.base import (
    CalendarInfo,
    CalendarMeta,
    CalendarPull,
    RawEvent,
    SourcePull,
)
from calsync.sources.google_auth import TokenProvider
from calsync.sources.google_map import alarms_from_reminders, build_series

BASE_URL = "https://www.googleapis.com/calendar/v3"

EVENT_FIELDS = (
    "id,status,updated,summary,description,location,start,end,"
    "originalStartTime,recurringEventId,recurrence,reminders,transparency"
)
# Identical for the initial full sync and every incremental call, as Google
# requires. ``fields`` MUST keep nextSyncToken/nextPageToken or incremental
# sync silently never starts.
EVENTS_LIST_PARAMS: Mapping[str, str] = {
    "singleEvents": "false",
    "showDeleted": "true",
    "maxResults": "2500",
    "fields": f"nextPageToken,nextSyncToken,items({EVENT_FIELDS})",
}
CALENDAR_LIST_PARAMS: Mapping[str, str] = {
    "showDeleted": "true",
    "showHidden": "true",
    "maxResults": "250",
    "fields": "nextPageToken,nextSyncToken,items(id,summary,timeZone,defaultReminders,deleted)",
}


def _is_removal(event: Mapping[str, Any]) -> bool:
    return event.get("status") == "cancelled" and not event.get("recurringEventId")


def _meta_from_state(entry: Mapping[str, Any] | None) -> CalendarMeta:
    if not entry:
        return CalendarMeta()
    return CalendarMeta(
        timezone=entry.get("timezone") or "UTC",
        default_alarms=tuple(Alarm(timedelta(seconds=s)) for s in entry.get("default_alarms", [])),
    )


class GoogleSource:
    def __init__(self, client: httpx.AsyncClient, token: TokenProvider, *, base_url: str = BASE_URL) -> None:
        self._client = client
        self._token = token
        self._base = base_url.rstrip("/")

    async def _get(self, path: str, params: Mapping[str, str], kind: str) -> dict[str, Any]:
        token = await self._token()
        response = await request_with_retry(
            self._client,
            "GET",
            f"{self._base}{path}",
            kind=kind,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        status = response.status_code
        if status == 410:
            raise CursorExpired(path)
        if status in (401, 403):
            raise AuthError(f"Google API {status}: {response.text[:200]}")
        if status == 404:
            raise NotFoundError(path)
        if status >= 400:
            raise CalsyncError(f"Google API {status} for {path}: {response.text[:200]}")
        return response.json()

    async def _list_all(
        self, path: str, base: Mapping[str, str], sync_token: str | None, kind: str
    ) -> tuple[list[dict[str, Any]], str]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            params = dict(base)
            if sync_token:
                params["syncToken"] = sync_token
            if page:
                params["pageToken"] = page
            data = await self._get(path, params, kind)
            items.extend(data.get("items", []))
            page = data.get("nextPageToken")
            if page:
                continue
            next_token = data.get("nextSyncToken")
            if not next_token:
                raise CalsyncError(f"Google response for {path} has no nextSyncToken")
            return items, next_token

    async def list_calendars(self) -> list[CalendarInfo]:
        items, _ = await self._list_all(
            "/users/me/calendarList", CALENDAR_LIST_PARAMS, None, "google-calendarlist"
        )
        return [
            CalendarInfo(item["id"], item.get("summary", item["id"]))
            for item in items
            if not item.get("deleted")
        ]

    async def _refresh_calendar_meta(self, state: dict[str, Any]) -> None:
        meta: dict[str, Any] = state.setdefault("meta", {})
        token = state.get("calendars_token")
        path = "/users/me/calendarList"
        try:
            items, next_token = await self._list_all(path, CALENDAR_LIST_PARAMS, token, "google-calendarlist")
        except CursorExpired:
            meta.clear()
            items, next_token = await self._list_all(path, CALENDAR_LIST_PARAMS, None, "google-calendarlist")
        for item in items:
            if item.get("deleted"):
                meta.pop(item["id"], None)
                continue
            alarms = alarms_from_reminders(item.get("defaultReminders") or [])
            meta[item["id"]] = {
                "summary": item.get("summary", ""),
                "timezone": item.get("timeZone") or "UTC",
                "default_alarms": [int(a.offset.total_seconds()) for a in alarms],
            }
        state["calendars_token"] = next_token

    @staticmethod
    def _resolve(ref: str, meta: Mapping[str, Any]) -> str:
        if ref in meta:
            return ref
        matches = [cid for cid, entry in meta.items() if entry.get("summary") == ref]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ConfigError([f"Google calendar name {ref!r} is ambiguous; use its ID"])
        if "@" in ref:
            return ref  # an ID the user can access but has not added to their list
        raise ConfigError([f"Google calendar {ref!r} not found; run `calsync calendars <account>`"])

    async def _pull_events(
        self, calendar_id: str, sync_token: str | None
    ) -> tuple[list[dict[str, Any]], str, bool]:
        path = f"/calendars/{quote(calendar_id, safe='')}/events"
        try:
            items, next_token = await self._list_all(path, EVENTS_LIST_PARAMS, sync_token, "google-events")
            return items, next_token, sync_token is None
        except CursorExpired:
            items, next_token = await self._list_all(path, EVENTS_LIST_PARAMS, None, "google-events")
            return items, next_token, True

    async def pull(self, calendar_refs: Sequence[str], state: Mapping[str, Any]) -> SourcePull:
        new_state: dict[str, Any] = copy.deepcopy(dict(state))
        await self._refresh_calendar_meta(new_state)
        meta = new_state["meta"]
        tokens: dict[str, str] = new_state.setdefault("events_tokens", {})
        resolved = {ref: self._resolve(ref, meta) for ref in calendar_refs}
        unique_ids = sorted(set(resolved.values()))
        results = await asyncio.gather(*(self._pull_events(cid, tokens.get(cid)) for cid in unique_ids))
        pulls: dict[str, CalendarPull] = {}
        for cid, (items, next_token, full) in zip(unique_ids, results, strict=True):
            tokens[cid] = next_token
            pulls[cid] = CalendarPull(
                calendar_id=cid,
                full=full,
                upserts=[item for item in items if not _is_removal(item)],
                deletes=[item["id"] for item in items if _is_removal(item)],
                meta=_meta_from_state(meta.get(cid)),
            )
        return SourcePull(calendars={ref: pulls[cid] for ref, cid in resolved.items()}, state=new_state)

    def to_series(self, raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]:
        return build_series(raw_events, meta)
