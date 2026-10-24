import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from calsync.errors import AuthError, CalsyncError, ConfigError, TransientError
from calsync.http import RequestLog, make_client
from calsync.model import Alarm
from calsync.sources import google as google_mod
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import (
    RefreshTokenProvider,
    StaticToken,
    load_client_secret,
    parse_refresh_token,
)
from tests.factories import google_event
from tests.fakes.google import BASE_URL, FakeGoogle

pytestmark = pytest.mark.anyio
CAL = "family@group.calendar.google.com"


def make_source(fake: FakeGoogle, log: RequestLog) -> GoogleSource:
    client = make_client(log, transport=fake.transport())
    return GoogleSource(client, StaticToken("t"), base_url=BASE_URL)


def seeded() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(CAL, "Family", default_reminders=[{"method": "popup", "minutes": 10}])
    fake.put_event(CAL, google_event("e1", "One", "2026-10-25T09:00:00+08:00", "2026-10-25T09:05:00+08:00"))
    fake.put_event(CAL, google_event("e2", "Two", "2026-10-26T09:00:00+08:00", "2026-10-26T09:05:00+08:00"))
    return fake


async def test_full_then_idle_then_delta() -> None:
    fake, log = seeded(), RequestLog()
    source = make_source(fake, log)

    first = await source.pull(["Family"], {})
    pull = first.calendars["Family"]
    assert pull.calendar_id == CAL and pull.full
    assert sorted(e["id"] for e in pull.upserts) == ["e1", "e2"]
    assert pull.meta.default_alarms == (Alarm(timedelta(minutes=-10)),)
    assert log.kinds() == ["google-calendarlist", "google-events"]

    mark = log.mark()
    idle = await source.pull(["Family"], first.state)
    assert idle.calendars["Family"].upserts == [] and not idle.calendars["Family"].full
    assert idle.calendars["Family"].meta.default_alarms == (Alarm(timedelta(minutes=-10)),)
    assert [e.kind for e in log.since(mark)] == ["google-calendarlist", "google-events"]

    fake.put_event(
        CAL, google_event("e1", "One (moved)", "2026-10-25T10:00:00+08:00", "2026-10-25T10:05:00+08:00")
    )
    fake.cancel_event(CAL, "e2")
    delta = (await source.pull([CAL], idle.state)).calendars[CAL]
    assert [e["summary"] for e in delta.upserts] == ["One (moved)"]
    assert delta.deletes == ["e2"]


async def test_expired_token_triggers_full_resync() -> None:
    fake = seeded()
    source = make_source(fake, RequestLog())
    state = (await source.pull([CAL], {})).state
    fake.expire_tokens()
    again = (await source.pull([CAL], state)).calendars[CAL]
    assert again.full and len(again.upserts) == 2


async def test_pagination() -> None:
    fake = seeded()
    fake.page_size = 1
    log = RequestLog()
    pull = (await make_source(fake, log).pull([CAL], {})).calendars[CAL]
    assert len(pull.upserts) == 2
    assert log.count("google-events") == 2


async def test_missing_sync_token_in_fields_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        google_mod, "EVENTS_LIST_PARAMS", {**google_mod.EVENTS_LIST_PARAMS, "fields": "items(id)"}
    )
    with pytest.raises(CalsyncError, match="nextSyncToken"):
        await make_source(seeded(), RequestLog()).pull([CAL], {})


async def test_unknown_calendar_name() -> None:
    with pytest.raises(ConfigError, match="not found"):
        await make_source(seeded(), RequestLog()).pull(["Nope"], {})


async def test_list_calendars_and_to_series() -> None:
    source = make_source(seeded(), RequestLog())
    assert [(c.id, c.name) for c in await source.list_calendars()] == [(CAL, "Family")]
    pull = (await source.pull([CAL], {})).calendars[CAL]
    assert sorted(s.title for s in source.to_series(pull.upserts, pull.meta)) == ["One", "Two"]


