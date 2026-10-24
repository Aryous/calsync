"""In-memory fake of the Google Calendar API, mounted on httpx.MockTransport.

Implements the parts calsync relies on: calendarList and events.list with
syncToken semantics, pagination, 410 on expired tokens, and dropping
nextSyncToken when the ``fields`` mask omits it (as Google does).
"""

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

BASE_URL = "https://google.test/calendar/v3"


@dataclass
class _Calendar:
    summary: str
    tz: str
    default_reminders: list[dict[str, Any]]
    changed_at: int
    events: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)


class FakeGoogle:
    def __init__(self, *, page_size: int = 2500) -> None:
        self.page_size = page_size
        self.generation = 0
        self._seq = 0
        self._calendars: dict[str, _Calendar] = {}

    # ----- test helpers -------------------------------------------------
    def _tick(self) -> int:
        self._seq += 1
        return self._seq

    def add_calendar(
        self,
        cid: str,
        summary: str,
        *,
        tz: str = "Asia/Shanghai",
        default_reminders: list[dict[str, Any]] | None = None,
    ) -> None:
        self._calendars[cid] = _Calendar(summary, tz, default_reminders or [], self._tick())

    def set_default_reminders(self, cid: str, reminders: list[dict[str, Any]]) -> None:
        cal = self._calendars[cid]
        cal.default_reminders = reminders
        cal.changed_at = self._tick()

    def put_event(self, cid: str, event: dict[str, Any]) -> None:
        self._calendars[cid].events[event["id"]] = (self._tick(), dict(event))

    def cancel_event(self, cid: str, event_id: str) -> None:
        _, old = self._calendars[cid].events[event_id]
        self.put_event(cid, {**old, "status": "cancelled"})

    def expire_tokens(self) -> None:
        self.generation += 1

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ----- protocol -----------------------------------------------------
    def _token(self) -> str:
        return f"g{self.generation}-{self._seq}"

    def _since(self, token: str | None) -> int | None:
        """Sequence number encoded in a token; None for a full listing."""
        if token is None:
            return None
        gen, _, seq = token[1:].partition("-")
        if int(gen) != self.generation:
            raise LookupError
        return int(seq)

    def _page(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        params = request.url.params
        offset = int(params.get("pageToken", "0"))
        chunk = items[offset : offset + self.page_size]
        body: dict[str, Any] = {"items": chunk}
        if offset + self.page_size < len(items):
            body["nextPageToken"] = str(offset + self.page_size)
        elif "nextSyncToken" in params.get("fields", "nextSyncToken"):
            body["nextSyncToken"] = self._token()
        return httpx.Response(200, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"error": "unauthenticated"})
        path = request.url.path.removeprefix("/calendar/v3")
        try:
            since = self._since(request.url.params.get("syncToken"))
        except LookupError:
            return httpx.Response(
                410, json={"error": {"code": 410, "message": "Sync token is no longer valid"}}
            )
        if path == "/users/me/calendarList":
            items = [
                {"id": cid, "summary": c.summary, "timeZone": c.tz, "defaultReminders": c.default_reminders}
                for cid, c in self._calendars.items()
                if since is None or c.changed_at > since
            ]
            return self._page(request, items)
        if path.startswith("/calendars/") and path.endswith("/events"):
            cid = unquote(path[len("/calendars/") : -len("/events")])
            if cid not in self._calendars:
                return httpx.Response(404, json={"error": "notFound"})
            events = self._calendars[cid].events.values()
            items = [
                json.loads(json.dumps(e))
                for seq, e in sorted(events, key=lambda p: p[0])
                if since is None or seq > since
            ]
            return self._page(request, items)
        return httpx.Response(404, json={"error": f"unknown path {path}"})
