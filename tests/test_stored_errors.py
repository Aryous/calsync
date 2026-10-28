"""Stored errors (``RuleResult.error``, ``run.error``) are shown by ``status``, ``runs``, the API
and the GUI: ``"<ExceptionType>: <plain sentence>"`` from the error class and config names only,
never the exception's text (request URLs, hrefs, calendar IDs). ``run.error_account`` names the
account whose adapter or setup failed."""

import logging
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from calsync.cycle import run_cycle
from calsync.errors import AuthError, ConflictError, TransientError
from calsync.http import RequestLog, make_client
from calsync.sources.google import GoogleSource
from calsync.sources.google_auth import StaticToken
from calsync.views import StateReader, build_status
from calsync.wiring import Adapters
from tests.factories import google_event
from tests.fakes.google import BASE_URL, FakeGoogle
from tests.harness import GCAL, NOW, FakeHarness, build_fake, config_for, rule

pytestmark = pytest.mark.anyio

URL = "https://p01-caldav.example.com/123456/calendars/home/calsync-0123abcd-0123456789abcdef01234567.ics"
GOOGLE_URL = "https://www.googleapis.com/calendar/v3/calendars/someone%40example.com/events"


def seeded() -> FakeGoogle:
    fake = FakeGoogle()
    fake.add_calendar(GCAL, "Family")
    for i in range(3):
        fake.put_event(
            GCAL,
            google_event(
                f"e{i}", f"Event {i}", f"2026-10-2{i + 5}T10:00:00+08:00", f"2026-10-2{i + 5}T11:00:00+08:00"
            ),
        )
    return fake


def assert_plain(text: str | None) -> None:
    assert text is not None
    for leak in ("http", "/calendars/", ".ics", "example.com", "%40", "@", "/dav/home/"):
        assert leak not in text, (leak, text)


def stored_runs(h: FakeHarness) -> list[tuple[str, str | None, str | None, str | None]]:
    return h.store._db.execute(
        "SELECT rule, error, error_kind, error_account FROM run ORDER BY id"
    ).fetchall()


class Failing:
    """A source whose pull fails account-wide with ``exc``."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def list_calendars(self) -> list[Any]:
        raise self.exc

    async def pull(self, refs: Any, state: Any) -> Any:
        raise self.exc

    def to_series(self, raw: Any, meta: Any) -> list[Any]:
        return []

    def series_ids(self, raw: Any) -> set[str]:
        return set()


async def test_source_failure_is_stored_without_its_text(tmp_path: Path) -> None:
    h = build_fake(
        tmp_path,
        seeded(),
        config_for([rule()]),
        sources={"me": Failing(TransientError(f"GET {GOOGLE_URL} timed out"))},
    )
    res = (await h.engine.cycle()).rules["family"]
    assert res.error == 'TransientError: the source of rule "family" could not be reached'
    assert (res.error_kind, res.error_account) == ("other", "me")
    assert stored_runs(h) == [("family", res.error, "other", "me")]


async def test_source_calendar_404_is_stored_without_the_calendar_id(tmp_path: Path) -> None:
    """An ID the account cannot see: the 404's text is the request path with the address in it."""
    h = build_fake(tmp_path, seeded(), config_for([rule(source="someone@example.com")]))
    res = (await h.engine.cycle()).rules["family"]
    assert res.status == "failed"
    assert res.error == 'NotFoundError: the source of rule "family" was not found'
    assert res.error_account == "me"
    assert_plain(stored_runs(h)[0][1])


async def test_target_failure_is_stored_without_the_url(tmp_path: Path) -> None:
    h = build_fake(tmp_path, seeded(), config_for([rule()]))
    await h.engine.cycle()
    h.target.fail_changes["Family"] = TransientError(f"REPORT {URL} timed out")
    res = (await h.engine.cycle()).rules["family"]
    assert res.error == 'TransientError: the target of rule "family" could not be reached'
    assert res.error_account == "dav"
    assert stored_runs(h)[-1] == ("family", res.error, "other", "dav")


async def test_write_failures_are_stored_without_hrefs(tmp_path: Path) -> None:
    h = build_fake(tmp_path, seeded(), config_for([rule()]))

    def refuse(op: str, href: str) -> None:
        raise ConflictError(f"412 {op} {href}")

    h.target.before_write = refuse
    res = (await h.engine.cycle()).rules["family"]
    assert (res.status, res.failed) == ("failed", 3)
    assert res.error == 'ConflictError: the target of rule "family" changed during a write (+2 more)'
    assert res.error_account == "dav"
    assert_plain(stored_runs(h)[0][1])


