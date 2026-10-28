"""Writing events to Google Calendar: the one place calsync changes a source.

calsync stays one-way: it writes Google's originals on a person's or an agent's request, and
its copies in iCloud follow at the next cycle. Every change is conditional (``If-Match`` with
the event's etag, 412 when it changed meanwhile), never notifies attendees
(``sendUpdates=none``), and never touches the OAuth grant (no revoke anywhere).

Batches go to Google's batch endpoint as ``multipart/mixed``, at most ``BATCH_SIZE`` calls per
request; the replies come back in request order, each with its own status.
"""

import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import httpx

from calsync.errors import AuthError, CalsyncError, ForbiddenError, NotFoundError, TransientError
from calsync.http import request_with_retry
from calsync.model import When
from calsync.sources.google import BASE_URL, _error_reasons, _is_rate_limited
from calsync.sources.google_auth import TokenProvider
from calsync.sources.google_map import parse_when

READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
BATCH_URL = "https://www.googleapis.com/batch/calendar/v3"
BATCH_SIZE = 50
KEY_PROPERTY = "calsync_key"  # extendedProperties.private: an agent's identity for an event
MAX_ALARM_MINUTES = 40320  # Google's limit: four weeks before the start

# Google's reasons for a token without the write scope (as opposed to a calendar the account
# may only read, which is ``requiredAccessLevel`` / ``forbidden``).
_SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})


class WriteConflict(CalsyncError):
    """The event changed since the caller read it (412, or a mismatch calsync noticed)."""


class NoWriteAccess(AuthError):
    """The stored grant has no write scope: a person runs ``calsync auth <account> --write``."""


@dataclass(frozen=True, slots=True)
class EventFields:
    """The fields calsync lets a person or an agent set. ``None``: not set (a patch leaves it)."""

    title: str | None = None
    start: When | None = None
    end: When | None = None
    location: str | None = None
    description: str | None = None
    # Minutes before the start, each 0..MAX_ALARM_MINUTES; () = no reminder. ``alarms_default``:
    # the calendar's default reminders (then ``alarms`` is ignored).
    alarms: tuple[int, ...] | None = None
    alarms_default: bool = False
    key: str | None = None


def _when_json(when: When) -> dict[str, Any]:
    if isinstance(when.value, datetime):
        # ``date: None`` clears the other form when a patch turns an all-day event into a timed one.
        return {"dateTime": when.value.isoformat(), "timeZone": when.tzid, "date": None}
    return {"date": when.value.isoformat(), "dateTime": None, "timeZone": None}


def check_fields(fields: EventFields) -> list[str]:
    """What is wrong with ``fields`` as a person or an agent gave them (empty: fine)."""
    problems: list[str] = []
    if (fields.start is None) != (fields.end is None):
        problems.append("start and end: give both or neither")
    if fields.start is not None and fields.end is not None:
        if fields.start.all_day != fields.end.all_day:
            problems.append("start and end: both dates (all day) or both times")
        elif fields.end.utc() <= fields.start.utc():
            problems.append("end: must be after the start")
    if fields.alarms is not None and not fields.alarms_default:
        bad = [m for m in fields.alarms if not 0 <= m <= MAX_ALARM_MINUTES]
        if bad:
            problems.append(f"alarms: minutes before the start, 0 to {MAX_ALARM_MINUTES}")
        if len(fields.alarms) > 5:
            problems.append("alarms: at most 5")
    if fields.title is not None and len(fields.title) > 1024:
        problems.append("title: at most 1024 characters")
    return problems


def to_resource(fields: EventFields, *, patch: bool) -> dict[str, Any]:
    """The Google event resource for ``fields``: only the set fields when ``patch``; an insert
    gets an empty title and the calendar's default reminders for whatever is not set."""
    body: dict[str, Any] = {}
    if fields.title is not None or not patch:
        body["summary"] = fields.title or ""
    if fields.start is not None and fields.end is not None:
        start, end = _when_json(fields.start), _when_json(fields.end)
        if not patch:  # an insert has nothing to clear
            start = {k: v for k, v in start.items() if v is not None}
            end = {k: v for k, v in end.items() if v is not None}
        body["start"], body["end"] = start, end
    if fields.location is not None:
        body["location"] = fields.location
    if fields.description is not None:
        body["description"] = fields.description
    if fields.alarms_default or (fields.alarms is None and not patch):
        body["reminders"] = {"useDefault": True, "overrides": []}
    elif fields.alarms is not None:
        overrides = [{"method": "popup", "minutes": m} for m in sorted(set(fields.alarms))]
        body["reminders"] = {"useDefault": False, "overrides": overrides}
    if fields.key is not None:
        body["extendedProperties"] = {"private": {KEY_PROPERTY: fields.key}}
    return body


