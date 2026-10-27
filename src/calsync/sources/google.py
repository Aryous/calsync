"""Google Calendar source: thin REST client with incremental sync.

Per cycle and Google account this costs exactly one ``calendarList`` request
plus one ``events.list`` request per source calendar when nothing changed.
"""

import asyncio
import copy
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx

from calsync.errors import (
    AuthError,
    CalsyncError,
    ConfigError,
    CursorExpired,
    ForbiddenError,
    NotFoundError,
    TransientError,
)
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
from calsync.sources.google_map import alarms_from_reminders, build_series, master_ids

BASE_URL = "https://www.googleapis.com/calendar/v3"

EVENT_FIELDS = (
    "id,status,updated,summary,description,location,start,end,"
    "originalStartTime,recurringEventId,recurrence,reminders,transparency,"
    "attendees(email,displayName,resource),hangoutLink,conferenceData(entryPoints(entryPointType,uri))"
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
# Sync tokens are only valid for the parameters they were issued with, and a mirror
# filled with fewer fields lacks data. When the parameters change, pull everything again.
_EVENTS_PARAMS_JSON = json.dumps(dict(EVENTS_LIST_PARAMS), sort_keys=True)
EVENTS_PARAMS_ID = hashlib.sha256(_EVENTS_PARAMS_JSON.encode()).hexdigest()[:16]
CALENDAR_LIST_PARAMS: Mapping[str, str] = {
    "showDeleted": "true",
    "showHidden": "true",
    "maxResults": "250",
    "fields": "nextPageToken,nextSyncToken,items(id,summary,summaryOverride,timeZone,defaultReminders,deleted)",
}


RATE_LIMIT_REASONS = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
        "dailyLimitExceeded",
        "RESOURCE_EXHAUSTED",
    }
)


def _error_reasons(response: httpx.Response) -> list[str]:
    """The machine-readable causes in a Google error body: ``errors[0].reason``, then ``status``."""
    try:
        error = response.json()["error"]
        candidates = [error["errors"][0].get("reason")] if error.get("errors") else []
        candidates.append(error.get("status"))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return []
    return [c for c in candidates if isinstance(c, str) and c]


def _is_rate_limited(response: httpx.Response) -> bool:
    """403 bodies carry the real cause: quota exhaustion is retryable, the rest is not."""
    return bool(set(_error_reasons(response)) & RATE_LIMIT_REASONS)


def _is_removal(event: Mapping[str, Any]) -> bool:
    return event.get("status") == "cancelled" and not event.get("recurringEventId")


def meta_from_state(entry: Mapping[str, Any] | None) -> CalendarMeta:
    """A calendar's metadata as stored in the source state (``state["meta"][<calendar id>]``)."""
    if not entry:
        return CalendarMeta()
    return CalendarMeta(
        timezone=entry.get("timezone") or "UTC",
        default_alarms=tuple(Alarm(timedelta(seconds=s)) for s in entry.get("default_alarms", [])),
    )


def resolve_calendar_ref(ref: str, meta: Mapping[str, Any]) -> str:
    """The calendar ID a configured ref (an ID or a calendar name) names, given ``state["meta"]``."""
    if ref in meta:
        return ref
    matches: list[str] = []
    for field in ("summary_override", "summary"):  # the name the user sees comes first
        matches = [cid for cid, entry in meta.items() if entry.get(field) == ref]
        if matches:
            break
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConfigError([f"Google calendar name {ref!r} is ambiguous; use its ID"])
    if "@" in ref:
        return ref  # an ID the user can access but has not added to their list
    raise ConfigError([f"Google calendar {ref!r} not found; run `calsync calendars <account>`"])


