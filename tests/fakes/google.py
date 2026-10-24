"""In-memory fake of the Google Calendar API, mounted on httpx.MockTransport.

Implements the parts calsync relies on: calendarList and events.list with
syncToken semantics, pagination, 410 on expired tokens, and dropping
nextSyncToken when the ``fields`` mask omits it (as Google does).
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

BASE_URL = "https://google.test/calendar/v3"


@dataclass
class _Calendar:
    summary: str
    summary_override: str | None
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
        self._removed: dict[str, int] = {}

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
        summary_override: str | None = None,
    ) -> None:
        self._removed.pop(cid, None)
        self._calendars[cid] = _Calendar(summary, summary_override, tz, default_reminders or [], self._tick())

    def remove_calendar(self, cid: str) -> None:
        del self._calendars[cid]
        self._removed[cid] = self._tick()

    def set_default_reminders(self, cid: str, reminders: list[dict[str, Any]]) -> None:
        cal = self._calendars[cid]
        cal.default_reminders = reminders
        cal.changed_at = self._tick()

    def put_event(self, cid: str, event: dict[str, Any]) -> None:
        self._calendars[cid].events[event["id"]] = (self._tick(), dict(event))

    def cancel_event(self, cid: str, event_id: str) -> None:
        _, old = self._calendars[cid].events[event_id]
        # Google returns only a stub for cancelled events.
        stub = {"id": event_id, "status": "cancelled"}
        stub.update({k: old[k] for k in ("recurringEventId", "originalStartTime") if k in old})
        self.put_event(cid, stub)

    def expire_tokens(self) -> None:
        self.generation += 1

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # ----- protocol -----------------------------------------------------
    @staticmethod
    def _fingerprint(request: httpx.Request) -> str:
        """Short hash of every request param except the paging cursors."""
        pairs = sorted(
            (k, v) for k, v in request.url.params.multi_items() if k not in ("syncToken", "pageToken")
        )
        return hashlib.sha1(json.dumps(pairs).encode()).hexdigest()[:8]

    def _token(self, request: httpx.Request) -> str:
        return f"g{self.generation}-{self._seq}-{self._fingerprint(request)}"

    def _since(self, request: httpx.Request) -> int | None:
        """Sequence number encoded in a sync token; None for a full listing.

        Raises ValueError for a malformed token or one issued for different
        request params (HTTP 400), LookupError for an expired one (HTTP 410).
        """
        token = request.url.params.get("syncToken")
        if token is None:
            return None
        parts = token.split("-")
        if len(parts) != 3 or not parts[0].startswith("g"):
            raise ValueError("malformed")
        try:
            gen, seq = int(parts[0][1:]), int(parts[1])
        except ValueError:
            raise ValueError("malformed") from None
        if gen != self.generation:
            raise LookupError
        if parts[2] != self._fingerprint(request):
            raise ValueError("params differ from the ones the token was issued for")
        return seq

    def _page(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        params = request.url.params
        offset = int(params.get("pageToken", "0"))
        chunk = items[offset : offset + self.page_size]
        body: dict[str, Any] = {"items": chunk}
        if offset + self.page_size < len(items):
            body["nextPageToken"] = str(offset + self.page_size)
        elif "nextSyncToken" in params.get("fields", "nextSyncToken"):
            body["nextSyncToken"] = self._token(request)
        return httpx.Response(200, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"error": "unauthenticated"})
        path = request.url.path.removeprefix("/calendar/v3")
        params = request.url.params
        if "syncToken" in params and any(k in params for k in ("timeMin", "timeMax", "updatedMin")):
            return httpx.Response(400, json={"error": {"code": 400, "message": "syncToken conflicts"}})
        try:
            since = self._since(request)
        except ValueError as exc:
            return httpx.Response(400, json={"error": {"code": 400, "message": str(exc)}})
        except LookupError:
            return httpx.Response(
                410, json={"error": {"code": 410, "message": "Sync token is no longer valid"}}
            )
        if path == "/users/me/calendarList":
            items = [
                {
                    "id": cid,
                    "summary": c.summary,
                    **({"summaryOverride": c.summary_override} if c.summary_override else {}),
                    "timeZone": c.tz,
                    "defaultReminders": c.default_reminders,
                }
                for cid, c in self._calendars.items()
                if since is None or c.changed_at > since
            ]
            if since is not None:
                items += [{"id": cid, "deleted": True} for cid, at in self._removed.items() if at > since]
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