async def test_unauthorized_maps_to_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid"})

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler))
    with pytest.raises(AuthError):
        await GoogleSource(client, StaticToken("t"), base_url=BASE_URL).list_calendars()


async def test_refresh_token_provider_caches_until_near_expiry() -> None:
    now = {"t": 1000.0}
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        calls.append(form)
        return httpx.Response(200, json={"access_token": f"a{len(calls)}", "expires_in": 3600})

    log = RequestLog()
    provider = RefreshTokenProvider(
        make_client(log, transport=httpx.MockTransport(handler)),
        client_id="cid",
        client_secret="cs",
        refresh_token="rt",
        clock=lambda: now["t"],
    )
    assert await provider() == "a1"
    now["t"] += 3000
    assert await provider() == "a1"
    now["t"] += 400  # within 5 minutes of expiry
    assert await provider() == "a2"
    assert calls[0]["grant_type"] == "refresh_token" and calls[0]["refresh_token"] == "rt"
    assert log.count("google-token") == 2


async def test_refresh_token_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    provider = RefreshTokenProvider(
        make_client(RequestLog(), transport=httpx.MockTransport(handler)),
        client_id="c",
        client_secret="s",
        refresh_token="r",
    )
    with pytest.raises(AuthError, match="invalid_grant"):
        await provider()


def test_client_secret_and_credential_parsing(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"installed": {"client_id": "id1", "client_secret": "sec1"}}))
    assert load_client_secret(path) == ("id1", "sec1")
    with pytest.raises(ConfigError):
        load_client_secret(tmp_path / "missing.json")
    assert parse_refresh_token('{"refresh_token": "rt"}') == "rt"
    with pytest.raises(AuthError):
        parse_refresh_token("not json")


# ----- review fixes ---------------------------------------------------------
AUTH = {"Authorization": "Bearer t"}


def error_body(reason: str | None = None, status: str | None = None) -> dict:
    error: dict = {"code": 403, "message": "nope"}
    if reason:
        error["errors"] = [{"reason": reason, "domain": "usageLimits"}]
    if status:
        error["status"] = status
    return {"error": error}


def forbidden_source(body: object) -> GoogleSource:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=body)

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler))
    return GoogleSource(client, StaticToken("t"), base_url=BASE_URL)


@pytest.mark.parametrize(
    "reason", ["rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"]
)
async def test_403_rate_limit_reasons_are_transient(reason: str) -> None:
    with pytest.raises(TransientError):
        await forbidden_source(error_body(reason)).list_calendars()


@pytest.mark.parametrize("status", ["rateLimitExceeded", "RESOURCE_EXHAUSTED"])
async def test_403_rate_limit_via_error_status_is_transient(status: str) -> None:
    with pytest.raises(TransientError):
        await forbidden_source(error_body(status=status)).list_calendars()


@pytest.mark.parametrize("body", [error_body("forbidden"), error_body(), {"nope": 1}, "plain"])
async def test_403_other_reasons_stay_auth_errors(body: object) -> None:
    with pytest.raises(AuthError):
        await forbidden_source(body).list_calendars()


def refreshing_setup(api_ok_token: str, token_calls: list[int]):
    fake = seeded()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            token_calls.append(1)
            return httpx.Response(200, json={"access_token": f"a{len(token_calls)}", "expires_in": 3600})
        if request.headers.get("authorization") != f"Bearer {api_ok_token}":
            return httpx.Response(401, json={"error": "expired"})
        return fake.handler(request)

    log = RequestLog()
    client = make_client(log, transport=httpx.MockTransport(handler))
    provider = RefreshTokenProvider(client, client_id="c", client_secret="s", refresh_token="r")
    return GoogleSource(client, provider, base_url=BASE_URL), log