class GoogleSource:
    def __init__(self, client: httpx.AsyncClient, token: TokenProvider, *, base_url: str = BASE_URL) -> None:
        self._client = client
        self._token = token
        self._base = base_url.rstrip("/")

    async def _get(self, path: str, params: Mapping[str, str], kind: str) -> dict[str, Any]:
        response = await self._send(path, params, kind)
        if response.status_code == 401:
            # The cached access token may have died (sleep, revocation): refresh once.
            invalidate = getattr(self._token, "invalidate", None)
            if invalidate is not None and invalidate():
                response = await self._send(path, params, kind)
        status = response.status_code
        if status == 410:
            raise CursorExpired(path)
        if status == 403 and _is_rate_limited(response):
            raise TransientError(f"Google API rate limit for {path}: {response.text[:200]}")
        if status == 401:
            raise AuthError(f"Google API 401: {response.text[:200]}")
        if status == 403:
            # Not throttling: a permission problem for this request (often one calendar).
            reasons = _error_reasons(response)
            raise ForbiddenError(f"Google API 403 for {path}: {reasons[0] if reasons else 'forbidden'}")
        if status == 404:
            raise NotFoundError(path)
        if status >= 400:
            raise CalsyncError(f"Google API {status} for {path}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError:
            raise TransientError(f"Google API returned a non-JSON body for {path}") from None
        if not isinstance(data, dict):
            raise TransientError(f"Google API returned an unexpected body for {path}")
        return data

    async def _send(self, path: str, params: Mapping[str, str], kind: str) -> httpx.Response:
        token = await self._token()
        return await request_with_retry(
            self._client,
            "GET",
            f"{self._base}{path}",
            kind=kind,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )

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
            CalendarInfo(item["id"], item.get("summaryOverride") or item.get("summary", item["id"]))
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
                "summary_override": item.get("summaryOverride") or "",
                "timezone": item.get("timeZone") or "UTC",
                "default_alarms": [int(a.offset.total_seconds()) for a in alarms],
            }
        state["calendars_token"] = next_token

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
        """Pull every referenced calendar.

        The calendarList refresh is account-wide: if it fails (including an
        expired or revoked token), ``pull`` raises. After that, each calendar
        succeeds or fails on its own: its failure lands in ``errors`` and its
        events token is left untouched, so the next pull retries it.
        """
        new_state: dict[str, Any] = copy.deepcopy(dict(state))
        if new_state.get("events_params") != EVENTS_PARAMS_ID:
            new_state["events_tokens"] = {}
            new_state["events_params"] = EVENTS_PARAMS_ID
        await self._refresh_calendar_meta(new_state)
        meta = new_state["meta"]
        tokens: dict[str, str] = new_state.setdefault("events_tokens", {})
        errors: dict[str, Exception] = {}
        resolved: dict[str, str] = {}
        for ref in calendar_refs:
            try:
                resolved[ref] = resolve_calendar_ref(ref, meta)
            except ConfigError as exc:
                errors[ref] = exc
        unique_ids = sorted(set(resolved.values()))
        results = await asyncio.gather(
            *(self._pull_events(cid, tokens.get(cid)) for cid in unique_ids), return_exceptions=True
        )
        pulls: dict[str, CalendarPull] = {}
        failed: dict[str, Exception] = {}
        for cid, outcome in zip(unique_ids, results, strict=True):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome  # cancellation and the like are not per-calendar failures
                failed[cid] = outcome
                continue
            items, next_token, full = outcome
            tokens[cid] = next_token
            pulls[cid] = CalendarPull(
                calendar_id=cid,
                full=full,
                upserts=[item for item in items if not _is_removal(item)],
                deletes=[item["id"] for item in items if _is_removal(item)],
                meta=meta_from_state(meta.get(cid)),
            )
        for ref, cid in resolved.items():
            if cid in failed:
                errors[ref] = failed[cid]
        return SourcePull(
            calendars={ref: pulls[cid] for ref, cid in resolved.items() if cid in pulls},
            state=new_state,
            errors=errors,
        )

    def to_series(self, raw_events: Iterable[RawEvent], meta: CalendarMeta) -> list[Series]:
        return build_series(raw_events, meta)

    def series_ids(self, raw_events: Iterable[RawEvent]) -> set[str]:
        return master_ids(raw_events)