def event_key(raw: Mapping[str, Any]) -> str | None:
    private = (raw.get("extendedProperties") or {}).get("private") or {}
    key = private.get(KEY_PROPERTY)
    return key if isinstance(key, str) and key else None


def from_resource(raw: Mapping[str, Any], default_tz: str) -> EventFields:
    """The editable fields of a Google event resource."""
    reminders = raw.get("reminders") or {}
    use_default = bool(reminders.get("useDefault", True)) if reminders else True
    alarms: tuple[int, ...] | None = None
    if not use_default:
        alarms = tuple(
            sorted(
                int(r.get("minutes", 0))
                for r in reminders.get("overrides") or []
                if r.get("method") == "popup"
            )
        )
    return EventFields(
        title=raw.get("summary") or "",
        start=parse_when(raw["start"], default_tz) if "start" in raw else None,
        end=parse_when(raw["end"], default_tz) if "end" in raw else None,
        location=raw.get("location") or "",
        description=raw.get("description") or "",
        alarms=alarms,
        alarms_default=use_default,
        key=event_key(raw),
    )


def _when_text(when: When | None) -> str | None:
    return None if when is None else when.value.isoformat()


def _alarms_text(fields: EventFields) -> str:
    if fields.alarms_default:
        return "default"
    return ",".join(str(m) for m in fields.alarms or ())


def field_changes(before: EventFields, after: EventFields) -> list[dict[str, Any]]:
    """What a write changed, field by field, for the history and the UI ("time 10:00 → 09:30").
    Only the fields ``after`` sets count; values are plain text (times in ISO 8601)."""
    changes: list[dict[str, Any]] = []
    if after.title is not None and after.title != before.title:
        changes.append({"field": "title", "before": before.title, "after": after.title})
    if after.start is not None and (after.start != before.start or after.end != before.end):
        changes.append(
            {
                "field": "time",
                "before": [_when_text(before.start), _when_text(before.end)],
                "after": [_when_text(after.start), _when_text(after.end)],
            }
        )
    if after.location is not None and after.location != before.location:
        changes.append({"field": "location", "before": before.location, "after": after.location})
    if after.description is not None and after.description != before.description:
        changes.append({"field": "description", "before": None, "after": None})  # text not kept
    alarms_set = after.alarms_default or after.alarms is not None
    if alarms_set and _alarms_text(after) != _alarms_text(before):
        changes.append({"field": "alarms", "before": _alarms_text(before), "after": _alarms_text(after)})
    return changes


def instance_start(when: When) -> str:
    """The ``originalStart`` for ``events.instances``: RFC 3339 for a time, the date otherwise."""
    return when.value.isoformat()


def as_when(value: datetime | date, tzid: str | None) -> When:
    if isinstance(value, datetime):
        zone = tzid or "UTC"
        aware = value if value.tzinfo is not None else value.replace(tzinfo=ZoneInfo(zone))
        return When(aware.astimezone(ZoneInfo(zone)), zone)
    return When(value)


# ----- the client --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchCall:
    method: str  # POST | PATCH | DELETE
    calendar_id: str
    event_id: str | None = None  # None for an insert
    body: Mapping[str, Any] | None = None
    etag: str | None = None


@dataclass(slots=True)
class BatchReply:
    status: int
    body: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def gone(self) -> bool:
        """No such event (a delete of it has nothing left to do)."""
        return self.status in (404, 410)

    def reason(self) -> str:
        error = self.body.get("error") if isinstance(self.body, dict) else None
        if isinstance(error, dict):
            errors = error.get("errors") or []
            if errors and isinstance(errors[0], dict) and errors[0].get("reason"):
                return str(errors[0]["reason"])
            if error.get("status"):
                return str(error["status"])
        return f"HTTP {self.status}"