async def test_auth_failure_names_the_account(tmp_path: Path) -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": f"revoked for {GOOGLE_URL}"})

    log = RequestLog()
    broken = GoogleSource(
        make_client(log, transport=httpx.MockTransport(deny)), StaticToken("t"), base_url=BASE_URL
    )
    bad = {**rule("bad"), "source": {"account": "broken", "calendar": "Family"}}
    h = build_fake(
        tmp_path, seeded(), config_for([rule(), bad], extra_google=True), sources={"broken": broken}
    )
    result = await h.engine.cycle()
    res = result.rules["bad"]
    assert res.error == 'AuthError: account "broken" was rejected'
    assert (res.error_kind, res.error_account) == ("auth", "broken")
    assert result.rules["family"].status == "ok"
    rows = {r[0]: r for r in stored_runs(h)}
    assert rows["bad"] == ("bad", res.error, "auth", "broken") and rows["family"][3] is None


async def test_auth_attention_uses_the_failing_account(tmp_path: Path) -> None:
    """``status`` names the account to reconnect from ``run.error_account``, not from the text."""

    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "revoked"})

    log = RequestLog()
    broken = GoogleSource(
        make_client(log, transport=httpx.MockTransport(deny)), StaticToken("t"), base_url=BASE_URL
    )
    config = config_for(
        [{**rule("bad"), "source": {"account": "broken", "calendar": "Family"}}], extra_google=True
    )
    h = build_fake(tmp_path, seeded(), config, sources={"broken": broken})
    await h.engine.cycle()
    with StateReader(tmp_path / "state.db") as reader:
        (item,) = build_status(config, reader, NOW).to_json()["attention"]
    assert (item["account"], item["command"]) == ("broken", "calsync auth broken")


async def test_setup_errors_are_stored_without_their_text(tmp_path: Path) -> None:
    config = config_for([rule()])
    h = build_fake(tmp_path, seeded(), config)
    adapters = Adapters(
        RequestLog(), errors={"dav": AuthError("credential file not found: /secret/place/dav")}
    )
    result = await run_cycle(h.engine, h.store, config, adapters, None, dry_run=False, allow_deletes=False)
    res = result.rules["family"]
    assert res.error == 'AuthError: account "dav" has no usable credential'
    assert (res.error_kind, res.error_account) == ("auth", "dav")
    assert stored_runs(h) == [("family", res.error, "auth", "dav")]


async def test_missing_target_calendar_is_a_not_found_error(tmp_path: Path) -> None:
    no_create = {**rule(), "target": {"account": "dav", "calendar": "Nowhere"}}
    h = build_fake(tmp_path, seeded(), config_for([no_create]))
    res = (await h.engine.cycle()).rules["family"]
    assert res.error == 'NotFoundError: the target of rule "family" was not found'
    assert res.error_account == "dav"


async def test_full_text_only_through_debug_details(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALSYNC_DEBUG", "1")
    h = build_fake(tmp_path, seeded(), config_for([rule()]))
    await h.engine.cycle()
    h.target.fail_changes["Family"] = TransientError(f"REPORT {URL} timed out")
    with caplog.at_level(logging.DEBUG, logger="calsync"):
        await h.engine.cycle()
    plain = [r for r in caplog.records if r.exc_info is None]
    assert plain and all(URL not in r.getMessage() for r in plain)
    detailed = [r for r in caplog.records if r.exc_info is not None]
    assert any(URL in str(r.exc_info[1]) for r in detailed if r.exc_info)  # the traceback keeps it


def test_old_rows_keep_working(tmp_path: Path) -> None:
    """A run row from before this change (no error_account) still reads."""
    from calsync.store import RunRecord, Store

    with Store(tmp_path / "state.db") as store:
        store.record_runs(
            [RunRecord("family", NOW, NOW, "failed", 0, 0, 0, 0, 1, "AuthError: x", error_kind="auth")]
        )
    db = sqlite3.connect(tmp_path / "state.db")
    assert db.execute("SELECT error_account FROM run").fetchone() == (None,)
    db.close()
