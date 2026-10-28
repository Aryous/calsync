"""The Google write client against the fake Google (writes, etags, batch)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from calsync.errors import AuthError, ForbiddenError, NotFoundError, TransientError
from calsync.http import RequestLog, make_client
from calsync.model import When
from calsync.sources.google_auth import RefreshTokenProvider, StaticToken
from calsync.sources.google_write import (
    BATCH_SIZE,
    BatchCall,
    EventFields,
    GoogleWriter,
    NoWriteAccess,
    WriteConflict,
    check_fields,
    field_changes,
    from_resource,
    to_resource,
)
from tests.factories import google_event
from tests.fakes.google import BASE_URL, BATCH_URL, FakeGoogle

pytestmark = pytest.mark.anyio
CAL = "family@group.calendar.google.com"
TZ = "Asia/Shanghai"
SH = ZoneInfo(TZ)


def at(day: str, hm: str) -> When:
    return When(datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=SH), TZ)


def seeded(**kw: object) -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(CAL, "Family", **kw)  # type: ignore[arg-type]
    fake.put_event(CAL, google_event("e1", "Walk", "2026-10-25T09:00:00+08:00", "2026-10-25T10:00:00+08:00"))
    fake.put_event(
        CAL,
        google_event(
            "s1",
            "Reminder",
            "2026-10-20T08:00:00+08:00",
            "2026-10-20T08:05:00+08:00",
            recurrence=["RRULE:FREQ=DAILY"],
        ),
    )
    return fake


def writer(fake: FakeGoogle, log: RequestLog | None = None, token: object = None) -> GoogleWriter:
    client = make_client(log or RequestLog(), transport=fake.transport())
    return GoogleWriter(client, token or StaticToken("t"), base_url=BASE_URL, batch_url=BATCH_URL)  # type: ignore[arg-type]


# ----- mapping -----------------------------------------------------------------------------------


def test_timed_resource_keeps_its_zone_and_clears_the_date_on_patch() -> None:
    body = to_resource(
        EventFields(start=at("2026-10-25", "09:30"), end=at("2026-10-25", "10:00")), patch=True
    )
    assert body == {
        "start": {"dateTime": "2026-10-25T09:30:00+08:00", "timeZone": TZ, "date": None},
        "end": {"dateTime": "2026-10-25T10:00:00+08:00", "timeZone": TZ, "date": None},
    }


def test_all_day_resource_clears_the_time_on_patch_and_insert_has_no_nulls() -> None:
    fields = EventFields(title="Trip", start=When(date(2026, 11, 1)), end=When(date(2026, 11, 3)))
    assert to_resource(fields, patch=True)["start"] == {
        "date": "2026-11-01",
        "dateTime": None,
        "timeZone": None,
    }
    insert = to_resource(fields, patch=False)
    assert insert["start"] == {"date": "2026-11-01"} and insert["end"] == {"date": "2026-11-03"}
    assert insert["summary"] == "Trip"
    assert insert["reminders"] == {"useDefault": True, "overrides": []}  # nothing set: the default


def test_alarms_map_to_reminders() -> None:
    assert to_resource(EventFields(alarms=()), patch=True) == {
        "reminders": {"useDefault": False, "overrides": []}
    }
    assert to_resource(EventFields(alarms=(10, 0, 10)), patch=True)["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 0}, {"method": "popup", "minutes": 10}],
    }
    assert to_resource(EventFields(alarms_default=True), patch=True)["reminders"]["useDefault"] is True


def test_a_patch_carries_only_the_set_fields() -> None:
    assert to_resource(EventFields(title="New"), patch=True) == {"summary": "New"}
    assert to_resource(EventFields(key="k1"), patch=True) == {
        "extendedProperties": {"private": {"calsync_key": "k1"}}
    }


def test_from_resource_round_trip() -> None:
    raw = google_event(
        "e1",
        "Walk",
        "2026-10-25T09:00:00+08:00",
        "2026-10-25T10:00:00+08:00",
        location="Park",
        extendedProperties={"private": {"calsync_key": "walk-1"}},
    )
    fields = from_resource(raw, "UTC")
    assert fields.title == "Walk" and fields.location == "Park" and fields.key == "walk-1"
    assert fields.start == at("2026-10-25", "09:00") and fields.alarms == (0,) and not fields.alarms_default
    default = from_resource({**raw, "reminders": {"useDefault": True}}, "UTC")
    assert default.alarms_default and default.alarms is None


def test_check_fields() -> None:
    assert check_fields(EventFields(start=at("2026-10-25", "10:00"), end=at("2026-10-25", "09:00"))) == [
        "end: must be after the start"
    ]
    assert check_fields(EventFields(start=at("2026-10-25", "10:00"))) == [
        "start and end: give both or neither"
    ]
    assert check_fields(EventFields(start=When(date(2026, 10, 25)), end=at("2026-10-26", "09:00"))) == [
        "start and end: both dates (all day) or both times"
    ]
    assert check_fields(EventFields(alarms=(-5,))) == ["alarms: minutes before the start, 0 to 40320"]
    assert check_fields(EventFields(alarms=(1, 2, 3, 4, 5, 6))) == ["alarms: at most 5"]
    assert check_fields(EventFields(title="ok", alarms=(10,))) == []


def test_field_changes() -> None:
    before = EventFields(
        title="Walk", start=at("2026-10-25", "10:00"), end=at("2026-10-25", "11:00"), alarms=(0,)
    )
    after = EventFields(
        title="Walk", start=at("2026-10-25", "09:30"), end=at("2026-10-25", "10:30"), alarms=(10,)
    )
    assert field_changes(before, after) == [
        {
            "field": "time",
            "before": ["2026-10-25T10:00:00+08:00", "2026-10-25T11:00:00+08:00"],
            "after": ["2026-10-25T09:30:00+08:00", "2026-10-25T10:30:00+08:00"],
        },
        {"field": "alarms", "before": "0", "after": "10"},
    ]
    assert field_changes(before, EventFields(title="Walk")) == []


# ----- single writes -----------------------------------------------------------------------------


async def test_insert_patch_delete() -> None:
    fake = seeded()
    w = writer(fake)
    made = await w.insert(
        CAL,
        to_resource(
            EventFields(title="New", start=at("2026-10-26", "09:00"), end=at("2026-10-26", "09:30")),
            patch=False,
        ),
    )
    assert fake.event(CAL, made["id"])["summary"] == "New"
    live = await w.get(CAL, "e1")
    changed = await w.patch(CAL, "e1", {"summary": "Run"}, etag=live["etag"])
    assert changed["summary"] == "Run" and changed["etag"] != live["etag"]
    await w.delete(CAL, "e1", etag=changed["etag"])
    assert fake.event(CAL, "e1")["status"] == "cancelled"
    await w.delete(CAL, "e1", etag=None)  # already gone: done
    assert all(m != "GET" for m, _ in fake.writes)


async def test_a_stale_etag_is_a_conflict() -> None:
    fake = seeded()
    w = writer(fake)
    live = await w.get(CAL, "e1")
    fake.touch(CAL, "e1", summary="Changed elsewhere")
    with pytest.raises(WriteConflict):
        await w.patch(CAL, "e1", {"summary": "Mine"}, etag=live["etag"])
    with pytest.raises(WriteConflict):
        await w.delete(CAL, "e1", etag=live["etag"])
    assert fake.event(CAL, "e1")["summary"] == "Changed elsewhere"


async def test_no_write_scope_and_read_only_calendar() -> None:
    fake = seeded()
    fake.write_scope = False
    with pytest.raises(NoWriteAccess):
        await writer(fake).patch(CAL, "e1", {"summary": "x"}, etag='"1"')
    assert issubclass(NoWriteAccess, AuthError)
    ro = seeded(access_role="reader")
    with pytest.raises(ForbiddenError):
        await writer(ro).insert(CAL, {"summary": "x"})


async def test_newer_scope_error_body_is_no_write_access() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "status": "PERMISSION_DENIED",
                    "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
                }
            },
        )

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler))
    with pytest.raises(NoWriteAccess):
        await GoogleWriter(client, StaticToken("t"), base_url=BASE_URL).insert(CAL, {})


async def test_rate_limit_is_transient_and_missing_is_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={"error": {"code": 404, "errors": [{"reason": "notFound"}]}})
        return httpx.Response(403, json={"error": {"code": 403, "errors": [{"reason": "rateLimitExceeded"}]}})

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler))
    w = GoogleWriter(client, StaticToken("t"), base_url=BASE_URL)
    with pytest.raises(TransientError):
        await w.insert(CAL, {})
    with pytest.raises(NotFoundError):
        await w.get(CAL, "nope")


async def test_401_refreshes_the_token_once() -> None:
    fake = seeded()
    calls = {"token": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth.test":
            calls["token"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"a{calls['token']}",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/calendar.events",
                },
            )
        if request.headers["authorization"] == "Bearer a1":
            return httpx.Response(401, json={"error": "expired"})
        return fake.handler(request)

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler))
    token = RefreshTokenProvider(
        client, client_id="c", client_secret="s", refresh_token="r", token_url="https://oauth.test/token"
    )
    w = GoogleWriter(client, token, base_url=BASE_URL)
    assert (await w.get(CAL, "e1"))["id"] == "e1"
    assert calls["token"] == 2
    assert w.scopes == frozenset({"https://www.googleapis.com/auth/calendar.events"})


async def test_instance_of_a_series_and_changing_only_it() -> None:
    fake = seeded()
    w = writer(fake)
    inst = await w.instance(CAL, "s1", "2026-10-22T08:00:00+08:00")
    assert inst["recurringEventId"] == "s1" and inst["start"]["dateTime"].startswith("2026-10-22T08:00")
    await w.patch(CAL, inst["id"], {"summary": "Reminder (late)"}, etag=inst["etag"])
    assert fake.event(CAL, inst["id"])["summary"] == "Reminder (late)"
    assert fake.event(CAL, "s1")["summary"] == "Reminder"
    again = await w.instance(CAL, "s1", "2026-10-22T00:00:00Z")  # the same instant in UTC
    assert again["summary"] == "Reminder (late)"


async def test_deleting_an_instance_cancels_that_occurrence_only() -> None:
    fake = seeded()
    w = writer(fake)
    inst = await w.instance(CAL, "s1", "2026-10-23T08:00:00+08:00")
    await w.delete(CAL, inst["id"], etag=inst["etag"])
    stub = fake.event(CAL, inst["id"])
    assert stub["status"] == "cancelled" and stub["recurringEventId"] == "s1"
    assert fake.event(CAL, "s1")["status"] == "confirmed"


async def test_every_write_sends_no_notifications() -> None:
    fake = seeded()
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            seen.append(request.url.params.get("sendUpdates"))
        return fake.handler(request)

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler))
    w = GoogleWriter(client, StaticToken("t"), base_url=BASE_URL)
    live = await w.get(CAL, "e1")
    await w.patch(CAL, "e1", {"summary": "x"}, etag=live["etag"])
    await w.insert(CAL, {"summary": "y", "start": {"date": "2026-10-30"}, "end": {"date": "2026-10-31"}})
    await w.delete(CAL, "e1", etag=None)
    assert seen == ["none", "none", "none"]


# ----- batch -------------------------------------------------------------------------------------


async def test_batch_splits_by_fifty_and_keeps_the_order() -> None:
    fake = seeded()
    log = RequestLog()
    w = writer(fake, log)
    calls = [
        BatchCall(
            "POST",
            CAL,
            body={"summary": f"n{i}", "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"}},
        )
        for i in range(120)
    ]
    replies = await w.batch(calls)
    assert fake.batches == 3 and log.count("google-batch") == 3 and BATCH_SIZE == 50
    assert [r.body["summary"] for r in replies] == [f"n{i}" for i in range(120)]
    assert all(r.ok for r in replies)


async def test_a_failing_call_is_only_its_own_reply() -> None:
    fake = seeded()
    w = writer(fake)
    live = await w.get(CAL, "e1")
    replies = await w.batch(
        [
            BatchCall("PATCH", CAL, "e1", {"summary": "ok"}, etag=live["etag"]),
            BatchCall("PATCH", CAL, "e1", {"summary": "stale"}, etag='"0"'),
            BatchCall("DELETE", CAL, "nope"),
        ]
    )
    assert [r.status for r in replies] == [200, 412, 404]
    assert replies[0].ok and not replies[1].ok and replies[1].reason() == "conditionNotMet"
    assert replies[2].gone
    assert fake.event(CAL, "e1")["summary"] == "ok"


async def test_an_insert_resent_after_a_lost_answer_makes_one_event() -> None:
    fake = seeded()
    lost = {"done": False}

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handler(request)
        if request.method == "POST" and not lost["done"]:
            lost["done"] = True  # Google made it; the answer never arrives
            raise httpx.ReadTimeout("lost", request=request)
        return response

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler))
    made = await GoogleWriter(client, StaticToken("t"), base_url=BASE_URL).insert(
        CAL, {"summary": "Once", "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"}}
    )
    assert made["summary"] == "Once"
    assert [e["summary"] for e in fake.events(CAL)].count("Once") == 1


async def test_a_batch_insert_whose_id_exists_counts_as_made() -> None:
    fake = seeded()
    w = writer(fake)
    body = {
        "id": "abcdef12345",
        "summary": "x",
        "start": {"date": "2026-11-01"},
        "end": {"date": "2026-11-02"},
    }
    first = await w.batch([BatchCall("POST", CAL, body=body)])
    again = await w.batch([BatchCall("POST", CAL, body=body)])
    assert first[0].ok and again[0].exists