async def test_401_refreshes_token_and_retries_once() -> None:
    calls: list[int] = []
    source, log = refreshing_setup("a2", calls)
    assert [c.id for c in await source.list_calendars()] == [CAL]
    assert len(calls) == 2  # initial token plus exactly one refresh
    assert log.count("google-calendarlist") == 2


async def test_persistent_401_raises_after_one_retry() -> None:
    calls: list[int] = []
    source, log = refreshing_setup("never", calls)
    with pytest.raises(AuthError):
        await source.list_calendars()
    assert len(calls) == 2
    assert log.count("google-calendarlist") == 2


async def test_static_token_401_is_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid"})

    log = RequestLog()
    client = make_client(log, transport=httpx.MockTransport(handler))
    with pytest.raises(AuthError):
        await GoogleSource(client, StaticToken("t"), base_url=BASE_URL).list_calendars()
    assert log.count() == 1  # a static token cannot be refreshed, so no retry


async def test_refresh_provider_default_clock_is_wall_clock() -> None:
    import time

    provider = RefreshTokenProvider(
        make_client(RequestLog(), transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        client_id="c",
        client_secret="s",
        refresh_token="r",
    )
    assert provider._clock is time.time  # pyright: ignore[reportPrivateUsage]


async def test_token_response_without_access_token_is_auth_error() -> None:
    provider = RefreshTokenProvider(
        make_client(RequestLog(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
        client_id="c",
        client_secret="s",
        refresh_token="r",
    )
    with pytest.raises(AuthError, match="access_token"):
        await provider()


async def test_non_json_success_body_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>captive portal</html>")

    client = make_client(RequestLog(), transport=httpx.MockTransport(handler))
    with pytest.raises(TransientError):
        await GoogleSource(client, StaticToken("t"), base_url=BASE_URL).list_calendars()


async def fake_get(fake: FakeGoogle, path: str, **params: str) -> httpx.Response:
    client = make_client(RequestLog(), transport=fake.transport())
    return await client.get(f"{BASE_URL}{path}", params=params, headers=AUTH)


@pytest.mark.parametrize("extra", ["timeMin", "timeMax", "updatedMin"])
async def test_fake_rejects_sync_token_with_time_filters(extra: str) -> None:
    fake = seeded()
    token = (await fake_get(fake, f"/calendars/{CAL}/events")).json()["nextSyncToken"]
    response = await fake_get(
        fake, f"/calendars/{CAL}/events", syncToken=token, **{extra: "2026-01-01T00:00:00Z"}
    )
    assert response.status_code == 400


async def test_fake_rejects_changed_params_with_sync_token() -> None:
    fake = seeded()
    path = f"/calendars/{CAL}/events"
    token = (await fake_get(fake, path, maxResults="10")).json()["nextSyncToken"]
    assert (await fake_get(fake, path, maxResults="10", syncToken=token)).status_code == 200
    assert (await fake_get(fake, path, maxResults="11", syncToken=token)).status_code == 400
    assert (
        await fake_get(fake, path, showDeleted="true", maxResults="10", syncToken=token)
    ).status_code == 400


async def test_fake_rejects_malformed_sync_token() -> None:
    fake = seeded()
    for bad in ["", "garbage", "g-x", "gx-1-2", "g0-1"]:
        response = await fake_get(fake, f"/calendars/{CAL}/events", syncToken=bad)
        assert response.status_code == 400, bad


async def test_fake_page_token_is_not_part_of_the_params_fingerprint() -> None:
    fake = seeded()
    fake.page_size = 1
    path = f"/calendars/{CAL}/events"
    first = (await fake_get(fake, path)).json()
    last = (await fake_get(fake, path, pageToken=first["nextPageToken"])).json()
    again = await fake_get(fake, path, syncToken=last["nextSyncToken"])
    assert again.status_code == 200 and again.json()["items"] == []


async def test_removed_calendar_is_dropped_from_meta_and_listing() -> None:
    fake = seeded()
    fake.add_calendar("other@x", "Other")
    source = make_source(fake, RequestLog())
    state = (await source.pull([CAL], {})).state
    assert set(state["meta"]) == {CAL, "other@x"}
    fake.remove_calendar("other@x")
    state = (await source.pull([CAL], state)).state
    assert set(state["meta"]) == {CAL}
    assert [c.id for c in await source.list_calendars()] == [CAL]


async def test_fake_incremental_listing_reports_deleted_calendars() -> None:
    fake = seeded()
    token = (await fake_get(fake, "/users/me/calendarList")).json()["nextSyncToken"]
    fake.remove_calendar(CAL)
    items = (await fake_get(fake, "/users/me/calendarList", syncToken=token)).json()["items"]
    assert items == [{"id": CAL, "deleted": True}]
    assert (await fake_get(fake, "/users/me/calendarList")).json()["items"] == []


async def test_summary_override_wins_for_names_and_matching() -> None:
    fake = FakeGoogle()
    fake.add_calendar(CAL, "Family", summary_override="Home")
    fake.add_calendar("w@x", "Work")
    source = make_source(fake, RequestLog())
    assert [(c.id, c.name) for c in await source.list_calendars()] == [(CAL, "Home"), ("w@x", "Work")]
    assert (await source.pull(["Home"], {})).calendars["Home"].calendar_id == CAL
    assert (await source.pull(["Family"], {})).calendars["Family"].calendar_id == CAL


async def test_summary_override_is_matched_before_summary() -> None:
    fake = FakeGoogle()
    fake.add_calendar("a@x", "Work", summary_override="Misc")
    fake.add_calendar("b@x", "Misc", summary_override="Work")
    pull = await make_source(fake, RequestLog()).pull(["Work"], {})
    assert pull.calendars["Work"].calendar_id == "b@x"


async def test_cancel_event_payload_is_minimal_like_google() -> None:
    fake = seeded()
    fake.put_event(
        CAL,
        google_event(
            "e1_20261027",
            "inst",
            "2026-10-27T09:00:00+08:00",
            "2026-10-27T09:05:00+08:00",
            recurringEventId="e1",
            originalStartTime={"dateTime": "2026-10-27T09:00:00+08:00"},
        ),
    )
    fake.cancel_event(CAL, "e2")
    fake.cancel_event(CAL, "e1_20261027")
    items = {e["id"]: e for e in (await fake_get(fake, f"/calendars/{CAL}/events")).json()["items"]}
    assert items["e2"] == {"id": "e2", "status": "cancelled"}
    assert items["e1_20261027"] == {
        "id": "e1_20261027",
        "status": "cancelled",
        "recurringEventId": "e1",
        "originalStartTime": {"dateTime": "2026-10-27T09:00:00+08:00"},
    }


async def test_default_reminder_change_flows_through_incremental_pull() -> None:
    fake = seeded()
    source = make_source(fake, RequestLog())
    first = await source.pull([CAL], {})
    fake.set_default_reminders(CAL, [{"method": "popup", "minutes": 30}])
    second = await source.pull([CAL], first.state)
    assert second.calendars[CAL].meta.default_alarms == (Alarm(timedelta(minutes=-30)),)
    assert not second.calendars[CAL].full


async def test_fake_sync_token_is_per_calendar() -> None:
    fake = seeded()
    fake.add_calendar("other@x", "Other")
    token = (await fake_get(fake, f"/calendars/{CAL}/events")).json()["nextSyncToken"]
    assert (await fake_get(fake, f"/calendars/{CAL}/events", syncToken=token)).status_code == 200
    assert (await fake_get(fake, "/calendars/other@x/events", syncToken=token)).status_code == 400


def test_invalidate_reports_whether_a_fresh_token_is_possible() -> None:
    assert StaticToken("t").invalidate() is False
    provider = RefreshTokenProvider(
        make_client(RequestLog(), transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        client_id="c",
        client_secret="s",
        refresh_token="r",
    )
    assert provider.invalidate() is True
