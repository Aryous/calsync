import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from calsync.errors import AuthError, CalsyncError, ConfigError
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
