"""In-memory fake of the Google Calendar API, mounted on httpx.MockTransport.

Implements the parts calsync relies on: calendarList and events.list with
syncToken semantics, pagination, 410 on expired tokens, and dropping
nextSyncToken when the ``fields`` mask omits it (as Google does).

Writes (phase C): events.get / insert / patch / delete and events.instances, with etags
(``If-Match`` → 412), ``sendUpdates`` required, a read-only role refused (403
``requiredAccessLevel``), a grant without the write scope refused (``write_scope = False``:
403 ``insufficientPermissions``), and the batch endpoint (``multipart/mixed``).
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

BASE_URL = "https://google.test/calendar/v3"
BATCH_URL = "https://google.test/batch/calendar/v3"
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


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
        self._roles: dict[str, str] = {}  # calendarList accessRole
        self._primary: str | None = None  # the account's own calendar (its id is the email)
        self.write_scope = True  # False: the grant has no calendar.events scope
        self.writes: list[tuple[str, str]] = []  # (method, path) of every write, batch parts too
        self.batches = 0  # batch requests received
        self.fail: dict[str, int] = {}  # event id -> HTTP status its next write answers
        self._new = 0

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
        access_role: str = "owner",
        primary: bool = False,
    ) -> None:
        self._removed.pop(cid, None)
        self._calendars[cid] = _Calendar(summary, summary_override, tz, default_reminders or [], self._tick())
        self._roles[cid] = access_role
        if primary:
            self._primary = cid

    def remove_calendar(self, cid: str) -> None:
        del self._calendars[cid]
        self._removed[cid] = self._tick()

    def set_default_reminders(self, cid: str, reminders: list[dict[str, Any]]) -> None:
        cal = self._calendars[cid]
        cal.default_reminders = reminders
        cal.changed_at = self._tick()

    def put_event(self, cid: str, event: dict[str, Any]) -> None:
        """Store ``event``; a live one gets a fresh etag and ``updated`` (a cancelled stub stays
        minimal, as Google sends it)."""
        seq = self._tick()
        stored = dict(event)
        if stored.get("status") != "cancelled":
            stored["etag"] = f'"{seq}"'
            stored["updated"] = (_EPOCH + timedelta(seconds=seq)).isoformat().replace("+00:00", "Z")
        self._calendars[cid].events[event["id"]] = (seq, stored)

    def event(self, cid: str, event_id: str) -> dict[str, Any] | None:
        """The stored event (a copy), or None."""
        pair = self._calendars[cid].events.get(event_id)
        return json.loads(json.dumps(pair[1])) if pair else None

    def events(self, cid: str) -> list[dict[str, Any]]:
        """Every stored event of ``cid``, cancelled stubs too (copies)."""
        return [json.loads(json.dumps(e)) for _, e in self._calendars[cid].events.values()]

    def touch(self, cid: str, event_id: str, **changes: Any) -> None:
        """Change an event as another client would (a new etag and ``updated``)."""
        event = self.event(cid, event_id)
        assert event is not None
        event.update(changes)
        event.pop("etag", None)
        self.put_event(cid, event)

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
        """Short hash of the path plus every param except the paging cursors."""
        pairs = sorted(
            (k, v) for k, v in request.url.params.multi_items() if k not in ("syncToken", "pageToken")
        )
        return hashlib.sha1(json.dumps([request.url.path, pairs]).encode()).hexdigest()[:8]

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
        if request.url.path == "/batch/calendar/v3":
            return self._batch(request)
        if request.method != "GET" or "/instances" in request.url.path or _single(request.url.path):
            return self._write(
                request.method,
                request.url.path,
                dict(request.url.params),
                dict(request.headers),
                request.content,
            )
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
                    "accessRole": self._roles.get(cid, "owner"),
                    **({"primary": True} if cid == self._primary else {}),
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

    # ----- writes -------------------------------------------------------
    def _err(self, status: int, reason: str) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"code": status, "errors": [{"reason": reason}], "message": reason}}
        )

    def _write(
        self, method: str, path: str, params: dict[str, str], headers: dict[str, str], content: bytes
    ) -> httpx.Response:
        match = re.fullmatch(r"/calendar/v3/calendars/([^/]+)/events(?:/([^/]+))?(/instances)?", path)
        if not match:
            return httpx.Response(404, json={"error": f"unknown path {path}"})
        cid, eid, instances = unquote(match.group(1)), match.group(2), match.group(3)
        eid = unquote(eid) if eid else None
        if cid not in self._calendars:
            return self._err(404, "notFound")
        cal = self._calendars[cid]
        if method == "GET":
            if instances:
                return self._instances(cid, eid or "", params.get("originalStart", ""))
            found = cal.events.get(eid or "")
            if found is None or found[1].get("status") == "cancelled":
                return self._err(404, "notFound")
            return httpx.Response(200, json=json.loads(json.dumps(found[1])))
        lower = {k.lower(): v for k, v in headers.items()}
        if not self.write_scope:
            return self._err(403, "insufficientPermissions")
        if self._roles.get(cid, "owner") not in ("owner", "writer"):
            return self._err(403, "requiredAccessLevel")
        if params.get("sendUpdates") != "none":
            return self._err(400, "sendUpdates must be none in this fake")
        self.writes.append((method, path))
        if eid is not None and eid in self.fail:
            return self._err(self.fail.pop(eid), "failed")
        if method == "POST" and eid is None:
            body = json.loads(content or b"{}")
            self._new += 1
            body["id"] = body.get("id") or f"new{self._new}"
            if body["id"] in cal.events:
                return self._err(409, "duplicate")
            body.setdefault("status", "confirmed")
            body.setdefault("organizer", {"self": True})
            body["created"] = (_EPOCH + timedelta(seconds=self._seq + 1)).isoformat().replace("+00:00", "Z")
            self.put_event(cid, _strip_nulls(body))
            return httpx.Response(200, json=self.event(cid, body["id"]))
        current = cal.events.get(eid or "")
        virtual = None
        if current is None or current[1].get("status") == "cancelled":
            virtual = self._virtual_instance(cid, eid or "")
            if virtual is None:
                return self._err(
                    404 if current is None else 410, "notFound" if current is None else "deleted"
                )
        stored = current[1] if virtual is None else virtual
        if "if-match" in lower and lower["if-match"] != stored["etag"]:
            return self._err(412, "conditionNotMet")
        if method == "DELETE":
            if stored.get("recurringEventId"):
                self.put_event(
                    cid,
                    {
                        "id": stored["id"],
                        "status": "cancelled",
                        "recurringEventId": stored["recurringEventId"],
                        "originalStartTime": stored["originalStartTime"],
                    },
                )
            else:
                self.cancel_event(cid, stored["id"])
            return httpx.Response(204)
        if method == "PATCH":
            merged = _merge(json.loads(json.dumps(stored)), json.loads(content or b"{}"))
            merged.pop("etag", None)
            merged.pop("updated", None)
            self.put_event(cid, merged)
            return httpx.Response(200, json=self.event(cid, merged["id"]))
        return self._err(405, "method")

    def _virtual_instance(self, cid: str, iid: str) -> dict[str, Any] | None:
        """An instance of a series that has no stored exception yet (``<series>_<YYYYMMDDTHHMMSSZ>``)."""
        if "_" not in iid:
            return None
        series_id, stamp = iid.rsplit("_", 1)
        master = self._calendars[cid].events.get(series_id)
        if master is None or not master[1].get("recurrence"):
            return None
        return self._instance_of(master[1], stamp)

    @staticmethod
    def _instance_of(master: dict[str, Any], stamp: str) -> dict[str, Any] | None:
        start = master["start"]
        if "date" in start:
            if not re.fullmatch(r"\d{8}", stamp):
                return None
            day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
            span = datetime.fromisoformat(master["end"]["date"]) - datetime.fromisoformat(start["date"])
            end_day = (datetime.fromisoformat(day) + span).date().isoformat()
            new_start, new_end, original = {"date": day}, {"date": end_day}, {"date": day}
        else:
            try:
                at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            except ValueError:
                return None
            first = datetime.fromisoformat(start["dateTime"])
            span = datetime.fromisoformat(master["end"]["dateTime"]) - first
            tz = start.get("timeZone")
            local = at.astimezone(first.tzinfo)
            new_start = {"dateTime": local.isoformat(), **({"timeZone": tz} if tz else {})}
            new_end = {"dateTime": (local + span).isoformat(), **({"timeZone": tz} if tz else {})}
            original = dict(new_start)
        instance = {k: v for k, v in master.items() if k not in ("recurrence", "id", "etag")}
        instance.update(
            id=f"{master['id']}_{stamp}",
            recurringEventId=master["id"],
            originalStartTime=original,
            start=new_start,
            end=new_end,
            etag=master["etag"],
        )
        return instance

    def _instances(self, cid: str, series_id: str, original_start: str) -> httpx.Response:
        master = self._calendars[cid].events.get(series_id)
        if master is None or not master[1].get("recurrence"):
            return self._err(404, "notFound")
        if "T" in original_start:
            at = datetime.fromisoformat(original_start.replace("Z", "+00:00")).astimezone(UTC)
            stamp = at.strftime("%Y%m%dT%H%M%SZ")
        else:
            stamp = original_start.replace("-", "")
        iid = f"{series_id}_{stamp}"
        stored = self._calendars[cid].events.get(iid)
        if stored is not None:
            items = [] if stored[1].get("status") == "cancelled" else [json.loads(json.dumps(stored[1]))]
        else:
            virtual = self._instance_of(master[1], stamp)
            items = [virtual] if virtual else []
        return httpx.Response(200, json={"items": items})

    def _batch(self, request: httpx.Request) -> httpx.Response:
        self.batches += 1
        match = re.search(r"boundary=([^;]+)", request.headers.get("content-type", ""))
        assert match, "a batch request needs a boundary"
        boundary = match.group(1)
        out = []
        for chunk in request.content.decode().split(f"--{boundary}"):
            chunk = chunk.strip("\r\n")
            if not chunk or chunk == "--":
                continue
            head, http = chunk.split("\r\n\r\n", 1)
            cid_match = re.search(r"Content-ID: <([^>]+)>", head)
            lines = http.split("\r\n")
            method, target, _ = lines[0].split(" ")
            headers: dict[str, str] = {}
            body = b""
            for i, line in enumerate(lines[1:], 1):
                if line == "":
                    body = "\r\n".join(lines[i + 1 :]).strip().encode()
                    break
                name, value = line.split(":", 1)
                headers[name.strip()] = value.strip()
            parts = urlsplit(target)
            reply = self._write(method, parts.path, dict(parse_qsl(parts.query)), headers, body)
            text = reply.content.decode() if reply.content else ""
            status_text = {200: "OK", 204: "No Content"}.get(reply.status_code, "Error")
            out.append(
                f"--resp\r\nContent-Type: application/http\r\n"
                f"Content-ID: <response-{cid_match.group(1) if cid_match else ''}>\r\n\r\n"
                f"HTTP/1.1 {reply.status_code} {status_text}\r\nContent-Type: application/json\r\n\r\n{text}\r\n"
            )
        payload = "".join(out) + "--resp--\r\n"
        return httpx.Response(
            200, content=payload.encode(), headers={"Content-Type": "multipart/mixed; boundary=resp"}
        )


def _single(path: str) -> bool:
    """A path naming one event (``.../events/<id>``)."""
    return re.fullmatch(r"/calendar/v3/calendars/[^/]+/events/[^/]+", path) is not None


def _strip_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    return value


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Google's patch: objects merge, ``null`` removes, everything else replaces."""
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = _strip_nulls(value)
    return base