def _check(response: httpx.Response, what: str) -> None:
    """Raise the calsync error for a failed write or read of one event."""
    status = response.status_code
    if status < 400:
        return
    if status == 412:
        raise WriteConflict(f"{what}: the event changed in Google meanwhile")
    if status == 401:
        raise AuthError(f"Google API 401 for {what}")
    if status == 403 and _is_rate_limited(response):
        raise TransientError(f"Google API rate limit for {what}")
    if status == 403:
        reasons = _error_reasons(response)
        if set(reasons) & _SCOPE_REASONS or _scope_detail(response):
            raise NoWriteAccess(f"Google API 403 for {what}: no write access")
        raise ForbiddenError(f"Google API 403 for {what}: {reasons[0] if reasons else 'forbidden'}")
    if status in (404, 410):
        raise NotFoundError(what)
    raise CalsyncError(f"Google API {status} for {what}")


def _scope_detail(response: httpx.Response) -> bool:
    """Google's newer error bodies carry the scope reason in ``error.details[].reason``."""
    try:
        details = response.json()["error"].get("details") or []
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    return any(isinstance(d, dict) and d.get("reason") in _SCOPE_REASONS for d in details)


def _json(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        raise TransientError(f"Google API returned a non-JSON body for {what}") from None
    if not isinstance(data, dict):
        raise TransientError(f"Google API returned an unexpected body for {what}")
    return data


class GoogleWriter:
    """Reads and writes single events of one Google account."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: TokenProvider,
        *,
        base_url: str = BASE_URL,
        batch_url: str = BATCH_URL,
    ) -> None:
        self._client = client
        self._token = token
        self._base = base_url.rstrip("/")
        self._batch_url = batch_url
        self._prefix = urlsplit(self._base).path  # "/calendar/v3": batch parts are path-only

    @property
    def scopes(self) -> frozenset[str] | None:
        return getattr(self._token, "scopes", None)

    def _path(self, calendar_id: str, event_id: str | None = None, tail: str = "") -> str:
        path = f"/calendars/{quote(calendar_id, safe='')}/events"
        if event_id is not None:
            path += f"/{quote(event_id, safe='')}"
        return path + tail

    async def _send(self, method: str, path: str, kind: str, **kwargs: Any) -> httpx.Response:
        response = await self._once(method, path, kind, **kwargs)
        if response.status_code == 401:
            invalidate = getattr(self._token, "invalidate", None)
            if invalidate is not None and invalidate():
                response = await self._once(method, path, kind, **kwargs)
        return response

    async def _once(self, method: str, url: str, kind: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._token()}", **kwargs.pop("headers", {})}
        target = url if url.startswith("http") else f"{self._base}{url}"
        return await request_with_retry(self._client, method, target, kind=kind, headers=headers, **kwargs)

    async def get(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        path = self._path(calendar_id, event_id)
        response = await self._send("GET", path, "google-event-get")
        _check(response, path)
        return _json(response, path)

    async def instance(self, calendar_id: str, series_id: str, original_start: str) -> dict[str, Any]:
        """The one instance of ``series_id`` that originally starts at ``original_start``."""
        path = self._path(calendar_id, series_id, "/instances")
        response = await self._send(
            "GET",
            path,
            "google-event-instance",
            params={"originalStart": original_start, "showDeleted": "false", "maxResults": "1"},
        )
        _check(response, path)
        items = _json(response, path).get("items") or []
        if not items:
            raise NotFoundError(f"{path}: no instance at {original_start}")
        return items[0]

    async def insert(self, calendar_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        path = self._path(calendar_id)
        response = await self._send(
            "POST", path, "google-event-insert", params={"sendUpdates": "none"}, json=dict(body)
        )
        _check(response, path)
        return _json(response, path)

    async def patch(
        self, calendar_id: str, event_id: str, body: Mapping[str, Any], *, etag: str
    ) -> dict[str, Any]:
        path = self._path(calendar_id, event_id)
        response = await self._send(
            "PATCH",
            path,
            "google-event-patch",
            params={"sendUpdates": "none"},
            json=dict(body),
            headers={"If-Match": etag},
        )
        _check(response, path)
        return _json(response, path)

    async def delete(self, calendar_id: str, event_id: str, *, etag: str | None) -> None:
        """Delete the event (an instance id cancels that one occurrence). Already gone: done."""
        path = self._path(calendar_id, event_id)
        headers = {"If-Match": etag} if etag else {}
        response = await self._send(
            "DELETE", path, "google-event-delete", params={"sendUpdates": "none"}, headers=headers
        )
        if response.status_code in (404, 410):
            return
        _check(response, path)

    async def batch(self, calls: Sequence[BatchCall]) -> list[BatchReply]:
        """Run ``calls`` through the batch endpoint, ``BATCH_SIZE`` per request; one reply per
        call in order. A failed batch request (transport, 5xx after retries, 401) raises; a
        failed call is only its own reply."""
        replies: list[BatchReply] = []
        for at in range(0, len(calls), BATCH_SIZE):
            replies.extend(await self._batch_once(calls[at : at + BATCH_SIZE]))
        return replies

    def _part(self, index: int, call: BatchCall) -> str:
        path = self._prefix + self._path(call.calendar_id, call.event_id) + "?sendUpdates=none"
        lines = [
            "Content-Type: application/http",
            f"Content-ID: <item{index}>",
            "",
            f"{call.method} {path} HTTP/1.1",
        ]
        if call.etag:
            lines.append(f"If-Match: {call.etag}")
        if call.body is not None:
            lines += ["Content-Type: application/json; charset=UTF-8", "", json.dumps(call.body)]
        else:
            lines.append("")
        return "\r\n".join(lines) + "\r\n"

    async def _batch_once(self, calls: Sequence[BatchCall]) -> list[BatchReply]:
        boundary = f"calsync_{secrets.token_hex(8)}"
        body = "".join(f"--{boundary}\r\n{self._part(i, c)}" for i, c in enumerate(calls))
        body += f"--{boundary}--\r\n"
        response = await self._send(
            "POST",
            self._batch_url,
            "google-batch",
            content=body.encode(),
            headers={"Content-Type": f"multipart/mixed; boundary={boundary}"},
        )
        if response.status_code >= 400:
            _check(response, "batch")
        replies = parse_batch(response)
        by_id = {index: reply for index, reply in replies}
        if set(by_id) != set(range(len(calls))):
            raise TransientError("Google batch reply does not match the request")
        return [by_id[i] for i in range(len(calls))]


_STATUS_LINE = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})")
_ITEM = re.compile(r"<?response-item(\d+)>?")


def _split_head(text: str) -> tuple[str, str]:
    for sep in ("\r\n\r\n", "\n\n"):
        if sep in text:
            head, rest = text.split(sep, 1)
            return head, rest
    return text, ""


def parse_batch(response: httpx.Response) -> list[tuple[int, BatchReply]]:
    """(request index, reply) for every part of a batch response."""
    ctype = response.headers.get("content-type", "")
    match = re.search(r'boundary="?([^";]+)"?', ctype)
    if not match:
        raise TransientError("Google batch reply has no multipart boundary")
    boundary = match.group(1)
    text = response.content.decode("utf-8", "replace")
    out: list[tuple[int, BatchReply]] = []
    for chunk in text.split(f"--{boundary}"):
        chunk = chunk.strip("\r\n")
        if not chunk or chunk.startswith("--"):
            continue
        part_head, http = _split_head(chunk)
        found = _ITEM.search(part_head)
        if not found:
            continue
        status_head, payload = _split_head(http.lstrip("\r\n"))
        status = _STATUS_LINE.match(status_head)
        if not status:
            raise TransientError("Google batch reply part has no status line")
        payload = payload.strip()
        body: dict[str, Any] = {}
        if payload:
            try:
                parsed = json.loads(payload)
            except ValueError:
                parsed = {}
            body = parsed if isinstance(parsed, dict) else {}
        out.append((int(found.group(1)), BatchReply(int(status.group(1)), body)))
    return out
